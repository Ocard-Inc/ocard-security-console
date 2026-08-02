"""台北牆鐘時間與監測視窗計算。

ClickHouse 伺服器時區為 UTC，但四張 log 表的 create_time 存的是台北牆鐘時間，
因此所有查詢邊界一律由 Python 端以台北時間算好、以完整字串（含秒）傳參，
絕不在 SQL 端使用 now()。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from console.core.config import settings

TZ = ZoneInfo("Asia/Taipei")
FMT = "%Y-%m-%d %H:%M:%S"


def taipei_now() -> datetime:
    """當下台北牆鐘時間（naive，可直接與 create_time 比較）。"""
    return datetime.now(TZ).replace(tzinfo=None)


def fmt(dt: datetime) -> str:
    """ClickHouse DateTime 參數字串（必須含秒，否則 CANNOT_PARSE_DATETIME）。"""
    return dt.strftime(FMT)


def parse(s: str) -> datetime:
    for pattern in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), pattern)
        except ValueError:
            continue
    raise ValueError(f"無法解析時間 {s!r}，格式須為 YYYY-MM-DD[ HH:MM[:SS]]")


def effective_now(now: datetime | None = None) -> datetime:
    """扣除資料落地延遲後的視窗右界。"""
    lag = settings()["time"]["lag_buffer_minutes"]
    return (now or taipei_now()) - timedelta(minutes=lag)


def align_tick(dt: datetime, tick_minutes: int | None = None) -> datetime:
    """對齊到 tick 邊界（向下取整）。"""
    tick = tick_minutes or settings()["time"]["tick_minutes"]
    minute = (dt.minute // tick) * tick
    return dt.replace(minute=minute, second=0, microsecond=0)


def window(minutes: int, end: datetime | None = None) -> tuple[str, str]:
    """回傳 (start, end) 字串參數，end 預設為 effective_now 對齊 tick。"""
    end_dt = end or align_tick(effective_now())
    start_dt = end_dt - timedelta(minutes=minutes)
    return fmt(start_dt), fmt(end_dt)


def is_business_hours(dt: datetime) -> bool:
    hours = settings()["business_hours"]
    return hours["start"] <= dt.hour < hours["end"]
