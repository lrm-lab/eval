from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import tomllib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import yaml

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.identity import (
    canonicalize_model,
    determine_run_scope,
    infer_scoring_protocol,
    make_batch_id,
    make_canonical_trial_key,
    resolve_dataset_identity,
    source_priority_rank,
)
from app.security.redaction import (
    compute_internal_source_fingerprint,
    redact_host,
    redact_path,
    redact_text,
)
from app.security.storage_probe import SAFE_ID_RE, resolve_within_root
from app.services.methodology import TokenSemanticsValidator, finite, normalize_score_to_100

_DATA_IMAGE_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]{16,})")

_WATCHED_TOP_FILES = frozenset(
    {
        "publish-manifest.json",
        "manifest.json",
        "batch-status.json",
        "batch-report.json",
        "episodes.jsonl",
        "official-result.json",
        "selection.json",
        "run-config.json",
        "trial-digests.jsonl",
    }
)

_MANDATORY_MANIFEST_FILES = (
    "manifest.json",
    "episodes.jsonl",
    "batch-status.json",
    "batch-report.json",
    "trial-digests.jsonl",
)

_OPTIONAL_MANIFEST_FILES = (
    "official-result.json",
    "vlmevalkit/metrics.json",
    "selection.json",
    "run-config.json",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _stat_tuple(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _training_ref_needs_backfill(ev_refs_json: str | None) -> bool:
    """Return True if an indexed episode's training capture ref lacks bound SHA-256 digests
    for training_trace.json or sibling capture.json (pre-rev5 SQLite upgrade detection, I-01 / E-02)."""
    try:
        refs = json.loads(ev_refs_json or "{}")
    except Exception:
        return False
    if not isinstance(refs, dict):
        return False
    tr = refs.get("training")
    if not isinstance(tr, dict) or not tr.get("rel_path"):
        return False
    if not isinstance(tr.get("sha256"), str) or not tr["sha256"]:
        return True
    cap = refs.get("training_capture")
    if isinstance(cap, dict) and cap.get("rel_path"):
        return not (isinstance(cap.get("sha256"), str) and bool(cap["sha256"]))
    if tr.get("capture_present") is False:
        return False
    return True


def _batch_needs_backfill(
    checks_json_str: str | None,
    generation_id: str | None,
    is_running_placeholder: int | bool,
    *,
    runner: str,
    has_unbackfilled_training: bool = False,
) -> bool:
    """Identify committed batches that need digest or legacy VLMEvalKit media backfill."""
    if is_running_placeholder:
        return False
    if has_unbackfilled_training:
        return True
    try:
        checks = json.loads(checks_json_str or "{}")
    except Exception:
        return True
    if not isinstance(checks, dict):
        return True
    top_files = checks.get("top_level_files")
    if not isinstance(top_files, dict) or not top_files:
        return True
    gen = str(generation_id or "")
    manifest_mode = str(checks.get("manifest_mode") or "")
    is_v2 = manifest_mode == "publish_manifest_v2" or (
        manifest_mode != "legacy_v1"
        and bool(gen)
        and not gen.startswith(("gen_legacy_", "gen_placeholder_"))
    )
    if is_v2:
        pm_sha = checks.get("publish_manifest_sha256")
        return not isinstance(pm_sha, str) or not pm_sha
    return runner == "vlmevalkit" and checks.get("legacy_vlm_media_indexed") is not True


def _dir_content_digest(batch_dir: Path) -> str:
    """Single-scandir digest for 15s incremental polling (< 200ms across all harbor-runs on CIFS).
    Explicitly ignores `.publish.lock` (v2.2.1 I-06 / run.py:64)."""
    items: list[str] = []
    has_vlm_dir = False
    with os.scandir(batch_dir) as it:
        for entry in it:
            name = entry.name
            if name == "vlmevalkit" and entry.is_dir(follow_symlinks=False):
                has_vlm_dir = True
                continue
            if name in _WATCHED_TOP_FILES:
                try:
                    st = entry.stat(follow_symlinks=False)
                    items.append(f"{name}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    pass
    if has_vlm_dir:
        st_v = _stat_tuple(batch_dir / "vlmevalkit" / "metrics.json")
        if st_v is not None:
            items.append(f"vlmevalkit/metrics.json:{st_v[0]}:{st_v[1]}:{st_v[2]}")
    items.sort()
    return hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()


def _recompute_report_summary(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Exact report.py-compatible deterministic aggregate computation (A-01 & I-01)."""
    task_attempts = progress.get("task_attempts") or {}
    if not isinstance(task_attempts, dict):
        task_attempts = {}
    planned = progress.get("planned")
    if planned is None and task_attempts:
        try:
            planned = sum(int(v) for v in task_attempts.values())
        except (TypeError, ValueError):
            planned = None

    dataset = meta.get("dataset")
    if dataset is None and rows:
        dataset = rows[0].get("dataset")

    valid_scores = [r["reward"] for r in rows if r.get("status") == "scored" and finite(r.get("reward"))]
    pass_threshold: float | None = None
    if valid_scores and all(x in (0, 1) for x in valid_scores) and dataset != "wildclawbench-harbor":
        pass_threshold = 1.0

    def _count_summary(sub_rows: list[dict[str, Any]], sub_planned: int | None) -> dict[str, Any]:
        outcomes: Counter[str] = Counter()
        scores: list[float] = []
        for r in sub_rows:
            status = str(r.get("status") or "unknown")
            if status == "scored":
                if finite(r.get("reward")):
                    scores.append(float(r["reward"]))
                else:
                    status = "invalid_reward"
            outcomes[status] += 1
        if isinstance(sub_planned, int) and sub_planned > len(sub_rows):
            outcomes["unrecorded"] = sub_planned - len(sub_rows)
        total = math.fsum(scores)
        return {
            "planned": sub_planned,
            "recorded": len(sub_rows),
            "scored": len(scores),
            "coverage": (len(scores) / sub_planned) if (isinstance(sub_planned, int) and sub_planned > 0) else None,
            "reward_sum": total,
            "valid_mean": (total / len(scores)) if scores else None,
            "passed": sum(v >= pass_threshold for v in scores) if pass_threshold is not None else None,
            "pass_threshold": pass_threshold,
            "outcomes": dict(sorted(outcomes.items())),
        }

    totals = _count_summary(rows, planned if isinstance(planned, int) else None)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[str(r["task_id"])].append(r)
    tasks = {
        tid: _count_summary(groups[tid], task_attempts.get(tid))
        for tid in sorted(set(groups.keys()) | set(task_attempts.keys()))
    }
    usage: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "cost_usd", "duration_seconds"):
        vals = [(r.get("usage") or {}).get(key) for r in rows]
        vals = [float(v) for v in vals if finite(v) and float(v) >= 0]
        usage[key] = {"observed": len(vals), "total": math.fsum(vals) if vals else None}

    failure_kinds = dict(
        sorted(
            Counter(
                (r.get("failure") or {}).get("kind") or r["exception_type"]
                for r in rows
                if r.get("exception_type")
            ).items()
        )
    )
    return {
        "totals": totals,
        "tasks": tasks,
        "usage": usage,
        "failure_kinds": failure_kinds,
    }


def _reports_agree(published: dict[str, Any], recomputed: dict[str, Any]) -> bool:
    """Compare deterministic recomputable aggregates between batch-report.json and episodes.jsonl (I-01)."""
    pt = published.get("totals")
    if not isinstance(pt, dict):
        return False
    rt = recomputed["totals"]
    if rt.get("planned") is not None and pt.get("planned") != rt.get("planned"):
        return False
    for k in ("recorded", "scored", "passed"):
        if pt.get(k) != rt.get(k):
            return False
    if pt.get("outcomes") != rt.get("outcomes"):
        return False
    for fk in ("reward_sum", "valid_mean"):
        pv, rv = pt.get(fk), rt.get(fk)
        if (pv is None) != (rv is None):
            return False
        if pv is not None and rv is not None and abs(float(pv) - float(rv)) > 1e-6:
            return False
    pub_tasks = published.get("tasks")
    if not isinstance(pub_tasks, dict):
        return False
    rec_tasks = recomputed["tasks"]
    if set(pub_tasks.keys()) != set(rec_tasks.keys()):
        return False
    for tid, rt_item in rec_tasks.items():
        pt_item = pub_tasks.get(tid)
        if not isinstance(pt_item, dict):
            return False
        if pt_item.get("recorded") != rt_item.get("recorded") or pt_item.get("scored") != rt_item.get("scored"):
            return False
    return True


def _extract_atif_metrics(
    traj_path: Path, rel_traj: str, *, raw_bytes: bytes | None = None
) -> dict[str, Any]:
    """Extract reasoning_tokens / reasoning_output_tokens dual keys, cached tokens, tool_call_count,
    and inline media SHA-256 digests without storing blobs (A-12, I-01).
    Accepts pre-verified `raw_bytes` to guarantee zero double-read TOCTOU."""
    out: dict[str, Any] = {
        "reasoning_tokens": None,
        "reasoning_key_source": None,
        "cached_tokens": None,
        "tool_call_count": None,
        "has_inline_media": False,
        "inline_media_meta": [],
        "file_sha256": None,
    }
    if raw_bytes is None:
        try:
            raw_bytes = traj_path.read_bytes()
        except OSError:
            return out

    file_sha = _sha256_bytes(raw_bytes)
    out["file_sha256"] = file_sha
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    for idx, m in enumerate(_DATA_IMAGE_RE.finditer(raw_text)):
        mime = m.group(1)
        b64_data = re.sub(r"\s+", "", m.group(2))
        out["has_inline_media"] = True
        out["inline_media_meta"].append(
            {
                "media_idx": idx,
                "source": "trajectory_inline_base64",
                "rel_path": rel_traj,
                "inline_ordinal": idx,
                "mime": mime,
                "file_sha256": file_sha,
                "sha256": hashlib.sha256(b64_data.encode("ascii")).hexdigest(),
            }
        )

    try:
        data = json.loads(raw_text)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    fm = data.get("final_metrics") or {}
    if isinstance(fm, dict):
        if isinstance(fm.get("total_cached_tokens"), int) and fm["total_cached_tokens"] >= 0:
            out["cached_tokens"] = fm["total_cached_tokens"]
        elif isinstance(fm.get("cached_tokens"), int) and fm["cached_tokens"] >= 0:
            out["cached_tokens"] = fm["cached_tokens"]

    steps = data.get("steps")
    if isinstance(steps, list):
        tool_calls = 0
        has_tool_field = False
        r_sum = 0
        r_found = False
        r_key_src: str | None = None
        for step in steps:
            if not isinstance(step, dict):
                continue
            tcs = step.get("tool_calls")
            if isinstance(tcs, list):
                has_tool_field = True
                tool_calls += len(tcs)
            metrics = step.get("metrics") or {}
            extra = metrics.get("extra") if isinstance(metrics, dict) else None
            for container in (extra, metrics):
                if not isinstance(container, dict):
                    continue
                if isinstance(container.get("reasoning_tokens"), int) and container["reasoning_tokens"] >= 0:
                    r_sum += container["reasoning_tokens"]
                    r_found = True
                    r_key_src = r_key_src or "reasoning_tokens"
                    break
                if isinstance(container.get("reasoning_output_tokens"), int) and container["reasoning_output_tokens"] >= 0:
                    r_sum += container["reasoning_output_tokens"]
                    r_found = True
                    r_key_src = r_key_src or "reasoning_output_tokens"
                    break
        if has_tool_field or tool_calls > 0:
            out["tool_call_count"] = tool_calls
        if r_found:
            out["reasoning_tokens"] = r_sum
            out["reasoning_key_source"] = r_key_src
    return out


def _inspect_trial_result_and_media(
    batch_dir: Path,
    rel_result: str | None,
    trial_id: str,
    *,
    raw_result_bytes: bytes | None = None,
    verified_media_map: dict[str, str] | None = None,
    skip_disk_media_scan: bool = False,
) -> dict[str, Any]:
    """Inspect trial result.json & media/ dir for timestamps, http_status, direct/vlm tokens,
    and full SHA-256 media metadata (stripping raw base64 blobs from SQLite, A-10 & I-01).
    When `raw_result_bytes` / `verified_media_map` are provided (from verified publish-manifest),
    parses the exact verified bytes without re-reading from disk (zero TOCTOU)."""
    info: dict[str, Any] = {
        "started_at": None,
        "finished_at": None,
        "http_status": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "reasoning_key_source": None,
        "has_inline_media": False,
        "media_refs": [],
        "phase_timings": None,
        "has_verifier_output": False,
        "result_sha256": None,
    }
    try:
        trial_dir = resolve_within_root(batch_dir, f"trials/{trial_id}")
    except AppError:
        trial_dir = None
    if verified_media_map is not None:
        for idx, (m_rel, m_sha) in enumerate(sorted(verified_media_map.items())):
            info["has_inline_media"] = True
            info["media_refs"].append(
                {
                    "media_idx": idx,
                    "source": "trial_media_dir",
                    "rel_path": m_rel,
                    "filename": Path(m_rel).name,
                    "sha256": m_sha,
                }
            )
    elif not skip_disk_media_scan and trial_dir is not None:
        media_dir = trial_dir / "media"
        if media_dir.is_dir():
            try:
                for idx, mf in enumerate(sorted(p for p in media_dir.iterdir() if p.is_file())):
                    m_sha, _ = _sha256_file(mf)
                    info["has_inline_media"] = True
                    info["media_refs"].append(
                        {
                            "media_idx": idx,
                            "source": "trial_media_dir",
                            "rel_path": f"trials/{trial_id}/media/{mf.name}",
                            "filename": mf.name,
                            "sha256": m_sha,
                        }
                    )
            except OSError:
                pass

    if not skip_disk_media_scan and trial_dir is not None and (trial_dir / "verifier-output.txt").is_file():
        info["has_verifier_output"] = True

    if not rel_result:
        return info
    if raw_result_bytes is not None:
        raw_bytes = raw_result_bytes
    else:
        try:
            res_path = resolve_within_root(batch_dir, rel_result)
        except AppError:
            return info
        try:
            raw_bytes = res_path.read_bytes()
        except OSError:
            return info

    res_sha = _sha256_bytes(raw_bytes)
    info["result_sha256"] = res_sha
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    base_idx = len(info["media_refs"])
    for offset, m in enumerate(_DATA_IMAGE_RE.finditer(raw_text)):
        mime = m.group(1)
        b64_data = re.sub(r"\s+", "", m.group(2))
        info["has_inline_media"] = True
        info["media_refs"].append(
            {
                "media_idx": base_idx + offset,
                "source": "result_inline_base64",
                "rel_path": rel_result,
                "inline_ordinal": offset,
                "mime": mime,
                "file_sha256": res_sha,
                "sha256": hashlib.sha256(b64_data.encode("ascii")).hexdigest(),
            }
        )

    try:
        obj = json.loads(raw_text)
    except Exception:
        return info
    if not isinstance(obj, dict):
        return info

    info["started_at"] = obj.get("started_at")
    info["finished_at"] = obj.get("finished_at")
    vlm = obj.get("vlmeval_result")
    if isinstance(vlm, dict) and isinstance(vlm.get("http_status"), int):
        info["http_status"] = vlm["http_status"]
    dr = obj.get("direct_result")
    if isinstance(dr, dict):
        du = dr.get("usage") or {}
        if isinstance(du, dict):
            if isinstance(du.get("cached_tokens"), int):
                info["cached_tokens"] = du["cached_tokens"]
            if isinstance(du.get("reasoning_tokens"), int):
                info["reasoning_tokens"] = du["reasoning_tokens"]
                info["reasoning_key_source"] = "reasoning_tokens"
    return info


@contextmanager
def _submission_dir(state_dir: Path, *, create: bool = False) -> Iterator[int]:
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    state = os.open(state_dir, flags)
    try:
        st = os.fstat(state)
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise PermissionError("Expected private submission state directory")
        if create:
            try:
                os.mkdir("runner-submissions", 0o700, dir_fd=state)
                os.fsync(state)
            except FileExistsError:
                pass
        directory = os.open("runner-submissions", flags, dir_fd=state)
        try:
            st = os.fstat(directory)
            if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise PermissionError("Expected private runner-submissions directory")
            yield directory
        finally:
            os.close(directory)
    finally:
        os.close(state)


def _check_submission_file(st: os.stat_result) -> None:
    if (
        not stat.S_ISREG(st.st_mode)
        or stat.S_IMODE(st.st_mode) != 0o600
        or st.st_uid != os.geteuid()
        or st.st_nlink != 1
    ):
        raise PermissionError("Expected private regular submission file")


def _submission_filename(batch_name: str) -> str:
    # Batch names support consecutive dots within a single filename component.
    if not SAFE_ID_RE.fullmatch(batch_name):
        raise AppError(400, "INVALID_PATH_OR_ID", "非法路径标识 batch_name", {"field": "batch_name"})
    return f"{batch_name}.json"


def write_runner_submission(state_dir: Path, doc: dict[str, Any]) -> None:
    """Replace the ledger entry atomically, relative to a pinned private directory."""
    name = _submission_filename(doc["batch_name"])
    with _submission_dir(state_dir, create=True) as directory:
        try:
            _check_submission_file(os.stat(name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        tmp = f".submission-{secrets.token_hex(16)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(fd, 0o600)
                json.dump(doc, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(fd)
            os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(tmp, dir_fd=directory)
            except FileNotFoundError:
                pass


class SourceApprovalRegistry:
    """Granular directory/prefix approval registry enforcing `approved_evidence_classes` per evidence class."""

    def __init__(self, config_file: Path) -> None:
        self.config_file = config_file
        self.default_project_id = "default"
        self.default_owner_id = "system"
        self.sources: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        if self.config_file.is_file():
            cfg = yaml.safe_load(self.config_file.read_text(encoding="utf-8")) or {}
            self.default_project_id = cfg.get("default_project_id", "default")
            self.default_owner_id = cfg.get("default_owner_id", "system")
            self.sources = cfg.get("sources", [])
        else:
            self.sources = []

    def lookup_full(self, target_path: Path) -> tuple[str, str, str, frozenset[str]]:
        """Return (project_id, owner_id, approval_status, approved_evidence_classes) for target_path.
        Unmatched directories default strictly to pending with EMPTY approved_evidence_classes."""
        resolved = str(target_path.resolve())
        best: dict[str, Any] | None = None
        best_len = -1
        for entry in self.sources:
            raw_root = str(entry.get("source_root") or "")
            if not raw_root:
                continue
            mode = str(entry.get("match_mode", "dir"))
            if mode == "prefix":
                p_raw = Path(raw_root)
                norm_prefix = (
                    str(p_raw.parent.resolve() / p_raw.name)
                    if p_raw.parent.exists()
                    else raw_root
                )
                if resolved.startswith(norm_prefix) and len(norm_prefix) > best_len:
                    best = entry
                    best_len = len(norm_prefix)
            else:
                root = str(Path(raw_root).resolve())
                if (resolved == root or resolved.startswith(root.rstrip("/") + "/")) and len(root) > best_len:
                    best = entry
                    best_len = len(root)
        if best is not None:
            classes = frozenset(str(c) for c in (best.get("approved_evidence_classes") or []))
            return (
                str(best.get("project_id", self.default_project_id)),
                str(best.get("owner_id", self.default_owner_id)),
                str(best.get("approval_status", "pending")),
                classes,
            )
        return self.default_project_id, self.default_owner_id, "pending", frozenset()

    def lookup(self, target_path: Path) -> tuple[str, str, str]:
        p, o, s, _ = self.lookup_full(target_path)
        return p, o, s

    def is_evidence_class_approved(self, target_path: Path | None, evidence_class: str, batch_approval: str) -> bool:
        if batch_approval == "pending":
            return False
        if target_path is None:
            return batch_approval in ("approved_public", "approved_restricted")
        _, _, status, classes = self.lookup_full(target_path)
        if status == "pending":
            return False
        return evidence_class in classes


class HotIndexer:
    """Hot-tier atomic indexer (< 5s initial full scan, < 200ms 15s incremental poll)."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.token_validator = TokenSemanticsValidator(settings.token_semantics_file)
        self.approval_registry = SourceApprovalRegistry(settings.source_approval_file)

    def _record_warning(
        self,
        conn: sqlite3.Connection,
        *,
        tier: str,
        severity: str,
        code: str,
        target_path: str,
        message: str,
    ) -> None:
        redacted_target = redact_path(target_path) or "<redacted>"
        redacted_msg = redact_text(message) or ""
        conn.execute(
            """
            INSERT INTO index_warnings (tier, severity, code, target_path, message, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tier, severity, code, redacted_target, redacted_msg, utcnow_iso()),
        )

    def _verify_publish_manifest(
        self, batch_dir: Path, manifest_path: Path, *, prefetched_pm_bytes: bytes | None = None
    ) -> tuple[bool, dict[str, Any] | None, dict[str, bytes], dict[str, dict[str, Any]], dict[str, bytes], str | None]:
        """Strict atomic verification of schema_version=2 publish-manifest.json (v2.2.1 05-1 §2.2 & I-01):
        - Requires mandatory top-level files to be listed and hash-verified.
        - Requires every known optional file to be explicitly in top_level_files OR absent_optional_files
          (rejects undeclared optional files on disk and prevents reading unverified optional files!).
        - Returns the exact verified in-memory bytes for BOTH top-level files and trial artifacts
          so callers NEVER double-read from disk (zero TOCTOU).
        - Defensively rejects non-object JSON lines, invalid digest specs, and path traversals in
          trial-digests.jsonl without ever raising unhandled AttributeError/TypeError."""
        verified_bytes: dict[str, bytes] = {}
        trial_digests_map: dict[str, dict[str, Any]] = {}
        verified_trial_bytes: dict[str, bytes] = {}
        try:
            raw_pm = prefetched_pm_bytes if prefetched_pm_bytes is not None else manifest_path.read_bytes()
            pm = json.loads(raw_pm.decode("utf-8"))
        except Exception as exc:
            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"publish-manifest.json parse error: {exc}"
        if not isinstance(pm, dict) or not isinstance(pm.get("generation_id"), str) or not pm["generation_id"].strip():
            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, "publish-manifest.json missing generation_id"

        top_files = pm.get("top_level_files")
        if not isinstance(top_files, dict) or not top_files:
            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, "publish-manifest.json missing top_level_files"

        absent_raw = pm.get("absent_optional_files")
        if not isinstance(absent_raw, list):
            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, "publish-manifest.json missing absent_optional_files list"
        absent_opt = set(str(x) for x in absent_raw)

        # 1. Mandatory files must all be listed in top_level_files
        for req_name in _MANDATORY_MANIFEST_FILES:
            if req_name not in top_files:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Mandatory file omitted from top_level_files: {req_name}"

        # 2. Every known optional file must be declared in EXACTLY ONE of top_level_files or absent_optional_files
        for opt_name in _OPTIONAL_MANIFEST_FILES:
            in_top = opt_name in top_files
            in_abs = opt_name in absent_opt
            if in_top and in_abs:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Conflicting declaration for {opt_name}"
            if not in_top and not in_abs:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Optional file undeclared (must be in top_level_files or absent_optional_files): {opt_name}"
            if in_abs and (batch_dir / opt_name).is_file():
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"File declared absent exists on disk: {opt_name}"

        # 3. Read & verify every listed top-level file ONCE into memory
        for rel_name, spec in top_files.items():
            if not isinstance(rel_name, str) or not isinstance(spec, dict) or not isinstance(spec.get("sha256"), str) or not spec["sha256"]:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Invalid digest entry for {rel_name}"
            try:
                fpath = resolve_within_root(batch_dir, rel_name)
            except AppError as exc:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Illegal path in top_level_files ({rel_name}): {exc.message}"
            if not fpath.is_file():
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Listed file missing: {rel_name}"
            try:
                raw = fpath.read_bytes()
            except OSError as exc:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Read error for {rel_name}: {exc}"
            if _sha256_bytes(raw) != spec["sha256"]:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"SHA-256 mismatch for {rel_name}"
            if spec.get("size") is not None:
                try:
                    expected_sz = int(spec["size"])
                except (TypeError, ValueError):
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Invalid size for {rel_name}"
                if expected_sz != len(raw):
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Size mismatch for {rel_name}"
            verified_bytes[rel_name] = raw

        # 3b. Validate mandatory & listed optional JSON top-level files parse cleanly with expected JSON types
        for json_req in ("manifest.json", "batch-status.json", "batch-report.json"):
            try:
                parsed_req = json.loads(verified_bytes[json_req].decode("utf-8"))
            except Exception as exc:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Corrupt JSON in {json_req}: {exc}"
            if not isinstance(parsed_req, dict):
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Non-object JSON in {json_req}"
        for opt_dict_name in ("official-result.json", "selection.json", "run-config.json"):
            if opt_dict_name in verified_bytes:
                try:
                    parsed_opt = json.loads(verified_bytes[opt_dict_name].decode("utf-8"))
                except Exception as exc:
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Corrupt JSON in {opt_dict_name}: {exc}"
                if not isinstance(parsed_opt, dict):
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Non-object JSON in {opt_dict_name}"
        if "vlmevalkit/metrics.json" in verified_bytes:
            try:
                parsed_vlm = json.loads(verified_bytes["vlmevalkit/metrics.json"].decode("utf-8"))
            except Exception as exc:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Corrupt JSON in vlmevalkit/metrics.json: {exc}"
            if not isinstance(parsed_vlm, (dict, list)):
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, "Invalid JSON type in vlmevalkit/metrics.json"

        # 4. Parse & verify every trial-level digest in verified trial-digests.jsonl (reading ONCE into memory!)
        td_raw = verified_bytes.get("trial-digests.jsonl", b"").decode("utf-8", errors="replace")
        for ln, line in enumerate(td_raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception as exc:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Corrupt trial-digests.jsonl line {ln}: {exc}"
            if not isinstance(entry, dict):
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Non-object entry in trial-digests.jsonl line {ln}"
            tid = entry.get("trial_id")
            if not isinstance(tid, str) or not tid.strip():
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Missing trial_id in trial-digests.jsonl line {ln}"
            if tid in trial_digests_map:
                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Duplicate trial_id {tid} in trial-digests.jsonl line {ln}"
            # Verify each listed artifact digest on disk (supporting both shared_public_dto & isolated 0700 evidence_store!)
            for kind in (
                "result",
                "trajectory",
                "gateway",
                "verifier_output",
                "training",
                "training_capture",
                "direct_qa",
                "failure_locator",
            ):
                spec = entry.get(kind)
                if spec is None:
                    continue
                if not isinstance(spec, dict) or not isinstance(spec.get("rel_path"), str) or not spec["rel_path"] or not isinstance(spec.get("sha256"), str) or not spec["sha256"]:
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Malformed {kind} digest spec for trial {tid} at line {ln}"
                rel_p = str(spec["rel_path"])
                tier = str(spec.get("storage_tier") or "shared_public_dto")
                try:
                    if tier == "evidence_store_0700":
                        if self.settings.evidence_store_dir is None:
                            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Evidence store unconfigured for {rel_p}"
                        art_p = resolve_within_root(self.settings.evidence_store_dir, rel_p)
                    else:
                        art_p = resolve_within_root(batch_dir, rel_p)
                except AppError as exc:
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Illegal trial artifact path {rel_p}: {exc.message}"
                if not art_p.is_file():
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Missing trial artifact {rel_p}"
                try:
                    art_bytes = art_p.read_bytes()
                except OSError as exc:
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Read error for trial artifact {rel_p}: {exc}"
                if _sha256_bytes(art_bytes) != spec["sha256"]:
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Trial artifact SHA mismatch {rel_p}"
                if spec.get("size") is not None:
                    try:
                        if int(spec["size"]) != len(art_bytes):
                            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Trial artifact size mismatch {rel_p}"
                    except (TypeError, ValueError):
                        return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Invalid size for trial artifact {rel_p}"
                verified_trial_bytes[rel_p] = art_bytes

            media_raw = entry.get("media")
            if media_raw is not None:
                if not isinstance(media_raw, list):
                    return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Malformed media list for trial {tid} at line {ln}"
                for m_spec in media_raw:
                    if not isinstance(m_spec, dict) or not isinstance(m_spec.get("rel_path"), str) or not m_spec["rel_path"] or not isinstance(m_spec.get("sha256"), str) or not m_spec["sha256"]:
                        return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Malformed media digest spec for trial {tid} at line {ln}"
                    m_rel = str(m_spec["rel_path"])
                    m_tier = str(m_spec.get("storage_tier") or "shared_public_dto")
                    try:
                        if m_tier == "evidence_store_0700":
                            if self.settings.evidence_store_dir is None:
                                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Evidence store unconfigured for trial media {m_rel}"
                            mp = resolve_within_root(self.settings.evidence_store_dir, m_rel)
                        else:
                            mp = resolve_within_root(batch_dir, m_rel)
                    except AppError as exc:
                        return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Illegal trial media path {m_rel}: {exc.message}"
                    if not mp.is_file():
                        return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Missing trial media {m_rel}"
                    actual_sha, actual_sz = _sha256_file(mp)
                    if actual_sha != m_spec["sha256"]:
                        return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Trial media SHA mismatch {m_rel}"
                    if m_spec.get("size") is not None:
                        try:
                            if int(m_spec["size"]) != actual_sz:
                                return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Trial media size mismatch {m_rel}"
                        except (TypeError, ValueError):
                            return False, None, verified_bytes, trial_digests_map, verified_trial_bytes, f"Invalid size for trial media {m_rel}"
            trial_digests_map[tid] = entry

        return True, pm, verified_bytes, trial_digests_map, verified_trial_bytes, None

    def _resolve_runner_published_ownership(
        self,
        batch_name: str,
        default_batch_id: str,
        *,
        generation_id: str | None,
        runner_job_id: str | None,
        manifest_sha256: str | None,
    ) -> tuple[str, str, str] | None:
        """Bind ownership to the current manifest and its authenticated control-plane publication."""
        doc = None
        if self.settings.state_dir is not None:
            try:
                name = _submission_filename(batch_name)
                with _submission_dir(self.settings.state_dir) as directory:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(fd, "rb") as stream:
                        _check_submission_file(os.fstat(fd))
                        doc = json.load(stream)
            except (AppError, OSError, ValueError):
                pass
        if (
            isinstance(doc, dict)
            and doc.get("batch_name") == batch_name
            and doc.get("batch_id") == default_batch_id
            and doc.get("source_namespace") == "runner_published"
            and doc.get("generation_id") == generation_id
            and (generation_id is None or isinstance(doc.get("job_id"), str))
            and doc.get("manifest_sha256") in (None, manifest_sha256)
            and (runner_job_id is None or (
                doc.get("job_id") == runner_job_id and doc.get("manifest_sha256") == manifest_sha256
            ))
            and doc.get("evidence_approval") in ("approved_restricted", "pending", "revoked")
            and all(isinstance(doc.get(k), str) and doc[k].strip() for k in ("project_id", "owner_id"))
        ):
            job = self.db.fetchone(
                "SELECT batch_name, project_id, owner_id FROM exec_jobs WHERE job_id = ?",
                (doc.get("job_id"),),
            )
            if job is None or (job["batch_name"], job["project_id"], job["owner_id"]) == (
                batch_name, doc["project_id"], doc["owner_id"]
            ):
                return doc["project_id"], doc["owner_id"], doc["evidence_approval"]

        if generation_id is None or manifest_sha256 is None:
            return None
        # Releases before the private ledger still recorded the complete manifest digest in
        # the control-plane reconciliation event. A matching batch name alone is insufficient.
        job = self.db.fetchone(
            """
            SELECT j.project_id, j.owner_id FROM exec_jobs j
            JOIN exec_job_events e ON e.job_id = j.job_id AND e.event_type = 'reconciled'
            WHERE j.batch_id = ? AND j.batch_name = ? AND (? IS NULL OR j.job_id = ?)
              AND json_extract(e.payload_json, '$.source_namespace') = 'runner_published'
              AND json_extract(e.payload_json, '$.generation_id') = ?
              AND json_extract(e.payload_json, '$.snapshot_digest') = ?
            LIMIT 1
            """,
            (default_batch_id, batch_name, runner_job_id, runner_job_id,
             generation_id, f"sha256:{manifest_sha256}"),
        )
        if job is None:
            return None
        return job["project_id"], job["owner_id"], "approved_restricted"

    def _reject_unowned_runner_batch(self, batch_dir: Path, source_locator: str) -> str:
        """Remove stale derived rows while leaving the publish artifacts available for recovery."""
        target_path = redact_path(str(batch_dir)) or batch_dir.name
        with self.db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM search_fts WHERE entity_type = 'batch' AND entity_id IN (
                    SELECT batch_id FROM batches WHERE source_locator = ? AND source_namespace = 'runner_published'
                )
                """,
                (source_locator,),
            )
            conn.execute(
                "DELETE FROM batches WHERE source_locator = ? AND source_namespace = 'runner_published'",
                (source_locator,),
            )
            warning = conn.execute(
                "SELECT 1 FROM index_warnings WHERE tier = 'hot' AND code = 'RUNNER_OWNERSHIP_MISSING' AND target_path = ?",
                (target_path,),
            ).fetchone()
            if warning is None:
                self._record_warning(
                    conn,
                    tier="hot",
                    severity="error",
                    code="RUNNER_OWNERSHIP_MISSING",
                    target_path=str(batch_dir),
                    message="缺少可信 Runner 提交归属，批次已从查询索引隔离。恢复提交记录后可重新索引。",
                )
        return "rejected"

    def scan_single_harbor_batch(
        self,
        batch_dir: Path,
        *,
        force: bool = False,
        source_namespace: str = "harbor_runs",
        project_override: str | None = None,
        owner_override: str | None = None,
        approval_override: str | None = None,
        existing_digests: dict[str, str] | None = None,
        precomputed_digest: str | None = None,
        prefetched_top_bytes: dict[str, bytes] | None = None,
        deep_trial_enrich: bool = True,
    ) -> str:
        """Index one harbor-runs/<batch> or runner_published/<batch> directory atomically (I-01, I-03)."""
        try:
            with ExitStack() as resources:
                if source_namespace == "runner_published":
                    try:
                        fd = os.open(batch_dir / ".publish.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    except FileNotFoundError:
                        pass
                    else:
                        lease = resources.enter_context(os.fdopen(fd, "rb"))
                        try:
                            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return "skipped_locked"
                return self._scan_single_harbor_batch_impl(
                    batch_dir,
                    force=force,
                    source_namespace=source_namespace,
                    project_override=project_override,
                    owner_override=owner_override,
                    approval_override=approval_override,
                    existing_digests=existing_digests,
                    precomputed_digest=precomputed_digest,
                    prefetched_top_bytes=prefetched_top_bytes,
                    deep_trial_enrich=deep_trial_enrich,
                )
        except AppError as exc:
            if exc.code == "LOCAL_DISK_LOW":
                raise
            with self.db.transaction() as conn:
                self._record_warning(
                    conn,
                    tier="hot",
                    severity="error",
                    code="BATCH_SCAN_ERROR",
                    target_path=str(batch_dir),
                    message=f"批次扫描失败，已保留旧代并跳过: {exc.message}",
                )
            return "error"
        except Exception as exc:
            with self.db.transaction() as conn:
                self._record_warning(
                    conn,
                    tier="hot",
                    severity="error",
                    code="BATCH_SCAN_ERROR",
                    target_path=str(batch_dir),
                    message=f"批次扫描遇到未预期异常，已保留旧代并继续扫描其他批次: {type(exc).__name__}: {exc}",
                )
            return "error"

    def _scan_single_harbor_batch_impl(
        self,
        batch_dir: Path,
        *,
        force: bool = False,
        source_namespace: str = "harbor_runs",
        project_override: str | None = None,
        owner_override: str | None = None,
        approval_override: str | None = None,
        existing_digests: dict[str, str] | None = None,
        precomputed_digest: str | None = None,
        prefetched_top_bytes: dict[str, bytes] | None = None,
        deep_trial_enrich: bool = True,
    ) -> str:
        """Internal atomic single-batch indexer.
        Returns one of: 'indexed', 'skipped_unchanged', 'placeholder', 'rejected', 'error'."""
        batch_name = batch_dir.name
        ns = source_namespace or "harbor_runs"
        source_tags_json = json.dumps([ns])
        source_locator = f"{ns}:{batch_name}"
        default_batch_id = make_batch_id(source_locator)
        now = utcnow_iso()
        pub_manifest_path = batch_dir / "publish-manifest.json"
        runner_ownership = None
        if ns == "runner_published":
            runner_job_id = None
            runner_generation = None
            manifest_sha256 = None
            try:
                if pub_manifest_path.is_symlink():
                    raise ValueError("Runner manifest is a symlink")
                raw_runner_manifest = pub_manifest_path.read_bytes()
                parsed_manifest = json.loads(raw_runner_manifest)
                runner_generation = parsed_manifest["generation_id"]
                runner_job_id = parsed_manifest.get("runner_job_id")
                if not isinstance(runner_generation, str) or not runner_generation:
                    raise ValueError("Invalid Runner manifest generation")
                if runner_job_id is not None and (
                    not isinstance(runner_job_id, str) or not SAFE_ID_RE.fullmatch(runner_job_id)
                ):
                    raise ValueError("Invalid Runner manifest job")
                manifest_sha256 = _sha256_bytes(raw_runner_manifest)
                prefetched_top_bytes = {"publish-manifest.json": raw_runner_manifest}
            except FileNotFoundError:
                pass  # Legacy private submissions predate v2 manifests.
            except (OSError, ValueError, TypeError, KeyError):
                return self._reject_unowned_runner_batch(batch_dir, source_locator)
            runner_ownership = self._resolve_runner_published_ownership(
                batch_name, default_batch_id, generation_id=runner_generation,
                runner_job_id=runner_job_id, manifest_sha256=manifest_sha256,
            )
            if runner_ownership is None:
                return self._reject_unowned_runner_batch(batch_dir, source_locator)

        if precomputed_digest is not None:
            digest = precomputed_digest
        else:
            # Single scandir performs both permission check and digest calculation (fast on CIFS!)
            try:
                digest = _dir_content_digest(batch_dir)
            except PermissionError:
                red_p = redact_path(str(batch_dir)) or batch_name
                already = self.db.fetchone(
                    "SELECT 1 FROM index_warnings WHERE tier = 'hot' AND code = 'DIR_PERMISSION_DENIED' AND target_path = ?",
                    (red_p,),
                )
                if already is None:
                    with self.db.transaction() as conn:
                        self._record_warning(
                            conn,
                            tier="hot",
                            severity="warning",
                            code="DIR_PERMISSION_DENIED",
                            target_path=str(batch_dir),
                            message=f"目录无读权限，已安全跳过: {batch_name}",
                        )
                return "error"
            except OSError as exc:
                with self.db.transaction() as conn:
                    self._record_warning(
                        conn,
                        tier="hot",
                        severity="warning",
                        code="DIR_READ_ERROR",
                        target_path=str(batch_dir),
                        message=f"目录读取失败: {exc}",
                    )
                return "error"

        if not force:
            if existing_digests is not None and runner_ownership is None:
                if existing_digests.get(source_locator) == digest:
                    return "skipped_unchanged"
            else:
                existing = self.db.fetchone(
                    """
                    SELECT batch_id, content_digest, generation_id, checks_json, is_running_placeholder, runner,
                           project_id, owner_id, evidence_approval
                    FROM batches WHERE source_locator = ?
                    """,
                    (source_locator,),
                )
                if (
                    existing is not None
                    and existing["content_digest"] == digest
                    and (
                        runner_ownership is None
                        or runner_ownership == (existing["project_id"], existing["owner_id"], existing["evidence_approval"])
                    )
                ):
                    tr_rows = self.db.fetchall(
                        """
                        SELECT evidence_refs_json FROM episodes
                        WHERE batch_id = ? AND evidence_refs_json LIKE '%"training"%'
                        """,
                        (existing["batch_id"],),
                    )
                    needs_tr = any(_training_ref_needs_backfill(r["evidence_refs_json"]) for r in tr_rows)
                    if not _batch_needs_backfill(
                        existing["checks_json"],
                        existing["generation_id"],
                        existing["is_running_placeholder"],
                        runner=existing["runner"],
                        has_unbackfilled_training=needs_tr,
                    ):
                        return "skipped_unchanged"

        if runner_ownership is not None:
            project_id, owner_id, reg_approval = runner_ownership
        elif project_override and owner_override and approval_override:
            project_id, owner_id, reg_approval = project_override, owner_override, approval_override
        else:
            reg_proj, reg_owner, reg_approval, _ = self.approval_registry.lookup_full(batch_dir)
            project_id = project_override or reg_proj
            owner_id = owner_override or reg_owner
            reg_approval = approval_override or reg_approval

        manifest_path = batch_dir / "manifest.json"
        status_path = batch_dir / "batch-status.json"
        episodes_path = batch_dir / "episodes.jsonl"
        report_path = batch_dir / "batch-report.json"
        official_path = batch_dir / "official-result.json"
        vlm_metrics_path = batch_dir / "vlmevalkit" / "metrics.json"
        selection_path = batch_dir / "selection.json"
        run_config_path = batch_dir / "run-config.json"
        # Rule 3: strict publish-manifest.json verification — parse ONLY verified in-memory bytes!
        pub_manifest: dict[str, Any] | None = None
        verified_bytes: dict[str, bytes] = {}
        legacy_top_bytes: dict[str, bytes] = {}
        raw_pm_sha256: str | None = None
        trial_digests_sha256: str | None = None
        absent_optional: list[str] = []
        trial_digests_map: dict[str, dict[str, Any]] = {}
        verified_trial_bytes: dict[str, bytes] = {}
        using_manifest = (
            ("publish-manifest.json" in prefetched_top_bytes)
            if prefetched_top_bytes is not None
            else pub_manifest_path.is_file()
        )

        if using_manifest:
            raw_pm_bytes = (
                prefetched_top_bytes.get("publish-manifest.json")
                if prefetched_top_bytes is not None
                else None
            )
            if raw_pm_bytes is None:
                try:
                    if pub_manifest_path.is_symlink():
                        raise OSError("Refusing symlink publish-manifest.json")
                    raw_pm_bytes = pub_manifest_path.read_bytes()
                except OSError as exc:
                    with self.db.transaction() as conn:
                        self._record_warning(
                            conn,
                            tier="hot",
                            severity="error",
                            code="PUBLISH_MANIFEST_MISMATCH",
                            target_path=str(batch_dir),
                            message=f"publish-manifest 读取失败，已保留上一代快照: {exc}",
                        )
                    return "rejected"
            raw_pm_sha256 = _sha256_bytes(raw_pm_bytes)
            ok, pub_manifest, verified_bytes, trial_digests_map, verified_trial_bytes, err = self._verify_publish_manifest(
                batch_dir,
                pub_manifest_path,
                prefetched_pm_bytes=raw_pm_bytes,
            )
            if not ok or pub_manifest is None:
                with self.db.transaction() as conn:
                    self._record_warning(
                        conn,
                        tier="hot",
                        severity="error",
                        code="PUBLISH_MANIFEST_MISMATCH",
                        target_path=str(batch_dir),
                        message=f"publish-manifest 校验失败，已保留上一代快照: {err}",
                    )
                return "rejected"
            generation_id = str(pub_manifest["generation_id"])
            absent_optional = list(pub_manifest.get("absent_optional_files") or [])
            td_spec = (pub_manifest.get("top_level_files") or {}).get("trial-digests.jsonl")
            if isinstance(td_spec, dict):
                trial_digests_sha256 = td_spec.get("sha256")
        else:
            generation_id = f"gen_legacy_{digest[:16]}"

        def _read_top_json(rel_name: str, fpath: Path) -> Any | None:
            if using_manifest:
                raw = verified_bytes.get(rel_name)
                if raw is None:
                    return None  # Unlisted optional file is strictly ignored!
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
            if prefetched_top_bytes is not None:
                raw_pf = prefetched_top_bytes.get(rel_name)
                if raw_pf is None:
                    return None
                legacy_top_bytes[rel_name] = raw_pf
                try:
                    return json.loads(raw_pf.decode("utf-8"))
                except Exception:
                    return None
            if not fpath.is_file() or fpath.is_symlink():
                return None
            try:
                raw_disk = fpath.read_bytes()
            except OSError:
                return None
            legacy_top_bytes[rel_name] = raw_disk
            try:
                return json.loads(raw_disk.decode("utf-8"))
            except Exception:
                return None

        # Load batch-status.json safely
        progress_raw = _read_top_json("batch-status.json", status_path)
        progress: dict[str, Any] = progress_raw if isinstance(progress_raw, dict) else {}

        src_obj = progress.get("source") if isinstance(progress.get("source"), dict) else {}
        raw_src_host = src_obj.get("host") if isinstance(src_obj, dict) else None
        raw_src_path = src_obj.get("path") if isinstance(src_obj, dict) else None
        internal_fp = compute_internal_source_fingerprint(raw_src_host, raw_src_path)
        host_redacted = redact_host(raw_src_host)
        path_redacted = redact_path(raw_src_path)

        # Case A: Running placeholder (only batch-status.json, no manifest.json & no episodes.jsonl) (I-06)
        has_manifest = (
            ("manifest.json" in prefetched_top_bytes)
            if prefetched_top_bytes is not None
            else manifest_path.is_file()
        )
        has_episodes = (
            ("episodes.jsonl" in prefetched_top_bytes)
            if prefetched_top_bytes is not None
            else episodes_path.is_file()
        )
        if not using_manifest and not has_manifest and not has_episodes:
            if not progress:
                return "skipped_unchanged"
            # If this placeholder was already promoted in-place to a finished batch (same batch_id
            # OR same (internal_source_fingerprint, project_id)), NEVER overwrite or collide with it!
            already_pub = self.db.fetchone(
                """
                SELECT 1 FROM batches
                WHERE is_running_placeholder = 0
                  AND (
                    batch_id = ?
                    OR (? IS NOT NULL AND internal_source_fingerprint = ? AND project_id = ?)
                  )
                LIMIT 1
                """,
                (default_batch_id, internal_fp, internal_fp, project_id),
            )
            if already_pub is not None:
                return "skipped_unchanged"
            planned_ph = progress.get("planned") if isinstance(progress.get("planned"), int) else None
            task_attempts_ph = progress.get("task_attempts") if isinstance(progress.get("task_attempts"), dict) else {}
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_id, source_locator, source_namespace, batch_name,
                        project_id, owner_id, evidence_approval,
                        is_running_placeholder, internal_source_fingerprint,
                        source_host_redacted, source_path_redacted, source_tags_json,
                        identity_status, runner, run_scope, scoring_protocol,
                        official_status, batch_status, report_origin,
                        generation_id, unique_tasks, planned, recorded, scored, unscored,
                        content_digest, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?,
                              'running_placeholder', 'harbor', 'unknown', 'unknown',
                              'unscored', ?, 'running_placeholder', ?, ?, ?, 0, 0, 0, ?, ?)
                    ON CONFLICT(source_locator) DO UPDATE SET
                        is_running_placeholder = 1,
                        internal_source_fingerprint = excluded.internal_source_fingerprint,
                        source_host_redacted = excluded.source_host_redacted,
                        source_path_redacted = excluded.source_path_redacted,
                        batch_status = excluded.batch_status,
                        unique_tasks = excluded.unique_tasks,
                        planned = excluded.planned,
                        generation_id = excluded.generation_id,
                        content_digest = excluded.content_digest,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        default_batch_id,
                        source_locator,
                        ns,
                        batch_name,
                        project_id,
                        owner_id,
                        reg_approval,
                        internal_fp,
                        host_redacted,
                        path_redacted,
                        source_tags_json,
                        str(progress.get("status") or "running"),
                        generation_id,
                        len(task_attempts_ph),
                        planned_ph,
                        digest,
                        now,
                    ),
                )
            return "placeholder"

        # Case B: Published / partial batch
        meta_raw = _read_top_json("manifest.json", manifest_path)
        meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}

        # Pre/Post stat fence for legacy batches without publish-manifest.json (when reading from disk)
        pre_ep_stat = None if (using_manifest or prefetched_top_bytes is not None) else _stat_tuple(episodes_path)
        pre_rep_stat = None if (using_manifest or prefetched_top_bytes is not None) else _stat_tuple(report_path)

        rows: list[dict[str, Any]] = []
        corrupt_lines: list[tuple[int, str]] = []
        seen_trials: set[str] = set()
        raw_ep_text: str | None = None
        if using_manifest:
            raw_ep_text = verified_bytes.get("episodes.jsonl", b"").decode("utf-8", errors="replace")
        elif prefetched_top_bytes is not None:
            raw_ep_b = prefetched_top_bytes.get("episodes.jsonl")
            if raw_ep_b is not None:
                legacy_top_bytes["episodes.jsonl"] = raw_ep_b
                raw_ep_text = raw_ep_b.decode("utf-8", errors="replace")
        elif episodes_path.is_file() and not episodes_path.is_symlink():
            try:
                raw_ep_b = episodes_path.read_bytes()
                legacy_top_bytes["episodes.jsonl"] = raw_ep_b
                raw_ep_text = raw_ep_b.decode("utf-8", errors="replace")
            except OSError as exc:
                with self.db.transaction() as conn:
                    self._record_warning(
                        conn,
                        tier="hot",
                        severity="error",
                        code="EPISODES_READ_ERROR",
                        target_path=str(episodes_path),
                        message=f"episodes.jsonl 读取失败: {exc}",
                    )
                return "error"

        if raw_ep_text is not None:
            for ln, line in enumerate(raw_ep_text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception as exc:
                    corrupt_lines.append((ln, str(exc)))
                    continue
                if not isinstance(obj, dict) or not obj.get("trial_id") or not obj.get("task_id"):
                    corrupt_lines.append((ln, "missing trial_id or task_id"))
                    continue
                tid = str(obj["trial_id"])
                if tid in seen_trials:
                    corrupt_lines.append((ln, f"duplicate trial_id {tid}"))
                    continue
                seen_trials.add(tid)
                rows.append(obj)

        # Strict I-01 completeness check: when publish-manifest.json is present, EVERY referenced
        # trial artifact in episodes.jsonl (plus any existing verifier-output.txt or media/* on disk)
        # MUST have a verified SHA-256 digest in trial-digests.jsonl! Zero unverified evidence refs!
        if using_manifest:
            missing_reason: str | None = None
            for r in rows:
                tid = str(r["trial_id"])
                td_entry = trial_digests_map.get(tid)
                ep_ev_refs = r.get("evidence_refs") if isinstance(r.get("evidence_refs"), dict) else {}
                for kind, rel_key in (
                    ("result", "result"),
                    ("trajectory", "trajectory"),
                    ("gateway", "gateway_trace"),
                    ("training", "training_trace"),
                    ("training_capture", "training_capture"),
                    ("direct_qa", "direct_qa"),
                    ("verifier_output", "verifier_output"),
                    ("failure_locator", "failure_locator"),
                ):
                    rel_val = r.get(rel_key)
                    if not (isinstance(rel_val, str) and rel_val):
                        ev_k = ep_ev_refs.get(kind)
                        if isinstance(ev_k, dict) and isinstance(ev_k.get("rel_path"), str) and ev_k["rel_path"]:
                            rel_val = ev_k["rel_path"]
                    if isinstance(rel_val, str) and rel_val:
                        if td_entry is None:
                            missing_reason = f"Trial {tid} references {rel_key}={rel_val} but is missing from trial-digests.jsonl"
                            break
                        spec = td_entry.get(kind)
                        if (
                            not isinstance(spec, dict)
                            or spec.get("rel_path") != rel_val
                            or not spec.get("sha256")
                            or rel_val not in verified_trial_bytes
                        ):
                            missing_reason = f"Trial {tid} artifact {kind} ({rel_val}) lacks verified digest in trial-digests.jsonl"
                            break
                if missing_reason:
                    break
                # Check on-disk verifier-output.txt, capture.json, and media/* under both legacy trials/ and generations/<gen>/trials/
                check_prefixes = [f"trials/{tid}"]
                if generation_id:
                    check_prefixes.append(f"generations/{generation_id}/trials/{tid}")
                for pfx in check_prefixes:
                    vo_rel = f"{pfx}/verifier-output.txt"
                    cap_rel = f"{pfx}/capture.json"
                    try:
                        vo_path = resolve_within_root(batch_dir, vo_rel)
                        cap_path = resolve_within_root(batch_dir, cap_rel)
                        media_dir = resolve_within_root(batch_dir, f"{pfx}/media")
                    except AppError as exc:
                        missing_reason = f"Trial {tid} has illegal trial_id path: {exc.message}"
                        break
                    if vo_path.is_file():
                        vo_spec = (td_entry or {}).get("verifier_output") if isinstance(td_entry, dict) else None
                        if not isinstance(vo_spec, dict) or vo_spec.get("rel_path") != vo_rel or not vo_spec.get("sha256"):
                            missing_reason = f"Trial {tid} has unverified verifier-output.txt omitted from trial-digests.jsonl"
                            break
                    if cap_path.is_file():
                        cap_spec = (td_entry or {}).get("training_capture") if isinstance(td_entry, dict) else None
                        if not isinstance(cap_spec, dict) or cap_spec.get("rel_path") != cap_rel or not cap_spec.get("sha256"):
                            missing_reason = f"Trial {tid} has unverified capture.json omitted from trial-digests.jsonl"
                            break
                    if media_dir.is_dir():
                        declared_media = {
                            str(m.get("rel_path"))
                            for m in ((td_entry or {}).get("media") or [])
                            if isinstance(m, dict) and m.get("rel_path") and m.get("sha256")
                        }
                        for mf in media_dir.iterdir():
                            if mf.is_file():
                                m_rel = f"{pfx}/media/{mf.name}"
                                if m_rel not in declared_media:
                                    missing_reason = f"Trial {tid} has unverified media file {m_rel} omitted from trial-digests.jsonl"
                                    break
                        if missing_reason:
                            break
                if missing_reason:
                    break
            if missing_reason:
                with self.db.transaction() as conn:
                    self._record_warning(
                        conn,
                        tier="hot",
                        severity="error",
                        code="PUBLISH_MANIFEST_MISMATCH",
                        target_path=str(batch_dir),
                        message=f"逐题摘要清册不完整，已保留上一代快照: {missing_reason}",
                    )
                return "rejected"

        published_report_raw = _read_top_json("batch-report.json", report_path)
        published_report = published_report_raw if isinstance(published_report_raw, dict) else None

        if not using_manifest and prefetched_top_bytes is None:
            post_ep_stat = _stat_tuple(episodes_path)
            post_rep_stat = _stat_tuple(report_path)
            if pre_ep_stat != post_ep_stat or pre_rep_stat != post_rep_stat:
                return "rejected"

        recomputed = _recompute_report_summary(rows, meta, progress)
        if published_report is not None and _reports_agree(published_report, recomputed):
            report_origin = "published_report"
            totals = published_report.get("totals") or recomputed["totals"]
            usage_summary = published_report.get("usage") or recomputed["usage"]
            failure_kinds = published_report.get("failure_kinds") or recomputed["failure_kinds"]
        else:
            report_origin = "recomputed_from_episodes"
            totals = recomputed["totals"]
            usage_summary = recomputed["usage"]
            failure_kinds = recomputed["failure_kinds"]

        # Optional files: read ONLY from verified_bytes when using_manifest is True!
        official_raw = _read_top_json("official-result.json", official_path)
        official_obj: dict[str, Any] | None = None
        if (
            isinstance(official_raw, dict)
            and finite(official_raw.get("score"))
            and finite(official_raw.get("denominator"))
            and float(official_raw["denominator"]) > 0
        ):
            official_obj = official_raw

        vlm_metrics_obj = _read_top_json("vlmevalkit/metrics.json", vlm_metrics_path)
        run_config_raw = _read_top_json("run-config.json", run_config_path)
        run_config_obj = run_config_raw if isinstance(run_config_raw, dict) else None
        selection_raw = _read_top_json("selection.json", selection_path)
        selection_obj = selection_raw if isinstance(selection_raw, dict) else None

        # Compute immutable verified payload fingerprint for this generation_id (I-01):
        # Prevents a valid second publish from mutating content under an already-committed generation_id!
        if using_manifest:
            gen_hasher = hashlib.sha256()
            gen_hasher.update(f"manifest_v2|{generation_id}\n".encode("utf-8"))
            gen_hasher.update(json.dumps(sorted(absent_optional), ensure_ascii=False).encode("utf-8"))
            for rel_k in sorted(verified_bytes.keys()):
                gen_hasher.update(f"\ntop:{rel_k}:{_sha256_bytes(verified_bytes[rel_k])}".encode("utf-8"))
            for art_k in sorted(verified_trial_bytes.keys()):
                gen_hasher.update(f"\ntrial:{art_k}:{_sha256_bytes(verified_trial_bytes[art_k])}".encode("utf-8"))
            for td_tid in sorted(trial_digests_map.keys()):
                td_e = trial_digests_map[td_tid]
                for m_spec in sorted(
                    (m for m in (td_e.get("media") or []) if isinstance(m, dict) and m.get("rel_path")),
                    key=lambda x: str(x["rel_path"]),
                ):
                    gen_hasher.update(
                        f"\nmedia:{td_tid}:{m_spec['rel_path']}:{m_spec.get('sha256', '')}".encode("utf-8")
                    )
            verified_payload_sha256 = gen_hasher.hexdigest()
        else:
            # For legacy batches without publish-manifest.json, derive generation_id from actual
            # in-memory content hash (NOT stat-only digest) so even same-size same-tick overwrites
            # produce a distinct generation_id!
            content_hasher = hashlib.sha256()
            content_hasher.update((raw_ep_text or "").encode("utf-8"))
            content_hasher.update(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            content_hasher.update(json.dumps(published_report, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            content_hasher.update(json.dumps(official_obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            content_hasher.update(json.dumps(vlm_metrics_obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            content_hasher.update(json.dumps(run_config_obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            content_hasher.update(json.dumps(selection_obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            verified_payload_sha256 = content_hasher.hexdigest()
            generation_id = f"gen_legacy_{verified_payload_sha256[:16]}"

        # Derive identity fields
        raw_dataset_name = meta.get("dataset") or (rows[0].get("dataset") if rows else None)
        runner = str(meta.get("runner") or (rows[0].get("runner") if rows else None) or "harbor")
        index_legacy_vlm_media = not using_manifest and runner == "vlmevalkit"
        harness = meta.get("harness") or (rows[0].get("harness") if rows else None)
        harness_version = next((r.get("harness_version") for r in rows if r.get("harness_version")), None)
        model_route = meta.get("model") or next((r.get("model_route") for r in rows if r.get("model_route")), None)
        explicit_ck = next((r.get("checkpoint") for r in rows if r.get("checkpoint")), None)
        model_canonical, model_provider, checkpoint, thinking_mode = canonicalize_model(
            model_route, explicit_checkpoint=explicit_ck, batch_name=batch_name
        )

        unique_task_ids = {str(r["task_id"]) for r in rows}
        task_attempts = progress.get("task_attempts") if isinstance(progress.get("task_attempts"), dict) else {}
        unique_tasks_count = len(unique_task_ids | set(task_attempts.keys()))

        recorded_count = int(totals.get("recorded") or len(rows))
        scored_count = int(totals.get("scored") or 0)
        unscored_count = sum(1 for r in rows if r.get("status") == "unscored")
        planned_count = totals.get("planned") if isinstance(totals.get("planned"), int) else None
        coverage_val = totals.get("coverage") if finite(totals.get("coverage")) else None
        valid_mean_val = totals.get("valid_mean") if finite(totals.get("valid_mean")) else None
        passed_val = totals.get("passed") if isinstance(totals.get("passed"), int) else None
        pass_threshold_val = totals.get("pass_threshold") if finite(totals.get("pass_threshold")) else None

        official_status = (
            "official_verified"
            if official_obj is not None
            else ("unofficial_valid_mean" if scored_count > 0 else "unscored")
        )

        # Determine stable batch_id (preserving running placeholder's batch_id within SAME project_id!)
        effective_batch_id = default_batch_id
        existing_same_loc = self.db.fetchone(
            "SELECT batch_id FROM batches WHERE source_locator = ?", (source_locator,)
        )
        if existing_same_loc is not None:
            effective_batch_id = existing_same_loc["batch_id"]
        elif internal_fp:
            ph_match = self.db.fetchone(
                """
                SELECT batch_id FROM batches
                WHERE is_running_placeholder = 1
                  AND internal_source_fingerprint = ?
                  AND project_id = ?
                LIMIT 1
                """,
                (internal_fp, project_id),
            )
            if ph_match is not None:
                effective_batch_id = ph_match["batch_id"]

        # Build enriched episode rows
        attempt_counters: Counter[str] = Counter()
        enriched_episodes: list[dict[str, Any]] = []
        has_gateway = 0
        batch_token_semantics = "none"
        batch_started_at: str | None = None
        batch_finished_at: str | None = None

        legacy_vlm_results: list[dict[str, Any]] = []
        if index_legacy_vlm_media and rows:
            # Legacy VLMEvalKit publications have no media digest manifest. Read their
            # results and sidecar media in parallel before committing the hot index.
            with ThreadPoolExecutor(max_workers=12) as pool:
                legacy_vlm_results = list(
                    pool.map(
                        lambda row: _inspect_trial_result_and_media(batch_dir, row.get("result"), str(row["trial_id"])),
                        rows,
                    )
                )

        for row_index, r in enumerate(rows):
            tid = str(r["trial_id"])
            task_id = str(r["task_id"])
            attempt_counters[task_id] += 1
            att_idx = attempt_counters[task_id]
            status = str(r.get("status") or "unknown")
            reward = float(r["reward"]) if (status == "scored" and finite(r.get("reward"))) else None
            reward_valid = 1 if (status == "scored" and reward is not None) else 0
            exc_type = r.get("exception_type")
            failure = r.get("failure") if isinstance(r.get("failure"), dict) else {}
            fail_kind = failure.get("kind") or exc_type
            fail_summary = redact_text(failure.get("summary"))
            raw_log_val = failure.get("raw_log")
            if isinstance(raw_log_val, dict):
                fail_host = redact_host(raw_log_val.get("host") or failure.get("host"))
                fail_log = redact_path(raw_log_val.get("path"))
            else:
                fail_host = redact_host(failure.get("host"))
                fail_log = redact_path(raw_log_val)

            ep_usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
            in_tok = int(ep_usage["input_tokens"]) if isinstance(ep_usage.get("input_tokens"), int) else None
            out_tok = int(ep_usage["output_tokens"]) if isinstance(ep_usage.get("output_tokens"), int) else None
            cached_tok = int(ep_usage["cached_tokens"]) if isinstance(ep_usage.get("cached_tokens"), int) else None
            reason_tok = int(ep_usage["reasoning_tokens"]) if isinstance(ep_usage.get("reasoning_tokens"), int) else None
            reason_src = "reasoning_tokens" if reason_tok is not None else None
            cost_usd = float(ep_usage["cost_usd"]) if finite(ep_usage.get("cost_usd")) else None
            cost_source = "recorded_native" if cost_usd is not None else "none"
            dur_sec = float(ep_usage["duration_seconds"]) if finite(ep_usage.get("duration_seconds")) else None

            td_entry = trial_digests_map.get(tid, {})
            if using_manifest:
                # Parse EXCLUSIVELY from already-verified in-memory bytes (ZERO second disk read / zero TOCTOU!)
                res_rel = r.get("result")
                verified_res_b = verified_trial_bytes.get(res_rel) if isinstance(res_rel, str) else None
                verified_media_map = {
                    str(m["rel_path"]): str(m["sha256"])
                    for m in (td_entry.get("media") or [])
                    if isinstance(m, dict) and m.get("rel_path") and m.get("sha256")
                }
                res_info = _inspect_trial_result_and_media(
                    batch_dir,
                    res_rel if isinstance(res_rel, str) else None,
                    tid,
                    raw_result_bytes=verified_res_b,
                    verified_media_map=verified_media_map,
                    skip_disk_media_scan=True,
                )
                if isinstance(td_entry.get("verifier_output"), dict) and td_entry["verifier_output"].get("sha256"):
                    res_info["has_verifier_output"] = True
            elif index_legacy_vlm_media:
                res_info = legacy_vlm_results[row_index]
            elif deep_trial_enrich:
                res_info = _inspect_trial_result_and_media(batch_dir, r.get("result"), tid)
            else:
                res_info = {
                    "started_at": None,
                    "finished_at": None,
                    "http_status": None,
                    "cached_tokens": None,
                    "reasoning_tokens": None,
                    "reasoning_key_source": None,
                    "has_inline_media": False,
                    "media_refs": [],
                    "phase_timings": None,
                    "has_verifier_output": False,
                    "result_sha256": None,
                }
            if cached_tok is None and res_info["cached_tokens"] is not None:
                cached_tok = res_info["cached_tokens"]
            if reason_tok is None and res_info["reasoning_tokens"] is not None:
                reason_tok = res_info["reasoning_tokens"]
                reason_src = res_info["reasoning_key_source"]

            tool_call_count = r.get("tool_call_count") if isinstance(r.get("tool_call_count"), int) else None
            ep_ev_refs = r.get("evidence_refs") if isinstance(r.get("evidence_refs"), dict) else {}
            traj_td = td_entry.get("trajectory") if isinstance(td_entry.get("trajectory"), dict) else None
            traj_rel = (
                r.get("trajectory")
                or (traj_td.get("rel_path") if traj_td else None)
                or ((ep_ev_refs.get("trajectory") or {}).get("rel_path") if isinstance(ep_ev_refs.get("trajectory"), dict) else None)
            )
            inline_media_refs = list(res_info["media_refs"])
            has_inline_media = 1 if res_info["has_inline_media"] else 0
            traj_file_sha: str | None = None

            if using_manifest and isinstance(traj_rel, str) and traj_rel in verified_trial_bytes:
                atif_m = _extract_atif_metrics(batch_dir / traj_rel, traj_rel, raw_bytes=verified_trial_bytes[traj_rel])
                traj_file_sha = atif_m["file_sha256"]
                if reason_tok is None and atif_m["reasoning_tokens"] is not None:
                    reason_tok = atif_m["reasoning_tokens"]
                    reason_src = atif_m["reasoning_key_source"]
                if cached_tok is None and atif_m["cached_tokens"] is not None:
                    cached_tok = atif_m["cached_tokens"]
                if tool_call_count is None and atif_m["tool_call_count"] is not None:
                    tool_call_count = atif_m["tool_call_count"]
                if atif_m["has_inline_media"]:
                    has_inline_media = 1
                    base_m = len(inline_media_refs)
                    for off_m, mm_item in enumerate(atif_m["inline_media_meta"]):
                        mm_copy = dict(mm_item)
                        mm_copy["media_idx"] = base_m + off_m
                        inline_media_refs.append(mm_copy)
            elif deep_trial_enrich and isinstance(traj_rel, str) and traj_rel:
                try:
                    traj_path = resolve_within_root(batch_dir, traj_rel)
                except AppError:
                    traj_path = None
                if traj_path is not None and traj_path.is_file():
                    atif_m = _extract_atif_metrics(traj_path, traj_rel)
                    traj_file_sha = atif_m["file_sha256"]
                    if reason_tok is None and atif_m["reasoning_tokens"] is not None:
                        reason_tok = atif_m["reasoning_tokens"]
                        reason_src = atif_m["reasoning_key_source"]
                    if cached_tok is None and atif_m["cached_tokens"] is not None:
                        cached_tok = atif_m["cached_tokens"]
                    if tool_call_count is None and atif_m["tool_call_count"] is not None:
                        tool_call_count = atif_m["tool_call_count"]
                    if atif_m["has_inline_media"]:
                        has_inline_media = 1
                        base_m = len(inline_media_refs)
                        for off_m, mm_item in enumerate(atif_m["inline_media_meta"]):
                            mm_copy = dict(mm_item)
                            mm_copy["media_idx"] = base_m + off_m
                            inline_media_refs.append(mm_copy)

            ep_started = r.get("started_at") or res_info["started_at"]
            ep_finished = r.get("finished_at") or res_info["finished_at"]
            if ep_started and (batch_started_at is None or ep_started < batch_started_at):
                batch_started_at = ep_started
            if ep_finished and (batch_finished_at is None or ep_finished > batch_finished_at):
                batch_finished_at = ep_finished

            gw_status = r.get("gateway_status")
            if gw_status == "captured" or r.get("gateway_trace"):
                has_gateway = 1

            ep_tok_sem = self.token_validator.classify(
                runner=runner,
                harness=r.get("harness") or harness,
                harness_version=r.get("harness_version") or harness_version,
                input_tokens=in_tok,
                cached_tokens=cached_tok,
                output_tokens=out_tok,
                reasoning_tokens=reason_tok,
            )
            if ep_tok_sem == "mutually_exclusive_verified" and batch_token_semantics == "none":
                batch_token_semantics = "mutually_exclusive_verified"
            elif ep_tok_sem == "unverified_components":
                batch_token_semantics = "unverified_components"

            # Bind full SHA-256 digests & storage_tier into evidence_refs_json for lazy-read same-generation checks (I-01 / S-06)
            ev_refs: dict[str, Any] = {}
            td_entry = trial_digests_map.get(tid, {})
            ep_ev_refs = r.get("evidence_refs") if isinstance(r.get("evidence_refs"), dict) else {}
            for kind, rel_key, fallback_sha in (
                ("result", "result", res_info["result_sha256"]),
                ("trajectory", "trajectory", traj_file_sha),
                ("gateway", "gateway_trace", None),
                ("training", "training_trace", None),
                ("training_capture", "training_capture", None),
                ("direct_qa", "direct_qa", None),
                ("failure_locator", "failure_locator", None),
            ):
                td_kind = td_entry.get(kind) if isinstance(td_entry.get(kind), dict) else None
                ep_kind = ep_ev_refs.get(kind) if isinstance(ep_ev_refs.get(kind), dict) else None
                rel_val = (
                    (td_kind.get("rel_path") if td_kind else None)
                    or r.get(rel_key)
                    or (ep_kind.get("rel_path") if ep_kind else None)
                )
                if isinstance(rel_val, str) and rel_val:
                    sha_val = (
                        (td_kind.get("sha256") if td_kind else None)
                        or td_entry.get(f"{kind}_sha256")
                        or (ep_kind.get("sha256") if ep_kind else None)
                        or fallback_sha
                    )
                    storage_tier = (
                        (td_kind.get("storage_tier") if td_kind else None)
                        or (ep_kind.get("storage_tier") if ep_kind else None)
                        or "shared_cifs"
                    )
                    gateway_identity = None
                    defer_gateway_digest = False
                    if sha_val is None and not using_manifest and storage_tier != "evidence_store_0700" and kind in ("training", "gateway"):
                        defer_gateway_digest = kind == "gateway" and not (force or deep_trial_enrich)
                        try:
                            art_fp = resolve_within_root(batch_dir, rel_val)
                            if art_fp.is_file() and not art_fp.is_symlink():
                                if defer_gateway_digest:
                                    gateway_identity = _stat_tuple(art_fp)
                                else:
                                    sha_val, _ = _sha256_file(art_fp)
                        except (AppError, OSError):
                            sha_val = None
                    ref_obj: dict[str, Any] = {
                        "rel_path": rel_val,
                        "generation_id": generation_id,
                        "sha256": sha_val,
                        "storage_tier": storage_tier,
                    }
                    if defer_gateway_digest:
                        ref_obj["pending_file_identity"] = gateway_identity
                    ev_id = (td_kind.get("evidence_id") if td_kind else None) or (ep_kind.get("evidence_id") if ep_kind else None)
                    if ev_id:
                        ref_obj["evidence_id"] = ev_id
                    ev_refs[kind] = ref_obj

            # Legacy training capture compatibility (E-02): bind sibling trials/<tid>/capture.json SHA-256
            # under strict batch-root containment when legacy episodes.jsonl only lists training_trace!
            if (
                not using_manifest
                and "training" in ev_refs
                and "training_capture" not in ev_refs
                and ev_refs["training"].get("storage_tier") != "evidence_store_0700"
            ):
                tr_rel_p = PurePosixPath(str(ev_refs["training"]["rel_path"]))
                if not tr_rel_p.is_absolute() and tr_rel_p.name == "training_trace.json" and tr_rel_p.parent.name == tid:
                    sib_cap_rel = str(tr_rel_p.with_name("capture.json"))
                    try:
                        sib_cap_p = resolve_within_root(batch_dir, sib_cap_rel)
                        if sib_cap_p.is_file() and not sib_cap_p.is_symlink():
                            sib_cap_sha, _ = _sha256_file(sib_cap_p)
                            ev_refs["training_capture"] = {
                                "rel_path": sib_cap_rel,
                                "generation_id": generation_id,
                                "sha256": sib_cap_sha,
                                "storage_tier": "shared_cifs",
                            }
                            ev_refs["training"]["capture_present"] = True
                        else:
                            ev_refs["training"]["capture_present"] = False
                    except (AppError, OSError):
                        ev_refs["training"]["capture_present"] = False
            elif using_manifest and "training" in ev_refs and "training_capture" not in ev_refs:
                ev_refs["training"]["capture_present"] = False
            if res_info["has_verifier_output"] or isinstance(ep_ev_refs.get("verifier_output"), dict):
                res_info["has_verifier_output"] = True
                td_vo = td_entry.get("verifier_output") if isinstance(td_entry.get("verifier_output"), dict) else None
                ep_vo = ep_ev_refs.get("verifier_output") if isinstance(ep_ev_refs.get("verifier_output"), dict) else None
                vo_rel = (
                    str(td_vo["rel_path"])
                    if td_vo and td_vo.get("rel_path")
                    else (
                        str(ep_vo["rel_path"])
                        if ep_vo and ep_vo.get("rel_path")
                        else (
                            str(r["verifier_output"])
                            if isinstance(r.get("verifier_output"), str) and r.get("verifier_output")
                            else f"trials/{tid}/verifier-output.txt"
                        )
                    )
                )
                vo_sha = (td_vo.get("sha256") if td_vo else None) or (ep_vo.get("sha256") if ep_vo else None)
                vo_tier = (td_vo.get("storage_tier") if td_vo else None) or (ep_vo.get("storage_tier") if ep_vo else None) or "shared_cifs"
                if vo_sha is None and not using_manifest and vo_tier != "evidence_store_0700":
                    try:
                        vo_p = resolve_within_root(batch_dir, vo_rel)
                        if vo_p.is_file():
                            vo_sha, _ = _sha256_file(vo_p)
                    except AppError:
                        vo_sha = None
                vo_ref_obj: dict[str, Any] = {
                    "rel_path": vo_rel,
                    "generation_id": generation_id,
                    "sha256": vo_sha,
                    "storage_tier": vo_tier,
                }
                vo_ev_id = (td_vo.get("evidence_id") if td_vo else None) or (ep_vo.get("evidence_id") if ep_vo else None)
                if vo_ev_id:
                    vo_ref_obj["evidence_id"] = vo_ev_id
                ev_refs["verifier_output"] = vo_ref_obj
            if isinstance(td_entry.get("media"), list):
                td_media_by_rel = {
                    str(m["rel_path"]): m
                    for m in td_entry["media"]
                    if isinstance(m, dict) and m.get("rel_path")
                }
                if inline_media_refs:
                    for item in inline_media_refs:
                        m_td = td_media_by_rel.get(str(item.get("rel_path") or ""))
                        if m_td:
                            if m_td.get("storage_tier"):
                                item["storage_tier"] = str(m_td["storage_tier"])
                            if m_td.get("evidence_id"):
                                item["evidence_id"] = m_td["evidence_id"]
                else:
                    for m_idx, m_spec in enumerate(td_entry["media"]):
                        if isinstance(m_spec, dict) and m_spec.get("rel_path"):
                            has_inline_media = 1
                            m_item: dict[str, Any] = {
                                "media_idx": m_idx,
                                "source": str(m_spec.get("source") or "trial_media_dir"),
                                "rel_path": str(m_spec["rel_path"]),
                                "inline_ordinal": int(m_spec.get("inline_ordinal", m_idx)),
                                "file_sha256": m_spec.get("file_sha256"),
                                "sha256": m_spec.get("sha256"),
                                "storage_tier": str(m_spec.get("storage_tier") or "shared_cifs"),
                            }
                            if m_spec.get("evidence_id"):
                                m_item["evidence_id"] = m_spec["evidence_id"]
                            inline_media_refs.append(m_item)
            if inline_media_refs:
                ev_refs["media"] = inline_media_refs

            # Final invariant for manifest-v2 batches: EVERY indexed evidence ref MUST carry a verified non-empty SHA-256!
            if using_manifest:
                unverified_kind: str | None = None
                for k_ev, v_ev in ev_refs.items():
                    if k_ev == "media":
                        for m_ev in v_ev:
                            if not isinstance(m_ev, dict) or not m_ev.get("sha256"):
                                unverified_kind = f"media:{m_ev}"
                                break
                            if m_ev.get("source") in ("result_inline_base64", "trajectory_inline_base64") and not m_ev.get("file_sha256"):
                                unverified_kind = f"inline_media_file:{m_ev.get('rel_path')}"
                                break
                    elif not isinstance(v_ev, dict) or not v_ev.get("sha256"):
                        unverified_kind = k_ev
                    if unverified_kind:
                        break
                if unverified_kind:
                    with self.db.transaction() as conn:
                        self._record_warning(
                            conn,
                            tier="hot",
                            severity="error",
                            code="PUBLISH_MANIFEST_MISMATCH",
                            target_path=str(batch_dir),
                            message=f"索引证据引用缺少已校验 SHA-256 ({tid}/{unverified_kind})，已保留上一代快照",
                        )
                    return "rejected"

            canon_key = make_canonical_trial_key(
                trial_id=tid,
                batch_id=effective_batch_id,
                task_id=task_id,
                checkpoint=r.get("checkpoint") or checkpoint,
                harness_version=r.get("harness_version") or harness_version,
                attempt_index=att_idx,
            )
            enriched_episodes.append(
                {
                    "trial_id": tid,
                    "canonical_trial_key": canon_key,
                    "task_id": task_id,
                    "task_checksum": r.get("task_checksum"),
                    "attempt_index": att_idx,
                    "retry_ids_json": json.dumps(r.get("retry_ids") or []),
                    "status": status,
                    "reward": reward,
                    "reward_valid": reward_valid,
                    "exception_type": exc_type,
                    "http_status": res_info["http_status"],
                    "failure_kind": fail_kind,
                    "failure_summary": fail_summary,
                    "failure_host_redacted": fail_host,
                    "failure_log_redacted": fail_log,
                    "verifier_rewards_json": json.dumps(r.get("verifier_rewards") or {}, ensure_ascii=False),
                    "started_at": ep_started,
                    "finished_at": ep_finished,
                    "cached_tokens": cached_tok,
                    "reasoning_tokens": reason_tok,
                    "tool_call_count": tool_call_count,
                    "phase_timings_json": json.dumps(r.get("phase_timings")) if r.get("phase_timings") else None,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "token_semantics": ep_tok_sem,
                    "reasoning_key_source": reason_src,
                    "cost_usd": cost_usd,
                    "cost_source": cost_source,
                    "duration_sec": dur_sec,
                    "trajectory_status": r.get("trajectory_status"),
                    "gateway_status": gw_status,
                    "training_status": r.get("training_status"),
                    "has_verifier_output": 1 if res_info["has_verifier_output"] else 0,
                    "has_inline_media": has_inline_media,
                    "evidence_refs_json": json.dumps(ev_refs, ensure_ascii=False),
                }
            )

        # Single-transaction atomic commit (Rule 6) with strict in-transaction generation immutability check (I-01)
        with self.db.transaction() as conn:
            prev_row = conn.execute(
                "SELECT generation_id, checks_json, is_running_placeholder FROM batches WHERE batch_id = ?",
                (effective_batch_id,),
            ).fetchone()
            prev_checks: dict[str, Any] = {}
            if prev_row is not None:
                try:
                    parsed_checks = json.loads(prev_row["checks_json"] or "{}")
                    if isinstance(parsed_checks, dict):
                        prev_checks = parsed_checks
                except Exception:
                    prev_checks = {}
            gen_digests_hist: dict[str, str] = {}
            if isinstance(prev_checks.get("generation_digests"), dict):
                gen_digests_hist = {
                    str(k): str(v) for k, v in prev_checks["generation_digests"].items() if isinstance(v, str)
                }

            if prev_row is not None and prev_row["is_running_placeholder"] == 0:
                committed_sha = gen_digests_hist.get(generation_id)
                if committed_sha is None and prev_row["generation_id"] == generation_id:
                    raw_prev_sha = prev_checks.get("verified_payload_sha256")
                    if isinstance(raw_prev_sha, str) and raw_prev_sha:
                        committed_sha = raw_prev_sha
                if committed_sha is not None and committed_sha != verified_payload_sha256:
                    # Same generation_id reused with DIFFERENT verified content -> REJECT & preserve committed data!
                    self._record_warning(
                        conn,
                        tier="hot",
                        severity="error",
                        code="PUBLISH_GENERATION_CONFLICT",
                        target_path=str(batch_dir),
                        message=(
                            f"拒绝同代覆写：generation_id={generation_id} 已绑定内容摘要 "
                            f"{committed_sha[:16]}，与本次 {verified_payload_sha256[:16]} 冲突，已保留已提交数据"
                        ),
                    )
                    return "rejected"
                # Note: When committed_sha == verified_payload_sha256 (idempotent rescan of identical content),
                # intentionally fall through to the single-transaction upsert below so updated project_id,
                # owner_id, evidence_approval, trial_protocol_scores, and search_fts stay atomically synced!

            gen_digests_hist[generation_id] = verified_payload_sha256
            if using_manifest:
                committed_top_map = {
                    rel_k: {"sha256": _sha256_bytes(raw_b), "size": len(raw_b)}
                    for rel_k, raw_b in sorted(verified_bytes.items())
                }
                checks_payload_doc: dict[str, Any] = {
                    "manifest_mode": "publish_manifest_v2",
                    "publish_manifest_sha256": raw_pm_sha256,
                    "top_level_files": committed_top_map,
                    "absent_optional_files": sorted(absent_optional),
                    "verified_payload_sha256": verified_payload_sha256,
                    "generation_digests": gen_digests_hist,
                }
            else:
                if "trial-digests.jsonl" not in legacy_top_bytes:
                    td_legacy_p = batch_dir / "trial-digests.jsonl"
                    if prefetched_top_bytes is not None and "trial-digests.jsonl" in prefetched_top_bytes:
                        legacy_top_bytes["trial-digests.jsonl"] = prefetched_top_bytes["trial-digests.jsonl"]
                    elif td_legacy_p.is_file() and not td_legacy_p.is_symlink():
                        try:
                            legacy_top_bytes["trial-digests.jsonl"] = td_legacy_p.read_bytes()
                        except OSError:
                            pass
                committed_top_map = {
                    rel_k: {"sha256": _sha256_bytes(raw_b), "size": len(raw_b)}
                    for rel_k, raw_b in sorted(legacy_top_bytes.items())
                }
                committed_abs_legacy = sorted(
                    fn for fn in (*_MANDATORY_MANIFEST_FILES, *_OPTIONAL_MANIFEST_FILES)
                    if fn not in legacy_top_bytes
                )
                checks_payload_doc = {
                    "manifest_mode": "legacy_v1",
                    "top_level_files": committed_top_map,
                    "absent_optional_files": committed_abs_legacy,
                    "verified_payload_sha256": verified_payload_sha256,
                    "generation_digests": gen_digests_hist,
                }
                if index_legacy_vlm_media:
                    checks_payload_doc["legacy_vlm_media_indexed"] = True
            checks_payload_json = json.dumps(checks_payload_doc, ensure_ascii=False)

            bm_slug, release_id, identity_status, inferred_scope = resolve_dataset_identity(
                conn, raw_dataset_name, ns, runner
            )
            known_tc: int | None = None
            if release_id:
                rel_row = conn.execute(
                    "SELECT known_task_count FROM dataset_releases WHERE dataset_release_id = ?",
                    (release_id,),
                ).fetchone()
                if rel_row is not None:
                    known_tc = rel_row["known_task_count"]

            run_scope = determine_run_scope(
                harness=harness,
                raw_dataset_name=raw_dataset_name,
                batch_name=batch_name,
                planned=planned_count,
                recorded=recorded_count,
                unique_tasks=unique_tasks_count,
                known_task_count=known_tc,
                inferred_run_scope=inferred_scope,
            )
            scoring_protocol = infer_scoring_protocol(
                source_namespace=ns,
                runner=runner,
                benchmark_slug=bm_slug,
                has_official_result=official_obj is not None,
            )

            red_ep_path = redact_path(str(episodes_path)) or batch_name
            conn.execute(
                "DELETE FROM index_warnings WHERE tier = 'hot' AND code = 'CORRUPT_JSONL_LINE' AND target_path = ?",
                (red_ep_path,),
            )
            for ln, err_msg in corrupt_lines:
                self._record_warning(
                    conn,
                    tier="hot",
                    severity="warning",
                    code="CORRUPT_JSONL_LINE",
                    target_path=str(episodes_path),
                    message=f"第 {ln} 行 JSONL 损坏已跳过: {err_msg}",
                )

            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    is_running_placeholder, internal_source_fingerprint,
                    source_host_redacted, source_path_redacted, source_tags_json,
                    benchmark_slug, dataset_release_id, identity_status, raw_dataset_name,
                    model_route, model_canonical, model_provider, checkpoint, thinking_mode,
                    harness, harness_version, runner, run_scope, scoring_protocol,
                    fault_policy, official_status, batch_status, report_origin,
                    generation_id, trial_digests_sha256, absent_optional_json,
                    unique_tasks, planned, recorded, scored, unscored,
                    coverage, valid_mean, passed, pass_threshold,
                    outcomes_json, failure_kinds_json, vlmeval_metrics_json,
                    run_config_json, token_semantics, usage_json, checks_json,
                    has_gateway, has_selection, started_at, finished_at,
                    content_digest, indexed_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    0, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    'exclude_errors_valid_mean', ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?
                )
                ON CONFLICT(batch_id) DO UPDATE SET
                    source_locator = excluded.source_locator,
                    batch_name = excluded.batch_name,
                    project_id = excluded.project_id,
                    owner_id = excluded.owner_id,
                    evidence_approval = excluded.evidence_approval,
                    is_running_placeholder = 0,
                    internal_source_fingerprint = excluded.internal_source_fingerprint,
                    source_host_redacted = excluded.source_host_redacted,
                    source_path_redacted = excluded.source_path_redacted,
                    benchmark_slug = excluded.benchmark_slug,
                    dataset_release_id = excluded.dataset_release_id,
                    identity_status = excluded.identity_status,
                    raw_dataset_name = excluded.raw_dataset_name,
                    model_route = excluded.model_route,
                    model_canonical = excluded.model_canonical,
                    model_provider = excluded.model_provider,
                    checkpoint = excluded.checkpoint,
                    thinking_mode = excluded.thinking_mode,
                    harness = excluded.harness,
                    harness_version = excluded.harness_version,
                    runner = excluded.runner,
                    run_scope = excluded.run_scope,
                    scoring_protocol = excluded.scoring_protocol,
                    official_status = excluded.official_status,
                    batch_status = excluded.batch_status,
                    report_origin = excluded.report_origin,
                    generation_id = excluded.generation_id,
                    trial_digests_sha256 = excluded.trial_digests_sha256,
                    absent_optional_json = excluded.absent_optional_json,
                    unique_tasks = excluded.unique_tasks,
                    planned = excluded.planned,
                    recorded = excluded.recorded,
                    scored = excluded.scored,
                    unscored = excluded.unscored,
                    coverage = excluded.coverage,
                    valid_mean = excluded.valid_mean,
                    passed = excluded.passed,
                    pass_threshold = excluded.pass_threshold,
                    outcomes_json = excluded.outcomes_json,
                    failure_kinds_json = excluded.failure_kinds_json,
                    vlmeval_metrics_json = excluded.vlmeval_metrics_json,
                    run_config_json = excluded.run_config_json,
                    token_semantics = excluded.token_semantics,
                    usage_json = excluded.usage_json,
                    checks_json = excluded.checks_json,
                    has_gateway = excluded.has_gateway,
                    has_selection = excluded.has_selection,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    content_digest = excluded.content_digest,
                    indexed_at = excluded.indexed_at
                """,
                (
                    effective_batch_id,
                    source_locator,
                    ns,
                    batch_name,
                    project_id,
                    owner_id,
                    reg_approval,
                    internal_fp,
                    host_redacted,
                    path_redacted,
                    source_tags_json,
                    bm_slug,
                    release_id,
                    identity_status,
                    raw_dataset_name,
                    model_route,
                    model_canonical,
                    model_provider,
                    checkpoint,
                    thinking_mode,
                    harness,
                    harness_version,
                    runner,
                    run_scope,
                    scoring_protocol,
                    official_status,
                    progress.get("status"),
                    report_origin,
                    generation_id,
                    trial_digests_sha256,
                    json.dumps(absent_optional),
                    unique_tasks_count,
                    planned_count,
                    recorded_count,
                    scored_count,
                    unscored_count,
                    coverage_val,
                    valid_mean_val,
                    passed_val,
                    pass_threshold_val,
                    json.dumps(totals.get("outcomes") or {}, ensure_ascii=False),
                    json.dumps(failure_kinds, ensure_ascii=False),
                    json.dumps(vlm_metrics_obj, ensure_ascii=False) if vlm_metrics_obj is not None else None,
                    json.dumps(run_config_obj, ensure_ascii=False) if run_config_obj is not None else None,
                    batch_token_semantics,
                    json.dumps(usage_summary, ensure_ascii=False),
                    checks_payload_json,
                    has_gateway,
                    1 if selection_obj is not None else 0,
                    batch_started_at,
                    batch_finished_at,
                    digest,
                    now,
                ),
            )

            conn.execute("DELETE FROM episodes WHERE batch_id = ?", (effective_batch_id,))
            conn.execute("DELETE FROM trial_protocol_scores WHERE batch_id = ?", (effective_batch_id,))
            conn.execute("DELETE FROM selection_tasks WHERE batch_id = ?", (effective_batch_id,))
            conn.execute(
                "DELETE FROM official_results WHERE batch_id = ? AND origin = 'batch_file'", (effective_batch_id,)
            )

            prio_rank = source_priority_rank(ns, scoring_protocol)
            for ep in enriched_episodes:
                conn.execute(
                    """
                    INSERT INTO episodes (
                        batch_id, trial_id, canonical_trial_key, generation_id, source_tags_json,
                        task_id, task_checksum, attempt_index, retry_ids_json,
                        status, reward, reward_valid, exception_type, http_status,
                        failure_kind, failure_summary, failure_host_redacted, failure_log_redacted,
                        verifier_rewards_json, started_at, finished_at,
                        cached_tokens, reasoning_tokens, tool_call_count, phase_timings_json,
                        input_tokens, output_tokens, token_semantics, reasoning_key_source,
                        cost_usd, cost_source, duration_sec,
                        trajectory_status, gateway_status, training_status,
                        has_verifier_output, has_inline_media, evidence_refs_json
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?
                    )
                    """,
                    (
                        effective_batch_id,
                        ep["trial_id"],
                        ep["canonical_trial_key"],
                        generation_id,
                        source_tags_json,
                        ep["task_id"],
                        ep["task_checksum"],
                        ep["attempt_index"],
                        ep["retry_ids_json"],
                        ep["status"],
                        ep["reward"],
                        ep["reward_valid"],
                        ep["exception_type"],
                        ep["http_status"],
                        ep["failure_kind"],
                        ep["failure_summary"],
                        ep["failure_host_redacted"],
                        ep["failure_log_redacted"],
                        ep["verifier_rewards_json"],
                        ep["started_at"],
                        ep["finished_at"],
                        ep["cached_tokens"],
                        ep["reasoning_tokens"],
                        ep["tool_call_count"],
                        ep["phase_timings_json"],
                        ep["input_tokens"],
                        ep["output_tokens"],
                        ep["token_semantics"],
                        ep["reasoning_key_source"],
                        ep["cost_usd"],
                        ep["cost_source"],
                        ep["duration_sec"],
                        ep["trajectory_status"],
                        ep["gateway_status"],
                        ep["training_status"],
                        ep["has_verifier_output"],
                        ep["has_inline_media"],
                        ep["evidence_refs_json"],
                    ),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trial_protocol_scores (
                        canonical_trial_key, scoring_protocol, batch_id, trial_id,
                        project_id, source_namespace, source_priority_rank,
                        dataset_release_id, task_id, status, reward, reward_valid,
                        exception_type, exception_policy, source_tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        ep["canonical_trial_key"],
                        scoring_protocol,
                        effective_batch_id,
                        ep["trial_id"],
                        project_id,
                        ns,
                        prio_rank,
                        release_id,
                        ep["task_id"],
                        ep["status"],
                        ep["reward"],
                        ep["reward_valid"],
                        ep["exception_type"],
                        source_tags_json,
                    ),
                )

            if official_obj is not None:
                conn.execute(
                    """
                    INSERT INTO official_results (
                        batch_id, dataset, metric, score, denominator, source,
                        origin, fault_policy, attached_by, attached_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'batch_file', 'official_protocol', 'indexer', ?)
                    ON CONFLICT(batch_id) DO UPDATE SET
                        dataset = excluded.dataset,
                        metric = excluded.metric,
                        score = excluded.score,
                        denominator = excluded.denominator,
                        source = excluded.source,
                        attached_at = excluded.attached_at
                    """,
                    (
                        effective_batch_id,
                        str(official_obj.get("dataset") or raw_dataset_name or ""),
                        str(official_obj.get("metric") or "score"),
                        float(official_obj["score"]),
                        int(official_obj["denominator"]),
                        str(official_obj.get("source") or "official-result.json"),
                        now,
                    ),
                )

            if selection_obj and isinstance(selection_obj.get("tasks"), list):
                for st_item in selection_obj["tasks"]:
                    if not isinstance(st_item, dict) or not st_item.get("task_id"):
                        continue
                    lane = str(st_item.get("lane") or "review")
                    if lane not in ("grpo", "teacher", "regression", "review"):
                        lane = "review"
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO selection_tasks (
                            batch_id, task_id, task_directory, attempts, scored, passed, excluded_json, lane
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            effective_batch_id,
                            str(st_item["task_id"]),
                            st_item.get("task_directory"),
                            int(st_item.get("attempts") or 0),
                            int(st_item.get("scored") or 0),
                            int(st_item.get("passed") or 0),
                            json.dumps(st_item.get("excluded") or {}, ensure_ascii=False),
                            lane,
                        ),
                    )

            conn.execute("DELETE FROM search_fts WHERE entity_type = 'batch' AND entity_id = ?", (effective_batch_id,))
            conn.execute(
                """
                INSERT INTO search_fts (entity_type, entity_id, title, subtitle, body, project_id, required_scope)
                VALUES ('batch', ?, ?, ?, ?, ?, '')
                """,
                (
                    effective_batch_id,
                    batch_name,
                    f"{raw_dataset_name or ''} · {model_canonical or ''} · {harness or ''} ({runner})",
                    f"{bm_slug or ''} {release_id or ''} {run_scope} {scoring_protocol}",
                    project_id,
                ),
            )

        return "indexed"

    def scan_legacy_leaderboard_csv(self) -> int:
        csv_path = self.settings.legacy_root_dir / "leaderboard.csv"
        if not csv_path.is_file():
            return 0
        st = _stat_tuple(csv_path)
        if st is None:
            return 0
        cache_key = str(csv_path)
        cached = self.db.fetchone("SELECT mtime_ns, size_bytes FROM cold_file_cache WHERE file_path = ?", (cache_key,))
        if cached and cached["mtime_ns"] == st[1] and cached["size_bytes"] == st[2]:
            return 0

        rows_to_insert: list[tuple[Any, ...]] = []
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                raw_bm = (row.get("benchmark") or "").strip()
                if not raw_bm:
                    continue
                m = re.match(r"^(.+?)\(([^)]+)\)$", raw_bm)
                bm_name = m.group(1).strip() if m else raw_bm
                metric = m.group(2).strip() if m else "score"
                scale_type = "score_0_1000" if "1000" in metric else "pct_0_100"

                for col_name, raw_val in row.items():
                    if col_name == "benchmark" or not raw_val or not raw_val.strip():
                        continue
                    try:
                        raw_score = float(raw_val.strip())
                    except ValueError:
                        continue
                    norm_100 = normalize_score_to_100(raw_score, scale_type)
                    harness = "direct"
                    if col_name.endswith("_OpenClaw"):
                        harness = "openclaw"
                    elif col_name.endswith("_Pi"):
                        harness = "pi"
                    model_canonical, _, ck, think = canonicalize_model(col_name)
                    rows_to_insert.append(
                        (
                            bm_name,
                            metric,
                            col_name,
                            model_canonical or col_name,
                            ck,
                            think,
                            harness,
                            raw_score,
                            norm_100,
                            scale_type,
                            redact_path(str(csv_path)) or "2026-11-20/leaderboard.csv",
                        )
                    )

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM legacy_leaderboard_rows")
            for item in rows_to_insert:
                bm_name = item[0]
                bm_slug, rel_id, _, _ = resolve_dataset_identity(conn, bm_name, "legacy_csv", "legacy")
                conn.execute(
                    """
                    INSERT INTO legacy_leaderboard_rows (
                        benchmark, metric, benchmark_slug, dataset_release_id,
                        model_col, model_canonical, checkpoint, thinking_mode,
                        harness, raw_score, norm_score_100, scale_type, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item[0],
                        item[1],
                        bm_slug,
                        rel_id,
                        item[2],
                        item[3],
                        item[4],
                        item[5],
                        item[6],
                        item[7],
                        item[8],
                        item[9],
                        item[10],
                    ),
                )
            conn.execute(
                """
                INSERT INTO cold_file_cache (file_path, cache_kind, mtime_ns, size_bytes, item_count, status, updated_at)
                VALUES (?, 'legacy_csv', ?, ?, ?, 'ready', ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    mtime_ns = excluded.mtime_ns,
                    size_bytes = excluded.size_bytes,
                    item_count = excluded.item_count,
                    status = 'ready',
                    updated_at = excluded.updated_at
                """,
                (cache_key, st[1], st[2], len(rows_to_insert), utcnow_iso()),
            )
        return len(rows_to_insert)

    def scan_hot_datasets(self, *, force: bool = False) -> int:
        ds_root = self.settings.datasets_dir
        if not ds_root.is_dir():
            return 0
        indexed = 0
        mapping = [
            (ds_root / "tianxi-synth-v1" / "train", "tianxi-synth-v1__1.0__train", "train"),
            (ds_root / "tianxi-synth-v1" / "holdout", "tianxi-synth-v1__1.0__holdout", "holdout"),
        ]
        for folder, rel_id, split in mapping:
            st = _stat_tuple(folder)
            if st is None:
                continue
            cache_key = f"hot_ds:{folder}"
            if not force:
                cached = self.db.fetchone(
                    "SELECT mtime_ns, status FROM cold_file_cache WHERE file_path = ?",
                    (cache_key,),
                )
                if cached and cached["status"] == "ready" and cached["mtime_ns"] == st[1]:
                    continue
            # Parallel read of task.toml & instruction.md presence on CIFS (~10x faster on cold cache!)
            try:
                task_dirs = sorted(
                    Path(e.path) for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)
                )
            except OSError:
                continue

            def _read_task_dir(td: Path) -> tuple[Any, ...] | None:
                toml_file = td / "task.toml"
                try:
                    parsed = tomllib.loads(toml_file.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    return None
                task_sec = parsed.get("task") or {}
                meta_sec = parsed.get("metadata") or {}
                task_id = str(task_sec.get("name") or td.name)
                inst_file = td / "instruction.md"
                inst_ref = str(inst_file) if inst_file.is_file() else None
                return (
                    rel_id,
                    task_id,
                    td.name,
                    split,
                    str(meta_sec.get("source") or "tianxi-synth-v1"),
                    meta_sec.get("difficulty"),
                    meta_sec.get("category"),
                    meta_sec.get("language") or "en",
                    json.dumps(meta_sec.get("tags") or [], ensure_ascii=False),
                    json.dumps(parsed.get("environment") or {}, ensure_ascii=False),
                    json.dumps(meta_sec, ensure_ascii=False),
                    inst_ref,
                )

            with ThreadPoolExecutor(max_workers=16) as pool:
                task_rows = [row for row in pool.map(_read_task_dir, task_dirs) if row is not None]

            folder_count = len(task_rows)
            with self.db.transaction() as conn:
                rel_exists = conn.execute(
                    "SELECT 1 FROM dataset_releases WHERE dataset_release_id = ?", (rel_id,)
                ).fetchone()
                if not rel_exists:
                    continue
                conn.executemany(
                    """
                    INSERT INTO task_definitions (
                        dataset_release_id, task_id, task_dir, split, source,
                        difficulty, category, language, tags_json, resources_json,
                        extra_json, instruction_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset_release_id, task_id) DO UPDATE SET
                        task_dir = excluded.task_dir,
                        difficulty = excluded.difficulty,
                        category = excluded.category,
                        tags_json = excluded.tags_json,
                        instruction_ref = excluded.instruction_ref
                    """,
                    task_rows,
                )
                indexed += folder_count
                conn.execute(
                    """
                    INSERT INTO cold_file_cache (file_path, cache_kind, mtime_ns, size_bytes, item_count, status, updated_at)
                    VALUES (?, 'hot_dataset_dir', ?, 0, ?, 'ready', ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        mtime_ns = excluded.mtime_ns,
                        item_count = excluded.item_count,
                        status = 'ready',
                        updated_at = excluded.updated_at
                    """,
                    (cache_key, st[1], folder_count, utcnow_iso()),
                )
        return indexed

    def enrich_harbor_trials(self, *, batch_ids: list[str] | None = None) -> dict[str, int]:
        """Parallel trial-level enrichment (result, trajectory, media, and deferred Gateway digests)
        for legacy harbor-runs batches (without publish-manifest.json).
        Manifest-v2 batches (trial_digests_sha256 IS NOT NULL) are already enriched from verified in-memory bytes
        during atomic ingest and are strictly skipped here to eliminate any second unverified disk read."""
        if batch_ids:
            ph = ",".join("?" for _ in batch_ids)
            batches = self.db.fetchall(
                f"SELECT batch_id, source_locator, runner, harness, harness_version, content_digest FROM batches WHERE batch_id IN ({ph}) AND source_namespace = 'harbor_runs' AND is_running_placeholder = 0 AND trial_digests_sha256 IS NULL",
                batch_ids,
            )
        else:
            batches = self.db.fetchall(
                "SELECT batch_id, source_locator, runner, harness, harness_version, content_digest FROM batches WHERE source_namespace = 'harbor_runs' AND is_running_placeholder = 0 AND trial_digests_sha256 IS NULL"
            )
        enriched_trials = 0
        enriched_batches = 0
        for b in batches:
            cache_key = f"trial_enrich:{b['batch_id']}"
            cached = self.db.fetchone("SELECT error_message, status FROM cold_file_cache WHERE file_path = ?", (cache_key,))
            if cached and cached["status"] == "ready" and cached["error_message"] == b["content_digest"]:
                continue
            bname = str(b["source_locator"]).split(":", 1)[1]
            bdir = self.settings.harbor_runs_dir / bname
            if not bdir.is_dir():
                continue
            eps = self.db.fetchall(
                """
                SELECT trial_id, input_tokens, output_tokens, cached_tokens, reasoning_tokens,
                       reasoning_key_source, tool_call_count, started_at, finished_at, evidence_refs_json
                FROM episodes WHERE batch_id = ?
                """,
                (b["batch_id"],),
            )
            if not eps:
                continue

            def _enrich_one(ep_row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                tid = ep_row["trial_id"]
                ev = json.loads(ep_row["evidence_refs_json"] or "{}")
                rel_res = (ev.get("result") or {}).get("rel_path")
                rel_traj = (ev.get("trajectory") or {}).get("rel_path")
                r_info = _inspect_trial_result_and_media(bdir, rel_res, tid)
                t_info = None
                if isinstance(rel_traj, str) and rel_traj:
                    try:
                        tp = resolve_within_root(bdir, rel_traj)
                        if tp.is_file():
                            t_info = _extract_atif_metrics(tp, rel_traj)
                    except AppError:
                        t_info = None
                gateway = ev.get("gateway")
                if isinstance(gateway, dict) and gateway.get("pending_file_identity"):
                    try:
                        gateway_path = resolve_within_root(bdir, gateway["rel_path"])
                        expected_identity = tuple(gateway["pending_file_identity"])
                        if _stat_tuple(gateway_path) == expected_identity:
                            gateway_digest, _ = _sha256_file(gateway_path)
                            if _stat_tuple(gateway_path) == expected_identity:
                                gateway["sha256"] = gateway_digest
                                del gateway["pending_file_identity"]
                    except (AppError, OSError):
                        pass
                return tid, {"ep": ep_row, "ev": ev, "res": r_info, "traj": t_info}

            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(_enrich_one, eps))

            with self.db.transaction() as conn:
                if conn.execute(
                    "SELECT 1 FROM batches WHERE batch_id = ? AND content_digest = ?",
                    (b["batch_id"], b["content_digest"]),
                ).fetchone() is None:
                    continue
                for tid, bundle in results:
                    ep_row = bundle["ep"]
                    ev = bundle["ev"]
                    r_info = bundle["res"]
                    t_info = bundle["traj"]
                    cached_tok = ep_row["cached_tokens"]
                    reason_tok = ep_row["reasoning_tokens"]
                    reason_src = ep_row["reasoning_key_source"]
                    tool_cnt = ep_row["tool_call_count"]
                    if cached_tok is None and r_info["cached_tokens"] is not None:
                        cached_tok = r_info["cached_tokens"]
                    if reason_tok is None and r_info["reasoning_tokens"] is not None:
                        reason_tok = r_info["reasoning_tokens"]
                        reason_src = r_info["reasoning_key_source"]
                    media_refs = list(r_info["media_refs"])
                    has_media = 1 if r_info["has_inline_media"] else 0
                    if t_info:
                        if cached_tok is None and t_info["cached_tokens"] is not None:
                            cached_tok = t_info["cached_tokens"]
                        if reason_tok is None and t_info["reasoning_tokens"] is not None:
                            reason_tok = t_info["reasoning_tokens"]
                            reason_src = t_info["reasoning_key_source"]
                        if tool_cnt is None and t_info["tool_call_count"] is not None:
                            tool_cnt = t_info["tool_call_count"]
                        if t_info["has_inline_media"]:
                            has_media = 1
                            base_m = len(media_refs)
                            for off_m, mm in enumerate(t_info["inline_media_meta"]):
                                mm_c = dict(mm)
                                mm_c["media_idx"] = base_m + off_m
                                media_refs.append(mm_c)
                        if "trajectory" in ev and not ev["trajectory"].get("sha256") and t_info["file_sha256"]:
                            ev["trajectory"]["sha256"] = t_info["file_sha256"]
                    if "result" in ev and not ev["result"].get("sha256") and r_info["result_sha256"]:
                        ev["result"]["sha256"] = r_info["result_sha256"]
                    if media_refs:
                        ev["media"] = media_refs
                    tok_sem = self.token_validator.classify(
                        runner=b["runner"],
                        harness=b["harness"],
                        harness_version=b["harness_version"],
                        input_tokens=ep_row["input_tokens"],
                        cached_tokens=cached_tok,
                        output_tokens=ep_row["output_tokens"],
                        reasoning_tokens=reason_tok,
                    )
                    conn.execute(
                        """
                        UPDATE episodes
                        SET started_at = COALESCE(started_at, ?),
                            finished_at = COALESCE(finished_at, ?),
                            http_status = COALESCE(http_status, ?),
                            cached_tokens = ?,
                            reasoning_tokens = ?,
                            reasoning_key_source = ?,
                            tool_call_count = ?,
                            token_semantics = ?,
                            has_verifier_output = MAX(has_verifier_output, ?),
                            has_inline_media = MAX(has_inline_media, ?),
                            evidence_refs_json = ?
                        WHERE batch_id = ? AND trial_id = ?
                        """,
                        (
                            r_info["started_at"],
                            r_info["finished_at"],
                            r_info["http_status"],
                            cached_tok,
                            reason_tok,
                            reason_src,
                            tool_cnt,
                            tok_sem,
                            1 if r_info["has_verifier_output"] else 0,
                            has_media,
                            json.dumps(ev, ensure_ascii=False),
                            b["batch_id"],
                            tid,
                        ),
                    )
                    enriched_trials += 1
                conn.execute(
                    """
                    INSERT INTO cold_file_cache (file_path, cache_kind, mtime_ns, size_bytes, item_count, status, error_message, updated_at)
                    VALUES (?, 'trial_enrich', 0, 0, ?, 'ready', ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        item_count = excluded.item_count,
                        status = 'ready',
                        error_message = excluded.error_message,
                        updated_at = excluded.updated_at
                    """,
                    (cache_key, len(results), b["content_digest"], utcnow_iso()),
                )
            enriched_batches += 1
        return {"batches": enriched_batches, "trials": enriched_trials}

    def run_hot_scan(self, *, force: bool = False, deep_trial_enrich: bool = False) -> dict[str, Any]:
        """Parallel CIFS-optimized hot scan:
        - Uses single os.scandir(harbor-runs) + ThreadPoolExecutor to stat all batch dirs concurrently
          -> < 120ms on restart cache-hit & no-change poll even after CIFS actimeo=1 expires!
        - Prefetches top-level files of changed/new batches IN PARALLEL across 20 worker threads
          -> < 2.0s initial cold scan across all 53 harbor-runs + leaderboard.csv + datasets!"""
        t0 = time.perf_counter()
        counts: Counter[str] = Counter()
        hr = self.settings.harbor_runs_dir
        if hr.is_dir():
            unbackfilled_tr_batches: set[str] = set()
            if not force:
                for tr_r in self.db.fetchall(
                    """
                    SELECT e.batch_id, e.evidence_refs_json
                    FROM episodes e
                    JOIN batches b ON b.batch_id = e.batch_id
                    WHERE b.source_namespace = 'harbor_runs'
                      AND b.is_running_placeholder = 0
                      AND e.evidence_refs_json LIKE '%"training"%'
                    """
                ):
                    if _training_ref_needs_backfill(tr_r["evidence_refs_json"]):
                        unbackfilled_tr_batches.add(str(tr_r["batch_id"]))

            existing_digests: dict[str, str] = {}
            for r in self.db.fetchall(
                """
                SELECT batch_id, source_locator, content_digest, generation_id, checks_json, is_running_placeholder, runner
                FROM batches WHERE source_namespace = 'harbor_runs'
                """
            ):
                if not force and _batch_needs_backfill(
                    r["checks_json"],
                    r["generation_id"],
                    r["is_running_placeholder"],
                    runner=r["runner"],
                    has_unbackfilled_training=str(r["batch_id"]) in unbackfilled_tr_batches,
                ):
                    # Revisit older rows even when the source files have not changed.
                    continue
                existing_digests[str(r["source_locator"])] = str(r["content_digest"])
            try:
                with os.scandir(hr) as it:
                    entries = sorted(
                        (Path(e.path) for e in it if e.is_dir(follow_symlinks=False)),
                        key=lambda p: p.name,
                    )
            except OSError:
                entries = []

            def _probe_dir(d: Path) -> tuple[Path, str | None, list[str], str | None]:
                items: list[str] = []
                present_top: list[str] = []
                has_vlm_dir = False
                try:
                    with os.scandir(d) as it:
                        for entry in it:
                            name = entry.name
                            if name == "vlmevalkit" and entry.is_dir(follow_symlinks=False):
                                has_vlm_dir = True
                                continue
                            if name in _WATCHED_TOP_FILES:
                                try:
                                    st = entry.stat(follow_symlinks=False)
                                    items.append(f"{name}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}")
                                    present_top.append(name)
                                except OSError:
                                    pass
                except PermissionError:
                    return d, None, [], "permission_denied"
                except OSError as exc:
                    return d, None, [], f"os_error:{exc}"
                if has_vlm_dir:
                    st_v = _stat_tuple(d / "vlmevalkit" / "metrics.json")
                    if st_v is not None:
                        items.append(f"vlmevalkit/metrics.json:{st_v[0]}:{st_v[1]}:{st_v[2]}")
                        present_top.append("vlmevalkit/metrics.json")
                items.sort()
                dg = hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()
                return d, dg, present_top, None

            with ThreadPoolExecutor(max_workers=20) as pool:
                probed = list(pool.map(_probe_dir, entries))

            to_scan: list[tuple[Path, str, list[str]]] = []
            for d, dg, present_top, err in probed:
                if err == "permission_denied":
                    red_p = redact_path(str(d)) or d.name
                    if self.db.fetchone(
                        "SELECT 1 FROM index_warnings WHERE tier = 'hot' AND code = 'DIR_PERMISSION_DENIED' AND target_path = ?",
                        (red_p,),
                    ) is None:
                        with self.db.transaction() as conn:
                            self._record_warning(
                                conn,
                                tier="hot",
                                severity="warning",
                                code="DIR_PERMISSION_DENIED",
                                target_path=str(d),
                                message=f"目录无读权限，已安全跳过: {d.name}",
                            )
                    counts["error"] += 1
                    continue
                if err is not None or dg is None:
                    counts["error"] += 1
                    continue
                loc = f"harbor_runs:{d.name}"
                if not force and existing_digests.get(loc) == dg:
                    counts["skipped_unchanged"] += 1
                    continue
                to_scan.append((d, dg, present_top))

            # Prefetch top-level files of all dirty batches in parallel on CIFS!
            if to_scan:
                def _prefetch_batch(item: tuple[Path, str, list[str]]) -> tuple[Path, str, dict[str, bytes] | None]:
                    d, dg, present_top = item
                    if "publish-manifest.json" in present_top:
                        # Manifest batches do strict hash-verification read inside _verify_publish_manifest
                        try:
                            pm_bytes = (d / "publish-manifest.json").read_bytes()
                            return d, dg, {"publish-manifest.json": pm_bytes}
                        except OSError:
                            return d, dg, None
                    buf: dict[str, bytes] = {}
                    for rel in present_top:
                        fp = d / rel
                        try:
                            pre_st = _stat_tuple(fp)
                            raw = fp.read_bytes()
                            post_st = _stat_tuple(fp)
                            if pre_st != post_st:
                                return d, dg, None
                            buf[rel] = raw
                        except OSError:
                            return d, dg, None
                    return d, dg, buf

                # Source registry paths also require CIFS metadata lookups.
                def _lookup_approval(batch_dir: Path) -> tuple[str | None, str | None, str | None]:
                    try:
                        return self.approval_registry.lookup(batch_dir)
                    except (OSError, RuntimeError):
                        # Let the batch scanner report resolution errors on its normal path.
                        return None, None, None

                with ThreadPoolExecutor(max_workers=20) as pool:
                    prefetched = list(pool.map(_prefetch_batch, to_scan))
                    approvals = list(pool.map(_lookup_approval, (d for d, _, _ in prefetched)))

                for (d, dg, top_buf), (project_id, owner_id, approval) in zip(prefetched, approvals, strict=True):
                    res = self.scan_single_harbor_batch(
                        d,
                        force=force,
                        project_override=project_id,
                        owner_override=owner_id,
                        approval_override=approval,
                        existing_digests=existing_digests,
                        precomputed_digest=dg,
                        prefetched_top_bytes=top_buf,
                        deep_trial_enrich=deep_trial_enrich,
                    )
                    counts[res] += 1

        # Also index Runner-published isolated public DTOs (source_namespace='runner_published') so
        # batches published via default Web Start survive restarts & never return 404 (I-05)!
        pub_dir = self.settings.published_batches_dir
        if pub_dir is not None and pub_dir.is_dir():
            try:
                pub_entries = sorted(
                    (Path(e.path) for e in os.scandir(pub_dir) if e.is_dir(follow_symlinks=False)),
                    key=lambda p: p.name,
                )
            except OSError:
                pub_entries = []
            for pd in pub_entries:
                res = self.scan_single_harbor_batch(
                    pd,
                    force=force,
                    source_namespace="runner_published",
                    deep_trial_enrich=deep_trial_enrich,
                )
                counts[res] += 1

        csv_rows = self.scan_legacy_leaderboard_csv()
        ds_tasks = self.scan_hot_datasets(force=force)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "elapsed_ms": elapsed_ms,
            "batches": dict(counts),
            "legacy_csv_rows": csv_rows,
            "dataset_tasks": ds_tasks,
        }
