from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from app.api.errors import AppError
from app.config import Settings
from app.security.storage_probe import probe_secret_file


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


class GatewayProfileRegistry:
    """Server-side Gateway / Teacher / Judge profile registry (v2.2.1 · 02-6, 02-7, 06-1 §3).
    Secrets NEVER enter YAML, SQLite, logs, or API DTOs; they are loaded exclusively from
    process env or a Fail-Closed 0600 local ext4 `gateways.env` file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profiles: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        p = self.settings.gateway_profiles_file
        if p.is_file():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw = cfg.get("profiles") or []
            self.profiles = [dict(x) for x in raw if isinstance(x, dict) and x.get("profile_id")]
        else:
            self.profiles = []

    def _load_gateways_env(self, *, fail_on_unsafe: bool = True) -> dict[str, str]:
        gpath = self.settings.gateways_env_file
        if gpath is None or not (gpath.exists() or gpath.is_symlink()):
            return {}
        try:
            probe_secret_file(gpath, create_parent=False)
        except AppError:
            if fail_on_unsafe:
                raise
            return {}
        return _parse_env_file(gpath)

    def _resolve_secret(self, env_name: str, *, fail_on_unsafe: bool = True) -> str | None:
        if not env_name:
            return None
        file_env = self._load_gateways_env(fail_on_unsafe=fail_on_unsafe)
        val = file_env.get(env_name) or os.environ.get(env_name)
        return val.strip() if isinstance(val, str) and val.strip() else None

    def list_public_profiles(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        """Return safe public metadata ONLY — NEVER exposes secret values."""
        out: list[dict[str, Any]] = []
        for prof in self.profiles:
            if kind and prof.get("kind") != kind:
                continue
            env_name = str(prof.get("api_key_env") or "")
            key_ok = bool(self._resolve_secret(env_name, fail_on_unsafe=False))
            out.append(
                {
                    "profile_id": str(prof["profile_id"]),
                    "display_name": str(prof.get("display_name") or prof["profile_id"]),
                    "kind": str(prof.get("kind") or "model_gateway"),
                    "protocol": str(prof.get("protocol") or "openai_chat_completions"),
                    "base_url": str(prof.get("base_url") or ""),
                    "api_key_env": env_name,
                    "default_model": str(prof.get("default_model") or ""),
                    "notes": str(prof.get("notes") or ""),
                    "key_configured": key_ok,
                }
            )
        return out

    def get_public_profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.list_public_profiles():
            if p["profile_id"] == profile_id:
                return p
        raise AppError(404, "RESOURCE_NOT_FOUND", f"未登记的网关 Profile: {profile_id}")

    def resolve_runtime_credentials(self, profile_id: str) -> tuple[dict[str, Any], str]:
        """Server-side only: resolve profile metadata + secret key after Fail-Closed 0600 check."""
        raw = next((p for p in self.profiles if p["profile_id"] == profile_id), None)
        if raw is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"未登记的网关 Profile: {profile_id}")
        env_name = str(raw.get("api_key_env") or "")
        secret = self._resolve_secret(env_name, fail_on_unsafe=True)
        if not secret:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"服务端尚未在 0600 gateways.env 或环境变量中配置凭据 {env_name}（严禁通过浏览器明文提交密钥）",
                {"profile_id": profile_id, "api_key_env": env_name},
            )
        return dict(raw), secret
