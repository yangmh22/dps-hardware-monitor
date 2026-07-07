#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from monitor import Monitor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless metrics writer")
    parser.add_argument("--interval", type=float, default=10.0, help="Sampling interval seconds")
    parser.add_argument("--jsonl", type=str, required=True, help="Output JSONL file path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be > 0")

    out = Path(args.jsonl).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    monitor = Monitor()
    while True:
        snap = monitor.collect()
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
