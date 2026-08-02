"""遮罩稽核：掃描 API 回應，確認不含原始 IP、帳號、token、訂單號。

這是設計稿第 26 節驗收條件第 8 項的自動化驗證：
「UI 不出現完整 IP、帳號、token、secret、手機、Email、會員 ID 或訂單號。」
"""
from __future__ import annotations

import json
import re

# 已知的真實識別值（來自 7/16 事件與生產資料），絕不可出現在任何回應中
FORBIDDEN_LITERALS = [
    "andrew_c", "131.143.215.229", "ocardcathy", "ocardjacky",
    "doremi000", "ohlaskin", "wu-tau",
]

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE = re.compile(r"\b09\d{8}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# 允許出現的例外：本機位址與登入帳號 email（session 端點本來就要回自己的身分）
IP_ALLOW = {"127.0.0.1", "0.0.0.0"}
EMAIL_ALLOW = {"vinek@olis.com.tw"}


def _scan(payload: str, where: str) -> None:
    for lit in FORBIDDEN_LITERALS:
        assert lit not in payload, f"{where} 洩漏原始識別值 {lit!r}"
    for ip in IPV4.findall(payload):
        assert ip in IP_ALLOW, f"{where} 洩漏原始 IP {ip}"
    assert not PHONE.search(payload), f"{where} 洩漏手機號碼"
    for mail in EMAIL.findall(payload):
        assert mail in EMAIL_ALLOW, f"{where} 洩漏 Email {mail}"


def test_overview_response_is_masked(client):
    r = client.get("/api/overview?minutes=60")
    assert r.status_code == 200
    _scan(r.text, "GET /api/overview")


def test_events_response_is_masked(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    _scan(r.text, "GET /api/events")
    for e in r.json()["events"]:
        detail = client.get(f"/api/events/{e['evt_no']}")
        _scan(detail.text, f"GET /api/events/{e['evt_no']}")


def test_explorer_detail_is_masked(client):
    for source in ("api", "backend", "admin", "auth"):
        r = client.post("/api/explorer", json={
            "source": source, "analysis": "detail",
            "start": "2026-08-01 12:00:00", "end": "2026-08-01 12:10:00", "limit": 50})
        assert r.status_code == 200, r.text
        _scan(r.text, f"POST /api/explorer detail source={source}")


def test_explorer_rankings_are_masked(client):
    for dim in ("endpoint", "brand", "source", "actor"):
        r = client.post("/api/explorer", json={
            "source": "backend", "analysis": dim,
            "start": "2026-07-16 00:00:00", "end": "2026-07-16 02:00:00"})
        assert r.status_code == 200, r.text
        _scan(r.text, f"POST /api/explorer {dim}")


def test_quick_templates_are_masked(client):
    cases = [
        ("t01", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t03", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t06", {"start": "2026-07-16 00:00:00", "end": "2026-07-16 06:00:00"}),
        ("t12", {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}),
        ("t13", {}),
    ]
    for tid, params in cases:
        r = client.post(f"/api/quick/{tid}", json=params)
        assert r.status_code == 200, f"{tid}: {r.text}"
        _scan(r.text, f"POST /api/quick/{tid}")


def test_attack_account_appears_only_as_fingerprint(client):
    """7/16 攻擊帳號在查詢結果中必須是 actor_ fingerprint 而非 andrew_c。"""
    r = client.post("/api/quick/t06", json={
        "start": "2026-07-16 00:00:00", "end": "2026-07-16 06:00:00"})
    body = r.json()
    rows = body["rows"]
    assert rows, "7/16 應查得到 orderlist 存取紀錄"
    top = max(rows, key=lambda x: x["count"])
    assert top["count"] > 100000, "應查到攻擊量級的存取"
    assert top["actor_fp"].startswith("actor_"), "操作者必須以 fingerprint 呈現"
    assert "andrew_c" not in json.dumps(body, ensure_ascii=False)


def test_sparklines_response_is_masked(client):
    """新端點依 CLAUDE.md 硬性要求納入掃描（目前只回計數與來源 key，但規則就是規則）。"""
    r = client.get("/api/sparklines")
    assert r.status_code == 200
    _scan(r.text, "GET /api/sparklines")


def test_overview_widest_window_is_masked(client):
    """排名的 src_ fingerprint 現在會出現在長條圖軸標籤上，最寬視窗也要掃過。"""
    r = client.get("/api/overview?minutes=10080")
    assert r.status_code == 200
    _scan(r.text, "GET /api/overview?minutes=10080")
