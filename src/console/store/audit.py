"""操作稽核：所有查詢、匯出、狀態變更寫入 audit_log。"""
from __future__ import annotations

import hashlib

from console.core import masking, timewin
from console.store import db

# 可以用來篩選的欄位。**這是白名單而不是註解。**
#
# recent() 把篩選欄名直接 f-string 進 SQL（歷史寫法），所以呼叫端若寫成
# `recent(**request.query_params)` 就直接把使用者字串接進 WHERE 子句。
# route 那邊只用具名參數，但白名單要跟著會被誤用的**這個函式**，
# 而不是只在呼叫端擋：它是公開函式，第二個呼叫者不會知道那個 f-string 的危險。
# 這與 core/ch.py 對 identifier 的處理是同一個原則。
_FILTER_COLUMNS = frozenset({"who", "role", "action", "result", "case_no"})

# 自由文字欄位的長度上限。reason 現在會收「為什麼調整規則」「為什麼建立例外」
# 這類人工輸入，比原本的判定理由多。
_REASON_MAX = 1000


def query_hash(text: str) -> str:
    """給人比對「是同一個查詢嗎」用的短碼。

    只有 6 個 hex 字元（24 bit）—— **不是防碰撞的識別碼**，UI 不要暗示它唯一。
    """
    return "qh_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:6].upper()


def record(
    *,
    who: str,
    role: str,
    action: str,
    target: str,
    result: str = "成功",
    query_text: str | None = None,
    time_range: str | None = None,
    row_count: int | None = None,
    duration_ms: int | None = None,
    case_no: str | None = None,
    reason: str | None = None,
) -> str | None:
    qh = query_hash(query_text) if query_text else None
    # reason 是人工輸入，而 audit_log 會被 /api/audit 原樣回到畫面上。
    # 有人在理由裡打「客戶 0912345678 反映」的話，手機號就落進磁碟上的 SQLite
    # 並回到所有主控台使用者眼前 —— 而 tests/test_masking_audit.py 會在
    # **別人打字之後的某一天**失敗，看起來像不穩定的測試。
    if reason is not None:
        reason = masking.scrub_text(reason, max_len=_REASON_MAX)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log (at, who, role, action, target, query_hash,"
            " time_range, row_count, duration_ms, case_no, result, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (timewin.fmt(timewin.taipei_now()), who, role, action, target, qh,
             time_range, row_count, duration_ms, case_no, result, reason))
    return qh


def _where(filters: dict, *, start: str | None, end: str | None,
           target: str | None, before_id: int | None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list[object] = []
    for col, val in filters.items():
        if col not in _FILTER_COLUMNS:
            raise ValueError(
                f"未知的稽核篩選欄位 {col!r}（允許：{sorted(_FILTER_COLUMNS)}）")
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if start:
        clauses.append("at >= ?")
        params.append(start)
    if end:
        clauses.append("at <= ?")
        params.append(end)
    if target:
        clauses.append("target LIKE ?")
        params.append(f"%{target}%")
    if before_id:
        clauses.append("id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def recent(limit: int = 100, *, start: str | None = None, end: str | None = None,
           target: str | None = None, before_id: int | None = None,
           **filters) -> list[dict]:
    """依條件取稽核紀錄，新的在前。

    分頁走 keyset（`before_id`）而不是 OFFSET —— 排序是 id DESC，主鍵最便宜，
    而 audit_log 只增不減。
    """
    where, params = _where(filters, start=start, end=end,
                           target=target, before_id=before_id)
    params.append(limit)
    return db.rows(
        f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", tuple(params))


def count(*, start: str | None = None, end: str | None = None,
          target: str | None = None, **filters) -> int:
    """符合條件的總筆數。

    畫面上必須寫出「顯示最近 N 筆，共 M 筆」：默默截斷會讓稽查人員的結論
    變成「這段時間就這些操作」。
    """
    where, params = _where(filters, start=start, end=end,
                           target=target, before_id=None)
    row = db.one(f"SELECT COUNT(*) AS n FROM audit_log{where}", tuple(params))
    return int(row["n"]) if row else 0


def distinct_actions() -> list[str]:
    """實際存在於資料裡的 action。

    篩選下拉一律用這個，**不要寫死清單** —— 設計稿那份寫死的動作名（「開啟遮罩
    明細」「執行 SQL」）與程式實際寫入的字串不一致，照抄的結果是一半選項篩出
    0 筆、而真正存在的動作篩不到。
    """
    return [r["action"] for r in db.rows(
        "SELECT DISTINCT action FROM audit_log ORDER BY action")]


def oldest_at() -> str | None:
    row = db.one("SELECT MIN(at) AS m FROM audit_log")
    return row["m"] if row and row["m"] else None
