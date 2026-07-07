from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOTAL_MEMORY = 128 * 1024**3
DISK_TOTAL = 1024 * 1024**3
INTERVAL_SECONDS = 60
WINDOW_HOURS = 24


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def wave(i: int, period: float, phase: float = 0.0) -> float:
    return math.sin((i / period) * math.tau + phase)


def snapshot(
    *,
    ts: datetime,
    hostname: str,
    cpu: float,
    temp: float,
    memory: float,
    read_rate: float,
    write_rate: float,
    gpu: bool,
    gpu_util: float = 0.0,
    gpu_mem_percent: float = 0.0,
    uptime_seconds: int,
) -> dict:
    memory_used = int(TOTAL_MEMORY * memory / 100)
    payload = {
        "timestamp": ts.replace(microsecond=0).isoformat(),
        "hostname": hostname,
        "uptime_seconds": uptime_seconds,
        "cpu": {
            "model": "Demo Processor",
            "total_percent": round(cpu, 1),
            "per_core_percent": [round(cpu, 1) for _ in range(8)],
            "physical_cores": 8,
            "logical_cores": 16,
            "temperature_c": round(temp, 1),
        },
        "memory": {
            "virtual": {
                "total": TOTAL_MEMORY,
                "used": memory_used,
                "percent": round(memory, 1),
            }
        },
        "gpu": {
            "available": gpu,
            "gpus": (
                [
                    {
                        "name": "Demo GPU",
                        "gpu_util": round(gpu_util, 1),
                        "mem_total": 24 * 1024**3,
                        "mem_used": int(24 * 1024**3 * gpu_mem_percent / 100),
                    }
                ]
                if gpu
                else []
            ),
        },
        "disk": {
            "io": {
                "read_rate": int(read_rate),
                "write_rate": int(write_rate),
            },
            "partitions": [
                {
                    "device": "/dev/nvme0n1p1",
                    "mount": "/",
                    "total": DISK_TOTAL,
                    "used": int(DISK_TOTAL * (0.32 + memory / 400)),
                    "percent": round(32 + memory / 4, 1),
                }
            ],
        },
        "network": {
            "total": {
                "sent_rate": int(180_000 + cpu * 3_000),
                "recv_rate": int(260_000 + memory * 5_000),
            }
        },
        "users": {
            "online_users": ["demo"],
            "top_process_user": {"name": "demo", "cpu_percent": round(cpu / 3, 1)},
        },
    }
    return payload


def write_node(path: Path, hostname: str, kind: str, now: datetime) -> None:
    count = WINDOW_HOURS * 3600 // INTERVAL_SECONDS
    start = now - timedelta(seconds=(count - 1) * INTERVAL_SECONDS)
    lines = []
    for i in range(count):
        ts = start + timedelta(seconds=i * INTERVAL_SECONDS)
        if kind == "gpu":
            cpu = clamp(34 + 18 * wave(i, 18) + 42 * max(0, wave(i, 36, 0.6)), 6, 92)
            temp = clamp(52 + 10 * wave(i, 22, 0.5) + cpu / 10, 38, 82)
            memory = clamp(50 + 9 * wave(i, 44, 1.0), 32, 74)
            gpu_util = clamp(24 + 38 * max(0, wave(i, 28, 1.2)), 0, 88)
            gpu_mem = clamp(34 + 12 * wave(i, 31), 18, 62)
            read_rate = 55_000_000 + max(0, wave(i, 20)) * 70_000_000
            write_rate = 48_000_000 + max(0, wave(i, 24, 0.3)) * 64_000_000
            gpu_enabled = True
        elif kind == "edge":
            cpu = clamp(9 + 14 * max(0, wave(i, 26, 0.8)), 0, 34)
            temp = clamp(37 + 5 * max(0, wave(i, 30)), 32, 48)
            memory = clamp(27 + 9 * max(0, wave(i, 33, 1.4)), 18, 42)
            gpu_util = 0
            gpu_mem = 0
            read_rate = 4_000_000 + max(0, wave(i, 18, 0.2)) * 65_000_000
            write_rate = 35_000_000 + max(0, wave(i, 21, 0.4)) * 24_000_000
            gpu_enabled = False
        else:
            cpu = clamp(22 + 17 * max(0, wave(i, 24, 0.2)), 5, 56)
            temp = clamp(44 + 7 * max(0, wave(i, 28, 0.6)), 35, 58)
            memory = clamp(42 + 16 * max(0, wave(i, 36, 1.0)), 28, 64)
            gpu_util = 0
            gpu_mem = 0
            read_rate = 15_000_000 + max(0, wave(i, 20)) * 74_000_000
            write_rate = 40_000_000 + max(0, wave(i, 23, 0.7)) * 40_000_000
            gpu_enabled = False

        obj = snapshot(
            ts=ts,
            hostname=hostname,
            cpu=cpu,
            temp=temp,
            memory=memory,
            read_rate=read_rate,
            write_rate=write_rate,
            gpu=gpu_enabled,
            gpu_util=gpu_util,
            gpu_mem_percent=gpu_mem,
            uptime_seconds=86_400 + i * INTERVAL_SECONDS,
        )
        lines.append(json.dumps(obj, ensure_ascii=False))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    now = datetime.now()
    write_node(ROOT / "demo-cpu-01.metrics.jsonl", "demo-cpu-01", "cpu", now)
    write_node(ROOT / "demo-gpu-01.metrics.jsonl", "demo-gpu-01", "gpu", now)
    write_node(ROOT / "demo-edge-01.metrics.jsonl", "demo-edge-01", "edge", now)
    print(f"Generated demo metrics ending at {now.replace(microsecond=0).isoformat()}")


if __name__ == "__main__":
    main()
