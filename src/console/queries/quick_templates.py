"""快速查詢模板（設計稿 11 節，16 個模板 / 4 分類）。

每個模板宣告：名稱、用途、所需輸入、資料來源、預估耗時、執行函式。
執行結果統一為 {columns, rows, interpretation}，供稽查現場 1–3 分鐘內取得答案。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from console.core import brands, timewin
from console.core.ch import query
from console.core.masking import src_fp, actor_fp
from console.queries import exprs, health
from console.rules import baseline


@dataclass(frozen=True)
class Template:
    id: str
    category: str
    name: str
    desc: str
    inputs: tuple[str, ...]
    source: str
    eta: str
    run: Callable


def _win(params: dict, default_minutes: int = 60) -> tuple[str, str, object]:
    if params.get("start") and params.get("end"):
        s, e = timewin.parse(params["start"]), timewin.parse(params["end"])
    else:
        e = timewin.align_tick(timewin.effective_now())
        s = e - timedelta(minutes=default_minutes)
    return timewin.fmt(s), timewin.fmt(e), e


def _result(columns, rows, interpretation, **extra):
    return {"columns": columns, "rows": rows, "interpretation": interpretation, **extra}


def _top_endpoints(p: dict) -> dict:
    s, e, end_dt = _win(p)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS cnt, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map"
        f" FROM ods_api_log WHERE {exprs.time_filter()} GROUP BY endpoint"
        f" ORDER BY cnt DESC LIMIT 15", {"start": s, "end": e})
    rows = []
    for _, r in df.iterrows():
        b = baseline.get(f"api_endpoint_60m:{r['endpoint']}", hour=end_dt.hour,
                         day_class=baseline.day_class_of(end_dt))
        rows.append({
            "endpoint": r["endpoint"], "count": int(r["cnt"]), "brands": int(r["brands"]),
            "brand_top": brands.breakdown(r["brand_map"]),
            "median": round(b.median) if b else None,
            "p95": round(b.p95) if b else None,
            "multiple": round(int(r["cnt"]) / b.median, 1) if b and b.median else None,
        })
    over = [r for r in rows if r["multiple"] and r["multiple"] >= 2]
    note = (f"{len(over)} 個 endpoint 超過同時段 median 兩倍：" +
            "、".join(f"{r['endpoint']}（{r['multiple']}×）" for r in over[:3])
            ) if over else "所有 endpoint 流量都在 28 天同時段基線範圍內。"
    return _result(["endpoint", "count", "brands", "median", "p95", "multiple"],
                   rows, note, time_range=f"{s} ~ {e}")


def _top_brands(p: dict) -> dict:
    s, e, _ = _win(p)
    df = query(
        f"SELECT _brand, count() AS cnt, uniq(concat(controller,'/',function)) AS endpoints"
        f" FROM ods_api_log WHERE {exprs.time_filter()} GROUP BY _brand"
        f" ORDER BY cnt DESC LIMIT 15", {"start": s, "end": e})
    brand_labels = brands.labels(df["_brand"]) if len(df) else {}
    rows = [{"brand": brand_labels.get(int(r["_brand"]), str(int(r["_brand"]))),
             "count": int(r["cnt"]), "endpoints": int(r["endpoints"])}
            for _, r in df.iterrows()]
    total = sum(r["count"] for r in rows)
    top_share = round(rows[0]["count"] / total, 3) if rows and total else 0
    note = (f"前 15 品牌合計 {total:,} 筆，最大單一品牌 {rows[0]['brand']} 佔 {top_share:.1%}。"
            "品牌名稱與編號皆為營運資訊，不屬個資。" if rows
            else "此時間範圍沒有 API 請求。")
    return _result(["brand", "count", "endpoints"], rows, note, time_range=f"{s} ~ {e}")


def _top_sources(p: dict) -> dict:
    s, e, end_dt = _win(p)
    df = query(
        f"SELECT src, count() AS cnt, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map,"
        f" uniq(concat(controller,'/',function)) AS endpoints FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, _brand, controller, function, create_time"
        f"  FROM ods_api_log WHERE {exprs.time_filter()})"
        f" WHERE src != '' GROUP BY src ORDER BY cnt DESC LIMIT 15",
        {"start": s, "end": e})
    b = baseline.get("api_src_60m")
    rows = [{"source_fp": src_fp(r["src"]), "count": int(r["cnt"]),
             "brands": int(r["brands"]), "brand_top": brands.breakdown(r["brand_map"]),
             "endpoints": int(r["endpoints"]),
             "p99": round(b.p99) if b else None} for _, r in df.iterrows()]
    cross = [r for r in rows if r["brands"] > 5]
    return _result(["source_fp", "count", "brands", "endpoints", "p99"], rows,
                   (f"{len(cross)} 個來源跨 5 個以上品牌，需確認是否為平台級整合。"
                    if cross else "所有高流量來源都集中在少數品牌，符合單品牌整合特徵。")
                   + " 來源 IP 由 forwarded header 推導，屬未驗證來源。",
                   time_range=f"{s} ~ {e}")


def _endpoint_baseline(p: dict) -> dict:
    endpoint = p.get("endpoint")
    if not endpoint:
        raise ValueError("需要 endpoint 參數")
    rows = []
    for hour in range(24):
        for dc in ("weekday", "weekend"):
            b = baseline.get(f"api_endpoint_60m:{endpoint}", hour=hour, day_class=dc)
            if b and b.samples:
                rows.append({"hour": f"{hour:02d}:00", "day_class": dc,
                             "median": round(b.median), "p95": round(b.p95),
                             "p99": round(b.p99), "max": round(b.maxv),
                             "samples": b.samples})
    if not rows:
        return _result([], [], f"{endpoint} 不在 28 天基線範圍內（非 top 300 endpoint）。")
    peak = max(rows, key=lambda r: r["median"])
    return _result(["hour", "day_class", "median", "p95", "p99", "max", "samples"], rows,
                   f"{endpoint} 的 28 天基線：尖峰在 {peak['hour']}（{peak['day_class']}，"
                   f"median {peak['median']:,}、P95 {peak['p95']:,}）。")


def _source_cross_brand(p: dict) -> dict:
    fp = p.get("source_fp")
    if not fp:
        raise ValueError("需要 source_fp 參數")
    s, e, _ = _win(p, 1440)
    df = query(
        f"SELECT src, _brand, count() AS cnt FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, _brand, create_time FROM ods_api_log"
        f"  WHERE {exprs.time_filter()}) WHERE src != '' GROUP BY src, _brand",
        {"start": s, "end": e})
    matched = [(int(r["_brand"]), int(r["cnt"])) for _, r in df.iterrows()
               if src_fp(r["src"]) == fp]
    if not matched:
        return _result([], [], f"{fp} 在此時間範圍內沒有 API 請求。", time_range=f"{s} ~ {e}")
    matched.sort(key=lambda x: -x[1])
    top = matched[:20]
    brand_labels = brands.labels(b for b, _ in top)
    rows = [{"brand": brand_labels.get(b, str(b)), "count": c} for b, c in top]
    total = sum(c for _, c in matched)
    return _result(["brand", "count"], rows,
                   f"{fp} 涉及 {len(matched)} 個品牌、共 {total:,} 筆請求。"
                   + ("跨品牌來源需確認是否為平台級整合或憑證外洩。"
                      if len(matched) > 5 else "集中於少數品牌，符合單一整合來源特徵。"),
                   time_range=f"{s} ~ {e}")


def _source_first_seen(p: dict) -> dict:
    fp = p.get("source_fp")
    if not fp:
        raise ValueError("需要 source_fp 參數")
    from console.store import db
    row = db.one("SELECT kind, first_seen, origin FROM known_sources"
                 " WHERE entity_key = ? OR entity_key LIKE ?", (fp, f"%|{fp}"))
    if row is None:
        return _result([], [], f"{fp} 不在 known_sources（90 天內未見），屬新來源。")
    label = "系統播種時已存在（90 天內出現過）" if row["origin"] == "seed" else "監測期間首次記錄"
    return _result(["kind", "first_seen", "origin"], [dict(row)],
                   f"{fp} 於 {row['first_seen']} 首次記錄（{label}）。")


def _top_login_failed(p: dict) -> dict:
    s, e, _ = _win(p)
    df = query(
        f"SELECT ip, count() AS cnt, uniq(acc) AS accs FROM ods_admin_log"
        f" WHERE {exprs.time_filter()} AND {exprs.ANY_LOGIN_FAILED} AND ip != ''"
        f" GROUP BY ip ORDER BY cnt DESC LIMIT 15", {"start": s, "end": e})
    rows = [{"source_fp": src_fp(r["ip"]), "failures": int(r["cnt"]),
             "accounts": int(r["accs"])} for _, r in df.iterrows()]
    noip = query(
        f"SELECT count() AS n FROM ods_admin_log WHERE {exprs.time_filter()}"
        f" AND {exprs.ANY_LOGIN_FAILED} AND ip = ''", {"start": s, "end": e}
    ).iloc[0]["n"]
    worst = rows[0]["failures"] if rows else 0
    return _result(["source_fp", "failures", "accounts"], rows,
                   f"單一來源最多失敗 {worst} 次"
                   + ("，未達暴力破解門檻（20/15 分）。" if worst < 20 else "，已達暴力破解門檻。")
                   + f" 另有 {int(noip):,} 筆失敗紀錄沒有 IP（來源 IP 不可用）。",
                   time_range=f"{s} ~ {e}")


def _login_success_anomaly(p: dict) -> dict:
    s, e, _ = _win(p, 90)
    df = query(
        f"SELECT toStartOfTenMinutes(create_time) AS b, count() AS cnt,"
        f" uniq(ip) AS ips, uniq(_brand) AS brands, {exprs.BRAND_MAP} AS brand_map"
        f" FROM ods_admin_log"
        f" WHERE {exprs.time_filter()} AND {exprs.BOSS_LOGIN_SUCCESS}"
        f" GROUP BY b ORDER BY b", {"start": s, "end": e})
    rows = []
    peak = None
    for _, r in df.iterrows():
        bt = r["b"].to_pydatetime()
        base = baseline.get("boss_login_success_10m", hour=bt.hour,
                            day_class=baseline.day_class_of(bt))
        mult = round(int(r["cnt"]) / base.median, 1) if base and base.median else None
        row = {"bucket": bt.strftime("%m/%d %H:%M"), "login_success": int(r["cnt"]),
               "sources": int(r["ips"]), "brands": int(r["brands"]),
               "brand_top": brands.breakdown(r["brand_map"]),
               "median": round(base.median) if base else None,
               "p95": round(base.p95) if base else None, "multiple": mult}
        rows.append(row)
        if mult and (peak is None or mult > peak["multiple"]):
            peak = row
    note = ("此時間範圍內登入成功量都在同時段基線範圍內。" if not peak or peak["multiple"] < 2
            else f"{peak['bucket']} 登入成功 {peak['login_success']} 次，為同時段 median "
                 f"（{peak['median']}）的 {peak['multiple']} 倍，涉及 {peak['sources']} 個來源、"
                 f"{peak['brands']} 個品牌。")
    return _result(["bucket", "login_success", "sources", "brands", "median", "p95", "multiple"],
                   rows, note, time_range=f"{s} ~ {e}")


def _post_login_api(p: dict) -> dict:
    s, e, _ = _win(p, 60)
    df = query(
        f"SELECT toStartOfTenMinutes(create_time) AS b, count() AS logins FROM ods_admin_log"
        f" WHERE {exprs.time_filter()} AND {exprs.ANY_LOGIN_SUCCESS} GROUP BY b ORDER BY b",
        {"start": s, "end": e})
    adf = query(
        f"SELECT toStartOfTenMinutes(create_time) AS b, count() AS api_calls,"
        f" countIf(has_error = 1) AS errors FROM ods_api_log"
        f" WHERE {exprs.time_filter()} GROUP BY b ORDER BY b", {"start": s, "end": e})
    api_map = {r["b"]: (int(r["api_calls"]), int(r["errors"])) for _, r in adf.iterrows()}
    rows = []
    for _, r in df.iterrows():
        calls, errs = api_map.get(r["b"], (0, 0))
        rows.append({"bucket": r["b"].to_pydatetime().strftime("%m/%d %H:%M"),
                     "logins": int(r["logins"]), "api_calls": calls, "errors": errs})
    return _result(["bucket", "logins", "api_calls", "errors"], rows,
                   "比對登入尖峰後 10 分鐘的 API 行為：若登入暴增但 API 量與錯誤率持平，"
                   "較可能是批次重新認證而非入侵後的資料存取。", time_range=f"{s} ~ {e}")


def _cell_lookup(p: dict) -> dict:
    s, e, end_dt = _win(p)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS cnt, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map,"
        f" uniq(_admin) AS actors FROM ods_api_log WHERE {exprs.time_filter()}"
        f" AND function IN {exprs.in_list(list(exprs.CELL_LOOKUP_FUNCTIONS))}"
        f" GROUP BY endpoint ORDER BY cnt DESC", {"start": s, "end": e})
    rows = []
    for _, r in df.iterrows():
        b = baseline.get(f"api_endpoint_60m:{r['endpoint']}", hour=end_dt.hour,
                         day_class=baseline.day_class_of(end_dt))
        rows.append({"endpoint": r["endpoint"], "count": int(r["cnt"]),
                     "brands": int(r["brands"]),
                     "brand_top": brands.breakdown(r["brand_map"]),
                     "actors": int(r["actors"]),
                     "median": round(b.median) if b else None,
                     "multiple": round(int(r["cnt"]) / b.median, 1) if b and b.median else None})
    return _result(["endpoint", "count", "brands", "actors", "median", "multiple"], rows,
                   "手機條件查詢屬個資敏感操作。UI 不顯示查詢的手機號碼本身，"
                   "只呈現「手機條件查詢」的次數與來源分布。", time_range=f"{s} ~ {e}")


def _orderlist_traversal(p: dict) -> dict:
    s, e, end_dt = _win(p)
    df = query(
        f"SELECT {exprs.ROUTE2} AS route2, acc, count() AS cnt, uniq(_brand) AS brands,"
        f" {exprs.BRAND_MAP} AS brand_map,"
        f" uniqExact(route) AS uniq_paths FROM ods_backend_sys_log"
        # 用 startsWith 而非 LIKE：SQL 走 Python % 參數格式化，裸露的 % 會被誤判為佔位符
        f" WHERE {exprs.time_filter()} AND startsWith({exprs.ROUTE2}, 'orderlist')"
        f" GROUP BY route2, acc ORDER BY cnt DESC LIMIT 15", {"start": s, "end": e})
    rows = []
    for _, r in df.iterrows():
        b = baseline.get(f"backend_route_60m:{r['route2']}", hour=end_dt.hour,
                         day_class=baseline.day_class_of(end_dt))
        cnt = int(r["cnt"])
        rows.append({"route": r["route2"], "actor_fp": actor_fp(r["acc"]), "count": cnt,
                     "brands": int(r["brands"]),
                     "brand_top": brands.breakdown(r["brand_map"]),
                     "unique_paths": int(r["uniq_paths"]),
                     "unique_ratio": round(int(r["uniq_paths"]) / cnt, 3) if cnt else None,
                     "median": round(b.median) if b else None,
                     "multiple": round(cnt / b.median, 1) if b and b.median else None})
    # orderlist/detail 的訂單 ID 放在 POST body 而非 URL，因此該 route 的
    # unique 路徑比例恆為 0，不能作為遍歷指標；改以「量 vs 該 route 同時段基線」判定。
    trav = [r for r in rows
            if r["count"] > 1000 and ((r["multiple"] or 0) > 10 or (r["unique_ratio"] or 0) > 0.8)]
    if trav:
        top = trav[0]
        note = (f"{len(trav)} 個操作者呈現大量查閱特徵。最高者 {top['actor_fp']} 於 "
                f"{top['route']} 查閱 {top['count']:,} 次"
                + (f"，為同時段 median（{top['median']}）的 {top['multiple']:,.0f} 倍" if top["multiple"] else "")
                + f"，涉及 {top['brands']} 個品牌。")
    else:
        note = "未觀察到大量查閱特徵；此範圍內的 orderlist 存取量都在同時段基線範圍內。"
    return _result(["route", "actor_fp", "count", "brands", "unique_paths",
                    "unique_ratio", "median", "multiple"], rows,
                   note + " 注意：orderlist/detail 的訂單識別在 POST body 而非 URL，"
                          "因此 unique 路徑比例對該 route 恆為 0，不可作為遍歷判定依據。"
                          "7/16–17 事件即為此模式（單一帳號 75 萬次 orderlist/detail）。",
                   time_range=f"{s} ~ {e}")


def _api_error(p: dict) -> dict:
    s, e, _ = _win(p)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS total, countIf(has_error = 1) AS errors"
        f" FROM ods_api_log WHERE {exprs.time_filter()} GROUP BY endpoint"
        f" HAVING errors > 0 ORDER BY errors DESC LIMIT 15", {"start": s, "end": e})
    rows = [{"endpoint": r["endpoint"], "total": int(r["total"]), "errors": int(r["errors"]),
             "error_rate": round(int(r["errors"]) / int(r["total"]), 4)}
            for _, r in df.iterrows()]
    b = baseline.get("api_error_5m")
    total_err = sum(r["errors"] for r in rows)
    return _result(["endpoint", "total", "errors", "error_rate"], rows,
                   f"此範圍共 {total_err} 筆錯誤。5 分鐘窗 error 數的 28 天 P99 為 "
                   f"{round(b.p99) if b else '（無基線）'}；常態 error 比例約 0.02%。",
                   time_range=f"{s} ~ {e}")


def _freshness(p: dict) -> dict:
    cards = health.source_health()
    rows = [{"source": c["label"], "table": c["table"], "status": c["status"],
             "latest": c["latest"], "lag_minutes": c["lag_minutes"],
             "today_rows": c.get("today_rows")} for c in cards]
    worst = max((c for c in cards if c.get("lag_minutes") is not None),
                key=lambda c: c["lag_minutes"], default=None)
    return _result(["source", "table", "status", "latest", "lag_minutes", "today_rows"], rows,
                   f"最大延遲為 {worst['label']}（{worst['lag_minutes']:.1f} 分鐘）。"
                   if worst else "無法取得任何來源的新鮮度。")


def _missing_rate(p: dict) -> dict:
    cards = health.source_health()
    rows = [{"source": c["label"], "missing_field": c.get("missing_label"),
             "missing_rate": c.get("missing_rate"), "dup_rate": c.get("dup_rate")}
            for c in cards]
    return _result(["source", "missing_field", "missing_rate", "dup_rate"], rows,
                   "欄位缺漏公開呈現，不隱藏：缺漏的欄位不可作為判斷依據"
                   "（例如 Admin Log 無 IP 的登入紀錄無法做單 IP 暴力破解判定）。")


def _dup_rate(p: dict) -> dict:
    s, e, _ = _win(p, 1440)
    rows = []
    for key in ("backend", "admin"):
        from console.core.config import settings
        table = settings()["data_sources"][key]["table"]
        df = query(f"SELECT count() AS total, uniqExact(_id) AS uniq FROM {table}"
                   f" WHERE {exprs.time_filter()}", {"start": s, "end": e})
        r = df.iloc[0]
        total, uniq = int(r["total"]), int(r["uniq"])
        rows.append({"source": settings()["data_sources"][key]["label"], "total": total,
                     "unique": uniq, "duplicates": total - uniq,
                     "dup_rate": round(1 - uniq / total, 4) if total else 0})
    return _result(["source", "total", "unique", "duplicates", "dup_rate"], rows,
                   "Backend System Log 歷史資料可能重複；系統以事件 ID（_id）去重後顯示。",
                   time_range=f"{s} ~ {e}")


def _compare_dates(p: dict) -> dict:
    endpoint = p.get("endpoint")
    ranges = [(p.get("start_a"), p.get("end_a")), (p.get("start_b"), p.get("end_b"))]
    if not endpoint or not all(all(r) for r in ranges):
        raise ValueError("需要 endpoint、start_a/end_a、start_b/end_b")
    metrics = []
    for start, end in ranges:
        s, e = timewin.fmt(timewin.parse(start)), timewin.fmt(timewin.parse(end))
        params = {"start": s, "end": e, "ep": endpoint}
        df = query(
            f"SELECT count() AS total, countIf(has_error = 1) AS errors,"
            f" uniqExact(order_number) AS uniq_res,"
            f" countIf(order_number IS NOT NULL AND order_number != '') AS with_res"
            f" FROM ods_api_log WHERE {exprs.time_filter()} AND {exprs.ENDPOINT} = %(ep)s",
            params)
        r = df.iloc[0]
        sdf = query(
            f"SELECT src, count() AS cnt FROM (SELECT {exprs.API_SRC_IP} AS src, create_time,"
            f" controller, function FROM ods_api_log WHERE {exprs.time_filter()}"
            f" AND {exprs.ENDPOINT} = %(ep)s) WHERE src != ''"
            f" GROUP BY src ORDER BY cnt DESC LIMIT 1", params)
        total = int(r["total"])
        top_src = (src_fp(sdf.iloc[0]["src"]), int(sdf.iloc[0]["cnt"])) if len(sdf) else (None, 0)
        with_res = int(r["with_res"])
        metrics.append({
            "range": f"{s} ~ {e}", "total": total,
            "top_source_fp": top_src[0],
            "top_source_share": round(top_src[1] / total, 3) if total else 0,
            "unique_resource_ratio": round(int(r["uniq_res"]) / with_res, 4) if with_res else None,
            "error_rate": round(int(r["errors"]) / total, 4) if total else 0,
        })
    a, b = metrics
    same_src = a["top_source_fp"] == b["top_source_fp"] and a["top_source_fp"]
    note = (f"兩個時段的最大來源皆為 {a['top_source_fp']}（佔 {a['top_source_share']:.0%} / "
            f"{b['top_source_share']:.0%}），模式重複且規律，較接近固定批次整合。"
            if same_src else "兩個時段的主要來源不同，需個別檢視。")
    return _result(["range", "total", "top_source_fp", "top_source_share",
                    "unique_resource_ratio", "error_rate"], metrics, note)


TEMPLATES: tuple[Template, ...] = (
    Template("t01", "即時異常 · 登入安全", "最近一小時 request 最多的 endpoint",
             "依 endpoint 排名並附 28 天基線", ("時間",), "API Log", "3 秒", _top_endpoints),
    Template("t02", "即時異常 · 登入安全", "登入成功異常",
             "各 10 分鐘桶登入成功量 vs 歷史同時段", ("時間",), "Admin Log", "4 秒",
             _login_success_anomaly),
    Template("t03", "即時異常 · 登入安全", "登入失敗最多的來源",
             "依來源 fingerprint 排名登入失敗", ("時間",), "Admin Log", "3 秒", _top_login_failed),
    Template("t04", "即時異常 · 登入安全", "登入後 10 分鐘內的 API 行為",
             "關聯登入與後續 API request", ("時間",), "Admin + API Log", "8 秒", _post_login_api),
    Template("t05", "API 大量讀取 · Backend 查閱", "GetUserByCell 大量使用",
             "手機條件查詢量與來源分布", ("時間",), "API Log", "3 秒", _cell_lookup),
    Template("t06", "API 大量讀取 · Backend 查閱", "orderlist/detail 大量遍歷",
             "逐筆讀取行為與 unique 路徑比例", ("時間",), "Backend System Log", "5 秒",
             _orderlist_traversal),
    Template("t07", "API 大量讀取 · Backend 查閱", "API error 異常",
             "error 比例 vs 28 天基線", ("時間",), "API Log", "3 秒", _api_error),
    Template("t08", "API 大量讀取 · Backend 查閱", "最近一小時 request 最多的品牌",
             "品牌流量排名", ("時間",), "API Log", "3 秒", _top_brands),
    Template("t09", "品牌調查 · 來源調查", "指定 endpoint 的 28 天基線",
             "median / P95 / P99 / 歷史最高", ("endpoint",), "API Log", "1 秒",
             _endpoint_baseline),
    Template("t10", "品牌調查 · 來源調查", "指定來源是否跨品牌",
             "source fingerprint 涉及的品牌數", ("source_fp", "時間"), "API Log", "6 秒",
             _source_cross_brand),
    Template("t11", "品牌調查 · 來源調查", "指定來源是否第一次出現",
             "90 天內首見時間與來源類型", ("source_fp",), "known_sources", "1 秒",
             _source_first_seen),
    Template("t12", "品牌調查 · 來源調查", "最近一小時 request 最多的來源",
             "來源流量排名附基線", ("時間",), "API Log", "6 秒", _top_sources),
    Template("t13", "資料品質 · 歷史比較", "Log 最新時間與延遲",
             "各資料來源新鮮度檢查", (), "全部", "3 秒", _freshness),
    Template("t14", "資料品質 · 歷史比較", "欄位缺漏率",
             "IP / params 等關鍵欄位缺漏比例", (), "全部", "3 秒", _missing_rate),
    Template("t15", "資料品質 · 歷史比較", "重複資料比例",
             "去重前後筆數差異", ("時間",), "Backend / Admin Log", "5 秒", _dup_rate),
    Template("t16", "資料品質 · 歷史比較", "比較兩個日期的異常模式",
             "同 endpoint 兩時段流量與來源結構對比",
             ("兩個時間", "endpoint"), "API Log", "8 秒", _compare_dates),
)

BY_ID = {t.id: t for t in TEMPLATES}


def catalog() -> list[dict]:
    cats: dict[str, list] = {}
    for t in TEMPLATES:
        cats.setdefault(t.category, []).append({
            "id": t.id, "name": t.name, "desc": t.desc,
            "inputs": list(t.inputs), "source": t.source, "eta": t.eta})
    return [{"category": c, "items": items} for c, items in cats.items()]
