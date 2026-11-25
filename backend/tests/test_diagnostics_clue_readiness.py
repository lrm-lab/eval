from __future__ import annotations

import json
import os
from errno import EIO
from pathlib import Path
from shutil import copyfile

import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.diagnostics_service import DiagnosticsService
from tests.conftest import create_user, login_client


def test_clues_retry_gateway_and_reuse_same_generation_summaries(
    tmp_path: Path, isolated_settings: Settings, monkeypatch,
) -> None:
    settings = isolated_settings
    taxonomy = settings.taxonomy_file
    settings.config_dir = tmp_path / "config"
    settings.config_dir.mkdir()
    copyfile(taxonomy, settings.taxonomy_file)
    settings.tianxi_root = tmp_path / "shared-source"
    batch_dir = settings.harbor_runs_dir / "trusted-clues"
    batch_dir.mkdir(parents=True)
    settings.source_approval_file.write_text(yaml.safe_dump({"sources": [{
        "source_root": str(batch_dir),
        "project_id": "team-clues",
        "approval_status": "approved_restricted",
        "approved_evidence_classes": ["atif_trajectory", "gateway_capture"],
    }]}), encoding="utf-8")
    app = create_app(settings, run_startup_scan=False)
    ctx = app.state.ctx
    paths = []
    atif_paths = []
    with ctx.db.transaction() as conn:
        conn.execute("""
            INSERT INTO batches (batch_id, source_locator, source_namespace, batch_name, project_id, evidence_approval,
                benchmark_slug, dataset_release_id, generation_id, run_scope, content_digest, indexed_at, has_gateway)
            VALUES ('b_clues', 'harbor_runs:trusted-clues', 'harbor_runs', 'trusted-clues', 'team-clues',
                'approved_restricted', 'deepswe', 'deepswe__1.1__default', 'gen_test', 'subset', 'digest', '2026-11-25', 1)
        """)
        conn.execute("""
            INSERT INTO task_definitions (dataset_release_id, task_id, split)
            VALUES ('deepswe__1.1__default', 'task-held', 'holdout')
        """)
        conn.execute("""
            INSERT INTO cold_file_cache (file_path, cache_kind, mtime_ns, size_bytes, item_count, status, error_message, updated_at)
            VALUES ('trial_enrich:b_clues', 'trial_enrich', 0, 0, 2, 'ready', 'digest:gateway-clue-v1', '2026-11-25')
        """)
        for task in ("task-visible", "task-held"):
            path = batch_dir / f"{task}.json"
            path.write_text('{"turns":[{"discarded":true}]}', encoding="utf-8")
            paths.append(path)
            atif_path = batch_dir / f"{task}-atif.json"
            atif_path.write_text(json.dumps({"steps": [
                {"tool_calls": [{"id": str(i), "name": "read", "arguments": {"page": 1}}]}
                for i in range(3)
            ]}), encoding="utf-8")
            atif_paths.append(atif_path)
            st = path.stat()
            refs = {"gateway": {
                "rel_path": path.name,
                "generation_id": "gen_test",
                "sha256": None,
                "pending_file_identity": [st.st_ino, st.st_mtime_ns, st.st_size],
            }, "trajectory": {"rel_path": atif_path.name, "generation_id": "gen_test"}}
            conn.execute("""
                INSERT INTO episodes (batch_id, trial_id, canonical_trial_key, generation_id, task_id, status, evidence_refs_json)
                VALUES ('b_clues', ?, ?, 'gen_test', ?, 'scored', ?)
            """, (f"trial-{task}", task, task, json.dumps(refs)))

    def client(name: str, projects: list[str], scopes: list[str]) -> TestClient:
        password = "IsolatedDiagFixture!26"
        create_user(ctx.db, username=name, password=password, role="viewer", projects=projects, scopes=scopes)
        c = TestClient(app)
        login_client(c, name, password)
        return c

    authorized = client("authorized", ["team-clues"], ["gateway:read", "trace:read"])
    holdout = client("holdout", ["team-clues"], ["gateway:read", "trace:read", "dataset:holdout_read"])
    gateway_only = client("gateway_only", ["team-clues"], ["gateway:read"])
    no_scope = client("no_scope", ["team-clues"], [])
    wrong_project = client("wrong_project", ["other"], ["gateway:read"])
    url = "/api/v1/diagnostics/clues?batch_id=b_clues"

    def result(c: TestClient) -> tuple[dict, dict]:
        response = c.get(url)
        assert response.status_code == 200
        payload = response.json()
        gateway = next(clue for clue in payload["clues"] if clue["code"] == "gateway_discarded_or_msg_drop")
        return payload["evidence_coverage"]["gateway"], gateway

    original_open = Path.open

    def skip_evidence_bodies(path: Path, *args, **kwargs):
        assert path not in paths + atif_paths, "the request should not read pending or indexed evidence"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", skip_evidence_bodies)
        coverage, gateway = result(authorized)
        assert coverage == {"verified_trials": 0, "pending_trials": 1, "unavailable_trials": 0}
        assert gateway["applicable_denominator"] == gateway["hit_count"] == 0
        assert result(holdout)[0]["pending_trials"] == 2
        assert result(no_scope)[0]["pending_trials"] == result(wrong_project)[0]["pending_trials"] == 0

    def fail_one_gateway_read(path: Path, *args, **kwargs):
        if path == paths[0]:
            raise OSError(EIO, "transient read failure")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_one_gateway_read)
        assert ctx.coordinator.hot.enrich_harbor_trials(batch_ids=["b_clues"])["trials"] == 2
    assert ctx.db.fetchone("SELECT status FROM cold_file_cache WHERE file_path = 'trial_enrich:b_clues'")["status"] == "pending"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", skip_evidence_bodies)
        assert result(authorized)[0]["pending_trials"] == 1
        recovered = result(holdout)[0]
        assert recovered == {"verified_trials": 1, "pending_trials": 1, "unavailable_trials": 0}
    def skip_recovered_gateway_body(path: Path, *args, **kwargs):
        assert path != paths[1], "retry should reuse the already-verified gateway summary"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", skip_recovered_gateway_body)
        assert ctx.coordinator.hot.enrich_harbor_trials(batch_ids=["b_clues"])["trials"] == 2
    assert ctx.db.fetchone("SELECT status FROM cold_file_cache WHERE file_path = 'trial_enrich:b_clues'")["status"] == "ready"
    assert ctx.coordinator.hot.enrich_harbor_trials(batch_ids=["b_clues"])["trials"] == 0
    # A fresh service with an empty process cache should reuse both stored summaries.
    ctx.diagnostics = DiagnosticsService(settings, ctx.db)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", skip_evidence_bodies)
        coverage, gateway = result(authorized)
        assert coverage == {"verified_trials": 1, "pending_trials": 0, "unavailable_trials": 0}
        assert gateway["applicable_denominator"] == gateway["hit_count"] == 1
        assert [item["task_id"] for item in gateway["trials"]] == ["task-visible"]
        assert result(holdout)[0]["verified_trials"] == 2
        assert result(no_scope)[0]["verified_trials"] == result(wrong_project)[0]["verified_trials"] == 0
        for c, expected in ((authorized, 1), (holdout, 2), (gateway_only, 0), (wrong_project, 0)):
            response = c.get(url)
            assert response.status_code == 200
            payload = response.json()
            assert payload["evidence_coverage"]["atif"]["verified_trials"] == expected
            repeated = next(clue for clue in payload["clues"] if clue["code"] == "repeated_identical_tool_call")
            assert repeated["hit_count"] == expected
        stricter = authorized.get(f"{url}&min_repeat=4").json()
        repeated = next(clue for clue in stricter["clues"] if clue["code"] == "repeated_identical_tool_call")
        assert repeated["hit_count"] == 0

    atif_visible = atif_paths[0]
    original = atif_visible.read_bytes()
    atif_stat = atif_visible.stat()
    atif_visible.write_bytes(original.replace(b'"page": 1', b'"page": 2'))
    os.utime(atif_visible, ns=(atif_stat.st_atime_ns, atif_stat.st_mtime_ns))
    assert authorized.get(url).status_code == 409
    assert gateway_only.get(url).status_code == 200
    atif_visible.write_bytes(original)

    visible = paths[0]
    st = visible.stat()
    visible.write_text('{"turns":[{"discarded":null}]}', encoding="utf-8")
    os.utime(visible, ns=(st.st_atime_ns, st.st_mtime_ns))
    response = authorized.get(url)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GENERATION_MISMATCH"
