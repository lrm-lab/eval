from __future__ import annotations

import hmac
from urllib.parse import urlparse

from fastapi import Request

from app.api.errors import AppError
from app.security.auth import CSRF_COOKIE_NAME

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalize_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def verify_csrf(request: Request, allowed_origins: list[str], *, require_token: bool = True) -> None:
    """Enforce Origin/Referer allowlist + Double-Submit CSRF token on all mutating requests (S-01)."""
    if request.method.upper() in SAFE_METHODS:
        return

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    allowed = {_normalize_origin(o) for o in allowed_origins}
    # Also allow same-host origin derived from Host header (behind TLS reverse proxy)
    host = request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if host:
        allowed.add(f"{proto.lower()}://{host.lower()}")

    if origin:
        if _normalize_origin(origin) not in allowed:
            raise AppError(403, "CSRF_INVALID", "非法请求 Origin，已拒绝写入", {"origin": origin})
    elif referer:
        if _normalize_origin(referer) not in allowed:
            raise AppError(403, "CSRF_INVALID", "非法请求 Referer，已拒绝写入")

    if not require_token:
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get("x-csrf-token")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise AppError(403, "CSRF_INVALID", "缺失或无效的 CSRF Token")
