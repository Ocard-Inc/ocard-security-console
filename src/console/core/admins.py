"""後台帳號對照：ClickHouse 的 `_admin` 編號 → 帳號名（`acc`）。

`ods_api_log` 與 `ods_order_api_log` 都沒有 `acc` 欄位，操作者只有 `_admin`
（整數）。畫面上「操作者 26465」查不下去 —— 而「追究是哪個帳號」正是這個
主控台唯一的任務。

## 為什麼查 ClickHouse 而不是 MySQL

`core/brands.py` 與 `core/stores.py` 都查 MySQL，這裡刻意不一致。理由同
`queries/brand_search.py`（見 docs/superpowers/specs/2026-08-03-explorer-brand-picker-design.md）：
`mysql_config()` 可以回 None（「品牌名稱只是輔助標示，缺它不該讓監測起不來」），
而 ClickHouse 是必要依賴。

差別在於**這個名稱不是輔助標示**。品牌名稱缺了，畫面上還有品牌編號可以追；
`_admin` 是裸整數，缺了帳號名它本身沒有任何調查價值。所以它不該綁在一個
可以是 None 的依賴上。

## `FINAL` 是正確性需求，不是優化

`ods_user_admin` 是 ReplacingMergeTree，實測（2026-08-06）**59,293 列只有
41,300 個相異 `idx`** —— 尚未合併的舊版本還在。不加 `FINAL` 的話同一個
`_admin` 會回多列，批次組 dict 時後到的（可能是舊版本）會蓋掉先到的。
症狀是「帳號名偶爾是舊的」，沒有任何錯誤訊息。
實測 `FINAL` 批次查 10 個 idx 是 0.21 秒。

## 只取 `idx` 與 `acc`

那張表還有 `pwd`、`vtoken`、`email`、`tel`、`ip` —— 沒有一個是這裡需要的，
而它們全部是不該進主控台的東西。

`name` 也刻意不取。那個欄位是分店名（`永安市場店`、`新店寶橋 POS 串接金鑰_order`），
而明細與排名旁邊已經有 `_store` 自己的 `store_label` —— 帶進來會讓同一列
出現兩個店名。

## 這裡的「帳號」語意

實測 Order Log 一天的 2,887 個相異 `_admin` **100% 對得到帳號**，而且對出來的是
POS 與串接金鑰帳號（`cp07_pos`、`kbk_298_pos_order`、`curistacoffee_19`）。
也就是說 Order Log 的「操作者」是**哪一支整合程式／哪一台 POS**，不是哪個人。
這一句寫在 `queries/explorer.SOURCE_LIMITS["order"]` 與
`api/routes._LIMITATIONS_BY_SOURCE["order"]`，畫面上要說出來。

帳號名屬營運資訊、依 `core/masking.py` 的政策**原樣顯示**，不需遮罩。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

from console.core.brands import coerce_id
from console.core.ch import ChConnectionError, ChQueryError, query
from console.core.config import settings

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "（查無帳號）"
UNAVAILABLE_NAME = "（帳號查詢失敗）"

TABLE = "ods_user_admin"

# **`FINAL` 與「只取 idx, acc」都由 tests/test_admins_labels.py 綁著**，
# 不是可以順手簡化的東西。理由見模組說明。
_SQL_TEMPLATE = (
    f"SELECT idx, acc FROM {TABLE} FINAL WHERE idx IN %(ids)s"
)

# 一次查幾個。`idx` 是 sorting key，等值剪枝很有效（實測 10 個 0.21 秒），
# 但參數化的 IN 清單不宜無上限。
_CHUNK = 500

_lock = threading.Lock()
_cache: dict[int, tuple[float, str | None]] = {}


def _cache_config() -> tuple[int, int]:
    """共用 `brands` 的快取參數 —— 帳號名與品牌名的變動頻率相同。"""
    cfg = settings().get("brands") or {}
    return int(cfg.get("cache_ttl_seconds", 21600)), int(cfg.get("max_cached", 20000))


def clear_cache() -> None:
    """測試用。正式路徑靠 TTL 過期，不需要手動清。"""
    with _lock:
        _cache.clear()


def accounts(values: Iterable[object]) -> dict[int, str]:
    """批次取得 `{編號: 帳號名}`。無法解析為編號的值不會出現在結果中。

    查無此編號 → `UNKNOWN_NAME`；查詢失敗 → 整批 `UNAVAILABLE_NAME`
    （不半真半假：一部分真名一部分「查無」會讓人以為那幾個帳號被刪了）。
    """
    ids = [i for i in (coerce_id(v) for v in values) if i is not None]
    if not ids:
        return {}
    found, ok = _resolve(ids)
    if not ok:
        return {i: UNAVAILABLE_NAME for i in ids}
    return {i: (found.get(i) or UNKNOWN_NAME) for i in ids}


def account(value: object) -> str:
    admin_id = coerce_id(value)
    if admin_id is None:
        return "（空）"
    return accounts([admin_id])[admin_id]


def _resolve(ids: list[int]) -> tuple[dict[int, str | None], bool]:
    wanted = list(dict.fromkeys(ids))
    now = time.time()
    found: dict[int, str | None] = {}
    misses: list[int] = []
    with _lock:
        for i in wanted:
            hit = _cache.get(i)
            if hit is not None and hit[0] > now:
                found[i] = hit[1]
            else:
                misses.append(i)
    if not misses:
        return found, True

    fetched = _fetch(misses)
    if fetched is None:
        # 查詢失敗：已快取的部分仍可用，但整批視為不可用以免半真半假
        return found, False

    ttl, max_cached = _cache_config()
    expires = time.time() + ttl
    with _lock:
        if len(_cache) + len(misses) > max_cached:
            _cache.clear()
        for i in misses:
            _cache[i] = (expires, fetched.get(i))
    found.update({i: fetched.get(i) for i in misses})
    return found, True


def _fetch(ids: list[int]) -> dict[int, str] | None:
    """向 ClickHouse 批次查帳號。回 None 代表查詢失敗（與「查無此編號」語意不同）。

    任何查詢錯誤只記 log、不往上拋：帳號名是呈現層的補充，
    讓它把整份明細或排名變成 500 是不成比例的。
    """
    out: dict[int, str] = {}
    try:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            df = query(_SQL_TEMPLATE, {"ids": chunk})
            for _, row in df.iterrows():
                acc = row["acc"]
                # `acc` 是 Nullable(String)，pandas 給 pd.NA
                if acc is None or str(acc).strip() in ("", "None", "<NA>"):
                    continue
                out[int(row["idx"])] = str(acc).strip()
    except (ChQueryError, ChConnectionError) as exc:
        logger.warning("帳號名稱查詢失敗：%s", exc)
        return None
    return out
