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
