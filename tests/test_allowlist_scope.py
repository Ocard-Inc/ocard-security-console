"""Allowlist 的比對範圍。

這個檔案守的是「抑制只能抑制它該抑制的東西」。兩個方向都要守 ——
只守一邊的話：

- 只守「不該抑制的沒抑制」→ 有人把比對改嚴到什麼都不匹配也不會失敗，
  症狀是辦公室出口開始洪水式告警，而那會被誤讀成「規則太吵」並繼續調高門檻。
- 只守「該抑制的有抑制」→ 一筆打錯字的條目（例如把規則 id 貼進來）
  可以靜靜關掉整條規則。
"""
from __future__ import annotations

import pytest

from console.rules import engine
from console.rules.model import EntityField, Rule
from console.store import allowlist


def _rule(rid: str, entity: tuple[EntityField, ...]) -> Rule:
    return Rule(id=rid, name=rid, severity="P1", source="backend",
                kind="sql_threshold", window_minutes=10, enabled=True,
                entity=entity)


# R01 的形狀：帳號 + 來源 IP
ACC_IP = _rule("R01", (EntityField("acc", "actor"), EntityField("ip", "src")))
# R09 的形狀：只有一個 fp=None 的欄位，沒有來源 IP
NO_SRC = _rule("R09", (EntityField("scope", None),))
ROW = {"acc": "andrew_c", "ip": "1.2.3.4", "endpoint": "Api2/TransDetail"}


def _index(*entries: allowlist.Entry):
    return allowlist.build_index(entries)


def _entry(value: str = "", *, rule_id=None, endpoint="", eid=1):
    return allowlist.Entry(id=eid, name=f"entry-{eid}", source_ip=value,
                           endpoint=endpoint, rule_id=rule_id,
                           valid_from=None, valid_to=None)


# ─────────── 該抑制的確實有抑制（反向守護，不可刪） ───────────

def test_matching_ip_is_suppressed():
    entry = _entry("1.2.3.4")
    hit = engine._allowlist_hit(ACC_IP, ROW, _index(entry))
    assert hit is entry, "IP 相符卻沒有抑制 —— 辦公室出口會開始洪水式告警"


def test_hit_returns_the_entry_not_a_bool():
    """抑制必須說得出是**哪一條**例外遮掉的，否則畫面只能顯示一個數字。"""
    hit = engine._allowlist_hit(ACC_IP, ROW, _index(_entry("1.2.3.4", eid=7)))
    assert hit.id == 7 and hit.name == "entry-7"


# ─────────── 不該抑制的沒有被抑制 ───────────

def test_rule_id_in_the_entry_does_not_disable_the_rule():
    """entity_key 的第一段是 rule id。逐段比對的話這一筆會讓整條 R01 失效。"""
    assert engine._allowlist_hit(ACC_IP, ROW, _index(_entry("R01"))) is None


def test_account_name_in_a_source_entry_does_not_suppress():
    """allowlist 只收來源 IP。條目字面等於帳號名時不該抑制那個帳號。"""
    assert engine._allowlist_hit(ACC_IP, ROW, _index(_entry("andrew_c"))) is None


def test_endpoint_value_does_not_suppress():
    """fp=None 的欄位（route、endpoint、品牌編號）原樣進 entity_key。"""
    assert engine._allowlist_hit(
        ACC_IP, ROW, _index(_entry("Api2/TransDetail"))) is None


def test_rule_without_any_dimension_is_never_suppressed():
    """R09 這類規則的對象是字面常數 —— 沒有任何維度可以縮小，例外對它無效。"""
    row = {"scope": "1.2.3.4"}
    assert engine._allowlist_hit(NO_SRC, row, _index(_entry("1.2.3.4"))) is None
    assert allowlist.allowlistable(NO_SRC) is False


# ─────── 只有端點、沒有 IP 的規則（R04 的形狀） ───────
#
# 實測 Api2/GetProfile 同時觸發 R03（src + endpoint）與 R04（只有 endpoint）。
# 少了這一組，例外只能讓 R03 閉嘴而 R04 繼續叫 —— 等於沒解決問題。

ENDPOINT_ONLY = _rule("R04", (EntityField("endpoint", None),))
EP_ROW = {"endpoint": "Api2/GetProfile"}


def test_endpoint_only_rule_is_allowlistable():
    assert allowlist.allowlistable(ENDPOINT_ONLY) is True
    assert allowlist.has_source(ENDPOINT_ONLY) is False
    assert allowlist.dimensions(ENDPOINT_ONLY) == ("endpoint",)


def test_rule_scoped_endpoint_entry_suppresses_a_source_less_rule():
    entry = _entry(rule_id="R04", endpoint="Api2/GetProfile")
    assert engine._allowlist_hit(ENDPOINT_ONLY, EP_ROW, _index(entry)) is entry


def test_endpoint_entry_does_not_leak_to_other_endpoints():
    entry = _entry(rule_id="R04", endpoint="Api2/GetProfile")
    other = {"endpoint": "Api2/GetProfileExtra"}
    hit = engine._allowlist_hit(ENDPOINT_ONLY, other, _index(entry))
    assert hit is None, "端點必須是完全相等比對，前綴比對會連別的端點一起放行"


