"""例外名單（Allowlist）的管理端點。

**這個功能的安全模型是「留痕 + 可見」，不是「阻止」。** `auth/roles.guard()`
是 no-op、沒有角色分級，四眼原則在產品內做不到；主控台在 VPC 內也拿不到操作者
的來源 IP，所以連「不准把自己的 IP 加進去」都檢查不了。

因此約束改由四件事承擔，缺一不可：
1. 必填負責人、用途、理由、**到期日** —— 會自己到期的抑制比任何核准流程有效。
2. 每次寫入進 audit_log，並發 Slack ops 訊息（唯一一個當事人改不掉的通道）。
3. 抑制可見化：掃描報告與規則頁列出「被哪一條例外遮掉了什麼」。
4. 沒有 DELETE，只有停用 —— audit_log.target 裡的 #id 必須永遠解得回一筆條目。

**這些端點不得呼叫 ClickHouse**（純 SQLite，所以 async def 是對的）。
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from console.alerting import notify
from console.api import allowlist_view, validate
from console.auth.roles import CurrentUser, current_user, guard
from console.core import timewin
from console.core.config import settings
from console.intel import classify
from console.intel import store as intel_store
from console.rules.loader import load_rules
from console.store import allowlist, audit, db, rule_suppressions

router = APIRouter()

# 必填的文字欄位。**valid_to 刻意不在裡面**（使用者決定）：留空 = 永不到期。
# 那讓「會自己到期」不再是一個保證，所以剩下的三件事更重要：
# 沒有到期日的條目在清單上有 warn pill、summary 有 no_expiry 計數、
# 資安總覽的橫幅會把它算進「目前有多少監測被關閉」。**可以永久，但不能安靜。**
#
# **`owner`（創立人）不是可寫欄位**（2026-08 使用者決定）。原本它是可填的
# 「負責人」、留空才帶登入帳號 —— 於是它可能是任何字串（實測播種列是
# 「Ocard 內部」，不是一個帳號），當不了「這筆核准是誰建的」的答案。
# 現在由 `store/allowlist.create()` 從登入帳號直接寫入、之後不可修改，
# 所以它也**不在 _CREATE_KEYS／_PATCH_KEYS 裡** —— 送 owner 進來是 400，
# 不是靜靜忽略。靜靜忽略的話前端可以顯示一個「已存」的值而資料庫是別的。
_REQUIRED_TEXTS = ("name", "purpose", "reason")
_TEXT_FIELDS = ("name", "purpose", "reason")
_LABELS = {"name": "名稱", "owner": "創立人", "purpose": "用途",
           "reason": "建立理由", "valid_to": "到期日", "source_ip": "來源 IP",
           "endpoint": "端點"}
_CREATE_KEYS = set(_TEXT_FIELDS) | {"source_ip", "rule_id", "endpoint",
                                    "valid_from", "valid_to"}
# source_ip 刻意不可修改：一筆條目是「對某個特定來源的核准紀錄」，就地改 IP
# 會讓 audit_log 裡「#12 核准了 1.2.3.4」在事後解讀成別的 IP。要換就停用 + 新增。
_PATCH_KEYS = set(_TEXT_FIELDS) | {"rule_id", "endpoint", "valid_from", "valid_to"}

_MAX_VALID_DAYS = 730          # 有填的話最多這麼久；留空 = 永不到期

# 進階篩選維度的呈現。label 依規則的資料來源而異 —— 同一個 endpoint 欄位在
# 三張表叫的名字不一樣（見 web/pages/explorer.js 的 endpointLabel）。
_ENDPOINT_LABEL = {"api": "Controller/Function", "backend": "Route（前 2 段）",
                   "admin": "Function/Action"}
_ENDPOINT_PLACEHOLDER = {"api": "Api2/GetProfile", "backend": "orderlist/detail",
                         "admin": "Boss_initial/auth_v2"}


def _rule_index() -> dict:
    return {r.id: r for r in load_rules()}


def _rule_public(rule) -> dict:
    """給範圍選單用：這條規則能不能被抑制、還能再用什麼縮小。"""
    dims = allowlist.dimensions(rule)
    return {
        "id": rule.id, "name": rule.name, "source": rule.source,
        "allowlistable": allowlist.allowlistable(rule),
        # 沒有來源 IP 維度的規則（R04 只有 endpoint）就不能要求填 IP
        "has_source": allowlist.has_source(rule),
        "filters": [
            {"key": "endpoint",
             "label": _ENDPOINT_LABEL.get(rule.source, "端點"),
             "placeholder": _ENDPOINT_PLACEHOLDER.get(rule.source, "")}
            for d in dims if d == "endpoint"
        ],
    }


def _check_rule_scope(rule_id: object) -> str | None:
    """規則範圍必須存在，而且該規則要真的有可抑制的對象維度。"""
    if rule_id in (None, "", "global"):
        return None
    rules = _rule_index()
    rid = str(rule_id)
    if rid not in rules:
        raise HTTPException(400, f"沒有規則 {rid}（可用：{'、'.join(sorted(rules))}）")
    if not allowlist.allowlistable(rules[rid]):
        cols = "、".join(f.col for f in rules[rid].entity) or "無"
        raise HTTPException(
            400, f"{rid}「{rules[rid].name}」的對象沒有可抑制的維度"
                 f"（entity：{cols}），Allowlist 對它不會有任何效果。"
                 f"要讓它安靜請去規則頁停用它 —— 停用會出現在資安總覽的橫幅上，"
                 f"一筆沒有作用的例外不會。")
    return rid


def _check_scope_targets(rid: str | None, ip: str, endpoint: str) -> None:
    """範圍與對象的組合必須真的能比對到東西。"""
    if rid is None:
        if not ip:
            raise HTTPException(
                400, "全域例外必須指定來源 IP。全域 + 只有端點等於「所有規則都不看"
                     "這個端點」，那個盲區太大 —— 請改成「只對某一條規則」。")
        return
    if not ip and not endpoint:
        raise HTTPException(
            400, f"只指定 {rid} 而不給來源 IP 或端點，等於「這條規則永不觸發」。"
                 f"要那樣請去規則頁停用它（停用會出現在資安總覽的橫幅上）。")
    rules = _rule_index()
    if endpoint and "endpoint" not in allowlist.dimensions(rules[rid]):
        raise HTTPException(
            400, f"{rid}「{rules[rid].name}」的對象不含端點欄位，"
                 f"填了端點這條例外永遠不會命中。請清空端點欄位。")
    if ip and not allowlist.has_source(rules[rid]):
        raise HTTPException(
            400, f"{rid}「{rules[rid].name}」的對象不含來源 IP"
                 f"（它以端點為單位聚合），填了 IP 這條例外永遠不會命中。"
                 f"請只填端點。")


def _check_period(valid_from: object, valid_to: object) -> tuple[str, str | None]:
    """回傳 (valid_from, valid_to)。valid_to 留空 = 永不到期。"""
    now = timewin.taipei_now()
    start = (validate.bound(valid_from, "生效時間", end_of_day=False)
             if valid_from else timewin.fmt(now))
    if not valid_to:
        return start, None
    end = validate.bound(valid_to, _LABELS["valid_to"], end_of_day=True)
    end_dt = timewin.parse(end)
    if end_dt <= now:
        raise HTTPException(400, f"到期日 {end} 已經過去 —— 這樣建出來的是一筆"
                                 f"一出生就失效的條目。不需要到期日請留空。")
    if end_dt > now + timedelta(days=_MAX_VALID_DAYS):
        raise HTTPException(400, f"到期日最多 {_MAX_VALID_DAYS} 天後。"
                                 f"要永久生效請直接留空 —— 那會在清單與資安總覽上"
                                 f"標示為「永不到期」，而一個 9999 年的到期日不會。")
    if timewin.parse(start) >= end_dt:
        raise HTTPException(400, "生效時間必須早於到期時間")
    return start, end


@router.get("/allowlist")
async def list_allowlist(
    status: str | None = None,
    rule_id: str | None = None,
    scope: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_allowlist")
    if status and status not in allowlist.STATUSES:
        raise HTTPException(400, f"status 必須是 {'、'.join(allowlist.STATUSES)} 之一")
    if scope and scope not in ("all", "global", "rule"):
        raise HTTPException(400, "scope 必須是 all / global / rule")
    rows = allowlist.rows(status=status, rule_id=rule_id,
                          scope=None if scope == "all" else scope, q=q, limit=limit)
    entries = allowlist_view.public_rows(
        rows, rules=_rule_index(),
        suppressions=rule_suppressions.counts_by_entry(7))
    active_ids = {e.id for e in allowlist.active_entries()}
    soon = settings()["allowlist"]["expiring_soon_days"]
    all_rows = allowlist.rows(limit=500)
    return {
        "entries": entries,
        "summary": {
            "total": len(all_rows),
            "active": len(active_ids),
            "expiring_soon": sum(
                1 for e in entries
                if e["effective"] and e["days_to_expiry"] is not None
                and 0 <= e["days_to_expiry"] <= soon),
            "no_expiry": sum(1 for e in entries
                             if e["effective"] and e["expiry_missing"]),
            "disabled": sum(1 for r in all_rows
                            if r["status"] == allowlist.STATUS_DISABLED),
        },
        "expiring_soon_days": soon,
        # 範圍選單。allowlistable=False 的規則不該被選（選了也沒作用）；
        # filters 是「選了這條規則之後還能再用什麼縮小」。
        "rules": [_rule_public(r) for r in load_rules()],
        "suppression_measured_since": rule_suppressions.measured_since(),
        # 到期一律用後端的牆鐘，不用瀏覽器時鐘
        "now": timewin.fmt(timewin.taipei_now()),
    }


@router.post("/allowlist/preview")
async def preview(payload: dict = Body(...),
                  user: CurrentUser = Depends(current_user)) -> dict:
    """建立之前先說出後果：這條例外會遮掉什麼。不寫入任何東西。"""
    guard(user, "view_allowlist")
    validate.reject_unknown_keys(payload, {"source_ip", "rule_id", "endpoint"})
    ip = (validate.source_ip(payload["source_ip"])
          if str(payload.get("source_ip") or "").strip() else "")
    rid = _check_rule_scope(payload.get("rule_id"))
    endpoint = str(payload.get("endpoint") or "").strip()
    if not ip and not endpoint:
        # 預覽不擋（使用者可能還在打字），但也沒東西可算
        return {"source_ip": "", "rule_id": rid, "endpoint": "",
                "scope_note": "", "existing": [],
                "events_28d": {"count": 0, "by_severity": {}, "rows": []},
                "ip_intel": None, "intel_warning": None}

    existing = [r for r in allowlist.rows(status=allowlist.STATUS_ACTIVE, limit=200)
                if (r["source_ip"] or "") == ip
                and (r["endpoint"] or "") == endpoint]
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=28))
    # 條件逐項疊上去：IP 與端點都在 entity_key 裡（entity_key 是
    # f"{rule_id}|" + 各 entity 欄位值以 | 串起來）。
    sql = ("SELECT evt_no, rule_id, rule_name, severity, first_seen, entity_label"
           " FROM events WHERE first_seen >= ?")
    params: list[object] = [since]
    if ip:
        sql += " AND entity_key LIKE ?"
        params.append(f"%{ip}%")
    if endpoint:
        sql += " AND entity_key LIKE ?"
        params.append(f"%{endpoint}%")
    if rid:
        sql += " AND rule_id = ?"
        params.append(rid)
    events = db.rows(sql + " ORDER BY first_seen DESC LIMIT 50", tuple(params))
    by_sev: dict[str, int] = {}
    for e in events:
        by_sev[e["severity"]] = by_sev.get(e["severity"], 0) + 1

    intel = None
    if ip and intel_store.available():
        found = intel_store.lookup([ip]).get(ip)
        if found:
            # 標籤的唯一真相在 intel.classify，不要在這裡再定義一份
            intel = {**found,
                     "type_label": classify.LABELS.get(found["source_type"],
                                                       found["source_type"])}
    target = "、".join(x for x in (ip, endpoint) if x)
    scope_note = (
        f"只對 {rid} 生效的例外不影響期間異常掃描 —— 掃描不跑規則，"
        f"它只認全域例外。{target} 在掃描結果中仍會出現。" if rid else
        f"全域：{target} 命中 {len(load_rules())} 條規則中的任何一條都不會再產生"
        f"事件，期間異常掃描也會抑制它。")
    return {
        "source_ip": ip,
        "rule_id": rid,
        "endpoint": endpoint,
        "scope_note": scope_note,
        "existing": existing,
        "events_28d": {"count": len(events), "by_severity": by_sev, "rows": events[:20]},
        "ip_intel": intel,
        # 報告最強的單一訊號就是「真人不會從資料中心登入後台」
        "intel_warning": (
            None if not intel or intel["source_type"] not in ("hosting", "vpn", "forged")
            else f"此來源被判定為 {intel['type_label']}"
                 f"（{intel.get('org') or '未知業者'}）。「真人不會從資料中心登入後台」"
                 f"是本系統最強的單一訊號 —— 把它加入 Allowlist 等於關掉這個訊號。"),
    }


@router.post("/allowlist")
async def create_entry(payload: dict = Body(...),
                       user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "manage_allowlist")
    validate.reject_unknown_keys(payload, _CREATE_KEYS)
    texts = validate.require_text(payload, _REQUIRED_TEXTS, _LABELS)
    ip = (validate.source_ip(payload["source_ip"])
          if str(payload.get("source_ip") or "").strip() else "")
    rid = _check_rule_scope(payload.get("rule_id"))
    endpoint = str(payload.get("endpoint") or "").strip()
    _check_scope_targets(rid, ip, endpoint)
    valid_from, valid_to = _check_period(payload.get("valid_from"),
                                        payload.get("valid_to"))

    dup = allowlist.conflict(ip, rid, endpoint)
    if dup:
        raise HTTPException(409, {
            "code": "allowlist_duplicate",
            "message": f"{_target_text(ip, endpoint)} 在"
                       f"{'全域' if rid is None else rid}已有一筆生效中的條目"
                       f"「{dup['name']}」（#{dup['id']}）。再建一筆的話，"
                       f"停用其中任何一筆都不會解除抑制。",
            "existing_id": dup["id"], "existing_name": dup["name"],
        })

    # 創立人不在這裡傳 —— `create()` 從 who 寫入（見它的 docstring）
    entry_id = allowlist.create(
        {**texts, "source_ip": ip, "rule_id": rid,
         "endpoint": endpoint or None,
         "valid_from": valid_from, "valid_to": valid_to}, who=user.email)
    scope = "全域" if rid is None else rid
    target = f"#{entry_id} {_target_text(ip, endpoint)} · {scope}"
    period = f"{valid_from} ~ {valid_to or '永不到期'}"
    audit.record(who=user.email, role=user.role_label,
                 action="新增 Allowlist 例外", target=target,
                 reason=texts["reason"], time_range=period,
                 query_text=json.dumps({"ip": ip, "rule_id": rid,
                                        "endpoint": endpoint}, sort_keys=True))
    _notify_change("新增 Allowlist 例外", user, target, texts["reason"],
                   extra=f"有效期 {period}")

    warnings = []
    if rid is not None and ip and allowlist.conflict(ip, None):
        warnings.append("此 IP 已有全域例外，本條目不會有額外效果。")
    if not valid_to:
        warnings.append("這條例外沒有到期日 —— 它是永久的監測盲區，"
                        "會一直出現在清單與資安總覽的「永不到期」計數裡。")
    return {"ok": True, "entry": _one_public(entry_id), "warnings": warnings,
            "note": "已立即生效，並寫入操作稽核。"}


def _target_text(ip: str, endpoint: str) -> str:
    return " · ".join(x for x in (ip, endpoint) if x) or "（無對象）"


@router.patch("/allowlist/{entry_id}")
async def patch_entry(entry_id: int, payload: dict = Body(...),
                      user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "manage_allowlist")
    row = allowlist.get(entry_id)
    if row is None:
        raise HTTPException(404, f"沒有 Allowlist 條目 #{entry_id}")
    if "source_ip" in payload:
        raise HTTPException(
            400, "來源 IP 不可修改：一筆條目是「對某個特定來源的核准紀錄」，"
                 "就地改 IP 會讓稽核紀錄裡的 #id 事後指向別的 IP。"
                 "要換 IP 請停用這一筆並新增。")
    validate.reject_unknown_keys(payload, _PATCH_KEYS)
    texts = validate.require_text(payload, ("reason",), _LABELS)

    fields: dict[str, object] = {"reason": texts["reason"]}
    for f in ("name", "purpose"):
        if f in payload:
            fields[f] = validate.require_text(payload, (f,), _LABELS)[f]
    # owner（創立人）不可修改，也不在 _PATCH_KEYS 裡 —— 送進來已經是 400。
    if "rule_id" in payload:
        fields["rule_id"] = _check_rule_scope(payload["rule_id"])
    if "endpoint" in payload:
        fields["endpoint"] = str(payload["endpoint"] or "").strip() or None
    # valid_to 可以被清空（= 永不到期），所以用 `in payload` 判斷而不是真值。
    if "valid_to" in payload or "valid_from" in payload:
        vf, vt = _check_period(
            payload["valid_from"] if "valid_from" in payload else row["valid_from"],
            payload["valid_to"] if "valid_to" in payload else row["valid_to"])
        fields["valid_from"], fields["valid_to"] = vf, vt

    new_rule = fields.get("rule_id", row["rule_id"])
    new_endpoint = fields.get("endpoint", row["endpoint"]) or ""
    _check_scope_targets(new_rule, row["source_ip"] or "", new_endpoint)
    dup = allowlist.conflict(row["source_ip"] or "", new_rule, new_endpoint,
                             exclude_id=entry_id)
    if dup:
        raise HTTPException(409, {
            "code": "allowlist_duplicate",
            "message": f"{_target_text(row['source_ip'] or '', new_endpoint)} "
                       f"在該範圍已有生效中的條目 #{dup['id']}「{dup['name']}」。",
            "existing_id": dup["id"], "existing_name": dup["name"]})

    allowlist.update(entry_id, fields, who=user.email)
    diff = "、".join(f"{_LABELS.get(f, f)} {row.get(f)}→{v}"
                    for f, v in fields.items() if f != "reason" and row.get(f) != v)
    target = (f"#{entry_id} "
              f"{_target_text(row['source_ip'] or '', row['endpoint'] or '')}："
              f"{diff or '僅更新理由'}")
    audit.record(who=user.email, role=user.role_label,
                 action="修改 Allowlist 例外", target=target, reason=texts["reason"])
    _notify_change("修改 Allowlist 例外", user, target, texts["reason"])
    return {"ok": True, "entry": _one_public(entry_id), "note": "已寫入操作稽核。"}


@router.post("/allowlist/{entry_id}/disable")
async def disable_entry(entry_id: int, payload: dict = Body(...),
                        user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "manage_allowlist")
    row = _require(entry_id)
    validate.reject_unknown_keys(payload, {"reason"})
    reason = validate.require_text(payload, ("reason",), _LABELS)["reason"]
    if row["status"] == allowlist.STATUS_DISABLED:
        # 不用 200 + changed:false —— 那會讓畫面顯示「停用成功」而其實什麼都沒發生
        raise HTTPException(409, f"#{entry_id} 已經是停用狀態")
    allowlist.set_status(entry_id, allowlist.STATUS_DISABLED,
                         who=user.email, reason=reason)
    target = f"#{entry_id} {_target_text(row['source_ip'] or '', row['endpoint'] or '')}"
    audit.record(who=user.email, role=user.role_label,
                 action="停用 Allowlist 例外", target=target, reason=reason)
    _notify_change("停用 Allowlist 例外", user, target, reason)
    # 同一個對象可以有多筆條目（沒有唯一索引）。停用一筆而另一筆仍生效的話
    # 抑制**沒有解除**，而畫面上這一列變成「已停用」，看起來就像成功了。
    still = [r for r in allowlist.rows(status=allowlist.STATUS_ACTIVE, limit=200)
             if (r["source_ip"] or "") == (row["source_ip"] or "")
             and (r["endpoint"] or "") == (row["endpoint"] or "")]
    return {
        "ok": True, "entry": _one_public(entry_id),
        "still_suppressed_by": [{"id": r["id"], "name": r["name"],
                                 "scope": "global" if r["rule_id"] is None else r["rule_id"]}
                                for r in still],
        "note": "已寫入操作稽核。注意：此來源在抑制期間若曾被 R08A/B/C 判定過，"
                "known_sources 不會被回填 —— 停用之後它會重新被視為首見來源。",
    }


@router.post("/allowlist/{entry_id}/enable")
async def enable_entry(entry_id: int, payload: dict = Body(...),
                       user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "manage_allowlist")
    row = _require(entry_id)
    validate.reject_unknown_keys(payload, {"reason"})
    reason = validate.require_text(payload, ("reason",), _LABELS)["reason"]
    if row["status"] == allowlist.STATUS_ACTIVE:
        raise HTTPException(409, f"#{entry_id} 已經是生效中")
    # 沒有到期日是允許的（永不到期）；只有「已經過期」才要求先延期，
    # 否則恢復完會立刻又是失效狀態，畫面顯示「生效中」而它不生效。
    if row["valid_to"] and timewin.parse(row["valid_to"]) <= timewin.taipei_now():
        raise HTTPException(400, f"此條目已於 {row['valid_to']} 到期，"
                                 f"請先用「編輯」延長到期日或清空它（清空 = 永不到期）")
    allowlist.set_status(entry_id, allowlist.STATUS_ACTIVE, who=user.email, reason=reason)
    target = f"#{entry_id} {_target_text(row['source_ip'] or '', row['endpoint'] or '')}"
    audit.record(who=user.email, role=user.role_label,
                 action="恢復 Allowlist 例外", target=target, reason=reason)
    _notify_change("恢復 Allowlist 例外", user, target, reason)
    return {"ok": True, "entry": _one_public(entry_id), "note": "已寫入操作稽核。"}


@router.get("/allowlist/{entry_id}/suppressions")
async def entry_suppressions(entry_id: int, days: int = Query(28, ge=1, le=90),
                             limit: int = Query(100, ge=1, le=500),
                             user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_allowlist")
    _require(entry_id)
    rows = rule_suppressions.recent_for_entry(entry_id, days=days, limit=limit)
    by_rule: dict[str, int] = {}
    for r in rows:
        by_rule[r["rule_id"]] = by_rule.get(r["rule_id"], 0) + 1
    return {
        "rows": rows, "count": len(rows), "days": days, "by_rule": by_rule,
        # 表剛上線是空的。「0 次」必須渲染成「自 X 起沒有紀錄」而不是「從未抑制」
        "measured_since": rule_suppressions.measured_since(),
    }


def _require(entry_id: int) -> dict:
    row = allowlist.get(entry_id)
    if row is None:
        raise HTTPException(404, f"沒有 Allowlist 條目 #{entry_id}")
    return row


def _one_public(entry_id: int) -> dict:
    return allowlist_view.public_row(
        _require(entry_id), rules=_rule_index(),
        suppressions=rule_suppressions.counts_by_entry(7))


def _notify_change(action: str, user: CurrentUser, target: str, reason: str,
                   *, extra: str = "") -> None:
    """發 Slack ops 訊息。

    這是**唯一一個不在主控台裡、當事人改不掉的通道**。allowlist 是刻意製造的
    盲區，而沒有角色分級可以阻止任何人建立它 —— 所以偵測型控制得靠這裡。
    失敗不可擋住主要動作（條目已經寫進 DB 了）。
    """
    try:
        notify.send_ops_message(
            action,
            f"{target}\n操作者：{user.email}（{user.role_label}）\n理由：{reason}"
            + (f"\n{extra}" if extra else ""))
    except Exception:                                    # noqa: BLE001
        pass
