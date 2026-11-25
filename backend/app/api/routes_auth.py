from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.api.deps import enforce_csrf, get_current_user
from app.api.errors import AppError
from app.security.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, AuthUser, verify_password
from app.security.csrf import verify_csrf

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ctx = request.app.state.ctx
    verify_csrf(request, ctx.settings.allowed_origins, require_token=False)

    row = ctx.db.fetchone(
        "SELECT user_id, username, display_name, password_hash, role, scopes_json, projects_json, is_active FROM users WHERE username = ?",
        (body.username.strip(),),
    )
    if row is None or not row["is_active"] or not verify_password(row["password_hash"], body.password):
        raise AppError(401, "UNAUTHORIZED", "用户名或密码错误")

    token, csrf_token = ctx.sessions.create_token(row["user_id"])
    secure = ctx.settings.cookie_secure
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=ctx.settings.session_ttl_seconds,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=ctx.settings.session_ttl_seconds,
        path="/",
    )
    return {
        "user": {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "scopes": json.loads(row["scopes_json"] or "[]"),
            "projects": json.loads(row["projects_json"] or "[]"),
        },
        "csrf_token": csrf_token,
    }


@router.post("/logout", dependencies=[Depends(enforce_csrf)])
def logout(request: Request, response: Response, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        ctx.sessions.revoke_token(token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True, "username": user.username}


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "scopes": sorted(user.scopes),
        "projects": sorted(user.projects),
    }
