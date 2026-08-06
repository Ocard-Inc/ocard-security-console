"""SQLite（WAL）狀態庫：事件、案件、稽核、基線、known_sources、心跳。

單一檔案 state/monitor.db。daemon 寫入、Web API 讀寫並行，WAL 模式支援。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from console.core.config import STATE_DIR
from console.store import migrate

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
    -- active/resolved 由狀態機寫；closed 只由人寫（見 store/events.py 模組說明）。
    -- 所有機器端查詢都寫 status = 'active'，所以 closed 自動退出狀態機 ——
    -- 那是刻意的：新增一個機器不認識的狀態值時，「忘記排除它」的失敗模式
    -- 比「忘記納入它」危險得多。
    status TEXT NOT NULL DEFAULT 'active',  -- active/resolved/closed
    judgement TEXT,                         -- 已確認攻擊/合法整合/誤報/證據不足/保持觀察
    judgement_note TEXT,
    -- 人工結案的三個欄位。closed_from 記的是關閉當下狀態機的值，
    -- 復原時才還得回去（見 api/routes.reopen_event）。
    closed_at TEXT,
    closed_by TEXT,
    closed_from TEXT,                       -- active/resolved
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
-- 稽核檢視頁的主要篩選是 action 與 who，排序走 id DESC（主鍵最便宜）。
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action, id);
CREATE INDEX IF NOT EXISTS idx_audit_who ON audit_log (who, id);

-- 例外名單。這是整個系統唯一的靜音開關：即時規則引擎與期間掃描都讀它，
-- 命中就整筆丟棄。欄位變更一律走 store/migrate.py（見那個檔案的說明），
-- **絕不可**放進 _DERIVED_TABLES 丟掉重建 —— 那是人工核准紀錄。
CREATE TABLE IF NOT EXISTS allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner TEXT,
    purpose TEXT,
    reason TEXT,                            -- 為什麼建立這條例外（新 API 必填）
    rule_id TEXT,                           -- NULL = 全域（所有規則 + 期間掃描）；
                                            -- 有值 = 只對該規則，不影響掃描
    source_ip TEXT,                         -- 原始 IP，不是指紋（見 core/masking.py）
    endpoint TEXT,                          -- 規則層可再限縮到某端點；NULL/'' = 全部
    integration_type TEXT,
    -- 以下三欄零讀者，是 2026-08 之前的設計殘留。**不要新增讀者** ——
    -- token_fp 尤其：留一個沒人比對的憑證欄位在這裡，等於邀請下一個人
    -- 做出一個「存了看起來生效、實際完全沒作用」的輸入框。
    brand_scope TEXT,
    token_fp TEXT,
    expected_rate TEXT,
    valid_from TEXT,
    valid_to TEXT,
    approved_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    updated_by TEXT,
    -- 讀取端只認 '生效中'（見 store/allowlist.py 的常數）。這個 DEFAULT 是
    -- 死重量：新 API 一律顯式給值。改 DEFAULT 要重建整張表，不值得。
    status TEXT NOT NULL DEFAULT '待核准'
);
CREATE INDEX IF NOT EXISTS idx_allowlist_active ON allowlist (status, source_ip);
CREATE INDEX IF NOT EXISTS idx_allowlist_rule ON allowlist (rule_id);

-- 規則參數覆寫。YAML 是預設值，這張表是覆寫，rules/effective.py 是唯一的
-- 合成點。engine 每次評估重讀 → 改了下一個 tick 生效、不必重啟。
--
-- **每一欄 NULL = 沿用 YAML。** enabled = 0（停用）與 enabled IS NULL（沒覆寫）
-- 是兩件完全不同的事，讀取端一律 `if row[col] is not None`，
-- **不可以用 truthiness** —— 那會讓「停用」靜靜變成「沒覆寫」。
CREATE TABLE IF NOT EXISTS rule_overrides (
    rule_id TEXT PRIMARY KEY,
    enabled INTEGER,
    static_floor REAL,
    factor REAL,
    cooldown_minutes INTEGER,
    min_events INTEGER,                     -- new_source 規則的門檻（它沒有 threshold）
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    reason TEXT
);

-- 「哪一條 allowlist 抑制了什麼」。抑制原本是整筆丟棄、只在 log 留一行，
-- 於是沒有人看得出這個盲區實際遮掉了多少東西。有保留期限（見
-- settings.yaml 的 allowlist.suppression_retention_days），由每日排程修剪。
CREATE TABLE IF NOT EXISTS rule_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,                       -- 台北牆鐘（寫入時刻）
    allowlist_id INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    entity_label TEXT NOT NULL,             -- 被抑制的對象（帳號 · IP）
    metric REAL,
    threshold REAL,
    window_start TEXT,
    window_end TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_suppr_time ON rule_suppressions (at);
CREATE INDEX IF NOT EXISTS idx_rule_suppr_entry ON rule_suppressions (allowlist_id, at);

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

-- 期間異常掃描（sweep）。與 events 刻意分開：events 是即時規則的狀態機
-- （去重、cooldown、resolved），sweep 是回溯調查的一次性快照，沒有生命週期。
-- 存檔的目的是讓報告可重看、可跨區間比對；落盤一律 fingerprint。
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_no TEXT UNIQUE NOT NULL,          -- SWEEP-001
    range_start TEXT NOT NULL,              -- 台北牆鐘
    range_end TEXT NOT NULL,
    include_api_probe INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    duration_ms INTEGER,
    summary_json TEXT NOT NULL,             -- report.build() 的 summary
    limitations_json TEXT NOT NULL,         -- 可信度限制（產出當下的事實，不可事後重算）
    probes_json TEXT NOT NULL,
    narrative_md TEXT,                      -- LLM 敘事草稿（可為 NULL）
    narrative_model TEXT,
    narrative_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sweeps_time ON sweeps (created_at);

CREATE TABLE IF NOT EXISTS sweep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_no TEXT NOT NULL,
    rank INTEGER NOT NULL,
    entity TEXT NOT NULL,           -- 原始帳號或 IP（不再是指紋）
    entity_kind TEXT NOT NULL,              -- actor / src
    risk_level TEXT NOT NULL,               -- 極高/高/中高/中/中低
    score REAL NOT NULL,
    single_signal INTEGER NOT NULL DEFAULT 0,
    finding_json TEXT NOT NULL              -- 完整 finding（含 hits 與 evidence）
);
CREATE INDEX IF NOT EXISTS idx_sweep_findings ON sweep_findings (sweep_no, rank);
CREATE INDEX IF NOT EXISTS idx_sweep_findings_entity ON sweep_findings (entity);

-- 來源情報。**只存 fingerprint 與分類，不存原始 IP** —— 維持「系統沒有還原能力」
-- 的保證。分類由離線的雲端 IP 範圍檔與人工清單比對而來（見 console/intel）。
CREATE TABLE IF NOT EXISTS ip_intel (
    src TEXT PRIMARY KEY,                   -- 原始來源 IP（不再是指紋，見 core/masking.py）
    source_type TEXT NOT NULL,              -- hosting/vpn/residential/private/forged/unknown
    org TEXT,
    country TEXT,
    note TEXT,
    first_seen TEXT,
    last_seen TEXT,
    classified_at TEXT NOT NULL
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

-- 敏感路由清單。R05（非上班時間敏感操作）與期間掃描的 P03 探針共用同一份。
--
-- 原本寫在 config/settings.yaml，而 R05 的 SQL 裡還有第二份寫死的副本
-- （由 tests/test_sensitive_routes_consistency.py 綁著）。搬進 DB 是為了能從
-- 後台編輯，而 **移除一條就是製造盲區** —— 跟 allowlist 一樣必須能回答
-- 「這條是誰拿掉的、為什麼」，所以逐列留痕而不是一列存一份 JSON。
--
-- settings.yaml 的那份從此只是**首次播種的種子**（見 migrate.seed_after_schema）。
-- 播種之後改 YAML 沒有任何作用。
--
-- PRIMARY KEY 寫在 CREATE TABLE 裡是安全的，因為這是全新表 —— CLAUDE.md 警告的
-- 是「對既有表 ALTER TABLE 加不了約束」與「既有重複資料讓 CREATE UNIQUE INDEX
-- 失敗」那兩個坑，兩者都不適用。
CREATE TABLE IF NOT EXISTS sensitive_routes (
    route      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    removed_by TEXT,
    removed_at TEXT
);
"""


