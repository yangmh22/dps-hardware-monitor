#!/usr/bin/env bash
set -euo pipefail

# Verify and enforce TUNA-only conda config for the ARM Miniconda under /tmp/hwmon_forwarder.
# Only touches /tmp/hwmon_forwarder.

BASE="/tmp/hwmon_forwarder"
CONDA_BIN="$BASE/miniconda3/bin/conda"
CONDARC="$BASE/condarc.tuna"

if [ ! -x "$CONDA_BIN" ]; then
  echo "ERROR: conda not found at $CONDA_BIN" >&2
  exit 2
fi
if [ ! -f "$CONDARC" ]; then
  echo "ERROR: condarc not found at $CONDARC" >&2
  exit 3
fi

export CONDARC="$CONDARC"

echo "=== show-sources ==="
"$CONDA_BIN" config --show-sources || true
echo
echo "=== channels (effective) ==="
"$CONDA_BIN" config --show channels || true
echo
echo "=== default_channels (effective) ==="
"$CONDA_BIN" config --show default_channels || true

echo
echo "=== IMPORTANT ==="
echo "For our forwarder installs we DO NOT use default_channels."
echo "We always create envs with: conda create --override-channels -c <tuna urls> ..."

