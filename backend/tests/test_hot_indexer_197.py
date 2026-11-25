from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, sync_taxonomy_to_db
from app.indexing.hot_indexer import HotIndexer
from app.indexing.identity import determine_run_scope, make_batch_id, make_collision_safe_slug
from app.security.auth import AuthUser
from app.security.authz import ALL_SCOPES
from app.security.storage_probe import probe_isolated_dir
from app.services.query_service import QueryService


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_hot_scan_defers_unbound_legacy_gateway_evidence(isolated_settings: Settings) -> None:
    isolated_settings.tianxi_root = isolated_settings.runtime_dir / "tianxi"
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    indexer = HotIndexer(isolated_settings, db)
    trial_id = "trial-local"
    relative = f"trials/{trial_id}/model-gateway.json"
    paths = {}
    for name in ("legacy-gateway-stable", "legacy-gateway-changed"):
        batch = isolated_settings.harbor_runs_dir / name
        trace = batch / relative
        trace.parent.mkdir(parents=True)
        trace.write_text('{"model_calls": 1, "turns": []}', encoding="utf-8")
        (batch / "manifest.json").write_text(
            json.dumps({"batch": name, "dataset": "GAIA", "model": "fixture", "runner": "harbor"}),
            encoding="utf-8",
        )
        (batch / "episodes.jsonl").write_text(
            json.dumps({"trial_id": trial_id, "task_id": name, "status": "scored", "reward": 1.0,
                        "gateway_trace": relative}) + "\n",
            encoding="utf-8",
        )
        (batch / "batch-status.json").write_text('{"status":"finished","planned":1}', encoding="utf-8")
        paths[name] = trace

    assert indexer.run_hot_scan()["batches"]["indexed"] == 2
    query = QueryService(isolated_settings, db)
    user = AuthUser("u_fixture", "admin", "Admin", "admin", frozenset(ALL_SCOPES), frozenset({"default"}), True)
    stable_id = make_batch_id("harbor_runs:legacy-gateway-stable")
    changed_id = make_batch_id("harbor_runs:legacy-gateway-changed")
    with pytest.raises(AppError) as pending:
        query.read_trial_gateway(user, stable_id, trial_id)
    assert pending.value.code == "GENERATION_MISMATCH"

    paths["legacy-gateway-changed"].write_text('{"model_calls": 10, "turns": []}', encoding="utf-8")
    indexer.enrich_harbor_trials()
    assert query.read_trial_gateway(user, stable_id, trial_id)["summary"]["model_calls"] == 1
    with pytest.raises(AppError) as changed:
        query.read_trial_gateway(user, changed_id, trial_id)
    assert changed.value.code == "GENERATION_MISMATCH"
    db.close_thread_connection()


