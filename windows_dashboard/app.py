from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
from flask import Flask, jsonify, render_template, request

from collector import DeviceConfig, DeviceRegistry, fetch_disks, fetch_latest_all, fetch_range

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "devices.yaml"
PROCESS_MEMORY_LOG = BASE_DIR / "process_memory.jsonl"
PROCESS_MEMORY_INTERVAL_SECONDS = 600
STATUS_HISTORY_CACHE = Path(
    os.getenv("DASHBOARD_STATUS_HISTORY_CACHE", str(BASE_DIR / "status_history_cache.json"))
)
STATUS_HISTORY_BUCKET_COUNT = 96
STATUS_HISTORY_BUCKET_SECONDS = 15 * 60
STATUS_HISTORY_CACHE_TTL_SECONDS = max(
    STATUS_HISTORY_BUCKET_SECONDS,
    int(os.getenv("DASHBOARD_STATUS_HISTORY_CACHE_TTL_SECONDS", str(STATUS_HISTORY_BUCKET_SECONDS))),
)
STATUS_HISTORY_WORKERS = max(1, int(os.getenv("DASHBOARD_STATUS_HISTORY_WORKERS", "4")))
STATUS_OFFLINE_AFTER_SECONDS = 60

STATUS_METRICS = [
    {"key": "cpu", "label": "CPU", "field": "cpu_percent", "unit": "%"},
    {"key": "temp", "label": "Temp", "field": "cpu_temp_c", "unit": "C"},
    {"key": "memory", "label": "Memory", "field": "memory_percent", "unit": "%"},
    {"key": "disk_read", "label": "Disk Read", "field": "disk_read_rate", "unit": "B/s"},
    {"key": "disk_write", "label": "Disk Write", "field": "disk_write_rate", "unit": "B/s"},
]


def _device_group_rank(device_id: str, device_name: str) -> tuple[int, str]:
    text = f"{device_id} {device_name}".lower()
    if text.startswith("cpu") or " cpu" in text:
        return (0, device_name.lower())
    if text.startswith("gpu") or " gpu" in text:
        return (1, device_name.lower())
    if "cluster" in text or "edge" in text:
        return (2, device_name.lower())
    return (3, device_name.lower())

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))
registry = DeviceRegistry(os.getenv("DASHBOARD_CONFIG", str(DEFAULT_CONFIG)))
status_history_lock = threading.Lock()


def start_process_memory_logger(interval_seconds: int = PROCESS_MEMORY_INTERVAL_SECONDS) -> None:
    process = psutil.Process(os.getpid())

    def run() -> None:
        while True:
            try:
                with PROCESS_MEMORY_LOG.open("a", encoding="utf-8") as f:
                    info = process.memory_info()
                    payload = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "pid": process.pid,
                        "rss_bytes": info.rss,
                        "vms_bytes": info.vms,
                        "memory_percent": process.memory_percent(),
                    }
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception as exc:
                print(f"Process memory logger failed: {exc}")
            time.sleep(interval_seconds)

    threading.Thread(target=run, name="process-memory-logger", daemon=True).start()


start_process_memory_logger()

if os.getenv("DASHBOARD_ENABLE_ALERTER", "1").strip() != "0":
    try:
        from alerter import start_alerter
        start_alerter(registry)
    except ImportError as e:
        print(f"Alerter module could not be started: {e}")

@app.route("/")
def index():
    return render_template("index.html")


