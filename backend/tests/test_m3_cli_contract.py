import json
import secrets
import stat
import subprocess
import sys
import tomllib
from hashlib import sha256
from pathlib import Path

import pytest

from app.config import Settings
from app.runner.cli_contract import build_managed_run_py_command, materialize_managed_run_config
from app.runner.client import RunnerIPCClient
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from tests.test_api_m3 import _start_external_runner_daemon


def test_wildclaw_prepares_only_selected_tasks(tmp_path: Path):
    bundle = tmp_path / "bundle"
    source = bundle / "datasets" / "wildclaw-harbor"
    for name in ("first", "second", "unselected"):
        task = source / name
        task.mkdir(parents=True)
        (task / "task.toml").write_text("")
        (task / "setup.sh").write_bytes(b"#!/bin/sh\r\n")

    installer = bundle / "install" / "prepare-wildclaw.py"
    installer.parent.mkdir()
    installer.write_text("""
from pathlib import Path
import shutil

datasets = Path(__file__).resolve().parents[1] / "datasets"
target = datasets / "wildclaw-run"
shutil.copytree(datasets / "wildclaw-harbor", target)
for path in target.glob("*/setup.sh"):
    path.write_bytes(path.read_bytes().replace(b"\\r\\n", b"\\n"))
""")

    managed_dir = Path(__file__).resolve().parents[1] / "app" / "managed_framework"
    subprocess.run(
        [sys.executable, "-c", """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from run import prepare_wildclaw

source, bundle, work = map(Path, sys.argv[2:])
prepared = prepare_wildclaw(source, bundle, work, "wildclaw", ["first", "second"])
assert {path.name for path in prepared.iterdir()} == {"first", "second"}
assert (prepared / "first" / "setup.sh").read_bytes() == b"#!/bin/sh\\n"
assert (source / "first" / "setup.sh").read_bytes() == b"#!/bin/sh\\r\\n"
assert prepare_wildclaw(source, bundle, work, "wildclaw", ["first", "second"]) == prepared

all_tasks = prepare_wildclaw(source, bundle, work / "all", "wildclaw", [])
assert {path.name for path in all_tasks.iterdir()} == {"first", "second", "unselected"}
""", str(managed_dir), str(source), str(bundle), str(tmp_path / "work")],
        check=True,
    )


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

    ca_bundle = tmp_path / "runner-ca.pem"
    ca_bundle.touch()
    overlay = materialize_managed_run_config(
        isolated_settings, spec, work, harbor_agent_ca_bundle=str(ca_bundle),
    )
    overlay_ca = tomllib.loads(overlay.read_text()).get("agent_ca_bundle")
    assert overlay_ca == (str(ca_bundle) if kind == "harbor" else None)
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
    before = {p: p.read_bytes() for p in work.iterdir()}
    cmd = build_managed_run_py_command(
        run_py_path=str(resolve_verified_managed_script(isolated_settings, "run.py")),
        python_exe=sys.executable, spec=spec, base_url="http://127.0.0.1:1/v1",
        config_path=None if kind == "direct" else str(overlay), work_dir=str(work),
        output_dir=str(output), plan_only=True, harbor_agent_ca_bundle=overlay_ca,
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
        assert "--agent-ca-bundle" in cmd
        assert "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/tianxi-host-ca.pem" in result.stdout
        assert str(ca_bundle) in result.stdout
    else:
        assert "--agent-ca-bundle" not in cmd
        plan = json.loads(result.stdout)
        assert plan["runner"] == ("openenv-gateway" if kind == "harbor_gateway" else kind)
        if kind == "vlmevalkit":
            assert (plan["mode"], plan["tasks"]) == (mode, "all")
        elif kind == "direct":
            assert plan["tasks"] == spec["planned_tasks"]
        else:
            assert plan["tasks"] == spec["tasks"]


@pytest.mark.parametrize("previous_ca", [False, True])
def test_native_resume_reuses_unchanged_overlay(isolated_settings: Settings, tmp_path: Path, previous_ca: bool):
    spec = dict(kind="harbor", suite_id="tianxi-synth-v1-train", batch_name="locked",
                selection_mode="tasks", tasks=["log-rollup-train-001"])
    original_ca = tmp_path / "original-ca.pem"
    original_ca.touch()
    overlay = materialize_managed_run_config(
        isolated_settings, spec, tmp_path, harbor_agent_ca_bundle=str(original_ca) if previous_ca else None,
    )
    original = overlay.stat()
    resumed = dict(spec, resume_mode="native")
    path = materialize_managed_run_config(
        isolated_settings, resumed, tmp_path, harbor_agent_ca_bundle=str(tmp_path / "changed-ca.pem"),
    )
    assert path == overlay
    assert tomllib.loads(path.read_text()).get("agent_ca_bundle") == (str(original_ca) if previous_ca else None)
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


def test_gaia_pi_gateway_plan_keeps_the_registered_agent(isolated_settings: Settings, tmp_path: Path):
    bundle = tmp_path / "bundle"
    task = bundle / "datasets/gaia/gaia-test-case"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("")
    work = tmp_path / "work"
    output = tmp_path / "output"
    spec = dict(kind="harbor_gateway", suite_id="gaia", batch_name="gaia-plan", model_route="test-model",
                agent="pi", harness_version="0.87.1", selection_mode="tasks", tasks=[task.name])
    overlay = materialize_managed_run_config(isolated_settings, spec, work)
    cmd = build_managed_run_py_command(
        run_py_path=str(resolve_verified_managed_script(isolated_settings, "run.py")),
        python_exe=sys.executable, spec=spec, base_url="http://127.0.0.1:1/v1",
        config_path=str(overlay), work_dir=str(work), output_dir=str(output), plan_only=True,
    )
    result = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=30,
                            env=build_sanitized_subprocess_env({"TIANXI_BENCH_BUNDLE": str(bundle)}))
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["harness"] == "pi"
    assert plan["agent_import_path"] == "cached_pi_gateway:CachedInterceptPi"
    assert cmd[cmd.index("--agent-version") + 1] == "0.87.1"
    assert not output.exists()


