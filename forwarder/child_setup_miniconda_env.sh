#!/usr/bin/env bash
set -euo pipefail

# Create a self-contained conda env for the forwarder under /tmp/hwmon_forwarder.
# This avoids touching system python and avoids consuming the child's small disk elsewhere.

BASE="/tmp/hwmon_forwarder"
ENV_PREFIX="$BASE/conda_env"
CONDARC="$BASE/condarc.tuna"

mkdir -p "$BASE"

find_conda() {
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return 0
  fi
  for p in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "/opt/conda/bin/conda" "/opt/miniconda3/bin/conda"; do
    if [ -x "$p" ]; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

CONDA_BIN="$(find_conda || true)"
if [ -z "${CONDA_BIN:-}" ]; then
  echo "ERROR: conda/miniconda not found on child. (Not installing it automatically.)" >&2
  exit 2
fi

cat >"$CONDARC" <<'EOF'
channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2
  - defaults
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
EOF

if [ ! -x "$ENV_PREFIX/bin/python" ]; then
  # Use a prefix env under /tmp to keep it self-contained.
  CONDARC="$CONDARC" "$CONDA_BIN" create -y -p "$ENV_PREFIX" python=3.11 psutil rich >/dev/null
fi

# nvidia-ml-py is pip-only in our current env spec
CONDARC="$CONDARC" "$CONDA_BIN" run -p "$ENV_PREFIX" python -m pip install -q -U pip >/dev/null 2>&1 || true
CONDARC="$CONDARC" "$CONDA_BIN" run -p "$ENV_PREFIX" python -m pip install -q nvidia-ml-py >/dev/null 2>&1 || true

CONDARC="$CONDARC" "$CONDA_BIN" run -p "$ENV_PREFIX" python -c "import psutil, rich; print('deps_ok')" || exit 3
echo "OK: conda env ready at $ENV_PREFIX"

