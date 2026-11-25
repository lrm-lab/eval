from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

from app.api.errors import AppError


class Database:
    """Thread-safe SQLite WAL repository wrapper (ext4 exclusive, future PostgreSQL swappable).
    Supports a fail-closed `write_guard` hook checked before EVERY transaction / executescript
    so low-disk protection (`503 LOCAL_DISK_LOW`) can never be bypassed by any write path.
    In `read_only_mode=True` (triggered under low-disk write protection), read queries open
    SQLite via `mode=ro&immutable=1` (when no active non-empty WAL exists) or `mode=ro`
    (when WAL+SHM already exist), guaranteeing ZERO `-wal`/`-shm` sidecar creation on disk."""

    def __init__(self, db_path: Path, *, write_guard: Callable[[], None] | None = None) -> None:
        self.db_path = db_path
        self.write_guard = write_guard
        self.read_only_mode: bool = False
        self._local = threading.local()
        self._write_lock = threading.RLock()

    def _run_write_guard(self) -> None:
        if self.write_guard is not None:
            try:
                self.write_guard()
            except AppError as exc:
                if exc.code == "LOCAL_DISK_LOW":
                    self.set_read_only_mode(True)
                raise

    def check_write_guard(self) -> None:
        """Explicit write-guard check usable by coordinators/services before mutating or flipping state."""
        self._run_write_guard()

    def set_read_only_mode(self, enabled: bool) -> None:
        self.read_only_mode = enabled
        if enabled:
            self.close_thread_connection()

    def _open_strict_readonly(self) -> sqlite3.Connection:
        """Explicit low-disk read-only SQLite open contract:
        - When no non-empty `-wal` exists: opens `file:<db>?mode=ro&immutable=1`, guaranteeing ZERO `-wal`/`-shm` creation.
        - When both non-empty `-wal` and `-shm` already exist: opens `file:<db>?mode=ro`, reading existing WAL without creating new sidecars.
        - When non-empty `-wal` exists without `-shm`: fails closed with `503 LOCAL_DISK_LOW` rather than creating a new `-shm` file."""
        encoded = quote(str(self.db_path), safe="/")
        wal_path = Path(f"{self.db_path}-wal")
        shm_path = Path(f"{self.db_path}-shm")
        has_nonempty_wal = wal_path.exists() and wal_path.stat().st_size > 0
        if not has_nonempty_wal:
            # immutable=1 guarantees SQLite NEVER creates -wal or -shm sidecar files on read!
            uri = f"file:{encoded}?mode=ro&immutable=1"
        elif shm_path.exists():
            # Existing WAL + existing SHM can be read via mode=ro without creating new sidecars
            uri = f"file:{encoded}?mode=ro"
        else:
            raise AppError(
                503,
                "LOCAL_DISK_LOW",
                "低水位写保护模式下存在未检查点 WAL 且缺少 SHM，拒绝创建新侧文件以防撑满磁盘",
                {"wal_bytes": wal_path.stat().st_size},
            )
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly or self.read_only_mode:
            return self._open_strict_readonly()
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        ro_conn: sqlite3.Connection | None = getattr(self._local, "ro_conn", None)
        if ro_conn is not None:
            ro_conn.close()
            self._local.ro_conn = None

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Dedicated consistent MVCC read-snapshot connection (`BEGIN` + `PRAGMA query_only = ON`).
        In SQLite WAL mode, once the first SELECT executes inside this context, ALL subsequent
        SELECTs on the yielded connection observe the exact same committed point-in-time even if
        a concurrent write commits on another thread or hook mid-request!
        In `read_only_mode=True`, uses `_open_strict_readonly()` to guarantee zero sidecar creation."""
        if self.read_only_mode:
            conn = self._open_strict_readonly()
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Single-transaction atomic write (`BEGIN IMMEDIATE ... COMMIT`), guarded by write_guard."""
        self._run_write_guard()
        with self._write_lock:
            if self.read_only_mode:
                self.read_only_mode = False
                self.close_thread_connection()
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def executescript(self, script: str) -> None:
        self._run_write_guard()
        with self._write_lock:
            if self.read_only_mode:
                self.read_only_mode = False
                self.close_thread_connection()
            conn = self.connect()
            conn.executescript(script)
            conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        if self.read_only_mode:
            prev_ro: sqlite3.Connection | None = getattr(self._local, "ro_conn", None)
            if prev_ro is not None:
                prev_ro.close()
            ro_conn = self._open_strict_readonly()
            self._local.ro_conn = ro_conn
            return ro_conn.execute(sql, params)
        conn = self.connect()
        return conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        if self.read_only_mode:
            conn = self._open_strict_readonly()
            try:
                row = conn.execute(sql, params).fetchone()
                return dict(row) if row is not None else None
            finally:
                conn.close()
        row = self.connect().execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        if self.read_only_mode:
            conn = self._open_strict_readonly()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = self.connect().execute(sql, params).fetchall()
        return [dict(r) for r in rows]
