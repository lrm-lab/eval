from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from app.api.errors import AppError

UNSAFE_FSTYPES = frozenset({"cifs", "smb3", "smbfs", "vfat", "msdos", "ntfs", "fuseblk", "autofs"})
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._/@:-]{1,256}$")


def get_mount_fstype(target: Path, proc_mounts: str = "/proc/mounts") -> str:
    """Resolve topmost (last-mounted) filesystem type for target from /proc/mounts.
    Handles systemd autofs + overlaid CIFS mounts accurately by taking the later mount entry
    on ties (len(mp) >= len(best_mp)) and preferring real mounts over autofs stubs."""
    resolved = str(target.resolve())
    best_mp = ""
    best_fs = "unknown"
    try:
        with open(proc_mounts, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                fs = parts[2].lower()
                if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if len(mp) > len(best_mp):
                        best_mp = mp
                        best_fs = fs
                    elif len(mp) == len(best_mp):
                        # Later mount overlays earlier mount (e.g. cifs over autofs on /home/share-data)
                        if fs != "autofs" or best_fs == "unknown":
                            best_fs = fs
    except OSError:
        return "unknown"
    return best_fs


def probe_isolated_dir(
    path: Path,
    *,
    create: bool = True,
    proc_mounts: str = "/proc/mounts",
    write_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Fail-Closed POSIX 0700 + non-CIFS/autofs probe (S-06).
    Raises 503 STAGING_PERMISSION_UNSAFE if directory resides on 0777 CIFS/vfat/autofs
    or chmod 0700 does not take effect.
    When `create=False` (read-only health probe), performs zero filesystem mutations."""
    if create:
        # Check parent mount BEFORE write_guard/mkdir so CIFS/autofs ALWAYS fails with STAGING_PERMISSION_UNSAFE!
        parent_probe = path
        while not parent_probe.exists() and parent_probe.parent != parent_probe:
            parent_probe = parent_probe.parent
        parent_fs = get_mount_fstype(parent_probe, proc_mounts=proc_mounts)
        if parent_fs in UNSAFE_FSTYPES:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"隔离存储严禁落在不支持 POSIX 权限的文件系统 ({parent_fs}) 上",
                {"fstype": parent_fs},
            )
        if not path.exists():
            if write_guard is not None:
                write_guard()
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppError(
                    503,
                    "STAGING_PERMISSION_UNSAFE",
                    f"隔离目录无法创建: {exc}",
                    {"reason": "mkdir_failed"},
                ) from exc

    if not path.exists() or not path.is_dir():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            "隔离目录不存在或非目录",
            {"reason": "not_a_directory"},
        )

    fstype = get_mount_fstype(path, proc_mounts=proc_mounts)
    if fstype in UNSAFE_FSTYPES:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"隔离存储严禁落在不支持 POSIX 权限的文件系统 ({fstype}) 上",
            {"fstype": fstype},
        )

    try:
        st = os.stat(path)
        if create and (st.st_mode & 0o777) != 0o700:
            if write_guard is not None:
                write_guard()
            os.chmod(path, 0o700)
            st = os.stat(path)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"隔离目录权限校验失败: {exc}",
            {"fstype": fstype, "reason": "chmod_failed"},
        ) from exc

    mode_bits = st.st_mode & 0o777
    if (mode_bits & 0o077) != 0:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"隔离目录权限位不安全 ({oct(mode_bits)})，必须严格为 0700",
            {"fstype": fstype, "mode": oct(mode_bits)},
        )

    if st.st_uid != os.getuid():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            "隔离目录属主与当前服务账号不一致",
            {"fstype": fstype, "mode": oct(mode_bits)},
        )

    return {"ok": True, "fstype": fstype, "mode": oct(mode_bits), "uid": st.st_uid}


def probe_secret_file(
    path: Path,
    *,
    create_parent: bool = True,
    proc_mounts: str = "/proc/mounts",
    write_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Ensure secret file parent is 0700 isolated and secret file itself (if present, incl. symlink target)
    resides on a safe POSIX filesystem with strict 0600 permissions."""
    parent_res = probe_isolated_dir(
        path.parent, create=create_parent, proc_mounts=proc_mounts, write_guard=write_guard
    )
    if path.exists() or path.is_symlink():
        target_fs = get_mount_fstype(path, proc_mounts=proc_mounts)
        if target_fs in UNSAFE_FSTYPES:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"密钥文件严禁落在不支持 POSIX 权限的文件系统 ({target_fs}) 上",
                {"fstype": target_fs},
            )
        st = os.stat(path)
        mode_bits = st.st_mode & 0o777
        if (mode_bits & 0o177) != 0:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"密钥文件权限位不安全 ({oct(mode_bits)})，必须严格为 0600",
                {"mode": oct(mode_bits)},
            )
        if st.st_uid != os.getuid():
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                "密钥文件属主与当前服务账号不一致",
                {"mode": oct(mode_bits)},
            )
        return {"ok": True, "fstype": target_fs, "mode": oct(mode_bits)}
    return {"ok": True, "fstype": parent_res["fstype"], "mode": "pending_create"}


