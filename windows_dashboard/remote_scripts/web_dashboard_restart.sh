#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/web_dashboard_stop.sh" >/dev/null 2>&1 || true
"$SCRIPT_DIR/web_dashboard_start.sh"

