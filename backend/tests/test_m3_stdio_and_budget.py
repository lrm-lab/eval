import io
import logging.config
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import zstandard

from app import logging_setup as logs
from scripts import backup_db


MIB = 1024 * 1024


def test_stdio_flood_and_link_safety(tmp_path):
    path = tmp_path / "job" / "runner_stdio.log"
    target = tmp_path / "untouched"
    target.write_bytes(b"original")
    target.chmod(0o644)
    path.parent.mkdir()
    path.symlink_to(target)
    with pytest.raises(OSError):
        logs.BoundedJobStdioWriter(path)
    assert logs.read_bounded_tail_lines(path) == ([], False)
    path.unlink()
    path.with_suffix(".log.1").symlink_to(target)
    writer = logs.BoundedJobStdioWriter(path)
    try:
        writer.drain_stream(io.BytesIO(b"x" * (9 * MIB) + b"\nlast\n"))
    finally:
        writer.close()
    assert target.read_bytes() == b"original"
    assert target.stat().st_mode & 0o777 == 0o644
    segments = list(path.parent.iterdir())
    assert len(segments) == 2
    assert all(p.stat().st_size <= MIB and p.stat().st_mode & 0o777 == 0o600 for p in segments)
    assert logs.read_bounded_tail_lines(path)[0][-1] == "last"


@pytest.mark.parametrize("limit", [200, 128 * 1024, 512 * 1024])
def test_tail_io_budget_across_rotation(tmp_path, monkeypatch, limit):
    path = tmp_path / "runner_stdio.log"
    path.write_bytes(b"recent\n")
    path.with_suffix(".log.1").write_bytes(b"older\n" * 40000)
    real_read = os.pread
    requests = []

    def counted(fd, size, offset):
        requests.append(size)
        return real_read(fd, size, offset)

    monkeypatch.setattr(os, "pread", counted)
    lines, truncated = logs.read_bounded_tail_lines(path, max_read_bytes=limit)
    assert truncated and lines[-2:] == ["older", "recent"]
    assert sum(requests) == min(128 * 1024, limit)


def test_reserve_running_jobs_before_pruning(tmp_path):
    for name, stamp in [("old", 2), ("new", 3), ("running", 1)]:
        path = tmp_path / name / "runner_stdio.log"
        path.parent.mkdir()
        path.write_bytes(b"x" * 1024)
        os.utime(path, (stamp, stamp))
    unrelated = tmp_path / "old" / "runner_stdio.log.notes"
    unrelated.touch()
    result = logs.enforce_job_stdio_retention_cap(
        tmp_path, max_total_bytes=5120, active_job_ids={"running", "incoming"},
        reserve_per_active_job=2048,
    )
    assert result["committed_bytes_after"] == 5120
    assert {p.parent.name for p in tmp_path.glob("*/runner_stdio.log")} == {"new", "running"}
    assert unrelated.exists()


def test_daemon_multibyte_and_oversized_records(tmp_path):
    for component, count in logs.COMPONENT_BACKUP_COUNTS.items():
        cfg = logs.build_uvicorn_log_config(tmp_path, component, max_bytes=4096)
        config = cfg["handlers"]["rotating_file"].copy()
        config.pop("formatter")
        handler = logging.config.DictConfigurator(cfg).configure_handler(config)
        try:
            for n in (300, 500, 10000) * 6:
                handler.handle(logging.makeLogRecord({"msg": "\u754c" * n}))
        finally:
            handler.close()
        files = list(tmp_path.glob(f"{component}.log*"))
        assert len(files) == count + 1
        assert max(p.stat().st_size for p in files) <= 4096
    assert sum(1 + c for c in logs.COMPONENT_BACKUP_COUNTS.values()) * logs.MAX_LOG_BYTES_PER_FILE == 150 * MIB


def test_backup_replacement_survives_size_failure(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t(x)")
    result = backup_db._backup_locked(db, tmp_path)
    path = Path(result["backup_path"])
    previous = path.read_bytes()
    assert zstandard.ZstdDecompressor().stream_reader(previous).read().startswith(b"SQLite format 3")
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(backup_db, "MAX_COMPRESSED_BYTES", 16)
    with pytest.raises(ValueError):
        backup_db._backup_locked(db, tmp_path)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.db.zst")) == [path]


def test_197_deployed_peak():
    root = Path(os.environ.get("LRM_BUDGET_RUNTIME_DIR", Path.home() / ".local/share/lrm-eval"))
    if not (root / "releases/current").is_dir():
        pytest.skip("requires deployed runtime; set LRM_BUDGET_RUNTIME_DIR")
    assert subprocess.check_output(["findmnt", "-no", "FSTYPE", "-T", root], text=True).strip() == "ext4"
    releases = [p for p in (root / "releases").iterdir() if not p.is_symlink() and p.is_dir()]
    assert len(releases) == 2
    # Keep all current disk usage (including both releases); deliberately double-count
    # existing logs/backup/WAL rather than undercounting a transient peak.
    baseline = int(subprocess.check_output(["du", "-s", "-B1", root]).split()[0])
    db = root / "state/eval_platform.db"
    # Snapshot may contain pages still in the WAL. Reserve a full extra WAL for them.
    snapshot = db.stat().st_size + 32 * MIB
    retained = baseline + (32 + 1 + 150 + 20) * MIB + backup_db.MAX_COMPRESSED_BYTES
    peak = retained + snapshot + backup_db.MAX_COMPRESSED_BYTES
    print(f"ext4 bytes: baseline={baseline}, retained={retained}, backup_peak={peak}")
    assert peak < 750 * MIB
