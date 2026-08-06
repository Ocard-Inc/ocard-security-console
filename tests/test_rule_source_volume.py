"""R15（backend 單一來源大量請求）的粒度成對驗證。

CLAUDE.md 記著三種「基線與 metric 不成對」的災難，全都是不報錯、只給錯的數字：
GROUP BY 粗一級讓門檻系統性偏高（R03 曾誤用 api_src_60m，實測 P99 差 26 倍）、
WHERE 少一個條件讓哨兵值拿一個不含自己的母體當門檻（R13 的 `_store > 0`）、
時間分桶與基線粒度不成對讓倍數憑空放大 12 倍。

行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py）。
"""
from __future__ import annotations

from console.checker import calibrate as calib
from console.core.ch import query
from console.core.config import settings
from console.queries import exprs
from console.rules import baseline
from console.rules.loader import load_rules

# 7/16 攻擊視窗。這段歷史資料穩定，而且它正是 R15 要抓的形狀
#（單一來源 131.143.215.229 在一小時內數十萬次）。
ATTACK = {"start": "2026-07-16 00:10:00", "end": "2026-07-16 01:10:00"}


def _rule():
    rule = next((r for r in load_rules() if r.id == "R15"), None)
    assert rule is not None, "找不到規則 R15"
    return rule


def test_r15_metric_equals_the_count_for_that_source():
    """metric 的單位必須是「該來源在視窗內的請求數」。

    對不上就表示 SQL 多了或少了條件，而事件頁顯示的 metric 會與使用者在
    Explorer 用同一個 IP 查到的筆數不一致。
    """
    rule = _rule()
    df = query(rule.sql, ATTACK)
    assert not df.empty, "7/16 00:10-01:10 應該有來源超過 R15 的 HAVING 門檻"
    top = df.sort_values("metric", ascending=False).iloc[0]
    direct = query(
        "SELECT count() AS n FROM ods_backend_sys_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s AND ip = %(ip)s",
        {**ATTACK, "ip": top["ip"]})
    assert int(top["metric"]) == int(direct["n"][0]), (
        f"R15 的 metric（{int(top['metric'])}）不等於 {top['ip']} 在同視窗的"
        f" count()（{int(direct['n'][0])}）—— SQL 有多餘或缺少的條件")


def test_r15_never_emits_an_empty_source():
    """空的 ip 不可以成為事件對象。

    `ip` 是 String 而不是 Nullable，空字串是「沒記到來源」。它會產生一個在
    Explorer 查不到東西的對象，而 drilldown 會查出「所有人做了什麼」。
    """
    rule = _rule()
    df = query(rule.sql, ATTACK)
    assert not (df["ip"] == "").any(), "R15 吐出了空的來源 IP"
    assert not df["ip"].isna().any(), "R15 吐出了 NULL 的來源 IP"


def test_backend_ip_60m_baseline_exists():
    """基線鍵必須存在，否則門檻靜靜退化成只有 static_floor。

    畫面上的門檻公式會寫「max(800, 同時段 P99×3)」—— 那個公式會是謊話。
    """
    base = baseline.get("backend_ip_60m")
    assert base is not None, (
        "baselines 裡沒有 backend_ip_60m —— 請跑 "
        "`uv run python -m console.checker.calibrate`（calibrate 段 3b）")
    assert base.samples > 0


def test_backend_ip_60m_population_matches_the_rule_group_by():
    """母體的 GROUP BY 與 WHERE 必須與 R15 的 SQL 成對。

    對帳方式：用同一個母體定義在近期一段區間現算一次分布，與 baselines 裡那
    一列比對數量級。粗一級（例如漏掉 `ip != ''` 而把所有無來源的列併成一個
    巨大的桶）會讓 p99 差一個數量級以上，這裡就會失敗。

    **視窗與排除條件必須跟 calibrate 一樣，不是寫死的日期字串。** calibrate
    的母體是「now-3d 往前 28 天、排除污染窗」的**滾動**視窗（見
    `calibrate._range()`），不是某個固定區間。原本這裡寫死
    `2026-07-08 ~ 08-05`：一個月後那段區間會落到 calibrate 樣本範圍之外，
    這個測試會開始比對兩個不同時期的分布而變得忽通忽不通；而且原本沒帶
    `exprs.exclusion_filter()`，7/16-17 那個污染窗會被算進「現算」的分布卻
    不在 baselines 那一列裡，兩邊的 p99 天生就不該相等。改成呼叫
    calibrate 用的同一個 `_range()` 與 `exclusion_filter()`，這個測試才會
    一直對比到「calibrate 真的會拿來算基線」的那段資料。
    """
    base = baseline.get("backend_ip_60m")
    assert base is not None, "先跑 calibrate"
    start, end = calib._range(settings()["baseline"]["window_days"])
    live = query(
        "SELECT quantileExact(0.99)(c) AS p99 FROM ("
        "  SELECT ip, toStartOfHour(create_time) AS b, count() AS c"
        "  FROM ods_backend_sys_log"
        f"  WHERE {exprs.time_filter()}{exprs.exclusion_filter()}"
        "    AND ip IS NOT NULL AND ip != ''"
        "  GROUP BY ip, b)",
        {"start": start, "end": end})
    live_p99 = float(live["p99"][0])
    assert live_p99 > 0, "對帳查詢沒有樣本 —— 區間或表名不對"
    ratio = max(base.p99, live_p99) / max(min(base.p99, live_p99), 1.0)
    assert ratio < 3.0, (
        f"backend_ip_60m 的 p99（{base.p99:.0f}）與用 R15 的 GROUP BY／WHERE "
        f"現算的 p99（{live_p99:.0f}）差 {ratio:.1f} 倍 —— 母體定義與規則不成對。"
        "檢查 calibrate 段 3b 的 GROUP BY 是否只有 ip、WHERE 是否帶 "
        "`ip IS NOT NULL AND ip != ''`（見 CLAUDE.md「粒度必須成對」）")
