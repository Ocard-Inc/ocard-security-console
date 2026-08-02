"""API routes。所有回應皆為遮罩後資料；權限於 server 端強制。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from console.auth import ros
from console.auth.roles import CurrentUser, current_user, guard
from console.core import brands, timewin
from console.core.ch import ChConnectionError, ChQueryError
from console.core.config import settings
from console.queries import (
    brand_search, endpoint_suggest, explorer, health, quick_templates,
    sparklines, trends,
)
from console.rules.loader import load_rules
from console.store import audit, db, sweeps
from console.intel import store as intel_store
from console.sweep import narrate
from console.sweep import report as sweep_report

logger = logging.getLogger(__name__)
router = APIRouter()

_SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ─────────────────────────── 會話與導覽 ───────────────────────────

@router.get("/session")
async def session(user: CurrentUser = Depends(current_user)) -> dict:
    """目前登入者。沒有角色分級 —— 進得來就有全部功能。"""
    return {
        "email": user.email,
        "name": user.name,
        # 顯示 ROS 那邊的角色名稱（管理員、資訊主管…），不是主控台自己的等級
        "role_label": user.role_label,
        "env_label": settings()["app"]["env_label"],
        "timezone": settings()["time"]["timezone"],
        # dev = 未接 ROS 的離線模式，前端據此顯示警示
        "auth_source": user.source,
        "logout_url": f"{ros.base_url()}/api/auth/signout" if ros.enabled() else None,
        "ros_url": ros.base_url() if ros.enabled() else None,
    }


# ─────────────────────────── 資安總覽 ───────────────────────────

# 排名查詢的視窗上限。趨勢查詢跨四張表在 7 天視窗只要 0.5 秒（月分區 + part 級
# create_time 剪枝很有效），但 sources 排名要對 headers 做 JSONExtract，7 天要掃 19M 列
# 花 3.2 秒 —— 佔整個 /overview 的大半。排名夾在 24 小時，前端據 window_minutes 誠實標示。
RANKING_MAX_MINUTES = 1440


# 自訂區間的上限。四張表都有 create_time 範圍剪枝，但 sources 排名要對 headers
# 做 JSONExtract，區間拉太長會讓單一請求跑上好幾十秒。
MAX_CUSTOM_RANGE_DAYS = 31


def _parse_window(start: str | None, end: str | None) -> tuple:
    """自訂區間的共用解析與驗證。兩個都給才算數；只給一個視同沒給。"""
    if not start or not end:
        return None, None
    try:
        s, e = timewin.parse(start), timewin.parse(end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if s >= e:
        raise HTTPException(400, "開始時間必須早於結束時間")
    span_days = (e - s).total_seconds() / 86400
    if span_days > MAX_CUSTOM_RANGE_DAYS:
        raise HTTPException(
            400, f"自訂區間最長 {MAX_CUSTOM_RANGE_DAYS} 天，目前為 {span_days:.1f} 天")
    return s, e


@router.get("/overview")
async def overview(
    minutes: int = Query(60, ge=10, le=10080),
    # 自訂區間（台北牆鐘）。兩個都給時蓋過 minutes。
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_overview")
    started = time.time()
    win_start, win_end = _parse_window(start, end)
    if win_start is not None:
        minutes = max(int((win_end - win_start).total_seconds() // 60), 10)
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

    # 「已結束但從未判定」的積壓。事件會因為數值回到門檻以下而自動 resolved，
    # 但 attention 只查 status='active'，所以一旦自動結束就從首頁完全消失 ——
    # 首頁於是顯示「沒有未處理事件」，即使沒有任何人看過它們。那是假的安心感，
    # 跟本專案「沒有事件 ≠ 系統安全」的前提正好相反。
    # 不設時間下限：這是待辦積壓不是時間視窗。純 SQLite，零 ClickHouse 成本。
    pending_by_sev = {s: 0 for s in _SEV_ORDER}
    for row in db.rows(
            "SELECT severity, COUNT(*) AS n FROM events"
            " WHERE judgement IS NULL GROUP BY severity"):
        pending_by_sev[row["severity"]] = row["n"]
    pending_rows = db.rows(
        "SELECT * FROM events WHERE judgement IS NULL"
        " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1"
        " WHEN 'P2' THEN 2 ELSE 3 END, first_seen DESC LIMIT 5")
    oldest = db.one("SELECT MIN(first_seen) AS m FROM events WHERE judgement IS NULL")

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
        "trend": trends.request_trend(minutes=minutes, start=win_start, end=win_end),
        "attention": [_event_public(e) for e in attention],
        "pending_judgement": {
            "total": sum(pending_by_sev.values()),
            "by_severity": pending_by_sev,
            "oldest": oldest["m"] if oldest else None,
            "events": [_event_public(e) for e in pending_rows],
        },
        "health": cards,
        "freshness": fresh,
        "rankings": trends.risk_rankings(
            minutes=min(minutes, RANKING_MAX_MINUTES), end=win_end),
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
    unjudged: bool = False,
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
    # 首頁「待判定」的連結來源：已結束但沒人看過的事件
    if unjudged:
        clauses.append("judgement IS NULL")
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
async def event_detail(
    evt_no: str,
    # 趨勢圖往事件視窗前後各再拉多久。只看前後 30 分鐘看不出事件之前的脈絡。
    pad_minutes: int = Query(30, ge=10, le=10080),
    # 自訂絕對區間（台北牆鐘）。兩個都給時蓋過 pad_minutes。
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_events")
    row = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
    if row is None:
        raise HTTPException(404, f"找不到事件 {evt_no}")
    win_start, win_end = _parse_window(start, end)
    audit.record(who=user.email, role=user.role_label, action="查看事件", target=evt_no)
    event = _event_public(row)
    rule = next((r for r in load_rules() if r.id == row["rule_id"]), None)
    event["rule_note"] = rule.note if rule else ""
    event["evidence"] = _build_evidence(row, event)
    event["limitations"] = _extra_limitations(event) + _data_limitations(row["source_key"])
    event["trend"] = _event_trend(row, pad_minutes=pad_minutes,
                                  start=win_start, end=win_end)
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


def _brand_examples(event: dict) -> str:
    """證據要能自己站得住，所以把最大的幾個品牌寫進句子；完整前十名在上方展開。"""
    listed = brands.top_summary(event["brand_top"])
    return f"，最多的是 {listed}" if listed else ""


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


# 事件趨勢可以往事件視窗前後各再拉多久。分析師常需要看「事件之前長什麼樣」，
# 只給前後 30 分鐘看不出脈絡。必須與 web/pages/event-detail.js 的 PADS 一致。
EVENT_TREND_PADDINGS = (30, 180, 720, 1440, 2880)


def _event_trend(
    row: dict,
    pad_minutes: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """事件時間序列。

    給 start/end 就用絕對區間，否則以事件視窗前後各 pad_minutes 分鐘。
    分桶依總長自動選，start/end 一律對齊格線（見 trends.resolve_window 的說明）。
    """
    from console.core.ch import query
    from console.queries import exprs, trends
    from console.rules import baseline
    table = settings()["data_sources"].get(row["source_key"], {}).get("table")
    if table is None:
        return {"rows": [], "note": "此規則跨多個資料來源，不提供單一趨勢圖。"}

    if start is None or end is None:
        start = timewin.parse(row["first_seen"]) - timedelta(minutes=pad_minutes)
        end = timewin.parse(row["last_seen"]) + timedelta(minutes=pad_minutes)
    span = max(int((end - start).total_seconds() // 60), 1)
    bucket = trends.bucket_for(span)
    start, end, bucket = trends.resolve_window(start=start, end=end, bucket_minutes=bucket)
    try:
        df = query(
            f"SELECT toStartOfInterval(create_time, INTERVAL {bucket} MINUTE) AS b,"
            f" count() AS cnt FROM {table}"
            f" WHERE {exprs.time_filter()} GROUP BY b ORDER BY b",
            {"start": timewin.fmt(start), "end": timewin.fmt(end)})
    except ChQueryError as exc:
        return {"rows": [], "note": f"趨勢查詢失敗：{exc}"}

    counts = {timewin.fmt(r["b"].to_pydatetime()): int(r["cnt"]) for _, r in df.iterrows()}
    # 零填：沒有零填的話空桶會直接消失，而 category 軸依索引等距排列，
    # 安靜的時段會被壓縮成一段直線而不是凹下去。
    rows = []
    cursor = start
    while cursor < end:
        base = baseline.get(f"table_{bucket}m:{row['source_key']}", hour=cursor.hour,
                            day_class=baseline.day_class_of(cursor))
        rows.append({
            "bucket": cursor.strftime("%m/%d %H:%M"),
            "count": counts.get(timewin.fmt(cursor), 0),
            "median": round(base.median) if base else None,
            "p95": round(base.p95) if base else None,
        })
        cursor += timedelta(minutes=bucket)
    return {
        "rows": rows,
        "pad_minutes": pad_minutes,
        "bucket_minutes": bucket,
        "note": f"此為該資料來源的整體流量趨勢（{bucket} 分鐘分桶），非僅該異常對象的請求量。",
    }


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

def _explain_empty(f: explorer.ExplorerFilter) -> dict | None:
    """查詢回 0 筆時，說明是「這個對象不存在」還是「它不在你選的區間」。

    只在有下對象篩選時才問（多一趟查詢，實測回看 365 天約 0.1 秒）。
    沒下篩選就回 0 筆通常是區間太窄或該表沒資料，那個已經看得出來。
    """
    for field, value, label in (("source_ip", f.source_ip, "來源"),
                                ("actor", f.actor, "帳號")):
        if not value:
            continue
        try:
            extent = explorer.entity_extent(f.source, field, value)
        except ChQueryError:
            return None                      # 解釋失敗不該讓整個查詢失敗
        if extent is None:
            continue
        if not extent["found"]:
            return {
                "kind": "not_found", "field": field, "value": value,
                "message": f"回看 {extent['lookback_days']} 天內都找不到這個{label}"
                           f"（{value}）。請確認值是否正確 —— 注意比對是完全相等，"
                           "不是前綴。",
            }
        return {
            "kind": "outside_range", "field": field, "value": value,
            "first_seen": extent["first_seen"], "last_seen": extent["last_seen"],
            "total_in_lookback": extent["count"],
            "message": f"這個{label}（{value}）存在，但活動不在你選的區間內。"
                       f"它在近 {extent['lookback_days']} 天共有 "
                       f"{extent['count']:,} 筆，範圍是 {extent['first_seen']} ~ "
                       f"{extent['last_seen']}。把區間改到這段時間就看得到。",
        }
    return None


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
        # 依對象反查：把掃描結果或排名裡看到的帳號／IP 貼回來追明細
        source_ip=payload.get("source_ip"), actor=payload.get("actor"),
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
    # 「0 筆」要自己解釋原因。使用者從掃描結果貼一個對象進來，Explorer 的預設區間
    # 比掃描短得多（實測 192.168.97.1 最後出現在 7/29，而預設是最近 1 小時），
    # 只顯示空表格會讓人以為「這個對象不存在」。
    empty = not data.get("rows") and not data.get("total")
    if empty:
        data["empty_reason"] = _explain_empty(f)
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


@router.get("/brands")
async def search_brands(
    q: str = "",
    limit: int = Query(brand_search.DEFAULT_LIMIT, ge=1, le=brand_search.MAX_LIMIT),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """品牌選擇器的候選清單（Log Explorer 的品牌篩選欄位）。

    不記稽查：品牌名稱是營運資訊而非個資，而 debounce 打字會把 audit_log 洗版，
    稀釋真正該追的「Log Explorer 查詢」。實際的查詢行為在 POST /explorer 已記錄，
    meta.brand_filter 也會帶出最後選了哪個品牌。
    """
    guard(user, "use_explorer")
    try:
        return {"rows": brand_search.search(q, limit)}
    except ChQueryError as exc:
        # 不吞成空陣列 —— 空陣列在 UI 上等於「查無此品牌」，與查詢失敗是兩回事。
        # 同 core/brands.py 的「（查無品牌）」vs「（品牌名稱查詢失敗）」之分。
        raise HTTPException(502, f"品牌查詢失敗：{exc}") from exc


@router.get("/endpoints")
async def suggest_endpoints(
    source: str = "api",
    start: str = "",
    end: str = "",
    user: CurrentUser = Depends(current_user),
) -> dict:
    """該區間內的 endpoint 候選值，依呼叫量由高到低（Log Explorer 的建議選單）。

    一次回傳全部而非分頁：基數有界且小（實測最多約 600 種），前端過濾才能
    做到零延遲且完整 —— 只取 top N 會讓罕見的 endpoint 永遠找不到。
    不記稽查，理由同 GET /api/brands。
    """
    guard(user, "use_explorer")
    try:
        return endpoint_suggest.suggest(source, start, end)
    except explorer.FilterError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"endpoint 查詢失敗：{exc}") from exc


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


@router.get("/sparklines")
async def data_sparklines(user: CurrentUser = Depends(current_user)) -> dict:
    """統計卡的迷你趨勢線。刻意獨立於 /health —— source_health() 有三個呼叫端，
    只有一個要這份資料（詳見 queries/sparklines.py 的模組說明）。"""
    guard(user, "view_health")
    return sparklines.source_sparklines()


# 逐筆調閱完整 payload。
#
# 一般明細的 params 只給大小與欄位名稱（見 core/masking.payload_summary），因為
# 那些內容混著 authorization／cookie／secret，以及消費者的手機與 Email，而畫面、
# Slack、磁碟上的 log 都會沾到。但「要查的時候查不到」也不行 —— 所以保留這條路徑。
#
# 收斂的方式是「一次一筆、寫入 audit_log」，**不要求填理由**：這是對內的調查工具，
# 每次調閱都要打字說明只會讓人繞過它去直接查 DB，反而失去留痕。
# 稽核仍然記下誰、何時、哪一筆。
@router.post("/explorer/payload")
def explorer_payload(
    payload_body: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_raw_payload")
    source = str(payload_body.get("source") or "")
    row_id = str(payload_body.get("row_id") or "")
    try:
        data = explorer.payload(source, row_id)
    except explorer.FilterError as exc:
        # 失敗也要留痕：查不到什麼也是稽核事實
        audit.record(who=user.email, role=user.role_label, action="調閱完整 payload",
                     target=f"{source}/{row_id}", result="失敗", reason=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc

    audit.record(who=user.email, role=user.role_label, action="調閱完整 payload",
                 target=f"{source}/{row_id}", time_range=data.get("time"),
                 row_count=1)
    return data


# ─────────────────────────── 期間異常掃描 ───────────────────────────

# 掃描的區間上限與 Explorer 的 MAX_CUSTOM_RANGE_DAYS 刻意不同：Explorer 的限制來自
# 「排名要對 api_log 的 headers 做 JSONExtract」，而掃描的低成本探針全部走
# backend/admin/auth，實測 94 天的完整掃描牆鐘 1.9 秒。對齊稽查匯出的上限。
SWEEP_MAX_RANGE_DAYS = 92


def _sweep_window(payload: dict) -> tuple[datetime, datetime]:
    start_s, end_s = payload.get("start"), payload.get("end")
    if not start_s or not end_s:
        raise HTTPException(400, "必須指定 start 與 end（格式 YYYY-MM-DD[ HH:MM:SS]）")
    try:
        start, end = timewin.parse(start_s), timewin.parse(end_s)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if start >= end:
        raise HTTPException(400, "開始時間必須早於結束時間")
    span_days = (end - start).total_seconds() / 86400
    if span_days > SWEEP_MAX_RANGE_DAYS:
        raise HTTPException(
            400, f"掃描區間最長 {SWEEP_MAX_RANGE_DAYS} 天，目前為 {span_days:.1f} 天")
    return start, end


# 刻意用同步 def，不是 async def。裡面的 ClickHouse 查詢是阻塞的，寫成 async def
# 會佔住事件迴圈 —— 勾了 API 來源探針時單次要 30 秒，整台 server（含五分鐘檢查
# 排程與其他人的請求）都會卡住。同步的 path operation 由 FastAPI 丟進 threadpool。
@router.post("/sweep")
def run_sweep(
    payload: dict = Body(...),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "run_sweep")
    start, end = _sweep_window(payload)
    include_api = bool(payload.get("include_api_probe"))
    started = time.time()
    try:
        result = sweep_report.build(start, end, include_high_cost=include_api,
                                    intel_available=intel_store.available())
    except ChConnectionError as exc:
        raise HTTPException(503, f"ClickHouse 連線失敗：{exc}") from exc
    except ChQueryError as exc:
        raise HTTPException(502, f"查詢失敗：{exc}") from exc
    elapsed = int((time.time() - started) * 1000)

    sweep_no = sweeps.save(result, created_by=user.email, duration_ms=elapsed,
                           include_api_probe=include_api)
    audit.record(who=user.email, role=user.role_label, action="執行期間異常掃描",
                 target=sweep_no,
                 query_text=json.dumps({"start": timewin.fmt(start),
                                        "end": timewin.fmt(end),
                                        "include_api_probe": include_api},
                                       sort_keys=True),
                 time_range=f"{timewin.fmt(start)} ~ {timewin.fmt(end)}",
                 row_count=result["summary"]["findings"], duration_ms=elapsed)
    return {**result, "sweep_no": sweep_no, "duration_ms": elapsed,
            "include_api_probe": include_api}


@router.get("/sweep")
def list_sweeps(
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "run_sweep")
    return {"sweeps": sweeps.recent(limit),
            "max_range_days": SWEEP_MAX_RANGE_DAYS,
            "intel_available": intel_store.available()}


@router.get("/sweep/{sweep_no}")
def get_sweep(sweep_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "run_sweep")
    stored = sweeps.load(sweep_no)
    if stored is None:
        raise HTTPException(404, f"找不到掃描 {sweep_no}")
    audit.record(who=user.email, role=user.role_label, action="查看期間異常掃描",
                 target=sweep_no, row_count=len(stored["findings"]))
    return stored


# 敘事是獨立端點，不併進 /sweep：確定性的結果要能先上畫面，LLM 慢或掛掉
# 都不該讓整份報告產不出來。
@router.post("/sweep/{sweep_no}/narrate")
def narrate_sweep(sweep_no: str, user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "run_sweep")
    stored = sweeps.load(sweep_no)
    if stored is None:
        raise HTTPException(404, f"找不到掃描 {sweep_no}")
    started = time.time()
    result = narrate.write(stored)
    elapsed = int((time.time() - started) * 1000)
    if result.markdown:
        sweeps.save_narrative(sweep_no, result.markdown, result.model)
    audit.record(who=user.email, role=user.role_label, action="產生 AI 研判草稿",
                 target=sweep_no, duration_ms=elapsed,
                 result="成功" if result.markdown else "失敗",
                 reason=result.error)
    return {
        "sweep_no": sweep_no,
        "markdown": result.markdown,
        "model": result.model,
        "error": result.error,
        "disclaimer": narrate.DISCLAIMER,
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
