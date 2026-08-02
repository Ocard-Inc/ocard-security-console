"""API 煙霧測試（會實際打 ClickHouse，需要有效 .env）。"""
from __future__ import annotations


def test_session_default_admin(client):
    r = client.get("/api/session")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert "use_sql_console" in body["permissions"]


def test_session_viewer_lacks_admin_permissions(client):
    r = client.get("/api/session", headers={"X-Dev-Role": "viewer"})
    body = r.json()
    assert body["role_label"] == "Security Viewer"
    assert "use_sql_console" not in body["permissions"]
    assert "use_explorer" not in body["permissions"]
    assert "view_overview" in body["permissions"]


def test_explorer_forbidden_for_viewer(client):
    r = client.post("/api/explorer", headers={"X-Dev-Role": "viewer"},
                    json={"source": "api", "start": "2026-08-01 00:00:00",
                          "end": "2026-08-01 01:00:00"})
    assert r.status_code == 403
    # 權限相關的 detail 是結構化物件（前端要靠 code 分辨要顯示哪一種畫面）
    detail = r.json()["detail"]
    assert detail["code"] == "insufficient_role"
    assert "無法使用" in detail["message"]


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


def test_sparklines_allowed_for_viewer(client):
    """統計卡在總覽與資料健康兩頁都要用到，權限門檻與 view_health 相同。"""
    r = client.get("/api/sparklines", headers={"X-Dev-Role": "viewer"})
    assert r.status_code == 200
