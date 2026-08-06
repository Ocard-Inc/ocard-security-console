"""異常事件 → Log Explorer 的篩選條件推導。

事件詳細頁把「是什麼事」講完了，但要再往下查，原本只剩手動路徑：把畫面上的
帳號或 IP 抄下來、切到 Explorer、選對資料來源、把時間打成事件視窗、把值貼回
Filter Builder。四個步驟每一步都可能打錯，而打錯的症狀是 **0 筆** ——
與「真的沒有這件事」長得一模一樣。這個模組把那條路徑變成一顆按鈕。

## 這裡只放一種新知識

「規則的 entity 欄位 → Explorer 的篩選欄位名」（`_FILTER_BY_FP` / `_FILTER_BY_COL`）。
「哪個篩選在哪張表可用」不在這裡 —— 那是 `queries/explorer.filter_support()` 的事，
本模組問它。同理 SQL 運算式一概不碰：值貼回去要能命中，靠的是 Explorer 的
`_ENTITY_FILTER` 刻意複用 `GROUP_BY` 的那個不變量。

## 純函式、無 I/O、不拋例外

`build()` 由 `GET /events/{evt_no}` 呼叫。任何例外都會 500 掉事件詳細頁，
所以比照 `rules/engine.evaluate` 的逐規則錯誤隔離：全捕捉，降級成
`{"supported": False, "reason": ...}`。也**不做任何查詢** —— 需要的東西
（entity 值、視窗、資料來源）在偵測當下就已經寫進 `events` 了。

## 逐欄位降級，不是整筆否決

2026-08-03 政策改版前的事件，context 裡是 `actor_XXXXXXXXXXXX` 這類不可逆指紋，
拿去比對 ClickHouse 的原值永遠不會相等。但 EVT-0001（R03）同時有一個 legacy 的
`src_` 與一個完全可用的 `endpoint` —— 整筆否決等於丟掉能用的一半。所以每個
entity 欄位獨立判定，被丟掉的進 `dropped`，由畫面說出來是哪一個、為什麼。
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from console.core import brands, timewin
from console.core.config import settings
from console.queries import explorer
from console.rules.model import Rule

logger = logging.getLogger(__name__)

# entity 的 `fp` 種類 → Explorer 篩選欄位。
# `token` 刻意不在表內：那是不可逆指紋（`masking.token_fp`），反查不可能。
_FILTER_BY_FP = {"actor": "actor", "src": "source_ip"}

# `fp: null` 的 entity 欄位名 → Explorer 篩選欄位。
# 不在表內的 col 不產生篩選（R09 的 `scope`、R12 的 `key` 是字面常數而非欄位值）。
_FILTER_BY_COL = {"endpoint": "endpoint", "route2": "endpoint", "_brand": "brand",
                  "_store": "store"}

# 改版前的指紋格式（見 `core/masking.token_fp`：前綴 + HMAC 取 12 碼大寫）。
# 用嚴格樣式而不是只看前綴，免得把真的叫 `src_something` 的帳號誤判成指紋。
_LEGACY_FP_RE = re.compile(r"^(?:actor|src|token|resource)_[0-9A-F]{12}$")

# `rules/engine._masked_context` 對非 fp 的字串欄位走 `masking.scrub_text`，
# 它會把 `token:`／`secret=` 樣式改寫成 `***`、過長的截斷成「…（截斷，原長 N）」。
# 這種值當 startsWith 前綴會靜靜命中 0 筆，必須擋掉。
_SCRUBBED_MARKERS = ("***", "（截斷")

# 視窗長度上限。api 的來源 IP 走 `exprs.API_SRC_IP`（對 headers 做 JSONExtract），
# 實測 4 小時區間的逐筆明細 2.1 秒；而事件可以持續數週（`miss_ticks` 每次命中都
# 重設），所以不能直接把 first_seen→last_seen 整段丟過去。夾的理由與
# `routes.RANKING_MAX_MINUTES` / `MAX_CUSTOM_RANGE_DAYS` 完全相同。
_MAX_MINUTES_JSON_IP = 24 * 60
_MAX_MINUTES_DEFAULT = 31 * 24 * 60

# 「只有 endpoint 篩選」時要接著回答「誰在打」，各表的答案不同。
# admin 用 endpoint 排名是刻意的：Explorer 的 admin endpoint 排名分組是
# `concat(function, '/', action)`，而篩選作用在 `function` —— R06 的
# `Boss_initial/auth_v2` 前綴會同時撈到 login_failed，但排名會把 success 與
# failed 分成兩列，所以「過寬」在這個分析下反而看得更清楚。
_ANALYSIS_FOR_ENDPOINT_ONLY = {"api": "source", "backend": "actor", "admin": "endpoint"}


def build(rule: Rule | None, event: dict) -> dict:
    """事件 → Explorer 篩選條件。

    回傳 `{"supported": True, "filter": {...}, "window": {...}, "dropped": [...]}`
    或 `{"supported": False, "reason": "<中文原因>"}`。
    """
    try:
        return _build(rule, event)
    except Exception:                                  # noqa: BLE001 —— 見模組說明
        logger.exception("drilldown 推導失敗 evt=%s", event.get("evt_no"))
        return {"supported": False,
                "reason": "無法從這個事件推導 Explorer 篩選條件（已記錄於伺服器 log）。"}


def _build(rule: Rule | None, event: dict) -> dict:
    source = event.get("source")
    if rule is None:
        return _no(f"找不到規則 {event.get('rule_id')!r} 的定義"
                   "（規則可能已改名或移除），無法推導篩選條件。")
    if source not in settings()["data_sources"]:
        # `all`（R12 資料管線失速）沒有對應的單一資料表。這裡明確給原因，
        # 不要讓它走到 Explorer 才換來一個 400。
        return _no(f"{rule.name}沒有對應的單一資料表（涵蓋全部資料來源），"
                   "無法在 Log Explorer 重現。請改用「資料健康」頁。")

    ctx = event.get("context") or {}
    filters, dropped = _entity_filters(rule, ctx, source)

    # entity 是字面常數的規則（R09 的 `scope = 'api_error'`）本來就沒有對象 ——
    # 它的調查範圍就是整張表，這是唯一一個「沒有 entity 篩選仍然成立」的具名分支。
    # 要求 entity 非空：真正沒有 entity 的規則（R12）不該從這裡溜過去。
    constant_entity = bool(rule.entity) and all(
        f.fp is None and f.col not in _FILTER_BY_COL for f in rule.entity)
    if not filters and not constant_entity:
        why = "；".join(d["reason"] for d in dropped) or "這條規則的對象欄位無法轉成篩選條件"
        # dropped 一併回去：畫面要能逐欄位說明，而不是只有一句總結
        return _no(f"這個事件的對象無法帶進 Log Explorer：{why}。"
                   "少了對象條件，查出來的是「所有人做了什麼」而不是這個事件，"
                   "數字會與事件對不上，因此不提供跳轉。", dropped)

    window = _window(event, source, filters)
    analysis = _analysis(source, filters, constant_entity)
    return {
        "supported": True,
        "filter": {
            "source": source,
            "start": window["start"],
            "end": window["end"],
            # only_error 刻意不與 analysis='error' 併用：`error_analysis` 的
            # total 與 errors 共用同一個 WHERE，兩者相等會讓每一列的
            # error_rate 都變成 100%，看起來像全站壞掉。
            "only_error": False,
            "analysis": analysis,
            "bucket": "auto",
            **filters,
        },
        "window": window,
        "dropped": dropped,
        "origin": {"evt_no": event.get("evt_no"), "rule_id": rule.id, "rule_name": rule.name,
                   # 「往前後各拉 N 分鐘」用：等長視窗落在趨勢圖上會是一片高原，
                   # 看不出事件之前長什麼樣。預設不加緩衝，但要讓它一鍵可得。
                   "window_minutes": rule.window_minutes},
    }


def _no(reason: str, dropped: list[dict] | None = None) -> dict:
    return {"supported": False, "reason": reason, "dropped": dropped or []}


def _entity_filters(rule: Rule, ctx: dict, source: str) -> tuple[dict, list[dict]]:
    """逐 entity 欄位轉成 Explorer 篩選。回傳 (可用的篩選, 被丟掉的與原因)。"""
    filters: dict = {}
    dropped: list[dict] = []

    def drop(col: str, reason: str) -> None:
        dropped.append({"col": col, "reason": reason})

    for f in rule.entity:
        field = _FILTER_BY_FP.get(f.fp) if f.fp else _FILTER_BY_COL.get(f.col)
        if field is None:
            if f.fp == "token":
                drop(f.col, "操作者是 API token，畫面上是不可逆指紋，無法反查原始 token")
            continue                       # 字面常數欄位（scope/key）：不是缺陷，不用報告
        if f.col not in ctx or ctx[f.col] is None:
            # `_masked_context` 跳過 None，所以欄位可能整個不存在
            drop(f.col, f"偵測當下 {f.col} 為空")
            continue
        reason = explorer.filter_support(field, source)
        if reason:
            drop(f.col, reason)
            continue

        # 品牌與分店都是整數欄位，而事件 context 存的是 float（pandas 把純數值的
        # 整列升成 float64）。不轉的話 `4748.0` / `27681.0` 會直接進 SQL 而命中
        # 0 筆，畫面上看起來像「這個對象沒有活動」。
        if field in ("brand", "store"):
            value_id = brands.coerce_id(ctx[f.col])
            if value_id is None:
                label = "品牌" if field == "brand" else "分店"
                drop(f.col, f"{label}編號 {ctx[f.col]!r} 無法解析為整數")
                continue
            filters[field] = value_id
            continue

        value = _clean(ctx[f.col])
        if value is None:
            drop(f.col, f"偵測當下 {f.col} 為空")
        elif _LEGACY_FP_RE.match(value):
            drop(f.col, f"{value} 是 2026-08-03 政策改版前的不可逆指紋，無法反查原始值")
        elif any(m in value for m in _SCRUBBED_MARKERS):
            drop(f.col, f"{f.col} 的值在存檔時被清洗或截斷，不能當篩選條件")
        else:
            filters[field] = value
    return filters, dropped


def _clean(value: object) -> str | None:
    """entity 值 → 篩選字串。

    整數型的 entity（admin 的 `_admin`）經 pandas 可能是 float，直接 str() 會得到
    `-1.0` 而永遠比不中 `toString(_admin)`。與 `engine.entity_parts` 同一個處理。
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _window(event: dict, source: str, filters: dict) -> dict:
    """事件視窗 → Explorer 的絕對區間。

    用 `first_seen` .. `last_seen` 原值（兩者本來就完整包住所有命中），只做兩件
    必要的夾擠，且一律在 Python 端用 `core/timewin` 算 —— 絕不在 JS 或 SQL 裡
    做時間算術（見 CLAUDE.md「時間」那條）。
    """
    start = timewin.parse(event["first_seen"])
    end = timewin.parse(event["last_seen"])
    full = {"start": timewin.fmt(start), "end": timewin.fmt(end)}

    # 右界不可超過已落地的資料。`explorer.trend` 不做 zero-fill，超出的尾巴在圖上
    # 看不見，但 meta.time_range 與稽核列會宣稱查了那一段。
    landed = timewin.align_tick(timewin.effective_now())
    end = min(end, landed)
    if end <= start:
        end = start + timedelta(minutes=1)

    max_minutes = (_MAX_MINUTES_JSON_IP if source == "api" and "source_ip" in filters
                   else _MAX_MINUTES_DEFAULT)
    clamped = (end - start) > timedelta(minutes=max_minutes)
    if clamped:
        # 保留最近的一段：長跑事件要查的通常是它現在還在做什麼
        start = end - timedelta(minutes=max_minutes)
    return {"start": timewin.fmt(start), "end": timewin.fmt(end),
            "clamped": clamped, "max_minutes": max_minutes,
            "full_start": full["start"], "full_end": full["end"]}


def _analysis(source: str, filters: dict, constant_entity: bool) -> str:
    """落地時預選哪一種分析 —— 依**存活的篩選**決定，不是依規則 id。

    這樣新增規則不必回來改對照表，而每條規則仍然落在最合適的分析上。
    """
    if constant_entity and not filters:
        # R09：整張表的錯誤分布。error 分析只有 api 有（explorer.error_analysis）。
        return "error" if source == "api" else "trend"
    if "actor" in filters or "source_ip" in filters:
        # 事件頁的趨勢圖是**整個資料來源**的量（routes.py 明寫「非僅該異常對象的
        # 請求量」）。該對象自己的時序正是事件頁結構上給不出的東西，也最便宜，
        # 而且每一張表都支援 trend（error / unique_resource 只有 api 有）。
        return "trend"
    if "endpoint" in filters:
        return _ANALYSIS_FOR_ENDPOINT_ONLY.get(source, "trend")
    if "brand" in filters:
        return "endpoint"                  # 品牌內部是什麼在動
    return "trend"
