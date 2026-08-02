"""分店名稱對照：ClickHouse 的 `_store` 編號 → 「分店名稱（編號）」。

與 `core/brands.py` 同一套取捨（批次 + 行程內 TTL 快取、查不到不假裝、任何 MySQL
錯誤只記 log 不往上拋），只是查的是 MySQL `ocard.store`（`idx` = 編號、`name` = 名稱）。
共用 `brands` 的 `_cache_config()` 參數 —— 分店名與品牌名的變動頻率相同，
沒有理由各給一組設定。

`_store` 的兩個特殊值必須分開處理，混在一起會誤導：

    -1   **品牌層級操作**，不屬於任何分店（實測 andrew_c 的 120 萬次全部是 -1）。
         這不是「查不到分店」，而是「這個動作本來就沒有分店」。
     0   未填。

分店名稱屬營運資訊而非個資，不需遮罩（同品牌名稱）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

import pymysql

from console.core.brands import coerce_id
from console.core.config import mysql_config, settings

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "（查無分店）"
UNAVAILABLE_NAME = "（分店名稱查詢失敗）"
BRAND_LEVEL_NAME = "（品牌層級，非特定分店）"

# 品牌層級操作。ClickHouse 的 `_store` 用 -1 表示，0 視為未填。
BRAND_LEVEL = -1

_lock = threading.Lock()
_cache: dict[int, tuple[float, str | None]] = {}


def _cache_config() -> tuple[int, int]:
    cfg = settings().get("brands") or {}
    return int(cfg.get("cache_ttl_seconds", 21600)), int(cfg.get("max_cached", 20000))


def format_label(store_id: int, name: str | None) -> str:
    if store_id <= 0:
        return BRAND_LEVEL_NAME
    return f"{name or UNKNOWN_NAME}（{store_id}）"


def labels(values: Iterable[object]) -> dict[int, str]:
    """批次取得 {編號: 「名稱（編號）」}。無法解析為編號的值不會出現在結果中。"""
    ids = [i for i in (coerce_id(v) for v in values) if i is not None]
    real = [i for i in ids if i > 0]
    out = {i: BRAND_LEVEL_NAME for i in ids if i <= 0}
    if not real:
        return out
    found, ok = _resolve(real)
    if not ok:
        out.update({i: f"{UNAVAILABLE_NAME}（{i}）" for i in real})
        return out
    out.update({i: format_label(i, found.get(i)) for i in real})
    return out


def label(value: object) -> str:
    store_id = coerce_id(value)
    if store_id is None:
        return "（空）"
    return labels([store_id])[store_id]


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
    """向 MySQL 批次查名稱。回 None 代表查詢失敗（與「查無此編號」語意不同）。"""
    cfg = mysql_config()
    if cfg is None:
        return None
    out: dict[int, str] = {}
    try:
        conn = pymysql.connect(
            host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
            database=cfg.database, connect_timeout=5, read_timeout=10,
            cursorclass=pymysql.cursors.DictCursor)
    except Exception as exc:  # noqa: BLE001 - 名稱只是輔助標示，不可讓它拖垮監測
        logger.warning("分店名稱查詢失敗（連線）：%s", exc)
        return None
    try:
        with conn.cursor() as cur:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT idx, name FROM store WHERE idx IN ({placeholders})",
                    tuple(chunk))
                for row in cur.fetchall():
                    if row.get("name"):
                        out[int(row["idx"])] = str(row["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("分店名稱查詢失敗（查詢）：%s", exc)
        return None
    finally:
        conn.close()
    return out
