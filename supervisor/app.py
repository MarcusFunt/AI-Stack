import asyncio
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

import docker
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/models.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SERVICES = CONFIG["services"]
GPU_SERVICES = {name for name, spec in SERVICES.items() if spec.get("gpu")}
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "").strip()
if not SUPERVISOR_TOKEN:
    raise RuntimeError("SUPERVISOR_TOKEN must be set")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "").strip()
if not LLAMA_API_KEY:
    raise RuntimeError("LLAMA_API_KEY must be set")

app = FastAPI(title="Local AI GPU Supervisor", version="0.3.0")
docker_client = docker.from_env()
transition_lock = asyncio.Lock()
lease_condition = asyncio.Condition()
LEASE_STATE_PATH = Path(os.getenv("LEASE_STATE_PATH", "/state/supervisor-leases.json"))

def load_lease_state():
    try:
        payload = json.loads(LEASE_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        leases = payload.get("leases")
        if isinstance(leases, dict):
            return {
                name: {str(lease_id) for lease_id in ids if str(lease_id)}
                for name, ids in leases.items()
                if name in SERVICES and isinstance(ids, list)
            }

        # Migrate the older count-only format conservatively. These synthetic
        # IDs are cleared by gateway startup via /reset-leases.
        jobs = payload.get("active_jobs", payload)
        if not isinstance(jobs, dict):
            return {}
        migrated = {}
        for name, count in jobs.items():
            if name not in SERVICES:
                continue
            try:
                n = max(0, int(count))
            except (TypeError, ValueError):
                continue
            if n:
                migrated[name] = {f"legacy-{name}-{i}" for i in range(n)}
        return migrated
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}

def sync_active_jobs(service=None):
    if service is None:
        active_jobs.clear()
        for name, lease_ids in active_leases.items():
            if lease_ids:
                active_jobs[name] = len(lease_ids)
        return
    lease_ids = active_leases.get(service, set())
    if lease_ids:
        active_jobs[service] = len(lease_ids)
    else:
        active_jobs.pop(service, None)
        active_leases.pop(service, None)

def persist_active_jobs():
    LEASE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "leases": {
            name: sorted(lease_ids)
            for name, lease_ids in active_leases.items()
            if lease_ids
        },
        "active_jobs": {
            name: len(lease_ids)
            for name, lease_ids in active_leases.items()
            if lease_ids
        },
    }
    tmp = LEASE_STATE_PATH.with_name(LEASE_STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, LEASE_STATE_PATH)

active_leases = defaultdict(set, load_lease_state())
active_jobs = defaultdict(int)
sync_active_jobs()
lease_epoch = 0
idle_tasks = {}
idle_deadlines = {}