def _write_approval_yaml(config_dir: Path, entries: list[dict]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml"):
        src = Path("/home/share-data/eval/config") / fname
        (config_dir / fname).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (config_dir / "source_approval_registry.yaml").write_text(
        yaml.safe_dump({"version": "v1", "default_project_id": "default", "default_owner_id": "system", "sources": entries}),
        encoding="utf-8",
    )


def test_a01_exact_parity_with_report_py_and_i03_i04_i06_on_real_197(isolated_settings: Settings) -> None:
    """Real 197 hot-tier test verifying:
    - I-04: initial hot scan < 5000ms, restart cache hit < 200ms, no-change poll < 200ms
    - A-01: 100% parity with framework/report.py on 4 golden batches
    - A-07: synth-v1-qwen-opencode-round1 (40 tasks x 8 attempts = 320 trials, pass@8 = 19/40)
    - A-08: wildclaw valid 0.0 vs 1193 agent_protocol failure (valid_mean=None)
    - A-10: vlmevalkit 3 states (CMMMU 0.6667 + 6 subjects, MM-IFEval infer-only None)
    - I-02: idempotency on repeated scans
    - I-03: unreadable dir tolerated with index_warnings
    - I-06: .publish.lock non-blocking & real running placeholders indexed
    - A-12: ATIF dual-key reasoning extraction via enrich_harbor_trials()
    """
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)

    indexer = HotIndexer(isolated_settings, db)
    stats1 = indexer.run_hot_scan(force=False)
    assert stats1["elapsed_ms"] < 5000.0, f"Initial hot scan exceeded 5s: {stats1['elapsed_ms']}ms"

    # Restart simulation hitting warm SQLite cache -> < 200ms and idempotent (I-02)
    before_batches = db.fetchone("SELECT COUNT(*) AS c FROM batches")["c"]  # type: ignore[index]
    before_eps = db.fetchone("SELECT COUNT(*) AS c FROM episodes")["c"]  # type: ignore[index]
    db.close_thread_connection()
    db2 = Database(isolated_settings.db_path)
    indexer2 = HotIndexer(isolated_settings, db2)
    stats_restart = indexer2.run_hot_scan(force=False)
    assert stats_restart["elapsed_ms"] < 200.0, f"Restart hot scan exceeded 200ms: {stats_restart['elapsed_ms']}ms"

    # No-change poll -> < 200ms
    stats2 = indexer2.run_hot_scan(force=False)
    assert stats2["elapsed_ms"] < 200.0, f"Incremental hot scan exceeded 200ms: {stats2['elapsed_ms']}ms"
    assert db2.fetchone("SELECT COUNT(*) AS c FROM batches")["c"] == before_batches  # type: ignore[index]
    assert db2.fetchone("SELECT COUNT(*) AS c FROM episodes")["c"] == before_eps  # type: ignore[index]

    # Parallel trial enrichment (fills ATIF reasoning_tokens, cached_tokens, media SHA-256)
    enr = indexer2.enrich_harbor_trials()
    assert enr["batches"] >= 40 and enr["trials"] >= 300
    # Second enrich call hits cold_file_cache and skips unchanged batches
    assert indexer2.enrich_harbor_trials() == {"batches": 0, "trials": 0}

    # A-01: compare against real framework/report.py JSON output on 4 golden batches
    report_py = Path("/home/share-data/tianxi_benches/framework/report.py")
    golden_batches = [
        "synth-v1-qwen-opencode-round1",
        "wildclaw-pi-qwen197-20261124T062059",
        "gaia-pi-qwen197-20261124T062119",
        "direct-gateway-qwen197-20261124T082911",
    ]
    for bname in golden_batches:
        bdir = isolated_settings.harbor_runs_dir / bname
        bid = make_batch_id(f"harbor_runs:{bname}")
        row = db2.fetchone("SELECT * FROM batches WHERE batch_id = ?", (bid,))
        assert row is not None, f"Missing batch {bname}"
        cmd = [sys.executable, str(report_py), str(bdir), "--format", "json"]
        if not (bdir / "batch-status.json").is_file() and row["planned"] is not None:
            cmd.extend(["--planned", str(row["planned"])])
        out = subprocess.check_output(cmd, text=True)
        expected = json.loads(out)["totals"]
        assert row["planned"] == expected["planned"]
        assert row["recorded"] == expected["recorded"]
        assert row["scored"] == expected["scored"]
        assert row["passed"] == expected["passed"]
        if expected["coverage"] is not None:
            assert abs(row["coverage"] - expected["coverage"]) < 1e-9
        if expected["valid_mean"] is not None:
            assert abs(row["valid_mean"] - expected["valid_mean"]) < 1e-9

    # A-07: round1 multi-attempt 40 tasks x 8 attempts = 320 trials, pass@8 computable = 19/40
    admin = AuthUser("u_adm", "adm", "Adm", "admin", frozenset(ALL_SCOPES), frozenset(["default"]), True)
    qs = QueryService(isolated_settings, db2)
    r1_id = make_batch_id("harbor_runs:synth-v1-qwen-opencode-round1")
    r1_tasks = qs.get_batch_tasks(admin, r1_id)
    assert r1_tasks["unique_tasks"] == 40
    assert r1_tasks["total_attempts"] == 320
    assert r1_tasks["pass_at_k_summary"]["k8"]["computable_tasks"] == 19
    assert r1_tasks["pass_at_k_summary"]["k8"]["total_tasks"] == 40
    t1 = next(it for it in r1_tasks["items"] if it["scored"] < 8)
    assert t1["pass_at_8"] is None

    # A-08: wildclaw valid 0 vs 1193 agent_protocol failed
    wc_row = db2.fetchone("SELECT * FROM batches WHERE batch_id = ?", (make_batch_id("harbor_runs:wildclaw-pi-qwen197-20261124T062059"),))
    assert wc_row["scored"] >= 1 and wc_row["valid_mean"] == 0.0  # type: ignore[index]
    f1193 = db2.fetchone("SELECT * FROM batches WHERE batch_id = ?", (make_batch_id("harbor_runs:synth-v1-qwen-opencode-1193-20261124T023653"),))
    assert f1193 is not None and f1193["valid_mean"] is None

    # A-10 & I-06: vlmevalkit CMMMU summary-only (with .publish.lock present!) indexed immediately
    cmmmu_id = make_batch_id("harbor_runs:cmmmu-vlmkit-qwen38-27b-20261124-nonthinking")
    cmmmu_detail = qs.get_batch_detail(admin, cmmmu_id)
    assert cmmmu_detail["runner"] == "vlmevalkit"
    assert cmmmu_detail["benchmark_slug"] == "cmmmu"
    assert cmmmu_detail["run_scope"] == "smoke"
    assert cmmmu_detail["official_status"] == "official_verified"
    assert abs(cmmmu_detail["official_result"]["score"] - 0.6666666667) < 1e-6
    assert cmmmu_detail["totals"]["valid_mean"] is None
    assert isinstance(cmmmu_detail["vlmeval_metrics"], list)
    assert "internal_source_fingerprint" not in cmmmu_detail

    # MM-IFEval infer-only -> valid_mean is None, official_result is None
    mmif_id = make_batch_id("harbor_runs:mm-ifeval-vlmkit-qwen38-27b-20261124")
    mmif = qs.get_batch_detail(admin, mmif_id)
    assert mmif["totals"]["valid_mean"] is None
    assert mmif["official_result"] is None

    # I-03: unreadable dir logged cleanly to index_warnings without crashing
    recovered_name = "synth-v1-qwen-opencode-20261124T022715"
    warn = db2.fetchone(
        "SELECT 1 FROM index_warnings WHERE code = 'DIR_PERMISSION_DENIED' AND message LIKE ?",
        (f"%{recovered_name}%",),
    )
    try:
        list((isolated_settings.harbor_runs_dir / recovered_name).iterdir())
    except PermissionError:
        assert warn is not None
    else:
        assert warn is None
        assert db2.fetchone("SELECT 1 FROM batches WHERE source_locator = ?", (f"harbor_runs:{recovered_name}",))

    # A-12: ATIF dual-key reasoning extraction across 197 harbor-runs
    r_cnt = db2.fetchone("SELECT COUNT(*) AS c FROM episodes WHERE reasoning_tokens IS NOT NULL")["c"]  # type: ignore[index]
    assert r_cnt > 100
    db2.close_thread_connection()


