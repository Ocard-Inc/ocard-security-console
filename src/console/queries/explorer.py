"""Log Explorer 與快速查詢的參數化查詢層。

所有值走 %(param)s；identifier（表名、分組欄位）一律 enum 白名單，
輸出一律經 masking 遮罩後回傳。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from console.core import masking, timewin
from console.core.ch import query
from console.core.config import settings
from console.queries import exprs

BUCKETS = {"1m": 1, "5m": 5, "10m": 10, "1h": 60, "1d": 1440}

# 分組維度 → (SQL 運算式, 遮罩種類, 顯示名稱)
GROUP_BY = {
    "endpoint": {
        "api": (exprs.ENDPOINT, None, "Endpoint"),
        "backend": (exprs.ROUTE2, None, "Route"),
        "admin": ("concat(function, '/', action)", None, "功能/動作"),
        "auth": ("action", None, "動作"),
    },
    "brand": {k: ("toString(_brand)", None, "品牌") for k in ("api", "backend", "admin", "auth")},
    "source": {
        "api": (exprs.API_SRC_IP, "src", "來源"),
        "backend": ("ip", "src", "來源"),
        "admin": ("ip", "src", "來源"),
        "auth": ("ip", "src", "來源"),
    },
    "actor": {
        "backend": ("acc", "actor", "操作者"),
        "admin": ("coalesce(acc, toString(_admin))", "actor", "操作者"),
        "api": ("toString(_admin)", "actor", "操作者"),
        "auth": ("token", "token", "憑證"),
    },
}


@dataclass(frozen=True)
class ExplorerFilter:
    source: str = "api"
    start: str = ""
    end: str = ""
    brand: int | None = None
    endpoint: str | None = None      # 前綴比對（api: controller/function；backend: route2）
    source_fp: str | None = None     # fingerprint 反查（掃描期間比對）
    actor_fp: str | None = None
    only_error: bool = False
    limit: int = 500


class FilterError(ValueError):
    pass


def validate(f: ExplorerFilter) -> ExplorerFilter:
    if f.source not in settings()["data_sources"]:
        raise FilterError(f"未知資料來源 {f.source!r}")
    if not f.start or not f.end:
        raise FilterError("必須指定開始與結束時間（格式 YYYY-MM-DD HH:MM:SS）")
    try:
        start, end = timewin.parse(f.start), timewin.parse(f.end)
    except ValueError as exc:
        raise FilterError(str(exc)) from exc
    if start >= end:
        raise FilterError("開始時間必須早於結束時間")
    max_days = settings()["audit_export"]["max_range_days"]
    if (end - start).days > max_days:
        raise FilterError(f"時間範圍不可超過 {max_days} 天")
    if f.limit <= 0 or f.limit > 5000:
        raise FilterError("limit 必須在 1~5000")
    return f


def _where(f: ExplorerFilter) -> tuple[str, dict]:
    table = settings()["data_sources"][f.source]["table"]
    clauses = [exprs.time_filter()]
    params: dict = {"start": timewin.fmt(timewin.parse(f.start)),
                    "end": timewin.fmt(timewin.parse(f.end))}
    if f.brand is not None:
        clauses.append("_brand = %(brand)s")
        params["brand"] = f.brand
    if f.endpoint:
        col = exprs.ENDPOINT if f.source == "api" else (
            "route" if f.source == "backend" else "function")
        clauses.append(f"startsWith({col}, %(endpoint)s)")
        params["endpoint"] = f.endpoint
    if f.only_error and f.source == "api":
        clauses.append("has_error = 1")
    return f"FROM {table} WHERE " + " AND ".join(clauses), params


def trend(f: ExplorerFilter, bucket: str = "10m") -> dict:
    validate(f)
    minutes = BUCKETS.get(bucket)
    if minutes is None:
        raise FilterError(f"未知分桶 {bucket!r}，允許 {list(BUCKETS)}")
    where, params = _where(f)
    df = query(
        f"SELECT toStartOfInterval(create_time, INTERVAL {minutes} MINUTE) AS b,"
        f" count() AS cnt {where} GROUP BY b ORDER BY b", params)
    return {"bucket": bucket, "rows": [
        {"bucket": timewin.fmt(r["b"].to_pydatetime()), "count": int(r["cnt"])}
        for _, r in df.iterrows()]}


def ranking(f: ExplorerFilter, dimension: str, limit: int = 20) -> dict:
    validate(f)
    dims = GROUP_BY.get(dimension)
    if dims is None or f.source not in dims:
        raise FilterError(f"資料來源 {f.source} 不支援分組維度 {dimension!r}")
    expr, fp_kind, label = dims[f.source]
    where, params = _where(f)
    df = query(
        f"SELECT {expr} AS k, count() AS cnt, uniq(_brand) AS brands"
        f" {where} GROUP BY k ORDER BY cnt DESC LIMIT {int(limit)}", params)
    total = int(df["cnt"].sum()) if len(df) else 0
    rows = []
    for i, (_, r) in enumerate(df.iterrows(), 1):
        raw = r["k"]
        name = (masking.FP_FUNCS[fp_kind](raw) if fp_kind else str(raw)) if raw else "（空）"
        rows.append({"rank": i, "name": name, "count": int(r["cnt"]),
                     "brands": int(r["brands"]),
                     "share": round(int(r["cnt"]) / total, 4) if total else 0})
    return {"dimension": dimension, "label": label, "total": total, "rows": rows}


def unique_resource(f: ExplorerFilter) -> dict:
    """Unique resource 分析：逐筆讀取判定（設計稿 10.3）。僅 api_log 支援。"""
    validate(f)
    if f.source != "api":
        raise FilterError("Unique resource 分析僅支援 API Log")
    where, params = _where(f)
    df = query(
        f"SELECT count() AS total, uniqExact(order_number) AS uniq_resources,"
        f" countIf(order_number IS NOT NULL AND order_number != '') AS with_resource"
        f" {where}", params)
    r = df.iloc[0]
    with_res = int(r["with_resource"])
    return {
        "total": int(r["total"]),
        "with_resource": with_res,
        "unique_resources": int(r["uniq_resources"]),
        "unique_ratio": round(int(r["uniq_resources"]) / with_res, 4) if with_res else None,
        "note": "unique 比例接近 1 表示逐筆讀取不同資源（遍歷特徵）；為 0 表此區間無資源類請求",
    }


def error_analysis(f: ExplorerFilter) -> dict:
    validate(f)
    if f.source != "api":
        raise FilterError("錯誤分析僅支援 API Log")
    where, params = _where(f)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS total,"
        f" countIf(has_error = 1) AS errors {where} GROUP BY endpoint"
        f" HAVING errors > 0 ORDER BY errors DESC LIMIT 20", params)
    return {"rows": [
        {"endpoint": r["endpoint"], "total": int(r["total"]), "errors": int(r["errors"]),
         "error_rate": round(int(r["errors"]) / int(r["total"]), 4)}
        for _, r in df.iterrows()]}


_DETAIL_COLUMNS = {
    "api": ("create_time, controller, function, _brand, platform, headers, params,"
            " status, has_error, order_number"),
    "backend": "create_time, acc, ip, _brand, route, post_params, get_params",
    "admin": "create_time, acc, _admin, ip, _brand, function, action, params",
    "auth": "create_time, ip, _brand, action, token, params, response",
}


def detail(f: ExplorerFilter) -> dict:
    """遮罩後明細（設計稿 10.5：禁止顯示 _id、原始 IP/headers/params/token/訂單號）。"""
    validate(f)
    where, params = _where(f)
    cols = _DETAIL_COLUMNS[f.source]
    df = query(f"SELECT {cols} {where} ORDER BY create_time DESC LIMIT {int(f.limit)}", params)
    rows = [_mask_detail_row(f.source, dict(r)) for _, r in df.iterrows()]
    cnt = query(f"SELECT count() AS n {where}", params).iloc[0]["n"]
    return {
        "rows": rows,
        "returned": len(rows),
        "total": int(cnt),
        "truncated": int(cnt) > len(rows),
        "masked_note": "明細已遮罩：不顯示原始 IP、headers、params、token、訂單號、"
                       "會員 ID、手機或 Email。params 僅顯示大小與欄位類別。",
    }


def _mask_detail_row(source: str, r: dict) -> dict:
    # Nullable 欄位在 pandas 是 pd.NA，直接做布林比較會拋 TypeError，先正規化成 None
    r = {k: (None if v is pd.NA or (isinstance(v, float) and pd.isna(v)) else v)
         for k, v in r.items()}
    ts = r["create_time"]
    out = {
        "time": timewin.fmt(ts.to_pydatetime()) if hasattr(ts, "to_pydatetime") else str(ts),
        "source": settings()["data_sources"][source]["label"],
        "brand": int(r["_brand"]) if r.get("_brand") is not None else None,
    }
    if source == "api":
        out.update({
            "endpoint": f"{r['controller']}/{r['function']}",
            "platform": r.get("platform"),
            "source_fp": masking.src_fp(_api_ip_from_headers(r.get("headers"))),
            "actor_fp": None,
            "result": "錯誤" if r.get("has_error") == 1 else "成功",
            "params": masking.payload_summary(r.get("params")),
            "resource_fp": masking.resource_fp(r.get("order_number")),
        })
    elif source == "backend":
        out.update({
            "endpoint": "/".join(str(r["route"]).split("/")[:2]),
            "source_fp": masking.src_fp(r.get("ip")),
            "actor_fp": masking.actor_fp(r.get("acc")),
            "result": "—",
            "params": masking.payload_summary(r.get("post_params")),
            "resource_fp": None,
        })
    elif source == "admin":
        out.update({
            "endpoint": f"{r['function']}/{r['action']}",
            "source_fp": masking.src_fp(r.get("ip")) or "來源 IP 不可用",
            "actor_fp": masking.actor_fp(r.get("acc") or r.get("_admin")),
            "result": "成功" if "success" in str(r.get("action")) else (
                "失敗" if "fail" in str(r.get("action")) else "—"),
            "params": masking.payload_summary(r.get("params")),
            "resource_fp": None,
        })
    else:  # auth
        out.update({
            "endpoint": str(r.get("action")),
            "source_fp": masking.src_fp(r.get("ip")),
            "actor_fp": masking.token_fp(r.get("token")),
            "result": "—",
            "params": masking.payload_summary(r.get("params")),
            "resource_fp": None,
        })
    return out


def _api_ip_from_headers(headers: object) -> str | None:
    """從 headers JSON 取 forwarded IP（未驗證來源）。"""
    import json
    if not headers:
        return None
    try:
        d = json.loads(headers)
    except (ValueError, TypeError):
        return None
    for key in ("X-real-ip", "x-real-ip"):
        if d.get(key):
            return str(d[key])
    for key in ("X-forwarded-for", "x-forwarded-for"):
        if d.get(key):
            return str(d[key]).split(",")[0].strip()
    return None


def resolve_window(preset: str) -> tuple[str, str]:
    """全域時間快捷 → (start, end)（設計稿 5.3）。"""
    end = timewin.align_tick(timewin.effective_now())
    presets = {
        "10m": 10, "30m": 30, "1h": 60, "6h": 360, "7d": 7 * 1440,
    }
    if preset in presets:
        return timewin.fmt(end - timedelta(minutes=presets[preset])), timewin.fmt(end)
    if preset == "today":
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return timewin.fmt(start), timewin.fmt(end)
    if preset == "yesterday":
        today = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return timewin.fmt(today - timedelta(days=1)), timewin.fmt(today)
    raise FilterError(f"未知時間快捷 {preset!r}")
