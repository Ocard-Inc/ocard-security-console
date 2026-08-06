"""對外呈現的識別值處理。

## 為什麼這裡幾乎不再遮罩

本主控台是**對內**的資安調查工具。使用者的工作就是「追究問題出在哪個帳號、哪個
來源、哪個品牌」，把這些值換成不可逆指紋等於讓工具無法完成它唯一的任務 ——
畫面上看到 `actor_56CAB2602AA1` 卻無法對應到任何人，事件就查不下去。

所以**後台帳號、來源 IP、訂單號與會員 ID 一律原樣顯示**。

## 仍然遮罩的兩類，以及理由

**1. API token（`token_fp`）** —— 那是**還有效的憑證**。顯示它等於任何有主控台
讀取權限的人都能冒用該商家的身分呼叫 API，這與「知道是哪個帳號出問題」是兩件事：
調查需要前者，不需要後者。token 仍以 HMAC 指紋呈現，同一個 token 永遠得到同一個
指紋，因此仍可當關聯鍵與去重鍵使用。

**2. `params` / `headers` 原文（`payload_summary` / `scrub_text`）** —— 裡面混著
`authorization`、`cookie`、`secret`、`api_key`，以及消費者的手機與 Email。這些不是
調查對象，是「順帶被記進 log 的東西」。而且它們的去向不只畫面：
`alerting/notify.py` 會把事件內容送進 Slack 頻道，應用 log 明文寫在
`state/logs/*.log`。預設呈現因此只給大小與欄位名稱。

要看完整原文有專門的**逐筆調閱**路徑（見 `api/routes.py` 的 payload 端點）：
一次只回一筆、並寫入 `audit_log`（誰、何時、哪一筆）。預設收斂 + 逐筆留痕，
比全面攤開安全，也比完全看不到有用。

## 命名

`actor()` / `src()` / `resource()` 回的是**原樣值**，不是指紋 —— 名字裡刻意不留
`fp`，避免下一個人以為它們還有遮罩效果。只有 `token_fp()` 名符其實。
"""
from __future__ import annotations

import hashlib
import hmac
import re

from console.core.config import fp_secret

_EMPTY = {None, "", "None", "null"}

