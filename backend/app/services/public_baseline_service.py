from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.api.errors import AppError
from app.db.database import Database
from app.security.auth import AuthUser
from app.security.authz import can_access_project, has_scope
from app.services.public_baseline_sources import SOURCES, SOURCES_BY_ID, PublicSource

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _harness_key(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalnum())


def _comparison_reasons(batch: dict[str, Any], source: PublicSource, public: dict[str, Any]) -> list[str]:
    if batch["benchmark_slug"] != source.benchmark_slug:
        return ["该批次未测此基准"]
    if batch["split"] != source.split:
        return [f"题集分片不同：站内 {batch['split'] or '未知'}，公榜 {source.split}"]
    if batch["dataset_release_id"] != source.release_id:
        return [f"题集版本不同：站内 {batch['dataset_version'] or '未知'}，公榜 {source.dataset_version}"]
    if batch["is_running_placeholder"] or batch["valid_mean"] is None:
        return ["批次暂无可用分数"]

    reasons = []
    if batch["run_scope"] != "full_split" or batch["unique_tasks"] != source.task_count or public["task_count"] != source.task_count:
        reasons.append(f"题集覆盖不同：站内 {batch['unique_tasks']} 题，公榜 {public['task_count']} 题，需完整 {source.task_count} 题")
    if batch["scoring_protocol"] != source.protocol:
        reasons.append(f"评分协议不同：站内 {batch['scoring_protocol']}，公榜 {source.protocol}")
    elif batch["fault_policy"] != "official_protocol":
        reasons.append(f"故障处理不同：站内 {batch['fault_policy']}，公榜按官方协议处理")
    if source.runner and batch["runner"] != source.runner:
        reasons.append(f"运行器不同：站内 {batch['runner']}，公榜 {source.runner}")
    if _harness_key(batch["harness"] or "") != _harness_key(public["harness"]):
        reasons.append(f"Harness 不同：站内 {batch['harness'] or '未记录'}，公榜 {public['harness']}")
    if batch["scored"] != public["sample_count"]:
        reasons.append(f"计分样本量不同：站内 {batch['scored']} 次，公榜 {public['sample_count']} 次")
    return reasons


