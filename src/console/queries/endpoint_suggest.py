"""Log Explorer 的 endpoint 建議選單：該區間內呼叫量由高到低的候選值。

**為什麼一次回傳全部，而不是像品牌選擇器那樣逐次搜尋**：endpoint 的基數有界且小
（實測 30 天 api 86 種、backend 591 種、admin 31 種，整包約 25 KB），一次抓完之後
前端過濾是零延遲**且完整**的 —— 罕見的 endpoint 也找得到，不會因為只取 top N 而漏掉。
品牌那邊是 8,548 筆，不適用，所以兩者結論相反，這是刻意的。

**為什麼一定要快取**：api 掃描量巨大（30 天 9,000 萬列、實測 1,087 ms），而選擇器是
「聚焦即顯示」—— 點一下就得有結果。品牌選擇器 70 ms 不需要快取，這裡需要。
鍵包含絕對的 start／end，同一區間重複聚焦即命中；實際使用是「選好區間 → 點欄位 →
挑 → 查詢」，區間不常變。不能用 `lru_cache`：它不會過期，也淘汰不掉。

**候選值必須能當篩選值用**：見 `explorer.SUGGEST_EXPR` 的不變量說明。
auth 沒有可篩的 endpoint 維度（`action` 半年來只有一個值），直接拒絕。
"""
from __future__ import annotations

import threading
import time

from console.core.ch import query
from console.core.config import settings
from console.queries import explorer

CACHE_TTL_SECONDS = 120
# 區間由使用者自選，鍵的空間無上限 —— 沒有淘汰會無限長大
MAX_CACHED = 64

_lock = threading.Lock()
# (source, start, end) → (到期時間戳, payload)
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def expire_all() -> None:
    """把所有項目標記為已過期（測試用；也可在設定變更後手動呼叫）。"""
    with _lock:
        for k, (_, payload) in list(_cache.items()):
            _cache[k] = (0.0, payload)


def cache_size() -> int:
    with _lock:
        return len(_cache)


def suggest(source: str, start: str, end: str) -> dict:
    """該區間內的 endpoint 候選值，依次數由高到低。

    回傳 `{"rows": [{"value": ..., "count": ...}], "total": N}`。
    來源不支援 endpoint 篩選（auth）或時間參數不合法時拋 `explorer.FilterError`。
    """
    expr = _validate(source, start, end)
    key = (source, start, end)
    now = time.time()

    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    payload = _fetch(source, expr, start, end)

    with _lock:
        _cache[key] = (time.time() + CACHE_TTL_SECONDS, payload)
        _evict()
    return payload


def _validate(source: str, start: str, end: str) -> str:
    """驗證來源與時間，回傳該來源的 GROUP BY 運算式。

    時間規則（格式、順序、62 天上限）完全沿用 explorer.validate()，
    不另立一套 —— 兩邊查的是同一批表，限制不該不一樣。
    """
    expr = explorer.SUGGEST_EXPR.get(source)
    if expr is None:
        sources = settings()["data_sources"]
        if source in sources:
            raise explorer.FilterError(
                f"{sources[source]['label']} 不支援 endpoint 篩選（該表沒有對應欄位）")
        raise explorer.FilterError(f"未知資料來源 {source!r}")
    explorer.validate(explorer.ExplorerFilter(source=source, start=start, end=end))
    return expr


def _fetch(source: str, expr: str, start: str, end: str) -> dict:
    f = explorer.ExplorerFilter(source=source, start=start, end=end)
    where, params = explorer.where_clause(f)
    df = query(
        f"SELECT {expr} AS value, count() AS cnt {where}"
        f" GROUP BY value HAVING value != '' ORDER BY cnt DESC", params)
    rows = [{"value": str(r["value"]), "count": int(r["cnt"])}
            for _, r in df.iterrows()]
    return {"rows": rows, "total": len(rows)}


def _evict() -> None:
    """超過上限就丟掉最早到期的（呼叫端已持有 _lock）。"""
    while len(_cache) > MAX_CACHED:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
