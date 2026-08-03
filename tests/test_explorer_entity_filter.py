"""Explorer 依對象（帳號、來源 IP）反查。

這是「掃描結果 → 明細」的那一步：在事件清單看到 `andrew_c` 或 `131.143.215.229`，
貼進 Explorer 就只剩那個對象的資料。

守三件事：

1. **篩選真的有作用**。這兩個欄位曾經在 `ExplorerFilter` 裡宣告了但 `where_clause`
   完全沒讀 —— 使用者填了以為有篩，實際上回的是全部資料。那不會報錯。
2. **排名裡看到的值貼回去查得到**。篩選的運算式直接複用 `GROUP_BY`，
   這條測試驗證那個不變量在四張表上都成立。
3. **不支援的組合明確拒絕**，不是回 0 筆。Auth Log 的操作者是不可逆的 token 指紋，
   拿指紋去比對原值永遠不相等 —— 回「查無資料」會讓人以為沒有這個對象。
"""
from __future__ import annotations

import pytest

# 報告事件 1：2026-07-16 攻擊當日。andrew_c 全部來自 131.143.215.229。
WINDOW = {"start": "2026-07-16 00:00:00", "end": "2026-07-16 01:00:00"}
ACCOUNT = "andrew_c"
SOURCE_IP = "131.143.215.229"


def _post(client, **overrides):
    body = {"source": "backend", "analysis": "detail", "limit": 20, **WINDOW}
    body.update(overrides)
    return client.post("/api/explorer", json=body)


def test_actor_filter_narrows_to_that_account(client):
    everything = _post(client).json()["total"]
    filtered = _post(client, actor=ACCOUNT).json()
    assert filtered["total"] > 0, f"{ACCOUNT} 在這個區間應有資料"
    assert filtered["total"] < everything, (
        "帳號篩選沒有縮小結果 —— where_clause 可能沒讀這個欄位")
    assert all(r["actor"] == ACCOUNT for r in filtered["rows"])


def test_source_ip_filter_narrows_to_that_source(client):
    everything = _post(client).json()["total"]
    filtered = _post(client, source_ip=SOURCE_IP).json()
    assert filtered["total"] > 0
    assert filtered["total"] < everything, "來源 IP 篩選沒有縮小結果"
    assert all(r["source_ip"] == SOURCE_IP for r in filtered["rows"])


def test_both_filters_combine(client):
    both = _post(client, actor=ACCOUNT, source_ip=SOURCE_IP).json()
    assert both["total"] > 0
    for r in both["rows"]:
        assert r["actor"] == ACCOUNT and r["source_ip"] == SOURCE_IP


def test_filter_is_exact_not_prefix(client):
    """貼一個是真實 IP 前綴的字串不該命中 —— 否則使用者以為查到了特定主機。"""
    truncated = SOURCE_IP[:-1]          # 131.143.215.22
    assert _post(client, source_ip=truncated).json()["total"] == 0


def test_unknown_entity_returns_zero_not_error(client):
    r = _post(client, actor="這個帳號不存在")
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.parametrize("analysis", ["trend", "endpoint", "brand", "source", "actor"])
def test_filter_applies_to_every_analysis(client, analysis):
    """篩選必須對所有分析方式生效，不只明細 —— 否則趨勢圖與排名會與明細不一致。"""
    r = _post(client, analysis=analysis, actor=ACCOUNT)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows, f"{analysis} 在篩選後不該是空的"
    if analysis == "actor":
        assert [x["name"] for x in rows] == [ACCOUNT], "actor 排名應只剩這個帳號"


def test_auth_actor_filter_is_rejected_with_a_reason(client):
    """Auth Log 的操作者是 token 指紋，無法反查 —— 要明確拒絕而非回 0 筆。"""
    r = client.post("/api/explorer", json={
        "source": "auth", "analysis": "detail", "actor": "token_ABC123", **WINDOW})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "token" in detail and "指紋" in detail, f"錯誤訊息沒解釋原因：{detail}"


