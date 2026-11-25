#!/usr/bin/env python3
"""Create a single atomic zstd-compressed online backup of eval_platform.db (0700/0600, max 1 backup — S-05)."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.security.storage_probe import check_disk_write_guard, probe_isolated_dir


def create_single_compressed_backup(settings: Settings) -> dict[str, object]:
    assert settings.state_dir is not None and settings.db_path is not None
    if not settings.db_path.is_file():
        raise FileNotFoundError(f"SQLite DB not found: {settings.db_path}")

    backups_dir = settings.state_dir / "backups"
    check_disk_write_guard(
        settings.state_dir,
        min_free_gb=settings.root_disk_min_free_gb,
        runtime_dir=settings.runtime_dir,
        local_budget_mb=settings.local_budget_mb,
    )
    probe_isolated_dir(backups_dir, create=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = backups_dir / f"eval_platform-{ts}.db.zst"

    with tempfile.TemporaryDirectory(dir=str(backups_dir)) as tmp_dir:
        raw_snap = Path(tmp_dir) / "snapshot.db"
        src_conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=30.0)
        dst_conn = sqlite3.connect(str(raw_snap))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        tmp_zst = backups_dir / f".{final_path.name}.tmp"
        cctx = zstd.ZstdCompressor(level=6)
        try:
            with raw_snap.open("rb") as rf, tmp_zst.open("wb") as wf:
                cctx.copy_stream(rf, wf)
            os.chmod(tmp_zst, 0o600)
            tmp_zst.replace(final_path)
        finally:
            tmp_zst.unlink(missing_ok=True)

    # Enforce strict 1-backup retention policy (06-1 §4.2: latest 1 .db.zst <= 45 MB)
    removed: list[str] = []
    for old in sorted(backups_dir.glob("*.db.zst")):
        if old.resolve() != final_path.resolve():
            old.unlink(missing_ok=True)
            removed.append(old.name)

    return {
        "backup_path": str(final_path),
        "size_bytes": final_path.stat().st_size,
        "retained_count": len(list(backups_dir.glob("*.db.zst"))),
        "removed_old": removed,
    }


def main() -> int:
    res = create_single_compressed_backup(Settings())
    print(f"Created backup: {res['backup_path']} ({res['size_bytes']} bytes, retained={res['retained_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
