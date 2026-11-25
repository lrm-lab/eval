#!/usr/bin/env bash
# Production-grade, idempotent HTTPS demo launcher for 197 (LRM-02 / 10.110.158.197).
#
# Responsibilities:
#   1. Validate RUNTIME_DIR resides on local POSIX ext4 (never CIFS/autofs/vfat),
#      with >= 1.5 GiB free space and <= 750 MB runtime budget.
#   2. Deploy an immutable managed release (0750/0640, 0600 source_approval_registry.yaml,
#      local POSIX managed_framework/) via deploy_release.sh into ${RUNTIME_DIR}/releases/current.
#   3. Generate (if absent) a strict 0600 RSA-2048 private key and self-signed X.509 certificate
#      with SAN (IP:10.110.158.197,IP:127.0.0.1,DNS:localhost,DNS:LRM-02) under
#      ${RUNTIME_DIR}/state/tls/{server.key,server.crt}.
#   4. Zero hardcoded default admin passwords. If LRM_ADMIN_PASSWORD is supplied, ensure the
#      requested admin account exists via init_admin.py (ignoring already-exists cleanly).
#   5. Cleanly stop previous managed backend/proxy PIDs recorded in
#      ${RUNTIME_DIR}/state/{backend.pid,proxy.pid}.
#   6. Start the managed production FastAPI backend on 127.0.0.1:${BACKEND_PORT:-8680}
#      (LRM_PRODUCTION_MODE=true, COOKIE_SECURE=true) and wait for /api/v1/healthz.
#   7. Start the user-space HTTPS reverse proxy on 0.0.0.0:${HTTPS_PORT:-8443}, wait for
#      https://127.0.0.1:${HTTPS_PORT}/api/v1/healthz, and print access summary.
#   8. Support --stop and --status flags.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EVAL_ROOT="${LRM_EVAL_ROOT:-/home/share-data/eval}"
RUNTIME_DIR="${LRM_RUNTIME_DIR:-${HOME}/.local/share/lrm-eval}"
DEMO_HOST_IP="${LRM_DEMO_HOST_IP:-10.110.158.197}"
DEMO_HOSTNAME="${LRM_DEMO_HOSTNAME:-LRM-02}"

BACKEND_HOST="127.0.0.1"
BACKEND_PORT="${BACKEND_PORT:-${LRM_BIND_PORT:-8680}}"
HTTPS_HOST="${LRM_PROXY_HOST:-0.0.0.0}"
HTTPS_PORT="${HTTPS_PORT:-${LRM_PROXY_PORT:-8443}}"

MIN_FREE_GIB="${LRM_MIN_FREE_GIB:-1.5}"
LOCAL_BUDGET_MB="${LRM_LOCAL_BUDGET_MB:-750}"
ALLOW_OTHER_POSIX_FS="${LRM_ALLOW_OTHER_POSIX_FS:-false}"
TLS_DAYS="${LRM_TLS_DAYS:-365}"
HEALTH_TIMEOUT_SECONDS="${LRM_HEALTH_TIMEOUT_SECONDS:-45}"

ADMIN_USERNAME="${LRM_ADMIN_USERNAME:-admin}"
ADMIN_DISPLAY_NAME="${LRM_ADMIN_DISPLAY_NAME:-Administrator}"
ADMIN_PROJECTS="${LRM_ADMIN_PROJECTS:-default}"
ADMIN_FORCE_UPDATE="${LRM_ADMIN_FORCE_UPDATE:-false}"

STATE_DIR="${RUNTIME_DIR}/state"
LOGS_DIR="${STATE_DIR}/logs"
TLS_DIR="${STATE_DIR}/tls"
TLS_KEY="${TLS_DIR}/server.key"
TLS_CERT="${TLS_DIR}/server.crt"
DB_PATH="${LRM_DB_PATH:-${STATE_DIR}/eval_platform.db}"
BACKEND_PID_FILE="${STATE_DIR}/backend.pid"
PROXY_PID_FILE="${STATE_DIR}/proxy.pid"
BACKEND_LOG="${LOGS_DIR}/backend.log"
PROXY_LOG="${LOGS_DIR}/proxy.log"
RELEASE_CUR="${RUNTIME_DIR}/releases/current"
VENV_PY="${RUNTIME_DIR}/venv/bin/python"