def test_auth_still_supports_source_ip_filter(client):
    """auth 不支援帳號篩選，但來源 IP 沒有這個問題 —— 不可一起擋掉。"""
    r = client.post("/api/explorer", json={
        "source": "auth", "analysis": "detail", "source_ip": "1.2.3.4",
        "start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("source", ["api", "backend", "admin"])
def test_ranking_values_can_be_pasted_back_as_filters(client, source):
    """不變量：排名裡看到的對象值，貼回篩選器就查得到。

    篩選運算式複用 `GROUP_BY`，這條測試在三張表上實際驗證那個複用是對的
    （auth 的操作者是指紋，另有專屬測試）。
    """
    window = {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}
    for dimension, field in (("actor", "actor"), ("source", "source_ip")):
        rank = client.post("/api/explorer", json={
            "source": source, "analysis": dimension, **window})
        assert rank.status_code == 200, rank.text
        names = [r["name"] for r in rank.json()["rows"] if r["name"] != "（空）"]
        if not names:
            continue                      # 這個區間該維度沒有資料，跳過而非誤判失敗
        back = client.post("/api/explorer", json={
            "source": source, "analysis": "detail", "limit": 5,
            field: names[0], **window})
        assert back.status_code == 200, back.text
        assert back.json()["total"] > 0, (
            f"{source} 的 {dimension} 排名值 {names[0]!r} 貼回 {field} 查不到 —— "
            "GROUP_BY 與篩選的運算式不一致")


# ── 「0 筆」必須自己解釋原因 ──────────────────────────────────────────

# 實測：192.168.97.1 的活動在 2026-04-20 ~ 2026-07-29，而「今天」是 2026-08-03。
# 用最近一小時查它一定是 0 筆 —— 這正是使用者實際踩到的情況。
_DEV_IP = "192.168.97.1"
_RECENT_HOUR = {"start": "2026-08-03 05:30:00", "end": "2026-08-03 06:30:00"}


def test_empty_result_explains_that_object_is_outside_the_range(client):
    """對象存在但不在區間內 → 要說出它實際的活動範圍。

    掃描用 30 天視窗，Explorer 預設 1 小時。只顯示空表格的話，使用者會以為
    「掃描說有、Explorer 說沒有」是矛盾或 bug。
    """
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail", "source_ip": _DEV_IP, **_RECENT_HOUR})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    reason = body.get("empty_reason")
    assert reason, "0 筆卻沒有說明原因"
    assert reason["kind"] == "outside_range"
    assert reason["total_in_lookback"] > 0
    assert reason["first_seen"] < reason["last_seen"]
    # 訊息要足以讓人自己修正查詢
    assert _DEV_IP in reason["message"] and reason["last_seen"] in reason["message"]


def test_empty_result_distinguishes_typo_from_out_of_range(client):
    """打錯的值要說「找不到」，不是「不在區間」—— 兩者的下一步完全不同。"""
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail",
        "source_ip": _DEV_IP + "1", **_RECENT_HOUR})     # 192.168.97.11
    reason = r.json().get("empty_reason")
    assert reason and reason["kind"] == "not_found"
    assert "完全相等" in reason["message"], "應提醒比對是完全相等而非前綴"


def test_extent_probe_covers_actor_too(client):
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail", "actor": ACCOUNT, **_RECENT_HOUR})
    reason = r.json().get("empty_reason")
    assert reason and reason["kind"] == "outside_range"
    assert reason["field"] == "actor"


def test_no_explanation_when_there_is_data(client):
    """有資料時不該出現這個欄位 —— 多餘的橫幅會讓人以為出了什麼事。"""
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail", "actor": ACCOUNT, "limit": 5, **WINDOW})
    assert r.json()["total"] > 0
    assert r.json().get("empty_reason") is None


