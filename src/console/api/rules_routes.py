"""規則檢視與參數覆寫。

可改的只有四個數值旋鈕（`enabled` / `static_floor` / `factor` /
`cooldown_minutes`，new_source 規則是 `min_events`）。SQL、entity、baseline_key、
stat、population 一律唯讀 —— SQL 是 injection 面，而改 baseline_key 沒有重跑
calibrate 會憑空生出假倍數（見 CLAUDE.md 的「分桶與基線粒度必須成對」）。

覆寫存在 SQLite、engine 每個 tick 重讀，所以**改完下一個 tick 就生效、不必重啟**。
`applies_at` / `restart_required` 由這裡告訴前端，前端不可自己推斷 ——
猜錯的症狀是使用者以為改好了而檢查還在用舊值，完全沒有錯誤訊息。

**這些端點不得呼叫 ClickHouse**（純 SQLite + YAML，所以 async def 是對的）。
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException

from console.api import allowlist_view, validate
from console.auth.roles import CurrentUser, current_user, guard
from console.core import timewin
from console.core.config import settings
from console.rules import effective
from console.rules.loader import RuleConfigError, load_rules
from console.store import allowlist, audit, db, rule_overrides, rule_suppressions

router = APIRouter()

_LABELS = {
    "enabled": "啟用",
    "static_floor": "絕對下限",
    "factor": "基線倍率",
    "cooldown_minutes": "通知冷卻（分鐘）",
    "min_events": "最少事件數",
}

# 覆寫值的合法範圍。上限不是形式主義 —— 它擋的是「多打一個 0」
# （cooldown 從 60 打成 600 等於把那條規則靜音 10 小時，畫面上完全看不出來）。
_STATIC_RANGES = {
    "static_floor": (0.000001, 1e9),
    "factor": (0.1, 1000.0),
    "min_events": (1.0, 1e9),
}


def _range_for(field: str) -> tuple[float, float]:
    if field == "cooldown_minutes":
        # 下限是一個 tick：0 或負值會讓 store/events.py 的 escalate 條件永遠成立，
        # 於是每五分鐘對每個進行中的事件重發一次「持續中」通知。
        return float(settings()["time"]["tick_minutes"]), 1440.0
    return _STATIC_RANGES[field]


def _rules() -> tuple:
    try:
        return load_rules()
    except RuleConfigError as exc:
        # YAML 壞掉不是 500 —— 畫面要看得到原因，而不是一片白
        raise HTTPException(503, f"規則檔載入失敗：{exc}") from exc


def _find(rule_id: str):
    rule = next((r for r in _rules() if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(404, f"沒有規則 {rule_id}")
    return rule


def _formula(rule) -> str:
    t = rule.threshold
    if t is not None:
        return f"max({t.static_floor:g}, 同時段 {t.stat.upper()}×{t.factor:g})"
    return "首見即告警" if rule.kind == "new_source" else "新鮮度檢查"


def _public(rule, *, override: dict | None, last_triggered: str | None,
            suppressed: int, allowlist_count: int) -> dict:
    """規則清單的一列。**既有鍵名不可改** —— web/pages/events.js 讀 id/name 餵下拉。"""
    t = rule.threshold
    yaml_rule = next((r for r in _rules() if r.id == rule.id), rule)
    return {
        "id": rule.id, "name": rule.name, "severity": rule.severity,
        "source": rule.source, "kind": rule.kind,
        "window_minutes": rule.window_minutes, "enabled": rule.enabled,
        "formula": _formula(rule),
        "static_floor": t.static_floor if t else rule.min_events,
        "cooldown_minutes": rule.cooldown_minutes,
        "last_triggered": last_triggered,
        "note": rule.note,
        # 唯讀但要看得到的
        "stat": t.stat if t else None,
        "factor": t.factor if t else None,
        "baseline_key": t.baseline_key if t else None,
        "population": t.population if t else False,
        "off_hours_only": rule.off_hours_only,
        "known_kind": rule.known_kind,
        "min_events": rule.min_events,
        # SQL 端的預篩門檻。畫面上必須是可見的事實，否則「調低絕對下限」
        # 會是一個完全無效卻毫無回饋的操作（見 Rule.sql_floor）。
        "sql_floor": rule.sql_floor,
        # 編輯面
        "editable": list(effective.editable_fields(rule)),
        # 有來源 IP 或有可縮小的維度（endpoint）都算 —— R04 只有 endpoint，
        # 而它是 GetProfile 大量呼叫最主要的告警來源。
        "allowlistable": allowlist.allowlistable(rule),
        "allowlist_dimensions": list(allowlist.dimensions(rule)),
        "yaml": effective.yaml_values(yaml_rule),
        "overridden": sorted(f for f in override or {} if not f.startswith("_")),
        "override": None if not override else {
            "updated_at": override.get("_updated_at"),
            "updated_by": override.get("_updated_by"),
            "reason": override.get("_reason"),
        },
        "suppressed_28d": suppressed,
        "allowlist_count": allowlist_count,
    }


@router.get("/rules")
async def list_rules(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_rules")
    # 一趟 GROUP BY，不是每條規則一次查詢
    last = {r["rule_id"]: r["t"] for r in db.rows(
        "SELECT rule_id, max(last_seen) AS t FROM events GROUP BY rule_id")}
    al_counts = {r["rule_id"]: r["n"] for r in db.rows(
        "SELECT rule_id, COUNT(*) AS n FROM allowlist"
        " WHERE rule_id IS NOT NULL AND status = '生效中' GROUP BY rule_id")}
    overrides = rule_overrides.all_overrides()
    suppr = rule_suppressions.counts_by_rule(28)
    rules = effective.effective_rules()
    return {
        "rules": [_public(r, override=overrides.get(r.id),
                          last_triggered=last.get(r.id),
                          suppressed=suppr.get(r.id, 0),
                          allowlist_count=al_counts.get(r.id, 0))
                  for r in rules],
        # 停用的規則要在畫面上被看見。少了這個數字，「16 條規則有 3 條被停用」
        # 就只有進到這一頁的人知道 —— 那是靜靜的盲區。
        "disabled": [r.id for r in rules if not r.enabled],
        "overridden": sorted(overrides),
        "suppression_measured_since": rule_suppressions.measured_since(),
    }


@router.get("/rules/{rule_id}")
async def rule_detail(rule_id: str,
                      user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_rules")
    return _detail(rule_id)


def _detail(rule_id: str) -> dict:
    _find(rule_id)                                   # 404 檢查
    live = next(r for r in effective.effective_rules() if r.id == rule_id)
    override = rule_overrides.all_overrides().get(rule_id)
    last = db.one("SELECT max(last_seen) AS t FROM events WHERE rule_id = ?", (rule_id,))
    # 一定要經 allowlist_view：前端的 AllowlistChip 依 effective 決定顏色與文案，
    # 給它 raw row 會把一筆生效中的條目顯示成「不生效」。
    al = allowlist_view.public_rows(
        db.rows("SELECT * FROM allowlist WHERE (rule_id = ? OR rule_id IS NULL)"
                " AND status = ? ORDER BY rule_id IS NULL, id DESC",
                (rule_id, allowlist.STATUS_ACTIVE)),
        rules={r.id: r for r in load_rules()},
        suppressions=rule_suppressions.counts_by_entry(7))
    suppr = rule_suppressions.counts_by_rule(28)
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=28))
    stats = db.one(
        "SELECT COUNT(*) AS n, SUM(judgement = '誤報') AS false_positives"
        " FROM events WHERE rule_id = ? AND first_seen >= ?", (rule_id, since))
    return {
        "rule": {
            **_public(live, override=override, last_triggered=last["t"] if last else None,
                      suppressed=suppr.get(rule_id, 0),
                      allowlist_count=sum(1 for a in al if a["rule_id"] == rule_id)),
            "sql": live.sql,
            "entity": [{"col": f.col, "fp": f.fp} for f in live.entity],
            "ratio": None if live.ratio is None else {
                "den_col": live.ratio.den_col, "min_ratio": live.ratio.min_ratio},
        },
        "stats": {
            "events_28d": (stats or {}).get("n") or 0,
            "false_positives_28d": (stats or {}).get("false_positives") or 0,
        },
        "allowlist": al,
        "suppression": {
            "count_28d": suppr.get(rule_id, 0),
            "measured_since": rule_suppressions.measured_since(),
            "rows": rule_suppressions.recent_for_rule(rule_id),
        },
        # 前端不可自己推斷。覆寫在 SQLite、engine 每個 tick 重讀 → 立即生效；
        # 要改 SQL 或 baseline_key 才需要動 YAML 並重啟。
        "restart_required": False,
        "applies_at": _next_tick_text(),
    }


def _next_tick_text() -> str:
    tick = settings()["time"]["tick_minutes"]
    nxt = timewin.align_tick(timewin.taipei_now(), tick) + timedelta(minutes=tick)
    return f"下一次五分鐘檢查（約 {nxt.strftime('%H:%M')}）"


@router.patch("/rules/{rule_id}")
async def patch_rule(rule_id: str, payload: dict = Body(...),
                     user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "edit_rules")
    rule = _find(rule_id)
    allowed = set(effective.editable_fields(rule))
    validate.reject_unknown_keys(payload, allowed | {"reason"})
    reason = validate.require_text(payload, ("reason",), {"reason": "變更理由"})["reason"]

    values: dict[str, object] = {}
    for field in allowed:
        if field not in payload:
            continue
        if payload[field] is None:                    # 顯式清掉這一欄的覆寫
            values[field] = None
        elif field == "enabled":
            values[field] = validate.boolean(payload, field, _LABELS[field])
        else:
            lo, hi = _range_for(field)
            value = validate.number(payload, field, _LABELS[field], lo=lo, hi=hi)
            # cooldown 是分鐘數，落盤與稽核訊息都不該出現 30.0
            values[field] = int(value) if field == "cooldown_minutes" else value
    if not values:
        raise HTTPException(400, f"至少要指定一個欄位（可改：{'、'.join(sorted(allowed))}）")

    _reject_below_sql_floor(rule, values)
    before = effective.yaml_values(rule) | {
        f: v for f, v in (rule_overrides.all_overrides().get(rule_id) or {}).items()
        if not f.startswith("_")}
    # 等於 YAML 原值的欄位改成 None（= 清掉覆寫），見 effective.prune
    rule_overrides.put(rule_id, effective.prune(rule_id, values),
                       who=user.email, reason=reason)

    changed = [{"field": f, "from": before.get(f), "to": v} for f, v in values.items()]
    # before→after 一定要進 target：audit_log 沒有 diff 欄位，不寫進去就永遠
    # 查不到改了什麼。
    summary = "、".join(
        f"{_LABELS.get(c['field'], c['field'])} {c['from']}→"
        f"{'（還原為 YAML）' if c['to'] is None else c['to']}" for c in changed)
    audit.record(who=user.email, role=user.role_label,
                 action="調整規則參數", target=f"{rule_id}：{summary}",
                 reason=reason, query_text=json.dumps(values, sort_keys=True, default=str))
    return {"ok": True, "changed": changed, **_detail(rule_id),
            "note": "已寫入操作稽核。本系統不會回溯重算已產生的事件。"}


def _reject_below_sql_floor(rule, values: dict) -> None:
    """絕對下限不得低於 SQL 的 HAVING 字面值。

    ClickHouse 端就先濾掉低於它的列了。不擋的話，UI 顯示新值、
    events.threshold 記新值、而**命中數完全不變** —— 使用者的結論會是
    「調低門檻也沒有更多告警，所以真的沒事」。
    """
    floor = values.get("static_floor")
    if floor is None or rule.sql_floor is None or floor >= rule.sql_floor:
        return
    raise HTTPException(
        400, f"絕對下限不能低於 {rule.sql_floor:g}：這條規則的 SQL 含 "
             f"`HAVING metric >= {rule.sql_floor:g}` 的預篩，低於它的對象在 "
             f"ClickHouse 端就被濾掉了，設成 {floor:g} 不會讓它更靈敏。"
             f"要改預篩必須改 config/rules/ 的 SQL 並重啟 server。")


@router.delete("/rules/{rule_id}/override")
async def delete_override(rule_id: str, payload: dict = Body(default={}),
                          user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "edit_rules")
    _find(rule_id)
    validate.reject_unknown_keys(payload, {"reason"})
    reason = validate.require_text(payload, ("reason",), {"reason": "還原理由"})["reason"]
    before = rule_overrides.all_overrides().get(rule_id) or {}
    if not before:
        raise HTTPException(409, f"{rule_id} 目前沒有覆寫")
    if not rule_overrides.delete(rule_id):
        raise HTTPException(409, f"{rule_id} 目前沒有覆寫")
    fields = "、".join(f"{_LABELS.get(f, f)}={v}" for f, v in before.items()
                      if not f.startswith("_"))
    audit.record(who=user.email, role=user.role_label,
                 action="還原規則參數", target=f"{rule_id}：清除 {fields}", reason=reason)
    return {"ok": True, **_detail(rule_id),
            "note": "已還原為 YAML 的預設值，並寫入操作稽核。"}


@router.get("/rules/{rule_id}/whatif")
async def whatif(rule_id: str, static_floor: float | None = None,
                 user: CurrentUser = Depends(current_user)) -> dict:
    """把門檻改成這樣的話，近 28 天有幾筆事件會偵測不到。

    以 events 表裡的 metric 回算，**不是重跑規則** —— 被 allowlist 抑制或
    未達 SQL 預篩的對象本來就不在 events 裡。沒有這句話，「0 筆」會被讀成
    「調高門檻沒有代價」。
    """
    guard(user, "view_rules")
    _find(rule_id)
    if static_floor is None:
        raise HTTPException(400, "需要 static_floor 參數")
    if not (0 < static_floor < 1e9):
        raise HTTPException(400, "static_floor 超出合理範圍")
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=28))
    missed = db.rows(
        "SELECT evt_no, metric_value, threshold, first_seen, judgement FROM events"
        " WHERE rule_id = ? AND first_seen >= ? AND metric_value < ?"
        " ORDER BY metric_value DESC LIMIT 20",
        (rule_id, since, static_floor))
    total = db.one(
        "SELECT COUNT(*) AS n FROM events WHERE rule_id = ? AND first_seen >= ?"
        " AND metric_value < ?", (rule_id, since, static_floor))
    return {
        "static_floor": static_floor,
        "window_days": 28,
        "would_miss_count": (total or {}).get("n") or 0,
        "would_miss": missed,
        "note": "以 events 表中近 28 天的 metric 回算，不是重跑規則 —— "
                "被 Allowlist 抑制或未達 SQL 預篩的對象本來就不在 events 裡，"
                "所以這個數字是下限。",
    }
