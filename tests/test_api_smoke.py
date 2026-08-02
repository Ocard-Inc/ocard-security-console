"""API 煙霧測試（會實際打 ClickHouse，需要有效 .env）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console.api.app import app


@pytest.fixture(scope="module")
def client():
    # 不啟動 lifespan（避免測試期間啟動排程器）
    return TestClient(app, raise_server_exceptions=True)


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
    assert "權限不足" in r.json()["detail"] or "無法使用" in r.json()["detail"]


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
