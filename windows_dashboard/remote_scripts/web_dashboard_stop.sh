#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/web_dashboard_common.sh"

ensure_layout

mapfile -t pids < <(
  {
    read_pid_file || true
    find_app_pids || true
    port_listener_pids || true
  } | sed '/^$/d' | sort -u
)

if [ "${#pids[@]}" -eq 0 ]; then
  rm -f "$PID_FILE"
  echo "already stopped"
  print_runtime_status
  exit 0
fi

kill "${pids[@]}" 2>/dev/null || true

for _ in {1..10}; do
  still_alive=0
  for pid in "${pids[@]}"; do
    if pid_is_alive "$pid"; then
      still_alive=1
      break
    fi
  done
  if [ "$still_alive" -eq 0 ]; then
    break
  fi
  sleep 1
done

for pid in "${pids[@]}"; do
  if pid_is_alive "$pid"; then
    kill -9 "$pid" 2>/dev/null || true
  fi
done

rm -f "$PID_FILE"
echo "stopped"
print_runtime_status

