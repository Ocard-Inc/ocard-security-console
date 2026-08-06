"""對象趨勢（`entity_history.recent_trend()`）：分桶對齊與零填。

這個檔案守的兩件事壞掉時都**不會報錯**：

- 分桶對齊：`toStartOfInterval` 以 1970-01-01 為原點，用 `align_tick()` 對齊
  120 分鐘桶會差一格 —— zero-fill 的查表全部落空、整張圖靜靜變成一條 0。
- 前期位移：區間長度不是分桶的整數倍時，往回位移一個區間長度會讓前期那條線
  整條錯位，而畫面完全正常。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.queries import entity, entity_history

# 本機 DB 已知有資料的 api 時段（同 tests/test_event_entity.py 的 _NAMED_WINDOW）。
ANCHOR = "2026-08-01 13:00:00"


def test_every_range_is_a_whole_number_of_buckets():
    """區間必須是分桶的整數倍，否則「往回位移一個區間」會讓前期整條錯位。

    這是行為驗證而不是註解：`toStartOfInterval` 的格線固定在 1970-01-01，
    位移量不是分桶的倍數時前期的桶起點與本期不在同一組格線上，
    而症狀是「前期那條線看起來像平移了一點」—— 沒有人會發現。
    """
    assert entity_history.TREND_RANGES, "區間表不可為空"
    for minutes, bucket in entity_history.TREND_RANGES.items():
        assert minutes % bucket == 0, f"{minutes} 分鐘不是 {bucket} 分鐘桶的整數倍"
        points = minutes // bucket
        # 12–84 點是這個專案既有的可讀範圍（同 trends.bucket_for 的說明）
        assert 12 <= points <= 84, f"{minutes} 分鐘會產生 {points} 個點"


def test_buckets_come_from_the_same_grid_as_clickhouse():
    """分桶格線必須與 ClickHouse 的 `toStartOfInterval` 相同。

    `align_bucket()` 會對「不整除 1440 的分桶」直接拒絕，所以這裡順帶擋住
    有人把 90 分鐘之類的值加進 TREND_RANGES。
    """
    at = timewin.parse("2026-08-05 13:37:45")
    for bucket in sorted(set(entity_history.TREND_RANGES.values())):
        aligned = timewin.align_bucket(at, bucket)
        assert aligned <= at
        assert (at - aligned) < timedelta(minutes=bucket)
        # 從午夜起算必須落在整數格上（這正是 ClickHouse 的格線）
        midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
        assert int((aligned - midnight).total_seconds() // 60) % bucket == 0


@pytest.fixture(scope="module")
def ref():
    r = entity.from_filters("api", {"endpoint": "Api2/GetProfile"})
    assert r is not None
    return r


def test_zero_filled_to_the_whole_window_with_a_matching_previous_period(ref):
    """零填到整個區間，且前期逐桶對得上本期。

    沒有零填的話空桶會直接消失，而時間軸是 category、等距 —— 停掉的那幾小時
    不是「缺一格」而是**時間軸被壓縮**，看起來像一條往上爬的線。
    """
    anchor = timewin.parse(ANCHOR)
    for minutes, bucket in entity_history.TREND_RANGES.items():
        out = entity_history.recent_trend(ref, anchor, minutes)
        where = f"{minutes} 分鐘"

        assert out["bucket_minutes"] == bucket, where
        assert len(out["rows"]) == minutes // bucket, f"{where} 的點數不對"

        # 相鄰桶的間隔固定 = 沒有跳格也沒有重複
        stamps = [timewin.parse(r["bucket"]) for r in out["rows"]]
        gaps = {int((b - a).total_seconds() // 60) for a, b in zip(stamps, stamps[1:])}
        assert gaps == {bucket}, f"{where} 的分桶不連續：{gaps}"

        # 前期與本期逐桶差一個區間長度
        for r in out["rows"]:
            delta = timewin.parse(r["bucket"]) - timewin.parse(r["prev_bucket"])
            assert delta == timedelta(minutes=minutes), where

        # 零填之後 rows 永遠非空，所以「有沒有活動」只能問 total
        assert out["total"] == sum(r["count"] for r in out["rows"]), where
        assert out["prev_total"] == sum(r["prev_count"] for r in out["rows"]), where


def test_right_edge_never_claims_data_that_has_not_landed(ref):
    """錨點比「已落地的資料」還新時，右界要被夾住並說出來。

    不夾的話最後幾個桶是一段「還沒發生」的假 0，而那與「這段時間沒有活動」
    在畫面上一模一樣。
    """
    future = timewin.effective_now() + timedelta(days=3)
    out = entity_history.recent_trend(ref, future, 1440)
    assert timewin.parse(out["anchor"]) <= \
        timewin.effective_now() + timedelta(minutes=out["bucket_minutes"]), \
        "右界沒有被夾住"
    assert out["window_note"], "夾了右界卻沒有說"
    # 夾住之後仍然是完整長度的區間（往前滑，不是截短）
    assert len(out["rows"]) == 1440 // out["bucket_minutes"]


def test_absurd_range_is_rejected_by_the_query_layer(ref):
    """不在封閉集合裡的區間必須拋，不可以自己挑一個分桶。

    靜靜挑一個的話畫面會顯示「最近 5 小時」而圖是別的長度。
    端點層會把這個變成 400（見 routes 的 `event_entity_trend`）。
    """
    with pytest.raises(KeyError):
        entity_history.recent_trend(ref, timewin.parse(ANCHOR), 300)


def test_only_the_genuinely_slow_combination_is_flagged():
    """「較慢」的標註必須跟著實測，不是「含 JSONExtract 就算慢」。

    只有 api 的來源 IP 在 7d 慢（實測 11.8 秒，掃 14 天且無法剪枝）。
    `admin` 的 actor 也帶 JSONExtract 但 7d 只要 3.9 秒 —— 一起標的話警語
    會貼滿選單，而貼滿了就沒有人讀（同 explorer.extent_lookback_days()
    刻意不為 admin/actor 多切一個常數的理由）。
    """
    api_ip = entity.from_filters("api", {"source_ip": "1.2.3.4"})
    assert entity_history.slow_ranges(api_ip) == [10080]

    # 同一張表、不用 JSONExtract 的維度不標
    assert entity_history.slow_ranges(
        entity.from_filters("api", {"endpoint": "Api2/GetProfile"})) == []
    # 別的表的來源 IP 是真欄位（backend 7d 實測 0.1 秒）
    assert entity_history.slow_ranges(
        entity.from_filters("backend", {"source_ip": "1.2.3.4"})) == []
    # admin 的 actor 帶 JSONExtract 但仍然快，刻意不標
    assert entity_history.slow_ranges(
        entity.from_filters("admin", {"actor": "vibesktv"})) == []

    # 標註的值必須真的是可選的區間，否則前端比對不到、警語永遠不出現
    for m in entity_history.slow_ranges(api_ip):
        assert m in entity_history.TREND_RANGES


def test_anchor_and_last_seen_are_both_reported(ref):
    """區間右界與「事件最後出現」是兩個不同的時刻，兩個都要回。

    右界是「含事件那一刻的整個分桶」的結束，120 分鐘分桶下它比事件晚將近兩小時
    （實測 last_seen 22:05 → 右界 08-07 00:00）。只回一個而畫面寫
    「（事件最後出現）」的話，那句話會在指一個事件根本沒有發生過的時刻。
    """
    last = timewin.parse("2026-08-06 22:05:00")
    for minutes, bucket in entity_history.TREND_RANGES.items():
        out = entity_history.recent_trend(ref, last, minutes)
        assert out["last_seen"] == timewin.fmt(last), minutes
        anchor = timewin.parse(out["anchor"])
        if out["window_note"]:
            continue                      # 被夾到已落地的資料，右界與事件無關
        assert anchor > last, minutes
        assert (anchor - last) <= timedelta(minutes=bucket), (
            f"{minutes} 分鐘：右界應落在含 last_seen 的那個分桶結束處")
