#!/usr/bin/env bash
set -euo pipefail

# Setup a tiny venv under /tmp for forwarder dependencies.
# Only touches /tmp/hwmon_forwarder.

BASE="/tmp/hwmon_forwarder"
VENV="$BASE/.venv"

mkdir -p "$BASE"

python3 -m venv "$VENV"

"$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q psutil rich nvidia-ml-py

"$VENV/bin/python" -c "import psutil, rich; print('deps_ok', psutil.__version__)"

