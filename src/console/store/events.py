"""事件持久化與去重狀態機。

去重鍵 = (rule_id, entity_key)：
1. 無 active 事件 → 建新事件（EVT-XXXX），回報「新事件」
2. cooldown 內再命中 → 只累計 hit_count / peak / last_seen
3. 超過 cooldown 仍持續 → 回報「持續中」（升級通知）並重置 last_notified
4. 連續 resolve_after 個 tick 未命中 → 標 resolved，回報「已恢復」
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from console.core import timewin
from console.core.config import settings
from console.rules.model import Finding
from console.store import db

logger = logging.getLogger(__name__)

NEW = "new"
ONGOING = "ongoing"
RESOLVED = "resolved"


def apply_findings(findings: list[Finding], tick_at: datetime) -> list[dict]:
    """把本 tick 的 findings 併入 events 表。

    回傳需要通知的變化清單：[{kind: new/ongoing/resolved, event: {...}}]
    """
    notifications: list[dict] = []
    now_str = timewin.fmt(tick_at)
    seen_keys: set[tuple[str, str]] = set()

    for f in findings:
        seen_keys.add((f.rule.id, f.entity_key))
        active = db.one(
            "SELECT * FROM events WHERE rule_id = ? AND entity_key = ? AND status = 'active'",
            (f.rule.id, f.entity_key))
        if active is None:
            with db.tx() as conn:
                evt_no = db.next_serial("EVT", "events", "evt_no")
                conn.execute(
                    "INSERT INTO events (evt_no, rule_id, rule_name, severity, entity_key,"
                    " entity_label, source_key, metric_value, threshold, baseline_median,"
                    " baseline_p95, multiple, brands, first_seen, last_seen, last_notified,"
                    " hit_count, peak_value, miss_ticks, status, context_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,0,'active',?)",
                    (evt_no, f.rule.id, f.rule.name, f.severity, f.entity_key,
                     f.entity_label, f.rule.source, f.metric, f.threshold,
                     f.baseline_median, f.baseline_p95, f.multiple, f.brands,
                     f.window_start, f.window_end, now_str, f.metric,
                     json.dumps(f.context, ensure_ascii=False, default=str)))
            event = db.one("SELECT * FROM events WHERE evt_no = ?", (evt_no,))
            notifications.append({"kind": NEW, "event": event})
            logger.info("新事件 %s %s %s（%s）", evt_no, f.rule.id, f.entity_label, f.severity)
            continue

        peak = max(float(active["peak_value"]), f.metric)
        cooldown = timedelta(minutes=f.rule.cooldown_minutes)
        last_notified = _parse(active["last_notified"]) if active["last_notified"] else None
        escalate = last_notified is None or (tick_at - last_notified) >= cooldown
        with db.tx() as conn:
            conn.execute(
                "UPDATE events SET last_seen = ?, hit_count = hit_count + 1, peak_value = ?,"
                " metric_value = ?, multiple = ?, miss_ticks = 0, threshold = ?,"
                " last_notified = CASE WHEN ? THEN ? ELSE last_notified END WHERE id = ?",
                (f.window_end, peak, f.metric, f.multiple, f.threshold,
                 int(escalate), now_str, active["id"]))
        if escalate:
            event = db.one("SELECT * FROM events WHERE id = ?", (active["id"],))
            notifications.append({"kind": ONGOING, "event": event})

    # 未命中的 active 事件：miss_ticks 累加，達標則 resolved
    resolve_after = settings()["alerting"]["resolve_after_ticks"]
    for row in db.rows("SELECT * FROM events WHERE status = 'active'"):
        if (row["rule_id"], row["entity_key"]) in seen_keys:
            continue
        misses = row["miss_ticks"] + 1
        if misses >= resolve_after:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE events SET status = 'resolved', miss_ticks = ? WHERE id = ?",
                    (misses, row["id"]))
            if row["severity"] in ("P0", "P1"):
                event = db.one("SELECT * FROM events WHERE id = ?", (row["id"],))
                notifications.append({"kind": RESOLVED, "event": event})
            logger.info("事件 %s 已恢復（連續 %d tick 未命中）", row["evt_no"], misses)
        else:
            with db.tx() as conn:
                conn.execute("UPDATE events SET miss_ticks = ? WHERE id = ?",
                             (misses, row["id"]))
    return notifications


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
