#!/usr/bin/env bash
set -euo pipefail
exec "$HOME/miniconda3/bin/conda" run -n hardware-monitor python "$HOME/projects/HardwareMonitor/monitor.py" "$@"
