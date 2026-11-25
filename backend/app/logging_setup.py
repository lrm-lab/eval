from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, BinaryIO

# Hard combined daemon log ceiling across backend (4 x 15 MiB = 60 MiB),
# HTTPS proxy (4 x 15 MiB = 60 MiB), and isolated runner (2 x 15 MiB = 30 MiB)
# => EXACTLY 10 x 15 MiB = 150 MiB daemon rotating log ceiling (v2.2.1 06-1 §4.2 / S-05)!
MAX_LOG_BYTES_PER_FILE = 15 * 1024 * 1024
COMPONENT_BACKUP_COUNTS: dict[str, int] = {
    "backend": 3,  # active + 3 backups = 4 files = 60 MiB max
    "proxy": 3,    # active + 3 backups = 4 files = 60 MiB max
    "runner": 1,   # active + 1 backup  = 2 files = 30 MiB max
}

# Hard per-job and cumulative retention ceilings for `runner-work/<job_id>/runner_stdio.log*` (S-05):
# - Per job: active 1 MiB + 1 rotated backup 1 MiB = 2 MiB hard ceiling per job!
# - Cumulative across ALL jobs under runner-work/: 20 MiB hard ceiling (oldest finished jobs pruned first).
# - Job detail API read: strictly bounded 128 KiB tail read (NEVER reads entire file into memory!).
# => Combined daemon (150 MiB) + cumulative job stdio (20 MiB) = 170 MiB total hard log ceiling!
MAX_JOB_STDIO_SEGMENT_BYTES = 1 * 1024 * 1024  # 1 MiB per file segment
MAX_JOB_STDIO_BACKUP_COUNT = 1                 # active + .1 backup = 2 MiB max per job
MAX_JOB_STDIO_BYTES = MAX_JOB_STDIO_SEGMENT_BYTES * (1 + MAX_JOB_STDIO_BACKUP_COUNT)  # 2 MiB
MAX_TOTAL_JOB_STDIO_BYTES = 20 * 1024 * 1024   # 20 MiB cumulative across all runner-work jobs
MAX_JOB_DETAIL_STDIO_READ_BYTES = 128 * 1024   # 128 KiB bounded tail read in get_job_detail()


def ensure_logs_dir(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(logs_dir, 0o700)
    except OSError:
        pass
    return logs_dir


def build_uvicorn_log_config(
    logs_dir: Path,
    component: str,
    *,
    max_bytes: int = MAX_LOG_BYTES_PER_FILE,
    backup_count: int | None = None,
    emit_console: bool = False,
) -> dict[str, Any]:
    """Return a strict Uvicorn dictConfig using RotatingFileHandler under 0700 state/logs/
    so deployed backend / HTTPS proxy / runner logs NEVER grow unbounded (S-05)."""
    ensure_logs_dir(logs_dir)
    bc = COMPONENT_BACKUP_COUNTS.get(component, 2) if backup_count is None else int(backup_count)
    log_file = logs_dir / f"{component}.log"
    handlers: dict[str, Any] = {
        "rotating_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "default",
            "filename": str(log_file),
            "maxBytes": int(max_bytes),
            "backupCount": bc,
            "encoding": "utf-8",
        }
    }
    handler_names = ["rotating_file"]
    if emit_console:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
        }
        handler_names.append("console")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            }
        },
        "handlers": handlers,
        "loggers": {
            "": {"handlers": handler_names, "level": "INFO"},
            "uvicorn": {"handlers": handler_names, "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": handler_names, "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": handler_names, "level": "INFO", "propagate": False},
        },
    }


