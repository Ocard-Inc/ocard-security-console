"""基線校準（每日檢查核心）＋ known_sources 播種。

從 ClickHouse 聚合近 28 天（排除污染窗與最近 3 天）各 metric 的時間桶分布，
以 (hour, day_class) 粒度寫入 SQLite baselines；known_sources 以 90 天
distinct 來源播種（僅存 fingerprint，不落原始 IP／帳號）。

CLI：uv run python -m console.checker.calibrate [--seed-known-sources]
"""
from __future__ import annotations

import argparse
import logging
from contextlib import contextmanager
from datetime import timedelta

from console.core import timewin
from console.core.ch import ChQueryError, query
from console.core.config import settings
from console.core.logging_setup import setup_logging
from console.core import masking
from console.queries import exprs
from console.rules import baseline
from console.store import db

logger = logging.getLogger(__name__)

_QUANTS = (
    "quantile(0.5)(c) AS median, quantile(0.95)(c) AS p95,"
    " quantile(0.99)(c) AS p99, max(c) AS maxv, count() AS samples"
)

# 總覽趨勢的分桶粒度。基線的語意是「**該粒度的桶內計數**的分布」，
# 所以前端用幾分鐘的桶，就必須拿同粒度的基線來比 —— 用 10 分鐘的基線去比
# 120 分鐘的桶，原始計數約是基線的 12 倍，會憑空生出假的「12 倍」告警。
#
# 必須與 queries/trends.py 的 BUCKET_LADDER 一致。n=10 產生的正是既有的
# table_10m:* 與 login_*_10m，所以 health.py、routes.py 的讀取端不受影響。
GRANULARITIES = (5, 10, 30, 120)


def _range(days: int) -> tuple[str, str]:
    """基線樣本範圍：now-3d 往前 days 天（避免把未確認異常吸進基線）。"""
    end = timewin.align_tick(timewin.taipei_now() - timedelta(days=3), 60)
    start = end - timedelta(days=days)
    return timewin.fmt(start), timewin.fmt(end)


def _bucketed_distribution(
    inner_select: str, params: dict, metric_key_expr: str = "''"
) -> list[tuple]:
    """通用：對每桶計數做 (metric_key, hour, day_class) 分位數聚合。"""
    sql = f"""
    SELECT {metric_key_expr} AS mk, toHour(b) AS hr,
           if(toDayOfWeek(b) >= 6, 'weekend', 'weekday') AS dc, {_QUANTS}
    FROM ({inner_select})
    GROUP BY mk, hr, dc
    """
    df = query(sql, params)
    return [
        (r["mk"], int(r["hr"]), r["dc"], float(r["median"]), float(r["p95"]),
         float(r["p99"]), float(r["maxv"]), int(r["samples"]))
        for _, r in df.iterrows()
    ]


def _global_distribution(inner_select: str, params: dict) -> tuple | None:
    """母體的整體分布。**沒有任何樣本時回 None，不回一列 NaN。**

    無 GROUP BY 的聚合永遠回一列，所以空母體拿到的是
    `(nan, nan, nan, 0.0, 0)` —— 而 NaN 進 SQLite 會存成 NULL，
    讀取端 `float(None)` 就是 TypeError（同 api/validate.py 記的那類壞資料）。
    `baseline.get()` 本來就把「查不到列」當成沒有基線並回退到 static_floor，
    所以不寫列才是正確的降級。
    """
    sql = f"SELECT {_QUANTS} FROM ({inner_select})"
    df = query(sql, params)
    if df.empty:
        return None
    r = df.iloc[0]
    if not int(r["samples"]):
        return None
    return (float(r["median"]), float(r["p95"]), float(r["p99"]),
            float(r["maxv"]), int(r["samples"]))


