"""Lock the approved dependency snapshot for the Terminal nginx smoke task."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

NGINX_TASK = "nginx-request-logging"
NGINX_DEPS_DIR = "LRM_RUNNER_TERMINAL_NGINX_DEPS_DIR"
NGINX_DEPS_SHA256 = "LRM_RUNNER_TERMINAL_NGINX_DEPS_SHA256"
VERIFIER_HTTPS_PROXY = "LRM_TERMINAL_VERIFIER_HTTPS_PROXY"

_MOUNTS = (
    ("apt", "/mnt/official-debian"),
    ("apt/debian.sources", "/etc/apt/sources.list.d/debian.sources"),
    ("apt/91-isolated-downloads", "/etc/apt/apt.conf.d/91-isolated-downloads"),
    ("uv", "/mnt/official-uv"),
    ("python", "/mnt/official-python"),
)


def is_terminal_nginx_job(spec: dict[str, Any]) -> bool:
    return (
        spec.get("kind") == "harbor"
        and spec.get("suite_id") == "terminal"
        and spec.get("selection_mode") == "tasks"
        and spec.get("tasks") == [NGINX_TASK]
    )


def nginx_dependency_arguments(snapshot: dict[str, Any]) -> list[str]:
    root = Path(snapshot["root"]).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Terminal nginx dependencies require a regular SHA256SUMS")
    contents = manifest.read_bytes()
    if hashlib.sha256(contents).hexdigest() != snapshot["sha256"]:
        raise ValueError("Terminal nginx dependency manifest has an unexpected SHA-256")

    expected = {}
    for line in contents.decode("utf-8").splitlines():
        checksum, separator, name = line.partition("  ")
        relative = Path(name)
        if (not separator or not re.fullmatch(r"[a-f0-9]{64}", checksum)
                or not relative.parts or relative.is_absolute() or ".." in relative.parts or relative in expected):
            raise ValueError("Invalid Terminal nginx dependency manifest entry")
        expected[relative] = checksum

    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Terminal nginx dependencies must only contain regular files and directories")
        if path.is_file() and path != manifest:
            actual.add(relative)
    if actual != expected.keys():
        raise ValueError("Terminal nginx dependency files differ from the pinned manifest")
    for relative, checksum in expected.items():
        with (root / relative).open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != checksum:
                raise ValueError(f"Terminal nginx dependency SHA-256 mismatch: {relative}")

    mounts = []
    for relative, target in _MOUNTS:
        source = root / relative
        if not source.exists():
            raise ValueError(f"Terminal nginx dependency mount is missing: {relative}")
        mounts.append({"type": "bind", "source": str(source), "target": target, "read_only": True})
    verifier_env = [
        "UV_DOWNLOAD_URL=file:///mnt/official-uv",
        "UV_OFFLINE=1",
        "UV_NO_INDEX=1",
        "UV_FIND_LINKS=/mnt/official-python",
        "NO_PROXY=localhost,127.0.0.1",
        "no_proxy=localhost,127.0.0.1",
    ]
    if snapshot["verifier_proxy"]:
        verifier_env.append(f"https_proxy=${{{VERIFIER_HTTPS_PROXY}}}")
    args = ["--mounts", json.dumps(mounts)]
    for value in verifier_env:
        args.extend(["--verifier-env", value])
    return args
