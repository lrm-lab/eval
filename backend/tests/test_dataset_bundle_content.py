from __future__ import annotations

import io
import json
import shutil
import tarfile
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import create_user, login_client


def test_existing_bundle_cache_upgrades_public_tasks_for_viewers(
    isolated_settings: Settings, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(isolated_settings.taxonomy_file, config_dir / "taxonomy.yaml")
    settings = replace(isolated_settings, config_dir=config_dir, tianxi_root=tmp_path / "tianxi")
    settings.bundles_dir.mkdir(parents=True)
    archive = settings.bundles_dir / "gaia-validation-20261124.tar"
    task_id = "gaia/sample-id"
    members = {
        f"{task_id}/task.toml": b'[task]\nname = "gaia/sample-id"\n',
        f"{task_id}/instruction.md": b"# Question\nRead the provided attachment.",
        f"{task_id}/environment/workspace/sample-id.txt": b"original task material",
        f"{task_id}/tests/expected_answer.txt": b"private test answer",
    }
    with tarfile.open(archive, "w") as bundle:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    settings.source_approval_file.write_text(json.dumps({
        "sources": [{
            "source_root": str(archive),
            "approval_status": "approved_public",
            "approved_evidence_classes": ["public_task_metadata", "public_task_attachments"],
        }],
    }))
    app = create_app(settings, run_startup_scan=False)
    ctx = app.state.ctx
    with ctx.db.transaction() as conn:
        ctx.coordinator.cold._upsert_cache(
            conn, str(archive), "bundle_tar", archive.stat().st_mtime_ns, archive.stat().st_size, 1
        )
    assert ctx.coordinator.cold.index_bundles_metadata() == 1
    assert ctx.coordinator.cold.index_bundles_metadata() == 0

    api = "/api/v1/datasets/gaia__2026-11-24__val"
    client = TestClient(app)
    password = "ViewerForBundles!1"
    create_user(ctx.db, username="bundle_viewer", password=password, projects=["other-team"])
    login_client(client, "bundle_viewer", password)

    response = client.get(f"{api}/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json()["instruction"] == "# Question\nRead the provided attachment."
    assert response.json()["instruction_status"] == "ready"
    assert response.json()["attachments"] == ["sample-id.txt"]
    attachment = client.get(f"{api}/task-attachments/sample-id.txt", params={"task_id": task_id})
    assert attachment.content == b"original task material"
    assert attachment.headers["content-disposition"].startswith("attachment;")
    private = client.get(f"{api}/task-attachments/expected_answer.txt", params={"task_id": task_id})
    assert private.status_code == 404

    settings.source_approval_file.write_text("{}")
    ctx.query.approval_registry.reload()
    assert client.get(f"{api}/tasks/{task_id}").status_code == 403