ALLOWED_ORIGINS="${LRM_ALLOWED_ORIGINS:-https://${DEMO_HOST_IP}:${HTTPS_PORT},https://127.0.0.1:${HTTPS_PORT},https://localhost:${HTTPS_PORT}}"

log_info() {
    printf '[info] %s\n' "$*"
}

log_warn() {
    printf '[warn] %s\n' "$*" >&2
}

log_err() {
    printf '[error] %s\n' "$*" >&2
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [--start | --stop | --status | --help]

Start, stop, or inspect the managed HTTPS LRM Eval demo deployment on ${DEMO_HOSTNAME} (${DEMO_HOST_IP}).

Options:
  --start     Deploy managed release, ensure TLS cert, and start backend + HTTPS proxy (default)
  --stop      Cleanly stop managed HTTPS proxy and FastAPI backend processes
  --status    Show running status of backend/proxy, TLS fingerprint, and local disk usage
  -h, --help  Show this help text

Environment variables:
  LRM_RUNTIME_DIR        Local POSIX ext4 runtime root (default: \${HOME}/.local/share/lrm-eval)
  LRM_EVAL_ROOT          Source repository root (default: /home/share-data/eval)
  BACKEND_PORT           Loopback FastAPI port (default: 8680)
  HTTPS_PORT             External HTTPS proxy port (default: 8443)
  LRM_DEMO_HOST_IP       Public/LAN host IP for SAN & access URL (default: 10.110.158.197)
  LRM_DEMO_HOSTNAME      Hostname for SAN & cert CN (default: LRM-02)
  LRM_ADMIN_PASSWORD     Optional: if set (never hardcoded), ensures admin user exists via init_admin.py
  LRM_ADMIN_USERNAME     Admin username when LRM_ADMIN_PASSWORD is set (default: admin)
  LRM_ADMIN_PROJECTS     Comma-separated authorized projects for admin (default: default)
  LRM_ADMIN_FORCE_UPDATE Set to 'true' to overwrite existing admin user when LRM_ADMIN_PASSWORD is set
  LRM_ALLOWED_ORIGINS    Override CORS/Origin allowlist
EOF
}

require_cmd() {
    local cmd="$1"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        log_err "Required command not found in PATH: ${cmd}"
        exit 1
    fi
}

validate_port() {
    local label="$1"
    local port="$2"
    if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
        log_err "Invalid ${label} port: '${port}' (must be 1..65535)"
        exit 2
    fi
}

