"""SQLite（WAL）狀態庫：事件、案件、稽核、基線、known_sources、心跳。

單一檔案 state/monitor.db。daemon 寫入、Web API 讀寫並行，WAL 模式支援。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from console.core.config import STATE_DIR

DB_PATH = STATE_DIR / "monitor.db"

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evt_no TEXT UNIQUE NOT NULL,            -- EVT-0001
    rule_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    severity TEXT NOT NULL,                 -- P0/P1/P2/P3
    entity_key TEXT NOT NULL,               -- 去重鍵（fingerprint 組合）
    entity_label TEXT NOT NULL,             -- 顯示用（已遮罩）
    source_key TEXT NOT NULL,               -- admin/backend/api/auth
    metric_value REAL NOT NULL,
    threshold REAL,
    baseline_median REAL,
    baseline_p95 REAL,
    multiple REAL,                          -- 目前值 / median
    brands INTEGER,
    first_seen TEXT NOT NULL,               -- 台北牆鐘
    last_seen TEXT NOT NULL,
    last_notified TEXT,
    hit_count INTEGER NOT NULL DEFAULT 1,
    peak_value REAL NOT NULL,
    miss_ticks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',  -- active/resolved
    judgement TEXT,                         -- 已確認攻擊/合法整合/誤報/證據不足/保持觀察
    judgement_note TEXT,
    case_id TEXT,
    owner TEXT,
    context_json TEXT                       -- 已遮罩的補充資訊
);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events (rule_id, entity_key, status);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (first_seen);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT UNIQUE NOT NULL,           -- CASE-001
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '調查中',
    owner TEXT,
    summary TEXT,
    root_cause TEXT,
    disposition TEXT,
    followup TEXT,
    close_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS case_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT NOT NULL,
    at TEXT NOT NULL,
    who TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    who TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,                   -- 登入/查看事件/執行模板/執行SQL/匯出/...
    target TEXT NOT NULL,
    query_hash TEXT,
    time_range TEXT,
    row_count INTEGER,
    duration_ms INTEGER,
    case_no TEXT,
    result TEXT NOT NULL,                   -- 成功/失敗/timeout
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (at);

CREATE TABLE IF NOT EXISTS allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner TEXT,
    purpose TEXT,
    integration_type TEXT,
    endpoint TEXT,
    brand_scope TEXT,
    source_fp TEXT,
    token_fp TEXT,
    expected_rate TEXT,
    valid_from TEXT,
    valid_to TEXT,
    approved_by TEXT,
    status TEXT NOT NULL DEFAULT '待核准'
);

CREATE TABLE IF NOT EXISTS known_sources (
    kind TEXT NOT NULL,                     -- backend_acc_ip / api_src / admin_acc_ip
    entity_key TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    origin TEXT NOT NULL,                   -- seed / live
    PRIMARY KEY (kind, entity_key)
);

CREATE TABLE IF NOT EXISTS baselines (
    metric_key TEXT NOT NULL,               -- 例：api_endpoint_10m:Api2/TransDetail
    hour INTEGER NOT NULL,                  -- 0-23；-1 表示不分時段
    day_class TEXT NOT NULL,                -- weekday/weekend/all
    median REAL, p95 REAL, p99 REAL, maxv REAL, samples INTEGER,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (metric_key, hour, day_class)
);

CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    key TEXT PRIMARY KEY,                   -- five_min / daily
    last_tick TEXT,
    last_ok TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS slack_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rows(sql: str, params: tuple | dict = ()) -> list[dict]:
    cur = get_conn().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def one(sql: str, params: tuple | dict = ()) -> dict | None:
    cur = get_conn().execute(sql, params)
    r = cur.fetchone()
    return dict(r) if r else None


def next_serial(prefix: str, table: str, column: str) -> str:
    """產生 EVT-0001 / CASE-001 型流水號（呼叫端需在 tx 內）。"""
    row = one(f"SELECT {column} AS no FROM {table} ORDER BY id DESC LIMIT 1")
    width = 4 if prefix == "EVT" else 3
    if row is None:
        return f"{prefix}-{1:0{width}d}"
    last = int(str(row["no"]).rsplit("-", 1)[1])
    return f"{prefix}-{last + 1:0{width}d}"