def _append_global(all_rows: list, skipped: list, mk: str,
                   inner_select: str, params: dict) -> None:
    """附加 (-1,'all') 保底列；母體為空就跳過並記名。

    **一張表沒有資料，不該讓其他表的基線也停止更新。** 2026-08-05 實測：
    `ods_api_log` 的歷史分區消失後，第 5 段的 `top_df["ep"]` 在空 DataFrame
    上拋 KeyError，而所有列是最後才一次性寫入 —— 於是**連 backend 的基線
    都沒寫進去**，一張表的問題變成全部基線停擺。
    跳過的段落會回傳給呼叫端（見 `calibrate()` 的 `skipped`），不是靜靜跳過。
    """
    dist = _global_distribution(inner_select, params)
    if dist is None:
        skipped.append(mk)
        logger.warning("基線 %s：母體在 %s ~ %s 內沒有任何樣本，跳過（不寫列）",
                       mk, params.get("start"), params.get("end"))
        return
    all_rows.append((mk, -1, "all", *dist))


@contextmanager
def _segment(skipped: list, name: str):
    """一段基線的邊界：SQL 失敗只記名並跳過，不拖垮其他段。

    **這是「空母體」那個修補的另一半。** `_append_global` 處理的是「查詢成功
    但沒有資料」；這裡處理「查詢直接失敗」。2026-08-05 兩種都真的發生了，
    而且是同一張表在同一天的兩個階段：`ods_api_log` 先被清空（空母體），
    回填後 `has_error` 變成 `Nullable(String)`（查詢拋 code 386）。

    為什麼一段失敗會全毀：11 段的結果累積在 `all_rows`，**最後才一次性
    upsert**。第 7 段炸掉時，前 6 段已經算好的一萬多列跟著被丟掉 ——
    包括與 api_log 完全無關的 backend / admin / auth 基線。
    症狀是「一張表的 schema 變了，全部表的基線一起停止更新」。

    **只吃 `ChQueryError`，不吃 `ChConnectionError`。** 連不上 ClickHouse 不是
    某一段的問題，而是整個監測中斷；把它吞掉會寫出一份幾乎空的基線並回報成功，
    那比大聲失敗糟得多。
    """
    try:
        yield
    except ChQueryError as exc:
        skipped.append(name)
        logger.warning("基線 %s：查詢失敗，跳過此段（沿用舊值）—— %s", name, exc)


