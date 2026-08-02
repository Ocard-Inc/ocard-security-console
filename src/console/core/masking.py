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


def actor(account: object) -> str | None:
    """後台帳號（`acc` / `_admin` / `_user_admin`）。原樣顯示。"""
    return _plain(account)


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
