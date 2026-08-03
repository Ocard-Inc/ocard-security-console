"""操作稽核的檢視端點。

`store/audit.recent()` 早就寫好了但一直沒有呼叫者 —— 留痕存在卻沒人看得到。
而 Allowlist 與規則覆寫的約束靠的正是「事後查得到」，所以這一頁不是附加功能。

**這個端點不得呼叫 ClickHouse。** 純 SQLite 就夠了。
它是同步 `def`（同全站其餘端點，2026-08 統一改過來）—— 這樣哪天真的有人
在這裡 join 什麼豐富顯示，也不會因為一個阻塞查詢就凍住整個事件迴圈。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from console.auth.roles import CurrentUser, current_user, guard
from console.core import timewin
from console.store import audit

router = APIRouter()


def _parse_at(value: str | None, label: str) -> str | None:
    if not value:
        return None
    try:
        return timewin.fmt(timewin.parse(value))
    except ValueError as exc:
        raise HTTPException(400, f"{label}：{exc}") from exc


@router.get("/audit")
def list_audit(
    # **全部是具名參數。** 絕不可以寫成 `audit.recent(**request.query_params)`：
    # recent() 把篩選欄名 f-string 進 SQL，而 Python 允許用
    # `f(**{"任意字串": v})` 把非識別字的鍵送進 **kwargs。
    # recent() 自己另有欄名白名單（第二層防護）。
    who: str | None = None,
    role: str | None = None,
    action: str | None = None,
    result: str | None = None,
    case_no: str | None = None,
    target: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None, ge=1),
    user: CurrentUser = Depends(current_user),
) -> dict:
    guard(user, "view_audit")
    filters = {"who": who, "role": role, "action": action,
               "result": result, "case_no": case_no}
    applied = {k: v for k, v in {**filters, "target": target,
                                 "start": start, "end": end}.items() if v}
    start_at = _parse_at(start, "開始時間")
    end_at = _parse_at(end, "結束時間")
    try:
        rows = audit.recent(limit, start=start_at, end=end_at, target=target,
                            before_id=before_id, **filters)
        total = audit.count(start=start_at, end=end_at, target=target, **filters)
    except ValueError as exc:                 # 白名單外的欄名 → 使用者錯誤
        raise HTTPException(400, str(exc)) from exc

    # 刻意不寫 audit：沒有角色分級（見 auth/roles.py），能看的人本來就都能看，
    # 記了只會讓稽核表自我指涉，而且每次翻頁都把真正的操作推出第一頁。
    # 日後若恢復分級，這裡加一行 audit.record(action="查看操作稽核") 即可。
    return {
        "rows": rows,
        "total": total,
        "returned": len(rows),
        "has_more": len(rows) == limit,
        "next_before_id": rows[-1]["id"] if rows else None,
        # 空字串的篩選條件會被當成「不篩選」。畫面必須看得出「篩選後 0 筆」與
        # 「篩選沒生效」的差別，所以把實際套用的條件回去。
        "applied_filters": applied,
        # 下拉選項來自資料本身，不是寫死清單（見 audit.distinct_actions）
        "actions": audit.distinct_actions(),
        "oldest_at": audit.oldest_at(),
        "notes": [
            # query_text 不落盤，只存 hash（audit.record）。空欄位會讓人以為
            # 資料掉了，所以要明講。
            "查詢內容本身不落盤，只保留 6 位數的比對碼（可看出是否為同一個查詢，"
            "不是唯一識別碼）。",
            "audit_log 沒有輪替機制，保留全部歷史。",
            "案件管理尚未實作，案件欄目前一律為空。",
        ],
    }
