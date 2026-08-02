"""API 煙霧測試（會實際打 ClickHouse，需要有效 .env）。"""
from __future__ import annotations


def test_session_reports_identity(client):
    """沒有角色分級：session 回的是身分，不是等級。"""
    r = client.get("/api/session")
    assert r.status_code == 200
    body = r.json()
    assert body["email"]
    assert body["role_label"]          # ROS 的角色名（離線模式為「開發模式」）
    assert body["auth_source"] in ("ros", "dev")
    assert "permissions" not in body, "已移除角色分級，不該再回權限清單"


def test_explorer_rejects_bad_range(client):
    r = client.post("/api/explorer", json={
        "source": "api", "start": "2026-08-01 02:00:00", "end": "2026-08-01 01:00:00"})
    assert r.status_code == 400


def test_explorer_rejects_unknown_source(client):
    r = client.post("/api/explorer", json={
        "source": "system.tables", "start": "2026-08-01 00:00:00",
        "end": "2026-08-01 01:00:00"})
    assert r.status_code == 400


def test_quick_catalog_has_16_templates(client):
    r = client.get("/api/quick")
    cats = r.json()["categories"]
    assert sum(len(c["items"]) for c in cats) == 16
    assert len(cats) == 4


def test_rules_endpoint_lists_all(client):
    r = client.get("/api/rules")
    rules = r.json()["rules"]
    assert len(rules) == 16
    assert {"R01", "R04", "R06", "R12"} <= {x["id"] for x in rules}


