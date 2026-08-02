"""品牌名稱對照：ClickHouse 的 `_brand` 編號 → 「品牌名稱（編號）」。

四張 log 表只存數字品牌編號，稽查現場看到「7340」無法判斷影響對象；名稱來自
MySQL `ocard.brand`（`idx` = 編號、`name` = 名稱）。

設計取捨：
- **批次 + 行程內快取**：監測每五分鐘跑一次、排名頁一次就要幾十個編號，逐筆查
  MySQL 不合理。名稱極少變動，故以 TTL 快取（預設 6 小時）並批次 IN 查詢。
- **查不到不假裝**：MySQL 不可用時降級為「（品牌名稱查詢失敗）（編號）」，
  編號查無對應時為「（查無品牌）（編號）」。兩者語意不同，不可混為一談 ——
  這與本系統「沒有資料不等於沒有異常」的一貫原則相同。
- **絕不讓品牌名稱拖垮監測**：任何 MySQL 錯誤都只記 log，不往上拋。

品牌名稱屬營運資訊而非個資，不需遮罩（對照 masking.py 處理的 IP／帳號／token）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

import pymysql

from console.core.config import MysqlConfig, mysql_config, settings

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "（查無品牌）"
UNAVAILABLE_NAME = "（品牌名稱查詢失敗）"

# 「涉及品牌 N 個」展開時列出的品牌數上限
BREAKDOWN_LIMIT = 10

# 單次 IN 查詢的編號上限；超過就分批（避免超長 SQL 與封包上限）
_CHUNK = 500

_local = threading.local()
_lock = threading.Lock()
# 編號 → (到期時間戳, 名稱)；名稱為 None 代表 MySQL 明確查無此編號
_cache: dict[int, tuple[float, str | None]] = {}
_warned_missing_config = False


def _cache_config() -> tuple[int, int]:
    cfg = settings().get("brands") or {}
    return int(cfg.get("cache_ttl_seconds", 21600)), int(cfg.get("max_cached", 20000))


def coerce_id(value: object) -> int | None:
    """把 `_brand` 正規化為 int。

    ClickHouse 經 pandas 回來可能是 numpy int、float（Nullable 欄位）或字串，
    也可能是引擎已轉成的 label 文字 —— 無法解析時回 None（呼叫端原樣顯示）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def format_label(brand_id: int, name: str | None) -> str:
    """「品牌名稱（品牌編號）」；名稱為空字串時視同查無。"""
    return f"{name or UNKNOWN_NAME}（{brand_id}）"


def labels(values: Iterable[object]) -> dict[int, str]:
    """批次取得 {編號: 「名稱（編號）」}。無法解析為編號的值不會出現在結果中。"""
    ids = [i for i in (coerce_id(v) for v in values) if i is not None]
    if not ids:
        return {}
    found, ok = _resolve(ids)
    if not ok:
        return {i: f"{UNAVAILABLE_NAME}（{i}）" for i in dict.fromkeys(ids)}
    return {i: format_label(i, found.get(i)) for i in dict.fromkeys(ids)}


def label(value: object) -> str:
    """單一品牌的顯示字串；無法解析為編號時原樣回傳（例如已是「（空）」）。"""
    brand_id = coerce_id(value)
    if brand_id is None:
        return "（空）" if value is None or str(value).strip() == "" else str(value)
    return labels([brand_id])[brand_id]


def breakdown(brand_map: object, limit: int = BREAKDOWN_LIMIT) -> list[dict]:
    """`exprs.BRAND_MAP`（sumMap）的結果 → 次數由高到低的前 N 個品牌。

    回傳 `[{"brand": 1180, "label": "wa10 瓦城（1180）", "count": 2062}, ...]`。
    名稱一次批次查完，因此展開 10 個品牌只需一次 MySQL 查詢（且多半命中快取）。

    輸入可能是 ClickHouse 回來的 (編號陣列, 次數陣列)，也可能是已存進
    context_json 後再讀出的同一組資料；空值一律回空 list。
    """
    pairs = _to_pairs(brand_map)
    if not pairs:
        return []
    pairs.sort(key=lambda p: (-p[1], p[0]))
    top = pairs[:max(0, limit)]
    lut = labels(b for b, _ in top)
    return [{"brand": b, "label": lut.get(b, str(b)), "count": c} for b, c in top]


