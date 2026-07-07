#!/usr/bin/env python3
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time


def worker(stop_at: float) -> None:
    x = 1.000001
    y = 1.000002
    z = 1.000003
    while time.time() < stop_at:
        # Pure CPU floating-point loop.
        x = (x * y + z) / (y + 1e-9)
        y = (y * z + x) / (z + 1e-9)
        z = (z * x + y) / (x + 1e-9)


def main() -> int:
    parser = argparse.ArgumentParser(description="CPU stress test")
    parser.add_argument("--seconds", type=int, default=20, help="Duration in seconds")
    parser.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) - 1, 1), help="Worker count")
    args = parser.parse_args()

    stop_at = time.time() + max(args.seconds, 1)
    procs: list[mp.Process] = []
    for _ in range(max(args.workers, 1)):
        p = mp.Process(target=worker, args=(stop_at,))
        p.daemon = True
        p.start()
        procs.append(p)

    for p in procs:
        p.join()

    print(f"cpu_stress_done seconds={args.seconds} workers={args.workers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
