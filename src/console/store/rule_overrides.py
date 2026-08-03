"""規則參數覆寫的讀寫。YAML 是預設值，這張表是覆寫。

**每一欄 NULL = 沿用 YAML。** `enabled = 0`（停用）與 `enabled IS NULL`
（沒覆寫）是兩件完全不同的事 —— 讀取端一律 `if value is not None`，
不可以用 truthiness，否則「停用」會靜靜變成「沒覆寫」而規則繼續跑。

覆寫值等於 YAML 原值時要**清掉那一欄的覆寫**（見 rules/effective.py 的
`prune()`），不要存冗餘覆寫：畫面上的「已覆寫」標記必須誠實，而且日後有人
改了 YAML，一個「剛好等於舊值」的覆寫會把規則凍結在舊值上。
"""
from __future__ import annotations

from console.core import timewin
from console.store import db

# 可覆寫的欄位。static_floor / factor 屬於嵌套的 Threshold，
# min_events 是 new_source 規則的門檻（它沒有 Threshold）。
FIELDS = ("enabled", "static_floor", "factor", "cooldown_minutes", "min_events")


def all_overrides() -> dict[str, dict]:
    """rule_id → {欄位: 值}（只含非 NULL 的欄位）+ 三個中介資料鍵。"""
    out: dict[str, dict] = {}
    for row in db.rows("SELECT * FROM rule_overrides"):
        ov = {f: row[f] for f in FIELDS if row[f] is not None}
        if not ov:
            continue                     # 只剩中介資料的空殼，視同沒有覆寫
        ov["_updated_at"] = row["updated_at"]
        ov["_updated_by"] = row["updated_by"]
        ov["_reason"] = row["reason"]
        out[row["rule_id"]] = ov
    return out


def get(rule_id: str) -> dict | None:
    return db.one("SELECT * FROM rule_overrides WHERE rule_id = ?", (rule_id,))


def put(rule_id: str, values: dict, *, who: str, reason: str) -> None:
    """寫入覆寫。values 的鍵必須是 FIELDS 的子集；值為 None 表示清掉該欄位。

    全部欄位都變成 None 時直接刪列 —— 留一個空殼會讓「已覆寫」的判斷
    多一種狀態，而它與「沒有覆寫」在語意上完全相同。
    """
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError(f"未知的覆寫欄位：{sorted(unknown)}")

    existing = get(rule_id) or {}
    # 沒提到的欄位保留原本的覆寫；提到但給 None 的欄位清掉。
    merged = {f: (values[f] if f in values else existing.get(f)) for f in FIELDS}
    if all(v is None for v in merged.values()):
        delete(rule_id)
        return

    now = timewin.fmt(timewin.taipei_now())
    cols = list(FIELDS)
    with db.tx() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO rule_overrides"
            f" (rule_id, {','.join(cols)}, updated_at, updated_by, reason)"
            f" VALUES (?, {','.join('?' * len(cols))}, ?, ?, ?)",
            tuple([rule_id] + [merged[f] for f in cols] + [now, who, reason]))


def delete(rule_id: str) -> bool:
    with db.tx() as conn:
        return conn.execute(
            "DELETE FROM rule_overrides WHERE rule_id = ?", (rule_id,)).rowcount > 0
