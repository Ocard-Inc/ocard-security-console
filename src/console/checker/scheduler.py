"""asyncio 排程器：五分鐘檢查 + 每日檢查（FastAPI lifespan 內啟動）。

catch-up：啟動時若上次視窗落後超過一個 tick，以 5 分鐘步進補跑
（上限 max_catchup_hours，超過則記「監測中斷」事件並發通知）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from console.alerting import notify
from console.core import timewin
from console.core.config import settings
from console.checker import calibrate, tick
from console.store import db, rule_suppressions

logger = logging.getLogger(__name__)


def _last_window_end() -> datetime | None:
    row = db.one("SELECT value FROM poll_state WHERE key = 'last_window_end'")
    return timewin.parse(row["value"]) if row else None


def run_catchup_and_tick() -> dict:
    """同步執行：補跑落後視窗後執行當前 tick。回傳最後一次摘要。"""
    cfg = settings()
    tick_min = cfg["time"]["tick_minutes"]
    max_catchup = cfg["alerting"]["max_catchup_hours"]
    target = timewin.align_tick(timewin.effective_now())
    last = _last_window_end()

    if last is not None and target > last:
        gap_hours = (target - last).total_seconds() / 3600
        cursor = last + timedelta(minutes=tick_min)
        if gap_hours > max_catchup:
            skipped_from = cursor
            cursor = target - timedelta(hours=max_catchup)
            cursor = timewin.align_tick(cursor)
            msg = (f"監測中斷 {gap_hours:.1f} 小時（{timewin.fmt(skipped_from)} ~ "
                   f"{timewin.fmt(cursor)} 未檢查），只補跑最近 {max_catchup} 小時")
            logger.warning(msg)
            notify.send_ops_message("監測中斷", msg)
        while cursor < target:
            summary = tick.run_tick(cursor)
            notify.dispatch(summary["notifications"])
            cursor += timedelta(minutes=tick_min)

    summary = tick.run_tick(target)
    notify.dispatch(summary["notifications"])
    return summary


def run_daily() -> None:
    """每日檢查：基線重算 + known_sources 增量 + 來源情報更新 + 基線年齡檢查。"""
    from console.intel import refresh as intel_refresh
    from console.rules import baseline as bl
    result = calibrate.calibrate()
    calibrate.seed_known_sources()

    # 來源情報（機房／VPN 分類）。純離線比對，不發網路請求。
    # 失敗不影響基線 —— 期間掃描會自動跳過需要情報的探針並明確註明，
    # 而基線與五分鐘檢查完全不依賴它。
    try:
        intel_refresh.refresh()
    except Exception:
        logger.exception("來源情報更新失敗（不影響基線與五分鐘檢查）")

    # 抑制紀錄修剪。每個 tick、每個被抑制的命中一列，無上限。
    try:
        pruned = rule_suppressions.prune()
        if pruned:
            logger.info("抑制紀錄修剪 %d 列", pruned)
    except Exception:
        logger.exception("抑制紀錄修剪失敗（不影響基線與五分鐘檢查）")

    now_str = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO heartbeat (key, last_tick, last_ok, consecutive_failures, note)"
            " VALUES ('daily', ?, ?, 0, ?)"
            " ON CONFLICT(key) DO UPDATE SET last_tick = ?, last_ok = ?, note = ?",
            (now_str, now_str, f"基線 {result['rows']} 列",
             now_str, now_str, f"基線 {result['rows']} 列"))
    age = bl.age_days(timewin.taipei_now())
    if age is not None and age > settings()["baseline"]["max_age_days"]:
        notify.send_ops_message("基線超齡", f"基線已 {age:.0f} 天未重算，請檢查每日排程")


async def scheduler_loop(stop: asyncio.Event) -> None:
    """常駐迴圈：對齊 5 分鐘邊界執行 tick；每日 recalc_hour 執行每日檢查。"""
    cfg = settings()
    tick_min = cfg["time"]["tick_minutes"]
    daily_hour = cfg["baseline"]["recalc_hour"]
    last_daily_date = None

    while not stop.is_set():
        try:
            await asyncio.to_thread(run_catchup_and_tick)
        except Exception:
            logger.exception("tick 執行失敗")
            notify.on_tick_failure()
        now = timewin.taipei_now()
        if now.hour >= daily_hour and last_daily_date != now.date():
            row = db.one("SELECT last_ok FROM heartbeat WHERE key = 'daily'")
            already = row and row["last_ok"] and row["last_ok"][:10] == str(now.date())
            if not already:
                try:
                    await asyncio.to_thread(run_daily)
                except Exception:
                    logger.exception("每日檢查失敗")
                    notify.send_ops_message("每日檢查失敗", "基線重算失敗，詳見 log")
            last_daily_date = now.date()
        # 睡到下一個 tick 邊界
        now = timewin.taipei_now()
        next_tick = timewin.align_tick(now, tick_min) + timedelta(minutes=tick_min)
        delay = max(5.0, (next_tick - now).total_seconds() + 2)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
