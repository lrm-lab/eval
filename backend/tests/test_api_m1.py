from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.indexing.identity import make_batch_id
from app.main import create_app
from app.security.authz import ALL_SCOPES
from tests.conftest import create_user, login_client


def test_m1_profiles_3track_verdict_sample_sets_export_and_spa_fallback(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Comprehensive M1 backend contract verification on real 197 data:
    - Entity profiles: /models, /models/{canonical}, /harnesses, /harnesses/{name},
      /benchmarks, /benchmarks/{slug}, /benchmarks/{slug}/hardest-tasks (holdout gated!), /recommend
    - 3-Track Gain vs Guardrail verdict on real 2026-11-20 leaderboard.csv (A-14):
      Qwen3_8-27B vs Qwen3_8-27B_Trained_ck133 (+ threshold slider -1.0pp vs -0.5pp flipping MMMU-Pro!)
    - /compare/options real 197 presets
    - Custom sample_sets CRUD + sample_set_id filtering in /compare/query & /compare/task-diff
    - /compare/saved list & delete + /compare/export (csv/json/markdown/html) with live re-auth
    - /batches/{id}/trials/{tid}/artifacts & /training-capture scope checks
    - SPA fallback serving index.html on frontend routes while /api/* still returns 404 JSON
    """
    fake_dist = tmp_path / "fe_dist"
    (fake_dist / "assets").mkdir(parents=True)
    (fake_dist / "index.html").write_text("<!doctype html><html><body>LRM_EVAL_SPA_OK</body></html>", encoding="utf-8")
    isolated_settings.frontend_dist_dir = fake_dist

    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx

    # Index real 197 representative hot + cold sources
    for bname in (
        "cmmmu-vlmkit-qwen38-27b-20261124-nonthinking",
        "gateway-opencode-qwen197-20261124T065524",
        "synth-v1-qwen-opencode-round1",
        "wildclaw-pi-qwen197-20261124T062059",
    ):
        ctx.coordinator.hot.scan_single_harbor_batch(ctx.settings.harbor_runs_dir / bname, force=True)
    ctx.coordinator.hot.scan_legacy_leaderboard_csv()
    ctx.coordinator.hot.scan_hot_datasets()
    ctx.coordinator.cold.index_native_tb21(force=True)
    ctx.coordinator.cold.index_legacy_agent_json("DeepSWE", force=True)
    ctx.coordinator.cold.index_legacy_agent_json("GAIA", force=True)

    create_user(ctx.db, username="m1_admin", password="StrongAdminPassword!1", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="m1_viewer", password="StrongViewerPassword!2", role="viewer", projects=["default"], scopes=[])
    create_user(ctx.db, username="m1_other", password="StrongOtherPassword!3", role="researcher", projects=["proj_isolated"], scopes=[])

    adm = TestClient(app)
    csrf = login_client(adm, "m1_admin", "StrongAdminPassword!1")
    viewer = TestClient(app)
    login_client(viewer, "m1_viewer", "StrongViewerPassword!2")
    other = TestClient(app)
    login_client(other, "m1_other", "StrongOtherPassword!3")

    # 1. SPA fallback vs /api/* 404 JSON + no-cache header on index.html
    spa_r = adm.get("/leaderboard/agentic")
    assert spa_r.status_code == 200 and "LRM_EVAL_SPA_OK" in spa_r.text
    assert "no-cache" in spa_r.headers.get("cache-control", "")
    api_404 = adm.get("/api/v1/does-not-exist")
    assert api_404.status_code == 404 and api_404.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 1b. Global ⌘K search returns authorized model profiles (E-01) and isolates unauthorized projects
    sr_adm = adm.get("/api/v1/search?q=Qwen3_8-27B")
    assert sr_adm.status_code == 200
    assert any(x["entity_type"] == "model" and x["entity_id"] == "Qwen3_8-27B" for x in sr_adm.json()["items"])
    sr_other = other.get("/api/v1/search?q=Qwen3_8-27B")
    assert sr_other.status_code == 200
    assert not any(x["entity_type"] == "model" and x["entity_id"] == "Qwen3_8-27B" for x in sr_other.json()["items"])

    # 2. Entity profiles: Models, Harnesses, Benchmarks, Hardest Tasks, Recommend
    models_r = adm.get("/api/v1/models")
    assert models_r.status_code == 200 and models_r.json()["total"] >= 1
    m_names = [m["model_canonical"] for m in models_r.json()["items"]]
    assert "Qwen3_8-27B" in m_names

    mp = adm.get("/api/v1/models/Qwen3_8-27B")
    assert mp.status_code == 200
    mp_j = mp.json()
    assert mp_j["header"]["model_canonical"] == "Qwen3_8-27B"
    assert len(mp_j["checkpoint_trends"]) >= 5  # Verified base -> ck133 pairs from 2026-11-20!
    assert mp_j["verdict_suggestion"] is not None

    harnesses_r = adm.get("/api/v1/harnesses")
    assert harnesses_r.status_code == 200 and harnesses_r.json()["total"] >= 3
    hp = adm.get("/api/v1/harnesses/opencode")
    assert hp.status_code == 200 and hp.json()["harness"] == "opencode"

    bms_r = adm.get("/api/v1/benchmarks")
    assert bms_r.status_code == 200 and bms_r.json()["total"] >= 20
    bp = adm.get("/api/v1/benchmarks/terminal-bench")
    assert bp.status_code == 200 and len(bp.json()["mini_leaderboard"]) >= 6
    assert len(bp.json()["scatter_configs"]) >= 1

    hard_r = adm.get("/api/v1/benchmarks/terminal-bench/hardest-tasks?limit=10")
    assert hard_r.status_code == 200 and len(hard_r.json()["items"]) == 10

    rec_r = adm.post(
        "/api/v1/recommend",
        headers={"X-CSRF-Token": csrf},
        json={"track_weights": {"agentic": 1.0}, "min_score_pct": 10.0, "formal_only": True},
    )
    assert rec_r.status_code == 200 and rec_r.json()["total"] >= 1

    # 3. Leaderboard real Harbor filters & enriched metadata (ZERO fake commercial dropdowns)
    lb_r = adm.get("/api/v1/leaderboard?track_slug=agentic&runner=harbor&run_scope=full_split")
    assert lb_r.status_code == 200
    lb_j = lb_r.json()
    assert "available_filters" in lb_j and "harnesses" in lb_j["available_filters"]
    assert all(it["runner"] == "harbor" for it in lb_j["items"])

    # 4. A-14: 3-Track Gain vs Guardrail verdict on real 2026-11-20 Qwen3.8-27B vs ck133 (Text + Vision)
    v_10 = adm.get(
        "/api/v1/compare/verdict?kind=legacy_model"
        "&baseline=Qwen3_8-27B,Qwen3_8-27B_think"
        "&candidate=Qwen3_8-27B_Trained_ck133,Qwen3_8-27B_Trained_ck133_think"
        "&regression_threshold_pp=-1.0"
    )
    assert v_10.status_code == 200
    g_map_10 = {g["benchmark_slug"]: g for g in v_10.json()["gain_vs_guardrail_verdict"]["guardrails"]}
    assert g_map_10["aa-lcr"]["status"] == "regressed"  # 79.67 -> 78.00 = -1.67pp < -1.0pp
    assert g_map_10["hle"]["status"] == "pass"  # 14.60 -> 18.81 = +4.21pp
    assert g_map_10["cmmmu"]["status"] == "pass"  # 60.78 -> 65.11 = +4.33pp
    assert g_map_10["mmmu-pro-10c"]["status"] == "pass"  # 76.07 -> 75.26 = -0.81pp >= -1.0pp
    assert g_map_10["mmlu-pro"]["status"] == "undeterminable"
    assert g_map_10["bfcl-v4"]["status"] == "undeterminable"
    assert g_map_10["global-mmlu-lite"]["status"] == "undeterminable"

    # Tightening threshold to -0.5pp flips MMMU-Pro (-0.81pp) to 'regressed'!
    v_05 = adm.get(
        "/api/v1/compare/verdict?kind=legacy_model"
        "&baseline=Qwen3_8-27B,Qwen3_8-27B_think"
        "&candidate=Qwen3_8-27B_Trained_ck133,Qwen3_8-27B_Trained_ck133_think"
        "&regression_threshold_pp=-0.5"
    )
    g_map_05 = {g["benchmark_slug"]: g for g in v_05.json()["gain_vs_guardrail_verdict"]["guardrails"]}
    assert g_map_05["mmmu-pro-10c"]["status"] == "regressed"

    # Unauthorized user without 'default' project CANNOT view legacy verdict!
    assert other.get("/api/v1/compare/verdict?kind=legacy_model&baseline=Qwen3_8-27B&candidate=Qwen3_8-27B_Trained_ck133").status_code == 403

    # 5. Compare options & presets
    opts_r = adm.get("/api/v1/compare/options")
    assert opts_r.status_code == 200
    preset_ids = {p["preset_id"] for p in opts_r.json()["presets"]}
    assert {"qwen38-3track-ck133", "deepswe-2way", "deepswe-3way", "gaia-2way", "tb21-6way"}.issubset(preset_ids)

    # 6. Custom sample_sets CRUD + sample_set_id filtering in /compare/query & /compare/task-diff
    b_pi5 = make_batch_id("legacy_json:DeepSWE/Pi_v5_Qwen3_8-27B_think.json")
    b_oc = make_batch_id("legacy_json:DeepSWE/OpenClaw_Qwen3_8-27B_think.json")
    ds_tids = [
        r["task_id"]
        for r in ctx.db.fetchall(
            """
            SELECT e1.task_id
            FROM episodes e1 JOIN episodes e2 ON e1.task_id = e2.task_id
            WHERE e1.batch_id = ? AND e2.batch_id = ? AND e1.reward_valid = 1 AND e2.reward_valid = 1
            ORDER BY e1.task_id LIMIT 5
            """,
            (b_pi5, b_oc),
        )
    ]
    assert len(ds_tids) == 5

    ss_create = adm.post(
        "/api/v1/sample-sets",
        headers={"X-CSRF-Token": csrf},
        json={"name": "DeepSWE Top5", "project_id": "default", "dataset_release_id": "deepswe__1.1__default", "task_ids": ds_tids},
    )
    assert ss_create.status_code == 200
    ss_id = ss_create.json()["sample_set_id"]

    # Viewer cannot create sample set (requires Researcher+)
    v_csrf = viewer.cookies.get("lrm_eval_csrf", "")
    assert viewer.post("/api/v1/sample-sets", headers={"X-CSRF-Token": v_csrf}, json={"name": "X", "task_ids": ["t1"]}).status_code == 403

    cmp_ss = adm.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [{"id": "s1", "kind": "batch", "batch_id": b_pi5}, {"id": "s2", "kind": "batch", "batch_id": b_oc}],
            "baseline_subject_id": "s1",
            "scoring_protocol": "deepswe-f2p-p2p",
            "scope": {"sample_set_id": ss_id},
        },
    )
    assert cmp_ss.status_code == 200
    assert cmp_ss.json()["rows"][0]["intersection"]["common_valid_scored_tasks"] == 5
    cmp_id = cmp_ss.json()["comparison_id"]

    # Export in all 4 formats (csv, json, markdown, html)
    for fmt, expect_ct in (
        ("csv", "text/csv"),
        ("json", "application/json"),
        ("markdown", "text/markdown"),
        ("html", "text/html"),
    ):
        ex = adm.post("/api/v1/compare/export", headers={"X-CSRF-Token": csrf}, json={"comparison_id": cmp_id, "format": fmt})
        assert ex.status_code == 200
        assert expect_ct in ex.headers["content-type"]
        assert len(ex.content) > 50

    # Other user without default project access cannot export cmp_id
    o_csrf = other.cookies.get("lrm_eval_csrf", "")
    assert other.post("/api/v1/compare/export", headers={"X-CSRF-Token": o_csrf}, json={"comparison_id": cmp_id, "format": "html"}).status_code == 403

    # Update & delete sample set
    assert adm.put(f"/api/v1/sample-sets/{ss_id}", headers={"X-CSRF-Token": csrf}, json={"name": "DeepSWE Top5 Renamed"}).status_code == 200
    assert adm.delete(f"/api/v1/sample-sets/{ss_id}", headers={"X-CSRF-Token": csrf}).status_code == 200

    # 7. Trial artifacts & training-capture scope enforcement
    tb_bid = make_batch_id("native_harbor:terminal-bench2.1/jhpPiV5__Qwen3_8-27B")
    tb_ep = ctx.db.fetchone("SELECT trial_id FROM episodes WHERE batch_id = ? LIMIT 1", (tb_bid,))
    assert tb_ep is not None
    assert viewer.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/artifacts").status_code == 403
    assert adm.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/artifacts").status_code == 200
    assert viewer.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/training-capture").status_code == 403
    tc_r = adm.get(f"/api/v1/batches/{tb_bid}/trials/{tb_ep['trial_id']}/training-capture")
    assert tc_r.status_code == 200 and tc_r.json()["captured"] is False

    # 8. Hardest tasks returns paired (sample_batch_id, sample_trial_id) from the SAME episode row (never 404!)
    for ht in hard_r.json()["items"][:5]:
        tr_ok = adm.get(f"/api/v1/batches/{ht['sample_batch_id']}/trials/{ht['sample_trial_id']}")
        assert tr_ok.status_code == 200

    # 9. Security regression: legacy_model frozen snapshot saved under another project MUST NOT leak default data!
    create_user(
        ctx.db,
        username="m1_dual",
        password="StrongDualPassword!4",
        role="researcher",
        projects=["default", "proj_isolated"],
        scopes=[],
    )
    dual = TestClient(app)
    dual_csrf = login_client(dual, "m1_dual", "StrongDualPassword!4")
    leg_cmp = dual.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": dual_csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B"]},
                {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_Trained_ck133"]},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert leg_cmp.status_code == 200
    leg_saved = dual.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": dual_csrf},
        json={
            "name": "Cross-project legacy leak attempt",
            "mode": "snapshot",
            "project_id": "proj_isolated",
            "comparison_id": leg_cmp.json()["comparison_id"],
        },
    )
    assert leg_saved.status_code == 200
    short_id = leg_saved.json()["short_id"]
    # proj_isolated-only user without 'default' project MUST get 403 on BOTH open AND export!
    assert other.get(f"/api/v1/compare/s/{short_id}").status_code == 403
    assert other.post("/api/v1/compare/export", headers={"X-CSRF-Token": o_csrf}, json={"comparison_id": short_id, "format": "json"}).status_code == 403

    # 10. Read-only GET /compare/verdict NEVER writes to SQLite and still succeeds under read_only_mode!
    before_ephem = ctx.db.fetchone("SELECT COUNT(*) AS c FROM saved_comparisons")["c"]
    ctx.db.set_read_only_mode(True)
    try:
        ro_verdict = adm.get(
            "/api/v1/compare/verdict?kind=legacy_model"
            "&baseline=Qwen3_8-27B&candidate=Qwen3_8-27B_Trained_ck133&regression_threshold_pp=-1.0"
        )
        assert ro_verdict.status_code == 200
    finally:
        ctx.db.set_read_only_mode(False)
    after_ephem = ctx.db.fetchone("SELECT COUNT(*) AS c FROM saved_comparisons")["c"]
    assert after_ephem == before_ephem

    # 11. A-02 3-way DeepSWE intersection locking: task-diff on 3-way comparison_id MUST stay 39 valid / 48 covered!
    b_pi0 = make_batch_id("legacy_json:DeepSWE/Pi_Qwen3_8-27B_think.json")
    cmp3 = adm.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": b_pi5, "alias": "Pi v5"},
                {"id": "s2", "kind": "batch", "batch_id": b_oc, "alias": "OpenClaw"},
                {"id": "s3", "kind": "batch", "batch_id": b_pi0, "alias": "Pi"},
            ],
            "baseline_subject_id": "s1",
            "scoring_protocol": "deepswe-f2p-p2p",
        },
    )
    assert cmp3.status_code == 200
    assert cmp3.json()["rows"][0]["intersection"]["common_covered_tasks"] == 48
    assert cmp3.json()["rows"][0]["intersection"]["common_valid_scored_tasks"] == 39
    td3 = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": cmp3.json()["comparison_id"], "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all", "page_size": 100},
    )
    assert td3.status_code == 200
    assert td3.json()["intersection"]["common_covered_tasks"] == 48
    assert td3.json()["intersection"]["common_valid_scored_tasks"] == 39
    assert td3.json()["total"] == 39

    # 12. A-02 Frozen snapshot sample set immutability on EDIT & DELETE while preserving live authz!
    ss_one = adm.post(
        "/api/v1/sample-sets",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Frozen1", "project_id": "default", "dataset_release_id": "deepswe__1.1__default", "task_ids": [ds_tids[0]]},
    ).json()["sample_set_id"]
    cmp_one = adm.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [{"id": "s1", "kind": "batch", "batch_id": b_pi5}, {"id": "s2", "kind": "batch", "batch_id": b_oc}],
            "baseline_subject_id": "s1",
            "scoring_protocol": "deepswe-f2p-p2p",
            "scope": {"sample_set_id": ss_one},
        },
    )
    assert cmp_one.status_code == 200
    assert cmp_one.json()["rows"][0]["intersection"]["common_valid_scored_tasks"] == 1
    snap_short = adm.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Frozen 1-Task Snap", "mode": "snapshot", "project_id": "default", "comparison_id": cmp_one.json()["comparison_id"]},
    ).json()["short_id"]

    # Reopen frozen short link returns short_id as comparison_id and 1/1 task-diff
    reopen1 = adm.get(f"/api/v1/compare/s/{snap_short}")
    assert reopen1.status_code == 200
    assert reopen1.json()["result"]["comparison_id"] == snap_short
    td_snap1 = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": snap_short, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_snap1.status_code == 200
    assert td_snap1.json()["intersection"]["common_valid_scored_tasks"] == 1
    assert td_snap1.json()["items"][0]["task_id"] == ds_tids[0]

    # Edit sample set to 2 DIFFERENT tasks -> frozen snapshot matrix & task-diff MUST STILL be the original 1 task!
    assert adm.put(
        f"/api/v1/sample-sets/{ss_one}",
        headers={"X-CSRF-Token": csrf},
        json={"task_ids": [ds_tids[1], ds_tids[2]]},
    ).status_code == 200
    td_snap_after_edit = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": snap_short, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_snap_after_edit.status_code == 200
    assert td_snap_after_edit.json()["intersection"]["common_valid_scored_tasks"] == 1
    assert td_snap_after_edit.json()["items"][0]["task_id"] == ds_tids[0]

    # Delete sample set -> frozen snapshot MUST STILL be 1/1 (NEVER expand to 60/78!), and non-default user still gets 403!
    assert adm.delete(f"/api/v1/sample-sets/{ss_one}", headers={"X-CSRF-Token": csrf}).status_code == 200
    td_snap_after_del = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": snap_short, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_snap_after_del.status_code == 200
    assert td_snap_after_del.json()["intersection"]["common_valid_scored_tasks"] == 1
    assert td_snap_after_del.json()["items"][0]["task_id"] == ds_tids[0]
    exp_after_del = adm.post(
        "/api/v1/compare/export",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": snap_short, "format": "json"},
    )
    assert exp_after_del.status_code == 200
    exp_j = exp_after_del.json()
    assert exp_j["rows"][0]["intersection"]["common_valid_scored_tasks"] == 1
    assert exp_j["rows"][0]["intersection"]["common_covered_tasks"] == 1
    assert other.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": o_csrf},
        json={"comparison_id": snap_short, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    ).status_code == 403
    # Non-default user sees only their authorized writable_projects (NO hardcoded 'default')
    other_opts = other.get("/api/v1/compare/options").json()
    assert "default" not in other_opts["writable_projects"]
    assert any(p.startswith("personal:") for p in other_opts["writable_projects"])

    # 13. A-09 MM-IFEval TSV inline image endpoint & legacy dirs list + A-14 AA-LCR paired legacy sample diff
    dirs_r = adm.get("/api/v1/legacy/dirs")
    assert dirs_r.status_code == 200
    dir_names = {d["name"] for d in dirs_r.json()["items"]}
    assert {"CMMMU", "MMMU_Pro_10c", "MM-IFEval", "RealWorldQA", "AA-LCR-v1.1"}.issubset(dir_names)

    ctx.coordinator.cold.index_legacy_jsonl_offsets("MM-IFEval", force=True)
    ctx.coordinator.cold.index_legacy_jsonl_offsets("MMMU_Pro_10c", force=True)
    ctx.coordinator.cold.index_legacy_jsonl_offsets("RealWorldQA", force=True)

    mmif_list = adm.get("/api/v1/legacy/MM-IFEval/samples?page_size=5").json()
    mmif_hit = next((x for x in mmif_list["items"] if x["media_match_status"] == "unique_hit" and x["media_refs"]), None)
    assert mmif_hit is not None
    tsv_url = mmif_hit["media_refs"][0].get("url")
    assert tsv_url and "/tsv-inline/" in tsv_url
    img_resp = adm.get(tsv_url)
    assert img_resp.status_code == 200 and img_resp.headers["content-type"].startswith("image/") and len(img_resp.content) > 100
    assert viewer.get(tsv_url).status_code == 403  # requires direct_vlm_qa:read

    rw_list = adm.get("/api/v1/legacy/RealWorldQA/samples?page_size=5").json()
    assert rw_list["items"][0]["media_match_status"] == "unregistered_source"
    assert all(not m.get("is_image") for m in rw_list["items"][0]["media_refs"])

    # Legacy AA-LCR paired sample diff via POST /api/v1/compare/task-diff
    leg_q = adm.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B", "Qwen3_8-27B_think"]},
                {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_Trained_ck133", "Qwen3_8-27B_Trained_ck133_think"]},
            ],
            "baseline_subject_id": "s1",
            "regression_threshold_pp": -0.5,
        },
    )
    assert leg_q.status_code == 200
    leg_td = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": leg_q.json()["comparison_id"], "benchmark_slug": "aa-lcr", "filter": "all", "page_size": 20},
    )
    assert leg_td.status_code == 200
    assert leg_td.json()["kind"] == "legacy_sample_diff"
    assert leg_td.json()["benchmark_dir"] == "AA-LCR-v1.1"
    assert leg_td.json()["intersection"]["common_covered_tasks"] > 0
    assert len(leg_td.json()["items"]) > 0
    # Also verify scored legacy multimodal benchmark (MMMU_Pro_10c) returns common_valid > 0
    leg_td_mmmu = adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": leg_q.json()["comparison_id"], "benchmark_slug": "mmmu-pro-10c", "filter": "all", "page_size": 20},
    )
    assert leg_td_mmmu.status_code == 200
    assert leg_td_mmmu.json()["intersection"]["common_valid_scored_tasks"] > 0


def test_m1_s02_legacy_provenance_revocation_path_containment_and_holdout_task_diff(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Locks @theshy's M1 rev2 S-02 findings end-to-end:
    1) GET /api/v1/legacy/dirs filters directories & indexed_samples strictly by source_approval_registry
       (project_id, owner_id), including team revocation without owner_id bypass!
    2) CompareService._query_legacy_sample_diff enforces actual source directory & file provenance from
       source_approval_registry (NOT just `default`), rejects revoked team members even on saved snapshots,
       and rejects path traversal / unregistered model_col identifiers / symlink escapes (400 INVALID_PATH_OR_ID).
    3) CompareService batch `task-diff` (plus `/batches/{id}/tasks`, `/batches/{id}/episodes`, `/scores/deduplicated`,
       `/sample-sets`) enforces `dataset:holdout_read`: callers lacking it see ONLY aggregate metrics in
       `/compare/query`, `/compare/s/{id}`, `/compare/export`, and `/batches/{id}/tasks` (redacted=True, items=[]),
       while `POST /compare/task-diff` & `GET /batches/{id}/episodes` return 403 SCOPE_FORBIDDEN (both initially
       and immediately after revoking `dataset:holdout_read` on an already-bound comparison_id / snapshot!).
    """
    import yaml
    from app.db.migrations import utcnow_iso

    fake_tianxi = tmp_path / "tianxi_s02"
    legacy_root = fake_tianxi / "2026-11-20"
    dir_pub = legacy_root / "IFEval"
    dir_priv = legacy_root / "AA-LCR-v1.1"
    dir_escape = tmp_path / "outside_secret"
    dir_pub.mkdir(parents=True)
    dir_priv.mkdir(parents=True)
    dir_escape.mkdir(parents=True)

    # Leaderboard CSV lives under default; AA-LCR-v1.1 is assigned to team project `proj_legacy_priv`!
    (legacy_root / "leaderboard.csv").write_text(
        "benchmark,ModelA,ModelB\n"
        "IFEval(Prompt-level-strict-accuracy),80.0,85.0\n"
        "AA-LCR(accuracy),79.67,78.00\n",
        encoding="utf-8",
    )
    sample_a = {"0": {"prompt": "Q0", "prediction": "A0", "reward": 1.0}, "1": {"prompt": "Q1", "prediction": "A1", "reward": 0.0}}
    sample_b = {"0": {"prompt": "Q0", "prediction": "B0", "reward": 0.0}, "1": {"prompt": "Q1", "prediction": "B1", "reward": 1.0}}
    (dir_pub / "ModelA.json").write_text(json.dumps(sample_a, indent=4), encoding="utf-8")
    (dir_pub / "ModelB.json").write_text(json.dumps(sample_b, indent=4), encoding="utf-8")
    (dir_priv / "ModelA.json").write_text(json.dumps(sample_a, indent=4), encoding="utf-8")
    (dir_priv / "ModelB.json").write_text(json.dumps(sample_b, indent=4), encoding="utf-8")
    (dir_priv / "UnregisteredExtra.json").write_text(json.dumps(sample_a, indent=4), encoding="utf-8")
    # Symlink escape trap inside IFEval pointing outside legacy_root
    (dir_escape / "OutsideLeak.json").write_text(json.dumps(sample_a, indent=4), encoding="utf-8")
    (dir_pub / "SymlinkEscape.json").symlink_to(dir_escape / "OutsideLeak.json")

    cfg_dir = tmp_path / "cfg_s02"
    cfg_dir.mkdir(parents=True)
    for fname in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml"):
        (cfg_dir / fname).write_text((Path("/home/share-data/eval/config") / fname).read_text(encoding="utf-8"), encoding="utf-8")

    # Pre-create deterministic user IDs so we can bind owner_id == u_priv_owner on team project `proj_legacy_priv`
    isolated_settings.tianxi_root = fake_tianxi
    isolated_settings.config_dir = cfg_dir
    (cfg_dir / "source_approval_registry.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "v1",
                "default_project_id": "default",
                "default_owner_id": "system",
                "sources": [
                    {
                        "source_root": str(dir_pub),
                        "match_mode": "dir",
                        "project_id": "default",
                        "owner_id": "system",
                        "approval_status": "approved_public",
                        "approved_evidence_classes": ["summary_dto", "direct_vlm_qa"],
                    },
                    {
                        "source_root": str(dir_priv),
                        "match_mode": "dir",
                        "project_id": "proj_legacy_priv",
                        "owner_id": "placeholder_owner",
                        "approval_status": "approved_restricted",
                        "approved_evidence_classes": ["summary_dto", "direct_vlm_qa"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    ctx.coordinator.hot.scan_legacy_leaderboard_csv()
    ctx.coordinator.cold.index_legacy_text_json_offsets("IFEval", force=True)
    ctx.coordinator.cold.index_legacy_text_json_offsets("AA-LCR-v1.1", force=True)

    create_user(ctx.db, username="s02_admin", password="StrongPassword!1", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="s02_default", password="StrongPassword!2", role="researcher", projects=["default"], scopes=["direct_vlm_qa:read"])
    uid_dual = create_user(
        ctx.db,
        username="s02_dual_owner",
        password="StrongPassword!3",
        role="researcher",
        projects=["default", "proj_legacy_priv"],
        scopes=["direct_vlm_qa:read"],
    )
    create_user(ctx.db, username="s02_isolated", password="StrongPassword!4", role="researcher", projects=["proj_other"], scopes=["direct_vlm_qa:read"])
    uid_holdout_user = create_user(
        ctx.db,
        username="s02_holdout_user",
        password="StrongPassword!5",
        role="researcher",
        projects=["default"],
        scopes=["dataset:holdout_read"],
    )

    # Bind owner_id of team project `proj_legacy_priv` to uid_dual and reload registries
    reg_data = yaml.safe_load((cfg_dir / "source_approval_registry.yaml").read_text(encoding="utf-8"))
    reg_data["sources"][1]["owner_id"] = uid_dual
    (cfg_dir / "source_approval_registry.yaml").write_text(yaml.safe_dump(reg_data), encoding="utf-8")
    ctx.query.approval_registry.reload()
    ctx.compare.approval_registry.reload()
    ctx.profiles.approval_registry.reload()

    c_adm = TestClient(app)
    csrf_adm = login_client(c_adm, "s02_admin", "StrongPassword!1")
    c_def = TestClient(app)
    csrf_def = login_client(c_def, "s02_default", "StrongPassword!2")
    c_dual = TestClient(app)
    csrf_dual = login_client(c_dual, "s02_dual_owner", "StrongPassword!3")
    c_iso = TestClient(app)
    login_client(c_iso, "s02_isolated", "StrongPassword!4")
    c_hold = TestClient(app)
    csrf_hold = login_client(c_hold, "s02_holdout_user", "StrongPassword!5")

    # --- 1. GET /api/v1/legacy/dirs filters strictly by registry (project_id, owner_id) ---
    adm_dirs = {d["name"] for d in c_adm.get("/api/v1/legacy/dirs").json()["items"]}
    assert adm_dirs == {"IFEval", "AA-LCR-v1.1"}
    def_dirs = {d["name"] for d in c_def.get("/api/v1/legacy/dirs").json()["items"]}
    assert def_dirs == {"IFEval"}  # AA-LCR-v1.1 belongs to proj_legacy_priv -> hidden!
    dual_dirs = {d["name"] for d in c_dual.get("/api/v1/legacy/dirs").json()["items"]}
    assert dual_dirs == {"IFEval", "AA-LCR-v1.1"}
    iso_dirs = c_iso.get("/api/v1/legacy/dirs").json()["items"]
    assert iso_dirs == []  # Neither default nor proj_legacy_priv -> empty!

    # --- 2. CompareService._query_legacy_sample_diff enforces actual source provenance + team revocation ---
    leg_q_def = c_def.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_def},
        json={
            "subjects": [
                {"id": "s1", "kind": "legacy_model", "model_cols": ["ModelA"]},
                {"id": "s2", "kind": "legacy_model", "model_cols": ["ModelB"]},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert leg_q_def.status_code == 200
    cmp_def = leg_q_def.json()["comparison_id"]
    # IFEval is in default -> allowed
    assert c_def.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_def},
        json={"comparison_id": cmp_def, "benchmark_slug": "ifeval"},
    ).status_code == 200
    # AA-LCR-v1.1 is in proj_legacy_priv -> default-only user MUST get 403 PROJECT_FORBIDDEN!
    priv_deny = c_def.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_def},
        json={"comparison_id": cmp_def, "benchmark_slug": "aa-lcr"},
    )
    assert priv_deny.status_code == 403 and priv_deny.json()["error"]["code"] == "PROJECT_FORBIDDEN"

    # Dual user (member + owner_id of proj_legacy_priv) succeeds & saves a frozen snapshot
    leg_q_dual = c_dual.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_dual},
        json={
            "subjects": [
                {"id": "s1", "kind": "legacy_model", "model_cols": ["ModelA"]},
                {"id": "s2", "kind": "legacy_model", "model_cols": ["ModelB"]},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert leg_q_dual.status_code == 200
    cmp_dual = leg_q_dual.json()["comparison_id"]
    snap_dual = c_dual.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_dual},
        json={"name": "Legacy Priv Snap", "mode": "snapshot", "project_id": "default", "comparison_id": cmp_dual},
    ).json()["short_id"]
    assert c_dual.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_dual},
        json={"comparison_id": snap_dual, "benchmark_slug": "aa-lcr"},
    ).status_code == 200

    # Revoke proj_legacy_priv membership from uid_dual (leaving `default`, while owner_id STILL == uid_dual!)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = ? WHERE user_id = ?", (json.dumps(["default"]), uid_dual))
    assert {d["name"] for d in c_dual.get("/api/v1/legacy/dirs").json()["items"]} == {"IFEval"}
    rev_td = c_dual.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_dual},
        json={"comparison_id": snap_dual, "benchmark_slug": "aa-lcr"},
    )
    assert rev_td.status_code == 403 and rev_td.json()["error"]["code"] == "PROJECT_FORBIDDEN"

    # --- 3. Path traversal, unregistered model_col, and symlink escape containment ---
    for bad_col in ("../AA-LCR-v1.1/ModelA", "../../etc/passwd", "UnregisteredExtra", "SymlinkEscape"):
        bad_q = c_def.post(
            "/api/v1/compare/query",
            headers={"X-CSRF-Token": csrf_def},
            json={
                "subjects": [
                    {"id": "s1", "kind": "legacy_model", "model_cols": [bad_col]},
                    {"id": "s2", "kind": "legacy_model", "model_cols": ["ModelB"]},
                ],
                "baseline_subject_id": "s1",
            },
        )
        assert bad_q.status_code == 400 and bad_q.json()["error"]["code"] == "INVALID_PATH_OR_ID"

    # Even if a stored saved_comparisons row were tampered with traversal or symlinked model_col,
    # _query_legacy_sample_diff MUST still reject it!
    now = utcnow_iso()
    for bad_sid, bad_col in (
        ("cmp_forged_trav", "../AA-LCR-v1.1/ModelA"),
        ("cmp_forged_unreg", "UnregisteredExtra"),
        ("cmp_forged_sym", "SymlinkEscape"),
    ):
        with ctx.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO saved_comparisons (
                    short_id, name, mode, project_id, owner_id, config_json, snapshot_json,
                    methodology_ver, created_at, updated_at
                ) VALUES (?, 'Forged', 'ephemeral', 'default', 'system', ?, '{}', 'v2026.11.24', ?, ?)
                """,
                (
                    bad_sid,
                    json.dumps(
                        {
                            "payload": {
                                "subjects": [
                                    {"id": "s1", "kind": "legacy_model", "model_cols": [bad_col]},
                                    {"id": "s2", "kind": "legacy_model", "model_cols": ["ModelB"]},
                                ]
                            },
                            "bound_batches": [],
                            "requires_default_project": True,
                        }
                    ),
                    now,
                    now,
                ),
            )
        r_forged = c_adm.post(
            "/api/v1/compare/task-diff",
            headers={"X-CSRF-Token": csrf_adm},
            json={"comparison_id": bad_sid, "benchmark_slug": "ifeval"},
        )
        assert r_forged.status_code == 400 and r_forged.json()["error"]["code"] == "INVALID_PATH_OR_ID"

    # Also register SymlinkEscape in legacy_leaderboard_rows to prove resolve_within_root blocks symlink escape!
    with ctx.db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO legacy_leaderboard_rows (
                benchmark, metric, benchmark_slug, dataset_release_id, model_col, model_canonical,
                harness, raw_score, norm_score_100, scale_type, source_file
            ) VALUES ('IFEval', 'acc', 'ifeval', 'ifeval__2026-11-20__default', 'SymlinkEscape', 'SymlinkEscape',
                      'direct', 50.0, 50.0, 'pct_0_100', 'leaderboard.csv')
            """
        )
    r_sym = c_adm.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_adm},
        json={"comparison_id": "cmp_forged_sym", "benchmark_slug": "ifeval"},
    )
    assert r_sym.status_code == 400 and r_sym.json()["error"]["code"] == "INVALID_PATH_OR_ID"

    # --- 4. Holdout gate on CompareService batch task-diff, batch tasks/episodes, scores/deduplicated, & scope revocation ---
    with ctx.db.transaction() as conn:
        for bid, rew1, rew2 in (("b_hold_a", 1.0, 0.0), ("b_hold_b", 0.0, 1.0)):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    model_canonical, harness, runner, run_scope, scoring_protocol,
                    planned, recorded, scored, valid_mean, generation_id, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'system', 'approved_restricted',
                          'tianxi-synth-v1', 'tianxi-synth-v1__1.0__holdout', 'tianxi-synth-v1',
                          'HoldoutModel', 'opencode', 'harbor', 'full_split', 'lrm-eval-report-v1',
                          2, 2, 2, 0.5, 'g_hold_1', 'd_hold_1', ?)
                """,
                (bid, f"harbor_runs:{bid}", bid, now),
            )
            for idx, (tid, rew) in enumerate((("secret-holdout-task-001", rew1), ("secret-holdout-task-002", rew2)), 1):
                conn.execute(
                    """
                    INSERT INTO episodes (
                        batch_id, trial_id, canonical_trial_key, generation_id, task_id, status, reward, reward_valid
                    ) VALUES (?, ?, ?, 'g_hold_1', ?, 'scored', ?, 1)
                    """,
                    (bid, f"tr_{bid}_{idx}", f"ck_{bid}_{idx}", tid, rew),
                )
                conn.execute(
                    """
                    INSERT INTO trial_protocol_scores (
                        canonical_trial_key, scoring_protocol, batch_id, trial_id,
                        project_id, source_namespace, source_priority_rank,
                        dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                    ) VALUES (?, 'lrm-eval-report-v1', ?, ?, 'default', 'harbor_runs', 1,
                              'tianxi-synth-v1__1.0__holdout', ?, 'scored', ?, 1, '["harbor_runs"]')
                    """,
                    (f"ck_{bid}_{idx}", bid, f"tr_{bid}_{idx}", tid, rew),
                )

    # 4a. Researcher WITHOUT dataset:holdout_read sees ONLY aggregate metrics on compare/query, saved snapshot, export, and batch/tasks!
    hold_q_no = c_def.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_def},
        json={
            "subjects": [{"id": "s1", "kind": "batch", "batch_id": "b_hold_a"}, {"id": "s2", "kind": "batch", "batch_id": "b_hold_b"}],
            "baseline_subject_id": "s1",
        },
    )
    assert hold_q_no.status_code == 200
    assert hold_q_no.json()["rows"][0]["intersection"]["common_valid_scored_tasks"] == 2
    assert "secret-holdout-task-001" not in hold_q_no.text
    hold_cmp_id = hold_q_no.json()["comparison_id"]

    hold_snap_no = c_def.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_def},
        json={"name": "Holdout Aggregate Only", "mode": "snapshot", "project_id": "default", "comparison_id": hold_cmp_id},
    ).json()["short_id"]
    hold_open_no = c_def.get(f"/api/v1/compare/s/{hold_snap_no}")
    assert hold_open_no.status_code == 200 and hold_open_no.json()["result"]["holdout_details_redacted"] is True
    assert "secret-holdout-task-001" not in hold_open_no.text
    hold_exp_no = c_def.post(
        "/api/v1/compare/export",
        headers={"X-CSRF-Token": csrf_def},
        json={"comparison_id": hold_snap_no, "format": "json"},
    )
    assert hold_exp_no.status_code == 200 and hold_exp_no.json()["holdout_details_redacted"] is True
    assert "secret-holdout-task-001" not in hold_exp_no.text

    # Per-task detail branches MUST reject user lacking dataset:holdout_read with 403 SCOPE_FORBIDDEN!
    for target_cid in (hold_cmp_id, hold_snap_no):
        td_deny = c_def.post(
            "/api/v1/compare/task-diff",
            headers={"X-CSRF-Token": csrf_def},
            json={"comparison_id": target_cid, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
        )
        assert td_deny.status_code == 403
        assert td_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"
        assert td_deny.json()["error"]["detail"]["missing_scope"] == "dataset:holdout_read"

    # Batch tasks returns aggregate pass_at_k_summary ONLY (redacted=True, items=[]); batch episodes returns 403; scores/deduplicated excludes holdout!
    bt_no = c_def.get("/api/v1/batches/b_hold_a/tasks")
    assert bt_no.status_code == 200 and bt_no.json()["redacted"] is True and bt_no.json()["items"] == []
    assert bt_no.json()["unique_tasks"] == 2
    assert c_def.get("/api/v1/batches/b_hold_a/episodes").status_code == 403
    dedup_no = c_def.get("/api/v1/scores/deduplicated").json()["items"]
    assert all(it["batch_id"] not in ("b_hold_a", "b_hold_b") for it in dedup_no)
    assert c_def.post(
        "/api/v1/sample-sets",
        headers={"X-CSRF-Token": csrf_def},
        json={"name": "Probe Holdout", "project_id": "default", "dataset_release_id": "tianxi-synth-v1__1.0__holdout", "task_ids": ["secret-holdout-task-001"]},
    ).status_code == 403

    # 4b. Researcher WITH dataset:holdout_read succeeds on task-diff, batch tasks/episodes, and deduplicated scores
    td_allow = c_hold.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_hold},
        json={"comparison_id": hold_snap_no, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_allow.status_code == 200 and td_allow.json()["total"] == 2
    assert {it["task_id"] for it in td_allow.json()["items"]} == {"secret-holdout-task-001", "secret-holdout-task-002"}
    assert c_hold.get("/api/v1/batches/b_hold_a/tasks").json()["redacted"] is False
    assert c_hold.get("/api/v1/batches/b_hold_a/episodes").status_code == 200

    # 4c. Revoke dataset:holdout_read -> SAME bound snapshot task-diff immediately flips to 403 SCOPE_FORBIDDEN!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET scopes_json = '[]' WHERE user_id = ?", (uid_holdout_user,))
    td_revoked_scope = c_hold.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_hold},
        json={"comparison_id": hold_snap_no, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_revoked_scope.status_code == 403
    assert td_revoked_scope.json()["error"]["code"] == "SCOPE_FORBIDDEN"
    assert c_hold.get("/api/v1/batches/b_hold_a/episodes").status_code == 403
    ctx.db.close_thread_connection()


def test_m1_a02_a11_cross_version_rejection_and_same_release_checksum_drift_grouping(
    tmp_path: Path, isolated_settings: Settings
):
    """M1 rev4 (A-02 / A-11):
    1) Cross-version same task_id with different checksums: matrix marks BOTH releases incomparable AND
       task-diff strictly REJECTS with 400 INCOMPARABLE_PROTOCOL (never computes cross-release win/loss).
    2) Same-release same task_id with different checksums: both matrix and task-diff align strictly by
       (task_id, task_checksum), excluding drifted tasks from common_valid/win_tie_loss and grouping
       them independently in checksum_drift_groups."""
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    create_user(ctx.db, username="adm_ver", password="AdminSecret!2026", role="admin", projects=["default"], scopes=list(ALL_SCOPES))

    with ctx.db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO benchmarks (
                benchmark_slug, display_name, track_slug, track_display_name, goal_type,
                capability, modality, language, status, metadata_json, updated_at
            ) VALUES ('ver-drift-bm', 'Version Drift Bench', 'agentic', 'Agentic', 'optimize_gain',
                      'SWE', 'agent', 'en', 'integrated', '{}', '2026-11-25T00:00:00Z')
            """
        )
        for rel_id, ver in (("ver-drift-bm__1.0__default", "1.0"), ("ver-drift-bm__2.0__default", "2.0")):
            conn.execute(
                """
                INSERT OR IGNORE INTO dataset_releases (
                    dataset_release_id, benchmark_slug, dataset_version, split, runner,
                    known_task_count, metric_name, default_protocol, scale_type, source_approval, updated_at
                ) VALUES (?, 'ver-drift-bm', ?, 'default', 'harbor', 2, 'reward', 'lrm-eval-report-v1', 'ratio_0_1', 'approved', '2026-11-25T00:00:00Z')
                """,
                (rel_id, ver),
            )

        # Cross-version batches: b_v1 (rel 1.0) vs b_v2 (rel 2.0), both containing same task_id 'shared-task-1' with diff checksum
        for bid, rel_id, ck, rew in (
            ("b_ver_v1", "ver-drift-bm__1.0__default", "ck_v1_aaa", 1.0),
            ("b_ver_v2", "ver-drift-bm__2.0__default", "ck_v2_bbb", 0.0),
        ):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'u_admin',
                          'approved', 0, 'ver-drift-bm', ?,
                          'mapped', 'Version Drift Bench', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_v1',
                          1, 1, 1, 1, 0, 1.0, ?, ?, 1.0, 'sha_ver', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, rel_id, rew, int(rew >= 1.0)),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_v1', 'shared-task-1', ?, 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}", f"ck_{bid}", ck, rew),
            )

        # Same-release batches with 1 matching task ('same-ck-task') + 1 drifted task ('drifted-ck-task')
        for bid, drift_ck, same_rew, drift_rew in (
            ("b_same_rel_a", "ck_drift_old", 1.0, 1.0),
            ("b_same_rel_b", "ck_drift_new", 0.0, 0.0),
        ):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'u_admin',
                          'approved', 0, 'ver-drift-bm', 'ver-drift-bm__1.0__default',
                          'mapped', 'Version Drift Bench', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_same',
                          2, 2, 2, 2, 0, 1.0, ?, ?, 1.0, 'sha_same', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, (same_rew + drift_rew) / 2.0, int(same_rew >= 1.0) + int(drift_rew >= 1.0)),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_same', 'same-ck-task', 'ck_matched_001', 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}_same", f"ck_{bid}_same", same_rew),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_same', 'drifted-ck-task', ?, 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}_drift", f"ck_{bid}_drift", drift_ck, drift_rew),
            )

        # Same-release batches where ONLY task is checksum-drifted (0 matched, 1 drifted)
        for bid, drift_ck, rew in (
            ("b_only_drift_a", "ck_alpha", 1.0),
            ("b_only_drift_b", "ck_beta", 0.0),
        ):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'u_admin',
                          'approved', 0, 'ver-drift-bm', 'ver-drift-bm__2.0__default',
                          'mapped', 'Version Drift Bench', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_drift_only',
                          1, 1, 1, 1, 0, 1.0, ?, ?, 1.0, 'sha_od', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, rew, int(rew >= 1.0)),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_drift_only', 'only-drifted-task', ?, 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}_only", f"ck_{bid}_only", drift_ck, rew),
            )

    c = TestClient(app)
    csrf = login_client(c, "adm_ver", "AdminSecret!2026")

    # 1. Cross-version comparison: matrix marks BOTH releases incomparable AND task-diff MUST reject with 400 INCOMPARABLE_PROTOCOL!
    q_cross = c.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_ver_v1"},
                {"id": "s2", "kind": "batch", "batch_id": "b_ver_v2"},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert q_cross.status_code == 200
    cross_body = q_cross.json()
    assert len(cross_body["rows"]) == 2
    assert all(r["comparable"] is False for r in cross_body["rows"])
    cross_cid = cross_body["comparison_id"]

    cross_snap = c.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Cross Version Frozen", "mode": "snapshot", "project_id": "default", "comparison_id": cross_cid},
    ).json()["short_id"]

    for cid in (cross_cid, cross_snap):
        for rel_opt in (None, "ver-drift-bm__1.0__default", "ver-drift-bm__2.0__default"):
            td_payload = {"comparison_id": cid, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"}
            if rel_opt is not None:
                td_payload["dataset_release_id"] = rel_opt
            td_cross = c.post("/api/v1/compare/task-diff", headers={"X-CSRF-Token": csrf}, json=td_payload)
            assert td_cross.status_code == 400
            assert td_cross.json()["error"]["code"] == "INCOMPARABLE_PROTOCOL"

    # 2. Same release with 1 matching task + 1 checksum-drifted task
    q_same = c.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_same_rel_a"},
                {"id": "s2", "kind": "batch", "batch_id": "b_same_rel_b"},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert q_same.status_code == 200
    same_body = q_same.json()
    assert len(same_body["rows"]) == 1
    row_same = same_body["rows"][0]
    assert row_same["comparable"] is True
    assert row_same["intersection"]["common_covered_tasks"] == 1
    assert row_same["intersection"]["common_valid_scored_tasks"] == 1
    assert row_same["version_drift"]["detected"] is True
    assert row_same["version_drift"]["drifted_task_count"] == 1
    assert len(row_same["version_drift"]["checksum_groups"]) == 2

    td_same = c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={"comparison_id": same_body["comparison_id"], "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_same.status_code == 200
    td_s = td_same.json()
    assert td_s["intersection"]["common_covered_tasks"] == 1
    assert td_s["intersection"]["common_valid_scored_tasks"] == 1
    assert td_s["checksum_mismatch_tasks"] == 1
    assert td_s["win_tie_loss"] == {"a_wins": 1, "both_pass": 0, "both_fail": 0, "b_wins": 0, "fault_excluded": 0}
    assert [it["task_id"] for it in td_s["items"]] == ["same-ck-task"]
    assert all(it["task_checksum_match"] is True for it in td_s["items"])
    assert td_s["version_drift_warning"] is True
    assert len(td_s["checksum_drift_groups"]) == 2
    assert {(g["task_id"], g["task_checksum"]) for g in td_s["checksum_drift_groups"]} == {
        ("drifted-ck-task", "ck_drift_old"),
        ("drifted-ck-task", "ck_drift_new"),
    }

    # Asking task-diff for a mismatched dataset_release_id on a same-release comparison MUST also fail 400!
    td_wrong_rel = c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={
            "comparison_id": same_body["comparison_id"],
            "subject_a_id": "s1",
            "subject_b_id": "s2",
            "dataset_release_id": "ver-drift-bm__2.0__default",
            "filter": "all",
        },
    )
    assert td_wrong_rel.status_code == 400
    assert td_wrong_rel.json()["error"]["code"] == "INCOMPARABLE_PROTOCOL"

    # 3. Same release where ONLY task is checksum-drifted -> 0 matched win/loss items, 2 independent checksum groups!
    q_only_drift = c.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_only_drift_a"},
                {"id": "s2", "kind": "batch", "batch_id": "b_only_drift_b"},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert q_only_drift.status_code == 200
    od_row = q_only_drift.json()["rows"][0]
    assert od_row["intersection"]["common_valid_scored_tasks"] == 0
    assert od_row["version_drift"]["drifted_task_count"] == 1
    assert od_row["cells"]["s1"]["common_valid"]["score"] is None

    td_only_drift = c.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf},
        json={
            "comparison_id": q_only_drift.json()["comparison_id"],
            "subject_a_id": "s1",
            "subject_b_id": "s2",
            "filter": "all",
        },
    )
    assert td_only_drift.status_code == 200
    od_td = td_only_drift.json()
    assert od_td["items"] == []
    assert od_td["win_tie_loss"] == {"a_wins": 0, "both_pass": 0, "both_fail": 0, "b_wins": 0, "fault_excluded": 0}
    assert len(od_td["checksum_drift_groups"]) == 2
    assert {(g["task_id"], g["task_checksum"]) for g in od_td["checksum_drift_groups"]} == {
        ("only-drifted-task", "ck_alpha"),
        ("only-drifted-task", "ck_beta"),
    }
    ctx.db.close_thread_connection()


def test_m1_s02_mixed_dataset_per_task_holdout_authorization_and_revocation(
    tmp_path: Path, isolated_settings: Settings
):
    """M1 rev4 (S-02):
    Mixed dataset (`dataset_releases.split='default'`, batch name without 'holdout') storing BOTH normal tasks
    and per-task holdout tasks (`task_definitions.split='holdout'`):
    - Aggregate metrics remain available for users lacking dataset:holdout_read;
    - ALL detail exits (dataset task list, dataset task detail, batch tasks, batch episodes, trial detail,
      deduplicated scores, benchmark hardest tasks, sample sets, compare task-diff, and revoked frozen short links)
      consistently hide or 403-block the per-task holdout entries;
    - Users WITH dataset:holdout_read see both normal and holdout tasks;
    - Revoking dataset:holdout_read immediately enforces redaction/403 on previously saved short links."""
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx

    uid_no_hold = create_user(
        ctx.db,
        username="res_mixed_no_hold",
        password="MixedNoHoldPass!26",
        role="researcher",
        scopes=["trace:read"],
        projects=["default"],
    )
    uid_with_hold = create_user(
        ctx.db,
        username="res_mixed_hold",
        password="MixedHoldPass!26",
        role="researcher",
        scopes=["trace:read", "dataset:holdout_read"],
        projects=["default"],
    )
    assert uid_no_hold and uid_with_hold

    with ctx.db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO benchmarks (
                benchmark_slug, display_name, track_slug, track_display_name, goal_type,
                capability, modality, language, status, metadata_json, updated_at
            ) VALUES ('mixed-bench', 'Mixed Bench', 'agentic', 'Agentic', 'optimize_gain',
                      'Mixed', 'agent', 'en', 'integrated', '{}', '2026-11-25T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO dataset_releases (
                dataset_release_id, benchmark_slug, dataset_version, split, runner,
                known_task_count, metric_name, default_protocol, scale_type, source_approval, updated_at
            ) VALUES ('mixed-bench__1.0__default', 'mixed-bench', '1.0', 'default', 'harbor',
                      3, 'reward', 'lrm-eval-report-v1', 'ratio_0_1', 'approved', '2026-11-25T00:00:00Z')
            """
        )
        for tid, split_val in (
            ("mixed-pub-001", "train"),
            ("mixed-pub-002", "train"),
            ("mixed-secret-holdout-001", "holdout"),
        ):
            conn.execute(
                """
                INSERT INTO task_definitions (
                    dataset_release_id, task_id, task_dir, task_checksum, split, source,
                    environment, difficulty, category, language, tags_json, resources_json
                ) VALUES ('mixed-bench__1.0__default', ?, ?, 'ck_mix', ?, 'synth', 'docker', 'medium', 'mixed', 'en', '[]', '{}')
                """,
                (tid, tid, split_val),
            )
            conn.execute(
                """
                INSERT INTO search_fts (entity_type, entity_id, title, subtitle, body, project_id, required_scope)
                VALUES ('task', ?, ?, 'Mixed Bench', ?, 'default', '')
                """,
                (f"mixed-bench__1.0__default/{tid}", tid, tid),
            )

        for bid, scores in (
            ("b_mix_alpha", [("mixed-pub-001", 1.0), ("mixed-pub-002", 1.0), ("mixed-secret-holdout-001", 0.0)]),
            ("b_mix_beta", [("mixed-pub-001", 0.0), ("mixed-pub-002", 1.0), ("mixed-secret-holdout-001", 1.0)]),
        ):
            mean_v = sum(r for _, r in scores) / len(scores)
            passed_v = sum(1 for _, r in scores if r >= 1.0)
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', 'u_admin',
                          'approved', 0, 'mixed-bench', 'mixed-bench__1.0__default',
                          'mapped', 'Mixed Bench', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_mix',
                          3, 3, 3, 3, 0, 1.0, ?, ?, 1.0, 'sha_mix', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, mean_v, passed_v),
            )
            for idx, (tid, rew) in enumerate(scores, start=1):
                tr_id = f"tr_{bid}_{idx}"
                conn.execute(
                    """
                    INSERT INTO episodes (
                        batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                        attempt_index, status, reward, reward_valid, token_semantics
                    ) VALUES (?, ?, ?, 'gen_mix', ?, 'ck_mix', 1, 'scored', ?, 1, 'unverified_components')
                    """,
                    (bid, tr_id, f"ck_{tr_id}", tid, rew),
                )
                conn.execute(
                    """
                    INSERT INTO trial_protocol_scores (
                        canonical_trial_key, scoring_protocol, batch_id, trial_id,
                        project_id, source_namespace, source_priority_rank,
                        dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                    ) VALUES (?, 'lrm-eval-report-v1', ?, ?, 'default', 'harbor_runs', 1,
                              'mixed-bench__1.0__default', ?, 'scored', ?, 1, '["harbor_runs"]')
                    """,
                    (f"ck_{tr_id}", bid, tr_id, tid, rew),
                )

    c_no = TestClient(app)
    csrf_no = login_client(c_no, "res_mixed_no_hold", "MixedNoHoldPass!26")
    c_yes = TestClient(app)
    csrf_yes = login_client(c_yes, "res_mixed_hold", "MixedHoldPass!26")

    # A. User WITHOUT dataset:holdout_read: aggregate intact, ZERO holdout leakage across EVERY detail exit!
    # 1) Dataset task list & single task detail
    ds_list_no = c_no.get("/api/v1/datasets/mixed-bench__1.0__default/tasks")
    assert ds_list_no.status_code == 200
    ds_no_body = ds_list_no.json()
    assert ds_no_body["redacted"] is True and ds_no_body["hidden_holdout_count"] == 1
    assert ds_no_body["total_including_holdout"] == 3 and ds_no_body["total"] == 2
    assert {it["task_id"] for it in ds_no_body["items"]} == {"mixed-pub-001", "mixed-pub-002"}
    assert "mixed-secret-holdout-001" not in ds_list_no.text
    assert c_no.get("/api/v1/datasets/mixed-bench__1.0__default/tasks/mixed-pub-001").status_code == 200
    ds_ho_deny = c_no.get("/api/v1/datasets/mixed-bench__1.0__default/tasks/mixed-secret-holdout-001")
    assert ds_ho_deny.status_code == 403 and ds_ho_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # 2) Batch aggregate detail, batch tasks, batch episodes, trial detail
    b_det_no = c_no.get("/api/v1/batches/b_mix_alpha")
    assert b_det_no.status_code == 200 and b_det_no.json()["totals"]["unique_tasks"] == 3
    bt_no = c_no.get("/api/v1/batches/b_mix_alpha/tasks")
    assert bt_no.status_code == 200
    bt_no_b = bt_no.json()
    assert bt_no_b["unique_tasks"] == 3 and bt_no_b["redacted"] is True and bt_no_b["hidden_holdout_tasks"] == 1
    assert {it["task_id"] for it in bt_no_b["items"]} == {"mixed-pub-001", "mixed-pub-002"}
    assert "mixed-secret-holdout-001" not in bt_no.text

    be_no = c_no.get("/api/v1/batches/b_mix_alpha/episodes")
    assert be_no.status_code == 200
    be_no_b = be_no.json()
    assert be_no_b["redacted"] is True and be_no_b["hidden_holdout_episodes"] == 1 and be_no_b["total"] == 2
    assert {it["task_id"] for it in be_no_b["items"]} == {"mixed-pub-001", "mixed-pub-002"}
    assert "mixed-secret-holdout-001" not in be_no.text

    assert c_no.get("/api/v1/batches/b_mix_alpha/trials/tr_b_mix_alpha_1").status_code == 200
    tr_ho_deny = c_no.get("/api/v1/batches/b_mix_alpha/trials/tr_b_mix_alpha_3")
    assert tr_ho_deny.status_code == 403 and tr_ho_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # 3) Deduplicated scores, hardest tasks, global search, sample sets
    dedup_no = c_no.get("/api/v1/scores/deduplicated?dataset_release_id=mixed-bench__1.0__default")
    assert dedup_no.status_code == 200
    assert "mixed-secret-holdout-001" not in dedup_no.text
    assert {it["task_id"] for it in dedup_no.json()["items"]} == {"mixed-pub-001", "mixed-pub-002"}

    hard_no = c_no.get("/api/v1/benchmarks/mixed-bench/hardest-tasks")
    assert hard_no.status_code == 200
    assert "mixed-secret-holdout-001" not in hard_no.text
    assert {it["task_id"] for it in hard_no.json()["items"]} == {"mixed-pub-001", "mixed-pub-002"}

    search_no = c_no.get("/api/v1/search?q=mixed-secret-holdout")
    assert search_no.status_code == 200 and "mixed-secret-holdout-001" not in search_no.text

    ss_deny = c_no.post(
        "/api/v1/sample-sets",
        headers={"X-CSRF-Token": csrf_no},
        json={
            "name": "Sneak Holdout Task",
            "project_id": "default",
            "dataset_release_id": "mixed-bench__1.0__default",
            "task_ids": ["mixed-pub-001", "mixed-secret-holdout-001"],
        },
    )
    assert ss_deny.status_code == 403 and ss_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # 4) Compare aggregate + task-diff for user WITHOUT scope
    cmp_no = c_no.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_no},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_mix_alpha"},
                {"id": "s2", "kind": "batch", "batch_id": "b_mix_beta"},
            ],
            "baseline_subject_id": "s1",
        },
    )
    assert cmp_no.status_code == 200
    assert cmp_no.json()["rows"][0]["intersection"]["common_valid_scored_tasks"] == 3
    assert "mixed-secret-holdout-001" not in cmp_no.text

    td_no = c_no.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_no},
        json={"comparison_id": cmp_no.json()["comparison_id"], "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_no.status_code == 200
    td_no_b = td_no.json()
    assert td_no_b["redacted"] is True and td_no_b["hidden_holdout_tasks"] == 1
    assert td_no_b["total"] == 2
    assert {it["task_id"] for it in td_no_b["items"]} == {"mixed-pub-001", "mixed-pub-002"}
    assert td_no_b["win_tie_loss"] == {"a_wins": 1, "both_pass": 1, "both_fail": 0, "b_wins": 0, "fault_excluded": 0}
    assert "mixed-secret-holdout-001" not in td_no.text

    # B. User WITH dataset:holdout_read sees ALL 3 tasks everywhere AND saves frozen short links!
    assert len(c_yes.get("/api/v1/datasets/mixed-bench__1.0__default/tasks").json()["items"]) == 3
    assert c_yes.get("/api/v1/datasets/mixed-bench__1.0__default/tasks/mixed-secret-holdout-001").status_code == 200
    assert c_yes.get("/api/v1/batches/b_mix_alpha/tasks").json()["redacted"] is False
    assert len(c_yes.get("/api/v1/batches/b_mix_alpha/tasks").json()["items"]) == 3
    assert len(c_yes.get("/api/v1/batches/b_mix_alpha/episodes").json()["items"]) == 3
    assert c_yes.get("/api/v1/batches/b_mix_alpha/trials/tr_b_mix_alpha_3").status_code == 200
    assert "mixed-secret-holdout-001" in c_yes.get("/api/v1/benchmarks/mixed-bench/hardest-tasks").text

    cmp_yes = c_yes.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_yes},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_mix_alpha"},
                {"id": "s2", "kind": "batch", "batch_id": "b_mix_beta"},
            ],
            "baseline_subject_id": "s1",
        },
    ).json()
    snap_mix_full = c_yes.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_yes},
        json={"name": "Mixed Full Frozen", "mode": "snapshot", "project_id": "default", "comparison_id": cmp_yes["comparison_id"]},
    ).json()["short_id"]
    td_yes = c_yes.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_mix_full, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    ).json()
    assert td_yes["redacted"] is False and td_yes["total"] == 3
    assert {it["task_id"] for it in td_yes["items"]} == {"mixed-pub-001", "mixed-pub-002", "mixed-secret-holdout-001"}

    # Authorized user creates a mixed sample_set containing the holdout task & saves a frozen comparison short link
    ss_yes = c_yes.post(
        "/api/v1/sample-sets",
        headers={"X-CSRF-Token": csrf_yes},
        json={
            "name": "Mixed Holdout Subset",
            "project_id": "default",
            "dataset_release_id": "mixed-bench__1.0__default",
            "task_ids": ["mixed-pub-001", "mixed-secret-holdout-001"],
        },
    )
    assert ss_yes.status_code == 200
    ss_id = ss_yes.json()["sample_set_id"]
    # User without holdout scope MUST NOT see this sample set in listing!
    assert all(s["sample_set_id"] != ss_id for s in c_no.get("/api/v1/sample-sets").json()["items"])

    cmp_ss = c_yes.post(
        "/api/v1/compare/query",
        headers={"X-CSRF-Token": csrf_yes},
        json={
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": "b_mix_alpha"},
                {"id": "s2", "kind": "batch", "batch_id": "b_mix_beta"},
            ],
            "baseline_subject_id": "s1",
            "scope": {"sample_set_id": ss_id},
        },
    ).json()
    snap_mix_ss = c_yes.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_yes},
        json={"name": "Mixed SampleSet Frozen", "mode": "snapshot", "project_id": "default", "comparison_id": cmp_ss["comparison_id"]},
    ).json()["short_id"]

    # C. Revoke dataset:holdout_read -> BOTH old frozen short links & ALL detail endpoints enforce revocation immediately!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET scopes_json = '[\"trace:read\"]' WHERE user_id = ?", (uid_with_hold,))

    # 1) Full mixed frozen link: aggregate still readable, task-diff immediately redacts holdout task!
    open_full_rev = c_yes.get(f"/api/v1/compare/s/{snap_mix_full}")
    assert open_full_rev.status_code == 200 and open_full_rev.json()["result"]["holdout_details_redacted"] is True
    assert "mixed-secret-holdout-001" not in open_full_rev.text
    td_full_rev = c_yes.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_mix_full, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_full_rev.status_code == 200
    assert td_full_rev.json()["redacted"] is True and td_full_rev.json()["hidden_holdout_tasks"] == 1
    assert {it["task_id"] for it in td_full_rev.json()["items"]} == {"mixed-pub-001", "mixed-pub-002"}
    assert "mixed-secret-holdout-001" not in td_full_rev.text

    # 2) Sample-set frozen link containing holdout task: open, export, AND task-diff all immediately 403!
    assert c_yes.get(f"/api/v1/compare/s/{snap_mix_ss}").status_code == 403
    assert c_yes.post("/api/v1/compare/export", headers={"X-CSRF-Token": csrf_yes}, json={"comparison_id": snap_mix_ss, "format": "json"}).status_code == 403
    assert c_yes.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_mix_ss, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    ).status_code == 403
    assert all(s["sample_set_id"] != ss_id for s in c_yes.get("/api/v1/sample-sets").json()["items"])

    # 3) Direct task & trial detail endpoints immediately 403
    assert c_yes.get("/api/v1/datasets/mixed-bench__1.0__default/tasks/mixed-secret-holdout-001").status_code == 403
    assert c_yes.get("/api/v1/batches/b_mix_alpha/trials/tr_b_mix_alpha_3").status_code == 403
    assert "mixed-secret-holdout-001" not in c_yes.get("/api/v1/batches/b_mix_alpha/episodes").text
    assert "mixed-secret-holdout-001" not in c_yes.get("/api/v1/batches/b_mix_alpha/tasks").text
    ctx.db.close_thread_connection()


def test_m1_s02_version_drift_checksum_groups_holdout_filtering_and_revocation(
    tmp_path: Path, isolated_settings: Settings
):
    """M1 rev5 (S-02):
    `version_drift.checksum_groups` MUST NOT leak holdout per-task checksums when the viewer lacks
    `dataset:holdout_read`, across live query, frozen snapshot reopen, live short link, JSON export,
    and post-revocation revisit — while preserving aggregate denominators and `drifted_task_count`.
    Covers:
    1) Mixed dataset where BOTH a normal task AND a holdout task experience version drift simultaneously;
    2) Whole-holdout release with version drift (checksum_groups=[]);
    3) Legacy pre-filtered snapshot_json fallback redaction."""
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx

    uid_no = create_user(
        ctx.db,
        username="res_drift_no_ho",
        password="DriftNoHoldPass!26",
        role="researcher",
        scopes=["trace:read"],
        projects=["default"],
    )
    uid_yes = create_user(
        ctx.db,
        username="res_drift_yes_ho",
        password="DriftYesHoldPass!26",
        role="researcher",
        scopes=["trace:read", "dataset:holdout_read"],
        projects=["default"],
    )
    assert uid_no and uid_yes

    with ctx.db.transaction() as conn:
        # 1) Mixed dataset benchmark + release (split='default') with 2 normal tasks + 2 holdout tasks
        #    where 1 normal task AND 1 holdout task BOTH have checksum drift!
        conn.execute(
            """
            INSERT OR IGNORE INTO benchmarks (
                benchmark_slug, display_name, track_slug, track_display_name, goal_type,
                capability, modality, language, status, metadata_json, updated_at
            ) VALUES ('mixed-drift-bm', 'Mixed Drift Bench', 'agentic', 'Agentic', 'optimize_gain',
                      'Mixed', 'agent', 'en', 'integrated', '{}', '2026-11-25T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO dataset_releases (
                dataset_release_id, benchmark_slug, dataset_version, split, runner,
                known_task_count, metric_name, default_protocol, scale_type, source_approval, updated_at
            ) VALUES ('mixed-drift-bm__1.0__default', 'mixed-drift-bm', '1.0', 'default', 'harbor',
                      4, 'reward', 'lrm-eval-report-v1', 'ratio_0_1', 'approved', '2026-11-25T00:00:00Z')
            """
        )
        for tid, split_val in (
            ("pub-match-001", "train"),
            ("ho-match-001", "holdout"),
            ("pub-drift-001", "train"),
            ("ho-drift-001", "holdout"),
        ):
            conn.execute(
                """
                INSERT INTO task_definitions (
                    dataset_release_id, task_id, task_dir, task_checksum, split, source,
                    environment, difficulty, category, language, tags_json, resources_json
                ) VALUES ('mixed-drift-bm__1.0__default', ?, ?, 'ck_base', ?, 'synth', 'docker', 'medium', 'mixed', 'en', '[]', '{}')
                """,
                (tid, tid, split_val),
            )

        mixed_specs = (
            (
                "b_mdrift_a",
                [
                    ("pub-match-001", "ck_pub_same", 1.0),
                    ("ho-match-001", "ck_ho_same", 1.0),
                    ("pub-drift-001", "ck_pub_old_111", 1.0),
                    ("ho-drift-001", "ck_ho_secret_aaa", 1.0),
                ],
            ),
            (
                "b_mdrift_b",
                [
                    ("pub-match-001", "ck_pub_same", 0.0),
                    ("ho-match-001", "ck_ho_same", 1.0),
                    ("pub-drift-001", "ck_pub_new_222", 0.0),
                    ("ho-drift-001", "ck_ho_secret_bbb", 0.0),
                ],
            ),
        )
        for bid, eps in mixed_specs:
            mean_v = sum(r for _, _, r in eps) / len(eps)
            passed_v = sum(1 for _, _, r in eps if r >= 1.0)
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', ?,
                          'approved', 0, 'mixed-drift-bm', 'mixed-drift-bm__1.0__default',
                          'mapped', 'Mixed Drift Bench', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_md',
                          4, 4, 4, 4, 0, 1.0, ?, ?, 1.0, 'sha_md', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, uid_yes, mean_v, passed_v),
            )
            for idx, (tid, ck, rew) in enumerate(eps, start=1):
                tr_id = f"tr_{bid}_{idx}"
                conn.execute(
                    """
                    INSERT INTO episodes (
                        batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                        attempt_index, status, reward, reward_valid, token_semantics
                    ) VALUES (?, ?, ?, 'gen_md', ?, ?, 1, 'scored', ?, 1, 'unverified_components')
                    """,
                    (bid, tr_id, f"ck_{tr_id}", tid, ck, rew),
                )

        # 2) Whole-holdout release with 1 matched + 1 drifted task
        conn.execute(
            """
            INSERT INTO dataset_releases (
                dataset_release_id, benchmark_slug, dataset_version, split, runner,
                known_task_count, metric_name, default_protocol, scale_type, source_approval, updated_at
            ) VALUES ('mixed-drift-bm__1.0__holdout', 'mixed-drift-bm', '1.0', 'holdout', 'harbor',
                      2, 'reward', 'lrm-eval-report-v1', 'ratio_0_1', 'approved', '2026-11-25T00:00:00Z')
            """
        )
        for bid, drift_ck, rew in (
            ("b_whdrift_a", "ck_whole_ho_secret_x", 1.0),
            ("b_whdrift_b", "ck_whole_ho_secret_y", 0.0),
        ):
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name, project_id, owner_id,
                    evidence_approval, is_running_placeholder, benchmark_slug, dataset_release_id,
                    identity_status, raw_dataset_name, model_canonical, harness, runner,
                    run_scope, scoring_protocol, fault_policy, official_status, report_origin,
                    generation_id, unique_tasks, planned, recorded, scored, unscored, coverage,
                    valid_mean, passed, pass_threshold, content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, 'default', ?,
                          'approved', 0, 'mixed-drift-bm', 'mixed-drift-bm__1.0__holdout',
                          'mapped', 'Mixed Drift Holdout', 'Qwen3_8-27B', 'pi', 'harbor',
                          'full_split', 'lrm-eval-report-v1', 'exclude_errors_valid_mean',
                          'unofficial_valid_mean', 'batch_report', 'gen_wh',
                          2, 2, 2, 2, 0, 1.0, ?, ?, 1.0, 'sha_wh', '2026-11-25T00:00:00Z')
                """,
                (bid, f"harbor_runs:{bid}", bid, uid_yes, rew, int(rew >= 1.0) * 2),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_wh', 'wh-match-1', 'ck_wh_same', 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}_1", f"ck_{bid}_1", rew),
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                    attempt_index, status, reward, reward_valid, token_semantics
                ) VALUES (?, ?, ?, 'gen_wh', 'wh-drift-1', ?, 1, 'scored', ?, 1, 'unverified_components')
                """,
                (bid, f"tr_{bid}_2", f"ck_{bid}_2", drift_ck, rew),
            )

    c_no = TestClient(app)
    csrf_no = login_client(c_no, "res_drift_no_ho", "DriftNoHoldPass!26")
    c_yes = TestClient(app)
    csrf_yes = login_client(c_yes, "res_drift_yes_ho", "DriftYesHoldPass!26")

    mixed_payload = {
        "subjects": [
            {"id": "s1", "kind": "batch", "batch_id": "b_mdrift_a"},
            {"id": "s2", "kind": "batch", "batch_id": "b_mdrift_b"},
        ],
        "baseline_subject_id": "s1",
    }

    # A. Unauthorized user querying mixed dataset: sees aggregate denominator (2) & drifted_task_count (2),
    #    sees ONLY public checksum groups, NEVER holdout checksums!
    q_no = c_no.post("/api/v1/compare/query", headers={"X-CSRF-Token": csrf_no}, json=mixed_payload)
    assert q_no.status_code == 200
    assert "ck_ho_secret_aaa" not in q_no.text and "ck_ho_secret_bbb" not in q_no.text
    row_no = q_no.json()["rows"][0]
    assert row_no["intersection"]["common_covered_tasks"] == 2
    assert row_no["intersection"]["common_valid_scored_tasks"] == 2
    assert row_no["version_drift"]["detected"] is True
    assert row_no["version_drift"]["drifted_task_count"] == 2
    assert row_no["version_drift"]["checksum_groups_redacted"] is True
    assert row_no["version_drift"]["hidden_holdout_drifted_tasks"] == 1
    assert {g["task_checksum"] for g in row_no["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
    }

    # JSON export on unauthorized ephemeral comparison also strips holdout checksums
    exp_no_eph = c_no.post(
        "/api/v1/compare/export",
        headers={"X-CSRF-Token": csrf_no},
        json={"comparison_id": q_no.json()["comparison_id"], "format": "json"},
    )
    assert exp_no_eph.status_code == 200
    assert "ck_ho_secret_aaa" not in exp_no_eph.text and "ck_ho_secret_bbb" not in exp_no_eph.text
    exp_no_eph_row = exp_no_eph.json()["rows"][0]
    assert exp_no_eph_row["version_drift"]["drifted_task_count"] == 2
    assert {g["task_checksum"] for g in exp_no_eph_row["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
    }

    # B. Authorized user WITH dataset:holdout_read sees ALL 4 checksum groups AND saves frozen snapshot + live link
    q_yes = c_yes.post("/api/v1/compare/query", headers={"X-CSRF-Token": csrf_yes}, json=mixed_payload)
    assert q_yes.status_code == 200
    row_yes = q_yes.json()["rows"][0]
    assert row_yes["version_drift"]["drifted_task_count"] == 2
    assert row_yes["version_drift"]["checksum_groups_redacted"] is False
    assert row_yes["version_drift"]["hidden_holdout_drifted_tasks"] == 0
    assert {g["task_checksum"] for g in row_yes["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
        "ck_ho_secret_aaa",
        "ck_ho_secret_bbb",
    }

    snap_mixed_id = c_yes.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_yes},
        json={"name": "Mixed Drift Frozen", "mode": "snapshot", "project_id": "default", "comparison_id": q_yes.json()["comparison_id"]},
    ).json()["short_id"]
    live_mixed_id = c_yes.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_yes},
        json={"name": "Mixed Drift Live", "mode": "live", "project_id": "default", "comparison_id": q_yes.json()["comparison_id"]},
    ).json()["short_id"]

    # Snapshot at rest in SQLite MUST NOT even persist holdout checksums!
    db_snap_row = ctx.db.fetchone("SELECT snapshot_json FROM saved_comparisons WHERE short_id = ?", (snap_mixed_id,))
    assert db_snap_row is not None
    assert "ck_ho_secret_aaa" not in (db_snap_row["snapshot_json"] or "")
    assert "ck_ho_secret_bbb" not in (db_snap_row["snapshot_json"] or "")

    # Authorized user reopening frozen snapshot & exporting JSON still sees all 4 groups
    open_yes = c_yes.get(f"/api/v1/compare/s/{snap_mixed_id}")
    assert open_yes.status_code == 200
    assert {g["task_checksum"] for g in open_yes.json()["result"]["rows"][0]["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
        "ck_ho_secret_aaa",
        "ck_ho_secret_bbb",
    }
    exp_yes = c_yes.post(
        "/api/v1/compare/export",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_mixed_id, "format": "json"},
    )
    assert exp_yes.status_code == 200
    assert {g["task_checksum"] for g in exp_yes.json()["rows"][0]["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
        "ck_ho_secret_aaa",
        "ck_ho_secret_bbb",
    }

    # C. Unauthorized user opening & JSON-exporting the authorized user's frozen snapshot & live link
    for sid_chk in (snap_mixed_id, live_mixed_id):
        op_no = c_no.get(f"/api/v1/compare/s/{sid_chk}")
        assert op_no.status_code == 200
        assert "ck_ho_secret_aaa" not in op_no.text and "ck_ho_secret_bbb" not in op_no.text
        vd_op = op_no.json()["result"]["rows"][0]["version_drift"]
        assert vd_op["drifted_task_count"] == 2
        assert vd_op["checksum_groups_redacted"] is True
        assert vd_op["hidden_holdout_drifted_tasks"] == 1
        assert {g["task_checksum"] for g in vd_op["checksum_groups"]} == {"ck_pub_old_111", "ck_pub_new_222"}

        ex_no = c_no.post(
            "/api/v1/compare/export",
            headers={"X-CSRF-Token": csrf_no},
            json={"comparison_id": sid_chk, "format": "json"},
        )
        assert ex_no.status_code == 200
        assert "ck_ho_secret_aaa" not in ex_no.text and "ck_ho_secret_bbb" not in ex_no.text
        assert ex_no.json()["holdout_details_redacted"] is True
        vd_ex = ex_no.json()["rows"][0]["version_drift"]
        assert vd_ex["drifted_task_count"] == 2
        assert vd_ex["checksum_groups_redacted"] is True
        assert {g["task_checksum"] for g in vd_ex["checksum_groups"]} == {"ck_pub_old_111", "ck_pub_new_222"}

    # D. Legacy pre-rev5 snapshot_json containing raw holdout checksums MUST STILL be filtered on reopen & export!
    legacy_raw_snap = json.loads(db_snap_row["snapshot_json"])
    legacy_raw_snap["rows"][0]["version_drift"]["checksum_groups"] = [
        {"task_checksum": "ck_pub_old_111", "subject_ids": ["s1"], "task_count": 1},
        {"task_checksum": "ck_pub_new_222", "subject_ids": ["s2"], "task_count": 1},
        {"task_checksum": "ck_ho_secret_aaa", "subject_ids": ["s1"], "task_count": 1},
        {"task_checksum": "ck_ho_secret_bbb", "subject_ids": ["s2"], "task_count": 1},
    ]
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE saved_comparisons SET snapshot_json = ? WHERE short_id = ?",
            (json.dumps(legacy_raw_snap, ensure_ascii=False), snap_mixed_id),
        )
    op_no_leg = c_no.get(f"/api/v1/compare/s/{snap_mixed_id}")
    assert op_no_leg.status_code == 200
    assert "ck_ho_secret_aaa" not in op_no_leg.text and "ck_ho_secret_bbb" not in op_no_leg.text
    assert {g["task_checksum"] for g in op_no_leg.json()["result"]["rows"][0]["version_drift"]["checksum_groups"]} == {
        "ck_pub_old_111",
        "ck_pub_new_222",
    }

    # E. Whole-holdout release with drift: aggregate denominator & drifted_task_count intact, checksum_groups=[]!
    wh_payload = {
        "subjects": [
            {"id": "s1", "kind": "batch", "batch_id": "b_whdrift_a"},
            {"id": "s2", "kind": "batch", "batch_id": "b_whdrift_b"},
        ],
        "baseline_subject_id": "s1",
    }
    q_wh_yes = c_yes.post("/api/v1/compare/query", headers={"X-CSRF-Token": csrf_yes}, json=wh_payload)
    assert q_wh_yes.status_code == 200
    assert {g["task_checksum"] for g in q_wh_yes.json()["rows"][0]["version_drift"]["checksum_groups"]} == {
        "ck_whole_ho_secret_x",
        "ck_whole_ho_secret_y",
    }
    snap_wh_id = c_yes.post(
        "/api/v1/compare/saved",
        headers={"X-CSRF-Token": csrf_yes},
        json={"name": "Whole Holdout Drift Frozen", "mode": "snapshot", "project_id": "default", "comparison_id": q_wh_yes.json()["comparison_id"]},
    ).json()["short_id"]

    q_wh_no = c_no.post("/api/v1/compare/query", headers={"X-CSRF-Token": csrf_no}, json=wh_payload)
    assert q_wh_no.status_code == 200
    assert "ck_whole_ho_secret_x" not in q_wh_no.text and "ck_whole_ho_secret_y" not in q_wh_no.text
    r_wh_no = q_wh_no.json()["rows"][0]
    assert r_wh_no["intersection"]["common_valid_scored_tasks"] == 1
    assert r_wh_no["version_drift"]["detected"] is True
    assert r_wh_no["version_drift"]["drifted_task_count"] == 1
    assert r_wh_no["version_drift"]["checksum_groups"] == []
    assert r_wh_no["version_drift"]["checksum_groups_redacted"] is True
    assert r_wh_no["version_drift"]["hidden_holdout_drifted_tasks"] == 1

    # F. Revoke dataset:holdout_read from creator -> frozen snapshot, live link, JSON export & task-diff ALL immediately strip holdout checksums!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET scopes_json = '[\"trace:read\"]' WHERE user_id = ?", (uid_yes,))

    for sid_rev in (snap_mixed_id, live_mixed_id):
        op_rev = c_yes.get(f"/api/v1/compare/s/{sid_rev}")
        assert op_rev.status_code == 200
        assert "ck_ho_secret_aaa" not in op_rev.text and "ck_ho_secret_bbb" not in op_rev.text
        vd_r = op_rev.json()["result"]["rows"][0]["version_drift"]
        assert op_rev.json()["result"]["rows"][0]["intersection"]["common_valid_scored_tasks"] == 2
        assert vd_r["drifted_task_count"] == 2
        assert vd_r["checksum_groups_redacted"] is True
        assert vd_r["hidden_holdout_drifted_tasks"] == 1
        assert {g["task_checksum"] for g in vd_r["checksum_groups"]} == {"ck_pub_old_111", "ck_pub_new_222"}

        ex_rev = c_yes.post(
            "/api/v1/compare/export",
            headers={"X-CSRF-Token": csrf_yes},
            json={"comparison_id": sid_rev, "format": "json"},
        )
        assert ex_rev.status_code == 200
        assert "ck_ho_secret_aaa" not in ex_rev.text and "ck_ho_secret_bbb" not in ex_rev.text
        vd_er = ex_rev.json()["rows"][0]["version_drift"]
        assert vd_er["drifted_task_count"] == 2
        assert vd_er["checksum_groups_redacted"] is True
        assert {g["task_checksum"] for g in vd_er["checksum_groups"]} == {"ck_pub_old_111", "ck_pub_new_222"}

    td_rev = c_yes.post(
        "/api/v1/compare/task-diff",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_mixed_id, "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"},
    )
    assert td_rev.status_code == 200
    assert "ck_ho_secret_aaa" not in td_rev.text and "ck_ho_secret_bbb" not in td_rev.text
    assert {(g["task_id"], g["task_checksum"]) for g in td_rev.json()["checksum_drift_groups"]} == {
        ("pub-drift-001", "ck_pub_old_111"),
        ("pub-drift-001", "ck_pub_new_222"),
    }

    # Whole-holdout snapshot after creator revocation
    op_wh_rev = c_yes.get(f"/api/v1/compare/s/{snap_wh_id}")
    assert op_wh_rev.status_code == 200
    assert "ck_whole_ho_secret_x" not in op_wh_rev.text and "ck_whole_ho_secret_y" not in op_wh_rev.text
    assert op_wh_rev.json()["result"]["rows"][0]["version_drift"]["checksum_groups"] == []
    assert op_wh_rev.json()["result"]["rows"][0]["version_drift"]["drifted_task_count"] == 1
    ex_wh_rev = c_yes.post(
        "/api/v1/compare/export",
        headers={"X-CSRF-Token": csrf_yes},
        json={"comparison_id": snap_wh_id, "format": "json"},
    )
    assert ex_wh_rev.status_code == 200
    assert "ck_whole_ho_secret_x" not in ex_wh_rev.text and "ck_whole_ho_secret_y" not in ex_wh_rev.text
    assert ex_wh_rev.json()["rows"][0]["version_drift"]["checksum_groups"] == []
    assert ex_wh_rev.json()["rows"][0]["version_drift"]["drifted_task_count"] == 1
    ctx.db.close_thread_connection()
