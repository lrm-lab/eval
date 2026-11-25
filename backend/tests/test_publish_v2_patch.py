from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.indexing.identity import make_batch_id
from app.main import create_app
from app.security.authz import ALL_SCOPES
from tests.conftest import create_user, login_client

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"


def test_run_py_publish_v2_patch_applies_and_ingests_with_generation_guard(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """M2 rev2 Upstream `run.py:publish()` Common Finalizer v2 acceptance (v2.2.1 05-1 §2.2, I-01, S-06):
    1) Unified diff `backend/patches/run_py_publish_v2.patch` applies cleanly with `--fuzz=0`
       to an isolated temp copy of real 197 `framework/run.py` (NEVER touches upstream!).
    2) Actually executes patched `run.publish()` + `run.write_batch_report()` on a realistic trial
       fixture with >150 KiB verifier log (including multi-byte UTF-8, /home/*, /tmp/*, Bearer, sk-*),
       ATIF trajectory, and gateway capture:
       - Public trial DTO lands under immutable `generations/<generation_id>/trials/<tid>/result.json`.
       - Restricted evidence (`verifier-output.txt`, `trajectory.json`, `model-gateway.json`) lands
         STRICTLY in `0700`/`0600` `evidence_store_dir` — ZERO `0644` restricted files under shared output!
       - Redacted `verifier-output.txt` is STRICTLY `<= 131072` bytes (exact 128 KiB ceiling, zero overrun!).
       - `publish-manifest.json` (`schema_version: 2`, `generation_root`, `generation_id`) is committed
         atomically inside `.publish.lock` after `batch-report.json` and `trial-digests.jsonl`.
    3) `HotIndexer` ingests cleanly from public output + `0700` `evidence_store_dir`, and `QueryService` +
       `DiagnosticsService` serve verified evidence cleanly.
    4) Overwriting `verifier-output.txt` OR `trajectory.json` on disk after indexing (even AFTER warming
       `DiagnosticsService`'s trajectory cache!) IMMEDIATELY returns `409 GENERATION_MISMATCH` in BOTH
       `QueryService` AND `DiagnosticsService` (`/api/v1/diagnostics/tools` & `/api/v1/diagnostics/clues`)!"""
    orig_fw = Path("/home/share-data/tianxi_benches/framework")
    orig_run_py = orig_fw / "run.py"
    patch_file = PATCHES_DIR / "run_py_publish_v2.patch"
    assert orig_run_py.is_file() and patch_file.is_file()

    # 1. Copy upstream framework scripts into isolated temp dir & apply patch with --fuzz=0
    work_fw = tmp_path / "fw_patched"
    fw_sub = work_fw / "framework"
    fw_sub.mkdir(parents=True)
    for fn in ("run.py", "batch_guard.py", "direct.py", "gateway_capture.py", "report.py", "vlmevalkit.py", "training_capture.py"):
        shutil.copy2(orig_fw / fn, fw_sub / fn)
    shutil.copy2(PATCHES_DIR / "publish_v2.py", fw_sub / "publish_v2.py")

    proc = subprocess.run(
        ["patch", "-p1", "--fuzz=0", "-i", str(patch_file)],
        cwd=str(work_fw),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"patch --fuzz=0 failed: {proc.stdout}\n{proc.stderr}"

    # 2. Dynamically load patched run.py and execute publish() + write_batch_report() end-to-end!
    tid = "22222222-2222-2222-2222-222222222222"
    raw_job = tmp_path / "raw_work" / "job_demo"
    t_src = raw_job / tid
    (t_src / "verifier").mkdir(parents=True)
    (t_src / "agent").mkdir(parents=True)

    # >150 KiB verifier log with multi-byte UTF-8, /home/*, /tmp/*, Bearer, sk-*
    noisy_log = (
        "START /home/secretuser/work/test.py Bearer tokenSecret987654321 sk-liveSecretKey999999 中文日志测试\n"
        + ("中A" * 60000)
        + "\nEND /tmp/runner_xyz/debug.log\n"
    ).encode("utf-8")
    assert len(noisy_log) > 131072
    (t_src / "verifier" / "test-stdout.txt").write_bytes(noisy_log)
    (t_src / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": "ls"}}],
                        "observation": {"results": [{"source_call_id": "c1", "content": "ok", "is_error": False}]},
                    },
                    {
                        "step_id": 2,
                        "source": "agent",
                        "tool_calls": [{"tool_call_id": "c2", "function_name": "bash", "arguments": {"cmd": "pytest"}}],
                        "observation": {"results": [{"source_call_id": "c2", "content": "passed", "is_error": False}]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (t_src / "result.json").write_text(
        json.dumps(
            {
                "id": tid,
                "task_name": "v2-task-01",
                "task_checksum": "chk_v2_01",
                "agent_info": {"name": "opencode", "version": "1.19.3"},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-11-25T00:00:00Z",
                "finished_at": "2026-11-25T00:00:51Z",
                "environment_setup": {"started_at": "2026-11-25T00:00:00Z", "finished_at": "2026-11-25T00:00:02.500Z"},
                "agent_setup": {"started_at": "2026-11-25T00:00:02.500Z", "finished_at": "2026-11-25T00:00:06.000Z"},
                "agent_execution": {"started_at": "2026-11-25T00:00:06.000Z", "finished_at": "2026-11-25T00:00:46.000Z"},
                "verifier": {"started_at": "2026-11-25T00:00:46.000Z", "finished_at": "2026-11-25T00:00:51.000Z"},
            }
        ),
        encoding="utf-8",
    )

    fake_tianxi = tmp_path / "tianxi_v2"
    batch_name = "v2-finalizer-demo-20261125"
    bdir = fake_tianxi / "harbor-runs" / batch_name
    ev_store = isolated_settings.evidence_store_dir
    assert ev_store is not None
    ev_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(ev_store, 0o700)

    # Import patched run.py cleanly inside isolated sys.path
    old_sys_path = list(sys.path)
    sys.path.insert(0, str(fw_sub))
    try:
        for mod_name in ("publish_v2", "batch_guard", "direct", "gateway_capture", "report", "vlmevalkit", "training_capture", "patched_run_v2"):
            sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location("patched_run_v2", fw_sub / "run.py")
        assert spec and spec.loader
        run_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_mod)

        metadata = {
            "schema_version": 1,
            "dataset": "Terminal-Bench 2.1",
            "harness": "opencode",
            "model": "Qwen3_8-27B",
            "batch": batch_name,
        }
        with run_mod.publish_v2.publish_lock(bdir):
            count = run_mod.publish(raw_job, bdir, metadata, evidence_store_dir=ev_store)
            assert count == 1
            (bdir / "batch-status.json").write_text(
                json.dumps({"status": "finished", "planned": 1, "completed": 1, "task_attempts": {"v2-task-01": 1}}),
                encoding="utf-8",
            )
            run_mod.write_batch_report(bdir, evidence_store_dir=ev_store)
    finally:
        sys.path[:] = old_sys_path

    # 3. Verify immutable generations/<gen_id>/ layout, strict 0700/0600 evidence isolation, and <= 131072 bytes!
    pm_path = bdir / "publish-manifest.json"
    assert pm_path.is_file()
    pm = json.loads(pm_path.read_text(encoding="utf-8"))
    assert pm["schema_version"] == 2
    gen_id = pm["generation_id"]
    assert gen_id.startswith("gen_")
    assert pm["generation_root"] == f"generations/{gen_id}"
    assert "trial-digests.jsonl" in pm["top_level_files"]
    assert not (bdir / ".pending-generation.json").exists()

    # Public trial DTO exists ONLY under generations/<gen_id>/trials/<tid>/result.json
    pub_res = bdir / "generations" / gen_id / "trials" / tid / "result.json"
    assert pub_res.is_file()
    # ZERO restricted files in shared public output!
    assert not list(bdir.rglob("verifier-output.txt"))
    assert not list(bdir.rglob("trajectory.json"))
    assert not list(bdir.rglob("model-gateway.json"))

    # Restricted files live STRICTLY inside 0700 evidence_store with 0600 file permissions!
    ev_trial_dir = ev_store / batch_name / "generations" / gen_id / "trials" / tid
    vo_file = ev_trial_dir / "verifier-output.txt"
    traj_file = ev_trial_dir / "trajectory.json"
    assert vo_file.is_file() and traj_file.is_file()
    assert (stat.S_IMODE(ev_trial_dir.stat().st_mode) & 0o077) == 0
    assert stat.S_IMODE(vo_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(traj_file.stat().st_mode) == 0o600

    # Strict <= 131072 bytes (128 KiB) verifier log cap + full path/credential redaction!
    vo_bytes = vo_file.read_bytes()
    assert len(vo_bytes) <= 131072, f"Verifier log exceeded 128 KiB ceiling: {len(vo_bytes)} > 131072"
    vo_text = vo_bytes.decode("utf-8")
    assert "[truncated to 128 KiB" in vo_text
    assert "/home/secretuser" not in vo_text and "/tmp/runner_xyz" not in vo_text
    assert "tokenSecret987654321" not in vo_text and "sk-liveSecretKey999999" not in vo_text

    # 4. Ingest with HotIndexer and assert QueryService + DiagnosticsService same-generation enforcement!
    cfg_dir = tmp_path / "cfg_v2"
    cfg_dir.mkdir(parents=True)
    for fn in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml", "gateway_profiles.yaml", "direct_scorers.yaml"):
        src = Path("/home/share-data/eval/config") / fn
        if src.is_file():
            (cfg_dir / fn).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dir / "source_approval_registry.yaml").write_text(
        f"""version: "v1"
default_project_id: "default"
default_owner_id: "system"
sources:
  - source_root: "{bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
""",
        encoding="utf-8",
    )
    isolated_settings.tianxi_root = fake_tianxi
    isolated_settings.config_dir = cfg_dir
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    st = ctx.coordinator.hot.scan_single_harbor_batch(bdir, force=True)
    assert st == "indexed"

    bid = make_batch_id(f"harbor_runs:{batch_name}")
    create_user(ctx.db, username="v2_adm", password="StrongV2Password!1", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    c = TestClient(app)
    login_client(c, "v2_adm", "StrongV2Password!1")

    td = c.get(f"/api/v1/batches/{bid}/trials/{tid}")
    assert td.status_code == 200
    td_b = td.json()
    assert td_b["generation_id"] == gen_id
    assert td_b["phase_timings"]["agent_execution_s"] == 40.0
    assert td_b["tool_call_count"] == 2
    assert td_b["has_verifier_output"] is True

    vo = c.get(f"/api/v1/batches/{bid}/trials/{tid}/verifier-output")
    assert vo.status_code == 200 and "[truncated to 128 KiB" in vo.json()["content"]

    tr_r = c.get(f"/api/v1/batches/{bid}/trials/{tid}/trajectory")
    assert tr_r.status_code == 200 and tr_r.json()["total_steps"] == 2

    # Warm DiagnosticsService trajectory cache (tools & clues) -> succeeds cleanly with verified 0700 trajectory!
    diag_tools = c.get(f"/api/v1/diagnostics/tools?batch_id={bid}")
    assert diag_tools.status_code == 200
    assert diag_tools.json()["coverage_guard"]["tool_recording_trials"] == 1
    assert diag_tools.json()["tool_ranking"][0]["function_name"] == "bash"

    # 4b. E-02: Toolkit verify --check-smoke MUST pass on v2 private-evidence Harbor batch for authorized user,
    #     and MUST return 403 SCOPE_FORBIDDEN for a researcher lacking trace:read!
    csrf_adm = login_client(c, "v2_adm", "StrongV2Password!1")
    ver_smoke = c.post(
        f"/api/v1/toolkit/verify/{bid}",
        headers={"X-CSRF-Token": csrf_adm},
        json={"check_smoke": True},
    )
    assert ver_smoke.status_code == 200, ver_smoke.text
    assert ver_smoke.json()["passed_checks"] is True and ver_smoke.json()["exit_code"] == 0
    assert ver_smoke.json()["report"]["smoke_check"]["passed"] is True

    create_user(ctx.db, username="v2_notrace", password="StrongNoTrace!26", role="researcher", projects=["default"], scopes=[])
    c_nt = TestClient(app)
    csrf_nt = login_client(c_nt, "v2_notrace", "StrongNoTrace!26")
    ver_deny = c_nt.post(
        f"/api/v1/toolkit/verify/{bid}",
        headers={"X-CSRF-Token": csrf_nt},
        json={"check_smoke": True},
    )
    assert ver_deny.status_code == 403 and ver_deny.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # 4c. S-06 / 05-1 §2.2: Direct & VLMEvalKit through patched run.py isolate QA/trajectory/media strictly in 0700/0600
    #     AND are cleanly accepted by HotIndexer + served to authorized users!
    import base64 as _b64
    import struct as _struct
    import zlib as _zlib

    def _tiny_png() -> bytes:
        def _ch(ct: bytes, d: bytes) -> bytes:
            return _struct.pack(">I", len(d)) + ct + d + _struct.pack(">I", _zlib.crc32(ct + d) & 0xFFFFFFFF)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _ch(b"IHDR", _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _ch(b"IDAT", _zlib.compress(b"\x00\xff\x00\x00"))
            + _ch(b"IEND", b"")
        )

    png_bytes = _tiny_png()
    png_b64 = _b64.b64encode(png_bytes).decode("ascii")

    # 4c-i) Direct receipt via patched run.py
    d_tid = "33333333-3333-3333-3333-333333333333"
    d_job = tmp_path / "raw_work" / "job_direct_v2"
    (d_job / d_tid).mkdir(parents=True)
    (d_job / d_tid / "result.json").write_text(
        json.dumps(
            {
                "id": d_tid,
                "task_name": "direct-q1",
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-11-25T00:01:00Z",
                "finished_at": "2026-11-25T00:01:02Z",
                "direct_result": {
                    "prompt": "SECRET_DIRECT_PROMPT_999",
                    "prediction": "SECRET_DIRECT_PREDICTION_999",
                    "answer": "SECRET_DIRECT_GOLD_999",
                    "reported_model": "Qwen3_8-27B",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                },
            }
        ),
        encoding="utf-8",
    )
    d_bname = "direct-v2-isolated-20261125"
    d_bdir = fake_tianxi / "harbor-runs" / d_bname

    # 4c-ii) VLMEvalKit receipt with inline base64 PNG & prediction via patched run.py
    v_tid = "44444444-4444-4444-4444-444444444444"
    v_job = tmp_path / "raw_work" / "job_vlm_v2"
    (v_job / v_tid).mkdir(parents=True)
    (v_job / v_tid / "result.json").write_text(
        json.dumps(
            {
                "id": v_tid,
                "task_name": "vlm-q1",
                "agent_info": {"name": "vlmevalkit", "version": "0.1.0"},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-11-25T00:02:00Z",
                "finished_at": "2026-11-25T00:02:03Z",
                "vlmeval_result": {
                    "dataset": "CMMMU",
                    "http_status": 200,
                    "request_messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "SECRET_VLM_QUESTION_888"},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
                            ],
                        }
                    ],
                    "prediction": "SECRET_VLM_ANSWER_888",
                },
            }
        ),
        encoding="utf-8",
    )
    v_bname = "vlm-v2-isolated-20261125"
    v_bdir = fake_tianxi / "harbor-runs" / v_bname

    sys.path.insert(0, str(fw_sub))
    try:
        with run_mod.publish_v2.publish_lock(d_bdir):
            assert run_mod.publish(d_job, d_bdir, {"schema_version": 1, "dataset": "DirectSuite", "harness": "direct", "runner": "direct", "model": "Qwen3_8-27B", "batch": d_bname}, evidence_store_dir=ev_store) == 1
            run_mod.write_batch_report(d_bdir, planned=1, evidence_store_dir=ev_store)
        with run_mod.publish_v2.publish_lock(v_bdir):
            assert run_mod.publish(v_job, v_bdir, {"schema_version": 1, "dataset": "CMMMU", "harness": "vlmevalkit", "runner": "vlmevalkit", "model": "Qwen3_8-27B", "batch": v_bname}, evidence_store_dir=ev_store) == 1
            run_mod.write_batch_report(v_bdir, planned=1, evidence_store_dir=ev_store)
    finally:
        sys.path[:] = old_sys_path

    # Assert ZERO restricted QA/prediction/trajectory/media leaked into shared output dirs!
    for fpath in d_bdir.rglob("*"):
        if fpath.is_file():
            txt = fpath.read_text(encoding="utf-8", errors="ignore")
            assert "SECRET_DIRECT_PROMPT_999" not in txt
            assert "SECRET_DIRECT_PREDICTION_999" not in txt
            assert "SECRET_DIRECT_GOLD_999" not in txt
    assert not list(v_bdir.rglob("trajectory.json"))
    assert not list(v_bdir.rglob("*.png"))
    for fpath in v_bdir.rglob("*"):
        if fpath.is_file():
            txt = fpath.read_text(encoding="utf-8", errors="ignore")
            assert "SECRET_VLM_QUESTION_888" not in txt
            assert "SECRET_VLM_ANSWER_888" not in txt

    # Approve both new dirs and assert HotIndexer indexes BOTH cleanly!
    (cfg_dir / "source_approval_registry.yaml").write_text(
        f"""version: "v1"
default_project_id: "default"
default_owner_id: "system"
sources:
  - source_root: "{bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
  - source_root: "{d_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "direct_vlm_qa"]
  - source_root: "{v_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "direct_vlm_qa"]
""",
        encoding="utf-8",
    )
    ctx.coordinator.hot.approval_registry.reload()
    ctx.query.approval_registry.reload()
    assert ctx.coordinator.hot.scan_single_harbor_batch(d_bdir, force=True) == "indexed"
    assert ctx.coordinator.hot.scan_single_harbor_batch(v_bdir, force=True) == "indexed"

    # Authorized admin reads Direct QA & VLMEvalKit trajectory + PNG media from 0700 evidence_store!
    d_bid = make_batch_id(f"harbor_runs:{d_bname}")
    v_bid = make_batch_id(f"harbor_runs:{v_bname}")
    d_det = c.get(f"/api/v1/batches/{d_bid}/trials/{d_tid}")
    assert d_det.status_code == 200
    assert d_det.json()["qa"]["direct_result"]["prediction"] == "SECRET_DIRECT_PREDICTION_999"
    # Unauthorized user sees qa_redacted=True and zero secret QA!
    d_det_unauth = c_nt.get(f"/api/v1/batches/{d_bid}/trials/{d_tid}")
    assert d_det_unauth.status_code == 200 and d_det_unauth.json()["qa_redacted"] is True
    assert "SECRET_DIRECT_PREDICTION_999" not in d_det_unauth.text

    v_med = c.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/media/0")
    assert v_med.status_code == 200 and v_med.content == png_bytes
    assert c_nt.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/media/0").status_code == 403
    v_traj = c.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/trajectory")
    assert v_traj.status_code == 200 and "SECRET_VLM_ANSWER_888" in v_traj.text

    # 5. Tamper verifier-output.txt in 0700 store -> QueryService returns 409 GENERATION_MISMATCH!
    vo_file.write_text("tampered cross-generation log\n", encoding="utf-8")
    vo_tampered = c.get(f"/api/v1/batches/{bid}/trials/{tid}/verifier-output")
    assert vo_tampered.status_code == 409
    assert vo_tampered.json()["error"]["code"] == "GENERATION_MISMATCH"

    # 6. Tamper trajectory.json AFTER warming DiagnosticsService cache -> BOTH Diagnostics & Toolkit verify return 409!
    traj_file.write_text(json.dumps({"steps": [{"step_id": 1, "source": "agent", "tool_calls": []}]}), encoding="utf-8")
    diag_tools_tampered = c.get(f"/api/v1/diagnostics/tools?batch_id={bid}")
    assert diag_tools_tampered.status_code == 409
    assert diag_tools_tampered.json()["error"]["code"] == "GENERATION_MISMATCH"

    diag_clues_tampered = c.get(f"/api/v1/diagnostics/clues?batch_id={bid}")
    assert diag_clues_tampered.status_code == 409
    assert diag_clues_tampered.json()["error"]["code"] == "GENERATION_MISMATCH"

    ver_smoke_tampered = c.post(
        f"/api/v1/toolkit/verify/{bid}",
        headers={"X-CSRF-Token": csrf_adm},
        json={"check_smoke": True},
    )
    assert ver_smoke_tampered.status_code == 409
    assert ver_smoke_tampered.json()["error"]["code"] == "GENERATION_MISMATCH"

    ctx.db.close_thread_connection()


def test_m2_rev4_vlm_qa_scope_toolkit_mixed_holdout_gen_mismatch_and_direct_attempts2(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """M2 rev4 regression closing all 3 @theshy rev3 review findings:
    1) S-02 / S-06: VLMEvalKit `/trajectory` requires BOTH `trace:read` + `direct_vlm_qa:read` AND
       source approval for BOTH `atif_trajectory` + `direct_vlm_qa`; Toolkit `--check-smoke` on a
       mixed dataset filters holdout tasks BEFORE reading private evidence (never touches holdout files!).
    2) I-01 / E-02: Publishing gen2 (reward=0.0) over indexed gen1 (reward=1.0) without re-indexing causes
       Toolkit `verify/{batch_id}` (both plain and `--check-smoke`) to return 409 GENERATION_MISMATCH.
    3) 05-1 §2.2: Patched upstream `run.py` Direct CLI with 1 case & `--attempts 2` produces
       `planned=2, completed=2, task_attempts={case: 2}`, redacted public `batch-status.json`
       (no raw work path), private `0600` `.private-source.json`, valid `batch-report.json`,
       clean HotIndexer ingest, and refuses to seal `publish-manifest.json` if report fails!"""
    import http.server
    import threading

    orig_fw = Path("/home/share-data/tianxi_benches/framework")
    patch_file = PATCHES_DIR / "run_py_publish_v2.patch"
    work_fw = tmp_path / "fw_rev4"
    fw_sub = work_fw / "framework"
    fw_sub.mkdir(parents=True)
    for fn in ("run.py", "batch_guard.py", "direct.py", "gateway_capture.py", "report.py", "vlmevalkit.py", "training_capture.py"):
        shutil.copy2(orig_fw / fn, fw_sub / fn)
    shutil.copy2(PATCHES_DIR / "publish_v2.py", fw_sub / "publish_v2.py")

    p_apply = subprocess.run(
        ["patch", "-p1", "--fuzz=0", "-i", str(patch_file)],
        cwd=str(work_fw),
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_apply.returncode == 0, f"patch --fuzz=0 failed: {p_apply.stdout}\n{p_apply.stderr}"

    ev_store = isolated_settings.evidence_store_dir
    assert ev_store is not None
    ev_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(ev_store, 0o700)

    fake_tx = tmp_path / "tx_rev4"
    hr_dir = fake_tx / "harbor-runs"
    hr_dir.mkdir(parents=True)

    # --- 3. Real patched run.py Direct CLI with 1 case & --attempts 2 against mock HTTP gateway ---
    ds_dir = tmp_path / "direct_suite"
    ds_dir.mkdir(parents=True)
    cases_file = ds_dir / "cases.jsonl"
    cases_file.write_text(
        json.dumps({"id": "dir-case-1", "prompt": "What is 2+2?", "answer": "4"}) + "\n",
        encoding="utf-8",
    )
    toml_cfg = ds_dir / "harbor.toml"
    toml_cfg.write_text(
        '[datasets.direct-rev4]\nname = "DirectRev4"\nrunner = "direct"\npath = "cases.jsonl"\npath_root = "config"\n',
        encoding="utf-8",
    )

    class _MockGatewayHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            body = json.dumps(
                {
                    "model": "Qwen3_8-27B",
                    "choices": [{"message": {"content": "4"}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 2},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockGatewayHandler)
    port = srv.server_address[1]
    t_srv = threading.Thread(target=srv.serve_forever, daemon=True)
    t_srv.start()

    direct_work = tmp_path / "private_direct_work"
    direct_bname = "direct-attempts2-rev4"
    try:
        cli_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OPENAI_API_KEY": "sk-mock-direct-test",
            "LRM_EVIDENCE_STORE_DIR": str(ev_store),
        }
        cli_proc = subprocess.run(
            [
                sys.executable,
                str(fw_sub / "run.py"),
                "direct-rev4",
                "--config",
                str(toml_cfg),
                "--task",
                "dir-case-1",
                "--attempts",
                "2",
                "--model",
                "Qwen3_8-27B",
                "--base-url",
                f"http://127.0.0.1:{port}",
                "--batch",
                direct_bname,
                "--work",
                str(direct_work),
                "--output",
                str(hr_dir),
            ],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert cli_proc.returncode == 0, f"Direct --attempts 2 CLI failed: {cli_proc.stdout}\n{cli_proc.stderr}"
    finally:
        srv.shutdown()
        srv.server_close()

    d_out = hr_dir / direct_bname
    assert (d_out / "batch-status.json").is_file()
    assert (d_out / "batch-report.json").is_file()
    assert (d_out / "publish-manifest.json").is_file()
    bs = json.loads((d_out / "batch-status.json").read_text(encoding="utf-8"))
    assert bs["planned"] == 2 and bs["completed"] == 2
    assert bs["task_attempts"] == {"dir-case-1": 2}
    # Public batch-status.json MUST redact raw work dir path (06-1 §3.1)!
    assert "<redacted>/jobs/" in bs["source"]["path"]
    assert str(tmp_path) not in json.dumps(bs)
    assert "/tmp/" not in json.dumps(bs) and "/home/" not in json.dumps(bs)
    assert len(bs["source"].get("job_ref", "")) == 16
    # Private locator resides strictly in 0700/0600 evidence store
    priv_src = ev_store / direct_bname / ".private-source.json"
    assert priv_src.is_file() and stat.S_IMODE(priv_src.stat().st_mode) == 0o600
    assert str(direct_work.resolve()) in priv_src.read_text(encoding="utf-8")

    # Load patched run.py module for controlled v2 publishing + fail-closed write_batch_report check
    old_sys_path = list(sys.path)
    sys.path.insert(0, str(fw_sub))
    try:
        for mod_name in ("publish_v2", "batch_guard", "direct", "gateway_capture", "report", "vlmevalkit", "training_capture", "patched_run_rev4"):
            sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location("patched_run_rev4", fw_sub / "run.py")
        assert spec and spec.loader
        run_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_mod)

        # Fail-closed write_batch_report(): planned=1 with 2 recorded rows MUST raise & NOT seal publish-manifest.json!
        bad_dir = hr_dir / "bad-plan-failclosed"
        bad_dir.mkdir(parents=True)
        (bad_dir / "manifest.json").write_text(json.dumps({"schema_version": 1, "dataset": "D", "harness": "direct", "model": "M", "batch": "bad-plan-failclosed"}), encoding="utf-8")
        (bad_dir / "batch-status.json").write_text(json.dumps({"status": "finished", "planned": 1, "completed": 2}), encoding="utf-8")
        (bad_dir / "episodes.jsonl").write_text(
            json.dumps({"trial_id": "t1", "task_id": "c1", "status": "scored", "reward": 1.0, "dataset": "D", "model_route": "M", "harness": "direct"}) + "\n"
            + json.dumps({"trial_id": "t2", "task_id": "c1", "status": "scored", "reward": 1.0, "dataset": "D", "model_route": "M", "harness": "direct"}) + "\n",
            encoding="utf-8",
        )
        try:
            run_mod.write_batch_report(bad_dir, planned=1, evidence_store_dir=ev_store)
            raise AssertionError("Expected write_batch_report to raise on planned < recorded")
        except ValueError:
            pass
        assert not (bad_dir / "publish-manifest.json").exists()

        # --- 1 & 2. Build Harbor gen1->gen2 batch + Mixed holdout Harbor batch + VLMEvalKit batch ---
        def _mk_harbor_job(jdir: Path, trials: list[tuple[str, str, float]]) -> None:
            for t_id, tk_name, rew in trials:
                td = jdir / t_id
                (td / "agent").mkdir(parents=True, exist_ok=True)
                (td / "agent" / "trajectory.json").write_text(
                    json.dumps(
                        {
                            "steps": [
                                {
                                    "step_id": 1,
                                    "source": "agent",
                                    "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": "echo ok"}}],
                                    "observation": {"results": [{"source_call_id": "c1", "content": "ok", "is_error": False}]},
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (td / "result.json").write_text(
                    json.dumps(
                        {
                            "id": t_id,
                            "task_name": tk_name,
                            "agent_info": {"name": "opencode", "version": "1.19.3"},
                            "verifier_result": {"rewards": {"reward": rew}},
                            "started_at": "2026-11-25T00:00:00Z",
                            "finished_at": "2026-11-25T00:00:05Z",
                        }
                    ),
                    encoding="utf-8",
                )

        # Gen1 (reward=1.0) on gen-drift-batch
        g_tid = "55555555-5555-5555-5555-555555555555"
        g_job = tmp_path / "raw_work" / "job_gendrift"
        _mk_harbor_job(g_job, [(g_tid, "drift-task-1", 1.0)])
        g_bname = "harbor-gendrift-rev4"
        g_bdir = hr_dir / g_bname
        g_meta = {"schema_version": 1, "dataset": "Terminal-Bench 2.1", "harness": "opencode", "model": "Qwen3_8-27B", "batch": g_bname}
        with run_mod.publish_v2.publish_lock(g_bdir):
            assert run_mod.publish(g_job, g_bdir, g_meta, evidence_store_dir=ev_store) == 1
            (g_bdir / "batch-status.json").write_text(
                json.dumps({"status": "finished", "planned": 1, "completed": 1, "task_attempts": {"drift-task-1": 1}, "source": run_mod.publish_v2.public_source_locator("127.0.0.1", g_job, g_bname)}),
                encoding="utf-8",
            )
            run_mod.write_batch_report(g_bdir, evidence_store_dir=ev_store)

        # Mixed holdout v2 Harbor batch (1 public + 1 holdout)
        m_pub_tid = "66666666-6666-6666-6666-666666666661"
        m_ho_tid = "66666666-6666-6666-6666-666666666662"
        m_job = tmp_path / "raw_work" / "job_mixed_v2"
        _mk_harbor_job(m_job, [(m_pub_tid, "pub-task-1", 1.0), (m_ho_tid, "secret-ho-2", 1.0)])
        m_bname = "harbor-mixed-ho-rev4"
        m_bdir = hr_dir / m_bname
        m_meta = {"schema_version": 1, "dataset": "OpaqueMixedV2", "harness": "opencode", "model": "Qwen3_8-27B", "batch": m_bname}
        with run_mod.publish_v2.publish_lock(m_bdir):
            assert run_mod.publish(m_job, m_bdir, m_meta, evidence_store_dir=ev_store) == 2
            (m_bdir / "batch-status.json").write_text(
                json.dumps({"status": "finished", "planned": 2, "completed": 2, "task_attempts": {"pub-task-1": 1, "secret-ho-2": 1}, "source": run_mod.publish_v2.public_source_locator("127.0.0.1", m_job, m_bname)}),
                encoding="utf-8",
            )
            run_mod.write_batch_report(m_bdir, evidence_store_dir=ev_store)
        m_gen = json.loads((m_bdir / "publish-manifest.json").read_text(encoding="utf-8"))["generation_id"]

        # VLMEvalKit v2 batch for S-02 QA trajectory scope check
        v_tid = "77777777-7777-7777-7777-777777777777"
        v_job = tmp_path / "raw_work" / "job_vlm_rev4"
        (v_job / v_tid).mkdir(parents=True)
        (v_job / v_tid / "result.json").write_text(
            json.dumps(
                {
                    "id": v_tid,
                    "task_name": "vlm-qa-check",
                    "agent_info": {"name": "vlmevalkit", "version": "0.1.0"},
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "started_at": "2026-11-25T00:00:00Z",
                    "finished_at": "2026-11-25T00:00:02Z",
                    "vlmeval_result": {
                        "dataset": "CMMMU",
                        "http_status": 200,
                        "request_messages": [{"role": "user", "content": [{"type": "text", "text": "VLM_SECRET_Q_REV4"}]}],
                        "prediction": "VLM_SECRET_A_REV4",
                    },
                }
            ),
            encoding="utf-8",
        )
        v_bname = "vlm-scope-rev4"
        v_bdir = hr_dir / v_bname
        v_meta = {"schema_version": 1, "dataset": "CMMMU", "harness": "vlmevalkit", "runner": "vlmevalkit", "model": "Qwen3_8-27B", "batch": v_bname}
        with run_mod.publish_v2.publish_lock(v_bdir):
            assert run_mod.publish(v_job, v_bdir, v_meta, evidence_store_dir=ev_store) == 1
            run_mod.write_batch_report(v_bdir, planned=1, evidence_store_dir=ev_store)

        # Configure app & index all 4 batches
        cfg_dir = tmp_path / "cfg_rev4"
        cfg_dir.mkdir(parents=True)
        for fn in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml", "gateway_profiles.yaml", "direct_scorers.yaml"):
            src_f = Path("/home/share-data/eval/config") / fn
            if src_f.is_file():
                (cfg_dir / fn).write_text(src_f.read_text(encoding="utf-8"), encoding="utf-8")
        (cfg_dir / "source_approval_registry.yaml").write_text(
            f"""version: "v1"
default_project_id: "default"
default_owner_id: "system"
sources:
  - source_root: "{d_out}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "direct_vlm_qa"]
  - source_root: "{g_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory"]
  - source_root: "{m_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory"]
  - source_root: "{v_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory"]
""",
            encoding="utf-8",
        )
        isolated_settings.tianxi_root = fake_tx
        isolated_settings.config_dir = cfg_dir
        app = create_app(isolated_settings, run_startup_scan=False)
        ctx = app.state.ctx

        assert ctx.coordinator.hot.scan_single_harbor_batch(d_out, force=True) == "indexed"
        assert ctx.coordinator.hot.scan_single_harbor_batch(g_bdir, force=True) == "indexed"
        assert ctx.coordinator.hot.scan_single_harbor_batch(m_bdir, force=True) == "indexed"
        assert ctx.coordinator.hot.scan_single_harbor_batch(v_bdir, force=True) == "indexed"

        d_bid = make_batch_id(f"harbor_runs:{direct_bname}")
        g_bid = make_batch_id(f"harbor_runs:{g_bname}")
        m_bid = make_batch_id(f"harbor_runs:{m_bname}")
        v_bid = make_batch_id(f"harbor_runs:{v_bname}")

        # Verify Direct --attempts 2 batch indexed cleanly with planned=2, recorded=2, scored=2
        d_row = ctx.db.fetchone("SELECT planned, recorded, scored, valid_mean FROM batches WHERE batch_id = ?", (d_bid,))
        assert d_row is not None and d_row["planned"] == 2 and d_row["recorded"] == 2 and d_row["scored"] == 2 and d_row["valid_mean"] == 1.0

        # Mark secret-ho-2 as split='holdout' in task_definitions on opaque mixed release
        with ctx.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO benchmarks (benchmark_slug, display_name, track_slug, track_display_name, goal_type, capability, status, updated_at) VALUES ('opaque_rev4', 'OpaqueRev4', 'agentic', 'Agentic', 'optimize_gain', 'coding', 'integrated', '2026-11-25T00:00:00Z')")
            conn.execute("INSERT OR IGNORE INTO dataset_releases (dataset_release_id, benchmark_slug, dataset_version, split, runner, default_protocol, updated_at) VALUES ('opaque_rev4__v1__mixed', 'opaque_rev4', 'v1', 'mixed', 'harbor', 'lrm-eval-report-v1', '2026-11-25T00:00:00Z')")
            conn.execute("UPDATE batches SET benchmark_slug='opaque_rev4', dataset_release_id='opaque_rev4__v1__mixed' WHERE batch_id=?", (m_bid,))
            conn.execute("INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES ('opaque_rev4__v1__mixed', 'pub-task-1', 'train')")
            conn.execute("INSERT INTO task_definitions (dataset_release_id, task_id, split) VALUES ('opaque_rev4__v1__mixed', 'secret-ho-2', 'holdout')")

        create_user(ctx.db, username="rev4_adm", password="StrongRev4Adm!26", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
        create_user(ctx.db, username="rev4_trace_only", password="StrongRev4Tr!26", role="researcher", projects=["default"], scopes=["trace:read"])

        c_adm = TestClient(app)
        csrf_adm = login_client(c_adm, "rev4_adm", "StrongRev4Adm!26")
        c_tr = TestClient(app)
        csrf_tr = login_client(c_tr, "rev4_trace_only", "StrongRev4Tr!26")

        # 1a) VLMEvalKit /trajectory:
        # - Missing source approval for direct_vlm_qa -> 403 even for admin with ALL_SCOPES!
        r_vlm_no_app = c_adm.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/trajectory")
        assert r_vlm_no_app.status_code == 403 and r_vlm_no_app.json()["error"]["code"] == "SCOPE_FORBIDDEN"
        # Approve direct_vlm_qa on v_bdir
        reg_txt = (cfg_dir / "source_approval_registry.yaml").read_text(encoding="utf-8")
        reg_txt = reg_txt.replace(
            f'source_root: "{v_bdir}"\n    match_mode: "dir"\n    project_id: "default"\n    owner_id: "system"\n    approval_status: "approved_restricted"\n    approved_evidence_classes: ["summary_dto", "atif_trajectory"]',
            f'source_root: "{v_bdir}"\n    match_mode: "dir"\n    project_id: "default"\n    owner_id: "system"\n    approval_status: "approved_restricted"\n    approved_evidence_classes: ["summary_dto", "atif_trajectory", "direct_vlm_qa"]',
        )
        (cfg_dir / "source_approval_registry.yaml").write_text(reg_txt, encoding="utf-8")
        ctx.query.approval_registry.reload()
        # - User with trace:read ONLY (no direct_vlm_qa:read) -> STILL 403!
        r_vlm_no_scope = c_tr.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/trajectory")
        assert r_vlm_no_scope.status_code == 403 and r_vlm_no_scope.json()["error"]["code"] == "SCOPE_FORBIDDEN"
        # - Admin with both scopes + both approvals -> 200!
        r_vlm_ok = c_adm.get(f"/api/v1/batches/{v_bid}/trials/{v_tid}/trajectory")
        assert r_vlm_ok.status_code == 200 and "VLM_SECRET_A_REV4" in r_vlm_ok.text

        # 1b) Toolkit mixed-dataset per-task holdout filtering BEFORE reading private evidence!
        # Corrupt secret-ho-2's private trajectory in 0700 store:
        # - rev4_trace_only (NO dataset:holdout_read) MUST NOT read secret-ho-2 at all -> 200 & passed_checks=True on pub-task-1!
        # - rev4_adm (HAS dataset:holdout_read) DOES verify secret-ho-2 -> 409 GENERATION_MISMATCH!
        ho_traj_file = ev_store / m_bname / "generations" / m_gen / "trials" / m_ho_tid / "trajectory.json"
        ho_traj_file.write_text('{"corrupted": true}', encoding="utf-8")
        r_mix_noho = c_tr.post(f"/api/v1/toolkit/verify/{m_bid}", headers={"X-CSRF-Token": csrf_tr}, json={"check_smoke": True})
        assert r_mix_noho.status_code == 200, r_mix_noho.text
        assert r_mix_noho.json()["passed_checks"] is True
        assert "pub-task-1" in r_mix_noho.json()["report"]["tasks"]
        assert "secret-ho-2" not in r_mix_noho.json()["report"]["tasks"]
        assert r_mix_noho.json()["report"]["tasks_redacted"] is True
        assert r_mix_noho.json()["report"]["hidden_holdout_tasks"] == 1

        r_mix_adm = c_adm.post(f"/api/v1/toolkit/verify/{m_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": True})
        assert r_mix_adm.status_code == 409 and r_mix_adm.json()["error"]["code"] == "GENERATION_MISMATCH"

        # 2) Toolkit gen1 -> gen2 drift without re-indexing MUST return 409 GENERATION_MISMATCH (NEVER 200 with gen2 score 0.0!)
        # First confirm gen1 passes with score 1.0
        r_g1 = c_adm.post(f"/api/v1/toolkit/verify/{g_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": True})
        assert r_g1.status_code == 200 and r_g1.json()["passed_checks"] is True and r_g1.json()["report"]["totals"]["valid_mean"] == 1.0

        # Publish gen2 (reward=0.0) into the SAME batch directory WITHOUT re-indexing!
        _mk_harbor_job(g_job, [(g_tid, "drift-task-1", 0.0)])
        with run_mod.publish_v2.publish_lock(g_bdir):
            assert run_mod.publish(g_job, g_bdir, g_meta, evidence_store_dir=ev_store, source_host="127.0.0.1") == 1
            run_mod.write_batch_report(g_bdir, evidence_store_dir=ev_store)

        # BOTH plain verify AND --check-smoke MUST return 409 GENERATION_MISMATCH!
        r_g2_plain = c_adm.post(f"/api/v1/toolkit/verify/{g_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": False})
        assert r_g2_plain.status_code == 409 and r_g2_plain.json()["error"]["code"] == "GENERATION_MISMATCH"
        r_g2_smoke = c_adm.post(f"/api/v1/toolkit/verify/{g_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": True})
        assert r_g2_smoke.status_code == 409 and r_g2_smoke.json()["error"]["code"] == "GENERATION_MISMATCH"

        # After re-indexing, gen2 is cleanly served with score 0.0!
        assert ctx.coordinator.hot.scan_single_harbor_batch(g_bdir, force=True) == "indexed"
        r_g2_after = c_adm.post(f"/api/v1/toolkit/verify/{g_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": False})
        assert r_g2_after.status_code == 200 and r_g2_after.json()["report"]["totals"]["valid_mean"] == 0.0

        ctx.db.close_thread_connection()
    finally:
        sys.path[:] = old_sys_path


def test_m2_rev5_legacy_training_capture_failure_redaction_and_same_gen_content_binding(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """M2 rev5 regression closing all 3 @theshy rev4 review findings:
    1) E-02: Legacy-format training capture batch (trials/<tid>/training_trace.json + capture.json,
       no publish-manifest.json) passes BOTH direct managed report.py --check-training-captures AND
       Toolkit /verify --check-training-captures with strict source-approval, scope, path, and
       same-generation SHA-256 enforcement (tampering capture.json or training_trace.json -> 409).
    2) 06-1 §2.1 / S-06: Patched run.py redacts failure.raw_log.path in shared public episodes.jsonl
       AND generations/<gen>/trials/<tid>/result.json AND consecutive_failure.raw_log.path in public
       batch-status.json, persisting raw absolute locators strictly in 0700/0600 private evidence_store.
    3) I-01: Toolkit _read_verified_top_level_snapshot() binds strictly to SQLite-committed digests
       and mandatory/optional constraints — rejecting same-generation different-content rewrites
       (even with a self-consistent rewritten publish-manifest.json keeping the same generation_id,
       OR same-size same-mtime legacy overwrites) with 409 GENERATION_MISMATCH."""
    import hashlib

    orig_fw = Path("/home/share-data/tianxi_benches/framework")
    patch_file = PATCHES_DIR / "run_py_publish_v2.patch"
    work_fw = tmp_path / "fw_rev5"
    fw_sub = work_fw / "framework"
    fw_sub.mkdir(parents=True)
    for fn in ("run.py", "batch_guard.py", "direct.py", "gateway_capture.py", "report.py", "vlmevalkit.py", "training_capture.py"):
        shutil.copy2(orig_fw / fn, fw_sub / fn)
    shutil.copy2(PATCHES_DIR / "publish_v2.py", fw_sub / "publish_v2.py")

    p_apply = subprocess.run(
        ["patch", "-p1", "--fuzz=0", "-i", str(patch_file)],
        cwd=str(work_fw),
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_apply.returncode == 0, f"patch --fuzz=0 failed: {p_apply.stdout}\n{p_apply.stderr}"

    ev_store = isolated_settings.evidence_store_dir
    assert ev_store is not None
    ev_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(ev_store, 0o700)

    fake_tx = tmp_path / "tx_rev5"
    hr_dir = fake_tx / "harbor-runs"
    hr_dir.mkdir(parents=True)

    # --- 1) Legacy-format training capture batch (trials/<tid>/training_trace.json + capture.json) ---
    leg_tid = "88888888-8888-8888-8888-888888888888"
    leg_bname = "legacy-training-rev5"
    leg_bdir = hr_dir / leg_bname
    leg_tdir = leg_bdir / "trials" / leg_tid
    leg_tdir.mkdir(parents=True)

    trace_doc = {
        "schema_version": 1,
        "turns": [
            {
                "node_id": "turn-0",
                "prompt_token_ids": [1, 2, 3],
                "completion_token_ids": [4, 5],
                "per_token_logps": [-0.1, -0.2],
                "loss_mask": [0, 0, 0, 1, 1],
            }
        ],
    }
    trace_bytes = (json.dumps(trace_doc, ensure_ascii=False) + "\n").encode("utf-8")
    trace_sha = hashlib.sha256(trace_bytes).hexdigest()
    capture_doc = {
        "trial_id": leg_tid,
        "purpose": "train",
        "checkpoint": "ckpt-rev5-step100",
        "trace_sha256": trace_sha,
    }
    (leg_tdir / "training_trace.json").write_bytes(trace_bytes)
    (leg_tdir / "capture.json").write_text(json.dumps(capture_doc, ensure_ascii=False) + "\n", encoding="utf-8")
    (leg_tdir / "result.json").write_text(
        json.dumps(
            {
                "id": leg_tid,
                "task_name": "train-task-1",
                "verifier_rewards": {"reward": 1.0},
                "reward_key": "reward",
                "started_at": "2026-11-25T00:00:00Z",
                "finished_at": "2026-11-25T00:00:05Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (leg_bdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": "Terminal-Bench 2.1",
                "harness": "opencode",
                "model": "Qwen3_8-27B",
                "batch": leg_bname,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (leg_bdir / "batch-status.json").write_text(
        json.dumps({"status": "finished", "planned": 1, "completed": 1, "task_attempts": {"train-task-1": 1}}) + "\n",
        encoding="utf-8",
    )
    leg_ep_row = {
        "schema_version": 1,
        "trial_id": leg_tid,
        "dataset": "Terminal-Bench 2.1",
        "task_id": "train-task-1",
        "harness": "opencode",
        "runner": "harbor",
        "model_route": "Qwen3_8-27B",
        "checkpoint": "ckpt-rev5-step100",
        "status": "scored",
        "reward": 1.0,
        "result": f"trials/{leg_tid}/result.json",
        "training_trace": f"trials/{leg_tid}/training_trace.json",
        "training_status": "validated",
    }
    (leg_bdir / "episodes.jsonl").write_text(json.dumps(leg_ep_row, ensure_ascii=False) + "\n", encoding="utf-8")

    # Confirm managed report.py directly passes --check-training-captures on this exact legacy fixture!
    managed_report = Path(__file__).resolve().parent.parent / "app" / "managed_framework" / "report.py"
    direct_rep = subprocess.run(
        [sys.executable, "-B", str(managed_report), str(leg_bdir), "--format", "json", "--check-training-captures"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct_rep.returncode == 0, f"direct report.py failed: {direct_rep.stdout}\n{direct_rep.stderr}"
    assert json.loads(direct_rep.stdout)["training_capture_check"]["passed"] is True

    # --- 2) Patched run.py VerifierTimeout failure receipt + pause status redaction ---
    fail_tid = "99999999-9999-9999-9999-999999999999"
    fail_bname = "harbor-fail-redaction-rev5"
    fail_bdir = hr_dir / fail_bname
    fail_job = tmp_path / "private_work_rev5" / "jobs" / fail_bname
    fail_tdir = fail_job / fail_tid
    (fail_tdir / "verifier").mkdir(parents=True)
    (fail_tdir / "verifier" / "test-stdout.txt").write_text(
        "Verifier timeout at /tmp/secret_work/test.sh\n", encoding="utf-8"
    )
    (fail_tdir / "result.json").write_text(
        json.dumps(
            {
                "id": fail_tid,
                "task_name": "timeout-task-1",
                "agent_info": {"name": "opencode", "version": "1.19.3"},
                "exception_info": {"exception_type": "VerifierTimeoutError", "exception_message": "timed out"},
                "started_at": "2026-11-25T00:00:00Z",
                "finished_at": "2026-11-25T00:05:00Z",
            }
        ),
        encoding="utf-8",
    )

    # Also build a v2 Harbor batch for Blocker 3 (same-gen different-content binding test)
    sg_tid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sg_bname = "harbor-samegen-rev5"
    sg_bdir = hr_dir / sg_bname
    sg_job = tmp_path / "private_work_rev5" / "jobs" / sg_bname
    (sg_job / sg_tid / "agent").mkdir(parents=True)
    (sg_job / sg_tid / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": "ls"}}],
                        "observation": {"results": [{"source_call_id": "c1", "content": "ok", "is_error": False}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (sg_job / sg_tid / "result.json").write_text(
        json.dumps(
            {
                "id": sg_tid,
                "task_name": "samegen-task-1",
                "agent_info": {"name": "opencode", "version": "1.19.3"},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-11-25T00:00:00Z",
                "finished_at": "2026-11-25T00:00:05Z",
            }
        ),
        encoding="utf-8",
    )

    old_sys_path = list(sys.path)
    sys.path.insert(0, str(fw_sub))
    try:
        for mod_name in ("publish_v2", "batch_guard", "direct", "gateway_capture", "report", "vlmevalkit", "training_capture", "patched_run_rev5"):
            sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location("patched_run_rev5", fw_sub / "run.py")
        assert spec and spec.loader
        run_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_mod)

        # Publish VerifierTimeout receipt via patched run.py
        fail_meta = {
            "schema_version": 1,
            "dataset": "Terminal-Bench 2.1",
            "harness": "opencode",
            "model": "Qwen3_8-27B",
            "batch": fail_bname,
        }
        raw_pause_doc = {
            "kind": "verifier_timeout",
            "summary": "评分器运行超时。",
            "raw_log": {
                "host": "10.46.124.197",
                "path": str((fail_tdir / "verifier" / "test-stdout.txt").resolve()),
            },
            "consecutive": 3,
            "trial_id": fail_tid,
        }
        with run_mod.publish_v2.publish_lock(fail_bdir):
            assert run_mod.publish(fail_job, fail_bdir, fail_meta, source_host="10.46.124.197", evidence_store_dir=ev_store) == 1
            run_mod.publish_v2.record_private_pause_state(ev_store, fail_bname, raw_pause_doc)
            pub_pause = run_mod.publish_v2.sanitize_public_failure(raw_pause_doc, job_dir=fail_job, batch_name=fail_bname)
            (fail_bdir / "batch-status.json").write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "planned": 1,
                        "completed": 1,
                        "task_attempts": {"timeout-task-1": 1},
                        "consecutive_failure": pub_pause,
                        "source": run_mod.publish_v2.public_source_locator("10.46.124.197", fail_job, fail_bname),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            run_mod.write_batch_report(fail_bdir, evidence_store_dir=ev_store)

        # Also exercise patched execute_harbor() pause flow directly with a fake harbor runner writing .tianxi-pause.json!
        eh_bname = "harbor-exec-pause-rev5"
        eh_bdir = hr_dir / eh_bname
        eh_work = tmp_path / "private_eh_work"
        eh_ds = tmp_path / "eh_ds"
        (eh_ds / "eh-task-1").mkdir(parents=True)
        (eh_ds / "eh-task-1" / "task.toml").write_text('[task]\nname = "eh-task-1"\n', encoding="utf-8")
        fake_harbor = tmp_path / "fake_harbor.py"
        fake_harbor.write_text(
            f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
if "--version" in sys.argv:
    print("harbor 0.26.0")
    raise SystemExit(0)
job = Path({str((eh_work / "jobs" / eh_bname).resolve())!r})
tid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
tdir = job / tid
(tdir / "verifier").mkdir(parents=True, exist_ok=True)
logp = tdir / "verifier" / "test-stdout.txt"
logp.write_text("timeout log\\n", encoding="utf-8")
(tdir / "result.json").write_text(json.dumps({{
    "id": tid, "task_name": "eh-task-1",
    "agent_info": {{"name": "opencode", "version": "1.19.3"}},
    "exception_info": {{"exception_type": "VerifierTimeoutError"}},
    "started_at": "2026-11-25T00:00:00Z", "finished_at": "2026-11-25T00:01:00Z"
}}), encoding="utf-8")
(job / ".tianxi-pause.json").write_text(json.dumps({{
    "kind": "verifier_timeout",
    "summary": "评分器运行超时。",
    "raw_log": {{"host": "10.46.124.197", "path": str(logp.resolve())}},
    "consecutive": 3,
    "trial_id": tid
}}), encoding="utf-8")
raise SystemExit(75)
""",
            encoding="utf-8",
        )
        fake_harbor.chmod(0o755)
        from types import SimpleNamespace
        eh_args = SimpleNamespace(
            work=eh_work,
            batch=eh_bname,
            source_host="10.46.124.197",
            resume=False,
            task=["eh-task-1"],
            attempts=1,
            concurrent=1,
            agent="opencode",
            model="Qwen3_8-27B",
            mounts=[],
            reward_key="reward",
            pause_after=3,
            training_dir=None,
        )
        eh_meta = {"schema_version": 1, "dataset": "Terminal-Bench 2.1", "harness": "opencode", "model": "Qwen3_8-27B", "batch": eh_bname}
        eh_env = dict(os.environ)
        eh_env["LRM_EVIDENCE_STORE_DIR"] = str(ev_store)
        old_ev_env = os.environ.get("LRM_EVIDENCE_STORE_DIR")
        os.environ["LRM_EVIDENCE_STORE_DIR"] = str(ev_store)
        try:
            eh_rc = run_mod.execute_harbor(
                eh_args,
                [sys.executable, str(fake_harbor), "run", "--ak", "version=1.19.3"],
                eh_ds,
                eh_bdir,
                eh_meta,
                tmp_path,
                eh_env,
            )
            assert eh_rc == 75
        finally:
            if old_ev_env is None:
                os.environ.pop("LRM_EVIDENCE_STORE_DIR", None)
            else:
                os.environ["LRM_EVIDENCE_STORE_DIR"] = old_ev_env

        # Publish v2 batch for same-gen content binding test
        sg_meta = {"schema_version": 1, "dataset": "Terminal-Bench 2.1", "harness": "opencode", "model": "Qwen3_8-27B", "batch": sg_bname}
        with run_mod.publish_v2.publish_lock(sg_bdir):
            assert run_mod.publish(sg_job, sg_bdir, sg_meta, source_host="10.46.124.197", evidence_store_dir=ev_store) == 1
            (sg_bdir / "batch-status.json").write_text(
                json.dumps(
                    {
                        "status": "finished",
                        "planned": 1,
                        "completed": 1,
                        "task_attempts": {"samegen-task-1": 1},
                        "source": run_mod.publish_v2.public_source_locator("10.46.124.197", sg_job, sg_bname),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            run_mod.write_batch_report(sg_bdir, evidence_store_dir=ev_store)
    finally:
        sys.path[:] = old_sys_path

    # Assert ZERO raw work absolute paths leaked into ANY public file of fail_bdir OR eh_bdir!
    for check_dir, check_bname, check_tid in (
        (fail_bdir, fail_bname, fail_tid),
        (eh_bdir, eh_bname, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    ):
        pm_chk = json.loads((check_dir / "publish-manifest.json").read_text(encoding="utf-8"))
        gen_chk = pm_chk["generation_id"]
        for pub_f in check_dir.rglob("*"):
            if pub_f.is_file():
                body = pub_f.read_text(encoding="utf-8", errors="ignore")
                assert str(tmp_path) not in body, f"Raw tmp_path leaked in {pub_f}: {body}"
                assert "/tmp/" not in body, f"Raw /tmp/ leaked in {pub_f}: {body}"
                assert "/home/" not in body, f"Raw /home/ leaked in {pub_f}: {body}"
        ep_row = json.loads((check_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()[0])
        res_doc = json.loads((check_dir / "generations" / gen_chk / "trials" / check_tid / "result.json").read_text(encoding="utf-8"))
        bs_doc = json.loads((check_dir / "batch-status.json").read_text(encoding="utf-8"))
        assert ep_row["failure"]["raw_log"]["path"].startswith(f"<redacted>/jobs/{check_bname}/")
        assert len(ep_row["failure"]["raw_log"]["log_ref"]) == 16
        assert res_doc["failure"]["raw_log"]["path"].startswith(f"<redacted>/jobs/{check_bname}/")
        assert bs_doc["consecutive_failure"]["raw_log"]["path"].startswith(f"<redacted>/jobs/{check_bname}/")
        assert len(bs_doc["consecutive_failure"]["raw_log"]["log_ref"]) == 16
        # Raw unredacted paths live STRICTLY in 0700/0600 private evidence_store!
        priv_fail = ev_store / check_bname / "generations" / gen_chk / "trials" / check_tid / "failure-locator.json"
        priv_pause = ev_store / check_bname / ".private-pause.json"
        assert priv_fail.is_file() and stat.S_IMODE(priv_fail.stat().st_mode) == 0o600
        assert str(tmp_path.resolve()) in priv_fail.read_text(encoding="utf-8")
        assert priv_pause.is_file() and stat.S_IMODE(priv_pause.stat().st_mode) == 0o600
        assert str(tmp_path.resolve()) in priv_pause.read_text(encoding="utf-8")

    # Configure app, index all batches, and run Toolkit + API verifications
    cfg_dir = tmp_path / "cfg_rev5"
    cfg_dir.mkdir(parents=True)
    for fn in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml", "gateway_profiles.yaml", "direct_scorers.yaml"):
        src_f = Path("/home/share-data/eval/config") / fn
        if src_f.is_file():
            (cfg_dir / fn).write_text(src_f.read_text(encoding="utf-8"), encoding="utf-8")
    # Initially approve fail_bdir, eh_bdir, sg_bdir; leave leg_bdir pending first to test source-approval enforcement!
    approval_base = f"""version: "v1"
default_project_id: "default"
default_owner_id: "system"
sources:
  - source_root: "{fail_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
  - source_root: "{eh_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
  - source_root: "{sg_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
"""
    (cfg_dir / "source_approval_registry.yaml").write_text(approval_base, encoding="utf-8")
    isolated_settings.tianxi_root = fake_tx
    isolated_settings.config_dir = cfg_dir
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx

    assert ctx.coordinator.hot.scan_single_harbor_batch(leg_bdir, force=True) == "indexed"
    assert ctx.coordinator.hot.scan_single_harbor_batch(fail_bdir, force=True) == "indexed"
    assert ctx.coordinator.hot.scan_single_harbor_batch(eh_bdir, force=True) == "indexed"
    assert ctx.coordinator.hot.scan_single_harbor_batch(sg_bdir, force=True) == "indexed"

    leg_bid = make_batch_id(f"harbor_runs:{leg_bname}")
    fail_bid = make_batch_id(f"harbor_runs:{fail_bname}")
    sg_bid = make_batch_id(f"harbor_runs:{sg_bname}")

    create_user(ctx.db, username="rev5_adm", password="StrongRev5Adm!26", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="rev5_noscope", password="StrongRev5No!26", role="researcher", projects=["default"], scopes=[])
    c_adm = TestClient(app)
    csrf_adm = login_client(c_adm, "rev5_adm", "StrongRev5Adm!26")
    c_ns = TestClient(app)
    csrf_ns = login_client(c_ns, "rev5_noscope", "StrongRev5No!26")

    # Verify failed trial detail DTO has properly extracted & redacted failure host + log path
    f_det = c_adm.get(f"/api/v1/batches/{fail_bid}/trials/{fail_tid}")
    assert f_det.status_code == 200
    assert f_det.json()["failure"]["log_redacted"].startswith(f"<redacted>/jobs/{fail_bname}/")
    assert f_det.json()["failure"]["host_redacted"] == "10.46.x.197"

    # 1a) E-02 legacy training capture: unapproved source -> 403 SCOPE_FORBIDDEN
    r_leg_unapp = c_adm.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_training_captures": True})
    assert r_leg_unapp.status_code == 403 and r_leg_unapp.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # Approve legacy training source & rescan approval
    (cfg_dir / "source_approval_registry.yaml").write_text(
        approval_base
        + f"""  - source_root: "{leg_bdir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory"]
""",
        encoding="utf-8",
    )
    ctx.coordinator.hot.approval_registry.reload()
    ctx.toolkit.approval_registry.reload()
    assert ctx.coordinator.hot.scan_single_harbor_batch(leg_bdir, force=True) == "indexed"

    # Missing trace:read scope -> 403 SCOPE_FORBIDDEN
    r_leg_ns = c_ns.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_ns}, json={"check_training_captures": True})
    assert r_leg_ns.status_code == 403 and r_leg_ns.json()["error"]["code"] == "SCOPE_FORBIDDEN"

    # Authorized user -> 200, exit_code=0, passed_checks=True, training_capture_check.passed=True!
    r_leg_ok = c_adm.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_training_captures": True})
    assert r_leg_ok.status_code == 200, r_leg_ok.text
    assert r_leg_ok.json()["exit_code"] == 0 and r_leg_ok.json()["passed_checks"] is True
    assert r_leg_ok.json()["report"]["training_capture_check"]["passed"] is True

    # Tamper legacy capture.json -> 409 GENERATION_MISMATCH!
    orig_cap_bytes = (leg_tdir / "capture.json").read_bytes()
    (leg_tdir / "capture.json").write_text('{"tampered": true}\n', encoding="utf-8")
    r_leg_tamp_cap = c_adm.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_training_captures": True})
    assert r_leg_tamp_cap.status_code == 409 and r_leg_tamp_cap.json()["error"]["code"] == "GENERATION_MISMATCH"
    (leg_tdir / "capture.json").write_bytes(orig_cap_bytes)

    # Tamper legacy training_trace.json -> 409 GENERATION_MISMATCH!
    (leg_tdir / "training_trace.json").write_text('{"schema_version": 1, "turns": []}\n', encoding="utf-8")
    r_leg_tamp_tr = c_adm.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_training_captures": True})
    assert r_leg_tamp_tr.status_code == 409 and r_leg_tamp_tr.json()["error"]["code"] == "GENERATION_MISMATCH"
    (leg_tdir / "training_trace.json").write_bytes(trace_bytes)

    # 3a) I-01 legacy same-size same-mtime overwrite (defeating stat-only digest!) -> STILL 409 GENERATION_MISMATCH!
    leg_ep_path = leg_bdir / "episodes.jsonl"
    orig_leg_ep_bytes = leg_ep_path.read_bytes()
    orig_st = leg_ep_path.stat()
    mutated_leg_ep_bytes = orig_leg_ep_bytes.replace(b'"reward": 1.0', b'"reward": 0.0')
    assert len(mutated_leg_ep_bytes) == len(orig_leg_ep_bytes)
    leg_ep_path.write_bytes(mutated_leg_ep_bytes)
    os.utime(leg_ep_path, ns=(orig_st.st_atime_ns, orig_st.st_mtime_ns))
    r_leg_stat_bypass = c_adm.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": False})
    assert r_leg_stat_bypass.status_code == 409 and r_leg_stat_bypass.json()["error"]["code"] == "GENERATION_MISMATCH"

    # 3b) I-01 v2 same-generation different-content rewrite (self-consistent publish-manifest.json keeping SAME generation_id!)
    r_sg_ok = c_adm.post(f"/api/v1/toolkit/verify/{sg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": True})
    assert r_sg_ok.status_code == 200 and r_sg_ok.json()["passed_checks"] is True and r_sg_ok.json()["report"]["totals"]["valid_mean"] == 1.0

    sg_pm_path = sg_bdir / "publish-manifest.json"
    sg_ep_path = sg_bdir / "episodes.jsonl"
    sg_rep_path = sg_bdir / "batch-report.json"
    orig_sg_pm = sg_pm_path.read_bytes()
    orig_sg_ep = sg_ep_path.read_bytes()
    orig_sg_rep = sg_rep_path.read_bytes()

    # Rewrite episodes.jsonl + batch-report.json to reward=0.0 AND update publish-manifest.json with the SAME generation_id!
    pm_doc = json.loads(orig_sg_pm.decode("utf-8"))
    new_sg_ep = orig_sg_ep.replace(b'"reward": 1.0', b'"reward": 0.0')
    new_sg_rep = orig_sg_rep.replace(b'"valid_mean": 1.0', b'"valid_mean": 0.0')
    sg_ep_path.write_bytes(new_sg_ep)
    sg_rep_path.write_bytes(new_sg_rep)
    pm_doc["top_level_files"]["episodes.jsonl"]["sha256"] = hashlib.sha256(new_sg_ep).hexdigest()
    pm_doc["top_level_files"]["episodes.jsonl"]["size"] = len(new_sg_ep)
    pm_doc["top_level_files"]["batch-report.json"]["sha256"] = hashlib.sha256(new_sg_rep).hexdigest()
    pm_doc["top_level_files"]["batch-report.json"]["size"] = len(new_sg_rep)
    sg_pm_path.write_text(json.dumps(pm_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Toolkit MUST reject BOTH plain and --check-smoke with 409 GENERATION_MISMATCH!
    r_sg_samegen_plain = c_adm.post(f"/api/v1/toolkit/verify/{sg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": False})
    assert r_sg_samegen_plain.status_code == 409 and r_sg_samegen_plain.json()["error"]["code"] == "GENERATION_MISMATCH"
    r_sg_samegen_smoke = c_adm.post(f"/api/v1/toolkit/verify/{sg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": True})
    assert r_sg_samegen_smoke.status_code == 409 and r_sg_samegen_smoke.json()["error"]["code"] == "GENERATION_MISMATCH"

    # 3c) Materialize an absent optional file (official-result.json) and update publish-manifest.json keeping same gen_id -> 409!
    sg_ep_path.write_bytes(orig_sg_ep)
    sg_rep_path.write_bytes(orig_sg_rep)
    off_bytes = json.dumps({"dataset": "Terminal-Bench 2.1", "metric": "acc", "source": "official", "score": 0.5, "denominator": 1}).encode("utf-8")
    (sg_bdir / "official-result.json").write_bytes(off_bytes)
    pm_doc2 = json.loads(orig_sg_pm.decode("utf-8"))
    pm_doc2["absent_optional_files"] = [x for x in pm_doc2["absent_optional_files"] if x != "official-result.json"]
    pm_doc2["top_level_files"]["official-result.json"] = {"sha256": hashlib.sha256(off_bytes).hexdigest(), "size": len(off_bytes)}
    sg_pm_path.write_text(json.dumps(pm_doc2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    r_sg_opt_drift = c_adm.post(f"/api/v1/toolkit/verify/{sg_bid}", headers={"X-CSRF-Token": csrf_adm}, json={"check_smoke": False})
    assert r_sg_opt_drift.status_code == 409 and r_sg_opt_drift.json()["error"]["code"] == "GENERATION_MISMATCH"

    ctx.db.close_thread_connection()


def test_m2_rev6_existing_db_upgrade_backfill_and_fail_closed_409(
    tmp_path: Path, isolated_settings: Settings
) -> None:
    """M2 rev6 regression closing @theshy rev5 upgrade-path I-01 finding:
    Reusing an existing pre-rev5 (rev4) SQLite DB where `checks_json.top_level_files`,
    `publish_manifest_sha256`, and legacy `training`/`training_capture` SHA-256 digests are absent:
    1) Before backfill completes, Toolkit MUST fail closed with 409 GENERATION_MISMATCH on BOTH
       unbackfilled v2 rows (never self-verifying disk publish-manifest.json) AND unbackfilled legacy
       rows (never reading unbound sibling capture.json).
    2) Normal startup hot scan (`create_app(..., run_startup_scan=True)`, NO manual `force=True`!)
       automatically detects and backfills unchanged rev4 v2 & legacy rows while preserving their
       exact `generation_id`s, while REJECTING any rev4 batch whose on-disk content was tampered
       under the same `generation_id` (`PUBLISH_GENERATION_CONFLICT`, Toolkit still returns 409).
    3) After normal startup backfill, Toolkit passes (200) on both valid batches, subsequent normal
       hot scans skip both as `skipped_unchanged`, and post-backfill same-gen/capture tampers -> 409!"""
    import hashlib

    orig_fw = Path("/home/share-data/tianxi_benches/framework")
    patch_file = PATCHES_DIR / "run_py_publish_v2.patch"
    work_fw = tmp_path / "fw_rev6"
    fw_sub = work_fw / "framework"
    fw_sub.mkdir(parents=True)
    for fn in ("run.py", "batch_guard.py", "direct.py", "gateway_capture.py", "report.py", "vlmevalkit.py", "training_capture.py"):
        shutil.copy2(orig_fw / fn, fw_sub / fn)
    shutil.copy2(PATCHES_DIR / "publish_v2.py", fw_sub / "publish_v2.py")
    assert subprocess.run(["patch", "-p1", "--fuzz=0", "-i", str(patch_file)], cwd=str(work_fw), capture_output=True, text=True).returncode == 0

    ev_store = isolated_settings.evidence_store_dir
    assert ev_store is not None
    ev_store.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(ev_store, 0o700)

    fake_tx = tmp_path / "tx_rev6"
    hr_dir = fake_tx / "harbor-runs"
    hr_dir.mkdir(parents=True)

    # Helper to publish a clean v2 Harbor batch via patched run.py
    old_sys_path = list(sys.path)
    sys.path.insert(0, str(fw_sub))
    try:
        for mod_name in ("publish_v2", "batch_guard", "direct", "gateway_capture", "report", "vlmevalkit", "training_capture", "patched_run_rev6"):
            sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location("patched_run_rev6", fw_sub / "run.py")
        assert spec and spec.loader
        run_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_mod)

        def _publish_v2_harbor(bname: str, tid: str, task_id: str) -> Path:
            jdir = tmp_path / "raw_work_rev6" / bname
            (jdir / tid / "agent").mkdir(parents=True)
            (jdir / tid / "agent" / "trajectory.json").write_text(
                json.dumps(
                    {
                        "steps": [
                            {
                                "step_id": 1,
                                "source": "agent",
                                "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": "echo ok"}}],
                                "observation": {"results": [{"source_call_id": "c1", "content": "ok", "is_error": False}]},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (jdir / tid / "result.json").write_text(
                json.dumps(
                    {
                        "id": tid,
                        "task_name": task_id,
                        "agent_info": {"name": "opencode", "version": "1.19.3"},
                        "verifier_result": {"rewards": {"reward": 1.0}},
                        "started_at": "2026-11-25T00:00:00Z",
                        "finished_at": "2026-11-25T00:00:05Z",
                    }
                ),
                encoding="utf-8",
            )
            bdir = hr_dir / bname
            meta = {"schema_version": 1, "dataset": "Terminal-Bench 2.1", "harness": "opencode", "model": "Qwen3_8-27B", "batch": bname}
            with run_mod.publish_v2.publish_lock(bdir):
                assert run_mod.publish(jdir, bdir, meta, source_host="127.0.0.1", evidence_store_dir=ev_store) == 1
                (bdir / "batch-status.json").write_text(
                    json.dumps(
                        {
                            "status": "finished",
                            "planned": 1,
                            "completed": 1,
                            "task_attempts": {task_id: 1},
                            "source": run_mod.publish_v2.public_source_locator("127.0.0.1", jdir, bname),
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                run_mod.write_batch_report(bdir, evidence_store_dir=ev_store)
            return bdir

        v2_ok_bname = "rev6-v2-upgrade-ok"
        v2_ok_dir = _publish_v2_harbor(v2_ok_bname, "c1111111-1111-1111-1111-111111111111", "up-v2-task")

        v2_bad_bname = "rev6-v2-upgrade-tampered"
        v2_bad_dir = _publish_v2_harbor(v2_bad_bname, "c2222222-2222-2222-2222-222222222222", "up-v2-tampered")
    finally:
        sys.path[:] = old_sys_path

    # Legacy training capture batch (no publish-manifest.json)
    leg_tid = "c3333333-3333-3333-3333-333333333333"
    leg_bname = "rev6-legacy-train-upgrade"
    leg_dir = hr_dir / leg_bname
    leg_tdir = leg_dir / "trials" / leg_tid
    leg_tdir.mkdir(parents=True)
    trace_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "turns": [
                    {
                        "node_id": "t0",
                        "prompt_token_ids": [1, 2],
                        "completion_token_ids": [3],
                        "per_token_logps": [-0.1],
                        "loss_mask": [0, 0, 1],
                    }
                ],
            }
        )
        + "\n"
    ).encode("utf-8")
    (leg_tdir / "training_trace.json").write_bytes(trace_bytes)
    (leg_tdir / "capture.json").write_text(
        json.dumps(
            {
                "trial_id": leg_tid,
                "purpose": "train",
                "checkpoint": "ckpt-rev6-up",
                "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (leg_tdir / "result.json").write_text(
        json.dumps({"id": leg_tid, "task_name": "up-train-1", "verifier_rewards": {"reward": 1.0}, "reward_key": "reward"}) + "\n",
        encoding="utf-8",
    )
    (leg_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "dataset": "Terminal-Bench 2.1", "harness": "opencode", "model": "Qwen3_8-27B", "batch": leg_bname}) + "\n",
        encoding="utf-8",
    )
    (leg_dir / "batch-status.json").write_text(
        json.dumps({"status": "finished", "planned": 1, "completed": 1, "task_attempts": {"up-train-1": 1}}) + "\n",
        encoding="utf-8",
    )
    (leg_dir / "episodes.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "trial_id": leg_tid,
                "dataset": "Terminal-Bench 2.1",
                "task_id": "up-train-1",
                "harness": "opencode",
                "runner": "harbor",
                "model_route": "Qwen3_8-27B",
                "checkpoint": "ckpt-rev6-up",
                "status": "scored",
                "reward": 1.0,
                "result": f"trials/{leg_tid}/result.json",
                "training_trace": f"trials/{leg_tid}/training_trace.json",
                "training_status": "validated",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cfg_dir = tmp_path / "cfg_rev6"
    cfg_dir.mkdir(parents=True)
    for fn in ("taxonomy.yaml", "media_matching_rules.yaml", "token_semantics_registry.yaml", "gateway_profiles.yaml", "direct_scorers.yaml"):
        src_f = Path("/home/share-data/eval/config") / fn
        if src_f.is_file():
            (cfg_dir / fn).write_text(src_f.read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dir / "source_approval_registry.yaml").write_text(
        f"""version: "v1"
default_project_id: "default"
default_owner_id: "system"
sources:
  - source_root: "{hr_dir}"
    match_mode: "dir"
    project_id: "default"
    owner_id: "system"
    approval_status: "approved_restricted"
    approved_evidence_classes: ["summary_dto", "atif_trajectory", "verifier_log"]
""",
        encoding="utf-8",
    )
    isolated_settings.tianxi_root = fake_tx
    isolated_settings.config_dir = cfg_dir

    # Seed DB then downgrade rows to exact rev4 SQLite schema payload (no top_level_files / publish_manifest_sha256 / training_capture)
    app1 = create_app(isolated_settings, run_startup_scan=False)
    ctx1 = app1.state.ctx
    assert ctx1.coordinator.hot.scan_single_harbor_batch(v2_ok_dir, force=True) == "indexed"
    assert ctx1.coordinator.hot.scan_single_harbor_batch(v2_bad_dir, force=True) == "indexed"
    assert ctx1.coordinator.hot.scan_single_harbor_batch(leg_dir, force=True) == "indexed"

    v2_ok_bid = make_batch_id(f"harbor_runs:{v2_ok_bname}")
    v2_bad_bid = make_batch_id(f"harbor_runs:{v2_bad_bname}")
    leg_bid = make_batch_id(f"harbor_runs:{leg_bname}")

    orig_gens: dict[str, str] = {}
    with ctx1.db.transaction() as conn:
        for bid in (v2_ok_bid, v2_bad_bid, leg_bid):
            row = conn.execute("SELECT generation_id, checks_json FROM batches WHERE batch_id = ?", (bid,)).fetchone()
            assert row is not None
            orig_gens[bid] = str(row["generation_id"])
            chk = json.loads(row["checks_json"])
            rev4_checks = {
                "verified_payload_sha256": chk["verified_payload_sha256"],
                "generation_digests": chk["generation_digests"],
            }
            conn.execute("UPDATE batches SET checks_json = ? WHERE batch_id = ?", (json.dumps(rev4_checks), bid))
        # Also downgrade legacy training episode evidence_refs_json to exact rev4 shape (no sha256, no training_capture)
        ep_row = conn.execute("SELECT evidence_refs_json FROM episodes WHERE batch_id = ? AND trial_id = ?", (leg_bid, leg_tid)).fetchone()
        assert ep_row is not None
        rev4_ev = json.loads(ep_row["evidence_refs_json"])
        rev4_ev.pop("training_capture", None)
        rev4_ev["training"] = {
            "rel_path": f"trials/{leg_tid}/training_trace.json",
            "generation_id": orig_gens[leg_bid],
            "sha256": None,
            "storage_tier": "shared_cifs",
        }
        conn.execute(
            "UPDATE episodes SET evidence_refs_json = ? WHERE batch_id = ? AND trial_id = ?",
            (json.dumps(rev4_ev), leg_bid, leg_tid),
        )

    create_user(ctx1.db, username="rev6_adm", password="StrongRev6Adm!26", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    c1 = TestClient(app1)
    csrf1 = login_client(c1, "rev6_adm", "StrongRev6Adm!26")

    # 1) BEFORE backfill: Toolkit MUST fail closed with 409 GENERATION_MISMATCH on BOTH rev4 v2 and rev4 legacy!
    r_pre_v2 = c1.post(f"/api/v1/toolkit/verify/{v2_ok_bid}", headers={"X-CSRF-Token": csrf1}, json={"check_smoke": True})
    assert r_pre_v2.status_code == 409 and r_pre_v2.json()["error"]["code"] == "GENERATION_MISMATCH"
    assert r_pre_v2.json()["error"]["detail"].get("digest_backfill_required") is True

    r_pre_leg = c1.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf1}, json={"check_training_captures": True})
    assert r_pre_leg.status_code == 409 and r_pre_leg.json()["error"]["code"] == "GENERATION_MISMATCH"

    # Even if top_level_files existed on the legacy batch while training/training_capture digests are still missing -> STILL 409!
    saved_leg_checks = ctx1.db.fetchone("SELECT checks_json FROM batches WHERE batch_id = ?", (leg_bid,))["checks_json"]
    with ctx1.db.transaction() as conn:
        fake_top = {}
        for fn in ("manifest.json", "batch-status.json", "episodes.jsonl"):
            b = (leg_dir / fn).read_bytes()
            fake_top[fn] = {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
        tmp_chk = json.loads(saved_leg_checks)
        tmp_chk["manifest_mode"] = "legacy_v1"
        tmp_chk["top_level_files"] = fake_top
        tmp_chk["absent_optional_files"] = ["batch-report.json", "trial-digests.jsonl", "official-result.json", "vlmevalkit/metrics.json", "selection.json", "run-config.json"]
        conn.execute("UPDATE batches SET checks_json = ? WHERE batch_id = ?", (json.dumps(tmp_chk), leg_bid))
    r_pre_leg_cap = c1.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf1}, json={"check_training_captures": True})
    assert r_pre_leg_cap.status_code == 409 and r_pre_leg_cap.json()["error"]["code"] == "GENERATION_MISMATCH"
    # Restore exact rev4 legacy checks_json before normal upgrade startup
    with ctx1.db.transaction() as conn:
        conn.execute("UPDATE batches SET checks_json = ? WHERE batch_id = ?", (saved_leg_checks, leg_bid))
    ctx1.db.close_thread_connection()

    # Tamper v2_bad_dir on disk keeping the SAME generation_id BEFORE rev6 normal startup!
    bad_ep_p = v2_bad_dir / "episodes.jsonl"
    bad_rep_p = v2_bad_dir / "batch-report.json"
    bad_pm_p = v2_bad_dir / "publish-manifest.json"
    new_bad_ep = bad_ep_p.read_bytes().replace(b'"reward": 1.0', b'"reward": 0.0')
    new_bad_rep = bad_rep_p.read_bytes().replace(b'"valid_mean": 1.0', b'"valid_mean": 0.0')
    bad_ep_p.write_bytes(new_bad_ep)
    bad_rep_p.write_bytes(new_bad_rep)
    bad_pm = json.loads(bad_pm_p.read_text(encoding="utf-8"))
    bad_pm["top_level_files"]["episodes.jsonl"]["sha256"] = hashlib.sha256(new_bad_ep).hexdigest()
    bad_pm["top_level_files"]["episodes.jsonl"]["size"] = len(new_bad_ep)
    bad_pm["top_level_files"]["batch-report.json"]["sha256"] = hashlib.sha256(new_bad_rep).hexdigest()
    bad_pm["top_level_files"]["batch-report.json"]["size"] = len(new_bad_rep)
    bad_pm_p.write_text(json.dumps(bad_pm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 2) Normal rev6 startup reusing the SAME SQLite DB (`run_startup_scan=True` via real ASGI lifespan, NO manual force=True!)
    app2 = create_app(isolated_settings, run_startup_scan=True)
    ctx2 = app2.state.ctx
    with TestClient(app2) as c2:
        # Both valid batches MUST be automatically backfilled with EXACT original generation_id preserved!
        for bid in (v2_ok_bid, leg_bid):
            b_row = ctx2.db.fetchone("SELECT generation_id, checks_json FROM batches WHERE batch_id = ?", (bid,))
            assert b_row is not None and b_row["generation_id"] == orig_gens[bid]
            b_chk = json.loads(b_row["checks_json"])
            assert isinstance(b_chk.get("top_level_files"), dict) and len(b_chk["top_level_files"]) >= 3
        v2_ok_chk = json.loads(ctx2.db.fetchone("SELECT checks_json FROM batches WHERE batch_id = ?", (v2_ok_bid,))["checks_json"])
        assert isinstance(v2_ok_chk.get("publish_manifest_sha256"), str) and len(v2_ok_chk["publish_manifest_sha256"]) == 64

        leg_ep_after = json.loads(
            ctx2.db.fetchone("SELECT evidence_refs_json FROM episodes WHERE batch_id = ? AND trial_id = ?", (leg_bid, leg_tid))["evidence_refs_json"]
        )
        assert len(leg_ep_after["training"]["sha256"]) == 64
        assert len(leg_ep_after["training_capture"]["sha256"]) == 64

        # Tampered rev4 batch MUST have been rejected (PUBLISH_GENERATION_CONFLICT) and remain unbackfilled!
        assert ctx2.db.fetchone("SELECT 1 FROM index_warnings WHERE code = 'PUBLISH_GENERATION_CONFLICT'") is not None
        v2_bad_chk = json.loads(ctx2.db.fetchone("SELECT checks_json FROM batches WHERE batch_id = ?", (v2_bad_bid,))["checks_json"])
        assert "top_level_files" not in v2_bad_chk

        # 3) Post-startup Toolkit checks: both valid backfilled batches pass (200), tampered batch still 409!
        csrf2 = login_client(c2, "rev6_adm", "StrongRev6Adm!26")
        r_ok_v2 = c2.post(f"/api/v1/toolkit/verify/{v2_ok_bid}", headers={"X-CSRF-Token": csrf2}, json={"check_smoke": True})
        assert r_ok_v2.status_code == 200 and r_ok_v2.json()["passed_checks"] is True

        r_ok_leg = c2.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf2}, json={"check_training_captures": True})
        assert r_ok_leg.status_code == 200 and r_ok_leg.json()["passed_checks"] is True
        assert r_ok_leg.json()["report"]["training_capture_check"]["passed"] is True

        r_still_bad = c2.post(f"/api/v1/toolkit/verify/{v2_bad_bid}", headers={"X-CSRF-Token": csrf2}, json={"check_smoke": False})
        assert r_still_bad.status_code == 409 and r_still_bad.json()["error"]["code"] == "GENERATION_MISMATCH"

        # Subsequent normal hot scan skips both already-backfilled valid batches cleanly!
        second_scan = ctx2.coordinator.hot.run_hot_scan(force=False)
        assert second_scan["batches"].get("skipped_unchanged", 0) >= 2
        assert second_scan["batches"].get("indexed", 0) == 0

        # Post-backfill tamper on legacy capture.json -> 409 GENERATION_MISMATCH!
        (leg_tdir / "capture.json").write_text('{"tampered_after_backfill": true}\n', encoding="utf-8")
        r_leg_tamp = c2.post(f"/api/v1/toolkit/verify/{leg_bid}", headers={"X-CSRF-Token": csrf2}, json={"check_training_captures": True})
        assert r_leg_tamp.status_code == 409 and r_leg_tamp.json()["error"]["code"] == "GENERATION_MISMATCH"

    ctx2.db.close_thread_connection()
