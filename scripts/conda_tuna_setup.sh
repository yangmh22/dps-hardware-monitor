#!/usr/bin/env bash
set -euo pipefail

CONDA="${CONDA_BIN:-$HOME/miniconda3/bin/conda}"
if [ ! -x "$CONDA" ]; then
  echo "ERROR: conda not found/executable at: $CONDA" >&2
  exit 2
fi

ts="$(date +%Y%m%d-%H%M%S)"

backup_if_exists() {
  local f="$1"
  if [ -f "$f" ]; then
    cp "$f" "$f.bak.$ts"
  fi
}

write_condarc() {
  local f="$1"
  mkdir -p "$(dirname "$f")"
  cat >"$f" <<'EOF'
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
  pytorch: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
  nvidia: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
EOF
}

USER_CONDARC="$HOME/.condarc"
ROOT_CONDARC="${ROOT_CONDARC:-$HOME/miniconda3/.condarc}"

backup_if_exists "$USER_CONDARC"
backup_if_exists "$ROOT_CONDARC"

write_condarc "$USER_CONDARC"
write_condarc "$ROOT_CONDARC"

"$CONDA" clean -i -y >/dev/null 2>&1 || true

echo "=== conda config sources ==="
"$CONDA" config --show-sources || true
echo
echo "=== conda channels (effective) ==="
"$CONDA" config --show channels || true

