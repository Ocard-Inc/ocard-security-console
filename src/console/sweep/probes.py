"""期間掃描的訊號探針表。

每支探針是「一段 SQL + 一個訊號分組」。探針之間刻意**不共用邏輯**：
掃描的價值來自多個彼此獨立的訊號交叉命中（見報告第三節的判讀方法），
所以每支只回答一個問題，交叉的事交給 correlate.py。

## signal_group 是評分的前提，不是分類標籤

`correlate.py` 以 **distinct signal_group 數**計票，同組多支命中只算一票。
沒有這個約束，一個爆量帳號會在「量級突變」「敏感路由總量」「路由集中度」
同時命中，分數虛高、排序就爛掉 —— 這三者衡量的是同一件事的不同切面。
新增探針時先問「它和現有哪支會一起亮？」，會一起亮的就歸同一組。

## 硬性約束

- `ods_backend_sys_log.ip` 是 `Nullable(String)`，`splitByChar` / `position` 直接吃它會拋
  `ILLEGAL_TYPE_OF_ARGUMENT`（實測）。**一律 `coalesce(ip, '')`**。
- 每支 SQL 必須帶 `create_time` 範圍 —— 四張表的 sorting key 不含時間、只有月分區。
- 時間邊界一律由 Python 端算好以完整字串傳參（見 core/timewin），絕不在 SQL 用 `now()`。
- 輸出欄位固定：`entity`（原始識別值，離開探針層前必經 masking）、`metric`（排序與
  severity 的依據），其餘欄位原樣進 evidence。

## 地板必須隨區間長度縮放，否則門檻會隨區間變鬆

多數探針的 metric 是**區間內的總量**（總請求數、總認證數）。同一個絕對地板在
3 天區間與 90 天區間的嚴格程度差 30 倍 —— 實測 15 天的正常區間就冒出 26 個命中，
其中絕大多數是「某商家某天做了批次匯出」這種無害流量。這不會報錯，只會讓
「拉長區間」變成「悄悄降低門檻」。

因此 `floor_kind` 區分兩種語意：

    absolute   metric 與區間長度無關 —— 單日峰值、相異帳號數、相異品牌數
    per_day    metric 隨區間線性成長 —— 總請求數、總認證數、總失敗數

`run.py` 對 per_day 探針把地板乘上區間天數，並以 `%(floor)s` 傳進 SQL。
所以**每支 SQL 的門檻一律寫 `%(floor)s`**，不寫字面值。

## 參數契約

`run.py` 一律傳齊五個參數，探針只取自己用得到的：

    start / end          使用者選的區間
    prev_start           start - baseline.window_days（P01/P03 的「區間之前」基線）
    seed_start           start - baseline.seed_days（P11 的首見判定回看範圍）
    floor                已依 floor_kind 縮放好的有效地板
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from console.core.config import settings
from console.intel import classify
from console.queries import exprs

ABSOLUTE = "absolute"
PER_DAY = "per_day"

# 私有／loopback 網段。ClickHouse 的 match() 是 RE2，反斜線在 Python 字串裡要跳脫兩層。
PRIVATE_RE = r"^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|127\\.|169\\.254\\.)"

# 訊號權重。取自報告第三節對各訊號強度的判讀，score.py 讀這份表。
# 「來源型態」最高 —— 報告的原話是「真人不會從資料中心登入後台」，這是最強的單一訊號。
SIGNAL_WEIGHTS = {
    "source_type": 3.0,          # 機房 / VPN 出口
    "source_trust": 2.5,         # 偽造 XFF、私有 IP —— 刻意規避來源檢查
    "credential_sharing": 2.5,   # 單一來源持有大量帳號憑證（報告 R3：最大系統性風險）
    "volume": 2.0,               # 相對自身基線的量級突變
    "brute_force": 2.0,          # 登入失敗集中
    "concentration": 1.5,        # 路由集中於資料導出型端點
    "auth_ratio": 1.5,           # 認證與請求比例失衡
    "new_source": 1.0,           # 區間內首見
    "off_hours": 1.0,            # 非上班時間集中
}

# 少數訊號單獨成立就足以列入事件清單，因為它們沒有無害的解釋 ——
# 值是「metric 至少要是地板的幾倍」才給這個豁免。
#
# source_trust：客戶端送出 `X-Forwarded-For: 127.0.0.1` 沒有正當用途。報告的判讀是
#   「偽造來源標頭的行為排除了良性解釋 —— 正當用途沒有隱藏來源的動機」。倍率 1.0
#   等於「命中就算」，因為這支的地板本身已經是「有沒有發生」的門檻。
#
# credential_sharing：一個 IP 持有上百個商家帳號的憑證，報告直接稱為
#   「本次分析中最大的系統性風險：任一主機遭入侵，即等同上百家商家的後台同時失守」。
#   意圖良性與否不影響風險，所以不需要第二個訊號佐證。倍率 3.0 是為了只放行
#   規模真的很大的（實測地板 10 個帳號 → 需 30 個以上），避免把「一家店兩個店員
#   共用一台電腦」也算成事件。已知的正當集中出口（辦公室、代操服務）走 allowlist。
# source_type：報告的原話是「真人不會從資料中心登入後台，因此這是本報告最強的
#   單一異常訊號」。一台境外機房持續登入商家後台，不需要第二個訊號佐證。
#   倍率 3.0 濾掉貼著地板的零星存取；判定為正當的自動化（如內部巡檢）走 allowlist，
#   那才是「已審核過」的正確表達方式，不是把門檻調高讓它消失。
SUFFICIENT_ALONE = {
    "source_type": 3.0,
    "source_trust": 1.0,
    "credential_sharing": 3.0,
}


@dataclass(frozen=True)
class Probe:
    id: str
    name: str
    summary: str              # 一句話說明在找什麼（同時給 UI 與 LLM 讀）
    source: str               # backend / admin / api / auth / mixed
    signal_group: str
    entity_kind: str          # actor / src
    fp_kind: str              # masking.DISPLAY_FUNCS 的鍵
    floor: float              # 地板基準值，同時是 severity 的分母
    floor_kind: str           # absolute | per_day（見模組說明）
    sql: str
    cost: str = "low"         # low = 秒內；high = 需使用者明確勾選
    needs_intel: bool = False  # 需要來源情報才有意義（空表時 run.py 自動跳過）
    # 逐列的後處理。回 None = 丟掉這一列；回 dict = 併進 evidence。
    #
    # 為什麼需要它：來源型態存在 SQLite（ip_intel）而探針跑在 ClickHouse，
    # 兩邊無法在 SQL 裡 join。所以「是不是機房」只能在拿到原始 IP 之後、
    # 轉成 fingerprint 之前，在 process 內判定。原始值不會因此外流 ——
    # run.py 一律只把 fingerprint 與這裡回傳的 dict 放進 Hit。
    row_filter: Callable[[object, dict], dict | None] | None = None


def _suspicious_source(raw_entity: object, _row: dict) -> dict | None:
    """P07 的逐列判定：只留下機房／VPN 來源，並把歸屬併進 evidence。"""
    c = classify.classify(None if raw_entity is None else str(raw_entity))
    if not c.suspicious:
        return None
    return {"source_type": c.source_type, "type_label": c.label,
            "org": c.org, "country": c.country}


# P08：機房來源至少要佔該帳號多少比例才算「並存」。
#
# 沒有這個下限的話，一個帳號只要曾有**一次**請求來自雲端位址就會命中 ——
# 實測 94 天區間有 25 個帳號如此，多數是零星的、與「憑證被複製到境外主機
# 長期運行」完全不同的情況。報告事件 4 的實際比例是 42,183 / 105,555 ≈ 40%。
_MIXED_MIN_HOSTING_SHARE = 0.05


def _mixed_source_types(_raw_entity: object, row: dict) -> dict | None:
    """P08 的逐列判定：這個帳號是否同時從「機房」與「非機房」大量存取。

    報告事件 4 的型態 —— 同一帳號同時存在「真人操作」與「機房自動化」兩種流量，
    代表憑證已被複製到一台境外主機上長期運行。單看任一邊都不異常，
    並存才是訊號，所以判定需要看該帳號整組來源的**流量分布**。

    `row['ips']` 是 SQL 給的 (原始 IP, 次數) 陣列，只在這個函式內存在；
    回傳的 evidence 只有次數與業者名，不含任何位址。
    """
    pairs = row.get("ips") or []
    hosting_req = other_req = 0
    hosting_srcs = 0
    orgs: list[str] = []
    for item in pairs:
        try:
            ip, n = str(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not ip:
            continue
        c = classify.classify(ip)
        if c.suspicious:
            hosting_req += n
            hosting_srcs += 1
            if c.org and c.org not in orgs:
                orgs.append(c.org)
        elif c.source_type in (classify.RESIDENTIAL, classify.OFFICE, classify.UNKNOWN):
            other_req += n
    total = hosting_req + other_req
    if not hosting_req or not other_req or not total:
        return None
    share = hosting_req / total
    if share < _MIXED_MIN_HOSTING_SHARE:
        return None
    return {"hosting_requests": hosting_req, "other_requests": other_req,
            "hosting_share": round(share, 4), "hosting_sources": hosting_srcs,
            "hosting_orgs": "、".join(orgs[:3])}


def _off_hours_expr() -> str:
    """非上班時間判定。時數來自 settings，內插的是程式內取得的整數。"""
    h = settings()["business_hours"]
    return f"(toHour(create_time) >= {int(h['end'])} OR toHour(create_time) < {int(h['start'])})"


@lru_cache(maxsize=1)
def probes() -> tuple[Probe, ...]:
    """探針表。lru_cache 讓 settings() 在首次呼叫時才讀（改 config 要重啟 server）。"""
    tf = exprs.time_filter()
    route2 = exprs.ROUTE2
    sensitive_in = exprs.in_list(exprs.sensitive_routes())
    off_hours = _off_hours_expr()

    return (
        Probe(
            id="P01",
            name="帳號自身量級突變",
            summary="帳號在區間內的單日峰值遠高於它自己在區間之前的日常水準",
            source="backend", signal_group="volume",
            entity_kind="actor", fp_kind="actor",
            # 單日峰值與區間長度無關 —— 一天爆量就是一天爆量。
            # 常態單帳號日量中位數 24 次（實測 28 天樣本），500 已是兩個數量級之上。
            floor=500, floor_kind=ABSOLUTE,
            sql="""
            SELECT entity, metric, total_in, days_in, median_prev, days_prev
            FROM (
              SELECT acc AS entity,
                     max(c_in) AS metric,
                     sum(c_in) AS total_in,
                     countIf(c_in > 0) AS days_in,
                     quantileIf(0.5)(c_prev, c_prev > 0) AS median_prev,
                     countIf(c_prev > 0) AS days_prev
              FROM (
                SELECT acc, toDate(create_time) AS d,
                       countIf(create_time >= %(start)s) AS c_in,
                       countIf(create_time < %(start)s) AS c_prev
                FROM ods_backend_sys_log
                WHERE create_time >= %(prev_start)s AND create_time < %(end)s
                  AND acc IS NOT NULL AND acc != ''
                GROUP BY acc, d
              )
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P02",
            name="集中存取資料導出路由",
            summary="請求高度集中在單一資料導出型路由，缺乏真人操作應有的頁面跳轉多樣性",
            source="backend", signal_group="concentration",
            entity_kind="actor", fp_kind="actor",
            floor=300, floor_kind=ABSOLUTE,
            # 三個判定條件都是實測校準的結果，改動前先看這裡：
            #
            # 集中度算在**峰值日**，不是整個區間：一段兩天的爆量塞進 93 天的區間裡，
            #   形狀會被 91 天的正常操作洗掉。實測 doremi000 在 7/22–7/24 有 93% 的
            #   請求打 customer/profile，但攤到 93 天的區間就低於門檻、整起事件消失。
            #   這與 P01/P03/P10 用單日峰值是同一個理由：嚴重程度不該是
            #   「分析師選了多長的區間」的函數。
            #
            # top_share >= 0.85（不是 uniq_routes <= N）：7/16 攻擊期間 andrew_c 有 9 個
            #   相異 route（orderlist/detail 佔 98%，其餘 8 個是 4~106 次的長尾），
            #   用 uniq_routes 當硬條件會把最重大的事件整個濾掉。uniq_routes 只當 evidence。
            #
            # top_route 必須是敏感路由：報告的訊號 3 原話是「集中於單一**資料導出型**路由」。
            #   少了這個限定，任何做批次作業的商家都會命中，而 concentration 是這裡
            #   唯一與量級獨立的訊號 —— 汙染它就等於讓交叉計票失去意義。
            sql=f"""
            SELECT entity, metric, peak_day, top_route, top_share, uniq_routes, peak_day_total
            FROM (
              SELECT entity,
                     max(day_top) AS metric,
                     argMax(toString(d), day_top) AS peak_day,
                     argMax(day_top_route, day_top) AS top_route,
                     argMax(day_top / day_total, day_top) AS top_share,
                     argMax(day_routes, day_top) AS uniq_routes,
                     -- 別名不可與內層欄位同名：`argMax(day_total, day_top) AS day_total`
                     -- 會讓 ClickHouse 判定為「聚合函式套在聚合函式裡」（code 184）。
                     argMax(day_total, day_top) AS peak_day_total
              FROM (
                SELECT entity, d, sum(n) AS day_total, max(n) AS day_top,
                       argMax(rr, n) AS day_top_route, uniqExact(rr) AS day_routes
                FROM (
                  SELECT acc AS entity, toDate(create_time) AS d,
                         {route2} AS rr, count() AS n
                  FROM ods_backend_sys_log
                  WHERE {tf} AND acc IS NOT NULL AND acc != ''
                  GROUP BY entity, d, rr
                )
                GROUP BY entity, d
              )
              GROUP BY entity
            )
            WHERE metric >= %(floor)s AND top_share >= 0.85
              AND top_route IN {sensitive_in}
            """,
        ),
        Probe(
            id="P03",
            name="敏感路由大量存取",
            summary="對訂單明細、客戶資料等資料導出型路由的存取量遠高於區間之前",
            source="backend", signal_group="volume",
            # **與 P01 同組，這是刻意的。** 兩者都在量測「某一天做了多少」，只差在
            # P03 先過濾敏感路由 —— 對一個本業就是查訂單的帳號，兩支必然一起亮，
            # 那不是兩份獨立證據。實測把它放進 concentration 組時，14 天的正常區間
            # 從 3 個命中暴增到 36 個，其中 90% 都是同一個 volume/concentration 組合。
            #
            # 真正與量級獨立的是**形狀**：P02 的 top_share >= 0.85 說的是「這個帳號
            # 幾乎只做這一件事」，一個做正常批次的商家量大但路由分散，不會命中。
            entity_kind="actor", fp_kind="actor",
            # metric 是**單日峰值**而非區間總量，所以與區間長度無關。
            # 用總量的話同一起事件在 3 天區間與 93 天區間會算出差 30 倍的規模係數，
            # 嚴重程度就變成「分析師選了多長的區間」的函數 —— 報告談的也是單日
            #（「單日 4.3 萬次客戶資料查詢」），不是區間總和。
            floor=300, floor_kind=ABSOLUTE,
            sql=f"""
            SELECT entity, metric, total_in, days_in, median_prev, days_prev
            FROM (
              SELECT acc AS entity,
                     max(c_in) AS metric,
                     sum(c_in) AS total_in,
                     countIf(c_in > 0) AS days_in,
                     quantileIf(0.5)(c_prev, c_prev > 0) AS median_prev,
                     countIf(c_prev > 0) AS days_prev
              FROM (
                SELECT acc, toDate(create_time) AS d,
                       countIf(create_time >= %(start)s) AS c_in,
                       countIf(create_time < %(start)s) AS c_prev
                FROM ods_backend_sys_log
                WHERE create_time >= %(prev_start)s AND create_time < %(end)s
                  AND acc IS NOT NULL AND acc != ''
                  AND {route2} IN {sensitive_in}
                GROUP BY acc, d
              )
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P04",
            name="單一來源持有多帳號憑證",
            summary="一個來源 IP 登入了數十至上百個不同帳號，代表憑證被集中保管",
            source="backend", signal_group="credential_sharing",
            entity_kind="src", fp_kind="src",
            # 相異帳號數不是速率：正常情境一個 IP 對應一至數個帳號，
            # 10 個在任何區間長度下都不是單一使用者的樣子。
            floor=10, floor_kind=ABSOLUTE,
            sql=f"""
            SELECT entity, metric, total, days, brands
            FROM (
              SELECT coalesce(ip, '') AS entity,
                     uniqExact(acc) AS metric,
                     count() AS total,
                     uniqExact(toDate(create_time)) AS days,
                     uniqExact(_brand) AS brands
              FROM ods_backend_sys_log
              WHERE {tf}
                AND acc IS NOT NULL AND acc != ''
                AND ip IS NOT NULL AND ip != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P05",
            name="單一來源跨大量品牌認證",
            summary="一個來源代表大量不同品牌取得 API token，是第三方整合中介或憑證集中的特徵",
            source="auth", signal_group="credential_sharing",
            entity_kind="src", fp_kind="src",
            floor=20, floor_kind=ABSOLUTE,
            sql=f"""
            SELECT entity, metric, auth_count, tokens, days
            FROM (
              SELECT coalesce(ip, '') AS entity,
                     uniqExact(_brand) AS metric,
                     count() AS auth_count,
                     uniqExact(token) AS tokens,
                     uniqExact(toDate(create_time)) AS days
              FROM ods_auth_log
              WHERE {tf} AND ip IS NOT NULL AND ip != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P06",
            name="認證與請求比例失衡",
            summary="認證次數與後台請求數嚴重不成比例 —— 憑證輪詢，或單次登入重用 session 大量取數",
            source="mixed", signal_group="auth_ratio",
            entity_kind="src", fp_kind="src",
            floor=200, floor_kind=PER_DAY,
            # `req > 0` 是必要條件：只出現在 auth_log、後台一次都沒登入的來源是純 API
            # 整合，比例談不上「失衡」。少了這個條件實測會多出 20+ 個 req=0 的假陽性。
            sql=f"""
            SELECT entity, metric, req, req_per_auth, accs
            FROM (
              SELECT entity, sum(auth_c) AS metric, sum(req_c) AS req,
                     sum(req_c) / greatest(sum(auth_c), 1) AS req_per_auth,
                     max(accs) AS accs
              FROM (
                SELECT coalesce(ip, '') AS entity, count() AS auth_c,
                       toUInt64(0) AS req_c, toUInt64(0) AS accs
                FROM ods_auth_log
                WHERE {tf} AND ip IS NOT NULL AND ip != ''
                GROUP BY entity
                UNION ALL
                SELECT coalesce(ip, '') AS entity, toUInt64(0) AS auth_c,
                       count() AS req_c, uniqExact(acc) AS accs
                FROM ods_backend_sys_log
                WHERE {tf} AND ip IS NOT NULL AND ip != ''
                GROUP BY entity
              )
              WHERE entity != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s AND req > 0
              AND (req_per_auth < 2 OR req_per_auth > 500)
            """,
        ),
        Probe(
            id="P07",
            name="來源為機房或 VPN 出口",
            summary="這個來源屬資料中心／雲端主機商或匿名代理 —— 真人不會從資料中心登入後台",
            source="backend", signal_group="source_type",
            entity_kind="src", fp_kind="src",
            # 地板刻意低且是 ABSOLUTE：這支的訊號是**分類本身**，不是量 ——
            # 「機房在登入商家後台」與區間長短無關。報告事件 6 的 Oracle 主機
            # 30 天只有 625 次（約 20 次／日）。
            #
            # 用 per_day 會重現同一個稀釋錯誤：報告事件 3 的 VPN 出口是 9 天內
            # 7,622 次的集中爆發，攤到 94 天的區間就低於門檻、整起事件消失。
            floor=200, floor_kind=ABSOLUTE,
            needs_intel=True,
            row_filter=_suspicious_source,
            sql=f"""
            SELECT entity, metric, accs, days, brands
            FROM (
              SELECT coalesce(ip, '') AS entity,
                     count() AS metric,
                     uniqExact(acc) AS accs,
                     uniqExact(toDate(create_time)) AS days,
                     uniqExact(_brand) AS brands
              FROM ods_backend_sys_log
              WHERE {tf} AND ip IS NOT NULL AND ip != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P08",
            name="同帳號機房與一般來源並存",
            summary="同一帳號同時有真人操作與機房自動化的流量 —— 憑證已被複製到境外主機",
            source="backend", signal_group="source_type",
            entity_kind="actor", fp_kind="actor",
            floor=200, floor_kind=PER_DAY,
            needs_intel=True,
            row_filter=_mixed_source_types,
            # groupUniqArray 上限 50 組 (IP, 次數)：判定需要流量分布，不只是有沒有並存
            #（見 _MIXED_MIN_HOSTING_SHARE）。這些原始 IP 只在 row_filter 內存在，
            # evidence 落盤的是次數與業者名，`ips` 本身在 run._DROP_COLUMNS 裡被丟掉。
            sql=f"""
            SELECT entity, metric, ips, source_count
            FROM (
              SELECT entity,
                     sum(n) AS metric,
                     groupUniqArray(50)((ip, n)) AS ips,
                     uniqExact(ip) AS source_count
              FROM (
                SELECT acc AS entity, coalesce(ip, '') AS ip, count() AS n
                FROM ods_backend_sys_log
                WHERE {tf} AND acc IS NOT NULL AND acc != ''
                  AND ip IS NOT NULL AND ip != ''
                GROUP BY entity, ip
              )
              GROUP BY entity
            )
            WHERE metric >= %(floor)s AND source_count >= 2
            """,
        ),
        Probe(
            id="P09",
            name="來源位址不可信",
            summary="日誌記到的是偽造的 loopback／私有位址，而非真實客戶端 —— 刻意規避來源檢查或取值邏輯有誤",
            source="backend", signal_group="source_trust",
            entity_kind="src", fp_kind="src",
            # 全期僅 9 個私有 IP、3 筆偽造 XFF，是低頻高價值訊號，地板刻意設低。
            #
            # 刻意**不含**單純的多段 XFF：全期 28,701 筆多數是 Zscaler 企業代理
            # （136.226.x／165.225.x），報告附錄 A 明確標為正常。未正規化的代理鏈是
            # 資料品質問題，屬「可信度限制」而非事件 —— 統計數字由 limits.py 呈現。
            # 真正的攻擊訊號是**首段為私有位址**（shape='forged'），那才留在這裡。
            #
            # 地板是 ABSOLUTE 而非 PER_DAY：這個訊號的語意是「有沒有發生」，不是速率。
            # 報告的原話是「全期 235 萬筆紀錄中僅 3 筆出現偽造的 127.0.0.1 標頭，
            # 此非隨機雜訊」。實測那筆偽造 XFF 只有 128 列，用 per_day 地板在 94 天的
            # 區間會變成要求 940 列 —— 最刻意的規避行為反而被區間長度稀釋掉。
            floor=30, floor_kind=ABSOLUTE,
            sql=f"""
            SELECT entity, metric, shape, accs, days
            FROM (
              SELECT coalesce(ip, '') AS entity,
                     count() AS metric,
                     multiIf(
                       position(coalesce(ip, ''), ',') > 0
                         AND match(trim(BOTH ' ' FROM splitByChar(',', coalesce(ip, ''))[1]),
                                   '{PRIVATE_RE}'), 'forged',
                       position(coalesce(ip, ''), ',') > 0, 'multi_hop',
                       match(coalesce(ip, ''), '{PRIVATE_RE}'), 'private',
                       'ok') AS shape,
                     uniqExact(acc) AS accs,
                     uniqExact(toDate(create_time)) AS days
              FROM ods_backend_sys_log
              WHERE {tf} AND ip IS NOT NULL AND ip != ''
              GROUP BY entity, shape
            )
            WHERE shape IN ('forged', 'private') AND metric >= %(floor)s
            """,
        ),
        Probe(
            id="P10",
            name="非上班時間集中操作",
            summary="請求集中在非上班時間，且量級不像值班的零星查詢",
            source="backend", signal_group="off_hours",
            entity_kind="actor", fp_kind="actor",
            # 同 P03：metric 是單日峰值，與區間長度無關。off_share 則是整個區間的比例
            # —— 那是「這個帳號的作息型態」，本來就該看整段。
            floor=200, floor_kind=ABSOLUTE,
            sql=f"""
            SELECT entity, metric, off_total, total, off_share, days_off
            FROM (
              SELECT entity,
                     max(off_c) AS metric,
                     sum(off_c) AS off_total,
                     sum(all_c) AS total,
                     sum(off_c) / sum(all_c) AS off_share,
                     countIf(off_c > 0) AS days_off
              FROM (
                SELECT acc AS entity, toDate(create_time) AS d,
                       countIf({off_hours}) AS off_c,
                       count() AS all_c
                FROM ods_backend_sys_log
                WHERE {tf} AND acc IS NOT NULL AND acc != ''
                GROUP BY entity, d
              )
              GROUP BY entity
            )
            WHERE metric >= %(floor)s AND off_share >= 0.5
            """,
        ),
        Probe(
            id="P11",
            name="區間內首見來源",
            summary="這個來源在區間之前的回看範圍內完全沒出現過，卻在區間內有可觀的量",
            source="backend", signal_group="new_source",
            entity_kind="src", fp_kind="src",
            floor=200, floor_kind=PER_DAY,
            sql="""
            SELECT entity, metric, days_in, accs
            FROM (
              SELECT ip AS entity,
                     sum(c_in) AS metric,
                     sum(c_prev) AS prev_count,
                     uniqExactIf(d, c_in > 0) AS days_in,
                     max(accs_in) AS accs
              FROM (
                SELECT coalesce(ip, '') AS ip, toDate(create_time) AS d,
                       countIf(create_time >= %(start)s) AS c_in,
                       countIf(create_time < %(start)s) AS c_prev,
                       uniqExactIf(acc, create_time >= %(start)s) AS accs_in
                FROM ods_backend_sys_log
                WHERE create_time >= %(seed_start)s AND create_time < %(end)s
                  AND ip IS NOT NULL AND ip != ''
                GROUP BY ip, d
              )
              GROUP BY entity
            )
            WHERE prev_count = 0 AND metric >= %(floor)s
            """,
        ),
        Probe(
            id="P12",
            name="登入失敗集中",
            summary="單一來源在區間內大量登入失敗，且嘗試多個不同帳號",
            source="admin", signal_group="brute_force",
            entity_kind="src", fp_kind="src",
            floor=20, floor_kind=PER_DAY,
            sql=f"""
            SELECT entity, metric, accs, days
            FROM (
              SELECT ip AS entity, count() AS metric,
                     uniqExact(acc) AS accs,
                     uniqExact(toDate(create_time)) AS days
              FROM ods_admin_log
              WHERE {tf} AND {exprs.ANY_LOGIN_FAILED} AND ip != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
        Probe(
            id="P13",
            name="API 來源跨大量品牌讀取",
            summary="一個 API 來源橫跨大量品牌讀取資料（來源由 forwarded header 推導，屬未驗證來源）",
            source="api", signal_group="credential_sharing",
            entity_kind="src", fp_kind="src",
            floor=20, floor_kind=ABSOLUTE,
            # ods_api_log 有 3.4 億列，解 headers JSON 實測 30 天 16.7 秒、90 天 29.3 秒
            # （其餘探針全部在 1 秒內）。必須由使用者明確勾選才跑。
            cost="high",
            sql=f"""
            SELECT entity, metric, total, endpoints
            FROM (
              SELECT src AS entity, uniqExact(_brand) AS metric,
                     count() AS total, uniqExact(endpoint) AS endpoints
              FROM (
                SELECT {exprs.API_SRC_IP} AS src, _brand, {exprs.ENDPOINT} AS endpoint
                FROM ods_api_log
                WHERE {tf}
              )
              WHERE src != ''
              GROUP BY entity
            )
            WHERE metric >= %(floor)s
            """,
        ),
    )


def by_id(probe_id: str) -> Probe | None:
    return next((p for p in probes() if p.id == probe_id), None)
