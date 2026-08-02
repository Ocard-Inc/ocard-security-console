"""總覽趨勢與風險排名（皆附 28 天同時段基線，設計稿 7.3 / 7.5）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from console.core import timewin
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

    buckets: list[dict] = []
    cursor = start
    while cursor < end:
        label = timewin.fmt(cursor)
        base_api = baseline.get("table_10m:api", hour=cursor.hour,
                                day_class=baseline.day_class_of(cursor))
        base_login = baseline.get("login_success_10m", hour=cursor.hour,
                                  day_class=baseline.day_class_of(cursor))
        api_v = series["api"].get(label, 0)
        buckets.append({
            "bucket": label,
            "label": cursor.strftime("%H:%M"),
            "api": api_v,
            "backend": series["backend"].get(label, 0),
            "login_success": series["login_success"].get(label, 0),
            "login_failed": series["login_failed"].get(label, 0),
            "api_median": round(base_api.median) if base_api else None,
            "api_p95": round(base_api.p95) if base_api else None,
            "api_multiple": (round(api_v / base_api.median, 2)
                             if base_api and base_api.median else None),
            "login_median": round(base_login.median) if base_login else None,
            "login_p95": round(base_login.p95) if base_login else None,
        })
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
        f"SELECT {exprs.ENDPOINT} AS name, count() AS current, uniq(_brand) AS brands"
        f" FROM ods_api_log WHERE {tf} GROUP BY name ORDER BY current DESC LIMIT {limit}",
        params)
    endpoints = _with_baseline(
        [{"name": r["name"], "current": int(r["current"]), "brands": int(r["brands"])}
         for _, r in df.iterrows()],
        lambda r: f"api_endpoint_60m:{r['name']}", end)

    df = query(
        f"SELECT _brand AS name, count() AS current FROM ods_api_log WHERE {tf}"
        f" GROUP BY name ORDER BY current DESC LIMIT {limit}", params)
    brands = _with_baseline(
        [{"name": str(int(r["name"])), "current": int(r["current"])}
         for _, r in df.iterrows()],
        lambda r: "brand_api_15m", end)

    df = query(
        f"SELECT src, count() AS current, uniq(_brand) AS brands FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, _brand, create_time FROM ods_api_log WHERE {tf})"
        f" WHERE src != '' GROUP BY src ORDER BY current DESC LIMIT {limit}", params)
    sources = _with_baseline(
        [{"name": src_fp(r["src"]), "current": int(r["current"]),
          "brands": int(r["brands"])} for _, r in df.iterrows()],
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
        "endpoints": endpoints, "brands": brands,
        "sources": sources, "failed_actors": actors,
    }
