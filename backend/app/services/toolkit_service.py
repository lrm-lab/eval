from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.hot_indexer import (
    _MANDATORY_MANIFEST_FILES,
    _OPTIONAL_MANIFEST_FILES,
    SourceApprovalRegistry,
    _dir_content_digest,
)
from app.security.auth import AuthUser
from app.security.authz import (
    has_scope,
    is_holdout_dataset_or_batch,
    require_project_access,
    require_role,
    require_scope,
    sql_project_filter,
)
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from app.security.redaction import redact_json_recursive, redact_text
from app.security.storage_probe import probe_isolated_dir, resolve_within_root, validate_safe_id
from app.services.methodology import METHODOLOGY_VERSION, finite

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)


def _slugify_heading(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff\s-]", "", text).strip().lower()
    return re.sub(r"\s+", "-", clean) or "section"


def _phase_diff_sec(phase_obj: Any) -> float | None:
    if not isinstance(phase_obj, dict):
        return None
    st = phase_obj.get("started_at")
    en = phase_obj.get("finished_at")
    if not st or not en:
        return None
    try:
        t0 = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(en).replace("Z", "+00:00"))
        val = (t1 - t0).total_seconds()
        return round(val, 3) if val >= 0 else None
    except ValueError:
        return None


def _parse_native_harbor_trial_result(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Native Harbor TrialResult adapter preserving `reward_valid`, `exception_info.policy`,
    and 4-phase timings (v2.2.1 · 02-8 §1 & A-04/A-13)."""
    if not isinstance(doc, dict):
        return None
    if not (doc.get("task_name") or doc.get("verifier_result") or doc.get("agent_info") or doc.get("environment_setup")):
        return None
    tid = str(doc.get("id") or doc.get("trial_name") or f"trial_{secrets.token_hex(4)}")
    task_id = str(doc.get("task_name") or doc.get("task_id") or tid)
    ver = doc.get("verifier_result") or {}
    rewards = ver.get("rewards") if isinstance(ver, dict) and isinstance(ver.get("rewards"), dict) else {}
    raw_rew = rewards.get("reward")
    if raw_rew is None and rewards:
        first_num = next((v for v in rewards.values() if finite(v)), None)
        raw_rew = first_num
    exc_info = doc.get("exception_info") if isinstance(doc.get("exception_info"), dict) else None
    exc_type = exc_info.get("exception_type") if exc_info else None
    exc_policy = exc_info.get("policy") if exc_info else None

    # Honor explicit Harbor `reward_valid` when present (preserving 230 TB2.1 exception+valid cases!)
    if isinstance(doc.get("reward_valid"), bool):
        reward_valid = bool(doc["reward_valid"]) and finite(raw_rew)
    elif exc_info and str(exc_policy or "").lower() in ("allow", "keep_reward", "ignore"):
        reward_valid = finite(raw_rew)
    elif exc_info:
        reward_valid = False
    else:
        reward_valid = finite(raw_rew)

    phase_timings = {
        "environment_setup_s": _phase_diff_sec(doc.get("environment_setup")),
        "agent_setup_s": _phase_diff_sec(doc.get("agent_setup")),
        "agent_execution_s": _phase_diff_sec(doc.get("agent_execution")),
        "verifier_s": _phase_diff_sec(doc.get("verifier")),
    }
    total_dur = _phase_diff_sec(doc)
    return {
        "trial_id": tid,
        "task_id": task_id,
        "task_checksum": doc.get("task_checksum"),
        "reward": float(raw_rew) if finite(raw_rew) else None,
        "native_reward_valid": reward_valid,
        "status": "scored" if reward_valid else ("failed" if exc_type else "unscored"),
        "exception_type": exc_type,
        "exception_policy": exc_policy,
        "duration_sec": total_dur,
        "phase_timings": phase_timings,
        "verifier_rewards": rewards,
    }


def _safe_extract_archive(raw_bytes: bytes, filename: str, dest: Path) -> None:
    """Safely extract zip or tar(.gz) into `dest`, rejecting path traversal and symlinks."""
    dest_res = dest.resolve()
    fn_l = filename.lower()
    if fn_l.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            members = zf.infolist()
            if len(members) > 5000:
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "压缩包内文件数超过 5000 上限")
            total_unpacked = 0
            for info in members:
                if info.is_dir():
                    continue
                if info.file_size > 100 * 1024 * 1024:
                    raise AppError(413, "PAYLOAD_TOO_LARGE", "解压后单文件过大")
                total_unpacked += info.file_size
                if total_unpacked > 150 * 1024 * 1024:
                    raise AppError(413, "PAYLOAD_TOO_LARGE", "解压总大小超过安全上限")
                # Reject symlinks in zip (external_attr unix symlink mode 0o120000)
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise AppError(400, "INVALID_PATH_OR_ID", "拒绝压缩包内的符号链接")
                target = resolve_within_root(dest_res, info.filename)
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o600)
        return

    if fn_l.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if fn_l.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode=mode) as tf:
            members = tf.getmembers()
            if len(members) > 5000:
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "归档内文件数超过 5000 上限")
            total_unpacked = 0
            for m in members:
                if m.isdir():
                    continue
                if m.issym() or m.islnk() or not m.isfile():
                    raise AppError(400, "INVALID_PATH_OR_ID", "拒绝归档内的符号链接或特殊设备文件")
                total_unpacked += m.size
                if total_unpacked > 150 * 1024 * 1024:
                    raise AppError(413, "PAYLOAD_TOO_LARGE", "解压总大小超过安全上限")
                target = resolve_within_root(dest_res, m.name)
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                extracted = tf.extractfile(m)
                if extracted is not None:
                    with open(target, "wb") as out:
                        shutil.copyfileobj(extracted, out)
                    os.chmod(target, 0o600)
        return

    # Single file upload
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise AppError(400, "INVALID_PATH_OR_ID", "非法上传文件名")
    target = resolve_within_root(dest_res, safe_name)
    target.write_bytes(raw_bytes)
    os.chmod(target, 0o600)


class ToolkitService:
    """M2 Toolkit Services (v2.2.1 · 02-8 & E-02 & A-06 & S-06):
    1) Existing batch verification via real read-only `framework/report.py`
    2) 3-format Offline Calculation (`shared_batch_or_episodes`, `native_harbor_job`, `summary_only`)
       staged strictly into Fail-Closed `0700` staging_dir & persisted to `offline_reports`
    3) Admin-only official result attachment (SQLite only, NEVER mutates `run_scope` or CIFS files)
    4) Real-batch Param Compare groups (`param_compare_groups`)
    5) Framework & Platform Manuals reader directly backed by 197 `/home/share-data/tianxi_benches/framework/*.md`."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        approval_registry: SourceApprovalRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = approval_registry or SourceApprovalRegistry(settings.source_approval_file)

    def _verify_and_read_ref(self, root: Path, rel_path: str, expected_sha256: str | None) -> bytes:
        target = resolve_within_root(root, rel_path)
        if target.is_symlink():
            raise AppError(
                400,
                "INVALID_PATH_OR_ID",
                f"拒绝符号链接证据文件: {rel_path}",
            )
        if not target.is_file():
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                f"已索引同代证据文件缺失: {rel_path}",
            )
        raw = target.read_bytes()
        if expected_sha256:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected_sha256:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "检测到校验证据文件跨代覆写（摘要与已索引 generation_id 不一致），拒绝跨代校验",
                    {"expected_sha256": expected_sha256[:16], "actual_sha256": actual[:16]},
                )
        return raw

    def _read_verified_top_level_snapshot(self, batch: dict[str, Any], bdir: Path) -> dict[str, bytes]:
        """Verify that `bdir` on disk still matches the exact SQLite-committed generation digests,
        mandatory/optional top-level file set, and absent-optional constraints — NEVER trusting a
        disk `publish-manifest.json` self-referentially (I-01 / E-02, zero TOCTOU)."""
        indexed_gen = str(batch.get("generation_id") or "")
        committed_td_sha = batch.get("trial_digests_sha256")
        try:
            committed_checks = json.loads(batch.get("checks_json") or "{}")
            if not isinstance(committed_checks, dict):
                committed_checks = {}
        except Exception:
            committed_checks = {}
        try:
            committed_absent_list = json.loads(batch.get("absent_optional_json") or "[]")
            if not isinstance(committed_absent_list, list):
                committed_absent_list = []
        except Exception:
            committed_absent_list = []

        committed_top = committed_checks.get("top_level_files")
        if not isinstance(committed_top, dict):
            committed_top = {}
        committed_pm_sha = committed_checks.get("publish_manifest_sha256")
        committed_payload_sha = committed_checks.get("verified_payload_sha256")
        manifest_mode = str(committed_checks.get("manifest_mode") or "")

        is_v2_committed = manifest_mode == "publish_manifest_v2" or (
            manifest_mode != "legacy_v1"
            and bool(indexed_gen)
            and not indexed_gen.startswith(("gen_legacy_", "gen_placeholder_"))
        )

        pm_path = bdir / "publish-manifest.json"
        verified_top: dict[str, bytes] = {}

        if is_v2_committed:
            if pm_path.is_symlink() or not pm_path.is_file():
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "已索引的 v2 发布清单 publish-manifest.json 缺失或非法，拒绝混代校验",
                    {"indexed_generation_id": indexed_gen},
                )
            try:
                pm_raw = pm_path.read_bytes()
                pm = json.loads(pm_raw.decode("utf-8"))
            except Exception as exc:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    f"publish-manifest.json 无法解析，拒绝校验: {exc}",
                ) from exc

            # Bind publish-manifest.json bytes directly to SQLite's committed manifest SHA-256 (fail closed on unbackfilled rows!)
            if not isinstance(committed_pm_sha, str) or not committed_pm_sha or not committed_top:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "该 v2 批次索引尚未完成顶层内容摘要绑定（旧版索引待回填），拒绝自证校验，请先完成热扫回填",
                    {"indexed_generation_id": indexed_gen, "digest_backfill_required": True},
                )
            actual_pm_sha = hashlib.sha256(pm_raw).hexdigest()
            if actual_pm_sha != committed_pm_sha:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "publish-manifest.json 内容已偏离 SQLite 已提交摘要（即使同代亦拒绝核验）",
                    {"indexed_generation_id": indexed_gen},
                )

            if not isinstance(pm, dict) or str(pm.get("generation_id") or "") != indexed_gen:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "共享目录已发布新代（publish-manifest.json 的 generation_id 与已索引发布代不一致），请先重扫同步索引",
                    {
                        "indexed_generation_id": indexed_gen,
                        "disk_generation_id": pm.get("generation_id") if isinstance(pm, dict) else None,
                    },
                )

            top_files = pm.get("top_level_files")
            if not isinstance(top_files, dict) or not top_files:
                raise AppError(409, "GENERATION_MISMATCH", "publish-manifest.json 缺少 top_level_files")
            absent_raw = pm.get("absent_optional_files")
            if not isinstance(absent_raw, list):
                raise AppError(409, "GENERATION_MISMATCH", "publish-manifest.json 缺少 absent_optional_files")
            absent_opt = {str(x) for x in absent_raw}

            # Enforce identical mandatory & optional partition constraints as HotIndexer._verify_publish_manifest()
            for req_name in _MANDATORY_MANIFEST_FILES:
                if req_name not in top_files:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"publish-manifest.json 缺少必填顶层文件声明: {req_name}",
                    )
            for opt_name in _OPTIONAL_MANIFEST_FILES:
                in_top = opt_name in top_files
                in_abs = opt_name in absent_opt
                if in_top and in_abs:
                    raise AppError(409, "GENERATION_MISMATCH", f"可选文件声明冲突: {opt_name}")
                if not in_top and not in_abs:
                    raise AppError(409, "GENERATION_MISMATCH", f"可选文件未声明归属: {opt_name}")

            # Enforce absent_optional_files matches SQLite's committed absent set!
            expected_absent_set = {str(x) for x in committed_absent_list}
            if isinstance(committed_checks.get("absent_optional_files"), list):
                expected_absent_set = {str(x) for x in committed_checks["absent_optional_files"]}
            if absent_opt != expected_absent_set:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "absent_optional_files 与 SQLite 已提交发布代不一致，拒绝校验",
                )
            for abs_name in absent_opt:
                abs_p = resolve_within_root(bdir, abs_name)
                if abs_p.exists() or abs_p.is_symlink():
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"声明缺失的可选文件在磁盘出现，批次已偏离已索引发布代: {abs_name}",
                    )

            # Bind every top-level file strictly to SQLite's committed top_level_files digests!
            if set(top_files.keys()) != set(committed_top.keys()):
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "top_level_files 集合与 SQLite 已提交发布代不一致，拒绝校验",
                )
            for rel_name, c_spec in committed_top.items():
                if not isinstance(c_spec, dict) or not isinstance(c_spec.get("sha256"), str) or not c_spec["sha256"]:
                    raise AppError(409, "GENERATION_MISMATCH", f"SQLite 已提交顶层摘要非法: {rel_name}")
                pm_spec = top_files.get(rel_name)
                if (
                    not isinstance(pm_spec, dict)
                    or pm_spec.get("sha256") != c_spec["sha256"]
                    or (
                        c_spec.get("size") is not None
                        and pm_spec.get("size") is not None
                        and int(pm_spec["size"]) != int(c_spec["size"])
                    )
                ):
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"publish-manifest 顶层摘要与 SQLite 已提交摘要冲突（同代内容已变更）: {rel_name}",
                        {"file": str(rel_name)},
                    )
                fpath = resolve_within_root(bdir, str(rel_name))
                if fpath.is_symlink() or not fpath.is_file():
                    raise AppError(409, "GENERATION_MISMATCH", f"已索引发布代顶层文件缺失或非法: {rel_name}")
                raw = fpath.read_bytes()
                if hashlib.sha256(raw).hexdigest() != c_spec["sha256"]:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"顶层文件摘要与 SQLite 已提交发布代不一致: {rel_name}",
                        {"file": str(rel_name)},
                    )
                if c_spec.get("size") is not None and len(raw) != int(c_spec["size"]):
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"顶层文件大小与 SQLite 已提交发布代不一致: {rel_name}",
                        {"file": str(rel_name)},
                    )
                verified_top[str(rel_name)] = raw

            # Always bind trial-digests.jsonl SHA-256 directly to batches.trial_digests_sha256!
            if isinstance(committed_td_sha, str) and committed_td_sha:
                td_bytes = verified_top.get("trial-digests.jsonl")
                if td_bytes is None or hashlib.sha256(td_bytes).hexdigest() != committed_td_sha:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        "trial-digests.jsonl 与 SQLite 已提交摘要不一致，拒绝核验",
                    )
            return verified_top

        # Legacy v1 batch (indexed without publish-manifest.json):
        if pm_path.exists() or pm_path.is_symlink():
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "历史批次目录在磁盘已升级生成 publish-manifest.json，请先重扫同步索引",
                {"indexed_generation_id": indexed_gen},
            )

        if not committed_top:
            raise AppError(
                409,
                "GENERATION_MISMATCH",
                "该历史批次索引尚未完成顶层内容摘要绑定（旧版索引待回填），拒绝在未就绪状态下核验，请先完成热扫回填",
                {"indexed_generation_id": indexed_gen, "digest_backfill_required": True},
            )

        for abs_name in committed_checks.get("absent_optional_files") or []:
            abs_p = resolve_within_root(bdir, str(abs_name))
            if abs_p.exists() or abs_p.is_symlink():
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    f"历史批次磁盘新增了未索引顶层文件，请先重扫同步索引: {abs_name}",
                )
        for rel_name, c_spec in committed_top.items():
            if not isinstance(c_spec, dict) or not isinstance(c_spec.get("sha256"), str) or not c_spec["sha256"]:
                raise AppError(409, "GENERATION_MISMATCH", f"SQLite 已提交历史顶层摘要非法: {rel_name}")
            fpath = resolve_within_root(bdir, str(rel_name))
            if fpath.is_symlink() or not fpath.is_file():
                raise AppError(409, "GENERATION_MISMATCH", f"历史批次已索引顶层文件缺失或非法: {rel_name}")
            raw = fpath.read_bytes()
            if hashlib.sha256(raw).hexdigest() != c_spec["sha256"]:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    f"历史批次顶层文件内容与 SQLite 已提交摘要不一致: {rel_name}",
                    {"file": str(rel_name)},
                )
            if c_spec.get("size") is not None and len(raw) != int(c_spec["size"]):
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    f"历史批次顶层文件大小与 SQLite 已提交摘要不一致: {rel_name}",
                    {"file": str(rel_name)},
                )
            verified_top[str(rel_name)] = raw
        return verified_top

    def _materialize_verified_check_workspace(
        self,
        user: AuthUser,
        batch: dict[str, Any],
        bdir: Path,
        *,
        can_holdout: bool,
        whole_holdout: bool,
        holdout_task_ids: set[str],
        check_smoke: bool,
        check_gateway_capture: bool,
        check_training_captures: bool,
    ) -> tuple[Path, int]:
        """Build an isolated 0700/0600 ephemeral verification snapshot from:
        1) Same-generation verified top-level bytes (raises 409 GENERATION_MISMATCH on ANY drift/new gen);
        2) Already-indexed SQLite `(batch_id, generation_id)` rows & verified `evidence_refs_json`;
        3) Per-task holdout authorization applied BEFORE reading any private evidence or spawning report.py;
        4) Strict `validate_safe_id(tid)` + `resolve_within_root()` containment on every written trial path.
        Returns `(tmp_root, hidden_holdout_task_count)`."""
        any_deep_check = bool(check_smoke or check_gateway_capture or check_training_captures)
        indexed_gen = str(batch.get("generation_id") or "")

        # 1. Verify top-level files match the exact indexed generation BEFORE reading any trial artifacts!
        verified_top = self._read_verified_top_level_snapshot(batch, bdir)

        # 2. Load indexed episodes strictly for (batch_id, generation_id)
        ep_rows = self.db.fetchall(
            "SELECT trial_id, task_id, evidence_refs_json FROM episodes WHERE batch_id = ? AND generation_id = ?",
            (batch["batch_id"], indexed_gen),
        )
        ep_by_tid: dict[str, dict[str, Any]] = {}
        ev_by_tid: dict[str, dict[str, Any]] = {}
        uses_private_store = False
        for r in ep_rows:
            tid = str(r["trial_id"])
            ep_by_tid[tid] = r
            refs = json.loads(r.get("evidence_refs_json") or "{}")
            ev_by_tid[tid] = refs
            for k in ("trajectory", "gateway", "training", "training_capture", "direct_qa"):
                rk = refs.get(k)
                if isinstance(rk, dict) and rk.get("storage_tier") == "evidence_store_0700":
                    uses_private_store = True

        # 3. Compute per-task holdout exclusions BEFORE reading ANY private evidence or passing to report.py!
        hidden_tids: set[str] = set()
        hidden_task_ids: set[str] = set()
        if not can_holdout:
            for tid, r in ep_by_tid.items():
                tk = str(r["task_id"])
                if whole_holdout or tk in holdout_task_ids or "holdout" in tk.lower():
                    hidden_tids.add(tid)
                    hidden_task_ids.add(tk)

        if any_deep_check and not can_holdout and (whole_holdout or (ep_by_tid and len(hidden_tids) == len(ep_by_tid))):
            raise AppError(
                403,
                "SCOPE_FORBIDDEN",
                "缺少 dataset:holdout_read 权限，禁止对留出集批次执行深度证据核验",
                {"missing_scope": "dataset:holdout_read"},
            )

        # 4. Enforce strict Scope + evidence-class approval BEFORE reading ANY deep-check evidence (legacy or v2)!
        ev_approval = str(batch.get("evidence_approval") or "pending")
        if any_deep_check:
            if check_smoke or check_training_captures:
                require_scope(user, "trace:read")
                if not self.approval_registry.is_evidence_class_approved(bdir, "atif_trajectory", ev_approval):
                    raise AppError(
                        403,
                        "SCOPE_FORBIDDEN",
                        "来源未批准 atif_trajectory 证据类，禁止读取轨迹或训练采集执行核验",
                        {"evidence_class": "atif_trajectory"},
                    )
                if check_smoke and batch.get("runner") == "vlmevalkit":
                    require_scope(user, "direct_vlm_qa:read")
                    if not self.approval_registry.is_evidence_class_approved(bdir, "direct_vlm_qa", ev_approval):
                        raise AppError(
                            403,
                            "SCOPE_FORBIDDEN",
                            "来源未批准 direct_vlm_qa 证据类，禁止读取多模态问答轨迹执行核验",
                            {"evidence_class": "direct_vlm_qa"},
                        )
            if check_gateway_capture:
                require_scope(user, "gateway:read")
                if not self.approval_registry.is_evidence_class_approved(bdir, "gateway_capture", ev_approval):
                    raise AppError(
                        403,
                        "SCOPE_FORBIDDEN",
                        "来源未批准 gateway_capture 证据类，禁止读取私有网关抓包执行核验",
                        {"evidence_class": "gateway_capture"},
                    )

        if uses_private_store and self.settings.evidence_store_dir is None:
            raise AppError(503, "STAGING_PERMISSION_UNSAFE", "隔离证据存储目录未配置")
        assert self.settings.staging_dir is not None
        probe_isolated_dir(self.settings.staging_dir, create=True)

        tmp_root = (self.settings.staging_dir / "verify-tmp" / f"ver_{secrets.token_hex(6)}").resolve()
        tmp_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(tmp_root, 0o700)

        try:
            for top_fn in ("manifest.json", "official-result.json"):
                if top_fn in verified_top:
                    dst = resolve_within_root(tmp_root, top_fn)
                    dst.write_bytes(verified_top[top_fn])
                    os.chmod(dst, 0o600)

            ep_bytes = verified_top.get("episodes.jsonl")
            if ep_bytes is None:
                raise AppError(404, "RESOURCE_NOT_FOUND", "批次缺少 episodes.jsonl")

            parsed_ep_rows: list[dict[str, Any]] = []
            seen_disk_tids: set[str] = set()
            for raw_ln in ep_bytes.decode("utf-8", errors="replace").splitlines():
                if not raw_ln.strip():
                    continue
                row = json.loads(raw_ln)
                if not isinstance(row, dict):
                    raise AppError(409, "GENERATION_MISMATCH", "episodes.jsonl 包含非对象行")
                tid = str(row.get("trial_id") or "")
                validate_safe_id(tid, "trial_id")
                if tid not in ep_by_tid:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"episodes.jsonl 中的 trial_id 未在已索引发布代中登记: {tid}",
                    )
                if str(row.get("task_id") or "") != str(ep_by_tid[tid]["task_id"]):
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        f"trial_id={tid} 的 task_id 与已索引发布代不一致",
                    )
                seen_disk_tids.add(tid)
                parsed_ep_rows.append(row)

            if seen_disk_tids != set(ep_by_tid.keys()):
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "episodes.jsonl 试验集合与已索引发布代不一致，请先重扫同步索引",
                )

            # Filter out unauthorized holdout trials BEFORE reading/materializing ANY trial file!
            # (When whole_holdout without deep checks, keep aggregate-only path handled safely below.)
            filter_mixed_before_subproc = bool(hidden_tids) and not (whole_holdout and not any_deep_check)
            trials_root = tmp_root / "trials"
            if any_deep_check:
                trials_root.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.chmod(trials_root, 0o700)

            rewritten_lines: list[str] = []
            kept_tids: set[str] = set()
            for row in parsed_ep_rows:
                tid = str(row["trial_id"])
                if filter_mixed_before_subproc and tid in hidden_tids:
                    # Never read result.json or private evidence for unauthorized holdout trials!
                    continue
                kept_tids.add(tid)

                if any_deep_check:
                    refs = ev_by_tid.get(tid) or {}
                    t_dir = resolve_within_root(trials_root, tid)
                    t_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                    os.chmod(t_dir, 0o700)

                    # Copy & verify public result.json strictly from indexed SQLite ref
                    res_ref = refs.get("result") if isinstance(refs.get("result"), dict) else None
                    res_rel = (res_ref.get("rel_path") if res_ref else None) or row.get("result")
                    if isinstance(res_rel, str) and res_rel:
                        res_root = (
                            self.settings.evidence_store_dir
                            if (res_ref and res_ref.get("storage_tier") == "evidence_store_0700")
                            else bdir
                        )
                        assert res_root is not None
                        res_bytes = self._verify_and_read_ref(
                            res_root, res_rel, res_ref.get("sha256") if res_ref else None
                        )
                        out_res = resolve_within_root(t_dir, "result.json")
                        out_res.write_bytes(res_bytes)
                        os.chmod(out_res, 0o600)
                        row["result"] = f"trials/{tid}/result.json"

                    # Materialize authorized & SHA-256 verified private artifacts requested by check flags
                    checks_map: list[tuple[bool, str, str, str]] = [
                        (check_smoke, "trajectory", "trajectory", "trajectory.json"),
                        (check_gateway_capture, "gateway", "gateway_trace", "model-gateway.json"),
                        (check_training_captures, "training", "training_trace", "training_trace.json"),
                    ]
                    for enabled, ref_key, ep_key, out_fname in checks_map:
                        if not enabled:
                            continue
                        r_spec = refs.get(ref_key)
                        if isinstance(r_spec, dict) and r_spec.get("rel_path"):
                            if ref_key == "training":
                                tr_rel_p = PurePosixPath(str(r_spec["rel_path"]))
                                if (
                                    tr_rel_p.is_absolute()
                                    or tr_rel_p.name != "training_trace.json"
                                    or tr_rel_p.parent.name != tid
                                ):
                                    raise AppError(
                                        400,
                                        "INVALID_PATH_OR_ID",
                                        f"训练采集轨迹路径非法: {r_spec['rel_path']}",
                                    )
                                if not isinstance(r_spec.get("sha256"), str) or not r_spec["sha256"]:
                                    raise AppError(
                                        409,
                                        "GENERATION_MISMATCH",
                                        "训练采集轨迹缺少已提交 SHA-256 摘要（旧版索引待回填），拒绝核验",
                                        {"trial_id": tid, "digest_backfill_required": True},
                                    )
                            src_root = (
                                self.settings.evidence_store_dir
                                if r_spec.get("storage_tier") == "evidence_store_0700"
                                else bdir
                            )
                            assert src_root is not None
                            blob = self._verify_and_read_ref(src_root, str(r_spec["rel_path"]), r_spec.get("sha256"))
                            out_f = resolve_within_root(t_dir, out_fname)
                            out_f.write_bytes(blob)
                            os.chmod(out_f, 0o600)
                            row[ep_key] = f"trials/{tid}/{out_fname}"
                            if ref_key == "training":
                                cap_spec = refs.get("training_capture")
                                if isinstance(cap_spec, dict) and cap_spec.get("rel_path"):
                                    cap_rel_p = PurePosixPath(str(cap_spec["rel_path"]))
                                    if (
                                        cap_rel_p.is_absolute()
                                        or cap_rel_p.name != "capture.json"
                                        or cap_rel_p.parent.name != tid
                                    ):
                                        raise AppError(
                                            400,
                                            "INVALID_PATH_OR_ID",
                                            f"训练采集元数据路径非法: {cap_spec['rel_path']}",
                                        )
                                    if not isinstance(cap_spec.get("sha256"), str) or not cap_spec["sha256"]:
                                        raise AppError(
                                            409,
                                            "GENERATION_MISMATCH",
                                            "训练采集 capture.json 缺少已提交 SHA-256 摘要（旧版索引待回填），拒绝核验",
                                            {"trial_id": tid, "digest_backfill_required": True},
                                        )
                                    cap_root = (
                                        self.settings.evidence_store_dir
                                        if cap_spec.get("storage_tier") == "evidence_store_0700"
                                        else bdir
                                    )
                                    assert cap_root is not None
                                    cap_blob = self._verify_and_read_ref(
                                        cap_root, str(cap_spec["rel_path"]), cap_spec.get("sha256")
                                    )
                                    cap_out = resolve_within_root(t_dir, "capture.json")
                                    cap_out.write_bytes(cap_blob)
                                    os.chmod(cap_out, 0o600)
                                elif r_spec.get("capture_present") is False:
                                    if r_spec.get("storage_tier") != "evidence_store_0700":
                                        sib_cap_rel = str(tr_rel_p.with_name("capture.json"))
                                        sib_cap_target = resolve_within_root(bdir, sib_cap_rel)
                                        if sib_cap_target.exists() or sib_cap_target.is_symlink():
                                            raise AppError(
                                                409,
                                                "GENERATION_MISMATCH",
                                                f"训练采集目录新增了未索引的 capture.json，请先重扫同步索引: {sib_cap_rel}",
                                            )
                                else:
                                    # Fail closed on pre-upgrade SQLite rows lacking bound capture.json digest (I-01 / E-02)
                                    raise AppError(
                                        409,
                                        "GENERATION_MISMATCH",
                                        "训练采集缺少已提交 capture.json 摘要绑定（旧版索引待回填），拒绝读取未绑定文件，请先完成热扫回填",
                                        {"trial_id": tid, "digest_backfill_required": True},
                                    )

                rewritten_lines.append(json.dumps(row, ensure_ascii=False) + "\n")

            # Write verified (and pre-filtered when mixed holdout) batch-status.json
            if "batch-status.json" in verified_top:
                bs_raw = verified_top["batch-status.json"]
                if filter_mixed_before_subproc:
                    bs_doc = json.loads(bs_raw.decode("utf-8", errors="replace"))
                    if isinstance(bs_doc, dict):
                        ta = bs_doc.get("task_attempts")
                        if isinstance(ta, dict) and ta:
                            kept_ta = {
                                k: v for k, v in ta.items()
                                if str(k) not in hidden_task_ids and "holdout" not in str(k).lower()
                            }
                            bs_doc["task_attempts"] = kept_ta
                            bs_doc["planned"] = sum(int(v) for v in kept_ta.values()) if kept_ta else len(kept_tids)
                        elif isinstance(bs_doc.get("planned"), int):
                            bs_doc["planned"] = max(len(kept_tids), bs_doc["planned"] - len(hidden_tids))
                        bs_doc["completed"] = len(kept_tids)
                        bs_raw = (json.dumps(bs_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                dst_bs = resolve_within_root(tmp_root, "batch-status.json")
                dst_bs.write_bytes(bs_raw)
                os.chmod(dst_bs, 0o600)

            out_ep = resolve_within_root(tmp_root, "episodes.jsonl")
            out_ep.write_text("".join(rewritten_lines), encoding="utf-8")
            os.chmod(out_ep, 0o600)
            return tmp_root, len(hidden_task_ids)
        except Exception:
            shutil.rmtree(tmp_root, ignore_errors=True)
            raise

    # ------------------------------------------------------------------
    # 1. Existing Batch Verification via Real framework/report.py
    # ------------------------------------------------------------------

    def verify_existing_batch(
        self,
        user: AuthUser,
        batch_id: str,
        *,
        check_smoke: bool = False,
        check_gateway_capture: bool = False,
        check_training_captures: bool = False,
        pass_threshold: float | None = None,
    ) -> dict[str, Any]:
        require_role(user, "researcher")
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

        ns = str(batch["source_namespace"] or "")
        loc = str(batch["source_locator"] or "")
        if ns == "harbor_runs" and loc.startswith("harbor_runs:"):
            bname = loc.split(":", 1)[1]
            bdir = resolve_within_root(self.settings.harbor_runs_dir, bname)
        elif ns == "runner_published" and loc.startswith("runner_published:") and self.settings.published_batches_dir is not None:
            bname = loc.split(":", 1)[1]
            bdir = resolve_within_root(self.settings.published_batches_dir, bname)
        else:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "report.py 仅支持共享导出格式批次（harbor-runs/ 或 runner_published/）；原生 Harbor 或历史 JSON 批次请使用离线校验原生适配器",
            )
        if not bdir.is_dir():
            raise AppError(404, "RESOURCE_NOT_FOUND", "共享盘上找不到该批次目录")

        report_py = resolve_verified_managed_script(self.settings, "report.py")

        # S-02 holdout check & per-task lookup BEFORE reading private evidence or spawning report.py!
        can_holdout = has_scope(user, "dataset:holdout_read")
        whole_holdout = is_holdout_dataset_or_batch(
            dataset_release_id=batch.get("dataset_release_id"),
            release_split=batch.get("rel_split"),
            benchmark_slug=batch.get("benchmark_slug"),
            raw_dataset_name=batch.get("raw_dataset_name"),
            batch_name=batch.get("batch_name"),
        )
        holdout_task_ids: set[str] = set()
        if not can_holdout and not whole_holdout and batch.get("dataset_release_id"):
            ho_rows = self.db.fetchall(
                """
                SELECT task_id FROM task_definitions
                WHERE dataset_release_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'
                """,
                (batch["dataset_release_id"],),
            )
            holdout_task_ids = {str(r["task_id"]) for r in ho_rows}

        verify_tmp, pre_hidden_holdout_tasks = self._materialize_verified_check_workspace(
            user,
            batch,
            bdir,
            can_holdout=can_holdout,
            whole_holdout=whole_holdout,
            holdout_task_ids=holdout_task_ids,
            check_smoke=check_smoke,
            check_gateway_capture=check_gateway_capture,
            check_training_captures=check_training_captures,
        )
        try:
            cmd = [sys.executable, "-B", str(report_py), str(verify_tmp), "--format", "json"]
            if pass_threshold is not None and finite(pass_threshold):
                cmd.extend(["--pass-threshold", str(pass_threshold)])
            if check_smoke:
                cmd.append("--check-smoke")
            if check_gateway_capture:
                cmd.append("--check-gateway-capture")
            if check_training_captures:
                cmd.append("--check-training-captures")

            proc = subprocess.run(
                cmd,
                env=build_sanitized_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            shutil.rmtree(verify_tmp, ignore_errors=True)
        parsed: dict[str, Any] = {}
        if proc.stdout.strip():
            try:
                parsed = json.loads(proc.stdout)
            except json.JSONDecodeError:
                parsed = {"raw_stdout": redact_text(proc.stdout[:4000])}

        # Defense-in-depth post-filter + redaction metadata (primary mixed-dataset pruning already ran BEFORE subprocess!)
        hidden_holdout_count = pre_hidden_holdout_tasks
        if not can_holdout and isinstance(parsed, dict):
            raw_tasks = parsed.get("tasks")
            if whole_holdout:
                if isinstance(raw_tasks, dict):
                    hidden_holdout_count = max(hidden_holdout_count, len(raw_tasks))
                    parsed["tasks"] = {}
                elif isinstance(raw_tasks, list):
                    hidden_holdout_count = max(hidden_holdout_count, len(raw_tasks))
                    parsed["tasks"] = []
                parsed["tasks_redacted"] = True
                parsed["hidden_holdout_tasks"] = hidden_holdout_count
                if isinstance(parsed.get("checks"), dict):
                    parsed["checks"] = {
                        k: v for k, v in parsed["checks"].items() if not isinstance(v, (list, dict))
                    }
            elif holdout_task_ids or pre_hidden_holdout_tasks > 0:
                if isinstance(raw_tasks, dict):
                    kept_dict = {}
                    for tk, tv in raw_tasks.items():
                        if str(tk) in holdout_task_ids or "holdout" in str(tk).lower():
                            hidden_holdout_count += 1
                        else:
                            kept_dict[tk] = tv
                    parsed["tasks"] = kept_dict
                elif isinstance(raw_tasks, list):
                    kept_list = []
                    for item in raw_tasks:
                        tid_val = str(item.get("task_id") or item.get("task") or "") if isinstance(item, dict) else str(item)
                        if tid_val in holdout_task_ids or "holdout" in tid_val.lower():
                            hidden_holdout_count += 1
                        else:
                            kept_list.append(item)
                    parsed["tasks"] = kept_list
                if hidden_holdout_count > 0:
                    parsed["tasks_redacted"] = True
                    parsed["hidden_holdout_tasks"] = hidden_holdout_count

        stderr_clean = redact_text(proc.stderr[:2000]) if proc.stderr else ""
        if not can_holdout and (whole_holdout or holdout_task_ids) and stderr_clean:
            for ht in holdout_task_ids:
                stderr_clean = stderr_clean.replace(ht, "<holdout-task-redacted>")
            if whole_holdout:
                stderr_clean = "<holdout-stderr-redacted>"

        cleaned = redact_json_recursive(parsed) if isinstance(parsed, dict) else {}
        return {
            "batch_id": batch_id,
            "batch_name": batch["batch_name"],
            "exit_code": proc.returncode,
            "passed_checks": proc.returncode == 0,
            "requested_checks": {
                "check_smoke": check_smoke,
                "check_gateway_capture": check_gateway_capture,
                "check_training_captures": check_training_captures,
            },
            "report": cleaned,
            "stderr": stderr_clean,
            "methodology_version": METHODOLOGY_VERSION,
        }

    # ------------------------------------------------------------------
    # 2. Offline Calculation (3-Format Strict Router + 0700 Staging)
    # ------------------------------------------------------------------

    def run_offline_calc(
        self,
        user: AuthUser,
        *,
        report_name: str,
        filename: str,
        raw_bytes: bytes,
        project_id: str = "default",
        dataset_name: str | None = None,
        model_name: str | None = None,
        pass_threshold: float | None = None,
    ) -> dict[str, Any]:
        require_role(user, "researcher")
        require_project_access(user, project_id, owner_id=user.user_id)
        if not report_name.strip():
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "离线报告名称不能为空")
        if len(raw_bytes) == 0:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "上传内容不能为空")
        if len(raw_bytes) > self.settings.max_upload_bytes:
            raise AppError(
                413,
                "PAYLOAD_TOO_LARGE",
                f"离线上传包超过 {self.settings.max_upload_bytes // (1024 * 1024)} MB 上限",
                {"max_bytes": self.settings.max_upload_bytes, "received_bytes": len(raw_bytes)},
            )

        # Fail-Closed 0700 staging directory probe (S-06)!
        assert self.settings.staging_dir is not None
        probe_isolated_dir(self.settings.staging_dir, create=True)

        report_id = f"off_{secrets.token_hex(6)}"
        stage_dir = self.settings.staging_dir / "offline-verify" / report_id
        stage_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(stage_dir, 0o700)

        try:
            _safe_extract_archive(raw_bytes, filename, stage_dir)
            upload_format, adapter_used, summary_obj, detected_ds, detected_model = self._dispatch_offline_format(
                stage_dir, pass_threshold=pass_threshold
            )
        except AppError:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", f"离线包解析失败: {redact_text(str(exc))}") from exc

        eff_ds = dataset_name or detected_ds
        eff_model = model_name or detected_model
        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO offline_reports (
                    report_id, project_id, owner_id, report_name, upload_format, adapter_used,
                    model_name, dataset_name, staging_ref, summary_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                """,
                (
                    report_id,
                    project_id,
                    user.user_id,
                    report_name.strip(),
                    upload_format,
                    adapter_used,
                    eff_model,
                    eff_ds,
                    f"offline-verify/{report_id}",
                    json.dumps(redact_json_recursive(summary_obj), ensure_ascii=False),
                    now,
                ),
            )
        return self.get_offline_report(user, report_id)

    def _dispatch_offline_format(
        self,
        stage_dir: Path,
        *,
        pass_threshold: float | None = None,
    ) -> tuple[str, str, dict[str, Any], str | None, str | None]:
        """Strictly route into the 3 formats specified by v2.2.1 02-8 §1:
        1. shared_batch_or_episodes -> real framework/report.py
        2. native_harbor_job -> native Harbor adapter (preserves reward_valid, exception_info.policy, 4-phase timings)
        3. summary_only -> passthrough summary ONLY (NEVER fabricates per-task details!)."""
        # Locate episodes.jsonl anywhere under stage_dir
        ep_files = sorted(stage_dir.rglob("episodes.jsonl"))
        if ep_files:
            ep_file = ep_files[0]
            target_arg = ep_file.parent if (ep_file.parent / "manifest.json").is_file() or (ep_file.parent / "batch-status.json").is_file() else ep_file
            report_py = resolve_verified_managed_script(self.settings, "report.py")
            cmd = [sys.executable, "-B", str(report_py), str(target_arg), "--format", "json"]
            if pass_threshold is not None and finite(pass_threshold):
                cmd.extend(["--pass-threshold", str(pass_threshold)])
            proc = subprocess.run(
                cmd,
                env=build_sanitized_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"shared_batch_or_episodes 未通过 report.py 校验: {redact_text(proc.stderr or proc.stdout)}",
                )
            rep = json.loads(proc.stdout)
            return (
                "shared_batch_or_episodes",
                "report_py",
                {"report_py": rep, "totals": rep.get("totals"), "tasks": rep.get("tasks") or []},
                rep.get("dataset"),
                rep.get("model"),
            )

        # Check for native Harbor job / trial result.json files
        json_files = sorted(stage_dir.rglob("*.json"))
        native_trials: list[dict[str, Any]] = []
        native_job_meta: dict[str, Any] = {}
        summary_docs: dict[str, Any] = {}

        for jf in json_files:
            try:
                doc = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            name_l = jf.name.lower()
            if name_l in ("batch-report.json", "official-result.json", "metrics.json"):
                summary_docs[jf.relative_to(stage_dir).as_posix()] = doc
                continue
            # Harbor job-level result.json with stats/n_total_trials
            if name_l == "result.json" and ("n_total_trials" in doc or "stats" in doc) and "task_name" not in doc:
                native_job_meta = doc
                continue
            # Harbor trial-level result.json (or uploaded single trial result)
            parsed_trial = _parse_native_harbor_trial_result(doc)
            if parsed_trial is not None:
                native_trials.append(parsed_trial)
                continue
            # Also detect summary-only JSON even if renamed (e.g. uploaded as report.json with totals or metric+score)
            if "totals" in doc or ("metric" in doc and "score" in doc and "denominator" in doc):
                summary_docs[jf.relative_to(stage_dir).as_posix()] = doc

        if native_trials:
            valid_rewards = [t["reward"] for t in native_trials if t["native_reward_valid"] and finite(t["reward"])]
            thr = float(pass_threshold) if pass_threshold is not None and finite(pass_threshold) else 1.0
            outcomes = Counter(t["status"] for t in native_trials)
            exc_with_valid = sum(1 for t in native_trials if t["native_reward_valid"] and t["exception_type"])
            planned = native_job_meta.get("n_total_trials") if isinstance(native_job_meta.get("n_total_trials"), int) else None
            summary = {
                "scoring_protocol": "harbor-native-reward-valid-v1",
                "totals": {
                    "planned": planned,
                    "recorded": len(native_trials),
                    "scored": len(valid_rewards),
                    "coverage": (len(valid_rewards) / planned) if planned else None,
                    "valid_mean": (math.fsum(valid_rewards) / len(valid_rewards)) if valid_rewards else None,
                    "passed": sum(1 for v in valid_rewards if v >= thr),
                    "pass_threshold": thr,
                    "outcomes": dict(sorted(outcomes.items())),
                    "native_valid_with_exception_count": exc_with_valid,
                },
                "trials": native_trials[:500],
            }
            return ("native_harbor_job", "native_harbor_adapter", summary, None, None)

        if summary_docs:
            # Summary-only passthrough: NEVER fabricate per-task records or missing denominators!
            first_doc = next(iter(summary_docs.values()))
            ds = first_doc.get("dataset") if isinstance(first_doc, dict) else None
            mdl = first_doc.get("model") if isinstance(first_doc, dict) else None
            totals = first_doc.get("totals") if isinstance(first_doc.get("totals"), dict) else None
            official = (
                {
                    "dataset": first_doc.get("dataset"),
                    "metric": first_doc.get("metric"),
                    "score": first_doc.get("score"),
                    "denominator": first_doc.get("denominator"),
                    "source": first_doc.get("source"),
                }
                if ("metric" in first_doc and "score" in first_doc)
                else first_doc.get("official_result")
            )
            return (
                "summary_only",
                "summary_passthrough",
                {
                    "summary_only": True,
                    "notice": "仅上传汇总文件（summary_only），仅原样展示已有汇总指标，不补齐缺失分母且不伪造逐题明细。",
                    "files": sorted(summary_docs.keys()),
                    "totals": totals,
                    "official_result": official,
                    "raw_summaries": summary_docs,
                    "tasks": [],
                },
                ds,
                mdl,
            )

        raise AppError(
            422,
            "SCHEMA_VALIDATION_FAILED",
            "无法识别离线上传包格式（支持 shared episodes.jsonl、原生 Harbor result.json 或汇总 batch-report/official-result.json）",
        )

    def list_offline_reports(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = sql_project_filter(user, "project_id")
        rows = self.db.fetchall(
            f"""
            SELECT report_id, project_id, owner_id, report_name, upload_format, adapter_used,
                   model_name, dataset_name, summary_json, status, created_at
            FROM offline_reports
            WHERE {proj_sql}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            params,
        )
        items = []
        for r in rows:
            r["summary"] = json.loads(r.pop("summary_json") or "{}")
            items.append(r)
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_offline_report(self, user: AuthUser, report_id: str) -> dict[str, Any]:
        validate_safe_id(report_id, "report_id")
        row = self.db.fetchone(
            """
            SELECT report_id, project_id, owner_id, report_name, upload_format, adapter_used,
                   model_name, dataset_name, summary_json, status, created_at
            FROM offline_reports
            WHERE report_id = ?
            """,
            (report_id,),
        )
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"离线报告不存在: {report_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        row["summary"] = json.loads(row.pop("summary_json") or "{}")
        row["methodology_version"] = METHODOLOGY_VERSION
        return row

    def delete_offline_report(self, user: AuthUser, report_id: str) -> dict[str, Any]:
        require_role(user, "researcher")
        validate_safe_id(report_id, "report_id")
        row = self.db.fetchone("SELECT project_id, owner_id, staging_ref FROM offline_reports WHERE report_id = ?", (report_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"离线报告不存在: {report_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM offline_reports WHERE report_id = ?", (report_id,))
        if row.get("staging_ref") and self.settings.staging_dir is not None:
            try:
                target = resolve_within_root(self.settings.staging_dir, str(row["staging_ref"]))
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            except AppError:
                pass
        return {"ok": True, "report_id": report_id}

    # ------------------------------------------------------------------
    # 3. Admin-Only Official Result Attachment (NEVER mutates run_scope!)
    # ------------------------------------------------------------------

    def attach_official_result(self, user: AuthUser, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(user, "admin")
        validate_safe_id(batch_id, "batch_id")
        batch = self.db.fetchone("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if batch is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"批次不存在: {batch_id}")

        dataset = str(payload.get("dataset") or batch.get("raw_dataset_name") or "").strip()
        metric = str(payload.get("metric") or "").strip()
        source = str(payload.get("source") or "").strip()
        score = payload.get("score")
        denominator = payload.get("denominator")
        fault_policy = str(payload.get("fault_policy") or "official_protocol").strip()

        if not dataset or not metric or not source or not finite(score) or type(denominator) is not int or denominator <= 0:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "官方分挂载需要合法的 dataset、metric、source、有限数值 score 及正整数 denominator",
            )

        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO official_results (
                    batch_id, dataset, metric, score, denominator, source, origin, fault_policy, attached_by, attached_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'manual_attach', ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    dataset = excluded.dataset,
                    metric = excluded.metric,
                    score = excluded.score,
                    denominator = excluded.denominator,
                    source = excluded.source,
                    origin = 'manual_attach',
                    fault_policy = excluded.fault_policy,
                    attached_by = excluded.attached_by,
                    attached_at = excluded.attached_at
                """,
                (batch_id, dataset, metric, float(score), denominator, source, fault_policy, user.username, now),
            )
            # Orthogonal contract (A-06): update official_status ONLY, NEVER touch run_scope!
            conn.execute(
                "UPDATE batches SET official_status = 'official_verified' WHERE batch_id = ?",
                (batch_id,),
            )

        updated = self.db.fetchone("SELECT batch_id, run_scope, official_status FROM batches WHERE batch_id = ?", (batch_id,))
        return {
            "ok": True,
            "batch_id": batch_id,
            "run_scope": updated["run_scope"] if updated else batch["run_scope"],
            "official_status": updated["official_status"] if updated else "official_verified",
            "official_result": {
                "dataset": dataset,
                "metric": metric,
                "score": float(score),
                "denominator": denominator,
                "source": source,
                "origin": "manual_attach",
                "fault_policy": fault_policy,
                "attached_by": user.username,
                "attached_at": now,
            },
            "methodology_version": METHODOLOGY_VERSION,
        }

    # ------------------------------------------------------------------
    # 4. Real-Batch Param Compare Groups (/toolkit/params)
    # ------------------------------------------------------------------

    def _hydrate_param_group(self, user: AuthUser, row: dict[str, Any]) -> dict[str, Any]:
        batch_ids = [str(b) for b in json.loads(row.get("batch_ids_json") or "[]")]
        hydrated_rows: list[dict[str, Any]] = []
        for bid in batch_ids:
            b = self.db.fetchone(
                """
                SELECT b.batch_id, b.batch_name, b.project_id, b.owner_id, b.runner, b.harness, b.harness_version,
                       b.model_route, b.model_canonical, b.checkpoint, b.thinking_mode, b.run_scope,
                       b.scoring_protocol, b.official_status, b.planned, b.recorded, b.scored, b.valid_mean,
                       b.run_config_json, b.usage_json, b.failure_kinds_json, b.dataset_release_id,
                       o.metric AS official_metric, o.score AS official_score, o.denominator AS official_denominator
                FROM batches b
                LEFT JOIN official_results o ON o.batch_id = b.batch_id
                WHERE b.batch_id = ?
                """,
                (bid,),
            )
            if b is None:
                continue
            try:
                require_project_access(user, b["project_id"], owner_id=b["owner_id"])
            except AppError:
                continue
            ep_stats = self.db.fetchone(
                """
                SELECT AVG(duration_sec) AS avg_duration_sec,
                       AVG(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN input_tokens + output_tokens END) AS avg_total_tokens,
                       SUM(CASE WHEN status NOT IN ('scored', 'unscored') THEN 1 ELSE 0 END) AS failed_trials
                FROM episodes
                WHERE batch_id = ?
                """,
                (bid,),
            ) or {}
            rcfg = json.loads(b.get("run_config_json") or "{}") if b.get("run_config_json") else {}
            hydrated_rows.append(
                {
                    "batch_id": b["batch_id"],
                    "batch_name": b["batch_name"],
                    "dataset_release_id": b["dataset_release_id"],
                    "runner": b["runner"],
                    "harness": b["harness"],
                    "harness_version": b["harness_version"],
                    "model_route": b["model_route"],
                    "model_canonical": b["model_canonical"],
                    "checkpoint": b["checkpoint"],
                    "thinking_mode": b["thinking_mode"],
                    "run_scope": b["run_scope"],
                    "scoring_protocol": b["scoring_protocol"],
                    "official_status": b["official_status"],
                    "official_score": b["official_score"],
                    "official_metric": b["official_metric"],
                    "official_denominator": b["official_denominator"],
                    "valid_mean": b["valid_mean"],
                    "scored": b["scored"],
                    "planned": b["planned"],
                    "recorded": b["recorded"],
                    "avg_duration_sec": round(float(ep_stats["avg_duration_sec"]), 2) if ep_stats.get("avg_duration_sec") is not None else None,
                    "avg_total_tokens": round(float(ep_stats["avg_total_tokens"]), 1) if ep_stats.get("avg_total_tokens") is not None else None,
                    "failed_trials": int(ep_stats.get("failed_trials") or 0),
                    "run_config": rcfg,
                }
            )
        return {
            "group_id": row["group_id"],
            "project_id": row["project_id"],
            "owner_id": row["owner_id"],
            "test_topic": row["test_topic"],
            "label": row["label"],
            "dataset_release_id": row["dataset_release_id"],
            "batch_ids": [r["batch_id"] for r in hydrated_rows],
            "notes": row.get("notes"),
            "created_at": row["created_at"],
            "batches": hydrated_rows,
        }

    def list_param_groups(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = sql_project_filter(user, "project_id")
        rows = self.db.fetchall(
            f"""
            SELECT * FROM param_compare_groups
            WHERE {proj_sql}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            params,
        )
        items = [self._hydrate_param_group(user, r) for r in rows]
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def create_param_group(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(user, "researcher")
        project_id = str(payload.get("project_id") or "default").strip()
        require_project_access(user, project_id, owner_id=user.user_id)
        test_topic = str(payload.get("test_topic") or "").strip()
        label = str(payload.get("label") or "").strip()
        if not test_topic or not label:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "test_topic 与 label 不能为空")

        raw_bids = payload.get("batch_ids") or []
        if not isinstance(raw_bids, list) or len(raw_bids) < 2 or len(raw_bids) > 12:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "参数对照台账必须绑定 2–12 个平台真实已索引 batch_id")

        verified_bids: list[str] = []
        releases: list[str] = []
        for raw_bid in raw_bids:
            bid = str(raw_bid).strip()
            validate_safe_id(bid, "batch_id")
            b = self.db.fetchone("SELECT batch_id, project_id, owner_id, dataset_release_id FROM batches WHERE batch_id = ?", (bid,))
            if b is None:
                raise AppError(404, "RESOURCE_NOT_FOUND", f"绑定的批次不存在（禁止手填虚构批次）: {bid}")
            require_project_access(user, b["project_id"], owner_id=b["owner_id"])
            if bid not in verified_bids:
                verified_bids.append(bid)
            if b.get("dataset_release_id"):
                releases.append(str(b["dataset_release_id"]))

        if len(verified_bids) < 2:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "去重后至少需要 2 个不同的真实 batch_id")

        rel_id = str(payload.get("dataset_release_id") or (releases[0] if releases else "multi")).strip()
        group_id = f"pcg_{secrets.token_hex(6)}"
        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO param_compare_groups (
                    group_id, project_id, owner_id, test_topic, label, dataset_release_id, batch_ids_json, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    project_id,
                    user.user_id,
                    test_topic,
                    label,
                    rel_id,
                    json.dumps(verified_bids, ensure_ascii=False),
                    payload.get("notes"),
                    now,
                ),
            )
        row = self.db.fetchone("SELECT * FROM param_compare_groups WHERE group_id = ?", (group_id,))
        assert row is not None
        out = self._hydrate_param_group(user, row)
        out["methodology_version"] = METHODOLOGY_VERSION
        return out

    def delete_param_group(self, user: AuthUser, group_id: str) -> dict[str, Any]:
        require_role(user, "researcher")
        validate_safe_id(group_id, "group_id")
        row = self.db.fetchone("SELECT project_id, owner_id FROM param_compare_groups WHERE group_id = ?", (group_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"参数对照台账不存在: {group_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM param_compare_groups WHERE group_id = ?", (group_id,))
        return {"ok": True, "group_id": group_id}

    # ------------------------------------------------------------------
    # 5. Manuals Center (/toolkit/manuals — real 197 framework docs!)
    # ------------------------------------------------------------------

    def _manual_catalog(self) -> list[dict[str, Any]]:
        fw = self.settings.framework_dir
        ds = self.settings.datasets_dir
        docs = [
            ("framework-readme", "框架总览 (framework/README.md)", "framework", fw / "README.md"),
            ("framework-setup", "环境部署与依赖安装 (SETUP.md)", "framework", fw / "SETUP.md"),
            ("dataset-format", "数据集与 Direct 格式规范 (DATASET_FORMAT.md)", "framework", fw / "DATASET_FORMAT.md"),
            ("vlmevalkit-guide", "VLMEvalKit 多模态运行器手册 (VLMEVALKIT.md)", "framework", fw / "VLMEVALKIT.md"),
            ("datasets-readme", "197 共享题库说明 (datasets/README.md)", "datasets", ds / "README.md"),
        ]
        out: list[dict[str, Any]] = []
        for doc_id, title, category, path in docs:
            if path.is_file():
                text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
                out.append(
                    {
                        "doc_id": doc_id,
                        "title": title,
                        "category": category,
                        "size_bytes": path.stat().st_size,
                        "content": text,
                    }
                )
        return out

    def list_manuals(self, q: str | None = None) -> dict[str, Any]:
        kw = (q or "").strip().lower()
        items = []
        for doc in self._manual_catalog():
            text = doc["content"]
            headings = [
                {"level": len(m.group(1)), "title": m.group(2).strip(), "anchor": _slugify_heading(m.group(2).strip())}
                for m in _HEADING_RE.finditer(text)
            ]
            snippet = None
            if kw:
                idx = text.lower().find(kw)
                if idx == -1 and kw not in doc["title"].lower():
                    continue
                if idx >= 0:
                    start = max(0, idx - 60)
                    snippet = text[start : idx + 140].replace("\n", " ")
            items.append(
                {
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "category": doc["category"],
                    "size_bytes": doc["size_bytes"],
                    "heading_count": len(headings),
                    "snippet": snippet,
                }
            )
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_manual(self, doc_id: str) -> dict[str, Any]:
        validate_safe_id(doc_id, "doc_id")
        for doc in self._manual_catalog():
            if doc["doc_id"] == doc_id:
                text = doc["content"]
                toc = [
                    {"level": len(m.group(1)), "title": m.group(2).strip(), "anchor": _slugify_heading(m.group(2).strip())}
                    for m in _HEADING_RE.finditer(text)
                ]
                return {
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "category": doc["category"],
                    "size_bytes": doc["size_bytes"],
                    "toc": toc,
                    "content": text,
                    "methodology_version": METHODOLOGY_VERSION,
                }
        raise AppError(404, "RESOURCE_NOT_FOUND", f"手册文档不存在: {doc_id}")
