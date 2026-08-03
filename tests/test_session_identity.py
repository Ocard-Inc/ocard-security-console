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
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_session_endpoint_exposes_the_signed_in_email(client):
    """前端要能拿到登入者的 email —— 沒有它，任何「這是誰做的」都只能靠猜。"""
    body = client.get("/api/session").json()
    assert "email" in body and body["email"], body
    assert "@" in body["email"], body
    # 離線模式要能被前端辨認出來（畫面上必須說那是假身分）
    assert body["auth_source"] in ("ros", "dev"), body


def test_frontend_assigns_state_user_from_the_session():
    """`loadSession()` 必須把 session 的 email 指派給 `state.user`。

    只設 authSource 的話，`state.user` 會永遠停在 lib.js 的離線預設值 ——
    而那個值長得像一個正常的 email，所以沒有人會發現。
    """
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert re.search(r"state\.user\s*=\s*this\.session\.email", src), \
        "app.js 的 loadSession() 沒有把 session.email 指派給 state.user"


def test_state_user_default_is_only_a_dev_placeholder():
    """lib.js 的預設值只能是離線用的假身分，而且必須註明。

    這個預設值本身不是問題（離線模式需要它），問題是它看起來像真的。
    上面那條測試保證它會被真實身分蓋掉；這一條保證它不會被當成正式預設值。
    """
    src = (WEB / "lib.js").read_text(encoding="utf-8")
    m = re.search(r"user:\s*'([^']+)'", src)
    assert m, "lib.js 的 state 少了 user 欄位"
    assert "離線" in src, "lib.js 必須註明那個預設值只在離線模式有意義"


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
