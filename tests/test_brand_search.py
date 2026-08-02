"""品牌選擇器的搜尋來源（ClickHouse `ods_brand`）。

實際連 ClickHouse —— 這裡驗的正是「照抄 ROS 的 MySQL SQL 會壞掉」的那幾點
（`FINAL` 去重、`ILIKE` 而非 `LIKE`），用假資料驗不出來。
"""
from __future__ import annotations

import pytest

from console.queries import brand_search


# 生產資料裡實際存在、且用來驗證特定行為的品牌編號。
# 1180 在原始表有兩列（「瓦城泰統集團」2025-10-20 / 「wa10 瓦城」2026-07-31），
# 是 FINAL 去重的樣本。
BRAND_WA10 = 1180

# 驗跳脫用：要大到「沒跳脫就會回滿」才有鑑別力
BIG_LIMIT = brand_search.MAX_LIMIT


def test_blank_query_returns_empty_without_querying(monkeypatch):
    """空字串不該打 ClickHouse —— 焦點事件會觸發它，一次整表掃描沒有意義。"""
    def _boom(*args, **kwargs):
        raise AssertionError("空 q 不應該查詢 ClickHouse")
    monkeypatch.setattr(brand_search, "query", _boom)
    assert brand_search.search("") == []
    assert brand_search.search("   ") == []


def test_exact_id_match_is_first():
    rows = brand_search.search(str(BRAND_WA10))
    assert rows, "1180 應該找得到"
    assert rows[0]["idx"] == BRAND_WA10


def test_exact_id_outranks_active_first():
    """打完整編號就是要那一個，即使它已停用，也排在啟用中的前綴命中之前。"""
    rows = brand_search.search("118")
    assert rows[0]["idx"] == 118
    assert rows[0]["status"] != "active", "118 Broccoli Beer 在生產資料是停用的"
    assert any(r["idx"] != 118 for r in rows), "前綴命中（1189、1188…）也要列出來"


def test_final_dedupes_replacing_merge_tree():
    """ods_brand 是 ReplacingMergeTree，不加 FINAL 同一 idx 會出現多個舊名字。"""
    rows = brand_search.search("瓦城", limit=50)
    ids = [r["idx"] for r in rows]
    assert len(ids) == len(set(ids)), f"idx 重複，FINAL 沒生效：{ids}"
    wa10 = next(r for r in rows if r["idx"] == BRAND_WA10)
    assert wa10["name"] == "wa10 瓦城", "應取 update_time 最新的版本"


def test_search_is_case_insensitive():
    """ClickHouse 的 LIKE 大小寫敏感，必須用 ILIKE。"""
    lower = [r["idx"] for r in brand_search.search("coffee")]
    upper = [r["idx"] for r in brand_search.search("COFFEE")]
    assert lower and lower == upper


def test_like_wildcards_are_escaped():
    """使用者打 % 不該變成「匹配全部」，而是找名稱裡真的含 % 的品牌。

    生產資料裡確實有這種品牌（idx 8158「Lazy %」），所以不能斷言回空 ——
    要斷言每一筆都真的含 %。沒跳脫的話 `%%%` 會匹配全部、回滿 limit 筆。
    """
    rows = brand_search.search("%", limit=BIG_LIMIT)
    assert len(rows) < BIG_LIMIT, "回滿 limit 表示 % 被當成萬用字元了"
    for r in rows:
        assert "%" in r["name"] or "%" in (r["code"] or ""), r

    assert brand_search.escape_like("100%_a\\b") == "100\\%\\_a\\\\b"


def test_limit_is_clamped():
    assert len(brand_search.search("a", limit=3)) <= 3
    assert len(brand_search.search("a", limit=9999)) <= brand_search.MAX_LIMIT
    with pytest.raises(ValueError):
        brand_search.search("a", limit=0)


def test_row_shape_is_json_safe():
    """id 是 Nullable(String) → pandas 給 pd.NA；idx 是 Int64 → numpy int64。
    兩者直接進 JSON 都會炸，必須在模組內正規化。"""
    import json
    rows = brand_search.search("a", limit=20)
    assert rows
    json.dumps(rows)  # 不可拋 TypeError
    for r in rows:
        assert set(r) == {"idx", "name", "code", "country", "status"}
        assert isinstance(r["idx"], int) and not isinstance(r["idx"], bool)
        assert isinstance(r["name"], str)
        assert r["code"] is None or isinstance(r["code"], str)
        assert r["status"] in ("active", "disabled", "deleted")


def test_deleted_outranks_disabled_in_status():
    """同時 deleted=1、enable=1 要顯示「已刪除」—— 那是更強的訊號。"""
    assert brand_search.status_of(enable=1, deleted=1) == "deleted"
    assert brand_search.status_of(enable=0, deleted=1) == "deleted"
    assert brand_search.status_of(enable=0, deleted=0) == "disabled"
    assert brand_search.status_of(enable=1, deleted=0) == "active"


def test_disabled_brands_are_searchable():
    """這是調查工具：停用的品牌照樣有歷史 log，搜不到等於讓調查斷在這裡。"""
    rows = brand_search.search("瓦城", limit=50)
    assert any(r["status"] != "active" for r in rows)
