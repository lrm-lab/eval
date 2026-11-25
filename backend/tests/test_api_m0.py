from __future__ import annotations

import base64
import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.migrations import utcnow_iso
from app.indexing.identity import make_batch_id
from app.main import create_app
from app.security.authz import ALL_SCOPES
from tests.conftest import create_user, login_client


def _setup_custom_config(cfg_dir: Path, approval_entries: list[dict]) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml"):
        src = Path("/home/share-data/eval/config") / fname
        (cfg_dir / fname).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dir / "source_approval_registry.yaml").write_text(
        yaml.safe_dump({"version": "v1", "default_project_id": "default", "default_owner_id": "system", "sources": approval_entries}),
        encoding="utf-8",
    )


def test_api_m0_end_to_end_auth_scopes_and_revoked_snapshot(m0_app) -> None:
    """Verify M0 REST endpoints, 6-scope zero bypass (incl. direct_vlm_qa:read),
    admin health probes + source_coverage.by_source, taxonomy reload, legacy sample seek API,
    and revoked frozen snapshot reopen (S-02)."""
    ctx = m0_app.state.ctx
    for bname in (
        "cmmmu-vlmkit-qwen38-27b-20261124-nonthinking",
        "gateway-opencode-qwen197-20261124T065524",
        "synth-v1-qwen-opencode-round1",
    ):
        ctx.coordinator.hot.scan_single_harbor_batch(ctx.settings.harbor_runs_dir / bname, force=True)
    ctx.coordinator.hot.scan_legacy_leaderboard_csv()
    ctx.coordinator.hot.scan_hot_datasets()

    anon = TestClient(m0_app)
    assert anon.get("/api/v1/healthz").status_code == 200
    assert anon.get("/api/v1/system/overview").status_code == 401

    viewer_client = TestClient(m0_app)
    login_client(viewer_client, "viewer_noscope", "StrongViewerPassword!3")
    ov = viewer_client.get("/api/v1/system/overview")
    assert ov.status_code == 200
    assert ov.json()["kpis"]["readable_batches"] >= 3
    assert "dedup_recorded_trials" in ov.json()["kpis"]

    gw_bid = make_batch_id("harbor_runs:gateway-opencode-qwen197-20261124T065524")
    gw_ep = ctx.db.fetchone("SELECT trial_id FROM episodes WHERE batch_id = ? LIMIT 1", (gw_bid,))
    assert gw_ep is not None
    gw_resp = viewer_client.get(f"/api/v1/batches/{gw_bid}/trials/{gw_ep['trial_id']}/gateway")
    assert gw_resp.status_code == 200 and gw_resp.json()["redacted"] is True and gw_resp.json()["turns"] == []

    tr_resp = viewer_client.get(f"/api/v1/batches/{gw_bid}/trials/{gw_ep['trial_id']}/trajectory")
    assert tr_resp.status_code == 403 and tr_resp.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    cmmmu_bid = make_batch_id("harbor_runs:cmmmu-vlmkit-qwen38-27b-20261124-nonthinking")
    cmmmu_ep = ctx.db.fetchone("SELECT trial_id FROM episodes WHERE batch_id = ? LIMIT 1", (cmmmu_bid,))
    assert cmmmu_ep is not None
    td_noscope = viewer_client.get(f"/api/v1/batches/{cmmmu_bid}/trials/{cmmmu_ep['trial_id']}")
    assert td_noscope.status_code == 200 and td_noscope.json()["qa_redacted"] is True
    media_noscope = viewer_client.get(f"/api/v1/batches/{cmmmu_bid}/trials/{cmmmu_ep['trial_id']}/media/0")
    assert media_noscope.status_code == 403 and media_noscope.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    holdout_resp = viewer_client.get("/api/v1/datasets/tianxi-synth-v1__1.0__holdout/tasks")
    assert holdout_resp.status_code == 200 and holdout_resp.json()["redacted"] is True and holdout_resp.json()["items"] == []

    admin_client = TestClient(m0_app)
    admin_csrf = login_client(admin_client, "admin_test", "StrongAdminPassword!1")

    assert admin_client.get(f"/api/v1/batches/{gw_bid}/trials/{gw_ep['trial_id']}/trajectory").status_code == 200
    gw_full = admin_client.get(f"/api/v1/batches/{gw_bid}/trials/{gw_ep['trial_id']}/gateway")
    assert gw_full.status_code == 200 and gw_full.json()["redacted"] is False
    vlm_media = admin_client.get(f"/api/v1/batches/{cmmmu_bid}/trials/{cmmmu_ep['trial_id']}/media/0")
    assert vlm_media.status_code == 200 and vlm_media.headers["content-type"].startswith("image/")

    # Admin health reports all isolation probes + source_coverage.by_source
    health = admin_client.get("/api/v1/admin/health")
    assert health.status_code == 200
    hjson = health.json()
    assert hjson["storage"]["local_budget_mb"] == 750
    for probe_key in ("state_dir", "db_dir", "staging_dir", "evidence_store_dir", "runner_work_dir", "secrets_file"):
        assert hjson["storage"]["isolation_probes"][probe_key]["ok"] is True
    assert len(hjson["source_coverage"]["by_source"]) >= 25

    # Leaderboard includes 2026-11-20 historical IFEval, AI2D_TEST, RealWorldQA under legacy-20261120-csv!
    lb = admin_client.get("/api/v1/leaderboard?include_legacy=true")
    assert lb.status_code == 200
    best_slugs = {b["benchmark_slug"] for b in lb.json()["best_by_task"]}
    assert {"ifeval", "ai2d-test", "realworldqa"}.issubset(best_slugs)

    # Legacy O(1) offset API (IFEval & AI2D_TEST) with answer_read stripping!
    ctx.coordinator.cold.index_legacy_text_json_offsets("IFEval", force=True)
    ctx.coordinator.cold.index_legacy_jsonl_offsets("AI2D_TEST", force=True)
    ifeval_list = admin_client.get("/api/v1/legacy/IFEval/samples?page_size=5")
    assert ifeval_list.status_code == 200 and ifeval_list.json()["total"] > 0
    first_if = ifeval_list.json()["items"][0]

    # User with direct_vlm_qa:read but NO dataset:answer_read -> gold/answer stripped from IFEval sample!
    create_user(
        ctx.db,
        username="qa_no_ans",
        password="QaNoAnsPassword!1",
        role="researcher",
        projects=["default"],
        scopes=["direct_vlm_qa:read"],
    )
    qa_client = TestClient(m0_app)
    login_client(qa_client, "qa_no_ans", "QaNoAnsPassword!1")
    if_no_ans = qa_client.get(f"/api/v1/legacy/IFEval/samples/{first_if['model_file']}/{first_if['sample_id']}")
    assert if_no_ans.status_code == 200
    assert "gold" not in if_no_ans.json()["sample"] and "answer" not in if_no_ans.json()["sample"]

    if_with_ans = admin_client.get(f"/api/v1/legacy/IFEval/samples/{first_if['model_file']}/{first_if['sample_id']}")
    assert if_with_ans.status_code == 200 and isinstance(if_with_ans.json()["sample"], dict)

    # Revoked frozen snapshot reopen (S-02)
    uid_team = create_user(
        ctx.db,
        username="team_owner",
        password="TeamOwnerPassword!1",
        role="researcher",
        projects=["proj_alpha"],
        scopes=["trace:read"],
    )
    now = utcnow_iso()
    with ctx.db.transaction() as conn:
        for bid in ("b_alpha_1", "b_alpha_2"):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    runner, run_scope, scoring_protocol, generation_id, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'proj_alpha', ?, 'approved_public',
                          'deepswe', 'deepswe__1.1__default', 'DeepSWE', 'harbor', 'subset', 'deepswe-f2p-p2p', 'g1', 'd1', ?)
                """,
                (bid, f"loc:{bid}", bid, uid_team, now),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, status, reward, reward_valid
                ) VALUES (?, ?, ?, 'g1', 't1', 'scored', 1.0, 1)
                """,
                (bid, f"tr_{bid}", f"key_{bid}"),
            )

    team_client = TestClient(m0_app)
    team_csrf = login_client(team_client, "team_owner", "TeamOwnerPassword!1")
    cmp_r = team_client.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": team_csrf},
        json={"subjects": [{"id": "s1", "batch_id": "b_alpha_1"}, {"id": "s2", "batch_id": "b_alpha_2"}], "baseline_subject_id": "s1"},
    )
    assert cmp_r.status_code == 200
    save_r = team_client.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": team_csrf},
        json={"name": "Alpha Freeze", "mode": "snapshot", "project_id": "proj_alpha", "comparison_id": cmp_r.json()["comparison_id"]},
    )
    assert save_r.status_code == 200
    short_id = save_r.json()["short_id"]
    assert team_client.get(f"/api/v1/compare/s/{short_id}").status_code == 200

    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = '[]' WHERE user_id = ?", (uid_team,))
    revoked_open = team_client.get(f"/api/v1/compare/s/{short_id}")
    assert revoked_open.status_code == 403 and revoked_open.json()["error"]["code"] == "PROJECT_FORBIDDEN"

    # Taxonomy reload & global search project isolation
    reload_resp = admin_client.post("/api/v1/admin/taxonomy/reload", headers={"X-CSRF-Token": admin_csrf})
    assert reload_resp.status_code == 200 and reload_resp.json()["synced"]["benchmarks"] >= 20

    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO search_fts (entity_type, entity_id, title, subtitle, body, project_id, required_scope) VALUES ('batch', 'b_alpha_1', 'SecretAlphaBatch', 'alpha', 'alpha', 'proj_alpha', '')"
        )
    assert all(it["entity_id"] != "b_alpha_1" for it in viewer_client.get("/api/v1/search?q=SecretAlphaBatch").json()["items"])
    assert any(it["entity_id"] == "b_alpha_1" for it in admin_client.get("/api/v1/search?q=SecretAlphaBatch").json()["items"])

    # Verdict endpoint + TB2.1 verifier-output scope check
    v_resp = admin_client.get("/api/v1/compare/verdict?baseline=b_alpha_1&candidate=b_alpha_2&regression_threshold_pp=-1.0")
    assert v_resp.status_code == 200
    mmlu = next(g for g in v_resp.json()["gain_vs_guardrail_verdict"]["guardrails"] if g["benchmark_slug"] == "mmlu-pro")
    assert mmlu["status"] == "undeterminable"

    ctx.coordinator.cold.index_native_tb21(force=True)
    tb_bid = make_batch_id("native_harbor:terminal-bench2.1/jhpPiV5__Qwen3_8-27B")
    tb_ep = ctx.db.fetchone("SELECT trial_id FROM episodes WHERE batch_id = ? AND has_verifier_output = 1 LIMIT 1", (tb_bid,))
    assert tb_ep is not None
    assert viewer_client.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/verifier-output").status_code == 403
    assert admin_client.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/verifier-output").status_code == 200