def configure_component_rotating_logger(
    logs_dir: Path,
    component: str,
    *,
    max_bytes: int = MAX_LOG_BYTES_PER_FILE,
    backup_count: int | None = None,
) -> tuple[logging.Logger, Path]:
    ensure_logs_dir(logs_dir)
    bc = COMPONENT_BACKUP_COUNTS.get(component, 2) if backup_count is None else int(backup_count)
    log_file = logs_dir / f"{component}.log"
    logger = logging.getLogger(f"lrm_eval.{component}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fh = RotatingFileHandler(str(log_file), maxBytes=int(max_bytes), backupCount=bc, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(fh)
    try:
        if log_file.exists():
            os.chmod(log_file, 0o600)
    except OSError:
        pass
    return logger, log_file


class BoundedJobStdioWriter:
    """Strictly size-bounded, 0600 rotating binary sink for per-job `runner_stdio.log` (S-05).

    Enforces `<= max_segment_bytes * (1 + backup_count)` (default `2 x 1 MiB = 2 MiB`) even if
    a runaway child process floods gigabytes to stdout/stderr. Never parses child output."""

    def __init__(
        self,
        log_path: Path,
        *,
        max_segment_bytes: int = MAX_JOB_STDIO_SEGMENT_BYTES,
        backup_count: int = MAX_JOB_STDIO_BACKUP_COUNT,
    ) -> None:
        self.log_path = log_path
        self.max_segment_bytes = max(1024, int(max_segment_bytes))
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()
        self.total_written_bytes = 0
        self.rotated_count = 0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None
        self._cur_size = 0
        self._open_active()

    def _open_active(self) -> None:
        fd = os.open(str(self.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        st = os.fstat(fd)
        self._fd = fd
        self._cur_size = int(st.st_size)

    def _rotate_locked(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        if self.backup_count == 0:
            try:
                self.log_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            for idx in range(self.backup_count, 0, -1):
                src = self.log_path if idx == 1 else self.log_path.with_name(f"{self.log_path.name}.{idx - 1}")
                dst = self.log_path.with_name(f"{self.log_path.name}.{idx}")
                if src.exists() and not src.is_symlink():
                    try:
                        os.replace(src, dst)
                        os.chmod(dst, 0o600)
                    except OSError:
                        pass
        self.rotated_count += 1
        self._open_active()

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            mv = memoryview(data)
            pos = 0
            n = len(mv)
            while pos < n:
                if self._fd is None:
                    self._open_active()
                if self._cur_size >= self.max_segment_bytes:
                    self._rotate_locked()
                room = self.max_segment_bytes - self._cur_size
                chunk = mv[pos : pos + room]
                written = os.write(self._fd, chunk)  # type: ignore[arg-type]
                if written <= 0:
                    break
                pos += written
                self._cur_size += written
                self.total_written_bytes += written

    def drain_stream(self, stream: BinaryIO, *, chunk_size: int = 16384) -> None:
        try:
            while True:
                buf = stream.read(chunk_size)
                if not buf:
                    break
                self.write(buf)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None


def enforce_job_stdio_retention_cap(
    runner_work_dir: Path,
    *,
    max_total_bytes: int = MAX_TOTAL_JOB_STDIO_BYTES,
    active_job_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Enforce hard cumulative retention ceiling across ALL `runner-work/*/runner_stdio.log*` files (S-05).

    Active jobs are spared; oldest finished job stdio logs (sorted by mtime ascending) are deleted
    first until cumulative size across `runner_work_dir` is strictly `<= max_total_bytes`."""
    if not runner_work_dir.is_dir():
        return {"total_bytes_after": 0, "pruned_files": 0, "pruned_bytes": 0}

    active = active_job_ids or set()
    entries: list[tuple[float, str, Path, int]] = []
    total = 0
    try:
        for job_entry in os.scandir(runner_work_dir):
            if not job_entry.is_dir(follow_symlinks=False):
                continue
            jid = job_entry.name
            try:
                for f_entry in os.scandir(job_entry.path):
                    if not f_entry.name.startswith("runner_stdio.log") or not f_entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        st = f_entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += int(st.st_size)
                    entries.append((float(st.st_mtime), jid, Path(f_entry.path), int(st.st_size)))
            except OSError:
                continue
    except OSError:
        return {"total_bytes_after": 0, "pruned_files": 0, "pruned_bytes": 0}

    if total <= max_total_bytes:
        return {"total_bytes_after": total, "pruned_files": 0, "pruned_bytes": 0}

    # Prune oldest non-active job stdio files first (backups before active file for same mtime)
    pruned_files = 0
    pruned_bytes = 0
    candidates = [e for e in entries if e[1] not in active]
    candidates.sort(key=lambda item: (item[0], 0 if item[2].name != "runner_stdio.log" else 1))
    for _, _, fpath, sz in candidates:
        if total <= max_total_bytes:
            break
        try:
            fpath.unlink(missing_ok=True)
            total -= sz
            pruned_files += 1
            pruned_bytes += sz
        except OSError:
            continue

    return {
        "total_bytes_after": max(0, total),
        "pruned_files": pruned_files,
        "pruned_bytes": pruned_bytes,
    }


def read_bounded_tail_lines(
    log_path: Path,
    *,
    max_read_bytes: int = MAX_JOB_DETAIL_STDIO_READ_BYTES,
    max_lines: int = 200,
) -> tuple[list[str], bool]:
    """Read at most `max_read_bytes` from the tail of `runner_stdio.log` (and if active segment is small,
    optionally prepend tail of `runner_stdio.log.1`), NEVER loading an entire unbounded file into memory!"""
    if log_path.is_symlink() or not log_path.is_file():
        return [], False

    budget = max(4096, int(max_read_bytes))
    truncated = False
    chunks: list[bytes] = []
    remaining = budget

    # Read from active segment first (tail), then optional .1 backup if room remains
    backup_path = log_path.with_name(f"{log_path.name}.1")
    read_order: list[Path] = [log_path]
    if backup_path.is_file() and not backup_path.is_symlink():
        read_order.append(backup_path)

    for idx, fp in enumerate(read_order):
        if remaining <= 0:
            truncated = True
            break
        try:
            sz = fp.stat().st_size
            if sz <= 0:
                continue
            with fp.open("rb") as fh:
                if sz > remaining:
                    fh.seek(sz - remaining)
                    data = fh.read(remaining)
                    truncated = True
                    # Drop partial leading line after mid-file seek
                    nl = data.find(b"\n")
                    if nl != -1:
                        data = data[nl + 1 :]
                else:
                    data = fh.read(sz)
                remaining -= len(data)
                chunks.append(data)
                if idx == 0 and sz >= budget and backup_path.is_file():
                    truncated = True
                    break
        except OSError:
            continue

    if not chunks:
        return [], truncated
    raw_bytes = b"".join(reversed(chunks))
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        truncated = True
        lines = lines[-max_lines:]
    return lines, truncated
