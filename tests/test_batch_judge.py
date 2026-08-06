"""批次判定（`POST /api/events/judge`）。

這個端點與單筆的 `/{evt_no}/judge` 有三個刻意的差異，每一個都是「錯了不會報錯、
只會靜靜給出錯結果」的形狀，所以逐條用行為驗證：

① 留空的欄位**不覆寫**事件原本的文字（單筆是完整取代）。
② `evt_nos` 有一個查不到就整批失敗，**一列都不寫**（沒有部分成功）。
③ 稽核是**逐筆一列**（不是一批一列）—— audit-mode.js 對稽查人員的承諾是
   「每一筆判定都查得到」，一批一列會讓其中 N-1 筆用 evt_no 搜不到。

測試跑在 conftest 的 DB 複本上（見 state_db），所以可以放心寫入。
"""
from __future__ import annotations

import pytest

from console.api.routes import _judgement_detail
from console.core import timewin
from console.store import db


@pytest.fixture()
def two_events(client) -> list[str]:
    """兩筆專供本檔案使用的事件。

    **刻意自己建，不挑真實事件**：本機 DB 只有一筆（EVT-0001），挑不到兩筆時
    skip 等於這整組斷言在本機從來沒跑過 —— 而「批次判定」正是要在多筆之間
    才會出錯的東西。跑在 conftest 的複本上，結束後刪掉。
    """
    now = timewin.fmt(timewin.taipei_now())
    evt_nos = ["EVT-BT01", "EVT-BT02"]
    with db.tx() as conn:
        conn.execute("DELETE FROM events WHERE evt_no IN (?,?)", tuple(evt_nos))
        for i, evt_no in enumerate(evt_nos):
            conn.execute(
                "INSERT INTO events (evt_no, rule_id, rule_name, severity, entity_key,"
                " entity_label, source_key, metric_value, threshold, first_seen,"
                " last_seen, peak_value, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evt_no, "R13", "單一分店高量呼叫", "P2", f"R13|9990{i}|1",
                 f"批次判定測試 {i}", "api", 100.0, 50.0, now, now, 100.0, "active"))
    yield evt_nos
    with db.tx() as conn:
        conn.execute("DELETE FROM events WHERE evt_no IN (?,?)", tuple(evt_nos))


def _detail(client, evt_no: str) -> dict:
    """讀回三個欄位。

    刻意走 DB + `_judgement_detail`（端點自己用的同一個解析函式）而不是
    `GET /events/{evt_no}` —— 後者每次都會打一趟 ClickHouse 拉趨勢圖，
    這裡要驗的是寫入語意，不是那張圖。API 層的讀回由
    `test_judgement_detail_is_readable_through_the_api` 顧。
    """
    row = db.one("SELECT judgement_note FROM events WHERE evt_no = ?", (evt_no,))
    return _judgement_detail(row["judgement_note"])


def _judgement(evt_no: str) -> str | None:
    return db.one("SELECT judgement FROM events WHERE evt_no = ?", (evt_no,))["judgement"]