def test_granular_approval_recursive_qa_redaction_and_holdout_crosscheck(tmp_path: Path, isolated_settings: Settings) -> None:
    """Locks @theshy's findings on evidence governance:
    1) Summary-only approved source (`approved_evidence_classes: ["summary_dto"]`): even Admin gets
       qa_redacted=True and 403 on /media/0 & /trajectory!
    2) Approved Direct/VLM QA recursively redacts /home/*, /tmp/*, Bearer, sk-* AND strips answer/gold
       when caller lacks dataset:answer_read!
    3) Holdout batch blocks QA/media/trajectory when caller has direct_vlm_qa:read + trace:read
       but lacks dataset:holdout_read!
    """
    fake_tianxi = tmp_path / "tianxi_gov"
    hr = fake_tianxi / "harbor-runs"
    img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nSAMPLE_IMG_1234567890").decode("ascii")

    def _make_vlm_batch(name: str, dataset: str) -> tuple[Path, str]:
        bdir = hr / name
        tid = "11111111-1111-1111-1111-111111111111"
        tdir = bdir / "trials" / tid
        tdir.mkdir(parents=True)
        (bdir / "manifest.json").write_text(json.dumps({"dataset": dataset, "harness": "vlm", "model": "Qwen3_8-27B", "runner": "vlmevalkit"}))
        (bdir / "episodes.jsonl").write_text(
            json.dumps(
                {
                    "trial_id": tid,
                    "task_id": "t_gov_1",
                    "status": "scored",
                    "reward": 1.0,
                    "runner": "vlmevalkit",
                    "result": f"trials/{tid}/result.json",
                    "trajectory": f"trials/{tid}/trajectory.json",
                }
            )
            + "\n"
        )
        (tdir / "result.json").write_text(
            json.dumps(
                {
                    "vlmeval_result": {
                        "question": "Check /home/alice/secret.py and /tmp/job_xyz/a.log",
                        "prediction": "Bearer supersecret1234567890 and sk-livekey1234567890",
                        "answer": "GOLD_SECRET_42",
                        "ground_truth": "GOLD_GT_99",
                        "request_messages": [{"content": f"data:image/png;base64,{img_b64}"}],
                    }
                }
            )
        )
        (tdir / "trajectory.json").write_text(json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "secret /home/bob/x"}]}))
        return bdir, tid

    b_sum, tid_s = _make_vlm_batch("summary-only-vlm", "CMMMU")
    b_full, tid_f = _make_vlm_batch("approved-full-vlm", "CMMMU")
    b_hold, tid_h = _make_vlm_batch("synth-v1-holdout-vlm", "tianxi-synth-v1-holdout")

    cfg_dir = tmp_path / "cfg_gov"
    _setup_custom_config(
        cfg_dir,
        [
            {
                "source_root": str(b_sum),
                "match_mode": "dir",
                "project_id": "default",
                "owner_id": "system",
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto"],
            },
            {
                "source_root": str(b_full),
                "match_mode": "dir",
                "project_id": "default",
                "owner_id": "system",
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto", "direct_vlm_qa"],
            },
            {
                "source_root": str(b_hold),
                "match_mode": "dir",
                "project_id": "default",
                "owner_id": "system",
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto", "direct_vlm_qa", "atif_trajectory"],
            },
        ],
    )
    isolated_settings.tianxi_root = fake_tianxi
    isolated_settings.config_dir = cfg_dir

    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    for bd in (b_sum, b_full, b_hold):
        assert ctx.coordinator.hot.scan_single_harbor_batch(bd, force=True) == "indexed"

    create_user(ctx.db, username="adm", password="StrongPassword!1", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(
        ctx.db,
        username="res_no_ans_no_holdout",
        password="StrongPassword!2",
        role="researcher",
        projects=["default"],
        scopes=["direct_vlm_qa:read", "trace:read"],
    )

    adm_c = TestClient(app)
    login_client(adm_c, "adm", "StrongPassword!1")
    res_c = TestClient(app)
    login_client(res_c, "res_no_ans_no_holdout", "StrongPassword!2")

    # 1. Summary-only batch: EVEN Admin with ALL scopes gets qa_redacted=True and 403 on media & trajectory!
    bid_sum = make_batch_id("harbor_runs:summary-only-vlm")
    r_sum = adm_c.get(f"/api/v1/batches/{bid_sum}/trials/{tid_s}")
    assert r_sum.status_code == 200 and r_sum.json()["qa_redacted"] is True
    assert "qa" not in r_sum.json()
    assert adm_c.get(f"/api/v1/batches/{bid_sum}/trials/{tid_s}/media/0").status_code == 403
    assert adm_c.get(f"/api/v1/batches/{bid_sum}/trials/{tid_s}/trajectory").status_code == 403

    # 2. Approved full VLM batch: recursive redaction + answer stripping for researcher without dataset:answer_read
    bid_full = make_batch_id("harbor_runs:approved-full-vlm")
    r_res = res_c.get(f"/api/v1/batches/{bid_full}/trials/{tid_f}")
    assert r_res.status_code == 200 and r_res.json()["qa_redacted"] is False
    vlm_qa = r_res.json()["qa"]["vlmeval_result"]
    assert "/home/alice" not in vlm_qa["question"] and "/tmp/job_xyz" not in vlm_qa["question"]
    assert "supersecret1234567890" not in vlm_qa["prediction"] and "sk-livekey1234567890" not in vlm_qa["prediction"]
    assert "answer" not in vlm_qa and "ground_truth" not in vlm_qa
    # Admin WITH dataset:answer_read sees redacted QA with answer retained
    r_adm = adm_c.get(f"/api/v1/batches/{bid_full}/trials/{tid_f}")
    assert r_adm.json()["qa"]["vlmeval_result"]["answer"] == "GOLD_SECRET_42"

    # 3. Holdout batch: researcher WITHOUT dataset:holdout_read blocked on trial detail, media, and trajectory!
    bid_hold = make_batch_id("harbor_runs:synth-v1-holdout-vlm")
    r_hold_res = res_c.get(f"/api/v1/batches/{bid_hold}/trials/{tid_h}")
    assert r_hold_res.status_code == 403 and r_hold_res.json()["error"]["code"] == "SCOPE_FORBIDDEN"
    assert r_hold_res.json()["error"]["detail"]["missing_scope"] == "dataset:holdout_read"
    assert res_c.get(f"/api/v1/batches/{bid_hold}/trials/{tid_h}/media/0").status_code == 403
    assert res_c.get(f"/api/v1/batches/{bid_hold}/trials/{tid_h}/trajectory").status_code == 403
    # Admin WITH dataset:holdout_read succeeds
    assert adm_c.get(f"/api/v1/batches/{bid_hold}/trials/{tid_h}").status_code == 200
    assert adm_c.get(f"/api/v1/batches/{bid_hold}/trials/{tid_h}/media/0").status_code == 200


def test_production_routes_auth_before_dedup_protocol_isolation_and_bound_task_diff_409(tmp_path: Path, isolated_settings: Settings) -> None:
    """Locks @theshy's findings on business query routes:
    1) Same UUID in proj_a (prio 1, reward=1.0) & proj_b (prio 2, reward=0.0):
       HTTP /system/overview, /leaderboard, /scores/deduplicated return proj_b's record for researcher_b
       without leaking or being shadowed by proj_a!
    2) Same UUID under 2 protocols remains distinct in /scores/deduplicated and preserves local batch denominators.
    3) /leaderboard best_by_task NEVER lets a smoke batch (100%) beat a full_split batch (50%), and isolates protocols.
    4) Bound comparison_id: mutating batch A's episodes.jsonl (same line count & byte size!) and reindexing
       causes POST /api/v1/compare/task-diff with the old comparison_id to return 409 GENERATION_MISMATCH
       instead of silently changing 1 win -> 0 wins!
    """
    fake_tianxi = tmp_path / "tianxi_q"
    hr = fake_tianxi / "harbor-runs"
    isolated_settings.tianxi_root = fake_tianxi
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    now = utcnow_iso()

    create_user(ctx.db, username="adm", password="StrongPassword!1", role="admin", projects=["default", "proj_a", "proj_b"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="res_b", password="StrongPassword!2", role="researcher", projects=["proj_b"], scopes=[])

    # 1 & 2 & 3: Insert multi-project same-UUID, multi-protocol same-UUID, and smoke-vs-full_split rows
    shared_uuid = "99999999-8888-7777-6666-555555555555"
    with ctx.db.transaction() as conn:
        batch_specs = [
            # (batch_id, proj, model, harness, scope, proto, prio, rew, planned, scored, vm)
            ("b_pa_1", "proj_a", "ModelX", "pi", "full_split", "deepswe-f2p-p2p", 1, 1.0, 2, 1, 1.0),
            ("b_pb_1", "proj_b", "ModelX", "pi", "full_split", "deepswe-f2p-p2p", 2, 0.0, 3, 1, 0.0),
            ("b_pb_proto2", "proj_b", "ModelX", "pi", "full_split", "lrm-eval-report-v1", 2, 1.0, 5, 1, 1.0),
            ("b_pb_smoke", "proj_b", "ModelSmoke", "pi", "smoke", "deepswe-f2p-p2p", 2, 1.0, 1, 1, 1.0),
            ("b_pb_full", "proj_b", "ModelFull", "pi", "full_split", "deepswe-f2p-p2p", 2, 0.5, 10, 10, 0.5),
        ]
        for bid, proj, model, har, scope, proto, prio, rew, planned, scored, vm in batch_specs:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    model_canonical, harness, runner, run_scope, scoring_protocol,
                    planned, recorded, scored, valid_mean, generation_id, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, ?, 'system', 'approved_public',
                          'deepswe', 'deepswe__1.1__default', 'DeepSWE',
                          ?, ?, 'harbor', ?, ?, ?, ?, ?, ?, 'g1', 'd1', ?)
                """,
                (bid, f"loc:{bid}", bid, proj, model, har, scope, proto, planned, scored, scored, vm, now),
            )
            tkey = shared_uuid if bid in ("b_pa_1", "b_pb_1", "b_pb_proto2") else f"key_{bid}"
            conn.execute(
                """
                INSERT INTO trial_protocol_scores (
                    canonical_trial_key, scoring_protocol, batch_id, trial_id,
                    project_id, source_namespace, source_priority_rank,
                    dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                ) VALUES (?, ?, ?, ?, ?, 'harbor_runs', ?, 'deepswe__1.1__default', 't1', 'scored', ?, 1, '["harbor_runs"]')
                """,
                (tkey, proto, bid, tkey, proj, prio, rew),
            )

    res_b_client = TestClient(app)
    login_client(res_b_client, "res_b", "StrongPassword!2")

    # HTTP /scores/deduplicated: researcher_b sees b_pb_1 (reward=0.0) AND b_pb_proto2 (separate protocol!), NEVER b_pa_1!
    sc_r = res_b_client.get("/api/v1/scores/deduplicated?scoring_protocol=deepswe-f2p-p2p")
    assert sc_r.status_code == 200
    sc_by_batch = {it["batch_id"]: it for it in sc_r.json()["items"]}
    assert "b_pa_1" not in sc_by_batch
    assert "b_pb_1" in sc_by_batch and sc_by_batch["b_pb_1"]["reward"] == 0.0

    sc_all = res_b_client.get("/api/v1/scores/deduplicated")
    protos_for_shared = {(it["batch_id"], it["scoring_protocol"]) for it in sc_all.json()["items"] if it["canonical_trial_key"] == shared_uuid}
    assert protos_for_shared == {("b_pb_1", "deepswe-f2p-p2p"), ("b_pb_proto2", "lrm-eval-report-v1")}

    # Local batch denominators preserved independently
    assert res_b_client.get("/api/v1/batches/b_pb_1").json()["totals"]["planned"] == 3
    assert res_b_client.get("/api/v1/batches/b_pb_proto2").json()["totals"]["planned"] == 5

    # HTTP /system/overview uses auth-before-dedup KPIs (4 proj_b dedup trials, zero proj_a leak)
    ov_b = res_b_client.get("/api/v1/system/overview").json()
    assert ov_b["kpis"]["readable_batches"] == 4
    assert ov_b["kpis"]["dedup_recorded_trials"] == 4

    # HTTP /leaderboard: best_by_task NEVER picks ModelSmoke (smoke, 100%) over ModelFull (full_split, 50%)!
    lb_b = res_b_client.get("/api/v1/leaderboard?include_legacy=false").json()
    best_by_proto = {b["scoring_protocol"]: b for b in lb_b["best_by_task"]}
    assert best_by_proto["deepswe-f2p-p2p"]["model_canonical"] == "ModelFull"
    assert best_by_proto["deepswe-f2p-p2p"]["run_scope"] == "full_split"
    assert best_by_proto["lrm-eval-report-v1"]["model_canonical"] == "ModelX"
    # ModelX under deepswe-f2p-p2p MUST show proj_b's 0.0 (NOT proj_a's 100.0!)
    mx = next(it for it in lb_b["items"] if it["model_canonical"] == "ModelX")
    assert mx["benchmarks"]["deepswe__1.1__default|deepswe-f2p-p2p|full_split"]["score"] == 0.0

    # 4. Bound comparison_id + same-size reindex -> POST /compare/task-diff MUST return 409 GENERATION_MISMATCH!
    for name, rew in (("cmp-batch-a", 1.0), ("cmp-batch-b", 0.0)):
        bd = hr / name
        bd.mkdir(parents=True)
        (bd / "manifest.json").write_text(json.dumps({"dataset": "deepswe-1.1", "harness": "pi", "model": "Qwen3_8-27B"}))
        (bd / "episodes.jsonl").write_text(json.dumps({"trial_id": f"tr_{name}", "task_id": "t1", "status": "scored", "reward": rew}) + "\n")
        assert ctx.coordinator.hot.scan_single_harbor_batch(bd, force=True) == "indexed"

    adm_c = TestClient(app)
    adm_csrf = login_client(adm_c, "adm", "StrongPassword!1")
    bid_a = make_batch_id("harbor_runs:cmp-batch-a")
    bid_b = make_batch_id("harbor_runs:cmp-batch-b")

    q_r = adm_c.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": adm_csrf},
        json={"subjects": [{"id": "s1", "batch_id": bid_a}, {"id": "s2", "batch_id": bid_b}], "baseline_subject_id": "s1"},
    )
    assert q_r.status_code == 200
    cmp_id = q_r.json()["comparison_id"]

    d1 = adm_c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": adm_csrf},
        json={"comparison_id": cmp_id, "subject_a_id": "s1", "subject_b_id": "s2"},
    )
    assert d1.status_code == 200 and d1.json()["win_tie_loss"]["a_wins"] == 1

    # Overwrite cmp-batch-a episodes.jsonl with EXACT same byte length (1.0 -> 0.0) and reindex!
    (hr / "cmp-batch-a" / "episodes.jsonl").write_text(
        json.dumps({"trial_id": "tr_cmp-batch-a", "task_id": "t1", "status": "scored", "reward": 0.0}) + "\n"
    )
    assert ctx.coordinator.hot.scan_single_harbor_batch(hr / "cmp-batch-a", force=True) == "indexed"

    # Re-querying task-diff with OLD comparison_id MUST return 409 GENERATION_MISMATCH (NEVER 0 wins)!
    d2 = adm_c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": adm_csrf},
        json={"comparison_id": cmp_id, "subject_a_id": "s1", "subject_b_id": "s2"},
    )
    assert d2.status_code == 409
    assert d2.json()["error"]["code"] == "GENERATION_MISMATCH"


def test_same_gen_overwrite_rejection_concurrent_task_diff_and_leaderboard_filters(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Locks @theshy's final M0 Gate API findings:
    1) Manifest v2 same generation_id with mutated payload (reward 1 -> 0, all hashes valid) is REJECTED,
       preserving committed Gen 1 so existing comparison_id still returns a_wins == 1.
    2) Concurrent new generation commit (gen_001 -> gen_002) landing RIGHT BETWEEN generation check and
       episode read inside POST /api/v1/compare/task-diff: in-flight request uses ONE WAL read snapshot
       so it NEVER returns torn Gen 2 scores (still returns Gen 1 a_wins == 1), and the very next call
       returns HTTP 409 GENERATION_MISMATCH!
    3) Leaderboard single-filter (`protocol` only OR `run_scope` only), double-filter, AND official summary
       rows NEVER swallow the unfiltered dimension!
    """
    import hashlib
    import threading

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    fake_tianxi = tmp_path / "tianxi_r3"
    hr = fake_tianxi / "harbor-runs"
    isolated_settings.tianxi_root = fake_tianxi
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    now = utcnow_iso()

    create_user(
        ctx.db,
        username="adm_r3",
        password="StrongPassword!1",
        role="admin",
        projects=["default"],
        scopes=list(ALL_SCOPES),
    )
    adm_c = TestClient(app)
    csrf = login_client(adm_c, "adm_r3", "StrongPassword!1")

    def _write_manifest_v2_batch(name: str, gen_id: str, tid: str, reward: float) -> Path:
        bdir = hr / name
        tdir = bdir / "trials" / tid
        tdir.mkdir(parents=True, exist_ok=True)
        (bdir / "manifest.json").write_text(json.dumps({"dataset": "deepswe-1.1", "harness": "pi", "model": "Qwen3_8-27B"}))
        (bdir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1}))
        (bdir / "episodes.jsonl").write_text(
            json.dumps(
                {
                    "trial_id": tid,
                    "task_id": "t1",
                    "status": "scored",
                    "reward": reward,
                    "result": f"trials/{tid}/result.json",
                }
            )
            + "\n"
        )
        passed = 1 if reward >= 1.0 else 0
        (bdir / "batch-report.json").write_text(
            json.dumps(
                {
                    "totals": {
                        "planned": 1,
                        "recorded": 1,
                        "scored": 1,
                        "reward_sum": reward,
                        "valid_mean": reward,
                        "passed": passed,
                        "outcomes": {"scored": 1},
                    },
                    "tasks": {"t1": {"recorded": 1, "scored": 1}},
                }
            )
        )
        res_p = tdir / "result.json"
        res_p.write_text(json.dumps({"reward": reward}))
        (bdir / "trial-digests.jsonl").write_text(
            json.dumps({"trial_id": tid, "result": {"rel_path": f"trials/{tid}/result.json", "sha256": _sha(res_p)}}) + "\n"
        )
        top_names = ["manifest.json", "episodes.jsonl", "batch-status.json", "batch-report.json", "trial-digests.jsonl"]
        pm = {
            "schema_version": 2,
            "batch": name,
            "generation_id": gen_id,
            "top_level_files": {n: {"sha256": _sha(bdir / n), "size": (bdir / n).stat().st_size} for n in top_names},
            "absent_optional_files": ["official-result.json", "vlmevalkit/metrics.json", "selection.json", "run-config.json"],
        }
        (bdir / "publish-manifest.json").write_text(json.dumps(pm))
        return bdir

    # --- 1. Same generation_id overwrite rejection via Manifest v2 + comparison_id stability ---
    dir_a = _write_manifest_v2_batch("v2-batch-a", "gen_v2_001", "11111111-1111-1111-1111-111111111111", 1.0)
    dir_b = _write_manifest_v2_batch("v2-batch-b", "gen_v2_001", "22222222-2222-2222-2222-222222222222", 0.0)
    assert ctx.coordinator.hot.scan_single_harbor_batch(dir_a, force=True) == "indexed"
    assert ctx.coordinator.hot.scan_single_harbor_batch(dir_b, force=True) == "indexed"

    # Idempotent rescan of identical content under same generation_id succeeds
    assert ctx.coordinator.hot.scan_single_harbor_batch(dir_a, force=True) == "indexed"

    bid_a = make_batch_id("harbor_runs:v2-batch-a")
    bid_b = make_batch_id("harbor_runs:v2-batch-b")
    q_r = adm_c.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={"subjects": [{"id": "s1", "batch_id": bid_a}, {"id": "s2", "batch_id": bid_b}], "baseline_subject_id": "s1"},
    )
    assert q_r.status_code == 200
    cmp_id = q_r.json()["comparison_id"]

    # Valid Manifest v2 reusing generation_id='gen_v2_001' with reward mutated 1.0 -> 0.0 MUST BE REJECTED!
    _write_manifest_v2_batch("v2-batch-a", "gen_v2_001", "11111111-1111-1111-1111-111111111111", 0.0)
    assert ctx.coordinator.hot.scan_single_harbor_batch(dir_a, force=True) == "rejected"

    # Existing comparison_id MUST still report a_wins == 1 (NOT 0!)
    td_same_gen = adm_c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": cmp_id, "subject_a_id": "s1", "subject_b_id": "s2"},
    )
    assert td_same_gen.status_code == 200
    assert td_same_gen.json()["win_tie_loss"]["a_wins"] == 1

    # --- 2. Concurrent NEW generation commit landing BETWEEN generation check & episode read in /compare/task-diff ---
    _write_manifest_v2_batch("v2-batch-a", "gen_v2_002", "11111111-1111-1111-1111-111111111111", 0.0)
    orig_get_ep = ctx.compare._get_batch_episode_map
    injected = {"done": False, "result": None}

    def _hooked_get_ep(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            # Commit gen_v2_002 from a background thread right after generation check passed!
            t = threading.Thread(
                target=lambda: injected.__setitem__(
                    "result", ctx.coordinator.hot.scan_single_harbor_batch(dir_a, force=True)
                )
            )
            t.start()
            t.join()
        return orig_get_ep(*args, **kwargs)

    ctx.compare._get_batch_episode_map = _hooked_get_ep
    try:
        td_inflight = adm_c.post(
            "/api/v1/compare/task-diff",
            headers={"X-CSRF-Token": csrf},
            json={"comparison_id": cmp_id, "subject_a_id": "s1", "subject_b_id": "s2"},
        )
    finally:
        ctx.compare._get_batch_episode_map = orig_get_ep

    assert injected["result"] == "indexed"
    # In-flight request held a consistent WAL snapshot -> MUST see Gen 1's a_wins == 1 (NEVER torn Gen 2 a_wins == 0!)
    assert td_inflight.status_code == 200
    assert td_inflight.json()["win_tie_loss"]["a_wins"] == 1

    # Next request sees gen_v2_002 != gen_v2_001 -> HTTP 409 GENERATION_MISMATCH!
    td_after = adm_c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": cmp_id, "subject_a_id": "s1", "subject_b_id": "s2"},
    )
    assert td_after.status_code == 409
    assert td_after.json()["error"]["code"] == "GENERATION_MISMATCH"

    # --- 3. Leaderboard single-filter, double-filter, AND official summary rows preserve unfiltered dimensions ---
    with ctx.db.transaction() as conn:
        # Trial-scored model: ModelFilter on deepswe__1.1__default with 3 (protocol, run_scope) cells
        trial_specs = [
            ("b_flt_a_full", "ModelFilter", "pi", "deepswe", "deepswe__1.1__default", "full_split", "proto_a", 0.50),
            ("b_flt_a_smoke", "ModelFilter", "pi", "deepswe", "deepswe__1.1__default", "smoke", "proto_a", 1.00),
            ("b_flt_b_full", "ModelFilter", "pi", "deepswe", "deepswe__1.1__default", "full_split", "proto_b", 0.75),
        ]
        for bid, model, har, slug, rel_id, scope, proto, rew in trial_specs:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    model_canonical, harness, runner, run_scope, scoring_protocol,
                    planned, recorded, scored, valid_mean, generation_id, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'system', 'approved_public',
                          ?, ?, 'DeepSWE', ?, ?, 'harbor', ?, ?, 10, 10, 10, ?, 'g_flt', 'd_flt', ?)
                """,
                (bid, f"loc:{bid}", bid, slug, rel_id, model, har, scope, proto, rew, now),
            )
            conn.execute(
                """
                INSERT INTO trial_protocol_scores (
                    canonical_trial_key, scoring_protocol, batch_id, trial_id,
                    project_id, source_namespace, source_priority_rank,
                    dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                ) VALUES (?, ?, ?, ?, 'default', 'harbor_runs', 1, ?, 't1', 'scored', ?, 1, '["harbor_runs"]')
                """,
                (f"key_{bid}", proto, bid, f"tr_{bid}", rel_id, rew),
            )

        # Official-summary-only rows (0 trial_protocol_scores rows, score comes strictly from official_results!)
        off_specs = [
            ("b_off_a_full", "ModelOfficial", "vlmeval", "cmmmu", "cmmmu__2026-11-24__val", "full_split", "off_proto_a", 0.60),
            ("b_off_a_smoke", "ModelOfficial", "vlmeval", "cmmmu", "cmmmu__2026-11-24__val", "smoke", "off_proto_a", 0.80),
            ("b_off_b_full", "ModelOfficial", "vlmeval", "cmmmu", "cmmmu__2026-11-24__val", "full_split", "off_proto_b", 0.70),
        ]
        for bid, model, har, slug, rel_id, scope, proto, off_score in off_specs:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    model_canonical, harness, runner, run_scope, scoring_protocol,
                    official_status, planned, recorded, scored, valid_mean, generation_id, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'system', 'approved_public',
                          ?, ?, 'CMMMU', ?, ?, 'vlmevalkit', ?, ?, 'official_verified', 100, 0, 0, NULL, 'g_off', 'd_off', ?)
                """,
                (bid, f"loc:{bid}", bid, slug, rel_id, model, har, scope, proto, now),
            )
            conn.execute(
                """
                INSERT INTO official_results (
                    batch_id, dataset, metric, score, denominator, source, origin, fault_policy, attached_by, attached_at
                ) VALUES (?, 'CMMMU', 'acc', ?, 100, 'official-result.json', 'batch_file', 'official_protocol', 'indexer', ?)
                """,
                (bid, off_score, now),
            )

    def _find_bm_map(url: str, model_name: str) -> dict:
        resp = adm_c.get(url)
        assert resp.status_code == 200
        item = next(it for it in resp.json()["items"] if it["model_canonical"] == model_name)
        return item["benchmarks"]

    # 3a. No filters -> all 3 cells for ModelFilter AND all 3 cells for ModelOfficial
    mf_none = _find_bm_map("/api/v1/leaderboard?include_legacy=false", "ModelFilter")
    assert set(mf_none.keys()) == {
        "deepswe__1.1__default|proto_a|full_split",
        "deepswe__1.1__default|proto_a|smoke",
        "deepswe__1.1__default|proto_b|full_split",
    }
    mo_none = _find_bm_map("/api/v1/leaderboard?include_legacy=false", "ModelOfficial")
    assert set(mo_none.keys()) == {
        "cmmmu__2026-11-24__val|off_proto_a|full_split",
        "cmmmu__2026-11-24__val|off_proto_a|smoke",
        "cmmmu__2026-11-24__val|off_proto_b|full_split",
    }

    # 3b. Single filter: protocol ONLY -> preserves BOTH full_split AND smoke!
    mf_proto = _find_bm_map("/api/v1/leaderboard?include_legacy=false&protocol=proto_a", "ModelFilter")
    assert set(mf_proto.keys()) == {"deepswe__1.1__default|full_split", "deepswe__1.1__default|smoke"}
    assert mf_proto["deepswe__1.1__default|full_split"]["score"] == 50.0
    assert mf_proto["deepswe__1.1__default|smoke"]["score"] == 100.0

    mo_proto = _find_bm_map("/api/v1/leaderboard?include_legacy=false&protocol=off_proto_a", "ModelOfficial")
    assert set(mo_proto.keys()) == {"cmmmu__2026-11-24__val|full_split", "cmmmu__2026-11-24__val|smoke"}
    assert mo_proto["cmmmu__2026-11-24__val|full_split"]["score"] == 60.0
    assert mo_proto["cmmmu__2026-11-24__val|smoke"]["score"] == 80.0

    # 3c. Single filter: run_scope ONLY -> preserves BOTH protocol A AND protocol B!
    mf_scope = _find_bm_map("/api/v1/leaderboard?include_legacy=false&run_scope=full_split", "ModelFilter")
    assert set(mf_scope.keys()) == {"deepswe__1.1__default|proto_a", "deepswe__1.1__default|proto_b"}
    assert mf_scope["deepswe__1.1__default|proto_a"]["score"] == 50.0
    assert mf_scope["deepswe__1.1__default|proto_b"]["score"] == 75.0

    mo_scope = _find_bm_map("/api/v1/leaderboard?include_legacy=false&run_scope=full_split", "ModelOfficial")
    assert set(mo_scope.keys()) == {"cmmmu__2026-11-24__val|off_proto_a", "cmmmu__2026-11-24__val|off_proto_b"}
    assert mo_scope["cmmmu__2026-11-24__val|off_proto_a"]["score"] == 60.0
    assert mo_scope["cmmmu__2026-11-24__val|off_proto_b"]["score"] == 70.0

    # 3d. Double filter: protocol + run_scope -> clean release key with exact score!
    mf_both_full = _find_bm_map("/api/v1/leaderboard?include_legacy=false&protocol=proto_a&run_scope=full_split", "ModelFilter")
    assert list(mf_both_full.keys()) == ["deepswe__1.1__default"] and mf_both_full["deepswe__1.1__default"]["score"] == 50.0

    mf_both_smoke = _find_bm_map("/api/v1/leaderboard?include_legacy=false&protocol=proto_a&run_scope=smoke", "ModelFilter")
    assert list(mf_both_smoke.keys()) == ["deepswe__1.1__default"] and mf_both_smoke["deepswe__1.1__default"]["score"] == 100.0

    mo_both_smoke = _find_bm_map("/api/v1/leaderboard?include_legacy=false&protocol=off_proto_a&run_scope=smoke", "ModelOfficial")
    assert list(mo_both_smoke.keys()) == ["cmmmu__2026-11-24__val"] and mo_both_smoke["cmmmu__2026-11-24__val"]["score"] == 80.0


