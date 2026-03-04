#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[render-start] %s\n' "$*"
}

fail() {
  printf '[render-start] ERROR: %s\n' "$*" >&2
  exit 1
}

require_env() {
  local key="$1"
  if [[ -z "${!key:-}" ]]; then
    fail "Missing required env var: ${key}"
  fi
}

extract_archive() {
  local archive="$1"
  local target_dir="$2"

  mkdir -p "${target_dir}"
  if tar -xf "${archive}" -C "${target_dir}" >/dev/null 2>&1; then
    return 0
  fi
  if unzip -q "${archive}" -d "${target_dir}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

find_opend_bin() {
  local opend_root="${OPEND_DIR:-/opt/opend}"
  local candidate=""

  if [[ -n "${OPEND_BIN:-}" && -f "${OPEND_BIN}" ]]; then
    chmod +x "${OPEND_BIN}" || true
    if [[ -x "${OPEND_BIN}" ]]; then
      echo "${OPEND_BIN}"
      return 0
    fi
  fi

  for candidate in \
    "${opend_root}/FutuOpenD" \
    "${opend_root}/OpenD" \
    "/opt/opend/FutuOpenD" \
    "/opt/opend/OpenD"; do
    if [[ -f "${candidate}" ]]; then
      chmod +x "${candidate}" || true
      if [[ -x "${candidate}" ]]; then
        echo "${candidate}"
        return 0
      fi
    fi
  done

  candidate="$(find "${opend_root}" -type f \( -name "FutuOpenD" -o -name "OpenD" \) 2>/dev/null | head -n 1 || true)"
  if [[ -n "${candidate}" && -f "${candidate}" ]]; then
    chmod +x "${candidate}" || true
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  fi

  echo ""
}

download_opend_package() {
  local url="${OPEND_DOWNLOAD_URL:-}"
  local work_dir="/tmp/opend_pkg"
  local archive="${work_dir}/opend_package.bin"
  local opend_root="${OPEND_DIR:-/opt/opend}"

  if [[ -z "${url}" ]]; then
    fail "OpenD binary not found, and OPEND_DOWNLOAD_URL is empty"
  fi

  log "Downloading OpenD package from OPEND_DOWNLOAD_URL"
  rm -rf "${work_dir}"
  mkdir -p "${work_dir}" "${opend_root}"

  curl -fL "${url}" -o "${archive}"
  if ! extract_archive "${archive}" "${opend_root}"; then
    fail "Failed to extract OpenD package. Supported package types: tar/zip"
  fi
}

wait_for_port() {
  local host="$1"
  local port="$2"
  local timeout_sec="$3"
  local i

  for ((i=1; i<=timeout_sec; i++)); do
    if nc -z "${host}" "${port}" >/dev/null 2>&1; then
      log "OpenD is ready on ${host}:${port}"
      return 0
    fi
    sleep 1
  done

  log "OpenD stdout tail:"
  tail -n 100 /tmp/opend.stdout.log 2>/dev/null || true
  log "OpenD stderr tail:"
  tail -n 100 /tmp/opend.stderr.log 2>/dev/null || true
  fail "Timed out waiting for OpenD on ${host}:${port}"
}

main() {
  require_env "STOCK_CODES"
  require_env "FUTU_LOGIN_ACCOUNT"
  if [[ -z "${FUTU_LOGIN_PWD:-}" && -z "${FUTU_LOGIN_PWD_MD5:-}" ]]; then
    fail "Provide one of FUTU_LOGIN_PWD or FUTU_LOGIN_PWD_MD5"
  fi

  local opend_bin
  opend_bin="$(find_opend_bin)"
  if [[ -z "${opend_bin}" ]]; then
    download_opend_package
    opend_bin="$(find_opend_bin)"
  fi
  if [[ -z "${opend_bin}" ]]; then
    fail "Unable to locate OpenD binary after package extraction"
  fi

  local futu_api_ip="${FUTU_API_IP:-127.0.0.1}"
  local futu_api_port="${FUTU_API_PORT:-11111}"
  local futu_lang="${FUTU_LANG:-en}"
  local futu_no_monitor="${FUTU_NO_MONITOR:-1}"
  local futu_console="${FUTU_CONSOLE:-0}"

  local -a opend_args=(
    "-api_ip=${futu_api_ip}"
    "-api_port=${futu_api_port}"
    "-login_account=${FUTU_LOGIN_ACCOUNT}"
    "-lang=${futu_lang}"
    "-no_monitor=${futu_no_monitor}"
    "-console=${futu_console}"
  )
  if [[ -n "${FUTU_LOGIN_PWD_MD5:-}" ]]; then
    opend_args+=("-login_pwd_md5=${FUTU_LOGIN_PWD_MD5}")
  else
    opend_args+=("-login_pwd=${FUTU_LOGIN_PWD}")
  fi
  if [[ -n "${FUTU_LOG_LEVEL:-}" ]]; then
    opend_args+=("-log_level=${FUTU_LOG_LEVEL}")
  fi
  if [[ -n "${FUTU_WEBSOCKET_PORT:-}" ]]; then
    opend_args+=("-websocket_port=${FUTU_WEBSOCKET_PORT}")
  fi
  if [[ -n "${FUTU_EXTRA_ARGS:-}" ]]; then
    # FUTU_EXTRA_ARGS example: "-rsa_private_key=/etc/keys/rsa.pem -keep_alive=1"
    # shellcheck disable=SC2206
    local extra_args=( ${FUTU_EXTRA_ARGS} )
    opend_args+=("${extra_args[@]}")
  fi

  log "Starting OpenD: ${opend_bin}"
  "${opend_bin}" "${opend_args[@]}" >/tmp/opend.stdout.log 2>/tmp/opend.stderr.log &
  local opend_pid="$!"
  trap 'if kill -0 "${opend_pid}" >/dev/null 2>&1; then kill "${opend_pid}" >/dev/null 2>&1 || true; fi' EXIT INT TERM

  wait_for_port "${futu_api_ip}" "${futu_api_port}" "${OPEND_READY_TIMEOUT:-60}"

  local web_port="${PORT:-${WEB_PORT:-10000}}"
  log "Starting web server on 0.0.0.0:${web_port}"
  exec python /app/plot_positions_option_web.py "${STOCK_CODES}" \
    --host "${futu_api_ip}" \
    --port "${futu_api_port}" \
    --poll_interval "${POLL_INTERVAL:-10}" \
    --price_interval "${PRICE_INTERVAL:-10}" \
    --ui_interval "${UI_INTERVAL:-5}" \
    --price_mode "${PRICE_MODE:-implied}" \
    --profit_highlight_threshold "${PROFIT_HIGHLIGHT_THRESHOLD:-80}" \
    --web_host "0.0.0.0" \
    --web_port "${web_port}"
}

main "$@"
