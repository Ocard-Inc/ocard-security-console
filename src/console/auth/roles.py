"""身分與存取控制（server-side 強制，不依賴前端隱藏）。

身分來自 Ocard ROS 的登入 session（見 auth/ros.py）。**沒有角色分級** ——
ROS 的角色勾了 `security.console` 就能用主控台的全部功能，沒勾就進不來。

顯示給使用者看的「角色」是 ROS 那邊的角色名稱（管理員、資訊主管…），
不是主控台自己發明的等級。

未設定 `ros.base_url` 時退回 X-Dev-User header（僅供離線 demo；
沒有登入保護，正式環境務必設定 ros.base_url）。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from console.auth import ros

# 進入主控台所需的 ROS feature。有它就有全部功能。
REQUIRED_FEATURE = "security.console"


class CurrentUser:
    def __init__(self, email: str, *, name: str = "", source: str = "ros",
                 ros_role_name: str | None = None) -> None:
        self.email = email
        self.name = name or email.split("@")[0]
        self.source = source              # ros / dev
        self.ros_role_name = ros_role_name

    @property
    def role_label(self) -> str:
        """畫面上顯示的身分。以 ROS 的角色名為準，沒有就退回泛稱。"""
        return self.ros_role_name or ("開發模式" if self.source == "dev" else "已授權使用者")


class NotLoggedIn(HTTPException):
    """未登入 —— 前端需導向 ROS 登入頁。"""

    def __init__(self, next_path: str = "/") -> None:
        # 登入完要回到畫面，不是回到 API 端點 —— 否則使用者登入後
        # 看到的是一坨 JSON。API 路徑一律改回主控台首頁。
        if next_path.startswith("/api"):
            next_path = "/"
        super().__init__(status_code=401, detail={
            "code": "not_logged_in",
            "message": "尚未登入 Ocard ROS。",
            "login_url": ros.login_url(next_path) if ros.enabled() else None,
        })


class NoSecurityAccess(HTTPException):
    """已登入但沒有 security.console —— 顯示無權限頁，不是登入頁。"""

    def __init__(self, email: str) -> None:
        super().__init__(status_code=403, detail={
            "code": "no_security_access",
            "message": "你尚未取得資安監控權限。",
            "email": email,
            "hint": "請聯繫 Security Admin 於 ROS 的「設定 → 角色權限」為你的角色"
                    "勾選「資安監控」後再試。",
        })


def has_access(features: tuple[str, ...] | list[str]) -> bool:
    return REQUIRED_FEATURE in features


def current_user(
    request: Request,
    x_dev_user: str = Header(default="dev@olis.com.tw", alias="X-Dev-User"),
) -> CurrentUser:
    """FastAPI dependency：ROS session 優先，未設定 ROS 時退回 dev header。"""
    if not ros.enabled():
        return CurrentUser(email=x_dev_user, source="dev")

    try:
        user = ros.resolve_user(dict(request.cookies))
    except ros.RosUnavailable as exc:
        # ROS 掛掉時不可默默放行，也不該說「你未登入」（誤導使用者去重新登入）
        raise HTTPException(status_code=503, detail={
            "code": "ros_unavailable",
            "message": f"無法向 Ocard ROS 驗證登入狀態：{exc}",
        }) from exc

    if user is None:
        raise NotLoggedIn(request.url.path)
    if not user.active or not has_access(user.features):
        raise NoSecurityAccess(user.email)
    return CurrentUser(email=user.email, name=user.name,
                       source="ros", ros_role_name=user.role_name)


# 端點功能標記的唯一真相。目前**不做分級**（見模組說明），但這個集合仍然有用：
# 打錯的權限字串是程式錯誤，而它原本是完全靜默的 —— 一個 typo 就讓「這個端點
# 屬於哪個功能」的標記失去意義，而且沒有任何地方會發現。
#
# 之後要恢復分級，這裡就是角色 → 權限對照表的落點。
PERMISSIONS = frozenset({
    "view_overview",
    "view_events",
    "judge_event",
    "use_explorer",
    "view_masked_detail",
    "view_raw_payload",
    "view_quick",
    "view_health",
    "run_sweep",
    # 規則與 Allowlist 管理（見 api/rules_routes.py、api/allowlist_routes.py）。
    # 注意：edit_rules 與 manage_allowlist 目前**擋不住任何人** ——
    # 這個功能的安全模型是「留痕 + 可見」而不是「阻止」，理由見那兩個模組。
    "view_rules",
    "edit_rules",
    "view_allowlist",
    "manage_allowlist",
    "view_audit",
})


def guard(user: CurrentUser, permission: str) -> None:
    """保留呼叫點以標示「這個端點屬於哪個功能」，目前不再分級。

    能通過 current_user 的人就有全部功能；真正的關卡是 ROS 的
    security.console。之後若要分級，改這裡即可。

    未知的權限字串一律拋 ValueError（→ 500）。那是程式錯誤，不是使用者錯誤，
    而且大聲失敗才會在測試打過端點時被抓到。
    """
    if permission not in PERMISSIONS:
        raise ValueError(
            f"未知的權限字串 {permission!r} —— 要新增請加進 auth/roles.PERMISSIONS")
    return None


CurrentUserDep = Depends(current_user)
