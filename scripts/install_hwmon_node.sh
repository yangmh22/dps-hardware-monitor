#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE:?BASE required}"
INTERVAL="${INTERVAL:-10}"
PID_FILE="$BASE/hwmon.pid"
LOG_FILE="$BASE/logs/runner.log"
METRICS_FILE="$BASE/logs/metrics.jsonl"
mkdir -p "$BASE/logs"

CONDA_BIN=""
for c in "$HOME/miniconda3/bin/conda" "$BASE/miniconda3/bin/conda"; do
  if [ -x "$c" ]; then CONDA_BIN="$c"; break; fi
done
if [ -z "$CONDA_BIN" ]; then
  echo "ERROR: conda not found" >&2
  exit 2
fi
if [ ! -f "$BASE/app/daemon_writer.py" ]; then
  echo "ERROR: missing $BASE/app/daemon_writer.py" >&2
  exit 3
fi

cat > "$BASE/start_hwmon.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
BASE="$BASE"
PID_FILE="\$BASE/hwmon.pid"
LOG_FILE="\$BASE/logs/runner.log"
METRICS_FILE="\$BASE/logs/metrics.jsonl"
CONDA_BIN="$CONDA_BIN"
mkdir -p "\$BASE/logs"
if [ -f "\$PID_FILE" ]; then
  pid="\$(cat "\$PID_FILE" 2>/dev/null || true)"
  if [ -n "\${pid:-}" ] && kill -0 "\$pid" 2>/dev/null; then
    echo "already running pid=\$pid"
    exit 0
  fi
fi
existing="\$(pgrep -f "\$BASE/app/daemon_writer.py.*\$METRICS_FILE" | head -n 1 || true)"
if [ -n "\$existing" ]; then
  echo "\$existing" > "\$PID_FILE"
  echo "already running pid=\$existing"
  exit 0
fi
rm -f "\$PID_FILE"
nohup "\$CONDA_BIN" run -n hwmon python "\$BASE/app/daemon_writer.py" --interval $INTERVAL --jsonl "\$METRICS_FILE" >>"\$LOG_FILE" 2>&1 < /dev/null &
echo \$! > "\$PID_FILE"
echo "started pid=\$!"
EOF

cat > "$BASE/stop_hwmon.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
BASE="$BASE"
PID_FILE="\$BASE/hwmon.pid"
if [ -f "\$PID_FILE" ]; then
  pid="\$(cat "\$PID_FILE" 2>/dev/null || true)"
  if [ -n "\${pid:-}" ] && kill -0 "\$pid" 2>/dev/null; then
    kill "\$pid" || true
  fi
fi
pkill -f "\$BASE/app/daemon_writer.py" >/dev/null 2>&1 || true
rm -f "\$PID_FILE"
echo stopped
EOF

cat > "$BASE/status_hwmon.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
BASE="$BASE"
PID_FILE="\$BASE/hwmon.pid"
METRICS_FILE="\$BASE/logs/metrics.jsonl"
LOG_FILE="\$BASE/logs/runner.log"
echo "host=\$(hostname 2>/dev/null || true)"
echo "base=\$BASE"
if [ -f "\$PID_FILE" ]; then
  pid="\$(cat "\$PID_FILE" 2>/dev/null || true)"
  if [ -n "\${pid:-}" ] && kill -0 "\$pid" 2>/dev/null; then
    echo "running pid=\$pid"
    ps -p "\$pid" -o pid,etime,stat,cmd 2>/dev/null || true
  else
    echo "not running (stale pid=\${pid:-empty})"
  fi
else
  echo "not running (no pid file)"
fi
pgrep -af "\$BASE/app/daemon_writer.py|daemon_writer.py.*\$METRICS_FILE" 2>/dev/null || true
if [ -f "\$METRICS_FILE" ]; then
  ls -lh "\$METRICS_FILE" || true
  stat -c 'mtime=%y' "\$METRICS_FILE" 2>/dev/null || true
  python3 -c "import os,time; p='\$METRICS_FILE'; print('age_seconds=%d' % (time.time()-os.path.getmtime(p)))" 2>/dev/null || true
  tail -n 1 "\$METRICS_FILE" 2>/dev/null | cut -c1-220 || true
else
  echo "metrics missing: \$METRICS_FILE"
fi
tail -n 30 "\$LOG_FILE" 2>/dev/null || true
EOF

chmod +x "$BASE/start_hwmon.sh" "$BASE/stop_hwmon.sh" "$BASE/status_hwmon.sh"

# Install idempotent @reboot autostart. Preserve unrelated user crontab entries.
tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -Fv "$BASE/start_hwmon.sh" > "$tmp" || true
if ! grep -q '^SHELL=/bin/bash$' "$tmp" 2>/dev/null; then
  printf '%s\n' 'SHELL=/bin/bash' >> "$tmp"
fi
printf '@reboot %s/start_hwmon.sh >> %s/logs/boot-start.log 2>&1\n' "$BASE" "$BASE" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"

# Start now if needed.
"$BASE/start_hwmon.sh"
sleep 12
"$BASE/status_hwmon.sh"
echo "--- crontab ---"
crontab -l 2>/dev/null | grep -E 'dps_hwmonitor|hwmon' || true
