"""R13 單一分店持續高量呼叫（`config/rules/r13_store_sustained_volume.yaml`）。

這條規則補的是既有規則結構上抓不到的那一類：**長期持續**的濫用。
R04（endpoint 級）的基線是 endpoint 自己的歷史，濫用者就是基線
（實測 Api2/TransDetail 的 p95 = 22 萬/小時 → 門檻 44 萬，而實際峰值 22.8 萬，
整個 7 月一次都沒叫）。R10B（品牌級）的母體太不同質 —— 每小時計數的
p99/median 是 365 倍，被迫用 `static_floor: 30000`，2026-07-29 那天四個持續
濫用的品牌只有 414 過得了那個地板。改以 (品牌 × 分店) 為對象後同一份樣本的
p99/median 降到 24 倍，門檻可以低兩個數量級。

守的是 CLAUDE.md 那條「基線與 metric 的**對象粒度**必須成對」，而且刻意用
**行為**驗證而不是比對 SQL 字串：

1. `metric` 的單位必須等於基線母體的單位（該分店在該視窗的總記錄數）。
   不成對的症狀不是錯誤，是一個看起來精確的錯倍數。
2. 規則不可以吐出 `_store <= 0` 的對象。`-1`（品牌層級操作，7 月橫跨 301 個
   品牌、132 萬次）與 `0`（未填）在 calibrate 8b 的母體裡被濾掉了 ——
   規則這邊漏了同一個條件的話，那兩個哨兵值會拿一個不含自己的母體當門檻，
   而且事件對象會是一個在 Explorer 查不到東西的「分店 -1」。
3. 時間粒度必須與基線的分桶一致（60 分鐘）。
"""
from __future__ import annotations

import pytest

from console.api import drilldown
from console.core import stores
from console.core.ch import query
from console.rules import baseline, engine
from console.rules.loader import load_rules

RULE_ID = "R13"
BASELINE_KEY = "brand_store_60m"

# 2026-08-01 12:00–13:00：WA10 APP（分店 27681 / 品牌 1180）當時每小時上萬次
# Api2/GetProfile，是本輪調查找到、到 8/03 仍在跑的持續高量對象。
WINDOW = {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}


@pytest.fixture(scope="module")
def rule():
    found = next((r for r in load_rules() if r.id == RULE_ID), None)
    if found is None:
        pytest.fail(f"{RULE_ID} 不存在 —— config/rules/ 少了這條規則")
    return found


@pytest.fixture(scope="module")
def rows(rule):
    return query(rule.sql, WINDOW).to_dict(orient="records")


def test_threshold_reads_the_paired_population_baseline(rule):
    assert rule.threshold is not None
    assert rule.threshold.baseline_key == BASELINE_KEY
    assert rule.threshold.population is True, (
        "母體是跨對象的分布，不是這個分店自己的歷史 —— population=False 會讓引擎"
        "算出「相對自身」的倍數，而那個數字對母體分布沒有意義")


def test_window_matches_the_baseline_bucket(rule):
    assert rule.window_minutes == 60, (
        f"{BASELINE_KEY} 的語意是「每小時桶內計數」的分布；視窗不是 60 分鐘的話"
        "原始計數與基線不同粒度，會憑空生出假倍數")


def test_baseline_row_exists():
    b = baseline.get(BASELINE_KEY)
    assert b is not None, (
        f"SQLite 沒有 {BASELINE_KEY} 的基線 —— 要先跑 console.checker.calibrate")
    assert b.p99 > 0 and b.samples > 0


def test_rule_yields_rows_for_the_known_sustained_object(rows):
    assert rows, f"{WINDOW} 應該命中至少一個對象（WA10 APP 當時每小時上萬次）"
    keys = {(int(r["_brand"]), int(r["_store"])) for r in rows}
    assert (1180, 27681) in keys, (
        f"已知的持續高量對象沒有被命中，實際命中：{sorted(keys)}")


def test_rule_never_yields_sentinel_stores(rows):
    """`_store <= 0` 必須被濾掉 —— 母體（calibrate 8b）也濾了同一組。"""
    bad = [int(r["_store"]) for r in rows if int(r["_store"]) <= 0]
    assert not bad, (
        f"命中了哨兵分店 {sorted(set(bad))} —— SQL 少了 `_store > 0`，"
        "它們會拿一個不含自己的母體當門檻，事件對象也查不到東西")


def test_metric_unit_matches_the_baseline_population(rows):
    """metric 必須是「該分店在該視窗的總記錄數」，與母體同單位。

    多一個 WHERE（例如只算某類 endpoint）或少一個都會讓兩邊不成對。
    這裡直接跟母體的定義對帳，而不是比對 SQL 字串。
    """
    top = max(rows, key=lambda r: float(r["metric"]))
    df = query(
        "SELECT count() AS c FROM ods_api_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        " AND _brand = %(b)s AND _store = %(s)s",
        {**WINDOW, "b": int(top["_brand"]), "s": int(top["_store"])})
    expected = int(df.iloc[0]["c"])
    assert float(top["metric"]) == expected, (
        f"分店 {top['_store']} 的 metric 是 {top['metric']}，但母體單位下是 "
        f"{expected} —— 規則 SQL 的 WHERE 與 calibrate 8b 不成對")


def test_entity_label_names_the_store_but_the_key_keeps_the_number(rule):
    """標籤要看得懂是哪家店，去重鍵不可以跟著名稱漂移。

    `27681` 這個裸編號沒有人認得出是「WA10 APP」，而 Slack 通知與事件清單就只有
    這一行字。反過來，名稱是會改的 —— 把名稱放進 `entity_key` 的話，改一次店名
    就會讓同一個對象變成新事件（既有的 active 事件從此不再更新，三個 tick 後被
    標成「已恢復」）。品牌欄位早就是這樣處理的，分店必須一致。
    """
    key, label, _ = engine.entity_parts(rule, {"_brand": 1180, "_store": 27681})
    assert key == f"{RULE_ID}|1180|27681", (
        f"去重鍵含了編號以外的東西：{key!r}")
    assert "27681" in label
    assert label != f"{RULE_ID}|1180|27681"
    assert stores.label(27681) in label, (
        f"標籤沒有帶分店名稱：{label!r} —— 應含 {stores.label(27681)!r}")


def test_entity_columns_map_into_explorer(rule):
    """兩個 entity 欄位都要帶得進 Log Explorer，否則事件頁會查出「所有人做了什麼」。"""
    for f in rule.entity:
        assert f.col in drilldown._FILTER_BY_COL, (
            f"entity col={f.col!r} 沒有對應的 Explorer 篩選欄位")
