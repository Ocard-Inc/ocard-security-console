"""敏感資料遮罩：不可逆 fingerprint 與文字清洗。

設計稿硬性約束：UI／告警／匯出一律不出現原始 IP、帳號、token、secret、
cookie、手機、Email、會員 ID、訂單號。對外呈現統一為 fingerprint：

    src_XXXXXXXXXXXX      IP
    actor_XXXXXXXXXXXX    帳號（acc / _user_admin / _admin）
    token_XXXXXXXXXXXX    API token
    resource_XXXXXXXX     會員 / 訂單等資源識別

fingerprint = HMAC-SHA256(FP_SECRET, kind:value) 截 12（resource 8）hex 大寫。
同一輸入永遠得到同一輸出，因此可以當篩選鍵與跨頁關聯，但無法還原原文。
"""
from __future__ import annotations

import hashlib
import hmac
import re

from console.core.config import fp_secret

_PREFIX_LEN = {"src": 12, "actor": 12, "token": 12, "resource": 8}

_EMPTY = {None, "", "None", "null"}

# headers/params 內需要清洗的鍵（值以遮罩取代）
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(\"?(?:authorization|cookie|token|vtoken|password|pwd|secret|api[_-]?key)\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
# 疑似個資樣式：台灣手機、Email
_PHONE_RE = re.compile(r"\b09\d{8}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _digest(kind: str, value: str) -> str:
    mac = hmac.new(fp_secret(), f"{kind}:{value}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[: _PREFIX_LEN[kind]].upper()


def _fingerprint(kind: str, value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    if text in _EMPTY:
        return None
    return f"{kind}_{_digest(kind, text)}"


def src_fp(ip: object) -> str | None:
    return _fingerprint("src", ip)


def actor_fp(account: object) -> str | None:
    return _fingerprint("actor", account)


def token_fp(token: object) -> str | None:
    return _fingerprint("token", token)


def resource_fp(resource: object) -> str | None:
    return _fingerprint("resource", resource)


FP_FUNCS = {
    "src": src_fp,
    "actor": actor_fp,
    "token": token_fp,
    "resource": resource_fp,
}


def scrub_text(text: object, max_len: int = 300) -> str:
    """清洗自由文字（params/headers/error）：遮罩敏感鍵值與個資樣式後截斷。"""
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
    """params 不呈現原文，只回大小與頂層欄位類別（設計稿 20 節）。"""
    if text is None or str(text).strip() in _EMPTY:
        return "（空）"
    s = str(text)
    keys = re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]{0,30})[\"']\s*:", s[:2000])
    uniq = sorted(set(keys))[:8]
    fields = "、".join(uniq) if uniq else "無法解析為 JSON"
    return f"{len(s)} bytes · 欄位：{fields}"