def test_i01_strict_publish_manifest_undeclared_optional_and_inline_media_409(tmp_path: Path, isolated_settings: Settings) -> None:
    """Strict I-01 tests locking @theshy's findings:
    1) Valid v2 publish-manifest.json with all 5 mandatory files + explicit absent_optional_files -> indexed.
    2) Writing an UNDECLARED optional file (`vlmevalkit/metrics.json` or `official-result.json`) on disk
       (either declared absent OR omitted from both top_level_files & absent_optional_files) -> REJECTED, old gen preserved!
    3) Same-line-count republish with stale manifest -> REJECTED, old gen preserved!
    4) Mutating trajectory.json on disk -> lazy read raises 409 GENERATION_MISMATCH!
    5) Mutating inline base64 image in result.json on disk -> delayed read_trial_media raises 409 GENERATION_MISMATCH!
    6) Legacy batch without publish-manifest where batch-report disagrees -> recomputed_from_episodes!
    """
    fake_tianxi = tmp_path / "tianxi"
    bdir = fake_tianxi / "harbor-runs" / "gen-test-batch"
    tid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tdir = bdir / "trials" / tid
    tdir.mkdir(parents=True)

    cfg_dir = tmp_path / "cfg"
    _write_approval_yaml(
        cfg_dir,
        [
            {
                "source_root": str(bdir),
                "match_mode": "dir",
                "project_id": "default",
                "owner_id": "system",
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto", "atif_trajectory", "direct_vlm_qa"],
            }
        ],
    )
    isolated_settings.tianxi_root = fake_tianxi
    isolated_settings.config_dir = cfg_dir
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    idx = HotIndexer(isolated_settings, db)
    qs = QueryService(isolated_settings, db)
    admin = AuthUser("u_adm", "adm", "Adm", "admin", frozenset(ALL_SCOPES), frozenset(["default"]), True)

    img1_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nGEN1_IMAGE_PAYLOAD_00001").decode("ascii")
    img2_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nGEN2_OVERWRITTEN_IMAGE_2").decode("ascii")

    # Gen 1 valid publish with ALL 5 mandatory files + inline base64 image in result.json
    (bdir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "dataset": "deepswe-1.1", "harness": "pi", "model": "Qwen3_8-27B", "batch": "gen-test-batch", "runner": "vlmevalkit"})
    )
    (bdir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1}))
    ep1 = {
        "schema_version": 1,
        "trial_id": tid,
        "dataset": "deepswe-1.1",
        "task_id": "t1",
        "harness": "pi",
        "runner": "vlmevalkit",
        "model_route": "Qwen3_8-27B",
        "status": "scored",
        "reward": 0.0,
        "result": f"trials/{tid}/result.json",
        "trajectory": f"trials/{tid}/trajectory.json",
        "trajectory_status": "has_tool_calls",
    }
    (bdir / "episodes.jsonl").write_text(json.dumps(ep1) + "\n")
    (bdir / "batch-report.json").write_text(
        json.dumps({"totals": {"planned": 1, "recorded": 1, "scored": 1, "reward_sum": 0.0, "valid_mean": 0.0, "passed": 0, "outcomes": {"scored": 1}}, "tasks": {"t1": {"recorded": 1, "scored": 1}}})
    )
    res_path = tdir / "result.json"
    res_path.write_text(json.dumps({"vlmeval_result": {"request_messages": [{"content": f"data:image/png;base64,{img1_b64}"}]}}))
    traj_path = tdir / "trajectory.json"
    traj_path.write_text(json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "gen1"}]}))
    (bdir / "trial-digests.jsonl").write_text(
        json.dumps(
            {
                "trial_id": tid,
                "result": {"rel_path": f"trials/{tid}/result.json", "sha256": _sha256(res_path)},
                "trajectory": {"rel_path": f"trials/{tid}/trajectory.json", "sha256": _sha256(traj_path)},
            }
        )
        + "\n"
    )

    def _build_pm(gen_id: str, extra_top: list[str] | None = None, absent: list[str] | None = None) -> dict:
        top_names = ["manifest.json", "episodes.jsonl", "batch-status.json", "batch-report.json", "trial-digests.jsonl"] + (extra_top or [])
        return {
            "schema_version": 2,
            "batch": "gen-test-batch",
            "generation_id": gen_id,
            "top_level_files": {
                name: {"sha256": _sha256(bdir / name), "size": (bdir / name).stat().st_size}
                for name in top_names
            },
            "absent_optional_files": absent if absent is not None else ["official-result.json", "vlmevalkit/metrics.json", "selection.json", "run-config.json"],
        }

    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_001")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "indexed"
    bid = make_batch_id("harbor_runs:gen-test-batch")
    assert db.fetchone("SELECT generation_id, valid_mean FROM batches WHERE batch_id = ?", (bid,)) == {"generation_id": "gen_001", "valid_mean": 0.0}

    # Verify EVERY indexed evidence reference in manifest-v2 batch has a verified non-empty 64-char SHA-256!
    ev_row = db.fetchone("SELECT evidence_refs_json FROM episodes WHERE batch_id = ? AND trial_id = ?", (bid, tid))
    assert ev_row is not None
    ev_parsed = json.loads(ev_row["evidence_refs_json"])
    assert len(ev_parsed["result"]["sha256"]) == 64
    assert len(ev_parsed["trajectory"]["sha256"]) == 64
    assert len(ev_parsed["media"]) == 1 and len(ev_parsed["media"][0]["file_sha256"]) == 64 and len(ev_parsed["media"][0]["sha256"]) == 64

    # Running background enrich_harbor_trials() MUST skip manifest-v2 batches (zero second unverified disk read!)
    assert idx.enrich_harbor_trials(batch_ids=[bid]) == {"batches": 0, "trials": 0}

    # Before overwrite: inline media reads Gen 1 image cleanly
    raw_img, mime = qs.read_trial_media(admin, bid, tid, 0)
    assert mime == "image/png" and b"GEN1_IMAGE_PAYLOAD" in raw_img

    # Case 2a: Undeclared vlmevalkit/metrics.json placed on disk while declared absent -> MUST REJECT!
    (bdir / "vlmevalkit").mkdir(exist_ok=True)
    (bdir / "vlmevalkit" / "metrics.json").write_text(json.dumps([{"subject": "Physics", "acc": 0.99}]))
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_002_sneaky")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id, vlmeval_metrics_json FROM batches WHERE batch_id = ?", (bid,)) == {
        "generation_id": "gen_001",
        "vlmeval_metrics_json": None,
    }

    # Case 2b: Omitting vlmevalkit/metrics.json from BOTH top_level_files & absent_optional_files -> MUST REJECT!
    (bdir / "publish-manifest.json").write_text(
        json.dumps(_build_pm("gen_002_omitted", absent=["official-result.json", "selection.json", "run-config.json"]))
    )
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id, vlmeval_metrics_json FROM batches WHERE batch_id = ?", (bid,)) == {
        "generation_id": "gen_001",
        "vlmeval_metrics_json": None,
    }

    # Case 2c (@theshy I-01): episodes.jsonl references result.json/trajectory.json, but trial-digests.jsonl omits it!
    (bdir / "vlmevalkit" / "metrics.json").unlink()
    saved_td_text = (bdir / "trial-digests.jsonl").read_text(encoding="utf-8")
    (bdir / "trial-digests.jsonl").write_text(
        json.dumps({"trial_id": tid, "trajectory": {"rel_path": f"trials/{tid}/trajectory.json", "sha256": _sha256(traj_path)}}) + "\n"
    )
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_002_missing_result_digest")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id FROM batches WHERE batch_id = ?", (bid,)) == {"generation_id": "gen_001"}

    # Case 2d (@theshy I-03): non-object JSON line in trial-digests.jsonl MUST NOT raise AttributeError,
    # MUST preserve Gen 1, record batch warning, AND allow sibling batches in run_hot_scan to succeed!
    (bdir / "trial-digests.jsonl").write_text("123\n[\"not-an-object\"]\n")
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_002_non_object_td")))
    sibling_dir = fake_tianxi / "harbor-runs" / "sibling-ok-batch"
    sibling_dir.mkdir(parents=True)
    (sibling_dir / "manifest.json").write_text(json.dumps({"dataset": "deepswe-1.1", "harness": "pi", "model": "Qwen3_8-27B"}))
    (sibling_dir / "episodes.jsonl").write_text(json.dumps({"trial_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "task_id": "t_sib", "status": "scored", "reward": 1.0}) + "\n")
    scan_res = idx.run_hot_scan(force=True)
    assert scan_res["batches"].get("rejected", 0) >= 1
    assert scan_res["batches"].get("indexed", 0) >= 1
    assert db.fetchone("SELECT generation_id FROM batches WHERE batch_id = ?", (bid,)) == {"generation_id": "gen_001"}
    assert db.fetchone("SELECT scored FROM batches WHERE batch_id = ?", (make_batch_id("harbor_runs:sibling-ok-batch"),)) == {"scored": 1}

    # Case 2e: non-object batch-report.json in manifest-v2 batch is also rejected cleanly without crashing!
    (bdir / "trial-digests.jsonl").write_text(saved_td_text, encoding="utf-8")
    saved_report_text = (bdir / "batch-report.json").read_text(encoding="utf-8")
    (bdir / "batch-report.json").write_text("[\"not-a-report-dict\"]")
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_002_bad_report")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id FROM batches WHERE batch_id = ?", (bid,)) == {"generation_id": "gen_001"}
    (bdir / "batch-report.json").write_text(saved_report_text, encoding="utf-8")

    # Restore valid trial-digests.jsonl & manifest, then test:
    # Case 2f (@theshy I-01): Idempotent rescan of IDENTICAL gen_001 succeeds cleanly,
    # while a SECOND valid Manifest v2 reusing the SAME generation_id='gen_001' with MUTATED content
    # (reward 0.0 -> 1.0, ALL file/trial SHA-256 digests matching disk!) MUST be REJECTED and preserve Gen 1!
    (bdir / "trial-digests.jsonl").write_text(saved_td_text, encoding="utf-8")
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_001")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "indexed"
    assert db.fetchone("SELECT generation_id, valid_mean FROM batches WHERE batch_id = ?", (bid,)) == {
        "generation_id": "gen_001",
        "valid_mean": 0.0,
    }

    ep_mut = dict(ep1, reward=1.0)
    (bdir / "episodes.jsonl").write_text(json.dumps(ep_mut) + "\n")
    (bdir / "batch-report.json").write_text(
        json.dumps({"totals": {"planned": 1, "recorded": 1, "scored": 1, "reward_sum": 1.0, "valid_mean": 1.0, "passed": 1, "outcomes": {"scored": 1}}, "tasks": {"t1": {"recorded": 1, "scored": 1}}})
    )
    # Re-sign valid publish-manifest.json with the SAME generation_id='gen_001'!
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_001")))
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id, valid_mean FROM batches WHERE batch_id = ?", (bid,)) == {
        "generation_id": "gen_001",
        "valid_mean": 0.0,
    }
    assert db.fetchone("SELECT reward FROM episodes WHERE batch_id = ? AND trial_id = ?", (bid, tid)) == {"reward": 0.0}
    assert db.fetchone("SELECT 1 FROM index_warnings WHERE code = 'PUBLISH_GENERATION_CONFLICT'") is not None

    # Restore Gen 1 episodes/report/manifest, then test stale-manifest same-line-count overwrite -> REJECTED!
    (bdir / "episodes.jsonl").write_text(json.dumps(ep1) + "\n")
    (bdir / "batch-report.json").write_text(saved_report_text, encoding="utf-8")
    (bdir / "publish-manifest.json").write_text(json.dumps(_build_pm("gen_001")))
    ep2 = dict(ep1, reward=1.0)
    (bdir / "episodes.jsonl").write_text(json.dumps(ep2) + "\n")
    assert idx.scan_single_harbor_batch(bdir, force=True) == "rejected"
    assert db.fetchone("SELECT generation_id, valid_mean FROM batches WHERE batch_id = ?", (bid,)) == {"generation_id": "gen_001", "valid_mean": 0.0}

    # Mutate trajectory.json -> lazy read MUST raise 409 GENERATION_MISMATCH!
    traj_path.write_text(json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "gen2_overwritten"}]}))
    with pytest.raises(AppError) as exc_t:
        qs.read_trial_trajectory(admin, bid, tid)
    assert exc_t.value.status_code == 409 and exc_t.value.code == "GENERATION_MISMATCH"

    # Mutate inline base64 image in result.json on disk -> delayed read_trial_media MUST raise 409 GENERATION_MISMATCH!
    res_path.write_text(json.dumps({"vlmeval_result": {"request_messages": [{"content": f"data:image/png;base64,{img2_b64}"}]}}))
    with pytest.raises(AppError) as exc_m:
        qs.read_trial_media(admin, bid, tid, 0)
    assert exc_m.value.status_code == 409 and exc_m.value.code == "GENERATION_MISMATCH"

    # Overwrite result.json removing inline image altogether -> MUST STILL raise 409 GENERATION_MISMATCH (not 404)!
    res_path.write_text(json.dumps({"vlmeval_result": {"request_messages": [{"content": "no image now"}]}}))
    with pytest.raises(AppError) as exc_m2:
        qs.read_trial_media(admin, bid, tid, 0)
    assert exc_m2.value.status_code == 409 and exc_m2.value.code == "GENERATION_MISMATCH"

    # Legacy batch without publish-manifest where batch-report disagrees -> recomputed_from_episodes
    legacy_dir = fake_tianxi / "harbor-runs" / "legacy-mismatch"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "manifest.json").write_text(json.dumps({"dataset": "deepswe-1.1", "harness": "pi", "model": "Qwen3_8-27B"}))
    (legacy_dir / "episodes.jsonl").write_text(json.dumps(ep2) + "\n")
    (legacy_dir / "batch-report.json").write_text(
        json.dumps({"totals": {"recorded": 1, "scored": 1, "reward_sum": 0.0, "valid_mean": 0.0, "passed": 0, "outcomes": {"scored": 1}}, "tasks": {"t1": {"recorded": 1, "scored": 1}}})
    )
    assert idx.scan_single_harbor_batch(legacy_dir, force=True) == "indexed"
    leg_row = db.fetchone("SELECT report_origin, valid_mean FROM batches WHERE batch_id = ?", (make_batch_id("harbor_runs:legacy-mismatch"),))
    assert leg_row == {"report_origin": "recomputed_from_episodes", "valid_mean": 1.0}
    db.close_thread_connection()


