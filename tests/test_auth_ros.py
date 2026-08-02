"""ROS 登入整合：授權判定、未登入、無權限、ROS 不可用。

沒有角色分級 —— 有 security.console 就有全部功能，沒有就進不來。
不打真的 ROS —— 以 requests.get 的 mock 模擬各種回應。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from console.auth import ros
from console.auth.roles import has_access


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _me(features: list[str], *, email="someone@olis.com.tw", active=True) -> FakeResponse:
    return FakeResponse(200, {"success": True, "data": {"user": {
        "email": email, "name": "Someone", "features": features,
        "roleName": "資安值班", "active": active}}})


@pytest.fixture(autouse=True)
def _ros_enabled(monkeypatch):
    """啟用 ROS 驗證（網址來自環境變數），並在每個測試前後清掉身分快取。

    掛載路徑由 CONSOLE_BASE_URL 的 path 推導 —— 這裡設 /security，
    等同於部署在 https://ros.example.com/security。
    """
    monkeypatch.setenv("ROS_BASE_URL", "https://ros.example.com")
    monkeypatch.setenv("CONSOLE_BASE_URL", "https://ros.example.com/security")
    with patch.object(ros, "_cfg", return_value={"enabled": True,
                                                 "cache_ttl_seconds": 30}):
        ros.clear_cache()
        yield
        ros.clear_cache()


# ─────────────── 授權判定 ───────────────

def test_console_feature_is_the_only_gate():
    assert has_access(["security.console"]) is True
    assert has_access(["nav.dashboards", "settings.roles"]) is False
    assert has_access([]) is False


# ─────────────── ROS session 解析 ───────────────

def test_no_session_cookie_means_not_logged_in():
    assert ros.resolve_user({}) is None
    assert ros.resolve_user({"other_cookie": "x"}) is None


def test_resolves_user_from_ros():
    with patch.object(ros.requests, "get", return_value=_me(["security.admin"])) as g:
        user = ros.resolve_user({"authjs.session-token": "abc123"})
        assert user.email == "someone@olis.com.tw"
        assert user.has("security.admin")
        # 只轉發 session cookie，不把使用者其他 cookie 一起送去 ROS
        assert g.call_args.kwargs["cookies"] == {"authjs.session-token": "abc123"}


def test_secure_cookie_name_also_accepted():
    with patch.object(ros.requests, "get", return_value=_me(["security.console"])):
        user = ros.resolve_user({"__Secure-authjs.session-token": "abc"})
        assert user is not None


def test_401_from_ros_means_not_logged_in():
    with patch.object(ros.requests, "get", return_value=FakeResponse(401)):
        assert ros.resolve_user({"authjs.session-token": "expired"}) is None


def test_connection_error_raises_unavailable_not_logged_out():
    """ROS 掛掉必須與「未登入」區分，否則會把人導去登入頁繞圈。"""
    with patch.object(ros.requests, "get", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(ros.RosUnavailable):
            ros.resolve_user({"authjs.session-token": "abc"})


def test_server_error_raises_unavailable():
    with patch.object(ros.requests, "get", return_value=FakeResponse(500)):
        with pytest.raises(ros.RosUnavailable):
            ros.resolve_user({"authjs.session-token": "abc"})


def test_result_is_cached_within_ttl():
    with patch.object(ros.requests, "get", return_value=_me(["security.console"])) as g:
        ros.resolve_user({"authjs.session-token": "same-token"})
        ros.resolve_user({"authjs.session-token": "same-token"})
        assert g.call_count == 1, "同一 session 在 TTL 內不該重複打 ROS"


def test_login_url_includes_mount_path():
    url = ros.login_url("/")
    assert url.startswith("https://ros.example.com/login?callbackUrl=")
    assert "%2Fsecurity%2F" in url or "%2Fsecurity" in url


# ─────────────── 端到端：透過 API 驗證守衛 ───────────────

def test_api_requires_login_when_ros_enabled(client):
    with patch.object(ros.requests, "get", return_value=FakeResponse(401)):
        r = client.get("/api/session", cookies={"authjs.session-token": "expired"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["code"] == "not_logged_in"
    assert detail["login_url"].startswith("https://ros.example.com/login")


def test_login_callback_never_points_at_an_api_path(client):
    """登入完要回到主控台畫面，不是回到 API 端點（否則使用者會看到一坨 JSON）。"""
    with patch.object(ros.requests, "get", return_value=FakeResponse(401)):
        r = client.get("/api/overview", cookies={"authjs.session-token": "expired"})
    login_url = r.json()["detail"]["login_url"]
    assert "api" not in login_url.split("callbackUrl=")[1], login_url
    assert login_url.endswith("%2Fsecurity%2F")


def test_api_rejects_user_without_security_feature(client):
    with patch.object(ros.requests, "get", return_value=_me(["nav.dashboards"])):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "no_security_access"


def test_api_rejects_inactive_user(client):
    with patch.object(ros.requests, "get",
                      return_value=_me(["security.console"], active=False)):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
    assert r.status_code == 403


def test_console_feature_grants_full_access(client):
    """有 security.console 就能用全部功能，沒有分級。"""
    with patch.object(ros.requests, "get", return_value=_me(["security.console"])):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
        assert r.status_code == 200
        body = r.json()
        assert body["auth_source"] == "ros"
        assert body["email"] == "someone@olis.com.tw"
        # 顯示的是 ROS 的角色名，不是主控台自己發明的等級
        assert body["role_label"] == "資安值班"

        # 過去分級時 Viewer 會被擋在 Log Explorer 外，現在人人可用
        ros.clear_cache()
        allowed = client.post("/api/explorer",
                              cookies={"authjs.session-token": "abc"},
                              json={"source": "api", "analysis": "trend",
                                    "start": "2026-08-01 12:00:00",
                                    "end": "2026-08-01 13:00:00"})
        assert allowed.status_code == 200, allowed.text


def test_ros_unavailable_returns_503_not_401(client):
    with patch.object(ros.requests, "get", side_effect=requests.ConnectionError("down")):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "ros_unavailable"


def test_index_redirects_to_ros_login_when_not_logged_in(client):
    with patch.object(ros.requests, "get", return_value=FakeResponse(401)):
        r = client.get("/", cookies={"authjs.session-token": "expired"},
                       follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://ros.example.com/login")


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


# ─────────────── 設定來源：網址必須來自環境變數 ───────────────

def test_urls_come_from_env_not_versioned_config(monkeypatch):
    """網址隨環境而異，不可寫死在進版控的 settings.yaml。"""
    from console.core import config

    monkeypatch.setenv("ROS_BASE_URL", "https://ros.prod.example/")
    monkeypatch.setenv("CONSOLE_BASE_URL", "https://ros.prod.example/security/")
    # 尾端斜線一律去掉，組出來的網址才不會出現 //
    assert config.ros_base_url() == "https://ros.prod.example"
    assert config.console_base_url() == "https://ros.prod.example/security"
    # 掛載路徑由對外網址推導，不需另外設定，也就不會互相矛盾
    assert config.console_mount_path() == "/security"


def test_mount_path_empty_when_no_subpath(monkeypatch):
    monkeypatch.setenv("CONSOLE_BASE_URL", "http://127.0.0.1:8600")
    from console.core import config
    assert config.console_mount_path() == ""


def test_blank_ros_url_disables_auth(monkeypatch):
    """留空 = 沒有登入保護（離線 demo），這點必須是明確行為而非意外。"""
    monkeypatch.setenv("ROS_BASE_URL", "")
    assert ros.enabled() is False
