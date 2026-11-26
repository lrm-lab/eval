from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any

from app.security.authz import sql_task_holdout_exclude_clause


def _recorded_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value.strip()) < 16:
        return None
    try:
        instant = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def evaluation_recorded_at(
    conn: Connection, batch: dict[str, Any], *, can_holdout: bool,
) -> tuple[str | None, str | None]:
    """Return a run time and its provenance using the same snapshot as the batch."""
    params = (batch["batch_id"], batch["generation_id"])
    if not can_holdout and batch.get("dataset_release_id"):
        public_task = sql_task_holdout_exclude_clause("?", "e.task_id")
        has_hidden_trials = conn.execute(
            f"""SELECT 1 FROM episodes e
                WHERE e.batch_id = ? AND e.generation_id = ? AND NOT ({public_task})
                LIMIT 1""",
            (*params, batch["dataset_release_id"]),
        ).fetchone()
        if has_hidden_trials:
            return None, None

    instant = _recorded_instant(batch.get("finished_at"))
    if instant is not None:
        return instant.isoformat().replace("+00:00", "Z"), "batch_finished_at"

    if str(batch.get("batch_status") or "").strip().lower() != "finished" or not batch.get("recorded"):
        return None, None
    times = [
        _recorded_instant(row["finished_at"])
        for row in conn.execute(
            "SELECT finished_at FROM episodes WHERE batch_id = ? AND generation_id = ?", params,
        )
    ]
    if len(times) != batch["recorded"] or any(time is None for time in times):
        return None, None
    latest = max(time for time in times if time is not None)
    return latest.isoformat().replace("+00:00", "Z"), "latest_trial_finished_at"
