from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

from app.config import Settings
from app.runner import terminal_images
from app.runner.daemon import IsolatedRunnerManager


def test_terminal_readiness_checks_original_tags(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    dataset = bundle / "datasets" / "terminal-bench-2.1" / "tasks"
    for task, image in {
        "cached": "example/cached:official",
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
        return CompletedProcess(command, 0, "docker.io/example/cached:official\nexample/other-tag:different\n")

    monkeypatch.setattr(terminal_images.subprocess, "run", list_images)
    readiness = terminal_images.terminal_image_readiness(settings)
    assert readiness == {"task_count": 3, "ready_tasks": ["cached"]}
    assert terminal_images.terminal_selection_ready({"selection_mode": "tasks", "tasks": ["cached"]}, readiness)
    assert not terminal_images.terminal_selection_ready({"selection_mode": "all"}, readiness)

    monkeypatch.setenv("LRM_TERMINAL_VERIFIED_TASKS", "")
    assert terminal_images.terminal_image_readiness(settings) == {"task_count": 3, "ready_tasks": []}
    monkeypatch.setenv("LRM_TERMINAL_VERIFIED_TASKS", "cached, missing")
    assert terminal_images.terminal_image_readiness(settings) == readiness

    def unavailable(command, **kwargs):
        raise TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(terminal_images.subprocess, "run", unavailable)
    assert terminal_images.terminal_image_readiness(settings) is None


def test_runner_refuses_terminal_tasks_without_images(isolated_settings, monkeypatch):
    manager = IsolatedRunnerManager(isolated_settings, None)
    monkeypatch.setattr(manager, "health", lambda: {
        "ok": True,
        "docker_available": True,
        "harbor_gateway_runtime_ready": True,
        "terminal_image_readiness": {"task_count": 2, "ready_tasks": ["cached"]},
    })
    for kind in ("harbor", "harbor_gateway"):
        for mode, tasks in (("all", []), ("tasks", ["cached", "missing"])):
            status, body = manager.submit_job({
                "job_id": "terminal-missing-image",
                "runner_instance_id": manager.instance_id,
                "channel_secret": "test-channel-secret",
                "spec": {"kind": kind, "suite_id": "terminal", "selection_mode": mode, "tasks": tasks},
            })
            assert (status, body["code"]) == (409, "TASK_IMAGE_NOT_READY")
    assert not (isolated_settings.runner_work_dir / "terminal-missing-image").exists()
