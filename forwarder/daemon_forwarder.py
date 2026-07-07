#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from monitor import Monitor


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Forward metrics to a parent host over SSH")
    p.add_argument("--interval", type=float, default=10.0, help="Sampling interval seconds")
    p.add_argument("--parent-host", type=str, required=True, help="Parent host (reachable from child)")
    p.add_argument("--parent-port", type=int, default=22, help="Parent SSH port")
    p.add_argument("--parent-user", type=str, required=True, help="Parent SSH user")
    p.add_argument("--parent-path", type=str, required=True, help="Append-to JSONL path on parent")
    p.add_argument("--ssh-key", type=str, default="", help="SSH private key path (optional)")
    p.add_argument("--batch-lines", type=int, default=30, help="Max lines per SSH append")
    p.add_argument("--flush-seconds", type=float, default=5.0, help="Flush at least every N seconds")
    p.add_argument("--spool-dir", type=str, default="/tmp/hwmon_forwarder/spool", help="Local spool directory")
    p.add_argument("--spool-keep-hours", type=int, default=24, help="Keep at most N hours of spool files")
    p.add_argument("--spool-max-mb", type=int, default=200, help="Hard cap for total spool size MB")
    return p.parse_args()


def _ssh_args(args: argparse.Namespace) -> list[str]:
    a = [
        "ssh",
        "-p",
        str(args.parent_port),
        "-o",
        "BatchMode=yes",
        # Avoid touching system-wide SSH config; keep it scoped to this program only.
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/tmp/hwmon_known_hosts",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
    ]
    if args.ssh_key:
        a += ["-i", str(Path(args.ssh_key).expanduser())]
    a.append(f"{args.parent_user}@{args.parent_host}")
    return a


def _append_remote(args: argparse.Namespace, payload: str) -> None:
    cmd = _ssh_args(args) + [f"cat >> {sh_quote(args.parent_path)}"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(payload, timeout=20)
    if p.returncode != 0:
        raise RuntimeError((err or out or f"ssh failed rc={p.returncode}").strip())


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _spool_path(spool_dir: Path, now: datetime) -> Path:
    # Rotate hourly to keep files small and cleanup cheap.
    return spool_dir / f"spool-{now:%Y%m%d-%H}.jsonl"


def spool_cleanup(spool_dir: Path, keep_hours: int, max_total_bytes: int) -> None:
    spool_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(spool_dir.glob("spool-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return

    cutoff = datetime.now() - timedelta(hours=max(1, keep_hours))
    for f in files:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink(missing_ok=True)
        except Exception:
            continue

    # Enforce a hard cap too (safety net).
    files = sorted(spool_dir.glob("spool-*.jsonl"), key=lambda p: p.stat().st_mtime)
    total = 0
    sizes: list[tuple[Path, int]] = []
    for f in files:
        try:
            sz = int(f.stat().st_size)
        except Exception:
            sz = 0
        total += sz
        sizes.append((f, sz))
    if total <= max_total_bytes:
        return
    # Delete oldest until under cap.
    for f, sz in sizes:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
        total -= sz
        if total <= max_total_bytes:
            break


def spool_append(spool_dir: Path, text: str, keep_hours: int, max_total_bytes: int) -> None:
    now = datetime.now()
    path = _spool_path(spool_dir, now)
    spool_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
    spool_cleanup(spool_dir, keep_hours=keep_hours, max_total_bytes=max_total_bytes)


def spool_drain(spool_dir: Path, max_send_lines: int) -> list[str]:
    spool_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(spool_dir.glob("spool-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return []
    lines: list[str] = []
    src = files[0]
    try:
        with src.open("r", encoding="utf-8") as f:
            for _ in range(max_send_lines):
                line = f.readline()
                if not line:
                    break
                if line.strip():
                    lines.append(line if line.endswith("\n") else (line + "\n"))
        if not lines:
            # If file is empty, remove it.
            if src.stat().st_size == 0:
                src.unlink(missing_ok=True)
            return []
        # Rewrite remaining.
        rest = src.read_text(encoding="utf-8").splitlines(True)[len(lines) :]
        tmp = src.with_suffix(".tmp")
        tmp.write_text("".join(rest), encoding="utf-8")
        tmp.replace(src)
        # If drained fully, delete.
        if src.stat().st_size == 0:
            src.unlink(missing_ok=True)
        return lines
    except Exception:
        return []


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be > 0")

    spool_dir = Path(args.spool_dir).expanduser()
    keep_hours = max(1, int(args.spool_keep_hours))
    spool_max_total = int(args.spool_max_mb) * 1024 * 1024

    mon = Monitor()
    q: deque[str] = deque()
    last_flush = 0.0

    while True:
        snap = mon.collect()
        line = json.dumps(snap, ensure_ascii=False) + "\n"
        q.append(line)

        now = time.time()
        should_flush = (len(q) >= max(1, args.batch_lines)) or (now - last_flush >= max(0.5, args.flush_seconds))

        if should_flush:
            # Drain some spool first (oldest data).
            drained = spool_drain(spool_dir, max_send_lines=max(1, args.batch_lines))
            payload_lines = drained + [q.popleft() for _ in range(min(len(q), max(1, args.batch_lines) - len(drained)))]
            payload = "".join(payload_lines)
            if payload:
                try:
                    _append_remote(args, payload)
                except Exception:
                    # Re-spool the payload (best-effort) and continue.
                    spool_append(spool_dir, payload, keep_hours=keep_hours, max_total_bytes=spool_max_total)
            last_flush = now

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