def test_endpoint_entry_does_not_leak_to_other_rules():
    entry = _entry(rule_id="R04", endpoint="Api2/GetProfile")
    r11 = _rule("R11", (EntityField("endpoint", None),))
    assert engine._allowlist_hit(r11, EP_ROW, _index(entry)) is None


def test_endpoint_entry_also_covers_the_ip_plus_endpoint_rule_when_scoped_to_it():
    """瓦城的情境要兩筆條目：R03（IP + 端點）與 R04（只有端點）。"""
    r03_entry = _entry("18.182.228.100", rule_id="R03", endpoint="Api2/GetProfile", eid=1)
    r04_entry = _entry(rule_id="R04", endpoint="Api2/GetProfile", eid=2)
    index = _index(r03_entry, r04_entry)
    r03 = _rule("R03", (EntityField("src", "src"), EntityField("endpoint", None)))
    row = {"src": "18.182.228.100", "endpoint": "Api2/GetProfile"}
    assert engine._allowlist_hit(r03, row, index) is r03_entry
    assert engine._allowlist_hit(ENDPOINT_ONLY, EP_ROW, index) is r04_entry


def test_endpoint_only_entry_is_never_global():
    """全域 + 只有端點會讓所有規則都不看那個端點，build_index 直接不收。"""
    index = _index(_entry(rule_id=None, endpoint="Api2/GetProfile"))
    assert index.by_rule == {} and index.by_ip == {}


# ─────────── 範圍語意 ───────────

def test_rule_scoped_entry_only_affects_that_rule():
    entry = _entry("1.2.3.4", rule_id="R07B")
    assert engine._allowlist_hit(ACC_IP, ROW, _index(entry)) is None
    r07b = _rule("R07B", (EntityField("ip", "src"),))
    assert engine._allowlist_hit(r07b, ROW, _index(entry)) is entry


def test_global_entry_affects_every_rule():
    entry = _entry("1.2.3.4", rule_id=None)
    r07b = _rule("R07B", (EntityField("ip", "src"),))
    assert engine._allowlist_hit(ACC_IP, ROW, _index(entry)) is entry
    assert engine._allowlist_hit(r07b, ROW, _index(entry)) is entry


def test_endpoint_scoped_entry_narrows_to_that_endpoint():
    entry = _entry("1.2.3.4", endpoint="Api2/TransDetail")
    assert engine._allowlist_hit(ACC_IP, ROW, _index(entry)) is entry
    other = {**ROW, "endpoint": "Api2/Other"}
    assert engine._allowlist_hit(ACC_IP, other, _index(entry)) is None


def test_empty_endpoint_means_all_endpoints():
    entry = _entry("1.2.3.4", endpoint="")
    assert engine._allowlist_hit(
        ACC_IP, {**ROW, "endpoint": "anything"}, _index(entry)) is entry


def test_empty_source_value_never_matches():
    """來源欄位是空的時候不可以命中任何條目（空字串條目已由 SQL 排除）。"""
    assert engine._allowlist_hit(ACC_IP, {"acc": "x", "ip": None},
                                 _index(_entry(""))) is None


# ─────────── 掃描層只吃全域條目 ───────────

@pytest.fixture
def two_entries():
    """一筆全域、一筆規則範圍，指向不同 IP。"""
    gid = allowlist.create(
        {"name": "全域測試", "purpose": "t", "reason": "t",
         "rule_id": None, "source_ip": "203.0.113.1",
         "valid_from": None, "valid_to": None}, who="test@olis.com.tw")
    rid = allowlist.create(
        {"name": "規則範圍測試", "purpose": "t", "reason": "t",
         "rule_id": "R07B", "source_ip": "203.0.113.2",
         "valid_from": None, "valid_to": None}, who="test@olis.com.tw")
    yield gid, rid
    from console.store import db
    with db.tx() as conn:
        conn.execute("DELETE FROM allowlist WHERE id IN (?, ?)", (gid, rid))


def test_sweep_only_sees_global_entries(two_entries):
    ips = allowlist.global_source_ips()
    assert "203.0.113.1" in ips
    assert "203.0.113.2" not in ips, \
        "規則範圍的例外流進掃描 —— 那個來源會從整份報告消失"


def test_engine_sees_both(two_entries):
    index = allowlist.build_index(allowlist.active_entries())
    assert "203.0.113.1" in index.by_ip and "203.0.113.2" in index.by_ip


def test_expired_entry_is_not_active():
    eid = allowlist.create(
        {"name": "已到期", "purpose": "t", "reason": "t",
         "rule_id": None, "source_ip": "203.0.113.9",
         "valid_from": "2026-01-01 00:00:00", "valid_to": "2026-01-02 23:59:59"},
        who="test@olis.com.tw")
    try:
        assert "203.0.113.9" not in allowlist.global_source_ips()
    finally:
        from console.store import db
        with db.tx() as conn:
            conn.execute("DELETE FROM allowlist WHERE id = ?", (eid,))
