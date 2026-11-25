import io
import logging.config
import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import SimpleQueue

import pytest
import zstandard

from app import logging_setup as logs
from app.config import Settings
from app.runner.daemon import IsolatedRunnerManager
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


def test_writer_refuses_oversized_existing_backup(tmp_path):
    path = tmp_path / "runner_stdio.log"
    backup = path.with_suffix(".log.1")
    backup.write_bytes(b"x" * 1025)
    with pytest.raises(OSError, match="Existing rotated stdio segment exceeds quota"):
        logs.BoundedJobStdioWriter(path, max_segment_bytes=1024)
    assert not path.exists()


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


def test_runner_reserves_stdio_for_concurrent_submissions(tmp_path, monkeypatch):
    monkeypatch.setenv("LRM_JOB_STDIO_SEGMENT_BYTES", "1024")
    monkeypatch.setenv("LRM_JOB_STDIO_MAX_TOTAL_BYTES", "4096")
    work_dir = tmp_path / "runner-work"
    evidence_dir = tmp_path / "evidence"
    settings = Settings(
        runtime_dir=tmp_path, config_dir=tmp_path, runner_work_dir=work_dir,
        evidence_store_dir=evidence_dir, production_mode=False,
    )
    manager = IsolatedRunnerManager(settings, logging.getLogger(__name__))
    monkeypatch.setattr(manager, "health", lambda: {"ok": True})
    monkeypatch.setattr(manager.gateway_profiles, "resolve_runtime_credentials", lambda _: ({}, ""))

    old_log = work_dir / "old" / "runner_stdio.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_bytes(b"x" * 1024)
    submit_together = threading.Barrier(3, timeout=5)
    fill_logs = threading.Event()
    finish_jobs = threading.Event()
    filled = SimpleQueue()

    def run_direct(state, spec, work_dir, evidence_dir, writer, gw_pub, gw_key, total_planned):
        assert fill_logs.wait(5)
        writer.write(b"x" * 8192)
        filled.put(threading.current_thread())
        assert finish_jobs.wait(5)
        state.emit("runner_completed", {}, new_status="finalizing", exit_code=0)

    monkeypatch.setattr(manager, "_run_real_direct", run_direct)

    def submit(job_id):
        return manager.submit_job({
            "job_id": job_id, "channel_secret": "test-channel-secret", "spec": {"kind": "direct"},
        })

    def concurrent_submit(job_id):
        submit_together.wait()
        return submit(job_id)

    def stdio_bytes():
        return sum(path.stat().st_size for path in work_dir.glob("*/runner_stdio.log*"))

    workers = []
    try:
        job_ids = ["job-a", "job-b", "job-c"]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = dict(zip(job_ids, pool.map(concurrent_submit, job_ids)))
        assert sorted(status for status, _ in results.values()) == [202, 202, 503]
        rejected = next(job_id for job_id, (status, _) in results.items() if status == 503)
        assert results[rejected][1]["code"] == "RUNNER_STDIO_CAPACITY_EXHAUSTED"
        assert not (work_dir / rejected).exists()
        assert not (evidence_dir / rejected).exists()
        assert not old_log.exists()

        fill_logs.set()
        for _ in range(2):
            workers.append(filled.get(timeout=5))
        assert stdio_bytes() == 4096
    finally:
        fill_logs.set()
        finish_jobs.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()

    for job_id in job_ids:
        if job_id != rejected:
            assert manager.get_events(job_id, 0)[1]["status"] == "finalizing"
    assert submit(rejected)[0] == 202
    worker = filled.get(timeout=5)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert stdio_bytes() == 4096


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
