#!/usr/bin/env bash
# Freeze one Git commit and its independently verified frontend build.
set -euo pipefail

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    echo "Usage: bash backend/scripts/package_snapshot.sh [git-ref] [output.tar.gz]"
    exit 0
fi
if (( $# > 2 )); then
    echo "Usage: bash backend/scripts/package_snapshot.sh [git-ref] [output.tar.gz]" >&2
    exit 2
fi

export LC_ALL=C TZ=UTC
umask 022
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "${script_dir}" rev-parse --show-toplevel)"
commit="$(git -C "${repo_root}" rev-parse --verify --end-of-options "${1:-HEAD}^{commit}")"
export SOURCE_DATE_EPOCH="$(git -C "${repo_root}" show -s --format=%ct "${commit}")"
archive="${2:-${repo_root}/snapshots/lrm-eval-${commit:0:12}.tar.gz}"
if [[ "${archive}" != *.tar.gz ]]; then
    echo "Snapshot output must end in .tar.gz." >&2
    exit 2
fi

work="$(mktemp -d "${TMPDIR:-/dev/shm}/lrm-eval-snapshot.XXXXXXXX")"
pending=""
trap 'rm -rf -- "${work}"; if [[ -n "${pending}" ]]; then rm -f -- "${pending}"; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export npm_config_cache="${LRM_SNAPSHOT_NPM_CACHE:-${work}/npm-cache}"

git -C "${repo_root}" archive --format=tar "${commit}" > "${work}/source.tar"
for build in first second; do
    build_dir="${work}/${build}"
    mkdir "${build_dir}"
    tar -xf "${work}/source.tar" -C "${build_dir}"
    (
        cd "${build_dir}/frontend"
        npm ci --include=dev --prefer-offline --no-audit --no-fund
        npm run build
    ) >&2
    if [[ ! -f "${build_dir}/frontend/dist/index.html" ]]; then
        echo "Frontend build ${build} did not produce dist/index.html." >&2
        exit 1
    fi
done

if ! diff -r --no-dereference "${work}/first/frontend/dist" "${work}/second/frontend/dist" >&2; then
    echo "REPRO_BUILD_BYTE_IDENTICAL=False" >&2
    exit 1
fi

stage="${work}/snapshot"
mkdir "${stage}"
tar -xf "${work}/source.tar" -C "${stage}"
cp -a "${work}/first/frontend/dist" "${stage}/frontend/dist"
if [[ -n "$(find "${stage}" ! -type d ! -type f -print -quit)" ]]; then
    echo "Snapshot contains a symlink or another unsupported file type." >&2
    exit 1
fi

# Hash sorted paths and file bytes. NUL delimiters make filenames unambiguous.
tree_sum="$(
    cd "${stage}"
    find . -type f -print0 | sort -z | xargs -0 -r sha256sum --zero | sha256sum
)"
mkdir -p -- "$(dirname -- "${archive}")"
pending="$(mktemp -- "${archive}.XXXXXXXX")"
tar --sort=name --format=gnu --mtime="@${SOURCE_DATE_EPOCH}" \
    --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
    -cf - -C "${stage}" . | gzip -n -9 > "${pending}"
tar_sum="$(sha256sum < "${pending}")"
mv -fT -- "${pending}" "${archive}"
pending=""
printf '%s\n' \
    "GIT_COMMIT=${commit}" \
    'REPRO_BUILD_BYTE_IDENTICAL=True' \
    "TREE_SHA256=${tree_sum%% *}" \
    "TAR_SHA256=${tar_sum%% *}" \
    "SNAPSHOT=${archive}"
