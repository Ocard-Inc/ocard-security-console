"""分桶階梯的純 Python 測試（不連 ClickHouse）。

固定 10 分鐘分桶時，「最近 1 小時」只有 6 個點、「最近 7 天」是 1008 個點。
階梯讓點數維持在可讀範圍，但它有兩個硬性條件，這裡就是在守這兩條：

1. 每個分桶都要整除 1440，否則與 ClickHouse 的 toStartOfInterval 格線錯位。
2. 每個分桶都要有同粒度的基線，否則倍數會變假 ——
   用 10 分鐘的基線去比 120 分鐘的桶，會冒出假的「12 倍」。
"""
from __future__ import annotations

import pytest

from console.checker.calibrate import GRANULARITIES
from console.queries.trends import BUCKET_LADDER, bucket_for


@pytest.mark.parametrize("bucket", [b for _, b in BUCKET_LADDER])
def test_every_bucket_divides_1440(bucket):
    assert 1440 % bucket == 0, f"{bucket} 分鐘無法整除 1440，會與 ClickHouse 格線錯位"


@pytest.mark.parametrize("bucket", [b for _, b in BUCKET_LADDER])
def test_every_bucket_has_a_calibrated_granularity(bucket):
    assert bucket in GRANULARITIES, (
        f"階梯用了 {bucket} 分鐘分桶，但 calibrate.py 沒有算這個粒度的基線 —— "
        f"倍數會變成 {bucket // 10} 倍大的假數字")


def test_ladder_is_monotonic():
    limits = [lim for lim, _ in BUCKET_LADDER]
    buckets = [b for _, b in BUCKET_LADDER]
    assert limits == sorted(limits), "視窗上限必須遞增"
    assert buckets == sorted(buckets), "分桶必須隨視窗變粗"


@pytest.mark.parametrize("minutes, expected", [
    (10, 5), (60, 5),            # 1 小時以內
    (61, 10), (360, 10),         # 6 小時以內
    (361, 30), (1440, 30),       # 24 小時以內
    (1441, 120), (10080, 120),   # 7 天
    (99999, 120),                # 超出階梯 → 沿用最粗的
])
def test_bucket_for_boundaries(minutes, expected):
    assert bucket_for(minutes) == expected


@pytest.mark.parametrize("minutes", [60, 360, 1440, 10080])
def test_point_count_stays_readable(minutes):
    """點數落在 12–84 之間：太少畫不出趨勢，太多在 700px 寬的圖上糊成一團。"""
    points = minutes // bucket_for(minutes)
    assert 12 <= points <= 90, f"{minutes} 分鐘會產生 {points} 個點"
