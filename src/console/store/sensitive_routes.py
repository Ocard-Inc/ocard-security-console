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


# add() 的判定結果第三種：這條路由已經生效中，什麼都沒改。刻意是字串常數
# （同 disable() 下面那組），不是拿 "reactivated" 硬套 —— 呼叫端要能分辨
# 「真的把一條停用的路由改回生效」與「本來就生效中，這次呼叫什麼都沒動」，
# 才能決定回 200 還是 409。
ADD_ALREADY_ACTIVE = "already_active"


def add(route: str, *, who: str, reason: str) -> str:
    """新增、重新啟用一條已停用的路由，或偵測到它已經生效中。

    回 "created" / "reactivated" / `ADD_ALREADY_ACTIVE` 三者之一。
    **這三個字串的既有意義不可以變**——`"created"` 與 `"reactivated"` 已經被
    呼叫端與測試依賴，這裡只是多加第三種結果，不是重新定義前兩種。

    重新啟用要**清掉** `removed_by` / `removed_at`：留著的話畫面上會同時顯示
    「生效中」與「由某人於某時停用」，讀起來像兩件矛盾的事。

    **重新啟用的 UPDATE 只能套用在目前是「已停用」的列
    （`WHERE route = ? AND status = ?`，比對 `STATUS_DISABLED`），不是
    「INSERT OR IGNORE 失敗就代表可以重新啟用」。** 早期版本反過來：INSERT
    失敗（代表路由已存在，不論現在是生效中還是已停用）就無條件 UPDATE 成
    生效中並改寫 `added_by`/`added_at`/`reason`。對一條**本來就生效中**的
    路由重新 POST 一次（規則頁的新增表單是自由輸入 + datalist，沒有東西
    擋得住這件事）—— 那次 UPDATE 一樣會成功執行，把種子列的
    `added_by='seed'` / `reason='settings.yaml 初始清單'`，或前一個人的核准
    紀錄，靜靜改寫成這次呼叫的值；而 API 端點還會照樣寫一筆「恢復敏感路由」
    的 audit_log 與 ops 訊息，宣稱一件沒有發生過的事 —— 這條路由根本沒被
    停用過，沒有東西被恢復。現在 UPDATE 帶上 `status = 已停用` 的條件之後，
    對生效中的路由重複 POST 會讓這顆 UPDATE 的 `rowcount == 0`，呼叫端據此
    回 `ADD_ALREADY_ACTIVE`，provenance 三個欄位完全不會被寫入。

    **existence 檢查與寫入是同一顆 `INSERT OR IGNORE`，不是「先 SELECT 再依結果
    分支」。** 早期版本先呼叫 `get()` 判斷存不存在、再各自 INSERT / UPDATE ——
    SELECT 不會取寫入鎖，兩個併發呼叫可以同時看到「不存在」，都跑進 INSERT
    分支，其中一個會撞 `route` 的 PRIMARY KEY 而拋 `sqlite3.IntegrityError`
    （呼叫端沒接、直接 500）。這裡的 API 端點是唯一真的會併發寫入的地方
    （boot-time 播種只用一次性的 `INSERT OR IGNORE`，不受影響）。
    現在 `INSERT OR IGNORE` 本身就會啟動寫入交易並拿到 SQLite 的寫入鎖 ——
    兩個併發呼叫必然序列化，後執行的那個看到的一定是「已存在」（前一個已
    commit），走 UPDATE 分支，不會有第二次 INSERT 嘗試，也就不會有
    IntegrityError。這個既有的鎖定順序沒有變 —— 新加的只是 UPDATE 的 WHERE
    子句多一個條件，不影響序列化本身：兩個併發呼叫在同一條已停用的路由上
    重新啟用，仍然只有先執行的那個會讓 `rowcount > 0`，後執行的看到的是
    「已經生效中」（前一個已 commit），回 `ADD_ALREADY_ACTIVE`，不會有兩次
    「reactivated」。
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
        cur = conn.execute(
            "UPDATE sensitive_routes SET status = ?, added_by = ?, added_at = ?,"
            " reason = ?, removed_by = NULL, removed_at = NULL"
            " WHERE route = ? AND status = ?",
            (STATUS_ACTIVE, who, now, reason, route, STATUS_DISABLED))
        if cur.rowcount > 0:
            return "reactivated"
    return ADD_ALREADY_ACTIVE


# disable() 的判定結果。刻意是字串常數（同 add() 的 "created"/"reactivated"
# 慣例），不是 bool ——「拒絕」有兩種完全不同的原因（本來就沒生效 vs 是最後一條），
# 呼叫端要能分別顯示 404 / 409(已停用) / 409(最後一條) 三種不同訊息，
# 一個 bool 只能分兩類。
DISABLE_OK = "disabled"
DISABLE_NOT_FOUND = "not_found"
DISABLE_ALREADY_DISABLED = "already_disabled"
DISABLE_LAST_ACTIVE = "last_active"


def disable(route: str, *, who: str) -> str:
    """停用（不刪列）。回傳上面四個常數之一。

    **「還有沒有別的生效中路由」與「真的執行停用」是同一顆 UPDATE 的 WHERE
    子句，不是「呼叫端先讀 active_count() 判斷、再呼叫這裡執行」。**
    2026-08 review 抓到的 race：早期版本把這兩件事拆成兩次獨立的 DB 往返，
    中間沒有任何鎖 —— 兩個併發的 DELETE 打**不同**路由，可能都在各自的
    `active_count()` 讀到同一個「還有 2 條」、都通過「>1」檢查、都真的停用，
    清單被清空。而這正是這個檢查存在的理由：空清單在 ClickHouse 是 `IN ()`，
    不報錯、靜靜回 0 筆 —— R05 靜靜不再命中任何東西（畫面上仍顯示啟用中），
    掃描的 P02/P03 也靜靜被跳過。

    現在用一顆 UPDATE 同時做兩件事：

    ```sql
    UPDATE sensitive_routes SET status = ?, removed_by = ?, removed_at = ?
     WHERE route = ? AND status = ?
       AND (SELECT COUNT(*) FROM sensitive_routes WHERE status = ?) > 1
    ```

    這顆語句本身就會啟動寫入交易並取得 SQLite 的寫入鎖，所以兩個併發呼叫在
    這裡必然序列化 —— 後執行的那個看到的 `(SELECT COUNT(*) ...)` 一定是前一個
    已經 commit 之後的真實計數，不會有「兩者都看到還有 2 條」的情況。
    `rowcount == 0` 時**不代表發生了併發衝突**，可能只是路由不存在或早就停用，
    所以另外用一次 `SELECT` 判斷原因給呼叫端顯示正確的訊息 —— 那次 `SELECT`
    純粹是診斷用（決定 404 還是哪一種 409），不影響上面 UPDATE 已經保證好的
    不變量本身。
    """
    now = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE sensitive_routes SET status = ?, removed_by = ?, removed_at = ?"
            " WHERE route = ? AND status = ?"
            " AND (SELECT COUNT(*) FROM sensitive_routes WHERE status = ?) > 1",
            (STATUS_DISABLED, who, now, route, STATUS_ACTIVE, STATUS_ACTIVE))
        if cur.rowcount > 0:
            return DISABLE_OK
        row = conn.execute(
            "SELECT status FROM sensitive_routes WHERE route = ?", (route,)
        ).fetchone()
    if row is None:
        return DISABLE_NOT_FOUND
    return DISABLE_ALREADY_DISABLED if row["status"] != STATUS_ACTIVE else DISABLE_LAST_ACTIVE
