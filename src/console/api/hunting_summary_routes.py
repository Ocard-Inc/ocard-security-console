"""提供給外部偵測器的去識別化安全事件摘要。

此端點刻意不是 Console UI 的事件 API：它只輸出明確 allowlist 的統計欄位，
不會洩漏帳號、IP、token、payload、判讀文字或事件 context。
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query

from console.auth.roles import CurrentUser, current_user, guard
from console.core import timewin
from console.core.config import fp_secret
from console.store import db

router = APIRouter()

_EVENT_FIELDS = (
    "rule_id", "rule_name", "severity", "source_key", "metric_value", "threshold",
    "baseline_median", "baseline_p95", "multiple", "brands", "first_seen", "last_seen",
    "peak_value", "hit_count", "status", "entity_key",
)


def _event_fp(entity_key: object) -> str:
    """為可能含敏感資料的去重鍵再做一次不可逆的對外指紋化。"""
    text = str(entity_key or "")
    mac = hmac.new(fp_secret(), f"hunting-event:{text}".encode("utf-8"), hashlib.sha256)
    return "event_" + mac.hexdigest()[:12].upper()


def _event_summary(event: dict) -> dict:
    """將 SQLite event 轉為對外固定 allowlist，禁止加入任意 context。"""
    return {
        "event_fp": _event_fp(event["entity_key"]),
        "rule_id": event["rule_id"],
        "rule_name": event["rule_name"],
        "severity": event["severity"],
        "source": event["source_key"],
        "metric": event["metric_value"],
        "threshold": event["threshold"],
        "median": event["baseline_median"],
        "p95": event["baseline_p95"],
        "multiple": event["multiple"],
        "brands": event["brands"],
        "first_seen": event["first_seen"],
        "last_seen": event["last_seen"],
        "peak": event["peak_value"],
        "hit_count": event["hit_count"],
        "status": event["status"],
    }


@router.get("/hunting-summary")
def hunting_summary(
    since: str = Query(..., description="YYYY-MM-DD[ HH:MM[:SS]]"),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """回傳安全獵捕所需的遮罩化 Console 摘要。"""
    guard(user, "view_overview")
    try:
        start = timewin.fmt(timewin.parse(since))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="since 格式須為 YYYY-MM-DD[ HH:MM[:SS]]") from exc

    columns = ", ".join(_EVENT_FIELDS)
    events = db.rows(
        f"SELECT {columns} FROM events WHERE last_seen >= ? "
        "ORDER BY last_seen DESC, id DESC LIMIT 500",
        (start,),
    )
    intel = db.rows(
        "SELECT source_type, COUNT(*) AS count FROM ip_intel GROUP BY source_type ORDER BY source_type"
    )
    heartbeats = db.rows(
        "SELECT key, last_ok, consecutive_failures FROM heartbeat "
        "WHERE key IN ('five_min', 'daily') ORDER BY key"
    )
    return {
        "generated_at": timewin.fmt(timewin.taipei_now()),
        "events": [_event_summary(event) for event in events],
        "intel_counts": intel,
        "health": heartbeats,
    }
