"""把命中組合翻成一句人話。純函式。

## 為什麼需要這一層

原本的清單只給對象與訊號標籤（「量級突變」「路由集中」），看的人得自己去展開
evidence、比對數字、才知道發生了什麼。實際使用時的第一個問題永遠是
「所以這是什麼事？」—— 那句話應該直接寫在清單上。

每支探針負責產出自己的一小句（`_PHRASES`），組合起來就是事件的白話描述。
句子只用 evidence 裡實際有的數字，沒有的欄位就不提 —— 不補值、不推估。
"""
from __future__ import annotations

from console.sweep.run import Hit

# 敏感路由 → 給人看的說明。route 名稱本身不夠白話，
# 「orderlist/detail」對非工程背景的人不會自動變成「訂單明細」。
ROUTE_LABELS = {
    "orderlist/detail": "訂單明細",
    "orderlist/delivery": "訂單配送",
    "orderlist/summary": "訂單彙總",
    "orderlist": "訂單列表",
    "order": "訂單",
    "customer/profile": "客戶資料",
    "customer/index": "客戶列表",
    "customer/voucherList": "客戶票券",
    "customer": "客戶資料",
    "point/get-analysis-data": "點數分析",
    "coupon": "優惠券",
    "qa": "健康檢查",
}

SHAPE_LABELS = {
    "forged": "偽造的 loopback 位址（客戶端自己送的，沒有正當用途）",
    "private": "內網位址（取 IP 的邏輯取到了內部 hop，或測試流量混入）",
}


def route_label(route: object) -> str:
    """`orderlist/detail` → `orderlist/detail（訂單明細）`。查無對照就原樣回。"""
    name = str(route or "").strip()
    if not name:
        return "（未知路由）"
    label = ROUTE_LABELS.get(name) or ROUTE_LABELS.get(name.split("/")[0])
    return f"{name}（{label}）" if label else name


def _n(value: object) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: object) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _p01(e: dict) -> str:
    median, peak = e.get("median_prev"), e.get("metric_display")
    if median:
        times = float(peak) / float(median) if peak else None
        base = (f"單日最高 {_n(peak)} 次，而它在區間之前的日常水準是 "
                f"{_n(median)} 次／日")
        return base + (f"（{times:,.0f} 倍）" if times and times >= 2 else "")
    # 沒有自身歷史時明說，不生假倍數
    return (f"單日最高 {_n(peak)} 次；這個帳號在區間之前沒有活動紀錄，"
            "無法比對自身水準")


def _p02(e: dict) -> str:
    return (f"{e.get('peak_day', '峰值日')} 有 {_pct(e.get('top_share'))} 的請求"
            f"集中在 {route_label(e.get('top_route'))}，"
            f"當天共 {_n(e.get('peak_day_total'))} 次")


def _p03(e: dict) -> str:
    median = e.get("median_prev")
    tail = (f"，區間之前是 {_n(median)} 次／日" if median else
            "，區間之前沒有存取這些路由的紀錄")
    return f"單日最高 {_n(e.get('metric_display'))} 次存取訂單／客戶資料類路由{tail}"


def _p04(e: dict) -> str:
    return (f"這個來源登入了 {_n(e.get('metric_display'))} 個不同的後台帳號"
            f"（共 {_n(e.get('total'))} 次請求、{_n(e.get('days'))} 天）")


def _p05(e: dict) -> str:
    return (f"這個來源代 {_n(e.get('metric_display'))} 個不同品牌取得 API token，"
            f"共認證 {_n(e.get('auth_count'))} 次")


def _p06(e: dict) -> str:
    ratio = e.get("req_per_auth")
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = None
    if r is not None and r < 2:
        detail = (f"認證 {_n(e.get('metric_display'))} 次卻只發出 {_n(e.get('req'))} "
                  "次後台請求 —— 反覆取得憑證但幾乎不操作")
    else:
        detail = (f"認證 {_n(e.get('metric_display'))} 次換到 {_n(e.get('req'))} "
                  "次後台請求 —— 一次登入重用 session 大量取數")
    return detail


def _p07(e: dict) -> str:
    org = e.get("org") or "未知業者"
    country = f"／{e['country']}" if e.get("country") else ""
    return (f"來源屬 {org}{country}（{e.get('type_label', '機房或 VPN')}）—— "
            f"真人不會從資料中心登入後台")


