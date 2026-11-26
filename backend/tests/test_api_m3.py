from __future__ import annotations

import hashlib
import http.server
import json
import math
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import certifi
import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.migrations import utcnow_iso
from app.indexing.identity import make_batch_id
from app.logging_setup import (
    BoundedJobStdioWriter,
    MAX_JOB_DETAIL_STDIO_READ_BYTES,
    build_uvicorn_log_config,
    configure_component_rotating_logger,
    enforce_job_stdio_retention_cap,
)
from app.main import create_app
from app.runner.client import RunnerIPCClient
from app.runner.cli_contract import build_managed_run_py_command, materialize_managed_run_config
from app.security.auth import SESSION_COOKIE_NAME
from app.security.authz import ALL_SCOPES, personal_project_id
from app.security.managed_exec import resolve_verified_managed_script
from scripts.backup_db import create_single_compressed_backup
from tests.conftest import create_user, login_client

OPD_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "opd_sample"
TERMINAL_JOB_STATUSES = frozenset({"finished", "failed", "interrupted", "paused_by_guard"})


class _MockChatGatewayServer:
    """Real isolated loopback OpenAI Chat Completions HTTP server for testing real direct.py execution."""

    def __init__(self, handler_fn: Any) -> None:
        self._handler_fn = handler_fn
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                req_json = json.loads(raw.decode("utf-8"))
                code, resp_obj = outer._handler_fn(self.path, self.headers, req_json)
                out = json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._srv.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> "_MockChatGatewayServer":
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()


