"""每個資料來源的欄位綱要 —— 「這張表的時間／品牌／去重欄位叫什麼」的唯一真相。

## 為什麼需要這一層

原本 `create_time` 寫死在 60+ 處，隱含假設「每張表的時間欄位都叫 create_time
而且存台北牆鐘」。2026-08-07 接入的五張表把這個假設打破了三次：

  ① 欄位名不同（`created_at` / `created_time` / `recordedAt`）
  ② **有些是 UTC**（voucher / ec 的 `created_at`、console 的 `recordedAt`），
     而另外五張存的是台北牆鐘
  ③ **同一個名字語意相反** —— `ods_request_log.created_at` 是台北牆鐘，
     `ods_voucher_request_log.created_at` 是 UTC

第 ③ 點是這個模組存在的主要理由：照欄位名推導一定會錯，而錯的症狀是**整條
時間軸平移 8 小時、不報錯**。所以 `time_tz` 逐表明寫，沒有任何推導。

## 兩個時間欄位不是重複

`time_col` + `time_tz` 是**過濾**用的（要打在分區鍵上才有裁剪），
`time_expr` 是**分桶與顯示**用的台北牆鐘運算式。兩者刻意分開：

- voucher / ec 兩個都有真欄位（`created_at` UTC 分區鍵、`created_time` 台北），
  而且實測它們是**各自獨立寫入**的（ec 的 3,284 筆裡有 18 筆差 28,799 秒
  而不是 28,800），所以不可以用其中一個推導另一個。
- console 只有 `recordedAt`（UTC），台北運算式是 `recordedAt + INTERVAL 8 HOUR`。
  拿它去過濾的話分區裁剪會失效。

## UTC 表的過濾寫法

`toDateTime(%(start)s, 'Asia/Taipei')` —— 台北牆鐘字串直接轉成 UTC 瞬間再與
UTC 欄位比對。已實測分區裁剪有效（`EXPLAIN indexes=1`：44 parts → 2 parts、
180 granules → 6，見 tests/test_source_schema.py）。這樣就不需要新的參數名，
`%(start)s` / `%(end)s` 的契約不變。
"""
from __future__ import annotations

from dataclasses import dataclass

from console.core.config import settings


@dataclass(frozen=True)
class SourceSchema:
    key: str
    table: str
    # 過濾與分區裁剪用的實體欄位。必須是分區鍵所在的那一欄。
    time_col: str
    # None = `time_col` 本身就是台北牆鐘，直接與 %(start)s 比對。
    # 'Asia/Taipei' = `time_col` 是 UTC，要把台北字串轉成 UTC 瞬間再比。
    time_tz: str | None
    # 分桶與顯示用的台北牆鐘運算式。
    time_expr: str
    # 重複率計算的鍵（`health.source_health()` 的 `uniqExact`）。
    dedup_col: str
    # 同一個 `dedup_col` 有多個版本時，用哪一欄挑最新的。
    #
    # 只有 `ods_request_log` 需要：請求開始時先寫一列（`status_code = 0`、
    # `response_*` 全空），完成後再寫一列，兩列 `created_at` 相同、靠
    # `updated_at` 區分。不處理的話 `GROUP BY status_code` 會生出一格幽靈的 0。
    dedup_order: str | None = None
    # 品牌／分店欄位。**None = 這張表沒有**，`ranking()` 的 `uniq(_brand)` 與
    # `exprs.BRAND_MAP` 都不可以用（會是 Unknown identifier → 502）。
    # 值可以是欄位名，也可以是運算式（ec 的品牌埋在 response JSON 裡）。
    brand_col: str | None = "_brand"
    store_col: str | None = "_store"


def _legacy(key: str) -> SourceSchema:
    """綱要引入之前就存在的五張表：時間欄位就叫 create_time，本身就是台北牆鐘。"""
    return SourceSchema(
        key=key,
        table=settings()["data_sources"][key]["table"],
        time_col="create_time",
        time_tz=None,
        time_expr="create_time",
        dedup_col="_id",
    )


SCHEMAS: dict[str, SourceSchema] = {
    key: _legacy(key) for key in ("api", "backend", "admin", "auth", "order")
}

# ── 2026-08-07 接入的五張表 ─────────────────────────────────────────────────

# ods_batch_request_log：時間欄位就叫 create_time、就是台北牆鐘，與既有五張表
# 同名同語意，所以時間那三個欄位直接沿用。沒有 _brand / _store 真欄位。
SCHEMAS["batch"] = SourceSchema(
    key="batch",
    table=settings()["data_sources"]["batch"]["table"],
    time_col="create_time",
    time_tz=None,
    time_expr="create_time",
    dedup_col="_id",
    brand_col=None,
    store_col=None,
)


def get(source: str) -> SourceSchema:
    """來源代碼 → 綱要。未知來源拋 KeyError（呼叫端不該吞掉）。"""
    return SCHEMAS[source]


def time_filter(source: str) -> str:
    """該來源的標準時間範圍條件（搭配 %(start)s / %(end)s，值一律台北牆鐘字串）。

    舊表回傳的字串與 `exprs.time_filter()` 一字不差，所以既有測試不需要改。
    """
    s = get(source)
    if s.time_tz is None:
        return f"{s.time_col} >= %(start)s AND {s.time_col} < %(end)s"
    tz = s.time_tz
    return (f"{s.time_col} >= toDateTime(%(start)s, '{tz}')"
            f" AND {s.time_col} < toDateTime(%(end)s, '{tz}')")
