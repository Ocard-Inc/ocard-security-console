from __future__ import annotations

import json

from console.core.config import fp_secret
from console.store import db


def _flatten(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


TEST_EVT_NO = "EVT-HUNTING-TEST"


def test_hunting_summary_is_allowlisted_and_masks_sensitive_event_data(client, monkeypatch):
    """這支測試會**寫入** events 並竄改 fp_secret 的快取，兩者都必須還原。

    `conftest.state_db` 的 DB 複本是 session 範圍的，`fp_secret()` 有 `lru_cache`
    —— 所以殘留會活過這支測試，變成別人身上莫名其妙的失敗：

    - 這一列的 `entity_label` 是 `attacker@example.test`、`judgement` 是原始
      token、`context_json` 帶原始 IP，而 `last_seen` 寫到 2099 年（任何時間窗
      都查得到它）。留著的話 `tests/test_masking_audit.py` 掃 `/api/events` 與
      `/api/overview` 一律撞上非白名單 Email 而失敗 —— 那個檔案是驗收條件的
      自動化檢查，讓它恆紅等於把這個控制拆掉（下一個人只會學會跳過它）。
    - `monkeypatch.setenv` 會還原環境變數，但**還原不了 `lru_cache` 裡那個假
      密鑰**。不在事後再 `cache_clear()` 一次的話，後面每一個 `fp_secret()`
      的呼叫者（`masking.token_fp()`、`masking.actor()` 的截斷後綴）都拿到
      測試用的密鑰。同 CLAUDE.md 記的 `ch_config()` 那次（commit becb2ce）。
    """
    monkeypatch.setenv("FP_SECRET", "test-hunting-fingerprint-secret")
    fp_secret.cache_clear()
    raw_ip = "198.51.100.10"
    raw_account = "attacker@example.test"
    raw_token = "token-secret-value"
    try:
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
                    TEST_EVT_NO, "api_burst", "API burst", "P1",
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
    finally:
        with db.tx() as conn:
            conn.execute("DELETE FROM events WHERE evt_no = ?", (TEST_EVT_NO,))
        # monkeypatch 還原的是環境變數，不是快取住的值。
        fp_secret.cache_clear()


def test_hunting_summary_rejects_invalid_since(client):
    response = client.get("/api/hunting-summary?since=not-a-date")

    assert response.status_code == 400