def _write_test_config(
    cfg_dir: Path,
    *,
    approval_entries: list[dict[str, Any]],
    gateway_base_url: str,
) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    src_cfg = Path(__file__).resolve().parents[2] / "config"
    for fname in (
        "taxonomy.yaml",
        "media_matching_rules.yaml",
        "token_semantics_registry.yaml",
        "direct_scorers.yaml",
        "direct_suites.yaml",
    ):
        shutil.copyfile(src_cfg / fname, cfg_dir / fname)
    shutil.copytree(src_cfg / "direct_suites", cfg_dir / "direct_suites", dirs_exist_ok=True)

    (cfg_dir / "gateway_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "v1",
                "profiles": [
                    {
                        "profile_id": "qwen197-vllm",
                        "display_name": "Isolated Test Loopback Gateway",
                        "kind": "model_gateway",
                        "protocol": "openai_chat_completions",
                        "base_url": gateway_base_url,
                        "api_key_env": "OPENAI_API_KEY",
                        "default_model": "Qwen3_8-27B",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (cfg_dir / "source_approval_registry.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "v1",
                "default_project_id": "default",
                "default_owner_id": "system",
                "sources": approval_entries,
            }
        ),
        encoding="utf-8",
    )


def _copy_harbor_toml(tianxi_root: Path) -> None:
    fw_dir = tianxi_root / "framework"
    fw_dir.mkdir(parents=True, exist_ok=True)
    src = Path("/home/share-data/tianxi_benches/framework/harbor.toml")
    (fw_dir / "harbor.toml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    # Also copy multimodal/harbor.toml so VLMEvalKit suites are registered
    mm_src = Path("/home/share-data/tianxi_benches/datasets/multimodal/harbor.toml")
    if mm_src.is_file():
        mm_dst = tianxi_root / "datasets" / "multimodal"
        mm_dst.mkdir(parents=True, exist_ok=True)
        (mm_dst / "harbor.toml").write_text(mm_src.read_text(encoding="utf-8"), encoding="utf-8")


@contextmanager
def _start_external_runner_daemon(
    settings: Settings,
    *,
    extra_env: dict[str, str] | None = None,
) -> Iterator[subprocess.Popen[str]]:
    """Launch a REAL external `app.runner.daemon` OS process over a 0600 Unix Domain Socket."""
    assert settings.state_dir is not None and settings.runner_work_dir is not None
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.state_dir, 0o700)
    settings.runner_work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.runner_work_dir, 0o700)

    token_file = settings.state_dir / "runner_ipc.token"
    token_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

    sock_path = settings.runner_work_dir / "runner.sock"
    settings.runner_ipc_socket = sock_path
    settings.runner_ipc_token_file = token_file

    # Write 0600 gateways.env with isolated test key
    assert settings.gateways_env_file is not None
    settings.gateways_env_file.write_text("OPENAI_API_KEY=sk-test-isolated-gateway-key-0001\n", encoding="utf-8")
    os.chmod(settings.gateways_env_file, 0o600)

    backend_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(backend_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LRM_PRODUCTION_MODE": "false",
        "LRM_RUNTIME_DIR": str(settings.runtime_dir),
        "LRM_STATE_DIR": str(settings.state_dir),
        "LRM_RUNNER_WORK_DIR": str(settings.runner_work_dir),
        "LRM_EVIDENCE_STORE_DIR": str(settings.evidence_store_dir),
        "LRM_CONFIG_DIR": str(settings.config_dir),
        "LRM_TIANXI_ROOT": str(settings.tianxi_root),
        "LRM_GATEWAYS_ENV_FILE": str(settings.gateways_env_file),
        "LRM_MANAGED_FRAMEWORK_DIR": str(settings.managed_framework_dir),
    }
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.runner.daemon",
            "--socket",
            str(sock_path),
            "--token-file",
            str(token_file),
        ],
        cwd=str(backend_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(f"External runner daemon exited early ({proc.returncode}): {out} {err}")
        if sock_path.exists():
            break
        time.sleep(0.05)
    assert sock_path.exists(), "Runner Unix socket did not appear in time"
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _wait_job_terminal(
    client: TestClient,
    job_id: str,
    *,
    require_notification: bool = False,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/execution/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last_body = resp.json()
        if last_body.get("status") in TERMINAL_JOB_STATUSES:
            if not require_notification:
                return last_body
            if any(ev.get("event_type") == "notification" for ev in last_body.get("events", [])):
                return last_body
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach expected terminal state within {timeout_s}s; last={last_body}")


def _parse_sse_stream(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_block in raw_text.strip().split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        ev_id: int | None = None
        ev_type = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("id:"):
                ev_id = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                ev_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if ev_id is not None:
            data_str = "\n".join(data_lines)
            events.append({"id": ev_id, "event": ev_type, "data": json.loads(data_str) if data_str else {}})
    return events


def _p95_ms(samples_ms: list[float]) -> float:
    assert samples_ms, "samples_ms must be non-empty"
    ordered = sorted(samples_ms)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


def test_viewer_can_run_and_manage_owned_evaluations(tmp_path: Path, isolated_settings: Settings) -> None:
    gateway_key = "sk-test-isolated-gateway-key-0001"
    gateway_waiting = threading.Event()
    release_gateway = threading.Event()

    def answer(_path: str, headers: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert headers.get("Authorization") == f"Bearer {gateway_key}"
        arithmetic = "2 + 2" in str(body["messages"][0]["content"])
        if not arithmetic and not gateway_waiting.is_set():
            gateway_waiting.set()
            release_gateway.wait(timeout=15)
        return 200, {
            "choices": [{"message": {"content": "4" if arithmetic else "READY"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }

    with _MockChatGatewayServer(answer) as gateway:
        isolated_settings.tianxi_root = tmp_path / "tianxi"
        isolated_settings.config_dir = tmp_path / "config"
        _copy_harbor_toml(isolated_settings.tianxi_root)
        _write_test_config(isolated_settings.config_dir, approval_entries=[], gateway_base_url=gateway.base_url)

        with _start_external_runner_daemon(isolated_settings) as daemon:
            app = create_app(isolated_settings, run_startup_scan=False)
            ctx = app.state.ctx
            owner = create_user(
                ctx.db, username="viewer_owner", password="ViewerOwner!2026", role="viewer",
                projects=["default", "team_eval"],
            )
            private_project = personal_project_id(owner)
            create_user(
                ctx.db, username="viewer_peer", password="ViewerPeer!2026", role="viewer",
                projects=["default", "team_eval", private_project],
            )
            client = TestClient(app)
            peer = TestClient(app)
            headers = {"X-CSRF-Token": login_client(client, "viewer_owner", "ViewerOwner!2026")}
            peer_headers = {"X-CSRF-Token": login_client(peer, "viewer_peer", "ViewerPeer!2026")}
            prefix = "/api/v1/execution"
            spec = {
                "kind": "direct", "suite_id": "direct-smoke-v1", "selection_mode": "tasks",
                "tasks": ["smoke-arith-001"], "batch_name": "member-personal-evaluation",
            }
            caps = client.get(f"{prefix}/capabilities")
            assert caps.status_code == 200
            assert caps.json()["runner_environment"]["runner_pid"] == daemon.pid
            assert daemon.pid != os.getpid()
            assert gateway_key not in caps.text and gateway.base_url not in caps.text

            for endpoint in ("plan", "jobs"):
                url = f"{prefix}/{endpoint}"
                denied = client.post(url, json=spec)
                assert denied.status_code == 403
                assert denied.json()["error"]["code"] == "CSRF_INVALID"
                forged_project = peer.post(url, headers=peer_headers, json={**spec, "project_id": private_project})
                assert forged_project.status_code == 403
                assert forged_project.json()["error"]["code"] == "PROJECT_FORBIDDEN"
                holdout = client.post(
                    url, headers=headers, json={**spec, "kind": "harbor", "suite_id": "tianxi-synth-v1-holdout"},
                )
                assert holdout.status_code == 403
                assert holdout.json()["error"]["detail"]["missing_scope"] == "dataset:holdout_read"
                unregistered = client.post(
                    url, headers=headers, json={**spec, "kind": "harbor", "suite_id": "mmlu-pro-raw"},
                )
                assert unregistered.status_code == 422
                inline_secret = client.post(url, headers=headers, json={**spec, "api_key": gateway_key})
                assert inline_secret.status_code == 422
                assert gateway_key not in inline_secret.text
                shared_publish = client.post(url, headers=headers, json={**spec, "publish_to_harbor_runs": True})
                assert shared_publish.status_code == 403
                webhook = client.post(url, headers=headers, json={**spec, "notify": {"webhook_profile_id": "ops-alert"}})
                assert webhook.status_code == 403

            anonymous = TestClient(app, cookies=dict(client.cookies))
            anonymous.cookies.delete(SESSION_COOKIE_NAME)
            assert anonymous.post(f"{prefix}/plan", headers=headers, json=spec).status_code == 401

            plan = client.post(f"{prefix}/plan", headers=headers, json=spec)
            assert plan.status_code == 200, plan.text
            assert plan.json()["plan"]["project_id"] == private_project
            assert gateway_key not in plan.text and gateway.base_url not in plan.text
            submitted = client.post(f"{prefix}/jobs", headers=headers, json=spec)
            assert submitted.status_code == 202, submitted.text
            assert gateway_key not in submitted.text and gateway.base_url not in submitted.text
            job_id = submitted.json()["job_id"]
            detail = _wait_job_terminal(client, job_id)
            assert detail["status"] == "finished"
            assert (detail["project_id"], detail["owner_id"]) == (private_project, owner)
            assert detail["shared_snapshot"]["reconciled"]
            assert detail["live_channel"]["trials"][0]["reward"] == 1.0
            assert gateway_key not in json.dumps(detail)
            assert (isolated_settings.runner_work_dir / job_id).stat().st_mode & 0o777 == 0o700
            assert (isolated_settings.evidence_store_dir / job_id).stat().st_mode & 0o777 == 0o700
            stream = client.get(f"{prefix}/jobs/{job_id}/stream?once=true", headers={"Last-Event-ID": "1"})
            assert stream.status_code == 200 and _parse_sse_stream(stream.text)
            assert gateway_key not in stream.text
            assert peer.get(f"{prefix}/jobs/{job_id}").status_code == 403
            assert peer.get(f"{prefix}/jobs/{job_id}/stream?once=true").status_code == 403
            assert not peer.get(f"{prefix}/jobs").json()["items"]
            trial_id = detail["live_channel"]["trials"][0]["trial_id"]
            assert client.get(f"{prefix}/jobs/{job_id}/evidence/{trial_id}").status_code == 403

            team_job = client.post(f"{prefix}/jobs", headers=headers, json={
                **spec, "project_id": "team_eval", "batch_name": "member-team-evaluation",
                "tasks": ["smoke-echo-002"],
            })
            assert team_job.status_code == 202, team_job.text
            team_job_id = team_job.json()["job_id"]
            team_url = f"{prefix}/jobs/{team_job_id}"
            resume = {"resume_mode": "derived_task_subset"}
            try:
                assert gateway_waiting.wait(timeout=10)
                assert peer.get(team_url).status_code == 200
                assert peer.post(f"{team_url}/cancel", headers=peer_headers).status_code == 403
                assert peer.post(f"{team_url}/resume", headers=peer_headers, json=resume).status_code == 403
                with ctx.db.transaction() as conn:
                    conn.execute("UPDATE users SET projects_json = '[]' WHERE user_id = ?", (owner,))
                assert client.get(team_url).status_code == 403
                assert client.post(f"{team_url}/cancel", headers=headers).status_code == 403
                assert client.post(f"{team_url}/resume", headers=headers, json=resume).status_code == 403
                with ctx.db.transaction() as conn:
                    conn.execute("UPDATE users SET projects_json = ? WHERE user_id = ?", ('["team_eval"]', owner))
                assert client.post(f"{team_url}/cancel").json()["error"]["code"] == "CSRF_INVALID"
                cancel = client.post(f"{team_url}/cancel", headers=headers)
                assert cancel.status_code == 200, cancel.text
                assert _wait_job_terminal(client, team_job_id)["status"] == "interrupted"
            finally:
                release_gateway.set()

            assert client.post(f"{team_url}/resume", json=resume).json()["error"]["code"] == "CSRF_INVALID"
            resumed = client.post(f"{team_url}/resume", headers=headers, json=resume)
            assert resumed.status_code == 202, resumed.text
            resumed_detail = _wait_job_terminal(client, resumed.json()["job_id"])
            assert resumed_detail["status"] == "finished"
            assert (resumed_detail["project_id"], resumed_detail["owner_id"]) == ("team_eval", owner)
            assert resumed_detail["parent_job_id"] == team_job_id


def test_m3_gateway_capability_requires_openenv_runtime(tmp_path: Path, isolated_settings: Settings) -> None:
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    docker = mock_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o700)
    empty_env = tmp_path / "empty-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(empty_env)], check=True)
    isolated_settings.config_dir = Path(__file__).resolve().parents[2] / "config"

    with _start_external_runner_daemon(
        isolated_settings,
        extra_env={
            "PATH": f"{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "LRM_RUNNER_OPENENV_PYTHON": str(empty_env / "bin/python"),
            "LRM_RUNNER_OPENENV_PYTHONPATH": "",
        },
    ):
        ipc = RunnerIPCClient(isolated_settings)
        health = ipc.probe_health()
        assert health["ok"] and health["docker_available"]
        assert health["harbor_gateway_runtime_ready"] is False
        assert "harbor" in health["supported_runners"]
        assert "harbor_gateway" not in health["supported_runners"]
        job_id = "job_unready_gateway"
        status, reply = ipc.submit_job({
            "job_id": job_id,
            "runner_instance_id": health["runner_instance_id"],
            "channel_secret": secrets.token_hex(32),
            "spec": {"kind": "harbor_gateway"},
        })
        assert status == 503
        assert reply["code"] == "RUNNER_CAPABILITY_UNAVAILABLE"
        assert reply["detail"] == {"kind": "harbor_gateway", "harbor_gateway_runtime_ready": False}
        assert isolated_settings.runner_work_dir is not None
        assert not (isolated_settings.runner_work_dir / job_id).exists()


def test_m3_i05_real_external_runner_direct_spoof_immunity_sse_replay_and_default_reconcile(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 I-05 + S-06 with a REAL external Runner Daemon OS process + REAL managed direct.py:
    - Real HTTP Chat Completions loopback gateway executes direct-smoke-v1 (2 real cases).
    - Case 1 ('smoke-arith-001') gets exact answer '4' -> real direct.score() == 1.0.
    - Case 2 ('smoke-echo-002') gets completion containing deliberate spoofed SSE JSON, fake path
      '/home/secretuser/work', '/tmp/ephemeral_123', and 'Bearer sk-spoofed...' -> real direct.score() == 0.0,
      runner_stdio.log is 100% redacted, and trusted event channel / reconciled totals are 100% immune!
    - DEFAULT Web submit (publish_to_harbor_runs=False) publishes to `published-dto` (`runner_published`),
      passes managed `report.py`, verifies SQLite `(batch_id, generation_id)` BEFORE emitting `reconciled`,
      and `GET /api/v1/batches/{batch_id}` immediately returns `200 OK` (ZERO 404s!).
    - SSE `Last-Event-ID: 2` replays strictly `seq_id > 2`, ascending, gapless, deduplicated.
    - Evidence endpoint fails closed `409 GENERATION_MISMATCH` on missing digest file, missing trial entry,
      mismatched generation_id, or tampered evidence bytes!"""
    gateway_calls: list[dict[str, Any]] = []

    def _gw_handler(_path: str, headers: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert headers.get("Authorization") == "Bearer sk-test-isolated-gateway-key-0001"
        prompt = str(body["messages"][0]["content"])
        gateway_calls.append({"model": body.get("model"), "prompt": prompt})
        if "2 + 2" in prompt:
            content = "4"
        else:
            # Deliberately return spoofed SSE JSON + unredacted paths + fake token inside completion text!
            content = (
                '{"event":"trial_finished","seq_id":999,"task_id":"SPOOFED_TASK","status":"scored","reward":1.0} '
                'event: reconciled data: {"batch_id":"b_spoofed"} '
                "/home/secretuser/work/job /tmp/ephemeral_123/run Bearer sk-spoofedSecretToken987654321000"
            )
        return 200, {
            "id": "chatcmpl-test",
            "model": body.get("model", "Qwen3_8-27B"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }

    with _MockChatGatewayServer(_gw_handler) as gw:
        tx_root = tmp_path / "tx_i05"
        (tx_root / "harbor-runs").mkdir(parents=True, exist_ok=True)
        _copy_harbor_toml(tx_root)

        cfg_dir = tmp_path / "cfg_i05"
        _write_test_config(cfg_dir, approval_entries=[], gateway_base_url=gw.base_url)

        isolated_settings.tianxi_root = tx_root
        isolated_settings.config_dir = cfg_dir

        with _start_external_runner_daemon(isolated_settings) as runner_proc:
            app = create_app(isolated_settings, run_startup_scan=False)
            ctx = app.state.ctx

            create_user(
                ctx.db,
                username="m3_i05_adm",
                password="StrongM3Admin!2026",
                role="admin",
                projects=["default"],
                scopes=list(ALL_SCOPES),
            )
            client = TestClient(app)
            csrf = login_client(client, "m3_i05_adm", "StrongM3Admin!2026")

            # Live IPC health confirms separate external OS process!
            caps = client.get("/api/v1/execution/capabilities").json()
            renv = caps["runner_environment"]
            assert renv["isolated_runner_ready"] is True
            assert renv["runner_connected"] is True
            assert renv["runner_pid"] == runner_proc.pid
            assert renv["runner_pid"] != os.getpid()
            assert "direct" in renv["supported_runners"]

            # DEFAULT Web Start (NO publish_to_harbor_runs flag!) on real harmless direct-smoke-v1
            batch_name = "m3-i05-real-direct-default"
            sub = client.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf},
                json={
                    "kind": "direct",
                    "suite_id": "direct-smoke-v1",
                    "selection_mode": "all",
                    "model_route": "Qwen3_8-27B",
                    "gateway_profile_id": "qwen197-vllm",
                    "batch_name": batch_name,
                    "project_id": "default",
                },
            )
            assert sub.status_code == 202, sub.text
            job_id = sub.json()["job_id"]

            detail = _wait_job_terminal(client, job_id, require_notification=True)
            assert detail["status"] == "finished"
            assert detail["exit_code"] == 0
            assert len(gateway_calls) == 2

            # 1. Raw stdio log contains the spoofed completion snippet (redacted!) & ZERO raw paths/tokens!
            stdio_text = "\n".join(detail["redacted_stdio_logs"])
            assert "[direct-worker]" in stdio_text
            assert "SPOOFED_TASK" in stdio_text
            assert "<redacted>" in stdio_text
            assert "/home/secretuser" not in stdio_text
            assert "/tmp/ephemeral_123" not in stdio_text
            assert "sk-spoofedSecretToken" not in stdio_text

            # 2. Live channel has ONLY the 2 real direct.py results (1.0 & 0.0), ZERO SPOOFED_TASK!
            live_trials = detail["live_channel"]["trials"]
            assert len(live_trials) == 2
            by_task = {t["task_id"]: t for t in live_trials}
            assert set(by_task.keys()) == {"smoke-arith-001", "smoke-echo-002"}
            assert by_task["smoke-arith-001"]["reward"] == 1.0
            assert by_task["smoke-echo-002"]["reward"] == 0.0
            assert "SPOOFED_TASK" not in json.dumps(detail["live_channel"], ensure_ascii=False)

            # Verify HMAC-signed 0600 trusted event channel exists
            assert ctx.settings.runner_work_dir is not None
            channel_file = ctx.settings.runner_work_dir / job_id / ".trusted-events.ndjson"
            assert channel_file.is_file()
            assert (channel_file.stat().st_mode & 0o777) == 0o600
            channel_records = [json.loads(line) for line in channel_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert len(channel_records) >= 4
            assert all(isinstance(rec.get("hmac_sha256"), str) and len(rec["hmac_sha256"]) == 64 for rec in channel_records)

            # 3. SSE replay with Last-Event-ID: 2 -> strictly seq_id > 2, ascending, gapless, deduplicated
            sse_resp = client.get(
                f"/api/v1/execution/jobs/{job_id}/stream?once=true",
                headers={"Last-Event-ID": "2"},
            )
            assert sse_resp.status_code == 200, sse_resp.text
            sse_events = _parse_sse_stream(sse_resp.text)
            assert len(sse_events) >= 3
            seq_ids = [ev["id"] for ev in sse_events]
            assert all(sid > 2 for sid in seq_ids)
            assert len(seq_ids) == len(set(seq_ids))
            assert seq_ids == list(range(3, seq_ids[-1] + 1))

            lifecycle_events = [ev for ev in sse_events if ev["event"] != "notification"]
            assert lifecycle_events[-1]["event"] == "reconciled"
            rec_payload = lifecycle_events[-1]["data"]
            batch_id = rec_payload["batch_id"]
            generation_id = rec_payload["generation_id"]
            assert rec_payload["source_namespace"] == "runner_published"
            assert batch_id == make_batch_id(f"runner_published:{batch_name}")
            assert isinstance(generation_id, str) and generation_id.startswith("gen_")

            # 4. DEFAULT path reconciled batch is IMMEDIATELY readable at GET /api/v1/batches/{batch_id} (200 OK, NOT 404!)
            b_resp = client.get(f"/api/v1/batches/{batch_id}")
            assert b_resp.status_code == 200, b_resp.text
            b_body = b_resp.json()
            assert b_body["batch_id"] == batch_id
            assert b_body["generation_id"] == generation_id
            assert b_body["totals"]["recorded"] == 2
            assert b_body["totals"]["scored"] == 2
            assert abs(float(b_body["totals"]["valid_mean"]) - 0.5) < 1e-6

            # Toolkit report.py verification ALSO succeeds on the runner_published batch!
            tk_resp = client.post(
                f"/api/v1/toolkit/verify/{batch_id}",
                headers={"X-CSRF-Token": csrf},
                json={},
            )
            assert tk_resp.status_code == 200, tk_resp.text
            assert tk_resp.json()["report"]["totals"]["scored"] == 2

            # 5. Strict Fail-Closed 409 on evidence read when digest manifest / trial entry / generation_id missing or tampered!
            tid1 = by_task["smoke-arith-001"]["trial_id"]
            ev_ok = client.get(f"/api/v1/execution/jobs/{job_id}/evidence/{tid1}")
            assert ev_ok.status_code == 200, ev_ok.text
            assert ev_ok.json()["generation_id"] == generation_id
            assert ev_ok.json()["evidence"]["response"] == "4"

            runner_evidence = f"/api/v1/batches/{batch_id}/trials/{tid1}/runner-evidence"
            assert client.get(runner_evidence).json() == ev_ok.json()
            for name, projects, scopes, code in (
                ("evidence_viewer", ["default"], ["direct_vlm_qa:read"], None),
                ("plain_viewer", ["default"], [], "SCOPE_FORBIDDEN"),
                ("outside_viewer", ["other"], ["direct_vlm_qa:read"], "PROJECT_FORBIDDEN"),
            ):
                password = "RunnerViewerForEvidence!1"
                create_user(ctx.db, username=name, password=password, projects=projects, scopes=scopes)
                viewer = TestClient(app)
                login_client(viewer, name, password)
                evidence_response = viewer.get(runner_evidence)
                if code:
                    assert evidence_response.status_code == 403
                    assert evidence_response.json()["error"]["code"] == code
                else:
                    assert evidence_response.status_code == 200
                    evidence = evidence_response.json()["evidence"]
                    assert evidence["response"] == "4"
                    assert "answer" not in evidence
                    assert evidence["verifier_log_redacted"] is True

            digest_file = ctx.settings.runner_work_dir / job_id / ".evidence-digests.json"
            orig_digest_text = digest_file.read_text(encoding="utf-8")
            orig_digest_obj = json.loads(orig_digest_text)

            # 5a) Missing trial entry in .evidence-digests.json -> 409 GENERATION_MISMATCH
            missing_trial_doc = {**orig_digest_obj, "trials": {k: v for k, v in orig_digest_obj["trials"].items() if k != tid1}}
            digest_file.write_text(json.dumps(missing_trial_doc), encoding="utf-8")
            r_miss_entry = client.get(f"/api/v1/execution/jobs/{job_id}/evidence/{tid1}")
            assert r_miss_entry.status_code == 409
            assert r_miss_entry.json()["error"]["code"] == "GENERATION_MISMATCH"

            # 5b) Mismatched generation_id in .evidence-digests.json -> 409 GENERATION_MISMATCH
            wrong_gen_doc = {**orig_digest_obj, "generation_id": "gen_wrong_999"}
            digest_file.write_text(json.dumps(wrong_gen_doc), encoding="utf-8")
            r_wrong_gen = client.get(f"/api/v1/execution/jobs/{job_id}/evidence/{tid1}")
            assert r_wrong_gen.status_code == 409
            assert r_wrong_gen.json()["error"]["code"] == "GENERATION_MISMATCH"

            # 5c) Deleted .evidence-digests.json -> 409 GENERATION_MISMATCH
            digest_file.unlink()
            r_no_manifest = client.get(f"/api/v1/execution/jobs/{job_id}/evidence/{tid1}")
            assert r_no_manifest.status_code == 409
            assert r_no_manifest.json()["error"]["code"] == "GENERATION_MISMATCH"

            # Restore manifest, then tamper evidence.json bytes -> 409 GENERATION_MISMATCH
            digest_file.write_text(orig_digest_text, encoding="utf-8")
            os.chmod(digest_file, 0o600)
            assert ctx.settings.evidence_store_dir is not None
            ev_path = ctx.settings.evidence_store_dir / job_id / tid1 / "evidence.json"
            ev_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
            r_tamper = client.get(f"/api/v1/execution/jobs/{job_id}/evidence/{tid1}")
            assert r_tamper.status_code == 409
            assert r_tamper.json()["error"]["code"] == "GENERATION_MISMATCH"
            assert client.get(runner_evidence).status_code == 409

            ctx.db.close_thread_connection()


def test_execution_capabilities_limits_discovery_to_visible_selected_suite(m0_app, monkeypatch, tmp_path):
    service = m0_app.state.ctx.execution
    cases = tmp_path / "cases.jsonl"
    cases.write_text('{"id": "real-task"}\n', encoding="utf-8")
    suite_ids = ("quick", "large", "private-holdout", "missing")
    suites = [
        {"suite_id": suite_id, "display_name": suite_id, "runner": "direct",
         "dataset_path": str(tmp_path / "missing.jsonl" if suite_id == "missing" else cases)}
        for suite_id in suite_ids
    ]
    monkeypatch.setattr(service, "_load_registered_suites", lambda: suites)
    discover_tasks = service._discover_suite_tasks
    calls = []

    def discover(suite):
        calls.append(suite["suite_id"])
        return discover_tasks(suite)

    monkeypatch.setattr(service, "_discover_suite_tasks", discover)
    endpoint = "/api/v1/execution/capabilities"
    client = TestClient(m0_app)
    assert client.get(endpoint, params={"discover_tasks": "false"}).status_code == 401
    login_client(client, "viewer_noscope", "StrongViewerPassword!3")

    def get_suites(**params):
        response = client.get(endpoint, params=params)
        assert response.status_code == 200
        assert str(tmp_path) not in response.text
        return {suite["suite_id"]: suite for suite in response.json()["suites"]}

    overview = get_suites(discover_tasks="false")
    assert list(overview) == ["quick", "large", "missing"]
    assert overview["quick"]["task_count"] is None
    assert overview["quick"]["sample_tasks"] == []
    assert overview["quick"]["task_discovery_status"] == "not_requested"
    assert overview["missing"]["task_discovery_status"] == "dataset_file_missing"
    assert get_suites(suite_id="private-holdout") == {}
    assert get_suites(suite_id="unknown") == {}
    assert calls == []

    selected = get_suites(suite_id="quick")
    assert list(selected) == ["quick"]
    assert selected["quick"]["task_count"] == 1
    assert selected["quick"]["sample_tasks"] == ["real-task"]
    assert calls == ["quick"]

    full = get_suites()
    assert full["quick"] == selected["quick"]
    assert full["large"]["task_discovery_status"] == "ok"
    assert calls == ["quick", "quick", "large", "missing"]

    login_client(client, "admin_test", "StrongAdminPassword!1")
    holdout = get_suites(suite_id="private-holdout")
    assert list(holdout) == ["private-holdout"]
    assert holdout["private-holdout"]["task_count"] == 1
    assert calls == ["quick", "quick", "large", "missing", "private-holdout"]


def test_m3_s04_s06_disconnected_503_real_task_discovery_capabilities_resume_and_0700_authz(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 S-04 & S-06:
    - Disconnected Runner (no IPC configured) reports isolated_runner_ready=False & fails closed 503 RUNNER_NOT_ATTACHED.
    - Real suite task discovery returns real tasks (2 on direct-smoke-v1, 40 on 197 tianxi-synth-v1-train; ZERO fake 4-task IDs).
    - Unknown --task, unintegrated suites, and raw browser api_key rejected with 422.
    - VLMEvalKit --task forces mode='infer' & unscored (VLMEVALKIT.md:44).
    - Connected external Runner rejects Harbor without Docker (503 RUNNER_CAPABILITY_UNAVAILABLE) & VLMEvalKit without
      VLMEVALKIT_ROOT (503 RUNNER_CAPABILITY_UNAVAILABLE) instead of faking scores.
    - Non-prod Harbor exit-75 DispatchGuard fixture proves paused_by_guard + native --resume lock rules;
      direct native --resume returns 409 RESUME_UNSUPPORTED while derived_task_subset succeeds (202).
    - 0700/0600 isolation + CIFS 503 + Scope/Team Revocation 403 enforced on evidence & SSE."""

    def _gw_handler(_path: str, _headers: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prompt = str(body["messages"][0]["content"])
        ans = "4" if "2 + 2" in prompt else "READY"
        return 200, {
            "id": "chatcmpl-s04",
            "model": body.get("model", "Qwen3_8-27B"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ans}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

    with _MockChatGatewayServer(_gw_handler) as gw:
        cfg_dir = tmp_path / "cfg_s04"
        _write_test_config(cfg_dir, approval_entries=[], gateway_base_url=gw.base_url)
        isolated_settings.config_dir = cfg_dir
        # Point tianxi_root at real read-only /home/share-data/tianxi_benches so real 40-task discovery is verified!
        isolated_settings.tianxi_root = Path("/home/share-data/tianxi_benches")

        # 1. Disconnected Runner (default production posture) -> isolated_runner_ready=False, submit -> 503!
        app_disc = create_app(isolated_settings, run_startup_scan=False)
        ctx_d = app_disc.state.ctx
        create_user(
            ctx_d.db,
            username="m3_disc_adm",
            password="StrongM3Disc!2026",
            role="admin",
            projects=["default"],
            scopes=list(ALL_SCOPES),
        )
        c_disc = TestClient(app_disc)
        csrf_disc = login_client(c_disc, "m3_disc_adm", "StrongM3Disc!2026")

        caps_d = c_disc.get("/api/v1/execution/capabilities").json()
        assert caps_d["runner_environment"]["isolated_runner_ready"] is False
        assert caps_d["runner_environment"]["runner_connected"] is False
        assert caps_d["runner_environment"]["docker_group_attached"] is False

        r_disc_sub = c_disc.post(
            "/api/v1/execution/jobs",
            headers={"X-CSRF-Token": csrf_disc},
            json={
                "kind": "direct",
                "suite_id": "direct-smoke-v1",
                "selection_mode": "all",
                "project_id": "default",
            },
        )
        assert r_disc_sub.status_code == 503
        assert r_disc_sub.json()["error"]["code"] == "RUNNER_NOT_ATTACHED"

        # Real suite task discovery in --plan (ZERO fake 4-task fallback!)
        p_direct = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={"kind": "direct", "suite_id": "direct-smoke-v1", "selection_mode": "all", "project_id": "default"},
        )
        assert p_direct.status_code == 200, p_direct.text
        assert p_direct.json()["plan"]["planned_tasks"] == ["smoke-arith-001", "smoke-echo-002"]
        assert p_direct.json()["plan"]["planned_task_count"] == 2

        p_synth = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={"kind": "harbor", "suite_id": "tianxi-synth-v1-train", "selection_mode": "all", "project_id": "default"},
        )
        assert p_synth.status_code == 200, p_synth.text
        assert p_synth.json()["plan"]["planned_task_count"] == 40
        assert "log-rollup-train-001" in p_synth.json()["plan"]["planned_tasks"]
        assert "tianxi-synth-v1-train-task-01" not in p_synth.json()["plan"]["planned_tasks"]

        # Reject unknown --task, unintegrated suites, and raw browser api_key with 422
        r_bad_task = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={
                "kind": "direct",
                "suite_id": "direct-smoke-v1",
                "selection_mode": "tasks",
                "tasks": ["nonexistent-task-999"],
                "project_id": "default",
            },
        )
        assert r_bad_task.status_code == 422

        for bad_suite in ("mmlu-pro-raw", "bfcl-v4-raw", "global-mmlu-lite-pending"):
            r_bs = c_disc.post(
                "/api/v1/execution/plan",
                headers={"X-CSRF-Token": csrf_disc},
                json={"kind": "harbor", "suite_id": bad_suite, "selection_mode": "all", "project_id": "default"},
            )
            assert r_bs.status_code == 422

        r_raw_key = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={
                "kind": "direct",
                "suite_id": "direct-smoke-v1",
                "selection_mode": "all",
                "project_id": "default",
                "api_key": "sk-forbidden-browser-key",
            },
        )
        assert r_raw_key.status_code == 422

        # VLMEvalKit --task forces mode='infer' & unscored (VLMEVALKIT.md:44)
        vlm_suites = [s for s in caps_d["suites"] if s["runner"] == "vlmevalkit"]
        assert len(vlm_suites) >= 1
        vlm_suite = next((s for s in vlm_suites if s.get("sample_tasks")), vlm_suites[0])
        vlm_task = (vlm_suite.get("sample_tasks") or ["0"])[0]
        p_vlm = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={
                "kind": "vlmevalkit",
                "suite_id": vlm_suite["suite_id"],
                "selection_mode": "tasks",
                "tasks": [vlm_task],
                "vlmevalkit_mode": "all",
                "project_id": "default",
            },
        )
        assert p_vlm.status_code == 200, p_vlm.text
        vplan = p_vlm.json()["plan"]
        assert vplan["vlmevalkit_mode"] == "infer"
        assert vplan["forced_infer_by_task_selection"] is True
        assert vplan["effective_official_status"] == "unscored"
        # Native managed run.py has NO --mode and NO --evidence-store CLI flags!
        assert "--mode" not in shlex.split(vplan["command_preview"])
        assert "--evidence-store" not in shlex.split(vplan["command_preview"])

        # VLMEvalKit --all with explicit vlmevalkit_mode='infer' on default-'all' suite (cmmmu-val):
        p_cmmmu = c_disc.post(
            "/api/v1/execution/plan",
            headers={"X-CSRF-Token": csrf_disc},
            json={
                "kind": "vlmevalkit",
                "suite_id": "cmmmu-val",
                "selection_mode": "all",
                "vlmevalkit_mode": "infer",
                "project_id": "default",
            },
        )
        assert p_cmmmu.status_code == 200, p_cmmmu.text
        assert p_cmmmu.json()["plan"]["vlmevalkit_mode"] == "infer"
        assert "--mode" not in shlex.split(p_cmmmu.json()["plan"]["command_preview"])
        assert ".resolved_harbor.toml" in p_cmmmu.json()["plan"]["command_preview"]
        ctx_d.db.close_thread_connection()

        # 2. Connected external Runner Daemon (NO Docker / NO VLMEvalKit -> truthful 503 capability rejections!)
        with _start_external_runner_daemon(isolated_settings) as _runner_proc:
            app = create_app(isolated_settings, run_startup_scan=False)
            ctx = app.state.ctx
            uid_res = create_user(
                ctx.db,
                username="m3_s04_res",
                password="StrongM3Res!2026",
                role="researcher",
                projects=["proj_m3"],
                scopes=["trace:read", "direct_vlm_qa:read"],
            )
            create_user(
                ctx.db,
                username="m3_s04_noscope",
                password="StrongM3NoScope!26",
                role="viewer",
                projects=["proj_m3"],
                scopes=[],
            )
            c_res = TestClient(app)
            csrf_res = login_client(c_res, "m3_s04_res", "StrongM3Res!2026")
            c_noscope = TestClient(app)
            login_client(c_noscope, "m3_s04_noscope", "StrongM3NoScope!26")

            # Harbor without Docker -> truthful 503 RUNNER_CAPABILITY_UNAVAILABLE (NEVER fakes scores!)
            r_no_docker = c_res.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_res},
                json={
                    "kind": "harbor",
                    "suite_id": "tianxi-synth-v1-train",
                    "selection_mode": "tasks",
                    "tasks": ["log-rollup-train-001"],
                    "project_id": "proj_m3",
                },
            )
            assert r_no_docker.status_code == 503, r_no_docker.text
            assert r_no_docker.json()["error"]["code"] == "RUNNER_CAPABILITY_UNAVAILABLE"

            # VLMEvalKit without VLMEVALKIT_ROOT -> truthful 503 RUNNER_CAPABILITY_UNAVAILABLE!
            r_no_vlm = c_res.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_res},
                json={
                    "kind": "vlmevalkit",
                    "suite_id": vlm_suite["suite_id"],
                    "selection_mode": "tasks",
                    "tasks": [vlm_task],
                    "project_id": "proj_m3",
                },
            )
            assert r_no_vlm.status_code == 503, r_no_vlm.text
            assert r_no_vlm.json()["error"]["code"] == "RUNNER_CAPABILITY_UNAVAILABLE"

            # Real Direct single-task job succeeds, writes strict 0700/0600 dirs & enforces Scope/Revocation authz!
            r_dir = c_res.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_res},
                json={
                    "kind": "direct",
                    "suite_id": "direct-smoke-v1",
                    "selection_mode": "tasks",
                    "tasks": ["smoke-arith-001"],
                    "batch_name": "m3-s04-direct-single",
                    "project_id": "proj_m3",
                },
            )
            assert r_dir.status_code == 202, r_dir.text
            dir_jid = r_dir.json()["job_id"]
            dir_det = _wait_job_terminal(c_res, dir_jid)
            assert dir_det["status"] == "finished"
            assert dir_det["live_channel"]["trials"][0]["reward"] == 1.0
            dir_tid = dir_det["live_channel"]["trials"][0]["trial_id"]

            # Strict 0700 dirs & 0600 files (S-06)
            assert ctx.settings.runner_work_dir is not None and ctx.settings.evidence_store_dir is not None
            assert ((ctx.settings.runner_work_dir / dir_jid).stat().st_mode & 0o777) == 0o700
            assert ((ctx.settings.evidence_store_dir / dir_jid).stat().st_mode & 0o777) == 0o700
            assert ((ctx.settings.evidence_store_dir / dir_jid / dir_tid / "evidence.json").stat().st_mode & 0o777) == 0o600

            # Authorized user reads evidence (with verifier_log redacted because researcher lacks verifier_log:read!)
            ev_ok = c_res.get(f"/api/v1/execution/jobs/{dir_jid}/evidence/{dir_tid}")
            assert ev_ok.status_code == 200, ev_ok.text
            assert ev_ok.json()["evidence"].get("verifier_log_redacted") is True

            # Missing direct_vlm_qa:read scope -> 403 SCOPE_FORBIDDEN
            ev_denied = c_noscope.get(f"/api/v1/execution/jobs/{dir_jid}/evidence/{dir_tid}")
            assert ev_denied.status_code == 403
            assert ev_denied.json()["error"]["code"] == "SCOPE_FORBIDDEN"

            # Direct native --resume returns 409 RESUME_UNSUPPORTED (S-04)
            r_dir_res = c_res.post(
                f"/api/v1/execution/jobs/{dir_jid}/resume",
                headers={"X-CSRF-Token": csrf_res},
                json={"resume_mode": "native"},
            )
            assert r_dir_res.status_code == 409
            assert r_dir_res.json()["error"]["code"] == "RESUME_UNSUPPORTED"

            # Mark direct job interrupted to verify derived_task_subset resume runs cleanly (202)
            with ctx.db.transaction() as conn:
                conn.execute("UPDATE exec_jobs SET status = 'interrupted' WHERE job_id = ?", (dir_jid,))
            r_dir_der = c_res.post(
                f"/api/v1/execution/jobs/{dir_jid}/resume",
                headers={"X-CSRF-Token": csrf_res},
                json={"resume_mode": "derived_task_subset"},
            )
            assert r_dir_der.status_code == 202, r_dir_der.text
            der_det = _wait_job_terminal(c_res, r_dir_der.json()["job_id"])
            assert der_det["status"] == "finished"

            # Team project revocation blocks BOTH evidence read AND SSE replay immediately (S-02)!
            with ctx.db.transaction() as conn:
                conn.execute("UPDATE users SET projects_json = '[]' WHERE user_id = ?", (uid_res,))
            ev_rev = c_res.get(f"/api/v1/execution/jobs/{dir_jid}/evidence/{dir_tid}")
            assert ev_rev.status_code == 403
            assert ev_rev.json()["error"]["code"] == "PROJECT_FORBIDDEN"
            sse_rev = c_res.get(f"/api/v1/execution/jobs/{dir_jid}/stream?once=true")
            assert sse_rev.status_code == 403
            assert sse_rev.json()["error"]["code"] == "PROJECT_FORBIDDEN"

            # CIFS Fail-Closed probe (S-06)
            if Path("/home/share-data").exists():
                orig_work = ctx.settings.runner_work_dir
                try:
                    ctx.settings.runner_work_dir = Path("/home/share-data/eval")
                    with ctx.db.transaction() as conn:
                        conn.execute("UPDATE users SET projects_json = '[\"proj_m3\"]' WHERE user_id = ?", (uid_res,))
                    r_cifs = c_res.post(
                        "/api/v1/execution/jobs",
                        headers={"X-CSRF-Token": csrf_res},
                        json={
                            "kind": "direct",
                            "suite_id": "direct-smoke-v1",
                            "selection_mode": "tasks",
                            "tasks": ["smoke-arith-001"],
                            "batch_name": "m3-s06-cifs-reject",
                            "project_id": "proj_m3",
                        },
                    )
                    assert r_cifs.status_code == 503
                    assert r_cifs.json()["error"]["code"] == "STAGING_PERMISSION_UNSAFE"
                finally:
                    ctx.settings.runner_work_dir = orig_work

            ctx.db.close_thread_connection()

        # 3. Native managed `run.py --plan` CLI contract verification across Harbor, Harbor Gateway, & VLMEvalKit!
        run_py = resolve_verified_managed_script(isolated_settings, "run.py")
        plan_work = tmp_path / "native_plan_work"
        plan_work.mkdir(parents=True, exist_ok=True)
        os.chmod(plan_work, 0o700)

        for label, pspec, expect_json_key, expect_val in (
            (
                "harbor",
                {
                    "kind": "harbor",
                    "suite_id": "tianxi-synth-v1-train",
                    "selection_mode": "tasks",
                    "tasks": ["log-rollup-train-001"],
                    "agent": "opencode",
                    "harness_version": "1.19.3",
                    "attempts": 1,
                    "pause_after": 3,
                    "model_route": "Qwen3_8-27B",
                    "batch_name": "plan-native-harbor",
                    "concurrent": 1,
                },
                None,
                "harbor run",
            ),
            (
                "harbor_gateway",
                {
                    "kind": "harbor_gateway",
                    "suite_id": "tianxi-synth-v1-train",
                    "selection_mode": "tasks",
                    "tasks": ["log-rollup-train-001"],
                    "agent": "opencode",
                    "harness_version": "1.19.3",
                    "attempts": 1,
                    "pause_after": 3,
                    "model_route": "Qwen3_8-27B",
                    "batch_name": "plan-native-gw",
                    "concurrent": 1,
                },
                "runner",
                "openenv-gateway",
            ),
            (
                "vlmevalkit_cmmmu_all_infer",
                {
                    "kind": "vlmevalkit",
                    "suite_id": "cmmmu-val",
                    "selection_mode": "all",
                    "tasks": [],
                    "vlmevalkit_mode": "infer",
                    "model_route": "Qwen3_8-27B",
                    "batch_name": "plan-native-cmmmu-infer",
                    "concurrent": 1,
                    "config_path": str(isolated_settings.datasets_dir / "multimodal" / "harbor.toml"),
                },
                "mode",
                "infer",
            ),
            (
                "vlmevalkit_ai2d_task_infer",
                {
                    "kind": "vlmevalkit",
                    "suite_id": "ai2d-test-smoke",
                    "selection_mode": "tasks",
                    "tasks": ["0"],
                    "vlmevalkit_mode": "infer",
                    "model_route": "Qwen3_8-27B",
                    "batch_name": "plan-native-ai2d-task",
                    "concurrent": 1,
                    "config_path": str(isolated_settings.datasets_dir / "multimodal" / "harbor.toml"),
                },
                "mode",
                "infer",
            ),
            (
                "vlmevalkit_ai2d_all_all",
                {
                    "kind": "vlmevalkit",
                    "suite_id": "ai2d-test-smoke",
                    "selection_mode": "all",
                    "tasks": [],
                    "vlmevalkit_mode": "all",
                    "model_route": "Qwen3_8-27B",
                    "batch_name": "plan-native-ai2d-all",
                    "concurrent": 1,
                    "config_path": str(isolated_settings.datasets_dir / "multimodal" / "harbor.toml"),
                },
                "mode",
                "all",
            ),
        ):
            sub_w = plan_work / label
            sub_w.mkdir(parents=True, exist_ok=True)
            os.chmod(sub_w, 0o700)
            res_toml = materialize_managed_run_config(isolated_settings, pspec, sub_w)
            cmd_plan = build_managed_run_py_command(
                python_exe=sys.executable,
                run_py_path=str(run_py),
                spec=pspec,
                config_path=str(res_toml),
                base_url=gw.base_url,
                work_dir=str(sub_w),
                output_dir=str(sub_w / "raw-output"),
                plan_only=True,
            )
            assert "--evidence-store" not in cmd_plan
            assert "--mode" not in cmd_plan
            pr = subprocess.run(cmd_plan, capture_output=True, text=True, timeout=30, check=False)
            assert pr.returncode == 0, f"Native run.py --plan failed for {label}: {pr.stderr}\ncmd={cmd_plan}"
            if expect_json_key is None:
                assert expect_val in pr.stdout, pr.stdout
            else:
                parsed = json.loads(pr.stdout.strip())
                assert parsed.get(expect_json_key) == expect_val, f"{label}: expected {expect_json_key}={expect_val}, got {parsed}"

        # 4. REAL unmodified `managed_framework/run.py` execution via REAL external Runner Daemon:
        #    - Harbor exit-75 DispatchGuard pause + SAME-`--work` native `--resume` verifying real run.py lock!
        #    - Harbor Gateway (`--gateway-capture`) native `execute_gateway()` execution & reconcile!
        #    - VLMEvalKit native `execute_vlmevalkit()` execution (`--task` -> infer) & reconcile!
        mock_bin = tmp_path / "isolated_runner_bin"
        mock_bin.mkdir(parents=True, exist_ok=True)
        mock_docker = mock_bin / "docker"
        mock_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(mock_docker, 0o755)

        mock_harbor = mock_bin / "harbor"
        mock_harbor.write_text(
            f"""#!{sys.executable}
import hashlib, json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("harbor 0.26.0")
    raise SystemExit(0)

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

now = datetime.now(timezone.utc).isoformat()
if args[:2] == ["jobs", "resume"]:
    job_dir = Path(args[args.index("--job-path") + 1])
    assert (job_dir / "config.json").is_file(), "Native resume MUST reuse original --work job dir!"
    assert (job_dir / "lock.json").is_file(), "Native resume missing lock.json!"
    existing_results = list(job_dir.glob("*/result.json"))
    assert len(existing_results) == 1, f"Expected 1 prior trial in original job dir, got {{len(existing_results)}}"
    t2_id = str(uuid.uuid4())
    t2_dir = job_dir / t2_id
    t2_dir.mkdir(parents=True, exist_ok=True)
    (t2_dir / "result.json").write_text(json.dumps({{
        "id": t2_id,
        "task_name": "tianxi-synthetic/log-rollup-train-002",
        "started_at": now,
        "finished_at": now,
        "agent_info": {{"name": "opencode", "version": "1.19.3"}},
        "verifier_result": {{"rewards": {{"reward": 0.0}}}},
        "usage": {{"duration_seconds": 0.05}},
    }}), encoding="utf-8")
    (job_dir / "result.json").write_text(json.dumps({{"n_total_trials": 2, "stats": {{"n_completed_trials": 2}}}}), encoding="utf-8")
    raise SystemExit(0)

if args and args[0] == "run":
    jobs_dir = Path(args[args.index("--jobs-dir") + 1])
    job_name = args[args.index("--job-name") + 1]
    job_dir = jobs_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "config.json").write_text(json.dumps({{"job_name": job_name, "version": "1.19.3"}}), encoding="utf-8")
    (job_dir / "lock.json").write_text(json.dumps({{"locked": True}}), encoding="utf-8")
    (job_dir / ".tianxi-harbor-integrity.json").write_text(json.dumps({{
        "config.json": _sha(job_dir / "config.json"),
        "lock.json": _sha(job_dir / "lock.json"),
    }}), encoding="utf-8")
    t1_id = str(uuid.uuid4())
    t1_dir = job_dir / t1_id
    t1_dir.mkdir(parents=True, exist_ok=True)
    (t1_dir / "result.json").write_text(json.dumps({{
        "id": t1_id,
        "task_name": "tianxi-synthetic/log-rollup-train-001",
        "started_at": now,
        "finished_at": now,
        "agent_info": {{"name": "opencode", "version": "1.19.3"}},
        "verifier_result": {{"rewards": {{"reward": 1.0}}}},
        "usage": {{"duration_seconds": 0.04}},
    }}), encoding="utf-8")
    (job_dir / ".tianxi-pause.json").write_text(json.dumps({{
        "consecutive": 3,
        "kind": "agent_rate_limit",
        "summary": "Isolated DispatchGuard pause trigger",
    }}), encoding="utf-8")
    (job_dir / "result.json").write_text(json.dumps({{"n_total_trials": 2, "stats": {{"n_completed_trials": 1}}}}), encoding="utf-8")
    raise SystemExit(75)

raise SystemExit(2)
""",
            encoding="utf-8",
        )
        os.chmod(mock_harbor, 0o755)

        # Isolated minimal OpenEnv package so REAL `managed_framework/run.py --gateway-capture` executes end-to-end!
        openenv_root = tmp_path / "isolated_openenv"
        for rel, body in (
            ("openenv/__init__.py", ""),
            ("openenv/core/__init__.py", ""),
            ("openenv/core/harness/__init__.py", ""),
            ("openenv/core/harness/capture/__init__.py", ""),
            (
                "openenv/core/harness/capture/server.py",
                """
import os
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
def create_app(**kwargs):
    assert os.environ.get("SSL_CERT_FILE") == str(Path(__file__).parents[4] / "trusted-ca.pem")
    app = FastAPI()
    app.state.registry = {}
    app.state.inference = SimpleNamespace(**kwargs)
    return app
""",
            ),
            (
                "openenv/core/harness/capture/export.py",
                """
def export_session(session, **kwargs):
    return {
        "turns": [{
            "node_id": "n1", "index": 0, "root_id": "n1", "discarded": False,
            "request_messages": [{"role": "user", "content": "capture-only-test-contents s0123456789abcdef01234567"}],
            "request_tools": [], "response_message": {"role": "assistant", "content": "done"},
            "finish_reason": "stop", "sampling_params": {}, "requested_sampling_params": {},
        }],
        "validation": [],
    }
""",
            ),
            ("openenv/harbor/__init__.py", ""),
            (
                "openenv/harbor/rollout.py",
                """
import json, tomllib, uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

async def run_rollout(**kwargs):
    trials_dir = Path(kwargs["trials_dir"])
    task_dir = Path(kwargs["task_dir"])
    tmeta = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8")).get("task") or {}
    tname = str(tmeta.get("name") or task_dir.name)
    tid = str(uuid.uuid4())
    tdir = trials_dir / tid
    tdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (tdir / "result.json").write_text(json.dumps({
        "id": tid,
        "task_name": tname,
        "started_at": now,
        "finished_at": now,
        "agent_info": {"name": kwargs["harness"], "version": "1.19.3"},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "usage": {"duration_seconds": 0.03},
    }), encoding="utf-8")
    trial_obj = SimpleNamespace(id=tid, paths=SimpleNamespace(trial_dir=tdir))
    kwargs["on_trial_created"](trial_obj)
    sess_id = f"sess_{tid[:8]}"
    kwargs["registry"][sess_id] = SimpleNamespace(model_calls=1)
    kwargs["on_session_created"](sess_id)
    return SimpleNamespace(capture_level="text", atif={"ok": True}, reward=1.0, exception_type=None)
""",
            ),
        ):
            fp = openenv_root / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(body, encoding="utf-8")

        # Isolated minimal VLMEvalKit package so REAL `managed_framework/run.py` + `vlmevalkit.py` runs real HTTP loopback!
        vlm_root = tmp_path / "isolated_vlmevalkit"
        for rel, body in (
            ("vlmeval/__init__.py", '__version__ = "0.2.0-isolated"\n'),
            ("vlmeval/dataset/__init__.py", "from .image_base import ImageBaseDataset, ImageMCQDataset\n"),
            (
                "vlmeval/dataset/image_base.py",
                """
import os
from pathlib import Path
class _Row(dict):
    pass
class _Frame:
    def __init__(self, rows):
        self._rows = list(rows)
        self.columns = set(rows[0].keys()) if rows else set()
    def __len__(self):
        return len(self._rows)
    def __getitem__(self, key):
        if isinstance(key, list):
            return _Frame([r for r, keep in zip(self._rows, key) if keep])
        return _Col([r.get(key) for r in self._rows])
    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, r
    def drop(self, columns=None, errors="ignore"):
        drop_set = set(columns or [])
        return _Frame([{k: v for k, v in r.items() if k not in drop_set} for r in self._rows])
    def copy(self):
        return _Frame([dict(r) for r in self._rows])
    def __setitem__(self, key, vals):
        for r, v in zip(self._rows, vals):
            r[key] = v
        self.columns.add(key)
class _Col(list):
    def astype(self, _t):
        return _Col([str(x) for x in self])
    def duplicated(self):
        seen = set()
        out = []
        for x in self:
            out.append(x in seen)
            seen.add(x)
        return _Col(out)
    def any(self):
        return any(self)
    def isin(self, vals):
        s = set(vals)
        return [x in s for x in self]
class ImageBaseDataset:
    MODALITY = "IMAGE"
    meta_only = True
    def __init__(self, dataset="", **kwargs):
        self.dataset_name = dataset
        self.img_root = str(Path(os.environ.get("LMUData", "/tmp")))
        self.data = self.load_data(dataset)
    def load_data(self, dataset):
        return _Frame([])
    def build_prompt(self, row):
        return [{"type": "text", "value": str(row.get("question") or "2 + 2")}]
class ImageMCQDataset(ImageBaseDataset):
    def evaluate(self, prediction_file, **_kwargs):
        import time
        # Inference receipts exist before native evaluation writes the detail scores.
        time.sleep(0.35)
        detail = Path(prediction_file).with_name("predictions_exact_matching_result.xlsx")
        rows = "".join(f"{row['index']}\\t{i % 2}\\n" for i, row in enumerate(self.data._rows))
        detail.write_text("index\\thit\\n" + rows, encoding="utf-8")
        return [{"Overall": 0.5, "Science": 0.5}]
""",
            ),
            ("vlmeval/api/__init__.py", ""),
            (
                "vlmeval/api/gpt.py",
                """
import json, urllib.request
from types import SimpleNamespace
class GPT4V:
    fail_msg = "Failed to obtain answer via API."
    def __init__(self, model="", key="", api_base="", **kwargs):
        self.model = model
        self.key = key
        self.api_base = api_base
    def prepare_inputs(self, inputs, system_prompt=None):
        txt = " ".join(str(x.get("value") or "") for x in inputs if isinstance(x, dict))
        return [{"role": "user", "content": txt}]
    def generate(self, message, dataset=None):
        _, ans, _ = self.generate_inner(message)
        return ans
    def generate_inner(self, inputs, **kwargs):
        msgs = self.prepare_inputs(inputs)
        req = urllib.request.Request(
            self.api_base,
            data=json.dumps({"model": self.model, "messages": msgs}).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
            ans = body["choices"][0]["message"]["content"]
            return 0, ans, SimpleNamespace(status_code=int(getattr(resp, "status", 200)))
""",
            ),
            ("vlmeval/smp/__init__.py", "from .file import load, dump, INFER_FAIL_MSG\n"),
            (
                "vlmeval/smp/file.py",
                """
import csv, json
from pathlib import Path
from vlmeval.dataset.image_base import _Frame
INFER_FAIL_MSG = "Failed to obtain answer via API."
def load(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh, delimiter="\\t"))
    return _Frame(rows)
def dump(data, path):
    Path(path).write_text(json.dumps({"rows": len(data)}, ensure_ascii=False), encoding="utf-8")
""",
            ),
        ):
            fp = vlm_root / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(body, encoding="utf-8")

        trusted_ca = openenv_root / "trusted-ca.pem"
        shutil.copyfile(certifi.where(), trusted_ca)
        runner_extra_env = {
            "PATH": f"{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "PI_BENCH_RUNNER_BIN": str(mock_bin),
            "VLMEVALKIT_ROOT": str(vlm_root),
            "VLMEVALKIT_PYTHON": sys.executable,
            "LRM_RUNNER_OPENENV_PYTHON": sys.executable,
            "LRM_RUNNER_OPENENV_PYTHONPATH": str(openenv_root),
            "SSL_CERT_FILE": str(trusted_ca),
        }
        with _start_external_runner_daemon(isolated_settings, extra_env=runner_extra_env):
            app_h = create_app(isolated_settings, run_startup_scan=False)
            ctx_h = app_h.state.ctx
            original_owner_id = create_user(
                ctx_h.db,
                username="m3_hb_adm",
                password="StrongM3Hb!2026",
                role="admin",
                projects=["default"],
                scopes=list(ALL_SCOPES),
            )
            c_hb = TestClient(app_h)
            csrf_hb = login_client(c_hb, "m3_hb_adm", "StrongM3Hb!2026")

            # Verify Runner /health natively advertises all 4 runners (harbor, harbor_gateway, vlmevalkit, direct)
            caps_all = c_hb.get("/api/v1/execution/capabilities").json()["runner_environment"]
            assert caps_all["harbor_gateway_runtime_ready"] is True
            assert set(caps_all["supported_runners"]) == {"direct", "harbor", "harbor_gateway", "vlmevalkit"}

            # 4a) REAL managed `run.py` Harbor DispatchGuard exit-75 + SAME-`--work` native `--resume`!
            r_hb = c_hb.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_hb},
                json={
                    "kind": "harbor",
                    "suite_id": "tianxi-synth-v1-train",
                    "selection_mode": "tasks",
                    "tasks": ["log-rollup-train-001", "log-rollup-train-002"],
                    "agent": "opencode",
                    "harness_version": "1.19.3",
                    "batch_name": "m3-harbor-native-guard-75",
                    "project_id": "default",
                },
            )
            assert r_hb.status_code == 202, r_hb.text
            hb_jid = r_hb.json()["job_id"]
            hb_det = _wait_job_terminal(c_hb, hb_jid)
            assert hb_det["status"] == "paused_by_guard", hb_det
            assert hb_det["exit_code"] == 75
            # Verify REAL run.py wrote native batch-locks/<batch>.json inside original 0700 work dir!
            orig_work = isolated_settings.runner_work_dir / hb_jid  # type: ignore[operator]
            assert (orig_work / "batch-locks" / "m3-harbor-native-guard-75.json").is_file()

            # Wrong harness_version -> 409 RESUME_UNSUPPORTED
            r_bad_ver = c_hb.post(
                f"/api/v1/execution/jobs/{hb_jid}/resume",
                headers={"X-CSRF-Token": csrf_hb},
                json={"resume_mode": "native", "harness_version": "9.9.9"},
            )
            assert r_bad_ver.status_code == 409
            assert r_bad_ver.json()["error"]["code"] == "RESUME_UNSUPPORTED"

            # Matching harness_version + SAME original 0700 --work dir -> REAL `run.py --resume` passes lock check & finishes BOTH trials!
            create_user(
                ctx_h.db, username="m3_resume_adm", password="ResumeOperator!2026", role="admin",
                projects=["default"], scopes=list(ALL_SCOPES),
            )
            c_resume = TestClient(app_h)
            csrf_resume = login_client(c_resume, "m3_resume_adm", "ResumeOperator!2026")
            r_ok_res = c_resume.post(
                f"/api/v1/execution/jobs/{hb_jid}/resume",
                headers={"X-CSRF-Token": csrf_resume},
                json={"resume_mode": "native", "harness_version": "1.19.3"},
            )
            assert r_ok_res.status_code == 202, r_ok_res.text
            assert r_ok_res.json().get("reused_original_work_dir") is True
            res_det = _wait_job_terminal(c_hb, r_ok_res.json()["job_id"])
            assert res_det["status"] == "finished", res_det
            hb_bid = res_det["shared_snapshot"]["batch_id"]
            hb_b = c_hb.get(f"/api/v1/batches/{hb_bid}")
            assert hb_b.status_code == 200, hb_b.text
            assert hb_b.json()["totals"]["scored"] == 2
            assert abs(hb_b.json()["totals"]["valid_mean"] - 0.5) < 1e-6
            assert ctx_h.db.fetchone("SELECT owner_id FROM exec_jobs WHERE job_id = ?", (r_ok_res.json()["job_id"],)) == {
                "owner_id": original_owner_id,
            }
            assert ctx_h.db.fetchone("SELECT project_id, owner_id FROM batches WHERE batch_id = ?", (hb_bid,)) == {
                "project_id": "default", "owner_id": original_owner_id,
            }
            repeat = c_resume.post(
                f"/api/v1/execution/jobs/{hb_jid}/resume",
                headers={"X-CSRF-Token": csrf_resume},
                json={"resume_mode": "native"},
            )
            assert repeat.status_code == 409
            assert repeat.json()["error"]["code"] == "BATCH_ALREADY_EXISTS"

            # 4b) REAL managed `run.py --gateway-capture` (Harbor Gateway) execution & reconcile!
            r_gw = c_hb.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_hb},
                json={
                    "kind": "harbor_gateway",
                    "suite_id": "tianxi-synth-v1-train",
                    "selection_mode": "tasks",
                    "tasks": ["log-rollup-train-001"],
                    "agent": "opencode",
                    "harness_version": "1.19.3",
                    "batch_name": "m3-harbor-gw-native-run",
                    "project_id": "default",
                },
            )
            assert r_gw.status_code == 202, r_gw.text
            gw_jid = r_gw.json()["job_id"]
            gw_det = _wait_job_terminal(c_hb, gw_jid)
            assert gw_det["status"] == "finished", gw_det
            gw_bid = gw_det["shared_snapshot"]["batch_id"]
            batch_base = f"/api/v1/batches/{gw_bid}"
            batch_response = c_hb.get(batch_base)
            assert batch_response.status_code == 200
            assert batch_response.json()["has_gateway"]
            episode = c_hb.get(f"{batch_base}/episodes").json()["items"][0]
            assert episode["gateway_status"] == "captured"
            trial_base = f"{batch_base}/trials/{episode['trial_id']}"
            detail = c_hb.get(trial_base)
            assert detail.json()["runner_gateway"] is True
            assert detail.json()["gateway_status"] == "captured"
            assert "capture-only-test-contents" not in detail.text
            gateway_route = f"{trial_base}/runner-evidence?section=gateway"
            current_gateway = f"{gateway_route}&generation_id={episode['generation_id']}"
            clients = {}
            gateway_reader_id = None
            for name, projects, scopes in (
                ("gw_plain_viewer", ["default"], []),
                ("gw_scope_viewer", ["default"], ["gateway:read"]),
                ("gw_other_viewer", ["other"], ["gateway:read"]),
            ):
                password = "RunnerGatewayViewer!1"
                viewer_id = create_user(ctx_h.db, username=name, password=password, projects=projects, scopes=scopes)
                clients[name] = TestClient(app_h)
                login_client(clients[name], name, password)
                if name == "gw_scope_viewer":
                    gateway_reader_id = viewer_id
            plain = clients["gw_plain_viewer"].get(current_gateway)
            assert plain.status_code == 200 and plain.json()["redacted"] is True
            assert plain.json()["summary"]["turn_count"] == 1
            assert not plain.json()["turns"] and "gateway:read" in plain.json()["redacted_reason"]
            allowed = clients["gw_scope_viewer"]
            full = allowed.get(current_gateway)
            assert full.status_code == 200 and full.headers["cache-control"] == "private, no-store"
            assert full.json()["generation_id"] == episode["generation_id"]
            assert full.json()["redacted"] is False and "capture-only-test-contents" in full.text
            assert "s0123456789abcdef01234567" not in full.text
            assert clients["gw_other_viewer"].get(current_gateway).json()["error"]["code"] == "PROJECT_FORBIDDEN"
            stale = allowed.get(f"{gateway_route}&generation_id=gen_old")
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "GENERATION_MISMATCH"
            with ctx_h.db.transaction() as conn:
                conn.execute("UPDATE users SET scopes_json = '[]' WHERE user_id = ?", (gateway_reader_id,))
            assert allowed.get(current_gateway).json()["turns"] == []

            # The gateway reference is anchored in the committed restricted evidence and omitted
            # from the generic evidence API. Older bundles can recover it from the sealed native batch.
            digest_path = ctx_h.settings.runner_work_dir / gw_jid / ".evidence-digests.json"
            evidence_manifest = json.loads(digest_path.read_text())
            evidence_path = ctx_h.settings.evidence_store_dir / gw_jid / episode["trial_id"] / "evidence.json"
            bound_trial = json.loads(evidence_path.read_text())
            assert bound_trial["gateway_receipt"]
            default_evidence = c_hb.get(f"/api/v1/execution/jobs/{gw_jid}/evidence/{episode['trial_id']}")
            assert default_evidence.status_code == 200 and "gateway_receipt" not in default_evidence.text
            bound_trial.pop("gateway_receipt")
            legacy_bytes = json.dumps(bound_trial).encode()
            evidence_path.write_bytes(legacy_bytes)
            evidence_manifest["trials"][episode["trial_id"]].update(
                size=len(legacy_bytes), sha256=hashlib.sha256(legacy_bytes).hexdigest(),
            )
            digest_path.write_text(json.dumps(evidence_manifest))
            with ctx_h.db.transaction() as conn:
                conn.execute("UPDATE episodes SET gateway_status = 'not_captured' WHERE batch_id = ?", (gw_bid,))
            assert c_hb.get(trial_base).json()["gateway_status"] == "captured"
            assert c_hb.get(current_gateway).json()["summary"]["validated"] is True

            # Harbor Gateway native --resume -> 409 RESUME_UNSUPPORTED
            assert c_hb.post(
                f"/api/v1/execution/jobs/{gw_jid}/resume",
                headers={"X-CSRF-Token": csrf_hb},
                json={"resume_mode": "native"},
            ).status_code == 409

            # 4c) REAL managed `run.py` + `vlmevalkit.py` execution (--task -> infer, unscored) & reconcile!
            ai2d_suite = next(s for s in caps_d["suites"] if s["suite_id"] == "ai2d-test-smoke")
            ai2d_task = ai2d_suite["sample_tasks"][0]
            r_vlm = c_hb.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_hb},
                json={
                    "kind": "vlmevalkit",
                    "suite_id": "ai2d-test-smoke",
                    "selection_mode": "tasks",
                    "tasks": [ai2d_task],
                    "vlmevalkit_mode": "all",
                    "batch_name": "m3-vlm-native-run",
                    "project_id": "default",
                },
            )
            assert r_vlm.status_code == 202, r_vlm.text
            vlm_jid = r_vlm.json()["job_id"]
            vlm_det = _wait_job_terminal(c_hb, vlm_jid)
            assert vlm_det["status"] == "finished", vlm_det
            assert vlm_det["vlmevalkit_mode"] == "infer"
            vlm_bid = vlm_det["shared_snapshot"]["batch_id"]
            vlm_b = c_hb.get(f"/api/v1/batches/{vlm_bid}")
            assert vlm_b.status_code == 200, vlm_b.text
            assert vlm_b.json()["totals"]["recorded"] == 1

            # 4d) REAL managed `run.py` + `vlmevalkit.py` execution (--all with explicit mode='infer' via 0600 TOML overlay)!
            r_vlm_all = c_hb.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_hb},
                json={
                    "kind": "vlmevalkit",
                    "suite_id": "ai2d-test-smoke",
                    "selection_mode": "all",
                    "vlmevalkit_mode": "infer",
                    "batch_name": "m3-vlm-native-all-infer",
                    "project_id": "default",
                },
            )
            assert r_vlm_all.status_code == 202, r_vlm_all.text
            vlm_all_det = _wait_job_terminal(c_hb, r_vlm_all.json()["job_id"])
            assert vlm_all_det["status"] == "finished", vlm_all_det
            assert vlm_all_det["vlmevalkit_mode"] == "infer"
            vlm_all_bid = vlm_all_det["shared_snapshot"]["batch_id"]
            vlm_all_b = c_hb.get(f"/api/v1/batches/{vlm_all_bid}")
            assert vlm_all_b.status_code == 200, vlm_all_b.text
            assert vlm_all_b.json()["totals"]["recorded"] == 2
            assert vlm_all_b.json()["official_status"] == "unscored"
            assert vlm_all_b.json()["official_result"] is None

            # The all-mode scorer updates receipts after inference; publish the final rewards once.
            official_name = "m3-vlm-native-official"
            r_official = c_hb.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_hb},
                json={
                    "kind": "vlmevalkit",
                    "suite_id": "ai2d-test-smoke",
                    "selection_mode": "all",
                    "vlmevalkit_mode": "all",
                    "batch_name": official_name,
                    "model_route": "vlm-official-fixture",
                    "project_id": "default",
                },
            )
            assert r_official.status_code == 202, r_official.text
            official_job = r_official.json()["job_id"]
            official_detail = _wait_job_terminal(c_hb, official_job)
            assert official_detail["status"] == "finished", official_detail
            snapshot = official_detail["shared_snapshot"]
            official_batch = c_hb.get(f"/api/v1/batches/{snapshot['batch_id']}")
            assert official_batch.status_code == 200, official_batch.text
            batch_dto = official_batch.json()
            assert batch_dto["generation_id"] == snapshot["generation_id"]
            assert batch_dto["official_status"] == "official_verified"
            assert batch_dto["official_result"]["score"] == 0.5
            assert batch_dto["official_result"]["denominator"] == 2
            assert batch_dto["totals"]["scored"] == 2
            assert batch_dto["totals"]["valid_mean"] == 0.5

            rewards = ctx_h.db.fetchall(
                "SELECT reward, reward_valid FROM trial_protocol_scores WHERE batch_id = ? ORDER BY reward",
                (snapshot["batch_id"],),
            )
            assert [(row["reward"], row["reward_valid"]) for row in rewards] == [(0.0, 1), (1.0, 1)]
            leaderboard = c_hb.get("/api/v1/leaderboard?runner=vlmevalkit")
            assert leaderboard.status_code == 200, leaderboard.text
            cells = [
                cell for item in leaderboard.json()["items"] for cell in item["benchmarks"].values()
                if cell.get("batch_id") == snapshot["batch_id"]
            ]
            assert len(cells) == 1
            assert cells[0]["official_status"] == "official_verified"
            assert cells[0]["raw_score"] == 0.5 and cells[0]["planned"] == 2

            published_root = isolated_settings.published_batches_dir
            assert published_root is not None
            public_dir = published_root / official_name
            publish_manifest = json.loads((public_dir / "publish-manifest.json").read_text(encoding="utf-8"))
            assert publish_manifest["generation_id"] == snapshot["generation_id"]
            for name in ("official-result.json", "vlmevalkit/metrics.json"):
                raw = (public_dir / name).read_bytes()
                assert publish_manifest["top_level_files"][name] == {
                    "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                }
                assert name not in publish_manifest["absent_optional_files"]
            metrics = json.loads((public_dir / "vlmevalkit/metrics.json").read_text(encoding="utf-8"))
            assert metrics == [{"Overall": 0.5, "Science": 0.5}]
            ctx_h.db.close_thread_connection()


def test_m3_s02_reserves_publication_before_dispatch_until_finalization(
    tmp_path: Path, isolated_settings: Settings, monkeypatch,
) -> None:
    def completion(_path, _headers, body):
        return 200, {
            "id": "reservation-fixture", "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    with _MockChatGatewayServer(completion) as gateway:
        isolated_settings.config_dir = tmp_path / "config"
        isolated_settings.tianxi_root = tmp_path / "tianxi"
        _write_test_config(isolated_settings.config_dir, approval_entries=[], gateway_base_url=gateway.base_url)
        with _start_external_runner_daemon(isolated_settings):
            app = create_app(isolated_settings, run_startup_scan=False)
            ctx = app.state.ctx
            clients = {}
            for name, project in (("owner_alpha", "team_alpha"), ("owner_beta", "team_beta")):
                password = "TeamReservation!2026"
                owner = create_user(ctx.db, username=name, password=password, role="researcher",
                                    projects=[project], scopes=list(ALL_SCOPES))
                client = TestClient(app)
                clients[project] = client, login_client(client, name, password), owner

            def submit(project, batch_name):
                client, csrf, _ = clients[project]
                return client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json={
                    "kind": "direct", "suite_id": "direct-smoke-v1", "selection_mode": "tasks",
                    "tasks": ["smoke-arith-001"], "batch_name": batch_name, "project_id": project,
                })

            entered_dispatch, release_dispatch = threading.Event(), threading.Event()
            entered_finalize, release_finalize = threading.Event(), threading.Event()
            send_job = ctx.execution.ipc.submit_job
            finalize = ctx.execution._publish_public_dto_and_reconcile
            sent = []

            def stalled_dispatch(payload):
                sent.append(payload["job_id"])
                if len(sent) == 1:
                    entered_dispatch.set()
                    assert release_dispatch.wait(20)
                return send_job(payload)

            def stalled_finalize(**kwargs):
                entered_finalize.set()
                assert release_finalize.wait(20)
                return finalize(**kwargs)

            monkeypatch.setattr(ctx.execution.ipc, "submit_job", stalled_dispatch)
            monkeypatch.setattr(ctx.execution, "_publish_public_dto_and_reconcile", stalled_finalize)
            batch_name = "team-claimed-publication"
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(submit, "team_alpha", batch_name)
                try:
                    assert entered_dispatch.wait(10)
                    assert submit("team_beta", batch_name).status_code == 409
                    assert len(sent) == 1
                    release_dispatch.set()
                    accepted = first.result(timeout=15)
                    assert accepted.status_code == 202, accepted.text
                    assert entered_finalize.wait(10)
                    job_id = accepted.json()["job_id"]
                    assert ctx.db.fetchone("SELECT status FROM exec_jobs WHERE job_id = ?", (job_id,)) == {
                        "status": "finalizing",
                    }
                    assert submit("team_beta", batch_name).status_code == 409
                    assert len(sent) == 1
                finally:
                    release_dispatch.set()
                    release_finalize.set()

            alpha, _, owner = clients["team_alpha"]
            detail = _wait_job_terminal(alpha, job_id)
            assert detail["status"] == "finished", detail
            snapshot = detail["shared_snapshot"]
            batch_id = snapshot["batch_id"]
            assert ctx.db.fetchone("SELECT project_id, owner_id FROM batches WHERE batch_id = ?", (batch_id,)) == {
                "project_id": "team_alpha", "owner_id": owner,
            }
            folder = isolated_settings.published_batches_dir / batch_name
            manifest_bytes = (folder / "publish-manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            ledger_path = isolated_settings.state_dir / "runner-submissions" / f"{batch_name}.json"
            binding = json.loads(ledger_path.read_bytes())
            assert binding["job_id"] == manifest["runner_job_id"] == job_id
            assert binding["generation_id"] == snapshot["generation_id"] == manifest["generation_id"]
            assert binding["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
            assert ctx.db.fetchone("SELECT 1 FROM exec_batch_reservations WHERE job_id = ?", (job_id,)) is None
            assert submit("team_beta", batch_name).status_code == 409

            # A confirmed dispatch rejection releases the location for another team's retry.
            monkeypatch.setattr(ctx.execution.ipc, "submit_job", lambda _: (503, {"code": "RUNNER_STDIO_CAPACITY_EXHAUSTED"}))
            assert submit("team_alpha", "team-retry-publication").status_code == 503
            assert ctx.db.fetchone("SELECT 1 FROM exec_batch_reservations WHERE batch_name = 'team-retry-publication'") is None
            assert ctx.db.fetchone("SELECT 1 FROM exec_jobs WHERE batch_name = 'team-retry-publication'") is None
            monkeypatch.setattr(ctx.execution.ipc, "submit_job", send_job)
            retry = submit("team_beta", "team-retry-publication")
            assert retry.status_code == 202, retry.text
            beta, _, _ = clients["team_beta"]
            assert _wait_job_terminal(beta, retry.json()["job_id"])["status"] == "finished"


@pytest.mark.parametrize("crash_at", [
    "before_dispatch_intent", "before_dispatch", "after_acceptance", "after_trial_event", "after_publish",
])
def test_m3_s02_web_restart_recovers_runner_reservation(
    crash_at: str, tmp_path: Path, isolated_settings: Settings, monkeypatch,
) -> None:
    def completion(_path, _headers, body):
        return 200, {
            "id": "restart-fixture", "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    batch_name = "restart-publication"
    job_request = {"kind": "direct", "suite_id": "direct-smoke-v1", "selection_mode": "all",
                   "batch_name": batch_name, "project_id": "team_restart"}
    crash_code = '''
import json
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from tests.conftest import login_client

crash_at, config, request, wire_path = sys.argv[1:]
settings = Settings(**{name: Path(value) for name, value in json.loads(config).items()},
                    enable_background_indexer=False, cookie_secure=False)
app = create_app(settings, run_startup_scan=False)
service = app.state.ctx.execution
if crash_at == 'after_publish':
    def crash_before_reconcile(**kwargs):
        os._exit(57)
    service._reconcile_published_job = crash_before_reconcile
elif crash_at in ('before_dispatch_intent', 'after_trial_event'):
    emit = service._emit_trusted_event
    crash_event = 'job_queued' if crash_at == 'before_dispatch_intent' else 'trial_finished'
    def crash_after_event(job_id, event_type, payload, **kwargs):
        result = emit(job_id, event_type, payload, **kwargs)
        if event_type == crash_event:
            os._exit(57)
        return result
    service._emit_trusted_event = crash_after_event
else:
    send = service.ipc.submit_job
    def crash_during_dispatch(payload):
        if crash_at == 'after_acceptance':
            assert send(payload)[0] == 202
        else:
            fd = os.open(wire_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream)
        os._exit(57)
    service.ipc.submit_job = crash_during_dispatch
with TestClient(app) as client:
    csrf = login_client(client, 'restart_admin', 'RestartFixture!2026')
    response = client.post('/api/v1/execution/jobs', headers={'X-CSRF-Token': csrf}, json=json.loads(request))
    assert response.status_code == 202, response.text
    time.sleep(12)
'''

    with _MockChatGatewayServer(completion) as gateway:
        isolated_settings.config_dir = tmp_path / "config"
        isolated_settings.tianxi_root = tmp_path / "tianxi"
        _write_test_config(isolated_settings.config_dir, approval_entries=[], gateway_base_url=gateway.base_url)
        with _start_external_runner_daemon(isolated_settings):
            app = create_app(isolated_settings, run_startup_scan=False)
            owner = create_user(app.state.ctx.db, username="restart_admin", password="RestartFixture!2026",
                                role="researcher", projects=["team_restart"], scopes=list(ALL_SCOPES))
            paths = {name: str(getattr(isolated_settings, name)) for name in (
                "runtime_dir", "config_dir", "tianxi_root", "runner_ipc_socket", "runner_ipc_token_file",
            )}
            wire_path = tmp_path / "delayed-submission.json"
            backend_root = Path(__file__).resolve().parents[1]

            def crash_web(request, dispatch_file):
                child = subprocess.run(
                    [sys.executable, "-c", crash_code, crash_at, json.dumps(paths), json.dumps(request), str(dispatch_file)],
                    cwd=backend_root, env={**os.environ, "PYTHONPATH": str(backend_root)},
                    capture_output=True, text=True, timeout=20,
                )
                assert child.returncode == 57, child.stderr

            crash_web(job_request, wire_path)
            job = app.state.ctx.db.fetchone("SELECT job_id, status FROM exec_jobs WHERE batch_name = ?", (batch_name,))
            assert job is not None
            old_job_id = job["job_id"]
            published_manifest = isolated_settings.published_batches_dir / batch_name / "publish-manifest.json"
            manifest_before = published_manifest.read_bytes() if crash_at == "after_publish" else None

            restarted = create_app(isolated_settings, run_startup_scan=False)
            ctx = restarted.state.ctx
            if crash_at == "before_dispatch":
                recover = ctx.execution.ipc.recover_job
                unavailable, wrong_runner, allow_recovery = threading.Event(), threading.Event(), threading.Event()
                simulate_wrong_runner = threading.Event()

                def retrying_recovery(job_id):
                    if not allow_recovery.is_set():
                        if simulate_wrong_runner.is_set():
                            wrong_runner.set()
                            return 404, {"code": "JOB_DISPATCH_ABANDONED", "job_id": job_id,
                                         "runner_instance_id": "different-instance"}
                        unavailable.set()
                        raise OSError("Runner unavailable in fixture")
                    return recover(job_id)

                monkeypatch.setattr(ctx.execution.ipc, "recover_job", retrying_recovery)

            with TestClient(restarted) as client:
                csrf = login_client(client, "restart_admin", "RestartFixture!2026")
                if crash_at == "before_dispatch":
                    try:
                        assert unavailable.wait(5)
                        blocked = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=job_request)
                        assert blocked.status_code == 409
                        simulate_wrong_runner.set()
                        assert wrong_runner.wait(5)
                        blocked = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=job_request)
                        assert blocked.status_code == 409
                    finally:
                        allow_recovery.set()
                if crash_at in ("before_dispatch_intent", "before_dispatch"):
                    detail = _wait_job_terminal(client, old_job_id)
                    assert detail["status"] == "interrupted", detail
                if crash_at == "before_dispatch":
                    delayed = ctx.execution.ipc.submit_job(json.loads(wire_path.read_text()))
                    assert delayed == (409, {"code": "JOB_DISPATCH_ABANDONED", "job_id": old_job_id})
                if crash_at in ("before_dispatch_intent", "before_dispatch"):
                    retry = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=job_request)
                    assert retry.status_code == 202, retry.text
                    new_job_id = retry.json()["job_id"]
                    assert new_job_id != old_job_id
                    detail = _wait_job_terminal(client, new_job_id)
                else:
                    detail = _wait_job_terminal(client, old_job_id)

                assert detail["status"] == "finished", detail
                events = detail["events"]
                assert [event["seq_id"] for event in events] == list(range(1, len(events) + 1))
                event_types = [event["event_type"] for event in events]
                assert event_types.count("job_started") == event_types.count("reconciled") == 1
                snapshot = detail["shared_snapshot"]
                assert event_types.count("trial_finished") == snapshot["totals"]["recorded"]
                if manifest_before is not None:
                    assert published_manifest.read_bytes() == manifest_before
                    assert snapshot["snapshot_digest"] == f"sha256:{hashlib.sha256(manifest_before).hexdigest()}"
                row = ctx.db.fetchone("SELECT project_id, owner_id, generation_id FROM batches WHERE batch_id = ?",
                                      (snapshot["batch_id"],))
                assert row == {"project_id": "team_restart", "owner_id": owner,
                               "generation_id": snapshot["generation_id"]}
                assert not ctx.db.fetchall("SELECT job_id FROM exec_batch_reservations")

            if crash_at == "before_dispatch":
                # Leave a second reservation unresolved when the original Runner is replaced.
                unresolved_request = {**job_request, "batch_name": "restart-unresolved-publication"}
                unresolved_wire = tmp_path / "unresolved-submission.json"
                crash_web(unresolved_request, unresolved_wire)

        if crash_at == "before_dispatch":
            with _start_external_runner_daemon(isolated_settings):
                replacement = create_app(isolated_settings, run_startup_scan=False)
                ctx = replacement.state.ctx
                old_payload = json.loads(wire_path.read_text())
                unresolved_payload = json.loads(unresolved_wire.read_text())
                runner = ctx.execution.ipc
                assert runner.probe_health()["runner_instance_id"] != old_payload["runner_instance_id"]
                for payload in (old_payload, unresolved_payload):
                    assert runner.submit_job(payload) == (409, {
                        "code": "RUNNER_INSTANCE_MISMATCH", "job_id": payload["job_id"],
                    })
                    assert runner.get_events(payload["job_id"])[0] == 404

                recovered = threading.Event()
                recover = runner.recover_job

                def observe_recovery(job_id):
                    response = recover(job_id)
                    recovered.set()
                    return response

                monkeypatch.setattr(runner, "recover_job", observe_recovery)
                with TestClient(replacement) as client:
                    csrf = login_client(client, "restart_admin", "RestartFixture!2026")
                    assert recovered.wait(5)
                    reply = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=unresolved_request)
                    assert reply.status_code == 409
                    assert ctx.db.fetchone("SELECT job_id FROM exec_batch_reservations") == {
                        "job_id": unresolved_payload["job_id"],
                    }
                    valid_request = {**job_request, "batch_name": "restart-replacement-valid"}
                    # The Web health response can predate the restart. The new Runner's
                    # explicit refusal of this POST releases just this new reservation.
                    probe_health = ctx.execution.probe_runner_health
                    stale_health = {**probe_health(), "runner_instance_id": old_payload["runner_instance_id"]}
                    monkeypatch.setattr(ctx.execution, "probe_runner_health", lambda: stale_health)
                    rejected = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=valid_request)
                    assert rejected.status_code == 409, rejected.text
                    error = rejected.json()["error"]
                    assert error["code"] == "RUNNER_INSTANCE_MISMATCH"
                    failed_job_id = error["detail"]["job_id"]
                    failed = _wait_job_terminal(client, failed_job_id)
                    assert failed["status"] == "failed" and failed["events"][-1]["event_type"] == "job_failed"
                    assert runner.get_events(failed_job_id)[0] == 404
                    assert ctx.db.fetchone("SELECT job_id FROM exec_batch_reservations") == {
                        "job_id": unresolved_payload["job_id"],
                    }
                    monkeypatch.setattr(ctx.execution, "probe_runner_health", probe_health)
                    submit_to_runner = runner.submit_job

                    def accepted_without_reply(payload):
                        assert submit_to_runner(payload)[0] == 202
                        raise OSError("Runner accepted but the Web did not receive its reply")

                    monkeypatch.setattr(runner, "submit_job", accepted_without_reply)
                    reply = client.post("/api/v1/execution/jobs", headers={"X-CSRF-Token": csrf}, json=valid_request)
                    assert reply.status_code == 202, reply.text
                    new_job_id = reply.json()["job_id"]
                    assert new_job_id != failed_job_id
                    assert _wait_job_terminal(client, new_job_id)["status"] == "finished"


