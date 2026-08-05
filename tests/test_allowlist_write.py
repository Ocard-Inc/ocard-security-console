"""Allowlist 的寫入端點。

這個功能沒有權限關卡（沒有角色分級），約束全靠這裡守的四件事：
必填、會到期、留痕、以及「立即生效」真的立即。
"""
from __future__ import annotations

import pytest

from console.core import timewin
from console.store import allowlist, audit, db

IP = "203.0.113.55"
BASE = {"name": "測試整合", "purpose": "自動測試用",
        "reason": "驗收", "source_ip": IP, "valid_to": "2026-12-31"}
EP = "Api2/GetProfile"


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    with db.tx() as conn:
        conn.execute("DELETE FROM allowlist WHERE source_ip = ? OR endpoint = ?",
                     (IP, EP))
        conn.execute("DELETE FROM audit_log WHERE target LIKE ? OR target LIKE ?",
                     (f"%{IP}%", f"%{EP}%"))


def _create(client, **over):
    return client.post("/api/allowlist", json={**BASE, **over})


# ─────────────────────── 立即生效 ───────────────────────

def test_created_entry_is_active_in_the_same_second(client):
    """「單步生效」的自動化驗收條件。valid_from 的字串比較錯一個字元就不成立。"""
    r = _create(client)
    assert r.status_code == 200, r.text
    assert IP in allowlist.global_source_ips()
    assert IP in {e.source_ip for e in allowlist.active_entries()}


def test_rule_scoped_entry_is_invisible_to_the_sweep(client):
    r = _create(client, rule_id="R07B")
    assert r.status_code == 200, r.text
    assert IP not in allowlist.global_source_ips(), \
        "規則範圍的例外流進掃描 —— 那個來源會從整份報告消失"
    assert IP in {e.source_ip for e in allowlist.active_entries()}


# ─────────────────────── 必填與驗證 ───────────────────────

def test_required_text_fields_are_named(client):
    r = client.post("/api/allowlist", json={"source_ip": IP, "valid_to": "2026-12-31"})
    assert r.status_code == 400
    for label in ("名稱", "用途", "建立理由"):
        assert label in r.text, f"訊息要列出缺哪些欄位，少了「{label}」"
    assert "創立人" not in r.text, "創立人不是使用者填的欄位（由登入帳號自動帶入）"


def test_creator_is_the_signed_in_account_and_matches_approved_by(client):
    """創立人一律是登入帳號（2026-08 使用者決定：不給填、唯讀）。

    原本它是可填的「負責人」、留空才帶登入帳號 —— 於是它可能是任何字串
    （實測播種列是「Ocard 內部」，不是一個帳號），當不了「這筆核准是誰建的」
    的答案，而那是這個欄位唯一有稽核意義的用途。
    """
    r = client.post("/api/allowlist", json=BASE)
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert "@" in entry["owner"], "創立人必須是登入帳號"
    assert entry["owner"] == entry["approved_by"], \
        "新資料的創立人與 approved_by 都來自 who，必須一致"


def test_creator_cannot_be_supplied_or_modified(client):
    """送 owner 進來一律 400，**不是靜靜忽略**。

    靜靜忽略的話前端可以顯示一個「已存」的值而資料庫是另一個 —— 而這張表
    沒有 DELETE、只有停用，稽核紀錄裡的 #id 必須永遠解得回同一筆條目。
    """
    bad = client.post("/api/allowlist", json={**BASE, "owner": "別人"})
    assert bad.status_code == 400, bad.text

    ok = client.post("/api/allowlist", json=BASE)
    assert ok.status_code == 200, ok.text
    entry_id = ok.json()["entry"]["id"]
    mine = ok.json()["entry"]["owner"]

    patched = client.patch(f"/api/allowlist/{entry_id}",
                           json={"reason": "改創立人", "owner": "別人"})
    assert patched.status_code == 400, patched.text
    assert allowlist.get(entry_id)["owner"] == mine, "創立人被改掉了"


def test_valid_to_is_optional_and_means_never_expires(client):
    """使用者決定：到期日選填。留空 = 永久盲區，所以必須被標示出來。"""
    body = {k: v for k, v in BASE.items() if k != "valid_to"}
    r = client.post("/api/allowlist", json=body)
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["valid_to"] is None
    assert entry["expiry_missing"] is True
    assert entry["effective"] is True, "沒有到期日就是一直生效"
    assert any("永久" in w for w in r.json()["warnings"]), (
        "永不到期必須在回應裡說出來，不能安靜地建起來")
    assert client.get("/api/allowlist").json()["summary"]["no_expiry"] >= 1


