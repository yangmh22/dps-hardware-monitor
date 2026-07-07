#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-$HOME/dps_hwmonitor/web_dashboard}"
APP_FILE="$BASE/app.py"
PID_FILE="$BASE/dashboard.pid"
OUT_LOG="$BASE/logs/app_stdout.log"
ERR_LOG="$BASE/logs/app_stderr.log"
CONDA_BIN="${CONDA_BIN:-$HOME/miniconda3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-hwmon-dashboard-web}"
PORT="${PORT:-8080}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${PORT}/api/devices}"
APP_PATTERN="${APP_PATTERN:-python ${APP_FILE}}"

ensure_layout() {
  mkdir -p "$BASE/logs"
}

read_pid_file() {
  if [ -f "$PID_FILE" ]; then
    tr -d '[:space:]' < "$PID_FILE"
  fi
}

pid_is_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

find_app_pids() {
  pgrep -f "$APP_FILE" || true
}

port_listener_pids() {
  ss -ltnp "( sport = :$PORT )" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | sort -u
}

wait_for_http() {
  local tries="${1:-20}"
  local delay="${2:-1}"
  local i
  for ((i = 0; i < tries; i++)); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

print_runtime_status() {
  local pid_file_pid app_pids port_pids
  pid_file_pid="$(read_pid_file || true)"
  app_pids="$(find_app_pids | paste -sd, -)"
  port_pids="$(port_listener_pids | paste -sd, -)"

  echo "base=$BASE"
  echo "pid_file=$PID_FILE"
  echo "pid_file_pid=${pid_file_pid:-none}"
  echo "app_pids=${app_pids:-none}"
  echo "port=${PORT}"
  echo "port_pids=${port_pids:-none}"

  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "health=ok"
  else
    echo "health=down"
  fi
}

