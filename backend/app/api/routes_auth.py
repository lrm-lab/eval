from __future__ import annotations

import json
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError

from app.api.deps import enforce_csrf, get_current_user
from app.api.errors import AppError
from app.db.migrations import utcnow_iso
from app.security.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, AuthUser, hash_password, verify_password
from app.security.csrf import verify_csrf

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_REGISTERED_VIEWER_SCOPES = ("trace:read", "gateway:read", "verifier_log:read", "direct_vlm_qa:read")


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    password: Annotated[SecretStr, Field(min_length=8, max_length=128)]


def _create_session(request: Request, response: Response, row: dict[str, Any]) -> dict[str, Any]:
    ctx = request.app.state.ctx
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
    return _create_session(request, response, row)


@router.post(
    "/register",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": RegisterRequest.model_json_schema()}},
        },
    },
)
def register(request: Request, response: Response, body: Annotated[Any, Body()] = None) -> dict[str, Any]:
    ctx = request.app.state.ctx
    verify_csrf(request, ctx.settings.allowed_origins, require_token=False)
    try:
        credentials = RegisterRequest.model_validate(body)
    except ValidationError:
        # Validation errors must never include submitted credentials.
        raise AppError(422, "INVALID_REGISTRATION", "仅可提交用户名和密码，用户名为 1–64 字符，密码为 8–128 字符") from None

    account = {
        "user_id": f"u_{secrets.token_hex(16)}",
        "username": credentials.username,
        "display_name": credentials.username,
        "password_hash": hash_password(credentials.password.get_secret_value()),
        "role": "viewer",
        "scopes_json": json.dumps(_REGISTERED_VIEWER_SCOPES),
        "projects_json": json.dumps(["default"]),
        "now": utcnow_iso(),
    }
    with ctx.db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (
                user_id, username, display_name, password_hash, role,
                scopes_json, projects_json, created_at, updated_at
            ) VALUES (
                :user_id, :username, :display_name, :password_hash, :role,
                :scopes_json, :projects_json, :now, :now
            ) ON CONFLICT(username) DO NOTHING
            """,
            account,
        )
        if cursor.rowcount == 0:
            raise AppError(409, "USERNAME_TAKEN", "用户名已被使用")
    response.headers["Cache-Control"] = "no-store"
    return _create_session(request, response, account)


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
