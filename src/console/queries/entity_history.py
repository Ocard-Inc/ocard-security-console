"""事件對象**自己的**長期時序，以及由它導出的自身基線帶（面板 A）。

與 `queries/entity.py` 分開的唯一理由是**成本**：這裡一趟查詢實測 4.9 秒
（28 天、120 分鐘分桶、對 api 表的 headers 做 JSONExtract），
而 `entity.py` 的三支合計約 3 秒。前端因此把這一塊做成延後載入，
API 也走獨立端點 —— 綁在事件詳細頁的主查詢裡會讓每次開頁都多等 5 秒。

**端點必須是同步 `def` 而不是 `async def`。** 這裡的查詢是阻塞的，
放進事件迴圈會連五分鐘排程一起卡住（同 `sweep` 的 API 端點踩過的坑）。

## 為什麼自身基線在這裡算，不進 `calibrate`

`baselines` 表是「跨對象」或「全表」的分布，沒有逐對象的列，也不該有 ——
23 萬個來源 × 24 小時 × 2 day_class 是不可能每日重算的。
而自身基線只在有人打開某一個事件時才需要，所以由同一趟查詢的結果現算：

- 分桶與基線粒度**天生成對**（兩者是同一份聚合），不會出現 CLAUDE.md
  警告的「用 10 分鐘基線比 120 分鐘桶 → 憑空生出 12 倍」
- 不必新增 baseline key，也不必重跑 calibrate

## 基線一律取事件開始之前

與 `sweep/run.build_params()` 同一條規則：用含事件的區間算基線，帶會升上來
迎合線本身，最重大的事件反而靜靜消失。事件之前沒有足夠樣本時
`band` 留 None 並說明，**不生假的帶**。
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta

from console.core import timewin
from console.core.ch import query
from console.queries import exprs, trends
from console.queries.entity import EntityRef
from console.rules import baseline

logger = logging.getLogger(__name__)

# 回看天數。28 天與 `settings.baseline.window_days` 一致，也剛好讓 120 分鐘
# 分桶產生 336 個點（每個 (bucket_hour, day_class) 群組約 14 個樣本，夠算 median）。
TIMELINE_DAYS = 28

# 每個 (bucket_hour, day_class) 群組至少要幾個樣本才給該群組自己的 median。
# 不足就退到「事件前全部桶」的分布 —— 回退鏈同 `baseline.get()` 的精神：
# 寧可粗一點，也不要因為中間有破洞而把參考線斷成好幾截。
MIN_GROUP_SAMPLES = 4

# 事件之前至少要有幾個桶才畫帶。少於這個數，帶只是雜訊的形狀。
MIN_BAND_BUCKETS = 8


def timeline(ref: EntityRef, event_start: datetime, end: datetime,
             days: int = TIMELINE_DAYS) -> dict:
    """對象自己的時序 + 自身基線帶 + 趨勢摘要。"""
    bucket = trends.bucket_for(days * 1440)
    start, end, bucket = trends.resolve_window(
        start=end - timedelta(days=days), end=end, bucket_minutes=bucket)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end), **ref.params}

    df = query(
        f"SELECT toStartOfInterval(create_time, INTERVAL {bucket} MINUTE) AS b,"
        f" count() AS c FROM {ref.table}"
        f" WHERE {exprs.time_filter()} AND {ref.where} GROUP BY b ORDER BY b",
        params)
    counts = {timewin.fmt(r["b"].to_pydatetime()): int(r["c"]) for _, r in df.iterrows()}

    # 零填。沒有零填的話空桶會直接消失，而 category 軸依索引等距排列 ——
    # 停掉的那幾天會被壓縮成一段直線而不是凹下去。
    series: list[tuple[datetime, int]] = []
    cursor = start
    while cursor < end:
        series.append((cursor, counts.get(timewin.fmt(cursor), 0)))
        cursor += timedelta(minutes=bucket)

    band = _self_band(series, event_start, bucket)
    rows = [{
        "bucket": timewin.fmt(at),
        "label": at.strftime("%m/%d %H:%M"),
        "count": value,
        "median": band["median_of"](at) if band else None,
        "p95": band["p95_of"](at) if band else None,
        "in_event": at >= event_start,
    } for at, value in series]

    return {
        "start": params["start"], "end": params["end"],
        "bucket_minutes": bucket, "days": days,
        "rows": rows,
        "band": {k: band[k] for k in ("samples", "note")} if band else
                {"samples": 0, "note": _band_reason(series, event_start)},
        "summary": {**_summary(series, event_start, bucket), **_self_normal(rows)},
    }


def _self_normal(rows: list[dict]) -> dict:
    """最後一個分桶相對**自己的**基線帶在哪裡。

    長跑的整合程式會落在自己的帶**裡面** —— 因為「事件之前」幾乎等於
    「這個行為的全部」（實測某對象事件今天才觸發，行為從四月就在）。
    那不是 bug，而是這一頁最該講出來的結論：**對它自己是常態，對全體是離群。**
    畫面必須明說，否則看的人會把「線在帶裡」讀成「所以沒事」，
    而那正是這次改版要消滅的那種誤讀。
    """
    latest = next((r for r in reversed(rows) if r["count"] > 0), None)
    if latest is None or latest["median"] is None:
        return {"self_normal": None, "latest": None,
                "latest_median": None, "latest_p95": None}
    return {
        "self_normal": latest["count"] <= (latest["p95"] or latest["median"]),
        "latest": latest["count"],
        "latest_median": latest["median"],
        "latest_p95": latest["p95"],
    }


def _self_band(series: list[tuple[datetime, int]], event_start: datetime,
               bucket: int) -> dict | None:
    """事件開始之前的桶 → 逐 (桶起點小時, day_class) 的 median / P95。

    回傳兩個查表函式而不是一張表，讓呼叫端不必知道回退鏈長什麼樣。
    """
    prior = [(at, v) for at, v in series if at < event_start]
    if len(prior) < MIN_BAND_BUCKETS:
        return None

    groups: dict[tuple[int, str], list[int]] = {}
    for at, value in prior:
        groups.setdefault((at.hour, baseline.day_class_of(at)), []).append(value)
    all_values = [v for _, v in prior]

    def stat_of(fn, at: datetime) -> float:
        values = groups.get((at.hour, baseline.day_class_of(at)), [])
        return fn(values if len(values) >= MIN_GROUP_SAMPLES else all_values)

    return {
        "samples": len(prior),
        "note": None,
        "median_of": lambda at: round(stat_of(statistics.median, at)),
        "p95_of": lambda at: round(stat_of(_p95, at)),
    }


def _p95(values: list[int]) -> float:
    """P95。`statistics.quantiles` 在 n < 2 時會拋 StatisticsError，
    而「只有一個樣本」在事件剛開始的對象上是真的會發生的。"""
    if not values:
        return 0.0
    if len(values) < 2:
        return float(values[0])
    return float(statistics.quantiles(values, n=20)[-1])


def _band_reason(series: list[tuple[datetime, int]], event_start: datetime) -> str:
    prior = [1 for at, _ in series if at < event_start]
    if not prior:
        return ("事件開始時間落在這個區間的最前面，沒有「事件之前」的資料可當基線。"
                "把區間拉長才會有帶。")
    return (f"事件之前只有 {len(prior)} 個分桶（需要 {MIN_BAND_BUCKETS} 個），"
            "樣本太少，畫出來的帶只是雜訊的形狀，因此不畫。")


def _summary(series: list[tuple[datetime, int]], event_start: datetime,
             bucket: int) -> dict:
    """趨勢摘要：這是新出現的，還是一直都在、只是最近才越線？

    這是整頁最重要的一格 —— 事件頁頭的「開始：<first_seen>」講的是
    「我們什麼時候開始叫」，不是「這件事什麼時候開始」。兩者常常差幾個月。
    """
    values = [v for _, v in series]
    active = [v for v in values if v > 0]
    prior = [v for at, v in series if at < event_start]
    during = [v for at, v in series if at >= event_start]

    # 「查詢區間的第一個桶就有量」＝ 它在這個區間之前就存在。到底多久要另外查
    # （365 天的等值查詢實測 33 秒），所以這裡只說「至少」，不猜。
    starts_before = bool(values) and values[0] > 0

    per_hour = 60 / bucket if bucket else 1
    return {
        "active_buckets": len(active),
        "total_buckets": len(values),
        "starts_before_window": starts_before,
        "prior_median": round(statistics.median(prior)) if prior else None,
        "event_median": round(statistics.median(during)) if during else None,
        "prior_per_hour": round(statistics.median(prior) * per_hour) if prior else None,
        "event_per_hour": round(statistics.median(during) * per_hour) if during else None,
        "change_pct": (round((statistics.median(during) / statistics.median(prior) - 1) * 100)
                       if prior and during and statistics.median(prior) > 0 else None),
        "peak": max(values) if values else 0,
    }
