from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.security.storage_probe import probe_secret_file


class _UnixHTTPConnection(http.client.HTTPConnection):
    """stdlib HTTP/1.1 client speaking directly over a local 0600 Unix Domain Socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sp = Path(self._socket_path)
        if len(self._socket_path.encode("utf-8")) >= 100 and Path("/proc/self/fd").is_dir():
            dfd = os.open(str(sp.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                sock.connect(f"/proc/self/fd/{dfd}/{sp.name}")
            finally:
                os.close(dfd)
        else:
            sock.connect(self._socket_path)
        self.sock = sock


class RunnerIPCClient:
    """Authenticated IPC client talking to the external Isolated Execution Runner Daemon.

    Supports either:
    - A private Unix Domain Socket (`settings.runner_ipc_socket`), OR
    - An authenticated loopback/internal HTTP endpoint (`settings.runner_ipc_url`).
    Every call requires a shared secret loaded from a POSIX `0600`-verified token file.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.runner_ipc_socket or self.settings.runner_ipc_url)

    def _resolve_token_path(self) -> Path:
        if self.settings.runner_ipc_token_file is not None:
            return self.settings.runner_ipc_token_file
        assert self.settings.state_dir is not None
        return self.settings.state_dir / "runner_ipc.token"

    def _load_token(self) -> str:
        tpath = self._resolve_token_path()
        if not tpath.exists() and not tpath.is_symlink():
            raise AppError(
                503,
                "RUNNER_NOT_ATTACHED",
                "隔离 Runner IPC 凭据文件 runner_ipc.token 不存在，拒绝建立执行连接",
                {"token_configured": False},
            )
        probe_secret_file(tpath, create_parent=False)
        token = tpath.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise AppError(
                503,
                "RUNNER_NOT_ATTACHED",
                "隔离 Runner IPC 凭据强度不足（需 >= 16 字符），拒绝建立执行连接",
            )
        return token

    def _open_connection(self, timeout: float) -> tuple[http.client.HTTPConnection, str]:
        if self.settings.runner_ipc_socket is not None:
            sock_p = self.settings.runner_ipc_socket
            if sock_p.is_symlink() or not sock_p.exists():
                raise OSError(f"Runner Unix socket unavailable: {sock_p}")
            return _UnixHTTPConnection(str(sock_p), timeout=timeout), ""
        if self.settings.runner_ipc_url:
            parsed = urllib.parse.urlsplit(self.settings.runner_ipc_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise AppError(503, "RUNNER_NOT_ATTACHED", "非法的 LRM_RUNNER_IPC_URL 配置")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            prefix = parsed.path.rstrip("/")
            if parsed.scheme == "https":
                return http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout), prefix
            return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout), prefix
        raise AppError(503, "RUNNER_NOT_ATTACHED", "尚未配置独立隔离执行服务连接端点")

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, Any]]:
        token = self._load_token()
        conn, prefix = self._open_connection(timeout=timeout)
        full_path = f"{prefix}{path}"
        headers: dict[str, str] = {
            "X-Runner-Token": token,
            "Accept": "application/json",
        }
        body_bytes: bytes | None = None
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body_bytes))
        try:
            conn.request(method, full_path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
                if not isinstance(parsed, dict):
                    parsed = {"raw": parsed}
            except ValueError:
                parsed = {"raw": raw}
            return resp.status, parsed
        finally:
            conn.close()

    def probe_health(self, *, timeout: float = 0.8) -> dict[str, Any]:
        if not self.is_configured:
            return {
                "configured": False,
                "connected": False,
                "ok": False,
                "reason": "未配置 LRM_RUNNER_SOCKET_PATH 或 LRM_RUNNER_IPC_URL（生产只读与 --plan 模式）",
            }
        try:
            status, body = self.request_json("GET", "/health", timeout=timeout)
        except AppError as exc:
            return {
                "configured": True,
                "connected": False,
                "ok": False,
                "code": exc.code,
                "reason": exc.message,
            }
        except OSError as exc:
            return {
                "configured": True,
                "connected": False,
                "ok": False,
                "code": "RUNNER_UNREACHABLE",
                "reason": f"隔离 Runner 服务不可达: {type(exc).__name__}",
            }
        if status != 200:
            return {
                "configured": True,
                "connected": False,
                "ok": False,
                "http_status": status,
                "reason": str(body.get("message") or f"Runner /health returned HTTP {status}"),
            }
        return {
            "configured": True,
            "connected": True,
            **body,
        }

    def submit_job(self, job_payload: dict[str, Any], *, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
        return self.request_json("POST", "/v1/jobs", job_payload, timeout=timeout)

    def get_events(self, job_id: str, *, after_seq: int = 0, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
        return self.request_json("GET", f"/v1/jobs/{job_id}/events?after_seq={int(after_seq)}", timeout=timeout)

    def recover_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        return self.request_json("POST", f"/v1/jobs/{job_id}/recover", {}, timeout=2.0)

    def cancel_job(self, job_id: str, *, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
        return self.request_json("POST", f"/v1/jobs/{job_id}/cancel", {}, timeout=timeout)