def top_summary(top: list[dict] | None, limit: int = 3) -> str:
    """breakdown() 的結果 → 一句話。給無法展開的地方用（Slack、解讀與證據文字）。"""
    if not top:
        return ""
    return "、".join(f"{b['label']} {b['count']:,} 次" for b in top[:limit])


def _to_pairs(brand_map: object) -> list[tuple[int, int]]:
    """(keys, values) 兩個平行陣列 → [(編號, 次數)]；形狀不符就當作沒有資料。"""
    if brand_map is None or isinstance(brand_map, (str, bytes)):
        return []
    try:
        keys, values = brand_map            # tuple/list 長度必須是 2
    except (TypeError, ValueError):
        return []
    pairs = []
    for raw_id, raw_count in zip(keys, values):
        brand_id = coerce_id(raw_id)
        if brand_id is None:
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        pairs.append((brand_id, count))
    return pairs


def name(value: object) -> str | None:
    """只要名稱本身；查無或查詢失敗都回 None。"""
    brand_id = coerce_id(value)
    if brand_id is None:
        return None
    found, ok = _resolve([brand_id])
    return found.get(brand_id) if ok else None


# ─────────────────────────── 快取與查詢 ───────────────────────────

def _resolve(ids: list[int]) -> tuple[dict[int, str | None], bool]:
    """回傳 ({編號: 名稱或 None}, 是否成功取得)。失敗時不寫快取。"""
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
    """向 MySQL 批次查名稱。回傳 None 代表查詢失敗（與「查無」不同）。"""
    cfg = mysql_config()
    if cfg is None:
        _warn_missing_config()
        return None
    out: dict[int, str] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        rows = _query_chunk(cfg, chunk)
        if rows is None:
            return None
        out.update(rows)
    return out


def _query_chunk(cfg: MysqlConfig, chunk: list[int]) -> dict[int, str] | None:
    # chunk 全部是 int（coerce_id 保證），placeholder 仍走參數化
    sql = ("SELECT idx, name FROM brand WHERE idx IN ("
           + ",".join(["%s"] * len(chunk)) + ")")
    try:
        return _run(cfg, sql, chunk)
    except (pymysql.Error, OSError):
        logger.warning("品牌名稱查詢失敗，重建連線後重試一次", exc_info=True)
        _reset_conn()
        try:
            return _run(cfg, sql, chunk)
        except (pymysql.Error, OSError):
            logger.exception("品牌名稱查詢失敗（%d 個編號），降級為只顯示編號", len(chunk))
            _reset_conn()
            return None


def _run(cfg: MysqlConfig, sql: str, params: list[int]) -> dict[int, str]:
    conn = _get_conn(cfg)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {int(r["idx"]): (r["name"] or "").strip() for r in cur.fetchall()}


def _get_conn(cfg: MysqlConfig):
    """thread-local 連線（同 ch.py：避免每次新建洩漏 socket，也避免跨執行緒共用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None and not _alive(conn):
        _reset_conn()
        conn = None
    if conn is None:
        conn = pymysql.connect(
            host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
            database=cfg.database, charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5, read_timeout=15, write_timeout=15,
            autocommit=True,
        )
        _local.conn = conn
    return conn


def _alive(conn) -> bool:
    """MySQL 會主動斷閒置連線；用 ping 確認後再決定要不要重建。"""
    try:
        conn.ping(reconnect=False)
        return True
    except (pymysql.Error, OSError):
        return False


def _reset_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 關閉失敗不影響重建
            pass
        _local.conn = None


def _warn_missing_config() -> None:
    global _warned_missing_config
    if not _warned_missing_config:
        _warned_missing_config = True
        logger.warning("未設定 MYSQL_HOST，品牌只顯示編號（請確認 .env）")


def clear_cache() -> None:
    """測試與維運用：丟棄名稱快取，下次查詢重新向 MySQL 取。"""
    with _lock:
        _cache.clear()
