"""通知調度：事件變化 → Slack（未設定 webhook 時僅記 log 與佇列）。

Slack 告警內容只含聚合數字與 fingerprint / endpoint / 品牌 ID，
永不含原始 IP、帳號、token 或 log 原文。
"""
from __future__ import annotations

import json
import logging

import requests

from console.core import timewin
from console.core.config import settings, slack_webhook_url
from console.store import db

logger = logging.getLogger(__name__)

_SEV_EMOJI = {"P0": "🟥", "P1": "🔴", "P2": "🟠", "P3": "🔵"}


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
    lines = [
        f"{_SEV_EMOJI.get(sev, '')} *[{sev}] {head}｜{event['evt_no']} {event['rule_name']}*",
        f"對象：`{event['entity_label']}`",
        f"目前值 *{metric:,.0f}*（{compare}）"
        + (f"，峰值 {peak:,.0f}" if peak > metric else ""),
        f"視窗：{event['first_seen']} ~ {event['last_seen']}（Asia/Taipei）",
    ]
    if event.get("brands"):
        lines.append(f"涉及品牌：{event['brands']} 個")
    if kind == "ongoing":
        lines.append(f"已持續 {event['hit_count']} 個檢查視窗。")
    return "\n".join(lines)


def dispatch(notifications: list[dict]) -> None:
    for n in notifications:
        text = _format_event(n["kind"], n["event"])
        _send(text)


def send_ops_message(title: str, body: str) -> None:
    _send(f"⚙️ *{title}*\n{body}")


def on_tick_failure() -> None:
    """連續失敗達 3 次時發「監測中斷」（webhook 不依賴 ClickHouse，仍可送達）。"""
    row = db.one("SELECT consecutive_failures FROM heartbeat WHERE key = 'five_min'")
    failures = row["consecutive_failures"] if row else 0
    if failures == 3:
        send_ops_message(
            "監測中斷",
            f"五分鐘檢查已連續失敗 {failures} 次（ClickHouse 查詢異常），"
            "目前無法判定是否沒有異常。")


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
