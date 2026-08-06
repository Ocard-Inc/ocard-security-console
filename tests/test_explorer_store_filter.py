"""Explorer 依分店（`_store`）反查。

存在的理由：「長期持續的單店濫用」那條規則的對象是 (品牌 × 分店)，而
`api/drilldown.py` 只有在 `_FILTER_BY_COL` 找得到對照時才會產生篩選條件 ——
沒有對照就等於**沒有對象條件**，事件詳細頁的「在 Log Explorer 查此對象」會查出
「所有人做了什麼」而不是這個事件（見 `test_event_drilldown.py` 的模組說明）。

守四件事，前三件與 `test_explorer_entity_filter.py` 同一組（帳號／來源 IP）：

1. **篩選真的有作用** —— 宣告了欄位但 `where_clause()` 沒讀，是這個檔案裡最
   容易發生也最安靜的失敗：使用者填了以為有篩，回的是全部資料。
2. **完全相等，不是前綴或子字串** —— `_store` 是整數，`276` 不可以命中 `27681`。
3. **排名裡看到的值貼回去查得到** —— 篩選運算式複用 `GROUP_BY["store"]`，
   兩邊各寫一份遲早不一致。
4. **`entity_meta("store", ...)` 解得出來** —— `queries/entity.py` 的
   `from_filters()` 靠它把 drilldown 的篩選轉成事件對象視角的 `Dim`；
   解不出來的話那一整塊面板會靜靜消失。

分店編號的兩個哨兵值（`-1` 品牌層級操作、`0` 未填）**刻意仍然可以篩** ——
它們是合法的調查對象。不可篩的是「用它們當母體」，那是 calibrate 8b 的事。
"""
from __future__ import annotations

import pytest

from console.queries import entity, explorer

SOURCES = ("api", "backend", "admin", "auth", "order")

# 品牌 1180 / 分店 27681：2026-08-01 有 171,966 次 Api2/GetProfile
# （長期持續的單店濫用，本輪調查發現，到 8/03 仍在跑）。
STORE = 27681
WINDOW = {"start": "2026-08-01 00:00:00", "end": "2026-08-02 00:00:00"}


def _post(client, **overrides):
    body = {"source": "api", "analysis": "detail", "limit": 20, **WINDOW}
    body.update(overrides)
    r = client.post("/api/explorer", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_store_filter_is_supported_on_every_source():
    """五張表都有 `_store` 欄位，所以沒有任何一張該拒絕這個篩選。"""
    for source in SOURCES:
        assert explorer.filter_support("store", source) is None, (
            f"{source} 不支援 store 篩選，但該表有 _store 欄位")


def test_store_filter_narrows_the_result(client):
    everything = _post(client)["total"]
    filtered = _post(client, store=STORE)
    assert filtered["total"] > 0, f"分店 {STORE} 在這個區間應有資料"
    assert filtered["total"] < everything, (
        "分店篩選沒有縮小結果 —— where_clause() 可能沒讀這個欄位")


def test_store_filter_returns_only_that_store(client):
    rows = _post(client, store=STORE)["rows"]
    assert rows, "應有明細列可供檢查"
    assert all(r["store"] == STORE for r in rows), (
        f"回傳的列裡有不屬於分店 {STORE} 的資料")


def test_store_filter_is_exact_not_substring(client):
    """`276` 是 `27681` 的前綴 —— 相等比對下必須是 0 筆。

    寫成 `startsWith(toString(_store), ...)` 或 `LIKE` 的話，一個「分店 276」
    的查詢會靜靜地把 27681 的資料算進來，數字比實際大而且不會報錯。
    """
    assert _post(client, store=276)["total"] == 0


def test_store_filter_rejects_non_integer(client):
    """解不出整數要 400，不可以靜靜把篩選丟掉（那會回一份「全分店」的結果）。"""
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "detail", "store": "不是數字", **WINDOW})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("source", ["api", "backend", "admin"])
def test_ranking_values_can_be_pasted_back_as_store_filter(client, source):
    """不變量：分店排名裡看到的值，貼回篩選器就查得到。

    `ranking()` 用 `GROUP_BY["store"]` 的運算式，篩選用 `where_clause()` 的
    條件 —— 這條測試在三張表上實際驗證兩者指的是同一個東西。
    """
    rank = explorer.ranking(
        explorer.ExplorerFilter(source=source, **WINDOW), "store", limit=5)
    names = [r["name"] for r in rank["rows"] if r["name"] != "（空）"]
    if not names:
        pytest.skip(f"{source} 在這個區間沒有分店資料")
    back = _post(client, source=source, store=int(names[0]))
    assert back["total"] > 0, (
        f"{source} 的分店排名值 {names[0]!r} 貼回 store 查不到 —— "
        "GROUP_BY 與 where_clause 的運算式不一致")


@pytest.mark.parametrize("source", SOURCES)
def test_entity_meta_resolves_store(source):
    """`from_filters()` 要靠 `entity_meta` 把 store 轉成事件對象視角的 Dim。"""
    meta = explorer.entity_meta("store", source)
    assert meta is not None, f"{source} 的 entity_meta('store') 解不出來"
    expr, mask, label = meta
    assert "_store" in expr, f"分店的運算式應該讀 _store，實際是 {expr!r}"
    assert mask is None, "分店編號是營運資訊，不遮罩（同品牌）"
    assert label


def test_from_filters_builds_a_store_dimension():
    ref = entity.from_filters("api", {"brand": 1180, "store": STORE})
    assert ref is not None
    fields = {d.field for d in ref.dims}
    assert "store" in fields, (
        "drilldown 給了 store 篩選但 EntityRef 沒有這個維度 —— "
        "事件對象視角會退回品牌層級，數字與事件對不上")
