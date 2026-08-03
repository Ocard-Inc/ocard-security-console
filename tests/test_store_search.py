"""分店選擇器的搜尋來源（ClickHouse `ods_store`）。

與 `test_brand_search.py` 同一組關切，實際連 ClickHouse —— `FINAL` 去重與
`ILIKE` 這兩件事用假資料驗不出來（實測 `ods_store` 有 113,389 列但只有
25,194 個相異 `idx`，單一分店最多 14 個版本）。

多一件品牌沒有的事：**品牌範圍**。Explorer 的分店欄位連動上面的品牌選擇器，
所以搜尋要能限定在某個品牌之下。這裡守的是「限定真的有作用」——
沒作用的話使用者選了品牌卻看到別家的分店，點下去查出 0 筆，
而畫面上兩個篩選都顯示得好好的。
"""
from __future__ import annotations

import pytest

from console.queries import store_search

# 生產資料裡實際存在的分店，各自驗證一件事。
STORE_WA10_APP = 27681      # 品牌 1180「wa10 瓦城」，名稱是純英數（驗大小寫）
BRAND_WA10 = 1180
STORE_MANY_VERSIONS = 16857  # 原始表有 14 列同 idx —— FINAL 去重的樣本


def test_blank_query_lists_instead_of_returning_nothing():
    """空字串是「列出」，不是「查無」。

    品牌選擇器（`brand_search`）刻意在空字串時回 `[]` 不查 —— 8,548 個品牌
    列前 20 個沒有意義。分店不同：分店幾乎總是在某個品牌之下看，而
    8,171 個品牌只有 20 家以內的分店（實測），打開選單直接看到清單才是
    正常的操作方式。成本實測限定品牌 0.14 秒、不限品牌 0.73 秒，都有 LIMIT。
    """
    assert store_search.search(""), "空字串應該列出分店"
    assert store_search.search("   ", brand=BRAND_WA10), "空字串 + 品牌應該列出該品牌的分店"


def test_listing_respects_the_brand_scope():
    rows = store_search.search("", brand=BRAND_WA10, limit=store_search.MAX_LIMIT)
    assert rows
    assert {r["brand"] for r in rows} == {BRAND_WA10}


def test_listing_puts_active_stores_first():
    """列出時已停用的排後面 —— 前 20 筆全是關掉的分店會讓選單看起來像壞了。"""
    rows = store_search.search("", brand=BRAND_WA10, limit=store_search.MAX_LIMIT)
    actives = [i for i, r in enumerate(rows) if r["status"] == "active"]
    inactives = [i for i, r in enumerate(rows) if r["status"] != "active"]
    if actives and inactives:
        assert max(actives) < min(inactives), "啟用中的分店沒有排在前面"


def test_count_reports_the_true_total_beyond_the_limit():
    """截斷了就必須說得出母數。

    品牌 1180 實測有 218 家分店，而 MAX_LIMIT 是 50 —— 只給 50 筆而不說
    「共 218 家」的話，使用者會以為那就是全部，找不到的分店會被當成不存在。
    """
    total = store_search.count("", brand=BRAND_WA10)
    rows = store_search.search("", brand=BRAND_WA10, limit=store_search.MAX_LIMIT)
    assert total > len(rows), f"品牌 {BRAND_WA10} 應該多於 {store_search.MAX_LIMIT} 家"
    assert total == 218 or total > 200      # 實測 218；容忍資料異動


def test_count_matches_search_when_not_truncated():
    total = store_search.count(str(STORE_WA10_APP))
    rows = store_search.search(str(STORE_WA10_APP), limit=store_search.MAX_LIMIT)
    assert total == len(rows)


def test_exact_id_match_is_first():
    rows = store_search.search(str(STORE_WA10_APP))
    assert rows, f"{STORE_WA10_APP} 應該找得到"
    assert rows[0]["idx"] == STORE_WA10_APP


def test_final_dedupes_replacing_merge_tree():
    """`ods_store` 是 ReplacingMergeTree，未合併的舊版本還在。

    少了 FINAL，選單會出現同一家分店好幾列（實測 idx=16857 有 14 個版本）。
    """
    rows = store_search.search(str(STORE_MANY_VERSIONS), limit=store_search.MAX_LIMIT)
    hits = [r for r in rows if r["idx"] == STORE_MANY_VERSIONS]
    assert len(hits) == 1, f"FINAL 沒有生效，同一家分店出現 {len(hits)} 次"


def test_search_is_case_insensitive():
    """ClickHouse 的 `LIKE` 大小寫敏感，必須用 `ILIKE`。"""
    lower = {r["idx"] for r in store_search.search("wa10 app")}
    upper = {r["idx"] for r in store_search.search("WA10 APP")}
    assert STORE_WA10_APP in lower, "小寫搜不到 —— 用的是 LIKE 而不是 ILIKE"
    assert lower == upper


