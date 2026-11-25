#!/usr/bin/env bash
# Fast zero-root-disk frontend build using /dev/shm tmpfs (v2.2.1 06-1 §4.1)
# Unpacks cached Node 22 tarball into tmpfs so ZERO symlinks touch CIFS and ZERO bytes touch /!
set -euo pipefail

EVAL_ROOT="${LRM_EVAL_ROOT:-/home/share-data/eval}"
FE_SRC="${EVAL_ROOT}/frontend"
BUILD_TMP="/dev/shm/lrm-eval-fe-build-$$"
NPM_CACHE="${EVAL_ROOT}/.cache/npm"
NODE_TAR="${EVAL_ROOT}/.cache/node/node-v22.14.0-linux-x64.tar.xz"

if [[ ! -f "${FE_SRC}/package.json" ]]; then
    echo "Frontend package.json not found at ${FE_SRC}; skipping."
    exit 0
fi

mkdir -p "${NPM_CACHE}" "${BUILD_TMP}"
chmod 700 "${BUILD_TMP}"
trap 'rm -rf "${BUILD_TMP}"' EXIT

if ! command -v node >/dev/null 2>&1; then
    if [[ ! -f "${NODE_TAR}" ]]; then
        mkdir -p "$(dirname "${NODE_TAR}")"
        curl -fsSL "https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz" -o "${BUILD_TMP}/node.tar.xz"
        cp "${BUILD_TMP}/node.tar.xz" "${NODE_TAR}"
    fi
    tar -xJf "${NODE_TAR}" -C "${BUILD_TMP}"
    export PATH="${BUILD_TMP}/node-v22.14.0-linux-x64/bin:${PATH}"
fi

mkdir -p "${BUILD_TMP}/app"
cp -r "${FE_SRC}/." "${BUILD_TMP}/app/"
rm -rf "${BUILD_TMP}/app/node_modules" "${BUILD_TMP}/app/dist"
cd "${BUILD_TMP}/app"

if [[ -f "package-lock.json" ]]; then
    NPM_CONFIG_CACHE="${NPM_CACHE}" npm ci --prefer-offline --no-audit --no-fund
else
    NPM_CONFIG_CACHE="${NPM_CACHE}" npm install --prefer-offline --no-audit --no-fund
    cp package-lock.json "${FE_SRC}/package-lock.json" || true
fi
npm run build

rm -rf "${FE_SRC}/dist"
cp -r "${BUILD_TMP}/app/dist" "${FE_SRC}/dist"
echo "Frontend dist built cleanly into ${FE_SRC}/dist ($(du -sh "${FE_SRC}/dist" | awk '{print $1}')); /dev/shm cleaned."
