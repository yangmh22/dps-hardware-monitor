#!/usr/bin/env python3
"""Terminal hardware monitor for CPU, GPU, memory, disk and network."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psutil
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import pynvml  # type: ignore
except Exception:
    pynvml = None


def bytes_to_human(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(num)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


@dataclass
class SnapshotState:
    disk_read: int = 0
    disk_write: int = 0
    net_sent: int = 0
    net_recv: int = 0
    ts: float = 0.0


class Monitor:
    def __init__(self) -> None:
        self.hostname = socket.gethostname()
        self.boot_time = datetime.fromtimestamp(psutil.boot_time())
        self.state = SnapshotState()
        self.gpu_available = False
        self.gpu_error = ""
        self._init_gpu()

    def _init_gpu(self) -> None:
        if pynvml is None:
            self.gpu_available = False
            self.gpu_error = (
                "nvidia-ml-py not installed (pip install nvidia-ml-py); "
                "only NVIDIA GPUs are supported on Windows/Linux here"
            )
            return
        try:
            pynvml.nvmlInit()
            self.gpu_available = True
        except Exception as exc:
            self.gpu_available = False
            self.gpu_error = str(exc)

    def _get_cpu_model(self) -> str:
        sys_name = platform.system()
        if sys_name == "Windows":
            return platform.processor() or "Unknown CPU"
        elif sys_name == "Darwin":
            try:
                import subprocess
                out = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"])
                return out.strip().decode()
            except Exception:
                return platform.processor() or "Unknown CPU"
        else:
            try:
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if "model name" in line:
                            return line.split(":")[1].strip()
            except Exception:
                pass
            return platform.processor() or "Unknown CPU"

    def _collect_cpu(self) -> dict[str, Any]:
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        total = psutil.cpu_percent(interval=None)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        freq = psutil.cpu_freq()
        return {
            "model": self._get_cpu_model(),
            "total_percent": total,
            "per_core_percent": per_core,
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "freq_mhz": round(freq.current, 1) if freq else None,
            "load_avg": [round(v, 2) for v in load],
            "temperature_c": self._read_cpu_temp_c(),
        }

    def _read_cpu_temp_c(self) -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            temps = {}

        if temps:
            for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
                entries = temps.get(key)
                if entries:
                    values = [e.current for e in entries if getattr(e, "current", None) is not None]
                    if values:
                        return round(sum(values) / len(values), 1)
            for entries in temps.values():
                values = [e.current for e in entries if getattr(e, "current", None) is not None]
                if values:
                    return round(sum(values) / len(values), 1)

        # Fallback: Linux thermal zones
        zone_base = "/sys/class/thermal"
        try:
            for name in os.listdir(zone_base):
                if not name.startswith("thermal_zone"):
                    continue
                temp_path = os.path.join(zone_base, name, "temp")
                if not os.path.exists(temp_path):
                    continue
                with open(temp_path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                value = float(raw)
                if value > 1000:
                    value = value / 1000.0
                if 0 < value < 150:
                    return round(value, 1)
        except Exception:
            pass

        if platform.system() == "Windows":
            w = self._read_cpu_temp_c_windows_wmi()
            if w is not None:
                return w

        return None

    def _read_cpu_temp_c_windows_wmi(self) -> float | None:
        """Best-effort CPU temperature on Windows (psutil has no sensors here).

        Tries, in order:
        1) ACPI thermal zones via WMI (some laptops expose MSAcpi_ThermalZoneTemperature).
        2) LibreHardwareMonitor WMI (needs LHM running with WMI plugin enabled).

        Optional: pip install pywin32 WMI
        """
        try:
            import wmi  # type: ignore
        except ImportError:
            return None

        # 1) root\wmi — deciKelvin above absolute zero
        try:
            c = wmi.WMI(namespace="root\\wmi")
            vals: list[float] = []
            for t in c.MSAcpi_ThermalZoneTemperature():
                raw = getattr(t, "CurrentTemperature", None)
                if raw is None:
                    continue
                celsius = float(raw) / 10.0 - 273.15
                if -30.0 < celsius < 120.0:
                    vals.append(celsius)
            if vals:
                return round(sum(vals) / len(vals), 1)
        except Exception:
            pass

        # 2) LibreHardwareMonitor — Identifier like /amdcpu/0/temperature/2
        try:
            c = wmi.WMI(namespace="root\\LibreHardwareMonitor")
            cpuish: list[float] = []
            for s in c.Sensor():
                ident = str(getattr(s, "Identifier", "") or "").lower()
                if "/temperature/" not in ident:
                    continue
                try:
                    val = float(getattr(s, "Value", 0))
                except (TypeError, ValueError):
                    continue
                if not (-10.0 < val < 120.0):
                    continue
                name = (getattr(s, "Name", "") or "").lower()
                if any(
                    (k in name or k in ident)
                    for k in ("cpu", "core", "package", "ccd", "socket", "tctl", "tdie", "t die", "ryzen")
                ):
                    cpuish.append(val)
            if cpuish:
                return round(sum(cpuish) / len(cpuish), 1)
        except Exception:
            pass

        return None

    def _collect_memory(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return {
            "virtual": {
                "total": vm.total,
                "used": vm.used,
                "available": vm.available,
                "percent": vm.percent,
            },
            "swap": {
                "total": sm.total,
                "used": sm.used,
                "free": sm.free,
                "percent": sm.percent,
            },
        }

    def _collect_disk(self, dt: float) -> dict[str, Any]:
        io = psutil.disk_io_counters()
        read_rate = 0.0
        write_rate = 0.0
        if dt > 0 and self.state.ts > 0:
            read_rate = max(0.0, (io.read_bytes - self.state.disk_read) / dt)
            write_rate = max(0.0, (io.write_bytes - self.state.disk_write) / dt)

        parts = []
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except (PermissionError, OSError):
                # Windows: some drives (e.g. empty optical, odd network mounts) raise OSError WinError 1.
                continue
            parts.append(
                {
                    "device": p.device,
                    "mount": p.mountpoint,
                    "fstype": p.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "percent": usage.percent,
                }
            )

        return {
            "io": {
                "read_bytes": io.read_bytes,
                "write_bytes": io.write_bytes,
                "read_rate": read_rate,
                "write_rate": write_rate,
            },
            "partitions": parts,
        }

    def _collect_network(self, dt: float) -> dict[str, Any]:
        net = psutil.net_io_counters()
        sent_rate = 0.0
        recv_rate = 0.0
        if dt > 0 and self.state.ts > 0:
            sent_rate = max(0.0, (net.bytes_sent - self.state.net_sent) / dt)
            recv_rate = max(0.0, (net.bytes_recv - self.state.net_recv) / dt)

        interfaces = []
        stats = psutil.net_if_stats()
        io_per_nic = psutil.net_io_counters(pernic=True)
        for name, data in io_per_nic.items():
            nic_stat = stats.get(name)
            interfaces.append(
                {
                    "name": name,
                    "is_up": bool(nic_stat.isup) if nic_stat else False,
                    "speed_mbps": nic_stat.speed if nic_stat else 0,
                    "bytes_sent": data.bytes_sent,
                    "bytes_recv": data.bytes_recv,
                    "packets_sent": data.packets_sent,
                    "packets_recv": data.packets_recv,
                }
            )

        return {
            "total": {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
                "sent_rate": sent_rate,
                "recv_rate": recv_rate,
            },
            "interfaces": interfaces,
            "connections": len(psutil.net_connections(kind="inet")),
        }

    def _collect_gpu(self) -> dict[str, Any]:
        if not self.gpu_available:
            return {
                "available": False,
                "error": self.gpu_error,
                "gpus": [],
            }

        gpus: list[dict[str, Any]] = []
        try:
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                power = None
                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                except Exception:
                    power = None
                gpus.append(
                    {
                        "index": i,
                        "name": name,
                        "gpu_util": util.gpu,
                        "mem_util": util.memory,
                        "temp_c": temp,
                        "mem_total": mem.total,
                        "mem_used": mem.used,
                        "power_w": round(power, 1) if power is not None else None,
                    }
                )
            return {"available": True, "error": "", "gpus": gpus}
        except Exception as exc:
            return {"available": False, "error": str(exc), "gpus": []}

    def _collect_users(self) -> dict[str, Any]:
        sessions = []
        online_user_set: set[str] = set()
        for u in psutil.users():
            name = (u.name or "").strip()
            if name:
                online_user_set.add(name)
            sessions.append(
                {
                    "name": name,
                    "terminal": u.terminal,
                    "host": u.host,
                    "started": int(u.started) if getattr(u, "started", None) else None,
                }
            )

        process_count: dict[str, int] = {}
        proc_entries: list[tuple[Any, str]] = []
        for proc in psutil.process_iter(attrs=["username", "pid"]):
            try:
                username = proc.info.get("username") or "unknown"
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            short_name = username.split("\\")[-1].split("/")[-1] or username
            process_count[short_name] = process_count.get(short_name, 0) + 1
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
            proc_entries.append((proc, short_name))

        # Second sample after a short window so cpu_percent() is meaningful (see psutil docs).
        time.sleep(0.15)
        cpu_by_user: dict[str, float] = {}
        for proc, short_name in proc_entries:
            try:
                pct = proc.cpu_percent(interval=None)
                cpu_by_user[short_name] = cpu_by_user.get(short_name, 0.0) + float(pct)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        ranked = sorted(process_count.items(), key=lambda x: x[1], reverse=True)
        cpu_ranked = sorted(cpu_by_user.items(), key=lambda x: x[1], reverse=True)
        top_user = (
            {"name": cpu_ranked[0][0], "cpu_percent": round(cpu_ranked[0][1], 1)}
            if cpu_ranked
            else None
        )
        return {
            "online_users": sorted(online_user_set),
            "online_sessions": sessions,
            "process_count_by_user": [{"name": name, "processes": cnt} for name, cnt in ranked],
            "cpu_percent_by_user": [
                {"name": name, "cpu_percent": round(pct, 1)} for name, pct in cpu_ranked[:32]
            ],
            "top_process_user": top_user,
        }

    def collect(self) -> dict[str, Any]:
        now = time.time()
        dt = now - self.state.ts if self.state.ts else 0.0

        cpu = self._collect_cpu()
        memory = self._collect_memory()
        disk = self._collect_disk(dt)
        network = self._collect_network(dt)
        gpu = self._collect_gpu()
        users = self._collect_users()

        self.state.disk_read = disk["io"]["read_bytes"]
        self.state.disk_write = disk["io"]["write_bytes"]
        self.state.net_sent = network["total"]["bytes_sent"]
        self.state.net_recv = network["total"]["bytes_recv"]
        self.state.ts = now

        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "hostname": self.hostname,
            "uptime_seconds": int(now - psutil.boot_time()),
            "boot_time": self.boot_time.isoformat(timespec="seconds"),
            "cpu": cpu,
            "gpu": gpu,
            "memory": memory,
            "disk": disk,
            "network": network,
            "users": users,
        }


def render(snapshot: dict[str, Any]) -> Panel:
    header = Text(
        f"Host: {snapshot['hostname']} | Time: {snapshot['timestamp']} | Uptime: {snapshot['uptime_seconds']}s",
        style="bold cyan",
    )

    cpu = snapshot["cpu"]
    cpu_table = Table(title="CPU", expand=True)
    cpu_table.add_column("Metric", style="cyan")
    cpu_table.add_column("Value", style="white")
    cpu_table.add_row("Total", f"{cpu['total_percent']:.1f}%")
    cpu_table.add_row("Cores", f"{cpu['physical_cores']} phys / {cpu['logical_cores']} logical")
    cpu_table.add_row("Freq", f"{cpu['freq_mhz']} MHz" if cpu["freq_mhz"] else "N/A")
    cpu_table.add_row("Temp", f"{cpu['temperature_c']} C" if cpu.get("temperature_c") is not None else "N/A")
    cpu_table.add_row("Load(1/5/15)", "/".join(str(v) for v in cpu["load_avg"]))
    top_cores = sorted(enumerate(cpu["per_core_percent"]), key=lambda x: x[1], reverse=True)[:8]
    cpu_table.add_row("Top cores", ", ".join(f"#{idx}:{val:.0f}%" for idx, val in top_cores))

    gpu = snapshot["gpu"]
    gpu_table = Table(title="GPU", expand=True)
    gpu_table.add_column("GPU")
    gpu_table.add_column("Util")
    gpu_table.add_column("Mem")
    gpu_table.add_column("Temp")
    gpu_table.add_column("Power")
    if gpu["available"] and gpu["gpus"]:
        for item in gpu["gpus"]:
            gpu_table.add_row(
                f"{item['index']}:{item['name']}",
                f"{item['gpu_util']}%",
                f"{bytes_to_human(item['mem_used'])}/{bytes_to_human(item['mem_total'])} ({item['mem_util']}%)",
                f"{item['temp_c']} C",
                f"{item['power_w']} W" if item["power_w"] is not None else "N/A",
            )
    else:
        gpu_table.add_row("N/A", "N/A", "N/A", "N/A", gpu.get("error") or "Unavailable")

    mem = snapshot["memory"]
    mem_table = Table(title="Memory", expand=True)
    mem_table.add_column("Type")
    mem_table.add_column("Usage")
    mem_table.add_row(
        "RAM",
        f"{bytes_to_human(mem['virtual']['used'])}/{bytes_to_human(mem['virtual']['total'])} ({mem['virtual']['percent']}%)",
    )
    mem_table.add_row(
        "Swap",
        f"{bytes_to_human(mem['swap']['used'])}/{bytes_to_human(mem['swap']['total'])} ({mem['swap']['percent']}%)",
    )

    disk = snapshot["disk"]
    disk_table = Table(title="Disk", expand=True)
    disk_table.add_column("Mount")
    disk_table.add_column("Used/Total")
    disk_table.add_column("Use%")
    for p in disk["partitions"][:8]:
        disk_table.add_row(
            p["mount"],
            f"{bytes_to_human(p['used'])}/{bytes_to_human(p['total'])}",
            f"{p['percent']}%",
        )
    disk_table.add_row(
        "I/O rate",
        f"Read {bytes_to_human(disk['io']['read_rate'])}/s | Write {bytes_to_human(disk['io']['write_rate'])}/s",
        "-",
    )

    net = snapshot["network"]
    net_table = Table(title="Network", expand=True)
    net_table.add_column("Interface")
    net_table.add_column("State")
    net_table.add_column("Traffic")
    for nic in net["interfaces"][:8]:
        state = "UP" if nic["is_up"] else "DOWN"
        net_table.add_row(
            nic["name"],
            f"{state} {nic['speed_mbps']}Mbps",
            f"TX {bytes_to_human(nic['bytes_sent'])} | RX {bytes_to_human(nic['bytes_recv'])}",
        )
    net_table.add_row(
        "Rate",
        f"Active TCP/UDP: {net['connections']}",
        f"TX {bytes_to_human(net['total']['sent_rate'])}/s | RX {bytes_to_human(net['total']['recv_rate'])}/s",
    )

    body = Group(header, cpu_table, gpu_table, mem_table, disk_table, net_table)
    return Panel(body, title="Hardware Monitor", border_style="green")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Server performance monitor")
    parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Collect once and print JSON")
    parser.add_argument("--jsonl", type=str, default="", help="Append snapshots to JSONL file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    monitor = Monitor()
    console = Console()

    if args.once:
        snap = monitor.collect()
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0

    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2

    with Live(render(monitor.collect()), console=console, refresh_per_second=4) as live:
        while True:
            snap = monitor.collect()
            live.update(render(snap))
            if args.jsonl:
                with open(args.jsonl, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
