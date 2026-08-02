"""角色與權限守衛（server-side 強制，不依賴前端隱藏）。

身分來自 Ocard ROS 的登入 session（見 auth/ros.py）。ROS 的動態 RBAC feature
決定本主控台的角色：

    security.console → Viewer    security.analyst → Analyst    security.admin → Admin

未設定 `ros.base_url` 時（本機開發、ROS 尚未部署）退回 X-Dev-Role header 切換，
方便單機演示；正式環境務必設定 ros.base_url，否則任何人都能自稱 Admin。
"""
from __future__ import annotations

from enum import IntEnum

from fastapi import Depends, Header, HTTPException, Request

from console.auth import ros


class Role(IntEnum):
    VIEWER = 0
    ANALYST = 1
    ADMIN = 2


ROLE_LABELS = {
    Role.VIEWER: "Security Viewer",
    Role.ANALYST: "Security Analyst",
    Role.ADMIN: "Security Admin",
}

_BY_NAME = {"viewer": Role.VIEWER, "analyst": Role.ANALYST, "admin": Role.ADMIN}

# ROS feature key → 本主控台角色（取最高者）
FEATURE_ROLE = {
    "security.console": Role.VIEWER,
    "security.analyst": Role.ANALYST,
    "security.admin": Role.ADMIN,
}

# 需要的最低角色
PERMISSIONS = {
    "view_overview": Role.VIEWER,
    "view_events": Role.VIEWER,
    "view_quick": Role.VIEWER,
    "view_health": Role.VIEWER,
    "view_auditmode": Role.VIEWER,
    "use_explorer": Role.ANALYST,
    "view_masked_detail": Role.ANALYST,
    "manage_cases": Role.ANALYST,
    "judge_event": Role.ANALYST,
    "export_evidence": Role.ANALYST,
    "use_sql_console": Role.ADMIN,
    "manage_rules": Role.ADMIN,
    "manage_allowlist": Role.ADMIN,
    "view_audit_log": Role.ADMIN,
    "raw_drilldown": Role.ADMIN,
}


class CurrentUser:
    def __init__(self, email: str, role: Role, *, name: str = "",
                 source: str = "ros", ros_role_name: str | None = None) -> None:
        self.email = email
        self.role = role
        self.name = name or email.split("@")[0]
        self.source = source              # ros / dev
        self.ros_role_name = ros_role_name

    @property
    def role_name(self) -> str:
        return self.role.name.lower()

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    def can(self, permission: str) -> bool:
        required = PERMISSIONS.get(permission)
        if required is None:
            raise KeyError(f"未定義的權限 {permission!r}")
        return self.role >= required

    def allowed_permissions(self) -> list[str]:
        return [p for p in PERMISSIONS if self.can(p)]


class NotLoggedIn(HTTPException):
    """未登入 —— 前端需導向 ROS 登入頁。"""

    def __init__(self, next_path: str = "/") -> None:
        super().__init__(status_code=401, detail={
            "code": "not_logged_in",
            "message": "尚未登入 Ocard ROS。",
            "login_url": ros.login_url(next_path) if ros.enabled() else None,
        })


class NoSecurityAccess(HTTPException):
    """已登入但沒有任何 security.* feature —— 顯示無權限頁，不是登入頁。"""

    def __init__(self, email: str) -> None:
        super().__init__(status_code=403, detail={
            "code": "no_security_access",
            "message": "你尚未取得資安監控權限。",
            "email": email,
            "hint": "請聯繫 Security Admin 於 ROS 的「設定 → 角色」為你的角色"
                    "勾選「資安監控」功能後再試。",
        })


def role_from_features(features: tuple[str, ...] | list[str]) -> Role | None:
    """取 feature 對應的最高角色；完全沒有 security.* 時回 None。"""
    roles = [r for key, r in FEATURE_ROLE.items() if key in features]
    return max(roles) if roles else None


def current_user(
    request: Request,
    x_dev_role: str = Header(default="admin", alias="X-Dev-Role"),
    x_dev_user: str = Header(default="dev@olis.com.tw", alias="X-Dev-User"),
) -> CurrentUser:
    """FastAPI dependency：ROS session 優先，未設定 ROS 時退回 dev header。"""
    if not ros.enabled():
        role = _BY_NAME.get(x_dev_role.lower())
        if role is None:
            raise HTTPException(status_code=400, detail=f"未知角色 {x_dev_role!r}")
        return CurrentUser(email=x_dev_user, role=role, source="dev")

    next_path = request.url.path
    try:
        user = ros.resolve_user(dict(request.cookies))
    except ros.RosUnavailable as exc:
        # ROS 掛掉時不可默默放行，也不該說「你未登入」（誤導使用者去重新登入）
        raise HTTPException(status_code=503, detail={
            "code": "ros_unavailable",
            "message": f"無法向 Ocard ROS 驗證登入狀態：{exc}",
        }) from exc

    if user is None:
        raise NotLoggedIn(next_path)
    if not user.active:
        raise NoSecurityAccess(user.email)

    role = role_from_features(user.features)
    if role is None:
        raise NoSecurityAccess(user.email)
    return CurrentUser(email=user.email, role=role, name=user.name,
                       source="ros", ros_role_name=user.role_name)


def guard(user: CurrentUser, permission: str) -> None:
    """在 route 內呼叫；權限不足回 403（前端顯示「權限不足」頁）。"""
    if not user.can(permission):
        raise HTTPException(status_code=403, detail={
            "code": "insufficient_role",
            "message": f"你目前的角色（{user.role_label}）無法使用此功能。"
                       "需要 Security Admin 於 ROS 為你調整權限。",
        })


CurrentUserDep = Depends(current_user)
