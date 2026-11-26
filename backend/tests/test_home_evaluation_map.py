from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import create_user, login_client


PATH = "/api/v1/home/evaluation-map"
RELEASE = "map-fixture__1__default"
SECOND = "map-fixture__1__validation"
HOLDOUT = "map-fixture__1__holdout"
OLD = "2026-11-24T09:00:00Z"
NEW = "2026-11-25T08:00:00Z"
PASSWORD = "HomeMapFixturePassword1!"


def seed_catalog(isolated_settings):
    isolated_settings.config_dir = Path(__file__).resolve().parents[2] / "config"
    app = create_app(isolated_settings, run_startup_scan=False)
    db = app.state.ctx.db
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, capability, status, updated_at)
               VALUES ('map-fixture', 'Map fixture', 'agentic', 'Agentic', 'fixture', 'integrated', ?)""",
            (NEW,),
        )
        conn.executemany(
            """INSERT INTO dataset_releases (dataset_release_id, benchmark_slug, dataset_version, split,
                    runner, known_task_count, default_protocol, updated_at)
               VALUES (?, 'map-fixture', '1', ?, 'harbor', ?, 'fixture-reward', ?)""",
            [(RELEASE, "default", 20, NEW), (SECOND, "validation", None, NEW), (HOLDOUT, "holdout", 9, NEW)],
        )
        conn.executemany(
            "INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES (?, ?, ?)",
            [(RELEASE, "ordinary", "default"), (SECOND, "val-a", "validation"), (SECOND, "val-b", "validation")],
        )
    return app, db


def seed_batch(conn, bid, tasks=(("ordinary", 0.0, NEW),), **fields):
    scores = [reward for _, reward, _ in tasks if reward is not None]
    row = {
        "batch_id": bid, "source_locator": f"harbor_runs:{bid}", "source_namespace": "harbor_runs", "batch_name": bid,
        "dataset_release_id": RELEASE, "benchmark_slug": "map-fixture", "generation_id": "gen_original",
        "run_scope": "subset", "scoring_protocol": "fixture-reward", "content_digest": bid, "indexed_at": NEW,
        "model_canonical": "model-a", "model_route": "route-a", "harness": "codex", "harness_version": "1.0",
        "batch_status": "finished", "finished_at": NEW, "recorded": len(tasks), "scored": len(scores), "planned": 20,
        "valid_mean": sum(scores) / len(scores) if scores else None,
        **fields,
    }
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO batches ({', '.join(row)}) VALUES ({placeholders})", tuple(row.values()))
    for index, (task, reward, finished_at) in enumerate(tasks):
        trial = f"{bid}-{index}"
        conn.execute(
            """INSERT INTO episodes (batch_id, trial_id, canonical_trial_key, generation_id, task_id, status,
                                     reward, reward_valid, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bid, trial, trial, row["generation_id"], task, "scored" if reward is not None else "unscored",
             reward, reward is not None, finished_at),
        )


def viewer(app, db, name, *, projects=None, scopes=None):
    user_id = create_user(db, username=name, password=PASSWORD, projects=projects, scopes=scopes)
    client = TestClient(app)
    login_client(client, name, PASSWORD)
    return client, user_id


def cell_by_display_batch(result, batch_id):
    return next(cell for cell in result["cells"] if (cell["latest_batch"] or {}).get("batch_id") == batch_id)


