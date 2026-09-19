import asyncio
import json
import math
import os
import sqlite3
import time
from collections import deque
from pathlib import Path

import psutil
import pynvml
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Local AI Telemetry", version="0.2.0")
SAMPLE_INTERVAL = float(os.getenv("TELEMETRY_INTERVAL", "1.0"))
DISK_PATH = Path(os.getenv("TELEMETRY_DISK_PATH", "/workspace"))
HISTORY_DB_PATH = Path(os.getenv("TELEMETRY_HISTORY_DB", "/state/telemetry-history.sqlite3"))
PERSIST_INTERVAL = max(10, int(os.getenv("TELEMETRY_PERSIST_INTERVAL", "60")))
HISTORY_RETENTION_SECONDS = max(
    3600, int(os.getenv("TELEMETRY_HISTORY_RETENTION_SECONDS", str(7 * 24 * 3600)))
)
LIVE_HISTORY_SECONDS = 3600
history = deque(maxlen=max(3600, int(LIVE_HISTORY_SECONDS / max(SAMPLE_INTERVAL, 0.1))))
latest = {}
sampler_task = None
last_persist_bucket = None
history_error = None


def read_gpu():
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
    memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
    name = pynvml.nvmlDeviceGetName(handle)
    if isinstance(name, bytes):
        name = name.decode(errors="replace")
    return {
        "name": str(name),
        "utilization_percent": float(util.gpu),
        "vram_used_mib": round(memory.used / 1024 / 1024, 1),
        "vram_total_mib": round(memory.total / 1024 / 1024, 1),
        "temperature_c": float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)),
        "power_w": round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 2),
        "power_limit_w": round(pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0, 2),
        "core_clock_mhz": float(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)),
        "memory_clock_mhz": float(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)),
    }


def read_system():
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(str(DISK_PATH))
    net = psutil.net_io_counters()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_used_mib": round((vm.total - vm.available) / 1024 / 1024, 1),
        "ram_total_mib": round(vm.total / 1024 / 1024, 1),
        "ram_percent": vm.percent,
        "swap_used_mib": round(swap.used / 1024 / 1024, 1),
        "swap_total_mib": round(swap.total / 1024 / 1024, 1),
        "disk_used_gib": round(disk.used / 1024 / 1024 / 1024, 2),
        "disk_total_gib": round(disk.total / 1024 / 1024 / 1024, 2),
        "disk_free_gib": round(disk.free / 1024 / 1024 / 1024, 2),
        "network_sent_mib": round(net.bytes_sent / 1024 / 1024, 1),
        "network_recv_mib": round(net.bytes_recv / 1024 / 1024, 1),
    }


def collect_sample():
    now = time.time()
    sample = {"timestamp": now}
    try:
        sample["gpu"] = read_gpu()
        sample["gpu_error"] = None
    except Exception as exc:
        sample["gpu"] = None
        sample["gpu_error"] = str(exc)

    try:
        sample["system"] = read_system()
        sample["system_error"] = None
    except Exception as exc:
        sample["system"] = None
        sample["system_error"] = str(exc)
    return sample


def history_db():
    HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(HISTORY_DB_PATH, timeout=5)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            bucket INTEGER PRIMARY KEY,
            timestamp REAL NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    return connection


def initialize_history_db():
    with history_db():
        pass


def persist_sample(sample):
    global history_error
    bucket = int(float(sample["timestamp"]) // PERSIST_INTERVAL)
    cutoff = time.time() - HISTORY_RETENTION_SECONDS
    try:
        with history_db() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO samples(bucket, timestamp, payload) VALUES (?, ?, ?)",
                (bucket, float(sample["timestamp"]), json.dumps(sample, separators=(",", ":"))),
            )
            connection.execute("DELETE FROM samples WHERE timestamp < ?", (cutoff,))
        history_error = None
    except Exception as exc:
        history_error = str(exc)


