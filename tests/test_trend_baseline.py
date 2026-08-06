"""趨勢基線的迴歸測試。

舊版 `web/lib.js:89-93` 只讀 `buckets[0]` 的 median／P95，把「同時段 median–P95 範圍」
畫成一條橫跨全圖的平帶。6 小時視窗下 buckets[0] 正好是當日尖峰，位置誤差達 25 倍。

後端資料本來就是逐 bucket 正確的，錯的是前端。這些測試把「基線會隨時間變化」
這件事釘住 —— 一開始若有這個測試，那個假設就不會成立那麼久。

首頁改成 2×2 小倍數之後，每條線各自都要有自己的同時段基線（以前只有 api 與
login_success 被讀出來，backend 與 login_failed 明明算好了卻沒用），所以下面
一律對每條線都驗。

**Order Log 接入後是五條線，不是四條**（`queries/trends.request_trend()` 的
`baseline_keys["order"]`）。原本這裡的 `SERIES` 沒跟著加，症狀是 order 的
5m/10m/30m 基線完全沒有測試守著（只有 `test_api_smoke.py` 覆蓋到 120m 那一格），
少一個粒度的後果是那個區間的面板不畫 median 虛線 —— 正確的降級，但沒有任何
訊號說「這是退化」還是「本來就沒有」。
"""
from __future__ import annotations

import pytest

# 五條線與其對應的基線欄位前綴
SERIES = ["api", "backend", "login_success", "login_failed", "order"]


# 必須是 function scope：conftest 的 _offline_auth 是 autouse 的 function fixture，
# module scope 會先於它執行，那時 ROS 還開著 → 401。
@pytest.fixture
def buckets(client):
    r = client.get("/api/overview?minutes=360")
    assert r.status_code == 200
    return r.json()["trend"]["buckets"]


def test_six_hour_window_has_36_buckets(buckets):
    assert len(buckets) > 6, "6 小時、10 分鐘分桶應有 36 個點"


@pytest.mark.parametrize("name", SERIES)
def test_every_series_has_a_baseline(buckets, name):
    """四條線都要有自己的基線 —— 小倍數的每個面板都要畫得出 median 參考線。"""
    medians = [b[f"{name}_median"] for b in buckets]
    assert any(m is not None for m in medians), (
        f"{name} 完全沒有基線。檢查 checker/calibrate.py 是否已產生對應的 metric key，"
        f"以及 queries/trends.py 的 baseline_keys 有沒有列到它")


@pytest.mark.parametrize("name", ["api", "backend", "login_success"])
def test_baseline_varies_across_hours(buckets, name):
    """6 小時視窗跨越多個小時桶，基線必須不只一種值。

    login_failed 不在此列：它的 median 全天都在 1~4 之間，本來就可能整段同值。
    """
    medians = {b[f"{name}_median"] for b in buckets if b[f"{name}_median"] is not None}
    assert len(medians) > 1, (
        f"{name} 的 median 應隨小時變化；只有一種值代表基線退化成全域分布")


@pytest.mark.parametrize("name", SERIES)
def test_baseline_has_no_interior_gaps(buckets, name):
    """基線要嘛整段都有、要嘛整段都沒有；中間破洞會讓 median 參考線斷成好幾截。"""
    present = [b[f"{name}_median"] is not None for b in buckets]
    if any(present) and not all(present):
        first = present.index(True)
        last = len(present) - 1 - present[::-1].index(True)
        assert all(present[first:last + 1]), f"{name}_median 在頭尾之間不應有 None"


@pytest.mark.parametrize("name", SERIES)
def test_p95_not_below_median(buckets, name):
    """P95 低於 median 代表基線資料壞了。"""
    for b in buckets:
        med, p95 = b[f"{name}_median"], b[f"{name}_p95"]
        if med is not None and p95 is not None:
            assert p95 >= med, f"{b['label']} {name}: P95 {p95} 低於 median {med}"


def test_legacy_login_fields_still_present(buckets):
    """login_median／login_p95 是舊欄位名，前端表格檢視仍在用，不可拿掉。"""
    b = buckets[-1]
    assert b["login_median"] == b["login_success_median"]
    assert b["login_p95"] == b["login_success_p95"]


# ── start 也必須對齊分桶格線 ───────────────────────────────────────────────
# 「今天」的分鐘數是現算的（例如 125），不見得是分桶的倍數。以前只對齊了 end，
# start = end - minutes 就會落在格線之間，zero-fill 產生的每個 cursor 全部偏移，
# 與 ClickHouse 回傳的桶起點永遠對不上 —— 13 個桶全部讀成 0，而且不會報錯。

@pytest.mark.parametrize("minutes", [125, 164, 97, 233])
def test_odd_window_lengths_are_not_all_zero(client, minutes):
    """分鐘數不是分桶倍數時，整段不可以變成 0。"""
    r = client.get(f"/api/overview?minutes={minutes}")
    assert r.status_code == 200
    buckets = r.json()["trend"]["buckets"]
    assert buckets
    assert sum(b["api"] for b in buckets) > 0, (
        f"minutes={minutes} 的整段 api 都是 0 —— start 沒有對齊分桶格線？")


@pytest.mark.parametrize("minutes", [125, 164, 1440, 10080])
def test_bucket_starts_are_on_the_grid(client, minutes):
    from console.core import timewin
    t = client.get(f"/api/overview?minutes={minutes}").json()["trend"]
    b = t["bucket_minutes"]
    for row in t["buckets"]:
        dt = timewin.parse(row["bucket"])
        assert timewin.align_bucket(dt, b) == dt, (
            f"{row['bucket']} 不在 {b} 分鐘的格線上")


def test_multiday_window_labels_include_the_date(client):
    """7 天 × 120 分桶會有 84 個標籤，只寫 %H:%M 的話僅 12 種相異值，分不出哪一天。"""
    buckets = client.get("/api/overview?minutes=10080").json()["trend"]["buckets"]
    labels = [b["label"] for b in buckets]
    assert len(set(labels)) == len(labels), "跨日視窗的 x 標籤必須唯一"
    assert "/" in labels[0], f"跨日視窗的標籤要帶日期，目前是 {labels[0]!r}"


def test_single_day_window_labels_stay_short(client):
    labels = [b["label"] for b in
              client.get("/api/overview?minutes=360").json()["trend"]["buckets"]]
    assert "/" not in labels[0], f"單日視窗不需要日期，目前是 {labels[0]!r}"