def calibrate() -> dict:
    """重算全部基線，回傳統計摘要。"""
    cfg = settings()["baseline"]
    start, end = _range(cfg["window_days"])
    params = {"start": start, "end": end}
    tf, excl = exprs.time_filter(), exprs.exclusion_filter()
    now_str = timewin.fmt(timewin.taipei_now())
    all_rows: list[tuple] = []
    # 母體為空而被跳過的 metric key。**必須回報給呼叫端** ——
    # scheduler.run_daily() 會把它寫進 heartbeat 的 note，
    # 否則「基線少了幾段」與「基線正常」在畫面上長得一模一樣。
    skipped: list[str] = []

    # 1. 各表各粒度總量（overview 趨勢 / 資料健康；表的數量依 data_sources 設定，
    #    目前五張，含 Order Log）
    #    每個粒度都要算一份 —— 前端的分桶會隨時間區間變（見 trends.BUCKET_LADDER），
    #    拿錯粒度的基線去比就會產生假的倍數。
    for n in GRANULARITIES:
        for key, src in settings()["data_sources"].items():
            inner = (
                f"SELECT toStartOfInterval(create_time, INTERVAL {n} MINUTE) AS b,"
                f" count() AS c FROM {src['table']} WHERE {tf}{excl} GROUP BY b"
            )
            mk = f"table_{n}m:{key}"
            with _segment(skipped, mk):
                rows = _bucketed_distribution(inner, params, f"'{mk}'")
                all_rows.extend(rows)
                # (-1,'all') 保底列：baseline.get() 的回退鏈是
                # (hour,dc) → (hour,'all') → (-1,dc) → (-1,'all')，但上面只
                # 產生 (hour, weekday|weekend)。樣本一少就會出現中間破洞，
                # 把 median 參考線斷成好幾截；補這一列讓回退鏈不會走到底。
                _append_global(all_rows, skipped, mk, inner, params)
                logger.info("%s → %d 列", mk, len(rows))

    # 2. 登入成功 / 失敗（overview 線）
    for n in GRANULARITIES:
        for mk_base, cond in [
            ("login_success", exprs.ANY_LOGIN_SUCCESS),
            ("login_failed", exprs.ANY_LOGIN_FAILED),
        ]:
            mk = f"{mk_base}_{n}m"
            inner = (
                f"SELECT toStartOfInterval(create_time, INTERVAL {n} MINUTE) AS b,"
                f" count() AS c FROM ods_admin_log WHERE {tf}{excl} AND {cond} GROUP BY b"
            )
            with _segment(skipped, mk):
                all_rows.extend(_bucketed_distribution(inner, params, f"'{mk}'"))
                _append_global(all_rows, skipped, mk, inner, params)

    # 2b. R06 的 boss 登入成功。固定 10 分鐘、不進粒度迴圈 ——
    #     它是規則引擎的門檻依據（config/rules/r06_login_success_anomaly.yaml），
    #     不是顯示用的，粒度不可隨前端的分桶而變。
    inner = (
        f"SELECT toStartOfInterval(create_time, INTERVAL 10 MINUTE) AS b, count() AS c"
        f" FROM ods_admin_log WHERE {tf}{excl} AND {exprs.BOSS_LOGIN_SUCCESS} GROUP BY b"
    )
    with _segment(skipped, "boss_login_success_10m"):
        all_rows.extend(
            _bucketed_distribution(inner, params, "'boss_login_success_10m'"))

    # 3. backend 單帳號 10 分鐘請求分布（R01，全域）
    inner = (
        f"SELECT acc, toStartOfInterval(create_time, INTERVAL 10 MINUTE) AS b, count() AS c"
        f" FROM ods_backend_sys_log WHERE {tf}{excl} AND acc IS NOT NULL AND acc != ''"
        f" GROUP BY acc, b"
    )
    with _segment(skipped, "backend_acc_10m"):
        _append_global(all_rows, skipped, "backend_acc_10m", inner, params)

    # 3b. backend 單一來源 IP 的 60 分鐘請求分布（R15 的門檻依據，全域母體）
    #
    # **GROUP BY 與 WHERE 都必須與 R15 的 SQL 逐欄位相同**（只有 ip，且同樣帶
    # `ip IS NOT NULL AND ip != ''`）。CLAUDE.md 記著三種不成對的災難：
    # GROUP BY 粗一級讓門檻系統性偏高（R03 曾誤用 api_src_60m，實測 P99 差 26 倍）、
    # WHERE 少一個條件讓哨兵值拿一個不含自己的母體當門檻（R13 的 `_store > 0`）。
    # 這裡漏掉 `ip != ''` 的後果是所有「沒記到來源」的列併成一個巨大的桶，
    # p99 被拉高一個數量級，R15 的門檻跟著失效 —— 不報錯，只是不再告警。
    #
    # 與段 3 刻意分開而不是共用一趟查詢：粒度不同（10 分鐘 × 帳號 vs
    # 60 分鐘 × 來源），分布也不同（2026-08-05 實測 p99 分別是 104 與 240）。
    inner = (
        f"SELECT ip, toStartOfHour(create_time) AS b, count() AS c"
        f" FROM ods_backend_sys_log WHERE {tf}{excl}"
        f" AND ip IS NOT NULL AND ip != ''"
        f" GROUP BY ip, b"
    )
    with _segment(skipped, "backend_ip_60m"):
        _append_global(all_rows, skipped, "backend_ip_60m", inner, params)

    # 4. backend **全部** route 的 60 分鐘量（R14）
    #
    # 原本只算 `sensitive_routes()` 那六條（給已退休的 R02）。改成全路由是因為
    # 「敏感路由」是 7 月事後圈定的清單，它天生只涵蓋上次攻擊用過的路由 ——
    # 實測 7/16 那場攻擊實際打了 12 條 route，六條清單只看得到其中 3 條。
    #
    # 不設 top-N 上限（對比第 5 段的 api endpoint 取前 300）：backend 28 天只有
    # 587 個相異 route2，全算約 11,800 列，SQLite 與計算成本都不是問題。
    #
    # **這段的 GROUP BY 必須與 R14 的 SQL 逐欄位相同**（都是 route2、都不帶
    # 額外的 WHERE）。R14 沒有任何路由過濾，所以這裡也不可以有 —— 見 CLAUDE.md
    # 「基線與 metric 的對象粒度必須成對」與「成對的不只 GROUP BY，還有 WHERE」。
    inner = (
        f"SELECT {exprs.ROUTE2} AS r2, toStartOfHour(create_time) AS b, count() AS c"
        f" FROM ods_backend_sys_log WHERE {tf}{excl}"
        f" GROUP BY r2, b"
    )
    with _segment(skipped, "backend_route_60m"):
        all_rows.extend(
            _bucketed_distribution(inner, params, "concat('backend_route_60m:', r2)"))

    # 5. API endpoint 60 分鐘量（R04/R11 + 風險排名）：取 28 天量 top 300
    with _segment(skipped, "api_endpoint_60m"):
        top_df = query(
            f"SELECT {exprs.ENDPOINT} AS ep, count() AS cnt FROM ods_api_log"
            f" WHERE {tf}{excl} GROUP BY ep ORDER BY cnt DESC LIMIT 300",
            params,
        )
        # 空母體時 clickhouse-connect 回的是 (0, 0) 的 DataFrame —— **連欄位名都沒有**，
        # 所以 `top_df["ep"]` 是 KeyError 而不是空清單。2026-08-05 `ods_api_log` 的歷史
        # 分區消失時就是炸在這裡，而且因為基線最後才一次性寫入，backend 那幾段也跟著沒寫。
        top_eps = [str(e) for e in top_df["ep"]] if "ep" in top_df.columns else []
        if not top_eps:
            skipped.append("api_endpoint_60m")
            logger.warning("基線 api_endpoint_60m：%s ~ %s 內 ods_api_log 沒有任何列，跳過",
                           start, end)
        else:
            inner = (
                f"SELECT {exprs.ENDPOINT} AS ep, toStartOfHour(create_time) AS b,"
                f" count() AS c"
                f" FROM ods_api_log WHERE {tf}{excl} AND ep IN {exprs.in_list(top_eps)}"
                f" GROUP BY ep, b"
            )
            rows = _bucketed_distribution(inner, params, "concat('api_endpoint_60m:', ep)")
            all_rows.extend(rows)
            logger.info("api_endpoint_60m：%d endpoints → %d 列", len(top_eps), len(rows))

    # 6. API 單一來源 60 分鐘分布（全域；headers 解析成本高 → 取 7 天）。
    #    這是 **per (來源 IP)、跨全部 endpoint** 的分布，讀取端是
    #    trends.risk_rankings() 的「高流量來源」排名（那裡的 GROUP BY 也只有 src）。
    #    **不是 R03 的門檻依據** —— R03 的 metric 是 per (src, endpoint)，見 6b。
    s7, e7 = _range(7)
    inner = (
        f"SELECT src, toStartOfHour(create_time) AS b, count() AS c FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, create_time FROM ods_api_log WHERE {tf}{excl})"
        f" WHERE src != '' GROUP BY src, b"
    )
    with _segment(skipped, "api_src_60m"):
        _append_global(all_rows, skipped, "api_src_60m", inner,
                       {"start": s7, "end": e7})

    # 6b. API (來源 IP × endpoint) 60 分鐘分布（R03 的門檻依據）。
    #     **必須與 R03 的 metric 同單位。** R03 的 SQL 是 `GROUP BY src, endpoint`，
    #     一度誤用 6 的 `api_src_60m`（只 GROUP BY src），實測同一時段兩者的
    #     P99 差 26 倍（109 vs 2,835）—— 粗粒度的分布把同一個 IP 的全部 endpoint
    #     加總，值天生更大，於是門檻（p99 × 3）系統性偏高、規則長期漏抓，
    #     而事件頁「資料限制」顯示的 median/P95/P99 也在陳述錯的母體。
    #     這與 trends.BUCKET_LADDER 那條「分桶與基線粒度必須成對」是同一類錯誤，
    #     只是錯在**對象維度**而非時間維度。
    #
    #     **刻意只算全域一列，不做 (hour, day_class)。** 實測逐小時的結果是：
    #     凌晨 04:00 只有 518 個樣本、p99 = 6,060，而全域 443,391 個樣本的
    #     p99 = 168。原因是低流量時段活著的幾乎只有機器整合，它們撐高了自己的
    #     門檻 —— 逐小時會讓 04:00 的門檻變成 18,180（全域是 504），
    #     在最該敏感的時段把規則關掉。低流量端的保護交給 static_floor。
    inner = (
        f"SELECT src, ep, toStartOfHour(create_time) AS b, count() AS c FROM"
        f" (SELECT {exprs.API_SRC_IP} AS src, {exprs.ENDPOINT} AS ep, create_time"
        f"  FROM ods_api_log WHERE {tf}{excl}) WHERE src != '' GROUP BY src, ep, b"
    )
    with _segment(skipped, "api_src_ep_60m"):
        _append_global(all_rows, skipped, "api_src_ep_60m", inner,
                       {"start": s7, "end": e7})

    # 7. API error 5 分鐘分布（R09，全域）
    inner = (
        f"SELECT toStartOfFiveMinutes(create_time) AS b,"
        f" countIf({exprs.API_HAS_ERROR}) AS c"
        f" FROM ods_api_log WHERE {tf}{excl} GROUP BY b"
    )
    with _segment(skipped, "api_error_5m"):
        _append_global(all_rows, skipped, "api_error_5m", inner, params)

    # 8. 單品牌 15 分鐘分布（R10，全域）
    for mk, table in [("brand_backend_15m", "ods_backend_sys_log"),
                      ("brand_api_15m", "ods_api_log")]:
        inner = (
            f"SELECT _brand, toStartOfInterval(create_time, INTERVAL 15 MINUTE) AS b,"
            f" count() AS c FROM {table} WHERE {tf}{excl} GROUP BY _brand, b"
        )
        with _segment(skipped, mk):
            _append_global(all_rows, skipped, mk, inner, params)

    # 8b. API (品牌 × 分店) 60 分鐘分布。目前沒有規則讀它 —— 這是「長期持續的
    #     單店濫用」那條規則的門檻依據，先算出母體才有依據決定 factor 與 floor。
    #
    #     **為什麼不用既有的品牌層級（8 的 brand_api_15m）。** 品牌的母體撐不起低
    #     門檻：實測每小時計數在品牌層級是 median 12、p99 4,378、max 136,238，
    #     p99/median = 365 倍，因為同一個母體裡混著 621 個單店品牌與 8 個百店品牌。
    #     R10B 因此被迫用 static_floor 30000/15 分，而 2026-07-29 那天四個持續濫用
    #     的品牌只有 414 過得了那個地板（15 分峰值 34,616），2604／9016／8653 是
    #     10,408／6,358／4,419，全部漏抓 —— 且那四個品牌的濫用佔比是 99.5~100%，
    #     所以漏抓的原因是地板太高，不是被同品牌其他分店稀釋。同一份樣本改成
    #     (品牌 × 分店) 後 p99/median 降到 24 倍，門檻可以低兩個數量級。
    #
    #     **`_store > 0` 是必要的，不是清理。** `-1` 是品牌層級操作（7 月橫跨 301 個
    #     品牌、1,329,425 次）、`0` 是無分店（950 個品牌）。不濾掉的話這兩個哨兵值會
    #     把數百個品牌的伺服器端流量併成同一個對象，門檻失去意義，而事件的對象會是
    #     一個在 Explorer 查不到東西的「分店 -1」。**讀這個基線的規則 SQL 必須帶同一個
    #     條件**，母體與 metric 的對象粒度不成對就會憑空生出假倍數（同 6b）。
    #
    #     **刻意只算全域一列，理由同 6b 且更嚴重。** 實測平日逐小時：04:00 只有
    #     1,394 個樣本、p99 = 5,520（門檻會變 27,600），而全域 1,092,520 個樣本的
    #     p99 = 212（門檻 1,060）。撐高凌晨那幾格的正是長跑的濫用者本身
    #     （414/1018 平均 40,945、6710/23726 平均 25,670，它們 24 小時不停），
    #     逐小時等於讓濫用者自己決定自己的門檻，而且是在真人絕不可能操作的時段。
    #
    #     **刻意不加 endpoint 維度。** 實測 (品牌 × 分店 × endpoint) 的 max 與不含
    #     endpoint 完全相同（都是 42,136）—— 輪詢迴圈天生集中在單一 endpoint，多這
    #     一維不增加偵測力，卻讓去重鍵在對象換 endpoint 時斷開。「同一件事被拆成好
    #     幾筆」正是這條規則要解決的問題（R03 的對象是輪替 IP，實測同一家店 37 天
    #     產生 13~21 個 entity_key）。
    inner = (
        f"SELECT _brand, _store, toStartOfHour(create_time) AS b, count() AS c"
        f" FROM ods_api_log WHERE {tf}{excl} AND _store > 0 GROUP BY _brand, _store, b"
    )
    with _segment(skipped, "brand_store_60m"):
        _append_global(all_rows, skipped, "brand_store_60m", inner, params)

    n = baseline.upsert_many(all_rows, now_str)
    logger.info("基線寫入 %d 列（樣本 %s ~ %s）", n, start, end)
    if skipped:
        # 被跳過的段落**不會**清掉 baselines 裡的舊列（upsert 只覆蓋有算到的），
        # 所以那些 metric 會沿用上一次的值而 generated_at 停在舊時間。
        # 這是刻意的（舊基線比沒有基線好），但必須說出來。
        logger.warning("基線有 %d 段因母體為空而跳過，沿用舊值：%s",
                       len(skipped), "、".join(skipped))
    return {"rows": n, "start": start, "end": end, "generated_at": now_str,
            "skipped": skipped}


