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

# 對象趨勢可選的區間 → 分桶。**刻意不共用 `trends.BUCKET_LADDER`。**
#
# 那個階梯的每一格都必須出現在 `calibrate.GRANULARITIES` 裡
# （`tests/test_trend_buckets.py` 擋著），因為它的比較基準是 calibrate 算好的
# `baselines` —— 用 10 分鐘的基線比 120 分鐘的桶會憑空生出假的 12 倍。
# 這裡的比較基準是**同一趟查詢現算的前一個等長區間**，沒有那個耦合
# （同本模組的自身基線帶）。
#
# 取的分桶值仍是既有的 5/10/30/120，**不引入新粒度**，所以就算日後有人把兩者
# 接起來也不會多出一個 calibrate 沒算的粒度。
#
# **每個區間都必須是分桶的整數倍**：前期是「往回位移一個區間長度」，而
# `toStartOfInterval` 的格線固定在 1970-01-01 —— 位移量不是分桶的倍數時
# 前期那條線會整條錯位，而畫面完全正常。`tests/test_entity_recent_trend.py`
# 用行為驗證這件事。
TREND_RANGES = {60: 5, 180: 10, 720: 30, 1440: 30, 4320: 120, 10080: 120}


def _bucket_end(at: datetime, bucket: int) -> datetime:
    """含 `at` 的那一個桶的**右界**（開區間）。"""
    return timewin.align_bucket(at, bucket) + timedelta(minutes=bucket)


def slow_ranges(ref: EntityRef) -> list[int]:
    """這個對象的哪些區間慢到必須在選單上標出來。

    趨勢一趟查兩個等長區間，所以 7d 實際掃 14 天。成本幾乎完全由「對象條件能不能
    剪枝」決定，而**只有 `api` 的來源 IP 不能** —— 它要對 `headers` 做
    JSONExtract。實測（2026-08-01 錨點，本機）：

        api / endpoint      1h 0.5s   24h 0.3s   3d —      7d  1.4s
        api / source_ip     1h 0.3s   24h 2.2s   3d 3.2s   7d 11.8s   ← 只有這個
        admin / actor                                      7d  3.9s
        admin / source_ip                                  7d  0.5s
        backend / source_ip                                7d  0.1s
        auth / actor                                       7d  0.9s

    分級的形狀與 `explorer.extent_lookback_days()` 刻意相同（同一個事實的另一面）：
    `admin` 的 actor 也帶 JSONExtract，但 3.9 秒仍然快，所以**不標** ——
    「含 JSONExtract 就算慢」會讓一個仍然快的組合被貼上警語，
    而警語貼滿了就沒有人讀。

    **回的是「要標註」而不是「不給選」。** 使用者選了 7d 就該拿到 7d 的圖；
    偷偷降級成 3d 是最糟的結果（畫面寫 7 天而圖是 3 天）。
    """
    if ref.source == "api" and any(d.field == "source_ip" for d in ref.dims):
        return [10080]
    return []


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


def recent_trend(ref: EntityRef, anchor: datetime, minutes: int) -> dict:
    """選中對象的請求趨勢：本期 + 前一個等長區間，逐桶零填。

    ## 錨點是事件的 `last_seen`，不是 `now()`

    「過去 24 小時」是往**事件那個時刻**回推。用 `now()` 的話同一個事件在隔天
    會變成一張與它無關的圖，而且不會有任何錯誤 —— 呼叫端因此一律傳
    `timewin.parse(row["last_seen"])`，而回應把實際用的右界放在 `anchor` 裡
    讓畫面寫得出來。

    ## 「前一個同時期」= 緊接在前的等長區間

    24h → 前一個 24h；3d → 前一個 3d。六個區間統一規則、沒有特例。
    前期的每一點帶自己的真實時刻（`prev_bucket` / `prev_label`），
    否則虛線上的點沒有時間可讀。

    ## 一趟查詢覆蓋兩個區間

    查 `[前期起, 本期止]` 再在 Python 切開：兩個區間的分桶天生一致
    （同一份聚合），round trip 也減半。

    成本與逐區間的實測數字見 `slow_ranges()`。最貴的組合是 api 的來源 IP
    在 7d（掃 14 天、要對 `headers` 做 JSONExtract）：**實測 11.8 秒**，
    所以那一個選項在畫面上帶「較慢」標註 —— 不偷偷降級成別的區間。

    ## 右界被夾住時是**往前滑**，不是截短

    錨點比已落地的資料還新時（事件的 `last_seen` 落在 `lag_buffer_minutes`
    之內就會發生），最後幾個桶會是一段「還沒發生」的假 0 —— 而那與
    「這段時間沒有活動」在畫面上一模一樣。夾住右界並保持區間長度，
    「最近 24 小時」這個標籤才仍然是真的；夾了就在 `window_note` 說出來。

    `minutes` 不在 `TREND_RANGES` 裡會拋 `KeyError`（端點層轉成 400）——
    靜靜挑一個分桶的話畫面會寫「最近 5 小時」而圖是別的長度。
    """
    bucket = TREND_RANGES[minutes]
    span = timedelta(minutes=minutes)

    end = _bucket_end(anchor, bucket)
    landed = _bucket_end(timewin.effective_now(), bucket)
    window_note = None
    if end > landed:
        window_note = (
            f"這個對象的最後出現時間（{timewin.fmt(anchor)}）比已落地的資料還新，"
            f"右界已夾到 {timewin.fmt(landed)} 並整段往前滑 —— "
            f"填到未來的桶會是一段「還沒發生」的假 0，"
            f"而那與「這段時間沒有活動」在圖上一模一樣。")
        end = landed

    start = end - span
    prev_start = start - span

    params = {"start": timewin.fmt(prev_start), "end": timewin.fmt(end), **ref.params}
    df = query(
        f"SELECT toStartOfInterval(create_time, INTERVAL {bucket} MINUTE) AS b,"
        f" count() AS c FROM {ref.table}"
        f" WHERE {exprs.time_filter()} AND {ref.where} GROUP BY b ORDER BY b",
        params)
    counts = {timewin.fmt(r["b"].to_pydatetime()): int(r["c"]) for _, r in df.iterrows()}

    rows = []
    for i in range(minutes // bucket):
        at = start + timedelta(minutes=i * bucket)
        prev_at = prev_start + timedelta(minutes=i * bucket)
        rows.append({
            "bucket": timewin.fmt(at),
            "label": at.strftime("%m/%d %H:%M"),
            "count": counts.get(timewin.fmt(at), 0),
            "prev_bucket": timewin.fmt(prev_at),
            "prev_label": prev_at.strftime("%m/%d %H:%M"),
            "prev_count": counts.get(timewin.fmt(prev_at), 0),
        })

    return {
        "anchor": timewin.fmt(end),
        "minutes": minutes,
        "bucket_minutes": bucket,
        "start": timewin.fmt(start),
        "prev_start": timewin.fmt(prev_start),
        "prev_end": timewin.fmt(start),
        "total": sum(r["count"] for r in rows),
        "prev_total": sum(r["prev_count"] for r in rows),
        "rows": rows,
        "window_note": window_note,
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
