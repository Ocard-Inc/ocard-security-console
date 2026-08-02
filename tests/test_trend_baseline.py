"""趨勢基線的迴歸測試。

舊版 `web/lib.js:89-93` 只讀 `buckets[0]` 的 median／P95，把「同時段 median–P95 範圍」
畫成一條橫跨全圖的平帶。6 小時視窗下 buckets[0] 正好是當日尖峰，位置誤差達 25 倍。

後端資料本來就是逐 bucket 正確的，錯的是前端。這些測試把「基線會隨時間變化」
這件事釘住 —— 一開始若有這個測試，那個假設就不會成立那麼久。

首頁改成 2×2 小倍數之後，四條線各自都要有自己的同時段基線（以前只有 api 與
login_success 被讀出來，backend 與 login_failed 明明算好了卻沒用），所以下面
一律對四條線都驗。
"""
from __future__ import annotations

import pytest

# 四條線與其對應的基線欄位前綴
SERIES = ["api", "backend", "login_success", "login_failed"]


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