def test_no_explanation_probe_when_no_entity_filter(client):
    """沒下對象篩選就不多跑那趟查詢 —— 區間太窄本來就看得出來。"""
    r = client.post("/api/explorer", json={
        "source": "backend", "analysis": "detail",
        "start": "2026-08-03 06:29:00", "end": "2026-08-03 06:29:01"})
    assert r.status_code == 200
    assert r.json().get("empty_reason") is None


# ── API Log 的來源 IP：回看查詢的成本與非阻塞 ──────────────────────────────
#
# 這一段守的是一個實際發生的故障：在 **API Log** 查一個 IP 而結果 0 筆時，
# 使用者等了約 56 秒，得到空表格與**零解釋**，而且那段時間整個主控台失去回應
# （篩選、Controller 建議全部沒反應）。三個獨立缺陷：
#
# 1. `entity_extent` 對四張表用同一個 365 天回看，但 api 的來源 IP 要對 headers
#    做 JSONExtract（實測 30 天 7.5s／90 天 29.6s／365 天撞上 55 秒上限）
# 2. 超時的 `ChQueryError` 被 `_explain_empty` 吞掉回 None ——
#    畫面上「沒有解釋」與「查過了，真的不存在」長得一樣
# 3. `/explorer` 是 `async def` 而查詢是阻塞的 → 一個慢查詢凍住事件迴圈，
#    實測完全不碰 ClickHouse 的 `/api/session` 被拖到 53.6 秒

def test_api_source_ip_lookback_is_cheaper_than_the_others():
    """api 的來源 IP 回看天數必須比其他表短 —— 它是唯一要解析 headers 的。"""
    from console.queries import explorer as ex
    api_days = ex.extent_lookback_days("api", "source_ip")
    assert api_days < ex.extent_lookback_days("backend", "source_ip")
    assert api_days < ex.extent_lookback_days("api", "actor"), \
        "只有 source_ip 需要解析 headers，actor 是真欄位（_admin）"
    assert api_days <= 60, (
        f"實測 60 天要 18.5 秒、90 天 29.6 秒、365 天撞上 55 秒上限；"
        f"目前設 {api_days} 天。要調高請先重量一次。")


def test_api_source_ip_empty_result_still_explains_itself(client):
    """在 API Log 查一個存在但不在區間內的 IP，必須給得出解釋。

    這是原本壞掉的那一條路徑：回看查詢超時 → 例外被吞 → empty_reason 是 None。
    """
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "trend", "source_ip": _DEV_IP, **_RECENT_HOUR})
    assert r.status_code == 200, r.text
    reason = r.json().get("empty_reason")
    if r.json().get("rows"):
        return                                  # 這個 IP 剛好有流量，換測不了
    assert reason, "API Log 的 0 筆沒有任何說明（原本的故障）"
    # 三種都可以接受，唯一不可接受的是 None ——
    # explain_failed 也算合格：它明說「無法確認」，而不是假裝查過了。
    assert reason["kind"] in ("outside_range", "not_found", "explain_failed")
    if reason["kind"] == "explain_failed":
        assert "無法" in reason["message"]
        assert reason["lookback_days"] > 0


def test_explorer_endpoint_is_sync_so_slow_queries_cannot_freeze_the_loop():
    """`/explorer` 必須是同步 `def`（跑在 threadpool），不是 `async def`。

    寫成 async def 時，裡面阻塞的 ClickHouse 查詢會佔住事件迴圈：一個慢查詢
    讓**所有**請求排隊，使用者看到的不是「這個查詢慢」而是「整個主控台壞了」，
    連五分鐘排程都會停。同 /sweep 與 /explorer/payload 的理由。
    """
    import inspect
    from console.api import routes
    assert not inspect.iscoroutinefunction(routes.run_explorer), \
        "run_explorer 是 async def —— 阻塞查詢會凍住整個事件迴圈"
