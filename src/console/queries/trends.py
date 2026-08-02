"""總覽趨勢與風險排名（皆附 28 天同時段基線，設計稿 7.3 / 7.5）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from console.core import brands, timewin
from console.core.ch import query
from console.core import masking
from console.queries import exprs
from console.rules import baseline


# (視窗上限分鐘, 分桶分鐘)。兩個硬性條件：
#   1. 分桶必須整除 1440，否則與 ClickHouse 的 toStartOfInterval 格線錯位
#      （見 timewin.align_bucket）—— 錯一格會讓整張圖靜靜變成 0。
#   2. 每個分桶都必須有同粒度的基線（見 checker/calibrate.py 的 GRANULARITIES），
#      否則倍數會變假：用 10 分鐘的基線去比 120 分鐘的桶，會冒出假的「12 倍」。
BUCKET_LADDER = ((60, 5), (360, 10), (1440, 30), (10080, 120))


def bucket_for(minutes: int) -> int:
    """依查詢視窗挑分桶，讓點數維持在 12–84 之間。"""
    for limit, bucket in BUCKET_LADDER:
        if minutes <= limit:
            return bucket
    return BUCKET_LADDER[-1][1]


def resolve_window(
    minutes: int = 60,
    start: datetime | None = None,
    end: datetime | None = None,
    bucket_minutes: int | None = None,
) -> tuple[datetime, datetime, int]:
    """把「最近 N 分鐘」或「絕對區間」統一換算成對齊過的 (start, end, bucket)。

    **start 與 end 都必須對齊分桶格線**，這是本專案踩過的坑：
    只對齊 end 的話，任意長度的區間（自訂區間、或以前的「今天」）算出來的 start
    會落在格線之間（例如 00:25 而不是 00:20），zero-fill 迴圈產生的每一個 cursor
    就全部偏移，與 ClickHouse 回傳的桶起點永遠對不上 —— 症狀是整張圖靜靜變成
    一條 0，不會報錯。實測 minutes=125 時 13 個桶全為 0。
    對齊後視窗會略寬於要求的長度，那是正確的：一律取完整的桶。
    """
    if start is not None and end is not None:
        span = max(int((end - start).total_seconds() // 60), 1)
        bucket = bucket_minutes or bucket_for(span)
        # 右界要**向上**取整到完整的桶。自訂區間的結束是 23:59:59（整天），
        # 向下取整的話 120 分鐘分桶會退到 22:00，當天最後兩小時整段消失。
        aligned_end = timewin.align_bucket(end, bucket)
        if aligned_end < end:
            aligned_end += timedelta(minutes=bucket)
        # 但不可超過資料實際落地的時間，否則尾端會是一段假的 0
        aligned_end = min(aligned_end,
                          timewin.align_bucket(timewin.effective_now(), bucket))
        aligned_start = timewin.align_bucket(start, bucket)
        if aligned_start >= aligned_end:
            aligned_end = aligned_start + timedelta(minutes=bucket)
        return aligned_start, aligned_end, bucket

    bucket = bucket_minutes or bucket_for(minutes)
    # 必須用 align_bucket 而不是 align_tick：後者只對齊「分鐘」欄位，
    # 分桶 > 60 分鐘就會與 ClickHouse 錯位（120 分桶會對到 13:00 而非 12:00）。
    aligned_end = timewin.align_bucket(timewin.effective_now(), bucket)
    aligned_start = timewin.align_bucket(aligned_end - timedelta(minutes=minutes), bucket)
    return aligned_start, aligned_end, bucket


def request_trend(
    minutes: int = 60,
    bucket_minutes: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """四條線：API request / Backend request / 登入成功 / 登入失敗。

    給 start/end 就用絕對區間，否則是「最近 minutes 分鐘」。
    bucket_minutes 不給就依 BUCKET_LADDER 自動選 —— 固定 10 分鐘的話，
    「最近 1 小時」只有 6 個點、「最近 7 天」則是 1008 個點。
    """
    start, end, bucket_minutes = resolve_window(minutes, start, end, bucket_minutes)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    interval = f"toStartOfInterval(create_time, INTERVAL {bucket_minutes} MINUTE)"
    tf = exprs.time_filter()

    series: dict[str, dict[str, int]] = {}
    for name, sql in [
        ("api", f"SELECT {interval} AS b, count() AS c FROM ods_api_log WHERE {tf} GROUP BY b"),
        ("backend", f"SELECT {interval} AS b, count() AS c FROM ods_backend_sys_log WHERE {tf} GROUP BY b"),
        ("login_success", f"SELECT {interval} AS b, count() AS c FROM ods_admin_log"
                          f" WHERE {tf} AND {exprs.ANY_LOGIN_SUCCESS} GROUP BY b"),
        ("login_failed", f"SELECT {interval} AS b, count() AS c FROM ods_admin_log"
                         f" WHERE {tf} AND {exprs.ANY_LOGIN_FAILED} GROUP BY b"),
    ]:
        df = query(sql, params)
        series[name] = {timewin.fmt(r["b"].to_pydatetime()): int(r["c"])
                        for _, r in df.iterrows()}

    # 四條線各自對應的基線 metric key。四個都已由 calibrate.py 算好存在 SQLite，
    # 以前只讀了 api 與 login_success，另外兩個白算的 —— 首頁的小倍數圖要四個都有，
    # 每個面板才能對照自己的同時段基線（而不只是把一張圖切成四張）。
    # 基線的粒度必須跟分桶一致（見 BUCKET_LADDER 的說明）。
    baseline_keys = {
        "api": f"table_{bucket_minutes}m:api",
        "backend": f"table_{bucket_minutes}m:backend",
        "login_success": f"login_success_{bucket_minutes}m",
        "login_failed": f"login_failed_{bucket_minutes}m",
    }
    # baseline.get() 每次都是一趟 SQLite。7 天視窗 × 4 條線 = 4,032 次呼叫，
    # 但相異鍵最多 4 × 24 × 2 = 192 個，memoize 起來。
    base_cache: dict[tuple[str, int, str], object] = {}

    def base_of(name: str, at: datetime):
        dc = baseline.day_class_of(at)
        key = (name, at.hour, dc)
        if key not in base_cache:
            base_cache[key] = baseline.get(baseline_keys[name], hour=at.hour, day_class=dc)
        return base_cache[key]

    # 跨日的視窗只寫 %H:%M 是看不懂的：7 天 × 120 分桶會產生 84 個標籤，
    # 但只有 12 種相異值（同一組時刻重複 7 次），完全分不出是哪一天。
    label_fmt = "%H:%M" if (end - start) <= timedelta(days=1) else "%m/%d %H:%M"

    buckets: list[dict] = []
    cursor = start
    while cursor < end:
        label = timewin.fmt(cursor)
        row = {"bucket": label, "label": cursor.strftime(label_fmt)}
        for name in baseline_keys:
            value = series[name].get(label, 0)
            base = base_of(name, cursor)
            row[name] = value
            row[f"{name}_median"] = round(base.median) if base else None
            row[f"{name}_p95"] = round(base.p95) if base else None
            row[f"{name}_multiple"] = (round(value / base.median, 2)
                                       if base and base.median else None)
        # 舊欄位名，前端表格檢視與 lib.js 仍在用，保留以免破壞既有呼叫端
        row["login_median"] = row["login_success_median"]
        row["login_p95"] = row["login_success_p95"]
        buckets.append(row)
        cursor += timedelta(minutes=bucket_minutes)

    return {"start": params["start"], "end": params["end"],
            "bucket_minutes": bucket_minutes, "buckets": buckets}


def _with_baseline(rows: list[dict], metric_key_fn, at: datetime) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        base = baseline.get(metric_key_fn(r), hour=at.hour,
                            day_class=baseline.day_class_of(at))
        median = base.median if base else None
        out.append({
            "rank": i, **r,
            "median": round(median) if median else None,
            "p95": round(base.p95) if base else None,
            "multiple": round(r["current"] / median, 1) if median else None,
        })
    return out


def risk_rankings(
    minutes: int = 60,
    limit: int = 5,
    end: datetime | None = None,
) -> dict:
    """四個排名：高流量 endpoint / 品牌 / 來源 / 高失敗 actor。

    end 用於自訂區間（右界不是「現在」的情況）；排名不分桶，所以不需要對齊格線。
    """
    end = timewin.align_tick(min(end, timewin.effective_now()) if end
                             else timewin.effective_now())
    start = end - timedelta(minutes=minutes)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    tf = exprs.time_filter()

    df = query(
        f"SELECT {exprs.ENDPOINT} AS name, count() AS current, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map"
        f" FROM ods_api_log WHERE {tf} GROUP BY name ORDER BY current DESC LIMIT {limit}",
        params)
    endpoints = _with_baseline(
        [{"name": r["name"], "current": int(r["current"]), "brands": int(r["brands"]),
          "brand_top": brands.breakdown(r["brand_map"])}
         for _, r in df.iterrows()],
        lambda r: f"api_endpoint_60m:{r['name']}", end)

    df = query(
        f"SELECT _brand AS name, count() AS current FROM ods_api_log WHERE {tf}"
        f" GROUP BY name ORDER BY current DESC LIMIT {limit}", params)
    brand_labels = brands.labels(df["name"]) if len(df) else {}
    brand_rows = _with_baseline(
        [{"name": brand_labels.get(int(r["name"]), str(int(r["name"]))),
          "current": int(r["current"])}
         for _, r in df.iterrows()],
        lambda r: "brand_api_15m", end)

    df = query(
        f"SELECT src, count() AS current, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, _brand, create_time FROM ods_api_log WHERE {tf})"
        f" WHERE src != '' GROUP BY src ORDER BY current DESC LIMIT {limit}", params)
    sources = _with_baseline(
        [{"name": masking.src(r["src"]), "current": int(r["current"]),
          "brands": int(r["brands"]), "brand_top": brands.breakdown(r["brand_map"])}
         for _, r in df.iterrows()],
        lambda r: "api_src_60m", end)

    df = query(
        f"SELECT ip, count() AS current, uniq(acc) AS accs FROM ods_admin_log"
        f" WHERE {tf} AND {exprs.ANY_LOGIN_FAILED} AND ip != ''"
        f" GROUP BY ip ORDER BY current DESC LIMIT {limit}", params)
    actors = [{"rank": i, "name": masking.src(r["ip"]), "current": int(r["current"]),
               "accs": int(r["accs"]), "median": None, "p95": None, "multiple": None}
              for i, (_, r) in enumerate(df.iterrows(), 1)]

    return {
        "start": params["start"], "end": params["end"],
        # 排名視窗可能被 routes.RANKING_MAX_MINUTES 夾小（趨勢拉 7 天但排名只到 24 小時），
        # 前端要據此標示，否則會把 24 小時的排名說成 7 天的。
        "window_minutes": minutes,
        "endpoints": endpoints, "brands": brand_rows,
        "sources": sources, "failed_actors": actors,
    }
