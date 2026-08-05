"""既有 SQLite 的欄位遷移。

`db._SCHEMA` 是 `CREATE TABLE IF NOT EXISTS`，所以它**只能建新表與新索引**；
對已經存在的表，整段 CREATE 被跳過，新欄位永遠不會出現。純衍生表靠
`db._DERIVED_TABLES` 丟掉重建，但 allowlist / events / audit_log / rule_overrides
是人工核准或有稽核意義的資料 —— 丟掉重建等於刪掉別人的核准與留痕。
剩下的唯一手段就是這個檔案。

**為什麼掛在 `db.get_conn()` 而不是一個一次性的 CLI**：部署流程是 push → build →
`update-container`（reset VM），**沒有任何步驟能插在新映像啟動之前跑 SQL**。
「先 SSH 遷移再部署」在這個拓樸下做不到。放在 get_conn() 就沒有「忘記跑遷移」
這個狀態，代價是每條新連線多幾個 PRAGMA。

**因此全程必須 idempotent，而且不可假設「只跑一次」**：db 的連線是 thread-local，
排程器 thread、FastAPI threadpool 的每條 thread、每個 CLI process 都會各跑一次。
兩條 thread 同時判斷「欄位不存在」再同時 ALTER 是真的會發生的，所以每個動作
各自吞掉「已經做過」那一類的 OperationalError。

**遷移後舊版程式碼會壞**（查 `source_fp` 得到 `no such column`）。那是大聲失敗、
可接受；但 state/monitor.db 一旦被新版開過就不能再退回舊版程式碼。
部署前先備份 `state/monitor.db`、`-wal`、`-shm` 三個檔（要在 process 停掉之後做，
WAL 才是一致的）。
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# (表, 欄位, 型別)。既有列一律 NULL —— ALTER TABLE ADD COLUMN 不能給非常數預設值。
_ADD_COLUMNS = (
    # NULL = 全域（所有規則 + 期間掃描）。既有列因此保持現行行為，
    # 不會因為上了新功能而悄悄縮小既有例外的範圍。
    ("allowlist", "rule_id", "TEXT"),
    ("allowlist", "reason", "TEXT"),
    ("allowlist", "created_at", "TEXT"),
    ("allowlist", "updated_at", "TEXT"),
    ("allowlist", "updated_by", "TEXT"),
    # 人工結案（status='closed'）。既有列一律 NULL，也就是「沒有人結案過」——
    # 那正是既有資料的事實，不需要回填。
    ("events", "closed_at", "TEXT"),
    ("events", "closed_by", "TEXT"),
    ("events", "closed_from", "TEXT"),
)

# (表, 舊名, 新名)。
# source_fp 存的是原始 IP，2026-08 的呈現政策變更之後名字就不對了
# （ip_intel.src_fp→src、sweep_findings.entity_fp→entity 都改過，只有 allowlist
# 因為不是衍生表而留著）。留著的代價是下一個人會寫出「先 masking.src() 再比對」
# 或反過來 —— 而抑制不命中是完全無徵兆的。
_RENAME_COLUMNS = (
    ("allowlist", "source_fp", "source_ip"),
)


def apply(conn: sqlite3.Connection) -> list[str]:
    """執行所有待辦的遷移。回傳實際做過的動作（供 log；沒事做就是空 list）。"""
    done: list[str] = []
    for table, old, new in _RENAME_COLUMNS:
        cols = _columns(conn, table)
        if not cols or old not in cols or new in cols:
            continue
        if _try(conn, f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"):
            done.append(f"{table}.{old} → {new}")
    for table, column, type_sql in _ADD_COLUMNS:
        cols = _columns(conn, table)
        if not cols or column in cols:
            continue
        if _try(conn, f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"):
            done.append(f"{table}.{column} 新增")
    done += _backfill(conn)
    if done:
        logger.info("SQLite 遷移：%s", "；".join(done))
    return done


def _backfill(conn: sqlite3.Connection) -> list[str]:
    """一次性的資料修正。每一條都以 WHERE 保證跑第二次不會有動作。"""
    done: list[str] = []
    if "created_at" not in _columns(conn, "allowlist"):
        return done

    # created_at 對既有列只能是 NULL（ADD COLUMN 不能給 now()）。用 valid_from
    # 近似 —— seed_allowlist() 有寫它。**刻意不用現在的時間回填**：那會宣稱一個
    # 假的建立時間，而稽核資料上的假時間比缺資料糟得多。回填不到的留 NULL，
    # API 回 null、前端顯示「未記錄」。
    n = conn.execute(
        "UPDATE allowlist SET created_at = valid_from"
        " WHERE created_at IS NULL AND valid_from IS NOT NULL").rowcount
    if n:
        done.append(f"allowlist.created_at 由 valid_from 回填 {n} 列")

    # 2026-08 政策變更前播種的指紋條目（src_XXXXXXXX）。比對的是原始 IP，
    # 所以這些條目永遠不會命中任何來源 —— 而畫面上它們是「生效中」，
    # 看起來有抑制、實際沒有。停用比留著誠實。
    n = conn.execute(
        "UPDATE allowlist SET status = '已停用',"
        " reason = COALESCE(reason, '2026-08 呈現政策變更前的舊指紋條目："
        "比對的是原始 IP，此條目永遠不會命中，已於遷移時自動停用')"
        " WHERE source_ip LIKE 'src\\_%' ESCAPE '\\' AND status <> '已停用'").rowcount
    if n:
        done.append(f"allowlist 舊指紋條目停用 {n} 列")
    return done


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """表的欄位集合；表不存在回空集合（_SCHEMA 會建它）。"""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _try(conn: sqlite3.Connection, sql: str) -> bool:
    """執行 DDL；「別人已經做過了」不算錯誤。"""
    try:
        conn.execute(sql)
        return True
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "duplicate column" in msg or "no such column" in msg:
            logger.debug("遷移已由其他連線完成：%s（%s）", sql, exc)
            return False
        raise


# 播種用的常數。`add()` 的呼叫端要能分辨「這是種子」與「這是人加的」。
SEED_BY = "seed"
SEED_REASON = "settings.yaml 初始清單"


def seed_after_schema(conn: sqlite3.Connection) -> list[str]:
    """把 settings.yaml 的敏感路由補進 `sensitive_routes`。

    **必須在 `db.executescript(_SCHEMA)` 之後呼叫，不可以放進 `apply()`。**
    表是由 `_SCHEMA` 建立的，而 `apply()` 依規定跑在 `_SCHEMA` **之前**
    （`_SCHEMA` 的 CREATE INDEX 引用遷移後的欄位名，反過來會在舊 DB 上
    `no such column`，而那個例外發生在 `get_conn()` 裡 → 走到 DB 的請求全部
    500、排程器拿不到連線，而 /healthz 不碰 DB 照樣回 200，部署看起來成功）。
    放進 `apply()` 的話它會對一張還不存在的表下 INSERT。

    **`INSERT OR IGNORE` 刻意不看 `status`。** 人工停用的路由不可以被下一次
    啟動悄悄復活 —— 同 `intel/refresh.seed_allowlist()` 的去重檢查。復活一條
    路由會讓 R05 與期間掃描重新看它，而使用者以為自己已經關掉了。

    與 `apply()` 一樣全程 idempotent：連線是 thread-local，每條 thread 與每個
    CLI process 都會各跑一次。
    """
    # 這裡才 import：migrate 由 db.get_conn() 呼叫，而 config/timewin 不 import
    # store，所以沒有循環。放在模組頂端也可以，放在函式內是為了讓「migrate 只
    # 依賴 sqlite3」這個既有性質在讀檔時仍然明顯。
    from console.core import timewin
    from console.core.config import settings

    routes = list(settings().get("sensitive_routes") or [])
    if not routes:
        return []
    now = timewin.fmt(timewin.taipei_now())
    before = conn.execute("SELECT count(*) FROM sensitive_routes").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO sensitive_routes"
        " (route, status, added_by, added_at, reason)"
        " VALUES (?, '生效中', ?, ?, ?)",
        [(r, SEED_BY, now, SEED_REASON) for r in routes])
    after = conn.execute("SELECT count(*) FROM sensitive_routes").fetchone()[0]
    if after > before:
        done = [f"sensitive_routes 播種 {after - before} 條"]
        logger.info("SQLite 播種：%s", "；".join(done))
        return done
    return []
