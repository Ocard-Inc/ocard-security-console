"""操作稽核的檢視端點。

主要守 `store/audit.recent()` 的欄名內插：它把篩選欄名直接 f-string 進 SQL。
route 那邊只用具名參數，但白名單要跟著**這個函式** —— 它是公開函式，
第二個呼叫者不會知道那個 f-string 的危險性（同 core/ch.py 對 identifier 的原則）。
"""
from __future__ import annotations

import pytest

from console.store import audit, db


# ─────────────────── store 層的欄名白名單 ───────────────────

def test_unknown_filter_column_raises():
    with pytest.raises(ValueError, match="未知的稽核篩選欄位"):
        audit.recent(5, actor="x")


def test_injection_shaped_column_name_raises():
    """Python 允許 f(**{"任意字串": v})，所以這是真的能送進來的形狀。"""
    with pytest.raises(ValueError):
        audit.recent(5, **{"who = 'x' OR 1=1 --": "y"})


def test_count_uses_the_same_whitelist():
    with pytest.raises(ValueError):
        audit.count(role_name="x")


# ─────────────────── route 層 ───────────────────

def test_basic_query(client):
    r = client.get("/api/audit?limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) <= 5
    # 「顯示 N 筆，共 M 筆」不可省 —— 默默截斷會讓人以為這就是全部
    assert body["total"] >= len(body["rows"])
    assert body["returned"] == len(body["rows"])


def test_actions_come_from_the_data_not_a_hardcoded_list(client):
    """設計稿那份寫死的動作名與程式實際寫入的字串不一致。"""
    actions = client.get("/api/audit?limit=1").json()["actions"]
    assert actions
    real = {r["action"] for r in db.rows("SELECT DISTINCT action FROM audit_log")}
    assert set(actions) == real


def test_applied_filters_distinguishes_no_filter_from_no_result(client):
    """空字串的篩選被當成「不篩選」—— 畫面要看得出兩者的差別。"""
    body = client.get("/api/audit?who=&limit=3").json()
    assert body["applied_filters"] == {}
    body = client.get("/api/audit?who=nobody@example.com&limit=3").json()
    assert body["applied_filters"] == {"who": "nobody@example.com"}
    assert body["rows"] == [] and body["total"] == 0


def test_sql_injection_in_a_value_is_bound_not_interpolated(client):
    r = client.get("/api/audit?who=%27%20OR%201%3D1%20--&limit=5")
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_limit_bounds(client):
    assert client.get("/api/audit?limit=0").status_code == 422
    assert client.get("/api/audit?limit=999").status_code == 422


def test_bad_time_format_is_400_not_500(client):
    r = client.get("/api/audit?start=not-a-time")
    assert r.status_code == 400


def test_keyset_paging_does_not_repeat_rows(client):
    first = client.get("/api/audit?limit=5").json()
    if not first["has_more"]:
        pytest.skip("稽核紀錄不足 5 筆")
    second = client.get(
        f"/api/audit?limit=5&before_id={first['next_before_id']}").json()
    assert {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]} == set()


def test_notes_explain_the_empty_columns(client):
    """query_text 不落盤、案件欄永遠空 —— 空欄位會讓人以為資料掉了。"""
    notes = " ".join(client.get("/api/audit?limit=1").json()["notes"])
    assert "比對碼" in notes
    assert "案件" in notes


def test_reason_is_scrubbed_before_it_lands(client):
    """理由是人工輸入，而它會回到所有使用者的畫面上。"""
    audit.record(who="t@olis.com.tw", role="測試", action="__zzprobe",
                 target="t", reason="客戶 0912345678 與 foo@example.com 反映")
    try:
        row = client.get("/api/audit?action=__zzprobe&limit=1").json()["rows"][0]
        assert "0912345678" not in row["reason"]
        assert "foo@example.com" not in row["reason"]
        assert "09********" in row["reason"]
    finally:
        with db.tx() as conn:
            conn.execute("DELETE FROM audit_log WHERE action = '__zzprobe'")


def test_viewing_the_audit_log_does_not_audit_itself(client):
    """記了只會讓稽核表自我指涉，而且每次翻頁都把真正的操作推出第一頁。"""
    before = audit.count()
    client.get("/api/audit?limit=3")
    assert audit.count() == before
