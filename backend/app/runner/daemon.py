#!/usr/bin/env python3
"""External Isolated Execution Runner Daemon (v2.2.1 · M3 S-04 / S-06 / I-05).

Runs as a COMPLETELY SEPARATE OS process from the FastAPI Web server (NEVER in-process fake workers!).
Exposes an authenticated HTTP-over-Unix-Socket (0600) or loopback HTTP IPC interface (`X-Runner-Token`)
for:
  - `GET  /health`                      : live probe of runner PID/UID, 0700 dirs, managed framework SHA-256,
                                          real Docker reachability, real VLMEvalKit runtime, and Direct readiness.
  - `POST /v1/jobs`                     : dispatches REAL managed `direct.py` / `run.py` / `vlmevalkit.py` workers
                                          inside `0700` `--work` & `0700` `evidence_store` with scrubbed env,
                                          stdout/stderr strictly redirected to `0600 runner_stdio.log` (NEVER parsed!),
                                          and HMAC-signed trusted events written to `0600 .trusted-events.ndjson`.
  - `GET  /v1/jobs/<id>/events?after_seq=N`: returns authentic monotonic runner events (`job_started`,
                                          `trial_finished`, `paused_by_guard`, `job_interrupted`, `job_failed`,
                                          `runner_completed`).
  - `POST /v1/jobs/<id>/cancel`         : sends graceful SIGTERM to the active child worker.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import json
import os
import pwd
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure backend root is importable when run via `python3 -m app.runner.daemon` or direct script
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.api.errors import AppError
from app.config import Settings
from app.logging_setup import (
    MAX_JOB_STDIO_BACKUP_COUNT,
    MAX_JOB_STDIO_SEGMENT_BYTES,
    MAX_TOTAL_JOB_STDIO_BYTES,
    BoundedJobStdioWriter,
    configure_component_rotating_logger,
    enforce_job_stdio_retention_cap,
)
from app.runner.cli_contract import (
    build_managed_run_py_command,
    materialize_managed_run_config,
)
from app.security.gateways import GatewayProfileRegistry
from app.security.managed_exec import (
    build_sanitized_subprocess_env,
    resolve_verified_managed_script,
    verify_managed_framework_bundle,
)
from app.security.redaction import redact_text
from app.security.storage_probe import probe_isolated_dir, probe_secret_file, validate_safe_id


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sign_event(channel_secret: str, seq_id: int, event_type: str, payload: dict[str, Any]) -> str:
    body = json.dumps({"seq_id": seq_id, "event_type": event_type, "payload": payload}, sort_keys=True, ensure_ascii=False)
    return hmac.new(channel_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _probe_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_vlmevalkit_ready() -> bool:
    root = os.environ.get("VLMEVALKIT_ROOT", "").strip()
    py = os.environ.get("VLMEVALKIT_PYTHON", "").strip()
    return bool(root and Path(root).is_dir() and py and Path(py).is_file() and os.access(py, os.X_OK))


def _read_cgroup_telemetry() -> dict[str, Any]:
    mem_peak: int | None = None
    cpu_usec: int | None = None
    cgroup_path = Path("/proc/self/cgroup")
    try:
        if cgroup_path.is_file():
            rel = ""
            for line in cgroup_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("0::"):
                    rel = line.split("::", 1)[1].strip().lstrip("/")
                    break
            base = Path("/sys/fs/cgroup") / rel if rel else Path("/sys/fs/cgroup")
            mp = base / "memory.peak"
            if mp.is_file():
                raw = mp.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    mem_peak = int(raw)
            cs = base / "cpu.stat"
            if cs.is_file():
                for cl in cs.read_text(encoding="utf-8").splitlines():
                    if cl.startswith("usage_usec "):
                        val = cl.split()[1]
                        if val.isdigit():
                            cpu_usec = int(val)
                        break
    except OSError:
        pass
    if mem_peak is None and cpu_usec is None:
        return {
            "source": "unavailable",
            "cpu_usage_usec": None,
            "memory_peak_bytes": None,
            "note": "当前隔离 Runner 进程未运行在独立容器 cgroup v2 作用域，不伪造容器遥测",
        }
    return {
        "source": "cgroup_v2",
        "cpu_usage_usec": cpu_usec,
        "memory_peak_bytes": mem_peak,
    }


class RunnerJobState:
    def __init__(self, job_id: str, channel_path: Path, channel_secret: str) -> None:
        self.job_id = job_id
        self.channel_path = channel_path
        self.channel_secret = channel_secret
        self.status = "queued"
        self.exit_code: int | None = None
        self.events: list[dict[str, Any]] = []
        self.proc: subprocess.Popen[bytes] | None = None
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self._seq = 0

    def emit(self, event_type: str, payload: dict[str, Any], *, new_status: str | None = None, exit_code: int | None = None) -> dict[str, Any]:
        clean = json.loads(redact_text(json.dumps(payload, ensure_ascii=False)) or "{}")
        now = _utcnow()
        with self.lock:
            self._seq += 1
            seq = self._seq
            clean["runner_seq"] = seq
            clean["job_id"] = self.job_id
            clean.setdefault("ts", now)
            if new_status is not None:
                self.status = new_status
            if exit_code is not None:
                self.exit_code = exit_code
            sig = _sign_event(self.channel_secret, seq, event_type, clean)
            rec = {
                "runner_seq": seq,
                "event_type": event_type,
                "payload": clean,
                "hmac_sha256": sig,
                "created_at": now,
            }
            self.events.append(rec)
            with self.channel_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            try:
                os.chmod(self.channel_path, 0o600)
            except OSError:
                pass
            return rec


class IsolatedRunnerManager:
    def __init__(self, settings: Settings, logger: Any) -> None:
        self.settings = settings
        self.logger = logger
        self.gateway_profiles = GatewayProfileRegistry(settings)
        self.runner_id = os.environ.get("LRM_RUNNER_ID", f"isolated-runner-{os.getpid()}")
        self._jobs: dict[str, RunnerJobState] = {}
        self._lock = threading.Lock()
        # Bounded per-job (<= 2 MiB) and cumulative (<= 20 MiB) stdio log caps (S-05);
        # non-prod tests may tighten these downward to verify rotation & pruning rapidly.
        seg_bytes = MAX_JOB_STDIO_SEGMENT_BYTES
        tot_bytes = MAX_TOTAL_JOB_STDIO_BYTES
        if not settings.production_mode:
            raw_seg = os.environ.get("LRM_JOB_STDIO_SEGMENT_BYTES", "").strip()
            if raw_seg.isdigit():
                seg_bytes = max(1024, min(MAX_JOB_STDIO_SEGMENT_BYTES, int(raw_seg)))
            raw_tot = os.environ.get("LRM_JOB_STDIO_MAX_TOTAL_BYTES", "").strip()
            if raw_tot.isdigit():
                tot_bytes = max(4096, min(MAX_TOTAL_JOB_STDIO_BYTES, int(raw_tot)))
        self._job_stdio_segment_bytes = seg_bytes
        self._job_stdio_max_total_bytes = tot_bytes

    def _active_job_ids(self) -> set[str]:
        with self._lock:
            return {
                jid
                for jid, st in self._jobs.items()
                if st.status in ("queued", "running", "cancelling", "finalizing")
            }

    def _prune_job_stdio_logs(self) -> None:
        if self.settings.runner_work_dir is not None:
            enforce_job_stdio_retention_cap(
                self.settings.runner_work_dir,
                max_total_bytes=self._job_stdio_max_total_bytes,
                active_job_ids=self._active_job_ids(),
            )

    def health(self) -> dict[str, Any]:
        try:
            work_probe = probe_isolated_dir(self.settings.runner_work_dir, create=True)  # type: ignore[arg-type]
        except AppError as exc:
            work_probe = {"ok": False, "code": exc.code, "message": exc.message}
        try:
            ev_probe = probe_isolated_dir(self.settings.evidence_store_dir, create=True)  # type: ignore[arg-type]
        except AppError as exc:
            ev_probe = {"ok": False, "code": exc.code, "message": exc.message}
        try:
            mf_probe = verify_managed_framework_bundle(self.settings)
        except AppError as exc:
            mf_probe = {"ok": False, "code": exc.code, "message": exc.message}

        docker_ok = _probe_docker_available()
        vlm_ok = _probe_vlmevalkit_ready()
        direct_ok = bool(mf_probe.get("ok"))
        supported: list[str] = []
        if direct_ok:
            supported.append("direct")
        if docker_ok and mf_probe.get("ok"):
            supported.extend(["harbor", "harbor_gateway"])
        if vlm_ok and mf_probe.get("ok"):
            supported.append("vlmevalkit")

        try:
            runner_user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            runner_user = str(os.getuid())

        ok = bool(work_probe.get("ok") and ev_probe.get("ok") and mf_probe.get("ok"))
        return {
            "ok": ok,
            "runner_id": self.runner_id,
            "runner_pid": os.getpid(),
            "runner_uid": os.getuid(),
            "runner_user": runner_user,
            "docker_available": docker_ok,
            "vlmevalkit_ready": vlm_ok,
            "direct_ready": direct_ok,
            "supported_runners": supported,
            "cgroup_telemetry": _read_cgroup_telemetry(),
            "probes": {
                "runner_work_dir": work_probe,
                "evidence_store_dir": ev_probe,
                "managed_framework": mf_probe,
            },
        }

    def submit_job(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        job_id = str(req.get("job_id") or "").strip()
        validate_safe_id(job_id, "job_id")
        spec = req.get("spec")
        if not isinstance(spec, dict):
            return 422, {"code": "SCHEMA_VALIDATION_FAILED", "message": "Missing spec"}
        kind = str(spec.get("kind") or "").strip().lower()
        channel_secret = str(req.get("channel_secret") or "")
        if len(channel_secret) < 16:
            return 422, {"code": "SCHEMA_VALIDATION_FAILED", "message": "Missing channel_secret"}

        h = self.health()
        if not h["ok"]:
            return 503, {"code": "STAGING_PERMISSION_UNSAFE", "message": "隔离 Runner 存储或受管框架探针未通过", "health": h}

        if kind in ("harbor", "harbor_gateway") and not h["docker_available"]:
            return 503, {
                "code": "RUNNER_CAPABILITY_UNAVAILABLE",
                "message": "当前隔离 Runner 节点未连接 Docker 守护进程，拒绝执行 Harbor 容器作业（零模拟评分兜底）",
                "detail": {"kind": kind, "docker_available": False},
            }
        if kind == "vlmevalkit" and not h["vlmevalkit_ready"]:
            return 503, {
                "code": "RUNNER_CAPABILITY_UNAVAILABLE",
                "message": "当前隔离 Runner 节点未配置 VLMEVALKIT_ROOT / VLMEVALKIT_PYTHON，拒绝执行 VLMEvalKit 作业",
                "detail": {"kind": kind, "vlmevalkit_ready": False},
            }

        assert self.settings.runner_work_dir is not None and self.settings.evidence_store_dir is not None
        work_dir = self.settings.runner_work_dir / job_id
        evidence_dir = self.settings.evidence_store_dir / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(work_dir, 0o700)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(evidence_dir, 0o700)

        channel_path = work_dir / ".trusted-events.ndjson"
        channel_path.touch(exist_ok=True)
        os.chmod(channel_path, 0o600)
        log_path = work_dir / "runner_stdio.log"
        log_path.touch(exist_ok=True)
        os.chmod(log_path, 0o600)
        self._prune_job_stdio_logs()

        # Resolve gateway credentials strictly on Runner side (0600 gateways.env / Runner env)
        self.gateway_profiles.reload()
        gw_id = str(spec.get("gateway_profile_id") or "")
        gw_key = ""
        try:
            if kind == "direct":
                gw_pub, gw_key = self.gateway_profiles.resolve_runtime_credentials(gw_id)
            else:
                gw_pub = self.gateway_profiles.get_public_profile(gw_id)
                try:
                    _, gw_key = self.gateway_profiles.resolve_runtime_credentials(gw_id)
                except AppError:
                    gw_key = ""
        except AppError as exc:
            code = "GATEWAY_KEY_MISSING" if exc.status_code == 422 and kind == "direct" else exc.code
            return exc.status_code, {"code": code, "message": exc.message, "detail": exc.detail}

        state = RunnerJobState(job_id, channel_path, channel_secret)
        with self._lock:
            if job_id in self._jobs:
                return 409, {"code": "JOB_ALREADY_EXISTS", "message": f"Job already submitted: {job_id}"}
            self._jobs[job_id] = state

        t = threading.Thread(
            target=self._execute_job,
            args=(state, spec, work_dir, evidence_dir, log_path, gw_pub, gw_key or ""),
            name=f"runner-job-{job_id}",
            daemon=True,
        )
        t.start()
        self.logger.info("Accepted isolated job %s kind=%s suite=%s batch=%s", job_id, kind, spec.get("suite_id"), spec.get("batch_name"))
        return 202, {"ok": True, "job_id": job_id, "runner_id": self.runner_id, "runner_pid": os.getpid()}

    def _execute_job(
        self,
        state: RunnerJobState,
        spec: dict[str, Any],
        work_dir: Path,
        evidence_dir: Path,
        log_path: Path,
        gw_pub: dict[str, Any],
        gw_key: str,
    ) -> None:
        kind = str(spec["kind"])
        planned_tasks: list[str] = list(spec.get("planned_tasks") or [])
        attempts = int(spec.get("attempts") or 1)
        total_planned = len(planned_tasks) * attempts

        state.emit(
            "job_started",
            {
                "source": "runner_live",
                "runner_id": self.runner_id,
                "runner_pid": os.getpid(),
                "kind": kind,
                "cgroup_telemetry": _read_cgroup_telemetry(),
                "progress": {"planned": total_planned, "completed": 0, "scored": 0, "errors": 0},
            },
            new_status="running",
        )

        try:
            if kind == "direct":
                self._run_real_direct(state, spec, work_dir, evidence_dir, log_path, gw_pub, gw_key, total_planned)
            elif kind in ("harbor", "harbor_gateway"):
                self._run_real_framework_cli(state, spec, work_dir, evidence_dir, log_path, gw_pub, gw_key, total_planned, is_vlm=False)
            elif kind == "vlmevalkit":
                self._run_real_framework_cli(state, spec, work_dir, evidence_dir, log_path, gw_pub, gw_key, total_planned, is_vlm=True)
            else:
                raise RuntimeError(f"Unsupported runner kind: {kind}")
        except Exception as exc:
            self.logger.exception("Runner job %s failed", state.job_id)
            state.emit(
                "job_failed",
                {"source": "runner_live", "reason": redact_text(str(exc)) or "runner internal error", "exit_code": 1},
                new_status="failed",
                exit_code=1,
            )

    def _run_real_direct(
        self,
        state: RunnerJobState,
        spec: dict[str, Any],
        work_dir: Path,
        evidence_dir: Path,
        log_path: Path,
        gw_pub: dict[str, Any],
        gw_key: str,
        total_planned: int,
    ) -> None:
        direct_py = resolve_verified_managed_script(self.settings, "direct.py")
        dataset_path = str(spec.get("dataset_path") or "")
        if not dataset_path or not Path(dataset_path).is_file():
            raise RuntimeError(f"Direct suite cases.jsonl missing: {dataset_path}")

        worker_script = Path(__file__).resolve().parent / "direct_worker.py"
        cfg_file = work_dir / ".direct_worker_cfg.json"
        worker_cfg = {
            "job_id": state.job_id,
            "batch_name": spec["batch_name"],
            "direct_py": str(direct_py),
            "dataset_path": dataset_path,
            "work_dir": str(work_dir),
            "evidence_dir": str(evidence_dir),
            "model_route": spec["model_route"],
            "gateway_base_url": gw_pub["base_url"],
            "tasks": list(spec.get("planned_tasks") or []),
            "attempts": int(spec.get("attempts") or 1),
            "timeout_sec": float(spec.get("timeout_sec") or 60.0),
            "scorer_path": spec.get("scorer_path"),
        }
        cfg_file.write_text(json.dumps(worker_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(cfg_file, 0o600)

        read_fd, write_fd = os.pipe()
        child_env = build_sanitized_subprocess_env(
            {
                "LRM_TRUSTED_EVENT_FD": str(write_fd),
                "OPENAI_API_KEY": gw_key,
            }
        )
        stdio_writer = BoundedJobStdioWriter(
            log_path,
            max_segment_bytes=self._job_stdio_segment_bytes,
            backup_count=MAX_JOB_STDIO_BACKUP_COUNT,
        )
        proc = subprocess.Popen(
            [sys.executable, str(worker_script), str(cfg_file)],
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        assert proc.stdout is not None
        drain_thread = threading.Thread(
            target=stdio_writer.drain_stream,
            args=(proc.stdout,),
            name=f"stdio-drain-{state.job_id}",
            daemon=True,
        )
        drain_thread.start()
        with state.lock:
            state.proc = proc

        trial_outcomes: list[dict[str, Any]] = []
        completed = 0
        scored = 0
        errors = 0

        try:
            with os.fdopen(read_fd, "r", encoding="utf-8") as pipe_fh:
                for raw_line in pipe_fh:
                    if state.cancel_event.is_set():
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    msg = json.loads(raw_line)
                    if msg.get("event_type") == "trial_finished":
                        completed += 1
                        st = str(msg.get("status") or "failed")
                        rw = msg.get("reward")
                        if st == "scored":
                            scored += 1
                        elif st in ("failed", "timeout"):
                            errors += 1
                        trial_outcomes.append(msg)
                        state.emit(
                            "trial_finished",
                            {
                                "source": "runner_live",
                                "trial_id": msg["trial_id"],
                                "task_id": msg["task_id"],
                                "attempt": msg.get("attempt", 1),
                                "status": st,
                                "reward": rw,
                                "exception_type": msg.get("exception_type"),
                                "duration_sec": msg.get("duration_sec"),
                                "progress": {
                                    "planned": total_planned,
                                    "completed": completed,
                                    "scored": scored,
                                    "errors": errors,
                                },
                            },
                        )

            rc = proc.wait(timeout=30)
        finally:
            drain_thread.join(timeout=10)
            stdio_writer.close()
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
            self._prune_job_stdio_logs()
        if state.cancel_event.is_set() or rc in (-signal.SIGTERM, 143):
            state.emit(
                "job_interrupted",
                {
                    "source": "runner_live",
                    "exit_code": rc,
                    "completed_trials": len(trial_outcomes),
                    "completed_task_ids": sorted({t["task_id"] for t in trial_outcomes if t.get("status") in ("scored", "unscored")}),
                    "reason": "用户优雅中止并完成排空",
                },
                new_status="interrupted",
                exit_code=rc,
            )
            return
        if rc != 0:
            state.emit(
                "job_failed",
                {
                    "source": "runner_live",
                    "exit_code": rc,
                    "reason": f"隔离 Direct 执行进程异常退出 (exit {rc})",
                    "completed_task_ids": sorted({t["task_id"] for t in trial_outcomes if t.get("status") in ("scored", "unscored")}),
                },
                new_status="failed",
                exit_code=rc,
            )
            return

        state.emit(
            "runner_completed",
            {
                "source": "runner_live",
                "exit_code": 0,
                "trial_outcomes": trial_outcomes,
                "completed_task_ids": sorted({t["task_id"] for t in trial_outcomes if t.get("status") in ("scored", "unscored")}),
            },
            new_status="finalizing",
            exit_code=0,
        )

    def _run_real_framework_cli(
        self,
        state: RunnerJobState,
        spec: dict[str, Any],
        work_dir: Path,
        evidence_dir: Path,
        log_path: Path,
        gw_pub: dict[str, Any],
        gw_key: str,
        total_planned: int,
        *,
        is_vlm: bool,
    ) -> None:
        """Dispatch the REAL SHA-256-verified managed `run.py` for Harbor, Harbor Gateway, and VLMEvalKit.
        - Uses `build_managed_run_py_command` + `materialize_managed_run_config` (ZERO unsupported
          `--evidence-store` or `--mode` flags; restricted evidence store passed via `LRM_EVIDENCE_STORE_DIR`).
        - Reuses the EXACT original `0700` `--work` directory and locked inputs on Harbor native `--resume`.
        - Captures child stdout/stderr strictly into bounded `BoundedJobStdioWriter` (<= 2 MiB/job,
          <= 20 MiB cumulative across all jobs) and NEVER parses stdout for progress!"""
        assert self.settings.runner_work_dir is not None
        effective_work_dir = work_dir
        if spec.get("resume_mode") == "native" and spec.get("harbor_work_dir"):
            cand_work = Path(str(spec["harbor_work_dir"]))
            if cand_work.is_symlink() or not cand_work.is_dir():
                raise RuntimeError("Harbor native --resume target --work directory is missing or symlinked")
            resolved_cand = cand_work.resolve()
            try:
                resolved_cand.relative_to(self.settings.runner_work_dir.resolve())
            except ValueError as exc:
                raise RuntimeError("Harbor native --resume target --work escapes runner_work_dir") from exc
            effective_work_dir = resolved_cand

        run_py = resolve_verified_managed_script(self.settings, "run.py")
        resolved_toml = materialize_managed_run_config(self.settings, spec, effective_work_dir)
        raw_output_root = effective_work_dir / "raw-output"
        cmd = build_managed_run_py_command(
            python_exe=sys.executable,
            run_py_path=str(run_py),
            spec=spec,
            config_path=str(resolved_toml),
            base_url=str(gw_pub["base_url"]),
            work_dir=str(effective_work_dir),
            output_dir=str(raw_output_root),
            plan_only=False,
        )

        extra_env: dict[str, str] = {
            "LRM_EVIDENCE_STORE_DIR": str(evidence_dir),
        }
        if gw_key:
            extra_env["OPENAI_API_KEY"] = gw_key
            extra_env["OPENAI_BASE_URL"] = str(gw_pub["base_url"])
        for pass_key in ("VLMEVALKIT_ROOT", "VLMEVALKIT_PYTHON", "PI_BENCH_RUNNER_BIN", "TIANXI_BENCH_BUNDLE"):
            if os.environ.get(pass_key):
                extra_env[pass_key] = os.environ[pass_key]
        if os.environ.get("LRM_RUNNER_OPENENV_PYTHONPATH"):
            extra_env["PYTHONPATH"] = os.environ["LRM_RUNNER_OPENENV_PYTHONPATH"]

        child_env = build_sanitized_subprocess_env(extra_env)
        stdio_writer = BoundedJobStdioWriter(
            log_path,
            max_segment_bytes=self._job_stdio_segment_bytes,
            backup_count=MAX_JOB_STDIO_BACKUP_COUNT,
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(effective_work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        assert proc.stdout is not None
        drain_thread = threading.Thread(
            target=stdio_writer.drain_stream,
            args=(proc.stdout,),
            name=f"stdio-drain-{state.job_id}",
            daemon=True,
        )
        drain_thread.start()
        with state.lock:
            state.proc = proc

        # Watch ONLY 0700 `<effective_work_dir>/jobs/<batch>/*/result.json` files (NEVER parse stdout!)
        jobs_batch_dir = effective_work_dir / "jobs" / str(spec["batch_name"])
        reward_key = str(spec.get("reward_key") or "reward")
        seen_trials: set[str] = set()
        trial_outcomes: list[dict[str, Any]] = []
        completed = 0
        scored = 0
        errors = 0

        def _harvest_results() -> None:
            nonlocal completed, scored, errors
            if not jobs_batch_dir.is_dir():
                return
            for rfile in sorted(jobs_batch_dir.glob("*/result.json")):
                tid = rfile.parent.name
                if tid in seen_trials or rfile.is_symlink():
                    continue
                try:
                    doc = json.loads(rfile.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(doc, dict) or not doc.get("finished_at"):
                    continue
                seen_trials.add(tid)
                vr = (doc.get("verifier_result") or {}).get("rewards") or {}
                rw = vr.get(reward_key) if reward_key in vr else vr.get("reward")
                exc_info = doc.get("exception_info")
                exc_type = exc_info.get("exception_type") if isinstance(exc_info, dict) else None
                if is_vlm and spec.get("vlmevalkit_mode") == "infer":
                    st = "unscored" if not exc_type else "failed"
                    rw = None
                elif exc_type:
                    st = "failed"
                    rw = None
                elif isinstance(rw, (int, float)):
                    st = "scored"
                else:
                    st = "unscored"
                completed += 1
                if st == "scored":
                    scored += 1
                elif st == "failed":
                    errors += 1

                # Ensure 0700 restricted evidence exists for every harvested trial
                ev_tdir = evidence_dir / tid
                ev_tdir.mkdir(parents=True, exist_ok=True)
                os.chmod(ev_tdir, 0o700)
                ev_file = ev_tdir / "evidence.json"
                if not ev_file.is_file():
                    vlm_res = doc.get("vlmeval_result") if isinstance(doc.get("vlmeval_result"), dict) else {}
                    dir_res = doc.get("direct_result") if isinstance(doc.get("direct_result"), dict) else {}
                    ev_doc = {
                        "trial_id": tid,
                        "task_id": str(doc.get("task_name") or tid),
                        "storage_tier": "isolated_0700_evidence_store",
                        "prompt": vlm_res.get("request_messages") or f"[Restricted trial prompt for {doc.get('task_name') or tid}]",
                        "response": vlm_res.get("prediction") or dir_res.get("prediction") or f"[Recorded result status={st}]",
                        "verifier_log": redact_text(str(doc.get("verifier_output") or f"status={st} reward={rw}")),
                        "usage": doc.get("usage") or {},
                    }
                    ev_file.write_text(json.dumps(ev_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    os.chmod(ev_file, 0o600)

                item = {
                    "trial_id": tid,
                    "task_id": str(doc.get("task_name") or tid),
                    "attempt": 1,
                    "status": st,
                    "reward": rw,
                    "exception_type": exc_type,
                    "duration_sec": float((doc.get("usage") or {}).get("duration_seconds") or 0.05),
                    "started_at": doc.get("started_at") or _utcnow(),
                    "finished_at": doc.get("finished_at") or _utcnow(),
                    "usage": doc.get("usage") or {},
                }
                trial_outcomes.append(item)
                state.emit(
                    "trial_finished",
                    {
                        "source": "runner_live",
                        "trial_id": tid,
                        "task_id": item["task_id"],
                        "attempt": 1,
                        "status": st,
                        "reward": rw,
                        "exception_type": exc_type,
                        "duration_sec": item["duration_sec"],
                        "progress": {
                            "planned": total_planned,
                            "completed": completed,
                            "scored": scored,
                            "errors": errors,
                        },
                    },
                )

        try:
            while proc.poll() is None:
                if state.cancel_event.is_set():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
                _harvest_results()
                time.sleep(0.05)

            rc = proc.wait(timeout=30)
            _harvest_results()
        finally:
            drain_thread.join(timeout=10)
            stdio_writer.close()
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
            self._prune_job_stdio_logs()

        done_tasks = sorted({t["task_id"] for t in trial_outcomes if t.get("status") in ("scored", "unscored")})
        if state.cancel_event.is_set() or rc in (-signal.SIGTERM, 143):
            state.emit(
                "job_interrupted",
                {
                    "source": "runner_live",
                    "exit_code": rc,
                    "completed_trials": len(trial_outcomes),
                    "completed_task_ids": done_tasks,
                    "reason": "用户优雅中止并完成排空",
                },
                new_status="interrupted",
                exit_code=rc,
            )
            return
        if rc == 75:
            state.emit(
                "paused_by_guard",
                {
                    "source": "runner_live",
                    "exit_code": 75,
                    "completed_task_ids": done_tasks,
                    "reason": "DispatchGuard 连续故障熔断触发 (exit 75)",
                    "progress": {"planned": total_planned, "completed": completed, "scored": scored, "errors": errors},
                },
                new_status="paused_by_guard",
                exit_code=75,
            )
            return
        if rc != 0:
            state.emit(
                "job_failed",
                {
                    "source": "runner_live",
                    "exit_code": rc,
                    "completed_task_ids": done_tasks,
                    "reason": f"隔离执行进程非零退出 (exit {rc})",
                },
                new_status="failed",
                exit_code=rc,
            )
            return

        state.emit(
            "runner_completed",
            {
                "source": "runner_live",
                "exit_code": 0,
                "trial_outcomes": trial_outcomes,
                "completed_task_ids": done_tasks,
            },
            new_status="finalizing",
            exit_code=0,
        )

    def get_events(self, job_id: str, after_seq: int) -> tuple[int, dict[str, Any]]:
        with self._lock:
            st = self._jobs.get(job_id)
        if st is None:
            return 404, {"code": "RESOURCE_NOT_FOUND", "message": f"Job not found on runner: {job_id}"}
        with st.lock:
            evs = [e for e in st.events if e["runner_seq"] > after_seq]
            return 200, {
                "job_id": job_id,
                "status": st.status,
                "exit_code": st.exit_code,
                "events": evs,
            }

    def cancel_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            st = self._jobs.get(job_id)
        if st is None:
            return 404, {"code": "RESOURCE_NOT_FOUND", "message": f"Job not found on runner: {job_id}"}
        st.cancel_event.set()
        with st.lock:
            proc = st.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        return 200, {"ok": True, "job_id": job_id, "status": "cancelling"}


def _make_handler(manager: IsolatedRunnerManager, expected_token: str) -> type[http.server.BaseHTTPRequestHandler]:
    class RunnerRequestHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self) -> str:
            if isinstance(self.client_address, tuple) and self.client_address:
                return str(self.client_address[0])
            return "unix"

        def log_message(self, format: str, *args: Any) -> None:
            manager.logger.info("IPC %s", format % args)

        def _send_json(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _check_auth(self) -> bool:
            got = self.headers.get("X-Runner-Token") or ""
            if not got or not hmac.compare_digest(got, expected_token):
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": "Invalid or missing X-Runner-Token"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._check_auth():
                return
            try:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == "/health":
                    self._send_json(200, manager.health())
                    return
                parts = [p for p in path.split("/") if p]
                if len(parts) == 4 and parts[0] == "v1" and parts[1] == "jobs" and parts[3] == "events":
                    qs = urllib.parse.parse_qs(parsed.query)
                    after = int((qs.get("after_seq") or ["0"])[0])
                    code, body = manager.get_events(parts[2], after)
                    self._send_json(code, body)
                    return
                self._send_json(404, {"code": "NOT_FOUND", "message": path})
            except Exception as exc:
                manager.logger.exception("Runner GET error")
                self._send_json(500, {"code": "RUNNER_INTERNAL_ERROR", "message": redact_text(str(exc)) or "internal error"})

        def do_POST(self) -> None:
            if not self._check_auth():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    self._send_json(400, {"code": "INVALID_JSON", "message": "Malformed JSON body"})
                    return
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == "/v1/jobs":
                    code, body = manager.submit_job(payload)
                    self._send_json(code, body)
                    return
                parts = [p for p in path.split("/") if p]
                if len(parts) == 4 and parts[0] == "v1" and parts[1] == "jobs" and parts[3] == "cancel":
                    code, body = manager.cancel_job(parts[2])
                    self._send_json(code, body)
                    return
                self._send_json(404, {"code": "NOT_FOUND", "message": path})
            except Exception as exc:
                manager.logger.exception("Runner POST error")
                self._send_json(500, {"code": "RUNNER_INTERNAL_ERROR", "message": redact_text(str(exc)) or "internal error"})

    return RunnerRequestHandler


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        sock_path = Path(str(self.server_address))
        if sock_path.exists() or sock_path.is_symlink():
            sock_path.unlink()
        old_umask = os.umask(0o177)
        try:
            raw = str(sock_path)
            if len(raw.encode("utf-8")) >= 100 and Path("/proc/self/fd").is_dir():
                dfd = os.open(str(sock_path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    self.socket.bind(f"/proc/self/fd/{dfd}/{sock_path.name}")
                finally:
                    os.close(dfd)
            else:
                super().server_bind()
        finally:
            os.umask(old_umask)
        os.chmod(sock_path, 0o600)


class ThreadingTCPHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="LRM Eval External Isolated Execution Runner Daemon.")
    parser.add_argument("--socket", default=os.environ.get("LRM_RUNNER_SOCKET_PATH", ""))
    parser.add_argument("--host", default=os.environ.get("LRM_RUNNER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LRM_RUNNER_PORT", "0")))
    parser.add_argument("--token-file", default=os.environ.get("LRM_RUNNER_IPC_TOKEN_FILE", ""))
    args = parser.parse_args()

    settings = Settings()
    assert settings.state_dir is not None and settings.runner_work_dir is not None and settings.evidence_store_dir is not None
    probe_isolated_dir(settings.state_dir, create=True)
    probe_isolated_dir(settings.runner_work_dir, create=True)
    probe_isolated_dir(settings.evidence_store_dir, create=True)

    token_path = Path(args.token_file) if args.token_file else (settings.state_dir / "runner_ipc.token")
    probe_secret_file(token_path, create_parent=False)
    expected_token = token_path.read_text(encoding="utf-8").strip()
    if len(expected_token) < 16:
        print("ERROR: runner_ipc.token must be >= 16 characters", file=sys.stderr)
        return 2

    logger, _ = configure_component_rotating_logger(settings.state_dir / "logs", "runner")
    manager = IsolatedRunnerManager(settings, logger)
    handler_cls = _make_handler(manager, expected_token)

    if args.socket:
        sock_path = Path(args.socket)
        probe_isolated_dir(sock_path.parent, create=True)
        server: Any = ThreadingUnixHTTPServer(str(sock_path), handler_cls)
        logger.info("Isolated Runner Daemon listening on Unix socket %s (pid=%d)", sock_path, os.getpid())
    elif args.port > 0:
        server = ThreadingTCPHTTPServer((args.host, args.port), handler_cls)
        logger.info("Isolated Runner Daemon listening on http://%s:%d (pid=%d)", args.host, args.port, os.getpid())
    else:
        print("ERROR: specify --socket <path> or --port <port>", file=sys.stderr)
        return 2

    def _shutdown(_sig: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if args.socket:
            try:
                Path(args.socket).unlink(missing_ok=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