def test_absurd_far_future_expiry_is_still_rejected(client):
    """9999 年的到期日看起來有期限但沒有 —— 要永久請直接留空。"""
    r = _create(client, valid_to="9999-01-01")
    assert r.status_code == 400 and "留空" in r.text


@pytest.mark.parametrize("bad", ["10.0.0.0/8", "andrew_c", "10.0.0", ""])
def test_bad_source_ip_is_rejected(client, bad):
    assert _create(client, source_ip=bad).status_code == 400


def test_cidr_message_explains_why(client):
    """比對是字串完全相等 —— 網段條目會被存進去、看起來成功，而永遠不命中。"""
    r = _create(client, source_ip="10.0.0.0/8")
    assert "完全相等" in r.text


def test_datetime_local_t_form_is_rejected(client):
    """'T' 的碼位大於空格，帶 T 的 valid_from 會讓條目永遠不生效而畫面顯示生效中。"""
    assert _create(client, valid_to="2026-12-31T23:59").status_code == 400


def test_date_only_valid_to_is_padded_to_end_of_day(client):
    """只給日期而不補秒的話，到期日當天整天都算已過期 —— 早一整天失效。"""
    r = _create(client, valid_to="2026-12-31")
    assert r.json()["entry"]["valid_to"] == "2026-12-31 23:59:59"


def test_expired_expiry_is_rejected(client):
    assert _create(client, valid_to="2020-01-01").status_code == 400


def test_rule_scope_must_exist_and_have_a_dimension(client):
    assert _create(client, rule_id="R99").status_code == 400
    # R09 的 entity 只有字面常數 scope —— 沒有任何可縮小的維度
    r = _create(client, rule_id="R09")
    assert r.status_code == 400 and "沒有可抑制的維度" in r.text


# ────────── 規則範圍 + 端點（瓦城 GetProfile 的情境） ──────────
#
# 實測 Api2/GetProfile 同時觸發 R03（src + endpoint）與 R04（只有 endpoint）。
# 少了 endpoint-only 這條路徑，例外只能讓 R03 閉嘴而 R04 繼續叫。

def test_endpoint_scoped_exception_for_a_source_less_rule(client):
    """R04 的對象只有 endpoint，所以這種例外不填 IP。"""
    body = {k: v for k, v in BASE.items() if k != "source_ip"}
    r = client.post("/api/allowlist", json={**body, "rule_id": "R04", "endpoint": EP})
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["source_ip"] is None and entry["endpoint"] == EP
    assert entry["effective"] is True
    assert EP not in allowlist.global_source_ips(), (
        "規則範圍的例外不可以流進期間掃描")


def test_ip_on_a_source_less_rule_is_rejected(client):
    """R04 以端點聚合，填了 IP 這條例外永遠不會命中 —— 要擋，不要靜靜存起來。"""
    r = _create(client, rule_id="R04", endpoint=EP)
    assert r.status_code == 400 and "永遠不會命中" in r.text


def test_endpoint_on_a_rule_without_that_dimension_is_rejected(client):
    r = _create(client, rule_id="R07B", endpoint=EP)
    assert r.status_code == 400 and "不含端點欄位" in r.text


def test_rule_scope_without_any_target_is_rejected(client):
    """只給 rule_id 等於「這條規則永不觸發」—— 那應該去停用規則。"""
    body = {k: v for k, v in BASE.items() if k != "source_ip"}
    r = client.post("/api/allowlist", json={**body, "rule_id": "R03"})
    assert r.status_code == 400 and "停用" in r.text


def test_global_scope_still_requires_an_ip(client):
    """全域 + 只有端點 = 所有規則都不看那個端點，盲區太大。"""
    body = {k: v for k, v in BASE.items() if k != "source_ip"}
    r = client.post("/api/allowlist", json={**body, "endpoint": EP})
    assert r.status_code == 400 and "全域例外必須指定來源 IP" in r.text


def test_ip_plus_endpoint_and_endpoint_only_can_coexist(client):
    """瓦城的情境需要兩筆：R03（IP + 端點）與 R04（只有端點）。"""
    assert _create(client, rule_id="R03", endpoint=EP).status_code == 200
    body = {k: v for k, v in BASE.items() if k != "source_ip"}
    assert client.post("/api/allowlist",
                       json={**body, "rule_id": "R04", "endpoint": EP,
                             "name": "R04 端點例外"}).status_code == 200
    index = allowlist.build_index(allowlist.active_entries())
    assert IP in index.by_ip and "R04" in index.by_rule


