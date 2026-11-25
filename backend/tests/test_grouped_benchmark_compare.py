from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import Database
from app.main import create_app
from tests.conftest import create_user, login_client


BENCHMARK = "grouped-fixture"
RELEASE = "grouped-fixture__1__default"
HOLDOUT = "grouped-fixture__1__holdout"
NEXT = "grouped-fixture__2__default"
PROTOCOL = "gaia-official-scorer"
NOW = "2026-11-25T09:00:00Z"


def _insert_batch(
    db: Database,
    bid: str,
    tasks: dict[str, tuple[float | None, str]],
    *,
    harness: str = "codex",
    project: str = "default",
    release: str = RELEASE,
    protocol: str = PROTOCOL,
    route: str = "vllm/fixture-model",
    status: str = "finished",
    indexed_at: str = NOW,
    placeholder: int = 0,
) -> None:
    values = [reward for reward, _ in tasks.values() if reward is not None]
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO batches (
                batch_id, source_locator, source_namespace, batch_name, project_id, benchmark_slug,
                dataset_release_id, generation_id, run_scope, content_digest, indexed_at, model_canonical,
                model_route, harness, harness_version, scoring_protocol, recorded, scored, valid_mean,
                batch_status, unique_tasks, is_running_placeholder, run_config_json
            ) VALUES (?, ?, 'harbor_runs', ?, ?, ?, ?, 'gen_old', 'subset', 'fixture', ?, 'fixture-model',
                      ?, ?, '1.0', ?, ?, ?, ?, ?, ?, ?, '{"attempts":2,"timeout_seconds":60}')
            """,
            (
                bid, f"harbor_runs:{bid}", bid, project, BENCHMARK, release, indexed_at, route, harness, protocol,
                len(tasks), len(values), sum(values) / len(values) if values else None, status, len(tasks), placeholder,
            ),
        )
        for task, (reward, checksum) in tasks.items():
            key = f"{bid}-{task}"
            status = "failed" if reward is None else "scored"
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum, status, reward, reward_valid
                ) VALUES (?, ?, ?, 'gen_old', ?, ?, ?, ?, ?)
                """,
                (bid, key, key, task, checksum, status, reward, reward is not None),
            )
            conn.execute(
                """
                INSERT INTO trial_protocol_scores (
                    canonical_trial_key, scoring_protocol, batch_id, trial_id, project_id, source_namespace,
                    dataset_release_id, task_id, status, reward, reward_valid
                ) VALUES (?, ?, ?, ?, ?, 'harbor_runs', ?, ?, ?, ?, ?)
                """,
                (key, protocol, bid, key, project, release, task, status, reward, reward is not None),
            )


