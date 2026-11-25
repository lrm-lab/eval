from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tarfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import ijson
import zstandard as zstd

from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.hot_indexer import SourceApprovalRegistry, _stat_tuple
from app.indexing.identity import (
    canonicalize_model,
    make_batch_id,
    make_canonical_trial_key,
    resolve_dataset_identity,
    source_priority_rank,
)
from app.indexing.media_matcher import VersionedMediaMatcher
from app.security.redaction import redact_path, redact_text
from app.services.methodology import finite

_TOP_DICT_KEY_RE = re.compile(rb'^    "([^"]+)": \{\s*$')
_TOP_DICT_END_RE = re.compile(rb'^    \},?\s*$')
_MAX_TASK_INSTRUCTION_BYTES = 256 * 1024
MAX_TASK_ATTACHMENT_BYTES = 8 * 1024 * 1024
_GAIA_ATTACHMENT_EXTENSIONS = {
    ".csv", ".docx", ".jpg", ".jsonld", ".mp3", ".pdf", ".png",
    ".pdb", ".pptx", ".py", ".txt", ".xlsx", ".zip",
}
_TASK_BUNDLES = {
    "gaia-validation-20261124.tar": {(): "gaia__2026-11-24__val"},
    "wildclawbench-20261120-tasks.tar.zst": {(): "wildclawbench__2026-11-20__default"},
    "pi-openclaw-20261120-core.tar": {
        ("datasets", "deepswe", "tasks"): "deepswe__1.1__default",
        ("datasets", "terminal-bench-2.1", "tasks"): "terminal-bench__2.1__default",
    },
}
_CORE_TASK_ID_PREFIXES = {
    "deepswe__1.1__default": "",
    "terminal-bench__2.1__default": "terminal-bench/",
}
_WILDCLAW_SUMMARIES = {
    "openclaw-2026.11.6-Qwen3_8-27B": ("openclaw", "openclaw-2026.11.6", "vllm/Qwen3_8-27B"),
    "openclaw-2026.11.6-Qwen3_8-27B-v5prompt": ("openclaw", "openclaw-2026.11.6", "vllm/Qwen3_8-27B"),
    "pi-jhpV5-Qwen3_8-27B": ("pi", "pi-jhpV5", "relay-openai/Qwen3_8-27B"),
    "pi-jhpV5-Qwen3_8-27B-nov5": ("pi", "pi-jhpV5", "relay-openai/Qwen3_8-27B"),
}


def _wildclaw_episode(obj: dict[str, Any], task_id: str, trial_id: str, attempt: int) -> dict[str, Any]:
    scores = obj.get("scores") if isinstance(obj.get("scores"), dict) else {}
    raw_score = scores.get("overall_score") if finite(scores.get("overall_score")) else scores.get("reward")
    valid = finite(raw_score) and not (obj.get("error") or obj.get("timed_out"))
    status = "scored" if valid else ("timeout" if obj.get("timed_out") else "failed")
    return {
        "trial_id": trial_id,
        "task_id": task_id,
        "attempt_index": attempt,
        "status": status,
        "reward": float(raw_score) if valid else None,
        "reward_valid": int(valid),
        "verifier_rewards_json": json.dumps(scores, ensure_ascii=False),
    }


def _parse_iso_dt(val: str | None) -> float | None:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _phase_sec(phase_obj: Any) -> float | None:
    if not isinstance(phase_obj, dict):
        return None
    s = _parse_iso_dt(phase_obj.get("started_at"))
    e = _parse_iso_dt(phase_obj.get("finished_at"))
    if s is not None and e is not None and e >= s:
        return round(e - s, 3)
    return None


def _parse_tb21_dir_name(dir_name: str) -> tuple[str, str]:
    if "__" in dir_name:
        h, m = dir_name.split("__", 1)
        return h.strip(), m.strip()
    return "harbor", dir_name


def _parse_deepswe_filename(stem: str) -> tuple[str, str]:
    if stem.startswith("Pi_v5_"):
        return "pi-v5", stem[len("Pi_v5_") :]
    if stem.startswith("Pi_"):
        return "pi", stem[len("Pi_") :]
    if stem.startswith("OpenClaw_"):
        return "openclaw", stem[len("OpenClaw_") :]
    return "harbor", stem


def _parse_gaia_filename(stem: str) -> tuple[str, str]:
    if stem.endswith("_OpenClaw"):
        return "openclaw", stem[: -len("_OpenClaw")]
    if stem.endswith("_Pi"):
        return "pi", stem[: -len("_Pi")]
    return "harbor", stem


def read_legacy_sample_by_offset(file_path: Path, start_offset: int, end_offset: int) -> dict[str, Any]:
    """O(1) byte-slice seek read for both legacy JSONL lines and pretty-printed JSON dict entries (< 10ms)."""
    with open(file_path, "rb") as fh:
        fh.seek(start_offset)
        raw = fh.read(max(0, end_offset - start_offset))
    text = raw.decode("utf-8", errors="replace").strip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    return json.loads(text)