def test_batch_applies_to_every_selected_event(client, two_events):
    r = client.post("/api/events/judge",
                    json={"evt_nos": two_events, "judgement": "誤報",
                          "reason": "批次驗收"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["judgement"] == "誤報"
    for evt_no in two_events:
        assert _judgement(evt_no) == "誤報"
        assert _detail(client, evt_no)["reason"] == "批次驗收"


def test_judgement_detail_is_readable_through_the_api(client, two_events):
    """批次寫的東西，事件詳細頁真的讀得回來。

    這是 `judgement_note` 一度只寫不讀的那個坑（見 routes._judgement_detail）：
    寫入端測過了、畫面上仍然什麼都沒有。
    """
    client.post("/api/events/judge",
                json={"evt_nos": two_events[:1], "judgement": "合法整合",
                      "reason": "客戶自家 APP"})
    got = client.get(f"/api/events/{two_events[0]}").json()
    assert got["judgement"] == "合法整合"
    assert got["judgement_detail"] == {
        "reason": "客戶自家 APP", "evidence": "", "next_step": ""}


def test_blank_field_does_not_erase_existing_text(client, two_events):
    """① 留空 = 不動，不是清空。

    批次送出的是「這一批共同的說法」，而選取裡可能有別人已經寫過證據的事件。
    一律取代的話那些字會靜靜消失 —— 而畫面上只會顯示「已判定 N 筆」。
    """
    evt_no = two_events[0]
    client.post(f"/api/events/{evt_no}/judge",
                json={"judgement": "保持觀察", "reason": "原本的理由",
                      "evidence": "原本的證據", "next_step": "原本的下一步"})
    r = client.post("/api/events/judge",
                    json={"evt_nos": [evt_no], "judgement": "誤報",
                          "reason": "改判：客戶自家 APP"})
    assert r.status_code == 200, r.text
    assert r.json()["kept"] == ["evidence", "next_step"]
    detail = _detail(client, evt_no)
    assert detail["reason"] == "改判：客戶自家 APP"
    assert detail["evidence"] == "原本的證據", "留空的欄位把別人寫的證據清掉了"
    assert detail["next_step"] == "原本的下一步"


def test_single_endpoint_still_replaces_all_three(client, two_events):
    """單筆的語意**不可以**跟著批次改成合併。

    單筆表單三個欄位都顯示現值並一起送出，所以清空某一欄就是要清空它 ——
    改成合併的話那個動作會變成「按了沒反應」。
    """
    evt_no = two_events[0]
    client.post(f"/api/events/{evt_no}/judge",
                json={"judgement": "保持觀察", "reason": "甲", "evidence": "乙"})
    client.post(f"/api/events/{evt_no}/judge",
                json={"judgement": "保持觀察", "reason": "甲"})
    assert _detail(client, evt_no)["evidence"] == ""


def test_unknown_event_fails_the_whole_batch(client, two_events):
    """② 沒有部分成功：有一筆查不到就一列都不寫。"""
    before = [_judgement(n) for n in two_events]
    r = client.post("/api/events/judge",
                    json={"evt_nos": [*two_events, "EVT-9999"], "judgement": "誤報"})
    assert r.status_code == 404
    assert "EVT-9999" in r.json()["detail"]
    assert [_judgement(n) for n in two_events] == before, "整批失敗時仍然改到了其他事件"


def test_audit_writes_one_row_per_event(client, two_events):
    """③ 逐筆一列，且 target 帶批次標記。

    一批只寫一列的話，稽核頁用 evt_no 搜尋會搜不到其中一筆 —— 而
    web/pages/audit-mode.js 對稽查人員寫的是「每一筆判定都寫入 audit_log」。
    """
    client.post("/api/events/judge",
                json={"evt_nos": two_events, "judgement": "證據不足"})
    for evt_no in two_events:
        row = db.one(
            "SELECT target FROM audit_log WHERE action = '變更事件狀態'"
            " AND target LIKE ? ORDER BY id DESC LIMIT 1", (f"{evt_no}：%",))
        assert row is not None, f"{evt_no} 的判定沒有留下稽核紀錄"
        assert "判定為 證據不足" in row["target"]
        assert "批次 2 筆" in row["target"], "看不出這筆是某一批的一部分"


def test_response_reports_what_gets_overwritten(client, two_events):
    """覆寫要說出來 —— 前端在按下去之前也顯示同一段話。"""
    client.post(f"/api/events/{two_events[0]}/judge", json={"judgement": "合法整合"})
    r = client.post("/api/events/judge",
                    json={"evt_nos": two_events, "judgement": "誤報"})
    body = r.json()
    overwritten = {o["evt_no"]: o["from"] for o in body["overwritten"]}
    assert overwritten.get(two_events[0]) == "合法整合"
    assert any("已被覆寫" in w for w in body["warnings"])


def test_blank_batch_says_so(client, two_events):
    """三欄全空是允許的，但**不可以安靜**（同單筆端點的 note）。"""
    r = client.post("/api/events/judge",
                    json={"evt_nos": two_events[:1], "judgement": "誤報"})
    assert r.json()["applied"] == {}
    assert any("沒有留下任何理由" in w for w in r.json()["warnings"])


def test_duplicates_are_collapsed(client, two_events):
    r = client.post("/api/events/judge",
                    json={"evt_nos": [two_events[0], two_events[0]],
                          "judgement": "誤報"})
    assert r.json()["count"] == 1


@pytest.mark.parametrize("payload, expected", [
    ({"evt_nos": [], "judgement": "誤報"}, 400),
    ({"evt_nos": "EVT-0001", "judgement": "誤報"}, 400),     # 字串會被逐字元跑
    ({"evt_nos": ["EVT-0001", ""], "judgement": "誤報"}, 400),
    ({"evt_nos": ["EVT-0001"], "judgement": "待判定"}, 400),  # 篩選值不是判定值
    ({"evt_nos": ["EVT-0001"], "judgement": "誤報x"}, 400),
    ({"evt_nos": ["EVT-0001"]}, 400),                        # 判定必填
    ({"evt_nos": ["EVT-0001"], "judgement": "誤報",
      "nextStep": "通知平台團隊"}, 400),                      # 未知欄位不可靜靜忽略
    ({"evt_nos": [f"EVT-{i:04d}" for i in range(301)],
      "judgement": "誤報"}, 400),                            # 超過一次的上限
])
def test_rejects_bad_input(client, payload, expected):
    assert client.post("/api/events/judge", json=payload).status_code == expected
