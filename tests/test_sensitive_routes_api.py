"""敏感路由端點：行為 + 留痕。

**移除一條敏感路由就是製造盲區**，所以約束不是「阻止」（guard() 不分級）而是
留痕 + 可見：必填理由、寫入 audit_log 且 target 帶 before→after、
發 Slack ops 訊息、資安總覽把它算進「目前有多少監測被關閉」。
"""
from __future__ import annotations

from console.store import db, sensitive_routes as sr

NEW = "zzz_api_test/route"


def _cleanup():
    with db.tx() as conn:
        conn.execute("DELETE FROM sensitive_routes WHERE route = ?", (NEW,))


def test_get_lists_routes_and_names_both_readers(client):
    body = client.get("/api/sensitive-routes").json()
    assert set(body) >= {"routes", "readers", "summary"}
    routes = {r["route"] for r in body["routes"]}
    assert set(sr.active()) <= routes
    row = body["routes"][0]
    assert set(row) == {"route", "status", "added_by", "added_at", "reason",
                        "removed_by", "removed_at"}
    # 影響範圍必須由後端說出來，前端不自己列一份 —— 它同時影響即時規則與掃描，
    # 而使用者從規則頁面點進來，預設只會想到 R05。
    text = " ".join(body["readers"])
    assert "R05" in text
    assert "掃描" in text
    assert body["summary"]["active"] == len(sr.active())


def test_post_requires_reason(client):
    r = client.post("/api/sensitive-routes", json={"route": NEW})
    assert r.status_code == 400
    assert "理由" in r.json()["detail"]


def test_post_rejects_a_bad_route_shape(client):
    r = client.post("/api/sensitive-routes",
                    json={"route": "onlyonesegment", "reason": "測試"})
    assert r.status_code == 400
    r = client.post("/api/sensitive-routes",
                    json={"route": "a/b/c", "reason": "測試"})
    assert r.status_code == 400


def test_post_rejects_unknown_keys(client):
    r = client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "測試", "typo": 1})
    assert r.status_code == 400
    assert "typo" in r.json()["detail"]


def test_post_warns_when_the_route_does_not_exist_in_the_log(client,
                                                             slack_outbox):
    """打錯的路由不會報錯，只會永遠不生效 —— 所以要明說。

    同 allowlist 到期日留空的處理：可以，但不能安靜。
    """
    _cleanup()
    try:
        r = client.post("/api/sensitive-routes",
                        json={"route": NEW, "reason": "驗收測試"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "created"
        assert body["warnings"], "不存在的路由要帶 warnings"
        assert any("不存在" in w for w in body["warnings"])
        assert NEW in sr.active()
    finally:
        _cleanup()


def test_write_records_audit_with_before_after(client):
    _cleanup()
    try:
        before = len(sr.active())
        client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "驗收測試"})
        rows = db.rows(
            "SELECT action, target, reason FROM audit_log"
            " ORDER BY id DESC LIMIT 1")
        assert rows, "沒有寫 audit_log"
        entry = rows[0]
        assert "敏感路由" in entry["action"]
        assert NEW in entry["target"]
        # audit_log 沒有 diff 欄位 —— 不寫進 target 就永遠查不到改了什麼
        assert str(before) in entry["target"] and str(before + 1) in entry["target"]
        assert entry["reason"] == "驗收測試"
    finally:
        _cleanup()


def test_every_write_sends_an_ops_message(client, slack_outbox):
    """ops 訊息是唯一一個當事人改不掉的偵測型控制，不可為了消音而拿掉。

    反向守護，同 tests/test_allowlist_write.py 的同名測試。
    """
    _cleanup()
    try:
        slack_outbox.clear()
        client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "驗收測試"})
        assert slack_outbox, "新增敏感路由沒有發 ops 訊息"
        slack_outbox.clear()
        client.request("DELETE", f"/api/sensitive-routes/{NEW}",
                       json={"reason": "驗收測試"})
        assert slack_outbox, "移除敏感路由沒有發 ops 訊息"
    finally:
        _cleanup()


