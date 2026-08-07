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


# ── voucher（ods_voucher_request_log）─────────────────────────────────────────

def test_voucher_source_works(client):
    """voucher.ocard.co 的票券／兌換 API 請求紀錄。五張裡唯一夠大的一張。"""
    assert_source_works(client, "voucher",
                        expect_analyses={"trend", "endpoint", "actor", "detail"})


def test_voucher_has_no_source_ip():
    """header 只有 host / content-* / x-ocard-channel-* —— 全部是伺服器對伺服器。

    這與 order 是同一類結構性限制，理由要說出是**資料本身沒有**，
    不是「我們還沒做」—— 後者會讓人去等一個永遠不會來的功能。
    """
    assert "voucher" not in explorer.GROUP_BY["source"]
    reason = explorer.filter_support("source_ip", "voucher")
    assert reason and "伺服器對伺服器" in reason, (
        f"拒絕理由要說出是全部伺服器對伺服器呼叫：{reason!r}")


def test_voucher_actor_unwraps_the_array_header(client):
    """header 的值是 JSON **陣列**（`["ocard-api_prod"]`）。

    直接 `JSONExtractString` 對陣列會回空字串 → 一個全空的操作者排名；
    當成字串用則會得到 `['ocard-api_prod']` 這種帶括號的值 ——
    貼回篩選器永遠不會命中，而畫面上看起來只是「格式怪怪的」。
    """
    r = _explore(client, "voucher", "actor")
    assert r.status_code == 200, r.text
    names = [row["name"] for row in r.json()["rows"]]
    assert any(n and n != "（空）" for n in names), (
        f"呼叫通道排名全部是空的 —— 陣列沒有解開：{names[:5]}")
    assert not any(n.startswith(("[", "'", '"')) for n in names if n), (
        f"值帶著陣列或引號的括號，貼回篩選器不會命中：{names[:5]}")


def test_voucher_channel_secret_never_reaches_the_response(client):
    """`x-ocard-channel-secret` 是還有效的憑證，明細不可以吐出它。

    實測值形如 `AHtCAkV+2+tMij97yAB9Fw==` —— 顯示等於任何有主控台讀取權的人
    都能冒用該通道呼叫 API。
    """
    r = _explore(client, "voucher", "detail")
    assert r.status_code == 200, r.text
    assert "AHtCAkV" not in r.text, "channel secret 的值原樣外流"


# ── ec（ods_ec_request_log）───────────────────────────────────────────────────

def test_ec_source_works(client):
    """api-ec.ocard.co 的購物請求紀錄。五張裡唯一有真實消費者 IP 與品牌的一張。"""
    assert_source_works(
        client, "ec",
        expect_analyses={"trend", "endpoint", "brand", "source", "actor", "detail"})


def test_ec_endpoint_is_derived_from_url_not_function():
    """`request.function` 對 ec 是 cart id（`2Rb7xl`）。

    拿它當 endpoint 會生出上萬個一次性選項，而且完全看不出是什麼操作。
    """
    expr = explorer.GROUP_BY["endpoint"]["ec"][0]
    assert "'function'" not in expr, (
        f"ec 的 endpoint 不可以用 request.function（那是 cart id）：{expr}")
    assert "url" in expr


def test_ec_brand_uses_the_upstream_typo():
    """上游的鍵就叫 `ouput`（不是 `output`）。

    改成正確拼字會靜靜回 0 筆 —— 品牌維度整個變空，而畫面完全正常。
    """
    expr = explorer.GROUP_BY["brand"]["ec"][0]
    assert "'ouput'" in expr, (
        f"上游的鍵是 ouput（拼錯但那是事實），寫成 output 會永遠取不到值：{expr}")


def test_ec_bearer_token_never_reaches_the_response(client):
    """JWT 在 `request.header.authorization` 與 `response.authUser.bearer` 各一份。

    只清 header 會漏掉第二份，而症狀是「看起來清乾淨了」。
    """
    r = _explore(client, "ec", "detail", days=30)
    assert r.status_code == 200, r.text
    assert "eyJ0eXAiOiJKV1Qi" not in r.text, (
        'JWT 原樣外流（eyJ0eXAiOiJKV1Qi 是 {"typ":"JWT" 的 base64 前綴）')