def seed_known_sources() -> dict:
    """known_sources 播種（僅存 fingerprint）。"""
    cfg = settings()["baseline"]
    tf, excl = exprs.time_filter(), exprs.exclusion_filter()
    now_str = timewin.fmt(timewin.taipei_now())
    counts = {}

    s90, e90 = _range(cfg["seed_days"])
    p90 = {"start": s90, "end": e90}
    df = query(
        f"SELECT DISTINCT acc, ip FROM ods_backend_sys_log WHERE {tf}{excl}"
        f" AND acc IS NOT NULL AND acc != '' AND ip IS NOT NULL AND ip != ''", p90)
    pairs = [(f"{masking.actor(r['acc'])}|{masking.src(r['ip'])}",) for _, r in df.iterrows()]
    counts["backend_acc_ip"] = _insert_known("backend_acc_ip", pairs, now_str)

    # admin_log 的登入事件（有 acc）無 ip、操作事件（有 ip）以 _admin 識別
    df = query(
        f"SELECT DISTINCT _admin, ip FROM ods_admin_log WHERE {tf}{excl}"
        f" AND _admin IS NOT NULL AND ip != ''", p90)
    pairs = [(f"{masking.actor(int(r['_admin']))}|{masking.src(r['ip'])}",) for _, r in df.iterrows()]
    counts["admin_admin_ip"] = _insert_known("admin_admin_ip", pairs, now_str)

    # API 來源：headers 解析成本高，取 28 天
    s28, e28 = _range(28)
    df = query(
        f"SELECT DISTINCT src FROM (SELECT {exprs.API_SRC_IP} AS src FROM ods_api_log"
        f" WHERE {tf}{excl}) WHERE src != ''",
        {"start": s28, "end": e28})
    pairs = [(str(masking.src(r["src"])),) for _, r in df.iterrows()]
    counts["api_src"] = _insert_known("api_src", pairs, now_str)

    logger.info("known_sources 播種：%s", counts)
    return counts


def _insert_known(kind: str, keys: list[tuple], now_str: str) -> int:
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO known_sources (kind, entity_key, first_seen, origin)"
            f" VALUES ('{kind}', ?, '{now_str}', 'seed')",
            keys,
        )
    return len(keys)


def main() -> None:
    setup_logging("calibrate.log")
    parser = argparse.ArgumentParser(description="基線校準")
    parser.add_argument("--seed-known-sources", action="store_true")
    args = parser.parse_args()
    result = calibrate()
    print(f"基線完成：{result['rows']} 列（{result['start']} ~ {result['end']}）")
    if args.seed_known_sources:
        counts = seed_known_sources()
        print(f"known_sources 播種：{counts}")


if __name__ == "__main__":
    main()