validate_runtime_dir() {
    local stage="${1:-preflight}"
    python3 - "${RUNTIME_DIR}" "${MIN_FREE_GIB}" "${LOCAL_BUDGET_MB}" "${ALLOW_OTHER_POSIX_FS}" "${stage}" <<'PY'
import os
import shutil
import sys
from pathlib import Path

rt = Path(sys.argv[1])
min_free_gib = float(sys.argv[2])
budget_mb = float(sys.argv[3])
allow_other_posix = sys.argv[4].strip().lower() in {"1", "true", "yes", "on"}
stage = sys.argv[5]

UNSAFE_FSTYPES = {
    "cifs",
    "smb3",
    "smbfs",
    "vfat",
    "msdos",
    "ntfs",
    "fuseblk",
    "autofs",
    "nfs",
    "nfs4",
    "sshfs",
    "fuse.sshfs",
    "unknown",
}
OTHER_LOCAL_POSIX = {"ext3", "ext2", "xfs", "btrfs", "zfs", "f2fs", "tmpfs"}

probe = rt
while not probe.exists() and probe.parent != probe:
    probe = probe.parent
resolved = str(probe.resolve())

best_mp = ""
best_fs = "unknown"
with open("/proc/mounts", "r", encoding="utf-8") as fh:
    for line in fh:
        parts = line.split()
        if len(parts) < 3:
            continue
        mp = parts[1].replace("\\040", " ")
        fs = parts[2].lower()
        if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
            if len(mp) > len(best_mp):
                best_mp, best_fs = mp, fs
            elif len(mp) == len(best_mp):
                if fs != "autofs" or best_fs == "unknown":
                    best_fs = fs

if best_fs in UNSAFE_FSTYPES:
    print(
        f"[error] Refusing to use unsafe/non-POSIX filesystem ({best_fs}) for RUNTIME_DIR: {rt} (mount={best_mp})",
        file=sys.stderr,
    )
    raise SystemExit(2)

if best_fs != "ext4":
    if not (allow_other_posix and best_fs in OTHER_LOCAL_POSIX):
        print(
            f"[error] RUNTIME_DIR must reside on local POSIX ext4 (found '{best_fs}' at mount '{best_mp}'): {rt}",
            file=sys.stderr,
        )
        raise SystemExit(2)

free_gib = shutil.disk_usage(probe).free / (1024 ** 3)
if free_gib < min_free_gib:
    print(
        f"[error] Insufficient free space on {probe}: {free_gib:.2f} GiB < {min_free_gib:.2f} GiB minimum",
        file=sys.stderr,
    )
    raise SystemExit(3)

runtime_bytes = 0
if rt.exists():
    for root, _, files in os.walk(rt):
        for fn in files:
            try:
                p = os.path.join(root, fn)
                if not os.path.islink(p):
                    runtime_bytes += os.path.getsize(p)
            except OSError:
                pass

runtime_mb = runtime_bytes / (1024 ** 2)
if runtime_mb > budget_mb:
    print(
        f"[error] RUNTIME_DIR exceeds local disk budget ({runtime_mb:.1f} MB > {budget_mb:.1f} MB): {rt}",
        file=sys.stderr,
    )
    raise SystemExit(4)

print(
    f"[ok] Runtime storage ({stage}): fs={best_fs}, mount={best_mp}, "
    f"free={free_gib:.2f} GiB (>= {min_free_gib:.2f} GiB), "
    f"used={runtime_mb:.1f} MB (<= {budget_mb:.0f} MB)"
)
PY
}

ensure_runtime_dirs() {
    mkdir -p \
        "${RUNTIME_DIR}" \
        "${STATE_DIR}" \
        "${LOGS_DIR}" \
        "${TLS_DIR}" \
        "${RUNTIME_DIR}/uploads-staging" \
        "${RUNTIME_DIR}/evidence-store" \
        "${RUNTIME_DIR}/runner-work"

    chmod 0700 \
        "${RUNTIME_DIR}" \
        "${STATE_DIR}" \
        "${LOGS_DIR}" \
        "${TLS_DIR}" \
        "${RUNTIME_DIR}/uploads-staging" \
        "${RUNTIME_DIR}/evidence-store" \
        "${RUNTIME_DIR}/runner-work"
}

resolve_deploy_script() {
    if [[ -f "${EVAL_ROOT}/backend/scripts/deploy_release.sh" ]]; then
        printf '%s\n' "${EVAL_ROOT}/backend/scripts/deploy_release.sh"
    elif [[ -f "${SCRIPT_DIR}/deploy_release.sh" ]]; then
        printf '%s\n' "${SCRIPT_DIR}/deploy_release.sh"
    else
        log_err "Cannot locate deploy_release.sh under ${EVAL_ROOT}/backend/scripts or ${SCRIPT_DIR}"
        exit 1
    fi
}

