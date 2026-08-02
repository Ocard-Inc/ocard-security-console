"""操作稽核：所有查詢、匯出、狀態變更寫入 audit_log。"""
from __future__ import annotations

import hashlib

from console.core import timewin
from console.store import db


def query_hash(text: str) -> str:
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
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log (at, who, role, action, target, query_hash,"
            " time_range, row_count, duration_ms, case_no, result, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (timewin.fmt(timewin.taipei_now()), who, role, action, target, qh,
             time_range, row_count, duration_ms, case_no, result, reason))
    return qh


def recent(limit: int = 100, **filters) -> list[dict]:
    clauses, params = [], []
    for col, val in filters.items():
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.rows(f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", tuple(params))
