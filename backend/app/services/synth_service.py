from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.indexing.hot_indexer import SourceApprovalRegistry
from app.security.auth import AuthUser
from app.security.authz import (
    can_access_project,
    has_scope,
    is_holdout_dataset_or_batch,
    require_project_access,
    require_scope,
    sql_project_filter,
)
from app.security.gateways import GatewayProfileRegistry
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from app.security.redaction import redact_path, redact_text
from app.security.storage_probe import probe_isolated_dir, resolve_within_root, validate_safe_id
from app.services.methodology import (
    METHODOLOGY_VERSION,
    bootstrap_ci95,
    finite,
    pass_at_k,
    pass_hat_k,
    wilson_ci95,
)

_SYNTH_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SynthService:
    """M2 Synthetic Studio & RL/SFT 4-Lane Selection Board (v2.2.1 · 02-7 & A-07 & S-02)."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        approval_registry: SourceApprovalRegistry | None = None,
        gateway_profiles: GatewayProfileRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = approval_registry or SourceApprovalRegistry(settings.source_approval_file)
        self.gateway_profiles = gateway_profiles or GatewayProfileRegistry(settings)

    def _load_harbor_synth_config(self) -> dict[str, Any]:
        cfg_path = self.settings.framework_dir / "harbor.toml"
        if not cfg_path.is_file():
            return {"methods": {}, "auditors": {}, "teacher": {}}
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {"methods": {}, "auditors": {}, "teacher": {}}
        return data.get("synthesis") or {"methods": {}, "auditors": {}, "teacher": {}}

    def _discover_synth_dirs(self) -> list[tuple[str, Path, str]]:
        """Discover real synthetic dataset packs (datasets/tianxi-synth-v1, datasets/synthetic/*,
        datasets/synth-*, plus 0700 staging/synth-generated/*)."""
        found: list[tuple[str, Path, str]] = []
        seen: set[Path] = set()

        def _add(label: str, d: Path, tier: str) -> None:
            if not d.is_dir() or not (d / "synthesis.json").is_file():
                return
            rp = d.resolve()
            if rp in seen:
                return
            seen.add(rp)
            found.append((label, d, tier))

        ds_root = self.settings.datasets_dir
        if ds_root.is_dir():
            for entry in sorted(ds_root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and (entry / "synthesis.json").is_file():
                    _add(entry.name, entry, "shared_datasets")
            syn_sub = ds_root / "synthetic"
            if syn_sub.is_dir():
                for entry in sorted(syn_sub.iterdir(), key=lambda p: p.name.lower()):
                    if entry.is_dir() and (entry / "synthesis.json").is_file():
                        _add(f"synthetic/{entry.name}", entry, "shared_datasets")

        staged_root = self.settings.staging_dir / "synth-generated" if self.settings.staging_dir else None
        if staged_root and staged_root.is_dir():
            for entry in sorted(staged_root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and (entry / "synthesis.json").is_file():
                    _add(f"staged/{entry.name}", entry, "staged_0700")
        return found

    def _resolve_authorized_synth_dir(self, user: AuthUser, dataset_id: str) -> tuple[Path, str]:
        if not dataset_id or ".." in dataset_id or "\\" in dataset_id or dataset_id.startswith("/"):
            raise AppError(400, "INVALID_PATH_OR_ID", "非法合成题包标识")
        for label, dpath, tier in self._discover_synth_dirs():
            if label == dataset_id or dpath.name == dataset_id:
                if tier == "staged_0700":
                    meta_p = dpath / ".lrm_owner.json"
                    if meta_p.is_file():
                        om = json.loads(meta_p.read_text(encoding="utf-8"))
                        require_project_access(user, om.get("project_id", "default"), owner_id=om.get("owner_id"))
                    else:
                        require_project_access(user, "default")
                else:
                    proj_id, owner_id, _, _ = self.approval_registry.lookup_full(dpath)
                    require_project_access(user, proj_id, owner_id=owner_id)
                return dpath, tier
        raise AppError(404, "RESOURCE_NOT_FOUND", f"合成题包不存在: {dataset_id}")

    # ------------------------------------------------------------------
    # 1. Synthetic Studio: List / Generate / Check
    # ------------------------------------------------------------------
    def list_synth_datasets(self, user: AuthUser) -> dict[str, Any]:
        synth_cfg = self._load_harbor_synth_config()
        registered_methods = sorted((synth_cfg.get("methods") or {}).keys())
        can_holdout = has_scope(user, "dataset:holdout_read")
        items: list[dict[str, Any]] = []

        for label, dpath, tier in self._discover_synth_dirs():
            if tier == "staged_0700":
                meta_p = dpath / ".lrm_owner.json"
                if meta_p.is_file():
                    om = json.loads(meta_p.read_text(encoding="utf-8"))
                    if not can_access_project(user, om.get("project_id", "default"), owner_id=om.get("owner_id")):
                        continue
                elif not can_access_project(user, "default"):
                    continue
            else:
                proj_id, owner_id, _, _ = self.approval_registry.lookup_full(dpath)
                if not can_access_project(user, proj_id, owner_id=owner_id):
                    continue

            try:
                syn = json.loads((dpath / "synthesis.json").read_text(encoding="utf-8"))
            except Exception:
                continue

            tasks = syn.get("tasks") or []
            train_tasks = [t for t in tasks if str(t.get("split") or "") != "holdout"]
            holdout_tasks = [t for t in tasks if str(t.get("split") or "") == "holdout"]
            methods_used = sorted({str(t.get("method")) for t in tasks if t.get("method")})
            envs_used = sorted({str(t.get("environment")) for t in tasks if t.get("environment")})

            raw_notes = syn.get("teacher_notes") or {}
            safe_notes: dict[str, Any] = {}
            for k, v in raw_notes.items():
                if not can_holdout and "holdout" in str(k).lower():
                    continue
                if isinstance(v, dict):
                    safe_notes[str(k)] = {
                        "scenario": redact_text(str(v.get("scenario") or "")),
                        "edge_cases": redact_text(str(v.get("edge_cases") or "")),
                        "demonstration": redact_text(str(v.get("demonstration") or "")),
                    }

            harbor_toml_preview = None
            ht_path = dpath / "harbor.toml"
            if ht_path.is_file():
                harbor_toml_preview = redact_text(ht_path.read_text(encoding="utf-8", errors="replace"))

            items.append(
                {
                    "dataset_id": label,
                    "dataset_name": str(syn.get("dataset") or dpath.name),
                    "storage_tier": tier,
                    "path_redacted": redact_path(str(dpath)),
                    "seed": syn.get("seed"),
                    "teacher_model": syn.get("teacher_model"),
                    "methods": methods_used,
                    "environments": envs_used,
                    "train_count": len(train_tasks),
                    "holdout_count": len(holdout_tasks),
                    "holdout_redacted": not can_holdout and len(holdout_tasks) > 0,
                    "teacher_notes": safe_notes,
                    "train_tasks": train_tasks[:200],
                    "holdout_tasks": holdout_tasks[:200] if can_holdout else [],
                    "harbor_toml_preview": harbor_toml_preview,
                }
            )

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "registered_methods": registered_methods,
            "teacher_profiles": self.gateway_profiles.list_public_profiles(kind="synth_teacher"),
            "items": items,
            "total": len(items),
        }

    def generate_synth_dataset(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or payload.get("slug") or "").strip()
        if not name or not _SYNTH_SLUG_RE.fullmatch(name) or len(name) > 64:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "合成题包名称须为小写字母/数字/连字符 slug")
        project_id = str(payload.get("project_id") or "default").strip()
        require_project_access(user, project_id, owner_id=user.user_id)

        synth_cfg = self._load_harbor_synth_config()
        registered = set((synth_cfg.get("methods") or {}).keys())
        req_methods = [str(m).strip() for m in (payload.get("methods") or []) if str(m).strip()]
        if not req_methods:
            req_methods = sorted(registered)
        invalid_m = [m for m in req_methods if m not in registered]
        if invalid_m:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", f"包含未登记的合成方法: {', '.join(invalid_m)}")

        train_per = int(payload.get("train_per_method", 2))
        holdout_per = int(payload.get("holdout_per_method", 1))
        seed = int(payload.get("seed", 20261124))
        if train_per < 0 or holdout_per < 0 or (train_per + holdout_per) <= 0 or train_per > 50 or holdout_per > 50:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "train_per_method / holdout_per_method 须在合理正整数范围内 (1..50)")

        use_teacher = bool(payload.get("use_teacher", False))
        teacher_profile_id = str(payload.get("teacher_profile_id") or "").strip()
        teacher_model = None
        extra_env: dict[str, str] = {}
        if use_teacher:
            if not teacher_profile_id:
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "启用 --teacher 时必须选择服务端已登记的 Teacher Profile")
            prof, secret = self.gateway_profiles.resolve_runtime_credentials(teacher_profile_id)
            if prof.get("kind") != "synth_teacher":
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "所选 Profile 不是 synth_teacher 类型")
            teacher_model = str(prof.get("default_model") or "")
            extra_env[str(prof["api_key_env"])] = secret
        runtime_env = build_sanitized_subprocess_env(extra_env or None)

        cli_parts = [
            "python3",
            "framework/synthesize.py",
            "generate",
            name,
            *(arg for m in req_methods for arg in ("--method", m)),
            "--train-per-method",
            str(train_per),
            "--holdout-per-method",
            str(holdout_per),
            "--seed",
            str(seed),
        ]
        if use_teacher:
            cli_parts.append("--teacher")
            if teacher_model:
                cli_parts.extend(["--teacher-model", teacher_model])
        cli_preview = shlex.join(cli_parts)

        execute = bool(payload.get("execute", False))
        if not execute:
            return {
                "ok": True,
                "mode": "plan",
                "name": name,
                "methods": req_methods,
                "planned_train": len(req_methods) * train_per,
                "planned_holdout": len(req_methods) * holdout_per,
                "seed": seed,
                "use_teacher": use_teacher,
                "cli_command": cli_preview,
                "methodology_version": METHODOLOGY_VERSION,
            }

        # Fail-closed 0700 isolated staging + write guard (never mutates shared CIFS datasets!)
        assert self.settings.staging_dir is not None
        self.db.check_write_guard()
        probe_isolated_dir(self.settings.staging_dir, create=True, write_guard=self.db.check_write_guard)
        out_root = self.settings.staging_dir / "synth-generated"
        out_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(out_root, 0o700)
        target_dir = resolve_within_root(out_root, name)
        if target_dir.exists():
            raise AppError(409, "BATCH_ALREADY_EXISTS", f"隔离待审区已存在同名合成题包: {name}")

        script_path = resolve_verified_managed_script(self.settings, "synthesize.py")
        managed_toml = resolve_verified_managed_script(self.settings, "harbor.toml")

        cmd = [
            sys.executable,
            "-B",
            str(script_path),
            "generate",
            name,
            "--config",
            str(managed_toml),
            "--output",
            str(out_root),
            *(arg for m in req_methods for arg in ("--method", m)),
            "--train-per-method",
            str(train_per),
            "--holdout-per-method",
            str(holdout_per),
            "--seed",
            str(seed),
        ]
        if use_teacher:
            cmd.append("--teacher")
            if teacher_model:
                cmd.extend(["--teacher-model", teacher_model])

        proc = subprocess.run(
            cmd,
            cwd=str(script_path.parent),
            env=runtime_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"synthesize.py generate 执行失败: {redact_text((proc.stderr or proc.stdout).strip())}",
            )

        (target_dir / ".lrm_owner.json").write_text(
            json.dumps({"project_id": project_id, "owner_id": user.user_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        syn = json.loads((target_dir / "synthesis.json").read_text(encoding="utf-8"))
        tasks = syn.get("tasks") or []
        return {
            "ok": True,
            "mode": "executed_staged_0700",
            "dataset_id": f"staged/{name}",
            "name": name,
            "project_id": project_id,
            "methods": req_methods,
            "train_count": sum(1 for t in tasks if t.get("split") != "holdout"),
            "holdout_count": sum(1 for t in tasks if t.get("split") == "holdout"),
            "stdout": redact_text(proc.stdout.strip()),
            "cli_command": cli_preview,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def check_synth_dataset(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        dataset_id = str(payload.get("dataset_id") or "").strip()
        if not dataset_id:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "必须传入 dataset_id")
        ds_dir, tier = self._resolve_authorized_synth_dir(user, dataset_id)

        include_holdout = bool(payload.get("include_holdout", False))
        if include_holdout:
            require_scope(user, "dataset:holdout_read")
        can_holdout = has_scope(user, "dataset:holdout_read")

        # Optional single task check inside train (or holdout if permitted)
        task_name = str(payload.get("task_id") or "").strip()
        target_path = ds_dir
        if task_name:
            validate_safe_id(task_name, "task_id")
            if (ds_dir / "train" / task_name / "task.toml").is_file():
                target_path = resolve_within_root(ds_dir / "train", task_name)
            elif (ds_dir / "holdout" / task_name / "task.toml").is_file():
                require_scope(user, "dataset:holdout_read")
                target_path = resolve_within_root(ds_dir / "holdout", task_name)
            else:
                raise AppError(404, "RESOURCE_NOT_FOUND", f"题目不存在: {task_name}")
        elif not include_holdout or not can_holdout:
            # Only check train/ split if caller doesn't request or lacks dataset:holdout_read!
            train_dir = ds_dir / "train"
            if train_dir.is_dir():
                # synthesize.py check accepts dataset root or single task dir; check each train task cleanly
                target_path = ds_dir

        script_path = resolve_verified_managed_script(self.settings, "synthesize.py")
        managed_toml = resolve_verified_managed_script(self.settings, "harbor.toml")
        clean_env = build_sanitized_subprocess_env()

        # Run synthesize.py check on allowed targets (strictly filtering out holdout tasks if !include_holdout!)
        if target_path == ds_dir and (not include_holdout or not can_holdout):
            train_tasks = sorted(p.parent for p in (ds_dir / "train").glob("*/task.toml")) if (ds_dir / "train").is_dir() else []
            max_tasks = int(payload.get("max_tasks") or len(train_tasks))
            train_tasks = train_tasks[: max(1, min(max_tasks, 60))]
            tested = 0
            passed = 0
            indep = 0
            failed: dict[str, str] = {}
            for tdir in train_tasks:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(script_path),
                        "check",
                        str(tdir),
                        "--config",
                        str(managed_toml),
                    ],
                    cwd=str(script_path.parent),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                tested += 1
                try:
                    res = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
                except Exception:
                    failed[tdir.name] = redact_text((proc.stderr or proc.stdout).strip()) or "check_error"
                    continue
                passed += int(res.get("passed") or 0)
                indep += int(res.get("independent_audit") or 0)
                for fk, fv in (res.get("failed") or {}).items():
                    failed[str(fk)] = redact_text(str(fv)) or "failed"
            return {
                "dataset_id": dataset_id,
                "storage_tier": tier,
                "scope_checked": "train_only",
                "tested": tested,
                "passed": passed,
                "independent_audit": indep,
                "failed": failed,
                "all_passed": tested > 0 and len(failed) == 0,
                "methodology_version": METHODOLOGY_VERSION,
            }

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script_path),
                "check",
                str(target_path),
                "--config",
                str(managed_toml),
            ],
            cwd=str(script_path.parent),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw_out = (proc.stdout or "").strip()
        try:
            res = json.loads(raw_out.splitlines()[-1] if raw_out else "{}")
        except Exception as exc:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"synthesize.py check 解析失败: {redact_text((proc.stderr or raw_out).strip())}",
            ) from exc
        failed_clean = {str(k): (redact_text(str(v)) or "failed") for k, v in (res.get("failed") or {}).items()}
        return {
            "dataset_id": dataset_id,
            "storage_tier": tier,
            "scope_checked": "task" if task_name else "train_and_holdout",
            "tested": int(res.get("tested") or 0),
            "passed": int(res.get("passed") or 0),
            "independent_audit": int(res.get("independent_audit") or 0),
            "failed": failed_clean,
            "all_passed": int(res.get("tested") or 0) > 0 and len(failed_clean) == 0,
            "methodology_version": METHODOLOGY_VERSION,
        }

    # ------------------------------------------------------------------
    # 2. RL / SFT 4-Lane Selection Board + pass@k / pass^k Stability
    # ------------------------------------------------------------------
    def list_selection_batches(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = sql_project_filter(user, "b.project_id")
        rows = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.batch_name, b.benchmark_slug, b.dataset_release_id, b.raw_dataset_name,
                   b.model_canonical, b.harness, b.runner, b.has_selection,
                   b.unique_tasks, b.planned, b.recorded, b.scored, b.valid_mean, b.finished_at
            FROM batches b
            WHERE {proj_sql}
              AND b.is_running_placeholder = 0
              AND (b.has_selection = 1 OR b.recorded > b.unique_tasks)
            ORDER BY b.has_selection DESC, b.recorded DESC, b.batch_name ASC
            """,
            params,
        )
        return {"items": rows, "total": len(rows), "methodology_version": METHODOLOGY_VERSION}

    def _compute_selection_and_curves(
        self,
        batch: dict[str, Any],
        eps: list[dict[str, Any]],
        sel_rows: list[dict[str, Any]],
        *,
        required_attempts: int,
        min_grpo: int,
        ignore_exception_types: set[str] | None = None,
        use_stored_lanes: bool = True,
    ) -> dict[str, Any]:
        ignore_set = ignore_exception_types or set()
        pass_thr = float(batch["pass_threshold"]) if finite(batch.get("pass_threshold")) else 1.0
        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in eps:
            by_task[str(e["task_id"])].append(e)

        sel_map = {str(r["task_id"]): r for r in sel_rows}
        all_task_ids = sorted(set(by_task.keys()) | set(sel_map.keys()))
        max_k = max(required_attempts, max((len(by_task[t]) for t in all_task_ids), default=1))
        max_k = min(max_k, 32)

        # Compute exact 4-lane routing matching framework/synthesize.py:select (lines 301-326)!
        lanes_count: Counter[str] = Counter()
        task_items: list[dict[str, Any]] = []
        k_valid_buckets: dict[int, list[float]] = {k: [] for k in range(1, max_k + 1)}
        k_hat_buckets: dict[int, list[float]] = {k: [] for k in range(1, max_k + 1)}
        k_faz_buckets: dict[int, list[float]] = {k: [] for k in range(1, max_k + 1)}
        all_exception_types: Counter[str] = Counter()

        for tid in all_task_ids:
            sub = by_task[tid]
            eff_sub = [
                r
                for r in sub
                if not (
                    ignore_set
                    and r["status"] != "scored"
                    and (str(r.get("exception_type") or "") in ignore_set or str(r.get("failure_kind") or "") in ignore_set)
                )
            ]
            scored_rows = [r for r in eff_sub if r["status"] == "scored" and finite(r["reward"])]
            scored_ids = {r["trial_id"] for r in scored_rows}
            excluded: Counter[str] = Counter()
            for r in eff_sub:
                if r["trial_id"] not in scored_ids:
                    ek = str(r.get("exception_type") or r.get("failure_kind") or ("invalid_reward" if r["status"] == "scored" else r["status"]))
                    excluded[ek] += 1
                    all_exception_types[ek] += 1
            for r in sub:
                if r["status"] != "scored":
                    ek_raw = str(r.get("exception_type") or r.get("failure_kind") or r["status"])
                    if ek_raw in ignore_set:
                        all_exception_types[ek_raw] += 1

            n_scored = len(scored_rows)
            n_passed = sum(1 for r in scored_rows if float(r["reward"]) >= pass_thr)
            hashes = {r.get("task_checksum") for r in eff_sub}

            stored = sel_map.get(tid)
            if use_stored_lanes and stored is not None and not ignore_set:
                lane = str(stored["lane"])
                excl_dict = json.loads(stored["excluded_json"] or "{}")
                attempts_n = int(stored["attempts"])
            else:
                attempts_n = len(eff_sub)
                if len(eff_sub) != required_attempts or n_scored != required_attempts or len(hashes) > 1:
                    lane = "review"
                elif n_passed == 0:
                    lane = "teacher"
                elif n_passed == required_attempts:
                    lane = "regression"
                else:
                    lane = "grpo"
                excl_dict = dict(excluded)

            lanes_count[lane] += 1

            # Per-k pass@k & pass^k (strictly NULL when k > n_scored — A-07!)
            per_k_pass: dict[str, float | None] = {}
            per_k_hat: dict[str, float | None] = {}
            n_total_att = len(sub)
            for k in range(1, max_k + 1):
                pk = pass_at_k(n_scored, n_passed, k)
                phk = pass_hat_k(n_scored, n_passed, k)
                per_k_pass[f"k{k}"] = round(pk, 4) if pk is not None else None
                per_k_hat[f"k{k}"] = round(phk, 4) if phk is not None else None
                if pk is not None:
                    k_valid_buckets[k].append(pk)
                if phk is not None:
                    k_hat_buckets[k].append(phk)
                # Conservative faults_as_zero lower bound: treat all recorded attempts as denominator
                pk_faz = pass_at_k(n_total_att, n_passed, k)
                if pk_faz is not None:
                    k_faz_buckets[k].append(pk_faz)

            task_items.append(
                {
                    "task_id": tid,
                    "task_directory": stored["task_directory"] if stored else tid.rsplit("/", 1)[-1],
                    "lane": lane,
                    "attempts": attempts_n,
                    "recorded": len(sub),
                    "scored": n_scored,
                    "passed": n_passed,
                    "excluded": excl_dict,
                    "pass_at_k": per_k_pass,
                    "pass_hat_k": per_k_hat,
                    "attempt_dots": [
                        {
                            "trial_id": r["trial_id"],
                            "attempt_index": r["attempt_index"],
                            "status": r["status"],
                            "reward": r["reward"],
                            "failure_kind": r["failure_kind"],
                            "exception_type": r["exception_type"],
                            "duration_sec": r["duration_sec"],
                        }
                        for r in sub
                    ],
                }
            )

        total_tasks = len(all_task_ids)
        curves: list[dict[str, Any]] = []
        for k in range(1, max_k + 1):
            vals = k_valid_buckets[k]
            hats = k_hat_buckets[k]
            fazs = k_faz_buckets[k]
            comp = len(vals)
            mean_pk = (math.fsum(vals) / comp) if comp > 0 else None
            mean_phk = (math.fsum(hats) / len(hats)) if hats else None
            mean_faz = (math.fsum(fazs) / len(fazs)) if fazs else None
            ci = (
                wilson_ci95(int(round(math.fsum(vals))), comp)
                if (k == 1 and max_k == 1)
                else bootstrap_ci95(vals)
            )
            missing = total_tasks - comp
            curves.append(
                {
                    "k": k,
                    "pass_at_k": round(mean_pk, 4) if mean_pk is not None else None,
                    "pass_at_k_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
                    "pass_hat_k": round(mean_phk, 4) if mean_phk is not None else None,
                    "faults_as_zero_pass_at_k": round(mean_faz, 4) if mean_faz is not None else None,
                    "computable_tasks": comp,
                    "total_tasks": total_tasks,
                    "missing_tasks": missing,
                    "note": f"{missing} 题有效尝试数 <{k} 记缺测" if missing > 0 else None,
                }
            )

        grpo_cnt = int(lanes_count.get("grpo", 0))
        return {
            "required_attempts": required_attempts,
            "min_grpo": min_grpo,
            "threshold_met": grpo_cnt >= min_grpo,
            "totals": {
                "grpo": grpo_cnt,
                "teacher": int(lanes_count.get("teacher", 0)),
                "regression": int(lanes_count.get("regression", 0)),
                "review": int(lanes_count.get("review", 0)),
            },
            "unique_tasks": total_tasks,
            "total_attempts": len(eps),
            "available_exception_types": sorted(all_exception_types.keys()),
            "curves": curves,
            "tasks": task_items,
        }

    def _load_batch_and_episodes(self, user: AuthUser, batch_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], bool]:
        validate_safe_id(batch_id, "batch_id")
        batch = self.db.fetchone(
            """
            SELECT b.*, r.split AS rel_split
            FROM batches b
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE b.batch_id = ?
            """,
            (batch_id,),
        )
        if batch is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"批次不存在: {batch_id}")
        require_project_access(user, batch["project_id"], owner_id=batch["owner_id"])
        is_hold = is_holdout_dataset_or_batch(
            dataset_release_id=batch.get("dataset_release_id"),
            release_split=batch.get("rel_split"),
            benchmark_slug=batch.get("benchmark_slug"),
            raw_dataset_name=batch.get("raw_dataset_name"),
            batch_name=batch.get("batch_name"),
        )
        eps = self.db.fetchall(
            """
            SELECT trial_id, task_id, task_checksum, attempt_index, status, reward, reward_valid,
                   failure_kind, exception_type, duration_sec
            FROM episodes
            WHERE batch_id = ?
            ORDER BY task_id ASC, attempt_index ASC, trial_id ASC
            """,
            (batch_id,),
        )
        sel_rows = self.db.fetchall(
            "SELECT task_id, task_directory, attempts, scored, passed, excluded_json, lane FROM selection_tasks WHERE batch_id = ?",
            (batch_id,),
        )
        return batch, eps, sel_rows, is_hold

    def get_selection_board(self, user: AuthUser, batch_id: str) -> dict[str, Any]:
        batch, eps, sel_rows, is_hold = self._load_batch_and_episodes(user, batch_id)
        # Read required_attempts / min_grpo from selection.json if present on disk
        req_attempts = 8
        min_grpo = 10
        if batch.get("source_namespace") == "harbor_runs" and ":" in str(batch.get("source_locator") or ""):
            bname = str(batch["source_locator"]).split(":", 1)[1]
            try:
                sp = resolve_within_root(self.settings.harbor_runs_dir, f"{bname}/selection.json")
                if sp.is_file():
                    sdoc = json.loads(sp.read_text(encoding="utf-8"))
                    req_attempts = int(sdoc.get("required_attempts") or req_attempts)
                    min_grpo = int(sdoc.get("min_grpo") or min_grpo)
            except (AppError, OSError, ValueError):
                pass
        else:
            counts = Counter(e["task_id"] for e in eps)
            req_attempts = max(counts.values(), default=1)

        computed = self._compute_selection_and_curves(
            batch, eps, sel_rows, required_attempts=req_attempts, min_grpo=min_grpo, use_stored_lanes=True
        )
        can_hold = has_scope(user, "dataset:holdout_read")
        holdout_tids: set[str] = set()
        if not can_hold and not is_hold and batch.get("dataset_release_id"):
            holdout_tids = {
                str(r["task_id"])
                for r in self.db.fetchall(
                    "SELECT task_id FROM task_definitions WHERE dataset_release_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'",
                    (batch["dataset_release_id"],),
                )
            }
        if not can_hold and is_hold:
            visible_tasks = []
            redacted = True
        elif not can_hold and holdout_tids:
            visible_tasks = [t for t in computed["tasks"] if t["task_id"] not in holdout_tids]
            redacted = len(visible_tasks) < len(computed["tasks"])
        else:
            visible_tasks = computed["tasks"]
            redacted = False
        return {
            "batch_id": batch_id,
            "batch_name": batch["batch_name"],
            "model_canonical": batch["model_canonical"],
            "harness": batch["harness"],
            "dataset_release_id": batch["dataset_release_id"],
            "has_stored_selection": bool(sel_rows),
            "redacted": redacted,
            "redacted_reason": "留出集逐题明细缺少 dataset:holdout_read 权限，仅展示 4 通道聚合统计与 pass@k 曲线" if redacted else None,
            **{**computed, "tasks": visible_tasks},
            "methodology_version": METHODOLOGY_VERSION,
        }

    def preview_selection(self, user: AuthUser, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        batch, eps, sel_rows, is_hold = self._load_batch_and_episodes(user, batch_id)
        counts = Counter(e["task_id"] for e in eps)
        default_att = max(counts.values(), default=1)
        req_attempts = int(payload.get("required_attempts") or default_att)
        min_grpo = int(payload.get("min_grpo", 10))
        if req_attempts < 1 or req_attempts > 64 or min_grpo < 1 or min_grpo > 10000:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "required_attempts 或 min_grpo 超出有效范围")
        ignore_excs = {str(x).strip() for x in (payload.get("ignore_exception_types") or []) if str(x).strip()}

        computed = self._compute_selection_and_curves(
            batch,
            eps,
            sel_rows,
            required_attempts=req_attempts,
            min_grpo=min_grpo,
            ignore_exception_types=ignore_excs,
            use_stored_lanes=False,
        )
        can_hold = has_scope(user, "dataset:holdout_read")
        holdout_tids: set[str] = set()
        if not can_hold and not is_hold and batch.get("dataset_release_id"):
            holdout_tids = {
                str(r["task_id"])
                for r in self.db.fetchall(
                    "SELECT task_id FROM task_definitions WHERE dataset_release_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'",
                    (batch["dataset_release_id"],),
                )
            }
        if not can_hold and is_hold:
            visible_tasks = []
            redacted = True
        elif not can_hold and holdout_tids:
            visible_tasks = [t for t in computed["tasks"] if t["task_id"] not in holdout_tids]
            redacted = len(visible_tasks) < len(computed["tasks"])
        else:
            visible_tasks = computed["tasks"]
            redacted = False
        lane_task_ids: dict[str, list[str]] = {"grpo": [], "teacher": [], "regression": [], "review": []}
        for t in visible_tasks:
            lane_task_ids.setdefault(t["lane"], []).append(t["task_id"])

        return {
            "batch_id": batch_id,
            "simulated": True,
            "ignored_exception_types": sorted(ignore_excs),
            "redacted": redacted,
            "lane_task_ids": lane_task_ids,
            **{**computed, "tasks": visible_tasks},
            "methodology_version": METHODOLOGY_VERSION,
        }
