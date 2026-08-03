"""被 Allowlist 抑制的新來源不可以被記進 known_sources。

這是整個功能唯一**不可逆**的資料汙染。原本 `_eval_new_source` 的順序是
「寫 known_sources → 才判 allowlist」，所以被抑制的來源仍被記成「已知」——
日後停用那條例外，R08A/B/C **也永遠不會再對它告警**，而畫面上 allowlist 是
停用的、規則是啟用的，一切看起來正常。known_sources 有 23 萬列、不在
`_DERIVED_TABLES`，清不回來。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from console.rules import engine
from console.rules.model import EntityField, Rule
from console.store import allowlist, db

KIND = "zztest_kind"
IP = "203.0.113.201"
ACC = "zztester"

RULE = Rule(id="ZZNEW", name="測試新來源", severity="P3", source="backend",
            kind="new_source", window_minutes=10, enabled=True,
            sql="SELECT 1 WHERE %(start)s < %(end)s",
            entity=(EntityField("acc", "actor"), EntityField("ip", "src")),
            known_kind=KIND, min_events=1)


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    with db.tx() as conn:
        conn.execute("DELETE FROM known_sources WHERE kind = ?", (KIND,))


def _eval(index=None):
    df = pd.DataFrame([{"acc": ACC, "ip": IP, "metric": 500}])
    with patch("console.rules.engine.query", return_value=df):
        return engine._eval_new_source(
            RULE, "2026-08-01 00:00:00", "2026-08-01 00:10:00",
            datetime(2026, 8, 1, 0, 10),
            index if index is not None else allowlist.build_index([]))


def _known() -> list[dict]:
    return db.rows("SELECT * FROM known_sources WHERE kind = ?", (KIND,))


def _entry():
    return allowlist.Entry(id=99, name="測試例外", source_ip=IP, endpoint="",
                           rule_id=None, valid_from=None, valid_to=None)


def test_unsuppressed_new_source_is_recorded_and_reported():
    """基準：沒有例外時照常記錄並產生 finding。"""
    findings, suppressed = _eval()
    assert len(findings) == 1
    assert suppressed == []
    assert len(_known()) == 1, "沒有被抑制的新來源必須記進 known_sources"


def test_suppressed_new_source_is_not_recorded():
    findings, suppressed = _eval(allowlist.build_index([_entry()]))
    assert findings == []
    assert len(suppressed) == 1
    assert _known() == [], \
        "被抑制的來源被記成「已知」—— 停用例外之後永遠不會再告警，而且清不回來"


def test_removing_the_exception_restores_the_first_seen_signal():
    """停用例外之後，同一個來源必須重新被視為首見。"""
    _eval(allowlist.build_index([_entry()]))       # 抑制期間
    assert _known() == []
    findings, suppressed = _eval()               # 例外停用後
    assert len(findings) == 1, "訊號必須回來"
    assert suppressed == []
    assert len(_known()) == 1


def test_suppression_carries_the_entity_key_for_the_event_state_machine():
    """store/events.py 靠 entity_key 認出「本來會命中，只是被抑制」。"""
    _, suppressed = _eval(allowlist.build_index([_entry()]))
    s = suppressed[0]
    assert s.entity_key.startswith("ZZNEW|")
    assert s.allowlist_id == 99
    assert s.source_ip == IP
