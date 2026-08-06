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

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from console.core import brands, masking, stores, timewin
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

# 「這個對象還可以往下拆成什麼」的候選維度，順序固定成
# 「打什麼 → 誰 → 影響誰」—— 同一條規則的事件每次讀起來都一樣。
BREAKDOWN_FIELDS = ("endpoint", "actor", "brand", "store")

# 每個拆解維度取幾名。6 是「一眼看出有沒有一個壓倒性的值」與
# 「四張小圖並排放得下」的折衷；真正的相異值個數由 `groups` 說出來。
BREAKDOWN_LIMIT = 6


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


# 需要把編號換成名稱的維度 → **批次版**查詢函式。
#
# 品牌與分店的 `mask` 是 None（它們是營運資訊、原樣顯示，不遮罩），所以
# `_display()` 原本直接回裸編號 —— 母體位置的橫條與表格因此是「1180 · 27681」，
# 沒有人認得出那是「wa10 瓦城 · WA10 APP」，而那一塊的用途正是讓人一眼看出
# 離群的是誰。事件標題早就解名稱了（`rules/engine._LABEL_FUNCS`），同一頁兩處
# 不一致本身就是缺陷。
#
# **一律用批次版**：母體位置一次 12 列 × 最多 2 個維度，逐列呼叫單值版就是 24 趟
# MySQL（同 `brands.breakdown()` 的取捨）。
_NAME_FUNCS = {"brand": brands.labels, "store": stores.labels}


def _names(field: str, values: Iterable[str]) -> dict[str, str]:
    """`{原始值: 顯示字串}`；不需要查名稱的維度回空 dict。

    鍵刻意是**原始字串**而不是 int —— 呼叫端手上的是 SQL 回來的
    `toString(_brand)`，用它直接查表才不會有第二次型別轉換的機會出錯。
    查不到名稱時 `labels()` 自己會回「（查無分店）（27681）」之類的字串，
    這裡不再另外編故事。
    """
    fn = _NAME_FUNCS.get(field)
    if fn is None:
        return {}
    raw = list(dict.fromkeys(values))
    resolved = fn(raw)
    return {v: resolved.get(brands.coerce_id(v), v) for v in raw}


def _display(dim: Dim) -> str:
    named = _names(dim.field, [dim.value])
    if named:
        return named.get(dim.value, dim.value)
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
    for field in ("source_ip", "actor", "endpoint", "brand", "store"):
        if field not in filters or filters[field] is None:
            continue
        meta = explorer.entity_meta(field, source)
        if meta is None:
            continue
        expr, mask, label = meta
        dims.append(Dim(field=field, expr=expr, value=str(filters[field]),
                        mask=mask, label=label))
    return EntityRef(source=source, table=src_cfg["table"], dims=tuple(dims)) if dims else None


