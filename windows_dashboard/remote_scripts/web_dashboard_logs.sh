#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/web_dashboard_common.sh"

ensure_layout
tail -n "${1:-80}" "$ERR_LOG" "$OUT_LOG"
