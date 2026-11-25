from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import public_baseline_service as baselines
from app.services.public_baseline_sources import SOURCES, PublicScore
from conftest import create_user, login_client


def _batch(db, *, batch_id, release, benchmark, protocol, harness, project="default", score=0.5):
    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO batches (
                batch_id, source_locator, source_namespace, batch_name, dataset_release_id, benchmark_slug,
                scoring_protocol, fault_policy, harness, unique_tasks, scored, valid_mean, project_id,
                run_scope, generation_id, content_digest, indexed_at
            ) VALUES (?, ?, 'harbor_runs', ?, ?, ?, ?, 'official_protocol', ?, 2, 2, ?, ?, 'full_split', 'gen', 'digest', 'now')
        """, (batch_id, f"fixture:{batch_id}", batch_id, release, benchmark, protocol, harness, score, project))


def _episodes(db, batch_id, task_ids):
    with db.transaction() as conn:
        for index, task_id in enumerate(task_ids):
            conn.execute("""
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, task_id, generation_id, status, reward, reward_valid
                ) VALUES (?, ?, ?, ?, 'gen', 'scored', ?, 1)
            """, (batch_id, str(index), f"{batch_id}:{index}", task_id, index))


def test_public_baseline_sync_alignment_and_default(isolated_settings, monkeypatch):
    isolated_settings.config_dir = Path(__file__).resolve().parents[2] / "config"
    isolated_settings.root_disk_min_free_gb = 0
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    user = "baseline_viewer"
    password = "BaselineFixtureTest123!"
    create_user(ctx.db, username=user, password=password)
    create_user(ctx.db, username="attach_qa", password=password, role="admin")
    tb, _, gaia = SOURCES
    tb_rows = [
        PublicScore("terminal-2.1:highest-other-harness", "Model X", "Other", "high", 92, 2, 2),
        PublicScore("terminal-2.1:a", "Model A", "Codex", "high", 80, 2, 2),
        PublicScore("terminal-2.1:b", "Model B", "Codex", "low", 80, 2, 2),
    ]
    tb = replace(tb, task_count=2, task_ids_sha256=hashlib.sha256(b"official_a\nofficial_b").hexdigest(),
                 fetch=lambda: (tb_rows, "2026-11-24T00:00:00+00:00"))
    gaia = replace(gaia, fetch=lambda: ([PublicScore("gaia-test:a", None, "Public Agent", None, 60, 301, 301)], None))
    monkeypatch.setattr(baselines, "SOURCES", (tb, gaia))
    monkeypatch.setattr(baselines, "SOURCES_BY_ID", {source.id: source for source in (tb, gaia)})
    for source in (tb, gaia):
        assert ctx.public_baselines.refresh(source)
    _batch(ctx.db, batch_id="val", release="gaia__2026-11-20__val", benchmark="gaia",
           protocol="gaia-official-scorer", harness="Public Agent")
    _batch(ctx.db, batch_id="bad_corpus", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol=tb.protocol, harness="Codex")
    _episodes(ctx.db, "bad_corpus", ["official_a", "unrelated_b"])
    _batch(ctx.db, batch_id="bad_protocol", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol="unrelated_protocol", harness="Codex")
    _batch(ctx.db, batch_id="bad_harness", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol=tb.protocol, harness="Another")
    _batch(ctx.db, batch_id="hidden", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol=tb.protocol, harness="Codex", project="other_project")
    _episodes(ctx.db, "hidden", ["official_a", "official_b"])
    _batch(ctx.db, batch_id="aligned", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol="harbor-native-reward-valid-v1", harness="Codex")
    _episodes(ctx.db, "aligned", ["official_a", "official_b"])
    _batch(ctx.db, batch_id="bad_n", release=tb.release_id, benchmark=tb.benchmark_slug,
           protocol=tb.protocol, harness="Codex")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE batches SET scored = 1 WHERE batch_id = 'bad_n'")
        conn.execute("UPDATE batches SET model_canonical = 'model-b' WHERE batch_id = 'aligned'")

    def fail():
        raise OSError("upstream unavailable")

    with TestClient(app) as admin:
        csrf = login_client(admin, "attach_qa", password)
        attached = admin.put(
            "/api/v1/toolkit/official-result/aligned",
            json={"dataset": tb.release_id, "metric": tb.protocol, "source": "fixture-scoring", "score": 0.5, "denominator": 2},
            headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
        )
        assert attached.status_code == 200, attached.text

    with TestClient(app) as client:
        catalog_path = "/api/v1/public-baselines"
        assert client.get(catalog_path).status_code == 401
        login_client(client, user, password)
        original = client.get(catalog_path).json()["sources"][0]
        assert original["status"] == "ready"
        assert original["release_id"] == tb.release_id
        assert not ctx.public_baselines.refresh(replace(tb, fetch=fail))
        restored = client.get(catalog_path).json()["sources"][0]
        assert restored["status"] == "stale"
        assert restored["rows"] == original["rows"]
        assert restored["synced_at"] == original["synced_at"]

        ordered_batches = ["val", "hidden", "bad_corpus", "bad_protocol", "bad_n", "bad_harness", "aligned"]
        params = [("batch_id", batch_id) for batch_id in ordered_batches]
        auto = client.get(f"{catalog_path}/default", params=params)
        assert auto.json() == {"baseline_id": "terminal-2.1:a", "mode": "comparable"}
        no_auto = client.get(f"{catalog_path}/default", params=params[:-1])
        assert no_auto.json() == {"baseline_id": "gaia-test:a", "mode": "reference"}
        strict = client.get(f"{catalog_path}/default", params=[("require_same_model", "true"), *params])
        assert strict.json() == {"baseline_id": "terminal-2.1:b", "mode": "comparable"}
        wrong_model = client.get(f"{catalog_path}/compare", params={
            "baseline_id": "terminal-2.1:a", "batch_id": "aligned", "require_same_model": "true",
        }).json()["items"][0]
        assert wrong_model["delta_pp"] is None
        assert "模型不同" in wrong_model["reasons"][0]
        gaia_test = client.get(f"{catalog_path}/compare", params={"baseline_id": "gaia-test:a", "batch_id": "val"})
        gaia_result = gaia_test.json()["items"][0]
        assert gaia_result["delta_pp"] is None
        assert "站内 val，公榜 test" in gaia_result["reasons"][0]

        comparison = client.get(f"{catalog_path}/compare", params=[("baseline_id", "terminal-2.1:a"), *params])
        assert comparison.status_code == 200, comparison.text
        items = {item["batch_id"]: item for item in comparison.json()["items"]}
        assert items["aligned"]["delta_pp"] == -30
        assert items["aligned"]["generation_id"] == "gen"
        assert not items["aligned"]["reasons"]
        assert all(items[key]["delta_pp"] is None for key in ordered_batches[:-1])
        assert "protocol" not in items["hidden"]
        assert "试题" in items["bad_corpus"]["reasons"][0]
        assert "评分协议" in items["bad_protocol"]["reasons"][0]
        assert "样本量" in items["bad_n"]["reasons"][0]
        assert "Harness" in items["bad_harness"]["reasons"][0]