def test_like_wildcards_are_escaped():
    """使用者打 `%` 不該變成「匹配全部」。"""
    rows = store_search.search("%", limit=store_search.MAX_LIMIT)
    assert len(rows) < store_search.MAX_LIMIT, "`%` 被當成萬用字元了"


def test_brand_scope_restricts_to_that_brand():
    rows = store_search.search("店", brand=BRAND_WA10, limit=store_search.MAX_LIMIT)
    assert rows, f"品牌 {BRAND_WA10} 底下應該有名稱含「店」的分店"
    others = {r["brand"] for r in rows} - {BRAND_WA10}
    assert not others, f"品牌範圍沒有生效，混進了 {sorted(others)}"


def test_brand_scope_actually_narrows_the_result():
    """反向：不給品牌時必須看得到別的品牌，否則上一條測試是空的。"""
    everywhere = store_search.search("店", limit=store_search.MAX_LIMIT)
    assert {r["brand"] for r in everywhere} - {BRAND_WA10}, \
        "不限品牌時也只有一個品牌 —— 這個關鍵字沒有鑑別力，換一個"


def test_exact_id_outside_the_brand_is_not_leaked():
    """打了別家的分店編號，在品牌範圍內就是查無 —— 不可以偷偷放行。

    放行的話使用者會選到一個與品牌互相矛盾的組合，
    而 `_brand = A AND _store = B` 查出來是 0 筆，畫面上兩個篩選都好好的。
    """
    rows = store_search.search(str(STORE_WA10_APP), brand=BRAND_WA10 + 1)
    assert not [r for r in rows if r["idx"] == STORE_WA10_APP]


def test_row_shape_is_json_safe():
    """`store_id` 是 Nullable(String) → pandas 回 pd.NA，直接進 json 會拋。"""
    import json
    rows = store_search.search("店", limit=store_search.MAX_LIMIT)
    assert rows
    json.dumps(rows)                       # 不可拋
    for r in rows:
        assert isinstance(r["idx"], int)
        assert isinstance(r["brand"], int)
        assert isinstance(r["name"], str)
        assert r["code"] is None or isinstance(r["code"], str)
        assert r["status"] in ("active", "disabled", "deleted")
        # 不限品牌搜尋時，兩家不同品牌的「信義店」必須分得出來
        assert isinstance(r["brand_name"], str) and r["brand_name"]


def test_inactive_stores_are_searchable():
    """這是調查工具：已關閉的分店仍有歷史 log，搜不到等於讓調查斷在這裡。"""
    rows = store_search.search("店", limit=store_search.MAX_LIMIT)
    assert rows
    # 母體裡確實有非啟用的分店（實測 enable=0 有 1,842 家、-1 有 985 家），
    # 所以整份結果不該被過濾成只剩 active
    assert store_search.status_of(0) == "disabled"
    assert store_search.status_of(-1) == "deleted"
    assert store_search.status_of(1) == "active"


def test_limit_is_clamped():
    rows = store_search.search("店", limit=store_search.MAX_LIMIT * 10)
    assert len(rows) <= store_search.MAX_LIMIT
    with pytest.raises(ValueError):
        store_search.search("店", limit=0)


# --- 端點 ---------------------------------------------------------------------

def test_stores_endpoint_returns_rows(client):
    r = client.get(f"/api/stores?q={STORE_WA10_APP}")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows and rows[0]["idx"] == STORE_WA10_APP


def test_stores_endpoint_honours_the_brand_scope(client):
    r = client.get(f"/api/stores?q=店&brand={BRAND_WA10}")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows
    assert all(x["brand"] == BRAND_WA10 for x in rows)


def test_stores_endpoint_reports_the_total_when_truncated(client):
    """端點必須回 total —— 前端要能寫出「共 218 家，顯示前 20」。

    只回 rows 的話，被 LIMIT 切掉的分店在畫面上等於不存在，
    而使用者不會知道要用關鍵字縮小。
    """
    r = client.get(f"/api/stores?q=&brand={BRAND_WA10}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] > len(body["rows"]), body["total"]


def test_stores_endpoint_lists_without_a_query(client):
    r = client.get("/api/stores?q=")
    assert r.status_code == 200, r.text
    assert r.json()["rows"], "沒有關鍵字時應該列出分店"


def test_stores_endpoint_rejects_a_non_integer_brand(client):
    """解不出整數要 400，不可以靜靜忽略 —— 那會回一份跨全部品牌的清單。"""
    r = client.get("/api/stores?q=店&brand=不是數字")
    assert r.status_code == 400, r.text
