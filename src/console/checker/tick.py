"""五分鐘檢查：單次 tick 的執行入口（daemon 與 Web app 共用）。"""
from __future__ import annotations

import logging
from datetime import datetime

from console.core import timewin
from console.core.ch import ChConnectionError
from console.rules import engine
from console.rules.effective import effective_rules
from console.store import db, events, rule_suppressions

logger = logging.getLogger(__name__)


def run_tick(window_end: datetime | None = None) -> dict:
    """執行一次檢查。回傳摘要（供心跳與通知層使用）。"""
    end = window_end or timewin.align_tick(timewin.effective_now())
    now_str = timewin.fmt(timewin.taipei_now())
    # effective_rules() 而不是 load_rules()：YAML 是預設值，實際生效的是它加上
    # SQLite 的參數覆寫（見 rules/effective.py）。刻意每個 tick 重讀。
    rules = effective_rules()
    # 心跳一律要被碰到，不管是哪一種失敗。
    #
    # 這裡曾經只 catch ChConnectionError，其餘例外直接往上拋 —— 於是 heartbeat
    # 那一列完全沒被更新：consecutive_failures 留 0、note 留空，
    # `_monitor_status()` 顯示綠色「正常」，`notify.on_tick_failure()` 的
    # `failures == 3` 也永遠不成立，**Slack 一個字都不發**。五分鐘檢查每次崩潰，
    # 唯一的痕跡是 state/logs/console.log 裡每五分鐘一筆 traceback。
    #
    # 規則覆寫（rules/effective.py）讓「規則層的資料錯誤」第一次有能力打掉整個
    # tick，所以這條路徑從理論問題變成可觸發。
    try:
        findings, failures, suppressed = engine.evaluate(rules, end)
    except ChConnectionError:
        _heartbeat_fail(now_str, "ClickHouse 連線失敗")
        raise
    except Exception as exc:
        _heartbeat_fail(now_str, f"檢查中斷：{type(exc).__name__}")
        raise
    notifications = events.apply_findings(
        findings, timewin.taipei_now(), rules=rules, suppressed=suppressed)
    # 抑制紀錄。**刻意不進通知** —— 把抑制發成告警正好抵銷抑制的意義。
    rule_suppressions.record_many(suppressed, at=now_str)
    _heartbeat_ok(now_str, end, failures)
    summary = {
        "window_end": timewin.fmt(end),
        "findings": len(findings),
        "notifications": notifications,
        "rule_failures": failures,
        "suppressed": len(suppressed),
    }
    logger.info("tick 完成 window_end=%s findings=%d suppressed=%d failures=%s",
                summary["window_end"], len(findings), len(suppressed), failures or "無")
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
