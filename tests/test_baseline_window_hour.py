"""基線的 (hour, day_class) 必須對應「視窗實際涵蓋的時間」，不是 window_end。

CLAUDE.md 已經記了三種「基線與 metric 必須成對」的坑（時間粒度、對象粒度、
定義母體的 WHERE）。**這是第四種：同一個粒度下取錯了桶。**

`engine._resolve_threshold()` 原本以 `window_end.hour` 查基線，而 60 分鐘視窗是
`[window_end - 60m, window_end)` —— 每小時必有一個 tick 的 window_end 落在整點，
那時視窗內容剛好是**前一個**完整整點小時，卻拿**下一個**小時的分位數當門檻。
而基線是 `toStartOfHour(create_time)` 分桶算的（calibrate.py 第 5 段）。

2026-08-06 正式環境實測的症狀：20 筆 R04 事件裡 19 筆的 window_start 落在整點，
視窗起始小時集中在傍晚 19/20/21（流量逐小時陡降的時段），metric/threshold 比值
全部在 1.00~1.14，同一組 endpoint 每天重複。

    EVT-0109  Api2/GivePoint   21:00~22:00   metric 2,117
              用到 h22 的門檻 2,000（med 651）→ 命中
              視窗自己 h21 的門檻 5,083（med 2,006）→ 不該命中
              而 2,117 正是那一小時的實際計數，前七天同時段是 1,768~2,097

`Api2/MixTrans` 在 h21 只要 0.58 倍於自己的中位數就告警（設計上是 2.88 倍）——
那格等於「保證每個工作日發一次」。

**反方向更嚴重：流量上升的時段門檻被抬高、規則靜靜漏抓。** 掃過
`api_endpoint_60m` 全部 182 個動態門檻真正生效的格子，44 格偏低（誤報）、
39 格偏高（漏抓）：`Api2/GetProfile` 在 h07 要 9.22 倍才叫（應該 3.04 倍）。
兩個方向都不會報錯，只會給錯的結論 —— 所以這裡用行為驗證，不比對呼叫參數。

跨午夜的視窗還會連 day_class 一起取錯（週一 00:00 的視窗是週日的 23 點，
那是 weekend 的母體）。同一個修正一起解掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from console.core import timewin
from console.queries import quick_templates
from console.rules import baseline, engine
from console.rules.effective import effective_rules
from console.rules.model import Rule, Threshold
from console.store import db

# 合成的基線鍵：h21 是高量小時、h22 是低量小時，比照傍晚的真實形狀。
# 刻意不用真實的 metric_key —— 真實基線每天 06:00 重算，值會漂移。
KEY = "test_window_hour_60m"
FACTOR = 2.0
# (hour, day_class) → (median, p95)。四格刻意互不相同 —— 取錯任何一個維度都要
# 得到不同的門檻，這樣斷言才分辨得出「小時對了但 day_class 錯了」。
CELLS = {
    (21, "weekday"): (6000.0, 9000.0),
    (22, "weekday"): (300.0, 500.0),
    (23, "weekday"): (200.0, 400.0),
    (23, "weekend"): (2000.0, 3000.0),
    # 給跨午夜那條測試用：取錯的話會查到這一格，而它是「有值但錯」——
    # 比「查不到基線於是退回 static_floor」更能證明測到的是對的東西。
    (0, "weekday"): (5.0, 10.0),
}
# h21 的正常量：低於 h21 的門檻（9000×2），高於 h22 的門檻（500×2）。
NORMAL_FOR_HOUR_21 = 7000.0


@pytest.fixture
def seeded_baseline():
    """為 KEY 播種 CELLS 那幾格。

    測完刪掉：session 範圍的 DB 複本是共用的，留著假基線會讓後面的測試
    以錯誤的理由通過或失敗（見 commit b346d78 的同類問題）。
    """
    baseline.upsert_many(
        [(KEY, hour, dc, median, p95, p95 * 1.1, p95 * 1.2, 18)
         for (hour, dc), (median, p95) in CELLS.items()],
        "2026-08-06 06:00:00")
    yield
    with db.tx() as conn:
        conn.execute("DELETE FROM baselines WHERE metric_key = ?", (KEY,))


def _rule(window_minutes: int = 60) -> Rule:
    return Rule(
        id="TEST", name="視窗小時測試", severity="P3", source="api",
        kind="sql_threshold", window_minutes=window_minutes, enabled=True,
        sql="SELECT 1 AS metric",
        threshold=Threshold(static_floor=0.0, baseline_key=KEY,
                            stat="p95", factor=FACTOR),
    )


def test_threshold_comes_from_the_hour_the_window_covers(seeded_baseline):
    """window_end 落在整點時，視窗是前一小時 —— 門檻要用前一小時的基線。

    這是實際發生的誤報：h21 的正常量拿 h22 的門檻來比就會命中。
    """
    window_end = datetime(2026, 8, 6, 22, 0, 0)   # 週四；視窗 = [21:00, 22:00)
    median, p95 = CELLS[(21, "weekday")]
    threshold, base = engine._resolve_threshold(_rule(), {}, window_end)

    assert threshold == pytest.approx(p95 * FACTOR)
    assert base is not None and base.median == pytest.approx(median)
    # 行為斷言：h21 的正常量不可以命中
    assert NORMAL_FOR_HOUR_21 < threshold


def test_threshold_follows_the_window_when_it_moves_into_the_next_hour(seeded_baseline):
    """視窗大半落在 h22 時就該用 h22 的基線 —— 修正不是「一律減一小時」。

    少了這條，把 window_end.hour 改成 (window_end.hour - 1) 也會過，
    而那在 22:40 的 tick（視窗 40 分鐘在 h22）上是同一個錯誤的鏡像。
    """
    window_end = datetime(2026, 8, 6, 22, 45, 0)  # 視窗 = [21:45, 22:45)，45 分鐘在 h22
    median, p95 = CELLS[(22, "weekday")]
    threshold, base = engine._resolve_threshold(_rule(), {}, window_end)

    assert threshold == pytest.approx(p95 * FACTOR)
    assert base is not None and base.median == pytest.approx(median)


def test_day_class_follows_the_window_across_midnight(seeded_baseline):
    """週一 00:00 的視窗是**週日**的 23 點 —— 母體是 weekend，不是 weekday。

    用 weekday 的母體去比週末的量，兩個方向的錯都可能發生而且完全不報錯。
    CELLS 的 (23, weekday) 與 (23, weekend) 刻意差 7.5 倍，取錯就分辨得出來。
    """
    monday_midnight = datetime(2026, 8, 10, 0, 0, 0)
    assert monday_midnight.weekday() == 0        # 前一小時是週日 23:00
    weekend_median, weekend_p95 = CELLS[(23, "weekend")]
    threshold, base = engine._resolve_threshold(_rule(), {}, monday_midnight)

    assert threshold == pytest.approx(weekend_p95 * FACTOR)
    assert base is not None and base.median == pytest.approx(weekend_median)


@pytest.mark.parametrize("window_minutes", [5, 10, 15, 60, 120])
@pytest.mark.parametrize("minute", [0, 5, 25, 30, 35, 55])
def test_baseline_hour_is_the_hour_holding_most_of_the_window(window_minutes, minute):
    """基線小時 = 視窗涵蓋分鐘數最多的那一小時。

    獨立的 oracle：逐分鐘數過去，不重複實作端點的算法。
    涵蓋短視窗的規則（R09 5m、R01/R06 10m、R07A/B、R10A/B 15m）——
    它們只在跨整點的那幾個 tick 會錯，但錯的方式一樣。
    """
    window_end = datetime(2026, 8, 6, 22, minute, 0)
    covered: dict[int, int] = {}
    for offset in range(window_minutes):
        hour = (window_end - timedelta(minutes=offset + 1)).hour
        covered[hour] = covered.get(hour, 0) + 1
    expected = max(covered, key=lambda h: covered[h])

    at = engine._baseline_at(_rule(window_minutes=window_minutes), window_end)
    assert at.hour == expected, f"視窗涵蓋分鐘數 {covered}"


def test_every_rule_with_a_baseline_reads_a_point_inside_its_own_window():
    """所有規則（含日後新增的）都必須從自己的視窗內取基線。

    這條刻意跑真實的 effective_rules()：新規則寫了 baseline_key 卻在別的地方
    另外算小時的話，這裡會失敗而不是靜靜地給錯門檻。
    """
    window_end = datetime(2026, 8, 6, 22, 0, 0)
    checked = []
    for rule in effective_rules():
        if rule.threshold is None or not rule.threshold.baseline_key:
            continue
        at = engine._baseline_at(rule, window_end)
        window_start = window_end - timedelta(minutes=rule.window_minutes)
        assert window_start <= at < window_end, (
            f"{rule.id} 的基線取樣點 {at} 落在視窗 [{window_start}, {window_end}) 之外")
        checked.append(rule.id)
    assert checked, "沒有任何規則帶 baseline_key —— 這條測試失去意義"


# --- Explorer 快速範本（同一個 bug 的顯示端）-------------------------------
#
# `_top_endpoints` / `_cell_lookup` / `_orderlist_traversal` 也拿 window_end 的小時
# 查基線，回的是畫面上的 median 與「倍數」。那不會發告警，但使用者就是拿那個數字
# 判斷「這個量正不正常」—— 錯的方向與 R04 完全一樣。


def test_quick_template_window_hands_back_a_point_inside_the_window(monkeypatch):
    """`_win()` 的第三個元素是「基線該查哪一小時」，不是視窗右界。

    回 window_end 的話，預設的 60 分鐘視窗在整點的 tick 上會查到下一小時
    —— 與 engine 那邊同一個錯，只是症狀是畫面上的倍數而不是告警。
    """
    end = datetime(2026, 8, 6, 22, 0, 0)
    monkeypatch.setattr(quick_templates.timewin, "effective_now", lambda: end)
    s, e, at = quick_templates._win({}, default_minutes=60)

    assert (timewin.parse(s), timewin.parse(e)) == (end - timedelta(minutes=60), end)
    assert at == end - timedelta(minutes=30)
    assert at.hour == 21


def test_quick_template_window_scales_the_point_to_a_user_supplied_range():
    """使用者自訂絕對區間時也要取該區間的中點，不是寫死減 30 分鐘。"""
    s, e, at = quick_templates._win(
        {"start": "2026-08-06 08:00:00", "end": "2026-08-06 12:00:00"})

    assert (s, e) == ("2026-08-06 08:00:00", "2026-08-06 12:00:00")
    assert at == datetime(2026, 8, 6, 10, 0, 0)
