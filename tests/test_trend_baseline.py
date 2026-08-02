"""趨勢基線的迴歸測試。

舊版 `web/lib.js:89-93` 只讀 `buckets[0]` 的 median／P95，把「同時段 median–P95 範圍」
畫成一條橫跨全圖的平帶。6 小時視窗下 buckets[0] 正好是當日尖峰，位置誤差達 25 倍。

後端資料本來就是逐 bucket 正確的，錯的是前端。這個測試把「基線會隨時間變化」
這件事釘住 —— 一開始若有這個測試，那個假設就不會成立那麼久。
"""
from __future__ import annotations


def test_six_hour_window_has_varying_baseline(client):
    """6 小時視窗跨越多個小時桶，基線必須不只一種值。"""
    r = client.get("/api/overview?minutes=360")
    assert r.status_code == 200
    buckets = r.json()["trend"]["buckets"]
    assert len(buckets) > 6, "6 小時、10 分鐘分桶應有 36 個點"

    medians = {b["api_median"] for b in buckets if b["api_median"] is not None}
    assert len(medians) > 1, (
        "6 小時視窗的 api_median 應隨小時變化；只有一種值代表基線退化成全域分布，"
        "此時畫成逐 bucket 的帶沒有意義")


def test_baseline_has_no_interior_gaps(client):
    """基線要嘛整段都有、要嘛整段都沒有；中間破洞會讓 rangeArea 斷成好幾截。"""
    r = client.get("/api/overview?minutes=360")
    buckets = r.json()["trend"]["buckets"]
    present = [b["api_median"] is not None for b in buckets]
    if any(present) and not all(present):
        first, last = present.index(True), len(present) - 1 - present[::-1].index(True)
        assert all(present[first:last + 1]), "api_median 在頭尾之間不應有 None"


def test_p95_not_below_median(client):
    """帶的上緣不可低於下緣，否則 rangeArea 會畫出翻轉的形狀。"""
    r = client.get("/api/overview?minutes=360")
    for b in r.json()["trend"]["buckets"]:
        if b["api_median"] is not None and b["api_p95"] is not None:
            assert b["api_p95"] >= b["api_median"], f"{b['label']}: P95 低於 median"
