from __future__ import annotations

import csv
import fcntl
import grp
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable

import yaml

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.hot_indexer import write_runner_submission
from app.indexing.identity import make_batch_id
from app.logging_setup import (
    MAX_JOB_DETAIL_STDIO_READ_BYTES,
    MAX_JOB_STDIO_BYTES,
    read_bounded_tail_lines,
)
from app.runner.cli_contract import build_managed_run_py_command
from app.runner.client import RunnerIPCClient
from app.runner.vlmevalkit_results import OFFICIAL_RESULT_FILE, VLM_SCORING_FILES, encode_vlmevalkit_results
from app.security.auth import AuthUser
from app.security.authz import (
    authorize,
    has_scope,
    require_project_access,
    sql_project_filter,
)
from app.security.gateways import GatewayProfileRegistry
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from app.security.redaction import redact_json_recursive, redact_path, redact_text
from app.security.storage_probe import probe_isolated_dir, validate_safe_id

ALLOWED_RUNNER_KINDS = ("harbor", "harbor_gateway", "vlmevalkit", "direct")
TERMINAL_STATUSES = frozenset({"finished", "failed", "interrupted", "paused_by_guard"})
FORBIDDEN_SECRET_KEYS = frozenset({"api_key", "apikey", "token", "access_token", "secret", "password", "authorization"})
SAFE_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,95}$")
SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Unintegrated raw datasets visible on 197 (02-6 §1.2 & README §1.1)
RAW_VISIBLE_UNINTEGRATED_SUITES: tuple[dict[str, Any], ...] = (
    {
        "suite_id": "mmlu-pro-raw",
        "display_name": "MMLU-Pro",
        "runner": "none",
        "registered": False,
        "selectable": False,
        "reason": "原始数据可见、未接入评测（待登记 harbor.toml 与统一评分器）",
    },
    {
        "suite_id": "bfcl-v4-raw",
        "display_name": "BFCL-v4 (tool call)",
        "runner": "none",
        "registered": False,
        "selectable": False,
        "reason": "原始数据可见、未接入评测（待登记 harbor.toml 与工具调用评分器）",
    },
    {
        "suite_id": "global-mmlu-lite-pending",
        "display_name": "Global-MMLU-Lite",
        "runner": "none",
        "registered": False,
        "selectable": False,
        "reason": "上游版本/语言/Split 待固定，尚未接入评测",
    },
)


def is_docker_group_attached() -> bool:
    """Real runtime POSIX check ensuring the Web process is NEVER in the `docker` group (S-04)."""
    try:
        docker_gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        return False
    groups = set(os.getgroups())
    groups.add(os.getgid())
    groups.add(os.getegid())
    return docker_gid in groups


def _read_cgroup_telemetry() -> dict[str, Any]:
    """Read optional Linux cgroup v2 telemetry, honestly reporting `unavailable` when absent."""
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
            "note": "当前非隔离容器 cgroup v2 作用域，不伪造容器遥测",
        }
    return {
        "source": "cgroup_v2",
        "cpu_usage_usec": cpu_usec,
        "memory_peak_bytes": mem_peak,
    }


