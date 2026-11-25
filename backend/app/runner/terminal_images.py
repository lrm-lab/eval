"""Read the Terminal-Bench image inventory from the execution host."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any, TypedDict

from app.config import Settings
from app.runner.terminal_dependencies import NGINX_TASK, is_terminal_nginx_job, nginx_dependency_snapshot_ready

TERMINAL_SCOPE_MESSAGE = "Terminal-Bench 当前仅开放已就绪的标准 Harbor nginx-request-logging 单题"


class TerminalImageReadiness(TypedDict):
    task_count: int
    ready_tasks: list[str]


def terminal_image_readiness(settings: Settings) -> TerminalImageReadiness | None:
    config_path = settings.managed_framework_dir / "harbor.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        bundle_path = os.environ.get("TIANXI_BENCH_BUNDLE") or config["bundle"]
        bundle = (config_path.parent / Path(bundle_path).expanduser()).resolve()
        dataset = (bundle / config["datasets"]["terminal"]["path"]).resolve()
        if not dataset.is_relative_to(bundle):
            return None
        tasks: dict[str, str | None] = {}
        for path in sorted(dataset.glob("*/task.toml")):
            if path.is_file() and path.resolve().is_relative_to(dataset):
                task = tomllib.loads(path.read_text(encoding="utf-8"))
                image = task.get("environment", {}).get("docker_image")
                tasks[path.parent.name] = image.removeprefix("docker.io/") if isinstance(image, str) else None
        if not tasks:
            return None
        # Filter on the daemon to avoid enumerating unrelated images on this shared host.
        repositories = {image.rsplit("/", 1)[0] + "/*" if "/" in image else image for image in tasks.values() if image}
        if not repositories:
            return {"task_count": len(tasks), "ready_tasks": []}
        command = ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]
        for repository in sorted(repositories):
            command.extend(["--filter", f"reference={repository}"])
        result = subprocess.run(
            command, capture_output=True, check=True, text=True, timeout=0.4,
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None

    cached = {line.removeprefix("docker.io/") for line in result.stdout.splitlines()}
    verified_tasks = {task.strip() for task in os.environ.get("LRM_TERMINAL_VERIFIED_TASKS", "").split(",")}
    ready = [NGINX_TASK] if (
        tasks.get(NGINX_TASK) in cached and NGINX_TASK in verified_tasks and nginx_dependency_snapshot_ready()
    ) else []
    return {"task_count": len(tasks), "ready_tasks": ready}


def terminal_selection_ready(spec: dict[str, Any], readiness: TerminalImageReadiness | None) -> bool:
    return is_terminal_nginx_job(spec) and readiness is not None and NGINX_TASK in readiness["ready_tasks"]
