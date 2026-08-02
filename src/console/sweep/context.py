"""為事件清單補上「這到底是誰、影響誰、什麼時候」。

## 為什麼是獨立查詢，不塞進探針 SQL

探針的 metric 各自算在不同層的巢狀子查詢裡（單日峰值、峰值日集中度、跨表 UNION），
要把品牌／分店／時間範圍一路 `sumMap` 穿上來，每支的寫法都不一樣，而且踩過
「別名與內層欄位同名 → ClickHouse 判定聚合套聚合」這種坑。

改成拿到事件清單之後，對這批對象各補一次查詢：**兩趟查詢**（帳號一趟、來源一趟），
與探針數量無關，而且每個對象拿到的欄位一致 —— 不會因為它是被哪支探針命中的
而有不同的上下文。

## 這裡查的是「區間內」的實際活動範圍

`seen_from` / `seen_to` 是該對象在使用者選的區間內**第一次與最後一次**出現的時間，
不是區間邊界。使用者問的是「什麼時間到什麼時間點」，答案應該是資料說的那個範圍，
不是他自己選的那個範圍。
"""
from __future__ import annotations

import logging
from datetime import datetime

from console.core import brands, stores, timewin
from console.core.ch import ChQueryError, query
from console.queries import exprs

logger = logging.getLogger(__name__)

# 每個對象展開的品牌／分店數上限。跨品牌的規模用 `brands` 總數表達，
# 這裡只是讓人看到「是哪幾家」。
TOP_N = 5


def _rows(column: str, entities: list[str], start: datetime, end: datetime) -> dict:
    """對 `column`（acc 或 ip）在區間內的活動做一次彙總。"""
    if not entities:
        return {}
    # identifier 是程式內常數；值一律走參數
    placeholders = ", ".join(f"%(e{i})s" for i in range(len(entities)))
    params: dict = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    params.update({f"e{i}": v for i, v in enumerate(entities)})
    sql = f"""
    SELECT coalesce({column}, '') AS entity,
           count() AS total,
           min(create_time) AS seen_from,
           max(create_time) AS seen_to,
           uniqExact(toDate(create_time)) AS active_days,
           uniqExact(_brand) AS brand_count,
           uniqExact(_store) AS store_count,
           {exprs.BRAND_MAP} AS brand_map,
           {exprs.STORE_MAP} AS store_map
    FROM ods_backend_sys_log
    WHERE {exprs.time_filter()}
      AND coalesce({column}, '') IN ({placeholders})
    GROUP BY entity
    """
    df = query(sql, params)
    out = {}
    for _, r in df.iterrows():
        brand_top = brands.breakdown(r["brand_map"], limit=TOP_N)
        store_top = _store_breakdown(r["store_map"])
        out[str(r["entity"])] = {
            "total_requests": int(r["total"]),
            "seen_from": timewin.fmt(r["seen_from"].to_pydatetime()),
            "seen_to": timewin.fmt(r["seen_to"].to_pydatetime()),
            "active_days": int(r["active_days"]),
            "brand_count": int(r["brand_count"]),
            "store_count": int(r["store_count"]),
            "brand_top": brand_top,
            "store_top": store_top,
            "brand_summary": brands.top_summary(brand_top, limit=3),
        }
    return out


def _store_breakdown(store_map: object) -> list[dict]:
    """`STORE_MAP`（sumMap）→ 次數由高到低的前 N 個分店，帶名稱。"""
    pairs = brands.to_pairs(store_map)           # 形狀與 BRAND_MAP 相同，共用解析
    if not pairs:
        return []
    pairs.sort(key=lambda p: (-p[1], p[0]))
    top = pairs[:TOP_N]
    lut = stores.labels(s for s, _ in top)
    return [{"store": s, "label": lut.get(s, str(s)), "count": c} for s, c in top]


def collect(entities_by_kind: dict[str, list[str]],
            start: datetime, end: datetime) -> dict[tuple[str, str], dict]:
    """回傳 {(entity_kind, entity): 上下文}。查詢失敗不擋報告，回空的那一類。"""
    out: dict[tuple[str, str], dict] = {}
    for kind, column in (("actor", "acc"), ("src", "ip")):
        entities = entities_by_kind.get(kind) or []
        try:
            found = _rows(column, entities, start, end)
        except ChQueryError as exc:
            logger.warning("事件上下文查詢失敗（%s）：%s", kind, exc)
            continue
        for entity, ctx in found.items():
            out[(kind, entity)] = ctx
    return out
