"""向 Ocard ROS 驗證登入狀態。

ROS（Next.js + NextAuth v5）是全公司內部系統的統一登入入口。本主控台掛在
ROS 同一網域的子路徑（例如 https://ros.ocard.co/security），因此瀏覽器會把
ROS 的 session cookie 一併送過來 —— 我們原樣轉發給 ROS 的 `/api/auth/me`，
由 ROS 判斷這個 session 是誰、有哪些 feature。

這樣做的理由：NextAuth 的 session cookie 是用 AUTH_SECRET 派生金鑰加密的 JWE，
在 Python 端自行解密要複製 NextAuth 的 HKDF 細節，且會隨 NextAuth 改版而壞。
轉發給 ROS 則永遠與 ROS 的認知一致，撤銷權限也即時生效。

授權來自 ROS 的動態 RBAC（lib/features.ts）的單一 feature `security.console`：
勾了就能用主控台的全部功能，沒勾的登入者會看到「無權限」頁，不是登入頁 ——
這兩件事必須在畫面上明確區分，否則使用者會一直重複登入卻進不來。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import requests

from console.core.config import settings

logger = logging.getLogger(__name__)

# NextAuth v5 的 session cookie（HTTPS 下帶 __Secure- 前綴）。
# 只轉發這幾個，不要把使用者其他 cookie 一起送出去。
SESSION_COOKIE_NAMES = (
    "authjs.session-token",
    "__Secure-authjs.session-token",
    # NextAuth v4 命名，ROS 若曾降版仍可相容
    "next-auth.session-token",
    "__Secure-next-auth.session-token",
)


@dataclass(frozen=True)
class RosUser:
    email: str
    name: str
    features: tuple[str, ...]
    role_name: str | None
    active: bool

    def has(self, feature: str) -> bool:
        return feature in self.features


class RosUnavailable(RuntimeError):
    """ROS 無法連線或回應異常 —— 與「未登入」不同，不可當成未登入處理。"""


def _cfg() -> dict:
    return settings().get("ros", {}) or {}


def base_url() -> str:
    return str(_cfg().get("base_url", "")).rstrip("/")


def enabled() -> bool:
    """未設定 ros.base_url 時停用 ROS 驗證，走開發模式的 header 角色切換。"""
    return bool(base_url()) and bool(_cfg().get("enabled", True))


def login_url(next_path: str = "/") -> str:
    """ROS 登入頁；登入完成後導回主控台的 next_path。

    ROS 的 middleware 用 callbackUrl 參數（見 ocard-ros/middleware.ts）。
    主控台掛在同網域子路徑，所以帶相對路徑即可，不會觸發 NextAuth 的
    跨站 callback 阻擋。
    """
    mount = str(_cfg().get("mount_path", "")).rstrip("/")
    target = f"{mount}{next_path}" if next_path.startswith("/") else f"{mount}/{next_path}"
    return f"{base_url()}/login?callbackUrl={quote(target, safe='')}"


def _session_cookies(cookies: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in cookies.items() if k in SESSION_COOKIE_NAMES}


# email → (取得時間, RosUser)。ROS 自己也有 10 秒快取，這裡再加一層是為了
# 避免單一頁面載入的數個 API 請求各打一次 ROS。TTL 短到讓權限異動幾乎即時生效。
_cache: dict[str, tuple[float, RosUser]] = {}
_cache_lock = threading.Lock()


def _cache_ttl() -> float:
    return float(_cfg().get("cache_ttl_seconds", 30))


def _cache_get(key: str) -> RosUser | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < _cache_ttl():
            return hit[1]
        if hit:
            del _cache[key]
    return None


def _cache_put(key: str, user: RosUser) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), user)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve_user(cookies: dict[str, str]) -> RosUser | None:
    """依 cookie 向 ROS 取得使用者；未登入回 None，ROS 不可用則拋 RosUnavailable。"""
    session = _session_cookies(cookies)
    if not session:
        return None

    cache_key = next(iter(session.values()))[-32:]
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = urljoin(base_url() + "/", "api/auth/me")
    try:
        resp = requests.get(url, cookies=session, timeout=8,
                            headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise RosUnavailable(f"無法連線 ROS 驗證登入：{exc}") from exc

    if resp.status_code == 401:
        return None
    if resp.status_code != 200:
        raise RosUnavailable(f"ROS 回應異常 HTTP {resp.status_code}")

    try:
        body = resp.json()
        data = (body.get("data") or {}).get("user") or {}
    except ValueError as exc:
        raise RosUnavailable("ROS 回應不是合法 JSON") from exc

    email = str(data.get("email") or "").strip().lower()
    if not email:
        raise RosUnavailable("ROS 回應缺少 email")

    user = RosUser(
        email=email,
        name=str(data.get("name") or email.split("@")[0]),
        features=tuple(data.get("features") or ()),
        role_name=data.get("roleName"),
        active=bool(data.get("active", True)),
    )
    _cache_put(cache_key, user)
    return user
