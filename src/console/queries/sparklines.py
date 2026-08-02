"""統計卡上的迷你趨勢線資料：四張 log 表的每小時筆數。

設計取捨：
- **獨立端點，不塞進 `health.source_health()`**：那個函式被 `/api/health`（前端每 30 秒
  輪詢）、`/api/overview`（同樣 30 秒）與 `/api/explorer` 三處呼叫，其中只有一處要
  sparkline。塞進去等於讓另外兩個呼叫端永遠白付這筆成本。
- **一個 UNION ALL 而不是四個查詢**：實測 24 小時、每小時分桶，UNION ALL 0.19 秒，
  拆成四個獨立查詢 0.44 秒。
- **TTL 快取**：前端 30 秒輪詢一次，但每小時桶的邊界一小時才移動一次，
  120 秒的過時對「趨勢形狀」完全不可見。仿 `core/brands.py` 的模組層快取 +
  `threading.Lock`；不能用 `lru_cache`，那個不會過期。
- **零填**：回傳固定 `hours` 個點，前端可以直接依索引取用，不必處理缺口。

嚴重度（P0–P3）的 sparkline **不在這裡，也做不出來**：`store/db.py` 的 `events` 表
沒有 created_at／resolved_at，`store/events.py` 每個 tick 直接 UPDATE 覆蓋，
歷史在寫入當下就被銷毀，無法重建「N 小時前的 24 小時滾動計數」。
硬要做需要新增 append-only 的 tick 歷史表，而且無法回填。
"""
from __future__ import annotations

import threading
from datetime import timedelta

from console.core import timewin
from console.core.ch import query
from console.core.config import settings

_lock = threading.Lock()
# (到期時間戳, payload)
_cache: tuple[float, dict] | None = None


def _config() -> tuple[int, int]:
    cfg = settings().get("sparklines") or {}
    return int(cfg.get("hours", 24)), int(cfg.get("cache_ttl_seconds", 120))


def _fetch(hours: int) -> dict:
    # 右界對齊整點：最後一個桶是「目前這個小時」，還在累積中。
    # 邊界一律在 Python 端算好、以含秒的完整字串傳參（SQL 裡絕不用 now()）。
    now = timewin.effective_now()
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(hours=hours)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}

    sources = settings()["data_sources"]
    # 表名與來源 key 都來自 settings() 白名單，不是使用者輸入
    union = " UNION ALL ".join(
        f"SELECT '{key}' AS src, toStartOfHour(create_time) AS b, count() AS c"
        f" FROM {src['table']}"
        f" WHERE create_time >= %(start)s AND create_time < %(end)s"
        f" GROUP BY b"
        for key, src in sources.items()
    )
    df = query(union, params)

    counts: dict[str, dict[str, int]] = {k: {} for k in sources}
    for _, r in df.iterrows():
        counts.setdefault(str(r["src"]), {})[timewin.fmt(r["b"].to_pydatetime())] = int(r["c"])

    out: dict[str, dict] = {}
    for key, src in sources.items():
        points = []
        cursor = start
        while cursor < end:
            stamp = timewin.fmt(cursor)
            points.append({"bucket": stamp, "count": counts.get(key, {}).get(stamp, 0)})
            cursor += timedelta(hours=1)
        values = [p["count"] for p in points]
        out[key] = {
            "label": src["label"],
            "points": points,
            "max": max(values) if values else 0,
            "latest": values[-1] if values else 0,
        }

    return {
        "hours": hours,
        "bucket_minutes": 60,
        "start": params["start"],
        "end": params["end"],
        "generated_at": timewin.fmt(now),
        "sources": out,
        # 誠實回報做不到的部分，前端才不會自己編一條線出來
        "severity": None,
        "severity_note": (
            "events 表以 UPDATE 就地覆寫、沒有逐 tick 歷史，無法誠實重建嚴重度時間序列。"
        ),
    }


def source_sparklines() -> dict:
    """四張表最近 N 小時的每小時筆數（TTL 快取）。"""
    global _cache
    hours, ttl = _config()
    now = timewin.taipei_now().timestamp()

    with _lock:
        if _cache and _cache[0] > now and _cache[1].get("hours") == hours:
            return {**_cache[1], "cached": True}

    payload = _fetch(hours)
    with _lock:
        _cache = (now + ttl, payload)
    return {**payload, "cached": False}
