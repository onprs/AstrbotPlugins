#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STATE_PATH = Path(os.environ.get("ASTRBOT_PROBE_STATE", "/tmp/astrbot_server_probe_state.json"))


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def read_first_line(path: str) -> str:
    text = read_text(path)
    return text.splitlines()[0].strip() if text else ""


def now_ts() -> float:
    return time.time()


def read_cpu_times() -> tuple[int, int]:
    line = read_first_line("/proc/stat")
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return 0, 0
    values = [int(value) for value in parts[1:] if value.isdigit()]
    idle = (values[3] if len(values) > 3 else 0) + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def cpu_model() -> str:
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[-1].strip()
    return platform.processor() or ""


def read_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            result[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    return result


def read_network_bytes() -> tuple[int, int]:
    rx = 0
    tx = 0
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        iface, raw = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo":
            continue
        parts = raw.split()
        if len(parts) < 16:
            continue
        rx += int(parts[0])
        tx += int(parts[8])
    return rx, tx


def sector_size(device: str) -> int:
    value = read_first_line(f"/sys/block/{device}/queue/hw_sector_size")
    try:
        return int(value)
    except ValueError:
        return 512


def read_disk_sectors() -> tuple[int, int]:
    read_sectors = 0
    write_sectors = 0
    for line in read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if name.startswith(("loop", "ram", "fd")):
            continue
        if any(name.startswith(prefix) for prefix in ("dm-", "md")):
            continue
        try:
            reads = int(parts[5])
            writes = int(parts[9])
        except ValueError:
            continue
        size = sector_size(name)
        read_sectors += reads * size
        write_sectors += writes * size
    return read_sectors, write_sectors


def read_uptime() -> float:
    text = read_first_line("/proc/uptime")
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return 0.0


def os_release() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in read_text("/etc/os-release").splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key] = raw.strip().strip('"')
    return values.get("PRETTY_NAME", platform.platform()), values.get("VERSION_ID", "")


def storage_mounts() -> list[dict[str, Any]]:
    mounts = []
    seen: set[str] = set()
    for line in read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fs_type = parts[:3]
        if mount in seen:
            continue
        if fs_type in {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "overlay", "squashfs", "cgroup", "cgroup2", "tracefs", "securityfs", "debugfs", "pstore", "bpf"}:
            continue
        if not device.startswith("/"):
            continue
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        seen.add(mount)
        mounts.append(
            {
                "mount": mount,
                "device": device,
                "fs": fs_type,
                "used_bytes": usage.used,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            },
        )
    if not mounts:
        usage = shutil.disk_usage("/")
        mounts.append(
            {
                "mount": "/",
                "device": "/",
                "fs": "unknown",
                "used_bytes": usage.used,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            },
        )
    return mounts


def load_previous() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_current(snapshot: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
        pass


def rate(current: float, previous: float, seconds: float) -> float:
    if seconds <= 0 or current < previous:
        return 0.0
    return (current - previous) / seconds


def build_payload(location: str, remark: str) -> dict[str, Any]:
    ts = now_ts()
    cpu_idle, cpu_total = read_cpu_times()
    mem = read_meminfo()
    rx_bytes, tx_bytes = read_network_bytes()
    read_bytes, write_bytes = read_disk_sectors()
    previous = load_previous()
    seconds = max(0.001, ts - float(previous.get("timestamp", ts) or ts))

    previous_cpu_total = int(previous.get("cpu_total", cpu_total) or cpu_total)
    previous_cpu_idle = int(previous.get("cpu_idle", cpu_idle) or cpu_idle)
    total_delta = cpu_total - previous_cpu_total
    idle_delta = cpu_idle - previous_cpu_idle
    cpu_percent = 0.0
    if total_delta > 0:
        cpu_percent = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100.0))

    total_mem = mem.get("MemTotal", 0)
    available_mem = mem.get("MemAvailable", 0)
    used_mem = max(0, total_mem - available_mem)
    os_name, os_version = os_release()

    current = {
        "timestamp": ts,
        "cpu_total": cpu_total,
        "cpu_idle": cpu_idle,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
    }
    save_current(current)

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "hostname": socket.gethostname(),
        "remark": remark,
        "cpu": {
            "usage_percent": round(cpu_percent, 2),
            "cores": os.cpu_count() or 0,
            "model": cpu_model(),
        },
        "memory": {
            "used_bytes": used_mem,
            "total_bytes": total_mem,
            "available_bytes": available_mem,
        },
        "storage": storage_mounts(),
        "network": {
            "rx_bps": round(rate(rx_bytes, float(previous.get("rx_bytes", rx_bytes)), seconds), 2),
            "tx_bps": round(rate(tx_bytes, float(previous.get("tx_bytes", tx_bytes)), seconds), 2),
            "rx_total_bytes": rx_bytes,
            "tx_total_bytes": tx_bytes,
        },
        "disk_io": {
            "read_bps": round(rate(read_bytes, float(previous.get("read_bytes", read_bytes)), seconds), 2),
            "write_bps": round(rate(write_bytes, float(previous.get("write_bytes", write_bytes)), seconds), 2),
        },
        "location": location or "",
        "system": {
            "os": os_name,
            "version": os_version,
            "kernel": platform.release(),
            "arch": platform.machine(),
        },
        "uptime_seconds": int(read_uptime()),
    }


class ProbeHandler(BaseHTTPRequestHandler):
    token = ""
    location = ""
    remark = ""

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/metrics", "/probe"}:
            self.send_error(404, "not found")
            return
        if self.token:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {self.token}"
            if auth != expected and self.headers.get("X-Probe-Token", "") != self.token:
                self.send_error(401, "unauthorized")
                return
        payload = build_payload(self.location, self.remark)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="AstrBot server monitor Linux probe")
    parser.add_argument("--host", default=os.environ.get("ASTRBOT_PROBE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASTRBOT_PROBE_PORT", "9810")))
    parser.add_argument("--token", default=os.environ.get("ASTRBOT_PROBE_TOKEN", ""))
    parser.add_argument("--location", default=os.environ.get("ASTRBOT_PROBE_LOCATION", ""))
    parser.add_argument("--remark", default=os.environ.get("ASTRBOT_PROBE_REMARK", ""))
    parser.add_argument("--once", action="store_true", help="print one JSON payload and exit")
    args = parser.parse_args()

    ProbeHandler.token = args.token
    ProbeHandler.location = args.location
    ProbeHandler.remark = args.remark

    if args.once:
        print(json.dumps(build_payload(args.location, args.remark), ensure_ascii=False, indent=2))
        return

    server = ThreadingHTTPServer((args.host, args.port), ProbeHandler)
    print(f"astrbot server probe listening on http://{args.host}:{args.port}/metrics", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
