"""通知調度：事件變化 → Slack（未設定 webhook 時僅記 log 與佇列）。

## Slack 訊息含什麼

聚合數字、**原始後台帳號與來源 IP**、endpoint、品牌名稱（編號）。收到告警的人
不必再進主控台就知道是哪個帳號、哪個來源、影響哪些品牌 —— 這是刻意的
（見 `core/masking.py` 的模組說明）。

`entity_label` 由 `rules/engine.entity_parts()` 在事件建立時組好，所以 Slack 與 UI
看到的是同一組值，不需在此再查一次 MySQL。UI 的「涉及品牌」可以點開看明細，
Slack 不行，所以前十名直接列在訊息裡。

## 仍然不含什麼

**log 原文（params／headers）與有效的 API token。** 前者混著憑證與消費者手機、
Email，後者顯示了就能被冒用 —— 而 Slack 頻道的成員範圍不在主控台的權限控制內，
訊息也會留在頻道歷史裡。要看原文請走主控台的逐筆調閱（一次一筆、寫入稽核）。

**前提**：這個 Slack 頻道必須是對內且成員可控的。它拿到的資訊等同主控台的事件頁。
"""
from __future__ import annotations

import json
import logging

import requests

from console.core import brands, config, timewin
from console.core.config import slack_webhook_url
from console.store import db

logger = logging.getLogger(__name__)

_SEV_EMOJI = {"P0": "🟥", "P1": "🔴", "P2": "🟠", "P3": "🔵"}


def base_url() -> str:
    """主控台對外網址（.env 的 CONSOLE_BASE_URL）。沒設定就不放連結 ——
    寧可少一個連結，也不要給收到告警的人一個連到自己 localhost 的死連結。"""
    return config.console_base_url()


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


def log_startup_status() -> None:
    """啟動時把「通知會不會真的送出去」講清楚（由 `app.py` 的 lifespan 呼叫）。

    停用是 WARNING 而不是 INFO：`SLACK_ENABLED` 漏設的正式環境會安靜地不發任何
    告警，而主控台其餘部分完全正常 —— 那正是這個系統最糟的失效模式。啟動選擇
    「只警告、不擋啟動」（使用者於 2026-08 決定），所以這一行與資安總覽的橫幅
    就是唯一的痕跡。
    """
    setting = config.slack_setting()
    if not setting.enabled:
        logger.warning("Slack 通知已停用（%s）—— 告警只會寫進主控台與 log，"
                       "不會送到 Slack", setting.reason)
    elif not slack_webhook_url():
        logger.warning("Slack 通知已啟用（%s），但 SLACK_WEBHOOK_URL 是空的 ——"
                       " 告警只會寫進 log", setting.reason)
    else:
        logger.info("Slack 通知已啟用（%s）", setting.reason)


def summary() -> dict:
    """給資安總覽橫幅的現況：訊息**真的會送出去嗎**，不會的話是哪一個原因。

    開關與 webhook 兩者缺任一個都是「不會送」，但處置完全不同（改 .env 開開關
    vs 去補 webhook），所以 note 要分開講而不是合併成「未啟用」。
    """
    setting = config.slack_setting()
    has_url = bool(slack_webhook_url())
    if not setting.enabled:
        note = (f"Slack 通知已停用（{setting.reason}）。"
                "P0/P1 告警只會出現在這個主控台裡，沒有人會被通知。")
    elif not has_url:
        note = ("Slack 通知已開啟，但沒有設定 SLACK_WEBHOOK_URL，"
                "訊息只會寫進 log。")
    else:
        note = ""
    return {"enabled": setting.enabled and has_url, "note": note}


def _send(text: str) -> None:
    # 總開關關閉時**不寫 slack_queue**：那張表的語意是「送出失敗，待補送」，
    # 而刻意不發不是失敗。寫進去的話，之後某天把開關打開，_flush_queue 會把
    # 累積的整批舊訊息一次倒進頻道（本機跑過的每一次 replay 與驗收都在裡面）。
    if not config.slack_enabled():
        logger.info("Slack 通知已停用，僅記錄：%s", text.replace("\n", " / "))
        return
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
