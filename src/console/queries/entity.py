"""事件**對象視角**的查詢：母體位置、24 小時作息、端點來源集中度。

事件詳細頁原本唯一的圖是 `routes._event_trend()` —— 整個資料來源的總量，
與事件對象無關。實際造成的誤讀是：圖上實際值 12–20 萬、同時段基線 median
39–58 萬，於是看圖的人得到「量比平常低，所以沒事」，而圖上沒有任何一個像素
跟那個對象有關。`api/drilldown.py` 的註解早就記下這個缺口
（「該對象自己的時序正是事件頁結構上給不出的東西」）。

本模組提供三個**便宜**的對象視角查詢（實測合計約 3 秒）：

- `peers()`      —— 同單位的母體排名與分位數（回答「跟其他對象差多少」）
- `hour_profile()` —— 對象 vs 全站的 24 小時作息（回答「這是機器還是人」）
- `endpoint_share()` —— 該 endpoint 的來源集中度（回答「這正常嗎」）

對象自己的長期時序（28 天）在 `queries/entity_history.py` —— 那支要 15 秒，
必須走獨立端點與延後載入，不能和這裡混在一起。

## 對象條件一律經 `explorer.entity_expr()`

事件的 entity 值是規則 SQL 算出來的（`endpoint` = `exprs.ENDPOINT`、
`route2` = `exprs.ROUTE2`），與 `explorer.GROUP_BY` 逐表相同運算式，所以拿
`entity_expr()` 回來比對一定命中。**這裡不自己列第二份對照表** ——
「規則 entity → 篩選欄位」是 `api/drilldown.py` 的事，「篩選欄位 → SQL」是
`explorer` 的事，本模組只把兩者串起來。

比對是**完全相等**。用前綴的話 `Api2/GetProfileExtra` 會被算進
`Api2/GetProfile` 的對象裡，數字比事件大、而且不會有任何錯誤訊息。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from console.core import masking, timewin
from console.core.ch import query
from console.core.config import settings
from console.queries import explorer, exprs

logger = logging.getLogger(__name__)

# 母體排名取幾名。12 是「看得出分布形狀」與「一個面板放得下」的折衷；
# 真正的母體規模由 `peers()['groups']` 說出來，不是由這個數字暗示。
PEER_LIMIT = 12

# 端點來源集中度取幾名。集中度的重點是第一名的佔比，後面是陪襯。
SHARE_LIMIT = 6

# 作息與集中度的回看天數。7 天足以蓋過一個完整的週間／週末循環，
# 而且對 api 表（來源 IP 要對 headers 做 JSONExtract）實測仍在 1.5 秒內；
# 28 天要 15 秒，那是 `entity_history` 的工作。
PROFILE_DAYS = 7


@dataclass(frozen=True)
class Dim:
    """對象的一個維度。"""
    field: str          # Explorer 篩選欄位名（source_ip / actor / endpoint / brand）
    expr: str           # 完全相等比對用的 SQL 運算式
    value: str          # 事件當下的值（原始值，不是指紋）
    mask: str | None    # masking.DISPLAY_FUNCS 的鍵；None = 原樣顯示
    label: str          # 顯示名稱


@dataclass(frozen=True)
class EntityRef:
    """事件對象在 ClickHouse 的可查詢形式。"""
    source: str
    table: str
    dims: tuple[Dim, ...]

    @property
    def where(self) -> str:
        return " AND ".join(f"{d.expr} = %({_p(i)})s" for i, d in enumerate(self.dims))

    @property
    def params(self) -> dict:
        return {_p(i): d.value for i, d in enumerate(self.dims)}

    @property
    def label(self) -> str:
        return " · ".join(_display(d) for d in self.dims)

    def dim(self, field: str) -> Dim | None:
        return next((d for d in self.dims if d.field == field), None)


def _p(i: int) -> str:
    return f"ent{i}"


def _display(dim: Dim) -> str:
    if dim.mask is None:
        return dim.value
    return masking.DISPLAY_FUNCS[dim.mask](dim.value) or dim.value


def from_filters(source: str, filters: dict) -> EntityRef | None:
    """`drilldown` 推導出的篩選 → `EntityRef`；沒有任何可用維度回 None。

    `filters` 就是 `drilldown.build()['filter']` 裡的 entity 欄位，所以
    legacy 指紋、被清洗的值、該表不支援的欄位都已經在那裡被剔除了 ——
    本模組**不重做**那些判斷，重做就會有兩份會漂移的規則。

    回 None 的情況（R09 的字面常數 scope、R12 沒有 entity）不是缺陷，
    是「這條規則沒有可追蹤的對象」，呼叫端必須照實說，
    **不可以退回畫全站圖假裝有內容**。
    """
    src_cfg = settings()["data_sources"].get(source)
    if src_cfg is None:
        return None
    dims: list[Dim] = []
    for field in ("source_ip", "actor", "endpoint", "brand"):
        if field not in filters or filters[field] is None:
            continue
        meta = explorer.entity_meta(field, source)
        if meta is None:
            continue
        expr, mask, label = meta
        dims.append(Dim(field=field, expr=expr, value=str(filters[field]),
                        mask=mask, label=label))
    return EntityRef(source=source, table=src_cfg["table"], dims=tuple(dims)) if dims else None


def _grouped(ref: EntityRef) -> tuple[str, str]:
    """(分組運算式的 SELECT 片段, GROUP BY 片段)，維度與對象完全相同。

    母體必須與事件 metric **同單位**：R03 的 metric 是 per (src, endpoint)，
    用 per src 的分布去比，實測 P99 差 26 倍（見 checker/calibrate.py 的 6b）。
    """
    sel = ", ".join(f"{d.expr} AS d{i}" for i, d in enumerate(ref.dims))
    grp = ", ".join(f"d{i}" for i in range(len(ref.dims)))
    return sel, grp


def _nonempty(ref: EntityRef) -> str:
    """空值不算一個對象。規則 SQL 也是這樣做的（R03 的 `WHERE src != ''`）——
    少了它，`src = ''` 那一大坨會變成母體裡的第一名。"""
    return " AND ".join(f"{d.expr} <> ''" for d in ref.dims)


def peers(ref: EntityRef, start: datetime, end: datetime,
          limit: int = PEER_LIMIT, expected: float | None = None) -> dict:
    """同單位的母體：本對象的排名、分位數、前 N 名。

    區間長度必須等於規則的 `window_minutes`，否則排名與事件的 metric 不同單位。

    ## `expected` 是執行期的單位自我檢查，不是裝飾

    本函式數的是「該對象在此區間的**全部**記錄數」。對 R03／R04／R08／R10 而言
    那正好就是規則的 metric，但**有些規則的 metric 還帶額外條件** ——
    R07A 只算登入失敗、R09 只算錯誤回應、R05 只算敏感 route 且限非上班時間。
    那些規則的母體排名與 metric 不同單位。

    這裡刻意**不去重建各規則的 WHERE**（那等於把規則 SQL 抄第二份，遲早漂移，
    而漂移的症狀是一個看起來精確的錯數字）。改成把事件的 metric 傳進來對帳：
    對不上就把 `comparable` 設成 False 並說出實際數的是什麼。
    看的人因此知道要把它讀成「這個對象的總活動量在同儕中的位置」。
    """
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    tf = exprs.time_filter()
    sel, grp = _grouped(ref)
    inner = (f"SELECT {sel}, count() AS c FROM {ref.table}"
             f" WHERE {tf} AND {_nonempty(ref)} GROUP BY {grp}")

    own = int(query(f"SELECT count() AS c FROM {ref.table} WHERE {tf} AND {ref.where}",
                    {**params, **ref.params}).iloc[0]["c"] or 0)

    df = query(
        f"SELECT count() AS groups, quantile(0.5)(c) AS median,"
        f" quantile(0.95)(c) AS p95, quantile(0.99)(c) AS p99, max(c) AS maxv,"
        f" countIf(c > %(own)s) AS above FROM ({inner})",
        {**params, "own": own})
    s = df.iloc[0]

    df = query(f"SELECT {grp}, c FROM ({inner}) ORDER BY c DESC LIMIT {int(limit)}", params)
    top = []
    for _, r in df.iterrows():
        values = [str(r[f"d{i}"]) for i in range(len(ref.dims))]
        top.append({
            "label": " · ".join(
                _display(Dim(d.field, d.expr, v, d.mask, d.label))
                for d, v in zip(ref.dims, values)),
            "count": int(r["c"]),
            "is_self": values == [d.value for d in ref.dims],
        })

    comparable = expected is None or abs(own - float(expected)) < 1
    note = None if comparable else (
        f"這個排名數的是該對象在此區間的全部記錄（{own:,} 筆），"
        f"與事件指標（{float(expected):,.0f}）不同 —— 這條規則的指標另外帶了條件"
        f"（例如只算登入失敗、只算錯誤回應）。請把下面的排名讀成"
        f"「這個對象的總活動量在同儕中的位置」，不是「規則指標的排名」。")

    return {
        "window_start": params["start"], "window_end": params["end"],
        "own": own,
        "rank": int(s["above"]) + 1,
        "groups": int(s["groups"]),
        "median": float(s["median"]), "p95": float(s["p95"]),
        "p99": float(s["p99"]), "max": float(s["maxv"]),
        "top": top,
        "dims": [d.label for d in ref.dims],
        "comparable": comparable,
        "expected": float(expected) if expected is not None else None,
        "note": note,
    }


def hour_profile(ref: EntityRef, end: datetime, days: int = PROFILE_DAYS) -> dict:
    """24 小時作息：對象 vs 全站，各自佔**自身總量**的百分比。

    兩條線都是百分比，所以同一個 y 軸就夠 —— 雙軸是最容易誤導人的圖表做法。
    量級差 3 個數量級的兩個序列要放在一起比較的是**形狀**，不是高度。

    真人／商業流量有明顯的日夜波（實測全站 04:00 佔 0.69%、12:00 佔 9.49%，
    擺幅 13.7 倍）；常駐程式沒有（實測某整合每小時都是 4.29%，擺幅 1.25 倍）。
    """
    start = end - timedelta(days=days)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    tf = exprs.time_filter()

    def by_hour(where: str, extra: dict) -> dict[int, int]:
        df = query(f"SELECT toHour(create_time) AS hh, count() AS c FROM {ref.table}"
                   f" WHERE {tf}{where} GROUP BY hh", {**params, **extra})
        return {int(r["hh"]): int(r["c"]) for _, r in df.iterrows()}

    own = by_hour(f" AND {ref.where}", ref.params)
    site = by_hour("", {})
    own_total, site_total = sum(own.values()), sum(site.values())

    rows = [{
        "hour": h,
        "own": own.get(h, 0),
        "site": site.get(h, 0),
        # 小數（0..1），不是百分比。前端的 `pct()` 會乘 100 —— 這個專案的慣例
        # 是「比例一律用小數傳」，回百分比的話同一個值會被乘兩次
        # （實測 97.47 顯示成 9747.0%）。
        "own_share": round(own.get(h, 0) / own_total, 6) if own_total else None,
        "site_share": round(site.get(h, 0) / site_total, 6) if site_total else None,
    } for h in range(24)]

    return {
        "start": params["start"], "end": params["end"], "days": days,
        "rows": rows,
        "own_total": own_total, "site_total": site_total,
        "own": _flatness([r["own"] for r in rows]),
        "site": _flatness([r["site"] for r in rows]),
    }


def _flatness(counts: list[int]) -> dict:
    """作息的兩個純量：有活動的小時數，以及最忙／最閒的比值。

    有任何一小時是 0 就不給比值（那會是無限大）—— 改由 `active_hours`
    說話，而「只有 N 小時有活動」本身就是「像人」的證據。
    刻意不用一個數字硬蓋兩種形狀：把 0 當 1 來算會生出一個看起來精確的假數字。
    """
    total = sum(counts)
    active = sum(1 for c in counts if c > 0)
    if not total:
        return {"active_hours": 0, "ratio": None, "note": "此區間內沒有任何活動"}
    lo, hi = min(counts), max(counts)
    if lo <= 0:
        return {"active_hours": active, "ratio": None,
                "note": f"24 小時中有 {24 - active} 小時完全沒有活動"}
    return {"active_hours": active, "ratio": round(hi / lo, 2), "note": None}


def endpoint_share(ref: EntityRef, end: datetime, days: int = PROFILE_DAYS,
                   limit: int = SHARE_LIMIT) -> dict | None:
    """該 endpoint 的來源集中度。對象沒有 endpoint 維度時回 None（不適用）。

    回答的是「這個 endpoint 本來就這樣，還是被這個來源壟斷」。
    實測 `Api2/RecoverTrans` 近 7 天有 97.4% 來自單一 IP，第二名 0.9%。
    """
    ep = ref.dim("endpoint")
    if ep is None:
        return None
    src_expr = explorer.entity_expr("source_ip", ref.source)
    if src_expr is None:
        return None

    start = end - timedelta(days=days)
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end), "ep": ep.value}
    tf = exprs.time_filter()
    base = f"FROM {ref.table} WHERE {tf} AND {ep.expr} = %(ep)s"

    own_ip = ref.dim("source_ip")
    # 對象沒有來源維度時（R04 的 entity 只有 endpoint）「自己的佔比」不存在。
    # 那不是「查不到」—— 面板仍然有用（它回答「誰在打這個 endpoint」），
    # 但畫面不可以顯示一個空白的佔比讓人以為查詢失敗。
    shape = {"endpoint": ep.value, "start": params["start"], "end": params["end"],
             "days": days, "has_self": own_ip is not None,
             "self_note": None if own_ip is not None else
             "這個事件的對象只有 endpoint、沒有來源 IP，所以沒有「自己的佔比」；"
             "下面回答的是「這個 endpoint 由哪些來源在打」。"}

    total = int(query(f"SELECT count() AS c {base}", params).iloc[0]["c"] or 0)
    if not total:
        return {**shape, "total": 0, "rows": [], "own_share": None}

    _, mask, _ = explorer.entity_meta("source_ip", ref.source)
    df = query(f"SELECT {src_expr} AS s, count() AS c {base} AND {src_expr} <> ''"
               f" GROUP BY s ORDER BY c DESC LIMIT {int(limit)}", params)
    rows = [{
        "label": (masking.DISPLAY_FUNCS[mask](str(r["s"])) or str(r["s"]))
                 if mask else str(r["s"]),
        "count": int(r["c"]),
        "share": round(int(r["c"]) / total, 6),
        "is_self": own_ip is not None and str(r["s"]) == own_ip.value,
    } for _, r in df.iterrows()]

    own_share = next((r["share"] for r in rows if r["is_self"]), None)
    if own_share is None and own_ip is not None:
        # 本對象不在前 N 名時仍要給出它的佔比，否則畫面會顯示「—」
        # 而使用者無法分辨「沒進前六名」與「查不到」。
        c = int(query(f"SELECT count() AS c {base} AND {src_expr} = %(own)s",
                      {**params, "own": own_ip.value}).iloc[0]["c"] or 0)
        own_share = round(c / total, 6)

    return {**shape, "total": total, "rows": rows, "own_share": own_share}