def test_events_list_ok(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    assert "events" in r.json()


def test_event_detail_404(client):
    r = client.get("/api/events/EVT-9999")
    assert r.status_code == 404


def test_judge_requires_all_fields(client):
    r = client.post("/api/events/EVT-0001/judge", json={"judgement": "誤報"})
    assert r.status_code == 400


# ── 圖表相關：時間範圍與 sparkline ────────────────────────────────────────

def test_overview_accepts_seven_day_range(client):
    """前端 RANGES 有「最近 7 天」（minutes=10080）；上限曾是 1440，會 422。"""
    r = client.get("/api/overview?minutes=10080")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trend"]["buckets"], "7 天視窗仍應有趨勢資料"
    # 排名比趨勢貴得多（sources 的 JSONExtract 掃 19M 列），後端會夾在 24 小時
    assert body["rankings"]["window_minutes"] == 1440


def test_overview_ranking_window_matches_when_under_cap(client):
    r = client.get("/api/overview?minutes=360")
    assert r.status_code == 200
    assert r.json()["rankings"]["window_minutes"] == 360


def test_overview_rejects_over_cap(client):
    r = client.get("/api/overview?minutes=20000")
    assert r.status_code == 422


def test_sparklines_shape(client):
    r = client.get("/api/sparklines")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hours"] == 24
    assert set(body["sources"]) == {"admin", "backend", "api", "auth"}
    for key, src in body["sources"].items():
        # 零填過，前端可以直接依索引取用，不必處理缺口
        assert len(src["points"]) == 24, f"{key} 應有 24 個點"
        assert all(isinstance(p["count"], int) for p in src["points"])
    # 嚴重度時間序列做不到（events 表就地覆寫、無逐 tick 歷史），必須誠實回 None
    assert body["severity"] is None
    assert body["severity_note"]


def test_sparklines_available(client):
    """統計卡在總覽與資料健康兩頁都要用到。"""
    r = client.get("/api/sparklines")
    assert r.status_code == 200


# ── 待判定事件 ────────────────────────────────────────────────────────────
# 事件會在數值回到門檻以下時自動 resolved，但首頁的 attention 只查 status='active'，
# 於是自動結束的事件完全從首頁消失、頁面顯示「沒有未處理事件」—— 即使從來沒有人
# 看過它們。那是假的安心感，跟本專案「沒有事件 ≠ 系統安全」的前提正好相反。

def test_overview_reports_pending_judgement(client):
    r = client.get("/api/overview?minutes=60")
    assert r.status_code == 200
    pj = r.json()["pending_judgement"]
    assert set(pj) == {"total", "by_severity", "oldest", "events"}
    assert set(pj["by_severity"]) == {"P0", "P1", "P2", "P3"}
    assert pj["total"] == sum(pj["by_severity"].values())
    assert len(pj["events"]) <= 5
    for e in pj["events"]:
        assert e["judgement"] is None, "待判定清單裡不該出現已判定的事件"
    if pj["total"]:
        assert pj["oldest"], "有待判定事件就該有最早時間，橫幅要用它"


def test_events_unjudged_filter(client):
    """首頁「前往判定」連結指向的查詢。"""
    everything = client.get("/api/events").json()
    unjudged = client.get("/api/events?unjudged=true").json()
    assert all(e["judgement"] is None for e in unjudged["events"])
    assert unjudged["total"] <= everything["total"]
    expected = sum(1 for e in everything["events"] if e["judgement"] is None)
    assert unjudged["total"] == expected


def test_events_unjudged_default_off(client):
    """不帶參數時不可過濾 —— 預設行為不能被這個新參數改掉。"""
    a = client.get("/api/events").json()["total"]
    b = client.get("/api/events?unjudged=false").json()["total"]
    assert a == b


# ── 自適應分桶 ────────────────────────────────────────────────────────────
# 固定 10 分鐘分桶時「最近 1 小時」只有 6 個點、「最近 7 天」有 1008 個點。
# 桶數對不上預期通常代表 timewin.align_bucket 與 ClickHouse 的格線錯位 ——
# 那個 bug 不會報錯，只會讓整張圖靜靜變成一條 0，所以這裡連桶數一起驗。

def test_bucket_ladder_one_hour(client):
    t = client.get("/api/overview?minutes=60").json()["trend"]
    assert t["bucket_minutes"] == 5
    assert len(t["buckets"]) == 12


def test_bucket_ladder_seven_days(client):
    t = client.get("/api/overview?minutes=10080").json()["trend"]
    assert t["bucket_minutes"] == 120
    assert len(t["buckets"]) == 84


def test_wide_window_still_has_baselines_and_sane_multiples(client):
    """分桶變粗時基線也要跟著換粒度，否則會冒出假的 12 倍。"""
    buckets = client.get("/api/overview?minutes=10080").json()["trend"]["buckets"]
    for name in ("api", "backend", "login_success", "login_failed"):
        assert any(b[f"{name}_median"] is not None for b in buckets), (
            f"{name} 在 120 分鐘分桶下沒有基線 —— calibrate 是否漏算這個粒度？")
    mults = [b["api_multiple"] for b in buckets if b["api_multiple"] is not None]
    assert mults, "應該要有倍數"
    # 用 10 分鐘的基線比 120 分鐘的桶會系統性地放大約 12 倍；
    # 這個上限抓得寬鬆，只是要擋掉「整段都被放大」那種粒度錯配。
    assert sum(m > 12 for m in mults) < len(mults) / 2, (
        f"超過一半的桶倍數 > 12，疑似基線粒度與分桶不匹配：{mults[:8]}")


def test_no_all_zero_trend_from_grid_misalignment(client):
    """格線錯位的症狀就是每個桶都查不到資料 → 整段變成 0。"""
    buckets = client.get("/api/overview?minutes=10080").json()["trend"]["buckets"]
    assert sum(b["api"] for b in buckets) > 0, "7 天視窗的 API 總量不可能是 0"


def test_event_trend_padding_widens_the_window(client):
    """事件視窗常常只有一兩小時，只看前後 30 分鐘看不出事件之前的脈絡。"""
    narrow = client.get("/api/events/EVT-0001?pad_minutes=30").json()["trend"]
    wide = client.get("/api/events/EVT-0001?pad_minutes=720").json()["trend"]
    assert narrow["rows"] and wide["rows"]
    # 拉寬之後分桶會變粗，但涵蓋的時間一定更長
    assert wide["bucket_minutes"] >= narrow["bucket_minutes"]
    span = lambda t: len(t["rows"]) * t["bucket_minutes"]
    assert span(wide) > span(narrow)


def test_event_trend_does_not_extend_into_the_future(client):
    """往後拉的區間很容易超過現在，那段永遠是 0，看起來像流量歸零。"""
    from console.core import timewin
    rows = client.get("/api/events/EVT-0001?pad_minutes=2880").json()["trend"]["rows"]
    now = timewin.effective_now()
    for r in rows:
        # bucket 是 "%m/%d %H:%M"，補上年份再比（事件都在今年）
        stamp = f"{now.year}/{r['bucket']}"
        assert stamp <= now.strftime("%Y/%m/%d %H:%M"), f"{r['bucket']} 在未來"


def test_event_trend_is_zero_filled(client):
    """沒有零填的話空桶會消失，category 軸依索引等距排列，安靜時段會被壓縮成直線。"""
    t = client.get("/api/events/EVT-0001?pad_minutes=180").json()["trend"]
    from console.core import timewin
    b = t["bucket_minutes"]
    stamps = [timewin.parse(f"2026/{r['bucket']}".replace("/", "-", 1)
                            .replace("/", "-", 1)) for r in t["rows"]]
    gaps = {int((b2 - b1).total_seconds() // 60) for b1, b2 in zip(stamps, stamps[1:])}
    assert gaps <= {b}, f"桶之間應等距 {b} 分鐘，實際有 {gaps}"


# ── 自訂絕對區間 ──────────────────────────────────────────────────────────
# 自訂區間會產生任意長度的視窗（例如 13:07~09:23），正是以前「今天」害整張圖
# 變成一條 0 的那個情境 —— start 沒對齊分桶格線。這組測試守著它。

def test_custom_range_overview(client):
    r = client.get("/api/overview",
                   params={"start": "2026-07-16 00:00:00", "end": "2026-07-16 06:00:00"})
    assert r.status_code == 200, r.text
    t = r.json()["trend"]
    assert t["start"] == "2026-07-16 00:00:00"
    assert t["end"] == "2026-07-16 06:00:00"
    assert sum(b["api"] for b in t["buckets"]) > 0, "自訂區間整段都是 0 —— start 沒對齊？"


def test_custom_range_with_odd_boundaries_is_aligned(client):
    """邊界故意不落在格線上，回傳的每個桶起點仍必須對齊。"""
    from console.core import timewin
    r = client.get("/api/overview",
                   params={"start": "2026-08-01 13:07:00", "end": "2026-08-02 09:23:00"})
    assert r.status_code == 200
    t = r.json()["trend"]
    b = t["bucket_minutes"]
    for row in t["buckets"]:
        dt = timewin.parse(row["bucket"])
        assert timewin.align_bucket(dt, b) == dt, f"{row['bucket']} 不在 {b} 分鐘格線上"
    assert sum(x["api"] for x in t["buckets"]) > 0


def test_custom_range_validation(client):
    bad = [
        ({"start": "2026-08-02 10:00:00", "end": "2026-08-02 09:00:00"}, "start 晚於 end"),
        ({"start": "2026-01-01 00:00:00", "end": "2026-08-01 00:00:00"}, "超過上限天數"),
        ({"start": "不是時間", "end": "2026-08-02 09:00:00"}, "無法解析"),
    ]
    for params, why in bad:
        assert client.get("/api/overview", params=params).status_code == 400, why
    # 只給一半視同沒給，回退成 minutes
    assert client.get("/api/overview", params={"start": "2026-08-02 09:00:00"}).status_code == 200


def test_custom_range_event_trend(client):
    r = client.get("/api/events/EVT-0001",
                   params={"start": "2026-08-01 00:00:00", "end": "2026-08-03 00:00:00"})
    assert r.status_code == 200
    rows = r.json()["trend"]["rows"]
    assert rows and rows[0]["bucket"].startswith("08/01")


def test_three_day_preset(client):
    """新的「最近 3 天」= 4320 分鐘，應落在 120 分鐘分桶、36 個點。"""
    t = client.get("/api/overview?minutes=4320").json()["trend"]
    assert t["bucket_minutes"] == 120
    assert len(t["buckets"]) == 36


def test_whole_day_range_is_not_truncated(client):
    """自訂區間只選日期，結束一律是 23:59:59。

    右界必須**向上**取整到完整的桶 —— 向下取整的話 120 分鐘分桶會退到 22:00，
    當天最後兩小時整段消失，而且不會有任何錯誤訊息。
    """
    t = client.get("/api/overview",
                   params={"start": "2026-07-16 00:00:00",
                           "end": "2026-07-16 23:59:59"}).json()["trend"]
    b = t["bucket_minutes"]
    assert len(t["buckets"]) == 1440 // b, (
        f"整天應該有 {1440 // b} 個 {b} 分鐘的桶，實際 {len(t['buckets'])} 個")
    assert t["start"] == "2026-07-16 00:00:00"
    assert t["end"] == "2026-07-17 00:00:00", "右界要涵蓋到當天最後一刻"


def test_multi_day_range_is_not_truncated(client):
    t = client.get("/api/overview",
                   params={"start": "2026-07-15 00:00:00",
                           "end": "2026-07-17 23:59:59"}).json()["trend"]
    b = t["bucket_minutes"]
    assert len(t["buckets"]) == 3 * 1440 // b
    assert t["end"] == "2026-07-18 00:00:00"


# ─────────────────────── 品牌選擇器（GET /api/brands）───────────────────────

def test_brands_endpoint_searches_by_name(client):
    rows = client.get("/api/brands", params={"q": "瓦城"}).json()["rows"]
    assert rows
    assert all("idx" in b and "name" in b and "status" in b for b in rows)


def test_brands_endpoint_searches_by_id(client):
    rows = client.get("/api/brands", params={"q": "1180"}).json()["rows"]
    assert rows[0]["idx"] == 1180


def test_brands_endpoint_blank_query_is_empty(client):
    assert client.get("/api/brands", params={"q": ""}).json()["rows"] == []
    assert client.get("/api/brands").json()["rows"] == []


def test_brands_endpoint_rejects_out_of_range_limit(client):
    assert client.get("/api/brands", params={"q": "a", "limit": 0}).status_code == 422
    assert client.get("/api/brands", params={"q": "a", "limit": 999}).status_code == 422


def test_brands_endpoint_respects_limit(client):
    rows = client.get("/api/brands", params={"q": "a", "limit": 5}).json()["rows"]
    assert len(rows) <= 5


# ───────────────────── endpoint 建議（GET /api/endpoints）─────────────────────

_EP_WIN = {"start": "2026-08-01 00:00:00", "end": "2026-08-02 00:00:00"}


def test_endpoints_endpoint_returns_sorted_rows(client):
    body = client.get("/api/endpoints", params={"source": "api", **_EP_WIN}).json()
    counts = [r["count"] for r in body["rows"]]
    assert counts and counts == sorted(counts, reverse=True)
    assert body["total"] == len(body["rows"])


def test_endpoints_endpoint_rejects_auth_with_400(client):
    """迴歸：ods_auth_log 沒有 function 欄位，以前這條路徑會生出壞 SQL 回 502。"""
    r = client.get("/api/endpoints", params={"source": "auth", **_EP_WIN})
    assert r.status_code == 400, r.text


def test_endpoints_endpoint_rejects_bad_window(client):
    r = client.get("/api/endpoints", params={
        "source": "api", "start": "2026-08-02 00:00:00", "end": "2026-08-01 00:00:00"})
    assert r.status_code == 400


def test_endpoints_endpoint_requires_window(client):
    assert client.get("/api/endpoints", params={"source": "api"}).status_code == 400


def test_explorer_auth_endpoint_filter_is_400_not_502(client):
    """同一個 bug 的另一個入口。"""
    r = client.post("/api/explorer", json={
        "source": "auth", "analysis": "trend", "endpoint": "login", **_EP_WIN})
    assert r.status_code == 400, r.text
