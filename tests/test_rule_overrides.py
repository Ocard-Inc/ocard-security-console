"""規則參數覆寫。

守兩件事：
1. **覆寫真的生效，而 YAML 那一份沒有被污染。** `load_rules()` 有 lru_cache，
   Rule 是 frozen —— 如果哪天有人改成就地修改，這裡會失敗。
2. **能從 HTTP 送進來的壞值一律 400。** 沒有 Pydantic，這些檢查全靠手寫，
   而 NaN／Infinity／字串 "false" 都是真的能送進來的（見 api/validate.py）。
"""
from __future__ import annotations

import pytest

from console.rules import effective
from console.rules.loader import load_rules
from console.store import rule_overrides

REASON = "自動測試"


@pytest.fixture(autouse=True)
def _clean():
    for rid in ("R01", "R05", "R07A", "R08A", "R12"):
        rule_overrides.delete(rid)
    yield
    for rid in ("R01", "R05", "R07A", "R08A", "R12"):
        rule_overrides.delete(rid)


def _yaml(rule_id: str):
    return next(r for r in load_rules() if r.id == rule_id)


def _live(rule_id: str):
    return next(r for r in effective.effective_rules() if r.id == rule_id)


# ─────────────────────────── 合成 ───────────────────────────

def test_override_applies_without_touching_the_yaml_copy():
    before = _yaml("R01").threshold.static_floor
    rule_overrides.put("R01", {"static_floor": before + 400}, who="t", reason=REASON)
    assert _live("R01").threshold.static_floor == before + 400
    # lru_cache 裡那一份必須完全沒變 —— frozen dataclass + replace() 的意義就在這
    assert _yaml("R01").threshold.static_floor == before


def test_disabled_is_not_confused_with_no_override():
    """enabled=0 與 enabled IS NULL 是兩件事。用 truthiness 判斷會讓停用失效。"""
    rule_overrides.put("R01", {"enabled": False}, who="t", reason=REASON)
    assert _live("R01").enabled is False
    assert _yaml("R01").enabled is True


def test_cooldown_override_reaches_the_events_state_machine():
    """Finding.rule 就是 engine 收到的實例，所以 events.py 一行都不用改。"""
    rule_overrides.put("R01", {"cooldown_minutes": 15}, who="t", reason=REASON)
    assert _live("R01").cooldown_minutes == 15


def test_empty_override_row_is_deleted_not_left_as_a_shell():
    rule_overrides.put("R01", {"static_floor": 9999}, who="t", reason=REASON)
    rule_overrides.put("R01", {"static_floor": None}, who="t", reason=REASON)
    assert rule_overrides.get("R01") is None


def test_prune_clears_fields_equal_to_yaml():
    base = effective.yaml_values(_yaml("R01"))
    pruned = effective.prune("R01", {"static_floor": base["static_floor"],
                                     "cooldown_minutes": 5})
    assert pruned["static_floor"] is None, "等於 YAML 原值的欄位要清掉覆寫"
    assert pruned["cooldown_minutes"] == 5


# ─────────────────── 逐 kind 的可編輯欄位 ───────────────────

def test_editable_fields_per_kind():
    assert set(effective.editable_fields(_yaml("R01"))) == {
        "enabled", "static_floor", "cooldown_minutes", "factor"}
    # R07A 沒有 baseline_key → factor 乘上去恆為 0，改了不生效，所以不可編輯
    assert "factor" not in effective.editable_fields(_yaml("R07A"))
    # new_source 沒有 threshold，門檻是 min_events
    assert set(effective.editable_fields(_yaml("R08A"))) == {
        "enabled", "cooldown_minutes", "min_events"}
    # freshness 完全忽略 rule.threshold
    assert set(effective.editable_fields(_yaml("R12"))) == {"enabled", "cooldown_minutes"}


def test_new_source_override_lands_on_min_events():
    rule_overrides.put("R08A", {"min_events": 400}, who="t", reason=REASON)
    assert _live("R08A").min_events == 400


def test_inapplicable_field_is_ignored_not_silently_stored():
    """對 R12 寫 static_floor 不會產生「存了但引擎用舊值」的狀態。"""
    rule_overrides.put("R12", {"static_floor": 1}, who="t", reason=REASON)
    live = _live("R12")
    assert live.threshold is None or live.threshold == _yaml("R12").threshold


# ─────────────────────────── API 驗證 ───────────────────────────

