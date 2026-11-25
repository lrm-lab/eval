from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.api.errors import AppError

SESSION_COOKIE_NAME = "lrm_eval_session"
CSRF_COOKIE_NAME = "lrm_eval_csrf"

_ph = PasswordHasher()


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    username: str
    display_name: str
    role: str
    scopes: frozenset[str]
    projects: frozenset[str]
    is_active: bool


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def _atomic_write_secret_file(secrets_file: Path, key: str) -> None:
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = secrets_file.parent / f".{secrets_file.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, f"LRM_EVAL_SECRET_KEY={key}\n".encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, secrets_file)


def load_or_create_secret_key(
    secrets_file: Path,
    env_secret: str | None = None,
    *,
    write_guard: Callable[[], None] | None = None,
    rotate: bool = False,
) -> str:
    if not rotate and env_secret and len(env_secret.strip()) >= 32:
        return env_secret.strip()
    if not rotate and secrets_file.is_file():
        for line in secrets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("LRM_EVAL_SECRET_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(val) >= 32:
                    return val
    # Any creation, incomplete-file rewrite, or key rotation MUST pass write_guard on secrets_file's mount first!
    if write_guard is not None:
        write_guard()
    key = secrets.token_urlsafe(48)
    _atomic_write_secret_file(secrets_file, key)
    return key


def rotate_secret_key(
    secrets_file: Path,
    *,
    write_guard: Callable[[], None] | None = None,
) -> str:
    """Atomically rotate signing secret after passing low-disk write_guard on secrets_file's mount."""
    return load_or_create_secret_key(secrets_file, env_secret=None, write_guard=write_guard, rotate=True)


class SessionManager:
    def __init__(self, secret_key: str, ttl_seconds: int = 86400) -> None:
        self._secret = secret_key.encode("utf-8")
        self._ttl = ttl_seconds
        self._revoked_jtis: dict[str, float] = {}

    def _sign(self, payload_b64: str) -> str:
        sig = hmac.new(self._secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")

    def create_token(self, user_id: str) -> tuple[str, str]:
        now = int(time.time())
        jti = secrets.token_hex(12)
        payload = {"uid": user_id, "iat": now, "exp": now + self._ttl, "jti": jti}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        token = f"{b64}.{self._sign(b64)}"
        csrf_token = secrets.token_urlsafe(32)
        return token, csrf_token

    def verify_token(self, token: str) -> dict[str, Any]:
        if not token or "." not in token:
            raise AppError(401, "UNAUTHORIZED", "未登录或会话无效")
        b64, sig = token.rsplit(".", 1)
        expected = self._sign(b64)
        if not hmac.compare_digest(sig, expected):
            raise AppError(401, "UNAUTHORIZED", "会话签名无效")
        pad = "=" * ((4 - len(b64) % 4) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8"))
        except Exception as exc:
            raise AppError(401, "UNAUTHORIZED", "会话载荷损坏") from exc
        now = int(time.time())
        if not isinstance(payload.get("exp"), int) or payload["exp"] < now:
            raise AppError(401, "UNAUTHORIZED", "会话已过期，请重新登录")
        jti = payload.get("jti", "")
        if jti in self._revoked_jtis:
            raise AppError(401, "UNAUTHORIZED", "会话已注销")
        return payload

    def revoke_token(self, token: str) -> None:
        try:
            payload = self.verify_token(token)
        except AppError:
            return
        jti = payload.get("jti")
        exp = payload.get("exp", time.time() + 3600)
        if jti:
            self._revoked_jtis[jti] = float(exp)
        # Opportunistic prune
        now = time.time()
        expired = [k for k, v in self._revoked_jtis.items() if v < now]
        for k in expired:
            self._revoked_jtis.pop(k, None)
