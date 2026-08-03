"""schema 與遷移的守門測試。

擋的是一種只在正式環境出現的漂移：`db._SCHEMA` 改了、`store/migrate.py` 的
`_ADD_COLUMNS` 沒跟著改。本機的 DB 常常是重建過的（或手動改過），所以
「新環境有欄位、舊環境沒有」在本機完全看不出來 —— 而讀取端拿不到欄位時，
`row.get("rule_id")` 會靜靜回 None，也就是「全域」。
"""
from __future__ import annotations

import sqlite3

import pytest

from console.store import db, migrate

# 期望的欄位集合。改 schema 時**同時**改這裡，並確認 migrate._ADD_COLUMNS
# 能把舊 DB 帶到同一個狀態。
_ALLOWLIST_COLUMNS = {
    "id", "name", "owner", "purpose", "reason", "rule_id", "source_ip", "endpoint",
    "integration_type", "brand_scope", "token_fp", "expected_rate",
    "valid_from", "valid_to", "approved_by", "created_at", "updated_at",
    "updated_by", "status",
}
_RULE_OVERRIDES_COLUMNS = {
    "rule_id", "enabled", "static_floor", "factor", "cooldown_minutes",
    "min_events", "updated_at", "updated_by", "reason",
}
_RULE_SUPPRESSIONS_COLUMNS = {
    "id", "at", "allowlist_id", "rule_id", "source_ip", "entity_label",
    "metric", "threshold", "window_start", "window_end",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """全新的 DB：只走 _SCHEMA，migrate 應該無事可做。"""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.executescript(db._SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def legacy_db(tmp_path):
    """2026-08 之前的 allowlist：source_fp、沒有 rule_id 那批欄位。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE allowlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, owner TEXT, purpose TEXT, integration_type TEXT,
            endpoint TEXT, brand_scope TEXT, source_fp TEXT, token_fp TEXT,
            expected_rate TEXT, valid_from TEXT, valid_to TEXT, approved_by TEXT,
            status TEXT NOT NULL DEFAULT '待核准'
        );
    """)
    conn.execute(
        "INSERT INTO allowlist (name, source_fp, valid_from, status)"
        " VALUES ('辦公室出口', '1.34.41.218', '2026-08-03 10:00:00', '生效中')")
    conn.execute(
        "INSERT INTO allowlist (name, source_fp, status)"
        " VALUES ('舊指紋條目', 'src_BB9735634D3A', '生效中')")
    conn.commit()
    yield conn
    conn.close()


def test_fresh_schema_has_the_expected_columns(fresh_db):
    assert _columns(fresh_db, "allowlist") == _ALLOWLIST_COLUMNS
    assert _columns(fresh_db, "rule_overrides") == _RULE_OVERRIDES_COLUMNS
    assert _columns(fresh_db, "rule_suppressions") == _RULE_SUPPRESSIONS_COLUMNS


def test_migrate_is_a_noop_on_a_fresh_schema(fresh_db):
    assert migrate.apply(fresh_db) == []


def test_legacy_db_reaches_the_same_shape(legacy_db):
    """舊 DB 遷移後的欄位集合必須與全新 DB **完全相同**。"""
    migrate.apply(legacy_db)
    legacy_db.executescript(db._SCHEMA)      # 補上新表（CREATE IF NOT EXISTS）
    legacy_db.commit()
    assert _columns(legacy_db, "allowlist") == _ALLOWLIST_COLUMNS
    assert _columns(legacy_db, "rule_overrides") == _RULE_OVERRIDES_COLUMNS


def test_rename_keeps_the_data(legacy_db):
    migrate.apply(legacy_db)
    row = legacy_db.execute(
        "SELECT source_ip, created_at FROM allowlist WHERE name = '辦公室出口'").fetchone()
    assert row[0] == "1.34.41.218", "RENAME COLUMN 不該動到資料"
    # created_at 由 valid_from 回填，**不是**用現在的時間編一個假的建立時間
    assert row[1] == "2026-08-03 10:00:00"


def test_legacy_fingerprint_entries_are_disabled(legacy_db):
    """舊指紋條目比對的是原始 IP，永遠不會命中 —— 留著會顯示「生效中」卻沒作用。"""
    migrate.apply(legacy_db)
    row = legacy_db.execute(
        "SELECT status, reason FROM allowlist WHERE name = '舊指紋條目'").fetchone()
    assert row[0] == "已停用"
    assert "舊指紋" in row[1]
    # 真正的 IP 條目不可被牽連
    assert legacy_db.execute(
        "SELECT status FROM allowlist WHERE name = '辦公室出口'").fetchone()[0] == "生效中"


def test_migrate_runs_twice_without_side_effects(legacy_db):
    """每條 thread-local 連線都會跑一次，所以第二次必須完全沒有動作。"""
    first = migrate.apply(legacy_db)
    assert first, "第一次應該有動作，否則這則測試什麼都沒驗到"
    assert migrate.apply(legacy_db) == []


def test_session_db_is_already_migrated():
    """conftest 的複本經過 get_conn()，所以真實資料也必須已經遷移完成。"""
    assert "source_ip" in _columns(db.get_conn(), "allowlist")
    assert "source_fp" not in _columns(db.get_conn(), "allowlist")