def test_a06_a10_a11_i06_orthogonality_placeholder_project_isolation_and_stable_id(tmp_path: Path, isolated_settings: Settings) -> None:
    """Verify:
    - A-06: 85% subset remains 'subset'
    - A-11: slug collision safety
    - I-06 (Project Isolation & Stable ID):
      1) Project A creates running placeholder `ph_id` with source fingerprint F.
      2) Project B publishes a batch with the SAME source fingerprint F -> Project A's placeholder `ph_id`
         MUST NOT be deleted or modified!
      3) Project A publishes its finished batch in a new dir with fingerprint F -> upgrades `ph_id` IN-PLACE,
         keeping the exact same drillable `batch_id == ph_id` (`is_running_placeholder = 0`, episodes attached)!
    """
    scope = determine_run_scope(
        harness="pi",
        raw_dataset_name="deepswe-1.1",
        batch_name="deepswe-85pct",
        planned=96,
        recorded=96,
        unique_tasks=96,
        known_task_count=113,
    )
    assert scope == "subset"

    smap: dict[str, str] = {}
    assert make_collision_safe_slug("Bench A+B", smap) != make_collision_safe_slug("Bench A-B", smap)

    fake_tianxi = tmp_path / "tianxi2"
    ph_dir = fake_tianxi / "harbor-runs" / "live-run-placeholder"
    ph_dir.mkdir(parents=True)
    shared_source = {"host": "10.110.158.144", "path": "/tmp/secret-run-99/work/jobs/live-run-1"}
    (ph_dir / "batch-status.json").write_text(json.dumps({"status": "running", "planned": 4, "source": shared_source}))

    isolated_settings.tianxi_root = fake_tianxi
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    idx = HotIndexer(isolated_settings, db)

    assert idx.scan_single_harbor_batch(ph_dir, force=True, project_override="proj_a") == "placeholder"
    ph_id = make_batch_id("harbor_runs:live-run-placeholder")
    ph_row = db.fetchone("SELECT is_running_placeholder, project_id, internal_source_fingerprint, source_path_redacted FROM batches WHERE batch_id = ?", (ph_id,))
    assert ph_row["is_running_placeholder"] == 1 and ph_row["project_id"] == "proj_a"  # type: ignore[index]
    assert "/tmp/secret-run-99" not in (ph_row["source_path_redacted"] or "")  # type: ignore[index]

    # Step 2: Project B publishes with SAME raw source fingerprint -> MUST NOT delete or touch proj_a's placeholder!
    pub_b_dir = fake_tianxi / "harbor-runs" / "proj-b-same-source"
    pub_b_dir.mkdir(parents=True)
    (pub_b_dir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1, "source": shared_source}))
    (pub_b_dir / "manifest.json").write_text(json.dumps({"dataset": "Unregistered Custom (v9)", "harness": "custom_h", "model": "Qwen3_8-27B", "runner": "custom_x"}))
    (pub_b_dir / "episodes.jsonl").write_text(json.dumps({"trial_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "task_id": "t1", "status": "scored", "reward": 0.0}) + "\n")
    assert idx.scan_single_harbor_batch(pub_b_dir, force=True, project_override="proj_b") == "indexed"
    still_ph = db.fetchone("SELECT is_running_placeholder, project_id FROM batches WHERE batch_id = ?", (ph_id,))
    assert still_ph == {"is_running_placeholder": 1, "project_id": "proj_a"}

    # Step 3: Project A publishes finished batch in new dir -> upgrades ph_id IN-PLACE with stable batch_id!
    pub_a_dir = fake_tianxi / "harbor-runs" / "live-run-published"
    pub_a_dir.mkdir(parents=True)
    (pub_a_dir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1, "source": shared_source}))
    (pub_a_dir / "manifest.json").write_text(json.dumps({"dataset": "Unregistered Custom (v9)", "harness": "custom_h", "model": "Qwen3_8-27B", "runner": "custom_x"}))
    (pub_a_dir / "episodes.jsonl").write_text(
        json.dumps({"trial_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "task_id": "t1", "status": "scored", "reward": 1.0, "runner": "custom_x", "trajectory_status": "future_v2"})
        + "\n{corrupt json line\n"
    )
    assert idx.scan_single_harbor_batch(pub_a_dir, force=True, project_override="proj_a") == "indexed"
    upgraded = db.fetchone("SELECT batch_id, is_running_placeholder, runner, identity_status, raw_dataset_name, scored FROM batches WHERE batch_id = ?", (ph_id,))
    assert upgraded == {
        "batch_id": ph_id,
        "is_running_placeholder": 0,
        "runner": "custom_x",
        "identity_status": "unmapped",
        "raw_dataset_name": "Unregistered Custom (v9)",
        "scored": 1,
    }
    ep_attached = db.fetchone("SELECT COUNT(*) AS c FROM episodes WHERE batch_id = ?", (ph_id,))["c"]  # type: ignore[index]
    assert ep_attached == 1
    assert db.fetchone("SELECT 1 FROM index_warnings WHERE code = 'CORRUPT_JSONL_LINE'") is not None

    # Step 4: Subsequent poll encountering the old placeholder dir MUST NOT crash or downgrade ph_id!
    assert idx.scan_single_harbor_batch(ph_dir, force=True, project_override="proj_a") == "skipped_unchanged"
    post_poll = db.fetchone("SELECT batch_id, is_running_placeholder, scored FROM batches WHERE batch_id = ?", (ph_id,))
    assert post_poll == {"batch_id": ph_id, "is_running_placeholder": 0, "scored": 1}
    db.close_thread_connection()
