"""事件持久化與去重狀態機。

去重鍵 = (rule_id, entity_key)：
1. 無 active 事件 → 建新事件（EVT-XXXX），回報「新事件」
2. cooldown 內再命中 → 只累計 hit_count / peak / last_seen
3. 超過 cooldown 仍持續 → 回報「持續中」（升級通知）並重置 last_notified
4. 連續 resolve_after 個 tick 未命中 → 標 resolved，回報「已恢復」

**`status` 有三個值，但這個模組只寫兩個。** `active` / `resolved` 是狀態機的結論
（「還在命中」／「指標回到門檻以下」），`closed`（已處理完畢）**只由人寫**
（`api/routes.close_event`）。這裡每一條 SQL 都寫 `status = 'active'`，所以
closed 自動退出狀態機：不累加 miss_ticks、不會被標 resolved、不會發「已恢復」。

那個「自動」是刻意設計的。人工結案如果做成另一個欄位（例如只加 `closed_at`
而 status 留著 active），每一個既有的 `status = 'active'` 查詢都得記得加上
`AND closed_at IS NULL` —— 漏掉任何一處的症狀是「已處理完畢的事件還在發通知」，
而那是靜靜發生的。用一個狀態機不認識的值，漏掉的方向反過來變成
「多建一個新事件」，那是看得見的。

**因此關閉一個仍在命中的事件，下一個 tick 會建立一個新的 EVT 編號**
（上面第 1 條找不到 active 列）。那不是 bug 而是唯一誠實的行為：你說處理完了，
而它又發生了。UI 必須在關閉前把這件事講出來（見 event-detail.js）。
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


def apply_findings(findings: list[Finding], tick_at: datetime, *,
                   rules: tuple = (), suppressed: list = ()) -> list[dict]:
    """把本 tick 的 findings 併入 events 表。

    回傳需要通知的變化清單：[{kind: new/ongoing/resolved, event: {...}}]

    `rules` 是本 tick 實際評估的規則集合，`suppressed` 是被 allowlist 擋掉的
    Suppression 清單。兩者都是為了讓「已恢復」不說謊 —— 見 _silenced_keys()。
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
            # 同一對象先前被人標成「已處理完畢」→ 它又發生了。留一行 log：
            # 光看新事件通知沒辦法分辨「第一次出現」與「結案後再犯」，
            # 而後者是完全不同的結論。
            prev = db.one(
                "SELECT evt_no, closed_at, closed_by FROM events"
                " WHERE rule_id = ? AND entity_key = ? AND status = 'closed'"
                " ORDER BY closed_at DESC LIMIT 1", (f.rule.id, f.entity_key))
            if prev:
                logger.warning(
                    "%s 是結案後再犯：同一對象曾為 %s，由 %s 於 %s 標為已處理完畢",
                    evt_no, prev["evt_no"], prev["closed_by"], prev["closed_at"])
            continue

        peak = max(float(active["peak_value"]), f.metric)
        cooldown = timedelta(minutes=f.rule.cooldown_minutes)
        last_notified = _parse(active["last_notified"]) if active["last_notified"] else None
        escalate = last_notified is None or (tick_at - last_notified) >= cooldown
        with db.tx() as conn:
            # brands / context 跟著 metric_value 一起更新：畫面上「涉及品牌」的
            # 展開明細必須對應目前顯示的數值，停留在首次命中的視窗會造成誤讀。
            conn.execute(
                "UPDATE events SET last_seen = ?, hit_count = hit_count + 1, peak_value = ?,"
                " metric_value = ?, multiple = ?, miss_ticks = 0, threshold = ?,"
                " brands = ?, context_json = ?,"
                " last_notified = CASE WHEN ? THEN ? ELSE last_notified END WHERE id = ?",
                (f.window_end, peak, f.metric, f.multiple, f.threshold,
                 f.brands, json.dumps(f.context, ensure_ascii=False, default=str),
                 int(escalate), now_str, active["id"]))
        if escalate:
            event = db.one("SELECT * FROM events WHERE id = ?", (active["id"],))
            notifications.append({"kind": ONGOING, "event": event})

    # 未命中的 active 事件：miss_ticks 累加，達標則 resolved
    resolve_after = settings()["alerting"]["resolve_after_ticks"]
    silenced_rules, silenced_keys = _silenced_keys(rules, suppressed)
    frozen = 0
    for row in db.rows("SELECT * FROM events WHERE status = 'active'"):
        if (row["rule_id"], row["entity_key"]) in seen_keys:
            continue
        if row["rule_id"] in silenced_rules or \
                (row["rule_id"], row["entity_key"]) in silenced_keys:
            frozen += 1
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
    if frozen:
        logger.info("%d 筆 active 事件因規則停用或 Allowlist 抑制而暫停計時"
                    "（不標 resolved、不發「已恢復」）", frozen)
    return notifications


def _silenced_keys(rules: tuple, suppressed: list) -> tuple[set[str], set[tuple[str, str]]]:
    """本 tick「被我們自己關掉」的規則與對象。

    這是「已恢復」不說謊的關鍵。收尾迴圈只知道「這個 tick 沒命中」，
    而沒命中有兩種完全不同的原因：

    1. 指標真的回到門檻以下 —— 那是恢復。
    2. **我們停止看了** —— 規則被停用、對象被加進 Allowlist、門檻被調高。

    原本兩者不分，所以停用一條規則之後 15 分鐘，該規則所有進行中的事件會被
    標 resolved，P0/P1 還會在 Slack 顯示「已恢復」。攻擊沒有恢復。
    而 status 是就地 UPDATE、沒有逐 tick 歷史，這個誤標**無法從資料還原**。
    部署 reset VM 之後的 catch-up 會在幾秒內一次發出一整批，
    很容易被當成好消息。

    這裡的處理是**暫停計時**（miss_ticks 不動、不標 resolved）：事件維持
    active 掛在待判定清單上。規則重新啟用而對象已經冷卻的話，計時從凍結處
    接續，自然 resolved。

    「規則不在本次評估的集合裡」（YAML 刪掉了）與停用同等對待 ——
    那條訊號一樣已經不存在。
    """
    if not rules:
        return set(), {(s.rule_id, s.entity_key) for s in suppressed}
    live = {r.id for r in rules if r.enabled}
    silenced_rules = {row["rule_id"] for row in db.rows(
        "SELECT DISTINCT rule_id FROM events WHERE status = 'active'")
        if row["rule_id"] not in live}
    return silenced_rules, {(s.rule_id, s.entity_key) for s in suppressed}


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