deploy_managed_release() {
    local deploy_script
    deploy_script="$(resolve_deploy_script)"
    log_info "Deploying immutable managed release via ${deploy_script}..."
    LRM_EVAL_ROOT="${EVAL_ROOT}" LRM_RUNTIME_DIR="${RUNTIME_DIR}" bash "${deploy_script}"

    # Verify deployed artifacts and strict POSIX permissions
    if [[ ! -d "${RELEASE_CUR}/backend/app" || ! -f "${RELEASE_CUR}/backend/scripts/run_server.sh" || ! -f "${RELEASE_CUR}/backend/scripts/reverse_proxy.py" ]]; then
        log_err "Managed release at ${RELEASE_CUR} is incomplete."
        exit 1
    fi

    local approval_file="${RELEASE_CUR}/config/source_approval_registry.yaml"
    if [[ ! -f "${approval_file}" || -L "${approval_file}" ]]; then
        log_err "Missing managed approval registry: ${approval_file}"
        exit 1
    fi
    local approval_mode
    approval_mode="$(stat -c '%a' "${approval_file}")"
    if [[ "${approval_mode}" != "600" ]]; then
        log_err "Expected 0600 on ${approval_file}, got ${approval_mode}"
        exit 1
    fi

    local mf_dir="${RELEASE_CUR}/backend/app/managed_framework"
    if [[ ! -d "${mf_dir}" || ! -f "${mf_dir}/MANIFEST.json" ]]; then
        log_err "Managed framework bundle missing under ${mf_dir}"
        exit 1
    fi

    if [[ ! -x "${VENV_PY}" ]]; then
        log_err "Python venv not found at ${VENV_PY}. Run ${EVAL_ROOT}/backend/scripts/bootstrap_env.sh first."
        exit 1
    fi
}

ensure_tls_certificate() {
    mkdir -p "${TLS_DIR}"
    chmod 0700 "${TLS_DIR}"

    if [[ -L "${TLS_KEY}" || -L "${TLS_CERT}" ]]; then
        log_err "Refusing to use symlinked TLS key or certificate under ${TLS_DIR}"
        exit 1
    fi

    if [[ ! -s "${TLS_KEY}" || ! -s "${TLS_CERT}" ]]; then
        log_info "Generating self-signed RSA-2048 TLS certificate (SAN: IP:${DEMO_HOST_IP},IP:127.0.0.1,DNS:localhost,DNS:${DEMO_HOSTNAME})..."
        local tmp_key="${TLS_DIR}/.server.key.tmp.$$"
        local tmp_cert="${TLS_DIR}/.server.crt.tmp.$$"
        local san="IP:${DEMO_HOST_IP},IP:127.0.0.1,DNS:localhost,DNS:${DEMO_HOSTNAME}"

        if ! (
            umask 077
            openssl req -x509 -newkey rsa:2048 -nodes -sha256 \
                -days "${TLS_DAYS}" \
                -keyout "${tmp_key}" \
                -out "${tmp_cert}" \
                -subj "/CN=${DEMO_HOSTNAME}/O=LRM Eval Platform" \
                -addext "subjectAltName=${san}" \
                -addext "basicConstraints=critical,CA:FALSE" \
                -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
                -addext "extendedKeyUsage=serverAuth" \
                >/dev/null 2>&1
        ); then
            rm -f "${tmp_key}" "${tmp_cert}"
            log_err "openssl failed to generate TLS key/certificate."
            exit 1
        fi

        chmod 0600 "${tmp_key}"
        chmod 0644 "${tmp_cert}"
        mv -f "${tmp_key}" "${TLS_KEY}"
        mv -f "${tmp_cert}" "${TLS_CERT}"
    else
        log_info "Reusing existing TLS certificate at ${TLS_CERT}"
        chmod 0600 "${TLS_KEY}"
        chmod 0644 "${TLS_CERT}"
    fi

    local tls_dir_mode key_mode cert_mode
    tls_dir_mode="$(stat -c '%a' "${TLS_DIR}")"
    key_mode="$(stat -c '%a' "${TLS_KEY}")"
    cert_mode="$(stat -c '%a' "${TLS_CERT}")"
    if [[ "${tls_dir_mode}" != "700" || "${key_mode}" != "600" || "${cert_mode}" != "644" ]]; then
        log_err "TLS permission check failed (dir=${tls_dir_mode}, key=${key_mode}, cert=${cert_mode}); expected 700/600/644."
        exit 1
    fi
}

