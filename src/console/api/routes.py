"""API routes。所有回應皆為遮罩後資料；權限於 server 端強制。"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from console.auth import ros
from console.auth.roles import CurrentUser, PERMISSIONS, current_user, guard
from console.core import brands, timewin
from console.core.ch import ChQueryError
from console.core.config import settings
from console.queries import explorer, health, quick_templates, trends
from console.rules.loader import load_rules
from console.store import audit, db

logger = logging.getLogger(__name__)
router = APIRouter()

_SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ─────────────────────────── 會話與導覽 ───────────────────────────

@router.get("/session")
async def session(user: CurrentUser = Depends(current_user)) -> dict:
    return {
        "email": user.email,
        "name": user.name,
        "role": user.role_name,
        "role_label": user.role_label,
        "permissions": user.allowed_permissions(),
        "all_permissions": sorted(PERMISSIONS),
        "env_label": settings()["app"]["env_label"],
        "timezone": settings()["time"]["timezone"],
        # dev = 未接 ROS 的本機模式，前端據此顯示角色切換鈕與警示
        "auth_source": user.source,
        "ros_role_name": user.ros_role_name,
        "logout_url": f"{ros.base_url()}/api/auth/signout" if ros.enabled() else None,
    }


# ─────────────────────────── 資安總覽 ───────────────────────────

@router.get("/overview")
async def overview(
    minutes: int = Query(60, ge=10, le=1440),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_overview")
    started = time.time()
    cards = health.source_health()
    fresh = health.freshness_summary(cards)

    counts = {s: 0 for s in _SEV_ORDER}
    ongoing = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, status, COUNT(*) AS n FROM events"
            " WHERE first_seen >= ? GROUP BY severity, status",
            (timewin.fmt(timewin.taipei_now() - timedelta(hours=24)),)):
        counts[row["severity"]] = counts.get(row["severity"], 0) + row["n"]
        if row["status"] == "active":
            ongoing[row["severity"]] = ongoing.get(row["severity"], 0) + row["n"]

    prev = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, COUNT(*) AS n FROM events"
            " WHERE first_seen >= ? AND first_seen < ? GROUP BY severity",
            (timewin.fmt(timewin.taipei_now() - timedelta(hours=48)),
             timewin.fmt(timewin.taipei_now() - timedelta(hours=24)))):
        prev[row["severity"]] = row["n"]

    attention = db.rows(
        "SELECT * FROM events WHERE status = 'active' AND severity IN ('P0','P1','P2')"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,"
        " metric_value DESC LIMIT 5")

    hb = db.one("SELECT * FROM heartbeat WHERE key = 'five_min'")
    daily = db.one("SELECT * FROM heartbeat WHERE key = 'daily'")
    monitor_status = _monitor_status(hb, fresh)

    return {
        "severity_cards": [
            {"severity": s, "count": counts.get(s, 0),
             "diff": counts.get(s, 0) - prev.get(s, 0),
             "ongoing": ongoing.get(s, 0)} for s in ("P0", "P1", "P2", "P3")],
        "monitor": monitor_status,
        "last_five_min_check": hb["last_ok"] if hb else None,
        "last_daily_check": daily["last_ok"] if daily else None,
        "trend": trends.request_trend(minutes=minutes),
        "attention": [_event_public(e) for e in attention],
        "health": cards,
        "freshness": fresh,
        "rankings": trends.risk_rankings(minutes=minutes),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _monitor_status(hb: dict | None, fresh: dict) -> dict:
    if hb is None:
        return {"label": "尚未執行", "color": "#98A2B3",
                "note": "五分鐘檢查尚未執行過，目前無法判定是否沒有異常。"}
    if hb["consecutive_failures"] >= 1:
        return {"label": "監測失敗", "color": "#B42318",
                "note": f"五分鐘檢查連續失敗 {hb['consecutive_failures']} 次"
                        f"（{hb['note'] or '未知原因'}），現在無法判定是否沒有異常。"}
    if fresh["failed"]:
        return {"label": "部分失敗", "color": "#B42318",
                "note": f"{'、'.join(fresh['failed'])} 查詢失敗，結果不完整。"}
    if fresh["delayed"]:
        return {"label": "部分延遲", "color": "#DC6803",
                "note": fresh["banner"]}
    if hb["note"]:
        return {"label": "部分規則失敗", "color": "#DC6803", "note": hb["note"]}
    return {"label": "正常", "color": "#027A48", "note": ""}


# ─────────────────────────── 異常事件 ───────────────────────────

def _event_public(e: dict) -> dict:
    ctx = json.loads(e["context_json"]) if e.get("context_json") else {}
    return {
        "evt_no": e["evt_no"], "rule_id": e["rule_id"], "rule_name": e["rule_name"],
        "severity": e["severity"], "entity_label": e["entity_label"],
        "source": e["source_key"], "metric": e["metric_value"],
        "threshold": e["threshold"], "median": e["baseline_median"],
        "p95": e["baseline_p95"], "multiple": e["multiple"], "brands": e["brands"],
        # 「涉及品牌 N 個」的展開明細（偵測當下算好存進 context，見 rules/engine.py）
        "brand_top": ctx.get("brand_top") or [],
        "first_seen": e["first_seen"], "last_seen": e["last_seen"],
        "peak": e["peak_value"], "hit_count": e["hit_count"],
        "status": e["status"], "judgement": e["judgement"],
        "case_no": e["case_id"], "owner": e["owner"], "context": ctx,
    }


@router.get("/events")
async def list_events(
    severity: str | None = None,
    status: str | None = None,
    rule_id: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    hours: int = Query(168, ge=1, le=24 * 90),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_events")
    clauses = ["first_seen >= ?"]
    params: list = [timewin.fmt(timewin.taipei_now() - timedelta(hours=hours))]
    for col, val in (("severity", severity), ("status", status),
                     ("rule_id", rule_id), ("source_key", source)):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if keyword:
        clauses.append("(entity_label LIKE ? OR rule_name LIKE ? OR evt_no LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    rows = db.rows(
        f"SELECT * FROM events WHERE {' AND '.join(clauses)}"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1"
        " WHEN 'P2' THEN 2 ELSE 3 END, first_seen DESC LIMIT 300", tuple(params))
    stats = {s: sum(1 for r in rows if r["severity"] == s) for s in _SEV_ORDER}
    return {
        "events": [_event_public(r) for r in rows],
        "total": len(rows),
        "by_severity": stats,
        "ongoing": sum(1 for r in rows if r["status"] == "active"),
    }


@router.get("/events/{evt_no}")
async def event_detail(evt_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_events")
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    audit.record(who=user.email, role=user.role_label, action="查看事件", target=evt_no)
    event = _event_public(row)
    rule = next((r for r in load_rules() if r.id == row["rule_id"]), None)
    event["rule_note"] = rule.note if rule else ""
    event["evidence"] = _build_evidence(row, event)
    event["limitations"] = _extra_limitations(event) + _data_limitations(row["source_key"])
    event["trend"] = _event_trend(row)
    return event


def _build_evidence(row: dict, event: dict) -> dict:
    """證據矩陣：支持攻擊 / 支持正常（設計稿 9.4，皆由實際數據導出）。"""
    attack, normal = [], []
    ctx = event["context"]
    if event["multiple"] and event["multiple"] >= 2:
        attack.append(f"目前值為該對象歷史同時段 median 的 {event['multiple']}× "
                      f"（{event['metric']:.0f} vs {event['median']:.0f}）")
    elif ctx.get("baseline_note"):
        attack.append(f"目前值 {event['metric']:.0f} 超出同時段同類對象的高分位門檻"
                      f"（{event['threshold']:.0f}）")
    if event["threshold"] and not ctx.get("baseline_note"):
        attack.append(f"超過動態門檻 {event['threshold']:.0f}"
                      f"（max(靜態地板, 同時段基線倍數)）")
    if ctx.get("uniq_routes") is not None and ctx["uniq_routes"] <= 3:
        attack.append(f"行為集中於 {ctx['uniq_routes']} 個路由，符合逐筆遍歷特徵")
    if event["brands"] and event["brands"] > 10:
        attack.append(f"涉及 {event['brands']} 個品牌，範圍異常廣{_brand_examples(event)}")
    if event["hit_count"] > 3:
        attack.append(f"連續 {event['hit_count']} 個檢查視窗持續命中")

    if event["p95"] and event["metric"] < event["p95"] * 3:
        normal.append(f"未超過歷史 P95（{event['p95']:.0f}）的三倍，可能為尖峰時段自然波動")
    if event["status"] == "resolved":
        normal.append("事件已回落至基線，未持續擴大")
    if event["hit_count"] <= 2:
        normal.append(f"僅 {event['hit_count']} 個視窗命中，可能為短時批次作業")
    if ctx.get("uniq_routes") and ctx["uniq_routes"] > 8:
        normal.append(f"操作分散於 {ctx['uniq_routes']} 個路由，較接近正常瀏覽行為")
    if event["brands"] == 1:
        only = event["brand_top"][0]["label"] if event["brand_top"] else None
        normal.append(f"僅涉及單一品牌{f'（{only}）' if only else ''}，符合單一整合來源特徵")
    return {"attack": attack, "normal": normal}


def _brand_examples(event: dict, n: int = 3) -> str:
    """證據要能自己站得住，所以把最大的幾個品牌寫進句子；完整前十名在上方展開。"""
    top = event["brand_top"][:n]
    if not top:
        return ""
    listed = "、".join(f"{b['label']} {b['count']:,} 次" for b in top)
    return f"，最多的是 {listed}"


def _extra_limitations(event: dict) -> list[str]:
    note = event["context"].get("baseline_note")
    return [note] if note else []


def _data_limitations(source_key: str) -> list[str]:
    common = ["目前缺少 device fingerprint，無法確認請求是否來自同一批裝置。",
              "缺少 response bytes 與 row count，因此不可推論「外洩 N 筆資料」。"]
    per_source = {
        "admin": ["Admin Log 部分登入紀錄沒有 IP，顯示「來源 IP 不可用」，"
                  "此類紀錄無法納入單一來源判斷。"],
        "api": ["API Log 的來源 IP 由 forwarded header 推導，屬「未驗證來源」，"
                "不可作為可信來源證據。",
                "API Log 的 params 大量不是合法 JSON，無法一律展開比對。"],
        "backend": ["Backend System Log 歷史資料可能重複，已以事件 ID 去重後顯示。"],
        "auth": ["Auth Log 為最高敏感等級，僅能提供遮罩摘要。"],
    }
    return per_source.get(source_key, []) + common


def _event_trend(row: dict) -> dict:
    """事件時間序列：以事件視窗前後各 30 分鐘、10 分鐘分桶。"""
    from console.core.ch import query
    from console.queries import exprs
    from console.rules import baseline
    table = settings()["data_sources"].get(row["source_key"], {}).get("table")
    if table is None:
        return {"rows": [], "note": "此規則跨多個資料來源，不提供單一趨勢圖。"}
    start = timewin.parse(row["first_seen"]) - timedelta(minutes=30)
    end = timewin.parse(row["last_seen"]) + timedelta(minutes=30)
    try:
        df = query(
            f"SELECT toStartOfTenMinutes(create_time) AS b, count() AS cnt FROM {table}"
            f" WHERE {exprs.time_filter()} GROUP BY b ORDER BY b",
            {"start": timewin.fmt(start), "end": timewin.fmt(end)})
    except ChQueryError as exc:
        return {"rows": [], "note": f"趨勢查詢失敗：{exc}"}
    rows = []
    for _, r in df.iterrows():
        bt = r["b"].to_pydatetime()
        base = baseline.get(f"table_10m:{row['source_key']}", hour=bt.hour,
                            day_class=baseline.day_class_of(bt))
        rows.append({"bucket": bt.strftime("%m/%d %H:%M"), "count": int(r["cnt"]),
                     "median": round(base.median) if base else None,
                     "p95": round(base.p95) if base else None})
    return {"rows": rows, "note": "此為該資料來源的整體流量趨勢（10 分鐘分桶），"
                                  "非僅該異常對象的請求量。"}


@router.post("/events/{evt_no}/judge")
async def judge_event(
    evt_no: str,
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "judge_event")
    valid = ("已確認攻擊", "合法整合", "誤報", "證據不足", "保持觀察")
    judgement = payload.get("judgement")
    if judgement not in valid:
        raise HTTPException(400, f"判定必須是 {valid} 之一")
    for field in ("reason", "evidence", "next_step"):
        if not str(payload.get(field, "")).strip():
            raise HTTPException(400, "判定理由、主要證據、下一步或處置皆為必填")
    note = json.dumps({k: payload[k] for k in ("reason", "evidence", "next_step")},
                      ensure_ascii=False)
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE events SET judgement = ?, judgement_note = ?, owner = ? WHERE evt_no = ?",
            (judgement, note, user.email, evt_no))
        if cur.rowcount == 0:
            raise HTTPException(404, f"找不到事件 {evt_no}")
    audit.record(who=user.email, role=user.role_label, action="變更事件狀態",
                 target=f"{evt_no}：判定為 {judgement}", reason=payload["reason"])
    return {"ok": True, "judgement": judgement,
            "note": "本系統不會執行任何自動封鎖、停權或 token 撤銷；"
                    "後續處置請於案件中記錄。"}


# ─────────────────────────── Log Explorer ───────────────────────────

@router.post("/explorer")
async def run_explorer(
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "use_explorer")
    analysis = payload.get("analysis", "trend")
    f = explorer.ExplorerFilter(
        source=payload.get("source", "api"),
        start=payload.get("start", ""), end=payload.get("end", ""),
        brand=payload.get("brand"), endpoint=payload.get("endpoint"),
        only_error=bool(payload.get("only_error")),
        limit=int(payload.get("limit", 500)),
    )
    started = time.time()
    try:
        if analysis == "trend":
            data = explorer.trend(f, payload.get("bucket", "10m"))
        elif analysis in ("endpoint", "brand", "source", "actor"):
            data = explorer.ranking(f, analysis)
        elif analysis == "error":
            data = explorer.error_analysis(f)
        elif analysis == "unique_resource":
            data = explorer.unique_resource(f)
        elif analysis == "detail":
            guard(user, "view_masked_detail")
            data = explorer.detail(f)
        else:
            raise HTTPException(400, f"未知分析方式 {analysis!r}")
    except explorer.FilterError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)
    rng = f"{f.start} ~ {f.end}"
    qh = audit.record(who=user.email, role=user.role_label, action="Log Explorer 查詢",
                      target=f"{f.source}/{analysis}", query_text=json.dumps(payload, sort_keys=True),
                      time_range=rng, row_count=data.get("returned") or data.get("total"),
                      duration_ms=elapsed)
    return {**data, "meta": {
        "elapsed_ms": elapsed, "time_range": rng, "query_hash": qh,
        "brand_filter": brands.label(f.brand) if f.brand is not None else None,
        "dedup": "以事件 ID（_id）去重", "timezone": "Asia/Taipei",
        "data_latest": health.freshness_summary(health.source_health())["latest"],
    }}


# ─────────────────────────── 快速查詢 ───────────────────────────

@router.get("/quick")
async def quick_catalog(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_quick")
    return {"categories": quick_templates.catalog()}


@router.post("/quick/{template_id}")
async def quick_run(
    template_id: str,
    payload: dict = Body(default={}),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_quick")
    tpl = quick_templates.BY_ID.get(template_id)
    if tpl is None:
        raise HTTPException(404, f"找不到模板 {template_id}")
    started = time.time()
    try:
        data = tpl.run(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)
    qh = audit.record(who=user.email, role=user.role_label, action="執行模板",
                      target=tpl.name, query_text=f"{template_id}:{json.dumps(payload, sort_keys=True)}",
                      time_range=data.get("time_range"), row_count=len(data.get("rows", [])),
                      duration_ms=elapsed)
    return {**data, "template": {"id": tpl.id, "name": tpl.name, "source": tpl.source},
            "meta": {"elapsed_ms": elapsed, "query_hash": qh}}


# ─────────────────────────── 資料健康 ───────────────────────────

@router.get("/health")
async def data_health(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_health")
    cards = health.source_health()
    hb = db.one("SELECT * FROM heartbeat WHERE key = 'five_min'")
    daily = db.one("SELECT * FROM heartbeat WHERE key = 'daily'")
    return {
        "sources": cards,
        "freshness": health.freshness_summary(cards),
        "thresholds": settings()["freshness"],
        "heartbeat": {"five_min": hb, "daily": daily},
    }


# ─────────────────────────── 規則 ───────────────────────────

@router.get("/rules")
async def list_rules(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_events")
    out = []
    for r in load_rules():
        last = db.one("SELECT max(last_seen) AS t FROM events WHERE rule_id = ?", (r.id,))
        t = r.threshold
        out.append({
            "id": r.id, "name": r.name, "severity": r.severity, "source": r.source,
            "enabled": r.enabled, "kind": r.kind, "window_minutes": r.window_minutes,
            "formula": (f"max({t.static_floor:g}, 同時段 {t.stat.upper()}×{t.factor:g})"
                        if t else ("首見即告警" if r.kind == "new_source" else "新鮮度檢查")),
            "static_floor": t.static_floor if t else r.min_events,
            "cooldown_minutes": r.cooldown_minutes,
            "last_triggered": last["t"] if last else None,
            "note": r.note,
        })
    return {"rules": out}