# headers/params 內需要清洗的鍵（值以遮罩取代）
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(\"?(?:authorization|cookie|token|vtoken|password|pwd|secret|api[_-]?key)\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
# 疑似個資樣式：台灣手機、Email
_PHONE_RE = re.compile(r"\b09\d{8}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _plain(value: object) -> str | None:
    """原樣值。空值統一回 None，讓呼叫端能區分「沒有」與「空字串」。"""
    text = str(value).strip() if value is not None else ""
    return None if text in _EMPTY else text


# R07A 的新版登入端點把 acc 存在 params 裡（見
# config/rules/r07a_login_failed_acc.yaml 的 JSONExtractString fallback）——
# 那是攻擊者可以自由填寫的登入表單欄位，沒有經過任何長度限制就落地。舊的
# acc 欄位本來就可能很長（同一類風險不是新的），但這裡加一個上限：這個值會
# 變成事件去重鍵（entity_key）的一部分，也會原樣進 Slack 訊息文字，一個極長
# 的字串會讓去重鍵變得笨重、也可能撐大 Slack 訊息。上限刻意寬鬆（真實帳號名
# 遠遠不會碰到），只是把成本壓低，不影響任何真實識別值的顯示。
_ACTOR_MAX_LEN = 200


def actor(account: object) -> str | None:
    """後台帳號（`acc` / `_admin` / `_user_admin`）。原樣顯示，長度有上限。

    **超過上限的後綴刻意帶完整原文的雜湊，不是只帶原長。** 這個回傳值不只是
    顯示文字：`rules/engine.entity_parts()` 直接拿它當 `entity_key` 的一段，
    `store/events.py` 再拿 `entity_key` 去重。若後綴只由「原長」組成，
    兩個前 200 字元相同、原長也相同、但中間或尾端不同的帳號名會產生一模一樣
    的輸出 —— 兩個不同的攻擊來源被靜靜合併成同一個事件，`metric_value` 只留下
    該 tick 最後處理到的那筆，另一筆的存在完全消失（R07A 的 `params.acc` 是
    攻擊者自由填寫的登入表單欄位，實測 90 天內真實出現過 439 字元，這條路徑
    不是理論上的）。改成對**完整原字串**取 HMAC-SHA256 + `FP_SECRET`
    （複用 `token_fp()` 的作法，不另外發明第二種雜湊方式），碰不到剛好同前綴、
    同原長又同雜湊的兩個值。

    三個性質刻意保留：
    - 沒超過上限的值完全原樣返回（不雜湊、不加後綴）—— 絕大多數真實帳號都在
      這裡，既有測試也依賴這件事。
    - 後綴仍然要讓人一看就知道「這不是完整帳號名」，不會被誤認成真值。
    - 同一個輸入永遠得到同一個輸出：它是去重鍵的一部分，兩個 tick 看到同一個
      帳號必須落在同一個 `entity_key`。
    """
    text = _plain(account)
    if text is None:
        return None
    if len(text) > _ACTOR_MAX_LEN:
        mac = hmac.new(fp_secret(), f"actor:{text}".encode("utf-8"), hashlib.sha256)
        digest = mac.hexdigest()[:12].upper()
        text = text[:_ACTOR_MAX_LEN] + f"…（截斷，原長 {len(text)}，{digest}）"
    return text


def src(ip: object) -> str | None:
    """來源 IP。原樣顯示，包含多段 X-Forwarded-For 字串。"""
    return _plain(ip)


def resource(resource_id: object) -> str | None:
    """訂單號、會員 ID 等資源識別。原樣顯示 —— 清點外洩範圍需要它。"""
    return _plain(resource_id)


def token_fp(token: object) -> str | None:
    """API token → `token_XXXXXXXXXXXX`（HMAC-SHA256 + FP_SECRET，取 12 碼大寫）。

    **這一個仍然是指紋**，因為 token 是有效憑證，見模組說明。
    同一個 token 永遠得到同一個指紋，可當關聯鍵與去重鍵。
    """
    text = _plain(token)
    if text is None:
        return None
    mac = hmac.new(fp_secret(), f"token:{text}".encode("utf-8"), hashlib.sha256)
    return "token_" + mac.hexdigest()[:12].upper()


# 依「識別值種類」取得對外呈現函式。鍵與 config/rules/*.yaml 的 `entity[].fp`、
# `explorer.GROUP_BY` 的第二個元素、`sweep.probes.Probe.fp_kind` 相同。
DISPLAY_FUNCS = {
    "actor": actor,
    "src": src,
    "resource": resource,
    "token": token_fp,
}


def echoable(kind: str | None, value: str) -> bool:
    """這個原始值的對外呈現是否等於它本身 —— 也就是回送它會不會多洩漏東西。

    事件詳細頁的母體排名可以點任一列往下拆，而「那一列是誰」必須以原始值回送
    後端（它要拿去組 WHERE）。IP、endpoint、品牌編號、一般長度的帳號名在
    2026-08 的政策下都是**原樣顯示**，所以回送它們不會多洩漏任何東西；
    但 API token 的呈現是指紋（`token_fp()`），而 `actor()` 對超長帳號名會截斷
    並附 HMAC 摘要 —— 那兩種回送等於用主控台把不可逆的東西還原。

    **刻意用執行期比對，不是靜態的「哪些 kind 是單向的」清單**：
    `actor` 是否單向取決於**值的長度**，靜態清單一定會漂移，而漂移的方向是
    靜靜地把指紋當原值送出去。未知的 kind 一律回 False（往安全的方向倒）。
    """
    if kind is None:
        return True
    fn = DISPLAY_FUNCS.get(kind)
    if fn is None:
        return False
    return fn(value) == value


def scrub_text(text: object, max_len: int = 300) -> str:
    """清洗自由文字（params/headers/error）：遮罩憑證鍵值與個資樣式後截斷。

    用在「順帶帶出來的文字」上（規則 context、探針 evidence 的字串欄位）。
    要完整原文請走逐筆調閱端點。
    """
    if text is None:
        return ""
    s = str(text)
    s = _SENSITIVE_KEY_RE.sub(r"\1***", s)
    s = _PHONE_RE.sub("09********", s)
    s = _EMAIL_RE.sub("***@***", s)
    if len(s) > max_len:
        s = s[:max_len] + f"…（截斷，原長 {len(s)}）"
    return s


def payload_summary(text: object) -> str:
    """params 的預設呈現：只給大小與頂層欄位名稱，不給值。

    完整原文走逐筆調閱端點（一次一筆、寫入 audit_log）。
    """
    if text is None or str(text).strip() in _EMPTY:
        return "（空）"
    s = str(text)
    keys = re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]{0,30})[\"']\s*:", s[:2000])
    uniq = sorted(set(keys))[:8]
    fields = "、".join(uniq) if uniq else "無法解析為 JSON"
    return f"{len(s)} bytes · 欄位：{fields}"