def _p08(e: dict) -> str:
    return (f"同一帳號有 {_n(e.get('hosting_requests'))} 次來自機房"
            f"（{e.get('hosting_orgs') or '未知業者'}，佔 {_pct(e.get('hosting_share'))}）、"
            f"另有 {_n(e.get('other_requests'))} 次來自一般來源 —— "
            "憑證同時被真人與程式使用")


def _p09(e: dict) -> str:
    shape = SHAPE_LABELS.get(str(e.get("shape")), "不可信的來源位址")
    return (f"日誌記到的是{shape}，共 {_n(e.get('metric_display'))} 次、"
            f"涉及 {_n(e.get('accs'))} 個帳號")


def _p10(e: dict) -> str:
    return (f"單日最高 {_n(e.get('metric_display'))} 次發生在非上班時間，"
            f"整段期間有 {_pct(e.get('off_share'))} 的請求落在非上班時間")


def _p11(e: dict) -> str:
    return (f"這個來源在區間之前的回看範圍內完全沒出現過，"
            f"卻在區間內發出 {_n(e.get('metric_display'))} 次請求、"
            f"用了 {_n(e.get('accs'))} 個帳號")


def _p12(e: dict) -> str:
    return (f"登入失敗 {_n(e.get('metric_display'))} 次，"
            f"嘗試了 {_n(e.get('accs'))} 個不同帳號")


def _p13(e: dict) -> str:
    return (f"這個 API 來源橫跨 {_n(e.get('metric_display'))} 個品牌讀取資料"
            f"（共 {_n(e.get('total'))} 次、{_n(e.get('endpoints'))} 個 endpoint）")


_PHRASES = {
    "P01": _p01, "P02": _p02, "P03": _p03, "P04": _p04, "P05": _p05, "P06": _p06,
    "P07": _p07, "P08": _p08, "P09": _p09, "P10": _p10, "P11": _p11, "P12": _p12,
    "P13": _p13,
}


def phrase(hit: Hit) -> str:
    """單一命中的白話說明。"""
    fn = _PHRASES.get(hit.probe_id)
    if fn is None:
        return f"{hit.probe_name}：{hit.metric:,.0f}（門檻 {hit.floor:,.0f}）"
    # metric 是 Hit 的一級欄位，但句子產生器統一從 evidence 讀，所以先併進去
    return fn({**hit.evidence, "metric_display": hit.metric})


def headline(hits: tuple[Hit, ...] | list[Hit], ctx: dict | None = None) -> str:
    """一列表格讀得完的摘要：什麼時間到什麼時間、幾次、哪些品牌與分店。

    **刻意不含逐項命中內容** —— 那是 `explains()`，展開時才看。把五個探針的說明
    塞進同一句會變成沒人讀得完的長句（實測 andrew_c 那句有 300 多字）。

    `ctx` 是 `context.collect()` 給的上下文。缺它時退回命中內容 —— 不編造範圍。
    """
    parts: list[str] = []

    if ctx:
        span = (f"{ctx['seen_from']} ~ {ctx['seen_to']}"
                if ctx.get("seen_from") else "")
        scale = f"共 {_n(ctx.get('total_requests'))} 次請求"
        if ctx.get("active_days"):
            scale += f"、{_n(ctx['active_days'])} 天"
        parts.append(f"{span}，{scale}" if span else scale)

        brand_count = ctx.get("brand_count") or 0
        summary = ctx.get("brand_summary") or ""
        if brand_count > 1:
            parts.append(f"跨 {_n(brand_count)} 個品牌"
                         + (f"（{summary}）" if summary else ""))
        elif summary:
            parts.append(f"品牌 {summary}")

        store_top = ctx.get("store_top") or []
        # 只有一個分店且不是品牌層級時才點名 —— 列一長串分店沒有幫助
        if len(store_top) == 1 and store_top[0].get("store", 0) > 0:
            parts.append(f"分店 {store_top[0]['label']}")
        elif (ctx.get("store_count") or 0) > 1:
            parts.append(f"涉及 {_n(ctx['store_count'])} 個分店")

    if not parts:
        # 沒有上下文（查詢失敗或該對象不在 backend log）時退回最強的那句命中
        strongest = max(hits, key=lambda h: h.metric / max(h.floor, 1), default=None)
        return phrase(strongest) if strongest else ""
    return "；".join(parts)


def explains(hits: tuple[Hit, ...] | list[Hit]) -> list[str]:
    """逐項命中的白話說明。依探針編號排序，讀起來的順序固定。"""
    return [phrase(h) for h in sorted(hits, key=lambda x: x.probe_id)]
