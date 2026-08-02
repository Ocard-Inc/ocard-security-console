"""總覽趨勢與風險排名（皆附 28 天同時段基線，設計稿 7.3 / 7.5）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from console.core import brands, timewin
from console.core.ch import query
from console.core.masking import src_fp
from console.queries import exprs
from console.rules import baseline


def request_trend(minutes: int = 60, bucket_minutes: int = 10) -> dict:
    """四條線：API request / Backend request / 登入成功 / 登入失敗。"""
    end = timewin.align_tick(timewin.effective_now(), bucket_minutes)
    start = end - timedelta(minutes=minutes)
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
    baseline_keys = {
        "api": "table_10m:api",
        "backend": "table_10m:backend",
        "login_success": "login_success_10m",
        "login_failed": "login_failed_10m",
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

    buckets: list[dict] = []
    cursor = start
    while cursor < end:
        label = timewin.fmt(cursor)
        row = {"bucket": label, "label": cursor.strftime("%H:%M")}
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


def risk_rankings(minutes: int = 60, limit: int = 5) -> dict:
    """四個排名：高流量 endpoint / 品牌 / 來源 / 高失敗 actor。"""
    end = timewin.align_tick(timewin.effective_now())
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
        [{"name": src_fp(r["src"]), "current": int(r["current"]),
          "brands": int(r["brands"]), "brand_top": brands.breakdown(r["brand_map"])}
         for _, r in df.iterrows()],
        lambda r: "api_src_60m", end)

    df = query(
        f"SELECT ip, count() AS current, uniq(acc) AS accs FROM ods_admin_log"
        f" WHERE {tf} AND {exprs.ANY_LOGIN_FAILED} AND ip != ''"
        f" GROUP BY ip ORDER BY current DESC LIMIT {limit}", params)
    actors = [{"rank": i, "name": src_fp(r["ip"]), "current": int(r["current"]),
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
