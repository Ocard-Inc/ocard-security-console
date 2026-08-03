"""停用規則或建立 Allowlist 之後，「已恢復」不可以是假的。

`store/events.py` 的收尾迴圈只知道「這個 tick 沒命中」，而沒命中有兩種完全不同
的原因：指標真的回到門檻以下（那是恢復），或者**我們停止看了**。

原本兩者不分，所以停用一條規則之後 15 分鐘，該規則所有進行中的事件會被標
resolved、P0/P1 還會在 Slack 顯示「已恢復」。攻擊沒有恢復。而 status 是就地
UPDATE、沒有逐 tick 歷史，那個誤標**無法從資料還原**。
"""
from __future__ import annotations

import pytest

from console.core import timewin
from console.core.config import settings
from console.rules.model import EntityField, Rule, Suppression
from console.store import db, events

RULE_ID = "ZZTEST"
ENTITY_KEY = f"{RULE_ID}|tester|203.0.113.88"


def _rule(*, enabled: bool) -> Rule:
    return Rule(id=RULE_ID, name="測試規則", severity="P1", source="backend",
                kind="sql_threshold", window_minutes=10, enabled=enabled,
                entity=(EntityField("acc", "actor"), EntityField("ip", "src")))


@pytest.fixture
def active_event():
    now = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO events (evt_no, rule_id, rule_name, severity, entity_key,"
            " entity_label, source_key, metric_value, threshold, first_seen, last_seen,"
            " peak_value, miss_ticks, status)"
            " VALUES ('EVT-ZZTEST', ?, '測試規則', 'P1', ?, 'tester · 203.0.113.88',"
            " 'backend', 999, 100, ?, ?, 999, 0, 'active')",
            (RULE_ID, ENTITY_KEY, now, now))
    yield db.one("SELECT * FROM events WHERE evt_no = 'EVT-ZZTEST'")
    with db.tx() as conn:
        conn.execute("DELETE FROM events WHERE evt_no = 'EVT-ZZTEST'")


def _run_ticks(n: int, **kwargs) -> list[dict]:
    """跑 n 次「本 tick 沒有任何 finding」。"""
    out = []
    for _ in range(n):
        out += events.apply_findings([], timewin.taipei_now(), **kwargs)
    return out


def _status() -> str:
    return db.one("SELECT status FROM events WHERE evt_no = 'EVT-ZZTEST'")["status"]


def _misses() -> int:
    return db.one("SELECT miss_ticks FROM events WHERE evt_no = 'EVT-ZZTEST'")["miss_ticks"]


def _ticks_to_resolve() -> int:
    return settings()["alerting"]["resolve_after_ticks"]


# ─────────── 基準：真的回到門檻以下時「已恢復」仍然要發 ───────────

def test_genuine_recovery_still_resolves(active_event):
    notes = _run_ticks(_ticks_to_resolve(), rules=(_rule(enabled=True),))
    assert _status() == "resolved"
    assert any(n["kind"] == events.RESOLVED for n in notes), \
        "指標真的回落時必須標 resolved 並通知 —— 修過頭會讓事件永遠掛著"


# ─────────── 我們自己關掉的：不可以標 resolved ───────────

def test_disabled_rule_does_not_fake_a_recovery(active_event):
    notes = _run_ticks(_ticks_to_resolve() + 2, rules=(_rule(enabled=False),))
    assert _status() == "active", "停用規則不代表事件恢復了"
    assert not [n for n in notes if n["kind"] == events.RESOLVED]
    assert _misses() == 0, "計時應該凍結，規則重新啟用後從這裡接續"


def test_rule_missing_from_the_evaluated_set_is_treated_as_disabled(active_event):
    """YAML 把規則刪掉之後，那條訊號一樣已經不存在了 —— 不該報「已恢復」。"""
    other = Rule(id="R01", name="別的規則", severity="P1", source="backend",
                 kind="sql_threshold", window_minutes=10, enabled=True,
                 entity=(EntityField("acc", "actor"),))
    notes = _run_ticks(_ticks_to_resolve() + 1, rules=(other,))
    assert _status() == "active"
    assert not [n for n in notes if n["kind"] == events.RESOLVED]


def test_no_rules_argument_keeps_the_old_behaviour(active_event):
    """rules 未提供時不做規則層判斷（相容既有呼叫端與純 store 層測試）。"""
    _run_ticks(_ticks_to_resolve())
    assert _status() == "resolved"


def test_allowlisted_entity_does_not_fake_a_recovery(active_event):
    suppression = Suppression(
        rule_id=RULE_ID, rule_name="測試規則", allowlist_id=1,
        allowlist_name="測試例外", source_ip="203.0.113.88",
        entity_key=ENTITY_KEY, entity_label="tester · 203.0.113.88",
        metric=999, threshold=100, window_start="", window_end="")
    notes = _run_ticks(_ticks_to_resolve() + 2,
                       rules=(_rule(enabled=True),), suppressed=[suppression])
    assert _status() == "active", "對象被抑制不代表它恢復了"
    assert not [n for n in notes if n["kind"] == events.RESOLVED]


def test_suppression_of_another_entity_does_not_protect_this_one(active_event):
    """抑制的比對必須是精確的 (rule_id, entity_key)，不是「有抑制就全部凍結」。"""
    other = Suppression(
        rule_id=RULE_ID, rule_name="測試規則", allowlist_id=1,
        allowlist_name="測試例外", source_ip="203.0.113.99",
        entity_key=f"{RULE_ID}|someone|203.0.113.99", entity_label="someone",
        metric=1, threshold=1, window_start="", window_end="")
    _run_ticks(_ticks_to_resolve(), rules=(_rule(enabled=True),), suppressed=[other])
    assert _status() == "resolved"
