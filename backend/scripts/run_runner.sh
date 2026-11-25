#!/usr/bin/env bash
# Launch the External Isolated Execution Runner Daemon on a 0600 Unix Domain Socket
# (separate OS process from the Web server; bounded 30 MiB rotating log in 0700 state/logs/).
set -euo pipefail

RUNTIME_DIR="${LRM_RUNTIME_DIR:-${HOME}/.local/share/lrm-eval}"
VENV_PY="${RUNTIME_DIR}/venv/bin/python"
RELEASE_CUR="${RUNTIME_DIR}/releases/current"
STATE_DIR="${LRM_STATE_DIR:-${RUNTIME_DIR}/state}"
RUNNER_WORK_DIR="${LRM_RUNNER_WORK_DIR:-${RUNTIME_DIR}/runner-work}"
SOCKET_PATH="${LRM_RUNNER_SOCKET_PATH:-${RUNNER_WORK_DIR}/runner.sock}"
TOKEN_FILE="${LRM_RUNNER_IPC_TOKEN_FILE:-${STATE_DIR}/runner_ipc.token}"
PROD_MODE="${LRM_PRODUCTION_MODE:-true}"

mkdir -p "${STATE_DIR}" "${RUNNER_WORK_DIR}"
chmod 700 "${STATE_DIR}" "${RUNNER_WORK_DIR}"

if [[ ! -f "${TOKEN_FILE}" ]]; then
    umask 077
    "${VENV_PY}" -c 'import secrets; print(secrets.token_hex(32))' > "${TOKEN_FILE}"
    chmod 600 "${TOKEN_FILE}"
fi

if [[ "${PROD_MODE}" == "true" ]]; then
    export LRM_PRODUCTION_MODE=true
    export LRM_CONFIG_DIR="${RELEASE_CUR}/config"
    export LRM_MANAGED_FRAMEWORK_DIR="${RELEASE_CUR}/backend/app/managed_framework"
    cd "${RELEASE_CUR}/backend"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "${SCRIPT_DIR}/.."
fi

exec "${VENV_PY}" -m app.runner.daemon --socket "${SOCKET_PATH}" --token-file "${TOKEN_FILE}"