def probe_managed_release_path(
    path: Path,
    *,
    label: str,
    require_strict_0600: bool = False,
    proc_mounts: str = "/proc/mounts",
) -> dict[str, Any]:
    """Fail-closed production check ensuring managed code & approval config live on local POSIX FS
    with non-world-accessible permissions (0750/0640, or strict 0600 for approval registry)."""
    if not path.exists():
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"生产模式受管 {label} 不存在，请先执行 scripts/deploy_release.sh",
            {"target": label},
        )
    fstype = get_mount_fstype(path, proc_mounts=proc_mounts)
    if fstype in UNSAFE_FSTYPES:
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"生产模式禁止从非 POSIX 共享盘 ({fstype}) 加载受管 {label}，请先执行 scripts/deploy_release.sh",
            {"target": label, "fstype": fstype},
        )
    st = os.stat(path)
    mode_bits = st.st_mode & 0o777
    if require_strict_0600:
        if (mode_bits & 0o177) != 0:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"生产模式受管 {label} 权限位不安全 ({oct(mode_bits)})，必须为 0600",
                {"target": label, "mode": oct(mode_bits)},
            )
    else:
        if (mode_bits & 0o007) != 0 or (mode_bits & 0o020) != 0:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"生产模式受管 {label} 权限位不安全 ({oct(mode_bits)})，禁止同组写或其他用户访问",
                {"target": label, "mode": oct(mode_bits)},
            )
    if st.st_uid not in (os.getuid(), 0):
        raise AppError(
            503,
            "STAGING_PERMISSION_UNSAFE",
            f"生产模式受管 {label} 属主不安全",
            {"target": label},
        )
    return {"ok": True, "fstype": fstype, "mode": oct(mode_bits)}


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def check_disk_write_guard(
    target_path: Path,
    *,
    min_free_gb: float = 1.5,
    runtime_dir: Path | None = None,
    local_budget_mb: int = 750,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Fail-Closed low-disk write guard (503 LOCAL_DISK_LOW) covering ALL write entry points:
    startup, scheduled hot/cold indexing, manual reindex, taxonomy reload, saved comparisons, init_admin.
    Checks free space on the ACTUAL filesystem hosting target_path (e.g. / ext4 or private tmpfs)."""
    probe_target = target_path
    while not probe_target.exists() and probe_target.parent != probe_target:
        probe_target = probe_target.parent

    usage = disk_usage_fn(probe_target)
    free_gb = usage.free / (1024**3)
    if free_gb < min_free_gb:
        raise AppError(
            503,
            "LOCAL_DISK_LOW",
            f"状态存储所在分区剩余空间 {free_gb:.2f} GiB 低于 {min_free_gb:.2f} GiB 保护线，已触发写入保护",
            {"free_gb": round(free_gb, 3), "min_free_gb": min_free_gb},
        )

    runtime_mb: float | None = None
    if runtime_dir is not None and runtime_dir.exists():
        try:
            if os.stat(runtime_dir).st_dev == os.stat(probe_target).st_dev:
                runtime_mb = _dir_size_bytes(runtime_dir) / (1024**2)
                if runtime_mb > float(local_budget_mb):
                    raise AppError(
                        503,
                        "LOCAL_DISK_LOW",
                        f"本地运行目录占用 {runtime_mb:.1f} MB 超过 {local_budget_mb} MB 红线，已触发写入保护",
                        {"runtime_mb": round(runtime_mb, 2), "local_budget_mb": local_budget_mb},
                    )
        except OSError:
            pass

    return {"ok": True, "free_gb": round(free_gb, 3), "runtime_mb": round(runtime_mb, 2) if runtime_mb is not None else None}


def validate_safe_id(value: str, field_name: str = "id") -> str:
    if not value or "/" in value or "\\" in value or ".." in value or "\x00" in value or not SAFE_ID_RE.fullmatch(value):
        raise AppError(
            400,
            "INVALID_PATH_OR_ID",
            f"非法路径标识 {field_name}",
            {"field": field_name},
        )
    return value


def validate_safe_task_id(value: str) -> str:
    if not value or ".." in value or "\\" in value or "\x00" in value or value.startswith("/") or not SAFE_TASK_ID_RE.fullmatch(value):
        raise AppError(
            400,
            "INVALID_PATH_OR_ID",
            "非法 task_id",
            {"field": "task_id"},
        )
    return value


def resolve_within_root(root: Path, rel_path: str) -> Path:
    if not rel_path or "\x00" in rel_path or rel_path.startswith(("/", "\\")):
        raise AppError(400, "INVALID_PATH_OR_ID", "非法相对路径")
    parts = Path(rel_path).parts
    if any(p in ("..", "") for p in parts):
        raise AppError(400, "INVALID_PATH_OR_ID", "检测到路径穿越字符")
    root_resolved = root.resolve()
    target = (root_resolved / rel_path).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise AppError(400, "INVALID_PATH_OR_ID", "路径超出白名单根目录") from exc
    return target
