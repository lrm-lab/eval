"""Read optional OpenAI SSE measurements, isolated cgroup v2 counters, and storage status."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.config import Settings
from app.logging_setup import (
    COMPONENT_BACKUP_COUNTS,
    MAX_JOB_DETAIL_STDIO_READ_BYTES,
    MAX_JOB_STDIO_BACKUP_COUNT,
    MAX_JOB_STDIO_BYTES,
    MAX_JOB_STDIO_SEGMENT_BYTES,
    MAX_LOG_BYTES_PER_FILE,
    MAX_TOTAL_JOB_STDIO_BYTES,
)

STREAM_SAMPLES_FILENAME = "stream_telemetry.ndjson"
MAX_STREAM_SAMPLES_FILE_BYTES = 256 * 1024
MAX_BACKUP_BYTES = 45 * 1000 * 1000
JOB_STDIO_FILENAME = "runner_stdio.log"

UNAVAILABLE_STREAM: dict[str, Any] = {"source": "unavailable", "ttft_ms": None, "tps": None}

_CONTAINER_SCOPE = re.compile(r"(?:docker|cri-containerd|crio|libpod)-[0-9a-f]{12,64}\.scope")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
_ISOLATED_SCOPES = ("container", "container_namespace_root")


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite_nonneg(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def unavailable_stream(note: str) -> dict[str, Any]:
    return {**UNAVAILABLE_STREAM, "sample_count": 0, "note": note}


# ----------------------------------------------------------------------------
# Streaming TTFT / TPS (OpenAI-compatible `text/event-stream` chat completions)
# ----------------------------------------------------------------------------
class OpenAIStreamMeter:
    """Measure one real OpenAI SSE response. Feed raw lines as they arrive off the wire.

    - TTFT = first non-empty `delta.content` / `delta.reasoning_content` arrival - request start.
    - TPS  = (output_tokens - 1) / (last_token_at - first_token_at), i.e. decode throughput after the
      first token. A numeric TPS requires the gateway's `usage.completion_tokens` and distinct
      first and last output arrival times. SSE chunks can contain any number of tokens.
    - A non-streaming JSON body or a stream with no token deltas yields `source="unavailable"`."""

    def __init__(self, started_at: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.started_at = clock() if started_at is None else float(started_at)
        self.first_token_at: float | None = None
        self.last_token_at: float | None = None
        self.usage_completion_tokens: int | None = None
        self.finish_reason: str | None = None
        self.saw_sse = False
        self.done = False

    def feed_line(self, line: str | bytes, at: float | None = None) -> None:
        if self.done:
            return
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line.startswith("data:"):
            return
        self.saw_sse = True
        payload = line[5:].strip()
        if payload == "[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            return
        if not isinstance(chunk, dict):
            return
        now = self._clock() if at is None else float(at)
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            ct = _nonneg_int(usage.get("completion_tokens"))
            if ct is not None:
                self.usage_completion_tokens = ct
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not math.isfinite(now) or now < self.started_at:
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and any(
                isinstance(delta.get(k), str) and delta.get(k) for k in ("content", "reasoning_content", "reasoning")
            ):
                if self.first_token_at is None:
                    self.first_token_at = now
                if self.last_token_at is None or now > self.last_token_at:
                    self.last_token_at = now
            if isinstance(choice.get("finish_reason"), str):
                self.finish_reason = choice["finish_reason"]

    def feed_text(self, raw: str, at: float | None = None) -> None:
        for ln in raw.splitlines():
            self.feed_line(ln, at)

    def result(self) -> dict[str, Any]:
        if not self.saw_sse:
            return unavailable_stream("TTFT/TPS 需要 SSE 流式响应")
        if self.first_token_at is None or self.last_token_at is None:
            return unavailable_stream("SSE 流中没有可计时的输出")
        ttft_ms = (self.first_token_at - self.started_at) * 1000.0
        tokens = self.usage_completion_tokens
        window = self.last_token_at - self.first_token_at
        token_count = _finite_nonneg(tokens)
        tps = (token_count - 1) / window if token_count is not None and token_count >= 2 and window > 0 else None
        return {
            "source": "openai_sse_stream",
            "ttft_ms": round(ttft_ms, 2),
            "tps": round(tps, 2) if tps is not None and math.isfinite(tps) else None,
            "output_tokens": tokens,
            "decode_seconds": round(window, 4),
            "finish_reason": self.finish_reason,
            "sample_count": 1,
        }


def measure_sse_lines(timed_lines: Iterable[tuple[float, str | bytes]], started_at: float) -> dict[str, Any]:
    """Measure an already-captured stream given `(arrival_monotonic_ts, raw_line)` pairs."""
    meter = OpenAIStreamMeter(started_at=started_at)
    for at, ln in timed_lines:
        meter.feed_line(ln, at)
    return meter.result()


def append_stream_sample(job_dir: Path, sample: dict[str, Any]) -> bool:
    """Save measured metrics to the private job directory without recording response content."""
    ttft = _finite_nonneg(sample.get("ttft_ms"))
    if sample.get("source") != "openai_sse_stream" or ttft is None or job_dir.is_symlink():
        return False
    line = (json.dumps({"ttft_ms": ttft, "tps": _finite_nonneg(sample.get("tps"))}) + "\n").encode()
    try:
        fd = os.open(
            job_dir / STREAM_SAMPLES_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(fd, "wb", buffering=0) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            fcntl.flock(stream, fcntl.LOCK_EX)
            if os.fstat(stream.fileno()).st_size + len(line) > MAX_STREAM_SAMPLES_FILE_BYTES:
                return False
            return stream.write(line) == len(line)
    except OSError:
        return False


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[idx]


def summarize_stream_samples(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ttfts: list[float] = []
    tpss: list[float] = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        t = _finite_nonneg(s.get("ttft_ms"))
        if t is None:
            continue
        ttfts.append(t)
        p = _finite_nonneg(s.get("tps"))
        if p is not None:
            tpss.append(p)
    if not ttfts:
        return unavailable_stream("本作业暂无 SSE 采样记录")
    return {
        "source": "openai_sse_stream",
        "ttft_ms": round(statistics.median(ttfts), 2),
        "ttft_p95_ms": round(_percentile(ttfts, 95), 2),
        "tps": round(statistics.median(tpss), 2) if tpss else None,
        "sample_count": len(ttfts),
        "tps_sample_count": len(tpss),
        "aggregation": "median",
    }


def load_job_stream_telemetry(job_dir: Path) -> dict[str, Any]:
    if job_dir.is_symlink():
        return summarize_stream_samples([])
    samples: list[dict[str, Any]] = []
    try:
        fd = os.open(job_dir / STREAM_SAMPLES_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return summarize_stream_samples([])
            raw = stream.read(MAX_STREAM_SAMPLES_FILE_BYTES)
    except OSError:
        return summarize_stream_samples([])
    for ln in raw.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            samples.append(obj)
    return summarize_stream_samples(samples)


# ----------------------------------------------------------------------------
# Linux cgroup v2 container telemetry
# ----------------------------------------------------------------------------
def _read_int_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _classify_scope(rel: str, cgroup_root: Path, dockerenv: Path, containerenv: Path) -> str:
    if not rel:
        has_runtime_marker = dockerenv.is_file() or containerenv.is_file()
        has_group_limit = (cgroup_root / "memory.max").is_file() or (cgroup_root / "cpu.max").is_file()
        return "container_namespace_root" if has_runtime_marker and has_group_limit else "host_root"
    parts = rel.lower().split("/")
    if any(_CONTAINER_SCOPE.fullmatch(part) for part in parts):
        return "container"
    if any(
        parent in ("docker", "kubepods") and _CONTAINER_ID.fullmatch(parts[-1])
        for parent in parts[:-1]
    ):
        return "container"
    if parts[0] == "user.slice" or parts[-1].endswith(".scope"):
        return "host_session"
    return "host_service"


def read_cgroup_v2_telemetry(
    proc_self_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    dockerenv: Path = Path("/.dockerenv"),
    containerenv: Path = Path("/run/.containerenv"),
) -> dict[str, Any]:
    """Read cgroup v2 metrics for the current process ONLY when it runs in an isolated container scope.
    Host sessions/services (shared user.slice / system.slice) return `source="unavailable"`."""
    base = normalize_cgroup_telemetry(None)
    try:
        lines = proc_self_cgroup.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {**base, "note": "无法读取 /proc/self/cgroup"}
    unified = next((ln.split("::", 1)[1].strip() for ln in lines if ln.startswith("0::")), None)
    if unified is None:
        return {**base, "note": "缺少 cgroup v2 统一层级条目"}
    rel = unified.strip("/")
    if any(part == ".." for part in rel.split("/")):
        return {**base, "note": "cgroup 路径无效"}
    scope = _classify_scope(rel, cgroup_root, dockerenv, containerenv)
    base["scope"] = scope
    if scope not in _ISOLATED_SCOPES:
        return {**base, "note": f"容器遥测需要隔离的 cgroup v2 作用域（当前为 {scope}）"}

    cg = cgroup_root / rel if rel else cgroup_root
    cpu_usec: int | None = None
    try:
        for cl in (cg / "cpu.stat").read_text(encoding="utf-8").splitlines():
            if cl.startswith("usage_usec "):
                val = cl.split()[1]
                cpu_usec = int(val) if val.isdigit() else None
                break
    except (OSError, IndexError):
        pass
    metrics = {
        "cpu_usage_usec": cpu_usec,
        "memory_peak_bytes": _read_int_file(cg / "memory.peak"),
        "memory_current_bytes": _read_int_file(cg / "memory.current"),
        "memory_max_bytes": _read_int_file(cg / "memory.max"),  # literal "max" (unlimited) -> None
        "pids_current": _read_int_file(cg / "pids.current"),
    }
    return normalize_cgroup_telemetry({**metrics, "source": "cgroup_v2", "scope": scope})


def normalize_cgroup_telemetry(raw: Any) -> dict[str, Any]:
    """Coerce a cgroup telemetry dict (e.g. reported by the isolated Runner over IPC) to the canonical schema."""
    out: dict[str, Any] = {
        "source": "unavailable",
        "scope": "unknown",
        "cpu_usage_usec": None,
        "memory_peak_bytes": None,
        "memory_current_bytes": None,
        "memory_max_bytes": None,
        "pids_current": None,
    }
    if not isinstance(raw, dict):
        return {**out, "note": "未上报 cgroup 遥测"}
    for key in ("cpu_usage_usec", "memory_peak_bytes", "memory_current_bytes", "memory_max_bytes", "pids_current"):
        out[key] = _nonneg_int(raw.get(key))
    if isinstance(raw.get("scope"), str):
        out["scope"] = raw["scope"]
    if isinstance(raw.get("note"), str):
        out["note"] = raw["note"]
    measured = any(out[k] is not None for k in ("cpu_usage_usec", "memory_peak_bytes", "memory_current_bytes"))
    if raw.get("source") == "cgroup_v2" and out["scope"] in _ISOLATED_SCOPES and measured:
        out["source"] = "cgroup_v2"
    else:
        out.update({k: None for k in ("cpu_usage_usec", "memory_peak_bytes", "memory_current_bytes", "memory_max_bytes", "pids_current")})
        out.setdefault("note", "缺少隔离容器 cgroup v2 指标")
    return out


# ----------------------------------------------------------------------------
# Read-only ops status: stdio caps, daemon log rotation, single .db.zst backup
# ----------------------------------------------------------------------------
def _file_size(path: Path) -> int | None:
    try:
        st = path.lstat()
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def stdio_policy() -> dict[str, int]:
    return {
        "per_job_cap_bytes": MAX_JOB_STDIO_BYTES,
        "segment_bytes": MAX_JOB_STDIO_SEGMENT_BYTES,
        "backup_segments": MAX_JOB_STDIO_BACKUP_COUNT,
        "cumulative_cap_bytes": MAX_TOTAL_JOB_STDIO_BYTES,
        "detail_read_bytes": MAX_JOB_DETAIL_STDIO_READ_BYTES,
    }


def job_stdio_status(job_dir: Path) -> dict[str, Any]:
    segments = []
    if not job_dir.is_symlink():
        segments = [_file_size(job_dir / JOB_STDIO_FILENAME)]
        segments.extend(_file_size(job_dir / f"{JOB_STDIO_FILENAME}.{n}") for n in range(1, MAX_JOB_STDIO_BACKUP_COUNT + 1))
    present = [size for size in segments if size is not None]
    return {
        "present": bool(present),
        "total_bytes": sum(present),
        "segments": len(present),
        "rotated": any(size is not None for size in segments[1:]),
        **stdio_policy(),
    }


def runner_work_stdio_usage(runner_work_dir: Path) -> dict[str, Any]:
    total = 0
    jobs = 0
    readable = True
    try:
        with os.scandir(runner_work_dir) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(entry.path) as files:
                        job_bytes = sum(
                            file.stat(follow_symlinks=False).st_size for file in files
                            if file.name.startswith(JOB_STDIO_FILENAME) and file.is_file(follow_symlinks=False)
                        )
                except FileNotFoundError:
                    # Retention can unlink a segment while a snapshot is being collected.
                    continue
                if job_bytes:
                    jobs += 1
                    total += job_bytes
    except FileNotFoundError:
        pass
    except OSError:
        readable = False
    return {
        "total_bytes": total if readable else None,
        "jobs_with_stdio": jobs if readable else None,
        "cap_bytes": MAX_TOTAL_JOB_STDIO_BYTES,
        "within_cap": total <= MAX_TOTAL_JOB_STDIO_BYTES if readable else None,
    }


def daemon_log_rotation_status(logs_dir: Path) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    total = 0
    cap_total = 0
    for comp, backups in COMPONENT_BACKUP_COUNTS.items():
        max_files = 1 + backups
        cap = max_files * MAX_LOG_BYTES_PER_FILE
        sizes = [_file_size(logs_dir / (f"{comp}.log" if i == 0 else f"{comp}.log.{i}")) for i in range(max_files)]
        cur = sum(s for s in sizes if s is not None)
        total += cur
        cap_total += cap
        components.append({
            "component": comp,
            "max_files": max_files,
            "max_bytes_per_file": MAX_LOG_BYTES_PER_FILE,
            "cap_bytes": cap,
            "current_bytes": cur,
            "files_present": sum(1 for s in sizes if s is not None),
        })
    return {
        "logs_dir_present": logs_dir.is_dir(),
        "components": components,
        "total_bytes": total,
        "cap_bytes": cap_total,
        "within_cap": total <= cap_total,
    }


def backup_status(state_dir: Path, now: float | None = None) -> dict[str, Any]:
    backups_dir = state_dir / "backups"
    found: list[tuple[float, str, int]] = []
    readable = True
    try:
        with os.scandir(backups_dir) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.name.endswith(".db.zst") or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                found.append((st.st_mtime, entry.name, st.st_size))
    except FileNotFoundError:
        pass
    except OSError:
        readable = False
    found.sort(reverse=True)
    res: dict[str, Any] = {
        "retained_count": len(found) if readable else None,
        "retention_limit": 1,
        "size_limit_bytes": MAX_BACKUP_BYTES,
        "latest_name": None,
        "latest_size_bytes": None,
        "latest_at": None,
        "age_hours": None,
    }
    if not readable:
        return {**res, "status": "unavailable", "note": "无法读取备份目录"}
    if not found:
        return {**res, "status": "missing", "note": "尚无 .db.zst 备份"}
    mtime, name, size = found[0]
    ref = time.time() if now is None else now
    res.update({
        "latest_name": name,
        "latest_size_bytes": size,
        "latest_at": utc_iso(mtime),
        "age_hours": round(max(0.0, ref - mtime) / 3600.0, 2),
    })
    if len(found) > 1:
        return {**res, "status": "multiple", "note": f"保留了 {len(found)} 份备份，超出单份上限"}
    if size > MAX_BACKUP_BYTES:
        return {**res, "status": "oversize", "note": "备份体积超出 45 MB 上限"}
    return {**res, "status": "ok"}


def execution_telemetry_overview(settings: Settings, *, include_ops: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "generated_at": utc_iso(time.time()),
        "web_cgroup": read_cgroup_v2_telemetry(),
        "streaming": {
            "mode": "optional_gateway_stream",
            "sample_file": STREAM_SAMPLES_FILENAME,
            "note": "TTFT/TPS 来自逐请求 SSE 采样。TPS 还需要网关返回输出 token 数。",
        },
        "stdio_policy": stdio_policy(),
        "ops": None,
    }
    if include_ops:
        out["ops"] = {
            "daemon_logs": daemon_log_rotation_status(Path(settings.state_dir) / "logs"),
            "job_stdio_usage": runner_work_stdio_usage(Path(settings.runner_work_dir)),
            "backup": backup_status(Path(settings.state_dir)),
        }
    return out


def job_telemetry(settings: Settings, job_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    assert settings.runner_work_dir is not None
    job_dir = settings.runner_work_dir / job_id
    runner_sample = next(
        (event["payload"].get("cgroup_telemetry") for event in reversed(events) if event["event_type"] == "job_started"),
        None,
    )
    return {
        "cgroup": normalize_cgroup_telemetry(runner_sample),
        "streaming_ttft_tps": load_job_stream_telemetry(job_dir),
        "stdio": job_stdio_status(job_dir),
    }
