import json
import secrets
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.runner.cli_contract import build_managed_run_py_command, materialize_managed_run_config
from app.runner.client import RunnerIPCClient
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from tests.test_api_m3 import _start_external_runner_daemon


@pytest.mark.parametrize(("kind", "mode"), [
    ("harbor", None), ("harbor_gateway", None), ("vlmevalkit", "infer"),
    ("vlmevalkit", "all"), ("direct", None),
])
def test_readonly_frozen_plan(isolated_settings: Settings, tmp_path: Path, kind, mode):
    work = tmp_path / "job"
    output = tmp_path / "output"
    evidence = tmp_path / "private-evidence"
    spec = dict(kind=kind, suite_id="tianxi-synth-v1-train", model_route="test-model",
                tasks=["log-rollup-train-001"], selection_mode="tasks", batch_name="plan",
                agent="opencode", harness_version="1.19.3")
    if kind == "direct":
        cases = Path(__file__).resolve().parents[2] / "config/direct_suites/direct-smoke-v1/cases.jsonl"
        spec.update(suite_id="direct-smoke-v1", dataset_path=str(cases), tasks=[],
                    planned_tasks=["smoke-arith-001", "smoke-echo-002"], selection_mode="all")
    elif kind == "vlmevalkit":
        spec.update(suite_id="cmmmu-val", vlmevalkit_mode=mode, selection_mode="all", tasks=[])

    overlay = materialize_managed_run_config(isolated_settings, spec, work)
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
    before = {p: p.read_bytes() for p in work.iterdir()}
    cmd = build_managed_run_py_command(
        run_py_path=str(resolve_verified_managed_script(isolated_settings, "run.py")),
        python_exe=sys.executable, spec=spec, base_url="http://127.0.0.1:1/v1",
        config_path=None if kind == "direct" else str(overlay), work_dir=str(work),
        output_dir=str(output), plan_only=True,
    )
    assert {"--evidence-store", "--mode"}.isdisjoint(cmd)
    assert ("--gateway-capture" in cmd) == (kind == "harbor_gateway")
    result = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=30,
                            env=build_sanitized_subprocess_env({"LRM_EVIDENCE_STORE_DIR": str(evidence)}))
    assert result.returncode == 0, result.stderr
    assert not output.exists() and not evidence.exists()
    assert {p: p.read_bytes() for p in work.iterdir()} == before
    if kind == "harbor":
        assert "harbor run" in result.stdout and spec["tasks"][0] in result.stdout
    else:
        plan = json.loads(result.stdout)
        assert plan["runner"] == ("openenv-gateway" if kind == "harbor_gateway" else kind)
        if kind == "vlmevalkit":
            assert (plan["mode"], plan["tasks"]) == (mode, "all")
        elif kind == "direct":
            assert plan["tasks"] == spec["planned_tasks"]
        else:
            assert plan["tasks"] == spec["tasks"]


def test_native_resume_reuses_unchanged_overlay(isolated_settings: Settings, tmp_path: Path):
    spec = dict(kind="harbor", suite_id="tianxi-synth-v1-train", batch_name="locked",
                selection_mode="tasks", tasks=["log-rollup-train-001"])
    overlay = materialize_managed_run_config(isolated_settings, spec, tmp_path)
    original = overlay.stat()
    resumed = dict(spec, resume_mode="native")
    path = materialize_managed_run_config(isolated_settings, resumed, tmp_path)
    assert path == overlay
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
    with pytest.raises(ValueError):
        materialize_managed_run_config(isolated_settings, dict(resumed, tasks=[]), tmp_path)
    overlay.write_bytes(overlay.read_bytes() + b"# changed\n")
    with pytest.raises(ValueError):
        materialize_managed_run_config(isolated_settings, resumed, tmp_path)
    overlay.unlink()
    with pytest.raises(ValueError):
        materialize_managed_run_config(isolated_settings, resumed, tmp_path)
    assert not overlay.exists()


def test_external_runner_rejects_invalid_native_resume(isolated_settings: Settings):
    ipc = RunnerIPCClient(isolated_settings)
    with _start_external_runner_daemon(isolated_settings) as daemon:
        health = ipc.probe_health()
        assert health["runner_pid"] == daemon.pid
        for kind in ("harbor_gateway", "direct", "vlmevalkit", "harbor"):
            job_id = f"reject-{kind}"
            status, body = ipc.submit_job({
                "job_id": job_id, "channel_secret": secrets.token_hex(16),
                "runner_instance_id": health["runner_instance_id"],
                "spec": {"kind": kind, "resume_mode": "native"},
            })
            assert (status, body["code"]) == (409, "RESUME_UNSUPPORTED")
            assert not (isolated_settings.runner_work_dir / job_id).exists()
