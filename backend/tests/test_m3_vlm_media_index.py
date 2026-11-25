import base64
import hashlib
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.indexing.identity import make_batch_id
from app.main import create_app
from app.security.authz import ALL_SCOPES
from tests.conftest import create_user, login_client


def test_legacy_vlm_media_indexed_and_backfilled_on_hot_scan(tmp_path, isolated_settings):
    cfg = isolated_settings
    cfg.tianxi_root = tmp_path / "external"
    cfg.dataset_root = tmp_path / "datasets"
    cfg.config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[2] / "config", cfg.config_dir)
    batch = cfg.harbor_runs_dir / "ai2d-vlmkit-legacy"
    batch.mkdir(parents=True)
    cfg.source_approval_file.write_text(
        json.dumps({
            "sources": [{
                "source_root": str(batch),
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto", "direct_vlm_qa"],
            }],
        }),
        encoding="utf-8",
    )
    (batch / "manifest.json").write_text(
        json.dumps({"dataset": "ai2d", "harness": "vlmevalkit", "runner": "vlmevalkit"}),
        encoding="utf-8",
    )

    image = b"\xff\xd8\xff\xe0test JPEG bytes\xff\xd9"
    image_b64 = base64.b64encode(image).decode("ascii")
    rows = []
    trials = {
        "file": "11111111-1111-4111-8111-111111111111",
        "inline": "22222222-2222-4222-8222-222222222222",
        "none": "33333333-3333-4333-8333-333333333333",
    }
    for kind, trial_id in trials.items():
        trial = batch / "trials" / trial_id
        trial.mkdir(parents=True)
        result = {"vlmeval_result": {"http_status": 200, "sample_id": kind}}
        if kind == "file":
            (trial / "media").mkdir()
            (trial / "media" / "step-1-image-1.jpg").write_bytes(image)
        elif kind == "inline":
            result["vlmeval_result"]["request_messages"] = [{"image_url": f"data:image/jpeg;base64,{image_b64}"}]
        (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (trial / "trajectory.json").write_text(
            json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "Test trajectory"}]}),
            encoding="utf-8",
        )
        rows.append({
            "trial_id": trial_id,
            "task_id": kind,
            "runner": "vlmevalkit",
            "status": "scored",
            "reward": 1,
            "result": f"trials/{trial_id}/result.json",
            "trajectory": f"trials/{trial_id}/trajectory.json",
        })
    (batch / "episodes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    app = create_app(cfg, run_startup_scan=False)
    ctx = app.state.ctx
    hot = ctx.coordinator.hot
    batch_id = make_batch_id(f"harbor_runs:{batch.name}")

    def check_media():
        db_rows = ctx.db.fetchall(
            "SELECT task_id, has_inline_media, evidence_refs_json FROM episodes WHERE batch_id = ?",
            (batch_id,),
        )
        assert {row["task_id"]: row["has_inline_media"] for row in db_rows} == {"file": 1, "inline": 1, "none": 0}
        refs = {row["task_id"]: json.loads(row["evidence_refs_json"]).get("media", []) for row in db_rows}
        assert refs["none"] == []
        assert refs["file"][0]["source"] == "trial_media_dir"
        assert refs["file"][0]["sha256"] == hashlib.sha256(image).hexdigest()
        assert refs["inline"][0]["source"] == "result_inline_base64"
        assert refs["inline"][0]["sha256"] == hashlib.sha256(image_b64.encode("ascii")).hexdigest()

    assert hot.run_hot_scan()["batches"] == {"indexed": 1}
    check_media()
    previous = ctx.db.fetchone("SELECT generation_id, content_digest FROM batches WHERE batch_id = ?", (batch_id,))

    # An older shallow reindex could erase episode media while leaving the cold cache ready.
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE batches SET checks_json = json_remove(checks_json, '$.legacy_vlm_media_indexed') WHERE batch_id = ?",
            (batch_id,),
        )
        conn.execute(
            """
            UPDATE episodes
            SET has_inline_media = 0, evidence_refs_json = json_remove(evidence_refs_json, '$.media')
            WHERE batch_id = ?
            """,
            (batch_id,),
        )
        conn.execute(
            """
            INSERT INTO cold_file_cache (file_path, cache_kind, mtime_ns, size_bytes, item_count, status, error_message, updated_at)
            VALUES (?, 'trial_enrich', 0, 0, 3, 'ready', ?, CURRENT_TIMESTAMP)
            """,
            (f"trial_enrich:{batch_id}", previous["content_digest"]),
        )

    assert hot.run_hot_scan()["batches"] == {"indexed": 1}
    check_media()
    assert hot.run_hot_scan()["batches"] == {"skipped_unchanged": 1}
    assert hot.run_hot_scan(force=True)["batches"] == {"indexed": 1}
    check_media()
    assert ctx.db.fetchone("SELECT generation_id, content_digest FROM batches WHERE batch_id = ?", (batch_id,)) == previous

    password = "LocalMediaTest!2026"
    create_user(ctx.db, username="media-admin", password=password, role="admin", scopes=list(ALL_SCOPES))
    with TestClient(app) as client:
        login_client(client, "media-admin", password)
        url = f"/api/v1/batches/{batch_id}/trials/{trials['file']}"
        detail = client.get(url)
        assert detail.status_code == 200
        assert detail.json()["has_inline_media"] is True
        assert detail.json()["qa"]["media_count"] == 1
        media = client.get(f"{url}/media/0")
        assert media.status_code == 200 and media.headers["content-type"] == "image/jpeg"
        assert media.content == image
        trajectory = client.get(f"{url}/trajectory")
        assert trajectory.status_code == 403 and trajectory.json()["error"]["code"] == "SCOPE_FORBIDDEN"
