import json
import shutil
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.database import Database
from app.indexing.hot_indexer import write_runner_submission
from app.indexing.identity import make_batch_id
from app.main import create_app
from tests.conftest import create_user, login_client


def submission(owner="researcher", *, batch_name="team-run"):
    return {
        "batch_name": batch_name, "batch_id": make_batch_id(f"runner_published:{batch_name}"),
        "source_namespace": "runner_published", "evidence_approval": "approved_restricted",
        "owner_id": owner, "project_id": "science",
    }


def assert_binding(db: Database, bid: str, project: str, owner: str, approval: str):
    assert db.fetchone(
        "SELECT project_id, owner_id, evidence_approval FROM batches WHERE batch_id=?", (bid,)
    ) == {"project_id": project, "owner_id": owner, "evidence_approval": approval}
    for sql in (
        "SELECT DISTINCT project_id FROM trial_protocol_scores WHERE batch_id=?",
        "SELECT DISTINCT project_id FROM search_fts WHERE entity_type='batch' AND entity_id=?",
    ):
        assert db.fetchall(sql, (bid,)) == [{"project_id": project}]


def test_runner_and_external_bindings(tmp_path, isolated_settings):
    cfg = isolated_settings
    cfg.config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[2] / "config", cfg.config_dir)
    cfg.tianxi_root = tmp_path / "external"
    cfg.dataset_root = tmp_path / "datasets"
    cfg.harbor_runs_dir.mkdir(parents=True)
    app = create_app(cfg, run_startup_scan=False)
    ctx = app.state.ctx
    pw = "ResearchTest!2026"
    owner = create_user(ctx.db, username="researcher", password=pw, projects=["science"])
    create_user(ctx.db, username="admin", password=pw, role="admin")
    doc = submission(owner, batch_name="team..run")
    bid = doc["batch_id"]
    write_runner_submission(cfg.state_dir, doc)
    batch = cfg.published_batches_dir / doc["batch_name"]
    batch.mkdir(parents=True)
    (batch / "manifest.json").write_text(json.dumps({"dataset": "deepswe-1.1", "harness": "pi"}))
    (batch / "episodes.jsonl").write_text(json.dumps({
        "trial_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "task_id": "t1", "status": "scored", "reward": 1,
    }) + "\n")
    external = cfg.harbor_runs_dir / batch.name
    shutil.copytree(batch, external)
    ext_id = make_batch_id(f"harbor_runs:{batch.name}")
    ctx.coordinator.run_hot_now(force=False)
    assert_binding(ctx.db, bid, "science", owner, "approved_restricted")
    assert_binding(ctx.db, ext_id, "default", "system", "pending")

    # Simulate a pre-fix database with unchanged DTO bytes but a stale project.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE batches SET project_id='default' WHERE batch_id=?", (bid,))
    ctx.coordinator.run_hot_now(force=False)
    assert_binding(ctx.db, bid, "science", owner, "approved_restricted")

    with TestClient(app) as admin, TestClient(app) as member:
        headers = {"X-CSRF-Token": login_client(admin, "admin", pw)}
        login_client(member, "researcher", pw)
        cfg.source_approval_file.write_text(json.dumps({"sources": [{
            "source_root": str(external), "project_id": "external", "owner_id": "curator",
            "approval_status": "approved_public",
        }]}))
        for endpoint in (f"/admin/reindex/{bid}", "/admin/reindex?force=true"):
            r = admin.post("/api/v1" + endpoint, headers=headers)
            assert r.status_code == 200, r.text
            assert_binding(ctx.db, bid, "science", owner, "approved_restricted")
        assert_binding(ctx.db, ext_id, "external", "curator", "approved_public")
        url = f"/api/v1/batches/{bid}"
        assert member.get(url).status_code == 200
        with ctx.db.transaction() as conn:
            conn.execute("UPDATE users SET projects_json='[]' WHERE user_id=?", (owner,))
        denied = member.get(url)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "PROJECT_FORBIDDEN"

    fresh = create_app(replace(cfg, db_path=cfg.state_dir / "fresh.db"), run_startup_scan=True)
    with TestClient(fresh):
        assert_binding(fresh.state.ctx.db, bid, "science", owner, "approved_restricted")


@pytest.mark.parametrize("unsafe", ["directory_mode", "file_mode", "directory_link", "file_link", "wrong_batch"])
def test_private_submission_boundary(tmp_path, isolated_settings, unsafe):
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    state = isolated_settings.state_dir
    doc = submission()
    write_runner_submission(state, doc)
    ledger = state / "runner-submissions"
    path = ledger / "team-run.json"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if unsafe == "directory_mode":
        ledger.chmod(0o755)
    elif unsafe == "file_mode":
        path.chmod(0o644)
    elif unsafe in {"directory_link", "file_link"}:
        target = ledger if unsafe == "directory_link" else path
        target.rename(tmp_path / "original")
        target.symlink_to(tmp_path / "original")
    else:
        path.write_text(json.dumps(dict(doc, batch_id="different")))
    if unsafe != "wrong_batch":
        with pytest.raises(OSError):
            write_runner_submission(state, doc)
    with pytest.raises((OSError, ValueError)):
        ctx.coordinator.hot._resolve_runner_published_ownership(
            doc["batch_name"], doc["batch_id"], "runner_published:team-run",
        )