def persisted_samples(cutoff):
    global history_error
    if not HISTORY_DB_PATH.exists():
        return []
    try:
        with history_db() as connection:
            rows = connection.execute(
                "SELECT payload FROM samples WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()
        history_error = None
        return [json.loads(row[0]) for row in rows]
    except Exception as exc:
        history_error = str(exc)
        return []


def downsample(samples, max_points):
    if len(samples) <= max_points:
        return samples
    step = max(1, math.ceil(len(samples) / max_points))
    reduced = samples[::step]
    if reduced[-1].get("timestamp") != samples[-1].get("timestamp"):
        reduced.append(samples[-1])
    return reduced


async def sampler():
    global latest, last_persist_bucket
    while True:
        latest = await asyncio.to_thread(collect_sample)
        history.append(latest)
        bucket = int(float(latest["timestamp"]) // PERSIST_INTERVAL)
        if bucket != last_persist_bucket:
            last_persist_bucket = bucket
            await asyncio.to_thread(persist_sample, latest)
        await asyncio.sleep(SAMPLE_INTERVAL)


@app.on_event("startup")
async def startup():
    global sampler_task, latest, last_persist_bucket
    pynvml.nvmlInit()
    psutil.cpu_percent(interval=None)
    await asyncio.to_thread(initialize_history_db)
    latest = await asyncio.to_thread(collect_sample)
    history.append(latest)
    last_persist_bucket = int(float(latest["timestamp"]) // PERSIST_INTERVAL)
    await asyncio.to_thread(persist_sample, latest)
    sampler_task = asyncio.create_task(sampler())


@app.on_event("shutdown")
async def shutdown():
    if sampler_task:
        sampler_task.cancel()
    try:
        pynvml.nvmlShutdown()
    except Exception:
        pass


@app.get("/health")
def health():
    ok = latest.get("gpu") is not None
    return {
        "status": "ok" if ok else "degraded",
        "version": app.version,
        "history_persistent": history_error is None,
        "history_error": history_error,
    }


@app.get("/snapshot")
def snapshot():
    return latest


@app.get("/history")
def get_history(seconds: int = 120, max_points: int = 900):
    seconds = max(5, min(seconds, HISTORY_RETENTION_SECONDS))
    max_points = max(60, min(max_points, 2000))
    cutoff = time.time() - seconds
    live_samples = [sample for sample in history if sample.get("timestamp", 0) >= cutoff]

    live_start = live_samples[0].get("timestamp", 0) if live_samples else None
    live_covers_window = bool(
        live_start is not None
        and live_start <= cutoff + max(PERSIST_INTERVAL, SAMPLE_INTERVAL * 2)
    )

    if seconds <= LIVE_HISTORY_SECONDS and live_covers_window:
        samples = live_samples
    elif seconds <= LIVE_HISTORY_SECONDS:
        persisted = persisted_samples(cutoff)
        cutoff_for_live = (live_start or time.time()) - SAMPLE_INTERVAL
        older = [sample for sample in persisted if sample.get("timestamp", 0) < cutoff_for_live]
        samples = older + live_samples
    else:
        persisted = persisted_samples(cutoff)
        by_bucket = {
            int(float(sample.get("timestamp", 0)) // PERSIST_INTERVAL): sample
            for sample in persisted
        }
        for sample in live_samples:
            bucket = int(float(sample.get("timestamp", 0)) // PERSIST_INTERVAL)
            by_bucket[bucket] = sample
        samples = sorted(by_bucket.values(), key=lambda sample: sample.get("timestamp", 0))

    return {
        "samples": downsample(samples, max_points),
        "retention_seconds": HISTORY_RETENTION_SECONDS,
        "persistent": history_error is None,
        "history_error": history_error,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    gpu = latest.get("gpu") or {}
    system = latest.get("system") or {}
    lines = [
        "# TYPE localai_gpu_utilization_percent gauge",
        f"localai_gpu_utilization_percent {gpu.get('utilization_percent', 0)}",
        "# TYPE localai_gpu_vram_used_mib gauge",
        f"localai_gpu_vram_used_mib {gpu.get('vram_used_mib', 0)}",
        "# TYPE localai_gpu_temperature_c gauge",
        f"localai_gpu_temperature_c {gpu.get('temperature_c', 0)}",
        "# TYPE localai_gpu_power_w gauge",
        f"localai_gpu_power_w {gpu.get('power_w', 0)}",
        "# TYPE localai_system_cpu_percent gauge",
        f"localai_system_cpu_percent {system.get('cpu_percent', 0)}",
        "# TYPE localai_system_ram_percent gauge",
        f"localai_system_ram_percent {system.get('ram_percent', 0)}",
        "# TYPE localai_disk_free_gib gauge",
        f"localai_disk_free_gib {system.get('disk_free_gib', 0)}",
    ]
    return "\n".join(lines) + "\n"
