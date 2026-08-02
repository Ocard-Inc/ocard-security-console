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
