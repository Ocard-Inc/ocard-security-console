from __future__ import annotations

import json

from console.core.config import fp_secret
from console.store import db


def _flatten(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_hunting_summary_is_allowlisted_and_masks_sensitive_event_data(client, monkeypatch):
    monkeypatch.setenv("FP_SECRET", "test-hunting-fingerprint-secret")
    fp_secret.cache_clear()
    raw_ip = "198.51.100.10"
    raw_account = "attacker@example.test"
    raw_token = "token-secret-value"
    with db.tx() as conn:
        conn.execute(
            """
            INSERT INTO events (
                evt_no, rule_id, rule_name, severity, entity_key, entity_label,
                source_key, metric_value, threshold, baseline_median, baseline_p95,
                multiple, brands, first_seen, last_seen, peak_value, status,
                judgement, context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "EVT-HUNTING-TEST", "api_burst", "API burst", "P1",
                f"{raw_ip}:{raw_account}", raw_account, "api", 120, 30, 5, 10,
                24, 3, "2026-08-06 10:00:00", "2099-08-06 10:15:00", 120,
                "active", raw_token,
                json.dumps({"ip": raw_ip, "account": raw_account, "token": raw_token}),
            ),
        )

    response = client.get("/api/hunting-summary?since=2026-08-06")

    assert response.status_code == 200
    payload = response.json()
    event = next(item for item in payload["events"] if item["event_fp"])
    assert set(event) == {
        "event_fp", "rule_id", "rule_name", "severity", "source", "metric",
        "threshold", "median", "p95", "multiple", "brands", "first_seen",
        "last_seen", "peak", "hit_count", "status",
    }
    assert raw_ip not in _flatten(payload)
    assert raw_account not in _flatten(payload)
    assert raw_token not in _flatten(payload)
    assert "entity_key" not in _flatten(payload)
    assert "entity_label" not in _flatten(payload)
    assert "context_json" not in _flatten(payload)
    assert "judgement" not in _flatten(payload)


def test_hunting_summary_rejects_invalid_since(client):
    response = client.get("/api/hunting-summary?since=not-a-date")

    assert response.status_code == 400
