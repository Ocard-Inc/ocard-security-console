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
    """對齊到 tick 邊界（向下取整）。

    只對齊「分鐘」欄位，因此**僅適用於整除 60 的間隔**（五分鐘排程器就是這樣用的）。
    要對齊分桶格線請用 align_bucket() —— 兩者在 tick > 60 分鐘時會給出不同答案，
    詳見那邊的說明。
    """
    tick = tick_minutes or settings()["time"]["tick_minutes"]
    minute = (dt.minute // tick) * tick
    return dt.replace(minute=minute, second=0, microsecond=0)


def align_bucket(dt: datetime, bucket_minutes: int) -> datetime:
    """對齊到 ClickHouse `toStartOfInterval(create_time, INTERVAL n MINUTE)` 的同一格線。

    toStartOfInterval 以 1970-01-01 00:00 為原點；伺服器時區是 UTC，而 create_time
    存的是台北牆鐘，所以那條格線等同於**牆鐘的午夜格線** —— 但只在 n 整除 1440 時成立，
    因此不整除就直接拒絕。

    為什麼不能用 align_tick：它只把「分鐘」欄位向下取整，n > 60 就會錯位。
    實測對 2026-08-02 13:37:45：

        n=120   align_tick → 13:00   ClickHouse → 12:00
        n=1440  align_tick → 13:00   ClickHouse → 00:00

    差一格的後果不是報錯，是 request_trend 的 zero-fill 迴圈用 fmt(cursor) 當 key
    去查 ClickHouse 回傳的桶起點時**全部落空**，整張圖靜靜地變成一條 0。
    """
    if bucket_minutes <= 0 or 1440 % bucket_minutes:
        raise ValueError(
            f"分桶 {bucket_minutes} 分鐘無法整除 1440，會與 ClickHouse 的 "
            f"toStartOfInterval 格線錯位")
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((dt - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(elapsed // bucket_minutes) * bucket_minutes)


def window(minutes: int, end: datetime | None = None) -> tuple[str, str]:
    """回傳 (start, end) 字串參數，end 預設為 effective_now 對齊 tick。"""
    end_dt = end or align_tick(effective_now())
    start_dt = end_dt - timedelta(minutes=minutes)
    return fmt(start_dt), fmt(end_dt)


def is_business_hours(dt: datetime) -> bool:
    hours = settings()["business_hours"]
    return hours["start"] <= dt.hour < hours["end"]
