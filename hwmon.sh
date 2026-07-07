#!/usr/bin/env bash
set -euo pipefail
svc=hardware-monitor.service

cmd=${1:-ensure}
case "$cmd" in
  status)
    systemctl --no-pager status "$svc" | sed -n "1,12p"
    ;;
  start)
    sudo systemctl start "$svc"
    systemctl is-active "$svc"
    ;;
  stop)
    sudo systemctl stop "$svc"
    systemctl is-active "$svc" || true
    ;;
  restart)
    sudo systemctl restart "$svc"
    systemctl is-active "$svc"
    ;;
  ensure)
    if systemctl is-active --quiet "$svc"; then
      echo "running"
    else
      echo "not running, starting..."
      sudo systemctl start "$svc"
      systemctl is-active "$svc"
    fi
    ;;
  logs)
    journalctl -u "$svc" -n 50 --no-pager
    ;;
  *)
    echo "Usage: hwmon [ensure|status|start|stop|restart|logs]"
    exit 2
    ;;
esac