def test_terminal_nginx_dependency_mounts_are_readonly_and_locked(
    isolated_settings: Settings, tmp_path: Path,
):
    depot = tmp_path / "deps"
    files = (
        "apt/debian.sources", "apt/91-isolated-downloads",
        "uv/uv.tar.gz", "python/pytest.whl",
    )
    for name in files:
        path = depot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    manifest = "".join(f"{sha256(name.encode()).hexdigest()}  ./{name}\n" for name in files).encode()
    (depot / "SHA256SUMS").write_bytes(manifest)
    dependency_snapshot = {"root": str(depot), "sha256": sha256(manifest).hexdigest(), "verifier_proxy": True}
    spec = dict(kind="harbor", suite_id="terminal", batch_name="terminal-smoke", model_route="test-model",
                agent="pi", harness_version="0.87.1", selection_mode="tasks", tasks=["nginx-request-logging"])
    work = tmp_path / "work"
    overlay = materialize_managed_run_config(
        isolated_settings, spec, work, terminal_nginx_dependencies=dependency_snapshot,
    )
    resumed = dict(spec, resume_mode="native")
    original = materialize_managed_run_config(
        isolated_settings, resumed, work, terminal_nginx_dependencies={"root": "/different-snapshot"},
    )
    assert original == overlay
    locked_dependencies = tomllib.loads(original.read_text())["terminal_nginx_dependencies"]
    assert locked_dependencies == dependency_snapshot

    launch = dict(python_exe=sys.executable, run_py_path="run.py", config_path=str(overlay), base_url=None,
                  work_dir=str(work), output_dir=str(tmp_path / "output"),
                  terminal_nginx_dependencies=locked_dependencies)
    argv = build_managed_run_py_command(spec=resumed, **launch)
    mounts = json.loads(argv[argv.index("--mounts") + 1])
    assert len(mounts) == 5
    assert all(mount["read_only"] and Path(mount["source"]).is_relative_to(depot) for mount in mounts)
    assert "https_proxy=${LRM_TERMINAL_VERIFIER_HTTPS_PROXY}" in argv
    assert "--agent-env" not in argv
    other_specs = (
        dict(spec, kind="harbor_gateway"),
        dict(spec, tasks=["adaptive-rejection-sampler"]),
        dict(spec, tasks=["nginx-request-logging", "adaptive-rejection-sampler"]),
        dict(spec, selection_mode="all"),
    )
    for other in other_specs:
        other_argv = build_managed_run_py_command(spec=other, **launch)
        assert "--mounts" not in other_argv and "--verifier-env" not in other_argv

    (depot / "python/pytest.whl").write_text("corrupted")
    with pytest.raises(ValueError, match="dependency SHA-256 mismatch"):
        build_managed_run_py_command(spec=spec, **launch)


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