class PublicBaselineService:
    def __init__(self, db: Database, *, interval_seconds: int) -> None:
        self.db = db
        self.interval = max(60, interval_seconds) if interval_seconds > 0 else 0
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self.interval and (self._worker is None or not self._worker.is_alive()):
            self._stop.clear()
            self._worker = threading.Thread(target=self._poll, name="public-baseline-sync", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._stop.set()

    def _poll(self) -> None:
        try:
            while not self._stop.is_set():
                wait = self.interval
                for source in SOURCES:
                    if self._stop.is_set():
                        return
                    if self.db.read_only_mode:
                        wait = min(wait, 300)
                        break
                    try:
                        snapshot = self._snapshot(source.id)
                        delay = self.interval if not snapshot or snapshot["succeeded"] else min(self.interval, 300)
                        remaining = 0.0
                        if snapshot:
                            remaining = delay - (_now() - datetime.fromisoformat(snapshot["attempted_at"])).total_seconds()
                        if remaining <= 0:
                            ok = self.refresh(source)
                            remaining = self.interval if ok else min(self.interval, 300)
                        wait = min(wait, remaining)
                    except (AppError, sqlite3.Error) as exc:
                        logger.warning("Could not persist public baseline %s (%s)", source.id, type(exc).__name__)
                        wait = min(wait, 300)
                self._stop.wait(max(1, wait))
        finally:
            self.db.close_thread_connection()

    def refresh(self, source: PublicSource) -> bool:
        attempted = _now().isoformat()
        try:
            rows, published = source.fetch()
            if not rows or len({row.id for row in rows}) != len(rows):
                raise ValueError("empty or duplicate score rows")
            payload = json.dumps([asdict(row) for row in rows], ensure_ascii=False, allow_nan=False)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Could not validate public baseline %s (%s)", source.id, type(exc).__name__)
            if not self._stop.is_set():
                with self.db.transaction() as conn:
                    conn.execute(
                        """INSERT INTO public_baseline_snapshots (source_id, attempted_at, succeeded)
                           VALUES (?, ?, 0) ON CONFLICT(source_id) DO UPDATE SET
                           attempted_at = excluded.attempted_at, succeeded = 0
                           WHERE excluded.attempted_at > public_baseline_snapshots.attempted_at""",
                        (source.id, attempted),
                    )
            return False

        if not self._stop.is_set():
            with self.db.transaction() as conn:
                conn.execute(
                    """INSERT INTO public_baseline_snapshots
                       (source_id, synced_at, attempted_at, published_at, rows_json, succeeded)
                       VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(source_id) DO UPDATE SET
                       synced_at = excluded.synced_at, attempted_at = excluded.attempted_at,
                       published_at = excluded.published_at, rows_json = excluded.rows_json, succeeded = 1
                       WHERE excluded.attempted_at > public_baseline_snapshots.attempted_at""",
                    (source.id, _now().isoformat(), attempted, published, payload),
                )
        return True

    def _snapshot(self, source_id: str) -> dict[str, Any] | None:
        try:
            return self.db.fetchone("SELECT * FROM public_baseline_snapshots WHERE source_id = ?", (source_id,))
        except sqlite3.OperationalError as exc:
            if self.db.read_only_mode and "no such table" in str(exc):
                return None
            raise

    def catalog(self) -> dict[str, Any]:
        result = []
        for source in SOURCES:
            snapshot = self._snapshot(source.id)
            synced = snapshot["synced_at"] if snapshot else None
            status = "pending"
            if snapshot:
                status = "unavailable"
                if synced:
                    overdue = self.interval and _now() - datetime.fromisoformat(synced) > timedelta(seconds=self.interval * 2)
                    status = "ready" if snapshot["succeeded"] and not overdue else "stale"
            result.append({
                "id": source.id,
                "label": source.label,
                "benchmark_slug": source.benchmark_slug,
                "url": source.url,
                "dataset_version": source.dataset_version,
                "split": source.split,
                "task_count": source.task_count,
                "metric": source.metric,
                "protocol": source.protocol,
                "scoring_rule": source.scoring_rule,
                "runner": source.runner,
                "status": status,
                "synced_at": synced,
                "published_at": snapshot["published_at"] if snapshot else None,
                "rows": json.loads(snapshot["rows_json"]) if synced else [],
            })
        return {"sources": result}

    @staticmethod
    def _visible_batches(
        conn: sqlite3.Connection, user: AuthUser, batch_ids: list[str],
    ) -> list[tuple[str, dict[str, Any] | None]]:
        results = []
        can_read_holdout = has_scope(user, "dataset:holdout_read")
        for batch_id in dict.fromkeys(batch_ids):
            batch = conn.execute(
                """SELECT b.batch_id, b.project_id, b.owner_id, b.benchmark_slug, b.dataset_release_id,
                          b.raw_dataset_name, b.batch_name, b.run_scope, b.unique_tasks, b.is_running_placeholder,
                          b.valid_mean, b.scoring_protocol, b.fault_policy, b.runner, b.harness, b.harness_version,
                          b.scored, b.generation_id, b.official_status, r.dataset_version, r.split,
                          o.dataset AS official_dataset, o.metric AS official_metric, o.score AS official_score,
                          o.denominator AS official_denominator, o.fault_policy AS official_fault_policy
                   FROM batches b LEFT JOIN dataset_releases r USING (dataset_release_id)
                   LEFT JOIN official_results o USING (batch_id)
                   WHERE b.batch_id = ?""",
                (batch_id,),
            ).fetchone()
            if batch is None or not can_access_project(user, batch["project_id"], batch["owner_id"]):
                results.append((batch_id, None))
            elif not can_read_holdout and (
                any("holdout" in (batch[key] or "").lower() for key in ("split", "dataset_release_id", "raw_dataset_name", "batch_name"))
                or conn.execute(
                    """SELECT 1 FROM episodes e JOIN task_definitions t ON t.task_id = e.task_id
                       WHERE e.batch_id = ? AND t.dataset_release_id = ? AND LOWER(t.split) = 'holdout' LIMIT 1""",
                    (batch_id, batch["dataset_release_id"]),
                ).fetchone()
            ):
                results.append((batch_id, None))
            else:
                results.append((batch_id, dict(batch)))
        return results

    @staticmethod
    def _use_attached_score(batch: dict[str, Any], source: PublicSource) -> dict[str, Any]:
        # Use the existing indexed/admin official-result entry point. Its free-form labels
        # must identify the exact registered release and scorer before substituting a score.
        if (
            batch["official_status"] == "official_verified"
            and batch["official_dataset"] == source.release_id
            and batch["official_metric"] == source.protocol
            and batch["official_fault_policy"] == "official_protocol"
            and batch["official_denominator"] == batch["scored"]
            and batch["official_score"] is not None
            and 0 <= batch["official_score"] <= 1
        ):
            return {
                **batch,
                "scoring_protocol": source.protocol,
                "fault_policy": batch["official_fault_policy"],
                "valid_mean": batch["official_score"],
            }
        return batch

    def default(self, user: AuthUser, batch_ids: list[str]) -> dict[str, str | None]:
        """First visible batch with an aligned row; highest official score, then stable row ID."""
        with self.db.read_snapshot() as conn:
            snapshots = {s.id: self._snapshot(s.id) for s in SOURCES}
            rows_by_source = {sid: json.loads(snap["rows_json"]) for sid, snap in snapshots.items() if snap is not None}
            by_benchmark = {source.benchmark_slug: source for source in SOURCES}
            for _, batch in self._visible_batches(conn, user, batch_ids):
                source = by_benchmark.get(batch["benchmark_slug"]) if batch else None
                if batch is None or source is None:
                    continue
                batch = self._use_attached_score(batch, source)
                for row in rows_by_source.get(source.id, []):
                    if not _comparison_reasons(batch, source, row) and not self._check_episodes(conn, batch, source, row):
                        return {"baseline_id": row["id"]}
        return {"baseline_id": None}

    def compare(self, user: AuthUser, baseline_id: str, batch_ids: list[str]) -> dict[str, Any]:
        source = SOURCES_BY_ID.get(baseline_id.partition(":")[0])
        snapshot = self._snapshot(source.id) if source else None
        baseline = next((r for r in json.loads(snapshot["rows_json"]) if r["id"] == baseline_id), None) if snapshot else None
        if source is None or baseline is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "公开基准当前不可用")

        items = []
        with self.db.read_snapshot() as conn:
            for batch_id, batch in self._visible_batches(conn, user, batch_ids):
                if batch is None:
                    items.append({"batch_id": batch_id, "reasons": ["批次缺失或当前账号无权读取"], "delta_pp": None})
                    continue
                batch = self._use_attached_score(batch, source)
                reasons = _comparison_reasons(batch, source, baseline)
                if not reasons:
                    reasons.extend(self._check_episodes(conn, batch, source, baseline))
                local = batch["valid_mean"]
                local_pct = local * 100 if local is not None and 0 <= local <= 1 else None
                items.append({
                    "batch_id": batch_id,
                    "local_score_pct": local_pct,
                    "dataset_version": batch["dataset_version"],
                    "split": batch["split"],
                    "task_count": batch["unique_tasks"],
                    "sample_count": batch["scored"],
                    "harness": batch["harness"],
                    "harness_version": batch["harness_version"],
                    "protocol": batch["scoring_protocol"],
                    "delta_pp": local_pct - baseline["score_pct"] if local_pct is not None and not reasons else None,
                    "reasons": reasons,
                })
        return {"baseline": baseline, "synced_at": snapshot["synced_at"], "items": items}

    @staticmethod
    def _check_episodes(conn: sqlite3.Connection, batch: dict[str, Any], source: PublicSource, baseline: dict[str, Any]) -> list[str]:
        if not source.task_ids_sha256:
            return ["公榜未公开可核验的试题清单，无法确认同题"]
        counts = conn.execute(
            """SELECT e.task_id, COUNT(*) AS n, COUNT(DISTINCT e.canonical_trial_key) AS unique_trials,
                      SUM(CASE WHEN e.reward_valid = 1 AND e.reward IN (0, 1) THEN 1 ELSE 0 END) AS valid,
                      SUM(CASE WHEN e.reward = 1 THEN 1 ELSE 0 END) AS passed,
                      MAX(CASE WHEN NULLIF(t.task_checksum, '') IS NOT NULL
                                AND COALESCE(LOWER(e.task_checksum), '') != LOWER(t.task_checksum)
                               THEN 1 ELSE 0 END) AS drift
               FROM episodes e LEFT JOIN task_definitions t ON t.dataset_release_id = ? AND t.task_id = e.task_id
               WHERE e.batch_id = ? AND e.generation_id = ? GROUP BY e.task_id ORDER BY e.task_id""",
            (batch["dataset_release_id"], batch["batch_id"], batch["generation_id"]),
        ).fetchall()
        digest = hashlib.sha256("\n".join(r["task_id"] for r in counts).encode()).hexdigest()
        if digest != source.task_ids_sha256 or any(r["drift"] for r in counts):
            return ["站内实测试题与公榜登记的题集不一致，或缺少可核验的试题记录"]
        if (
            sum(r["n"] for r in counts) != baseline["sample_count"]
            or any(r["n"] != r["unique_trials"] or r["n"] != r["valid"] for r in counts)
            or len({r["n"] for r in counts}) != 1
        ):
            return ["试验记录的有效性、去重或逐题重复次数未对齐"]
        if not math.isclose(batch["valid_mean"], sum(r["passed"] for r in counts) / baseline["sample_count"], abs_tol=1e-6):
            return ["站内批次总分与逐次有效评分记录不一致"]
        return []
