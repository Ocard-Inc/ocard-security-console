"""登入身分從後端流到前端的那一段。

守的是一個實際發生過、而且完全靜默的 bug（2026-08 修）：

`web/lib.js` 的 `state.user` 初始值是離線模式的假身分 `dev@olis.com.tw`，
而 `app.js` 的 `loadSession()` 一度只指派 `state.authSource`、沒有指派
`state.user`。於是接了 ROS、以真實帳號登入時：

- 側邊欄顯示 `session.email` → **正確**
- 任何讀 `state.user` 的地方 → 拿到那個假 email

症狀不只是顯示錯。Allowlist 表單原本把 `state.user` 預填進「負責人」並送給
後端，而舊的寫入端是「payload 有值就用它」—— 結果**每一筆從 UI 建立的例外，
不論是誰建的，負責人都被存成 dev@olis.com.tw**，而畫面看起來完全正常。

這個檔案是結構性檢查（比照 `test_mount_prefix.py` 對 index.html 的做法）：
前端沒有測試框架，但「有沒有指派」這件事讀得出來，而它值得被擋住。
"""
from __future__ import annotations

import re
from unittest.mock import patch
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_session_endpoint_exposes_the_signed_in_email(client):
    """前端要能拿到登入者的 email —— 沒有它，任何「這是誰做的」都只能靠猜。"""
    body = client.get("/api/session").json()
    assert "email" in body and body["email"], body
    assert "@" in body["email"], body
    # 離線模式要能被前端辨認出來（畫面上必須說那是假身分）
    assert body["auth_source"] in ("ros", "dev"), body


def test_signed_in_ros_email_reaches_session_and_writes(client, monkeypatch):
    """接了 ROS 時，`/session` 與寫入端記錄的必須是**那個登入者**的 email。

    這條是「畫面顯示 dev@olis.com.tw」那個 bug 的後端側對照：前端的部分由
    `test_frontend_assigns_state_user_from_the_session` 守，這裡守的是
    「後端拿到的身分確實是 ROS 回的人，而且 X-Dev-User 完全無效」。
    """
    import requests
    from console.auth import ros

    signed_in = "vinek@olis.com.tw"

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "data": {"user": {
                "email": signed_in, "name": "逸生", "active": True,
                "roleName": "管理員", "features": ["security.console"]}}}

    monkeypatch.setenv("ROS_BASE_URL", "https://ros.example.com")
    monkeypatch.setenv("CONSOLE_BASE_URL", "")
    with patch.object(ros, "_cfg", return_value={"enabled": True,
                                                 "cache_ttl_seconds": 0}), \
            patch.object(requests, "get", return_value=FakeResponse()):
        ros.clear_cache()
        try:
            body = client.get(
                "/api/session",
                # 硬塞離線身分：ROS 啟用時它必須完全無效
                headers={"X-Dev-User": "attacker@example.com"},
                cookies={"authjs.session-token": "abc"}).json()
            assert body["email"] == signed_in, body
            assert body["auth_source"] == "ros", body
        finally:
            ros.clear_cache()


def test_frontend_assigns_state_user_from_the_session():
    """`loadSession()` 必須把 session 的 email 指派給 `state.user`。

    只設 authSource 的話，`state.user` 會永遠停在 lib.js 的離線預設值 ——
    而那個值長得像一個正常的 email，所以沒有人會發現。
    """
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert re.search(r"state\.user\s*=\s*this\.session\.email", src), \
        "app.js 的 loadSession() 沒有把 session.email 指派給 state.user"


def test_state_user_has_no_hardcoded_email():
    """`state.user` 的初始值必須是空字串，**不可以是任何一個 email**。

    寫死一個 email 的問題不是它錯，而是它**看起來完全正常** ——
    以真實帳號登入時畫面顯示那個假位址，沒有人會覺得需要去查。
    空字串讓「還不知道是誰」不可能被渲染成一個像真的帳號：顯示端要嘛拿到
    真身分，要嘛拿到空值並自己說「未取得」。
    離線模式的假身分由後端決定（auth/roles.py 的 X-Dev-User 預設值）。
    """
    src = (WEB / "lib.js").read_text(encoding="utf-8")
    m = re.search(r"^\s*user:\s*(.+?),\s*$", src, re.MULTILINE)
    assert m, "lib.js 的 state 少了 user 欄位"
    assert m.group(1) in ("''", '""'), \
        f"state.user 的初始值必須是空字串，目前是 {m.group(1)}"
    # 整個檔案不可以出現 email 字面值（註解裡提到歷史值是可以的，見下方過濾）
    code = "\n".join(line.split("//")[0] for line in src.splitlines())
    assert "@" not in code.replace("@param", ""), \
        "lib.js 的程式碼不可以出現任何 email 字面值"


def test_dev_user_header_is_not_sent_when_identity_is_unknown():
    """身分未知時不可以送空的 X-Dev-User。

    送 `X-Dev-User: `（空字串）比不送更糟：後端的 Header 預設值會被空字串蓋掉，
    CurrentUser.email 變空，`name` 走 email.split('@')[0] 也變空 ——
    畫面上就是一個沒有身分的操作者，而它會被寫進核准紀錄。
    """
    src = (WEB / "lib.js").read_text(encoding="utf-8")
    assert re.search(r"authSource === 'dev' && state\.user", src), \
        "lib.js 送 X-Dev-User 之前沒有檢查 state.user 是否有值"


def test_allowlist_form_labels_the_offline_identity():
    """離線模式下，創立人欄位必須說出「這是假身分」。

    不說的話畫面上是一個看起來完全正常的位址，而它會被寫進核准紀錄。
    """
    src = (WEB / "components" / "allowlist-form.js").read_text(encoding="utf-8")
    assert "creatorNote" in src, "少了離線身分的提示"
    assert "authSource === 'dev'" in src, "提示沒有依 authSource 判斷"


def test_allowlist_form_never_sends_owner():
    """創立人由後端從登入帳號寫入，前端送 owner 一律 400（見 allowlist_routes）。

    這條擋的是「有人為了讓畫面好看，把 owner 加回送出的 body」——
    那會讓寫入端變成 400，而使用者看到的是一個沒有說明的儲存失敗。
    """
    src = (WEB / "components" / "allowlist-form.js").read_text(encoding="utf-8")
    body = src[src.index("const body = {"):src.index("if (this.f.valid_from)")]
    # 註解裡出現「owner」是**應該的**（那裡寫的正是「刻意不送」的理由），
    # 所以先把 // 註解拿掉再比 —— 否則這條測試會擋住自己的說明文字。
    code = "\n".join(line.split("//")[0] for line in body.splitlines())
    assert "owner" not in code, f"送出的 body 不可以帶 owner：{code}"
