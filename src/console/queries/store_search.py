"""Log Explorer 分店選擇器的搜尋來源（ClickHouse `ods_store`）。

與 `brand_search.py` 同一套取捨，理由完全相同，不重述：走 ClickHouse 而不是
`core/stores.py` 的 MySQL（MySQL 是選配，搜尋不該因為它掛掉而不能用）、
`FINAL` 去重（`ods_store` 也是 ReplacingMergeTree，實測 113,389 列只有 25,194 個
相異 `idx`，單一分店最多 14 個版本）、`ILIKE` 而非 `LIKE`（大小寫）、
非啟用的分店照樣搜得到（已關閉的分店仍有歷史 log）。

分店多兩件品牌沒有的事：

**品牌範圍**：Explorer 的分店欄位連動上面的品牌選擇器，所以 `search()` 收
`brand`。**限定是硬的，精確編號也不放行** —— 放行的話使用者會選到
「品牌 A + 分店 B（屬於品牌 C）」，而 `_brand = A AND _store = B` 查出來是 0 筆，
畫面上兩個篩選都顯示得好好的。查無時由前端說明「目前只搜尋 <品牌>」。

**每一列都帶品牌名稱**：不限品牌搜尋時「信義店」會有好幾家，只給分店名稱
分不出是誰的。名稱由 `ods_brand FINAL` join 進來而不是走 `core/brands.py` ——
同一個理由（MySQL 選配），而且省掉一趟往返。

`enable` 有三個值（實測 1 = 22,367、0 = 1,842、-1 = 985）。`ods_store_deleted`
是空的，所以 -1 就是這張表表達「已刪除」的方式，對應 `ods_brand` 的 `deleted`
欄位。三個狀態字與品牌共用同一組詞彙，前端才能共用標示。
"""
from __future__ import annotations

import pandas as pd

from console.core.ch import query
from console.queries.brand_search import escape_like

# 維度表，不是四張 log 表之一，所以**不進** `settings()["data_sources"]`
# —— 那是 `config.source_table()` 的白名單，混進維度表會壞掉它的語意。
TABLE = "ods_store"
BRAND_TABLE = "ods_brand"

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# `{brand_clause}` / `{term_clause}` 是程式內常數，不是使用者輸入。
# 值一律走 `%(name)s`。
_SELECT = f"""
SELECT s.idx AS idx, s._brand AS brand, s.name AS name,
       s.store_id AS store_id, s.enable AS enable,
       b.name AS brand_name
FROM {TABLE} AS s FINAL
LEFT JOIN (SELECT idx, name FROM {BRAND_TABLE} FINAL) AS b ON b.idx = s._brand
WHERE {{where}}
ORDER BY {{exact_first}}
         (s.enable = 1) DESC,
         s.idx ASC
LIMIT {{limit}}
"""

_COUNT = f"""
SELECT count() AS n FROM {TABLE} AS s FINAL WHERE {{where}}
"""

# 沒有關鍵字時的 WHERE：只剩品牌範圍（或什麼都沒有）。
_MATCH = """(
        s.name ILIKE %(like)s
     OR s.store_id ILIKE %(like)s
     OR toString(s.idx) = %(exact)s
     OR toString(s.idx) LIKE %(prefix)s
      )"""


def status_of(enable: int) -> str:
    """`active` / `disabled` / `deleted`。

    詞彙與 `brand_search.status_of()` 一致，前端才能共用「已停用」／「已刪除」
    的標示。分店把兩個狀態壓在同一個 `enable` 欄位裡（品牌是 enable + deleted
    兩欄），所以這裡只吃一個參數。
    """
    value = int(enable)
    if value < 0:
        return "deleted"
    return "active" if value == 1 else "disabled"


def _where(q: str, brand: int | None) -> tuple[str, dict, bool]:
    """(WHERE 片段, 參數, 有沒有關鍵字)。

    **空字串是「列出」，不是「查無」。** 品牌選擇器在空字串時刻意不查
    （8,548 個品牌列前 20 個沒有意義），分店相反：分店幾乎總是在某個品牌之下
    看，而實測 8,171 個品牌只有 20 家以內的分店 —— 打開選單直接看到清單才是
    正常的操作方式。成本實測限定品牌 0.14 秒、不限品牌 0.73 秒，兩者都有 LIMIT，
    所以焦點事件觸發它不會變成整表掃描。
    """
    term = q.strip()
    params: dict = {}
    parts: list[str] = []
    if brand is not None:
        parts.append("s._brand = %(brand)s")
        params["brand"] = int(brand)
    if term:
        pattern = escape_like(term)
        parts.append(_MATCH)
        params.update({"like": f"%{pattern}%", "exact": term,
                       "prefix": f"{pattern}%"})
    return (" AND ".join(parts) or "1", params, bool(term))


def search(q: str, brand: int | None = None,
           limit: int = DEFAULT_LIMIT) -> list[dict]:
    """關鍵字 → 候選分店，最多 `limit` 筆；`q` 為空則列出（見 `_where`）。

    名稱、`store_id`、編號都能搜；編號精確命中排最前面，其次啟用中優先
    （列出時前 N 筆全是已停用的分店會讓選單看起來像壞了）。
    `brand` 有值時**硬性**限定在該品牌之下（精確編號也不例外，見模組說明）。

    截斷時的母數由 `count()` 另外給 —— 只回 50 筆而不說「共 218 家」的話，
    被切掉的分店在畫面上等於不存在。
    """
    if limit <= 0:
        raise ValueError("limit 必須大於 0")
    limit = min(limit, MAX_LIMIT)
    where, params, has_term = _where(q, brand)
    # 沒有關鍵字時 `%(exact)s` 不存在，排序片段也要跟著拿掉，否則 ClickHouse
    # 會抱怨未綁定的參數（而不是靜靜忽略）。
    exact_first = "(toString(s.idx) = %(exact)s) DESC," if has_term else ""
    df = query(_SELECT.format(where=where, exact_first=exact_first,
                              limit=int(limit)), params)
    return [_row(r) for _, r in df.iterrows()]


def count(q: str, brand: int | None = None) -> int:
    """符合條件的分店總數（不受 `limit` 影響）。

    只在需要「共 N 家，顯示前 M」時呼叫。呼叫端可以在
    `len(rows) < limit` 時直接用 `len(rows)` 省掉這一趟。
    """
    where, params, _ = _where(q, brand)
    df = query(_COUNT.format(where=where), params)
    return int(df.iloc[0]["n"])


def _row(r: pd.Series) -> dict:
    """ClickHouse → JSON 可序列化的 dict。

    `name` / `store_id` 是 `Nullable(String)`，經 pandas 回來是 `pd.NA`，
    直接進 json 會拋 `TypeError: Object of type NAType is not JSON serializable`；
    `idx` / `_brand` 是 `Int64` → numpy int64，同樣不可序列化。

    `brand_name` 用 LEFT JOIN，品牌不在 `ods_brand` 時是空字串 —— 回
    「（查無品牌）」而不是空字串，空字串在選單裡看起來像渲染壞掉。
    """
    return {
        "idx": int(r["idx"]),
        "brand": int(r["brand"]),
        "brand_name": _text_or_none(r["brand_name"]) or "（查無品牌）",
        "name": _text_or_none(r["name"]) or "（未命名）",
        "code": _text_or_none(r["store_id"]),
        "status": status_of(r["enable"]),
    }


def _text_or_none(value: object) -> str | None:
    if value is None or value is pd.NA or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None