def test_m3_e02_training_opd_empty_prod_state_contract_fixture_and_holdout_guardrail(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 E-02 Training & OPD Monitor:
    1) Real 197 `/home/share-data/tianxi_benches/training-runs/` empty production state -> 0 fake runs + CLI plan.
    2) Isolated fixture `tests/fixtures/opd_sample/` -> routing topology, step dual-axis series, strict holdout guardrail."""
    app_real = create_app(isolated_settings, run_startup_scan=False)
    ctx_real = app_real.state.ctx
    create_user(
        ctx_real.db,
        username="opd_real_res",
        password="StrongOpdRes!2026",
        role="researcher",
        projects=["default"],
        scopes=list(ALL_SCOPES),
    )
    c_real = TestClient(app_real)
    csrf_real = login_client(c_real, "opd_real_res", "StrongOpdRes!2026")

    r_runs = c_real.get("/api/v1/training/runs")
    assert r_runs.status_code == 200, r_runs.text
    runs_body = r_runs.json()
    assert runs_body["is_empty_production_state"] is True
    assert runs_body["items"] == []
    assert runs_body["total"] == 0

    r_plan = c_real.post(
        "/api/v1/training/plan",
        headers={"X-CSRF-Token": csrf_real},
        json={"suite_id": "tianxi-synth-v1-train", "student_model": "Qwen3_8-27B", "steps": 20},
    )
    assert r_plan.status_code == 200, r_plan.text
    assert "train.py --backend opd --plan" in r_plan.json()["plan_command"]
    ctx_real.db.close_thread_connection()

    tx_opd = tmp_path / "tx_opd_isolated"
    tr_dir = tx_opd / "training-runs"
    tr_dir.mkdir(parents=True, exist_ok=True)
    _copy_harbor_toml(tx_opd)

    opd_runtime = tmp_path / "opd_runtime"
    opd_state = opd_runtime / "state"
    opd_settings = Settings(
        eval_root=isolated_settings.eval_root,
        tianxi_root=tx_opd,
        dataset_root=isolated_settings.dataset_root,
        config_dir=isolated_settings.config_dir,
        runtime_dir=opd_runtime,
        state_dir=opd_state,
        staging_dir=opd_runtime / "uploads-staging",
        evidence_store_dir=opd_runtime / "evidence-store",
        runner_work_dir=opd_runtime / "runner-work",
        db_path=opd_state / "eval_platform.db",
        secrets_file=opd_state / "secrets.env",
        gateways_env_file=opd_state / "gateways.env",
        cookie_secure=False,
        enable_background_indexer=False,
    )
    app_opd = create_app(opd_settings, run_startup_scan=False)
    ctx_opd = app_opd.state.ctx
    create_user(ctx_opd.db, username="opd_adm", password="StrongOpdAdm!2026", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx_opd.db, username="opd_no_ho", password="StrongOpdNoHo!2026", role="researcher", projects=["default"], scopes=["trace:read"])

    c_adm = TestClient(app_opd)
    login_client(c_adm, "opd_adm", "StrongOpdAdm!2026")
    c_noho = TestClient(app_opd)
    login_client(c_noho, "opd_no_ho", "StrongOpdNoHo!2026")

    shutil.copytree(OPD_FIXTURE_DIR, tr_dir / "opd-sample-run")
    r_disc = c_adm.get("/api/v1/training/runs")
    assert r_disc.status_code == 200
    assert r_disc.json()["total"] == 1

    det = c_adm.get("/api/v1/training/runs/opd-sample-run").json()
    assert det["steps_completed"] == 5
    assert [s["step"] for s in det["step_series"]] == [1, 2, 3, 4, 5]
    assert det["holdout_comparison"]["status"] == "unavailable"
    assert c_noho.get("/api/v1/training/runs/opd-sample-run").json()["holdout_comparison"]["status"] == "redacted_missing_scope"

    now = utcnow_iso()
    with ctx_opd.db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, goal_type, capability, status, updated_at) VALUES ('opd_holdout_bm', 'OPD Holdout Suite', 'synthetic', 'Synthetic', 'synthesis_loop', 'coding', 'integrated', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dataset_releases (dataset_release_id, benchmark_slug, dataset_version, split, runner, known_task_count, metric_name, default_protocol, updated_at) VALUES ('opd-synth-v1__v1__holdout', 'opd_holdout_bm', 'v1', 'holdout', 'harbor', 12, 'reward', 'lrm-eval-report-v1', ?)",
            (now,),
        )
        conn.execute(
            """
            INSERT INTO batches (
                batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                benchmark_slug, dataset_release_id, raw_dataset_name, model_canonical, checkpoint,
                harness, runner, run_scope, scoring_protocol, official_status, generation_id,
                unique_tasks, planned, recorded, scored, valid_mean, content_digest, indexed_at
            ) VALUES
                ('b_opd_ho_base', 'harbor_runs:opd-ho-base', 'harbor_runs', 'opd-ho-base', 'default', 'system',
                 'opd_holdout_bm', 'opd-synth-v1__v1__holdout', 'tianxi-synth-v1-holdout', 'Qwen3_8-27B', 'checkpoint-0000',
                 'opencode', 'harbor', 'full_split', 'lrm-eval-report-v1', 'unofficial_valid_mean', 'gen_ho_0',
                 12, 12, 12, 12, 0.45, 'dig_ho_0', ?),
                ('b_opd_ho_step5', 'harbor_runs:opd-ho-step5', 'harbor_runs', 'opd-ho-step5', 'default', 'system',
                 'opd_holdout_bm', 'opd-synth-v1__v1__holdout', 'tianxi-synth-v1-holdout', 'Qwen3_8-27B', 'checkpoint-0005',
                 'opencode', 'harbor', 'full_split', 'lrm-eval-report-v1', 'unofficial_valid_mean', 'gen_ho_5',
                 12, 12, 12, 12, 0.65, 'dig_ho_5', ?)
            """,
            (now, now),
        )
    hc = c_adm.get("/api/v1/training/runs/opd-sample-run").json()["holdout_comparison"]
    assert hc["status"] == "available"
    assert hc["items"][0]["delta_pp"] == 20.0
    ctx_opd.db.close_thread_connection()


def test_m3_s05_stress_5000_episodes_latency_rotating_logs_backup_and_750mb_budget(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 S-05:
    - 5,120 episodes across 20 batches -> P95 < 250ms and SQLite DB+WAL+SHM < 30 MB.
    - Bounded RotatingFileHandler strictly enforces hard byte ceiling under log flood (<= 150 MiB total design).
    - Single compressed `.db.zst` online backup retains strictly 1 latest 0600 backup file.
    - `/api/v1/admin/health` honestly reports `isolated_runner_ready=False` when unattached and `docker_group_attached=False`."""
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    create_user(ctx.db, username="stress_adm", password="StrongStressAdm!26", role="admin", projects=["default"], scopes=list(ALL_SCOPES))

    now = utcnow_iso()
    num_batches = 20
    eps_per_batch = 256
    batch_rows, episode_rows, tps_rows = [], [], []
    for bi in range(num_batches):
        bid = f"b_stress_{bi:02d}"
        bname = f"stress-batch-{bi:02d}"
        gen_id = f"gen_stress_{bi:02d}"
        scored_cnt = 220
        failed_cnt = eps_per_batch - scored_cnt
        passed_cnt = 140 + (bi % 25)
        batch_rows.append(
            (
                bid, f"harbor_runs:{bname}", "harbor_runs", bname, "default", "system", "approved_restricted",
                "stress_bm", "stress_bm__v1__default", "stress-bench-v1", f"Qwen3_8-27B-v{bi % 4}", f"Qwen3_8-27B-v{bi % 4}",
                "opencode" if bi % 2 == 0 else "pi", "1.19.3", "harbor", "full_split", "lrm-eval-report-v1",
                "unofficial_valid_mean", "finished", gen_id, eps_per_batch, eps_per_batch, eps_per_batch, scored_cnt, 0,
                1.0, round(passed_cnt / scored_cnt, 6), passed_cnt, 1.0,
                json.dumps({"scored": scored_cnt, "failed": failed_cnt}),
                json.dumps({"agent_timeout": failed_cnt // 2, "agent_protocol": failed_cnt - (failed_cnt // 2)}),
                f"digest_{bi:02d}", now, now,
            )
        )
        for ei in range(eps_per_batch):
            tid = f"tr_{bi:02d}_{ei:04d}"
            task_id = f"stress-task-{ei:04d}"
            ckey = f"stress_uuid_{bi:02d}_{ei:04d}"
            if ei < scored_cnt:
                status, reward, rvalid, fkind, ex_type, fsum = "scored", (1.0 if ei < passed_cnt else 0.0), 1, None, None, None
            else:
                fkind = "agent_timeout" if (ei % 2 == 0) else "agent_protocol"
                status, reward, rvalid, ex_type, fsum = "failed", None, 0, ("TimeoutError" if fkind == "agent_timeout" else "ProtocolError"), f"Stress {fkind}"
            episode_rows.append((bid, tid, ckey, gen_id, task_id, 1, status, reward, rvalid, ex_type, fkind, fsum, 1200 + ei, 320, 12.5, now, now))
            tps_rows.append((ckey, "lrm-eval-report-v1", bid, tid, "default", "harbor_runs", 10, "stress_bm__v1__default", task_id, status, reward, rvalid, ex_type))

    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, goal_type, capability, status, updated_at) VALUES ('stress_bm', 'Stress Bench 5K', 'agentic', 'Agentic', 'optimize_gain', 'coding', 'integrated', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dataset_releases (dataset_release_id, benchmark_slug, dataset_version, split, runner, known_task_count, metric_name, default_protocol, updated_at) VALUES ('stress_bm__v1__default', 'stress_bm', 'v1', 'default', 'harbor', 256, 'reward', 'lrm-eval-report-v1', ?)",
            (now,),
        )
        conn.executemany(
            """
            INSERT INTO batches (
                batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                evidence_approval, benchmark_slug, dataset_release_id, raw_dataset_name,
                model_route, model_canonical, harness, harness_version, runner, run_scope,
                scoring_protocol, official_status, batch_status, generation_id,
                unique_tasks, planned, recorded, scored, unscored, coverage, valid_mean,
                passed, pass_threshold, outcomes_json, failure_kinds_json,
                content_digest, finished_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch_rows,
        )
        conn.executemany(
            """
            INSERT INTO episodes (
                batch_id, trial_id, canonical_trial_key, generation_id, task_id, attempt_index,
                status, reward, reward_valid, exception_type, failure_kind, failure_summary,
                input_tokens, output_tokens, duration_sec, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            episode_rows,
        )
        conn.executemany(
            """
            INSERT INTO trial_protocol_scores (
                canonical_trial_key, scoring_protocol, batch_id, trial_id, project_id,
                source_namespace, source_priority_rank, dataset_release_id, task_id,
                status, reward, reward_valid, exception_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tps_rows,
        )

    client = TestClient(app)
    login_client(client, "stress_adm", "StrongStressAdm!26")
    last_bodies: dict[str, dict[str, Any]] = {}
    for ep in ("/api/v1/leaderboard", "/api/v1/batches", "/api/v1/diagnostics/failures", "/api/v1/admin/health"):
        # Warm up CIFS/SQLite caches once before measuring genuine 20-request steady-state P95
        w = client.get(ep)
        assert w.status_code == 200, w.text
        samples_ms = []
        for _ in range(20):
            t0 = time.perf_counter()
            r = client.get(ep)
            assert r.status_code == 200, r.text
            samples_ms.append((time.perf_counter() - t0) * 1000.0)
            last_bodies[ep] = r.json()
        assert _p95_ms(samples_ms) < 250.0, f"{ep} p95={_p95_ms(samples_ms):.2f}ms samples={samples_ms}"

    db_total_bytes = sum(
        p.stat().st_size
        for p in (
            isolated_settings.db_path,
            Path(f"{isolated_settings.db_path}-wal"),
            Path(f"{isolated_settings.db_path}-shm"),
        )
        if p.exists()
    )
    assert db_total_bytes < 30 * 1024 * 1024, f"5,120-episode SQLite DB+WAL+SHM exceeded 30 MiB: {db_total_bytes}"

    assert last_bodies["/api/v1/admin/health"]["runner_status"]["isolated_runner_ready"] is False
    assert last_bodies["/api/v1/admin/health"]["runner_status"]["docker_group_attached"] is False
    assert last_bodies["/api/v1/admin/health"]["storage"]["local_budget_mb"] == 750

    # Verify bounded RotatingFileHandler caps log growth strictly under flood (S-05)
    logs_dir = tmp_path / "rot_logs"
    cfg_uv = build_uvicorn_log_config(logs_dir, "backend")
    assert cfg_uv["handlers"]["rotating_file"]["maxBytes"] == 15 * 1024 * 1024
    assert cfg_uv["handlers"]["rotating_file"]["backupCount"] == 3  # 4 x 15 MiB = 60 MiB max
    logger, _ = configure_component_rotating_logger(logs_dir, "floodtest", max_bytes=4096, backup_count=2)
    for i in range(400):
        logger.info("flood line %04d %s", i, "x" * 120)
    total_rot_bytes = sum(p.stat().st_size for p in logs_dir.glob("floodtest.log*"))
    assert total_rot_bytes <= 3 * 4096 + 1024, f"Rotating log exceeded hard cap: {total_rot_bytes} bytes"
    assert len(list(logs_dir.glob("floodtest.log*"))) <= 3

    # Verify single compressed .db.zst backup policy (strictly 1 retained, 0600)
    b1 = create_single_compressed_backup(isolated_settings)
    time.sleep(1.1)
    b2 = create_single_compressed_backup(isolated_settings)
    assert b1["size_bytes"] > 0 and b2["size_bytes"] > 0
    assert b2["retained_count"] == 1
    assert (Path(str(b2["backup_path"])).stat().st_mode & 0o777) == 0o600
    assert last_bodies["/api/v1/admin/health"]["storage"]["log_budgets"] == {
        "daemon_rotating_logs_max_mb": 150,
        "per_job_stdio_max_mb": 2.0,
        "cumulative_job_stdio_max_mb": 20.0,
        "combined_all_logs_max_mb": 170.0,
    }
    ctx.db.close_thread_connection()


def test_m3_rev3_s02_runner_published_ownership_preserved_on_reindex_poll_and_fresh_db_recovery(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 rev3 S-02 fix:
    1) A 2-task Direct job published under a non-default team project (`team_beta`) by `beta_res`
       keeps exact `(project_id='team_beta', owner_id=beta_res, evidence_approval='approved_restricted')`
       AND identical `generation_id` across:
       - `POST /api/v1/admin/reindex/{batch_id}` (`indexed`)
       - `POST /api/v1/admin/reindex?force=true`
       - normal polling (`run_hot_now(force=False)`)
       in `batches`, `trial_protocol_scores`, AND `search_fts`!
    2) Fresh-DB restart recovery (brand-new SQLite DB pointing to the same `0700 state_dir` +
       `published_batches_dir`) STILL restores `team_beta / beta_res / approved_restricted` from
       `0700 state/runner-submissions/<batch>.json` (0600).
    3) External `harbor_runs` sources STILL follow `source_approval_registry.yaml` approval updates."""

    def _gw_handler(_path: str, _headers: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prompt = str(body["messages"][0]["content"])
        ans = "4" if "2 + 2" in prompt else "READY"
        return 200, {
            "id": "chatcmpl-s02-own",
            "model": body.get("model", "Qwen3_8-27B"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ans}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        }

    with _MockChatGatewayServer(_gw_handler) as gw:
        tx_root = tmp_path / "tx_s02_own"
        hr_dir = tx_root / "harbor-runs"
        hr_dir.mkdir(parents=True, exist_ok=True)
        _copy_harbor_toml(tx_root)
        cfg_dir = tmp_path / "cfg_s02_own"
        _write_test_config(cfg_dir, approval_entries=[], gateway_base_url=gw.base_url)

        isolated_settings.tianxi_root = tx_root
        isolated_settings.config_dir = cfg_dir

        with _start_external_runner_daemon(isolated_settings):
            app = create_app(isolated_settings, run_startup_scan=False)
            ctx = app.state.ctx
            create_user(
                ctx.db,
                username="own_admin",
                password="StrongOwnAdm!2026",
                role="admin",
                projects=["default", "team_beta"],
                scopes=list(ALL_SCOPES),
            )
            beta_uid = create_user(
                ctx.db,
                username="beta_res",
                password="StrongBetaRes!26",
                role="researcher",
                projects=["team_beta"],
                scopes=list(ALL_SCOPES),
            )
            create_user(
                ctx.db,
                username="def_viewer",
                password="StrongDefView!26",
                role="viewer",
                projects=["default"],
                scopes=list(ALL_SCOPES),
            )

            c_adm = TestClient(app)
            csrf_adm = login_client(c_adm, "own_admin", "StrongOwnAdm!2026")
            c_beta = TestClient(app)
            csrf_beta = login_client(c_beta, "beta_res", "StrongBetaRes!26")
            c_def = TestClient(app)
            login_client(c_def, "def_viewer", "StrongDefView!26")

            r_sub = c_beta.post(
                "/api/v1/execution/jobs",
                headers={"X-CSRF-Token": csrf_beta},
                json={
                    "kind": "direct",
                    "suite_id": "direct-smoke-v1",
                    "selection_mode": "all",
                    "batch_name": "m3-team-beta-ownership",
                    "project_id": "team_beta",
                },
            )
            assert r_sub.status_code == 202, r_sub.text
            jid = r_sub.json()["job_id"]
            det = _wait_job_terminal(c_beta, jid)
            assert det["status"] == "finished", det
            bid = det["shared_snapshot"]["batch_id"]
            orig_gen = det["shared_snapshot"]["generation_id"]
            assert bid and orig_gen

            def _assert_ownership(db_conn: Any, label: str) -> None:
                brow = db_conn.fetchone(
                    "SELECT project_id, owner_id, evidence_approval, generation_id FROM batches WHERE batch_id = ?",
                    (bid,),
                )
                assert brow is not None, f"{label}: batch missing"
                assert brow["project_id"] == "team_beta", f"{label}: project_id drifted to {brow['project_id']}"
                assert brow["owner_id"] == beta_uid, f"{label}: owner_id drifted to {brow['owner_id']}"
                assert brow["evidence_approval"] == "approved_restricted", f"{label}: approval drifted to {brow['evidence_approval']}"
                assert brow["generation_id"] == orig_gen, f"{label}: generation_id changed"

                tps_projs = {
                    r["project_id"]
                    for r in db_conn.fetchall("SELECT DISTINCT project_id FROM trial_protocol_scores WHERE batch_id = ?", (bid,))
                }
                assert tps_projs == {"team_beta"}, f"{label}: trial_protocol_scores drifted to {tps_projs}"

                fts_projs = {
                    r["project_id"]
                    for r in db_conn.fetchall(
                        "SELECT DISTINCT project_id FROM search_fts WHERE entity_type = 'batch' AND entity_id = ?",
                        (bid,),
                    )
                }
                assert fts_projs == {"team_beta"}, f"{label}: search_fts drifted to {fts_projs}"

            _assert_ownership(ctx.db, "initial_reconcile")

            # 1a) Forced single-batch reindex via /api/v1/admin/reindex/{batch_id}
            r_rx1 = c_adm.post(f"/api/v1/admin/reindex/{bid}", headers={"X-CSRF-Token": csrf_adm})
            assert r_rx1.status_code == 200, r_rx1.text
            assert r_rx1.json()["status"] == "indexed"
            _assert_ownership(ctx.db, "forced_single_batch_reindex")

            # 1b) Forced full reindex via /api/v1/admin/reindex?force=true
            r_rx2 = c_adm.post("/api/v1/admin/reindex?force=true", headers={"X-CSRF-Token": csrf_adm})
            assert r_rx2.status_code == 200, r_rx2.text
            _assert_ownership(ctx.db, "forced_full_reindex")

            # 1c) Regular background polling (force=False)
            ctx.coordinator.run_hot_now(force=False)
            _assert_ownership(ctx.db, "normal_polling")

            # Cross-project authorization stays enforced after reindex:
            assert c_beta.get(f"/api/v1/batches/{bid}").status_code == 200
            assert c_def.get(f"/api/v1/batches/{bid}").status_code == 403

            # Verify 0700 state/runner-submissions/<batch>.json (0600) exists
            meta_file = isolated_settings.state_dir / "runner-submissions" / "m3-team-beta-ownership.json"  # type: ignore[operator]
            assert meta_file.is_file()
            assert (meta_file.stat().st_mode & 0o777) == 0o600
            ctx.db.close_thread_connection()

        # 2) Fresh-DB restart recovery: brand-new SQLite DB (empty exec_jobs) pointing at SAME state_dir + published-dto!
        fresh_db = isolated_settings.state_dir / "recovered_eval_platform.db"  # type: ignore[operator]
        rec_settings = Settings(
            runtime_dir=isolated_settings.runtime_dir,
            db_path=fresh_db,
            tianxi_root=tx_root,
            config_dir=cfg_dir,
            production_mode=False,
            cookie_secure=False,
        )
        app_rec = create_app(rec_settings, run_startup_scan=False)
        ctx_rec = app_rec.state.ctx
        ctx_rec.coordinator.run_hot_now(force=True)
        _assert_ownership(ctx_rec.db, "fresh_db_restart_recovery")

        published_root = isolated_settings.published_batches_dir
        assert published_root is not None
        source_dir = published_root / "m3-team-beta-ownership"
        with ctx_rec.db.transaction() as conn:
            conn.execute(
                "UPDATE batches SET project_id = 'default', owner_id = 'system', evidence_approval = 'pending' WHERE batch_id = ?",
                (bid,),
            )
        assert ctx_rec.coordinator.run_hot_now(force=False)["batches"]["indexed"] == 1
        _assert_ownership(ctx_rec.db, "poll_repaired_default_ownership")

        held_meta = meta_file.with_suffix(".held")
        meta_file.rename(held_meta)
        try:
            # Reject even a cache hit from a prior scan, and keep the DTO available for recovery.
            assert ctx_rec.coordinator.run_hot_now(force=False)["batches"]["rejected"] == 1
            assert ctx_rec.db.fetchone("SELECT 1 FROM batches WHERE batch_id = ?", (bid,)) is None
            assert ctx_rec.db.fetchone("SELECT 1 FROM trial_protocol_scores WHERE batch_id = ?", (bid,)) is None
            assert ctx_rec.db.fetchone("SELECT 1 FROM search_fts WHERE entity_id = ?", (bid,)) is None
            assert (source_dir / "publish-manifest.json").is_file()
            assert ctx_rec.coordinator.hot.scan_single_harbor_batch(
                source_dir, force=True, source_namespace="runner_published"
            ) == "rejected"
            stale_ledger = json.loads(held_meta.read_bytes())
            stale_ledger["generation_id"] = "gen_stale"
            meta_file.write_text(json.dumps(stale_ledger), encoding="utf-8")
            meta_file.chmod(0o600)
            assert ctx_rec.coordinator.run_hot_now(force=False)["batches"]["rejected"] == 1
            assert ctx_rec.db.fetchone("SELECT 1 FROM batches WHERE batch_id = ?", (bid,)) is None
        finally:
            held_meta.replace(meta_file)

        assert ctx_rec.coordinator.run_hot_now(force=False)["batches"]["indexed"] == 1
        _assert_ownership(ctx_rec.db, "original_provenance_restored")

        # 3) External `harbor_runs` batch STILL obeys `source_approval_registry.yaml` approval updates!
        ext_batch = hr_dir / "ext-harbor-appr-check"
        shutil.copytree(isolated_settings.published_batches_dir / "m3-team-beta-ownership", ext_batch)  # type: ignore[operator]
        # Replace manifest/batch name references only? No, keep identical copy under harbor-runs/<name>
        ext_bid = make_batch_id("harbor_runs:ext-harbor-appr-check")
        assert ctx_rec.coordinator.hot.scan_single_harbor_batch(ext_batch, force=True) == "indexed"
        ext_r1 = ctx_rec.db.fetchone("SELECT project_id, evidence_approval FROM batches WHERE batch_id = ?", (ext_bid,))
        assert ext_r1 is not None and ext_r1["project_id"] == "default" and ext_r1["evidence_approval"] == "pending"

        # Update source_approval_registry.yaml to approve & assign external batch to proj_ext
        _write_test_config(
            cfg_dir,
            approval_entries=[
                {
                    "source_root": str(ext_batch),
                    "match_mode": "prefix",
                    "project_id": "proj_ext",
                    "owner_id": "ext_owner",
                    "approval_status": "approved_restricted",
                    "approved_evidence_classes": ["summary_dto", "direct_vlm_qa"],
                }
            ],
            gateway_base_url=gw.base_url,
        )
        ctx_rec.coordinator.hot.approval_registry.reload()
        assert ctx_rec.coordinator.hot.scan_single_harbor_batch(ext_batch, force=True) == "indexed"
        ext_r2 = ctx_rec.db.fetchone(
            "SELECT project_id, owner_id, evidence_approval FROM batches WHERE batch_id = ?",
            (ext_bid,),
        )
        assert ext_r2 is not None
        assert ext_r2["project_id"] == "proj_ext"
        assert ext_r2["owner_id"] == "ext_owner"
        assert ext_r2["evidence_approval"] == "approved_restricted"
        ctx_rec.db.close_thread_connection()


def test_m3_rev3_s05_per_job_stdio_cap_cumulative_retention_and_bounded_detail_read(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M3 rev3 S-05 per-job stdio cap, cumulative retention pruning, and bounded detail tail read:
    1) `BoundedJobStdioWriter` caps a 5x flood strictly to `<= 2 * max_segment_bytes` with `0600` perms.
    2) `enforce_job_stdio_retention_cap` spares active jobs and prunes oldest finished job stdio logs
       strictly below `max_total_bytes`.
    3) `GET /api/v1/execution/jobs/{job_id}` reads strictly a bounded 128 KiB tail (`stdio_log_truncated=True`,
       never loading the whole file into memory)."""
    # 1) Per-job rotating writer cap
    job_dir = tmp_path / "rw_stdio_test" / "job_flood_01"
    job_dir.mkdir(parents=True, exist_ok=True)
    log_p = job_dir / "runner_stdio.log"
    writer = BoundedJobStdioWriter(log_p, max_segment_bytes=4096, backup_count=1)
    for idx in range(500):
        writer.write(f"line-{idx:04d} {'Z' * 100}\n".encode("utf-8"))
    writer.close()
    files = sorted(job_dir.glob("runner_stdio.log*"))
    assert len(files) == 2
    total_sz = sum(f.stat().st_size for f in files)
    assert total_sz <= 2 * 4096, f"Per-job stdio exceeded 2-segment hard cap: {total_sz}"
    for f in files:
        assert (f.stat().st_mode & 0o777) == 0o600

    # 2) Cumulative retention pruning across runner-work/
    rw_root = tmp_path / "rw_ret_test"
    for i in range(10):
        jd = rw_root / f"job_old_{i:02d}"
        jd.mkdir(parents=True, exist_ok=True)
        lp = jd / "runner_stdio.log"
        lp.write_bytes(b"A" * 4096)
        os.utime(lp, (1700000000 + i * 10, 1700000000 + i * 10))
    active_jd = rw_root / "job_active_99"
    active_jd.mkdir(parents=True, exist_ok=True)
    active_lp = active_jd / "runner_stdio.log"
    active_lp.write_bytes(b"B" * 4096)
    res_prune = enforce_job_stdio_retention_cap(rw_root, max_total_bytes=16384, active_job_ids={"job_active_99"})
    assert res_prune["total_bytes_after"] <= 16384
    assert res_prune["pruned_files"] >= 7
    assert active_lp.is_file(), "Active running job stdio log MUST NOT be pruned!"

    # 3) Bounded tail read in GET /api/v1/execution/jobs/{job_id} (never reads entire large file!)
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    adm_uid = create_user(
        ctx.db,
        username="stdio_adm",
        password="StrongStdioAdm!26",
        role="admin",
        projects=["default"],
        scopes=list(ALL_SCOPES),
    )
    jid = "job_big_stdio_tail_01"
    j_work = isolated_settings.runner_work_dir / jid  # type: ignore[operator]
    j_work.mkdir(parents=True, exist_ok=True)
    os.chmod(j_work, 0o700)
    big_log = j_work / "runner_stdio.log"
    # Write ~512 KiB (> 128 KiB detail read cap) with unique sentinel strictly at the tail!
    head_block = ("HEAD_LINE_" + ("X" * 90) + "\n") * 5000
    tail_block = "\n".join(f"TAIL_SENTINEL_{i:03d}" for i in range(50)) + "\n"
    big_log.write_text(head_block + tail_block, encoding="utf-8")
    os.chmod(big_log, 0o600)
    assert big_log.stat().st_size > MAX_JOB_DETAIL_STDIO_READ_BYTES * 3

    now = utcnow_iso()
    with ctx.db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO exec_jobs (
                job_id, kind, project_id, owner_id, batch_name, batch_id, status,
                params_json, command_preview, runner_id, exit_code, last_event_seq,
                log_ref, notify_json, created_at, finished_at
            ) VALUES (?, 'direct', 'default', ?, 'stdio-tail-batch', NULL, 'finished', '{}', 'cmd', 'r1', 0, 0, ?, '{}', ?, ?)
            """,
            (jid, adm_uid, f"runner_work:{jid}/runner_stdio.log", now, now),
        )

    c = TestClient(app)
    login_client(c, "stdio_adm", "StrongStdioAdm!26")
    r_det = c.get(f"/api/v1/execution/jobs/{jid}")
    assert r_det.status_code == 200, r_det.text
    det = r_det.json()
    assert det["stdio_log_truncated"] is True
    assert det["stdio_log_cap_bytes"] == 2 * 1024 * 1024
    assert len(det["redacted_stdio_logs"]) <= 200
    assert det["redacted_stdio_logs"][-1] == "TAIL_SENTINEL_049"
    assert not any("HEAD_LINE_" in ln for ln in det["redacted_stdio_logs"][-50:])
    ctx.db.close_thread_connection()