def test_cannot_disable_the_last_active_route(client):
    """清空清單一律 409。

    實測 ClickHouse 的 `IN []` 不報錯、靜靜回 0 筆 —— 空清單等於 R05 靜靜
    失效。要關掉 R05 請停用規則（那會出現在資安總覽的橫幅上）。

    **還原不可以用 `sr.add()`。** 它會覆寫 `added_by`/`added_at`/`reason`
    並清掉 `removed_by`/`removed_at` —— 對種子列（本來就沒被停用過）來說，
    那是靜靜改寫了它的來源紀錄。這裡先存下每一列的原始欄位，事後逐欄還原，
    而不是呼叫「新增或重新啟用」這個語意不同的函式。DB 複本是 session 範圍的，
    這裡的損壞會一路帶到之後的測試。
    """
    active = sr.active()
    assert len(active) > 1, "前提：至少兩條，否則這個測試會真的清空清單"
    originals = {route: sr.get(route) for route in active}
    disabled: list[str] = []
    try:
        for route in active[:-1]:
            r = client.request("DELETE", f"/api/sensitive-routes/{route}",
                               json={"reason": "測試清空防護"})
            assert r.status_code == 200, r.text
            disabled.append(route)
        last = active[-1]
        r = client.request("DELETE", f"/api/sensitive-routes/{last}",
                           json={"reason": "測試清空防護"})
        assert r.status_code == 409, r.text
        assert "停用規則" in r.json()["detail"]
        assert sr.active() == [last]
    finally:
        with db.tx() as conn:
            for route in disabled:
                orig = originals[route]
                conn.execute(
                    "UPDATE sensitive_routes SET status = ?, added_by = ?,"
                    " added_at = ?, reason = ?, removed_by = ?, removed_at = ?"
                    " WHERE route = ?",
                    (orig["status"], orig["added_by"], orig["added_at"],
                     orig["reason"], orig["removed_by"], orig["removed_at"],
                     route))


def test_delete_a_missing_route_is_404(client):
    # 路由參數是 `{route:path}`，所以斜線直接寫、不要用 %2F ——
    # 編碼的斜線在不同的 ASGI 層有不同的解碼時機，那會變成一個脆弱的測試。
    r = client.request("DELETE", "/api/sensitive-routes/nope_test/nope",
                       json={"reason": "測試"})
    assert r.status_code == 404


def test_overview_banner_counts_disabled_sensitive_routes(client):
    """移除的路由必須出現在「目前有多少監測被我們自己關閉」的橫幅上。

    少了這個數字，一個刻意的盲區就只有進到規則頁的人知道 —— 那正是
    CLAUDE.md 對 allowlist、停用規則、Slack 關閉一再要求的同一件事。
    """
    body = client.get("/api/overview?minutes=60").json()
    s = body["suppression"]
    assert "disabled_sensitive_routes" in s
    assert "active_sensitive_routes" in s
    assert s["active_sensitive_routes"] == sr.active_count()
    assert s["disabled_sensitive_routes"] == sr.disabled_count()


def test_audit_mode_page_lists_the_new_write_endpoints():
    """web/pages/audit-mode.js 是對稽查人員的承諾清單。

    CLAUDE.md：新增可寫端點必須同步。宣稱一個不存在的控制比什麼都不說更糟，
    反過來漏掉一個真實的可寫端點也一樣。
    """
    from pathlib import Path
    text = Path("web/pages/audit-mode.js").read_text(encoding="utf-8")
    assert "敏感路由" in text, "audit-mode.js 沒有提到敏感路由的可寫端點"


def test_overview_banner_counts_survive_rule_yaml_failure(client, monkeypatch):
    """`_suppression_summary()` 的降級分支（規則 YAML 讀取失敗）一樣要帶

    這兩個數字。那個分支跑的情境是「規則一條都沒在跑」—— 正是最需要看見
    「我們自己關掉了什麼」的時候，而它是這個 task 裡最容易被漏掉的分支
    （brief 明說理由同既有的 `slack` 鍵，這裡連同 `slack` 一起斷言，
    讓這個測試守住整個降級分支的契約，不只是這個 task 加的那一半）。
    """
    from console.api import routes as routes_module

    def _boom():
        raise RuntimeError("規則 YAML 壞掉（測試模擬）")

    monkeypatch.setattr(routes_module.effective, "effective_rules", _boom)
    body = client.get("/api/overview?minutes=60").json()
    s = body["suppression"]
    assert s["available"] is False
    assert "slack" in s
    assert s["active_sensitive_routes"] == sr.active_count()
    assert s["disabled_sensitive_routes"] == sr.disabled_count()