def _patch(client, rule_id, body):
    return client.patch(f"/api/rules/{rule_id}", json=body)


def test_reason_is_required(client):
    r = _patch(client, "R01", {"static_floor": 1200})
    assert r.status_code == 400 and "理由" in r.text


def test_unknown_field_is_rejected(client):
    r = _patch(client, "R01", {"reason": REASON, "sql": "SELECT 1"})
    assert r.status_code == 400 and "sql" in r.text


@pytest.mark.parametrize("raw", [b'NaN', b'Infinity', b'-Infinity'])
def test_non_finite_numbers_are_rejected(client, raw):
    """json.loads 預設接受這些值：NaN 進 SQLite 變 NULL、Infinity 讓
    JSONResponse 序列化時 500 —— 一筆壞資料讓整個主控台掛掉。"""
    r = client.patch("/api/rules/R01",
                     content=b'{"reason":"x","static_floor":' + raw + b'}',
                     headers={"content-type": "application/json"})
    assert r.status_code == 400 and "有限" in r.text


def test_enabled_must_be_a_real_bool(client):
    """bool("false") 是 True —— 送字串會把停用變成啟用。"""
    r = _patch(client, "R01", {"reason": REASON, "enabled": "false"})
    assert r.status_code == 400
    r = _patch(client, "R01", {"reason": REASON, "enabled": 0})
    assert r.status_code == 400


def test_static_floor_below_sql_having_is_rejected(client):
    """R01 的 SQL 含 HAVING metric >= 400；設到它以下不會更靈敏，只會誤導。"""
    r = _patch(client, "R01", {"reason": REASON, "static_floor": 200})
    assert r.status_code == 400
    assert "400" in r.text and "HAVING" in r.text


def test_cooldown_below_one_tick_is_rejected(client):
    """0 會讓 events.py 的 escalate 永遠成立 → 每五分鐘重發一次「持續中」。"""
    assert _patch(client, "R01", {"reason": REASON, "cooldown_minutes": 0}).status_code == 400
    assert _patch(client, "R01", {"reason": REASON, "cooldown_minutes": 5000}).status_code == 400


def test_field_not_editable_for_this_kind_is_rejected(client):
    assert _patch(client, "R12", {"reason": REASON, "static_floor": 5}).status_code == 400
    assert _patch(client, "R07A", {"reason": REASON, "factor": 3}).status_code == 400


def test_unknown_rule_is_404(client):
    assert _patch(client, "R99", {"reason": REASON, "enabled": False}).status_code == 404


def test_patch_reports_before_and_after_and_applies_immediately(client):
    r = _patch(client, "R01", {"reason": REASON, "static_floor": 1200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"field": "static_floor", "from": 800.0, "to": 1200.0} in body["changed"]
    # 前端不可自己推斷這兩個 —— 猜錯的症狀是「以為改好了而檢查還在用舊值」
    assert body["restart_required"] is False
    assert "五分鐘檢查" in body["applies_at"]
    assert _live("R01").threshold.static_floor == 1200


def test_disabled_rule_still_appears_in_the_list(client):
    """停用的規則若不列出，畫面就沒辦法把它開回來。"""
    _patch(client, "R05", {"reason": REASON, "enabled": False})
    body = client.get("/api/rules").json()
    assert len(body["rules"]) == 18
    assert "R05" in body["disabled"]
    assert any(x["id"] == "R05" for x in body["rules"])


def test_delete_override_requires_reason_and_409s_when_absent(client):
    assert client.request("DELETE", "/api/rules/R01/override",
                          json={"reason": REASON}).status_code == 409
    _patch(client, "R01", {"reason": REASON, "static_floor": 1200})
    assert client.request("DELETE", "/api/rules/R01/override",
                          json={}).status_code == 400
    assert client.request("DELETE", "/api/rules/R01/override",
                          json={"reason": REASON}).status_code == 200
    assert rule_overrides.get("R01") is None


def test_every_change_writes_an_audit_row_with_the_diff(client):
    from console.store import audit
    _patch(client, "R01", {"reason": "測試留痕", "static_floor": 1200})
    rows = audit.recent(5, action="調整規則參數")
    assert rows, "覆寫必須留痕"
    latest = rows[0]
    # audit_log 沒有 diff 欄位 —— 不寫進 target 就永遠查不到改了什麼
    assert "R01" in latest["target"] and "800" in latest["target"] and "1200" in latest["target"]
    assert latest["reason"] == "測試留痕"
