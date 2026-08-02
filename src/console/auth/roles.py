"""角色與權限守衛（server-side 強制，不依賴前端隱藏）。

Phase 4 接 Google SSO 前，以 dev header（X-Dev-Role）切換角色示範。
"""
from __future__ import annotations

from enum import IntEnum

from fastapi import Header, HTTPException


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
    def __init__(self, email: str, role: Role) -> None:
        self.email = email
        self.role = role

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


def current_user(
    x_dev_role: str = Header(default="admin", alias="X-Dev-Role"),
    x_dev_user: str = Header(default="vinek@olis.com.tw", alias="X-Dev-User"),
) -> CurrentUser:
    """FastAPI dependency。Phase 4 換成 SSO session 解析。"""
    role = _BY_NAME.get(x_dev_role.lower())
    if role is None:
        raise HTTPException(status_code=400, detail=f"未知角色 {x_dev_role!r}")
    return CurrentUser(email=x_dev_user, role=role)


def guard(user: CurrentUser, permission: str) -> None:
    """在 route 內呼叫；權限不足回 403（前端顯示「權限不足」頁）。"""
    if not user.can(permission):
        raise HTTPException(
            status_code=403,
            detail=f"你目前的角色（{user.role_label}）無法使用此功能。"
                   "需要 Security Admin 為你調整權限。")