def test_same_ip_different_endpoint_is_not_a_duplicate(client):
    assert _create(client, rule_id="R03", endpoint=EP).status_code == 200
    r = _create(client, rule_id="R03", endpoint="Api2/GivePoint", name="另一個端點")
    assert r.status_code == 200, "唯一性鍵含端點，不同端點不是重複"


def test_rules_payload_lists_the_available_filters(client):
    """「選了規則之後還能再用什麼縮小」由後端給，前端不自己推導。"""
    rules = {r["id"]: r for r in client.get("/api/allowlist").json()["rules"]}
    r03, r04, r07b, r09 = rules["R03"], rules["R04"], rules["R07B"], rules["R09"]
    assert r03["allowlistable"] and r03["has_source"]
    assert [f["key"] for f in r03["filters"]] == ["endpoint"]
    assert r04["allowlistable"] and not r04["has_source"]
    assert [f["key"] for f in r04["filters"]] == ["endpoint"]
    assert r07b["allowlistable"] and r07b["has_source"] and r07b["filters"] == []
    assert not r09["allowlistable"]
    # 標籤依資料來源而異（api 與 backend 的 endpoint 不是同一個東西）
    assert "Controller" in r03["filters"][0]["label"]
    assert "Route" in rules["R14"]["filters"][0]["label"]


def test_unknown_key_is_rejected(client):
    assert _create(client, token_fp="token_X").status_code == 400


def test_duplicate_in_the_same_scope_is_409_not_500(client):
    assert _create(client).status_code == 200
    r = _create(client)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "allowlist_duplicate"
    assert detail["existing_id"]


def test_rule_scoped_duplicate_of_a_global_entry_warns_but_allows(client):
    _create(client)
    r = _create(client, rule_id="R07B", name="規則範圍")
    assert r.status_code == 200
    assert r.json()["warnings"], "已有全域例外時要說出新條目不會有額外效果"


# ─────────────────────── 修改與停用 ───────────────────────

def test_source_ip_cannot_be_changed(client):
    eid = _create(client).json()["entry"]["id"]
    r = client.patch(f"/api/allowlist/{eid}", json={"reason": "x", "source_ip": "1.1.1.1"})
    assert r.status_code == 400 and "不可修改" in r.text


def test_patch_requires_reason(client):
    eid = _create(client).json()["entry"]["id"]
    assert client.patch(f"/api/allowlist/{eid}", json={"name": "改名"}).status_code == 400


def test_disable_is_not_delete_and_the_row_survives(client):
    eid = _create(client).json()["entry"]["id"]
    assert client.post(f"/api/allowlist/{eid}/disable",
                       json={"reason": "不需要了"}).status_code == 200
    row = allowlist.get(eid)
    assert row is not None, "沒有 DELETE —— 稽核紀錄裡的 #id 必須永遠解得回一筆"
    assert row["status"] == allowlist.STATUS_DISABLED
    assert IP not in allowlist.global_source_ips()


def test_disable_twice_is_409(client):
    eid = _create(client).json()["entry"]["id"]
    client.post(f"/api/allowlist/{eid}/disable", json={"reason": "x"})
    r = client.post(f"/api/allowlist/{eid}/disable", json={"reason": "x"})
    assert r.status_code == 409, "回 200 會讓畫面顯示「停用成功」而什麼都沒發生"


def test_same_ip_can_be_added_again_after_disable(client):
    eid = _create(client).json()["entry"]["id"]
    client.post(f"/api/allowlist/{eid}/disable", json={"reason": "x"})
    assert _create(client).status_code == 200, "停用的舊條目不可佔住這個 IP"


def test_disable_reports_other_entries_still_suppressing(client):
    """同一個 IP 可以有多筆。停用一筆而另一筆仍生效時抑制沒有解除。"""
    gid = _create(client).json()["entry"]["id"]
    _create(client, rule_id="R07B", name="規則範圍")
    r = client.post(f"/api/allowlist/{gid}/disable", json={"reason": "x"})
    assert r.json()["still_suppressed_by"], \
        "另一筆仍生效卻不說，畫面看起來就像抑制已解除"


def test_enable_rejects_an_already_expired_entry(client):
    """恢復一個已過期的條目會讓畫面顯示「生效中」而它不生效。"""
    eid = _create(client).json()["entry"]["id"]
    client.post(f"/api/allowlist/{eid}/disable", json={"reason": "x"})
    with db.tx() as conn:
        conn.execute("UPDATE allowlist SET valid_to = ? WHERE id = ?",
                     ("2026-01-01 00:00:00", eid))
    r = client.post(f"/api/allowlist/{eid}/enable", json={"reason": "x"})
    assert r.status_code == 400 and "到期" in r.text


