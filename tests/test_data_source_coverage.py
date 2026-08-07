"""每個 `data_source` 都必須出現在「漏了會 500」的每一張對照表裡。

`health._MISSING_EXPR[table]`、`explorer._DETAIL_COLUMNS[source]`、
`explorer._PAYLOAD_COLUMNS[source]` 的查表失敗都是 **`KeyError`** ——
不是 `ChQueryError`，`health.source_health()` 的 `except ChQueryError` 接不到。
漏一個的症狀是 `/api/health`、`/api/overview`、`/api/explorer` 三個端點一起 500，
而 `/healthz` 不碰它們、照樣回 200，**部署看起來成功**。
（同 `tests/test_schema_migration.py` 守 `_SCHEMA` 與 `_ADD_COLUMNS` 漂移的理由。）

**刻意不含三張對照表**，因為它們合法地不覆蓋全部來源：

- `explorer.GROUP_BY["source"]` —— Order Log 真的沒有 `ip` 也沒有 `headers`，
  要求它有等於逼下一個人編一個假欄位。
- `explorer.FILTER_COLUMN` / `SUGGEST_EXPR` —— `ods_auth_log` 沒有 `function`
  欄位，本來就不支援 endpoint 篩選。

換句話說：這個檔案守的是「漏了會 500」的那幾張，不是「漏了會降級」的那幾張。
後者由 `explorer.filter_support()` 擋成可讀的 400。
"""
from __future__ import annotations

import pytest

from console.api import routes
from console.core.config import settings
from console.queries import explorer, health

SOURCES = tuple(settings()["data_sources"])

# **只有 `endpoint` 是每張表都做得到的。**
#
# 原本這裡還有 `brand` / `store` / `actor`，前提是「每張表都有 _brand 與
# _store，而 endpoint 與 actor 各表欄位不同但都有得對」。2026-08-07 接入的
# 五張表把這個前提打破了：一張都沒有 _brand / _store，而 batch 是排程觸發的、
# 根本沒有操作者。
#
# 那三個維度改由**綱要**與**逐維度的反向測試**守著（見下方
# test_brand_and_store_dimensions_follow_the_schema 與
# test_dimensions_that_are_missing_say_why）—— 要求每張表都有，會逼下一個人
# 為沒有那個欄位的表編一個假運算式，而那正是這個專案一再警告的錯誤。
#
# `source` 不在這裡的理由見模組說明（同一件事，只是更早就發生了）。
REQUIRED_DIMENSIONS = ("endpoint",)


def test_there_is_more_than_one_source():
    """防呆：settings 讀壞時上面的 parametrize 會變成空清單、整個檔案靜靜跳過。"""
    assert len(SOURCES) >= 4, f"data_sources 只有 {SOURCES}，settings.yaml 是否讀錯？"


@pytest.mark.parametrize("source", SOURCES)
def test_missing_expr_covers_every_source(source):
    table = settings()["data_sources"][source]["table"]
    assert table in health._MISSING_EXPR, (
        f"health._MISSING_EXPR 少了 {table}（來源 {source}）—— "
        "那是 KeyError 而不是 ChQueryError，會讓 /api/health、/api/overview、"
        "/api/explorer 三個端點一起 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_source_notes_cover_every_source(source):
    assert source in health._NOTES, (
        f"health._NOTES 少了 {source} —— 健康卡會沒有任何「這張表的資料限制」說明。")


@pytest.mark.parametrize("source", SOURCES)
def test_detail_columns_cover_every_source(source):
    assert source in explorer._DETAIL_COLUMNS, (
        f"explorer._DETAIL_COLUMNS 少了 {source} —— 逐筆明細會 KeyError → 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_payload_columns_cover_every_source(source):
    assert source in explorer._PAYLOAD_COLUMNS, (
        f"explorer._PAYLOAD_COLUMNS 少了 {source} —— 調閱原文會 KeyError → 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_data_limitations_cover_every_source(source):
    assert source in routes._LIMITATIONS_BY_SOURCE, (
        f"routes._LIMITATIONS_BY_SOURCE 少了 {source} —— 事件詳細頁的「資料限制」"
        "只會有五張表通用的兩句，說不出這張表自己的缺口。")


@pytest.mark.parametrize("dimension", REQUIRED_DIMENSIONS)
@pytest.mark.parametrize("source", SOURCES)
def test_group_by_covers_every_source(source, dimension):
    assert source in explorer.GROUP_BY[dimension], (
        f"explorer.GROUP_BY[{dimension!r}] 少了 {source} —— 該維度的排名會回 400，"
        "而畫面上那個選項看起來是正常功能。")


def test_source_dimension_is_deliberately_incomplete():
    """反向：`GROUP_BY["source"]` 不覆蓋全部來源是刻意的，不是漏的。

    有人「順手補齊」的話會需要為 Order Log 編一個假的來源 IP 運算式，
    而那正是這個專案一再警告的錯誤（把「沒有資料」偷換成一個看起來合理的值）。
    """
    assert "order" not in explorer.GROUP_BY["source"], (
        "Order Log 沒有 ip 也沒有 headers 欄位，不可以有來源 IP 運算式。")


def test_brand_and_store_dimensions_follow_the_schema():
    """`_brand` / `_store` 不再是「每張表都有」的假設。

    2026-08-07 接入的五張表沒有一張有這兩個真欄位。原本的
    `{k: … for k in _ALL_SOURCES}` 會讓那些來源的排名在 ClickHouse 端拋
    「Unknown expression or function identifier」→ 502，
    而畫面上那個選項看起來是正常功能。
    """
    from console.queries import source_schema
    for key in SOURCES:
        schema = source_schema.get(key)
        assert (key in explorer.GROUP_BY["brand"]) == (schema.brand_col is not None), (
            f"{key} 的 GROUP_BY['brand'] 與 source_schema.brand_col 不一致")
        assert (key in explorer.GROUP_BY["store"]) == (schema.store_col is not None), (
            f"{key} 的 GROUP_BY['store'] 與 source_schema.store_col 不一致")


@pytest.mark.parametrize("field", ("brand", "store", "actor", "source_ip"))
def test_dimensions_that_are_missing_say_why(field):
    """某個維度不可用時，`filter_support` 必須說出**原因**而不是回 None。

    回 None 的話 Explorer 會顯示那個輸入框，填進去查到 0 筆 ——
    而「這張表沒有這個欄位」與「這個值沒有活動」在畫面上一模一樣。

    這則測試取代了原本 `REQUIRED_DIMENSIONS` 裡的 brand/store/actor：
    從「每張表都必須有」改成「沒有的話必須說得出為什麼」。
    """
    dim = {"source_ip": "source"}.get(field, field)
    for key in SOURCES:
        if key in explorer.GROUP_BY[dim]:
            continue
        reason = explorer.filter_support(field, key)
        assert reason and len(reason) > 10, (
            f"{key} 沒有 {field} 維度，但 filter_support 沒有說出原因：{reason!r}")


def test_health_endpoint_lists_every_source(client):
    keys = {c["key"] for c in client.get("/api/health").json()["sources"]}
    assert keys == set(SOURCES)


def test_sparklines_cover_every_source(client):
    payload = client.get("/api/sparklines").json()
    assert set(payload["sources"]) == set(SOURCES)
