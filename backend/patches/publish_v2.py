"""Upstream `framework/run.py` Common Finalizer v2 Patch Module (v2.2.1 · 05-1 §2, I-01, S-06).

Provides drop-in helpers for `run.py:publish()` and all four runner finalizers
(Harbor, Harbor Gateway, Direct, VLMEvalKit) to enforce:
1. Immutable `generations/<generation_id>/` layout for all published per-trial DTOs.
2. Strict separation of approved public DTOs vs restricted evidence:
   - Restricted evidence (`verifier-output.txt`, `trajectory.json`, `model-gateway.json`,
     `training_trace.json`) is written STRICTLY into a Fail-Closed `0700` POSIX private
     evidence store (`0600` files, NEVER `0644` under `0777` CIFS shared output!).
   - Approved public output holds ONLY `manifest.json`, redacted `batch-status.json`,
     `episodes.jsonl` (with opaque `evidence_refs`), `batch-report.json`, `run-config.json`,
     `trial-digests.jsonl`, `generations/<generation_id>/trials/<tid>/result.json`, and
     atomic `publish-manifest.json` (`schema_version: 2`).
3. Strict `<= 131072` bytes (128 KiB) final UTF-8 size budget for redacted verifier logs
   (head 32 KiB + marker + tail <= 131072 bytes total, valid UTF-8 boundaries guaranteed).
4. Re-entrant `.publish.lock` (`fcntl.flock(LOCK_EX)`) held continuously through
   `publish()` -> `batch-report.json` -> `trial-digests.jsonl` -> atomic `publish-manifest.json`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_VERIFIER_LOG_BYTES = 131072  # Hard 128 KiB ceiling (inclusive)
HEAD_TARGET_BYTES = 32768  # 32 KiB target head window
TRUNC_MARKER = b"\n... [truncated to 128 KiB: head 32 KiB + tail 96 KiB] ...\n"
UNSAFE_FSTYPES = frozenset({"cifs", "smb3", "smbfs", "vfat", "msdos", "ntfs", "fuseblk", "autofs"})

_REDACT_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted-token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-<redacted>"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?)([^\s'\",;}{]{6,})"), r"\1<redacted>"),
    (re.compile(r"/home/[A-Za-z0-9._-]+(?:/[^\s'\"<>)]*)?"), lambda m: "<redacted>/" + Path(m.group(0)).name),
    (re.compile(r"/var/tmp/[^\s'\"<>)]+"), lambda m: "<redacted>/" + Path(m.group(0)).name),
    (re.compile(r"/tmp/[^\s'\"<>)]+"), lambda m: "<redacted>/" + Path(m.group(0)).name),
    (re.compile(r"/root(?:/[^\s'\"<>)]*)?"), lambda m: "<redacted>/" + Path(m.group(0)).name),
]

STANDARD_TOP_LEVEL_FILES: tuple[str, ...] = (
    "manifest.json",
    "episodes.jsonl",
    "batch-status.json",
    "batch-report.json",
    "official-result.json",
    "vlmevalkit/metrics.json",
    "run-config.json",
    "selection.json",
    "trial-digests.jsonl",
)

MANDATORY_TOP_LEVEL_FILES: tuple[str, ...] = (
    "manifest.json",
    "episodes.jsonl",
    "batch-status.json",
    "batch-report.json",
    "trial-digests.jsonl",
)


def public_source_locator(source_host: str, job_dir: Path, batch_name: str) -> dict[str, str]:
    """Return a redacted public source locator (06-1 §3.1) containing ZERO raw absolute paths,
    while keeping a deterministic `job_ref` digest so checked `--resume` still verifies origin identity."""
    resolved = str(job_dir.resolve())
    return {
        "host": str(source_host),
        "path": f"<redacted>/jobs/{batch_name}",
        "job_ref": hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16],
    }


def record_private_source_locator(
    evidence_store_dir: Path,
    batch_name: str,
    source_host: str,
    job_dir: Path,
) -> Path:
    """Persist the raw private execution job path strictly inside 0700/0600 evidence_store for ops/resume."""
    store = ensure_private_evidence_store(evidence_store_dir)
    target = store / batch_name / ".private-source.json"
    resolved = str(job_dir.resolve())
    doc = {
        "batch": batch_name,
        "host": str(source_host),
        "job_path": resolved,
        "job_ref": hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16],
    }
    atomic_write_bytes(
        target,
        (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
        dir_mode=0o700,
    )
    return target


def redact_public_text(text: str | None) -> str | None:
    """Scrub secrets and host absolute paths from short public DTO strings (06-1 §2.1 / S-06)."""
    if text is None:
        return None
    out = str(text)
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_public_path(
    raw_path: str | Path | None,
    *,
    job_dir: Path | None = None,
    batch_name: str | None = None,
) -> str:
    """Redact any execution-host absolute path for shared public DTOs (06-1 §2.1 / §3.1, S-06).
    Guarantees ZERO raw working-directory absolute paths regardless of mount prefix."""
    raw_str = str(raw_path or "").strip()
    if not raw_str:
        return "<redacted>"
    if raw_str.startswith("<redacted>"):
        return raw_str
    p = Path(raw_str)
    if job_dir is not None:
        try:
            rel = p.resolve().relative_to(job_dir.resolve()).as_posix()
            prefix = f"<redacted>/jobs/{batch_name}" if batch_name else "<redacted>/jobs"
            return f"{prefix}/{rel}" if rel and rel != "." else prefix
        except (ValueError, OSError):
            pass
    parts = [seg for seg in p.parts if seg not in ("/", "")]
    for anchor in ("jobs", "harbor-runs", "trials", "verifier", "agent"):
        if anchor in parts:
            idx = parts.index(anchor)
            return "<redacted>/" + "/".join(parts[idx:])
    if len(parts) >= 2:
        return f"<redacted>/{parts[-2]}/{parts[-1]}"
    if parts:
        return f"<redacted>/{parts[-1]}"
    return "<redacted>"


def sanitize_public_failure(
    failure: dict[str, Any] | None,
    *,
    job_dir: Path | None = None,
    batch_name: str | None = None,
) -> dict[str, Any] | None:
    """Apply shared public DTO redaction to failure diagnostics and DispatchGuard pause objects
    (06-1 §2.1 / S-06), stripping all raw execution-host absolute paths while retaining
    deterministic `log_ref` digests and redacted relative tails."""
    if not isinstance(failure, dict):
        return None
    sanitized: dict[str, Any] = {}
    for k, v in failure.items():
        if k == "raw_log":
            if isinstance(v, dict):
                raw_p = v.get("path")
                red_log: dict[str, Any] = {}
                if v.get("host") is not None:
                    red_log["host"] = str(v["host"])
                if raw_p is not None:
                    raw_p_str = str(raw_p)
                    red_log["path"] = redact_public_path(raw_p_str, job_dir=job_dir, batch_name=batch_name)
                    try:
                        canon_p = str(Path(raw_p_str).resolve())
                    except OSError:
                        canon_p = raw_p_str
                    red_log["log_ref"] = hashlib.sha256(canon_p.encode("utf-8")).hexdigest()[:16]
                for extra_k, extra_v in v.items():
                    if extra_k not in ("host", "path", "log_ref"):
                        red_log[extra_k] = redact_public_text(extra_v) if isinstance(extra_v, str) else extra_v
                sanitized["raw_log"] = red_log
            elif isinstance(v, str):
                sanitized["raw_log"] = redact_public_path(v, job_dir=job_dir, batch_name=batch_name)
            else:
                sanitized["raw_log"] = v
        elif isinstance(v, str):
            sanitized[k] = redact_public_text(v)
        else:
            sanitized[k] = v
    return sanitized


def record_private_pause_state(
    evidence_store_dir: Path,
    batch_name: str,
    pause_doc: dict[str, Any],
) -> Path:
    """Persist the unredacted DispatchGuard pause state (including raw log path) strictly inside 0700/0600 evidence_store."""
    store = ensure_private_evidence_store(evidence_store_dir)
    target = store / batch_name / ".private-pause.json"
    atomic_write_bytes(
        target,
        (json.dumps(pause_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
        dir_mode=0o700,
    )
    return target

# Per-thread re-entrant lock depth per resolved batch directory so nested publish()+write_batch_report()
# holds ONE continuous flock(LOCK_EX) until publish-manifest.json is atomically committed!
_LOCK_STATE = threading.local()


def new_generation_id() -> str:
    return f"gen_{secrets.token_hex(8)}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_mount_fstype(target: Path, proc_mounts: str = "/proc/mounts") -> str:
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    resolved = str(probe.resolve())
    best_mp = ""
    best_fs = "unknown"
    try:
        with open(proc_mounts, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                fs = parts[2].lower()
                if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if len(mp) > len(best_mp) or (len(mp) == len(best_mp) and (fs != "autofs" or best_fs == "unknown")):
                        best_mp, best_fs = mp, fs
    except OSError:
        return "unknown"
    return best_fs


def ensure_private_evidence_store(evidence_store_dir: Path | None = None, *, job_dir: Path | None = None) -> Path:
    """Resolve and Fail-Closed verify the `0700` POSIX private evidence store (S-06).
    Resolution order:
    1. Explicit `evidence_store_dir` argument;
    2. `LRM_EVIDENCE_STORE_DIR` environment variable;
    3. Private sibling `<job_dir.parent>/.evidence-store` when `job_dir` resides on local POSIX FS.
    Refuses CIFS/vfat/autofs or any directory whose mode cannot enforce `0700`."""
    cand = evidence_store_dir
    if cand is None:
        env_dir = os.environ.get("LRM_EVIDENCE_STORE_DIR", "").strip()
        if env_dir:
            cand = Path(env_dir)
        elif job_dir is not None:
            cand = job_dir.resolve().parent / ".evidence-store"
        else:
            raise PermissionError("LRM_EVIDENCE_STORE_DIR or explicit evidence_store_dir is required for restricted evidence")

    fs = _get_mount_fstype(cand)
    if fs in UNSAFE_FSTYPES:
        raise PermissionError(f"Refusing to store restricted evidence on unsafe filesystem ({fs}): {cand}")

    cand.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(cand, 0o700)
    st = os.stat(cand)
    if (st.st_mode & 0o077) != 0:
        raise PermissionError(f"Evidence store failed 0700 POSIX check (mode={oct(st.st_mode & 0o777)}): {cand}")
    return cand.resolve()


@contextmanager
def publish_lock(output_dir: Path) -> Iterator[None]:
    """Re-entrant exclusive `.publish.lock` guard ensuring publish() + write_batch_report() +
    publish-manifest.json execute atomically inside ONE lock acquisition (05-1 §2.2)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    key = str(output_dir.resolve())
    held: dict[str, int] = getattr(_LOCK_STATE, "held", None)  # type: ignore[assignment]
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    if held.get(key, 0) > 0:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    lock_path = output_dir / ".publish.lock"
    with lock_path.open("a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held[key] -= 1
            if held[key] <= 0:
                held.pop(key, None)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644, dir_mode: int | None = None) -> None:
    if dir_mode is not None:
        # Create intermediate directories with strict private permissions
        missing: list[Path] = []
        cur = path.parent
        while not cur.exists() and cur != cur.parent:
            missing.append(cur)
            cur = cur.parent
        for d in reversed(missing):
            d.mkdir(mode=dir_mode, exist_ok=True)
            os.chmod(d, dir_mode)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            handle.close()
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def _utf8_prefix_bytes(text: str, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        return b""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    trimmed = raw[:max_bytes]
    # Back off any incomplete UTF-8 continuation bytes at the cut boundary
    while trimmed and (trimmed[-1] & 0xC0) == 0x80:
        trimmed = trimmed[:-1]
    if trimmed:
        try:
            trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return trimmed


def _utf8_suffix_bytes(text: str, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        return b""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    trimmed = raw[-max_bytes:]
    # Skip any leading UTF-8 continuation bytes so output decodes cleanly
    idx = 0
    while idx < len(trimmed) and (trimmed[idx] & 0xC0) == 0x80:
        idx += 1
    return trimmed[idx:]


def redact_and_truncate_verifier_output(raw: bytes) -> bytes:
    """Scrub paths/secrets AND enforce strict `<= 131072` bytes (128 KiB) final UTF-8 budget.
    Preserves head (up to ~32 KiB) + marker + tail (up to ~96 KiB) with `len(out) <= 131072` ALWAYS!"""
    text = raw.decode("utf-8", errors="replace")
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    scrubbed = text.encode("utf-8")
    if len(scrubbed) <= MAX_VERIFIER_LOG_BYTES:
        return scrubbed

    payload_budget = MAX_VERIFIER_LOG_BYTES - len(TRUNC_MARKER)
    head_budget = min(HEAD_TARGET_BYTES, payload_budget // 4)
    tail_budget = payload_budget - head_budget

    head_b = _utf8_prefix_bytes(text, head_budget)
    tail_b = _utf8_suffix_bytes(text, tail_budget)
    result = head_b + TRUNC_MARKER + tail_b
    # Mathematical safety guard: guarantee <= 131072 bytes strictly
    if len(result) > MAX_VERIFIER_LOG_BYTES:
        excess = len(result) - MAX_VERIFIER_LOG_BYTES
        tail_b = _utf8_suffix_bytes(text, max(0, tail_budget - excess))
        result = head_b + TRUNC_MARKER + tail_b
    return result


def extract_phase_timings(result: dict[str, Any]) -> dict[str, float | None] | None:
    """Extract 4-phase Harbor timings (environment_setup, agent_setup, agent_execution, verifier)."""
    out: dict[str, float | None] = {}
    any_present = False
    for out_key, src_key in (
        ("environment_setup_s", "environment_setup"),
        ("agent_setup_s", "agent_setup"),
        ("agent_execution_s", "agent_execution"),
        ("verifier_s", "verifier"),
    ):
        phase = result.get(src_key)
        val: float | None = None
        if isinstance(phase, dict) and phase.get("started_at") and phase.get("finished_at"):
            try:
                t0 = datetime.fromisoformat(str(phase["started_at"]).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(phase["finished_at"]).replace("Z", "+00:00"))
                sec = (t1 - t0).total_seconds()
                if sec >= 0:
                    val = round(sec, 3)
                    any_present = True
            except ValueError:
                val = None
        out[out_key] = val
    return out if any_present else None


def count_atif_tool_calls(trajectory_path: Path | None) -> int | None:
    if trajectory_path is None or not trajectory_path.is_file():
        return None
    try:
        doc = json.loads(trajectory_path.read_text(encoding="utf-8"))
        steps = doc.get("steps")
        if not isinstance(steps, list):
            return None
        return sum(len(s.get("tool_calls") or []) for s in steps if isinstance(s, dict) and isinstance(s.get("tool_calls"), list))
    except (OSError, ValueError, TypeError):
        return None


def publish_restricted_bytes(
    *,
    evidence_store_dir: Path,
    batch_name: str,
    generation_id: str,
    trial_id: str,
    filename: str,
    data: bytes,
) -> dict[str, Any]:
    """Write a restricted trial evidence blob strictly under `0700` evidence store with `0600` mode (S-06)."""
    store = ensure_private_evidence_store(evidence_store_dir)
    rel_path = f"{batch_name}/generations/{generation_id}/trials/{trial_id}/{filename}"
    target = store / rel_path
    atomic_write_bytes(target, data, mode=0o600, dir_mode=0o700)
    sha = _sha256_bytes(data)
    return {
        "evidence_id": f"ev_{sha[:16]}",
        "generation_id": generation_id,
        "rel_path": rel_path,
        "sha256": sha,
        "size": len(data),
        "storage_tier": "evidence_store_0700",
    }


def publish_verifier_output(
    trial_src_dir: Path,
    *,
    evidence_store_dir: Path,
    batch_name: str,
    generation_id: str,
    trial_id: str,
) -> dict[str, Any] | None:
    """Truncate (<= 128 KiB) + redact `verifier/test-stdout.txt` and write STRICTLY into `0700`/`0600` evidence store!"""
    for rel in ("verifier/test-stdout.txt", "verifier/stdout.txt", "verifier-output.txt"):
        cand = trial_src_dir / rel
        if cand.is_file():
            cleaned = redact_and_truncate_verifier_output(cand.read_bytes())
            assert len(cleaned) <= MAX_VERIFIER_LOG_BYTES
            return publish_restricted_bytes(
                evidence_store_dir=evidence_store_dir,
                batch_name=batch_name,
                generation_id=generation_id,
                trial_id=trial_id,
                filename="verifier-output.txt",
                data=cleaned,
            )
    return None


def stage_pending_generation(
    output_dir: Path,
    *,
    generation_id: str,
    evidence_store_dir: Path | None,
    trial_records: list[dict[str, Any]],
) -> None:
    """Persist staged generation metadata inside `.publish.lock` so `write_batch_report()` seals the exact same generation."""
    state = {
        "generation_id": generation_id,
        "evidence_store_dir": str(evidence_store_dir) if evidence_store_dir else None,
        "trials": trial_records,
    }
    atomic_write_bytes(
        output_dir / ".pending-generation.json",
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def write_trial_digests_and_manifest_v2(
    output_dir: Path,
    *,
    batch_name: str,
    generation_id: str | None = None,
    run_config: dict[str, Any] | None = None,
    evidence_store_dir: Path | None = None,
) -> dict[str, Any]:
    """Common Finalizer (v2.2.1 05-1 §2.2 & S-06) executed STRICTLY under `.publish.lock`:
    1. Resolves active `generation_id` (from staged `.pending-generation.json` or explicit arg) and
       verifies immutable `generations/<generation_id>/trials/` + `0700` `evidence_store_dir`.
    2. Writes sanitized `run-config.json` (if provided) and `trial-digests.jsonl`.
    3. Atomically commits `publish-manifest.json` (`schema_version: 2`) as the VERY LAST step inside the lock!"""
    with publish_lock(output_dir):
        pending_path = output_dir / ".pending-generation.json"
        pending_doc: dict[str, Any] = {}
        if pending_path.is_file():
            try:
                pending_doc = json.loads(pending_path.read_text(encoding="utf-8"))
            except Exception:
                pending_doc = {}

        gen_id = generation_id or pending_doc.get("generation_id") or new_generation_id()
        ev_store: Path | None = evidence_store_dir
        if ev_store is None and pending_doc.get("evidence_store_dir"):
            ev_store = Path(str(pending_doc["evidence_store_dir"]))
        elif ev_store is None and os.environ.get("LRM_EVIDENCE_STORE_DIR", "").strip():
            ev_store = Path(os.environ["LRM_EVIDENCE_STORE_DIR"].strip())

        if run_config is not None:
            atomic_write_bytes(
                output_dir / "run-config.json",
                (json.dumps(run_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

        # Build trial-digests.jsonl from immutable generations/<gen_id>/trials/ (or legacy trials/) + 0700 evidence_store
        digest_by_trial: dict[str, dict[str, Any]] = {}

        # Merge any pre-recorded restricted evidence refs from pending_doc
        for rec in pending_doc.get("trials") or []:
            if not isinstance(rec, dict) or not rec.get("trial_id"):
                continue
            tid = str(rec["trial_id"])
            entry = digest_by_trial.setdefault(tid, {"trial_id": tid, "generation_id": gen_id})
            for k in (
                "result",
                "trajectory",
                "gateway",
                "verifier_output",
                "training",
                "training_capture",
                "direct_qa",
                "failure_locator",
            ):
                if isinstance(rec.get(k), dict):
                    entry[k] = dict(rec[k])
            if isinstance(rec.get("media"), list) and rec["media"]:
                entry["media"] = [dict(m) for m in rec["media"] if isinstance(m, dict)]

        gen_trials_dir = output_dir / "generations" / gen_id / "trials"
        scan_root = gen_trials_dir if gen_trials_dir.is_dir() else (output_dir / "trials")
        rel_prefix = f"generations/{gen_id}/trials" if gen_trials_dir.is_dir() else "trials"

        if scan_root.is_dir():
            for tdir in sorted(p for p in scan_root.iterdir() if p.is_dir()):
                tid = tdir.name
                entry = digest_by_trial.setdefault(tid, {"trial_id": tid, "generation_id": gen_id})
                for kind, fname in (
                    ("result", "result.json"),
                    ("trajectory", "trajectory.json"),
                    ("gateway", "model-gateway.json"),
                    ("verifier_output", "verifier-output.txt"),
                    ("training", "training_trace.json"),
                    ("training_capture", "capture.json"),
                ):
                    fp = tdir / fname
                    if fp.is_file() and kind not in entry:
                        entry[kind] = {
                            "rel_path": f"{rel_prefix}/{tid}/{fname}",
                            "sha256": _sha256_file(fp),
                            "size": fp.stat().st_size,
                            "storage_tier": "shared_public_dto",
                        }

        # Also inspect 0700 evidence_store for restricted artifacts belonging to this exact generation
        if ev_store is not None:
            ev_gen_trials = ev_store / batch_name / "generations" / gen_id / "trials"
            if ev_gen_trials.is_dir():
                for tdir in sorted(p for p in ev_gen_trials.iterdir() if p.is_dir()):
                    tid = tdir.name
                    entry = digest_by_trial.setdefault(tid, {"trial_id": tid, "generation_id": gen_id})
                    for kind, fname in (
                        ("trajectory", "trajectory.json"),
                        ("gateway", "model-gateway.json"),
                        ("verifier_output", "verifier-output.txt"),
                        ("training", "training_trace.json"),
                        ("training_capture", "capture.json"),
                        ("direct_qa", "direct-qa.json"),
                        ("failure_locator", "failure-locator.json"),
                    ):
                        fp = tdir / fname
                        if fp.is_file() and kind not in entry:
                            sha = _sha256_file(fp)
                            entry[kind] = {
                                "evidence_id": f"ev_{sha[:16]}",
                                "generation_id": gen_id,
                                "rel_path": f"{batch_name}/generations/{gen_id}/trials/{tid}/{fname}",
                                "sha256": sha,
                                "size": fp.stat().st_size,
                                "storage_tier": "evidence_store_0700",
                            }
                    ev_media_dir = tdir / "media"
                    if ev_media_dir.is_dir() and "media" not in entry:
                        media_items = []
                        for m_idx, mf in enumerate(sorted(p for p in ev_media_dir.iterdir() if p.is_file())):
                            m_sha = _sha256_file(mf)
                            media_items.append(
                                {
                                    "evidence_id": f"ev_m_{m_sha[:16]}",
                                    "generation_id": gen_id,
                                    "source": "trial_media_dir",
                                    "inline_ordinal": m_idx,
                                    "rel_path": f"{batch_name}/generations/{gen_id}/trials/{tid}/media/{mf.name}",
                                    "sha256": m_sha,
                                    "size": mf.stat().st_size,
                                    "storage_tier": "evidence_store_0700",
                                }
                            )
                        if media_items:
                            entry["media"] = media_items

        digest_entries = [digest_by_trial[k] for k in sorted(digest_by_trial.keys()) if len(digest_by_trial[k]) > 2]
        td_bytes = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in digest_entries).encode("utf-8")
        atomic_write_bytes(output_dir / "trial-digests.jsonl", td_bytes)

        # Compute top_level_files + absent_optional_files[]
        top_level_files: dict[str, dict[str, Any]] = {}
        absent_optional: list[str] = []
        for rel_name in STANDARD_TOP_LEVEL_FILES:
            fp = output_dir / rel_name
            if fp.is_file():
                entry_m: dict[str, Any] = {"sha256": _sha256_file(fp), "size": fp.stat().st_size}
                if rel_name == "episodes.jsonl":
                    entry_m["lines"] = sum(
                        1 for ln in fp.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()
                    )
                elif rel_name == "trial-digests.jsonl":
                    entry_m["entries"] = len(digest_entries)
                top_level_files[rel_name] = entry_m
            else:
                absent_optional.append(rel_name)

        missing_mandatory = [req for req in MANDATORY_TOP_LEVEL_FILES if req not in top_level_files]
        if missing_mandatory:
            raise RuntimeError(
                f"Refusing to seal publish-manifest.json without mandatory top-level files: {', '.join(missing_mandatory)}"
            )

        manifest_v2 = {
            "schema_version": 2,
            "batch": batch_name,
            "generation_id": gen_id,
            "generation_root": f"generations/{gen_id}",
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "top_level_files": top_level_files,
            "absent_optional_files": absent_optional,
        }
        atomic_write_bytes(
            output_dir / "publish-manifest.json",
            (json.dumps(manifest_v2, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        pending_path.unlink(missing_ok=True)
        return manifest_v2
