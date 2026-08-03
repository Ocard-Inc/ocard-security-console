"""allowlist 一列 → 給前端的公開形狀。

抽成獨立模組是因為**兩個** router 需要它（Allowlist 頁與規則詳細頁），
而少了它的那一邊會直接產生誤導：前端的 AllowlistChip 依 `effective` 判斷顏色與
文案，拿到沒有這個欄位的 raw row 時會把一筆生效中的條目顯示成「不生效」。

`effective` **一律由後端算**，而且以 `allowlist.active_entries()` 的 SQL 當唯一
真相（不在 Python 再寫一套同樣的日期比較）—— 兩套邏輯遲早會漂移，而漂移的
症狀是畫面說「生效中」而引擎看不到它，或反過來。
"""
from __future__ import annotations

from console.core import timewin
from console.store import allowlist


def public_rows(rows: list[dict], *, rules: dict,
                suppressions: dict | None = None) -> list[dict]:
    """`rules` 是 rule_id → Rule；`suppressions` 是 allowlist_id → {count, last_at}。"""
    active_ids = {e.id for e in allowlist.active_entries()}
    suppr = suppressions or {}
    return [_one(r, active_ids=active_ids, rules=rules, suppr=suppr) for r in rows]


def public_row(row: dict, *, rules: dict, suppressions: dict | None = None) -> dict:
    return public_rows([row], rules=rules, suppressions=suppressions)[0]


def _one(row: dict, *, active_ids: set[int], rules: dict, suppr: dict) -> dict:
    rid = row["rule_id"]
    is_active = row["id"] in active_ids
    days = None
    if row["valid_to"]:
        try:
            days = (timewin.parse(row["valid_to"]) - timewin.taipei_now()).days
        except ValueError:
            days = None
    note = None
    if row["status"] != allowlist.STATUS_ACTIVE:
        note = f"狀態為「{row['status']}」，不生效"
    elif not is_active and row["valid_to"]:
        note = f"已於 {row['valid_to']} 到期"
    elif not is_active:
        note = "尚未生效（生效時間還沒到）"
    s = suppr.get(row["id"], {})
    return {
        **row,
        "scope": "global" if rid is None else "rule",
        "rule_name": rules[rid].name if rid in rules else None,
        # 規則改名或移除之後，條目會靜靜變成無效 —— 要說出來
        "rule_missing": rid is not None and rid not in rules,
        "effective": is_active,
        "effective_note": note,
        "days_to_expiry": days,
        # seed 播種的舊列沒有到期日，那是永久盲區，清單上要看得出來
        "expiry_missing": not row["valid_to"],
        "seeded": row["approved_by"] == "intel.refresh",
        "suppressed_7d": s.get("count", 0),
        "last_suppressed_at": s.get("last_at"),
    }
