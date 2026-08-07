"""資料來源健康：新鮮度、今日量、重複率、欄位缺漏率。"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from console.core import timewin
from console.core.ch import ChQueryError, query
from console.core.config import settings
from console.queries import exprs, source_schema
from console.rules import baseline

# 各表的關鍵欄位缺漏定義（設計稿 4.1 的資料限制）
_MISSING_EXPR = {
    "ods_admin_log": ("ip = ''", "來源 IP"),
    "ods_backend_sys_log": ("ip IS NULL OR ip = ''", "來源 IP"),
    # api_log 的 has_error 僅在出錯時設值，NULL 屬正常；設計稿關注的是 params 可解析性
    "ods_api_log": ("NOT isValidJSON(params)", "params 無法解析"),
    "ods_auth_log": ("ip IS NULL OR ip = ''", "來源 IP"),
    # Order Log 的 params 實測 99.9998% 是合法 JSON（92.7 萬列只有 2 列不是），
    # 拿它當缺漏指標抓不到東西而且要 1.85 秒；改量「分店未填」（0.016%、0.33 秒，
    # 與 api 現況的 0.35 秒同級）。
    #
    # **「沒有來源 IP」刻意不放進 missing_rate。** 那是 100% 的結構事實、
    # 不是浮動比率，放進去只會讓卡片永遠顯示 100% 而看不出任何變化。
    # 它改在四個地方各說一次：_NOTES（就在下面）、Explorer 的
    # explorer.source_meta()（unsupported_filters，經 explorer._ENTITY_FILTER_UNSUPPORTED
    # 產生）、routes._LIMITATIONS_BY_SOURCE，以及 explorer._ENTITY_FILTER_UNSUPPORTED
    # 本身的拒絕理由。
    "ods_order_api_log": ("_store <= 0", "分店未填"),
    # 批次工作沒有 status 也沒有 payload，唯一有意義的缺漏指標是 route 未填。
    # 實測目前 0%，但 route 是這張表唯一的分析維度，空了就完全看不出是哪個工作。
    "ods_batch_request_log": ("route = ''", "批次工作名稱未填"),
    # 53% 的列沒有 xForwardedFor（內部健康檢查與 LB 直連）。這**不是** 100% 的
    # 結構事實而是會浮動的比率，所以放進 missing_rate 是有意義的 ——
    # 比率大幅變化代表流量組成變了。
    "ods_console_backend_sys_log": (
        "JSONExtractString(requester, 'xForwardedForRaw') = ''", "來源 IP 不可用"),
    # in-flight 的那一列 status_code 是 0。它不是「缺漏」而是「還沒完成」，
    # 但比率異常升高代表有大量請求沒有寫回完成狀態 —— 那是真的訊號。
    "ods_request_log": ("status_code = 0", "尚未寫回完成狀態"),
    # request 是這張表唯一的分析來源，不是合法 JSON 就什麼維度都算不出來。
    "ods_voucher_request_log": ("NOT isValidJSON(request)", "request 無法解析"),
    "ods_ec_request_log": ("NOT isValidJSON(request)", "request 無法解析"),
}

_NOTES = {
    "admin": "部分登入紀錄沒有 IP，顯示「來源 IP 不可用」；登入事件以帳號識別、操作事件以 _admin 識別",
    "backend": "歷史資料可能重複，已以事件 ID 去重後顯示；route 含動態段，聚合時取前 2 段",
    "api": "來源 IP 由 forwarded header 推導，標示「未驗證來源」；params 大量非合法 JSON",
    "auth": "最高敏感等級：可能含 token 與登入 secret，僅顯示遮罩摘要",
    "order": "此表沒有 ip 也沒有 headers 欄位，完全沒有來源 IP，"
             "不可做任何單一來源判斷；操作者是 _admin，實測全部是 POS 或串接金鑰帳號"
             "（代表哪一支整合程式，不是哪個人）；歷史資料可能重複，已以事件 ID 去重後顯示",
    "batch": "這是可靠度 log 不是行為 log —— 它回答「批次有沒有跑、量有沒有突變」，"
             "不適合用來找攻擊；ip 欄位恆為 0.0.0.0（內部排程直接呼叫）、"
             "input 實測全部是空的，因此沒有來源、操作者、品牌與分店維度",
    "console": "上游的身分解析目前沒有寫入 —— authentication.account 全部是空、"
               "tokenValid 全部是 false、brandIdx 全部是 null，所以沒有品牌維度，"
               "操作者只有登入請求看得到（取自 body.account）；"
               "約 53% 的列沒有來源 IP（內部健康檢查與 LB 直連，"
               "刻意不退回 requester.ipAddress —— 那是我方 LB 不是來源）；"
               "本表只保留 90 天",
    "request": "報表下載服務；同一個 idx 會短暫有「請求開始」與「完成」兩列"
               "（靠 updated_at 區分，ReplacingMergeTree 合併後只剩一列），"
               "所以合併前的視窗內重複率會短暫上升，那不是資料重複；"
               "沒有帳號欄位（身分只在 headers.authorization 的憑證裡）、"
               "沒有品牌與分店；排名的路由收斂成 api/reports，"
               "要看是哪一份報表請用逐筆明細",
    "voucher": "完全沒有來源 IP（全部是伺服器對伺服器呼叫），不可做任何單一來源判斷；"
               "操作者是呼叫通道（x-ocard-channel-id），代表哪一支整合程式、"
               "不是哪個人；input.brand 是雜湊 token 而非 _brand，所以沒有品牌維度",
    "ec": "五張新表裡唯一有真實消費者 IP 的一張（CloudFront 的 x-forwarded-for，"
          "屬未驗證來源）；操作者是會員 ID，0 代表未登入的訪客請求、不是會員 0 號；"
          "品牌在 response.ouput.ec._brand，非購物車類請求為 0（那是「與品牌無關」"
          "不是「品牌 0」）；沒有分店維度；本表流量低（約 700–1,000 筆/日），"
          "夜間的資料間隔實測可達 12.5 小時，R12 的門檻已另外放寬",
}


# 「本表資料自 X 起」。**由查詢期取得，不可寫死** —— 上游隨時可能回填更早的
# 資料，寫死的值會靜靜過時。一天變化不了幾次，所以快取 6 小時
# （同 brands 的 cache_ttl_seconds 量級）。實測十個來源合計 1.25 秒。
_SINCE_TTL_SECONDS = 21600

# 時間戳零值的下限。**實測 `ods_admin_log` 有 42 列（1,623 萬中）的
# `create_time` 是 1970-01-01**，真正的起始是 2017-04-12。
#
# 不擋的話健康卡會寫「Admin Log 資料自 1970-01-01 起」—— 那是一句假話，而且
# 它會讓整個標註失去可信度：看到 1970 的人不會再相信 Console API Log 那個
# 真實的「自 2026-08-06 起」。
#
# 但**擋掉之後要說出擋了幾列**（`invalid_time_rows`）—— 靜靜排除就是這個專案
# 一再警告的「把沒有資料說成沒有發生」。它與 `missing_rate` 是同一類東西：
# 異常的比率本身就是訊號。
_MIN_VALID_TIME = "2000-01-01 00:00:00"

_since_lock = threading.Lock()
_since_cache: tuple[float, dict[str, dict]] | None = None


def _data_since() -> dict[str, dict]:
    """每個來源的 `{"since": 台北牆鐘字串 | None, "invalid_rows": int}`。

    `since` 排除時間戳零值；`invalid_rows` 是被排除的列數（0 是常態）。
    查詢失敗時整筆回 None／0 —— 這是輔助標示，不該讓整張健康卡失敗。
    """
    global _since_cache
    with _since_lock:
        if _since_cache and _since_cache[0] > time.monotonic():
            return _since_cache[1]

    out: dict[str, dict] = {}
    for key in settings()["data_sources"]:
        schema = source_schema.get(key)
        try:
            df = query(
                f"SELECT minIf({schema.time_expr},"
                f" {schema.time_expr} >= %(floor)s) AS since,"
                f" countIf({schema.time_expr} < %(floor)s) AS invalid"
                f" FROM {schema.table}",
                {"floor": _MIN_VALID_TIME})
            r = df.iloc[0]
            val = r["since"]
            out[key] = {
                "since": (timewin.fmt(val.to_pydatetime())
                          if val is not None and hasattr(val, "to_pydatetime")
                          else None),
                "invalid_rows": int(r["invalid"] or 0),
            }
        except ChQueryError:
            out[key] = {"since": None, "invalid_rows": 0}

    with _since_lock:
        _since_cache = (time.monotonic() + _SINCE_TTL_SECONDS, out)
    return out


def freshness_scale(key: str) -> float:
    """這個來源的新鮮度門檻要放寬幾倍。

    **「距離最後一筆多久」同時混著兩件事**：管線延遲，以及這段時間本來就沒有
    流量。高流量的表兩者分得開，低流量的分不開 —— 實測 2 天內
    `ods_ec_request_log` 有 12 個資料間隔超過 20 分鐘、最長 **752 分**
    （整晚沒有購物流量），`ods_request_log` 有 11 個、最長 152 分。

    那些來源在 `data_sources` 用 `freshness_alert_minutes` 放寬了 R12 的門檻
    （見 `rules/engine._eval_freshness`），而**健康卡的狀態帶必須跟著放寬** ——
    否則規則不誤報了，畫面卻還在誤報：EC API Log 常駐顯示「異常」，
    而 `freshness_summary()` 會把它推到總覽頂部的橫幅上，寫著
    「此期間的異常判斷可能不完整」。那句話是假的，而且它會常駐。

    把值班的人訓練成忽略一個永遠亮著的警示，等於把這個控制拆掉。
    """
    cfg = settings()["freshness"]
    default_alert = float(cfg["alert_minutes"])
    # 未知／缺席的 key 一律不放寬（回 1.0）—— 合成的卡片與尚未註冊的來源
    # 都走這一支，而「不放寬」是安全的方向。
    src = settings()["data_sources"].get(key) or {}
    override = src.get("freshness_alert_minutes")
    if not override or default_alert <= 0:
        return 1.0
    return float(override) / default_alert


def _status(lag_min: float, scale: float = 1.0) -> tuple[str, str]:
    """延遲分鐘數 → (狀態, 顏色)。`scale` 是逐來源的放寬倍數。"""
    cfg = settings()["freshness"]
    if lag_min <= cfg["ok"] * scale:
        return "正常", "#12B76A"
    if lag_min <= cfg["notice"] * scale:
        return "注意", "#DC6803"
    if lag_min <= cfg["stale"] * scale:
        return "異常", "#B42318"
    return "停更", "#98A2B3"


def source_health() -> list[dict]:
    """各來源卡的資料（設計稿 14.1；卡片數量依 `data_sources` 的數量，非固定值）。"""
    now = timewin.taipei_now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_map = _data_since()
    cards: list[dict] = []

    for key, src in settings()["data_sources"].items():
        table = src["table"]
        # `data_since` / `invalid_time_rows` 放在**兩個分支之前**的共用部分，
        # 這樣查詢失敗的卡也帶得到這兩個鍵 —— 前端 `c.data_since` 若在失敗時
        # 是 undefined，那一格會靜靜不渲染。
        since = since_map.get(key, {"since": None, "invalid_rows": 0})
        card = {
            "key": key, "label": src["label"], "table": f"ocard.{table}",
            "sensitive": bool(src.get("sensitive")), "note": _NOTES.get(key, ""),
            "data_since": since["since"],
            "invalid_time_rows": since["invalid_rows"],
        }
        try:
            miss_cond, miss_label = _MISSING_EXPR[table]
            schema = source_schema.get(key)
            tf = exprs.time_filter_for(key)
            # **右界是必要的，不是順手加的。** 原本只有 `create_time >= %(start)s`
            # 沒有右界；改用 `time_filter_for()` 之後 SQL 裡有 `%(end)s`，
            # 缺參數會讓 ClickHouse 直接報錯。用 `now` 而不是明天午夜 ——
            # 「今天到目前為止」才是這張卡要講的事。
            #
            # 去重的鍵走綱要：`ods_request_log` 的鍵是 `idx` 不是 `_id`。
            df = query(
                f"SELECT max({schema.time_expr}) AS latest, count() AS today_rows,"
                f" countIf({miss_cond}) AS missing,"
                f" uniqExact({schema.dedup_col}) AS uniq_ids"
                f" FROM {table} WHERE {tf}",
                {"start": timewin.fmt(today), "end": timewin.fmt(now)})
            r = df.iloc[0]
            latest = r["latest"]
            lag = ((now - latest.to_pydatetime()).total_seconds() / 60
                   if latest is not None else 9999)
            today_rows = int(r["today_rows"])
            uniq_ids = int(r["uniq_ids"])
            status, color = _status(lag, freshness_scale(key))

            y_start = today - timedelta(days=1)
            y_end = y_start + (now - today)
            ydf = query(
                f"SELECT count() AS n FROM {table} WHERE {tf}",
                {"start": timewin.fmt(y_start), "end": timewin.fmt(y_end)})
            yesterday_rows = int(ydf.iloc[0]["n"])

            base = baseline.get(f"table_10m:{key}", hour=now.hour,
                                day_class=baseline.day_class_of(now))
            card.update({
                "status": status, "status_color": color,
                "latest": timewin.fmt(latest.to_pydatetime()) if latest is not None else None,
                "lag_minutes": round(lag, 1),
                "today_rows": today_rows, "yesterday_rows": yesterday_rows,
                "baseline_10m_median": base.median if base else None,
                "dup_rate": round(1 - uniq_ids / today_rows, 4) if today_rows else 0,
                "missing_rate": round(int(r["missing"]) / today_rows, 4) if today_rows else 0,
                "missing_label": miss_label,
                "error": None,
            })
        except ChQueryError as exc:
            card.update({"status": "查詢失敗", "status_color": "#B42318",
                         "latest": None, "lag_minutes": None, "error": str(exc)})
        cards.append(card)
    return cards


def freshness_summary(cards: list[dict]) -> dict:
    """Header 用：整體新鮮度與是否需要顯示延遲橫幅。"""
    cfg = settings()["freshness"]
    worst = None
    for c in cards:
        if c.get("lag_minutes") is None:
            continue
        if worst is None or c["lag_minutes"] > worst["lag_minutes"]:
            worst = c
    failed = [c["label"] for c in cards if c["status"] == "查詢失敗"]
    # 門檻逐來源放寬（同 `_status`）—— 低流量的表用全域門檻會讓橫幅常駐，
    # 而那句「此期間的異常判斷可能不完整」是假的。
    # `c.get("key")` 而不是 `c["key"]`：`freshness_summary()` 也被測試以合成的
    # 卡片呼叫（只帶 lag_minutes / status），而 `freshness_scale(None)` 回 1.0
    # —— 未知來源就是不放寬，那是安全的方向。
    delayed = [c for c in cards
               if (c.get("lag_minutes") or 0)
               > cfg["notice"] * freshness_scale(c.get("key"))]
    return {
        "worst_label": worst["label"] if worst else None,
        "worst_lag": worst["lag_minutes"] if worst else None,
        "latest": worst["latest"] if worst else None,
        "delayed": [{"label": c["label"], "lag": c["lag_minutes"]} for c in delayed],
        "failed": failed,
        "banner": (f"{delayed[0]['label']} 已延遲 {delayed[0]['lag_minutes']:.0f} 分鐘。"
                   "此期間的異常判斷可能不完整。") if delayed else None,
    }
