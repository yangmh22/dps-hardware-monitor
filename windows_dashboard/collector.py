from __future__ import annotations

import json
import os
import posixpath
import shlex
import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import paramiko
import yaml

SAMPLE_INTERVAL_SECONDS = 10

RANGE_SETTINGS = {
    "30m": {"seconds": 1800, "max_points": 360},
    "1h": {"seconds": 3600, "max_points": 360},
    "1d": {"seconds": 86400, "max_points": 720},
    "1w": {"seconds": 604800, "max_points": 840},
    "1m": {"seconds": 2592000, "max_points": 900},
}

RANGE_FETCH_TIMEOUT_SECONDS = {
    "30m": 20.0,
    "1h": 20.0,
    "1d": 20.0,
    "1w": 35.0,
    "1m": 60.0,
}

RANGE_CACHE_TTL_SECONDS = {
    "1w": 120,
    "1m": 300,
}

_range_cache_lock = threading.Lock()
_range_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_range_cache_max_entries = int(os.getenv("DASHBOARD_RANGE_CACHE_MAX_ENTRIES", "64"))

_latest_executor_lock = threading.Lock()
_latest_executor: ThreadPoolExecutor | None = None
_latest_executor_workers = max(1, int(os.getenv("DASHBOARD_LATEST_FETCH_WORKERS", "4")))


def _get_latest_executor() -> ThreadPoolExecutor:
    global _latest_executor
    with _latest_executor_lock:
        if _latest_executor is None:
            _latest_executor = ThreadPoolExecutor(
                max_workers=_latest_executor_workers,
                thread_name_prefix="latest-fetch",
            )
        return _latest_executor


def _shutdown_latest_executor() -> None:
    global _latest_executor
    with _latest_executor_lock:
        if _latest_executor is not None:
            _latest_executor.shutdown(wait=False, cancel_futures=True)
            _latest_executor = None


atexit.register(_shutdown_latest_executor)


def _expand_metrics_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def _local_use_utc_axis(remote_file: str, metrics_utc: bool) -> bool:
    return "/children/" in remote_file or metrics_utc


def _parse_dt_naive(text: str) -> datetime:
    t = (text or "").strip()
    if not t:
        raise ValueError("empty datetime")
    t = t.replace(" ", "T")
    if len(t) >= 19:
        return datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")


