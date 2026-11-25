from hashlib import sha256
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

from fastapi.testclient import TestClient

from app.config import Settings
from app.runner import terminal_images
from app.runner.daemon import IsolatedRunnerManager
from app.runner.terminal_dependencies import NGINX_DEPS_DIR, NGINX_DEPS_SHA256, NGINX_TASK
from tests.conftest import login_client


def test_terminal_readiness_checks_original_tags(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    dataset = bundle / "datasets" / "terminal-bench-2.1" / "tasks"
    for task, image in {
        NGINX_TASK: "example/cached:official",
        "other-tag": "example/other-tag:official",
        "missing": "example/missing:official",
    }.items():
        directory = dataset / task
        directory.mkdir(parents=True)
        (directory / "task.toml").write_text(f'[environment]\ndocker_image = "{image}"\n')

    monkeypatch.setenv("TIANXI_BENCH_BUNDLE", str(bundle))
    managed = Path(__file__).resolve().parents[1] / "app" / "managed_framework"
    settings = Settings(managed_framework_dir=managed)

    def list_images(command, **kwargs):
        assert command[:3] == ["docker", "image", "ls"]
        assert command[-2:] == ["--filter", "reference=example/*"]
        return CompletedProcess(command, 0, "docker.io/example/cached:official\nexample/other-tag:official\n")

    monkeypatch.setattr(terminal_images.subprocess, "run", list_images)
    monkeypatch.delenv("LRM_TERMINAL_VERIFIED_TASKS", raising=False)
    empty = {"task_count": 3, "ready_tasks": []}
    assert terminal_images.terminal_image_readiness(settings) == empty
    monkeypatch.setenv("LRM_TERMINAL_VERIFIED_TASKS", f"{NGINX_TASK}, other-tag")
    monkeypatch.delenv(NGINX_DEPS_DIR, raising=False)
    assert terminal_images.terminal_image_readiness(settings) == empty
    depot = tmp_path / "deps"
    depot.mkdir()
    manifest = b"verified dependency manifest"
    (depot / "SHA256SUMS").write_bytes(manifest)
    monkeypatch.setenv(NGINX_DEPS_DIR, str(depot))
    monkeypatch.setenv(NGINX_DEPS_SHA256, "incorrect")
    assert terminal_images.terminal_image_readiness(settings) == empty
    monkeypatch.setenv(NGINX_DEPS_SHA256, sha256(manifest).hexdigest())
    readiness = terminal_images.terminal_image_readiness(settings)
    assert readiness == {"task_count": 3, "ready_tasks": [NGINX_TASK]}
    spec = dict(kind="harbor", suite_id="terminal", selection_mode="tasks", tasks=[NGINX_TASK])
    assert terminal_images.terminal_selection_ready(spec, readiness)
    assert not terminal_images.terminal_selection_ready(dict(spec, kind="harbor_gateway"), readiness)
    assert not terminal_images.terminal_selection_ready(dict(spec, selection_mode="all"), readiness)
    assert not terminal_images.terminal_selection_ready(dict(spec, tasks=[NGINX_TASK, "other-tag"]), readiness)

    def unavailable(command, **kwargs):
        raise TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(terminal_images.subprocess, "run", unavailable)
    assert terminal_images.terminal_image_readiness(settings) is None


def test_runner_refuses_terminal_scopes_outside_the_standard_nginx_smoke(isolated_settings, monkeypatch):
    manager = IsolatedRunnerManager(isolated_settings, None)
    monkeypatch.setattr(manager, "health", lambda: {
        "ok": True,
        "docker_available": True,
        "harbor_gateway_runtime_ready": False,
        "terminal_image_readiness": {"task_count": 2, "ready_tasks": [NGINX_TASK]},
    })
    refused = (
        ("harbor_gateway", "tasks", [NGINX_TASK]),
        ("harbor", "all", []),
        ("harbor", "tasks", ["other-tag"]),
        ("harbor", "tasks", [NGINX_TASK, "other-tag"]),
    )
    for kind, mode, tasks in refused:
        status, body = manager.submit_job({
            "job_id": "terminal-missing-image",
            "runner_instance_id": manager.instance_id,
            "channel_secret": "test-channel-secret",
            "spec": {"kind": kind, "suite_id": "terminal", "selection_mode": mode, "tasks": tasks},
        })
        assert (status, body["code"], body["message"]) == (
            409, "TASK_IMAGE_NOT_READY", terminal_images.TERMINAL_SCOPE_MESSAGE,
        )
    assert not (isolated_settings.runner_work_dir / "terminal-missing-image").exists()


def test_web_uses_runner_inventory_and_refuses_other_terminal_plans(m0_app, monkeypatch):
    execution = m0_app.state.ctx.execution
    runner = execution.probe_runner_health()
    runner["terminal_image_readiness"] = {"task_count": 89, "ready_tasks": [NGINX_TASK, "other-tag"]}
    monkeypatch.setattr(execution, "probe_runner_health", lambda: runner)
    client = TestClient(m0_app)
    csrf = login_client(client, "admin_test", "StrongAdminPassword!1")
    response = client.get("/api/v1/execution/capabilities")
    assert response.status_code == 200
    [terminal] = [suite for suite in response.json()["suites"] if suite["suite_id"] == "terminal"]
    assert terminal["task_count"] == 89
    assert terminal["ready_tasks"] == terminal["sample_tasks"] == [NGINX_TASK]
    assert terminal["selectable"]

    for kind, mode, tasks in (
        ("harbor_gateway", "tasks", [NGINX_TASK]),
        ("harbor", "all", []),
        ("harbor", "tasks", ["other-tag"]),
        ("harbor", "tasks", [NGINX_TASK, "other-tag"]),
    ):
        response = client.post(
            "/api/v1/execution/plan", headers={"X-CSRF-Token": csrf},
            json={"suite_id": "terminal", "kind": kind, "selection_mode": mode, "tasks": tasks},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_IMAGE_NOT_READY"
