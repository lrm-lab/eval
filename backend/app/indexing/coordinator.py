from __future__ import annotations

import threading
from collections import deque
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.cold_indexer import ColdIndexer
from app.indexing.hot_indexer import HotIndexer


class IndexCoordinator:
    """Manages initial synchronous hot scan, async background cold scan (including large .tar.zst bundles),
    15s incremental hot polling, and fail-closed LOCAL_DISK_LOW write protection."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.hot = HotIndexer(settings, db)
        self.cold = ColdIndexer(settings, db)
        self._stop_event = threading.Event()
        self._hot_thread: threading.Thread | None = None
        self._cold_thread: threading.Thread | None = None
        self._enrich_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.last_hot_sync_at: str | None = None
        self.last_hot_stats: dict[str, Any] = {}
        self.recent_poll_ms: deque[float] = deque(maxlen=10)
        self.cold_status: str = "idle"
        self.last_cold_stats: dict[str, Any] = {}
        self.last_cold_sync_at: str | None = None
        self.write_protected: bool = False
        self.write_block_reason: str | None = None

    def run_hot_now(self, *, force: bool = False) -> dict[str, Any]:
        try:
            self.db.check_write_guard()
            stats = self.hot.run_hot_scan(force=force)
        except AppError as exc:
            if exc.code == "LOCAL_DISK_LOW":
                with self._lock:
                    self.write_protected = True
                    self.write_block_reason = exc.message
            raise
        with self._lock:
            self.write_protected = False
            self.write_block_reason = None
            self.last_hot_sync_at = utcnow_iso()
            self.last_hot_stats = stats
            self.recent_poll_ms.append(float(stats.get("elapsed_ms", 0.0)))
        return stats

    def run_cold_now(self, *, force: bool = False, include_large_zst: bool = True) -> dict[str, Any]:
        with self._lock:
            self.cold_status = "running"
        try:
            self.db.check_write_guard()
            stats = self.cold.run_cold_scan(force=force, include_large_zst=include_large_zst)
            with self._lock:
                self.write_protected = False
                self.write_block_reason = None
                self.cold_status = "ready"
                self.last_cold_stats = stats
                self.last_cold_sync_at = utcnow_iso()
            return stats
        except AppError as exc:
            with self._lock:
                if exc.code == "LOCAL_DISK_LOW":
                    self.write_protected = True
                    self.write_block_reason = exc.message
                    self.cold_status = "write_protected_disk_low"
                else:
                    self.cold_status = f"error: {exc.message}"
            raise
        except Exception as exc:
            with self._lock:
                self.cold_status = f"error: {exc}"
            raise

    def start(self) -> None:
        # Initial synchronous hot scan (< 5s); respect LOCAL_DISK_LOW without crashing read-only health!
        try:
            self.run_hot_now(force=False)
        except AppError as exc:
            if exc.code != "LOCAL_DISK_LOW":
                raise
        if not self.settings.enable_background_indexer:
            return
        self._stop_event.clear()
        self._cold_thread = threading.Thread(
            target=self._cold_worker, name="lrm-eval-cold-indexer", daemon=True
        )
        self._cold_thread.start()
        self._enrich_thread = threading.Thread(
            target=self._enrich_worker, name="lrm-eval-trial-enrichment", daemon=True
        )
        self._enrich_thread.start()
        self._hot_thread = threading.Thread(
            target=self._hot_poll_loop, name="lrm-eval-hot-poller", daemon=True
        )
        self._hot_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._hot_thread and self._hot_thread.is_alive():
            self._hot_thread.join(timeout=2.0)

    def _enrich_worker(self) -> None:
        interval = max(2, int(self.settings.poll_interval_seconds))
        while not self._stop_event.is_set():
            try:
                self.db.check_write_guard()
                self.hot.enrich_harbor_trials()
            except Exception:
                pass
            finally:
                self.db.close_thread_connection()
            self._stop_event.wait(interval)

    def _cold_worker(self) -> None:
        try:
            self.run_cold_now(force=False, include_large_zst=True)
        except AppError as exc:
            if exc.code == "LOCAL_DISK_LOW":
                return
        except Exception:
            pass

    def _hot_poll_loop(self) -> None:
        interval = max(2, int(self.settings.poll_interval_seconds))
        while not self._stop_event.wait(interval):
            try:
                self.run_hot_now(force=False)
            except AppError as exc:
                if exc.code == "LOCAL_DISK_LOW":
                    continue
            except Exception:
                pass

    def snapshot_health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_hot_sync_at": self.last_hot_sync_at,
                "last_hot_stats": self.last_hot_stats,
                "recent_poll_ms": list(self.recent_poll_ms),
                "cold_status": self.cold_status,
                "last_cold_sync_at": self.last_cold_sync_at,
                "last_cold_stats": self.last_cold_stats,
                "write_protected": self.write_protected,
                "write_block_reason": self.write_block_reason,
            }
