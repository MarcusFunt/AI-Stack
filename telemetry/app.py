import asyncio
import os
import time
from collections import deque
from pathlib import Path

import psutil
import pynvml
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Local AI Telemetry", version="0.1.0")
SAMPLE_INTERVAL = float(os.getenv("TELEMETRY_INTERVAL", "1.0"))
DISK_PATH = Path(os.getenv("TELEMETRY_DISK_PATH", "/workspace"))
history = deque(maxlen=3600)
latest = {}
sampler_task = None

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


async def sampler():
    global latest
    while True:
        latest = await asyncio.to_thread(collect_sample)
        history.append(latest)
        await asyncio.sleep(SAMPLE_INTERVAL)


@app.on_event("startup")
async def startup():
    global sampler_task, latest
    pynvml.nvmlInit()
    psutil.cpu_percent(interval=None)
    latest = await asyncio.to_thread(collect_sample)
    history.append(latest)
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
    return {"status": "ok" if ok else "degraded", "version": app.version}

@app.get("/snapshot")
def snapshot():
    return latest


@app.get("/history")
def get_history(seconds: int = 120):
    seconds = max(5, min(seconds, 3600))
    cutoff = time.time() - seconds
    return {"samples": [sample for sample in history if sample.get("timestamp", 0) >= cutoff]}


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
