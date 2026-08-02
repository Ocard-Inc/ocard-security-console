"""ROS 登入整合：feature → 角色映射、未登入、無權限、ROS 不可用。

不打真的 ROS —— 以 requests.get 的 mock 模擬各種回應。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from console.auth import ros
from console.auth.roles import Role, role_from_features


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


BASE_CFG = {"base_url": "https://ros.example.com", "enabled": True,
            "mount_path": "/security", "cache_ttl_seconds": 30, "role_mode": "full"}


@pytest.fixture(autouse=True)
def _ros_enabled():
    """把 ROS 設定成啟用，並在每個測試前後清掉身分快取。"""
    with patch.object(ros, "_cfg", return_value=BASE_CFG):
        ros.clear_cache()
        yield
        ros.clear_cache()


@pytest.fixture
def tiered():
    """切成分級模式（未來要分 Viewer/Analyst/Admin 時的設定）。"""
    with patch.object(ros, "_cfg", return_value={**BASE_CFG, "role_mode": "tiered"}):
        ros.clear_cache()
        yield
        ros.clear_cache()


# ─────────────── feature → 角色映射 ───────────────

def test_full_mode_grants_admin_to_anyone_with_console_feature():
    """現況：進得來就是完整權限。"""
    assert role_from_features(["security.console"]) is Role.ADMIN


def test_tiered_mode_takes_highest(tiered):
    """分級機制仍在，切換設定即可啟用。"""
    assert role_from_features(["security.console"]) is Role.VIEWER
    assert role_from_features(["security.console", "security.analyst"]) is Role.ANALYST
    assert role_from_features(["security.analyst", "security.admin"]) is Role.ADMIN
    # 只給 admin 沒給 console 也算 Admin（勾選 admin 的意圖已經很明確）
    assert role_from_features(["security.admin"]) is Role.ADMIN


def test_no_security_feature_is_denied_in_both_modes(tiered):
    """沒有任何 security.* 一律擋下 —— full 模式也不能變成人人可進。"""
    assert role_from_features(["nav.dashboards", "settings.roles"]) is None
    assert role_from_features([]) is None
    assert role_from_features(["nav.dashboards"], mode="full") is None


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
                      return_value=_me(["security.admin"], active=False)):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
    assert r.status_code == 403


def test_console_feature_grants_full_access(client):
    """現況（role_mode=full）：勾了 security.console 就是完整權限。"""
    with patch.object(ros.requests, "get", return_value=_me(["security.console"])):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert body["auth_source"] == "ros"
        for perm in ("view_overview", "use_explorer", "use_sql_console",
                     "export_evidence", "view_audit_log"):
            assert perm in body["permissions"], f"full 模式應包含 {perm}"


def test_tiered_mode_restricts_viewer(client, tiered):
    """切成 tiered 後，只有 security.console 的人不能用 Log Explorer。"""
    with patch.object(ros.requests, "get", return_value=_me(["security.console"])):
        r = client.get("/api/session", cookies={"authjs.session-token": "abc"})
        assert r.json()["role"] == "viewer"
        assert "use_explorer" not in r.json()["permissions"]

        ros.clear_cache()
        denied = client.post("/api/explorer",
                             cookies={"authjs.session-token": "abc"},
                             json={"source": "api", "start": "2026-08-01 00:00:00",
                                   "end": "2026-08-01 01:00:00"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "insufficient_role"


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