maybe_init_admin() {
    # Never hardcode a default password. Only bootstrap admin when caller provides LRM_ADMIN_PASSWORD.
    if [[ -z "${LRM_ADMIN_PASSWORD:-}" ]]; then
        log_info "LRM_ADMIN_PASSWORD not set; skipping admin bootstrap (use init_admin.py if an admin account is needed)."
        return 0
    fi

    local force_update="false"
    if [[ "${ADMIN_FORCE_UPDATE,,}" == "true" || "${ADMIN_FORCE_UPDATE}" == "1" ]]; then
        force_update="true"
    fi

    if [[ "${force_update}" != "true" && -f "${DB_PATH}" ]]; then
        local exists_status=0
        "${VENV_PY}" - "${DB_PATH}" "${ADMIN_USERNAME}" <<'PY' || exists_status=$?
import sqlite3
import sys

db_path, username = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    tbl = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if tbl is None:
        raise SystemExit(1)
    row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    raise SystemExit(0 if row is not None else 1)
finally:
    conn.close()
PY
        if [[ "${exists_status}" -eq 0 ]]; then
            log_info "Admin user '${ADMIN_USERNAME}' already exists; skipping init_admin.py (set LRM_ADMIN_FORCE_UPDATE=true to update)."
            unset LRM_ADMIN_PASSWORD
            return 0
        fi
    fi

    log_info "Ensuring admin user '${ADMIN_USERNAME}' exists via managed init_admin.py..."
    local rc=0
    local init_out=""
    init_out="$(
        LRM_RUNTIME_DIR="${RUNTIME_DIR}" \
        LRM_EVAL_ROOT="${EVAL_ROOT}" \
        LRM_CONFIG_DIR="${RELEASE_CUR}/config" \
        LRM_DB_PATH="${DB_PATH}" \
        LRM_PRODUCTION_MODE="true" \
        "${VENV_PY}" "${RELEASE_CUR}/backend/scripts/init_admin.py" \
            --username "${ADMIN_USERNAME}" \
            --display-name "${ADMIN_DISPLAY_NAME}" \
            --role "admin" \
            --projects "${ADMIN_PROJECTS}" \
            --scopes "all" \
            --password-env "LRM_ADMIN_PASSWORD" 2>&1
    )" || rc=$?

    unset LRM_ADMIN_PASSWORD

    if [[ "${rc}" -ne 0 ]]; then
        if grep -qiE "already exists|unique constraint failed" <<<"${init_out}"; then
            log_info "Admin user '${ADMIN_USERNAME}' already exists; continuing."
            return 0
        fi
        log_err "init_admin.py failed (exit ${rc}): ${init_out}"
        exit "${rc}"
    fi

    log_info "${init_out}"
}

read_pid_file() {
    local pid_file="$1"
    if [[ ! -f "${pid_file}" ]]; then
        return 1
    fi
    local pid=""
    IFS= read -r pid < "${pid_file}" || true
    pid="${pid//[[:space:]]/}"
    if [[ ! "${pid}" =~ ^[0-9]+$ ]] || (( pid <= 1 )); then
        return 1
    fi
    printf '%s\n' "${pid}"
}

pid_matches_role() {
    local pid="$1"
    local role="$2"
    if ! kill -0 "${pid}" 2>/dev/null; then
        return 1
    fi
    if [[ -r "/proc/${pid}/cmdline" ]]; then
        local cmdline
        cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
        case "${role}" in
            backend)
                [[ "${cmdline}" == *"uvicorn"* || "${cmdline}" == *"app.main:get_default_app"* || "${cmdline}" == *"run_server.sh"* ]]
                return $?
                ;;
            proxy)
                [[ "${cmdline}" == *"reverse_proxy.py"* ]]
                return $?
                ;;
        esac
    fi
    return 0
}

