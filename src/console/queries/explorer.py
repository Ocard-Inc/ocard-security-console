"""Log Explorer 與快速查詢的參數化查詢層。

所有值走 %(param)s；identifier（表名、分組欄位）一律 enum 白名單。

呈現政策見 `core/masking.py`：帳號、來源 IP、訂單號、品牌與分店為**原始值**；
只有 API token 仍是指紋，`params` 預設只給大小與欄位名稱。完整 payload 原文走
`payload()`（由 `api/routes.py` 寫入稽核）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from console.core import brands, masking, stores, timewin
from console.core.ch import query
from console.core.config import settings
from console.queries import exprs, trends

# "auto" 依查詢視窗長度走 trends.BUCKET_LADDER；其餘為手動指定。
# Explorer 是臨時調查工具，手動選項全部保留 —— 分析師要能自己決定顆粒度。
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

# endpoint 篩選作用的欄位。**不在此表的來源就是不支援 endpoint 篩選** ——
# ods_auth_log 沒有 function 欄位，以前這裡是個行內三元式，
# 對 auth 會生出 `startsWith(function, ...)` 而在 ClickHouse 端拋
# 「Unknown expression or function identifier `function`」→ API 回 502。
FILTER_COLUMN = {
    "api": exprs.ENDPOINT,      # controller/function
    "backend": "route",         # 完整 route（含動態段）
    "admin": "function",
}

# 產生 endpoint 候選值的 GROUP BY 運算式（queries/endpoint_suggest.py 用）。
#
# **不變量：SUGGEST_EXPR 的每個輸出都必須是 FILTER_COLUMN 的合法前綴**，
# 否則選單裡點得到、但點下去查不到東西。tests/test_endpoint_suggest.py 會實際
# 把建議值丟回 trend() 驗證這件事。
#
# backend 兩者刻意不同：篩選作用在完整 route，但建議必須取前 2 段，否則
# orderlist/detail/12345 這類含動態段的值會產生上千個一次性選項。
# ROUTE2 的輸出是 route 的前綴，所以拿去 startsWith 仍然成立。
SUGGEST_EXPR = {
    "api": exprs.ENDPOINT,
    "backend": exprs.ROUTE2,
    "admin": "function",
}

# 依「對象」反查的欄位（來源 IP、帳號）。
#
# **刻意直接複用 GROUP_BY 的運算式**，這是一個不變量：排名或明細裡看到的值，
# 貼回篩選器就一定查得到。各表的欄位不同（backend 是 acc、api 是 _admin、
# api 的來源要從 headers 推導），兩邊各寫一份遲早會不一致。
#
# 比對是**完全相等**，不是前綴：貼 `1.34.41.21` 不該連帶命中 `1.34.41.218`。
_ENTITY_FILTER = {
    "source_ip": {src: expr for src, (expr, _, _) in GROUP_BY["source"].items()},
    "actor": {src: expr for src, (expr, _, _) in GROUP_BY["actor"].items()},
}

# 不支援依對象反查的組合，以及為什麼。
#
# auth 的「操作者」是 API token，畫面上是 `token_XXXX` 指紋（HMAC，見 core/masking）。
# 指紋無法反推成原始 token，所以拿指紋去比對資料庫裡的原值永遠不會相等 ——
# 與其讓使用者貼進去查到 0 筆並以為「沒有這個對象」，不如明確說不支援。
_ENTITY_FILTER_UNSUPPORTED = {
    ("actor", "auth"): "Auth Log 的操作者是 API token，畫面上為不可逆指紋，"
                       "無法用指紋反查原始 token。請改用來源 IP 或品牌篩選。",
}


@dataclass(frozen=True)
class ExplorerFilter:
    source: str = "api"
    start: str = ""
    end: str = ""
    brand: int | None = None
    endpoint: str | None = None      # 前綴比對（api: controller/function；backend: route2）
    source_ip: str | None = None     # 依來源 IP 反查（掃描結果 → 明細）
    actor: str | None = None         # 依帳號反查
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


def where_clause(f: ExplorerFilter) -> tuple[str, dict]:
    table = settings()["data_sources"][f.source]["table"]
    clauses = [exprs.time_filter()]
    params: dict = {"start": timewin.fmt(timewin.parse(f.start)),
                    "end": timewin.fmt(timewin.parse(f.end))}
    if f.brand is not None:
        clauses.append("_brand = %(brand)s")
        params["brand"] = f.brand
    if f.endpoint:
        col = FILTER_COLUMN.get(f.source)
        if col is None:
            raise FilterError(
                f"{settings()['data_sources'][f.source]['label']} 不支援 endpoint 篩選"
                "（該表沒有對應欄位）")
        clauses.append(f"startsWith({col}, %(endpoint)s)")
        params["endpoint"] = f.endpoint
    # 依對象反查。這是「從掃描結果或排名追到明細」的那一步 ——
    # 把看到的帳號或 IP 貼進來，就只剩那個對象的資料。
    for field, value in (("source_ip", f.source_ip), ("actor", f.actor)):
        if not value:
            continue
        reason = _ENTITY_FILTER_UNSUPPORTED.get((field, f.source))
        if reason:
            raise FilterError(reason)
        expr = _ENTITY_FILTER[field].get(f.source)
        if expr is None:
            label = settings()["data_sources"][f.source]["label"]
            raise FilterError(f"{label} 不支援依{'來源 IP' if field == 'source_ip' else '帳號'}篩選")
        clauses.append(f"{expr} = %({field})s")
        params[field] = str(value).strip()
    if f.only_error and f.source == "api":
        clauses.append("has_error = 1")
    return f"FROM {table} WHERE " + " AND ".join(clauses), params


def trend(f: ExplorerFilter, bucket: str = "auto") -> dict:
    validate(f)
    if bucket == "auto":
        # 依實際視窗長度挑，跟總覽用同一個階梯
        span = int((timewin.parse(f.end) - timewin.parse(f.start)).total_seconds() // 60)
        minutes = trends.bucket_for(max(span, 1))
    else:
        minutes = BUCKETS.get(bucket)
        if minutes is None:
            raise FilterError(f"未知分桶 {bucket!r}，允許 {['auto', *BUCKETS]}")
    where, params = where_clause(f)
    df = query(
        f"SELECT toStartOfInterval(create_time, INTERVAL {minutes} MINUTE) AS b,"
        f" count() AS cnt {where} GROUP BY b ORDER BY b", params)
    return {
        "bucket": bucket,
        # 前端要顯示「實際用了幾分鐘的桶」，auto 時 bucket 本身看不出來
        "bucket_minutes": minutes,
        "rows": [{"bucket": timewin.fmt(r["b"].to_pydatetime()), "count": int(r["cnt"])}
                 for _, r in df.iterrows()],
    }


def ranking(f: ExplorerFilter, dimension: str, limit: int = 20) -> dict:
    validate(f)
    dims = GROUP_BY.get(dimension)
    if dims is None or f.source not in dims:
        raise FilterError(f"資料來源 {f.source} 不支援分組維度 {dimension!r}")
    expr, fp_kind, label = dims[f.source]
    where, params = where_clause(f)
    # 品牌維度的 k 本身就是品牌，再算一次逐品牌分布沒有意義
    is_brand_dim = dimension == "brand"
    breakdown_col = "" if is_brand_dim else f", {exprs.BRAND_MAP} AS brand_map"
    df = query(
        f"SELECT {expr} AS k, count() AS cnt, uniq(_brand) AS brands{breakdown_col}"
        f" {where} GROUP BY k ORDER BY cnt DESC LIMIT {int(limit)}", params)
    total = int(df["cnt"].sum()) if len(df) else 0
    brand_labels = brands.labels(df["k"]) if is_brand_dim and len(df) else {}
    rows = []
    for i, (_, r) in enumerate(df.iterrows(), 1):
        raw = r["k"]
        # Nullable 欄位在 pandas 是 pd.NA，`not raw` 會拋
        # 「boolean value of NA is ambiguous」而讓整個端點回 502。
        # admin 的操作者是 coalesce(acc, toString(_admin))，兩者皆 NULL 時就會走到這裡。
        if raw is pd.NA or raw is None:
            raw = ""
        if is_brand_dim:
            brand_id = brands.coerce_id(raw)
            name = brand_labels.get(brand_id, "（空）") if brand_id is not None else "（空）"
        elif not raw:
            name = "（空）"
        else:
            name = masking.DISPLAY_FUNCS[fp_kind](raw) if fp_kind else str(raw)
        rows.append({"rank": i, "name": name, "count": int(r["cnt"]),
                     "brands": int(r["brands"]),
                     "brand_top": [] if is_brand_dim else brands.breakdown(r["brand_map"]),
                     "share": round(int(r["cnt"]) / total, 4) if total else 0})
    return {"dimension": dimension, "label": label, "total": total, "rows": rows}


def unique_resource(f: ExplorerFilter) -> dict:
    """Unique resource 分析：逐筆讀取判定（設計稿 10.3）。僅 api_log 支援。"""
    validate(f)
    if f.source != "api":
        raise FilterError("Unique resource 分析僅支援 API Log")
    where, params = where_clause(f)
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
    where, params = where_clause(f)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS total,"
        f" countIf(has_error = 1) AS errors {where} GROUP BY endpoint"
        f" HAVING errors > 0 ORDER BY errors DESC LIMIT 20", params)
    return {"rows": [
        {"endpoint": r["endpoint"], "total": int(r["total"]), "errors": int(r["errors"]),
         "error_rate": round(int(r["errors"]) / int(r["total"]), 4)}
        for _, r in df.iterrows()]}


_DETAIL_COLUMNS = {
    "api": ("_id, create_time, controller, function, _brand, _store, _admin, platform,"
            " headers, params, status, has_error, order_number"),
    "backend": "_id, create_time, acc, ip, _brand, _store, route, post_params, get_params",
    "admin": "_id, create_time, acc, _admin, ip, _brand, _store, function, action, params",
    "auth": "_id, create_time, ip, _brand, _store, action, token, params, response",
}

# 逐筆調閱回傳的欄位（完整原文）。與 _DETAIL_COLUMNS 分開：預設明細給摘要，
# 這裡才給原文，兩者的風險完全不同。
_PAYLOAD_COLUMNS = {
    "api": "_id, create_time, headers, params, status, error",
    "backend": "_id, create_time, post_params, get_params, add_data",
    "admin": "_id, create_time, params",
    "auth": "_id, create_time, headers, params, response",
}


def detail(f: ExplorerFilter) -> dict:
    """明細清單。帳號、IP、訂單號原樣顯示；token 仍為指紋、params 只給摘要。

    完整 payload 原文走 `payload()` —— 一次一筆並寫入稽核。
    """
    validate(f)
    where, params = where_clause(f)
    cols = _DETAIL_COLUMNS[f.source]
    df = query(f"SELECT {cols} {where} ORDER BY create_time DESC LIMIT {int(f.limit)}", params)
    masked = [_mask_detail_row(f.source, dict(r)) for _, r in df.iterrows()]
    brand_labels = brands.labels(r["brand"] for r in masked)
    # 分店編號本身看不出是哪一家；-1 代表品牌層級操作（見 core/stores.py）
    store_labels = stores.labels(r["store"] for r in masked)
    rows = [{**r, "brand_label": brand_labels.get(r["brand"]),
             "store_label": store_labels.get(r["store"])} for r in masked]
    cnt = query(f"SELECT count() AS n {where}", params).iloc[0]["n"]
    return {
        "rows": rows,
        "returned": len(rows),
        "total": int(cnt),
        "truncated": int(cnt) > len(rows),
        # 這段直接寫在明細下方，必須與實際行為一致 —— 說「已遮罩」但畫面上是
        # 原始帳號與 IP，會讓人誤判可以外流。
        "masked_note": "帳號、來源 IP、訂單號與品牌／分店為原始值，可直接追查。"
                       "params 只顯示大小與欄位名稱；token 為不可逆指紋。"
                       "需要 params／headers 原文請用每列的「調閱原文」（會寫入操作稽核）。",
    }


def _mask_detail_row(source: str, r: dict) -> dict:
    # Nullable 欄位在 pandas 是 pd.NA，直接做布林比較會拋 TypeError，先正規化成 None
    r = {k: (None if v is pd.NA or (isinstance(v, float) and pd.isna(v)) else v)
         for k, v in r.items()}
    ts = r["create_time"]
    out = {
        # 逐筆調閱的把手。_id 本身沒有調查價值，只是用來精準指到一列。
        "row_id": str(r.get("_id") or ""),
        "time": timewin.fmt(ts.to_pydatetime()) if hasattr(ts, "to_pydatetime") else str(ts),
        "source": settings()["data_sources"][source]["label"],
        "brand": int(r["_brand"]) if r.get("_brand") is not None else None,
        "store": int(r["_store"]) if r.get("_store") is not None else None,
    }
    if source == "api":
        out.update({
            "endpoint": f"{r['controller']}/{r['function']}",
            "platform": r.get("platform"),
            "source_ip": masking.src(_api_ip_from_headers(r.get("headers"))),
            # api_log 沒有 acc 欄位，操作者以 _admin 識別（同 GROUP_BY 的做法）。
            # 0 代表非後台操作（一般 API 呼叫），不是「查不到」。
            "actor": masking.actor(r.get("_admin")) if r.get("_admin") else None,
            "result": "錯誤" if r.get("has_error") == 1 else "成功",
            "params": masking.payload_summary(r.get("params")),
            "resource": masking.resource(r.get("order_number")),
        })
    elif source == "backend":
        out.update({
            "endpoint": "/".join(str(r["route"]).split("/")[:2]),
            "source_ip": masking.src(r.get("ip")),
            "actor": masking.actor(r.get("acc")),
            "result": "—",
            "params": masking.payload_summary(r.get("post_params")),
            "resource": None,
        })
    elif source == "admin":
        out.update({
            "endpoint": f"{r['function']}/{r['action']}",
            "source_ip": masking.src(r.get("ip")) or "來源 IP 不可用",
            "actor": masking.actor(r.get("acc") or r.get("_admin")),
            "result": "成功" if "success" in str(r.get("action")) else (
                "失敗" if "fail" in str(r.get("action")) else "—"),
            "params": masking.payload_summary(r.get("params")),
            "resource": None,
        })
    else:  # auth
        out.update({
            "endpoint": str(r.get("action")),
            "source_ip": masking.src(r.get("ip")),
            "actor": masking.token_fp(r.get("token")),
            "result": "—",
            "params": masking.payload_summary(r.get("params")),
            "resource": None,
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


def payload(source: str, row_id: str) -> dict:
    """單一筆的**完整 payload 原文**（逐筆調閱）。

    這是預設收斂之外的路徑，不是一般查詢：呼叫端（`api/routes.py`）負責寫入
    `audit_log`。這裡只負責取值，不做遮罩 —— 遮罩與不遮罩的界線在呼叫路徑上，
    不在這個函式裡。

    一次只回一筆、以 `_id` 精準定位：沒有「把整個區間的原文倒出來」這個選項。
    `_id` 是 sorting key 之外的欄位，所以仍需帶時間範圍才不會全表掃 ——
    但調閱的前提是使用者已經在明細裡看到那一列，時間範圍由呼叫端從該列帶入。
    """
    if source not in _PAYLOAD_COLUMNS:
        raise FilterError(f"未知資料來源 {source!r}")
    if not row_id or len(row_id) > 64:
        raise FilterError("row_id 不合法")
    table = settings()["data_sources"][source]["table"]
    cols = _PAYLOAD_COLUMNS[source]
    df = query(f"SELECT {cols} FROM {table} WHERE _id = %(row_id)s LIMIT 1",
               {"row_id": row_id})
    if not len(df):
        raise FilterError("找不到這一筆（可能已超出資料保留範圍）")
    r = {k: (None if v is pd.NA or (isinstance(v, float) and pd.isna(v)) else v)
         for k, v in dict(df.iloc[0]).items()}
    ts = r.pop("create_time", None)
    return {
        "source": source,
        "source_label": settings()["data_sources"][source]["label"],
        "row_id": str(r.pop("_id", "")),
        "time": timewin.fmt(ts.to_pydatetime()) if hasattr(ts, "to_pydatetime") else str(ts),
        # 原文原樣回傳。這是這個端點存在的理由。
        "fields": {k: (None if v is None else str(v)) for k, v in r.items()},
        "warning": "以下為未經清洗的原始內容，可能含消費者個資與有效憑證。"
                   "本次調閱已記錄於操作稽核（誰、何時、哪一筆）。",
    }


# 「查不到」的回看範圍。實測 365 天的等值查詢只要 0.11 秒（月分區 + 等值條件
# 剪枝很有效），所以這裡可以問得很寬 —— 目的就是分辨「這個對象不存在」與
# 「它存在，但不在你選的區間」。
EXTENT_LOOKBACK_DAYS = 365


def entity_extent(source: str, field: str, value: str) -> dict | None:
    """某個對象在近 EXTENT_LOOKBACK_DAYS 天的活動範圍。查無回 None。

    只在「有下對象篩選但結果是 0 筆」時才呼叫（見 api/routes.py）。存在的理由是
    這個系統的一貫原則：**「沒找到」與「查不到」是不同的結論**。畫面只說「0 筆」
    的話，使用者無法分辨自己是打錯值、還是區間選得不對 —— 實測 192.168.97.1
    最後一次出現在 7/29，而 Explorer 預設區間是最近 1 小時。
    """
    expr = _ENTITY_FILTER.get(field, {}).get(source)
    if expr is None or not value:
        return None
    end = timewin.effective_now()
    params = {"start": timewin.fmt(end - timedelta(days=EXTENT_LOOKBACK_DAYS)),
              "end": timewin.fmt(end), "value": str(value).strip()}
    table = settings()["data_sources"][source]["table"]
    df = query(
        f"SELECT count() AS c, min(create_time) AS mn, max(create_time) AS mx"
        f" FROM {table} WHERE {exprs.time_filter()} AND {expr} = %(value)s", params)
    r = df.iloc[0]
    count = int(r["c"] or 0)
    if not count:
        return {"found": False, "lookback_days": EXTENT_LOOKBACK_DAYS}
    return {
        "found": True,
        "count": count,
        "first_seen": timewin.fmt(r["mn"].to_pydatetime()),
        "last_seen": timewin.fmt(r["mx"].to_pydatetime()),
        "lookback_days": EXTENT_LOOKBACK_DAYS,
    }
