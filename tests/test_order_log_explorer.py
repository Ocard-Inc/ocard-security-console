"""Order Log 在 Log Explorer 的能力與降級行為。

這張表與另外四張的差別不是「少做了幾個功能」，而是**資料本身沒有那些欄位**。
所以每一個不支援的地方都必須說出原因 —— 只說「不支援」的話，使用者會以為
是我們還沒做，然後去等一個永遠不會來的功能；更糟的是把「查不到」讀成
「這個對象不存在」。
"""
from __future__ import annotations

import pytest

from console.queries import endpoint_suggest, explorer

# Order Log 從 2026-01-01 起有資料，這個區間實測約 5 萬筆
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 01:00:00"}


def _filter(**overrides) -> explorer.ExplorerFilter:
    return explorer.ExplorerFilter(source="order", **WINDOW, **overrides)


# ── 支援的 ──────────────────────────────────────────────────────────

def test_endpoint_filter_is_supported_and_uses_url():
    assert explorer.filter_support("endpoint", "order") is None
    assert explorer.FILTER_COLUMN["order"] == "url"
    assert explorer.SUGGEST_EXPR["order"] == "url"


def test_actor_filter_is_supported():
    """操作者是 _admin（整數），但它是真的可以篩的。"""
    assert explorer.filter_support("actor", "order") is None


def test_endpoint_ranking_keeps_the_action_segment():
    """`url` 而不是 `controller/function` —— accept／deny／complete 要分得開。

    用 `concat(controller,'/',function)` 的話這三個動作全部收進 `v1/order` 一格，
    「誰在大量拒單」就從排名上消失了。
    """
    rows = explorer.ranking(_filter(), "endpoint", limit=20)["rows"]
    names = [r["name"] for r in rows]
    assert names, "這個區間應該要有資料"
    assert any("/" in n and n.count("/") >= 2 for n in names), (
        f"排名裡沒有任何帶動作段的 url，維度可能被改回 controller/function：{names}")


def test_endpoint_prefix_filter_actually_narrows():
    """endpoint 是前綴比對，貼一個 url 前綴要真的縮小結果。"""
    total = explorer.trend(_filter())["total"]
    narrowed = explorer.trend(_filter(endpoint="v1/order"))["total"]
    assert 0 < narrowed < total, (
        f"前綴篩選沒有縮小（全部 {total}、篩選後 {narrowed}）")


def test_suggested_endpoints_are_usable_as_filters():
    """不變量：每個建議值都是 FILTER_COLUMN 的合法前綴，拿去篩一定查得到資料。"""
    endpoint_suggest.clear_cache()
    rows = endpoint_suggest.suggest("order", WINDOW["start"], WINDOW["end"])["rows"]
    assert rows, "應該要有建議值"
    top = rows[0]["value"]
    assert explorer.trend(_filter(endpoint=top))["total"] > 0, (
        f"建議值 {top!r} 篩不到任何資料 —— SUGGEST_EXPR 與 FILTER_COLUMN 對不上")


# ── 不支援的，而且說得出原因 ────────────────────────────────────────

def test_source_ip_says_why_not_just_that_it_cannot():
    reason = explorer.filter_support("source_ip", "order")
    assert reason is not None, "Order Log 沒有來源 IP，不可以回「支援」"
    assert "ip" in reason.lower(), f"理由沒提到 ip 欄位：{reason}"
    assert "headers" in reason.lower(), f"理由沒提到 headers 欄位：{reason}"


def test_source_ip_filter_is_a_readable_error_not_a_keyerror():
    """`where_clause()` 要拋 FilterError（→ 400），不是 KeyError（→ 500）。"""
    with pytest.raises(explorer.FilterError) as exc:
        explorer.where_clause(_filter(source_ip="1.2.3.4"))
    assert "ip" in str(exc.value).lower()


def test_source_ranking_is_a_readable_error():
    with pytest.raises(explorer.FilterError) as exc:
        explorer.ranking(_filter(), "source")
    assert "source" in str(exc.value) or "來源" in str(exc.value)


def test_error_and_unique_resource_are_api_only():
    """Order Log 沒有 has_error 也沒有 order_number。"""
    with pytest.raises(explorer.FilterError):
        explorer.error_analysis(_filter())
    with pytest.raises(explorer.FilterError):
        explorer.unique_resource(_filter())


def test_detail_rows_do_not_invent_a_source_ip():
    """`source_ip` 必須是 None，不可以是空字串或某個看起來像 IP 的值。"""
    rows = explorer.detail(_filter(limit=20))["rows"]
    assert rows, "這個區間應該要有資料"
    for r in rows:
        assert r["source_ip"] is None, f"憑空生出了來源 IP：{r['source_ip']!r}"
        assert r["result"] == "—", f"沒有 status 欄位卻回了結果：{r['result']!r}"
        assert r["resource"] is None
        assert r["endpoint"], "endpoint 不該是空的"