stop_managed_process() {
    local role="$1"
    local pid_file="$2"

    if [[ ! -f "${pid_file}" ]]; then
        return 0
    fi

    local pid=""
    if ! pid="$(read_pid_file "${pid_file}")"; then
        log_warn "Removing invalid PID file for ${role}: ${pid_file}"
        rm -f "${pid_file}"
        return 0
    fi

    if ! kill -0 "${pid}" 2>/dev/null; then
        log_info "Removing stale ${role} PID file (pid=${pid} not running)."
        rm -f "${pid_file}"
        return 0
    fi

    if ! pid_matches_role "${pid}" "${role}"; then
        log_warn "PID ${pid} in ${pid_file} does not match managed ${role}; removing stale PID file without signaling."
        rm -f "${pid_file}"
        return 0
    fi

    log_info "Stopping managed ${role} (pid=${pid})..."
    kill -TERM "${pid}" 2>/dev/null || true

    local waited=0
    while (( waited < 50 )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            rm -f "${pid_file}"
            log_info "Stopped managed ${role} (pid=${pid})."
            return 0
        fi
        sleep 0.2
        waited=$((waited + 1))
    done

    log_warn "Managed ${role} (pid=${pid}) did not exit after SIGTERM; sending SIGKILL..."
    kill -KILL "${pid}" 2>/dev/null || true

    waited=0
    while (( waited < 10 )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 0.2
        waited=$((waited + 1))
    done

    rm -f "${pid_file}"
    log_info "Terminated managed ${role} (pid=${pid})."
}

stop_all() {
    # Stop external HTTPS proxy first so no new traffic enters while backend shuts down.
    stop_managed_process "proxy" "${PROXY_PID_FILE}"
    stop_managed_process "backend" "${BACKEND_PID_FILE}"
}

assert_port_free() {
    local host="$1"
    local port="$2"
    local label="$3"
    python3 - "${host}" "${port}" "${label}" <<'PY'
import socket
import sys

host, port, label = sys.argv[1], int(sys.argv[2]), sys.argv[3]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
except OSError as exc:
    print(f"[error] Cannot bind {label} on {host}:{port}: {exc}", file=sys.stderr)
    raise SystemExit(1)
finally:
    s.close()
PY
}

wait_for_health() {
    local role="$1"
    local pid="$2"
    local url="$3"
    local log_file="$4"
    local curl_extra="$5"
    local max_iters=$(( HEALTH_TIMEOUT_SECONDS * 2 ))
    local i=0

    while (( i < max_iters )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            log_err "Managed ${role} (pid=${pid}) exited prematurely. Recent log (${log_file}):"
            tail -n 40 "${log_file}" >&2 || true
            return 1
        fi
        # shellcheck disable=SC2086
        if curl --noproxy '*' ${curl_extra} -fsS --max-time 2 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
        i=$((i + 1))
    done

    log_err "Timed out after ${HEALTH_TIMEOUT_SECONDS}s waiting for ${role} health at ${url}. Recent log (${log_file}):"
    tail -n 40 "${log_file}" >&2 || true
    return 1
}

cert_fingerprint() {
    if [[ -f "${TLS_CERT}" ]]; then
        openssl x509 -in "${TLS_CERT}" -noout -fingerprint -sha256 2>/dev/null || printf 'unavailable'
    else
        printf 'not generated'
    fi
}

start_all() {
    require_cmd python3
    require_cmd openssl
    require_cmd curl
    validate_port "backend" "${BACKEND_PORT}"
    validate_port "HTTPS proxy" "${HTTPS_PORT}"

    validate_runtime_dir "preflight"
    ensure_runtime_dirs
    deploy_managed_release
    ensure_tls_certificate
    maybe_init_admin
    validate_runtime_dir "post-deploy"

    stop_all
    assert_port_free "${BACKEND_HOST}" "${BACKEND_PORT}" "backend"
    assert_port_free "${HTTPS_HOST}" "${HTTPS_PORT}" "HTTPS proxy"

    # Logs are strictly rotated via RotatingFileHandler inside ${LOGS_DIR} (<= 150 MiB hard cap, S-05).
    # Redirect raw daemon stdout/stderr to /dev/null so no unbounded append log can ever grow!
    log_info "Starting managed production FastAPI backend on ${BACKEND_HOST}:${BACKEND_PORT}..."
    nohup env \
        -u LRM_ADMIN_PASSWORD \
        LRM_EVAL_ROOT="${EVAL_ROOT}" \
        LRM_RUNTIME_DIR="${RUNTIME_DIR}" \
        LRM_DB_PATH="${DB_PATH}" \
        LRM_BIND_HOST="${BACKEND_HOST}" \
        LRM_BIND_PORT="${BACKEND_PORT}" \
        LRM_PRODUCTION_MODE="true" \
        COOKIE_SECURE="true" \
        LRM_ALLOWED_ORIGINS="${ALLOWED_ORIGINS}" \
        bash "${RELEASE_CUR}/backend/scripts/run_server.sh" \
        </dev/null >/dev/null 2>&1 &
    local backend_pid=$!
    printf '%s\n' "${backend_pid}" > "${BACKEND_PID_FILE}"
    chmod 0600 "${BACKEND_PID_FILE}"

    if ! wait_for_health "backend" "${backend_pid}" "http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1/healthz" "${BACKEND_LOG}" ""; then
        stop_all
        exit 1
    fi
    log_info "Backend healthy (pid=${backend_pid})."

    log_info "Starting user-space HTTPS reverse proxy on ${HTTPS_HOST}:${HTTPS_PORT} -> http://${BACKEND_HOST}:${BACKEND_PORT}..."
    nohup env \
        -u LRM_ADMIN_PASSWORD \
        LRM_RUNTIME_DIR="${RUNTIME_DIR}" \
        "${VENV_PY}" "${RELEASE_CUR}/backend/scripts/reverse_proxy.py" \
        --listen-host "${HTTPS_HOST}" \
        --listen-port "${HTTPS_PORT}" \
        --upstream "http://${BACKEND_HOST}:${BACKEND_PORT}" \
        --ssl-certfile "${TLS_CERT}" \
        --ssl-keyfile "${TLS_KEY}" \
        --logs-dir "${LOGS_DIR}" \
        </dev/null >/dev/null 2>&1 &
    local proxy_pid=$!
    printf '%s\n' "${proxy_pid}" > "${PROXY_PID_FILE}"
    chmod 0600 "${PROXY_PID_FILE}"

    if ! wait_for_health "proxy" "${proxy_pid}" "https://127.0.0.1:${HTTPS_PORT}/api/v1/healthz" "${PROXY_LOG}" "-k"; then
        stop_all
        exit 1
    fi
    log_info "HTTPS reverse proxy healthy (pid=${proxy_pid})."

    local resolved_release fp disk_usage
    resolved_release="$(readlink -f "${RELEASE_CUR}" 2>/dev/null || printf '%s' "${RELEASE_CUR}")"
    fp="$(cert_fingerprint)"
    disk_usage="$(du -sh "${RUNTIME_DIR}" 2>/dev/null | awk '{print $1}')"

    cat <<EOF

========================================================================
LRM Eval Managed HTTPS Demo Ready (${DEMO_HOSTNAME})
========================================================================
Access URL      : https://${DEMO_HOST_IP}:${HTTPS_PORT}
Loopback URL    : https://127.0.0.1:${HTTPS_PORT}
Allowed Origins : ${ALLOWED_ORIGINS}
Managed Release : ${resolved_release}
TLS Certificate : ${TLS_CERT}
TLS Fingerprint : ${fp}
Backend PID     : ${backend_pid} (127.0.0.1:${BACKEND_PORT}, log: ${BACKEND_LOG})
HTTPS Proxy PID : ${proxy_pid} (${HTTPS_HOST}:${HTTPS_PORT}, log: ${PROXY_LOG})
Local Disk Usage: ${disk_usage} (${RUNTIME_DIR}, budget <= ${LOCAL_BUDGET_MB} MB)
========================================================================
EOF
}

show_status() {
    local backend_state="stopped"
    local proxy_state="stopped"
    local backend_pid="-"
    local proxy_pid="-"
    local pid=""

    if pid="$(read_pid_file "${BACKEND_PID_FILE}" 2>/dev/null)" && pid_matches_role "${pid}" "backend"; then
        backend_pid="${pid}"
        if command -v curl >/dev/null 2>&1 && curl --noproxy '*' -fsS --max-time 2 "http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1/healthz" >/dev/null 2>&1; then
            backend_state="running (healthy)"
        else
            backend_state="running (healthz unreachable)"
        fi
    fi

    if pid="$(read_pid_file "${PROXY_PID_FILE}" 2>/dev/null)" && pid_matches_role "${pid}" "proxy"; then
        proxy_pid="${pid}"
        if command -v curl >/dev/null 2>&1 && curl --noproxy '*' -kfsS --max-time 2 "https://127.0.0.1:${HTTPS_PORT}/api/v1/healthz" >/dev/null 2>&1; then
            proxy_state="running (healthy)"
        else
            proxy_state="running (healthz unreachable)"
        fi
    fi

    local overall="stopped"
    if [[ "${backend_state}" == running* && "${proxy_state}" == running* ]]; then
        overall="running"
    elif [[ "${backend_state}" == running* || "${proxy_state}" == running* ]]; then
        overall="degraded"
    fi

    local resolved_release="not deployed"
    if [[ -e "${RELEASE_CUR}" ]]; then
        resolved_release="$(readlink -f "${RELEASE_CUR}" 2>/dev/null || printf '%s' "${RELEASE_CUR}")"
    fi

    local disk_usage="not created"
    if [[ -d "${RUNTIME_DIR}" ]]; then
        disk_usage="$(du -sh "${RUNTIME_DIR}" 2>/dev/null | awk '{print $1}')"
    fi

    cat <<EOF
LRM Eval Managed HTTPS Demo Status (${DEMO_HOSTNAME})
  Overall         : ${overall}
  Access URL      : https://${DEMO_HOST_IP}:${HTTPS_PORT}
  Backend         : ${backend_state} (pid=${backend_pid}, bind=${BACKEND_HOST}:${BACKEND_PORT})
  HTTPS Proxy     : ${proxy_state} (pid=${proxy_pid}, bind=${HTTPS_HOST}:${HTTPS_PORT})
  Managed Release : ${resolved_release}
  TLS Certificate : ${TLS_CERT}
  TLS Fingerprint : $(cert_fingerprint)
  Local Disk Usage: ${disk_usage} (${RUNTIME_DIR}, budget <= ${LOCAL_BUDGET_MB} MB)
EOF
}

main() {
    local action="start"
    if (( $# > 1 )); then
        log_err "Too many arguments: $*"
        usage >&2
        exit 2
    fi
    if (( $# == 1 )); then
        case "$1" in
            --start)
                action="start"
                ;;
            --stop)
                action="stop"
                ;;
            --status)
                action="status"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                log_err "Unknown option: $1"
                usage >&2
                exit 2
                ;;
        esac
    fi

    case "${action}" in
        start)
            start_all
            ;;
        stop)
            stop_all
            log_info "All managed LRM Eval demo processes are stopped."
            ;;
        status)
            show_status
            ;;
    esac
}

main "$@"