def _reject_forbidden_secret_fields(payload: dict[str, Any]) -> None:
    for k in payload:
        if str(k).lower() in FORBIDDEN_SECRET_KEYS:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"严禁通过浏览器请求传入明文凭据字段 `{k}`，请仅选择服务端 0600 Gateway Profile",
                {"field": k},
            )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign_event_record(channel_secret: str, seq_id: int, event_type: str, payload: dict[str, Any]) -> str:
    body = json.dumps({"seq_id": seq_id, "event_type": event_type, "payload": payload}, sort_keys=True, ensure_ascii=False)
    return hmac.new(channel_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_runner_event_hmac(channel_secret: str, rec: dict[str, Any]) -> bool:
    seq = rec.get("runner_seq")
    etype = rec.get("event_type")
    payload = rec.get("payload")
    sig = rec.get("hmac_sha256")
    if not isinstance(seq, int) or not isinstance(etype, str) or not isinstance(payload, dict) or not isinstance(sig, str):
        return False
    expected = _sign_event_record(channel_secret, seq, etype, payload)
    return hmac.compare_digest(sig, expected)


class ExecutionService:
    """M3 External Isolated Execution Runner Control Plane (v2.2.1 · I-05, S-04, S-06).

    Hard invariants:
    1. ZERO in-process simulated score workers — ALL jobs are dispatched strictly over authenticated IPC
       (`RunnerIPCClient`) to an independent external `app.runner.daemon` OS process (`runner_pid != os.getpid()`).
    2. When no external Runner daemon is attached or healthy, `isolated_runner_ready` is `False` and
       `POST /jobs`, `POST .../resume`, `POST .../cancel` fail closed with `503 RUNNER_NOT_ATTACHED`.
    3. Suite task planning reads REAL datasets (`*/task.toml`, VLMEvalKit TSV indices, Direct `cases.jsonl`) —
       ZERO synthetic 4-task fallback IDs!
    4. Progress & state are driven EXCLUSIVELY by HMAC-authenticated Runner IPC events (backed by private pipe /
       `0700` `result.json` files), NEVER by subprocess stdout/stderr.
    5. Common Finalizer publishes approved Public DTOs to an indexed output store (`published_batches_dir` by default
       with `source_namespace='runner_published'`, or `harbor_runs_dir` when explicitly requested & allowed), verifies
       via managed `report.py`, confirms `HotIndexer` indexed the exact `(batch_id, generation_id)` into SQLite, and
       ONLY THEN emits `reconciled` (ZERO 404 reconciled links!).
    6. `get_authorized_trial_evidence()` fails closed with `409 GENERATION_MISMATCH` if committed `generation_id`,
       `0600 .evidence-digests.json`, trial digest entry, size, or SHA-256 is missing or mismatched.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        coordinator: Any,
        gateway_profiles: GatewayProfileRegistry,
        write_guard: Callable[[], None],
    ) -> None:
        self.settings = settings
        self.db = db
        self.coordinator = coordinator
        self.gateway_profiles = gateway_profiles
        self.write_guard = write_guard
        self.ipc = RunnerIPCClient(settings)
        self._lock = threading.Lock()
        self._event_cond = threading.Condition()
        self._stopping = threading.Event()
        self._webhook_dispatch_fn: Callable[[str, dict[str, Any]], None] | None = None

    def start_recovery(self) -> None:
        if not self.ipc.is_configured or self.db.read_only_mode:
            return
        for row in self.db.fetchall("SELECT job_id FROM exec_batch_reservations"):
            job_id = row["job_id"]
            threading.Thread(target=self._recover_reserved_job, args=(job_id,), daemon=True,
                             name=f"lrm-recover-{job_id}").start()

    def stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _check_private_job_file(fd: int) -> None:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise PermissionError("Expected private Runner job file")

    def _load_channel_key(self, work_dir: Path) -> str | None:
        try:
            fd = os.open(work_dir / ".web-channel.key", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="ascii") as stream:
                self._check_private_job_file(stream.fileno())
                key = stream.read(65)
            return key if HEX64_RE.fullmatch(key) else None
        except (OSError, UnicodeError):
            return None

    def _recover_reserved_job(self, job_id: str) -> None:
        assert self.settings.runner_work_dir is not None
        work_dir = self.settings.runner_work_dir / job_id
        channel_path = work_dir / ".web-events.ndjson"
        try:
            validate_safe_id(job_id, "job_id")
            while not self._stopping.is_set():
                try:
                    fd = os.open(channel_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "r+b") as lease:
                        self._check_private_job_file(lease.fileno())
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # Load the latest checkpoint only after the previous owner released its lease.
                        row = self.db.fetchone(
                            """
                            SELECT j.params_json FROM exec_jobs j JOIN exec_batch_reservations r USING (job_id)
                            WHERE j.job_id = ?
                            """,
                            (job_id,),
                        )
                        if row is None:
                            return
                        spec = json.loads(row["params_json"])
                        key = self._load_channel_key(work_dir)
                        if spec.get("dispatch_started") is False:
                            self._interrupt_missing_runner_job(job_id, channel_path, key, never_dispatched=True)
                            return
                        status, body = self.ipc.recover_job(job_id)
                        if status == 404 and body.get("code") == "NOT_FOUND":
                            # A Runner from before the recovery protocol can still replay accepted jobs.
                            status, body = self.ipc.get_events(job_id)
                        if body.get("job_id") == job_id:
                            if self._confirmed_runner_absence(status, body, job_id, spec):
                                self._interrupt_missing_runner_job(job_id, channel_path, key)
                                return
                            if status == 200:
                                if key is None:
                                    self._emit_trusted_event(job_id, "job_recovery_pending", {
                                        "source": "control_plane",
                                        "reason": "Runner 已接收作业，但缺少恢复所需的私有事件密钥",
                                    })
                                    return
                                self._sync_runner_job_events(job_id, spec, channel_path, key)
                                return
                except (OSError, HTTPException, AppError):
                    # A held lease or unavailable Runner gives no evidence that a job is absent.
                    if self.db.fetchone("SELECT 1 FROM exec_batch_reservations WHERE job_id = ?", (job_id,)) is None:
                        return
                self._stopping.wait(0.5)
        finally:
            self.db.close_thread_connection()

    @staticmethod
    def _confirmed_runner_absence(status: int, body: dict[str, Any], job_id: str, spec: dict[str, Any]) -> bool:
        instance = spec.get("runner_instance_id")
        return bool(instance and status == 404 and body.get("code") == "JOB_DISPATCH_ABANDONED"
                    and body.get("job_id") == job_id and body.get("runner_instance_id") == instance)

    def _interrupt_missing_runner_job(
        self, job_id: str, channel_path: Path, channel_secret: str | None, *, never_dispatched: bool = False,
    ) -> None:
        if never_dispatched:
            reason = "Web 进程在开始派发前中断"
            reason_code = "runner_dispatch_not_started"
        else:
            reason = "隔离 Runner 已确认该作业不存在并拒绝迟到的派发"
            reason_code = "runner_job_absent"
        self._emit_trusted_event(
            job_id, "job_interrupted",
            {"source": "control_plane", "reason": reason, "exit_code": 1},
            channel_path=channel_path, channel_secret=channel_secret, new_status="interrupted", exit_code=1,
        )
        self._dispatch_terminal_notification(job_id, "interrupted", {"reason": reason_code})

    # ------------------------------------------------------------------
    # Live External Runner Health Probe (NEVER fakes readiness!)
    # ------------------------------------------------------------------
    def probe_runner_health(self) -> dict[str, Any]:
        try:
            work_probe = probe_isolated_dir(self.settings.runner_work_dir, create=False)  # type: ignore[arg-type]
        except AppError as exc:
            work_probe = {"ok": False, "code": exc.code, "message": exc.message}
        try:
            ev_probe = probe_isolated_dir(self.settings.evidence_store_dir, create=False)  # type: ignore[arg-type]
        except AppError as exc:
            ev_probe = {"ok": False, "code": exc.code, "message": exc.message}

        docker_attached = is_docker_group_attached()
        ipc_health = self.ipc.probe_health(timeout=0.8)
        runner_pid = ipc_health.get("runner_pid")
        separate_process = isinstance(runner_pid, int) and runner_pid > 0 and runner_pid != os.getpid()

        ready = bool(
            ipc_health.get("connected") is True
            and ipc_health.get("ok") is True
            and separate_process
            and work_probe.get("ok") is True
            and ev_probe.get("ok") is True
            and not docker_attached
        )
        reason = None
        if not ready:
            if docker_attached:
                reason = "Web 进程禁止挂载 docker 用户组"
            elif not ipc_health.get("configured"):
                reason = "尚未接入独立隔离执行服务（生产入口仅开放 --plan 预检、CLI 生成与只读监控）"
            elif not ipc_health.get("connected"):
                reason = str(ipc_health.get("reason") or "独立隔离 Runner 服务不可达")
            elif not separate_process:
                reason = "隔离 Runner 必须运行在独立 OS 进程中"
            elif not ipc_health.get("ok"):
                reason = str(ipc_health.get("reason") or "隔离 Runner 内部探针未通过")
            else:
                reason = "Web 侧 0700 存储探针未通过"

        return {
            "isolated_runner_ready": ready,
            "runner_connected": bool(ipc_health.get("connected")),
            "runner_configured": bool(ipc_health.get("configured")),
            "runner_mode": "external_ipc" if ready else "disconnected_readonly",
            "runner_id": ipc_health.get("runner_id"),
            "runner_instance_id": ipc_health.get("runner_instance_id"),
            "runner_pid": runner_pid if separate_process else None,
            "runner_user": ipc_health.get("runner_user"),
            "docker_available": bool(ipc_health.get("docker_available", False)),
            "vlmevalkit_ready": bool(ipc_health.get("vlmevalkit_ready", False)),
            "direct_ready": bool(ipc_health.get("direct_ready", False)),
            "supported_runners": list(ipc_health.get("supported_runners") or []),
            "reason": reason,
            "docker_group_attached": docker_attached,
            "runner_work_isolation": work_probe,
            "evidence_store_isolation": ev_probe,
            "trusted_event_channel": "hmac_authenticated_private_0600_ndjson",
            "stdout_drives_progress": False,
            "telemetry": {
                "streaming_ttft_tps": "optional_gateway_stream",
                "container_cgroup": ipc_health.get("cgroup_telemetry") or _read_cgroup_telemetry(),
            },
        }

    # ------------------------------------------------------------------
    # Real Suite Registry & Real Task Discovery (ZERO synthetic task IDs!)
    # ------------------------------------------------------------------
    def _load_registered_suites(self) -> list[dict[str, Any]]:
        suites: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Harbor suites from managed / framework harbor.toml
        managed_toml = self.settings.managed_framework_dir / "harbor.toml"
        fw_toml = self.settings.framework_dir / "harbor.toml"
        toml_path = managed_toml if (self.settings.production_mode and managed_toml.is_file()) else (fw_toml if fw_toml.is_file() else managed_toml)
        bundle_root = Path.home() / ".cache" / "tianxi-harbor" / "bundle"
        if toml_path.is_file():
            try:
                cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
                if isinstance(cfg.get("bundle"), str) and cfg["bundle"].strip():
                    bundle_root = Path(cfg["bundle"]).expanduser()
                for suite_id, meta in (cfg.get("datasets") or {}).items():
                    if not isinstance(meta, dict):
                        continue
                    sid = str(suite_id)
                    runner = str(meta.get("runner") or "harbor")
                    raw_path = str(meta.get("path") or "")
                    path_root = str(meta.get("path_root") or "")
                    if path_root == "bundle":
                        ds_path = (bundle_root / raw_path) if raw_path else None
                    elif raw_path:
                        p_cand = Path(raw_path)
                        ds_path = p_cand if p_cand.is_absolute() else (self.settings.framework_dir / p_cand).resolve()
                    else:
                        ds_path = None
                    seen_ids.add(sid)
                    suites.append(
                        {
                            "suite_id": sid,
                            "display_name": str(meta.get("name") or sid),
                            "runner": runner,
                            "split": str(meta.get("split") or ("holdout" if sid.endswith("-holdout") else "default")),
                            "handoff_profile": meta.get("handoff_profile"),
                            "reward_key": str(meta.get("reward_key") or "reward"),
                            "dataset_path": str(ds_path) if ds_path is not None else None,
                            "config_path": str(toml_path),
                            "registered": True,
                            "selectable": True,
                            "reason": None,
                        }
                    )
            except Exception:
                pass

        # 2. VLMEvalKit suites from real datasets/multimodal/harbor.toml
        mm_toml = self.settings.datasets_dir / "multimodal" / "harbor.toml"
        if mm_toml.is_file():
            try:
                mm_cfg = tomllib.loads(mm_toml.read_text(encoding="utf-8"))
                for suite_id, meta in (mm_cfg.get("datasets") or {}).items():
                    if not isinstance(meta, dict) or str(suite_id) in seen_ids:
                        continue
                    sid = str(suite_id)
                    vlm_cfg = meta.get("vlmevalkit") if isinstance(meta.get("vlmevalkit"), dict) else {}
                    raw_path = str(meta.get("path") or "")
                    ds_path = Path(raw_path) if raw_path else None
                    seen_ids.add(sid)
                    suites.append(
                        {
                            "suite_id": sid,
                            "display_name": str(meta.get("name") or sid),
                            "runner": str(meta.get("runner") or "vlmevalkit"),
                            "split": "smoke" if "smoke" in sid else ("val" if "val" in sid else "test"),
                            "handoff_profile": None,
                            "reward_key": str(vlm_cfg.get("metric") or "accuracy"),
                            "default_vlm_mode": str(vlm_cfg.get("mode") or "all"),
                            "dataset_path": str(ds_path) if ds_path is not None else None,
                            "config_path": str(mm_toml),
                            "registered": True,
                            "selectable": True,
                            "reason": None,
                        }
                    )
            except Exception:
                pass

        # 3. Direct native suites from config/direct_suites.yaml (real native cases.jsonl!)
        ds_yaml = self.settings.direct_suites_file
        if ds_yaml.is_file():
            try:
                dcfg = yaml.safe_load(ds_yaml.read_text(encoding="utf-8")) or {}
                for item in dcfg.get("suites") or []:
                    if not isinstance(item, dict) or not item.get("suite_id"):
                        continue
                    sid = str(item["suite_id"]).strip()
                    if sid in seen_ids:
                        continue
                    rel_cases = str(item.get("cases_path") or "").strip()
                    cases_p = (self.settings.config_dir / rel_cases).resolve() if rel_cases else None
                    if cases_p is not None:
                        try:
                            cases_p.relative_to(self.settings.config_dir.resolve())
                        except ValueError:
                            cases_p = None
                    seen_ids.add(sid)
                    suites.append(
                        {
                            "suite_id": sid,
                            "display_name": str(item.get("display_name") or sid),
                            "runner": "direct",
                            "split": str(item.get("split") or "smoke"),
                            "handoff_profile": None,
                            "reward_key": str(item.get("reward_key") or "reward"),
                            "dataset_path": str(cases_p) if cases_p is not None else None,
                            "scorer_id": str(item.get("scorer_id") or "exact_match"),
                            "config_path": str(ds_yaml),
                            "registered": True,
                            "selectable": True,
                            "reason": None,
                        }
                    )
            except Exception:
                pass

        return suites

    def _discover_suite_tasks(self, suite: dict[str, Any]) -> tuple[list[str], int | None, str]:
        """Discover REAL task IDs from the actual dataset on disk (ZERO hardcoded synthetic 4-task fallback!).
        Returns (task_ids, total_count, discovery_status)."""
        runner = str(suite.get("runner") or "")
        ds_raw = suite.get("dataset_path")
        if not ds_raw:
            return [], None, "dataset_path_unset"
        ds_path = Path(str(ds_raw))

        if runner == "direct":
            if not ds_path.is_file() or ds_path.is_symlink():
                return [], None, "dataset_file_missing"
            ids: list[str] = []
            try:
                for line in ds_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj["id"].strip():
                        ids.append(obj["id"].strip())
                return ids, len(ids), "ok"
            except Exception:
                return [], None, "dataset_parse_error"

        if runner == "harbor":
            if not ds_path.is_dir():
                return [], None, "requires_runner_bundle"
            try:
                ids = sorted(p.parent.name for p in ds_path.glob("*/task.toml") if p.is_file())
                return ids, len(ids), "ok" if ids else "empty_dataset"
            except OSError:
                return [], None, "dataset_unreadable"

        if runner == "vlmevalkit":
            if not ds_path.is_file():
                return [], None, "dataset_file_missing"
            try:
                # Stream only header + index column without reading huge base64 image columns into RAM!
                ids = []
                with ds_path.open("r", encoding="utf-8", errors="replace") as fh:
                    header_line = fh.readline()
                    if not header_line:
                        return [], 0, "empty_dataset"
                    cols = header_line.rstrip("\r\n").split("\t")
                    idx_col = cols.index("index") if "index" in cols else 0
                    for line in fh:
                        if not line.strip():
                            continue
                        parts = line.split("\t", idx_col + 2)
                        if len(parts) > idx_col and parts[idx_col].strip():
                            ids.append(parts[idx_col].strip())
                return ids, len(ids), "ok"
            except OSError:
                return [], None, "dataset_unreadable"

        return [], None, "unknown_runner"

    def get_capabilities(self, user: AuthUser) -> dict[str, Any]:
        registered = self._load_registered_suites()
        if not has_scope(user, "dataset:holdout_read"):
            visible_registered = [
                s for s in registered if s.get("split") != "holdout" and "holdout" not in s["suite_id"].lower()
            ]
        else:
            visible_registered = registered

        # Strip internal absolute filesystem paths before returning public capabilities (S-03)
        public_suites: list[dict[str, Any]] = []
        for s in visible_registered:
            tasks, count, disc_status = self._discover_suite_tasks(s)
            public_suites.append(
                {
                    "suite_id": s["suite_id"],
                    "display_name": s["display_name"],
                    "runner": s["runner"],
                    "split": s.get("split"),
                    "handoff_profile": s.get("handoff_profile"),
                    "reward_key": s.get("reward_key"),
                    "task_count": count,
                    "sample_tasks": tasks[:10],
                    "task_discovery_status": disc_status,
                    "registered": True,
                    "selectable": True,
                    "reason": None,
                }
            )

        runner_env = self.probe_runner_health()
        return {
            "methodology_version": self.settings.methodology_version,
            "runner_kinds": [
                {
                    "kind": "harbor",
                    "display_name": "Harbor 标准模式",
                    "requires_docker": True,
                    "available_on_runner": "harbor" in runner_env["supported_runners"],
                    "native_resume_supported": True,
                    "derived_resume_supported": True,
                    "manual_pause_supported": False,
                    "guard_pause_exit_code": 75,
                    "partial_task_forces_infer": False,
                    "notes": "隔离容器 Runner（需 Runner 节点连接 Docker）；原生 --resume 需锁定 harness_version 且私有 0700 --work 锁完整；paused_by_guard (exit 75) 仅由 DispatchGuard 熔断触发。",
                },
                {
                    "kind": "harbor_gateway",
                    "display_name": "Harbor Gateway 模式 (--gateway-capture)",
                    "requires_docker": True,
                    "available_on_runner": "harbor_gateway" in runner_env["supported_runners"],
                    "native_resume_supported": False,
                    "derived_resume_supported": True,
                    "manual_pause_supported": False,
                    "guard_pause_exit_code": 75,
                    "partial_task_forces_infer": False,
                    "notes": "OpenEnv 代理抓包落 0700 独立授权证据存储（需 Runner 节点连接 Docker）；原生不支持 --resume（返回 409 RESUME_UNSUPPORTED），支持 M3 任务级派生续跑。",
                },
                {
                    "kind": "vlmevalkit",
                    "display_name": "VLMEvalKit 多模态模式",
                    "requires_docker": False,
                    "available_on_runner": "vlmevalkit" in runner_env["supported_runners"],
                    "native_resume_supported": False,
                    "derived_resume_supported": True,
                    "manual_pause_supported": False,
                    "guard_pause_exit_code": None,
                    "partial_task_forces_infer": True,
                    "notes": "需 Runner 节点配置 VLMEVALKIT_ROOT / VLMEVALKIT_PYTHON；VLMEVALKIT.md:44 硬约束：指定 --task 局部运行时强制锁定 mode=infer（unscored）；原生不支持 --resume。",
                },
                {
                    "kind": "direct",
                    "display_name": "Direct 原生直测模式",
                    "requires_docker": False,
                    "available_on_runner": "direct" in runner_env["supported_runners"],
                    "native_resume_supported": False,
                    "derived_resume_supported": True,
                    "manual_pause_supported": False,
                    "guard_pause_exit_code": None,
                    "partial_task_forces_infer": False,
                    "notes": "调度受管 framework/direct.py + 服务端 0600 Gateway Profile，无需 Docker；原生不支持 --resume，支持 M3 任务级派生续跑。",
                },
            ],
            "suites": public_suites,
            "unintegrated_suites": list(RAW_VISIBLE_UNINTEGRATED_SUITES),
            "gateway_profiles": self.gateway_profiles.list_public_profiles(),
            "webhook_profiles": [
                {"profile_id": "eval-channel", "display_name": "#eval 频道通知 (站内 + Webhook)"},
                {"profile_id": "ops-alert", "display_name": "运维告警通道 (熔断/失败)"},
            ],
            "runner_environment": runner_env,
        }

    # ------------------------------------------------------------------
    # Plan & Validation (REAL suite tasks — ZERO fake 4-task fallback!)
    # ------------------------------------------------------------------
    def _validate_and_normalize_spec(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        _reject_forbidden_secret_fields(payload)
        kind = str(payload.get("kind") or "").strip().lower()
        if kind not in ALLOWED_RUNNER_KINDS:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"不支持的执行器类型: {kind}（允许值: {', '.join(ALLOWED_RUNNER_KINDS)}）",
                {"field": "kind"},
            )

        suite_id = str(payload.get("suite_id") or "").strip()
        if not suite_id:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "必须选择已正式登记的题库 suite_id", {"field": "suite_id"})

        unintegrated_ids = {s["suite_id"] for s in RAW_VISIBLE_UNINTEGRATED_SUITES}
        if suite_id in unintegrated_ids:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"题库 `{suite_id}` 为「原始数据可见、未接入评测」，禁止提交执行",
                {"field": "suite_id"},
            )

        registered = {s["suite_id"]: s for s in self._load_registered_suites()}
        if suite_id not in registered:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"未在 harbor.toml / direct_suites.yaml 中登记的题库: {suite_id}",
                {"field": "suite_id"},
            )
        suite = registered[suite_id]
        expected_runner = str(suite.get("runner") or "harbor")
        allowed_kinds_for_suite = {"harbor", "harbor_gateway"} if expected_runner == "harbor" else {expected_runner}
        if kind not in allowed_kinds_for_suite:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"题库 `{suite_id}` 归属运行器 `{expected_runner}`，与所选 `{kind}` 不匹配",
                {"field": "kind", "expected_runner": expected_runner},
            )

        if (suite.get("split") == "holdout" or "holdout" in suite_id.lower()) and not has_scope(user, "dataset:holdout_read"):
            raise AppError(
                403,
                "SCOPE_FORBIDDEN",
                "缺少 dataset:holdout_read 权限，禁止针对留出题库发起执行或预检",
                {"missing_scope": "dataset:holdout_read"},
            )

        selection_mode = str(payload.get("selection_mode") or "all").strip().lower()
        if selection_mode not in ("all", "tasks"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "selection_mode 仅支持 all 或 tasks", {"field": "selection_mode"})

        raw_tasks = payload.get("tasks") or []
        if isinstance(raw_tasks, str):
            raw_tasks = [t.strip() for t in re.split(r"[,\s]+", raw_tasks) if t.strip()]
        if not isinstance(raw_tasks, list):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "tasks 必须为任务 ID 数组", {"field": "tasks"})
        tasks: list[str] = []
        for t in raw_tasks:
            ts = str(t).strip()
            if not ts:
                continue
            if ".." in ts or ts.startswith("/") or not SAFE_TASK_RE.fullmatch(ts):
                raise AppError(400, "INVALID_PATH_OR_ID", f"非法 task_id: {ts}", {"field": "tasks"})
            if ts not in tasks:
                tasks.append(ts)
        if selection_mode == "tasks" and not tasks:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "selection_mode=tasks 时必须指定至少一个 --task", {"field": "tasks"})
        if selection_mode == "all":
            tasks = []

        # Discover REAL tasks from disk (ZERO artificial 4-task fabrication!)
        discovered_tasks, discovered_count, discovery_status = self._discover_suite_tasks(suite)
        if selection_mode == "tasks" and discovery_status == "ok" and discovered_tasks:
            known_set = set(discovered_tasks)
            unknown = [t for t in tasks if t not in known_set]
            if unknown:
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"题库 `{suite_id}` 中不存在指定任务 ID: {', '.join(unknown[:5])}",
                    {"field": "tasks", "unknown_tasks": unknown, "available_sample": discovered_tasks[:10]},
                )

        if selection_mode == "tasks":
            planned_tasks = list(tasks)
            planned_task_count: int | None = len(tasks)
        else:
            planned_tasks = list(discovered_tasks)
            planned_task_count = discovered_count

        model_route = str(payload.get("model_route") or "Qwen3_8-27B").strip()
        if not SAFE_MODEL_RE.fullmatch(model_route):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "model_route 格式非法", {"field": "model_route"})

        gateway_profile_id = str(payload.get("gateway_profile_id") or "qwen197-vllm").strip()
        gw_pub = self.gateway_profiles.get_public_profile(gateway_profile_id)
        judge_profile_id = payload.get("judge_profile_id")
        if judge_profile_id:
            judge_profile_id = str(judge_profile_id).strip()
            self.gateway_profiles.get_public_profile(judge_profile_id)
        else:
            judge_profile_id = None

        agent = str(payload.get("agent") or ("opencode" if kind in ("harbor", "harbor_gateway") else kind)).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", agent):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "agent 名称非法", {"field": "agent"})
        if kind == "harbor_gateway" and agent in ("oracle", "nop"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "Harbor Gateway 模式不支持 oracle / nop 代理", {"field": "agent"})
        harness_version = str(payload.get("harness_version") or ("1.19.3" if agent == "opencode" else "native-v1")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", harness_version):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "harness_version 格式非法", {"field": "harness_version"})

        concurrent = int(payload.get("concurrent") or 2)
        if concurrent < 1 or concurrent > 32:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "concurrent 必须在 1..32 之间", {"field": "concurrent"})

        attempts = int(payload.get("attempts") or 1)
        if attempts < 1 or attempts > 16:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "attempts 必须在 1..16 之间", {"field": "attempts"})
        if kind == "vlmevalkit" and attempts != 1:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "VLMEvalKit 模式仅支持 attempts=1", {"field": "attempts"})

        pause_after = int(payload.get("pause_after") or 3)
        if pause_after < 1 or pause_after > 50:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "pause_after 必须在 1..50 之间", {"field": "pause_after"})

        batch_name = str(
            payload.get("batch_name")
            or f"{suite_id}-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        ).strip()
        if not SAFE_BATCH_RE.fullmatch(batch_name):
            raise AppError(
                400,
                "INVALID_PATH_OR_ID",
                "batch_name 仅允许字母/数字/._-，长度 2–96 且禁止路径穿越",
                {"field": "batch_name"},
            )

        project_id = str(payload.get("project_id") or "default").strip()
        validate_safe_id(project_id.replace(":", "_"), "project_id")
        require_project_access(user, project_id)

        # VLMEvalKit VLMEVALKIT.md:44 hard constraint (S-04):
        suite_default_vlm_mode = str(suite.get("default_vlm_mode") or "all").strip().lower()
        requested_vlm_mode = str(payload.get("vlmevalkit_mode") or suite_default_vlm_mode or "all").strip().lower()
        if requested_vlm_mode not in ("all", "infer"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "vlmevalkit_mode 仅支持 all 或 infer", {"field": "vlmevalkit_mode"})

        warnings: list[str] = []
        forced_infer = False
        effective_vlm_mode: str | None = None
        if kind == "vlmevalkit":
            if selection_mode == "tasks":
                effective_vlm_mode = "infer"
                forced_infer = True
                warnings.append(
                    "依据 VLMEVALKIT.md:44 源码硬约束：指定 --task 局部运行时强制锁定 mode='infer'（仅推理，不运行官方评分器、不生成 official-result.json），自动标为 run_scope='smoke'/'subset' 与 official_status='unscored'。"
                )
            elif suite_default_vlm_mode == "infer":
                effective_vlm_mode = "infer"
                if requested_vlm_mode == "all":
                    warnings.append(
                        f"题库 `{suite_id}` 在 multimodal/harbor.toml 中固定为 mode='infer'，已保持仅推理模式。"
                    )
            else:
                effective_vlm_mode = requested_vlm_mode
                if effective_vlm_mode == "infer":
                    warnings.append(
                        "受管 run.py 无 --mode 命令行参数：全量 infer 模式已通过作业私有 0600 .resolved_harbor.toml 覆写 [vlmevalkit].mode='infer' 生效。"
                    )

        if selection_mode == "all":
            effective_run_scope = "smoke" if suite.get("split") == "smoke" else "full_split"
        else:
            effective_run_scope = "smoke" if len(tasks) <= 10 else "subset"

        if kind == "vlmevalkit" and effective_vlm_mode == "infer":
            effective_official_status = "unscored"
        elif kind == "vlmevalkit":
            effective_official_status = "official_pending"
        else:
            effective_official_status = "unofficial_valid_mean"

        if selection_mode == "all" and discovery_status != "ok":
            warnings.append(
                f"题库 `{suite_id}` 在当前 Web 节点未挂载本地题集目录（状态: {discovery_status}），全量任务列表将在执行节点展开。"
            )

        # Safe redacted CLI command preview strictly generated by the shared native run.py contract
        # (ZERO unsupported --evidence-store or --mode flags; restricted evidence uses LRM_EVIDENCE_STORE_DIR!)
        preview_config: str | None = None
        if kind == "vlmevalkit":
            preview_config = (
                f"<RUNNER_WORK_0700>/{batch_name}/.resolved_harbor.toml"
                if (selection_mode == "all" and effective_vlm_mode == "infer" and suite_default_vlm_mode != "infer")
                else "datasets/multimodal/harbor.toml"
            )
        elif kind in ("harbor", "harbor_gateway"):
            preview_config = f"<RUNNER_WORK_0700>/{batch_name}/.resolved_harbor.toml"

        preview_spec = {
            "kind": kind,
            "suite_id": suite_id,
            "selection_mode": selection_mode,
            "tasks": tasks,
            "planned_tasks": planned_tasks,
            "agent": agent,
            "harness_version": harness_version,
            "attempts": attempts,
            "pause_after": pause_after,
            "model_route": model_route,
            "batch_name": batch_name,
            "concurrent": concurrent,
        }
        cmd_parts = build_managed_run_py_command(
            python_exe="python3",
            run_py_path="framework/run.py",
            spec=preview_spec,
            config_path=preview_config,
            base_url=None,
            work_dir=f"<RUNNER_WORK_0700>/{batch_name}",
            output_dir=f"<PUBLIC_DTO_OUTPUT>/{batch_name}",
            plan_only=False,
        )
        command_preview = shlex.join(cmd_parts)

        notify = payload.get("notify") or {}
        if not isinstance(notify, dict):
            notify = {}
        notify_clean = {
            "in_app": bool(notify.get("in_app", True)),
            "webhook_profile_id": str(notify.get("webhook_profile_id") or "").strip() or None,
            "on_events": [
                str(e)
                for e in (notify.get("on_events") or ["finished", "failed", "paused_by_guard"])
                if str(e) in ("finished", "failed", "paused_by_guard", "interrupted")
            ],
        }

        return {
            "kind": kind,
            "suite_id": suite_id,
            "suite_display_name": suite["display_name"],
            "selection_mode": selection_mode,
            "tasks": tasks,
            "vlmevalkit_mode": effective_vlm_mode,
            "forced_infer_by_task_selection": forced_infer,
            "effective_run_scope": effective_run_scope,
            "effective_official_status": effective_official_status,
            "model_route": model_route,
            "gateway_profile_id": gateway_profile_id,
            "gateway_base_url": gw_pub["base_url"],
            "judge_profile_id": judge_profile_id,
            "agent": agent,
            "harness_version": harness_version,
            "concurrent": concurrent,
            "attempts": attempts,
            "pause_after": pause_after,
            "batch_name": batch_name,
            "project_id": project_id,
            "notify": notify_clean,
            "command_preview": command_preview,
            "warnings": warnings,
            "native_resume_supported": kind == "harbor",
            "derived_resume_supported": True,
            "requires_docker": kind in ("harbor", "harbor_gateway"),
            "execution_driver": "external_isolated_runner_ipc",
            "planned_tasks": planned_tasks,
            "planned_task_count": planned_task_count,
            "task_discovery_status": discovery_status,
            "reward_key": str(suite.get("reward_key") or "reward"),
            # Internal execution metadata stripped from public plan response
            "_dataset_path": suite.get("dataset_path"),
            "_config_path": suite.get("config_path"),
        }

    def plan_job(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        authorize(user, project_id=str(payload.get("project_id") or "default"), min_role="researcher")
        spec = self._validate_and_normalize_spec(user, payload)
        public_spec = {k: v for k, v in spec.items() if not k.startswith("_")}
        # Limit public preview task array to 100 items while exposing exact planned_task_count
        if len(public_spec["planned_tasks"]) > 100:
            public_spec["planned_tasks_truncated"] = True
            public_spec["planned_tasks"] = public_spec["planned_tasks"][:100]
        return {
            "ok": True,
            "methodology_version": self.settings.methodology_version,
            "plan": public_spec,
            "storage_boundary": {
                "raw_work_storage": "isolated_0700_private_runner_work",
                "restricted_evidence_storage": "isolated_0700_evidence_store",
                "shared_output_layer": "approved_public_summary_dto_only",
            },
            "runner_environment": self.probe_runner_health(),
            "docker_group_attached": is_docker_group_attached(),
        }

    # ------------------------------------------------------------------
    # Event Persistence, Redaction & Notifications (I-05)
    # ------------------------------------------------------------------
    def _emit_trusted_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        channel_path: Path | None = None,
        channel_secret: str | None = None,
        new_status: str | None = None,
        exit_code: int | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically persist a verified control-plane or Runner-IPC event with strictly monotonic (job_id, seq_id)."""
        now = utcnow_iso()
        clean_payload = json.loads(redact_text(json.dumps(payload, ensure_ascii=False)) or "{}")
        with self._lock:
            with self.db.transaction() as conn:
                row = conn.execute("SELECT last_event_seq FROM exec_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if row is None:
                    raise AppError(404, "RESOURCE_NOT_FOUND", f"作业不存在: {job_id}")
                seq_id = int(row["last_event_seq"]) + 1
                clean_payload["seq_id"] = seq_id
                clean_payload["job_id"] = job_id
                clean_payload.setdefault("ts", now)
                conn.execute(
                    """
                    INSERT INTO exec_job_events (job_id, seq_id, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, seq_id, event_type, json.dumps(clean_payload, ensure_ascii=False), now),
                )
                updates = ["last_event_seq = ?"]
                params: list[Any] = [seq_id]
                if new_status is not None:
                    updates.append("status = ?")
                    params.append(new_status)
                    if new_status == "running":
                        updates.append("started_at = COALESCE(started_at, ?)")
                        params.append(now)
                    elif new_status in TERMINAL_STATUSES:
                        updates.append("finished_at = ?")
                        params.append(now)
                if exit_code is not None:
                    updates.append("exit_code = ?")
                    params.append(exit_code)
                if batch_id is not None:
                    updates.append("batch_id = ?")
                    params.append(batch_id)
                params.append(job_id)
                conn.execute(f"UPDATE exec_jobs SET {', '.join(updates)} WHERE job_id = ?", tuple(params))
                if new_status in TERMINAL_STATUSES:
                    conn.execute("DELETE FROM exec_batch_reservations WHERE job_id = ?", (job_id,))

            if channel_path is not None and channel_secret is not None:
                sig = _sign_event_record(channel_secret, seq_id, event_type, clean_payload)
                rec = {
                    "seq_id": seq_id,
                    "event_type": event_type,
                    "payload": clean_payload,
                    "hmac_sha256": sig,
                }
                with channel_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                try:
                    os.chmod(channel_path, 0o600)
                except OSError:
                    pass

        with self._event_cond:
            self._event_cond.notify_all()
        return {"seq_id": seq_id, "event_type": event_type, "payload": clean_payload, "created_at": now}

    def _dispatch_terminal_notification(self, job_id: str, terminal_status: str, detail: dict[str, Any]) -> None:
        row = self.db.fetchone(
            "SELECT batch_name, notify_json, project_id, owner_id FROM exec_jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None:
            return
        notify = json.loads(row["notify_json"] or "{}")
        on_events = set(notify.get("on_events") or ["finished", "failed", "paused_by_guard"])
        if terminal_status not in on_events:
            return
        if notify.get("in_app", True):
            self._emit_trusted_event(
                job_id,
                "notification",
                {
                    "source": "control_plane",
                    "channel": "in_app",
                    "terminal_status": terminal_status,
                    "batch_name": row["batch_name"],
                    "message": f"作业 {row['batch_name']} 已进入终态 `{terminal_status}`",
                    "detail": detail,
                },
            )
        wh_id = notify.get("webhook_profile_id")
        if wh_id and self._webhook_dispatch_fn is not None:
            try:
                self._webhook_dispatch_fn(
                    str(wh_id),
                    {
                        "job_id": job_id,
                        "batch_name": row["batch_name"],
                        "status": terminal_status,
                        "detail": detail,
                    },
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Common Finalizer (v2.2.1 · I-05 / S-06 — ZERO 404 Reconciled!)
    # ------------------------------------------------------------------
    def _publish_public_dto_and_reconcile(
        self,
        *,
        job_id: str,
        spec: dict[str, Any],
        trial_outcomes: list[dict[str, Any]],
        evidence_dir: Path,
        channel_path: Path,
        channel_secret: str,
        publish_to_harbor_runs: bool = False,
        vlmevalkit_scoring_results: Any = None,
    ) -> dict[str, Any]:
        """Common Finalizer:
        1. Binds `generation_id` into every real `0700`/`0600` `evidence.json` produced by the isolated Runner
           and persists committed `(sha256, size)` in `0600 <work_dir>/.evidence-digests.json`.
        2. Writes ONLY approved lightweight public DTOs (`manifest.json`, `episodes.jsonl` with opaque
           `evidence_refs`, `batch-status.json`, `batch-report.json`, `run-config.json`, `trial-digests.jsonl`,
           atomic `publish-manifest.json` v2 under exclusive `.publish.lock`) to either:
           - `harbor_runs_dir/<batch>` (`source_namespace='harbor_runs'`) if explicitly requested & allowed, OR
           - `published_batches_dir/<batch>` (`source_namespace='runner_published'`, local ext4 `0750`) by default!
        3. Validates the published public DTO using managed `report.py` AND triggers `HotIndexer.scan_single_harbor_batch`.
        4. STRICTLY VERIFIES `scan_status in ('indexed', 'skipped_unchanged')` AND SQLite `batches` holds the exact
           `(batch_id, generation_id)` BEFORE emitting `reconciled` (eliminates 404 reconciled links completely!)."""
        scoring_files = {}
        if spec["kind"] == "vlmevalkit" and spec.get("vlmevalkit_mode") == "all":
            scoring_files = encode_vlmevalkit_results(vlmevalkit_scoring_results)
        batch_name = spec["batch_name"]
        gen_id = f"gen_{uuid.uuid4().hex[:12]}"
        if publish_to_harbor_runs:
            source_ns = "harbor_runs"
            out_root = self.settings.harbor_runs_dir
            if not out_root.is_dir():
                raise RuntimeError("作业预约的共享发布目录不可用")
        else:
            source_ns = "runner_published"
            assert self.settings.published_batches_dir is not None
            out_root = self.settings.published_batches_dir
            out_root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(out_root, 0o750)
            except OSError:
                pass

        reconciled_batch_id = make_batch_id(f"{source_ns}:{batch_name}")
        job_row = self.db.fetchone(
            """
            SELECT j.project_id, j.owner_id
            FROM exec_jobs j JOIN exec_batch_reservations r ON r.job_id = j.job_id
            WHERE j.job_id = ? AND j.batch_name = r.batch_name AND j.status = 'finalizing'
              AND r.source_namespace = ? AND r.batch_name = ?
            """,
            (job_id, source_ns, batch_name),
        )
        if job_row is None or job_row["project_id"] != spec["project_id"]:
            raise RuntimeError("缺少与作业和项目匹配的可信发布预约")
        eff_proj = job_row["project_id"]
        eff_owner = job_row["owner_id"]
        evidence_approval = "approved_restricted"
        out_dir = out_root / batch_name
        if out_dir.is_symlink():
            raise RuntimeError("发布目录不得为符号链接")
        official = vlmevalkit_scoring_results[OFFICIAL_RESULT_FILE] if OFFICIAL_RESULT_FILE in scoring_files else None
        pending = spec.get("pending_publication")
        if pending is not None:
            published = out_dir / "publish-manifest.json"
            if (published.is_symlink() or not published.is_file()
                    or _sha256_bytes(published.read_bytes()) != pending["manifest_sha256"]):
                raise RuntimeError("重启恢复时未找到已提交的同代发布清单，发布需要对账")
            return self._reconcile_published_job(
                job_id=job_id, out_dir=out_dir, source_namespace=source_ns,
                ownership=(eff_proj, eff_owner, evidence_approval), publication=pending,
                official=official, channel_path=channel_path, channel_secret=channel_secret,
            )
        out_dir.mkdir(exist_ok=spec.get("resume_mode") == "native")
        now = utcnow_iso()

        manifest = {
            "schema_version": 1,
            "dataset": spec["suite_display_name"],
            "harness": spec["agent"],
            "harness_version": spec["harness_version"],
            "runner": "harbor" if spec["kind"] in ("harbor", "harbor_gateway") else spec["kind"],
            "model": spec["model_route"],
            "batch": batch_name,
            "run_scope": spec["effective_run_scope"],
        }

        ep_rows: list[dict[str, Any]] = []
        digest_rows: list[dict[str, Any]] = []
        ev_digests_by_tid: dict[str, dict[str, Any]] = {}
        scored_vals: list[float] = []
        outcomes_cnt: dict[str, int] = {}
        task_groups: dict[str, list[dict[str, Any]]] = {}
        task_attempts_plan: dict[str, int] = {}
        recorded_task_ids = {str(tr["task_id"]) for tr in trial_outcomes}
        for tid_rec in sorted(recorded_task_ids):
            task_attempts_plan[tid_rec] = int(spec["attempts"])
        for tname in spec.get("planned_tasks") or []:
            ts = str(tname)
            # Harbor tasks may record `[task].name` (e.g. `tianxi-synthetic/<dir>`) in result.json;
            # do not double-count `<dir>` when `<prefix>/<dir>` is already recorded!
            if ts in recorded_task_ids or any(r.endswith(f"/{ts}") for r in recorded_task_ids):
                continue
            task_attempts_plan[ts] = task_attempts_plan.get(ts, 0) + int(spec["attempts"])

        for tr in trial_outcomes:
            tid = str(tr["trial_id"])
            st = str(tr["status"])
            outcomes_cnt[st] = outcomes_cnt.get(st, 0) + 1
            rw = tr.get("reward")
            if st == "scored" and isinstance(rw, (int, float)):
                scored_vals.append(float(rw))

            # Bind `generation_id` into authentic 0700/0600 evidence.json produced by the Runner
            t_ev_dir = evidence_dir / tid
            t_ev_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(t_ev_dir, 0o700)
            ev_file = t_ev_dir / "evidence.json"
            if ev_file.is_symlink():
                raise RuntimeError(f"Refusing symlinked trial evidence file: {tid}")
            if ev_file.is_file():
                restricted_doc = json.loads(ev_file.read_text(encoding="utf-8"))
                if not isinstance(restricted_doc, dict):
                    raise RuntimeError(f"Invalid trial evidence JSON object: {tid}")
            else:
                restricted_doc = {
                    "trial_id": tid,
                    "task_id": tr["task_id"],
                    "storage_tier": "isolated_0700_evidence_store",
                    "verifier_log": f"trial completed status={st} reward={rw}",
                }
            restricted_doc["trial_id"] = tid
            restricted_doc["task_id"] = str(tr["task_id"])
            restricted_doc["generation_id"] = gen_id
            restricted_doc["storage_tier"] = "isolated_0700_evidence_store"
            if "verifier_log" in restricted_doc and isinstance(restricted_doc["verifier_log"], str):
                restricted_doc["verifier_log"] = redact_text(restricted_doc["verifier_log"])
            ev_bytes = (json.dumps(restricted_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            ev_file.write_bytes(ev_bytes)
            os.chmod(ev_file, 0o600)
            ev_sha = _sha256_bytes(ev_bytes)
            ev_id = f"ev_{tid[:12]}"
            ev_digests_by_tid[tid] = {
                "evidence_id": ev_id,
                "generation_id": gen_id,
                "sha256": ev_sha,
                "size": len(ev_bytes),
            }

            # Approved lightweight public trial summary inside immutable generations/<gen_id>/trials/<tid>/
            res_rel_path = f"generations/{gen_id}/trials/{tid}/result.json"
            t_pub_dir = out_dir / "generations" / gen_id / "trials" / tid
            t_pub_dir.mkdir(parents=True, exist_ok=True)
            pub_res = {
                "id": tid,
                "task_name": tr["task_id"],
                "agent_info": {"name": spec["agent"], "version": spec["harness_version"]},
                "verifier_rewards": {"reward": rw} if (st == "scored" and rw is not None) else {},
                "exception_type": tr.get("exception_type"),
                "started_at": tr.get("started_at") or now,
                "finished_at": tr.get("finished_at") or now,
                "usage": tr.get("usage") or {"duration_seconds": tr.get("duration_sec", 0.05)},
            }
            res_bytes = (json.dumps(pub_res, ensure_ascii=False) + "\n").encode("utf-8")
            (t_pub_dir / "result.json").write_bytes(res_bytes)
            res_sha = _sha256_bytes(res_bytes)

            digest_rows.append(
                {
                    "trial_id": tid,
                    "generation_id": gen_id,
                    "result": {
                        "rel_path": res_rel_path,
                        "sha256": res_sha,
                        "size": len(res_bytes),
                        "storage_tier": "shared_public_dto",
                    },
                }
            )

            ep_item = {
                "schema_version": 1,
                "trial_id": tid,
                "dataset": spec["suite_display_name"],
                "task_id": tr["task_id"],
                "harness": spec["agent"],
                "harness_version": spec["harness_version"],
                "runner": manifest["runner"],
                "model_route": spec["model_route"],
                "status": st,
                "reward": rw if st == "scored" else None,
                "reward_valid": bool(st == "scored" and rw is not None),
                "verifier_rewards": pub_res["verifier_rewards"],
                "exception_type": tr.get("exception_type"),
                "usage": pub_res["usage"],
                "started_at": pub_res["started_at"],
                "finished_at": pub_res["finished_at"],
                "result": res_rel_path,
                "trajectory": None,
                "trajectory_status": "missing",
                "gateway_trace": None,
                "gateway_status": "not_captured",
                "training_trace": None,
                "training_status": "not_captured",
                "evidence_refs": {
                    "restricted_bundle": {
                        "evidence_id": ev_id,
                        "generation_id": gen_id,
                        "sha256": ev_sha,
                        "storage_tier": "evidence_store_0700",
                    }
                },
            }
            ep_rows.append(ep_item)
            task_groups.setdefault(str(tr["task_id"]), []).append(ep_item)
            if str(tr["task_id"]) not in task_attempts_plan:
                task_attempts_plan[str(tr["task_id"])] = int(spec["attempts"])

        # Persist committed evidence SHA-256 digests in 0700/0600 private runner_work_dir (fail-closed 409 check!)
        ev_digest_file = self.settings.runner_work_dir / job_id / ".evidence-digests.json"  # type: ignore[operator]
        ev_digest_file.write_text(
            json.dumps({"generation_id": gen_id, "trials": ev_digests_by_tid}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(ev_digest_file, 0o600)

        planned = sum(task_attempts_plan.values()) if task_attempts_plan else len(ep_rows)
        recorded = len(ep_rows)
        scored = len(scored_vals)
        pass_threshold = 1.0 if (scored_vals and all(x in (0, 1) for x in scored_vals)) else None
        valid_mean = round(sum(scored_vals) / scored, 6) if scored > 0 else None
        passed = sum(1 for v in scored_vals if v >= pass_threshold) if pass_threshold is not None else None
        if planned > recorded:
            outcomes_cnt["unrecorded"] = planned - recorded

        tasks_summary: dict[str, Any] = {}
        failure_kinds: dict[str, int] = {}
        for r in ep_rows:
            ex = r.get("exception_type")
            if ex:
                failure_kinds[str(ex)] = failure_kinds.get(str(ex), 0) + 1

        for tname in sorted(set(task_groups.keys()) | set(task_attempts_plan.keys())):
            trows = task_groups.get(tname, [])
            tscores = [float(r["reward"]) for r in trows if r["status"] == "scored" and isinstance(r.get("reward"), (int, float))]
            tout: dict[str, int] = {}
            for r in trows:
                tout[r["status"]] = tout.get(r["status"], 0) + 1
            tpl = task_attempts_plan.get(tname)
            if isinstance(tpl, int) and tpl > len(trows):
                tout["unrecorded"] = tpl - len(trows)
            tasks_summary[tname] = {
                "planned": tpl,
                "recorded": len(trows),
                "scored": len(tscores),
                "coverage": round(len(tscores) / tpl, 6) if (isinstance(tpl, int) and tpl > 0) else None,
                "reward_sum": float(sum(tscores)),
                "valid_mean": round(sum(tscores) / len(tscores), 6) if tscores else None,
                "passed": sum(1 for v in tscores if v >= pass_threshold) if pass_threshold is not None else None,
                "pass_threshold": pass_threshold,
                "outcomes": dict(sorted(tout.items())),
            }

        usage_summary: dict[str, Any] = {}
        for ukey in ("input_tokens", "output_tokens", "cost_usd", "duration_seconds"):
            uvals = [float((r.get("usage") or {}).get(ukey)) for r in ep_rows if isinstance((r.get("usage") or {}).get(ukey), (int, float))]
            usage_summary[ukey] = {"observed": len(uvals), "total": float(sum(uvals)) if uvals else None}

        batch_status_obj = {
            "status": "finished",
            "planned": planned,
            "completed": recorded,
            "task_attempts": task_attempts_plan,
            "runner_kind": spec["kind"],
            "vlmevalkit_mode": spec.get("vlmevalkit_mode"),
            "run_scope": spec["effective_run_scope"],
            "official_status": spec["effective_official_status"],
            "source": {
                "host": "127.0.0.1",
                "path": f"<redacted>/runner-work/{job_id}",
                "job_ref": hashlib.sha256(f"{job_id}:{batch_name}".encode("utf-8")).hexdigest()[:16],
            },
        }
        batch_report_obj = {
            "schema_version": 1,
            "batch": batch_name,
            "dataset": spec["suite_display_name"],
            "model": spec["model_route"],
            "harness": spec["agent"],
            "batch_status": "finished",
            "totals": {
                "planned": planned,
                "recorded": recorded,
                "scored": scored,
                "coverage": round(scored / planned, 6) if planned > 0 else None,
                "reward_sum": float(sum(scored_vals)),
                "valid_mean": valid_mean,
                "passed": passed,
                "pass_threshold": pass_threshold,
                "outcomes": dict(sorted(outcomes_cnt.items())),
            },
            "tasks": tasks_summary,
            "usage": usage_summary,
            "failure_kinds": dict(sorted(failure_kinds.items())),
        }
        run_config_obj = {
            "runner": spec["kind"],
            "suite_id": spec["suite_id"],
            "selection_mode": spec["selection_mode"],
            "vlmevalkit_mode": spec.get("vlmevalkit_mode"),
            "forced_infer_by_task_selection": spec.get("forced_infer_by_task_selection", False),
            "agent": spec["agent"],
            "harness_version": spec["harness_version"],
            "model_route": spec["model_route"],
            "gateway_profile_id": spec["gateway_profile_id"],
            "concurrent": spec["concurrent"],
            "attempts": spec["attempts"],
            "project_id": spec["project_id"],
        }

        top_files_bytes: dict[str, bytes] = {
            "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "episodes.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ep_rows).encode("utf-8"),
            "batch-status.json": (json.dumps(batch_status_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "batch-report.json": (json.dumps(batch_report_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "run-config.json": (json.dumps(run_config_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "trial-digests.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in digest_rows).encode("utf-8"),
            **scoring_files,
        }

        def _atomic_pub_write(dest: Path, data: bytes) -> None:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp_fh:
                tmp_p = Path(tmp_fh.name)
                try:
                    tmp_fh.write(data)
                    os.fchmod(tmp_fh.fileno(), 0o644)
                    tmp_fh.close()
                    tmp_p.replace(dest)
                finally:
                    tmp_p.unlink(missing_ok=True)

        with (out_dir / ".publish.lock").open("a") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            top_level_meta: dict[str, Any] = {}
            for fn, raw in top_files_bytes.items():
                path = out_dir / fn
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_pub_write(path, raw)
                entry: dict[str, Any] = {"sha256": _sha256_bytes(raw), "size": len(raw)}
                if fn == "episodes.jsonl":
                    entry["lines"] = len(ep_rows)
                elif fn == "trial-digests.jsonl":
                    entry["entries"] = len(digest_rows)
                top_level_meta[fn] = entry

            absent_opt = [fn for fn in (*VLM_SCORING_FILES, "selection.json") if fn not in top_level_meta]
            pub_manifest = {
                "schema_version": 2,
                "batch": batch_name,
                "generation_id": gen_id,
                "generation_root": f"generations/{gen_id}",
                "runner_job_id": job_id,
                "published_at": now,
                "top_level_files": top_level_meta,
                "absent_optional_files": absent_opt,
            }
            pub_bytes = (json.dumps(pub_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            publication = {
                "generation_id": gen_id,
                "manifest_sha256": _sha256_bytes(pub_bytes),
                "totals": batch_report_obj["totals"],
            }
            if source_ns == "runner_published":
                self._record_runner_submission_meta(
                    batch_name=batch_name,
                    batch_id=reconciled_batch_id,
                    job_id=job_id,
                    project_id=eff_proj,
                    owner_id=eff_owner,
                    generation_id=gen_id,
                    manifest_sha256=publication["manifest_sha256"],
                )
            with self.db.transaction() as conn:
                spec["pending_publication"] = publication
                conn.execute("UPDATE exec_jobs SET params_json = ? WHERE job_id = ?",
                             (json.dumps(spec, ensure_ascii=False), job_id))
            _atomic_pub_write(out_dir / "publish-manifest.json", pub_bytes)
        return self._reconcile_published_job(
            job_id=job_id, out_dir=out_dir, source_namespace=source_ns,
            ownership=(eff_proj, eff_owner, evidence_approval), publication=publication,
            official=official, channel_path=channel_path, channel_secret=channel_secret,
        )

    def _reconcile_published_job(
        self, *, job_id: str, out_dir: Path, source_namespace: str,
        ownership: tuple[str, str, str], publication: dict[str, Any], official: dict[str, Any] | None,
        channel_path: Path, channel_secret: str,
    ) -> dict[str, Any]:
        batch_name = out_dir.name
        batch_id = make_batch_id(f"{source_namespace}:{batch_name}")
        generation_id = publication["generation_id"]
        manifest_sha256 = publication["manifest_sha256"]
        snapshot_digest = f"sha256:{manifest_sha256}"
        project_id, owner_id, evidence_approval = ownership

        # Verify published public DTO via managed report.py before indexing (05-1 / I-05)
        report_py = resolve_verified_managed_script(self.settings, "report.py")
        rp_proc = subprocess.run(
            [sys.executable, "-B", str(report_py), str(out_dir), "--format", "json"],
            capture_output=True,
            text=True,
            env=build_sanitized_subprocess_env(),
            timeout=30,
            check=False,
        )
        if rp_proc.returncode != 0:
            raise RuntimeError(f"Managed report.py verification failed (rc={rp_proc.returncode}): {redact_text(rp_proc.stderr)}")

        # ALWAYS trigger single-batch HotIndexer and STRICTLY verify exact (batch_id, generation_id) in SQLite!
        scan_status = self.coordinator.hot.scan_single_harbor_batch(
            out_dir,
            force=True,
            source_namespace=source_namespace,
            project_override=project_id,
            owner_override=owner_id,
            approval_override=evidence_approval,
        )
        if scan_status not in ("indexed", "skipped_unchanged"):
            raise RuntimeError(f"HotIndexer 拒绝或未能索引已发布批次 (status={scan_status})，拒绝发出 reconciled")

        indexed_row = self.db.fetchone(
            """
            SELECT b.batch_id, b.generation_id, b.project_id, b.owner_id, b.evidence_approval,
                   json_extract(b.checks_json, '$.publish_manifest_sha256') AS manifest_sha256,
                   b.official_status, o.score, o.denominator
            FROM batches b LEFT JOIN official_results o ON o.batch_id = b.batch_id
            WHERE b.batch_id = ?
            """,
            (batch_id,),
        )
        if (indexed_row is None or indexed_row["generation_id"] != generation_id
                or indexed_row["manifest_sha256"] != manifest_sha256):
            raise RuntimeError(
                f"SQLite 对账校验失败: 批次 {batch_id} 的发布代或清单摘要不一致"
            )
        if (indexed_row["project_id"], indexed_row["owner_id"], indexed_row["evidence_approval"]) != ownership:
            raise RuntimeError("SQLite 发布项目、属主或审批状态对账失败")
        if official is not None:
            if (
                indexed_row["official_status"] != "official_verified"
                or indexed_row["score"] != official["score"]
                or indexed_row["denominator"] != official["denominator"]
            ):
                raise RuntimeError("SQLite 正式评分及分母对账失败")

        self._emit_trusted_event(
            job_id,
            "reconciled",
            {
                "source": "shared_snapshot",
                "source_namespace": source_namespace,
                "batch_id": batch_id,
                "batch_name": batch_name,
                "generation_id": generation_id,
                "status": "finished",
                "snapshot_digest": snapshot_digest,
                "totals": publication["totals"],
            },
            channel_path=channel_path,
            channel_secret=channel_secret,
            new_status="finished",
            exit_code=0,
            batch_id=batch_id,
        )
        return {
            "batch_id": batch_id,
            "generation_id": generation_id,
            "source_namespace": source_namespace,
            "snapshot_digest": snapshot_digest,
        }

    # ------------------------------------------------------------------
    # Trusted Control-Plane Submission Ledger (0700 state/runner-submissions)
    # ------------------------------------------------------------------
    def _record_runner_submission_meta(
        self,
        *,
        batch_name: str,
        batch_id: str,
        job_id: str,
        project_id: str,
        owner_id: str,
        generation_id: str,
        manifest_sha256: str,
    ) -> None:
        """Save the Runner batch binding for indexing and database recovery."""
        state_dir = self.settings.state_dir
        if state_dir is None:
            return
        doc = {
            "batch_name": batch_name,
            "batch_id": batch_id,
            "job_id": job_id,
            "project_id": project_id,
            "owner_id": owner_id,
            "evidence_approval": "approved_restricted",
            "source_namespace": "runner_published",
            "generation_id": generation_id,
            "manifest_sha256": manifest_sha256,
            "recorded_at": utcnow_iso(),
        }
        write_runner_submission(state_dir, doc)

    # ------------------------------------------------------------------
    # Submit / Cancel / Resume via External Isolated Runner IPC
    # ------------------------------------------------------------------
    def submit_job(
        self,
        user: AuthUser,
        payload: dict[str, Any],
        *,
        parent_job_id: str | None = None,
        resume_mode: str | None = None,
        harbor_work_dir: Path | None = None,
    ) -> dict[str, Any]:
        self.write_guard()
        if is_docker_group_attached():
            raise AppError(503, "RUNNER_UNAVAILABLE", "Web 进程禁止挂载 docker 用户组，请通过隔离 Runner 运行")
        probe_isolated_dir(self.settings.runner_work_dir, create=True)  # type: ignore[arg-type]
        probe_isolated_dir(self.settings.evidence_store_dir, create=True)  # type: ignore[arg-type]

        # Validate spec BEFORE or alongside Runner check so 422/403 schema/authz errors stay crisp!
        spec = self._validate_and_normalize_spec(user, payload)

        # Require live external Isolated Runner Daemon over authenticated IPC!
        rhealth = self.probe_runner_health()
        if not rhealth.get("isolated_runner_ready"):
            raise AppError(
                503,
                "RUNNER_NOT_ATTACHED",
                str(rhealth.get("reason") or "尚未接入独立隔离执行服务，拒绝在线提交作业"),
                {"runner_connected": bool(rhealth.get("runner_connected")), "runner_configured": bool(rhealth.get("runner_configured"))},
            )
        runner_instance_id = rhealth.get("runner_instance_id")
        if not isinstance(runner_instance_id, str) or not runner_instance_id:
            raise AppError(503, "RUNNER_PROTOCOL_MISMATCH", "隔离 Runner 未提供实例标识，请同代更新 Web 与 Runner")

        batch_name = spec["batch_name"]
        want_harbor_publish = bool(payload.get("publish_to_harbor_runs", False))
        if want_harbor_publish:
            if self.settings.production_mode and not self.settings.allow_harbor_runs_publish:
                raise AppError(
                    403,
                    "PUBLISH_TARGET_FORBIDDEN",
                    "生产模式默认禁止直接写入共享 harbor-runs 目录（需显式开启 LRM_ALLOW_HARBOR_RUNS_PUBLISH=true）；请使用默认隔离发布区",
                )
            if not self.settings.harbor_runs_dir.is_dir():
                raise AppError(503, "PUBLISH_TARGET_UNAVAILABLE", "共享发布目录不可用")
            source_namespace = "harbor_runs"
            publish_root = self.settings.harbor_runs_dir
        else:
            assert self.settings.published_batches_dir is not None
            source_namespace = "runner_published"
            publish_root = self.settings.published_batches_dir

        if spec["kind"] == "direct" and not spec.get("planned_tasks"):
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"Direct 题库 `{spec['suite_id']}` 未解析到有效任务，拒绝提交",
            )

        job_id = f"job_{uuid.uuid4().hex[:14]}"
        now = utcnow_iso()

        work_dir = self.settings.runner_work_dir / job_id  # type: ignore[operator]
        work_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(work_dir, 0o700)

        evidence_dir = self.settings.evidence_store_dir / job_id  # type: ignore[operator]
        evidence_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(evidence_dir, 0o700)

        channel_secret = uuid.uuid4().hex + uuid.uuid4().hex
        channel_path = work_dir / ".web-events.ndjson"
        channel_path.touch()
        os.chmod(channel_path, 0o600)

        effective_harbor_work = work_dir
        if resume_mode == "native" and harbor_work_dir is not None:
            if harbor_work_dir.is_symlink() or not harbor_work_dir.is_dir():
                raise AppError(409, "RESUME_UNSUPPORTED", "Harbor 原生续跑目标私有 --work 目录不存在或已损坏")
            resolved_hw = harbor_work_dir.resolve()
            try:
                resolved_hw.relative_to(self.settings.runner_work_dir.resolve())  # type: ignore[union-attr]
            except ValueError as exc:
                raise AppError(400, "INVALID_PATH_OR_ID", "Harbor 原生续跑 --work 目录越界") from exc
            effective_harbor_work = resolved_hw

        # Write Harbor native resume lock metadata in private 0700 --work dir
        lock_file = work_dir / ".resume-lock.json"
        lock_file.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "kind": spec["kind"],
                    "harness": spec["agent"],
                    "harness_version": spec["harness_version"],
                    "batch_name": batch_name,
                    "harbor_work_dir": str(effective_harbor_work),
                    "created_at": now,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(lock_file, 0o600)

        runner_spec = {
            **{k: v for k, v in spec.items() if not k.startswith("_")},
            "dataset_path": spec.get("_dataset_path"),
            "config_path": spec.get("_config_path"),
            "parent_job_id": parent_job_id,
            "resume_mode": resume_mode,
            "harbor_work_dir": str(effective_harbor_work),
            "completed_task_ids": [],
            "publish_to_harbor_runs": want_harbor_publish,
        }

        key_path = work_dir / ".web-channel.key"
        key_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(key_fd, "w", encoding="ascii") as key_file:
            key_file.write(channel_secret)
            key_file.flush()
            os.fsync(key_file.fileno())

        lease_fd = os.open(channel_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(lease_fd, "r+b") as lease:
            self._check_private_job_file(lease.fileno())
            fcntl.flock(lease, fcntl.LOCK_EX)
            runner_id = str(rhealth.get("runner_id") or "external-isolated-runner")
            stored_params = {k: v for k, v in runner_spec.items() if k not in ("dataset_path", "config_path")}
            stored_params["runner_instance_id"] = runner_instance_id
            stored_params["dispatch_started"] = False
            try:
                with self.db.transaction() as conn:
                    existing = conn.execute(
                        """
                        SELECT 1 FROM exec_batch_reservations WHERE source_namespace = ? AND batch_name = ?
                        UNION ALL
                        SELECT 1 FROM exec_jobs
                        WHERE batch_name = ?
                          AND status IN ('queued', 'running', 'cancelling', 'finalizing', 'finished')
                          AND (CASE WHEN json_valid(params_json)
                                    THEN coalesce(json_extract(params_json, '$.publish_to_harbor_runs'), 0)
                                    ELSE 0 END) = ?
                        LIMIT 1
                        """,
                        (source_namespace, batch_name, batch_name, int(want_harbor_publish)),
                    ).fetchone()
                    target = publish_root / batch_name
                    if existing is not None or (resume_mode != "native" and (target.exists() or target.is_symlink())):
                        raise AppError(409, "BATCH_ALREADY_EXISTS", "目标发布位置已有作业或历史批次", {"batch_name": batch_name})

                    job_owner = user.user_id
                    if resume_mode == "native":
                        parent = conn.execute(
                            """
                            SELECT project_id, owner_id, params_json FROM exec_jobs
                            WHERE job_id = ? AND batch_name = ? AND kind = 'harbor'
                              AND status IN ('paused_by_guard', 'interrupted', 'failed')
                            """,
                            (parent_job_id, batch_name),
                        ).fetchone()
                        if (
                            parent is None
                            or parent["project_id"] != spec["project_id"]
                            or bool(json.loads(parent["params_json"]).get("publish_to_harbor_runs", False)) != want_harbor_publish
                        ):
                            raise AppError(409, "RESUME_UNSUPPORTED", "原生续跑必须继承原作业发布位置与项目")
                        job_owner = parent["owner_id"]

                    conn.execute(
                        """
                        INSERT INTO exec_jobs (
                            job_id, kind, project_id, owner_id, batch_name, status,
                            params_json, command_preview, runner_id, log_ref, notify_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id, spec["kind"], spec["project_id"], job_owner, batch_name,
                            json.dumps(stored_params, ensure_ascii=False), spec["command_preview"], runner_id,
                            f"runner_work:{job_id}/runner_stdio.log",
                            json.dumps(spec["notify"], ensure_ascii=False), now,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO exec_batch_reservations (source_namespace, batch_name, job_id) VALUES (?, ?, ?)",
                        (source_namespace, batch_name, job_id),
                    )
            except Exception:
                shutil.rmtree(work_dir, ignore_errors=True)
                shutil.rmtree(evidence_dir, ignore_errors=True)
                raise

            planned_total = (len(spec["planned_tasks"]) * spec["attempts"]) if spec["planned_tasks"] else 0
            self._emit_trusted_event(
                job_id,
                "job_queued",
                {
                    "source": "runner_live",
                    "runner_id": runner_id,
                    "kind": spec["kind"],
                    "batch_name": batch_name,
                    "run_scope": spec["effective_run_scope"],
                    "vlmevalkit_mode": spec.get("vlmevalkit_mode"),
                    "forced_infer_by_task_selection": spec.get("forced_infer_by_task_selection", False),
                    "progress": {"planned": planned_total, "completed": 0, "scored": 0, "errors": 0},
                },
                channel_path=channel_path,
                channel_secret=channel_secret,
            )

            # Persist the dispatch intent before sending anything to Runner.
            stored_params["dispatch_started"] = True
            with self.db.transaction() as conn:
                conn.execute("UPDATE exec_jobs SET params_json = ? WHERE job_id = ?",
                             (json.dumps(stored_params, ensure_ascii=False), job_id))
            try:
                ipc_status, ipc_body = self.ipc.submit_job(
                    {
                        "job_id": job_id,
                        "runner_instance_id": runner_instance_id,
                        "channel_secret": channel_secret,
                        "spec": runner_spec,
                    }
                )
            except AppError:
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM exec_jobs WHERE job_id = ?", (job_id,))
                shutil.rmtree(work_dir, ignore_errors=True)
                shutil.rmtree(evidence_dir, ignore_errors=True)
                raise
            except (OSError, HTTPException):
                # A lost POST reply can leave an accepted job on Runner. Keep its reservation
                # until the authenticated event monitor establishes a terminal outcome.
                ipc_status, ipc_body = 202, {}

            definite_rejection = (
                ipc_body.get("code") in {
                    "AUTH_REQUIRED", "GATEWAY_KEY_MISSING", "INVALID_JSON", "JOB_DISPATCH_ABANDONED",
                    "RESOURCE_NOT_FOUND", "RESUME_UNSUPPORTED", "RUNNER_CAPABILITY_UNAVAILABLE",
                    "RUNNER_STDIO_CAPACITY_EXHAUSTED", "SCHEMA_VALIDATION_FAILED", "STAGING_PERMISSION_UNSAFE",
                }
            )
            if definite_rejection:
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM exec_jobs WHERE job_id = ?", (job_id,))
                shutil.rmtree(work_dir, ignore_errors=True)
                shutil.rmtree(evidence_dir, ignore_errors=True)
                raise AppError(
                    ipc_status if 400 <= ipc_status < 600 else 503,
                    str(ipc_body.get("code") or "RUNNER_REJECTED"),
                    str(ipc_body.get("message") or f"隔离 Runner 拒绝执行作业 (HTTP {ipc_status})"),
                    ipc_body.get("detail") if isinstance(ipc_body.get("detail"), dict) else None,
                )

            reply_runner_id = ipc_body.get("runner_id") if ipc_body.get("runner_instance_id") == runner_instance_id else None
            if isinstance(reply_runner_id, str) and reply_runner_id and reply_runner_id != runner_id:
                runner_id = reply_runner_id
                with self.db.transaction() as conn:
                    conn.execute("UPDATE exec_jobs SET runner_id = ? WHERE job_id = ?", (runner_id, job_id))

            # The monitor retains the same flock after this request closes its descriptor.
            monitor_fd = os.dup(lease.fileno())
            t = threading.Thread(
                target=self._monitor_runner_job,
                args=(monitor_fd, job_id, stored_params, channel_path, channel_secret),
                name=f"lrm-ipc-sync-{job_id}", daemon=True,
            )
            try:
                t.start()
            except BaseException:
                os.close(monitor_fd)
                raise

            return {
                "job_id": job_id,
                "status": "queued",
                "runner_id": runner_id,
                "kind": spec["kind"],
                "batch_name": batch_name,
                "vlmevalkit_mode": spec.get("vlmevalkit_mode"),
                "forced_infer_by_task_selection": spec.get("forced_infer_by_task_selection", False),
                "effective_run_scope": spec["effective_run_scope"],
                "effective_official_status": spec["effective_official_status"],
                "command_preview": spec["command_preview"],
                "stream_url": f"/api/v1/execution/jobs/{job_id}/stream",
                "warnings": spec["warnings"],
            }

    def _monitor_runner_job(
        self, lease_fd: int, job_id: str, spec: dict[str, Any], channel_path: Path, channel_secret: str,
    ) -> None:
        try:
            self._sync_runner_job_events(job_id, spec, channel_path, channel_secret)
        finally:
            os.close(lease_fd)

    def _sync_runner_job_events(
        self,
        job_id: str,
        spec: dict[str, Any],
        channel_path: Path,
        channel_secret: str,
    ) -> None:
        """Consume ONLY HMAC-authenticated events from the external Isolated Runner Daemon via IPC,
        persist monotonic seq_ids into SQLite, and invoke Common Finalizer upon completion."""
        runner_done = False
        assert self.settings.evidence_store_dir is not None
        try:
            row = self.db.fetchone(
                """
                SELECT coalesce(max(json_extract(payload_json, '$.runner_seq')), 0) AS runner_cursor
                FROM exec_job_events WHERE job_id = ?
                """,
                (job_id,),
            )
            runner_cursor = int(row["runner_cursor"]) if row is not None else 0
            while not self._stopping.is_set():
                try:
                    code, body = self.ipc.get_events(job_id, after_seq=runner_cursor, timeout=5.0)
                    if code == 404:
                        fence_status, fence_body = self.ipc.recover_job(job_id)
                        if self._confirmed_runner_absence(fence_status, fence_body, job_id, spec):
                            self._interrupt_missing_runner_job(job_id, channel_path, channel_secret)
                            return
                except (AppError, OSError, HTTPException):
                    self._stopping.wait(0.5)
                    continue

                if code != 200 or body.get("job_id") != job_id:
                    self._stopping.wait(0.5)
                    continue
                if self._stopping.is_set():
                    return

                events = body.get("events") or []
                for rec in events:
                    if not _verify_runner_event_hmac(channel_secret, rec):
                        raise RuntimeError("Rejected Runner IPC event with invalid HMAC-SHA256 signature")
                    rseq = int(rec["runner_seq"])
                    if rseq <= runner_cursor:
                        continue
                    runner_cursor = rseq
                    etype = str(rec["event_type"])
                    payload = dict(rec["payload"])

                    if etype == "job_started":
                        self._emit_trusted_event(
                            job_id,
                            "job_started",
                            payload,
                            channel_path=channel_path,
                            channel_secret=channel_secret,
                            new_status="running",
                        )
                    elif etype == "trial_finished":
                        self._emit_trusted_event(
                            job_id,
                            "trial_finished",
                            payload,
                            channel_path=channel_path,
                            channel_secret=channel_secret,
                        )
                    elif etype in ("paused_by_guard", "job_interrupted", "job_failed"):
                        runner_done = True
                        done_tasks = payload.get("completed_task_ids") or []
                        if done_tasks:
                            spec["completed_task_ids"] = list(done_tasks)
                            with self.db.transaction() as conn:
                                conn.execute(
                                    "UPDATE exec_jobs SET params_json = ? WHERE job_id = ?",
                                    (json.dumps(spec, ensure_ascii=False), job_id),
                                )
                        new_st = (
                            "paused_by_guard"
                            if etype == "paused_by_guard"
                            else ("interrupted" if etype == "job_interrupted" else "failed")
                        )
                        rc = int(payload.get("exit_code") if payload.get("exit_code") is not None else (75 if new_st == "paused_by_guard" else 1))
                        self._emit_trusted_event(
                            job_id,
                            etype,
                            payload,
                            channel_path=channel_path,
                            channel_secret=channel_secret,
                            new_status=new_st,
                            exit_code=rc,
                        )
                        self._dispatch_terminal_notification(job_id, new_st, {"exit_code": rc})
                        return
                    elif etype == "runner_completed":
                        runner_done = True
                        done_tasks = payload.get("completed_task_ids") or []
                        spec["completed_task_ids"] = list(done_tasks)
                        with self.db.transaction() as conn:
                            conn.execute(
                                "UPDATE exec_jobs SET status = 'finalizing', params_json = ? WHERE job_id = ?",
                                (json.dumps(spec, ensure_ascii=False), job_id),
                            )
                        trial_outcomes = list(payload.get("trial_outcomes") or [])
                        rec_info = self._publish_public_dto_and_reconcile(
                            job_id=job_id,
                            spec=spec,
                            trial_outcomes=trial_outcomes,
                            evidence_dir=self.settings.evidence_store_dir / job_id,
                            channel_path=channel_path,
                            channel_secret=channel_secret,
                            publish_to_harbor_runs=bool(spec.get("publish_to_harbor_runs")),
                            vlmevalkit_scoring_results=payload.get("vlmevalkit_scoring_results"),
                        )
                        self._dispatch_terminal_notification(job_id, "finished", rec_info)
                        return

                self._stopping.wait(0.05)
        except Exception as exc:
            try:
                if self.db.fetchone("SELECT 1 FROM exec_batch_reservations WHERE job_id = ?", (job_id,)) is None:
                    return
                reason = redact_text(str(exc)) or "Runner reconciliation error"
                if not runner_done:
                    self._emit_trusted_event(
                        job_id, "job_recovery_pending", {"source": "control_plane", "reason": reason},
                        channel_path=channel_path, channel_secret=channel_secret,
                    )
                    return
                self._emit_trusted_event(
                    job_id,
                    "job_failed",
                    {"source": "control_plane", "reason": reason, "exit_code": 1},
                    channel_path=channel_path,
                    channel_secret=channel_secret,
                    new_status="failed",
                    exit_code=1,
                )
                self._dispatch_terminal_notification(job_id, "failed", {"reason": "finalizer_error"})
            except Exception:
                pass
        finally:
            self.db.close_thread_connection()

    def cancel_job(self, user: AuthUser, job_id: str) -> dict[str, Any]:
        validate_safe_id(job_id, "job_id")
        self.write_guard()
        row = self.db.fetchone("SELECT * FROM exec_jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"执行作业不存在: {job_id}")
        authorize(user, project_id=row["project_id"], owner_id=row["owner_id"], min_role="researcher")

        if row["status"] in TERMINAL_STATUSES:
            return {"job_id": job_id, "status": row["status"], "note": "作业已处于终态"}

        rhealth = self.probe_runner_health()
        if not rhealth.get("isolated_runner_ready"):
            raise AppError(503, "RUNNER_NOT_ATTACHED", "隔离执行服务未连接，无法中止作业")

        try:
            code, body = self.ipc.cancel_job(job_id)
        except OSError as exc:
            raise AppError(503, "RUNNER_NOT_ATTACHED", f"无法向隔离 Runner 发送中止信号: {type(exc).__name__}") from exc
        if code not in (200, 202, 404):
            raise AppError(503, "RUNNER_CANCEL_FAILED", str(body.get("message") or f"Runner cancel failed (HTTP {code})"))

        self._emit_trusted_event(
            job_id,
            "job_cancelling",
            {"source": "control_plane", "requested_by": user.username},
            new_status="cancelling",
        )
        return {"job_id": job_id, "status": "cancelling", "message": "已向隔离 Runner 发送优雅中止信号 (SIGTERM)，等待排空"}

    def resume_job(self, user: AuthUser, job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        validate_safe_id(job_id, "job_id")
        self.write_guard()
        body = payload or {}
        row = self.db.fetchone("SELECT * FROM exec_jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"执行作业不存在: {job_id}")
        authorize(user, project_id=row["project_id"], owner_id=row["owner_id"], min_role="researcher")

        kind = str(row["kind"])
        resume_mode = str(body.get("resume_mode") or "native").strip().lower()
        if resume_mode not in ("native", "derived_task_subset"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "resume_mode 仅支持 native 或 derived_task_subset")

        # S-04 / 04-2 §1 hard rule: native --resume is ONLY supported for `harbor`!
        if resume_mode == "native" and kind != "harbor":
            raise AppError(
                409,
                "RESUME_UNSUPPORTED",
                f"运行器 `{kind}` 原生不支持 --resume（仅 Harbor 标准模式支持原生续跑；可使用 resume_mode='derived_task_subset' 发起 M3 任务级派生续跑）",
                {"kind": kind, "supported_resume_mode": "derived_task_subset"},
            )

        if row["status"] not in ("paused_by_guard", "interrupted", "failed"):
            raise AppError(
                409,
                "RESUME_UNSUPPORTED",
                f"当前状态 `{row['status']}` 不允许续跑（仅 paused_by_guard / interrupted / failed 可续跑）",
                {"status": row["status"]},
            )

        orig_params = json.loads(row["params_json"] or "{}")
        orig_harbor_work: Path | None = None
        if resume_mode == "native":
            raw_hw = orig_params.get("harbor_work_dir")
            orig_harbor_work = Path(str(raw_hw)) if raw_hw else (self.settings.runner_work_dir / job_id)  # type: ignore[operator]
            lock_file = orig_harbor_work / ".resume-lock.json"
            if lock_file.is_symlink() or not lock_file.is_file():
                raise AppError(
                    409,
                    "RESUME_UNSUPPORTED",
                    "私有 0700 --work 续跑锁已丢失或损坏，无法执行 Harbor 原生 --resume",
                )
            try:
                lock_doc = json.loads(lock_file.read_text(encoding="utf-8"))
                if not isinstance(lock_doc, dict):
                    raise ValueError("invalid lock doc")
            except (OSError, ValueError) as exc:
                raise AppError(
                    409,
                    "RESUME_UNSUPPORTED",
                    "私有 0700 --work 续跑锁已损坏，无法执行 Harbor 原生 --resume",
                ) from exc
            req_ver = str(body.get("harness_version") or orig_params.get("harness_version") or "")
            if req_ver != str(lock_doc.get("harness_version") or ""):
                raise AppError(
                    409,
                    "RESUME_UNSUPPORTED",
                    f"Harbor 原生 --resume 要求锁定相同 harness_version（原 {lock_doc.get('harness_version')} vs 请求 {req_ver}）",
                )
            # Also verify native run.py batch-lock if present in the original --work directory
            native_lock = orig_harbor_work / "batch-locks" / f"{orig_params['batch_name']}.json"
            if native_lock.is_symlink():
                raise AppError(409, "RESUME_UNSUPPORTED", "Harbor 原生 batch-locks 锁文件为非法符号链接")
            if native_lock.is_file():
                try:
                    nlock_doc = json.loads(native_lock.read_text(encoding="utf-8"))
                    if not isinstance(nlock_doc, dict) or str(nlock_doc.get("harness_version") or "") != req_ver:
                        raise AppError(409, "RESUME_UNSUPPORTED", "Harbor 原生 batch-locks harness_version 不一致")
                except AppError:
                    raise
                except (OSError, ValueError) as exc:
                    raise AppError(409, "RESUME_UNSUPPORTED", "Harbor 原生 batch-locks 锁文件已损坏") from exc

        done = set(orig_params.get("completed_task_ids") or [])
        all_tasks = list(orig_params.get("planned_tasks") or [])
        remaining = [t for t in all_tasks if t not in done] or all_tasks

        if resume_mode == "native":
            # Native Harbor --resume MUST preserve EXACT original batch_name, selection_mode, tasks, and --work!
            new_batch_name = orig_params["batch_name"]
            eff_sel_mode = orig_params.get("selection_mode", "tasks" if orig_params.get("tasks") else "all")
            eff_tasks = list(orig_params.get("tasks") or [])
        else:
            new_batch_name = f"{orig_params['batch_name']}-cont-{uuid.uuid4().hex[:6]}"
            eff_sel_mode = "tasks" if remaining else orig_params.get("selection_mode", "all")
            eff_tasks = remaining

        new_payload = {
            "kind": kind,
            "suite_id": orig_params["suite_id"],
            "selection_mode": eff_sel_mode,
            "tasks": eff_tasks,
            "vlmevalkit_mode": orig_params.get("vlmevalkit_mode") or "all",
            "model_route": orig_params["model_route"],
            "gateway_profile_id": orig_params["gateway_profile_id"],
            "judge_profile_id": orig_params.get("judge_profile_id"),
            "agent": orig_params.get("agent"),
            "harness_version": orig_params.get("harness_version"),
            "concurrent": orig_params.get("concurrent", 2),
            "attempts": orig_params.get("attempts", 1),
            "pause_after": orig_params.get("pause_after", 3),
            "batch_name": new_batch_name,
            "project_id": row["project_id"],
            "notify": orig_params.get("notify") or {},
            "publish_to_harbor_runs": orig_params.get("publish_to_harbor_runs", False),
        }
        res = self.submit_job(
            user,
            new_payload,
            parent_job_id=job_id,
            resume_mode=resume_mode,
            harbor_work_dir=orig_harbor_work,
        )
        res["parent_job_id"] = job_id
        res["resume_mode"] = resume_mode
        res["remaining_tasks"] = remaining
        if orig_harbor_work is not None:
            res["reused_original_work_dir"] = True
        return res

    # ------------------------------------------------------------------
    # Queries, SSE Replay Stream & Fail-Closed Evidence Read (I-05 / S-06)
    # ------------------------------------------------------------------
    def _row_to_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        params = json.loads(row["params_json"] or "{}")
        return {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "project_id": row["project_id"],
            "owner_id": row["owner_id"],
            "batch_name": row["batch_name"],
            "batch_id": row["batch_id"],
            "status": row["status"],
            "suite_id": params.get("suite_id"),
            "suite_display_name": params.get("suite_display_name"),
            "model_route": params.get("model_route"),
            "agent": params.get("agent"),
            "harness_version": params.get("harness_version"),
            "vlmevalkit_mode": params.get("vlmevalkit_mode"),
            "forced_infer_by_task_selection": bool(params.get("forced_infer_by_task_selection", False)),
            "effective_run_scope": params.get("effective_run_scope"),
            "effective_official_status": params.get("effective_official_status"),
            "native_resume_supported": row["kind"] == "harbor",
            "parent_job_id": params.get("parent_job_id"),
            "resume_mode": params.get("resume_mode"),
            "command_preview": redact_text(row["command_preview"]),
            "runner_id": row["runner_id"],
            "exit_code": row["exit_code"],
            "last_event_seq": int(row["last_event_seq"] or 0),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def _is_holdout_job_row(self, row: dict[str, Any]) -> bool:
        try:
            params = json.loads(row.get("params_json") or "{}")
        except Exception:
            params = {}
        sid = str(params.get("suite_id") or "").lower()
        return "holdout" in sid

    def _require_holdout_if_needed(self, user: AuthUser, row: dict[str, Any]) -> None:
        if self._is_holdout_job_row(row) and not has_scope(user, "dataset:holdout_read"):
            raise AppError(
                403,
                "SCOPE_FORBIDDEN",
                "缺少 dataset:holdout_read 权限，禁止访问留出题库执行作业或其证据",
                {"missing_scope": "dataset:holdout_read"},
            )

    def list_jobs(self, user: AuthUser, *, limit: int = 50) -> dict[str, Any]:
        where, params = sql_project_filter(user, "project_id")
        rows = self.db.fetchall(
            f"SELECT * FROM exec_jobs WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )
        if not has_scope(user, "dataset:holdout_read"):
            rows = [r for r in rows if not self._is_holdout_job_row(r)]
        return {
            "methodology_version": self.settings.methodology_version,
            "items": [self._row_to_summary(r) for r in rows],
            "total": len(rows),
        }

    def get_job_detail(self, user: AuthUser, job_id: str) -> dict[str, Any]:
        validate_safe_id(job_id, "job_id")
        row = self.db.fetchone("SELECT * FROM exec_jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"执行作业不存在: {job_id}")
        authorize(user, project_id=row["project_id"], owner_id=row["owner_id"], min_role="viewer")
        self._require_holdout_if_needed(user, row)

        events_rows = self.db.fetchall(
            "SELECT seq_id, event_type, payload_json, created_at FROM exec_job_events WHERE job_id = ? ORDER BY seq_id ASC",
            (job_id,),
        )
        events = [
            {
                "seq_id": int(e["seq_id"]),
                "event_type": e["event_type"],
                "payload": json.loads(e["payload_json"] or "{}"),
                "created_at": e["created_at"],
            }
            for e in events_rows
        ]

        trials_live = [e["payload"] for e in events if e["event_type"] == "trial_finished"]
        reconciled_evt = next((e["payload"] for e in reversed(events) if e["event_type"] == "reconciled"), None)
        latest_progress = {"planned": 0, "completed": 0, "scored": 0, "errors": 0}
        for e in reversed(events):
            if isinstance(e["payload"].get("progress"), dict):
                latest_progress = e["payload"]["progress"]
                break

        log_path = self.settings.runner_work_dir / job_id / "runner_stdio.log"  # type: ignore[operator]
        raw_lines, stdio_log_truncated = read_bounded_tail_lines(
            log_path,
            max_read_bytes=MAX_JOB_DETAIL_STDIO_READ_BYTES,
            max_lines=200,
        )
        log_lines = [redact_text(ln) or "" for ln in raw_lines]

        summary = self._row_to_summary(row)
        return {
            **summary,
            "live_channel": {
                "source": "runner_live",
                "last_seq_id": summary["last_event_seq"],
                "progress": latest_progress,
                "trials": trials_live,
                "trusted_channel_note": "仅由独立隔离 Runner 的 HMAC 可信事件通道驱动；普通 stdout/stderr 绝不参与状态机或进度统计",
            },
            "shared_snapshot": {
                "reconciled": reconciled_evt is not None,
                "batch_id": reconciled_evt.get("batch_id") if reconciled_evt else row["batch_id"],
                "generation_id": reconciled_evt.get("generation_id") if reconciled_evt else None,
                "snapshot_digest": reconciled_evt.get("snapshot_digest") if reconciled_evt else None,
                "totals": reconciled_evt.get("totals") if reconciled_evt else None,
            },
            "events": events,
            "redacted_stdio_logs": log_lines,
            "stdio_log_truncated": stdio_log_truncated,
            "stdio_log_cap_bytes": MAX_JOB_STDIO_BYTES,
            "telemetry": {
                "cgroup": _read_cgroup_telemetry(),
                "streaming_ttft_tps": {"source": "unavailable", "ttft_ms": None, "tps": None},
            },
        }

    def read_events_after(self, user: AuthUser, job_id: str, last_event_id: int) -> tuple[str, list[dict[str, Any]]]:
        validate_safe_id(job_id, "job_id")
        eff_user = user
        if user.role != "admin":
            u_row = self.db.fetchone(
                "SELECT is_active, projects_json, scopes_json FROM users WHERE user_id = ?",
                (user.user_id,),
            )
            if u_row is None or not u_row["is_active"]:
                raise AppError(401, "AUTH_REQUIRED", "账号已停用或会话失效")
            eff_user = AuthUser(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                scopes=frozenset(json.loads(u_row["scopes_json"] or "[]")),
                projects=frozenset(json.loads(u_row["projects_json"] or "[]")),
                is_active=bool(u_row["is_active"]),
            )
        row = self.db.fetchone("SELECT project_id, owner_id, status, params_json FROM exec_jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"执行作业不存在: {job_id}")
        authorize(eff_user, project_id=row["project_id"], owner_id=row["owner_id"], min_role="viewer")
        self._require_holdout_if_needed(eff_user, row)

        rows = self.db.fetchall(
            """
            SELECT seq_id, event_type, payload_json, created_at
            FROM exec_job_events
            WHERE job_id = ? AND seq_id > ?
            ORDER BY seq_id ASC
            """,
            (job_id, max(0, int(last_event_id))),
        )
        events = [
            {
                "seq_id": int(r["seq_id"]),
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return str(row["status"]), events

    def get_authorized_trial_evidence(self, user: AuthUser, job_id: str, trial_id: str) -> dict[str, Any]:
        """Serve restricted trial evidence from 0700 isolated evidence_store ONLY after:
        1. Project + Scope + Holdout authorization (S-02 / S-06), AND
        2. STRICT FAIL-CLOSED verification against committed `generation_id` (batches + reconciled),
           `0600 .evidence-digests.json`, exact trial digest entry (`sha256` + `size`), and file contents (I-01 / S-06).
           Missing generation_id, missing digest file, missing trial entry, or mismatch ALWAYS returns `409 GENERATION_MISMATCH`!"""
        validate_safe_id(job_id, "job_id")
        validate_safe_id(trial_id, "trial_id")
        row = self.db.fetchone(
            "SELECT project_id, owner_id, kind, batch_id, status, params_json FROM exec_jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"执行作业不存在: {job_id}")
        req_scope = "direct_vlm_qa:read" if row["kind"] in ("direct", "vlmevalkit") else "trace:read"
        authorize(user, project_id=row["project_id"], owner_id=row["owner_id"], min_role="viewer", required_scope=req_scope)
        self._require_holdout_if_needed(user, row)

        # 1. Resolve committed generation_id strictly from SQLite batches + reconciled event (FAIL CLOSED!)
        batch_id = row.get("batch_id")
        if not batch_id:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "作业尚未完成 Common Finalizer 发布与 SQLite 入库对账，拒绝返回未提交发布代的受限证据",
                {"job_id": job_id, "trial_id": trial_id, "reason": "missing_committed_batch"},
            )
        b_row = self.db.fetchone("SELECT generation_id FROM batches WHERE batch_id = ?", (batch_id,))
        committed_gen_id = str(b_row["generation_id"]).strip() if (b_row and b_row.get("generation_id")) else ""
        if not committed_gen_id:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "已索引批次缺少已提交发布代 generation_id，拒绝返回受限证据",
                {"job_id": job_id, "trial_id": trial_id, "reason": "missing_committed_generation_id"},
            )

        # 2. Verify 0600 .evidence-digests.json exists, is non-symlink, matches committed_gen_id, and covers trial_id!
        ev_digest_file = self.settings.runner_work_dir / job_id / ".evidence-digests.json"  # type: ignore[operator]
        if ev_digest_file.is_symlink() or not ev_digest_file.is_file():
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "作业私有证据摘要清册 (.evidence-digests.json) 缺失或为符号链接，拒绝返回受限证据",
                {"job_id": job_id, "trial_id": trial_id, "reason": "missing_evidence_digests_manifest"},
            )
        try:
            dig_doc = json.loads(ev_digest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AppError(409, "GENERATION_MISMATCH", "作业证据摘要清册损坏，拒绝返回受限证据") from exc
        if not isinstance(dig_doc, dict) or str(dig_doc.get("generation_id") or "") != committed_gen_id:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "证据摘要清册发布代与 SQLite 已提交 generation_id 不一致，拒绝返回",
                {"job_id": job_id, "trial_id": trial_id, "reason": "digest_generation_mismatch"},
            )
        trials_dig = dig_doc.get("trials")
        t_dig = trials_dig.get(trial_id) if isinstance(trials_dig, dict) else None
        if (
            not isinstance(t_dig, dict)
            or not isinstance(t_dig.get("sha256"), str)
            or not HEX64_RE.fullmatch(t_dig["sha256"])
            or not isinstance(t_dig.get("size"), int)
            or t_dig["size"] <= 0
            or str(t_dig.get("generation_id") or "") != committed_gen_id
        ):
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "证据摘要清册缺少当前试验的合法同代摘要项，拒绝返回受限证据",
                {"job_id": job_id, "trial_id": trial_id, "reason": "missing_trial_digest_entry"},
            )

        # 3. Verify evidence.json path containment, non-symlink, size, SHA-256, and inner generation_id!
        ev_raw_path = self.settings.evidence_store_dir / job_id / trial_id / "evidence.json"  # type: ignore[operator]
        if ev_raw_path.is_symlink():
            raise AppError(400, "INVALID_PATH_OR_ID", "拒绝符号链接受限证据文件")
        ev_file = ev_raw_path.resolve()
        try:
            ev_file.relative_to(self.settings.evidence_store_dir.resolve())  # type: ignore[union-attr]
        except ValueError as exc:
            raise AppError(400, "INVALID_PATH_OR_ID", "非法证据路径") from exc
        if not ev_file.is_file():
            raise AppError(404, "RESOURCE_NOT_FOUND", f"受限证据不存在: {trial_id}")

        raw_bytes = ev_file.read_bytes()
        if len(raw_bytes) != int(t_dig["size"]) or _sha256_bytes(raw_bytes) != t_dig["sha256"]:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "受限证据文件字节摘要或大小与已提交发布代不一致，拒绝返回",
                {"trial_id": trial_id, "reason": "evidence_sha256_mismatch"},
            )

        try:
            doc = json.loads(raw_bytes.decode("utf-8"))
        except ValueError as exc:
            raise AppError(409, "GENERATION_MISMATCH", "受限证据 JSON 损坏，拒绝返回") from exc
        if not isinstance(doc, dict) or str(doc.get("generation_id") or "") != committed_gen_id:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "受限证据内容发布代与已提交 generation_id 不一致，拒绝返回",
                {"trial_id": trial_id, "reason": "evidence_inner_generation_mismatch"},
            )

        if not has_scope(user, "verifier_log:read") and "verifier_log" in doc:
            doc["verifier_log"] = "<redacted: requires verifier_log:read>"
            doc["verifier_log_redacted"] = True
        doc = redact_json_recursive(doc, strip_answers=not has_scope(user, "dataset:answer_read"))  # type: ignore[assignment]
        return {
            "job_id": job_id,
            "trial_id": trial_id,
            "generation_id": committed_gen_id,
            "authorized_scope": req_scope,
            "storage_tier": "isolated_0700_evidence_store",
            "evidence": doc,
        }

    def list_notifications(self, user: AuthUser, *, limit: int = 30) -> dict[str, Any]:
        where, params = sql_project_filter(user, "j.project_id")
        rows = self.db.fetchall(
            f"""
            SELECT e.job_id, e.seq_id, e.payload_json, e.created_at, j.batch_name, j.status, j.params_json
            FROM exec_job_events e
            JOIN exec_jobs j ON j.job_id = e.job_id
            WHERE e.event_type = 'notification' AND {where}
            ORDER BY e.created_at DESC, e.seq_id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        if not has_scope(user, "dataset:holdout_read"):
            rows = [r for r in rows if not self._is_holdout_job_row(r)]
        items = []
        for r in rows:
            p = json.loads(r["payload_json"] or "{}")
            items.append(
                {
                    "job_id": r["job_id"],
                    "seq_id": int(r["seq_id"]),
                    "batch_name": r["batch_name"],
                    "status": r["status"],
                    "message": p.get("message") or "",
                    "terminal_status": p.get("terminal_status"),
                    "created_at": r["created_at"],
                }
            )
        return {"items": items, "total": len(items)}
