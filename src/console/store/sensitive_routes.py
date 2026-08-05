"""敏感路由清單的唯一讀寫入口。

兩個讀取端，都在**執行期**取值：`rules/engine.py`（R05 的 `%(sensitive_routes)s`）
與 `sweep/run.py`（P03 的同名參數）。所以從 UI 改完 R05 下一個 tick 生效、
期間掃描下一次執行生效，都不必重啟 server。

`config/settings.yaml` 的 `sensitive_routes` 只是**首次播種的種子**
（見 `store/migrate.seed_after_schema`）—— 播種之後改那個 YAML 沒有任何作用，
而且不會有錯誤訊息。要改清單一律走 UI 或直接改表。

**移除一條路由就是製造盲區**，所以這裡沒有 DELETE，只有停用：`audit_log` 裡的
route 必須永遠解得回一筆條目（同 allowlist）。
"""
from __future__ import annotations

from console.core import timewin
from console.store import db

STATUS_ACTIVE = "生效中"
STATUS_DISABLED = "已停用"

# 讀取端一律明列欄位，不可用 `row.get(col, default)`。「欄位不存在」與「值是
# NULL」在語意上會撞在一起 —— `removed_by` 的 NULL 是「沒有被停用過」，
# 欄位沒建成功時每一列都靜靜變成「沒被停用過」而畫面完全正常。
_COLUMNS = ("route", "status", "added_by", "added_at", "reason",
            "removed_by", "removed_at")
_SELECT = ", ".join(_COLUMNS)


def active() -> list[str]:
    """生效中的路由，已排序。這是兩支 SQL 實際吃到的清單。

    只選 `route`：這裡不需要其他欄位，而 `_SELECT` 是給 `all_rows()` / `get()`
    那種要回整列的呼叫端用的。排序讓清單顯示與 SQL 參數都穩定。
    """
    return [r["route"] for r in db.rows(
        "SELECT route FROM sensitive_routes WHERE status = ? ORDER BY route",
        (STATUS_ACTIVE,))]


def active_count() -> int:
    row = db.one("SELECT count(*) AS n FROM sensitive_routes WHERE status = ?",
                 (STATUS_ACTIVE,))
    return int((row or {}).get("n") or 0)


def disabled_count() -> int:
    """已停用的路由數。brief 的 Produces 清單沒列這個函式，但資安總覽的
    「目前有多少監測被關閉」橫幅（Task 7）需要它 —— 那條橫幅同時要能數
    allowlist 的抑制與這裡的停用路由，兩者都是人工關掉的監測範圍。"""
    row = db.one("SELECT count(*) AS n FROM sensitive_routes WHERE status = ?",
                 (STATUS_DISABLED,))
    return int((row or {}).get("n") or 0)


def all_rows() -> list[dict]:
    """完整清單（含已停用），生效中的排前面。給 API 與畫面用。"""
    return db.rows(
        f"SELECT {_SELECT} FROM sensitive_routes"
        f" ORDER BY status = ? DESC, route", (STATUS_ACTIVE,))


def get(route: str) -> dict | None:
    return db.one(f"SELECT {_SELECT} FROM sensitive_routes WHERE route = ?",
                  (route,))


def add(route: str, *, who: str, reason: str) -> str:
    """新增或重新啟用一條路由。回 "created" 或 "reactivated"。

    重新啟用要**清掉** `removed_by` / `removed_at`：留著的話畫面上會同時顯示
    「生效中」與「由某人於某時停用」，讀起來像兩件矛盾的事。

    **existence 檢查與寫入是同一顆 `INSERT OR IGNORE`，不是「先 SELECT 再依結果
    分支」。** 早期版本先呼叫 `get()` 判斷存不存在、再各自 INSERT / UPDATE ——
    SELECT 不會取寫入鎖，兩個併發呼叫可以同時看到「不存在」，都跑進 INSERT
    分支，其中一個會撞 `route` 的 PRIMARY KEY 而拋 `sqlite3.IntegrityError`
    （呼叫端沒接、直接 500）。這裡的 API 端點是唯一真的會併發寫入的地方
    （boot-time 播種只用一次性的 `INSERT OR IGNORE`，不受影響）。
    現在 `INSERT OR IGNORE` 本身就會啟動寫入交易並拿到 SQLite 的寫入鎖 ——
    兩個併發呼叫必然序列化，後執行的那個看到的一定是「已存在」（前一個已
    commit），走 UPDATE 分支，不會有第二次 INSERT 嘗試，也就不會有
    IntegrityError。
    """
    now = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sensitive_routes"
            " (route, status, added_by, added_at, reason)"
            " VALUES (?, ?, ?, ?, ?)",
            (route, STATUS_ACTIVE, who, now, reason))
        if cur.rowcount > 0:
            return "created"
        conn.execute(
            "UPDATE sensitive_routes SET status = ?, added_by = ?, added_at = ?,"
            " reason = ?, removed_by = NULL, removed_at = NULL WHERE route = ?",
            (STATUS_ACTIVE, who, now, reason, route))
    return "reactivated"


def disable(route: str, *, who: str) -> bool:
    """停用（不刪列）。回傳是否真的改到一列。

    **呼叫端必須先擋「這是最後一條」** —— 空清單在 ClickHouse 是
    `IN ()` → 實測不報錯、靜靜回 0 筆，也就是 R05 靜靜失效。
    擋在 API 層（`active_count()`），因為那裡才回得了 409。
    """
    now = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        return conn.execute(
            "UPDATE sensitive_routes SET status = ?, removed_by = ?, removed_at = ?"
            " WHERE route = ? AND status = ?",
            (STATUS_DISABLED, who, now, route, STATUS_ACTIVE)).rowcount > 0