def test_home_map_counts_run_revisions_releases_and_history(isolated_settings):
    app, db = seed_catalog(isolated_settings)
    client, _ = viewer(app, db, "map-counts")
    archive_sql = """INSERT INTO legacy_leaderboard_rows (benchmark, metric, benchmark_slug, dataset_release_id,
                         model_col, model_canonical, raw_score, norm_score_100, scale_type, source_file)
                     VALUES ('Map fixture', 'accuracy', 'map-fixture', ?, 'model-a', 'model-a', 10, 10,
                             'pct_0_100', 'legacy/leaderboard.csv')"""
    with db.transaction() as conn:
        seed_batch(conn, "older", (("ordinary", 1.0, OLD),), finished_at=OLD, indexed_at="2030-01-01")
        seed_batch(conn, "published", finished_at=None, native_job_id="native-run", run_scope="smoke")
        seed_batch(conn, "duplicate", native_job_id="native-run", finished_at=OLD, indexed_at=OLD)
        seed_batch(conn, "progress", (), is_running_placeholder=1, batch_status="running", finished_at=None)
        seed_batch(conn, "val", (("val-a", None, NEW),), dataset_release_id=SECOND, harness="pi", batch_status="failed")
        seed_batch(conn, "unmapped", dataset_release_id=None, benchmark_slug=None, harness=None)
        seed_batch(conn, "empty-failure", (), dataset_release_id=SECOND, harness="openclaw", batch_status="failed")
        conn.execute(archive_sql, (RELEASE,))
        conn.execute(archive_sql, (RELEASE,))

    response = client.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert result["summary"] == {
        "cell_count": 5, "batch_count": 6, "published_run_count": 4, "running_count": 1,
        "unmapped_batch_count": 1, "historical_summary_count": 1,
    }
    combination = cell_by_display_batch(result, "progress")
    assert combination["published_run_counts_by_scope"] == {"subset": 1, "smoke": 1}
    assert combination["latest_scored_batch"]["batch_id"] == "published"
    assert combination["latest_scored_batch"]["score"] == 0
    assert combination["latest_scored_batch"]["recorded_at_source"] == "latest_trial_finished_at"
    assert cell_by_display_batch(result, "val")["latest_scored_batch"] is None
    archive = next(cell for cell in result["cells"] if cell["kind"] == "historical_summary")
    assert archive["published_run_count"] == 0 and archive["latest_batch"] is None
    assert archive["historical_scores"] == [{"model_col": "model-a", "score": 10, "raw_score": 10, "denominator": None}]

    for dimension, groups in result["groups"].items():
        assert sum(group["published_run_count"] for group in groups) == 4, dimension
        assert sorted(cid for group in groups for cid in group["cell_ids"]) == sorted(cell["id"] for cell in result["cells"])
    dataset = next(group for group in result["groups"]["dataset"] if group["id"] == "map-fixture")
    assert dataset["registered_task_count"] == 22 and dataset["dataset_release_ids"] == [RELEASE, SECOND]
    assert result["groups"]["model"][0]["registered_task_count"] == 22
    assert next(group for group in result["groups"]["harness"] if group["id"] == "codex")["registered_task_count"] == 20

    with db.transaction() as conn:
        conn.execute("UPDATE batches SET generation_id = 'gen_current', content_digest = 'next' WHERE batch_id = 'published'")
        conn.execute(
            """INSERT INTO episodes (batch_id, trial_id, canonical_trial_key, generation_id, task_id, status,
                                     reward, reward_valid, finished_at)
               VALUES ('published', 'new-trial', 'new-trial', 'gen_current', 'ordinary', 'scored', 0, 1, ?)""", (NEW,),
        )
    updated = client.get(PATH).json()
    current = cell_by_display_batch(updated, "progress")
    assert updated["summary"] == result["summary"]
    assert current["id"] == combination["id"]
    assert current["latest_scored_batch"]["generation_id"] == "gen_current"
    assert current["latest_scored_batch"]["recorded_at"] == NEW


def test_home_map_filters_projects_and_holdout_before_counting(isolated_settings):
    app, db = seed_catalog(isolated_settings)
    assert TestClient(app).get(PATH).status_code == 401
    ordinary, user_id = viewer(app, db, "map-ordinary")
    scoped, _ = viewer(app, db, "map-holdout", scopes=["dataset:holdout_read"])
    outside, _ = viewer(app, db, "map-outside", projects=["other-team"])
    with db.transaction() as conn:
        conn.execute("INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES (?, 'secret-task', 'holdout')", (RELEASE,))
        seed_batch(conn, "mixed", (("ordinary", 0.0, OLD), ("secret-task", 1.0, NEW)))
        seed_batch(conn, "private-release", dataset_release_id=HOLDOUT)
        seed_batch(conn, "only-sensitive", (("secret-task", 1.0, OLD),), finished_at=OLD)
        seed_batch(conn, "other-project", project_id="private-team", owner_id=user_id)
        seed_batch(conn, "wrong-owner", project_id=f"personal:{user_id}", owner_id="wrong")
        seed_batch(conn, "withdrawn", batch_status="withdrawn")
        seed_batch(conn, "approval-revoked", evidence_approval="revoked")

    restricted = ordinary.get(PATH).json()
    assert restricted["summary"]["published_run_count"] == 1
    assert restricted["releases"][0]["registered_task_count"] == 1
    batch = restricted["cells"][0]["latest_scored_batch"]
    assert (batch["batch_id"], batch["score"], batch["scored"], batch["recorded"]) == ("mixed", 0, 1, 1)
    assert batch["recorded_at"] is batch["recorded_at_source"] is batch["planned"] is None
    assert restricted["releases"][0]["dataset_release_id"] == RELEASE

    allowed = scoped.get(PATH).json()
    assert allowed["summary"]["published_run_count"] == 3
    assert {r["dataset_release_id"] for r in allowed["releases"]} == {RELEASE, HOLDOUT}
    assert allowed["groups"]["dataset"][0]["registered_task_count"] == 29
    full = cell_by_display_batch(allowed, "mixed")["latest_scored_batch"]
    assert (full["score"], full["scored"], full["recorded_at"], full["recorded_at_source"]) == (50, 2, NEW, "batch_finished_at")
    assert outside.get(PATH).json()["cells"] == []