# 純衍生資料表：內容全部可以從 ClickHouse 重算，欄位改名時直接丟掉重建即可。
# 這是 `CREATE TABLE IF NOT EXISTS` 的例外處理 —— 既有 DB 的舊欄位不會自動改名，
# 而讀取端已改用新名稱，不處理就是「查一個不存在的欄位」。
#
# 只有衍生表能這樣做。events / cases / audit_log / allowlist / rule_overrides
# 是人工產出或有稽核意義的資料，欄位變更一律走 store/migrate.py（見 CLAUDE.md）。
#
# rule_suppressions 是機器產出的，但它是**歷史紀錄**而非可重算的衍生資料
# （抑制發生過就是發生過，重跑得不到當時的 metric），所以也不放進來。
_DERIVED_TABLES = {
    # 表名 → (必須存在的欄位, 重建方式)
    "ip_intel": "src",           # 舊版是 src_fp；重建：console.intel.refresh
    "sweep_findings": "entity",  # 舊版是 entity_fp；重跑掃描即可
}


def _drop_stale_derived(conn: sqlite3.Connection) -> None:
    for table, required_column in _DERIVED_TABLES.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue                      # 表還不存在，_SCHEMA 會建
        columns = {r[1] for r in rows}
        if required_column not in columns:
            conn.execute(f"DROP TABLE {table}")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _drop_stale_derived(conn)
        # **必須在 _SCHEMA 之前。** _SCHEMA 裡的 CREATE INDEX 引用的是遷移**之後**
        # 的欄位名（idx_allowlist_active 用 source_ip），在還沒改名的舊 DB 上會
        # 直接 `no such column`。而這個例外發生在 get_conn() 裡 ——
        # 走到 DB 的請求全部 500、排程器 thread 拿不到連線，
        # 而 /healthz 不碰 DB、照樣回 200，部署看起來成功。
        #
        # 反過來（migrate 在後）沒有任何好處：migrate 對不存在的表一律跳過，
        # 所以全新的 DB 不需要「先讓 _SCHEMA 把表建起來」。
        migrate.apply(conn)
        conn.executescript(_SCHEMA)
        # **在 _SCHEMA 之後**：表是 _SCHEMA 建的，而 migrate.apply() 依規定在
        # _SCHEMA 之前（見上面的註解與 migrate.seed_after_schema 的說明）。
        migrate.seed_after_schema(conn)
        conn.commit()
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