def unresolved_reason(ref: EntityRef, start: datetime, end: datetime,
                      expected: float | None) -> str | None:
    """對象條件比對不到事件所宣稱的記錄時回一段說明；正常回 None。

    ## 為什麼需要這個執行期檢查

    本模組的模組說明假設「事件的 entity 值就是 `explorer.entity_expr()` 算出來的，
    所以拿它回來比對**一定命中**」。那個假設對規則 SQL 裡的**字面常數**不成立：
    R06 輸出 `'Boss_initial/auth_v2' AS endpoint`，而 admin 的 endpoint 母體鍵是
    `concat(function, '/', action)`（值有三段），完全相等比對永遠 0 筆。
    2026-08-05 由 EVT-0052 暴露，而它已經存在於三塊面板裡。

    **`own < expected` 是矛盾**：`own` 數的是該對象在母體單位下的**全部**記錄，
    而規則指標最多是它的子集（`peers()` 的 `comparable` 處理的正是
    `own > expected` 那一半 —— R07A 只算登入失敗、R09 只算錯誤回應）。
    所以這個檢查不必知道任何規則的細節，也不需要為 R06 寫特例。

    ## 為什麼擋在這裡，而不是改規則的 entity 值

    entity 值同時是**事件去重鍵**（`rules/engine` 的 `entity_key`）與 drilldown
    的篩選值。把 R06 的常數改成 `Boss_initial/auth_v2/login_success` 會讓進行中的
    事件換一個去重鍵、下一個 tick 開一筆新的 EVT；而 Explorer 的 `function` 篩選
    是前綴比對，`Boss_initial/auth_v2` 在那裡**查得到**，drilldown 本身沒有壞。
    真正壞掉的只有「用母體分組鍵做完全相等比對」這件事，所以擋在這裡。
    """
    if expected is None:
        return None
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end)}
    own = int(query(f"SELECT count() AS c FROM {ref.table}"
                    f" WHERE {exprs.time_filter()} AND {ref.where}",
                    {**params, **ref.params}).iloc[0]["c"] or 0)
    if own >= float(expected) - 1:
        return None
    return (
        f"這個事件的對象（{ref.label}）在 {ref.table} 上只比對到 {own:,} 筆，"
        f"而事件指標是 {float(expected):,.0f} —— 總數不可能小於它的子集，"
        f"所以是這條規則的對象值與母體的分組運算式不成對"
        f"（規則的 entity 是 SQL 裡的字面常數時會這樣）。"
        f"母體排名、24 小時作息與端點集中度都會是錯的，因此整塊不顯示 —— "
        f"這一頁寧可少一塊，也不要給三個看起來合理的錯答案。")


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
    rows = [([str(r[f"d{i}"]) for i in range(len(ref.dims))], int(r["c"]))
            for _, r in df.iterrows()]
    # 每個維度一次批次查名稱（品牌／分店），其餘維度拿到空 dict 並退回 `_display`
    name_maps = [_names(d.field, [values[i] for values, _ in rows])
                 for i, d in enumerate(ref.dims)]
    own_values = [d.value for d in ref.dims]
    top = []
    for values, count in rows:
        top.append({
            "label": " · ".join(
                name_maps[i].get(v)
                or _display(Dim(d.field, d.expr, v, d.mask, d.label))
                for i, (d, v) in enumerate(zip(ref.dims, values))),
            "count": count,
            # **比對原始值，不是標籤。** 店名會改，而且「（查無分店）」會讓多列
            # 長得一模一樣 —— 用標籤比對的話高亮會落在錯的長條上、或一次亮好幾條，
            # 而畫面看起來完全正常。
            "is_self": values == own_values,
            # 點這一列往下拆時要回送的**原始值**（順序同 `ref.dims`）。
            #
            # 品牌與分店的 `label` 是「名稱（編號）」，而這裡一律是裸編號 ——
            # 回送 label 的話後端拿「wa10 瓦城（1180）」去比對
            # `toString(_brand)` 永遠 0 筆，而畫面會顯示一個空的拆解面板，
            # 看起來像這個對象沒有活動。
            #
            # **不可回送時給 None，不是省略這個鍵**：前端要能分辨「這一列點不動」
            # 與「後端還是舊版」（後者整個 top 都沒有這個鍵，前端據此把整塊降級
            # 成唯讀，見 CLAUDE.md 關於「前端新、後端舊」的那一段）。
            "keys": list(values) if all(
                masking.echoable(d.mask, v)
                for d, v in zip(ref.dims, values)) else None,
        })

    comparable = expected is None or abs(own - float(expected)) < 1
    if comparable:
        note = None
    elif own < float(expected):
        # **方向很重要。** own 少於事件指標是矛盾（總數不可能小於子集），原因是
        # 對象值與母體分組運算式不成對，不是「規則指標另外帶了條件」——
        # 用下面那段話解釋這一半，等於給出一個看起來合理的錯誤診斷。
        # 正常路徑上 `unresolved_reason()` 已在端點層把整塊面板擋掉；
        # 這裡是給直接呼叫 `peers()` 的人的安全網。
        note = (
            f"對象條件只比對到 {own:,} 筆，而事件指標是 {float(expected):,.0f} —— "
            f"總數不可能小於它的子集，所以是對象值與母體的分組運算式不成對"
            f"（見 `unresolved_reason()`）。下面的排名與分位數都不可信。")
    else:
        note = (
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


def with_values(ref: EntityRef, values: Sequence[str]) -> EntityRef:
    """把 `ref` 的維度值換成 `values`，維度定義不變。

    母體排名可以點**任何一列**往下拆，不只本事件的對象 —— 實際調查時最有價值的
    往往是「排在我前面那幾名是誰」。那一列的原始值經 `masking.echoable()` 的
    閘門回送（見 `peers()` 的 `keys`），到這裡組成新的 ref。

    `EntityRef` / `Dim` 是 frozen dataclass，所以這裡必然產生**新物件** ——
    就地改掉會污染同一個請求裡其他面板共用的 ref（同 `rules/effective.py`
    用 `dataclasses.replace()` 的理由）。

    個數不符一律拋 `ValueError`：少一個維度就是在查一個**範圍更大**的對象，
    數字會比左欄那根長條大，而且不會有任何錯誤訊息。
    """
    if len(values) != len(ref.dims):
        raise ValueError(
            f"對象值的個數（{len(values)}）與維度數（{len(ref.dims)}）不符；"
            f"維度依序是 {[d.field for d in ref.dims]}")
    return dataclasses.replace(ref, dims=tuple(
        dataclasses.replace(d, value=str(v)) for d, v in zip(ref.dims, values)))


def breakdown_fields(ref: EntityRef) -> list[str]:
    """這個對象還可以往下拆的維度 —— 候選減掉「已經被拿去排序的」。

    對 (來源 IP × endpoint) 的對象再按 endpoint 拆只會得到一列，那不是資訊，
    而是一塊看起來壞掉的面板。

    「這張表有沒有這個分組運算式」問的是 `explorer.entity_meta()` ——
    **不是** `filter_support()`。後者管的是「使用者能不能用這個欄位反查」
    （auth 的 actor 是指紋，貼回去查不到），而這裡只是分組顯示，
    指紋當標籤是正確的呈現。
    """
    used = {d.field for d in ref.dims}
    return [f for f in BREAKDOWN_FIELDS
            if f not in used and explorer.entity_meta(f, ref.source) is not None]


def breakdown(ref: EntityRef, start: datetime, end: datetime,
              limit: int = BREAKDOWN_LIMIT) -> dict:
    """這個對象在此區間的活動，按每個「還沒被拿去排序的」維度分組的前 N 名。

    ## 與 `peers()` 是**不同的範圍**，兩者不可混讀

    `peers()` 問「我在母體的哪裡」（**不帶**對象條件的 GROUP BY）；
    這一支問「我自己打了哪些 endpoint／帳號／品牌／分店」（**帶**對象條件）。
    同一張卡的兩塊必須各自說出自己的範圍 —— 同一個數字兩種母體是這個專案
    一再出事的形狀（見 CLAUDE.md 的 `by_judgement`）。

    ## 區間必須與 `peers()` 相同

    呼叫端一律傳規則的 `window_minutes`，這樣左欄那根長條的長度就等於
    右邊各維度 `rows` 的總和 + `blank`。刻意**不吃自訂區間**就是為了維持
    這個對帳關係（趨勢那支才吃區間，見 `entity_history.recent_trend()`）。

    ## 查詢數是 1 + 維度數

    一支把 `count()` 與每個維度的 `uniqExact` / `countIf(= '')` 一次算完，
    剩下每個維度一支 top-N。欄位別名是程式產生的常數（`g0` / `b0`），
    運算式來自 `explorer.GROUP_BY`，沒有注入面。
    條件是「單一對象在 60 分鐘內」，非常選擇性。

    ## `blank` 一定要回

    前 N 名刻意**排除空值那一組**（標籤是空字串的長條沒有人讀得懂），
    所以佔比加不到 100%。不回 `blank` 的話「沒有帳號的那些筆」會靜靜藏在
    分母裡，而畫面看起來只是「剛好不到 100%」。

    品牌的 `_brand` 有兩個哨兵值（`-1` 是品牌層級操作、`0` 是未填），
    **照實列出**，不過濾 —— 過濾等於偷偷改分母。
    """
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end), **ref.params}
    base = f"FROM {ref.table} WHERE {exprs.time_filter()} AND {ref.where}"
    shape = {"window_start": params["start"], "window_end": params["end"]}

    fields = breakdown_fields(ref)
    if not fields:
        total = int(query(f"SELECT count() AS c {base}", params).iloc[0]["c"] or 0)
        return {**shape, "total": total, "dims": [], "note": (
            "這個事件的對象已經用掉全部可拆的維度"
            f"（{'、'.join(d.label for d in ref.dims)}），沒有可以再往下拆的欄位。")}

    metas = [(f, explorer.entity_meta(f, ref.source)) for f in fields]
    agg = ", ".join(
        f"uniqExact({expr}) AS g{i}, countIf({expr} = '') AS b{i}"
        for i, (_, (expr, _, _)) in enumerate(metas))
    s = query(f"SELECT count() AS total, {agg} {base}", params).iloc[0]
    total = int(s["total"] or 0)

    dims = []
    for i, (field, (expr, mask, label)) in enumerate(metas):
        df = query(f"SELECT {expr} AS d, count() AS c {base} AND {expr} <> ''"
                   f" GROUP BY d ORDER BY c DESC LIMIT {int(limit)}", params)
        pairs = [(str(r["d"]), int(r["c"])) for _, r in df.iterrows()]
        # 品牌與分店要一次批次查名稱（逐列呼叫單值版就是 6 趟 MySQL）
        names = _names(field, [v for v, _ in pairs])
        dims.append({
            "field": field,
            "label": label,
            "groups": int(s[f"g{i}"] or 0),
            "blank": int(s[f"b{i}"] or 0),
            "rows": [{
                # **不回原始值。** 這一層不再往下鑽所以不需要它，
                # 而 auth 的 actor 原始值是**還有效的憑證**。
                "label": names.get(v) or _display(Dim(field, expr, v, mask, label)),
                "count": c,
                # 小數（0..1），不是百分比 —— 前端的 pct() 會乘 100
                "share": round(c / total, 6) if total else None,
            } for v, c in pairs],
        })

    return {**shape, "total": total, "dims": dims, "note": None}


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