def test_enable_allows_an_entry_without_an_expiry(client):
    """沒有到期日是允許的（永不到期），不該擋住恢復。"""
    body = {k: v for k, v in BASE.items() if k != "valid_to"}
    eid = client.post("/api/allowlist", json=body).json()["entry"]["id"]
    client.post(f"/api/allowlist/{eid}/disable", json={"reason": "x"})
    r = client.post(f"/api/allowlist/{eid}/enable", json={"reason": "x"})
    assert r.status_code == 200, r.text


def test_no_delete_endpoint(client):
    eid = _create(client).json()["entry"]["id"]
    assert client.delete(f"/api/allowlist/{eid}").status_code in (404, 405)


# ─────────────────────── 留痕 ───────────────────────

@pytest.mark.parametrize("action,call", [
    ("新增 Allowlist 例外", "create"),
    ("修改 Allowlist 例外", "patch"),
    ("停用 Allowlist 例外", "disable"),
])
def test_every_write_is_audited(client, action, call):
    eid = _create(client).json()["entry"]["id"]
    if call == "patch":
        client.patch(f"/api/allowlist/{eid}", json={"reason": "改一下", "name": "新名"})
    elif call == "disable":
        client.post(f"/api/allowlist/{eid}/disable", json={"reason": "停掉"})
    rows = audit.recent(20, action=action)
    assert rows, f"{action} 沒有留痕"
    assert IP in rows[0]["target"]
    assert rows[0]["reason"]


def test_every_write_sends_an_ops_message(client, slack_outbox):
    """Slack ops 訊息是**當事人改不掉的**那個通道，不可以為了消音而拿掉。

    測試自己不會真的送出（見 `conftest.slack_outbox`），但「有沒有要送」必須守住：
    沒有角色分級，一筆全域條目就讓 17 條規則與整份掃描看不見那個來源，
    偵測型控制只剩這一個。
    """
    eid = _create(client).json()["entry"]["id"]
    client.post(f"/api/allowlist/{eid}/disable", json={"reason": "停掉"})
    actions = [m for m in slack_outbox if "Allowlist 例外" in m]
    assert len(actions) == 2, "新增或停用少發了 ops 訊息"
    assert IP in actions[0] and "dev@olis.com.tw" in actions[0], \
        "訊息要說出是誰對哪個來源做的，否則收到的人得自己進主控台查"


def test_effective_is_computed_by_the_backend(client):
    """前端看到 status='生效中' 就顯示「生效中」而它其實已過期，是誤導型 UI。"""
    eid = _create(client).json()["entry"]["id"]
    with db.tx() as conn:
        conn.execute("UPDATE allowlist SET valid_to = ? WHERE id = ?",
                     ("2026-01-01 00:00:00", eid))
    entry = next(e for e in client.get("/api/allowlist").json()["entries"]
                 if e["id"] == eid)
    assert entry["status"] == allowlist.STATUS_ACTIVE
    assert entry["effective"] is False
    assert "到期" in entry["effective_note"]


def test_preview_says_what_would_be_suppressed(client):
    r = client.post("/api/allowlist/preview", json={"source_ip": IP})
    assert r.status_code == 200
    body = r.json()
    assert "events_28d" in body and "scope_note" in body
    assert "期間異常掃描" in body["scope_note"]


def test_source_ip_is_shown_as_the_raw_value(client):
    """有人「順手」把它指紋化的話，抑制永遠不會命中，而且完全沒有錯誤。"""
    _create(client)
    entries = client.get("/api/allowlist").json()["entries"]
    entry = next(e for e in entries if e["id"])
    assert not entry["source_ip"].startswith("src_")
    assert IP in {e["source_ip"] for e in entries}


def test_valid_to_can_be_cleared_by_patch(client):
    """清空到期日 = 改為永不到期。後端用 `in payload` 判斷而不是真值。"""
    eid = _create(client).json()["entry"]["id"]
    r = client.patch(f"/api/allowlist/{eid}",
                     json={"reason": "改為長期例外", "valid_to": ""})
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["valid_to"] is None
    assert r.json()["entry"]["expiry_missing"] is True


def test_normalized_bounds_are_full_wall_clock_strings(client):
    eid = _create(client).json()["entry"]["id"]
    row = allowlist.get(eid)
    for col in ("valid_from", "valid_to"):
        timewin.parse(row[col])                 # 解析不了就是格式錯了
        assert len(row[col]) == 19, f"{col} 必須含秒：{row[col]!r}"
        assert " " in row[col] and "T" not in row[col]