# ── 五個來源一起過的跨來源守門 ────────────────────────────────────────────────

NEW_SOURCES = ("batch", "console", "request", "voucher", "ec")


@pytest.mark.parametrize("source", NEW_SOURCES)
def test_new_sources_detail_never_ships_raw_payload(client, source):
    """五張新表的內容幾乎全是 payload 欄位，逐筆明細只能給 `payload_summary()`。

    要看原文有專門的路徑：`POST /api/explorer/payload`，一次一筆並寫入
    `audit_log`。明細直接吐原文的話，那條留痕路徑等於被繞過。
    """
    start, end = _recent_window(days=30)
    rows = explorer.detail(
        explorer.ExplorerFilter(source=source, start=start, end=end))["rows"]
    if not rows:
        pytest.skip(f"{source} 在最近 30 天沒有資料")

    payload_fields = {"request", "response", "body", "response_headers",
                      "response_body", "input", "header", "headers",
                      "requester", "authentication", "params"}
    for row in rows[:20]:
        for key, value in row.items():
            if key not in payload_fields or not isinstance(value, str):
                continue
            # payload_summary() 給的是「N bytes · 欄位：…」，不是可解析的原文
            assert not value.lstrip().startswith(("{", "[")), (
                f"{source} 的明細把 {key} 原樣吐出來了（開頭是 JSON）："
                f"{value[:120]}")


@pytest.mark.parametrize("source", NEW_SOURCES)
def test_new_sources_pass_the_masking_audit(client, source):
    """把五個新來源納入既有的遮罩稽核：不該外流的沒外流。

    `_scan()` 檢查手機、消費者 Email、未清洗的憑證值三種樣式。
    """
    from tests.test_masking_audit import _scan

    for analysis in ("detail", "endpoint", "trend"):
        r = _explore(client, source, analysis, days=30)
        assert r.status_code == 200, f"{source}/{analysis} → {r.status_code}"
        _scan(r.text, f"POST /api/explorer source={source} analysis={analysis}")


def test_brand_zero_is_no_brand_not_lookup_failure():
    """`_brand = 0` 是「沒有品牌」，不是「查無品牌」。

    兩者在畫面上差很多：「查無」讀起來像「有一個品牌編號，但我們查不到它的
    名字」（資料問題，值得追），而 0 是「這個請求本來就與品牌無關」（正常）。
    MySQL 從來沒有編號 0 的品牌，所以說「查無」是錯的 ——
    同 explorer.NON_ADMIN_ACCOUNT 對 `_admin = 0` 的判斷。

    2026-08-07 接 ec 時現形：非購物車類請求的品牌是 0，實測 7 天 2,495 筆
    （最大宗），品牌排名第一列顯示「（查無品牌）（0）」。
    """
    from console.core import brands
    assert brands.format_label(0, None) == f"{brands.NO_BRAND_NAME}（0）"
    # 真的查無（非 0 的編號但 MySQL 沒有）仍然要說「查無」
    assert brands.UNKNOWN_NAME in brands.format_label(999999999, None)


def test_ec_detail_source_ip_matches_the_ranking_value(client):
    """排名裡看到的 IP，貼回篩選器就一定要命中。

    CloudFront 的 x-forwarded-for 會帶整條代理鏈
    （`"client, proxy1, proxy2"`）。排名的 SQL 取第一段，明細必須用同一個
    收斂方式 —— 不然使用者從明細複製一個帶逗號的字串貼回篩選器，會查到 0 筆
    而畫面上完全看不出為什麼。
    """
    start, end = _recent_window(days=7)
    rows = explorer.detail(
        explorer.ExplorerFilter(source="ec", start=start, end=end))["rows"]
    if not rows:
        pytest.skip("ods_ec_request_log 在最近 7 天沒有資料")
    ips = [r["source_ip"] for r in rows if r["source_ip"]]
    assert ips, "沒有任何一列有來源 IP"
    assert not any("," in ip for ip in ips), (
        f"明細的來源 IP 帶著整條代理鏈，與排名的值不一致："
        f"{[ip for ip in ips if ',' in ip][:2]}")


