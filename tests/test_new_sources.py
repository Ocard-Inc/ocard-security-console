"""2026-08-07 接入的五張表：可查（Explorer）+ 可看（健康卡 / sparkline）。

每張表一組測試。共用的 `assert_source_works()` 走一遍「使用者真的會做的事」——
比對照表的存在性檢查更嚴格：對照表齊全但運算式寫錯的話，
`tests/test_data_source_coverage.py` 會過而這裡會失敗。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.core.config import settings
from console.queries import explorer, source_schema


def _recent_window(days: int = 3) -> tuple[str, str]:
    """最近 N 天的台北牆鐘區間。新表的資料都是 2026-08-06 之後才有的。"""
    end = timewin.effective_now()
    return timewin.fmt(end - timedelta(days=days)), timewin.fmt(end)


def _explore(client, source: str, analysis: str, days: int = 3):
    """`/api/explorer` 是 **POST**（不是 GET）。"""
    start, end = _recent_window(days)
    return client.post("/api/explorer", json={
        "source": source, "start": start, "end": end, "analysis": analysis})


def assert_source_works(client, source: str, *, expect_analyses: set[str]) -> None:
    """一個來源「接好了」的完整定義。Task 5–9 共用。"""
    # ① 綱要存在且表名與 settings 一致
    schema = source_schema.get(source)
    assert schema.table == settings()["data_sources"][source]["table"]

    # ② Explorer 的能力清單有這個來源，且宣告的分析方式與預期相同
    meta = {m["key"]: m for m in explorer.source_meta()}
    assert source in meta, f"explorer.source_meta() 沒有 {source}"
    assert set(meta[source]["analyses"]) == expect_analyses, (
        f"{source} 宣告的分析方式與預期不同："
        f"{sorted(meta[source]['analyses'])} != {sorted(expect_analyses)}")

    # ③ 宣告支援的分析方式**真的跑得起來**。宣告了卻 400/502 是最糟的形狀：
    #    畫面上是個正常選項，點下去壞掉。
    for analysis in meta[source]["analyses"]:
        r = _explore(client, source, analysis)
        assert r.status_code == 200, (
            f"{source}/{analysis} → {r.status_code} {r.text[:300]}")

    # ④ 健康卡有這一張，而且不是「查詢失敗」
    cards = {c["key"]: c for c in client.get("/api/health").json()["sources"]}
    assert source in cards, f"/api/health 沒有 {source}"
    assert cards[source]["status"] != "查詢失敗", (
        f"{source} 的健康卡查詢失敗：{cards[source].get('error')}")
    assert cards[source]["note"], f"{source} 的健康卡沒有資料限制說明"

    # ⑤ sparkline 有這一條
    assert source in client.get("/api/sparklines").json()["sources"]

    # ⑥ 不支援的篩選必須說出原因，不可以是空字串或一句「不支援」
    for field, reason in meta[source]["unsupported_filters"].items():
        assert reason and len(reason) > 10, (
            f"{source} 的 {field} 不支援，但原因寫得太短：{reason!r}")

    # ⑦ **明細的時間必須落在查詢區間內（台北牆鐘）。**
    #
    # 實測踩到過：console 的 `_DETAIL_COLUMNS` 寫成
    # `recordedAt AS create_time`，別名的是**原始 UTC 欄位**而不是台北運算式，
    # 於是每一列都早 8 小時 —— 而排名、趨勢、健康卡全部正常，只有明細的時間
    # 「怪怪的」。上面 ①–⑥ 一則都抓不到。
    start, end = _recent_window()
    rows = explorer.detail(
        explorer.ExplorerFilter(source=source, start=start, end=end))["rows"]
    if rows:
        times = [r["time"] for r in rows[:20]]
        assert all(start <= t <= end for t in times), (
            f"{source} 的明細時間落在查詢區間 [{start}, {end}] 之外："
            f"{[t for t in times if not (start <= t <= end)][:3]} —— "
            "多半是 _DETAIL_COLUMNS 別名了原始的 UTC 欄位而不是台北運算式")


# ── batch（ods_batch_request_log）─────────────────────────────────────────────

def test_batch_source_works(client):
    """批次匯入排程（im.ocard.co）。

    ip 實測 100% 是 0.0.0.0、input 100% 空，所以沒有來源與操作者維度。
    """
    assert_source_works(client, "batch",
                        expect_analyses={"trend", "endpoint", "detail"})


def test_batch_has_no_source_ip_dimension():
    """反向：`ip` 欄位存在但恆為 0.0.0.0，不可以假裝它是來源。

    有人「順手補齊」的話，來源排名會出現一個佔 100% 的 0.0.0.0，
    而那會被讀成「所有請求都來自同一個 IP」—— 完全錯誤的結論。
    """
    assert "batch" not in explorer.GROUP_BY["source"]
    reason = explorer.filter_support("source_ip", "batch")
    assert reason and "0.0.0.0" in reason, (
        "拒絕的理由必須說出「ip 欄位有值但恆為 0.0.0.0」，"
        f"否則下一個人會以為只是漏掉了：{reason!r}")


@pytest.mark.parametrize("source", tuple(settings()["data_sources"]))
def test_every_registered_source_has_a_detail_branch(source):
    """`_mask_detail_row()` 的每個**已註冊**來源都要有自己的分支。

    原本最後一支是 catch-all `else:  # auth`。已註冊但沒有分支的來源會靜靜掉
    進去：endpoint 取 `action`、操作者取 `token`，新表兩個欄位都沒有，於是整張
    明細變成一列列的 None —— 而畫面看起來只是「這些欄位剛好是空的」。

    未註冊的來源本來就會在 `settings()[...]["label"]` 那裡 KeyError，
    所以真正需要守的是**已註冊**這一側。
    """
    # 假的一列，欄位**從 `_DETAIL_COLUMNS` 解析**（那正是這個分支實際會收到的
    # 欄位集合）。不能用 defaultdict —— `_mask_detail_row` 開頭的 dict
    # comprehension 會用 `.items()` 重建成普通 dict，空的 defaultdict 會變成空 dict。
    # `X AS create_time` 這種別名取最後一段。
    cols = [c.strip().split()[-1]
            for c in explorer._DETAIL_COLUMNS[source].split(",")]
    row = explorer._mask_detail_row(source, {c: None for c in cols})
    assert set(row) >= {"row_id", "time", "source", "endpoint", "actor",
                        "source_ip", "result", "params", "resource"}, (
        f"{source} 的明細列缺欄位，可能沒有自己的分支：{sorted(row)}")


def test_unknown_source_fails_loudly_in_detail_rows():
    """未註冊的來源必須大聲失敗，不可以靜靜渲染成別的表的形狀。"""
    with pytest.raises((KeyError, ValueError)):
        explorer._mask_detail_row("nonexistent_source", {"create_time": None})


# ── console（ods_console_backend_sys_log）─────────────────────────────────────

def test_console_source_works(client):
    """api-console.ocard.co（PHP 後台 API）的請求紀錄。"""
    assert_source_works(
        client, "console",
        expect_analyses={"trend", "endpoint", "source", "actor", "detail"})


def test_console_source_ip_never_falls_back_to_the_load_balancer():
    """`xForwardedForRaw` 空的時候就是空，不可以退回 `requester.ipAddress`。

    實測 53% 的列沒有 xForwardedFor，而那些列的 ipAddress 全部是
    10.100.0.173（我方 LB）、全部是 Welcome/index 健康檢查。coalesce 進來的話
    它會穩居每一份來源排名第一名 —— 而它不是任何「來源」。
    「查不到」不可以偷換成一個看起來合理的值。
    """
    expr = explorer.GROUP_BY["source"]["console"][0]
    assert "ipAddress" not in expr, (
        "來源 IP 運算式不可以引用 requester.ipAddress —— "
        f"那是我方 LB，53% 的列會變成它：{expr}")
    assert "xForwardedForRaw" in expr


def test_console_actor_falls_back_to_login_body(client):
    """`authentication.account` 目前全空，登入帳號只在 `body.account` 裡。

    少了 fallback 的話，這張表最有價值的那件事（誰在登入後台）完全看不到，
    而畫面上是一個 100% 都是「（空）」的操作者排名。
    """
    expr = explorer.GROUP_BY["actor"]["console"][0]
    assert "body" in expr and "account" in expr, (
        f"actor 運算式必須帶 body.account 的 fallback：{expr}")

    r = _explore(client, "console", "actor")
    assert r.status_code == 200, r.text
    names = [row["name"] for row in r.json()["rows"]]
    assert any(n and n != "（空）" for n in names), (
        f"操作者排名全部是空的 —— body.account 的 fallback 沒有生效：{names[:5]}")


def test_console_has_no_brand_dimension():
    """`authentication.brandIdx` 實測 100% null，不可以做成一個永遠空白的維度。

    拒絕理由要與「這張表沒有這個欄位」分開講：前者永遠不會有，
    後者是上游可以修好的 —— 只說「不支援」會讓人去等一個不會來的功能，
    或反過來以為資料結構天生如此而不去追上游。
    """
    assert "console" not in explorer.GROUP_BY["brand"]
    reason = explorer.filter_support("brand", "console")
    assert reason and "brandIdx" in reason, (
        f"拒絕理由必須說出是 brandIdx 沒有被寫入：{reason!r}")


# ── request（ods_request_log）────────────────────────────────────────────────

def test_request_source_works(client):
    """報表下載服務（dlc.ocard.co）。五張裡與資料外流最直接相關的一張。"""
    assert_source_works(client, "request",
                        expect_analyses={"trend", "endpoint", "source", "detail"})


def test_request_created_at_is_taipei_not_utc():
    """`created_at` 在這張表是**台北牆鐘**，與 voucher/ec 的同名欄位語意相反。

    猜錯的症狀是整條時間軸平移 8 小時、不報錯。實測依據：
    `created_at = 2026-08-07 01:30:12` 那列的 `response_headers.date`
    是 `Thu, 06 Aug 2026 17:30:12 GMT`。
    """
    schema = source_schema.get("request")
    assert schema.time_tz is None, (
        "ods_request_log.created_at 是台北牆鐘，不可以再做時區轉換 —— "
        "轉了會讓整條時間軸平移 8 小時")
    assert schema.time_expr == "created_at"


def test_request_dedups_the_in_flight_row(client):
    """同一個 `idx` 有 in-flight 與完成兩列，計數與狀態分析都必須去重。

    請求開始時先寫一列（`status_code = 0`、`response_*` 空），完成後再寫一列，
    兩列 `created_at` 相同、靠 `updated_at` 區分。不處理的話
    `GROUP BY status_code` 會生出一格幽靈的 0（「有一筆請求的狀態碼是 0」）。
    """
    schema = source_schema.get("request")
    assert schema.dedup_col == "idx", "ods_request_log 的去重鍵是 idx 不是 _id"
    assert schema.dedup_order == "updated_at", (
        "同一個 idx 的多個版本要靠 updated_at 挑最新的")


def test_request_detail_keeps_the_full_uri(client):
    """排名把路由收斂成 `api/reports`，但明細必須看得到是**哪一份**報表。

    「誰下載了哪一份報表」是 ods_request_log 存在的理由 ——
    明細也收斂的話這張表就只剩「有人下載了東西」，等於沒有接。
    """
    start, end = _recent_window()
    rows = explorer.detail(
        explorer.ExplorerFilter(source="request", start=start, end=end))["rows"]
    if not rows:
        pytest.skip("ods_request_log 在最近 3 天沒有資料")
    uris = [r["endpoint"] for r in rows]
    assert any(u.count("/") >= 3 or "?" in u for u in uris), (
        f"明細的 uri 全部被收斂了 —— 看不出是哪一份報表：{uris[:5]}")


def test_request_has_no_actor_dimension():
    """沒有帳號欄位；身分只在 headers.authorization 的憑證裡，不可反查。"""
    assert "request" not in explorer.GROUP_BY["actor"]
    reason = explorer.filter_support("actor", "request")
    assert reason and len(reason) > 10


# ── R12 新鮮度：逐來源門檻 ────────────────────────────────────────────────────

def test_freshness_uses_the_per_source_threshold():
    """R12 的門檻可以逐來源覆寫，而且覆寫值真的被讀到。

    R12 量的是「距離最後一筆多久」，那個值同時混著「管線延遲」與「這段時間
    本來就沒有流量」。高流量的表兩者分得開（實測 batch 的 15,622 個間隔裡只有
    1 個超過 20 分鐘），低流量的表分不開 —— 實測 2 天內 ods_request_log 有
    11 個間隔超過 20 分（最長 152 分）、ods_ec_request_log 有 12 個（最長 752 分）。

    用同一個 20 分鐘門檻，那兩張表每天會發好幾則假的 P2「資料管線失速」，
    而把值班的人訓練成忽略告警等於把這個控制拆掉。

    **這則測試守的是「覆寫真的生效」** —— 寫在 settings 裡卻沒被讀到的話，
    症狀是假告警照發而設定看起來已經改好了。
    """
    from console.core.config import settings

    default = settings()["freshness"]["alert_minutes"]
    overridden = {k: int(v["freshness_alert_minutes"])
                  for k, v in settings()["data_sources"].items()
                  if v.get("freshness_alert_minutes")}
    assert overridden, "至少 request 應該有覆寫（它 20 筆/小時，一定會誤報）"
    for key, val in overridden.items():
        assert val > default, (
            f"{key} 的覆寫值 {val} 不大於預設 {default} —— "
            "覆寫的用途是放寬低流量表的門檻，設得更嚴沒有意義")


def test_freshness_query_works_for_every_source():
    """R12 的查詢對**每一個**來源都跑得起來。

    `_eval_freshness` 原本寫死 `SELECT max(create_time)`，而 console 用
    `recordedAt`、request 用 `created_at`。寫死的話會在第一張不存在該欄位的表
    拋 Unknown identifier —— 而那個迴圈在 `evaluate()` 的 per-rule try 之內，
    例外一拋 **R12 對所有表一起停止運作**，畫面上規則卻仍顯示啟用中。

    排程器在測試裡不啟動（TestClient 沒有進 context manager），所以這個 bug
    不會被任何既有測試抓到 —— 這則測試就是那個缺口的補丁。
    """
    from datetime import timedelta

    from console.core.ch import query
    from console.core.config import settings
    from console.queries import exprs

    now = timewin.taipei_now()
    params = {"start": timewin.fmt(now - timedelta(hours=2)),
              "end": timewin.fmt(now)}
    for key, src in settings()["data_sources"].items():
        schema = source_schema.get(key)
        df = query(
            f"SELECT max({schema.time_expr}) AS mx FROM {src['table']}"
            f" WHERE {exprs.time_filter_for(key)}", params)
        assert "mx" in df.columns, f"{key} 的新鮮度查詢沒有回 mx"
