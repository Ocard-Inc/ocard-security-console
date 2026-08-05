"""Explorer 的 Request 趨勢必須畫在**使用者指定的區間**上，而不是資料的範圍。

原本 `trend()` 直接把 ClickHouse `GROUP BY b` 的結果丟給前端，沒有命中的桶根本
不存在。實測指定 08-01 00:00 ~ 08-05 08:00（104 小時）查一個只在 08-04 活動的
來源，只回 9 個點：

- 左端 83 小時的 0 完全不存在 → 圖從 08-04 11:00 開始畫，看不出「這段時間是 0」；
- **中間的 0 也被抽掉並接起來**：13:00 的下一點直接是 17:00，而 x 軸是 category
  （等距），所以 14–16 點的 0 在圖上變成一條往上爬的線 —— 那不是缺一格，
  是整條時間軸被壓縮，讀出來的結論與事實相反。

守四件事：

1. 每一個桶都在（含左端、中間、右端的 0），且相鄰桶恰好差一個分桶。
2. 右界不可超過資料實際落地的時間 —— 查「今天」時 end 是 23:59:59，
   填到 23:00 會畫出一段「還沒發生」的假 0（同 `trends.resolve_window`）。
   截掉了就必須在回應裡說出來。
3. zero-fill 之後 `rows` 永遠非空，所以「0 筆」的自我解釋（`empty_reason`）
   不可以跟著消失 —— 判斷依據要換成 `total`。
4. 桶數有上限。零填之後桶數只由 (區間 ÷ 分桶) 決定，180 天配 1 分鐘是 259,200
   個點；要明確拒絕並說怎麼改，不是靜靜吐出來。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.core.ch import query
from console.queries import exprs, explorer

# 不存在的帳號：這樣「每個桶都是 0」與資料無關，測試不會隨資料老化而漂移。
NOBODY = "這個帳號不存在-trend-window"


def _window(hours: int, ends_ago_hours: int = 24) -> dict:
    """一段完全在過去的區間（避開資料落地延遲的右界截斷）。"""
    end = timewin.align_bucket(timewin.taipei_now(), 60) - timedelta(hours=ends_ago_hours)
    return {"start": timewin.fmt(end - timedelta(hours=hours)), "end": timewin.fmt(end)}


def _filter(**kw) -> explorer.ExplorerFilter:
    return explorer.ExplorerFilter(source="admin", **kw)


def _buckets(rows) -> list[str]:
    return [r["bucket"] for r in rows]


def test_empty_result_still_covers_the_whole_requested_window():
    """完全沒有命中時，圖仍要畫滿指定區間的每一個桶（全部是 0）。"""
    w = _window(6)
    d = explorer.trend(_filter(actor=NOBODY, **w), "1h")
    assert d["total"] == 0
    assert len(d["rows"]) == 6, f"6 小時 × 1 小時桶應有 6 個點，實得 {len(d['rows'])}"
    assert d["rows"][0]["bucket"] == w["start"], "第一個桶必須是使用者指定的開始時間"
    # 時間過濾是 [start, end)，所以最後一個桶是 end 前一秒所屬的那個
    assert d["rows"][-1]["bucket"] == timewin.fmt(
        timewin.parse(w["end"]) - timedelta(hours=1))
    assert all(r["count"] == 0 for r in d["rows"])


def _sparse_source_ip(w: dict) -> str:
    """在區間內找一個活動稀疏的來源 —— 這樣「中間一定有空桶」不靠運氣。"""
    df = query(
        "SELECT ip, count() AS c FROM ods_admin_log"
        f" WHERE {exprs.time_filter()} AND ip != ''"
        " GROUP BY ip HAVING c <= 3 ORDER BY c LIMIT 1",
        {"start": w["start"], "end": w["end"]})
    if not len(df):
        pytest.skip("這個區間找不到活動稀疏的來源，換不出中間的空桶")
    return str(df.iloc[0]["ip"])


def test_middle_gaps_are_filled_not_collapsed_into_a_slope():
    """中間的 0 必須是 0，不是被抽掉 —— x 軸是 category（等距），
    抽掉之後 13:00 的下一點變成 17:00，圖上是一條往上爬的線而不是缺三格。"""
    w = _window(12)
    d = explorer.trend(_filter(source_ip=_sparse_source_ip(w), **w), "1h")
    assert len(d["rows"]) == 12, f"12 小時 × 1 小時桶應有 12 個點，實得 {len(d['rows'])}"
    stamps = [timewin.parse(b) for b in _buckets(d["rows"])]
    step = timedelta(minutes=d["bucket_minutes"])
    gaps = [(timewin.fmt(a), timewin.fmt(b)) for a, b in zip(stamps, stamps[1:])
            if b - a != step]
    assert not gaps, f"桶不連續（中間的 0 被抽掉了）：{gaps[:5]}"
    counts = [r["count"] for r in d["rows"]]
    assert any(c == 0 for c in counts) and any(c > 0 for c in counts), (
        "這個對象在區間內既該有空桶也該有命中，否則這條測試沒驗到東西")


def test_zero_fill_does_not_change_the_totals():
    """零填只補 0，不可以動到實際數字（明細與排名讀的是同一個篩選）。"""
    w = _window(6)
    f = _filter(**w)
    d = explorer.trend(f, "1h")
    assert d["total"] == explorer.detail(f)["total"]
    # 圖的總和必須等於 total。不相等表示有命中的桶落在畫出來的區間之外
    # （右界截短時最容易發生）—— 那是一張少畫了資料而不會報錯的圖。
    assert sum(r["count"] for r in d["rows"]) == d["total"]


def test_today_chart_still_shows_every_bucket_it_has_landed():
    """查「今天」時，落地區間內的每一個桶都要在（含還沒有流量的那幾個 0）。"""
    today = timewin.taipei_now().strftime("%Y-%m-%d")
    d = explorer.trend(
        _filter(start=f"{today} 00:00:00", end=f"{today} 23:59:59"), "1h")
    assert d["rows"][0]["bucket"] == f"{today} 00:00:00"
    assert sum(r["count"] for r in d["rows"]) == d["total"]
    expected = int((timewin.parse(d["rows"][-1]["bucket"])
                    - timewin.parse(d["rows"][0]["bucket"])).total_seconds() // 3600) + 1
    assert len(d["rows"]) == expected, "落地區間內的桶不連續"


def test_future_tail_is_not_drawn_as_zero_and_says_so():
    """查「今天整天」時右界要退到資料落地時間，並在回應裡說明。

    填到 23:00 的話尾端會是一段「還沒發生」的假 0，而畫面上與「這段時間沒有活動」
    長得一模一樣（同 `trends.resolve_window` 的教訓）。
    """
    today = timewin.taipei_now().strftime("%Y-%m-%d")
    d = explorer.trend(
        _filter(start=f"{today} 00:00:00", end=f"{today} 23:59:59"), "1h")
    last = timewin.parse(d["rows"][-1]["bucket"])
    assert last <= timewin.align_bucket(timewin.effective_now(), 60), (
        f"畫到了 {last}，超過資料落地時間 {timewin.effective_now()} —— 尾端是假的 0")
    assert d["window_note"], "右界被截短了卻沒有任何說明"


def test_bucket_count_is_capped_with_an_actionable_message():
    """區間 ÷ 分桶 太多點時明確拒絕，不要回一份沒人畫得出來的 JSON。"""
    w = _window(24 * 170, ends_ago_hours=24)
    with pytest.raises(explorer.FilterError) as exc:
        explorer.trend(_filter(**w), "1m")
    assert "分桶" in str(exc.value), f"訊息沒說怎麼改：{exc.value}"


def test_empty_reason_survives_zero_fill(client):
    """`rows` 因為零填而永遠非空，「0 筆」的解釋不可以跟著消失。

    這是這個專案一再警告的那件事：把「沒有資料」渲染成「沒有發生」。
    """
    r = client.post("/api/explorer", json={
        "source": "admin", "analysis": "trend", "bucket": "1h",
        "actor": NOBODY, **_window(6)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["rows"], "零填之後應該有一整排 0"
    assert body.get("empty_reason"), "0 筆卻沒有任何說明（零填把 empty 判斷弄丟了）"
