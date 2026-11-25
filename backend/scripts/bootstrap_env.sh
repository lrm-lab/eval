#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${LRM_RUNTIME_DIR:-${HOME}/.local/share/lrm-eval}"
UV_BIN="${RUNTIME_DIR}/bin/uv"
VENV_DIR="${RUNTIME_DIR}/venv"
SHM_CACHE="/dev/shm/lrm-eval-uv-cache-$$"

mkdir -p "${RUNTIME_DIR}/bin" "${RUNTIME_DIR}/state/logs" "${RUNTIME_DIR}/state/backups" \
         "${RUNTIME_DIR}/uploads-staging" "${RUNTIME_DIR}/evidence-store" "${RUNTIME_DIR}/runner-work"
chmod 700 "${RUNTIME_DIR}" "${RUNTIME_DIR}/state" "${RUNTIME_DIR}/uploads-staging" \
          "${RUNTIME_DIR}/evidence-store" "${RUNTIME_DIR}/runner-work"

if [[ ! -x "${UV_BIN}" ]]; then
    curl -LsSf https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz -o "/dev/shm/uv-$$.tar.gz"
    tar -xzf "/dev/shm/uv-$$.tar.gz" -C /dev/shm
    mv /dev/shm/uv-x86_64-unknown-linux-gnu/uv "${UV_BIN}"
    chmod 755 "${UV_BIN}"
    rm -rf "/dev/shm/uv-$$.tar.gz" /dev/shm/uv-x86_64-unknown-linux-gnu
fi

trap 'rm -rf "${SHM_CACHE}"' EXIT
mkdir -p "${SHM_CACHE}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    UV_CACHE_DIR="${SHM_CACHE}" "${UV_BIN}" venv --python 3.12 "${VENV_DIR}"
fi

UV_CACHE_DIR="${SHM_CACHE}" UV_LINK_MODE=copy "${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" \
    fastapi "uvicorn[standard]" pydantic pyyaml argon2-cffi zstandard ijson httpx pytest pytest-asyncio python-multipart

echo "Bootstrap complete. Local runtime size:"
du -sh "${RUNTIME_DIR}"
