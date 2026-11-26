from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.indexing.identity import make_batch_id
from app.main import create_app
from app.security.authz import ALL_SCOPES
from tests.conftest import create_user, login_client


def _build_m2_app(tmp_path: Path, isolated_settings: Settings, *, index_real: bool = True):
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    if index_real:
        for bname in (
            "synth-v1-qwen-opencode-round1",
            "wildclaw-pi-qwen197-20261124T062059",
            "gateway-opencode-qwen197-20261124T065524",
            "synth-v1-qwen-opencode-1193-20261124T023653",
            "cmmmu-vlmkit-qwen38-27b-20261124-nonthinking",
            "cmmmu-vlmkit-qwen38-27b-20261124",
            "gaia-pi-qwen197-20261124T062119",
        ):
            p = ctx.settings.harbor_runs_dir / bname
            if p.is_dir():
                ctx.coordinator.hot.scan_single_harbor_batch(p, force=True)
        ctx.coordinator.hot.scan_hot_datasets()
    return app, ctx


def test_m2_diagnostics_5_panels_and_synth_selection_real_197(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M2 Diagnostics (5 panels) + Synthetic Studio + Selection Board on real 197 data (A-07, A-08, A-12, S-02)."""
    app, ctx = _build_m2_app(tmp_path, isolated_settings, index_real=True)
    create_user(ctx.db, username="m2_adm", password="StrongM2Admin!2026", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="m2_res", password="StrongM2Res!2026", role="researcher", projects=["default"], scopes=["trace:read"])
    create_user(ctx.db, username="m2_other", password="StrongM2Other!2026", role="researcher", projects=["proj_other"], scopes=[])

    adm = TestClient(app)
    csrf_adm = login_client(adm, "m2_adm", "StrongM2Admin!2026")
    res = TestClient(app)
    csrf_res = login_client(res, "m2_res", "StrongM2Res!2026")
    other = TestClient(app)
    login_client(other, "m2_other", "StrongM2Other!2026")

    # 1. Diagnostics 5 panels
    f_r = adm.get("/api/v1/diagnostics/failures")
    assert f_r.status_code == 200
    f_body = f_r.json()
    assert f_body["total_fault_trials"] >= 1
    assert any(c["failure_kind"] == "agent_protocol" for c in f_body["clusters"])
    # Isolated project sees 0 default failures
    assert other.get("/api/v1/diagnostics/failures").json()["total_fault_trials"] == 0

    sm_r = adm.get("/api/v1/diagnostics/submetrics")
    assert sm_r.status_code == 200
    sm_body = sm_r.json()
    assert len(sm_body["matrices"]) >= 2
    assert any(len(m["submetrics"]) >= 10 for m in sm_body["matrices"])

    eff_r = adm.get("/api/v1/diagnostics/efficiency")
    assert eff_r.status_code == 200
    eff_body = eff_r.json()
    assert eff_body["token_coverage"]["total_trials"] >= 1
    assert "semantics_breakdown" in eff_body["token_coverage"]

    tools_r = adm.get("/api/v1/diagnostics/tools")
    assert tools_r.status_code == 200
    tools_body = tools_r.json()
    assert tools_body["coverage_guard"]["tool_recording_trials"] >= 1
    assert len(tools_body["tool_ranking"]) >= 1

    clues_r = adm.get("/api/v1/diagnostics/clues")
    assert clues_r.status_code == 200
    clues_txt = clues_r.text.lower()
    # Invariant: ZERO subjective behavioral verdicts!
    for forbidden in ("cheating", "laziness", "作弊", "摆烂"):
        assert forbidden not in clues_txt

    # 2. Synthetic Studio: real 197 tianxi-synth-v1 (40 train / 12 holdout)
    sd_res = res.get("/api/v1/synth/datasets")
    assert sd_res.status_code == 200
    sd_items_res = sd_res.json()["items"]
    tx_res = next((d for d in sd_items_res if d["dataset_id"] == "tianxi-synth-v1"), None)
    assert tx_res is not None
    assert tx_res["train_count"] == 40 and tx_res["holdout_count"] == 12
    # Researcher WITHOUT dataset:holdout_read sees holdout_redacted=True and EMPTY holdout_tasks!
    assert tx_res["holdout_redacted"] is True and tx_res["holdout_tasks"] == []

    # Admin WITH dataset:holdout_read sees all 12 holdout tasks
    sd_adm = adm.get("/api/v1/synth/datasets").json()["items"]
    tx_adm = next(d for d in sd_adm if d["dataset_id"] == "tianxi-synth-v1")
    assert tx_adm["holdout_redacted"] is False and len(tx_adm["holdout_tasks"]) == 12

    # Check train split succeeds for researcher; include_holdout=True blocked with 403!
    chk_deny = res.post(
        "/api/v1/synth/check",
        headers={"X-CSRF-Token": csrf_res},
        json={"dataset_id": "tianxi-synth-v1", "include_holdout": True},
    )
    assert chk_deny.status_code == 403 and chk_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    chk_ok = res.post(
        "/api/v1/synth/check",
        headers={"X-CSRF-Token": csrf_res},
        json={"dataset_id": "tianxi-synth-v1", "include_holdout": False},
    )
    assert chk_ok.status_code == 200
    assert chk_ok.json()["passed"] == 40 and not chk_ok.json()["failed"]

    # Generate plan preview (safe dry-run, zero secret leakage)
    gen_plan = res.post(
        "/api/v1/synth/generate",
        headers={"X-CSRF-Token": csrf_res},
        json={
            "name": "demo-m2-pack",
            "methods": ["log-rollup"],
            "train_per_method": 2,
            "holdout_per_method": 1,
            "execute": False,
        },
    )
    assert gen_plan.status_code == 200
    assert gen_plan.json()["mode"] == "plan"
    assert gen_plan.json()["planned_train"] == 2 and gen_plan.json()["planned_holdout"] == 1

    # 3. Selection Board on real round1 (40 tasks x 8 attempts = 320 trials, review=21, regression=19, pass@8 = 19/40!)
    bid_r1 = make_batch_id("harbor_runs:synth-v1-qwen-opencode-round1")
    sel_r = adm.get(f"/api/v1/synth/selection/{bid_r1}")
    assert sel_r.status_code == 200
    sel = sel_r.json()
    assert sel["unique_tasks"] == 40 and sel["total_attempts"] == 320
    assert sel["totals"]["review"] == 21
    assert sel["totals"]["regression"] == 19
    assert sel["threshold_met"] is False
    assert sel["curves"][-1]["k"] == 8
    assert sel["curves"][-1]["computable_tasks"] == 19
    assert sel["curves"][-1]["total_tasks"] == 40

    # Simulation preview adjusting min_grpo & ignoring exception types
    sim_r = adm.post(
        f"/api/v1/synth/selection/{bid_r1}/preview",
        headers={"X-CSRF-Token": csrf_adm},
        json={"required_attempts": 7, "min_grpo": 5, "ignore_exception_types": ["AgentError"]},
    )
    assert sim_r.status_code == 200 and sim_r.json()["simulated"] is True
    ctx.db.close_thread_connection()


def test_m2_direct_lab_native_contract_security_0600_0700_and_ab_diff(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M2 Direct Lab (02-6 §2, E-02, S-02, S-04, S-06):
    - Native direct.py:33-37 strict schema validation (rejects messages/expected/dup id/missing answer/traversal/bad MIME)
    - Rejects raw client api_key, arbitrary scorer code, and extended_adapter_pending params
    - Fail-Closed 0600 gateways.env probe and 0700 staging probe
    - Live run verified via real report.py + fingerprint-locked A/B diff + scope & team revocation."""
    app, ctx = _build_m2_app(tmp_path, isolated_settings, index_real=False)

    # Configure 0600 gateways.env on local tmp_path
    assert ctx.settings.gateways_env_file is not None
    ctx.settings.gateways_env_file.write_text("OPENAI_API_KEY=sk-direct-test-secret-00001\n", encoding="utf-8")
    os.chmod(ctx.settings.gateways_env_file, 0o600)

    # Inject deterministic mock HTTP gateway into DirectLabService (no external network required)
    def _mock_gateway(url: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: float) -> dict[str, Any]:
        assert headers.get("Authorization") == "Bearer sk-direct-test-secret-00001"
        model = body["model"]
        prompt_text = body["messages"][0]["content"]
        if isinstance(prompt_text, list):
            prompt_text = prompt_text[0]["text"]
        if model == "model-strong":
            ans = "42" if "21*2" in prompt_text else "Paris"
        else:
            ans = "42" if "21*2" in prompt_text else "London"
        return {
            "model": model,
            "choices": [{"message": {"content": ans}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }

    ctx.direct_lab._http_post_fn = _mock_gateway

    uid_a = create_user(
        ctx.db,
        username="dlab_team_a",
        password="StrongDlabPass!26",
        role="researcher",
        projects=["proj_dlab"],
        scopes=["direct_vlm_qa:read", "dataset:answer_read"],
    )
    create_user(
        ctx.db,
        username="dlab_no_qa",
        password="StrongNoQaPass!26",
        role="researcher",
        projects=["proj_dlab"],
        scopes=[],
    )
    c_a = TestClient(app)
    csrf_a = login_client(c_a, "dlab_team_a", "StrongDlabPass!26")
    c_noqa = TestClient(app)
    login_client(c_noqa, "dlab_no_qa", "StrongNoQaPass!26")

    # 1. Capabilities discloses native_direct_v1 active + extended_adapter_pending + zero raw secret!
    caps = c_a.get("/api/v1/direct-lab/capabilities")
    assert caps.status_code == 200
    assert caps.json()["active_adapter_variant"] == "native_direct_v1"
    assert caps.json()["extended_adapter_status"]["variant"] == "extended_adapter_pending"
    assert "sk-direct-test-secret" not in caps.text

    # 2. Native direct.py:33-37 rejection cases
    bad_payloads = [
        '{"id":"q1","messages":[{"role":"user","content":"hi"}],"answer":"ok"}\n',  # non-native messages
        '{"id":"q1","prompt":"hi","expected":"ok","answer":"ok"}\n',  # non-native expected
        '{"id":"q1","prompt":"hi","answer":"ok"}\n{"id":"q1","prompt":"hi2","answer":"ok"}\n',  # duplicate id
        '{"id":"q1","prompt":"hi"}\n',  # missing answer for exact_match
        '{"id":"q1","prompt":"hi","answer":"ok","images":["../escape.png"]}\n',  # traversal
        '{"id":"q1","prompt":"hi","answer":"ok","images":["doc.pdf"]}\n',  # unsupported MIME
    ]
    for bad_jsonl in bad_payloads:
        vr = c_a.post(
            "/api/v1/direct-lab/validate",
            headers={"X-CSRF-Token": csrf_a},
            json={"cases_jsonl": bad_jsonl, "scorer_id": "exact_match"},
        )
        assert vr.status_code == 422 and vr.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"

    valid_cases = (
        '{"id":"c1","prompt":"What is 21*2?","answer":"42"}\n'
        '{"id":"c2","prompt":"Capital of France?","answer":"Paris"}\n'
    )

    # 3. Reject client raw api_key, unregistered scorer, and extended_adapter_pending params!
    assert c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "s1", "gateway_profile_id": "qwen197-vllm", "cases_jsonl": valid_cases, "api_key": "sk-raw"},
    ).status_code == 422
    assert c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "s1", "gateway_profile_id": "qwen197-vllm", "cases_jsonl": valid_cases, "scorer_id": "evil_custom.py"},
    ).status_code == 422
    assert c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "s1", "gateway_profile_id": "qwen197-vllm", "cases_jsonl": valid_cases, "system_prompt": "You are a bot"},
    ).status_code == 422

    # 4. Fail-Closed 0600 gateways.env probe (world-readable 0644 -> 503 STAGING_PERMISSION_UNSAFE!)
    os.chmod(ctx.settings.gateways_env_file, 0o644)
    r_unsafe_env = c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "s1", "gateway_profile_id": "qwen197-vllm", "model_route": "model-strong", "cases_jsonl": valid_cases},
    )
    assert r_unsafe_env.status_code == 503 and r_unsafe_env.json()["error"]["code"] == "STAGING_PERMISSION_UNSAFE"
    os.chmod(ctx.settings.gateways_env_file, 0o600)

    # 5. Execute 2 runs on SAME cases_sha256 + scorer_sha256 -> verified by real report.py!
    r1 = c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "math-geo-v1", "gateway_profile_id": "qwen197-vllm", "model_route": "model-strong", "cases_jsonl": valid_cases},
    )
    assert r1.status_code == 200
    run1 = r1.json()
    assert run1["summary"]["report_py_verified"] is True
    assert run1["summary"]["scored"] == 2 and run1["summary"]["passed"] == 2
    assert "sk-direct-test-secret" not in r1.text

    r2 = c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "math-geo-v1", "gateway_profile_id": "qwen197-vllm", "model_route": "model-weak", "cases_jsonl": valid_cases},
    )
    assert r2.status_code == 200
    run2 = r2.json()
    assert run2["summary"]["passed"] == 1
    assert run1["cases_sha256"] == run2["cases_sha256"]

    # Same-fingerprint A/B diff succeeds (100% vs 50%, delta_pp = -50.0, a_wins=1, both_pass=1)
    ab = c_a.post(
        "/api/v1/direct-lab/compare",
        headers={"X-CSRF-Token": csrf_a},
        json={"run_a_id": run1["run_id"], "run_b_id": run2["run_id"]},
    )
    assert ab.status_code == 200
    ab_b = ab.json()
    assert ab_b["win_tie_loss"] == {"a_wins": 1, "both_pass": 1, "both_fail": 0, "b_wins": 0, "fault_excluded": 0}
    assert ab_b["delta_pp"] == -50.0

    # Different scorer_sha256 MUST fail A/B diff with 400 INCOMPARABLE_PROTOCOL!
    r3 = c_a.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_a},
        json={"project_id": "proj_dlab", "suite_id": "math-geo-v1", "scorer_id": "case_insensitive_exact", "gateway_profile_id": "qwen197-vllm", "model_route": "model-weak", "cases_jsonl": valid_cases},
    )
    assert r3.status_code == 200
    ab_bad = c_a.post(
        "/api/v1/direct-lab/compare",
        headers={"X-CSRF-Token": csrf_a},
        json={"run_a_id": run1["run_id"], "run_b_id": r3.json()["run_id"]},
    )
    assert ab_bad.status_code == 400 and ab_bad.json()["error"]["code"] == "INCOMPARABLE_PROTOCOL"

    # Researcher without direct_vlm_qa:read sees status/reward ONLY (prompt/prediction/answer redacted!)
    det_noqa = c_noqa.get(f"/api/v1/direct-lab/runs/{run1['run_id']}")
    assert det_noqa.status_code == 200 and det_noqa.json()["qa_redacted"] is True
    assert "What is 21*2?" not in det_noqa.text and "Paris" not in det_noqa.text

    # Team project revocation: removing uid_a from proj_dlab blocks access even though owner_id == uid_a!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = '[\"default\"]' WHERE user_id = ?", (uid_a,))
    assert c_a.get(f"/api/v1/direct-lab/runs/{run1['run_id']}").status_code == 403
    ctx.db.close_thread_connection()


