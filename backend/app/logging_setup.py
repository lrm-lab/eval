from __future__ import annotations

import logging
import os
import stat
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


class ByteRotatingFileHandler(RotatingFileHandler):
    """Count encoded bytes; even a single oversized record must fit a segment."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record).encode("utf-8", errors="replace")
            if len(line) >= self.maxBytes:
                line = line[: self.maxBytes - 1].decode("utf-8", errors="ignore").encode("utf-8")
            if self.stream is None:
                self.stream = self._open()
            if self.stream.tell() + len(line) + 1 > self.maxBytes:
                self.doRollover()
            self.stream.write(line.decode("utf-8") + "\n")
            self.flush()
        except Exception:
            self.handleError(record)


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
            "class": f"{__name__}.ByteRotatingFileHandler",
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
    fh = ByteRotatingFileHandler(str(log_file), maxBytes=int(max_bytes), backupCount=bc, encoding="utf-8")
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
        self._dir = os.open(log_path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for idx in range(1, self.backup_count + 1):
                try:
                    st = os.stat(f"{log_path.name}.{idx}", dir_fd=self._dir, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_size > self.max_segment_bytes:
                    raise OSError("Existing rotated stdio segment exceeds quota")
            self._open_active()
        except BaseException:
            os.close(self._dir)
            raise

    def _open_active(self) -> None:
        fd = os.open(self.log_path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._dir)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError("Expected a private regular stdio file")
            if st.st_size > self.max_segment_bytes:
                raise OSError("Existing stdio segment exceeds quota")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
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
                os.unlink(self.log_path.name, dir_fd=self._dir)
            except FileNotFoundError:
                pass
        else:
            for idx in range(self.backup_count, 0, -1):
                src = self.log_path if idx == 1 else self.log_path.with_name(f"{self.log_path.name}.{idx - 1}")
                dst = self.log_path.with_name(f"{self.log_path.name}.{idx}")
                try:
                    os.replace(src.name, dst.name, src_dir_fd=self._dir, dst_dir_fd=self._dir)
                except FileNotFoundError:
                    if idx == 1:
                        raise
        self.rotated_count += 1
        self._open_active()

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._dir < 0:
                raise OSError("Stdio writer closed")
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
            if self._dir >= 0:
                os.close(self._dir)
                self._dir = -1


def enforce_job_stdio_retention_cap(
    runner_work_dir: Path,
    *,
    max_total_bytes: int = MAX_TOTAL_JOB_STDIO_BYTES,
    active_job_ids: set[str] | None = None,
    reserve_per_active_job: int = 0,
) -> dict[str, Any]:
    """Enforce hard cumulative retention ceiling across ALL `runner-work/*/runner_stdio.log*` files (S-05).

    Active jobs are spared; oldest finished job stdio logs (sorted by mtime ascending) are deleted
    first until cumulative size across `runner_work_dir` is strictly `<= max_total_bytes`."""
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
                    if f_entry.name not in {"runner_stdio.log", "runner_stdio.log.1"} or not f_entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        st = f_entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    total += int(st.st_size)
                    entries.append((float(st.st_mtime), jid, Path(f_entry.path), int(st.st_size)))
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        pass

    used = dict.fromkeys(active, 0)
    for _, jid, _, size in entries:
        if jid in used:
            used[jid] += size
    reserved = sum(max(0, reserve_per_active_job - size) for size in used.values())

    # Prune oldest non-active job stdio files first (backups before active file for same mtime)
    pruned_files = 0
    pruned_bytes = 0
    candidates = [e for e in entries if e[1] not in active]
    last_write: dict[str, float] = {}
    for mtime, jid, _, _ in candidates:
        last_write[jid] = max(last_write.get(jid, mtime), mtime)
    candidates.sort(key=lambda item: (last_write[item[1]], item[1], item[2].name == "runner_stdio.log"))
    for _, _, fpath, sz in candidates:
        if total + reserved <= max_total_bytes:
            break
        try:
            parent = os.open(fpath.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.unlink(fpath.name, dir_fd=parent)
            finally:
                os.close(parent)
            total -= sz
            pruned_files += 1
            pruned_bytes += sz
        except OSError:
            continue

    return {
        "total_bytes_after": max(0, total),
        "committed_bytes_after": total + reserved,
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
    try:
        parent = os.open(log_path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return [], False

    remaining = max(0, min(MAX_JOB_DETAIL_STDIO_READ_BYTES, int(max_read_bytes)))
    truncated = False
    chunks: list[bytes] = []
    try:
        for name in (log_path.name, log_path.name + ".1"):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            except OSError:
                if name == log_path.name:
                    break
                continue
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    break
                size = min(st.st_size, remaining)
                data = os.pread(fd, size, st.st_size - size)
                remaining -= size
                if st.st_size > size:
                    truncated = True
                    nl = data.find(b"\n")
                    if nl != -1:
                        data = data[nl + 1 :]
                chunks.append(data)
            finally:
                os.close(fd)
    finally:
        os.close(parent)

    if not chunks:
        return [], truncated
    raw_bytes = b"".join(reversed(chunks))
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        truncated = True
        lines = lines[-max_lines:] if max_lines > 0 else []
    return lines, truncated
