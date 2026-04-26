#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${SIDEBAR_REDIRECT_PORT:-8099}"
STATE_DIR=".dev"
PID_FILE="${STATE_DIR}/vacuum_dashboard_dev_${PORT}.pid"
COMMAND="${1:-restart}"

export VACUUM_DASHBOARD_DEV=1
export SIDEBAR_REDIRECT_PORT="${PORT}"

usage() {
  cat <<EOF
Usage: $0 [start|restart|stop|status]

Environment:
  SIDEBAR_REDIRECT_PORT  Port to bind, defaults to 8099.
EOF
}

saved_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    tr -d '[:space:]' < "${PID_FILE}"
  fi
  return 0
}

pid_is_running() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

pid_is_dashboard() {
  local pid="$1"
  local command_line

  command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  [[ "${command_line}" == *"addon/vacuum_arrival_automation/redirect_dashboard.py"* ]]
}

remove_pid_file_if_current() {
  local pid="$1"

  if [[ "$(saved_pid)" == "${pid}" ]]; then
    rm -f "${PID_FILE}"
  fi
}

stop_server() {
  local pid
  pid="$(saved_pid)"

  if ! pid_is_running "${pid}"; then
    rm -f "${PID_FILE}"
    echo "No local dashboard is running on tracked port ${PORT}."
    return 0
  fi

  if ! pid_is_dashboard "${pid}"; then
    echo "PID file points to ${pid}, but it is not the dashboard. Leaving it alone."
    rm -f "${PID_FILE}"
    return 1
  fi

  echo "Stopping local dashboard on port ${PORT} (PID ${pid})..."
  kill "${pid}"

  for _ in {1..30}; do
    if ! pid_is_running "${pid}"; then
      remove_pid_file_if_current "${pid}"
      echo "Stopped."
      return 0
    fi
    sleep 0.1
  done

  echo "Dashboard did not exit after SIGTERM; sending SIGKILL to PID ${pid}."
  kill -9 "${pid}"
  remove_pid_file_if_current "${pid}"
}

port_listener() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
  fi
  return 0
}

ensure_port_available() {
  local listener
  listener="$(port_listener)"

  if [[ -z "${listener}" ]]; then
    return 0
  fi

  if [[ "${listener}" == "$(saved_pid)" ]] && pid_is_dashboard "${listener}"; then
    stop_server
    return 0
  fi

  echo "Port ${PORT} is already used by PID ${listener}."
  echo "Use a different port, for example:"
  echo "  SIDEBAR_REDIRECT_PORT=8100 $0"
  return 1
}

start_server() {
  mkdir -p "${STATE_DIR}"
  ensure_port_available

  echo "Starting local vacuum dashboard at http://127.0.0.1:${PORT}"
  echo "Edit addon/vacuum_arrival_automation/dashboard.html and refresh the browser."
  echo "Press Ctrl+C to stop, or run: $0 stop"

  python3 addon/vacuum_arrival_automation/redirect_dashboard.py &
  local pid="$!"
  echo "${pid}" > "${PID_FILE}"

  cleanup() {
    if pid_is_running "${pid}"; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
    remove_pid_file_if_current "${pid}"
  }

  trap cleanup EXIT INT TERM
  wait "${pid}"
}

status_server() {
  local pid
  pid="$(saved_pid)"

  if pid_is_running "${pid}" && pid_is_dashboard "${pid}"; then
    echo "Local dashboard is running at http://127.0.0.1:${PORT} (PID ${pid})."
    return 0
  fi

  rm -f "${PID_FILE}"
  echo "Local dashboard is not running on tracked port ${PORT}."
}

case "${COMMAND}" in
  start)
    start_server
    ;;
  restart|"")
    stop_server >/dev/null || true
    start_server
    ;;
  stop)
    stop_server
    ;;
  status)
    status_server
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