class ColdIndexer:
    """Async cold-tier indexer covering ALL 197 historical archives (v2.2.1 04-1 §1.1):
    - Native Harbor 2026-11-20/terminal-bench2.1/ (6 jobs, 534 trials, 490 valid incl. 230 with exception_info)
    - 2026-11-20/DeepSWE/*.json (3 files: 113 / 78 / 48)
    - 2026-11-20/GAIA/*.json (2 files x 165, preserving both gaia-official-scorer & gaia-raw-correct)
    - 2026-11-20/wildclawbench/* (indexes readable batches + reports CIFS PermissionError gaps per dir)
    - ALL 10 multimodal .jsonl directories -> O(1) byte offsets + cached versioned media matching
    - ALL 12 text .json dict directories (IFEval, GPQA, IFBench, HLE, AA-LCR, Multi-LCB, Apex, Multi-Challenge, LongMemEval x4) -> O(1) streaming byte-offset seek cache
    - bundles/*.tar and 7.5 GiB wildclawbench-20261120-tasks.tar.zst -> streamed once & cached by (file_path, mtime_ns, size)
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.media_matcher = VersionedMediaMatcher(settings.media_rules_file)
        self.approval_registry = SourceApprovalRegistry(settings.source_approval_file)
        self._coverage_lock = threading.Lock()
        self._coverage_cache: tuple[float, list[dict[str, Any]]] | None = None

    def invalidate_source_coverage_cache(self) -> None:
        with self._coverage_lock:
            self._coverage_cache = None

    def _load_cache_map(self) -> dict[str, tuple[int, int, str]]:
        return {
            r["file_path"]: (int(r["mtime_ns"]), int(r["size_bytes"]), str(r["status"]))
            for r in self.db.fetchall("SELECT file_path, mtime_ns, size_bytes, status FROM cold_file_cache")
        }

    def _is_cached(
        self,
        file_key: str,
        mtime_ns: int,
        size_bytes: int,
        *,
        allow_permission_denied: bool = False,
        cache_map: dict[str, tuple[int, int, str]] | None = None,
    ) -> bool:
        allowed = ("ready", "permission_denied", "partial_permission_denied") if allow_permission_denied else ("ready",)
        if cache_map is not None:
            entry = cache_map.get(file_key)
            return bool(entry is not None and entry[2] in allowed and entry[0] == mtime_ns and entry[1] == size_bytes)
        row = self.db.fetchone(
            "SELECT mtime_ns, size_bytes, status FROM cold_file_cache WHERE file_path = ?",
            (file_key,),
        )
        return bool(
            row is not None
            and row["status"] in allowed
            and row["mtime_ns"] == mtime_ns
            and row["size_bytes"] == size_bytes
        )

    def _upsert_cache(
        self,
        conn,
        file_key: str,
        cache_kind: str,
        mtime_ns: int,
        size_bytes: int,
        item_count: int,
        status: str = "ready",
        error_message: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO cold_file_cache (
                file_path, cache_kind, mtime_ns, size_bytes, item_count, status, error_message, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                cache_kind = excluded.cache_kind,
                mtime_ns = excluded.mtime_ns,
                size_bytes = excluded.size_bytes,
                item_count = excluded.item_count,
                status = excluded.status,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
            """,
            (file_key, cache_kind, mtime_ns, size_bytes, item_count, status, error_message, utcnow_iso()),
        )

    def index_native_tb21(self, *, force: bool = False) -> dict[str, int]:
        tb_root = self.settings.legacy_root_dir / "terminal-bench2.1"
        if not tb_root.is_dir():
            return {"batches": 0, "trials": 0}

        reg_proj, reg_owner, reg_approval = self.approval_registry.lookup(tb_root)
        cache_map = self._load_cache_map()
        batches_done = 0
        trials_done = 0

        try:
            with os.scandir(tb_root) as it:
                job_entries = sorted(
                    ((Path(e.path), e.stat(follow_symlinks=False)) for e in it if e.is_dir(follow_symlinks=False)),
                    key=lambda x: x[0].name,
                )
        except OSError:
            return {"batches": 0, "trials": 0}

        for job_dir, st_obj in job_entries:
            cache_key = str(job_dir)
            # Fast single-scandir stat check BEFORE walking 89 trial subdirectories on CIFS (< 20ms cache hit!)
            if not force and self._is_cached(cache_key, st_obj.st_mtime_ns, st_obj.st_size, cache_map=cache_map):
                continue
            trial_dirs = sorted(p for p in job_dir.iterdir() if p.is_dir() and (p / "result.json").is_file())
            if not trial_dirs:
                continue
            sig_mtime = st_obj.st_mtime_ns
            sig_size = st_obj.st_size

            source_locator = f"native_harbor:terminal-bench2.1/{job_dir.name}"
            batch_id = make_batch_id(source_locator)
            harness, model_route = _parse_tb21_dir_name(job_dir.name)
            model_canonical, model_provider, checkpoint, thinking_mode = canonicalize_model(model_route)
            digest = hashlib.sha256(f"{source_locator}:{sig_mtime}:{sig_size}".encode("utf-8")).hexdigest()
            generation_id = f"gen_native_{digest[:16]}"
            now = utcnow_iso()

            ep_rows: list[dict[str, Any]] = []
            valid_scores: list[float] = []
            outcomes: dict[str, int] = {}
            failure_kinds: dict[str, int] = {}
            batch_started: str | None = None
            batch_finished: str | None = None

            for td in trial_dirs:
                rj_path = td / "result.json"
                try:
                    obj = json.loads(rj_path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                tid = str(obj.get("id") or td.name)
                task_id = str(obj.get("task_name") or td.name.split("__", 1)[0])
                task_checksum = obj.get("task_checksum")
                vr = (obj.get("verifier_result") or {}).get("rewards") or {}
                raw_reward = vr.get("reward")
                native_rv = 1 if (bool(obj.get("reward_valid")) and finite(raw_reward)) else 0
                native_reward = float(raw_reward) if finite(raw_reward) else None

                exc_info = obj.get("exception_info") if isinstance(obj.get("exception_info"), dict) else None
                exc_type = str(exc_info.get("exception_type")) if (exc_info and exc_info.get("exception_type") is not None) else None
                raw_policy = exc_info.get("policy") if exc_info else None
                exc_policy = (
                    json.dumps(raw_policy, ensure_ascii=False)
                    if isinstance(raw_policy, (dict, list))
                    else (str(raw_policy) if raw_policy is not None else None)
                )
                exc_msg = redact_text(str(exc_info.get("exception_message"))) if (exc_info and exc_info.get("exception_message") is not None) else None

                if native_rv == 1:
                    status = "scored"
                    reward = native_reward
                    valid_scores.append(float(reward))  # type: ignore[arg-type]
                elif exc_type and "timeout" in str(exc_type).lower():
                    status = "timeout"
                    reward = None
                else:
                    status = "failed"
                    reward = None

                outcomes[status] = outcomes.get(status, 0) + 1
                if exc_type:
                    failure_kinds[str(exc_type)] = failure_kinds.get(str(exc_type), 0) + 1

                s_at = obj.get("started_at")
                f_at = obj.get("finished_at")
                s_ts = _parse_iso_dt(s_at)
                f_ts = _parse_iso_dt(f_at)
                dur = round(f_ts - s_ts, 3) if (s_ts is not None and f_ts is not None and f_ts >= s_ts) else None
                if s_at and (batch_started is None or s_at < batch_started):
                    batch_started = s_at
                if f_at and (batch_finished is None or f_at > batch_finished):
                    batch_finished = f_at

                phase_timings = {
                    "environment_setup_s": _phase_sec(obj.get("environment_setup")),
                    "agent_setup_s": _phase_sec(obj.get("agent_setup")),
                    "agent_execution_s": _phase_sec(obj.get("agent_execution")),
                    "verifier_s": _phase_sec(obj.get("verifier")),
                }
                ar = obj.get("agent_result") if isinstance(obj.get("agent_result"), dict) else {}
                in_tok = int(ar["n_input_tokens"]) if isinstance(ar.get("n_input_tokens"), int) else None
                out_tok = int(ar["n_output_tokens"]) if isinstance(ar.get("n_output_tokens"), int) else None
                cache_tok = int(ar["n_cache_tokens"]) if isinstance(ar.get("n_cache_tokens"), int) else None
                cost_usd = float(ar["cost_usd"]) if finite(ar.get("cost_usd")) else None
                cost_source = "recorded_native" if cost_usd is not None else "none"

                verifier_stdout = td / "verifier" / "test-stdout.txt"
                has_ver_out = 1 if verifier_stdout.is_file() else 0
                artifacts_dir = td / "artifacts"
                has_art = 1 if (artifacts_dir.is_dir() and any(artifacts_dir.iterdir())) else 0

                agent_info = obj.get("agent_info") if isinstance(obj.get("agent_info"), dict) else {}
                hv = agent_info.get("version")

                ev_refs: dict[str, Any] = {
                    "result": {"abs_path": str(rj_path), "generation_id": generation_id, "storage_tier": "shared_cifs"},
                }
                if has_ver_out:
                    ev_refs["verifier_output"] = {
                        "abs_path": str(verifier_stdout),
                        "generation_id": generation_id,
                        "storage_tier": "shared_cifs",
                    }
                traj_cand = td / "agent" / "trajectory.json"
                if traj_cand.is_file():
                    ev_refs["trajectory"] = {
                        "abs_path": str(traj_cand),
                        "generation_id": generation_id,
                        "storage_tier": "shared_cifs",
                    }

                canon_key = make_canonical_trial_key(
                    trial_id=tid,
                    batch_id=batch_id,
                    task_id=task_id,
                    checkpoint=checkpoint,
                    harness_version=hv,
                )
                ep_rows.append(
                    {
                        "trial_id": tid,
                        "canonical_trial_key": canon_key,
                        "task_id": task_id,
                        "task_checksum": task_checksum,
                        "status": status,
                        "reward": reward,
                        "reward_valid": native_rv,
                        "native_reward_valid": native_rv,
                        "native_reward": native_reward,
                        "exception_type": exc_type,
                        "exception_policy": exc_policy,
                        "failure_kind": exc_type,
                        "failure_summary": exc_msg[:400] if exc_msg else None,
                        "verifier_rewards_json": json.dumps(vr, ensure_ascii=False),
                        "started_at": s_at,
                        "finished_at": f_at,
                        "cached_tokens": cache_tok,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cost_usd": cost_usd,
                        "cost_source": cost_source,
                        "duration_sec": dur,
                        "phase_timings_json": json.dumps(phase_timings),
                        "has_verifier_output": has_ver_out,
                        "has_artifacts": has_art,
                        "evidence_refs_json": json.dumps(ev_refs, ensure_ascii=False),
                        "harness_version": hv,
                    }
                )

            scored_n = len(valid_scores)
            total_n = len(ep_rows)
            valid_mean = (math.fsum(valid_scores) / scored_n) if scored_n > 0 else None
            passed_n = sum(1 for v in valid_scores if v >= 1.0)
            hv_batch = next((e["harness_version"] for e in ep_rows if e["harness_version"]), None)

            with self.db.transaction() as conn:
                bm_slug, release_id, id_status, _ = resolve_dataset_identity(
                    conn, "terminal-bench2.1", "native_harbor", "harbor"
                )
                protocol = "harbor-native-reward-valid-v1"
                prio = source_priority_rank("native_harbor", protocol)
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_id, source_locator, source_namespace, batch_name,
                        project_id, owner_id, evidence_approval,
                        is_running_placeholder, source_path_redacted, source_tags_json,
                        benchmark_slug, dataset_release_id, identity_status, raw_dataset_name,
                        model_route, model_canonical, model_provider, checkpoint, thinking_mode,
                        harness, harness_version, runner, run_scope, scoring_protocol,
                        fault_policy, official_status, batch_status, report_origin,
                        generation_id, unique_tasks, planned, recorded, scored, unscored,
                        coverage, valid_mean, passed, pass_threshold,
                        outcomes_json, failure_kinds_json, token_semantics,
                        started_at, finished_at, content_digest, indexed_at
                    ) VALUES (
                        ?, ?, 'native_harbor', ?,
                        ?, ?, ?,
                        0, ?, '["native_harbor","2026-11-20"]',
                        ?, ?, ?, 'Terminal-Bench 2.1',
                        ?, ?, ?, ?, ?,
                        ?, ?, 'harbor', 'full_split', ?,
                        'exclude_errors_valid_mean', 'unofficial_valid_mean', 'finished', 'legacy_adapter',
                        ?, ?, ?, ?, ?, 0,
                        ?, ?, ?, 1.0,
                        ?, ?, 'unverified_components',
                        ?, ?, ?, ?
                    )
                    ON CONFLICT(batch_id) DO UPDATE SET
                        project_id = excluded.project_id,
                        owner_id = excluded.owner_id,
                        evidence_approval = excluded.evidence_approval,
                        valid_mean = excluded.valid_mean,
                        scored = excluded.scored,
                        recorded = excluded.recorded,
                        passed = excluded.passed,
                        coverage = excluded.coverage,
                        outcomes_json = excluded.outcomes_json,
                        failure_kinds_json = excluded.failure_kinds_json,
                        generation_id = excluded.generation_id,
                        content_digest = excluded.content_digest,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        batch_id,
                        source_locator,
                        job_dir.name,
                        reg_proj,
                        reg_owner,
                        reg_approval,
                        redact_path(str(job_dir)),
                        bm_slug,
                        release_id,
                        id_status,
                        model_route,
                        model_canonical,
                        model_provider,
                        checkpoint,
                        thinking_mode,
                        harness,
                        hv_batch,
                        protocol,
                        generation_id,
                        len({e["task_id"] for e in ep_rows}),
                        total_n,
                        total_n,
                        scored_n,
                        (scored_n / total_n) if total_n > 0 else None,
                        valid_mean,
                        passed_n,
                        json.dumps(outcomes, ensure_ascii=False),
                        json.dumps(failure_kinds, ensure_ascii=False),
                        batch_started,
                        batch_finished,
                        digest,
                        now,
                    ),
                )
                conn.execute("DELETE FROM episodes WHERE batch_id = ?", (batch_id,))
                conn.execute("DELETE FROM trial_protocol_scores WHERE batch_id = ?", (batch_id,))
                for ep in ep_rows:
                    conn.execute(
                        """
                        INSERT INTO episodes (
                            batch_id, trial_id, canonical_trial_key, generation_id, source_tags_json,
                            task_id, task_checksum, status, reward, reward_valid,
                            native_reward_valid, native_reward, exception_type, exception_policy,
                            failure_kind, failure_summary, verifier_rewards_json,
                            started_at, finished_at, cached_tokens, input_tokens, output_tokens,
                            cost_usd, cost_source, duration_sec, phase_timings_json,
                            has_verifier_output, has_artifacts, evidence_refs_json
                        ) VALUES (
                            ?, ?, ?, ?, '["native_harbor","2026-11-20"]',
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?
                        )
                        """,
                        (
                            batch_id,
                            ep["trial_id"],
                            ep["canonical_trial_key"],
                            generation_id,
                            ep["task_id"],
                            ep["task_checksum"],
                            ep["status"],
                            ep["reward"],
                            ep["reward_valid"],
                            ep["native_reward_valid"],
                            ep["native_reward"],
                            ep["exception_type"],
                            ep["exception_policy"],
                            ep["failure_kind"],
                            ep["failure_summary"],
                            ep["verifier_rewards_json"],
                            ep["started_at"],
                            ep["finished_at"],
                            ep["cached_tokens"],
                            ep["input_tokens"],
                            ep["output_tokens"],
                            ep["cost_usd"],
                            ep["cost_source"],
                            ep["duration_sec"],
                            ep["phase_timings_json"],
                            ep["has_verifier_output"],
                            ep["has_artifacts"],
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
                        ) VALUES (?, ?, ?, ?, ?, 'native_harbor', ?, ?, ?, ?, ?, ?, ?, ?, '["native_harbor"]')
                        """,
                        (
                            ep["canonical_trial_key"],
                            protocol,
                            batch_id,
                            ep["trial_id"],
                            reg_proj,
                            prio,
                            release_id,
                            ep["task_id"],
                            ep["status"],
                            ep["reward"],
                            ep["reward_valid"],
                            ep["exception_type"],
                            ep["exception_policy"],
                        ),
                    )
                self._upsert_cache(conn, cache_key, "native_harbor_job", sig_mtime, sig_size, len(ep_rows))
                batches_done += 1
                trials_done += len(ep_rows)

        return {"batches": batches_done, "trials": trials_done}

    def index_legacy_agent_json(self, benchmark_dir_name: str, *, force: bool = False) -> dict[str, int]:
        folder = self.settings.legacy_root_dir / benchmark_dir_name
        if not folder.is_dir():
            return {"batches": 0, "trials": 0}

        reg_proj, reg_owner, reg_approval = self.approval_registry.lookup(folder)
        cache_map = self._load_cache_map()
        batches_done = 0
        trials_done = 0

        try:
            with os.scandir(folder) as it:
                jfiles = sorted(
                    (
                        (Path(e.path), e.stat(follow_symlinks=False))
                        for e in it
                        if e.is_file(follow_symlinks=False) and e.name.endswith(".json")
                    ),
                    key=lambda x: x[0].name,
                )
        except OSError:
            return {"batches": 0, "trials": 0}

        for jfile, st_obj in jfiles:
            st = (st_obj.st_ino, st_obj.st_mtime_ns, st_obj.st_size)
            cache_key = str(jfile)
            if not force and self._is_cached(cache_key, st[1], st[2], cache_map=cache_map):
                continue

            source_locator = f"legacy_json:{benchmark_dir_name}/{jfile.name}"
            batch_id = make_batch_id(source_locator)
            if benchmark_dir_name == "DeepSWE":
                harness, model_route = _parse_deepswe_filename(jfile.stem)
                raw_ds = "DeepSWE"
                primary_protocol = "deepswe-f2p-p2p"
            else:
                harness, model_route = _parse_gaia_filename(jfile.stem)
                raw_ds = "GAIA"
                primary_protocol = "gaia-official-scorer"

            model_canonical, model_provider, checkpoint, thinking_mode = canonicalize_model(model_route)
            digest = hashlib.sha256(f"{source_locator}:{st[1]}:{st[2]}".encode("utf-8")).hexdigest()
            generation_id = f"gen_legacy_{digest[:16]}"
            now = utcnow_iso()

            ep_rows: list[dict[str, Any]] = []
            valid_scores: list[float] = []
            outcomes: dict[str, int] = {}
            failure_kinds: dict[str, int] = {}

            try:
                with open(jfile, "rb") as fh:
                    for key, item in ijson.kvitems(fh, ""):
                        if not isinstance(item, dict):
                            continue
                        task_id = str(item.get("question_id") or key)
                        raw_status = str(item.get("status") or "ok").lower()
                        exc_type = (item.get("exception") or "").strip() or None

                        if benchmark_dir_name == "DeepSWE":
                            raw_corr = item.get("correct")
                            raw_rew = item.get("reward")
                            passed_val = 1.0 if (raw_corr is True or raw_rew == 1) else 0.0
                            is_valid = raw_status == "ok" and not exc_type
                            status = "scored" if is_valid else ("timeout" if raw_status == "timeout" else "failed")
                            reward = passed_val if is_valid else None
                            vr = {
                                "reward": passed_val,
                                "correct": bool(raw_corr),
                                "f2p_passed": item.get("f2p_passed"),
                                "f2p_total": item.get("f2p_total"),
                                "p2p_passed": item.get("p2p_passed"),
                                "p2p_total": item.get("p2p_total"),
                                "partial": float(item["partial"]) if finite(item.get("partial")) else None,
                                "apply_failed": item.get("apply_failed"),
                            }
                            dur = float(item["trial_time_s"]) if finite(item.get("trial_time_s")) else None
                            in_tok = int(item["prompt_tokens"]) if isinstance(item.get("prompt_tokens"), int) else None
                            out_tok = int(item["completion_tokens"]) if isinstance(item.get("completion_tokens"), int) else None
                            reason_tok = int(item["reasoning_tokens"]) if isinstance(item.get("reasoning_tokens"), int) else None
                            extra_protocols: list[tuple[str, str, float | None, int]] = []
                        else:
                            raw_corr = bool(item.get("correct"))
                            off_corr = bool(item.get("correct_official_gaia_scorer"))
                            is_valid = raw_status == "ok" and not exc_type
                            status = "scored" if is_valid else ("timeout" if raw_status == "timeout" else "failed")
                            reward = (1.0 if off_corr else 0.0) if is_valid else None
                            raw_reward_val = (1.0 if raw_corr else 0.0) if is_valid else None
                            vr = {
                                "correct": raw_corr,
                                "correct_official_gaia_scorer": off_corr,
                                "level": item.get("level"),
                                "leak_category": item.get("leak_category"),
                                "leak_note": item.get("leak_note"),
                            }
                            dur = float(item["trial_time_s"]) if finite(item.get("trial_time_s")) else None
                            in_tok = None
                            out_tok = None
                            reason_tok = None
                            extra_protocols = [("gaia-raw-correct", status, raw_reward_val, 1 if is_valid else 0)]

                        if is_valid and reward is not None:
                            valid_scores.append(reward)
                        outcomes[status] = outcomes.get(status, 0) + 1
                        if exc_type:
                            failure_kinds[exc_type] = failure_kinds.get(exc_type, 0) + 1

                        tid = str(item.get("harbor_trial") or f"{jfile.stem}__{task_id}")
                        canon_key = make_canonical_trial_key(
                            trial_id=tid,
                            batch_id=batch_id,
                            task_id=task_id,
                            checkpoint=checkpoint,
                            harness_version=None,
                        )
                        ep_rows.append(
                            {
                                "trial_id": tid,
                                "canonical_trial_key": canon_key,
                                "task_id": task_id,
                                "status": status,
                                "reward": reward,
                                "reward_valid": 1 if is_valid else 0,
                                "exception_type": exc_type,
                                "failure_kind": exc_type,
                                "verifier_rewards_json": json.dumps(vr, ensure_ascii=False),
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "reasoning_tokens": reason_tok,
                                "reasoning_key_source": "reasoning_tokens" if reason_tok is not None else None,
                                "duration_sec": dur,
                                "evidence_refs_json": json.dumps(
                                    {"legacy_json": {"file_path": str(jfile), "key": str(key), "storage_tier": "shared_cifs"}}
                                ),
                                "extra_protocols": extra_protocols,
                            }
                        )
            except Exception as exc:
                with self.db.transaction() as conn:
                    self._upsert_cache(conn, cache_key, "legacy_json_dict", st[1], st[2], 0, "error", str(exc))
                continue

            total_n = len(ep_rows)
            scored_n = len(valid_scores)
            valid_mean = (math.fsum(valid_scores) / scored_n) if scored_n > 0 else None
            passed_n = sum(1 for v in valid_scores if v >= 1.0)
            known_full = 113 if benchmark_dir_name == "DeepSWE" else 165
            run_scope = "full_split" if total_n == known_full else "subset"

            with self.db.transaction() as conn:
                bm_slug, release_id, id_status, _ = resolve_dataset_identity(conn, raw_ds, "legacy_json", "harbor")
                prio = source_priority_rank("legacy_json", primary_protocol)
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_id, source_locator, source_namespace, batch_name,
                        project_id, owner_id, evidence_approval,
                        is_running_placeholder, source_path_redacted, source_tags_json,
                        benchmark_slug, dataset_release_id, identity_status, raw_dataset_name,
                        model_route, model_canonical, model_provider, checkpoint, thinking_mode,
                        harness, runner, run_scope, scoring_protocol,
                        fault_policy, official_status, batch_status, report_origin,
                        generation_id, unique_tasks, planned, recorded, scored, unscored,
                        coverage, valid_mean, passed, pass_threshold,
                        outcomes_json, failure_kinds_json, token_semantics,
                        content_digest, indexed_at
                    ) VALUES (
                        ?, ?, 'legacy_json', ?,
                        ?, ?, ?,
                        0, ?, '["legacy_json","2026-11-20"]',
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, 'harbor', ?, ?,
                        'exclude_errors_valid_mean', 'unofficial_valid_mean', 'finished', 'legacy_adapter',
                        ?, ?, ?, ?, ?, 0,
                        ?, ?, ?, 1.0,
                        ?, ?, 'unverified_components',
                        ?, ?
                    )
                    ON CONFLICT(batch_id) DO UPDATE SET
                        project_id = excluded.project_id,
                        owner_id = excluded.owner_id,
                        evidence_approval = excluded.evidence_approval,
                        valid_mean = excluded.valid_mean,
                        scored = excluded.scored,
                        recorded = excluded.recorded,
                        passed = excluded.passed,
                        coverage = excluded.coverage,
                        outcomes_json = excluded.outcomes_json,
                        failure_kinds_json = excluded.failure_kinds_json,
                        generation_id = excluded.generation_id,
                        content_digest = excluded.content_digest,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        batch_id,
                        source_locator,
                        f"{benchmark_dir_name}/{jfile.stem}",
                        reg_proj,
                        reg_owner,
                        reg_approval,
                        redact_path(str(jfile)),
                        bm_slug,
                        release_id,
                        id_status,
                        raw_ds,
                        model_route,
                        model_canonical,
                        model_provider,
                        checkpoint,
                        thinking_mode,
                        harness,
                        run_scope,
                        primary_protocol,
                        generation_id,
                        total_n,
                        total_n,
                        total_n,
                        scored_n,
                        (scored_n / total_n) if total_n > 0 else None,
                        valid_mean,
                        passed_n,
                        json.dumps(outcomes, ensure_ascii=False),
                        json.dumps(failure_kinds, ensure_ascii=False),
                        digest,
                        now,
                    ),
                )
                conn.execute("DELETE FROM episodes WHERE batch_id = ?", (batch_id,))
                conn.execute("DELETE FROM trial_protocol_scores WHERE batch_id = ?", (batch_id,))
                for ep in ep_rows:
                    conn.execute(
                        """
                        INSERT INTO episodes (
                            batch_id, trial_id, canonical_trial_key, generation_id, source_tags_json,
                            task_id, status, reward, reward_valid, exception_type, failure_kind,
                            verifier_rewards_json, input_tokens, output_tokens, reasoning_tokens,
                            reasoning_key_source, duration_sec, evidence_refs_json
                        ) VALUES (?, ?, ?, ?, '["legacy_json","2026-11-20"]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            ep["trial_id"],
                            ep["canonical_trial_key"],
                            generation_id,
                            ep["task_id"],
                            ep["status"],
                            ep["reward"],
                            ep["reward_valid"],
                            ep["exception_type"],
                            ep["failure_kind"],
                            ep["verifier_rewards_json"],
                            ep["input_tokens"],
                            ep["output_tokens"],
                            ep["reasoning_tokens"],
                            ep["reasoning_key_source"],
                            ep["duration_sec"],
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
                        ) VALUES (?, ?, ?, ?, ?, 'legacy_json', ?, ?, ?, ?, ?, ?, ?, NULL, '["legacy_json"]')
                        """,
                        (
                            ep["canonical_trial_key"],
                            primary_protocol,
                            batch_id,
                            ep["trial_id"],
                            reg_proj,
                            prio,
                            release_id,
                            ep["task_id"],
                            ep["status"],
                            ep["reward"],
                            ep["reward_valid"],
                            ep["exception_type"],
                        ),
                    )
                    for ex_proto, ex_status, ex_rew, ex_rv in ep["extra_protocols"]:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO trial_protocol_scores (
                                canonical_trial_key, scoring_protocol, batch_id, trial_id,
                                project_id, source_namespace, source_priority_rank,
                                dataset_release_id, task_id, status, reward, reward_valid,
                                exception_type, exception_policy, source_tags_json
                            ) VALUES (?, ?, ?, ?, ?, 'legacy_json', ?, ?, ?, ?, ?, ?, ?, NULL, '["legacy_json"]')
                            """,
                            (
                                ep["canonical_trial_key"],
                                ex_proto,
                                batch_id,
                                ep["trial_id"],
                                reg_proj,
                                prio,
                                release_id,
                                ep["task_id"],
                                ex_status,
                                ex_rew,
                                ex_rv,
                                ep["exception_type"],
                            ),
                        )

                self._upsert_cache(conn, cache_key, "legacy_json_dict", st[1], st[2], total_n)
                batches_done += 1
                trials_done += total_n

        return {"batches": batches_done, "trials": trials_done}

    def index_legacy_wildclawbench(self, *, force: bool = False) -> dict[str, Any]:
        """Index readable 2026-11-20/wildclawbench results & explicitly report CIFS permission gaps."""
        wc_root = self.settings.legacy_root_dir / "wildclawbench"
        if not wc_root.is_dir():
            return {"batches": 0, "unreadable_dirs": 0}
        cache_map = self._load_cache_map()
        batches_done = 0
        unreadable_dirs = 0

        try:
            with os.scandir(wc_root) as it:
                run_entries = sorted(
                    ((Path(e.path), e.stat(follow_symlinks=False)) for e in it if e.is_dir(follow_symlinks=False)),
                    key=lambda x: x[0].name,
                )
        except OSError:
            return {"batches": 0, "unreadable_dirs": 0}

        for run_dir, st_obj in run_entries:
            cache_key = str(run_dir)
            st = (st_obj.st_ino, st_obj.st_mtime_ns, st_obj.st_size)
            reg_proj, reg_owner, reg_approval, classes = self.approval_registry.lookup_full(run_dir)
            summary_identity = _WILDCLAW_SUMMARIES.get(run_dir.name)
            summary_path = run_dir / f"rsi_summary_{summary_identity[1]}_all.json" if summary_identity else None
            summary_stat = _stat_tuple(summary_path) if summary_path and not summary_path.is_symlink() else None
            use_summary = (
                summary_stat is not None and reg_approval != "pending" and "summary_dto" in classes
                and self.approval_registry.lookup_full(summary_path) == (reg_proj, reg_owner, reg_approval, classes)
            )
            if use_summary:
                st = summary_stat
                cache_key = str(summary_path)
            if not force and self._is_cached(
                cache_key, st[1], st[2], allow_permission_denied=not use_summary, cache_map=cache_map
            ):
                continue

            ep_rows: list[dict[str, Any]] = []
            summary_digest: str | None = None
            harness, harness_version, model_route = "harbor", None, run_dir.name
            run_config: str | None = None
            seen_tids: set[str] = set()
            att_counter: dict[str, int] = {}
            unreadable_files = 0
            if use_summary:
                harness, harness_version, model_route = summary_identity
                try:
                    raw = summary_path.read_bytes()
                    if _stat_tuple(summary_path) != summary_stat:
                        continue
                    summary = json.loads(raw)
                    reports = summary["results"]
                    if (
                        summary["bench"] != "wildclawbench" or summary["model"] != model_route
                        or summary["agent"] != harness_version or summary["n"] != 60
                        or not isinstance(reports, dict) or len(reports) != 60
                        or not finite(summary["score"])
                        or not all(
                            re.fullmatch(r"[A-Za-z0-9_.-]+", task_id)
                            and isinstance(result, dict)
                            and isinstance(result.get("task_id"), str)
                            and re.fullmatch(r"[A-Za-z0-9_.-]+", result["task_id"])
                            for task_id, result in reports.items()
                        )
                        or len({result["task_id"] for result in reports.values()}) != 60
                    ):
                        raise ValueError("WildClawBench summary identity or task records do not match")
                    known_tasks = self.db.fetchall(
                        "SELECT task_id FROM task_definitions WHERE dataset_release_id = ?",
                        ("wildclawbench__2026-11-20__default",),
                    )
                    if known_tasks and set(reports) != {row["task_id"] for row in known_tasks}:
                        raise ValueError("WildClawBench summary contains tasks outside its registered release")
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                for task_id, obj in sorted(reports.items()):
                    scores = obj.get("scores") or {}
                    public_scores = {
                        key: float(value) for key, value in scores.items() if finite(value)
                    } if isinstance(scores, dict) else {}
                    # Result keys identify benchmark tasks. The inner task_id identifies a historical attempt.
                    episode = _wildclaw_episode(
                        {**obj, "scores": public_scores}, task_id, f"{run_dir.name}__{obj['task_id']}", 1,
                    )
                    if not episode["reward_valid"] and not (obj.get("error") or obj.get("timed_out")):
                        episode["status"] = "unscored"
                    ep_rows.append(episode)
                valid_scores = [ep["reward"] for ep in ep_rows if ep["reward_valid"]]
                if not all(0 <= score <= 1 for score in valid_scores) or not math.isclose(
                    math.fsum(valid_scores) / len(ep_rows), summary["score"], abs_tol=1e-8,
                ):
                    continue
                summary_digest = hashlib.sha256(raw).hexdigest()
                run_config = json.dumps({"source_score": summary["score"], "source_denominator": summary["n"]})

            for rj in [] if use_summary else sorted(run_dir.rglob("result.json")):
                try:
                    obj = json.loads(rj.read_text(encoding="utf-8", errors="replace"))
                except PermissionError:
                    unreadable_files += 1
                    continue
                except Exception:
                    continue
                if not isinstance(obj, dict) or not obj.get("task_id"):
                    continue
                task_id = str(obj["task_id"])
                att_counter[task_id] = att_counter.get(task_id, 0) + 1
                rel_parent = str(rj.parent.relative_to(run_dir)).replace("/", "__")
                tid = f"{run_dir.name}__{rel_parent}"
                if tid in seen_tids:
                    tid = f"{tid}__{att_counter[task_id]}"
                seen_tids.add(tid)
                ep_rows.append(_wildclaw_episode(obj, task_id, tid, att_counter[task_id]))

            # Always inspect score.json permission gaps even when some result.json files were readable
            # (e.g. pi-jhpV5-Qwen3_8-27B has 1 readable result.json + 82 permission-denied score.json!)
            for sj in [] if use_summary else run_dir.rglob("score.json"):
                try:
                    with open(sj, "rb") as _fh:
                        _fh.read(1)
                except PermissionError:
                    unreadable_files += 1
                except Exception:
                    pass

            if not ep_rows:
                if unreadable_files > 0:
                    unreadable_dirs += 1
                    with self.db.transaction() as conn:
                        self._upsert_cache(
                            conn,
                            cache_key,
                            "legacy_wildclaw_dir",
                            st[1],
                            st[2],
                            0,
                            "permission_denied",
                            f"{unreadable_files} 个评分文件无读权限 (PermissionError)",
                        )
                        red_p = redact_path(str(run_dir)) or run_dir.name
                        conn.execute(
                            "DELETE FROM index_warnings WHERE tier = 'cold' AND code = 'WILDCLAW_PERMISSION_DENIED' AND target_path = ?",
                            (red_p,),
                        )
                        conn.execute(
                            """
                            INSERT INTO index_warnings (tier, severity, code, target_path, message, detected_at)
                            VALUES ('cold', 'warning', 'WILDCLAW_PERMISSION_DENIED', ?, ?, ?)
                            """,
                            (
                                red_p,
                                f"wildclawbench/{run_dir.name} 存在 {unreadable_files} 个无读权限评分文件，无法索引",
                                utcnow_iso(),
                            ),
                        )
                continue

            source_locator = f"legacy_json:wildclawbench/{run_dir.name}"
            batch_id = make_batch_id(source_locator)
            mc, mp, ck, th = canonicalize_model(model_route)
            digest = summary_digest or hashlib.sha256(f"{source_locator}:{st[1]}:{len(ep_rows)}".encode("utf-8")).hexdigest()
            gen_id = f"gen_legacy_{digest[:16]}"
            scored_vals = [e["reward"] for e in ep_rows if e["reward_valid"] == 1 and e["reward"] is not None]
            valid_mean = (math.fsum(scored_vals) / len(scored_vals)) if scored_vals else None

            with self.db.transaction() as conn:
                bm_slug, rel_id, id_status, _ = resolve_dataset_identity(conn, "wildclawbench", "legacy_json", "harbor")
                proto = "wildclaw-overall-score-v1"
                prio = source_priority_rank("legacy_json", proto)
                conn.execute(
                    """
                    INSERT INTO batches (
                        batch_id, source_locator, source_namespace, batch_name,
                        project_id, owner_id, evidence_approval,
                        is_running_placeholder, source_path_redacted, source_tags_json,
                        benchmark_slug, dataset_release_id, identity_status, raw_dataset_name,
                        model_route, model_canonical, model_provider, checkpoint, thinking_mode,
                        harness, harness_version, runner, run_scope, scoring_protocol,
                        official_status, batch_status, report_origin,
                        generation_id, unique_tasks, planned, recorded, scored, unscored,
                        coverage, valid_mean, run_config_json, content_digest, indexed_at
                    ) VALUES (
                        ?, ?, 'legacy_json', ?,
                        ?, ?, ?,
                        0, ?, '["legacy_json","2026-11-20"]',
                        ?, ?, ?, 'WildClawBench',
                        ?, ?, ?, ?, ?,
                        ?, ?, 'harbor', ?, ?,
                        'unofficial_valid_mean', 'finished', 'legacy_adapter',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(batch_id) DO UPDATE SET
                        project_id = excluded.project_id,
                        owner_id = excluded.owner_id,
                        evidence_approval = excluded.evidence_approval,
                        model_route = excluded.model_route,
                        model_canonical = excluded.model_canonical,
                        model_provider = excluded.model_provider,
                        checkpoint = excluded.checkpoint,
                        thinking_mode = excluded.thinking_mode,
                        harness = excluded.harness,
                        harness_version = excluded.harness_version,
                        run_scope = excluded.run_scope,
                        generation_id = excluded.generation_id,
                        unique_tasks = excluded.unique_tasks,
                        planned = excluded.planned,
                        coverage = excluded.coverage,
                        valid_mean = excluded.valid_mean,
                        scored = excluded.scored,
                        recorded = excluded.recorded,
                        unscored = excluded.unscored,
                        run_config_json = excluded.run_config_json,
                        content_digest = excluded.content_digest,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        batch_id,
                        source_locator,
                        f"wildclawbench/{run_dir.name}",
                        reg_proj,
                        reg_owner,
                        reg_approval,
                        redact_path(str(run_dir)),
                        bm_slug,
                        rel_id,
                        id_status,
                        model_route,
                        mc,
                        mp,
                        ck,
                        th,
                        harness,
                        harness_version,
                        "full_split" if len({e["task_id"] for e in ep_rows}) == 60 else "subset",
                        proto,
                        gen_id,
                        len({e["task_id"] for e in ep_rows}),
                        len(ep_rows),
                        len(ep_rows),
                        len(scored_vals),
                        sum(ep["status"] == "unscored" for ep in ep_rows),
                        (len(scored_vals) / len(ep_rows)) if ep_rows else None,
                        valid_mean,
                        run_config,
                        digest,
                        utcnow_iso(),
                    ),
                )
                conn.execute("DELETE FROM episodes WHERE batch_id = ?", (batch_id,))
                conn.execute("DELETE FROM trial_protocol_scores WHERE batch_id = ?", (batch_id,))
                for ep in ep_rows:
                    ckey = make_canonical_trial_key(
                        trial_id=ep["trial_id"],
                        batch_id=batch_id,
                        task_id=ep["task_id"],
                        checkpoint=ck,
                        harness_version=harness_version,
                        attempt_index=ep["attempt_index"],
                    )
                    conn.execute(
                        """
                        INSERT INTO episodes (
                            batch_id, trial_id, canonical_trial_key, generation_id, source_tags_json,
                            task_id, attempt_index, status, reward, reward_valid, verifier_rewards_json
                        ) VALUES (?, ?, ?, ?, '["legacy_json","2026-11-20"]', ?, ?, ?, ?, ?, ?)
                        """,
                        (batch_id, ep["trial_id"], ckey, gen_id, ep["task_id"], ep["attempt_index"], ep["status"], ep["reward"], ep["reward_valid"], ep["verifier_rewards_json"]),
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO trial_protocol_scores (
                            canonical_trial_key, scoring_protocol, batch_id, trial_id,
                            project_id, source_namespace, source_priority_rank,
                            dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                        ) VALUES (?, ?, ?, ?, ?, 'legacy_json', ?, ?, ?, ?, ?, ?, '["legacy_json"]')
                        """,
                        (ckey, proto, batch_id, ep["trial_id"], reg_proj, prio, rel_id, ep["task_id"], ep["status"], ep["reward"], ep["reward_valid"]),
                    )
                if unreadable_files > 0:
                    unreadable_dirs += 1
                    self._upsert_cache(
                        conn,
                        cache_key,
                        "legacy_wildclaw_dir",
                        st[1],
                        st[2],
                        len(ep_rows),
                        "partial_permission_denied",
                        f"已索引 {len(ep_rows)} 条可读结果，另 {unreadable_files} 个 score.json 无读权限",
                    )
                    red_p = redact_path(str(run_dir)) or run_dir.name
                    conn.execute(
                        "DELETE FROM index_warnings WHERE tier = 'cold' AND code = 'WILDCLAW_PERMISSION_DENIED' AND target_path = ?",
                        (red_p,),
                    )
                    conn.execute(
                        """
                        INSERT INTO index_warnings (tier, severity, code, target_path, message, detected_at)
                        VALUES ('cold', 'warning', 'WILDCLAW_PERMISSION_DENIED', ?, ?, ?)
                        """,
                        (
                            red_p,
                            f"wildclawbench/{run_dir.name} 已索引 {len(ep_rows)} 条，另 {unreadable_files} 个评分文件无读权限",
                            utcnow_iso(),
                        ),
                    )
                else:
                    if use_summary:
                        conn.execute("DELETE FROM cold_file_cache WHERE file_path = ?", (str(run_dir),))
                    self._upsert_cache(
                        conn, cache_key, "legacy_wildclaw_summary" if use_summary else "legacy_wildclaw_dir",
                        st[1], st[2], len(ep_rows),
                    )
                    conn.execute(
                        "DELETE FROM index_warnings WHERE tier = 'cold' AND code = 'WILDCLAW_PERMISSION_DENIED' AND target_path = ?",
                        (redact_path(str(run_dir)) or run_dir.name,),
                    )
                batches_done += 1
        return {"batches": batches_done, "unreadable_dirs": unreadable_dirs}

    def index_legacy_jsonl_offsets(self, benchmark_dir_name: str, *, force: bool = False) -> int:
        folder = self.settings.legacy_root_dir / benchmark_dir_name
        if not folder.is_dir():
            return 0
        cache_map = self._load_cache_map()
        indexed_files = 0
        try:
            with os.scandir(folder) as it:
                jl_entries = sorted(
                    (
                        (Path(e.path), e.stat(follow_symlinks=False))
                        for e in it
                        if e.is_file(follow_symlinks=False) and e.name.endswith(".jsonl")
                    ),
                    key=lambda x: x[0].name,
                )
        except OSError:
            return 0
        for jl, st_obj in jl_entries:
            st = (st_obj.st_ino, st_obj.st_mtime_ns, st_obj.st_size)
            cache_key = str(jl)
            if not force and self._is_cached(cache_key, st[1], st[2], cache_map=cache_map):
                continue

            offsets: list[tuple[Any, ...]] = []
            try:
                with open(jl, "rb") as fh:
                    idx_num = 0
                    while True:
                        start_off = fh.tell()
                        raw_line = fh.readline()
                        if not raw_line:
                            break
                        end_off = fh.tell()
                        if not raw_line.strip():
                            continue
                        try:
                            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except Exception:
                            idx_num += 1
                            continue
                        sample_id = str(
                            obj.get("id")
                            if obj.get("id") is not None
                            else (obj.get("index") if obj.get("index") is not None else idx_num)
                        )
                        mm = self.media_matcher.match_sample(benchmark_dir_name, obj)
                        rew = None
                        for rk in ("score", "reward", "correct", "hit", "judge_hit", "judge_score", "judge_res"):
                            if rk in obj and obj[rk] is not None:
                                if isinstance(obj[rk], bool):
                                    rew = 1.0 if obj[rk] else 0.0
                                elif finite(obj[rk]):
                                    rew = float(obj[rk])
                                break
                        offsets.append(
                            (
                                cache_key,
                                sample_id,
                                start_off,
                                end_off,
                                "scored" if rew is not None else "recorded",
                                rew,
                                obj.get("category") or obj.get("subject") or obj.get("subdomain"),
                                obj.get("difficulty") or obj.get("level"),
                                mm.status,
                                json.dumps(mm.refs, ensure_ascii=False),
                                json.dumps({"benchmark": benchmark_dir_name, "model": obj.get("model") or jl.stem}, ensure_ascii=False),
                            )
                        )
                        idx_num += 1
            except Exception as exc:
                with self.db.transaction() as conn:
                    self._upsert_cache(conn, cache_key, "legacy_jsonl", st[1], st[2], 0, "error", str(exc))
                continue

            with self.db.transaction() as conn:
                self._upsert_cache(conn, cache_key, "legacy_jsonl", st[1], st[2], len(offsets))
                conn.execute("DELETE FROM legacy_sample_offsets WHERE file_path = ?", (cache_key,))
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO legacy_sample_offsets (
                        file_path, sample_id, start_offset, end_offset,
                        status, reward, category, difficulty,
                        media_match_status, media_refs_json, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    offsets,
                )
            indexed_files += 1
        return indexed_files

    def index_legacy_text_json_offsets(self, benchmark_dir_name: str, *, force: bool = False) -> int:
        """O(1)-memory streaming byte-offset scanner for pretty-printed 2026-11-20 text JSON dict files
        (IFEval, IFBench, GPQA_diamond, hle_llmjudge, AA-LCR-v1.1, Multi-LCB, Apex-shortlist, Multi-Challenge, LongMemEval x4)."""
        folder = self.settings.legacy_root_dir / benchmark_dir_name
        if not folder.is_dir():
            return 0
        cache_map = self._load_cache_map()
        indexed_files = 0
        try:
            with os.scandir(folder) as it:
                jf_entries = sorted(
                    (
                        (Path(e.path), e.stat(follow_symlinks=False))
                        for e in it
                        if e.is_file(follow_symlinks=False) and e.name.endswith(".json")
                    ),
                    key=lambda x: x[0].name,
                )
        except OSError:
            return 0
        for jf, st_obj in jf_entries:
            st = (st_obj.st_ino, st_obj.st_mtime_ns, st_obj.st_size)
            cache_key = str(jf)
            if not force and self._is_cached(cache_key, st[1], st[2], cache_map=cache_map):
                continue

            offsets: list[tuple[Any, ...]] = []
            try:
                with open(jf, "rb") as fh:
                    cur_key: str | None = None
                    cur_start: int | None = None
                    cur_reward: float | None = None
                    while True:
                        line_pos = fh.tell()
                        raw_line = fh.readline()
                        if not raw_line:
                            break
                        if cur_key is None:
                            m = _TOP_DICT_KEY_RE.match(raw_line)
                            if m:
                                cur_key = m.group(1).decode("utf-8", errors="replace")
                                # Start right at '{' so seek(start_offset..end_offset) is valid JSON object!
                                brace_idx = raw_line.find(b"{")
                                cur_start = line_pos + brace_idx
                                cur_reward = None
                        else:
                            # Opportunistic lightweight score capture from indented top fields
                            if cur_reward is None:
                                stripped = raw_line.strip()
                                if stripped.startswith((b'"correct":', b'"score":', b'"reward":')):
                                    if b"true" in stripped.lower():
                                        cur_reward = 1.0
                                    elif b"false" in stripped.lower():
                                        cur_reward = 0.0
                                    else:
                                        m_num = re.search(rb":\s*([0-9]+(?:\.[0-9]+)?)", stripped)
                                        if m_num:
                                            cur_reward = float(m_num.group(1))
                            if _TOP_DICT_END_RE.match(raw_line):
                                cur_end = fh.tell()
                                offsets.append(
                                    (
                                        cache_key,
                                        cur_key,
                                        cur_start or 0,
                                        cur_end,
                                        "scored" if cur_reward is not None else "recorded",
                                        cur_reward,
                                        None,
                                        None,
                                        "not_applicable",
                                        "[]",
                                        json.dumps({"benchmark": benchmark_dir_name, "model": jf.stem}, ensure_ascii=False),
                                    )
                                )
                                cur_key = None
                                cur_start = None
            except Exception as exc:
                with self.db.transaction() as conn:
                    self._upsert_cache(conn, cache_key, "legacy_json_dict", st[1], st[2], 0, "error", str(exc))
                continue

            with self.db.transaction() as conn:
                self._upsert_cache(conn, cache_key, "legacy_json_dict", st[1], st[2], len(offsets))
                conn.execute("DELETE FROM legacy_sample_offsets WHERE file_path = ?", (cache_key,))
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO legacy_sample_offsets (
                        file_path, sample_id, start_offset, end_offset,
                        status, reward, category, difficulty,
                        media_match_status, media_refs_json, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    offsets,
                )
            indexed_files += 1
        return indexed_files

    def index_bundles_metadata(self, *, force: bool = False, include_large_zst: bool = True) -> int:
        b_dir = self.settings.bundles_dir
        if not b_dir.is_dir():
            return 0
        count = 0
        for fname, task_releases in _TASK_BUNDLES.items():
            release_ids = tuple(task_releases.values())
            if fname.endswith(".zst") and not include_large_zst:
                continue
            fpath = b_dir / fname
            st = _stat_tuple(fpath)
            if st is None:
                continue
            cache_key = str(fpath)
            if not force:
                cached = self.db.fetchone(
                    """
                    SELECT cache_kind, item_count FROM cold_file_cache
                    WHERE file_path = ? AND mtime_ns = ? AND size_bytes = ? AND status = 'ready'
                    """,
                    (cache_key, st[1], st[2]),
                )
                if cached and (
                    cached["item_count"] == 0
                    or all(self.db.fetchone(
                        """SELECT 1 FROM dataset_bundle_task_content
                           WHERE dataset_release_id = ? AND archive_path = ? LIMIT 1""",
                        (rel_id, cache_key),
                    ) for rel_id in release_ids)
                ):
                    if cached["cache_kind"] != "bundle_tar":
                        with self.db.transaction() as conn:
                            conn.execute(
                                "UPDATE cold_file_cache SET cache_kind = 'bundle_tar' WHERE file_path = ?",
                                (cache_key,),
                            )
                    continue
            try:
                if fname.endswith(".zst"):
                    dctx = zstd.ZstdDecompressor()
                    with open(fpath, "rb") as raw_fh, dctx.stream_reader(raw_fh) as zr, tarfile.open(fileobj=zr, mode="r|") as tf:
                        tasks, content = self._read_bundle_tasks(tf, task_releases, fpath)
                else:
                    with tarfile.open(fpath, "r:") as tf:
                        tasks, content = self._read_bundle_tasks(tf, task_releases, fpath)
            except Exception as exc:
                with self.db.transaction() as conn:
                    self._upsert_cache(conn, cache_key, "bundle_tar", st[1], st[2], 0, "error", str(exc))
                continue

            with self.db.transaction() as conn:
                for rel_id in release_ids:
                    if not conn.execute(
                        "SELECT 1 FROM dataset_releases WHERE dataset_release_id = ?", (rel_id,)
                    ).fetchone():
                        continue
                    conn.executemany(
                        """
                        INSERT INTO task_definitions (
                            dataset_release_id, task_id, task_dir, source,
                            difficulty, category, language, tags_json, resources_json, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(dataset_release_id, task_id) DO UPDATE SET
                            difficulty = excluded.difficulty,
                            category = excluded.category,
                            tags_json = excluded.tags_json
                        """,
                        (row for row in tasks if row[0] == rel_id),
                    )
                    conn.execute("DELETE FROM dataset_bundle_task_content WHERE dataset_release_id = ?", (rel_id,))
                    conn.executemany(
                        """
                        INSERT INTO dataset_bundle_task_content
                            (dataset_release_id, task_id, archive_path, instruction, attachments_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (row for row in content if row[0] == rel_id),
                    )
                self._upsert_cache(conn, cache_key, "bundle_tar", st[1], st[2], len(tasks))
            count += len(tasks)
        return count

    def _read_bundle_tasks(
        self, archive: tarfile.TarFile, task_releases: dict[tuple[str, ...], str], path: Path
    ) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        tasks: dict[PurePosixPath, tuple[Any, ...]] = {}
        instructions: dict[PurePosixPath, str] = {}
        attachments: dict[PurePosixPath, dict[str, dict[str, str]]] = {}
        for member in archive:
            if not member.isfile():
                continue
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                continue
            if name.name in {"task.toml", "instruction.md"} and member.size <= _MAX_TASK_INSTRUCTION_BYTES:
                task_prefix = name.parts[:-2]
                release_id = task_releases.get(task_prefix) or task_releases.get(())
                if release_id is None:
                    continue
                with archive.extractfile(member) as stream:
                    text = stream.read().decode("utf-8", errors="replace")
                if name.name == "task.toml":
                    # Match the task IDs already published by each Harbor adapter.
                    id_prefix = _CORE_TASK_ID_PREFIXES.get(release_id)
                    task_id = None if id_prefix is None else f"{id_prefix}{name.parent.name}"
                    tasks[name.parent] = self._toml_to_row(release_id, member.name, tomllib.loads(text), task_id=task_id)
                else:
                    instructions[name.parent] = redact_text(text).strip()
            elif (
                task_releases.get(()) == "gaia__2026-11-24__val"
                and len(name.parts) == 5 and name.parts[0] == "gaia"
                and name.parts[2:4] == ("environment", "workspace")
                and name.stem == name.parts[1]
                and name.suffix.lower() in _GAIA_ATTACHMENT_EXTENSIONS
                and member.size <= MAX_TASK_ATTACHMENT_BYTES
            ):
                with archive.extractfile(member) as stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
                task_folder = name.parent.parent.parent
                attachments.setdefault(task_folder, {})[name.name] = {"member": member.name, "sha256": digest}
        # Keep one content row for each task ID, matching the upsert order of its metadata.
        content_by_id = {
            (row[0], row[1]): (row[0], row[1], str(path), instructions.get(folder), json.dumps(attachments.get(folder, {})))
            for folder, row in tasks.items()
        }
        return list(tasks.values()), list(content_by_id.values())

    @staticmethod
    def _toml_to_row(
        rel_id: str, member_name: str, parsed: dict[str, Any], *, task_id: str | None = None,
    ) -> tuple[Any, ...]:
        task_sec = parsed.get("task") or {}
        meta_sec = parsed.get("metadata") or {}
        tdir = Path(member_name).parent.name
        task_id = task_id or str(task_sec.get("name") or tdir)
        return (
            rel_id,
            task_id,
            tdir,
            meta_sec.get("source"),
            meta_sec.get("difficulty"),
            meta_sec.get("category"),
            meta_sec.get("language") or "en",
            json.dumps(meta_sec.get("tags") or [], ensure_ascii=False),
            json.dumps(parsed.get("environment") or {}, ensure_ascii=False),
            json.dumps(meta_sec, ensure_ascii=False),
        )

    def build_source_coverage_report(
        self, *, ttl_sec: float = 20.0, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Per-source coverage report covering every 197 root (indexed / partial_permission_denied / raw_visible_unintegrated / empty).

        Uses parallel `os.scandir()` (avoiding sequential per-entry CIFS stat round-trips) plus a short
        thread-safe TTL cache invalidated on every cold scan / admin reindex so `/api/v1/admin/health`
        stays well below the 250ms P95 latency budget even under 24T CIFS load.
        """
        now_mono = time.monotonic()
        if not force_refresh and ttl_sec > 0:
            with self._coverage_lock:
                if self._coverage_cache is not None and (now_mono - self._coverage_cache[0]) < ttl_sec:
                    return [dict(item) for item in self._coverage_cache[1]]

        cache_rows = {r["file_path"]: r for r in self.db.fetchall("SELECT * FROM cold_file_cache")}
        hr_indexed = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM batches WHERE source_namespace = 'harbor_runs'"
        )["c"]  # type: ignore[index]
        hr_perm_warn = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM index_warnings WHERE tier = 'hot' AND code = 'DIR_PERMISSION_DENIED'"
        )["c"]  # type: ignore[index]

        def _scandir_dirs(root: Path) -> list[str] | None:
            try:
                with os.scandir(root) as it:
                    return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                return None

        def _scandir_bundles(root: Path) -> list[tuple[str, str, int]] | None:
            try:
                out: list[tuple[str, str, int]] = []
                with os.scandir(root) as it:
                    for e in it:
                        if e.name.endswith((".tar", ".zst", ".tgz")):
                            try:
                                sz = e.stat(follow_symlinks=False).st_size
                            except OSError:
                                sz = 0
                            out.append((e.name, e.path, sz))
                out.sort(key=lambda x: x[0])
                return out
            except OSError:
                return None

        with ThreadPoolExecutor(max_workers=5) as pool:
            f_hr = pool.submit(_scandir_dirs, self.settings.harbor_runs_dir)
            f_leg = pool.submit(_scandir_dirs, self.settings.legacy_root_dir)
            f_bnd = pool.submit(_scandir_bundles, self.settings.bundles_dir)
            f_tr = pool.submit(_scandir_dirs, self.settings.training_runs_dir)
            f_raw = pool.submit(_scandir_dirs, self.settings.dataset_root)

            hr_dirs = f_hr.result()
            leg_dirs = f_leg.result()
            bundle_files = f_bnd.result()
            tr_entries = f_tr.result()
            raw_dirs = f_raw.result()

        report: list[dict[str, Any]] = []

        # 1. harbor-runs
        if hr_dirs is not None:
            report.append(
                {
                    "source": "harbor-runs",
                    "status": "partial_permission_denied" if hr_perm_warn > 0 else "indexed",
                    "discovered_dirs": len(hr_dirs),
                    "indexed_batches": hr_indexed,
                    "permission_gaps": hr_perm_warn,
                }
            )

        # 2. All 2026-11-20 subdirectories
        if leg_dirs is not None:
            leg_root_str = str(self.settings.legacy_root_dir)
            for dname in leg_dirs:
                prefix = f"{leg_root_str}/{dname}"
                matched = [r for k, r in cache_rows.items() if k == prefix or k.startswith(prefix + "/")]
                ready = [r for r in matched if r["status"] in ("ready", "partial_permission_denied")]
                denied = [r for r in matched if r["status"] in ("permission_denied", "partial_permission_denied")]
                errors = [r for r in matched if r["status"] == "error"]
                items_total = sum(int(r["item_count"] or 0) for r in ready)
                if ready and denied:
                    st = "partial_permission_denied"
                elif denied:
                    st = "permission_denied"
                elif errors:
                    st = "error"
                elif ready:
                    st = "indexed"
                else:
                    st = "pending_cold_scan"
                report.append(
                    {
                        "source": f"2026-11-20/{dname}",
                        "status": st,
                        "cached_files": len(ready),
                        "error_files": len(errors) + len(denied),
                        "indexed_items": items_total,
                    }
                )

        # 3. bundles
        if bundle_files is not None:
            for bname, bpath, bsize in bundle_files:
                c = cache_rows.get(bpath)
                report.append(
                    {
                        "source": f"bundles/{bname}",
                        "status": c["status"]
                        if c
                        else (
                            "non_task_bundle"
                            if bname not in _TASK_BUNDLES and any(k in bname for k in ("opencode", "pi-openclaw", "images"))
                            else "pending_cold_scan"
                        ),
                        "size_mb": round(bsize / (1024**2), 1),
                        "indexed_items": int(c["item_count"]) if c else 0,
                    }
                )

        # 4. training-runs
        tr_count = len(tr_entries) if tr_entries is not None else 0
        report.append(
            {
                "source": "training-runs",
                "status": "indexed" if tr_count else "empty_contract_ready",
                "discovered_dirs": tr_count,
            }
        )

        # 5. raw /home/share-data/dataset unintegrated assets
        if raw_dirs is not None:
            registered_raw = set(self.media_matcher.allowed_sources.keys())
            for dname in raw_dirs:
                report.append(
                    {
                        "source": f"dataset/{dname}",
                        "status": "registered_media_source" if dname in registered_raw else "raw_visible_unintegrated",
                    }
                )

        with self._coverage_lock:
            self._coverage_cache = (time.monotonic(), [dict(item) for item in report])
        return report

    def run_cold_scan(self, *, force: bool = False, include_large_zst: bool = True) -> dict[str, Any]:
        """Full cold scan across ALL 26 directories in 2026-11-20/ + bundles (incl. 7.5 GiB tar.zst when enabled).
        Uses parallel CIFS stat probing against cold_file_cache so restart cache hits complete in < 50ms!"""
        t0 = time.perf_counter()
        self.invalidate_source_coverage_cache()
        tb = self.index_native_tb21(force=force)
        ds = self.index_legacy_agent_json("DeepSWE", force=force)
        gaia = self.index_legacy_agent_json("GAIA", force=force)
        wc = self.index_legacy_wildclawbench(force=force)

        jsonl_files = 0
        text_json_files = 0
        leg_root = self.settings.legacy_root_dir
        if leg_root.is_dir():
            cache_map = {
                r["file_path"]: (r["mtime_ns"], r["size_bytes"], r["status"])
                for r in self.db.fetchall("SELECT file_path, mtime_ns, size_bytes, status FROM cold_file_cache")
            }
            other_dirs = [
                p
                for p in sorted(leg_root.iterdir())
                if p.is_dir() and p.name not in ("terminal-bench2.1", "DeepSWE", "GAIA", "wildclawbench")
            ]

            def _probe_other_dir(d: Path) -> tuple[str, str | None, bool]:
                has_jl = False
                has_j = False
                dirty = force
                try:
                    with os.scandir(d) as it:
                        for entry in it:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            if entry.name.endswith(".jsonl"):
                                has_jl = True
                            elif entry.name.endswith(".json"):
                                has_j = True
                            else:
                                continue
                            if not dirty:
                                st = entry.stat(follow_symlinks=False)
                                c = cache_map.get(entry.path)
                                if not c or c[2] != "ready" or c[0] != st.st_mtime_ns or c[1] != st.st_size:
                                    dirty = True
                except OSError:
                    return d.name, None, False
                kind = "jsonl" if has_jl else ("json" if has_j else None)
                return d.name, kind, dirty

            with ThreadPoolExecutor(max_workers=16) as pool:
                probed = list(pool.map(_probe_other_dir, other_dirs))

            for dname, kind, dirty in probed:
                if not dirty or kind is None:
                    continue
                if kind == "jsonl":
                    jsonl_files += self.index_legacy_jsonl_offsets(dname, force=force)
                else:
                    text_json_files += self.index_legacy_text_json_offsets(dname, force=force)

        bundle_tasks = self.index_bundles_metadata(force=force, include_large_zst=include_large_zst)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "elapsed_ms": elapsed_ms,
            "tb21": tb,
            "deepswe": ds,
            "gaia": gaia,
            "wildclawbench": wc,
            "multimodal_jsonl_files": jsonl_files,
            "text_json_files": text_json_files,
            "bundle_tasks": bundle_tasks,
        }
