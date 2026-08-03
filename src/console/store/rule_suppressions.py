"""「哪一條 allowlist 遮掉了什麼」的歷史紀錄。

沒有這張表的話，抑制可見化只能做到「現在有哪些例外生效」，看不到「它實際
抑制了什麼」—— 而後者才是判斷一條例外該不該續期的依據。

**`measured_since` 是必填的呈現資訊。** 表剛上線時是空的，「0 次」必須渲染成
「自 X 起沒有抑制紀錄（此統計 X 上線）」而不是「從未抑制」。把「沒有資料」
說成「沒有發生」正是這個專案一再警告的錯誤。
"""
from __future__ import annotations

from datetime import timedelta

from console.core import timewin
from console.core.config import settings
from console.rules.model import Suppression
from console.store import db


def record_many(items: list[Suppression], *, at: str | None = None) -> int:
    """一個交易、一次 executemany。engine 不逐列寫 SQLite。"""
    if not items:
        return 0
    now = at or timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO rule_suppressions"
            " (at, allowlist_id, rule_id, source_ip, entity_label, metric, threshold,"
            "  window_start, window_end)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(now, s.allowlist_id, s.rule_id, s.source_ip, s.entity_label,
              s.metric, s.threshold, s.window_start, s.window_end) for s in items])
    return len(items)


def measured_since() -> str | None:
    """最早一筆紀錄的時間。None = 這張表還是空的（不是「從未抑制」）。"""
    row = db.one("SELECT MIN(at) AS m FROM rule_suppressions")
    return row["m"] if row and row["m"] else None


def counts_by_entry(days: int = 7) -> dict[int, dict]:
    """allowlist_id → {count, last_at}。一趟 GROUP BY，不是 N+1。"""
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=days))
    return {
        r["allowlist_id"]: {"count": r["n"], "last_at": r["last_at"]}
        for r in db.rows(
            "SELECT allowlist_id, COUNT(*) AS n, MAX(at) AS last_at"
            " FROM rule_suppressions WHERE at >= ? GROUP BY allowlist_id", (since,))
    }


def counts_by_rule(days: int = 28) -> dict[str, int]:
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=days))
    return {r["rule_id"]: r["n"] for r in db.rows(
        "SELECT rule_id, COUNT(*) AS n FROM rule_suppressions"
        " WHERE at >= ? GROUP BY rule_id", (since,))}


def recent_for_entry(allowlist_id: int, *, days: int = 28,
                     limit: int = 100) -> list[dict]:
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=days))
    return db.rows(
        "SELECT * FROM rule_suppressions WHERE allowlist_id = ? AND at >= ?"
        " ORDER BY id DESC LIMIT ?", (allowlist_id, since, limit))


def recent_for_rule(rule_id: str, *, days: int = 28, limit: int = 50) -> list[dict]:
    since = timewin.fmt(timewin.taipei_now() - timedelta(days=days))
    return db.rows(
        "SELECT * FROM rule_suppressions WHERE rule_id = ? AND at >= ?"
        " ORDER BY id DESC LIMIT ?", (rule_id, since, limit))


def prune(days: int | None = None) -> int:
    """刪掉保留期限之外的紀錄。每日排程呼叫。

    每個 tick、每個被抑制的命中一列。目前量很小，但它是無上限的 ——
    尤其 new_source 的抑制現在每五分鐘都會重新發生（見 engine 的順序說明）。
    """
    keep = days if days is not None else settings().get(
        "allowlist", {}).get("suppression_retention_days", 90)
    cutoff = timewin.fmt(timewin.taipei_now() - timedelta(days=keep))
    with db.tx() as conn:
        return conn.execute(
            "DELETE FROM rule_suppressions WHERE at < ?", (cutoff,)).rowcount
