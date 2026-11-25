from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.security.storage_probe import UNSAFE_FSTYPES, get_mount_fstype, probe_managed_release_path

# Strict allowlist of non-sensitive environment variables passed to managed framework subprocesses (S-04)
_SAFE_SUBPROCESS_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_sanitized_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a scrubbed minimal environment for managed framework subprocesses (S-04).
    Strips ALL Web session secrets (`LRM_*`), gateway credentials (`OPENAI_API_KEY`, `ANTHROPIC_*`,
    `TIANXI_SYNTH_TEACHER_API_KEY`, `VLMEVALKIT_JUDGE_*`), and unrelated host env vars.
    Only explicitly approved `extra_env` entries (e.g. a single requested teacher key) are injected."""
    clean: dict[str, str] = {}
    for k in _SAFE_SUBPROCESS_ENV_KEYS:
        val = os.environ.get(k)
        if val is not None:
            clean[k] = val
    clean.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        for ek, ev in extra_env.items():
            clean[str(ek)] = str(ev)
    return clean


# Required manifest-covered file sets per managed entry script (S-04: refuse if target or dependency omitted!)
REQUIRED_SCRIPT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "report.py": ("report.py", "gateway_capture.py", "training_capture.py"),
    "direct.py": ("direct.py",),
    "run.py": (
        "run.py",
        "publish_v2.py",
        "direct.py",
        "vlmevalkit.py",
        "batch_guard.py",
        "report.py",
        "gateway_capture.py",
        "training_capture.py",
        "harbor.toml",
    ),
    "synthesize.py": (
        "synthesize.py",
        "synthesis_methods.py",
        "synthesis_audit.py",
        "synthesis_reasoning.py",
        "synthesis_reasoning_audit.py",
        "synthesis_tool_cli.py",
        "synthesis_verify.py",
        "harbor.toml",
    ),
    "harbor.toml": ("harbor.toml",),
}


def verify_managed_framework_bundle(
    settings: Settings,
    *,
    require_posix_strict: bool | None = None,
    required_files: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Fail-Closed verification of pinned managed framework executables (`report.py`, `synthesize.py`, etc.).
    1. Ensures `managed_framework_dir` & `MANIFEST.json` exist and EVERY pinned file matches its exact SHA-256.
    2. In production mode (or when `require_posix_strict=True`), ALSO enforces `probe_managed_release_path`
       (non-CIFS/vfat/autofs local POSIX filesystem, owned by service user/root, no group-write or world bits!).
    Raises `503 STAGING_PERMISSION_UNSAFE` if integrity or permissions cannot be verified (S-04)."""
    mf_dir = settings.managed_framework_dir
    strict = settings.production_mode if require_posix_strict is None else require_posix_strict

    if not mf_dir.is_dir():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            "受管框架脚本目录缺失，拒绝从共享盘执行不可信脚本",
            {"target": "managed_framework"},
        )

    if strict:
        probe_managed_release_path(mf_dir, label="managed_framework_dir", require_strict_0600=False)

    manifest_path = mf_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            "受管框架脚本缺少完整性清单 MANIFEST.json，拒绝执行",
            {"target": "managed_framework/MANIFEST.json"},
        )
    if strict:
        probe_managed_release_path(manifest_path, label="managed_framework/MANIFEST.json", require_strict_0600=False)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"受管框架完整性清单损坏，拒绝执行: {exc}",
            {"target": "managed_framework/MANIFEST.json"},
        ) from exc

    files_spec = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files_spec, dict) or not files_spec:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            "受管框架完整性清单格式无效，拒绝执行",
            {"target": "managed_framework/MANIFEST.json"},
        )

    # S-04: Ensure every required entry script & runtime dependency is explicitly covered by MANIFEST.json!
    must_cover: set[str] = set(required_files or ())
    if required_files is None:
        for dep_set in REQUIRED_SCRIPT_DEPENDENCIES.values():
            must_cover.update(dep_set)
    missing_from_manifest = sorted(f for f in must_cover if f not in files_spec)
    if missing_from_manifest:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"受管框架完整性清单缺失必需目标或依赖: {', '.join(missing_from_manifest)}，拒绝执行",
            {"missing_from_manifest": missing_from_manifest},
        )

    for fname, spec in files_spec.items():
        if not isinstance(fname, str) or "/" in fname or "\\" in fname or ".." in fname:
            raise AppError(503, "STAGING_PERMISSION_UNSAFE", f"受管清单含非法文件名: {fname}")
        fpath = mf_dir / fname
        if not fpath.is_file() or fpath.is_symlink():
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"受管框架依赖缺失或为符号链接: {fname}",
                {"target": f"managed_framework/{fname}"},
            )
        if strict:
            probe_managed_release_path(fpath, label=f"managed_framework/{fname}", require_strict_0600=False)
        expected_sha = spec.get("sha256") if isinstance(spec, dict) else None
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise AppError(503, "STAGING_PERMISSION_UNSAFE", f"受管清单缺少合法 SHA-256: {fname}")
        actual_sha = _sha256_file(fpath)
        if actual_sha != expected_sha:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"受管框架脚本 {fname} 完整性校验失败（SHA-256 不匹配），已拒绝执行",
                {"target": f"managed_framework/{fname}"},
            )

    return {
        "ok": True,
        "version": str(manifest.get("version") or "unknown"),
        "file_count": len(files_spec),
        "fstype": get_mount_fstype(mf_dir),
        "strict_posix_verified": strict,
    }


def resolve_verified_managed_script(settings: Settings, script_name: str) -> Path:
    """Verify full managed framework bundle AND enforce that `script_name` plus all of its
    runtime dependencies are explicitly listed and verified in MANIFEST.json (S-04).
    NEVER falls back to 0666 CIFS `/home/share-data/tianxi_benches/framework/`."""
    if "/" in script_name or "\\" in script_name or ".." in script_name:
        raise AppError(400, "INVALID_PATH_OR_ID", f"非法受管脚本名: {script_name}")
    req_deps = REQUIRED_SCRIPT_DEPENDENCIES.get(script_name, (script_name,))
    verify_managed_framework_bundle(settings, required_files=req_deps)
    target = settings.managed_framework_dir / script_name
    if not target.is_file() or target.is_symlink():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"受管框架脚本不存在: {script_name}",
            {"target": script_name},
        )
    if settings.production_mode:
        fs = get_mount_fstype(target)
        if fs in UNSAFE_FSTYPES:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"生产模式禁止从不安全文件系统 ({fs}) 执行 {script_name}",
                {"target": script_name, "fstype": fs},
            )
    return target