# ── 資料起始時間 ─────────────────────────────────────────────────────────────

def test_every_health_card_says_when_the_data_starts(client):
    """十張裡有三張是 2026-08-06／07 才開始的。

    不標的話，查「最近 7 天」會畫出 6.9 天的 0 再突然跳起 ——
    那與「這個來源掛了又復活」在畫面上長得一樣。
    """
    cards = client.get("/api/health").json()["sources"]
    assert cards, "健康卡是空的"
    for c in cards:
        assert "data_since" in c, f"{c['key']} 的健康卡沒有 data_since 欄位"

    recent = {c["key"]: c["data_since"] for c in cards
              if c["key"] in ("console", "request", "batch")}
    assert len(recent) == 3
    for key, since in recent.items():
        assert since and since.startswith("2026-08-0"), (
            f"{key} 的資料起始應該是 2026-08-06 或之後，實際 {since!r}")


def test_data_since_ignores_the_epoch_sentinel(client):
    """`create_time` 的零值不可以被當成「資料起始」。

    實測 `ods_admin_log` 有 **42 列**（1,623 萬中）的 create_time 是
    1970-01-01（epoch 哨兵），真正的起始是 2017-04-12。不擋的話健康卡會寫
    「Admin Log 資料自 1970-01-01 起」—— 那是一句假話，而且它會讓整個
    「資料自 X 起」的標註失去可信度（看到 1970 就不會再相信 console 那個
    真實的 2026-08-06）。
    """
    cards = {c["key"]: c for c in client.get("/api/health").json()["sources"]}
    for key, card in cards.items():
        since = card.get("data_since")
        if since is None:
            continue
        assert since >= "2000-01-01", (
            f"{key} 的資料起始是 {since} —— 那是時間戳的零值哨兵，不是真的起始")


def test_admin_epoch_rows_are_not_silently_dropped(client):
    """擋掉哨兵值之後，「有幾列是零值」本身要說得出來。

    把 42 列靜靜排除掉，就是這個專案一再警告的「把沒有資料說成沒有發生」。
    它與健康卡的 missing_rate 是同一類東西：異常的比率本身就是訊號。
    """
    cards = {c["key"]: c for c in client.get("/api/health").json()["sources"]}
    admin = cards["admin"]
    assert "invalid_time_rows" in admin, (
        "健康卡要回報時間戳零值的列數 —— 擋掉它們卻不說，"
        "等於把一個資料品質訊號藏起來")


def test_health_status_band_scales_with_the_per_source_threshold():
    """健康卡的狀態帶必須跟著 `freshness_alert_minutes` 一起放寬。

    R12 的門檻放寬了但健康卡沒有的話，症狀是：**規則不誤報了，畫面卻還在
    誤報**。實測畫面：EC API Log 常駐顯示「異常（延遲 24.6 分）」，而
    `freshness_summary()` 把它推到總覽頂部的橫幅，寫著「此期間的異常判斷
    可能不完整」—— 那句話是假的（EC 只是週五晚上沒有購物流量），而且會常駐。

    把值班的人訓練成忽略一個永遠亮著的警示，等於把這個控制拆掉。
    """
    from console.core.config import settings
    from console.queries import health

    cfg = settings()["freshness"]
    for key, src in settings()["data_sources"].items():
        scale = health.freshness_scale(key)
        override = src.get("freshness_alert_minutes")
        if override:
            assert scale > 1, f"{key} 有覆寫門檻，狀態帶卻沒有跟著放寬"
            # **斷言比例一致**，不是斷言「R12 門檻內一律正常」。
            #
            # 全域的關係是 alert_minutes(20) 落在 notice(10)–stale(30) 之間 ——
            # 也就是卡片**本來就設計成比告警更敏感**（先變色、才發告警）。
            # 放寬只是把整條帶等比例拉長，不改變那個相對關係。
            for probe in (0.5, 1.5, 3.0, 8.0):
                assert (health._status(cfg["ok"] * probe * scale, scale)[0]
                        == health._status(cfg["ok"] * probe)[0]), (
                    f"{key} 在 {probe}× 的位置與全域來源不同帶 —— 放寬不是等比例的")
        else:
            assert scale == 1.0
    # 沒有覆寫的來源行為完全不變
    assert health._status(cfg["ok"] - 0.1)[0] == "正常"
    assert health._status(cfg["notice"] - 0.1)[0] == "注意"
    assert health._status(cfg["stale"] - 0.1)[0] == "異常"
    assert health._status(cfg["stale"] + 1)[0] == "停更"


