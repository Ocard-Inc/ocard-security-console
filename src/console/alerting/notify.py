"""通知調度：事件變化 → Slack（未設定 webhook 時僅記 log 與佇列）。

Slack 告警內容只含聚合數字與 fingerprint / endpoint / 品牌名稱（編號），
永不含原始 IP、帳號、token 或 log 原文。

品牌名稱在事件建立時就寫進 entity_label 與 context.brand_top（見 rules/engine.py），
因此 Slack 與 UI 看到的是同一組「品牌名稱（品牌編號）」，不需在此再查一次 MySQL。
UI 的「涉及品牌」可以點開看明細，Slack 不行，所以前十名直接列在訊息裡。
"""
from __future__ import annotations

import json
import logging

import requests

from console.core import brands, timewin
from console.core.config import settings, slack_webhook_url
from console.store import db

logger = logging.getLogger(__name__)

_SEV_EMOJI = {"P0": "🟥", "P1": "🔴", "P2": "🟠", "P3": "🔵"}


def base_url() -> str:
    return str(settings()["app"].get("base_url", "")).rstrip("/")


def event_url(evt_no: str) -> str:
    """事件詳細頁的深連結（前端 hash 路由，見 web/app.js）。"""
    root = base_url()
    return f"{root}/#/events/{evt_no}" if root else ""


def page_url(page: str) -> str:
    root = base_url()
    return f"{root}/#/{page}" if root else ""


def _format_event(kind: str, event: dict) -> str:
    sev = event["severity"]
    head = {"new": "新事件", "ongoing": "持續中", "resolved": "已恢復"}[kind]
    metric, peak = event["metric_value"], event["peak_value"]
    # baseline_median 為 None 代表該規則的基線是跨對象分布（見 rules/model.py），
    # 此時談「相對自身的倍數」沒有意義，只呈現門檻。
    if event.get("baseline_median"):
        compare = (f"門檻 {event['threshold'] or 0:,.0f}，"
                   f"同時段 median {event['baseline_median']:,.0f}，{event['multiple']}×")
    else:
        compare = f"門檻 {event['threshold'] or 0:,.0f} · 同類對象高分位"
    evt_no, url = event["evt_no"], event_url(event["evt_no"])
    # 標題直接是連結：Slack 的 <url|text> 語法，讓收到告警的人一鍵進事件詳細頁
    title = f"<{url}|{evt_no} {event['rule_name']}>" if url else f"{evt_no} {event['rule_name']}"
    lines = [
        f"{_SEV_EMOJI.get(sev, '')} *[{sev}] {head}｜{title}*",
        f"對象：`{event['entity_label']}`",
        f"目前值 *{metric:,.0f}*（{compare}）"
        + (f"，峰值 {peak:,.0f}" if peak > metric else ""),
        f"視窗：{event['first_seen']} ~ {event['last_seen']}（Asia/Taipei）",
    ]
    if event.get("brands"):
        lines.append(f"涉及品牌：{event['brands']} 個{_brand_detail(event)}")
    if kind == "ongoing":
        lines.append(f"已持續 {event['hit_count']} 個檢查視窗。")
    if url:
        lines.append(f"<{url}|查看完整原因與證據> · <{page_url('events')}|所有事件>")
    return "\n".join(lines)


def _brand_detail(event: dict) -> str:
    """Slack 無法「展開」，因此把前十名品牌直接列在告警裡。"""
    ctx = event.get("context_json")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except ValueError:
            ctx = {}
    top = (ctx or {}).get("brand_top") or []
    if not top:
        return ""
    listed = brands.top_summary(top, brands.BREAKDOWN_LIMIT)
    more = "" if event["brands"] <= len(top) else f"（前 {len(top)} 名）"
    return f"{more}：{listed}"


def dispatch(notifications: list[dict]) -> None:
    for n in notifications:
        text = _format_event(n["kind"], n["event"])
        _send(text)


def send_ops_message(title: str, body: str, link_page: str = "overview") -> None:
    """維運訊息（監測中斷、基線超齡、每日摘要）。link_page 決定尾端連結去哪一頁。"""
    text = f"⚙️ *{title}*\n{body}"
    url = page_url(link_page)
    if url:
        label = {"health": "查看資料健康", "events": "查看事件清單"}.get(link_page, "開啟資安總覽")
        text += f"\n<{url}|{label}>"
    _send(text)


def on_tick_failure() -> None:
    """連續失敗達 3 次時發「監測中斷」（webhook 不依賴 ClickHouse，仍可送達）。"""
    row = db.one("SELECT consecutive_failures FROM heartbeat WHERE key = 'five_min'")
    failures = row["consecutive_failures"] if row else 0
    if failures == 3:
        send_ops_message(
            "監測中斷",
            f"五分鐘檢查已連續失敗 {failures} 次（ClickHouse 查詢異常），"
            "目前無法判定是否沒有異常。",
            link_page="health")


def _send(text: str) -> None:
    url = slack_webhook_url()
    if not url:
        logger.info("Slack 未設定，通知僅記錄：%s", text.replace("\n", " / "))
        return
    payload = {"text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        _flush_queue(url)
    except requests.RequestException:
        logger.exception("Slack 送出失敗，寫入待送佇列")
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO slack_queue (created_at, payload_json) VALUES (?, ?)",
                (timewin.fmt(timewin.taipei_now()), json.dumps(payload, ensure_ascii=False)))


def _flush_queue(url: str) -> None:
    pending = db.rows(
        "SELECT id, payload_json FROM slack_queue WHERE sent_at IS NULL"
        " ORDER BY id LIMIT 20")
    for row in pending:
        try:
            resp = requests.post(url, json=json.loads(row["payload_json"]), timeout=10)
            resp.raise_for_status()
        except requests.RequestException:
            with db.tx() as conn:
                conn.execute("UPDATE slack_queue SET attempts = attempts + 1 WHERE id = ?",
                             (row["id"],))
            return
        with db.tx() as conn:
            conn.execute("UPDATE slack_queue SET sent_at = ? WHERE id = ?",
                         (timewin.fmt(timewin.taipei_now()), row["id"]))
