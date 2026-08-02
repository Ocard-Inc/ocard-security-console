from datetime import datetime

from console.core import timewin


def test_fmt_includes_seconds():
    s = timewin.fmt(datetime(2026, 7, 30, 21, 40))
    assert s == "2026-07-30 21:40:00"


def test_parse_accepts_three_formats():
    assert timewin.parse("2026-07-30 21:40:05") == datetime(2026, 7, 30, 21, 40, 5)
    assert timewin.parse("2026-07-30 21:40") == datetime(2026, 7, 30, 21, 40)
    assert timewin.parse("2026-07-30") == datetime(2026, 7, 30)


def test_align_tick():
    dt = datetime(2026, 7, 30, 21, 43, 27)
    assert timewin.align_tick(dt, 5) == datetime(2026, 7, 30, 21, 40)
    assert timewin.align_tick(dt, 10) == datetime(2026, 7, 30, 21, 40)


def test_window_lengths():
    start, end = timewin.window(10, end=datetime(2026, 7, 30, 21, 50))
    assert start == "2026-07-30 21:40:00"
    assert end == "2026-07-30 21:50:00"


def test_effective_now_subtracts_lag():
    now = datetime(2026, 7, 30, 21, 50)
    eff = timewin.effective_now(now)
    assert (now - eff).total_seconds() == 6 * 60


def test_business_hours():
    assert timewin.is_business_hours(datetime(2026, 7, 30, 12, 0))
    assert not timewin.is_business_hours(datetime(2026, 7, 30, 0, 13))
    assert not timewin.is_business_hours(datetime(2026, 7, 30, 23, 30))


# ── align_bucket：與 ClickHouse toStartOfInterval 的格線對齊 ──────────────
# 這裡的每一個期望值都是實際跑 ClickHouse 對照出來的（純 Python 測試，不連線）。
# 錯一格的後果不是報錯，是 request_trend 的 zero-fill 查表全部落空、
# 整張圖靜靜變成一條 0，所以這組測試是那個 bug 的護欄。

import pytest


@pytest.mark.parametrize("bucket, expected", [
    (5, "2026-08-02 13:35:00"),
    (10, "2026-08-02 13:30:00"),
    (30, "2026-08-02 13:30:00"),
    (60, "2026-08-02 13:00:00"),
    (120, "2026-08-02 12:00:00"),    # align_tick 會給 13:00 —— 這正是它不能用的原因
    (360, "2026-08-02 12:00:00"),
    (1440, "2026-08-02 00:00:00"),
])
def test_align_bucket_matches_clickhouse_grid(bucket, expected):
    dt = datetime(2026, 8, 2, 13, 37, 45)
    assert timewin.fmt(timewin.align_bucket(dt, bucket)) == expected


def test_align_bucket_differs_from_align_tick_above_60min():
    """這個差異就是 align_bucket 存在的理由，釘住它以免有人「簡化」掉。"""
    dt = datetime(2026, 8, 2, 13, 37, 45)
    assert timewin.align_tick(dt, 120) != timewin.align_bucket(dt, 120)
    # 60 分鐘以內兩者一致，所以五分鐘排程器繼續用 align_tick 沒問題
    for n in (5, 10, 30, 60):
        assert timewin.align_tick(dt, n) == timewin.align_bucket(dt, n)


@pytest.mark.parametrize("bad", [0, 7, 13, 50, -10])
def test_align_bucket_rejects_non_divisors_of_1440(bad):
    """不整除 1440 就與 ClickHouse 的格線對不上，寧可拋錯也不要靜靜畫錯。"""
    with pytest.raises(ValueError):
        timewin.align_bucket(datetime(2026, 8, 2, 13, 0), bad)


def test_align_bucket_is_idempotent():
    dt = datetime(2026, 8, 2, 13, 37, 45)
    for n in (5, 10, 30, 60, 120, 1440):
        once = timewin.align_bucket(dt, n)
        assert timewin.align_bucket(once, n) == once
