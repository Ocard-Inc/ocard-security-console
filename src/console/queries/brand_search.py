"""Log Explorer 品牌選擇器的搜尋來源（ClickHouse `ods_brand`）。

**為什麼走 ClickHouse 而不是 `core/brands.py` 的 MySQL**：MySQL 在本專案是選配
（`mysql_config()` 可以回 None —— 品牌名稱只是輔助標示，缺它不該讓監測起不來），
ClickHouse 是必要依賴。搜尋走 CH 就不會出現「MySQL 掛了所以選不了品牌」。
兩者職責也不同：`brands.py` 是「編號 → 顯示標籤」且在監測熱路徑上（engine 每五分鐘
呼叫），這裡是「關鍵字 → 候選清單」，只服務 UI。不共用快取、不互相依賴。

**`FINAL` 是正確性需求，不是優化**：`ods_brand` 是 ReplacingMergeTree，實測 9,349 列
只有 8,548 個相異 `idx` —— 未合併的舊版本還在。`idx=1180` 同時存在「瓦城泰統集團」
（2025-10-20）與「wa10 瓦城」（2026-07-31）兩列，不加 `FINAL` 選單會出現同一品牌
兩個不同名字。加了之後結果與 `brands.py` 走 MySQL 的完全一致。

**`ILIKE` 不是 `LIKE`**：ClickHouse 的 `LIKE` 大小寫敏感，MySQL 的預設不敏感。
照抄 ROS 的 SQL（`components/brand-picker.tsx` → `lib/ocard/ocard-db.ts`）會讓
`coffee` 搜不到 `Coffee`。

**停用與已刪除的品牌照樣要搜得到**：這是調查工具，上個月被停用的品牌仍有歷史 log，
搜不到等於讓調查斷在這裡（8,548 個品牌只有 5,419 啟用中）。改以 `status` 標示，
由前端顯示「已停用」／「已刪除」。
"""
from __future__ import annotations

import pandas as pd

from console.core.ch import query

# 維度表，不是四張 log 表之一，所以**不進** `settings()["data_sources"]`
# —— 那是 `config.source_table()` 的白名單，混進維度表會壞掉它的語意。
# identifier 來自程式內常數同樣滿足硬性約束。database 由 ch_config() 決定
# （CLICKHOUSE_DB，預設 ocard），故此處不加限定詞，與四張 log 表的用法一致。
TABLE = "ods_brand"

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

_SQL = f"""
SELECT idx, id, name, country, enable, deleted
FROM {TABLE} FINAL
WHERE name ILIKE %(like)s
   OR id ILIKE %(like)s
   OR toString(idx) = %(exact)s
   OR toString(idx) LIKE %(prefix)s
ORDER BY (toString(idx) = %(exact)s) DESC,
         (enable = 1 AND deleted = 0) DESC,
         rank ASC, idx DESC
LIMIT {{limit}}
"""


def escape_like(text: str) -> str:
    """跳脫 LIKE 的萬用字元。

    使用者打 `%` 不該變成「匹配全部」。反斜線要先跳脫，否則會吃掉後面補上的跳脫字元。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def status_of(enable: int, deleted: int) -> str:
    """`active` / `disabled` / `deleted`。

    `deleted` 的判定優先於 `disabled`：一筆同時 `deleted=1, enable=1` 的資料要顯示
    「已刪除」—— 那是更強的訊號。
    """
    if int(deleted) == 1:
        return "deleted"
    return "active" if int(enable) == 1 else "disabled"


def search(q: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """關鍵字 → 候選品牌，最多 `limit` 筆。

    名稱、公開代碼（`id`）、編號都能搜；編號精確命中排最前面，其次才是啟用中優先。
    打了完整編號就是要那一個，不管它停用與否。

    `q` 去空白後為空時回 `[]` **且不查 ClickHouse** —— 焦點事件會觸發它，
    一次整表掃描沒有意義。
    """
    if limit <= 0:
        raise ValueError("limit 必須大於 0")
    limit = min(limit, MAX_LIMIT)
    term = q.strip()
    if not term:
        return []
    pattern = escape_like(term)
    df = query(_SQL.format(limit=int(limit)), {
        "like": f"%{pattern}%",
        "exact": term,
        "prefix": f"{pattern}%",
    })
    return [_row(r) for _, r in df.iterrows()]


def _row(r: pd.Series) -> dict:
    """ClickHouse → JSON 可序列化的 dict。

    `id` 是 `Nullable(String)`，經 pandas 回來是 `pd.NA`，直接進 json 會拋
    `TypeError: Object of type NAType is not JSON serializable`；`idx` 是 `Int64`
    → numpy int64，同樣不可序列化。兩者都要在這裡正規化。
    """
    return {
        "idx": int(r["idx"]),
        "name": str(r["name"]),
        "code": _text_or_none(r["id"]),
        "country": _text_or_none(r["country"]),
        "status": status_of(r["enable"], r["deleted"]),
    }


def _text_or_none(value: object) -> str | None:
    if value is None or value is pd.NA or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None
