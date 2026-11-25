#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${LRM_RUNTIME_DIR:-${HOME}/.local/share/lrm-eval}"
STATE_DIR="${LRM_STATE_DIR:-${RUNTIME_DIR}/state}"
LOGS_DIR="${STATE_DIR}/logs"
VENV_PY="${RUNTIME_DIR}/venv/bin/python"
HOST="${LRM_BIND_HOST:-127.0.0.1}"
PORT="${LRM_BIND_PORT:-8680}"
PROD_MODE="${LRM_PRODUCTION_MODE:-true}"

mkdir -p "${LOGS_DIR}"
chmod 700 "${LOGS_DIR}"

if [[ "${PROD_MODE}" == "true" ]]; then
    RELEASE_CUR="${RUNTIME_DIR}/releases/current"
    if [[ ! -d "${RELEASE_CUR}/backend/app" || ! -d "${RELEASE_CUR}/config" ]]; then
        echo "Managed release not found at ${RELEASE_CUR}; deploying now..." >&2
        bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_release.sh"
    fi
    export LRM_PRODUCTION_MODE=true
    export LRM_CONFIG_DIR="${RELEASE_CUR}/config"
    export LRM_FRONTEND_DIST_DIR="${RELEASE_CUR}/frontend/dist"
    export LRM_MANAGED_FRAMEWORK_DIR="${RELEASE_CUR}/backend/app/managed_framework"
    cd "${RELEASE_CUR}/backend"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "${SCRIPT_DIR}/.."
fi

# Bounded RotatingFileHandler log config (4 x 15 MiB = 60 MiB max for backend; combined <= 150 MiB — S-05)
LOG_CFG="${LOGS_DIR}/uvicorn_backend_logging.json"
"${VENV_PY}" - "${LOGS_DIR}" "${LOG_CFG}" <<'PY'
import json, os, sys
from pathlib import Path
from app.logging_setup import build_uvicorn_log_config

cfg = build_uvicorn_log_config(Path(sys.argv[1]), "backend", emit_console=False)
out = Path(sys.argv[2])
out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
os.chmod(out, 0o600)
PY

exec "${VENV_PY}" -m uvicorn app.main:get_default_app \
    --factory \
    --host "${HOST}" \
    --port "${PORT}" \
    --proxy-headers \
    --forwarded-allow-ips="127.0.0.1" \
    --log-config "${LOG_CFG}"