@pytest.fixture()
def case(isolated_settings: Settings):
    isolated_settings.config_dir = Path(__file__).resolve().parents[2] / "config"
    app = create_app(isolated_settings, run_startup_scan=False)
    db = app.state.ctx.db
    with db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, capability, status, updated_at)
            VALUES (?, ?, 'agentic', 'Agentic', 'fixture', 'integrated', ?)
            """,
            [(BENCHMARK, "Grouped fixture", NOW), ("grouped-empty", "Empty fixture", NOW)],
        )
        for rid, ver, split in ((RELEASE, "1", "default"), (NEXT, "2", "default"), (HOLDOUT, "1", "holdout")):
            conn.execute(
                """
                INSERT INTO dataset_releases (
                    dataset_release_id, benchmark_slug, dataset_version, split, runner, known_task_count,
                    default_protocol, scale_type, updated_at
                ) VALUES (?, ?, ?, ?, 'harbor', 20, ?, 'pct_0_100', ?)
                """,
                (rid, BENCHMARK, ver, split, PROTOCOL, NOW),
            )
        conn.execute(
            "INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES (?, 'hidden-task', 'holdout')",
            (RELEASE,),
        )
        for metric, model_col, score in (
            ("accuracy", "fixture-column-a", 71.25), ("accuracy", "fixture-column-b", 72.5),
            ("alternate", "fixture-column-a", 10.0),
        ):
            conn.execute(
                """
                INSERT INTO legacy_leaderboard_rows (
                    benchmark, metric, benchmark_slug, dataset_release_id, model_col, model_canonical,
                    raw_score, norm_score_100, scale_type, source_file
                ) VALUES ('fixture', ?, ?, ?, ?, 'fixture-model', ?, ?, 'pct_0_100', 'legacy/leaderboard.csv')
                """,
                (metric, BENCHMARK, RELEASE, model_col, score, score),
            )

    password = "IsolatedFixturePassword1!"
    uid = create_user(db, username="researcher", password=password, role="researcher", projects=["default", "team-shared"])
    client = TestClient(app)
    csrf = login_client(client, "researcher", password)
    return app, db, client, {"X-CSRF-Token": csrf}, uid


def _payload(*batch_ids: str) -> dict:
    return {
        "subjects": [
            {"id": bid, "kind": "batch", "batch_id": bid, "expected_generation_id": "gen_old"}
            for bid in batch_ids
        ],
        "scope": {"dataset_release_ids": [RELEASE]},
        "scoring_protocol": PROTOCOL,
        "baseline_subject_id": batch_ids[0],
        "include_verdict": False,
    }


def test_grouped_profile_and_cross_dataset_matrix_use_exact_authorized_batches(case) -> None:
    _, db, client, _, uid = case
    _insert_batch(db, "a_current", {"ordinary": (1, "same"), "fault": (None, "same")})
    _insert_batch(db, "a_unscored", {"fault": (None, "same")}, indexed_at="2026-11-25T10:00:00Z")
    _insert_batch(db, "b_variant", {"ordinary": (0, "same")}, harness="pi")
    _insert_batch(db, "c_unknown", {"ordinary": (1, "same")}, harness="harbor")
    _insert_batch(db, "d_next", {"ordinary": (1, "same")}, release=NEXT, route="openai/fixture-model")
    _insert_batch(db, "e_other_protocol", {"ordinary": (1, "same")}, protocol="other-protocol")
    _insert_batch(db, "f_holdout", {"private": (1, "same")}, release=HOLDOUT)
    _insert_batch(db, "g_other_team", {"ordinary": (1, "same")}, project="team-other")
    _insert_batch(db, "h_placeholder", {}, placeholder=1, status="running")
    _insert_batch(db, "i_shared", {"ordinary": (1, "same")}, project="team-shared", harness="pi")

    result = client.get(f"/api/v1/benchmarks/{BENCHMARK}").json()
    grouped = result["grouped_comparison"]
    assert grouped["empty_reason"] is None
    contexts = {(c["dataset_release_id"], c["scoring_protocol"], c["metric_name"]): c for c in grouped["contexts"]}
    trial_context = contexts[(RELEASE, PROTOCOL, "reward")]
    by_batch = {b["batch_id"]: (g, b) for g in trial_context["groups"] for b in g["batches"]}
    assert set(by_batch) == {"a_current", "a_unscored", "b_variant", "c_unknown", "i_shared"}
    group, batch = by_batch["a_current"]
    assert group["default_batch_id"] == "a_current"
    assert [b["batch_id"] for b in group["batches"]] == ["a_unscored", "a_current"]
    assert batch["generation_id"] == "gen_old" and batch["has_visible_task_scores"]
    assert (batch["attempts_per_task"], batch["timeout_seconds"]) == (2, 60)
    assert by_batch["c_unknown"][0]["harness_key"] is None
    assert by_batch["c_unknown"][0]["runner"] == "harbor"
    assert (NEXT, PROTOCOL, "reward") in contexts and (RELEASE, "other-protocol", "reward") in contexts
    held = contexts[(HOLDOUT, PROTOCOL, "reward")]["groups"][0]["batches"][0]
    assert not held["task_details_allowed"] and not held["has_visible_task_scores"]
    assert held["scored"] == 1
    accuracy = contexts[(RELEASE, "legacy-20261120-csv", "accuracy")]
    assert accuracy["kind"] == "historical_summary" and len(accuracy["groups"]) == 2
    assert all(
        not g["batches"] and g["historical_scores"][0]["denominator"] is None
        for g in accuracy["groups"]
    )
    assert (RELEASE, "legacy-20261120-csv", "alternate") in contexts
    assert "private" not in json.dumps(grouped)

    harness = client.get("/api/v1/harnesses/codex").json()
    model = client.get("/api/v1/models/fixture-model").json()
    h_rows = {r["batch_id"]: r for r in harness["dataset_performance"]}
    m_rows = {r["batch_id"]: r for r in model["dataset_performance"]}
    left, right = h_rows["a_current"], h_rows["d_next"]
    assert left == m_rows["a_current"] and right == m_rows["d_next"]
    assert left["model_key"] == right["model_key"] and left["model_route"] != right["model_route"]
    assert (left["dataset_version"], right["dataset_version"], right["split"]) == ("1", "2", "default")
    assert (left["scored"], left["recorded"], left["known_task_count"], left["denominator"]) == (1, 2, 20, 1)
    assert left["score"] == 100 and left["score_kind"] == "trial_valid_mean"
    assert h_rows["a_unscored"]["score"] is None and h_rows["a_unscored"]["denominator"] is None
    assert "g_other_team" not in m_rows
    assert harness["score_matrix"][0]["metric_name"] == "reward"
    assert model["harness_matrix"][0]["dataset_version"]
    assert client.get("/api/v1/benchmarks/grouped-empty").json()["grouped_comparison"] == {
        "contexts": [], "empty_reason": "no_authorized_results",
    }
    with db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = '[\"default\"]' WHERE user_id = ?", (uid,))
    assert "i_shared" not in client.get(f"/api/v1/benchmarks/{BENCHMARK}").text


def test_grouped_n_way_and_scoped_legacy_totals_keep_their_denominators(case) -> None:
    _, db, client, headers, uid = case
    shared = {"ordinary": (1, "same"), "hidden-task": (1, "same"), "fault": (1, "same"), "drift": (1, "same")}
    _insert_batch(db, "a", {**shared, "pair-only": (1, "same")})
    _insert_batch(db, "b", {**shared, "ordinary": (0, "same"), "pair-only": (1, "same")}, harness="pi")
    _insert_batch(
        db, "c", {**shared, "fault": (None, "same"), "drift": (1, "changed")},
        harness="opencode", project="team-shared",
    )
    resp = client.post("/api/v1/compare/query", json=_payload("a", "b", "c"), headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    row = result["rows"][0]
    assert row["intersection"] == {
        "common_covered_tasks": 3, "common_valid_scored_tasks": 2, "fault_excluded_tasks": 1,
    }
    assert row["version_drift"]["drifted_task_count"] == 1
    assert row["cells"]["b"]["common_valid"]["score"] == 50
    assert row["cells"]["b"]["common_covered_faults_as_zero"]["score"] == 66.67
    assert row["cells"]["b"]["self_full_batch_valid"]["score"] == 80
    diff = client.post(
        "/api/v1/compare/task-diff",
        json={"comparison_id": result["comparison_id"], "subject_a_id": "a", "subject_b_id": "b"},
        headers=headers,
    ).json()
    assert [item["task_id"] for item in diff["items"]] == ["ordinary"]
    assert diff["hidden_holdout_tasks"] == 1

    saved = client.post(
        "/api/v1/compare/saved",
        json={"comparison_id": result["comparison_id"], "name": "Group", "mode": "snapshot", "project_id": "default"},
        headers=headers,
    ).json()
    url = f"/api/v1/compare/s/{saved['short_id']}"
    assert client.get(url).json()["result"]["rows"][0]["intersection"] == row["intersection"]
    with db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = '[\"default\"]' WHERE user_id = ?", (uid,))
    assert client.get(url).status_code == 403

    legacy = client.post(
        "/api/v1/compare/query",
        json={
            "subjects": [
                {"id": "legacy-a", "kind": "legacy_model", "model_cols": ["fixture-column-a"]},
                {"id": "legacy-b", "kind": "legacy_model", "model_cols": ["fixture-column-b"]},
            ],
            "scope": {"dataset_release_ids": [RELEASE], "legacy_metric": "accuracy", "legacy_scale_type": "pct_0_100"},
            "include_verdict": False,
        },
        headers=headers,
    )
    assert legacy.status_code == 200, legacy.text
    legacy_result = legacy.json()
    assert len(legacy_result["rows"]) == 1
    cells = legacy_result["rows"][0]["cells"]
    assert cells["legacy-a"]["common_valid"]["score"] == 71.25
    assert cells["legacy-a"]["common_valid"]["denominator"] is None
    assert cells["legacy-b"]["common_valid"]["delta_pp"] == 1.25
    outside = client.post(
        "/api/v1/compare/task-diff",
        json={"comparison_id": legacy_result["comparison_id"], "dataset_release_id": NEXT},
        headers=headers,
    )
    assert outside.status_code == 400 and outside.json()["error"]["code"] == "INCOMPARABLE_PROTOCOL"


def test_expected_generation_rejects_stale_selection_and_guards_read_snapshot(case, monkeypatch) -> None:
    app, db, client, headers, _ = case
    _insert_batch(db, "a", {"ordinary": (1, "same")})
    _insert_batch(db, "b", {"ordinary": (0, "same")}, harness="pi")
    compare = app.state.ctx.compare
    original = compare._load_authorized_batch
    published = False

    def publish_during_query(user, bid, *, conn=None):
        nonlocal published
        old = original(user, bid, conn=conn)
        if bid == "b" and not published:
            with db.transaction() as writer:
                writer.execute("UPDATE batches SET generation_id = 'gen_new' WHERE batch_id = 'b'")
                writer.execute("UPDATE episodes SET generation_id = 'gen_new', reward = 1 WHERE batch_id = 'b'")
            published = True
        return old

    with monkeypatch.context() as patch:
        patch.setattr(compare, "_load_authorized_batch", publish_during_query)
        old = client.post("/api/v1/compare/query", json=_payload("a", "b"), headers=headers).json()
    assert published
    assert old["rows"][0]["cells"]["b"]["common_valid"]["score"] == 0
    assert old["bound_snapshot"]["batches"][1]["generation_id"] == "gen_old"
    stale = client.post("/api/v1/compare/query", json=_payload("a", "b"), headers=headers)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "GENERATION_MISMATCH"
    live = client.post(
        "/api/v1/compare/saved",
        json={"comparison_id": old["comparison_id"], "name": "Group live", "mode": "live", "project_id": "default"},
        headers=headers,
    ).json()
    reopened = client.get(f"/api/v1/compare/s/{live['short_id']}").json()["result"]
    assert reopened["rows"][0]["cells"]["b"]["common_valid"]["score"] == 100
    assert reopened["bound_snapshot"]["batches"][1]["generation_id"] == "gen_new"

    old_caller = _payload("a", "b")
    for subject in old_caller["subjects"]:
        subject.pop("expected_generation_id")
    new = client.post("/api/v1/compare/query", json=old_caller, headers=headers).json()
    assert new["rows"][0]["cells"]["b"]["common_valid"]["score"] == 100
