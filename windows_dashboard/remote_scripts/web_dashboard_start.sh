#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/web_dashboard_common.sh"

ensure_layout

if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo "already running"
  print_runtime_status
  exit 0
fi

stale_pid="$(read_pid_file || true)"
if pid_is_alive "$stale_pid"; then
  echo "existing pid from pid file is still alive: $stale_pid"
  print_runtime_status
  exit 0
fi
rm -f "$PID_FILE"

nohup bash -lc "exec '$CONDA_BIN' run --no-capture-output -n '$CONDA_ENV' python '$APP_FILE'" \
  >>"$OUT_LOG" 2>>"$ERR_LOG" < /dev/null &
launcher_pid=$!
echo "$launcher_pid" > "$PID_FILE"

if wait_for_http 30 1; then
  echo "started"
  print_runtime_status
  exit 0
fi

echo "failed to start" >&2
print_runtime_status >&2
tail -n 80 "$ERR_LOG" >&2 || true
exit 1