def _parse_api_timestamp(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_level(metric_key: str, value: float | None) -> str:
    if value is None:
        return "empty"
    if metric_key == "cpu":
        if value >= 80:
            return "danger"
        if value >= 50:
            return "warning"
        if value >= 1:
            return "ok"
        return "quiet"
    if metric_key == "memory":
        if value >= 90:
            return "danger"
        if value >= 70:
            return "warning"
        if value >= 1:
            return "ok"
        return "quiet"
    if metric_key == "temp":
        if value >= 80:
            return "danger"
        if value >= 60:
            return "warning"
        if value >= 1:
            return "ok"
        return "quiet"
    if metric_key in {"disk_read", "disk_write"}:
        mbps = value / 1024 / 1024
        if mbps >= 500:
            return "danger"
        if mbps >= 100:
            return "warning"
        if mbps >= 1:
            return "ok"
        return "quiet"
    return "ok"


def _latest_status(latest_item: dict[str, Any] | None) -> dict[str, str]:
    if not latest_item:
        return {"state": "unknown", "label": "Unknown"}
    if latest_item.get("error"):
        return {"state": "connection_failed", "label": "Connection Failed"}
    latest = latest_item.get("latest")
    if not latest:
        return {"state": "no_data", "label": "No Data"}
    age = _num(latest_item.get("file_age_seconds"))
    if age is not None and age >= STATUS_OFFLINE_AFTER_SECONDS:
        return {"state": "offline", "label": "Offline"}
    return {"state": "online", "label": "Online"}


def _device_alerts(device_name: str, latest_item: dict[str, Any] | None) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    status = _latest_status(latest_item)
    if status["state"] == "connection_failed":
        alerts.append(
            {
                "severity": "critical",
                "device": device_name,
                "message": str((latest_item or {}).get("error") or "Connection failed"),
            }
        )
        return alerts
    if status["state"] in {"offline", "no_data", "unknown"}:
        alerts.append(
            {
                "severity": "critical" if status["state"] == "offline" else "warning",
                "device": device_name,
                "message": status["label"],
            }
        )

    latest = (latest_item or {}).get("latest") or {}
    cpu = _num(latest.get("cpu_percent"))
    temp = _num(latest.get("cpu_temp_c"))
    memory = _num(latest.get("memory_percent"))
    if cpu is not None and cpu >= 95:
        alerts.append({"severity": "warning", "device": device_name, "message": f"CPU {cpu:.1f}%"})
    if temp is not None and temp >= 85:
        alerts.append({"severity": "critical", "device": device_name, "message": f"Temp {temp:.1f} C"})
    elif temp is not None and temp >= 75:
        alerts.append({"severity": "warning", "device": device_name, "message": f"Temp {temp:.1f} C"})
    if memory is not None and memory >= 90:
        alerts.append({"severity": "critical", "device": device_name, "message": f"Memory {memory:.1f}%"})
    return alerts


def _empty_metric_rows() -> list[dict[str, Any]]:
    return [
        {
            "key": metric["key"],
            "label": metric["label"],
            "unit": metric["unit"],
            "bars": [{"value": None, "level": "empty"} for _ in range(STATUS_HISTORY_BUCKET_COUNT)],
        }
        for metric in STATUS_METRICS
    ]


def _aggregate_status_rows(points: list[dict[str, Any]], start_ts: int) -> list[dict[str, Any]]:
    sums: dict[str, list[float]] = {
        metric["key"]: [0.0 for _ in range(STATUS_HISTORY_BUCKET_COUNT)] for metric in STATUS_METRICS
    }
    counts: dict[str, list[int]] = {
        metric["key"]: [0 for _ in range(STATUS_HISTORY_BUCKET_COUNT)] for metric in STATUS_METRICS
    }

    for point in points:
        ts = _parse_api_timestamp(point.get("timestamp"))
        if ts is None:
            continue
        idx = int((ts - start_ts) // STATUS_HISTORY_BUCKET_SECONDS)
        if idx < 0 or idx >= STATUS_HISTORY_BUCKET_COUNT:
            continue
        for metric in STATUS_METRICS:
            value = _num(point.get(metric["field"]))
            if value is None:
                continue
            key = metric["key"]
            sums[key][idx] += value
            counts[key][idx] += 1

    rows: list[dict[str, Any]] = []
    for metric in STATUS_METRICS:
        key = metric["key"]
        bars = []
        for i in range(STATUS_HISTORY_BUCKET_COUNT):
            value = sums[key][i] / counts[key][i] if counts[key][i] else None
            bars.append({"value": value, "level": _metric_level(key, value)})
        rows.append({"key": key, "label": metric["label"], "unit": metric["unit"], "bars": bars})
    return rows


def _read_status_history_cache(force: bool, cache_slot: int | None = None) -> dict[str, Any] | None:
    if force:
        return None
    try:
        age = time.time() - STATUS_HISTORY_CACHE.stat().st_mtime
    except OSError:
        return None
    if age > STATUS_HISTORY_CACHE_TTL_SECONDS:
        return None
    try:
        data = json.loads(STATUS_HISTORY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if cache_slot is not None and data.get("cache_slot") != cache_slot:
        return None
    data["cache_hit"] = True
    return data


def _write_status_history_cache(data: dict[str, Any]) -> None:
    try:
        STATUS_HISTORY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_HISTORY_CACHE.with_suffix(STATUS_HISTORY_CACHE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_HISTORY_CACHE)
    except Exception as exc:
        print(f"Failed to write status history cache: {exc}")


def _build_device_status_history(
    device: DeviceConfig, latest_item: dict[str, Any] | None, start_ts: int
) -> dict[str, Any]:
    device_name = (latest_item or {}).get("device_name") or device.name
    try:
        history = fetch_range(device, "1d")
        rows = _aggregate_status_rows(history.get("points") or [], start_ts)
        history_error = None
    except Exception as exc:
        rows = _empty_metric_rows()
        history_error = str(exc)

    status = _latest_status(latest_item)
    alerts = _device_alerts(device_name, latest_item)
    if history_error and not alerts:
        alerts.append({"severity": "warning", "device": device_name, "message": history_error})

    return {
        "device_id": device.device_id,
        "device_name": device_name,
        "status": status["state"],
        "status_label": status["label"],
        "latest": (latest_item or {}).get("latest"),
        "file_age_seconds": (latest_item or {}).get("file_age_seconds"),
        "error": (latest_item or {}).get("error") or history_error,
        "alerts": alerts,
        "metrics": rows,
    }


def build_status_history(force: bool = False) -> dict[str, Any]:
    now_ts = int(time.time())
    cache_slot = now_ts // STATUS_HISTORY_BUCKET_SECONDS

    # Keep the status overview stable inside each natural 15-minute slot.
    cached = _read_status_history_cache(False, cache_slot)
    if cached is not None:
        return cached

    with status_history_lock:
        cached = _read_status_history_cache(False, cache_slot)
        if cached is not None:
            return cached

        devices = registry.list_devices()
        # Use only completed 15-minute buckets so bar colors stay stable inside the current bucket.
        end_ts = cache_slot * STATUS_HISTORY_BUCKET_SECONDS
        start_ts = end_ts - STATUS_HISTORY_BUCKET_COUNT * STATUS_HISTORY_BUCKET_SECONDS

        latest_items = fetch_latest_all(devices, max_workers=STATUS_HISTORY_WORKERS)
        latest_by_id = {item.get("device_id"): item for item in latest_items}

        device_rows: list[dict[str, Any]] = []
        worker_count = max(1, min(STATUS_HISTORY_WORKERS, len(devices)))
        if worker_count == 1:
            for device in devices:
                device_rows.append(_build_device_status_history(device, latest_by_id.get(device.device_id), start_ts))
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="status-history") as pool:
                futures = {
                    pool.submit(
                        _build_device_status_history,
                        device,
                        latest_by_id.get(device.device_id),
                        start_ts,
                    ): device
                    for device in devices
                }
                for future in as_completed(futures):
                    device_rows.append(future.result())

        order = {device.device_id: i for i, device in enumerate(devices)}
        device_rows.sort(
            key=lambda item: (
                *_device_group_rank(item.get("device_id", ""), item.get("device_name", "")),
                order.get(item["device_id"], 9999),
            )
        )
        alerts = [alert for row in device_rows for alert in row.get("alerts", [])]

        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cache_hit": False,
            "cache_slot": cache_slot,
            "cache_ttl_seconds": STATUS_HISTORY_CACHE_TTL_SECONDS,
            "bucket_count": STATUS_HISTORY_BUCKET_COUNT,
            "bucket_seconds": STATUS_HISTORY_BUCKET_SECONDS,
            "window_seconds": STATUS_HISTORY_BUCKET_COUNT * STATUS_HISTORY_BUCKET_SECONDS,
            "start_time": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(timespec="seconds"),
            "end_time": datetime.fromtimestamp(end_ts, timezone.utc).isoformat(timespec="seconds"),
            "devices": device_rows,
            "alerts": alerts,
        }
        _write_status_history_cache(data)
        return data


@app.get("/api/devices")
def api_devices():
    devices = registry.list_devices()
    payload = [
        {
            "id": d.device_id,
            "name": d.name,
            "host": d.host,
            "user": d.user,
            "port": d.port,
            "remote_file": d.remote_file,
            "local": d.local,
        }
        for d in devices
    ]
    return jsonify({"devices": payload, "sample_interval_seconds": 10})


@app.get("/api/overview")
def api_overview():
    devices = registry.list_devices()
    data = fetch_latest_all(devices)
    return jsonify({"devices": data})


@app.get("/api/status-history")
def api_status_history():
    force = request.args.get("force", "").lower() in {"1", "true", "yes"}
    try:
        return jsonify(build_status_history(force=force))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/device/<device_id>/metrics")
def api_metrics(device_id: str):
    range_key = request.args.get("range", "1h")
    device = registry.get(device_id)
    if not device:
        return jsonify({"error": f"unknown device: {device_id}"}), 404
    try:
        data = fetch_range(device, range_key)
        return jsonify(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/device/<device_id>/disks")
def api_disks(device_id: str):
    device = registry.get(device_id)
    if not device:
        return jsonify({"error": f"unknown device: {device_id}"}), 404
    try:
        data = fetch_disks(device)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/reload")
def api_reload():
    registry.reload()
    return jsonify({"ok": True, "device_count": len(registry.list_devices())})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