@app.middleware("http")
async def supervisor_auth(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    supplied = request.headers.get("x-supervisor-token", "")
    if not secrets.compare_digest(supplied, SUPERVISOR_TOKEN):
        return JSONResponse(status_code=401, content={"detail": "invalid supervisor token"})
    return await call_next(request)

def cancel_idle_stop(service: str):
    task = idle_tasks.pop(service, None)
    idle_deadlines.pop(service, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()

async def idle_stop_after(service: str, delay: int):
    try:
        idle_deadlines[service] = time.monotonic() + delay
        await asyncio.sleep(delay)
        async with lease_condition:
            if active_jobs.get(service, 0):
                return
        async with transition_lock:
            async with lease_condition:
                if active_jobs.get(service, 0):
                    return
            await stop_service(service)
    finally:
        if idle_tasks.get(service) is asyncio.current_task():
            idle_tasks.pop(service, None)
        idle_deadlines.pop(service, None)

def schedule_idle_stop(service: str):
    delay = int(SERVICES[service].get("idle_timeout", 0))
    cancel_idle_stop(service)
    if delay > 0:
        idle_tasks[service] = asyncio.create_task(idle_stop_after(service, delay))

def get_container(service: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    try:
        return docker_client.containers.get(SERVICES[service]["container"])
    except docker.errors.NotFound as exc:
        raise HTTPException(503, f"{service} container has not been created") from exc

def container_status(service: str):
    try:
        c = get_container(service)
        c.reload()
        return c.status
    except HTTPException:
        return "not-created"

def current_gpu_services():
    return [
        name for name in GPU_SERVICES
        if container_status(name) == "running"
    ]

async def stop_service(service: str):
    try:
        c = get_container(service)
    except HTTPException:
        return
    c.reload()
    if c.status == "running":
        await asyncio.to_thread(c.stop, timeout=30)

async def wait_ready(service: str):
    spec = SERVICES[service]
    base = spec.get("base")
    health = spec.get("health")
    if not base or not health:
        return
    deadline = time.monotonic() + int(spec.get("start_timeout", 180))
    startup_grace_deadline = time.monotonic() + 10
    last_error = "not checked"
    seen_running = False
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            c = get_container(service)
            c.reload()
            state = c.status
            if state == "running":
                seen_running = True
            elif state in {"exited", "dead"}:
                if seen_running or time.monotonic() >= startup_grace_deadline:
                    logs = c.logs(tail=80).decode(errors="replace")
                    raise HTTPException(503, f"{service} exited during startup\n{logs}")
                last_error = f"container state {state}"
                await asyncio.sleep(0.25)
                continue
            elif state not in {"created", "restarting"}:
                last_error = f"container state {state}"
            try:
                headers = {}
                if spec.get("backend_auth") == "llama":
                    headers["Authorization"] = f"Bearer {LLAMA_API_KEY}"
                r = await client.get(base + health, headers=headers)
                if 200 <= r.status_code < 400:
                    return
                last_error = f"HTTP {r.status_code}"
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(2)
    raise HTTPException(504, f"{service} failed readiness check: {last_error}")

async def start_and_wait_ready(service: str):
    last_exc = None
    for attempt in range(2):
        target = get_container(service)
        target.reload()
        if target.status != "running":
            await asyncio.to_thread(target.start)
        try:
            await wait_ready(service)
            return
        except HTTPException as exc:
            last_exc = exc
            target.reload()
            transient_exit = exc.status_code == 503 and target.status in {"exited", "dead"}
            if attempt == 0 and transient_exit:
                await asyncio.sleep(1)
                continue
            raise
    raise last_exc

async def prepare_service(service: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    if service not in GPU_SERVICES:
        return

    cancel_idle_stop(service)
    while True:
        async with lease_condition:
            while any(
                count for name, count in active_jobs.items()
                if name != service
            ):
                await lease_condition.wait()

        async with transition_lock:
            async with lease_condition:
                other_jobs = any(
                    count for name, count in active_jobs.items()
                    if name != service
                )
            if other_jobs:
                continue

            for other in GPU_SERVICES:
                if other != service:
                    cancel_idle_stop(other)
                    await stop_service(other)
            await start_and_wait_ready(service)
            return

async def acquire_service(service: str, lease_id: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    started_epoch = lease_epoch
    if service not in GPU_SERVICES:
        async with lease_condition:
            if started_epoch != lease_epoch:
                raise HTTPException(409, "lease reset during acquire")
            active_leases[service].add(lease_id)
            sync_active_jobs(service)
            persist_active_jobs()
            lease_condition.notify_all()
            return active_jobs[service]

    cancel_idle_stop(service)
    while True:
        async with lease_condition:
            while any(
                count for name, count in active_jobs.items()
                if name != service
            ):
                await lease_condition.wait()

        async with transition_lock:
            async with lease_condition:
                other_jobs = any(
                    count for name, count in active_jobs.items()
                    if name != service
                )
            if other_jobs:
                continue

            for other in GPU_SERVICES:
                if other != service:
                    cancel_idle_stop(other)
                    await stop_service(other)
            await start_and_wait_ready(service)

            async with lease_condition:
                if started_epoch != lease_epoch:
                    if service in GPU_SERVICES:
                        schedule_idle_stop(service)
                    raise HTTPException(409, "lease reset during acquire")
                active_leases[service].add(lease_id)
                sync_active_jobs(service)
                persist_active_jobs()
                lease_condition.notify_all()
                return active_jobs[service]

async def release_service(service: str, lease_id: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    async with lease_condition:
        active_leases[service].discard(lease_id)
        sync_active_jobs(service)
        remaining = active_jobs.get(service, 0)
        persist_active_jobs()
        lease_condition.notify_all()
    if service in GPU_SERVICES and remaining == 0:
        schedule_idle_stop(service)
    return remaining

@app.on_event("startup")
async def recover_idle_shutdowns():
    for service in await asyncio.to_thread(current_gpu_services):
        if active_jobs.get(service, 0) == 0:
            schedule_idle_stop(service)

@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}

@app.get("/status")
def status():
    states = {name: container_status(name) for name in SERVICES}
    running = [name for name in GPU_SERVICES if states.get(name) == "running"]
    owner = running[0] if len(running) == 1 else None
    now = time.monotonic()
    return {
        "gpu_owner": owner,
        "running_gpu_services": running,
        "active_jobs": dict(active_jobs),
        "lease_epoch": lease_epoch,
        "idle_stop_in_seconds": {
            name: max(0, round(deadline - now, 1))
            for name, deadline in idle_deadlines.items()
        },
        "services": states,
    }

@app.post("/acquire/{service}")
async def acquire(
    service: str,
    lease_id: str = Query(..., min_length=8, max_length=128),
):
    count = await acquire_service(service, lease_id)
    return {
        "service": service,
        "status": "ready",
        "lease_id": lease_id,
        "active_jobs": count,
    }

@app.post("/release/{service}")
async def release(
    service: str,
    lease_id: str = Query(..., min_length=8, max_length=128),
):
    count = await release_service(service, lease_id)
    return {
        "service": service,
        "status": "released",
        "lease_id": lease_id,
        "active_jobs": count,
    }

@app.post("/reset-leases")
async def reset_leases():
    global lease_epoch
    async with lease_condition:
        previous = dict(active_jobs)
        lease_epoch += 1
        active_leases.clear()
        active_jobs.clear()
        persist_active_jobs()
        lease_condition.notify_all()
    for service in await asyncio.to_thread(current_gpu_services):
        schedule_idle_stop(service)
    return {"status": "reset", "previous": previous}

@app.post("/ensure/{service}")
async def ensure(service: str):
    await prepare_service(service)
    return {"service": service, "status": "ready"}

@app.post("/stop/{service}")
async def stop(service: str):
    cancel_idle_stop(service)
    async with transition_lock:
        async with lease_condition:
            if active_jobs.get(service, 0):
                raise HTTPException(409, f"{service} has active jobs")
        await stop_service(service)
    return {"service": service, "status": "stopped"}

@app.post("/stop-all")
async def stop_all():
    async with transition_lock:
        async with lease_condition:
            busy = {k: v for k, v in active_jobs.items() if v}
            if busy:
                raise HTTPException(409, f"active jobs prevent stop-all: {busy}")
        for service in GPU_SERVICES:
            cancel_idle_stop(service)
            await stop_service(service)
    return {"status": "stopped"}

@app.get("/logs/{service}")
def logs(service: str, tail: int = Query(default=100, ge=1, le=1000)):
    c = get_container(service)
    return {
        "service": service,
        "logs": c.logs(tail=tail).decode(errors="replace"),
    }
