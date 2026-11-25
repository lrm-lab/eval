#!/usr/bin/env bash
# Deploy a managed immutable release copy (backend + managed config + frontend dist)
# into local POSIX ext4 `${RUNTIME_DIR}/releases/<ver>` with 0750/0640 permissions,
# so deployed production services NEVER import Python or source_approval_registry.yaml
# from 0666/0777 CIFS! (v2.2.1 06-1 §4.2)
set -euo pipefail

EVAL_ROOT="${LRM_EVAL_ROOT:-/home/share-data/eval}"
RUNTIME_DIR="${LRM_RUNTIME_DIR:-${HOME}/.local/share/lrm-eval}"
RELEASE_VER="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
RELEASES_ROOT="${RUNTIME_DIR}/releases"
TARGET_DIR="${RELEASES_ROOT}/${RELEASE_VER}"

# Fail-Closed check: never deploy managed release onto CIFS/autofs/vfat, and enforce >= 1.5 GiB free space
python3 - "${RUNTIME_DIR}" <<'PY'
import shutil, sys
from pathlib import Path

rt = Path(sys.argv[1])
probe = rt
while not probe.exists() and probe.parent != probe:
    probe = probe.parent
resolved = str(probe.resolve())
best_mp, best_fs = "", "unknown"
with open("/proc/mounts", "r", encoding="utf-8") as fh:
    for line in fh:
        parts = line.split()
        if len(parts) < 3:
            continue
        mp, fs = parts[1].replace("\\040", " "), parts[2].lower()
        if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
            if len(mp) > len(best_mp) or (len(mp) == len(best_mp) and (fs != "autofs" or best_fs == "unknown")):
                best_mp, best_fs = mp, fs
if best_fs in {"cifs", "smb3", "smbfs", "vfat", "msdos", "ntfs", "fuseblk", "autofs"}:
    print(f"ERROR: Refusing to deploy managed release onto unsafe filesystem ({best_fs}): {rt}", file=sys.stderr)
    raise SystemExit(2)
free_gb = shutil.disk_usage(probe).free / (1024 ** 3)
if free_gb < 1.5:
    print(f"ERROR: Insufficient free space ({free_gb:.2f} GiB < 1.50 GiB) on {probe}", file=sys.stderr)
    raise SystemExit(3)
PY

mkdir -p "${RELEASES_ROOT}"
chmod 750 "${RELEASES_ROOT}"

rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/backend" "${TARGET_DIR}/config"
cp -r "${EVAL_ROOT}/backend/app" "${TARGET_DIR}/backend/app"
cp -r "${EVAL_ROOT}/backend/scripts" "${TARGET_DIR}/backend/scripts"
cp "${EVAL_ROOT}/backend/pyproject.toml" "${TARGET_DIR}/backend/pyproject.toml"
cp -r "${EVAL_ROOT}/config/." "${TARGET_DIR}/config/"
if [[ -d "${EVAL_ROOT}/frontend/dist" ]]; then
    mkdir -p "${TARGET_DIR}/frontend"
    cp -r "${EVAL_ROOT}/frontend/dist" "${TARGET_DIR}/frontend/dist"
fi

# Generate deterministic RELEASE_MANIFEST.json for audit traceability
python3 - "${TARGET_DIR}" "${RELEASE_VER}" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
ver = sys.argv[2]
files = {}
for p in sorted(root.rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts:
        rel = str(p.relative_to(root))
        files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
manifest_digest = hashlib.sha256(
    "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode("utf-8")
).hexdigest()
(root / "RELEASE_MANIFEST.json").write_text(
    json.dumps(
        {
            "release_version": ver,
            "deployed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "file_count": len(files),
            "manifest_sha256": manifest_digest,
            "files": files,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

# Enforce strict POSIX permissions (no world access, group read-only for release, 0600 on approval registry)
find "${TARGET_DIR}" -type d -exec chmod 750 {} +
find "${TARGET_DIR}" -type f -exec chmod 640 {} +
chmod 750 "${TARGET_DIR}/backend/scripts/"*.sh "${TARGET_DIR}/backend/scripts/"*.py 2>/dev/null || true
chmod 600 "${TARGET_DIR}/config/source_approval_registry.yaml"

# Atomic blue-green symlink switch & retain at most 2 releases (v_curr + v_prev, 750 MB budget)
ln -sfn "${TARGET_DIR}" "${RELEASES_ROOT}/current.next"
mv -Tf "${RELEASES_ROOT}/current.next" "${RELEASES_ROOT}/current"

# Prune older releases beyond 2
mapfile -t OLD_RELS < <(find "${RELEASES_ROOT}" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +3)
for old in "${OLD_RELS[@]}"; do
    rm -rf "${old}"
done

echo "Deployed managed release: ${TARGET_DIR}"
du -sh "${RELEASES_ROOT}"
