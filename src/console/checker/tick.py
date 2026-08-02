"""五分鐘檢查：單次 tick 的執行入口（daemon 與 Web app 共用）。"""
from __future__ import annotations

import logging
from datetime import datetime

from console.core import timewin
from console.core.ch import ChConnectionError
from console.rules import engine
from console.rules.loader import load_rules
from console.store import db, events

logger = logging.getLogger(__name__)


def run_tick(window_end: datetime | None = None) -> dict:
    """執行一次檢查。回傳摘要（供心跳與通知層使用）。"""
    end = window_end or timewin.align_tick(timewin.effective_now())
    now_str = timewin.fmt(timewin.taipei_now())
    rules = load_rules()
    try:
        findings, failures = engine.evaluate(rules, end)
    except ChConnectionError:
        _heartbeat_fail(now_str, "ClickHouse 連線失敗")
        raise
    notifications = events.apply_findings(findings, timewin.taipei_now())
    _heartbeat_ok(now_str, end, failures)
    summary = {
        "window_end": timewin.fmt(end),
        "findings": len(findings),
        "notifications": notifications,
        "rule_failures": failures,
    }
    logger.info("tick 完成 window_end=%s findings=%d failures=%s",
                summary["window_end"], len(findings), failures or "無")
    return summary


def _heartbeat_ok(now_str: str, end, failures: list[str]) -> None:
    note = f"規則失敗：{','.join(failures)}" if failures else ""
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO heartbeat (key, last_tick, last_ok, consecutive_failures, note)"
            " VALUES ('five_min', ?, ?, 0, ?)"
            " ON CONFLICT(key) DO UPDATE SET last_tick = ?, last_ok = ?,"
            " consecutive_failures = 0, note = ?",
            (now_str, now_str, note, now_str, now_str, note))
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO poll_state (key, value) VALUES ('last_window_end', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?",
            (timewin.fmt(end), timewin.fmt(end)))


def _heartbeat_fail(now_str: str, note: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO heartbeat (key, last_tick, last_ok, consecutive_failures, note)"
            " VALUES ('five_min', ?, NULL, 1, ?)"
            " ON CONFLICT(key) DO UPDATE SET last_tick = ?,"
            " consecutive_failures = consecutive_failures + 1, note = ?",
            (now_str, note, now_str, note))
