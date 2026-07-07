#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time


def run_torch(seconds: int) -> bool:
    try:
        import torch  # type: ignore
    except Exception:
        return False
    if not torch.cuda.is_available():
        return False

    device = "cuda:0"
    a = torch.randn((4096, 4096), device=device)
    b = torch.randn((4096, 4096), device=device)
    end_at = time.time() + max(seconds, 1)
    loops = 0
    while time.time() < end_at:
        c = a @ b
        a = b
        b = c
        loops += 1
    torch.cuda.synchronize()
    print(f"gpu_stress_done backend=torch seconds={seconds} loops={loops}")
    return True


def run_cupy(seconds: int) -> bool:
    try:
        import cupy as cp  # type: ignore
    except Exception:
        return False

    a = cp.random.random((4096, 4096), dtype=cp.float32)
    b = cp.random.random((4096, 4096), dtype=cp.float32)
    end_at = time.time() + max(seconds, 1)
    loops = 0
    while time.time() < end_at:
        c = a @ b
        a = b
        b = c
        loops += 1
    cp.cuda.Stream.null.synchronize()
    print(f"gpu_stress_done backend=cupy seconds={seconds} loops={loops}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU stress test")
    parser.add_argument("--seconds", type=int, default=20, help="Duration in seconds")
    args = parser.parse_args()

    if run_torch(args.seconds):
        return 0
    if run_cupy(args.seconds):
        return 0

    print("gpu_stress_skipped no_supported_backend(torch/cupy)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