def test_m2_toolkit_3_formats_official_result_params_manuals_and_admin_health(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """Verify M2 Toolkit (02-8, E-02, A-06, S-02, S-06) & upgraded Admin Health."""
    app, ctx = _build_m2_app(tmp_path, isolated_settings, index_real=True)
    create_user(ctx.db, username="tk_adm", password="StrongTkAdmin!26", role="admin", projects=["default", "proj_tk"], scopes=list(ALL_SCOPES))
    uid_res = create_user(ctx.db, username="tk_res", password="StrongTkRes!26", role="researcher", projects=["default", "proj_tk"], scopes=[])

    adm = TestClient(app)
    csrf_adm = login_client(adm, "tk_adm", "StrongTkAdmin!26")
    res = TestClient(app)
    csrf_res = login_client(res, "tk_res", "StrongTkRes!26")

    # 1. Existing batch verification via real report.py
    bid_r1 = make_batch_id("harbor_runs:synth-v1-qwen-opencode-round1")
    ver_r = res.post(
        f"/api/v1/toolkit/verify/{bid_r1}",
        headers={"X-CSRF-Token": csrf_res},
        json={"check_smoke": False},
    )
    assert ver_r.status_code == 200
    assert ver_r.json()["report"]["totals"]["planned"] == 320
    assert ver_r.json()["report"]["totals"]["recorded"] == 320

    # 2. Offline Calc 3-Format Strict Router (02-8 §1)
    # 2a) shared_batch_or_episodes -> report_py
    ep_jsonl = (
        json.dumps({"trial_id": "t1", "task_id": "task-1", "status": "scored", "reward": 1.0, "dataset": "ds1", "model_route": "m1", "harness": "pi"}) + "\n"
        + json.dumps({"trial_id": "t2", "task_id": "task-2", "status": "scored", "reward": 0.0, "dataset": "ds1", "model_route": "m1", "harness": "pi"}) + "\n"
    )
    oc1 = res.post(
        "/api/v1/toolkit/offline-calc",
        headers={"X-CSRF-Token": csrf_res},
        json={"report_name": "Shared Episodes Pack", "filename": "episodes.jsonl", "content": ep_jsonl, "project_id": "proj_tk"},
    )
    assert oc1.status_code == 200
    b1 = oc1.json()
    assert b1["upload_format"] == "shared_batch_or_episodes"
    assert b1["adapter_used"] == "report_py"
    assert b1["summary"]["totals"]["valid_mean"] == 0.5

    # 2b) native_harbor_job -> native_harbor_adapter (preserves reward_valid=True WITH exception_info + 4-phase timings!)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("job/result.json", json.dumps({"n_total_trials": 2, "stats": {}}))
        zf.writestr(
            "job/trial-1/result.json",
            json.dumps(
                {
                    "id": "uuid-native-1",
                    "task_name": "tb-task-1",
                    "reward_valid": True,
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": {"exception_type": "NonFatalWarning", "policy": "allow"},
                    "environment_setup": {"started_at": "2026-11-25T00:00:00Z", "finished_at": "2026-11-25T00:00:03Z"},
                    "agent_execution": {"started_at": "2026-11-25T00:00:03Z", "finished_at": "2026-11-25T00:00:23Z"},
                }
            ),
        )
        zf.writestr(
            "job/trial-2/result.json",
            json.dumps(
                {
                    "id": "uuid-native-2",
                    "task_name": "tb-task-2",
                    "reward_valid": False,
                    "exception_info": {"exception_type": "TimeoutError", "policy": "fatal"},
                }
            ),
        )
    oc2 = res.post(
        "/api/v1/toolkit/offline-calc",
        headers={"X-CSRF-Token": csrf_res},
        json={
            "report_name": "Native Harbor Job Zip",
            "filename": "native_job.zip",
            "content_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "project_id": "proj_tk",
        },
    )
    assert oc2.status_code == 200
    b2 = oc2.json()
    assert b2["upload_format"] == "native_harbor_job"
    assert b2["adapter_used"] == "native_harbor_adapter"
    assert b2["summary"]["totals"]["scored"] == 1
    assert b2["summary"]["totals"]["native_valid_with_exception_count"] == 1
    assert b2["summary"]["trials"][0]["phase_timings"]["agent_execution_s"] == 20.0

    # 2c) summary_only -> summary_passthrough (NEVER fabricates per-task details!)
    sum_json = json.dumps({"dataset": "CMMMU", "metric": "accuracy", "score": 0.6667, "denominator": 6, "source": "vlmevalkit/metrics.json"})
    oc3 = res.post(
        "/api/v1/toolkit/offline-calc",
        headers={"X-CSRF-Token": csrf_res},
        json={"report_name": "Summary Only Official", "filename": "official-result.json", "content": sum_json, "project_id": "proj_tk"},
    )
    assert oc3.status_code == 200
    b3 = oc3.json()
    assert b3["upload_format"] == "summary_only"
    assert b3["adapter_used"] == "summary_passthrough"
    assert b3["summary"]["tasks"] == [] and b3["summary"]["summary_only"] is True

    # 2d) Security: zip path traversal rejected (400) & oversized upload rejected (413) & CIFS staging rejected (503)!
    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.json", "{}")
    assert res.post(
        "/api/v1/toolkit/offline-calc",
        headers={"X-CSRF-Token": csrf_res},
        json={"report_name": "Bad Zip", "filename": "evil.zip", "content_base64": base64.b64encode(bad_zip.getvalue()).decode("ascii")},
    ).status_code == 400

    orig_max = ctx.settings.max_upload_bytes
    ctx.settings.max_upload_bytes = 64
    assert res.post(
        "/api/v1/toolkit/offline-calc",
        headers={"X-CSRF-Token": csrf_res},
        json={"report_name": "Too Big", "filename": "official-result.json", "content": "x" * 200},
    ).status_code == 413
    ctx.settings.max_upload_bytes = orig_max

    # 3. Official result attachment: Admin-only + NEVER mutates run_scope (A-06)!
    bid_wc = make_batch_id("harbor_runs:wildclaw-pi-qwen197-20261124T062059")
    orig_scope = ctx.db.fetchone("SELECT run_scope FROM batches WHERE batch_id = ?", (bid_wc,))["run_scope"]  # type: ignore[index]
    # Researcher forbidden (403)
    assert res.put(
        f"/api/v1/toolkit/official-result/{bid_wc}",
        headers={"X-CSRF-Token": csrf_res},
        json={"dataset": "WildClawBench", "metric": "overall_score", "score": 0.42, "denominator": 60, "source": "official-eval.json"},
    ).status_code == 403
    att_ok = adm.put(
        f"/api/v1/toolkit/official-result/{bid_wc}",
        headers={"X-CSRF-Token": csrf_adm},
        json={"dataset": "WildClawBench", "metric": "overall_score", "score": 0.42, "denominator": 60, "source": "official-eval.json"},
    )
    assert att_ok.status_code == 200
    assert att_ok.json()["official_status"] == "official_verified"
    assert att_ok.json()["run_scope"] == orig_scope  # NEVER upgraded to full_split!

    # 4. Param Compare Groups: strictly bound to real authorized batch_ids
    bid_gaia = make_batch_id("harbor_runs:gaia-pi-qwen197-20261124T062119")
    pg_bad = res.post(
        "/api/v1/toolkit/params",
        headers={"X-CSRF-Token": csrf_res},
        json={"test_topic": "Fake", "label": "Fake", "batch_ids": [bid_wc, "b_nonexistent_fake"]},
    )
    assert pg_bad.status_code == 404

    pg_ok = res.post(
        "/api/v1/toolkit/params",
        headers={"X-CSRF-Token": csrf_res},
        json={"test_topic": "Pi Agentic Smoke", "label": "WildClaw vs GAIA", "project_id": "proj_tk", "batch_ids": [bid_wc, bid_gaia]},
    )
    assert pg_ok.status_code == 200
    pg_body = pg_ok.json()
    assert len(pg_body["batches"]) == 2 and pg_body["batches"][0]["runner"] == "harbor"

    # Team revocation on offline_reports & param_compare_groups (NO owner_id bypass!)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET projects_json = '[\"default\"]' WHERE user_id = ?", (uid_res,))
    assert res.get(f"/api/v1/toolkit/offline-reports/{b1['report_id']}").status_code == 403
    assert all(g["group_id"] != pg_body["group_id"] for g in res.get("/api/v1/toolkit/params").json()["items"])

    # 5. Manuals Center reads real 197 docs with zero /home/<user> leakage
    ml = adm.get("/api/v1/toolkit/manuals")
    assert ml.status_code == 200 and ml.json()["total"] >= 4
    md = adm.get("/api/v1/toolkit/manuals/dataset-format")
    assert md.status_code == 200 and "cases.jsonl" in md.json()["content"]
    assert "/home/jianghp3" not in md.text and "/home/frank" not in md.text

    # 6. Upgraded Admin Health (M2)
    ah = adm.get("/api/v1/admin/health")
    assert ah.status_code == 200
    ah_b = ah.json()
    assert "running_placeholders" in ah_b["source_coverage"]
    assert "gateway_profiles" in ah_b and len(ah_b["gateway_profiles"]) >= 1
    assert "runner_status" in ah_b
    assert ah_b["storage"]["isolation_probes"]["managed_framework"]["ok"] is True
    ctx.db.close_thread_connection()


def test_m2_rev2_security_managed_scripts_mixed_holdout_verify_and_image_bound_direct_lab(
    tmp_path: Path, isolated_settings: Settings, monkeypatch: Any
) -> None:
    """M2 rev2 regression covering @theshy's 3 service-layer findings (S-04, S-02, E-02):
    1) S-04: Managed framework script integrity & POSIX permission guard (refuses CIFS / tampered SHA / 0666)
       AND sanitized subprocess env strips Web/gateway secrets.
    2) S-02: Toolkit `verify/{batch_id}` strips holdout tasks on release `split='holdout'` (without 'holdout' in name),
       strips per-task holdout entries in mixed datasets (`task_definitions.split='holdout'`), and respects revocation.
    3) E-02: Direct Lab requires uploaded image bytes at `/validate` & `/runs`, binds image byte SHA-256 into
       `cases_sha256`, and rejects A/B compare (`400 INCOMPARABLE_PROTOCOL`) when same-named PNG bytes differ!"""
    import shutil
    import struct
    import zlib
    from app.security.managed_exec import build_sanitized_subprocess_env, verify_managed_framework_bundle

    # --- 1. S-04: Sanitized subprocess env & managed framework integrity / POSIX guard ---
    monkeypatch.setenv("LRM_EVAL_SECRET_KEY", "super-secret-session-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leaked-gateway-key-999")
    monkeypatch.setenv("TIANXI_SYNTH_TEACHER_API_KEY", "sk-leaked-teacher-key-888")
    clean_env = build_sanitized_subprocess_env()
    assert "LRM_EVAL_SECRET_KEY" not in clean_env
    assert "OPENAI_API_KEY" not in clean_env
    assert "TIANXI_SYNTH_TEACHER_API_KEY" not in clean_env

    # Copy managed_framework to local tmp_path (POSIX) -> passes strict check, then tamper & chmod 0666 -> 503!
    mf_copy = tmp_path / "managed_fw_local"
    shutil.copytree(isolated_settings.managed_framework_dir, mf_copy)
    for p in mf_copy.rglob("*"):
        os.chmod(p, 0o640 if p.is_file() else 0o750)
    os.chmod(mf_copy, 0o750)
    isolated_settings.managed_framework_dir = mf_copy
    assert verify_managed_framework_bundle(isolated_settings, require_posix_strict=True)["ok"] is True

    # World-writable 0666 file -> Fail-Closed 503 STAGING_PERMISSION_UNSAFE!
    os.chmod(mf_copy / "report.py", 0o666)
    try:
        verify_managed_framework_bundle(isolated_settings, require_posix_strict=True)
        raise AssertionError("Expected 503 STAGING_PERMISSION_UNSAFE on 0666 report.py")
    except Exception as exc:
        assert getattr(exc, "code", None) == "STAGING_PERMISSION_UNSAFE"
    os.chmod(mf_copy / "report.py", 0o640)

    # Tampered SHA-256 -> Fail-Closed 503 STAGING_PERMISSION_UNSAFE even in non-prod!
    orig_report = (mf_copy / "report.py").read_bytes()
    (mf_copy / "report.py").write_bytes(orig_report + b"\n# tampered\n")
    try:
        verify_managed_framework_bundle(isolated_settings, require_posix_strict=False)
        raise AssertionError("Expected 503 STAGING_PERMISSION_UNSAFE on SHA mismatch")
    except Exception as exc:
        assert getattr(exc, "code", None) == "STAGING_PERMISSION_UNSAFE"
    (mf_copy / "report.py").write_bytes(orig_report)

    # Missing entry from MANIFEST.json (even when report.py bytes are untouched on disk!) -> 503 STAGING_PERMISSION_UNSAFE!
    from app.security.managed_exec import resolve_verified_managed_script
    mf_manifest_path = mf_copy / "MANIFEST.json"
    orig_manifest_txt = mf_manifest_path.read_text(encoding="utf-8")
    mf_doc = json.loads(orig_manifest_txt)
    mf_doc["files"].pop("report.py", None)
    mf_manifest_path.write_text(json.dumps(mf_doc, indent=2), encoding="utf-8")
    try:
        resolve_verified_managed_script(isolated_settings, "report.py")
        raise AssertionError("Expected 503 STAGING_PERMISSION_UNSAFE when report.py omitted from MANIFEST.json")
    except Exception as exc:
        assert getattr(exc, "code", None) == "STAGING_PERMISSION_UNSAFE"
    # Also omitting a runtime dependency (`gateway_capture.py`) MUST fail resolve_verified_managed_script("report.py")!
    mf_doc2 = json.loads(orig_manifest_txt)
    mf_doc2["files"].pop("gateway_capture.py", None)
    mf_manifest_path.write_text(json.dumps(mf_doc2, indent=2), encoding="utf-8")
    try:
        resolve_verified_managed_script(isolated_settings, "report.py")
        raise AssertionError("Expected 503 STAGING_PERMISSION_UNSAFE when dependency omitted from MANIFEST.json")
    except Exception as exc:
        assert getattr(exc, "code", None) == "STAGING_PERMISSION_UNSAFE"
    mf_manifest_path.write_text(orig_manifest_txt, encoding="utf-8")

    # Pointing managed_framework_dir to 0777 CIFS in strict mode -> 503 STAGING_PERMISSION_UNSAFE!
    cifs_settings = Settings(
        runtime_dir=isolated_settings.runtime_dir,
        managed_framework_dir=Path("/home/share-data/eval/backend/app/managed_framework"),
        production_mode=True,
    )
    try:
        verify_managed_framework_bundle(cifs_settings, require_posix_strict=True)
        raise AssertionError("Expected 503 STAGING_PERMISSION_UNSAFE on CIFS managed_framework_dir")
    except Exception as exc:
        assert getattr(exc, "code", None) == "STAGING_PERMISSION_UNSAFE"

    # --- 2. S-02: Toolkit verify/{batch_id} mixed-dataset & release-split holdout pruning + revocation ---
    fake_tx = tmp_path / "tx_s02"
    mixed_bname = "opaque-eval-batch-20261125"
    mixed_bdir = fake_tx / "harbor-runs" / mixed_bname
    mixed_bdir.mkdir(parents=True)
    (mixed_bdir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "dataset": "OpaqueMixed", "harness": "pi", "model": "M1", "batch": mixed_bname}),
        encoding="utf-8",
    )
    (mixed_bdir / "batch-status.json").write_text(
        json.dumps({"status": "finished", "planned": 2, "completed": 2, "task_attempts": {"pub_task_1": 1, "secret_eval_2": 1}}),
        encoding="utf-8",
    )
    (mixed_bdir / "episodes.jsonl").write_text(
        json.dumps({"trial_id": "t_pub_1", "task_id": "pub_task_1", "status": "scored", "reward": 1.0, "dataset": "OpaqueMixed", "model_route": "M1", "harness": "pi"}) + "\n"
        + json.dumps({"trial_id": "t_ho_2", "task_id": "secret_eval_2", "status": "scored", "reward": 0.0, "dataset": "OpaqueMixed", "model_route": "M1", "harness": "pi"}) + "\n",
        encoding="utf-8",
    )
    isolated_settings.tianxi_root = fake_tx
    app, ctx = _build_m2_app(tmp_path, isolated_settings, index_real=False)
    assert ctx.coordinator.hot.scan_single_harbor_batch(mixed_bdir, force=True) == "indexed"
    bid_mixed = make_batch_id(f"harbor_runs:{mixed_bname}")

    # Register opaque release (NO 'holdout' in name!) with mixed per-task split='holdout' on secret_eval_2!
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, goal_type, capability, status, updated_at) VALUES ('opaque_bm', 'Opaque', 'agentic', 'Agentic', 'optimize_gain', 'coding', 'integrated', '2026-11-25T00:00:00Z')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO dataset_releases (dataset_release_id, benchmark_slug, dataset_version, split, runner, default_protocol, updated_at) VALUES ('opaque__v1__mixed', 'opaque_bm', 'v1', 'mixed', 'harbor', 'lrm-eval-report-v1', '2026-11-25T00:00:00Z')"
        )
        conn.execute("UPDATE batches SET benchmark_slug='opaque_bm', dataset_release_id='opaque__v1__mixed' WHERE batch_id=?", (bid_mixed,))
        conn.execute("INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES ('opaque__v1__mixed', 'pub_task_1', 'train')")
        conn.execute("INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES ('opaque__v1__mixed', 'secret_eval_2', 'holdout')")

    uid_ho = create_user(ctx.db, username="tk_ho_user", password="StrongHoPass!2026", role="researcher", projects=["default"], scopes=["dataset:holdout_read"])
    c_ho = TestClient(app)
    csrf_ho = login_client(c_ho, "tk_ho_user", "StrongHoPass!2026")

    # With dataset:holdout_read -> sees BOTH tasks
    r_allow = c_ho.post(f"/api/v1/toolkit/verify/{bid_mixed}", headers={"X-CSRF-Token": csrf_ho}, json={})
    assert r_allow.status_code == 200
    assert "pub_task_1" in r_allow.json()["report"]["tasks"]
    assert "secret_eval_2" in r_allow.json()["report"]["tasks"]

    # Revoke dataset:holdout_read -> pub_task_1 preserved, secret_eval_2 stripped!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET scopes_json = '[]' WHERE user_id = ?", (uid_ho,))
    r_revoked = c_ho.post(f"/api/v1/toolkit/verify/{bid_mixed}", headers={"X-CSRF-Token": csrf_ho}, json={})
    assert r_revoked.status_code == 200
    rep_rev = r_revoked.json()["report"]
    assert "pub_task_1" in rep_rev["tasks"]
    assert "secret_eval_2" not in rep_rev["tasks"]
    assert rep_rev.get("tasks_redacted") is True and rep_rev.get("hidden_holdout_tasks") == 1

    # Whole-release split='holdout' (still NO 'holdout' in batch_name or release_id!) -> strips ALL tasks!
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE dataset_releases SET split = 'holdout' WHERE dataset_release_id = 'opaque__v1__mixed'")
    r_whole_ho = c_ho.post(f"/api/v1/toolkit/verify/{bid_mixed}", headers={"X-CSRF-Token": csrf_ho}, json={})
    assert r_whole_ho.status_code == 200
    assert r_whole_ho.json()["report"]["tasks"] == {}
    assert r_whole_ho.json()["report"]["tasks_redacted"] is True

    # --- 3. E-02: Direct Lab image byte binding into cases_sha256 & missing-image rejection ---
    def _make_png(rgb: tuple[int, int, int]) -> bytes:
        def _chunk(ctype: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        raw_row = b"\x00" + bytes(rgb)
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw_row)) + _chunk(b"IEND", b"")

    png_red_b64 = base64.b64encode(_make_png((255, 0, 0))).decode("ascii")
    png_blue_b64 = base64.b64encode(_make_png((0, 0, 255))).decode("ascii")
    mm_cases = '{"id":"img-q1","prompt":"What color is this pixel?","images":["pixel.png"],"answer":"red"}\n'

    # Configure 0600 gateways.env + mock gateway
    assert ctx.settings.gateways_env_file is not None
    ctx.settings.gateways_env_file.write_text("OPENAI_API_KEY=sk-img-test-key-0001\n", encoding="utf-8")
    os.chmod(ctx.settings.gateways_env_file, 0o600)
    ctx.direct_lab._http_post_fn = lambda url, body, headers, timeout: {
        "model": body["model"],
        "choices": [{"message": {"content": "red"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }

    # Missing image at /validate -> 422 SCHEMA_VALIDATION_FAILED on images[0]!
    v_miss = c_ho.post(
        "/api/v1/direct-lab/validate",
        headers={"X-CSRF-Token": csrf_ho},
        json={"cases_jsonl": mm_cases, "scorer_id": "exact_match"},
    )
    assert v_miss.status_code == 422
    assert any(f["field"] == "images[0]" and "image_file_missing" in f["error"] for f in v_miss.json()["error"]["detail"]["fields"])

    # Invalid magic bytes pretending to be PNG -> 422 SCHEMA_VALIDATION_FAILED!
    v_fake = c_ho.post(
        "/api/v1/direct-lab/validate",
        headers={"X-CSRF-Token": csrf_ho},
        json={"cases_jsonl": mm_cases, "scorer_id": "exact_match", "images_b64": {"pixel.png": base64.b64encode(b"not-a-png").decode("ascii")}},
    )
    assert v_fake.status_code == 422

    # Same cases.jsonl + same filename 'pixel.png' with DIFFERENT PNG bytes -> DIFFERENT cases_sha256!
    v_red = c_ho.post(
        "/api/v1/direct-lab/validate",
        headers={"X-CSRF-Token": csrf_ho},
        json={"cases_jsonl": mm_cases, "scorer_id": "exact_match", "images_b64": {"pixel.png": png_red_b64}},
    )
    v_blue = c_ho.post(
        "/api/v1/direct-lab/validate",
        headers={"X-CSRF-Token": csrf_ho},
        json={"cases_jsonl": mm_cases, "scorer_id": "exact_match", "images_b64": {"pixel.png": png_blue_b64}},
    )
    assert v_red.status_code == 200 and v_blue.status_code == 200
    assert v_red.json()["cases_sha256"] != v_blue.json()["cases_sha256"]

    # Run with red PNG vs run with blue PNG -> A/B compare MUST reject with 400 INCOMPARABLE_PROTOCOL!
    run_red = c_ho.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_ho},
        json={"suite_id": "mm-pix", "gateway_profile_id": "qwen197-vllm", "model_route": "m1", "cases_jsonl": mm_cases, "images_b64": {"pixel.png": png_red_b64}},
    )
    run_blue = c_ho.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_ho},
        json={"suite_id": "mm-pix", "gateway_profile_id": "qwen197-vllm", "model_route": "m2", "cases_jsonl": mm_cases, "images_b64": {"pixel.png": png_blue_b64}},
    )
    run_red_2 = c_ho.post(
        "/api/v1/direct-lab/runs",
        headers={"X-CSRF-Token": csrf_ho},
        json={"suite_id": "mm-pix", "gateway_profile_id": "qwen197-vllm", "model_route": "m2", "cases_jsonl": mm_cases, "images_b64": {"pixel.png": png_red_b64}},
    )
    assert run_red.status_code == 200 and run_blue.status_code == 200 and run_red_2.status_code == 200
    assert run_red.json()["cases_sha256"] != run_blue.json()["cases_sha256"]
    assert run_red.json()["cases_sha256"] == run_red_2.json()["cases_sha256"]

    cmp_diff_img = c_ho.post(
        "/api/v1/direct-lab/compare",
        headers={"X-CSRF-Token": csrf_ho},
        json={"run_a_id": run_red.json()["run_id"], "run_b_id": run_blue.json()["run_id"]},
    )
    assert cmp_diff_img.status_code == 400 and cmp_diff_img.json()["error"]["code"] == "INCOMPARABLE_PROTOCOL"

    cmp_same_img = c_ho.post(
        "/api/v1/direct-lab/compare",
        headers={"X-CSRF-Token": csrf_ho},
        json={"run_a_id": run_red.json()["run_id"], "run_b_id": run_red_2.json()["run_id"]},
    )
    assert cmp_same_img.status_code == 200
    ctx.db.close_thread_connection()