def test_low_volume_sources_do_not_raise_the_delay_banner(client):
    """低流量來源不可以讓總覽頂部的「資料延遲」橫幅常駐。

    那個橫幅會說「此期間的異常判斷可能不完整」—— 對一張只是沒有流量的表，
    那是假的。
    """
    payload = client.get("/api/health").json()
    low = [c for c in payload["sources"] if c["key"] in ("ec", "request")]
    assert len(low) == 2
    for c in low:
        assert c["status"] in ("正常", "注意"), (
            f"{c['key']} 的健康卡顯示 {c['status']}（延遲 {c['lag_minutes']} 分）——"
            "它是低流量表，這是沒有流量而不是管線延遲")


def test_empty_aggregate_never_reaches_strftime():
    """`max()` / `min()` 的空結果是 pandas `NaT`，不是 `None`。

    **`NaT is not None` 是 True**，`NaT` 也有 `to_pydatetime()`（回 `NaT`），
    所以 `if value is not None` 這個防呆完全擋不住它 —— 一路走到
    `timewin.fmt()` 的 `strftime` 才拋 ValueError，而那是在 API 端點裡，
    症狀是 **/api/health 回 500**。

    實測正式環境：**每晚台北 00:00:0x 固定發生**，08-05／06／07／08 連續四晚
    都在 Cloud Logging 裡。原因是 `source_health()` 查「今日」而資料落地延遲約
    5 分鐘 —— 午夜剛過的那幾十秒每張表的 `max()` 都是 NULL。

    這個 bug 活了四晚沒被發現，正是因為沒有任何測試模擬「空結果」——
    真實資料在測試執行的時段永遠不是空的。
    """
    import pandas as pd
    from console.queries import health

    assert health._as_datetime(pd.NaT) is None, (
        "NaT 必須被當成「沒有資料」——它是 max()/min() 在空結果時的值")
    assert health._as_datetime(None) is None
    got = health._as_datetime(pd.Timestamp("2026-08-08 00:00:01"))
    assert got is not None and got.year == 2026

    # 端到端：正常值仍然格式化得出來（別為了擋 NaT 把正常路徑也擋掉）
    from console.core import timewin
    assert timewin.fmt(got) == "2026-08-08 00:00:01"


def test_health_card_survives_a_source_with_no_rows_today(client, monkeypatch):
    """某個來源今天完全沒有資料時，健康卡要降級而不是讓整個端點 500。

    低流量的表（ec 約 700–1,000 筆/日）夜間可能好幾個小時沒有任何一筆。
    """
    import pandas as pd
    from console.core import ch
    from console.queries import health

    real_query = ch.query
    empty = pd.DataFrame([{"latest": pd.NaT, "today_rows": 0, "missing": 0,
                           "uniq_ids": 0}])

    def fake_query(sql, params=None):
        if "AS latest" in sql:
            return empty
        return real_query(sql, params)

    monkeypatch.setattr(health, "query", fake_query)
    cards = health.source_health()
    assert cards, "健康卡不該是空的"
    for c in cards:
        assert c["latest"] is None, f"{c['key']} 沒有資料時 latest 應該是 None"
        assert c["status"] in ("停更", "查詢失敗"), (
            f"{c['key']} 今天沒有資料，狀態應該是停更，實際 {c['status']}")