def _display_offset_hours(use_utc_axis: bool) -> int:
    if use_utc_axis:
        return 0
    now = datetime.now().astimezone()
    off = now.utcoffset()
    if off is None:
        return 8
    return int(off.total_seconds() // 3600)


def _fmt_with_offset(dt: datetime | None, offset_hours: int) -> str | None:
    if dt is None:
        return None
    sign = "+" if offset_hours >= 0 else "-"
    abs_h = abs(offset_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "{0}{1:02d}:00".format(sign, abs_h)


def _fmt_ts_for_line(dt: datetime | None, use_utc_axis: bool) -> str | None:
    if dt is None:
        return None
    h = _display_offset_hours(use_utc_axis)
    return _fmt_with_offset(dt, h)


def _summarize_snapshot(obj: dict[str, Any], use_utc_axis: bool) -> dict[str, Any]:
    cpu = obj.get("cpu", {})
    mem = obj.get("memory", {}).get("virtual", {})
    gpu = obj.get("gpu", {})
    gpus = gpu.get("gpus", []) if isinstance(gpu, dict) else []
    disk = obj.get("disk", {}).get("io", {})
    net = obj.get("network", {}).get("total", {})
    users = obj.get("users", {})
    top_user = users.get("top_process_user") if isinstance(users, dict) else None
    online_users = users.get("online_users") if isinstance(users, dict) else []
    ts = obj.get("timestamp")
    try:
        ts_dt = _parse_dt_naive(ts) if ts else None
        ts_fmt = _fmt_ts_for_line(ts_dt, use_utc_axis)
    except Exception:
        ts_fmt = ts
    return {
        "timestamp": ts_fmt,
        "cpu_model": cpu.get("model", "Unknown CPU"),
        "cpu_cores": cpu.get("physical_cores"),
        "cpu_percent": cpu.get("total_percent"),
        "cpu_temp_c": cpu.get("temperature_c"),
        "memory_total": mem.get("total"),
        "memory_used": mem.get("used"),
        "memory_percent": mem.get("percent"),
        "gpu_name": (gpus[0].get("name") if gpus else "No GPU"),
        "gpu_percent": (gpus[0].get("gpu_util") if gpus else None),
        "gpu_mem_total": (gpus[0].get("mem_total") if gpus else None),
        "gpu_mem_used": (gpus[0].get("mem_used") if gpus else None),
        "disk_read_rate": disk.get("read_rate"),
        "disk_write_rate": disk.get("write_rate"),
        "net_tx_rate": net.get("sent_rate"),
        "net_rx_rate": net.get("recv_rate"),
        "hostname": obj.get("hostname"),
        "online_users": online_users if isinstance(online_users, list) else [],
        "top_process_user": top_user if isinstance(top_user, dict) else None,
    }


def _read_tail_lines(path: Path, max_lines: int = 2000, max_chunk: int = 2 * 1024 * 1024) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    with path.open("rb") as f:
        if size <= max_chunk:
            raw = f.read()
        else:
            f.seek(-max_chunk, 2)
            raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-max_lines:]


def _latest_json_object_from_file(path: Path) -> dict[str, Any] | None:
    for n in (200, 2000):
        for line in reversed(_read_tail_lines(path, max_lines=n)):
            try:
                candidate = json.loads(line)
            except Exception:
                continue
            if candidate and candidate.get("timestamp"):
                return candidate
    return None


def _estimate_avg_line_bytes(lines: list[str]) -> int:
    if not lines:
        return 512
    total = sum(len(line.encode("utf-8", errors="ignore")) + 1 for line in lines if line)
    if total <= 0:
        return 512
    return max(128, int(total / len(lines)))


def _read_tail_bytes(path: Path, num_bytes: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if size <= 0:
        return b""
    num_bytes = max(1, min(int(num_bytes), size))
    with path.open("rb") as f:
        if num_bytes < size:
            f.seek(-num_bytes, 2)
        return f.read()


def _read_recent_lines_for_range(path: Path, cutoff: datetime, expected_samples: int) -> list[str] | None:
    sample_lines = _read_tail_lines(path, max_lines=min(256, max(32, expected_samples // 1000 or 32)))
    avg_line_bytes = _estimate_avg_line_bytes(sample_lines)
    for factor in (1.25, 2.0, 4.0):
        approx_bytes = max(1024 * 1024, int(avg_line_bytes * expected_samples * factor))
        raw = _read_tail_bytes(path, approx_bytes)
        if not raw:
            return []
        lines = [ln.strip() for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            continue
        earliest_valid: datetime | None = None
        for line in lines:
            try:
                obj = json.loads(line)
                ts = obj.get("timestamp")
                if not ts:
                    continue
                earliest_valid = _parse_dt_naive(ts)
                break
            except Exception:
                continue
        if earliest_valid is None:
            continue
        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0
        if earliest_valid <= cutoff or approx_bytes >= file_size:
            return lines
    return None


def _range_cache_get(device_id: str, range_key: str) -> dict[str, Any] | None:
    ttl = RANGE_CACHE_TTL_SECONDS.get(range_key, 0)
    if ttl <= 0:
        return None
    with _range_cache_lock:
        entry = _range_cache.get((device_id, range_key))
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > ttl:
            _range_cache.pop((device_id, range_key), None)
            return None
        return data


def _range_cache_put(device_id: str, range_key: str, data: dict[str, Any]) -> None:
    ttl = RANGE_CACHE_TTL_SECONDS.get(range_key, 0)
    if ttl <= 0:
        return
    with _range_cache_lock:
        now = time.time()
        for key, (ts, _) in list(_range_cache.items()):
            key_range = key[1]
            key_ttl = RANGE_CACHE_TTL_SECONDS.get(key_range, 0)
            if key_ttl <= 0 or now - ts > key_ttl:
                _range_cache.pop(key, None)
        _range_cache[(device_id, range_key)] = (time.time(), data)
        if len(_range_cache) > _range_cache_max_entries:
            oldest = sorted(_range_cache.items(), key=lambda item: item[1][0])
            for key, _ in oldest[: len(_range_cache) - _range_cache_max_entries]:
                _range_cache.pop(key, None)


def _file_age_seconds(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (datetime.now(timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds()


def _build_latest_payload_from_obj(
    obj: dict[str, Any] | None, remote_file: str, metrics_utc: bool, file_age: float | None
) -> dict[str, Any]:
    use_utc_axis = _local_use_utc_axis(remote_file, metrics_utc)
    if use_utc_axis:
        remote_now = _fmt_with_offset(datetime.now(timezone.utc).replace(tzinfo=None), 0)
    else:
        remote_now = _fmt_with_offset(datetime.now(), _display_offset_hours(False))

    if obj is None:
        return {"latest": None, "remote_now": remote_now, "file_age_seconds": file_age}

    cpu = obj.get("cpu", {})
    mem = obj.get("memory", {}).get("virtual", {})
    gpu = obj.get("gpu", {})
    gpus = gpu.get("gpus", []) if isinstance(gpu, dict) else []
    users = obj.get("users", {})
    top_user = users.get("top_process_user") if isinstance(users, dict) else None
    online_users = users.get("online_users") if isinstance(users, dict) else []
    ts = obj.get("timestamp")
    try:
        ts_dt = _parse_dt_naive(ts) if ts else None
        ts_fmt = _fmt_ts_for_line(ts_dt, use_utc_axis)
    except Exception:
        ts_fmt = ts
    latest = {
        "timestamp": ts_fmt,
        "hostname": obj.get("hostname"),
        "cpu_model": cpu.get("model", "Unknown CPU"),
        "cpu_cores": cpu.get("physical_cores"),
        "cpu_percent": cpu.get("total_percent"),
        "cpu_temp_c": cpu.get("temperature_c"),
        "memory_total": mem.get("total"),
        "memory_used": mem.get("used"),
        "memory_percent": mem.get("percent"),
        "gpu_name": (gpus[0].get("name") if gpus else "No GPU"),
        "gpu_percent": (gpus[0].get("gpu_util") if gpus else None),
        "gpu_mem_total": (gpus[0].get("mem_total") if gpus else None),
        "gpu_mem_used": (gpus[0].get("mem_used") if gpus else None),
        "disk_read_rate": obj.get("disk", {}).get("io", {}).get("read_rate"),
        "disk_write_rate": obj.get("disk", {}).get("io", {}).get("write_rate"),
        "net_tx_rate": obj.get("network", {}).get("total", {}).get("sent_rate"),
        "net_rx_rate": obj.get("network", {}).get("total", {}).get("recv_rate"),
        "online_users": online_users if isinstance(online_users, list) else [],
        "top_process_user": top_user if isinstance(top_user, dict) else None,
    }
    return {"latest": latest, "remote_now": remote_now, "file_age_seconds": file_age}


def _disks_payload_from_snapshot(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not obj:
        return {"disks": [], "count": 0}
    disk = obj.get("disk", {}) or {}
    parts = disk.get("partitions", []) or []

    def base_dev(dev: Any) -> str | None:
        if not isinstance(dev, str):
            return None
        if dev.startswith("/dev/"):
            return dev[len("/dev/") :]
        return dev

    disks: list[dict[str, Any]] = []
    for p in parts:
        dev = p.get("device")
        mount = p.get("mount") or p.get("mountpoint")
        total = p.get("total")
        used = p.get("used")
        percent = p.get("percent")
        name = base_dev(dev) or mount or "unknown"
        size = int(total or 0)
        if size < (2 * (1024**3)):
            continue
        used_i = int(used or 0)
        free = None
        if total is not None and used is not None:
            free = int(total - used)
        disks.append(
            {
                "name": name,
                "size": size,
                "used": used_i,
                "free": free,
                "percent": float(percent) if percent is not None else None,
                "partitions": [{"name": name, "mountpoint": mount}],
            }
        )
    return {"disks": disks, "count": len(disks)}


def fetch_latest_local(device: DeviceConfig) -> dict[str, Any]:
    path = _expand_metrics_path(device.remote_file)
    age = _file_age_seconds(path) if path.is_file() else None
    obj = _latest_json_object_from_file(path) if path.is_file() else None
    inner = _build_latest_payload_from_obj(obj, device.remote_file, device.metrics_utc, age)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "latest": inner.get("latest"),
        "remote_now": inner.get("remote_now"),
        "file_age_seconds": inner.get("file_age_seconds"),
    }


def fetch_range_local(device: DeviceConfig, range_key: str) -> dict[str, Any]:
    if range_key not in RANGE_SETTINGS:
        raise ValueError(f"unsupported range: {range_key}")
    settings = RANGE_SETTINGS[range_key]
    path = _expand_metrics_path(device.remote_file)
    use_utc_axis = _local_use_utc_axis(device.remote_file, device.metrics_utc)
    total_samples = settings["seconds"] // SAMPLE_INTERVAL_SECONDS
    step = max(1, int(total_samples // settings["max_points"]))
    if use_utc_axis:
        cutoff = datetime.utcnow() - timedelta(seconds=settings["seconds"])
    else:
        cutoff = datetime.now() - timedelta(seconds=settings["seconds"])

    points: list[dict[str, Any]] = []
    total_after_cutoff = 0
    latest: dict[str, Any] | None = None

    if path.is_file():
        candidate_lines = _read_recent_lines_for_range(path, cutoff, total_samples)
        if candidate_lines is None:
            line_iter = path.open("r", encoding="utf-8", errors="replace")
        else:
            line_iter = candidate_lines
        try:
            for line in line_iter:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("timestamp")
                if not ts:
                    continue
                try:
                    t = _parse_dt_naive(ts)
                except Exception:
                    continue
                if t < cutoff:
                    continue
                total_after_cutoff += 1
                latest = obj
                if (total_after_cutoff - 1) % step == 0:
                    points.append(_summarize_snapshot(obj, use_utc_axis))
        finally:
            if hasattr(line_iter, "close"):
                line_iter.close()

    data: dict[str, Any] = {
        "points": points,
        "latest": _summarize_snapshot(latest, use_utc_axis) if latest else None,
        "samples": total_after_cutoff,
        "device_id": device.device_id,
        "device_name": device.name,
        "range": range_key,
        "step": step,
    }
    return data


def fetch_disks_local(device: DeviceConfig) -> dict[str, Any]:
    path = _expand_metrics_path(device.remote_file)
    obj = None
    if path.is_file():
        lines = _read_tail_lines(path, max_lines=50)
        for line in reversed(lines):
            try:
                cand = json.loads(line)
            except Exception:
                continue
            if cand:
                obj = cand
                break
    data = _disks_payload_from_snapshot(obj)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "count": data.get("count", 0),
        "disks": data.get("disks", []),
    }


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    name: str
    host: str
    user: str
    port: int
    remote_file: str
    key_file: str | None = None
    password: str | None = None
    # Optional SSH jump host (ProxyJump) settings. When set, the dashboard will SSH to the
    # jump host first and then open a direct-tcpip channel to the target device.
    jump_host: str | None = None
    jump_user: str | None = None
    jump_port: int | None = None
    jump_key_file: str | None = None
    jump_password: str | None = None
    # When True, remote aggregation treats JSONL `timestamp` as naive UTC (like /children/ files):
    # cutoff uses utcnow(), and API labels timestamps with +00:00. Use for hosts whose writer runs
    # with TZ=UTC while the SSH session uses local time.
    metrics_utc: bool = False
    # When True, read `remote_file` from this Windows host (no SSH). Path may use %VAR%.
    local: bool = False


class DeviceRegistry:
    def __init__(self, config_path: str) -> None:
        self._config_path = Path(config_path)
        self._lock = threading.Lock()
        self._devices: dict[str, DeviceConfig] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
            devices = {}
            for item in raw.get("devices", []):
                is_local = bool(item.get("local", False))
                device = DeviceConfig(
                    device_id=item["id"],
                    name=item.get("name", item["id"]),
                    host=item.get("host", "local") if is_local else item["host"],
                    user=item.get("user", "") if is_local else item["user"],
                    port=int(item.get("port", 0 if is_local else 22)),
                    remote_file=item["remote_file"],
                    key_file=item.get("key_file"),
                    password=item.get("password"),
                    jump_host=item.get("jump_host"),
                    jump_user=item.get("jump_user"),
                    jump_port=(int(item["jump_port"]) if item.get("jump_port") is not None else None),
                    jump_key_file=item.get("jump_key_file"),
                    jump_password=item.get("jump_password"),
                    metrics_utc=bool(item.get("metrics_utc", False)),
                    local=is_local,
                )
                devices[device.device_id] = device
            self._devices = devices

    def list_devices(self) -> list[DeviceConfig]:
        with self._lock:
            return list(self._devices.values())

    def get(self, device_id: str) -> DeviceConfig | None:
        with self._lock:
            return self._devices.get(device_id)


def _build_remote_range_command(
    remote_file: str, range_seconds: int, step: int, metrics_utc: bool = False
) -> str:
    script = r"""
python3 - <<'PY'
import json
import os
import subprocess
from datetime import datetime, timedelta

remote_file = __REMOTE_FILE__
RANGE_SECONDS = __RANGE_SECONDS__
step = __STEP__
USE_METRICS_UTC = __USE_METRICS_UTC__
EXPECTED_SAMPLES = __EXPECTED_SAMPLES__
is_child_path = "/children/" in remote_file
use_utc_axis = is_child_path or USE_METRICS_UTC

def parse_dt(text):
    # Python 3.5 compatible parser for ISO-like timestamps.
    t = (text or "").strip()
    if not t:
        raise ValueError("empty datetime")
    t = t.replace(" ", "T")
    if len(t) >= 19:
        return datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")

def fmt_ts(dt, offset_hours: int):
    sign = "+" if offset_hours >= 0 else "-"
    abs_h = abs(offset_hours)
    # Avoid f-strings for compatibility with older python3 versions on remote hosts.
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "{0}{1:02d}:00".format(sign, abs_h)

cutoff = (datetime.utcnow() if use_utc_axis else datetime.now()) - timedelta(seconds=RANGE_SECONDS)

points = []
total_after_cutoff = 0
latest = None

def estimate_avg_line_bytes(lines):
    if not lines:
        return 512
    total = 0
    for line in lines:
        try:
            total += len(line.encode("utf-8")) + 1
        except Exception:
            total += len(line) + 1
    if total <= 0:
        return 512
    return max(128, int(total / len(lines)))

def candidate_lines():
    try:
        sample_out = subprocess.check_output(
            ["tail", "-n", "256", remote_file],
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except Exception:
        sample_out = ""
    sample_lines = [ln.strip() for ln in sample_out.splitlines() if ln.strip()]
    avg_line_bytes = estimate_avg_line_bytes(sample_lines)
    try:
        file_size = os.path.getsize(remote_file)
    except Exception:
        file_size = 0
    for factor in (1.25, 2.0, 4.0):
        approx_bytes = max(1024 * 1024, int(avg_line_bytes * EXPECTED_SAMPLES * factor))
        try:
            out = subprocess.check_output(
                ["tail", "-c", str(approx_bytes), remote_file],
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
            )
        except Exception:
            continue
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            continue
        earliest_valid = None
        for line in lines:
            try:
                obj = json.loads(line)
                ts = obj.get("timestamp")
                if not ts:
                    continue
                earliest_valid = parse_dt(ts)
                break
            except Exception:
                continue
        if earliest_valid is None:
            continue
        if earliest_valid <= cutoff or (file_size and approx_bytes >= file_size):
            return lines
    return None

def parse_time(v):
    t = parse_dt(v)
    return t

def summarize(obj):
    cpu = obj.get("cpu", {})
    mem = obj.get("memory", {}).get("virtual", {})
    gpu = obj.get("gpu", {})
    gpus = gpu.get("gpus", []) if isinstance(gpu, dict) else []
    disk = obj.get("disk", {}).get("io", {})
    net = obj.get("network", {}).get("total", {})
    users = obj.get("users", {})
    top_user = users.get("top_process_user") if isinstance(users, dict) else None
    online_users = users.get("online_users") if isinstance(users, dict) else []
    ts = obj.get("timestamp")
    try:
        ts_dt = parse_dt(ts) if ts else None
        ts_fmt = fmt_ts(ts_dt, 0 if use_utc_axis else 8) if ts_dt else None
    except Exception:
        ts_fmt = ts
    return {
        "timestamp": ts_fmt,
        "cpu_model": cpu.get("model", "Unknown CPU"),
        "cpu_cores": cpu.get("physical_cores"),
        "cpu_percent": cpu.get("total_percent"),
        "cpu_temp_c": cpu.get("temperature_c"),
        "memory_total": mem.get("total"),
        "memory_used": mem.get("used"),
        "memory_percent": mem.get("percent"),
        "gpu_name": (gpus[0].get("name") if gpus else "No GPU"),
        "gpu_percent": (gpus[0].get("gpu_util") if gpus else None),
        "gpu_mem_total": (gpus[0].get("mem_total") if gpus else None),
        "gpu_mem_used": (gpus[0].get("mem_used") if gpus else None),
        "disk_read_rate": disk.get("read_rate"),
        "disk_write_rate": disk.get("write_rate"),
        "net_tx_rate": net.get("sent_rate"),
        "net_rx_rate": net.get("recv_rate"),
        "hostname": obj.get("hostname"),
        "online_users": online_users if isinstance(online_users, list) else [],
        "top_process_user": top_user if isinstance(top_user, dict) else None,
    }

lines = candidate_lines()
line_iter = lines if lines is not None else open(remote_file, "r", encoding="utf-8")
for line in line_iter:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        try:
            t = parse_time(ts)
        except Exception:
            continue
        if t < cutoff:
            continue
        total_after_cutoff += 1
        latest = obj
        if (total_after_cutoff - 1) % step == 0:
            points.append(summarize(obj))
if lines is None:
    line_iter.close()

print(json.dumps({
    "points": points,
    "latest": summarize(latest) if latest else None,
    "samples": total_after_cutoff,
}))
PY
"""
    return (
        script.replace("__REMOTE_FILE__", repr(remote_file))
        .replace("__STEP__", str(int(step)))
        .replace("__RANGE_SECONDS__", str(int(range_seconds)))
        .replace("__EXPECTED_SAMPLES__", str(int(range_seconds // SAMPLE_INTERVAL_SECONDS)))
        .replace("__USE_METRICS_UTC__", "True" if metrics_utc else "False")
    )


def _build_remote_latest_command(remote_file: str, metrics_utc: bool = False) -> str:
    script = r"""
python3 - <<'PY'
import json
import datetime
import subprocess
import os
remote_file = __REMOTE_FILE__
USE_METRICS_UTC = __USE_METRICS_UTC__
obj = None
file_age_seconds = None

try:
    mtime = os.path.getmtime(remote_file)
    # mtime is on the same (remote) host, so age seconds does not depend on timestamp timezone.
    file_age_seconds = (datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(mtime)).total_seconds()
except Exception:
    file_age_seconds = None

# Avoid scanning the entire JSONL file.
# Tail the last few lines and pick the newest valid JSON record.
# Sometimes the very last line is partially written, so we try multiple tail sizes.
def try_tail(n: int):
    buf = []
    out = subprocess.check_output(
        ["tail", "-n", str(n), remote_file],
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
    )
    for line in (out.splitlines() if out is not None else []):
        line = line.strip()
        if line:
            buf.append(line)
    for line in reversed(buf):
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if candidate and candidate.get("timestamp"):
            return candidate
    return None

is_child_path = "/children/" in remote_file
use_utc_axis = is_child_path or USE_METRICS_UTC

for n in (200, 2000):
    try:
        obj = try_tail(n)
    except Exception:
        obj = None
    if obj is not None:
        break

def parse_dt(text):
    t = (text or "").strip()
    if not t:
        raise ValueError("empty datetime")
    t = t.replace(" ", "T")
    if len(t) >= 19:
        return datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")

def fmt_with_offset(dt, offset_hours: int):
    # metrics.jsonl timestamps are naive datetimes; we just label them with the correct offset.
    sign = "+" if offset_hours >= 0 else "-"
    abs_h = abs(offset_hours)
    # Avoid f-strings for compatibility with older python3 versions on remote hosts.
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "{0}{1:02d}:00".format(sign, abs_h)

if obj is None:
    if use_utc_axis:
        remote_now = fmt_with_offset(datetime.datetime.utcnow(), 0)
    else:
        remote_now = fmt_with_offset(datetime.datetime.now(), 8)
    print(json.dumps({"latest": None, "remote_now": remote_now, "file_age_seconds": file_age_seconds}))
else:
    cpu = obj.get("cpu", {})
    mem = obj.get("memory", {}).get("virtual", {})
    gpu = obj.get("gpu", {})
    gpus = gpu.get("gpus", []) if isinstance(gpu, dict) else []
    users = obj.get("users", {})
    top_user = users.get("top_process_user") if isinstance(users, dict) else None
    online_users = users.get("online_users") if isinstance(users, dict) else []
    ts = obj.get("timestamp")
    try:
        ts_dt = parse_dt(ts) if ts else None
        # UTC-naive vs local-naive: same rule as range aggregation (use_utc_axis).
        ts_fmt = fmt_with_offset(ts_dt, 0) if ts_dt and use_utc_axis else (fmt_with_offset(ts_dt, 8) if ts_dt else None)
    except Exception:
        ts_fmt = ts
    print(json.dumps({
        # Keep remote_now consistent with `timestamp`'s basis for offline calculations.
        "remote_now": (fmt_with_offset(datetime.datetime.utcnow(), 0) if use_utc_axis else fmt_with_offset(datetime.datetime.now(), 8)),
        "file_age_seconds": file_age_seconds,
        "latest": {
            "timestamp": ts_fmt,
            "hostname": obj.get("hostname"),
            "cpu_model": cpu.get("model", "Unknown CPU"),
            "cpu_cores": cpu.get("physical_cores"),
            "cpu_percent": cpu.get("total_percent"),
            "cpu_temp_c": cpu.get("temperature_c"),
            "memory_total": mem.get("total"),
            "memory_used": mem.get("used"),
            "memory_percent": mem.get("percent"),
            "gpu_name": (gpus[0].get("name") if gpus else "No GPU"),
            "gpu_percent": (gpus[0].get("gpu_util") if gpus else None),
            "gpu_mem_total": (gpus[0].get("mem_total") if gpus else None),
            "gpu_mem_used": (gpus[0].get("mem_used") if gpus else None),
            "disk_read_rate": obj.get("disk", {}).get("io", {}).get("read_rate"),
            "disk_write_rate": obj.get("disk", {}).get("io", {}).get("write_rate"),
            "net_tx_rate": obj.get("network", {}).get("total", {}).get("sent_rate"),
            "net_rx_rate": obj.get("network", {}).get("total", {}).get("recv_rate"),
            "online_users": online_users if isinstance(online_users, list) else [],
            "top_process_user": top_user if isinstance(top_user, dict) else None,
        }
    }))
PY
"""
    return (
        script.replace("__REMOTE_FILE__", repr(remote_file)).replace(
            "__USE_METRICS_UTC__", "True" if metrics_utc else "False"
        )
    )


def _build_remote_disks_command(remote_file: str) -> str:
    # Important:
    # - For child devices, the SSH target in `devices.yaml` is the aggregate collector host,
    #   while `remote_file` points to the *child's* forwarded metrics.jsonl on the mother.
    # - Therefore, we must NOT use `df/lsblk` from the SSH host; we should extract disk usage
    #   from the forwarded snapshot itself (`disk.partitions`).
    script = r"""
python3 - <<'PY'
import json
import subprocess

remote_file = __REMOTE_FILE__

last = None
try:
    out = subprocess.check_output(
        ["tail", "-n", "50", remote_file],
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
    )
except Exception:
    out = ""

for line in reversed(out.splitlines() if out is not None else []):
    line = line.strip()
    if not line:
        continue
    try:
        json.loads(line)
    except Exception:
        continue
    last = line
    break

if not last:
    with open(remote_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line

if not last:
    print(json.dumps({"disks": [], "count": 0}))
    raise SystemExit(0)

obj = json.loads(last)
disk = obj.get("disk", {}) or {}
parts = disk.get("partitions", []) or []

def base_dev(dev):
    if not isinstance(dev, str):
        return None
    if dev.startswith("/dev/"):
        return dev[len("/dev/"):]
    return dev

disks = []
for p in parts:
    dev = p.get("device")
    mount = p.get("mount") or p.get("mountpoint")
    total = p.get("total")
    used = p.get("used")
    percent = p.get("percent")

    name = base_dev(dev) or mount or "unknown"
    size = int(total or 0)
    # Frontend no need to display tiny "disks" (e.g. loop devices / snapshots).
    # Filter by total capacity, using 2GiB = 2 * 1024^3 bytes.
    if size < (2 * (1024 ** 3)):
        continue
    used_i = int(used or 0)
    free = None
    if total is not None and used is not None:
        free = int(total - used)

    disks.append({
        # Treat each partition as a "disk" for UI compatibility (/dev/<name>).
        "name": name,
        "size": size,
        "used": used_i,
        "free": free,
        "percent": float(percent) if percent is not None else None,
        "partitions": [{"name": name, "mountpoint": mount}],
    })

print(json.dumps({"disks": disks, "count": len(disks)}))
PY
"""
    return script.replace("__REMOTE_FILE__", repr(remote_file))


def _wrap_remote_command_for_writable_tmp(device: DeviceConfig, command: str) -> str:
    """Remote bash uses heredocs (<<PY); they need a writable TMPDIR. If / is ro, /tmp fails."""
    parent = posixpath.dirname(device.remote_file)
    if not parent or parent == "/":
        parent = "/var/tmp"
    tmpdir = posixpath.join(parent, ".hwmon_dashboard_tmp")
    qdir = shlex.quote(tmpdir)
    qcmd = shlex.quote(command)
    return f"mkdir -p {qdir} && TMPDIR={qdir} bash -lc {qcmd}"


def _run_ssh_command(device: DeviceConfig, command: str, timeout: float = 20.0) -> str:
    def _connect_client(
        *,
        hostname: str,
        port: int,
        username: str,
        key_file: str | None,
        password: str | None,
        sock: Any | None = None,
    ) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {"hostname": hostname, "port": port, "username": username, "timeout": timeout}
        if key_file:
            kwargs["key_filename"] = str(Path(key_file).expanduser())
        if password:
            kwargs["password"] = password
        if sock is not None:
            kwargs["sock"] = sock
        c.connect(**kwargs)
        return c

    jump_client: paramiko.SSHClient | None = None
    target_client: paramiko.SSHClient | None = None
    channel: Any | None = None
    try:
        if device.jump_host:
            jump_user = device.jump_user or device.user
            jump_port = device.jump_port or 22
            jump_key = device.jump_key_file or device.key_file
            jump_pass = device.jump_password or device.password

            jump_client = _connect_client(
                hostname=device.jump_host,
                port=jump_port,
                username=jump_user,
                key_file=jump_key,
                password=jump_pass,
            )

            transport = jump_client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("jump transport not active")

            # Open tunnel from jump host to target host:port
            channel = transport.open_channel("direct-tcpip", (device.host, device.port), ("127.0.0.1", 0))
            target_client = _connect_client(
                hostname=device.host,
                port=device.port,
                username=device.user,
                key_file=device.key_file,
                password=device.password,
                sock=channel,
            )
        else:
            target_client = _connect_client(
                hostname=device.host,
                port=device.port,
                username=device.user,
                key_file=device.key_file,
                password=device.password,
            )

        wrapped = _wrap_remote_command_for_writable_tmp(device, command)
        stdin, stdout, stderr = target_client.exec_command(wrapped, timeout=timeout)
        try:
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
        finally:
            for stream in (stdin, stdout, stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        if err:
            raise RuntimeError(err)
        if not out:
            raise RuntimeError("empty response from remote command")
        return out
    finally:
        try:
            if target_client:
                target_client.close()
        finally:
            try:
                if channel is not None:
                    channel.close()
            finally:
                if jump_client:
                    jump_client.close()


def fetch_range(device: DeviceConfig, range_key: str) -> dict[str, Any]:
    cached = _range_cache_get(device.device_id, range_key)
    if cached is not None:
        return cached
    if device.local:
        data = fetch_range_local(device, range_key)
        _range_cache_put(device.device_id, range_key, data)
        return data
    if range_key not in RANGE_SETTINGS:
        raise ValueError(f"unsupported range: {range_key}")
    settings = RANGE_SETTINGS[range_key]
    # monitored hosts write `timestamp` without timezone information (see monitor.py: datetime.now()).
    # The dashboard must compute cutoff in the same "naive local time" basis to avoid range drift.
    total_samples = settings["seconds"] // SAMPLE_INTERVAL_SECONDS
    step = max(1, int(total_samples // settings["max_points"]))
    command = _build_remote_range_command(
        device.remote_file, settings["seconds"], step, device.metrics_utc
    )
    output = _run_ssh_command(device, command, timeout=RANGE_FETCH_TIMEOUT_SECONDS.get(range_key, 20.0))
    data = json.loads(output)
    data.update({"device_id": device.device_id, "device_name": device.name, "range": range_key, "step": step})
    _range_cache_put(device.device_id, range_key, data)
    return data


def fetch_latest(device: DeviceConfig) -> dict[str, Any]:
    if device.local:
        return fetch_latest_local(device)
    command = _build_remote_latest_command(device.remote_file, device.metrics_utc)
    output = _run_ssh_command(device, command)
    data = json.loads(output)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "latest": data.get("latest"),
        "remote_now": data.get("remote_now"),
        "file_age_seconds": data.get("file_age_seconds"),
    }


def fetch_latest_all(devices: list[DeviceConfig], max_workers: int = 8) -> list[dict[str, Any]]:
    def group_key(dev: DeviceConfig) -> tuple[Any, ...]:
        if dev.local:
            return ("local", dev.device_id)
        return ("ssh", dev.host, dev.port, dev.user, dev.jump_host, dev.jump_port)

    def fetch_group(group_devices: list[DeviceConfig]) -> list[dict[str, Any]]:
        group_results: list[dict[str, Any]] = []
        for dev in group_devices:
            try:
                group_results.append(fetch_latest(dev))
            except Exception as exc:
                group_results.append(
                    {
                        "device_id": dev.device_id,
                        "device_name": dev.name,
                        "latest": None,
                        "error": str(exc),
                    }
                )
        return group_results

    groups: dict[tuple[Any, ...], list[DeviceConfig]] = {}
    for dev in devices:
        groups.setdefault(group_key(dev), []).append(dev)

    results: list[dict[str, Any]] = []
    worker_count = max(1, min(max_workers, len(groups)))
    if worker_count == 1:
        for group in groups.values():
            results.extend(fetch_group(group))
        results.sort(key=lambda x: x["device_id"])
        return results

    pool = _get_latest_executor()
    futures = {pool.submit(fetch_group, group): group_id for group_id, group in groups.items()}
    for future in as_completed(futures):
        try:
            results.extend(future.result())
        except Exception as exc:
            results.append(
                {
                    "device_id": f"group:{futures[future]}",
                    "device_name": f"group:{futures[future]}",
                    "latest": None,
                    "error": str(exc),
                }
            )
    results.sort(key=lambda x: x["device_id"])
    return results


def fetch_disks(device: DeviceConfig) -> dict[str, Any]:
    if device.local:
        return fetch_disks_local(device)
    command = _build_remote_disks_command(device.remote_file)
    output = _run_ssh_command(device, command)
    data = json.loads(output)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "count": data.get("count", 0),
        "disks": data.get("disks", []),
    }
