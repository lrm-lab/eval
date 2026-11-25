from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, Request

from app.api.errors import AppError
from app.security.auth import SESSION_COOKIE_NAME, AuthUser
from app.security.authz import require_role
from app.security.csrf import verify_csrf


def get_app_state(request: Request) -> Any:
    return request.app.state.ctx


def get_current_user(request: Request) -> AuthUser:
    ctx = request.app.state.ctx
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise AppError(401, "UNAUTHORIZED", "请先登录")
    payload = ctx.sessions.verify_token(token)
    uid = payload.get("uid")
    row = ctx.db.fetchone(
        "SELECT user_id, username, display_name, role, scopes_json, projects_json, is_active FROM users WHERE user_id = ?",
        (uid,),
    )
    if row is None or not row["is_active"]:
        raise AppError(401, "UNAUTHORIZED", "账号不存在或已停用")
    return AuthUser(
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        scopes=frozenset(json.loads(row["scopes_json"] or "[]")),
        projects=frozenset(json.loads(row["projects_json"] or "[]")),
        is_active=bool(row["is_active"]),
    )


def enforce_csrf(request: Request) -> None:
    ctx = request.app.state.ctx
    verify_csrf(request, ctx.settings.allowed_origins, require_token=True)


def require_researcher(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    require_role(user, "researcher")
    return user


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    require_role(user, "admin")
    return user