def test_project_migration_reindex_same_generation_syncs_authz_scores_and_search(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Locks @theshy's authorization regression check:
    Moving a source in source_approval_registry.yaml from proj_a -> proj_b without changing ANY batch
    files, generation_id, or content digest, restarting the app, and calling
    POST /api/v1/admin/reindex/{batch_id} MUST atomically update batches, trial_protocol_scores,
    and search_fts so User A gets 403/hidden and User B gets 200/visible — while still rejecting
    same-generation content mutations!"""
    import hashlib

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    fake_tianxi = tmp_path / "tianxi_mig"
    bdir = fake_tianxi / "harbor-runs" / "migrate-v2-batch"
    tid = "33333333-3333-3333-3333-333333333333"
    tdir = bdir / "trials" / tid
    tdir.mkdir(parents=True)

    (bdir / "manifest.json").write_text(
        json.dumps({"dataset": "deepswe-1.1", "harness": "pi", "model": "MigratedModel"})
    )
    (bdir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1}))
    (bdir / "episodes.jsonl").write_text(
        json.dumps(
            {
                "trial_id": tid,
                "task_id": "t_mig",
                "status": "scored",
                "reward": 1.0,
                "result": f"trials/{tid}/result.json",
            }
        )
        + "\n"
    )
    (bdir / "batch-report.json").write_text(
        json.dumps(
            {
                "totals": {
                    "planned": 1,
                    "recorded": 1,
                    "scored": 1,
                    "reward_sum": 1.0,
                    "valid_mean": 1.0,
                    "passed": 1,
                    "outcomes": {"scored": 1},
                },
                "tasks": {"t_mig": {"recorded": 1, "scored": 1}},
            }
        )
    )
    res_p = tdir / "result.json"
    res_p.write_text(json.dumps({"reward": 1.0}))
    (bdir / "trial-digests.jsonl").write_text(
        json.dumps({"trial_id": tid, "result": {"rel_path": f"trials/{tid}/result.json", "sha256": _sha(res_p)}}) + "\n"
    )
    top_names = ["manifest.json", "episodes.jsonl", "batch-status.json", "batch-report.json", "trial-digests.jsonl"]
    (bdir / "publish-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "batch": "migrate-v2-batch",
                "generation_id": "gen_mig_001",
                "top_level_files": {n: {"sha256": _sha(bdir / n), "size": (bdir / n).stat().st_size} for n in top_names},
                "absent_optional_files": ["official-result.json", "vlmevalkit/metrics.json", "selection.json", "run-config.json"],
            }
        )
    )

    cfg_dir = tmp_path / "cfg_mig"
    _setup_custom_config(
        cfg_dir,
        [
            {
                "source_root": str(bdir),
                "match_mode": "dir",
                "project_id": "proj_a",
                "owner_id": "owner_a",
                "approval_status": "approved_restricted",
                "approved_evidence_classes": ["summary_dto"],
            }
        ],
    )
    isolated_settings.tianxi_root = fake_tianxi
    isolated_settings.config_dir = cfg_dir

    # Phase 1: Ingest under proj_a
    app1 = create_app(isolated_settings, run_startup_scan=False)
    ctx1 = app1.state.ctx
    assert ctx1.coordinator.hot.scan_single_harbor_batch(bdir, force=True) == "indexed"
    create_user(ctx1.db, username="adm_mig", password="StrongPassword!1", role="admin", projects=["default", "proj_a", "proj_b"], scopes=list(ALL_SCOPES))
    create_user(ctx1.db, username="user_a", password="StrongPassword!2", role="researcher", projects=["proj_a"], scopes=[])
    create_user(ctx1.db, username="user_b", password="StrongPassword!3", role="researcher", projects=["proj_b"], scopes=[])

    bid = make_batch_id("harbor_runs:migrate-v2-batch")
    ca1, cb1 = TestClient(app1), TestClient(app1)
    login_client(ca1, "user_a", "StrongPassword!2")
    login_client(cb1, "user_b", "StrongPassword!3")
    assert ca1.get(f"/api/v1/batches/{bid}").status_code == 200
    assert cb1.get(f"/api/v1/batches/{bid}").status_code == 403
    assert any(it["batch_id"] == bid for it in ca1.get("/api/v1/scores/deduplicated").json()["items"])
    assert not any(it["batch_id"] == bid for it in cb1.get("/api/v1/scores/deduplicated").json()["items"])
    assert any(it["entity_id"] == bid for it in ca1.get("/api/v1/search?q=migrate-v2-batch").json()["items"])
    assert not any(it["entity_id"] == bid for it in cb1.get("/api/v1/search?q=migrate-v2-batch").json()["items"])
    ctx1.db.close_thread_connection()

    # Phase 2: Migrate source in approval YAML from proj_a -> proj_b (owner_a -> owner_b, approved_public),
    # restart app, and call normal POST /api/v1/admin/reindex/{batch_id} with ZERO file/generation changes!
    _setup_custom_config(
        cfg_dir,
        [
            {
                "source_root": str(bdir),
                "match_mode": "dir",
                "project_id": "proj_b",
                "owner_id": "owner_b",
                "approval_status": "approved_public",
                "approved_evidence_classes": ["summary_dto", "atif_trajectory"],
            }
        ],
    )
    app2 = create_app(isolated_settings, run_startup_scan=False)
    adm2, ca2, cb2 = TestClient(app2), TestClient(app2), TestClient(app2)
    adm_csrf = login_client(adm2, "adm_mig", "StrongPassword!1")
    login_client(ca2, "user_a", "StrongPassword!2")
    login_client(cb2, "user_b", "StrongPassword!3")

    re_r = adm2.post(f"/api/v1/admin/reindex/{bid}", headers={"X-CSRF-Token": adm_csrf})
    assert re_r.status_code == 200 and re_r.json()["status"] == "indexed"

    # Now User A MUST get 403 / zero leakage across ALL endpoints, and User B MUST get 200 / full access!
    assert ca2.get(f"/api/v1/batches/{bid}").status_code == 403
    b_det = cb2.get(f"/api/v1/batches/{bid}")
    assert b_det.status_code == 200
    assert b_det.json()["project_id"] == "proj_b"
    assert b_det.json()["owner_id"] == "owner_b"
    assert b_det.json()["evidence_approval"] == "approved_public"

    assert not any(it["batch_id"] == bid for it in ca2.get("/api/v1/scores/deduplicated").json()["items"])
    assert any(it["batch_id"] == bid for it in cb2.get("/api/v1/scores/deduplicated").json()["items"])
    assert ca2.get("/api/v1/system/overview").json()["kpis"]["dedup_recorded_trials"] == 0
    assert cb2.get("/api/v1/system/overview").json()["kpis"]["dedup_recorded_trials"] == 1
    assert not any(it["model_canonical"] == "MigratedModel" for it in ca2.get("/api/v1/leaderboard?include_legacy=false").json()["items"])
    assert any(it["model_canonical"] == "MigratedModel" for it in cb2.get("/api/v1/leaderboard?include_legacy=false").json()["items"])
    assert not any(it["entity_id"] == bid for it in ca2.get("/api/v1/search?q=migrate-v2-batch").json()["items"])
    assert any(it["entity_id"] == bid for it in cb2.get("/api/v1/search?q=migrate-v2-batch").json()["items"])

    # Phase 3: Mutating content under the SAME generation_id='gen_mig_001' MUST STILL be rejected!
    (bdir / "episodes.jsonl").write_text(
        json.dumps({"trial_id": tid, "task_id": "t_mig", "status": "scored", "reward": 0.0, "result": f"trials/{tid}/result.json"}) + "\n"
    )
    (bdir / "batch-report.json").write_text(
        json.dumps({"totals": {"planned": 1, "recorded": 1, "scored": 1, "reward_sum": 0.0, "valid_mean": 0.0, "passed": 0, "outcomes": {"scored": 1}}, "tasks": {"t_mig": {"recorded": 1, "scored": 1}}})
    )
    (bdir / "publish-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "batch": "migrate-v2-batch",
                "generation_id": "gen_mig_001",
                "top_level_files": {n: {"sha256": _sha(bdir / n), "size": (bdir / n).stat().st_size} for n in top_names},
                "absent_optional_files": ["official-result.json", "vlmevalkit/metrics.json", "selection.json", "run-config.json"],
            }
        )
    )
    re_bad = adm2.post(f"/api/v1/admin/reindex/{bid}", headers={"X-CSRF-Token": adm_csrf})
    assert re_bad.status_code == 200 and re_bad.json()["status"] == "rejected"
    assert cb2.get(f"/api/v1/batches/{bid}").json()["totals"]["valid_mean"] == 1.0
    app2.state.ctx.db.close_thread_connection()
