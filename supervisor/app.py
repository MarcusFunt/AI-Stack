import asyncio
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from resource_scheduler import ResourceAllocation, ResourceScheduler

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/models.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SERVICES = CONFIG["services"]
GPU_SERVICES = {
    name for name, spec in SERVICES.items()
    if spec.get("resources", {}).get("gpu", spec.get("gpu", False))
}
SCHEDULER_MODE = os.getenv("SUPERVISOR_SCHEDULER_MODE", "compatibility").strip().lower()
try:
    RESOURCE_SCHEDULER = ResourceScheduler(CONFIG, mode=SCHEDULER_MODE)
except ValueError as exc:
    raise RuntimeError(f"invalid supervisor resource scheduler configuration: {exc}") from exc
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "").strip()
if not SUPERVISOR_TOKEN:
    raise RuntimeError("SUPERVISOR_TOKEN must be set")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "").strip()
if not LLAMA_API_KEY:
    raise RuntimeError("LLAMA_API_KEY must be set")
DOCKER_CONTROL_URL = os.getenv("DOCKER_CONTROL_URL", "http://docker-control:8000").rstrip("/")
DOCKER_CONTROL_TOKEN = os.getenv("DOCKER_CONTROL_TOKEN", "").strip()
if not DOCKER_CONTROL_TOKEN:
    raise RuntimeError("DOCKER_CONTROL_TOKEN must be set")

app = FastAPI(title="Local AI GPU Supervisor", version="0.5.0")
transition_lock = asyncio.Lock()
lease_condition = asyncio.Condition()
LEASE_STATE_PATH = Path(os.getenv("LEASE_STATE_PATH", "/state/supervisor-leases.json"))
RUNTIME_SETTINGS_PATH = Path(os.getenv("RUNTIME_SETTINGS_PATH", "/state/runtime-settings.json"))
SERVICE_METRICS_PATH = Path(os.getenv("SERVICE_METRICS_PATH", "/state/supervisor-metrics.json"))

DEFAULT_RUNTIME_SETTINGS = {
    "mqtt": {
        "enabled": False,
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "home_assistant_discovery": True,
        "discovery_prefix": "homeassistant",
        "publish_interval": 2.0,
    }
}

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
        active_lease_profiles.pop(service, None)
        active_exclusive_leases.pop(service, None)


def load_lease_profiles():
    try:
        payload = json.loads(LEASE_STATE_PATH.read_text(encoding="utf-8"))
        profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
        if not isinstance(profiles, dict):
            return {}
        result = {}
        for service, entries in profiles.items():
            if service not in SERVICES or not isinstance(entries, dict):
                continue
            active_ids = active_leases.get(service, set())
            accepted = {}
            for lease_id, profile in entries.items():
                try:
                    RESOURCE_SCHEDULER.preferred_resident(str(profile))
                except ValueError:
                    continue
                if str(lease_id) in active_ids:
                    accepted[str(lease_id)] = str(profile)
            if accepted:
                result[service] = accepted
        return result
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}

def load_exclusive_leases():
    try:
        payload = json.loads(LEASE_STATE_PATH.read_text(encoding="utf-8"))
        raw = payload.get("exclusive_leases", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw, dict):
            return {}
        result = {}
        for service, lease_ids in raw.items():
            if service not in SERVICES or not isinstance(lease_ids, list):
                continue
            active_ids = active_leases.get(service, set())
            accepted = {
                str(lease_id)
                for lease_id in lease_ids
                if str(lease_id) in active_ids
            }
            if accepted:
                result[service] = accepted
        return result
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def persist_active_jobs():
    LEASE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 4,
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
        "profiles": {
            name: {
                lease_id: active_lease_profiles.get(name, {}).get(lease_id, "interactive")
                for lease_id in sorted(lease_ids)
            }
            for name, lease_ids in active_leases.items()
            if lease_ids
        },
        "exclusive_leases": {
            name: sorted(
                lease_id
                for lease_id in active_exclusive_leases.get(name, set())
                if lease_id in lease_ids
            )
            for name, lease_ids in active_leases.items()
            if active_exclusive_leases.get(name)
        },
    }
    tmp = LEASE_STATE_PATH.with_name(LEASE_STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, LEASE_STATE_PATH)

active_leases = defaultdict(set, load_lease_state())
active_jobs = defaultdict(int)
active_lease_profiles = defaultdict(dict, load_lease_profiles())
active_exclusive_leases = defaultdict(set, load_exclusive_leases())
sync_active_jobs()
lease_epoch = 0
idle_tasks = {}
idle_deadlines = {}
service_phase = {}
service_metrics = {}
pending_requests = {}
try:
    service_metrics = json.loads(SERVICE_METRICS_PATH.read_text(encoding="utf-8"))
    if not isinstance(service_metrics, dict):
        service_metrics = {}
except Exception:
    service_metrics = {}


def persist_service_metrics():
    SERVICE_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SERVICE_METRICS_PATH.with_name(SERVICE_METRICS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(service_metrics, sort_keys=True), encoding="utf-8")
    os.replace(tmp, SERVICE_METRICS_PATH)


def load_runtime_settings():
    try:
        payload = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            merged = json.loads(json.dumps(DEFAULT_RUNTIME_SETTINGS))
            merged["mqtt"].update(payload.get("mqtt", {}))
            return merged
    except Exception:
        pass
    return json.loads(json.dumps(DEFAULT_RUNTIME_SETTINGS))


def save_runtime_settings(payload):
    mqtt = payload.get("mqtt", {}) if isinstance(payload, dict) else {}
    current = load_runtime_settings()
    target = current["mqtt"]
    target["enabled"] = bool(mqtt.get("enabled", target["enabled"]))
    target["host"] = str(mqtt.get("host", target["host"])).strip()[:253]
    target["port"] = max(1, min(65535, int(mqtt.get("port", target["port"]))))
    target["username"] = str(mqtt.get("username", target["username"]))[:256]
    if "password" in mqtt and mqtt["password"] not in {None, "", "********"}:
        target["password"] = str(mqtt["password"])[:512]
    target["home_assistant_discovery"] = bool(
        mqtt.get("home_assistant_discovery", target["home_assistant_discovery"])
    )
    target["discovery_prefix"] = str(
        mqtt.get("discovery_prefix", target["discovery_prefix"])
    ).strip()[:128] or "homeassistant"
    target["publish_interval"] = max(
        1.0, min(60.0, float(mqtt.get("publish_interval", target["publish_interval"])))
    )
    if target["enabled"] and not target["host"]:
        raise HTTPException(400, "MQTT host is required when MQTT is enabled")
    RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_SETTINGS_PATH.with_name(RUNTIME_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, RUNTIME_SETTINGS_PATH)
    return current


def semantic_state(service, raw_state):
    phase = service_phase.get(service)
    if phase in {"starting", "loading", "warming", "unloading", "error"}:
        return phase
    if active_jobs.get(service, 0):
        return "running"
    if raw_state == "running":
        return "ready"
    if raw_state == "restarting":
        return "starting"
    if raw_state in {"created", "exited", "dead", "not-created"}:
        return "stopped"
    return raw_state or "unknown"


def resource_allocations():
    allocations = []
    for service in sorted(GPU_SERVICES):
        for lease_id in sorted(active_leases.get(service, set())):
            profile = active_lease_profiles.get(service, {}).get(lease_id, "interactive")
            try:
                allocations.append(RESOURCE_SCHEDULER.allocation(lease_id, service, profile))
            except ValueError:
                allocations.append(RESOURCE_SCHEDULER.allocation(lease_id, service))
    return allocations


def _pending_exclusive_request(service, *, exclude_id=None):
    return any(
        request_id != exclude_id
        and request.get("service") == service
        and bool(request.get("exclusive"))
        for request_id, request in pending_requests.items()
    )


def has_blocking_active_jobs(service, profile, *, exclusive=False, pending_id=None):
    if active_exclusive_leases.get(service):
        return True
    if exclusive and active_jobs.get(service, 0):
        return True
    if not exclusive and _pending_exclusive_request(service, exclude_id=pending_id):
        return True
    for active_service, count in active_jobs.items():
        if not count or active_service == service:
            continue
        # CPU workers and the default compatibility policy retain the existing
        # lease serialization contract. Resource mode only overlaps GPU leases
        # after the scheduler verifies declared groups and measured capacity.
        if (
            service not in GPU_SERVICES
            or active_service not in GPU_SERVICES
            or RESOURCE_SCHEDULER.mode == "compatibility"
        ):
            return True
    if service in GPU_SERVICES:
        return not RESOURCE_SCHEDULER.admit(service, resource_allocations(), profile).allowed
    return False


def services_to_stop_for(service):
    for other in GPU_SERVICES:
        if other == service:
            continue
        if RESOURCE_SCHEDULER.mode == "resource" and active_jobs.get(other, 0):
            continue
        yield other


def loaded_services_admissible(services):
    allocations = resource_allocations()
    for service in sorted(services):
        if any(allocation.service == service for allocation in allocations):
            continue
        decision = RESOURCE_SCHEDULER.admit(service, allocations, profile="interactive")
        if not decision.allowed:
            return False, decision.reason
        allocations.append(RESOURCE_SCHEDULER.allocation(f"loaded-{service}", service))
    return True, "resource policy permits all loaded workers"


def check_profile(profile):
    try:
        RESOURCE_SCHEDULER.preferred_resident(profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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

def docker_control_request(method: str, path: str, timeout: int = 30):
    headers = {"X-Docker-Control-Token": DOCKER_CONTROL_TOKEN}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, DOCKER_CONTROL_URL + path, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"docker-control unavailable: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[-1200:]
        raise HTTPException(response.status_code, f"docker-control: {detail}")
    return response


class RemoteContainer:
    def __init__(self, service: str):
        self.service = service
        self.status = None

    def reload(self):
        payload = docker_control_request("GET", f"/containers/{self.service}").json()
        self.status = payload.get("status")
        return self

    def start(self):
        payload = docker_control_request("POST", f"/containers/{self.service}/start", timeout=120).json()
        self.status = payload.get("status")

    def stop(self, timeout=30):
        payload = docker_control_request(
            "POST", f"/containers/{self.service}/stop?timeout={int(timeout)}", timeout=timeout + 30
        ).json()
        self.status = payload.get("status")

    def logs(self, tail=100):
        response = docker_control_request("GET", f"/containers/{self.service}/logs?tail={int(tail)}")
        return response.content


def get_container(service: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    return RemoteContainer(service)

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
        c.reload()
    except HTTPException:
        service_phase[service] = "stopped"
        return
    if c.status == "running":
        service_phase[service] = "unloading"
        await asyncio.to_thread(c.stop, timeout=30)
    service_phase[service] = "stopped"

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
    started = time.perf_counter()
    was_running = False
    service_phase[service] = "starting"
    for attempt in range(2):
        target = get_container(service)
        target.reload()
        was_running = target.status == "running"
        if not was_running:
            await asyncio.to_thread(target.start)
        service_phase[service] = "loading"
        try:
            await wait_ready(service)
            elapsed = round(time.perf_counter() - started, 3)
            service_phase[service] = "ready"
            if not was_running:
                metric = service_metrics.setdefault(service, {})
                metric["last_start_s"] = elapsed
                metric["starts"] = int(metric.get("starts", 0)) + 1
                metric["last_started_at"] = time.time()
                metric["last_error"] = None
                persist_service_metrics()
            return
        except HTTPException as exc:
            last_exc = exc
            service_phase[service] = "error"
            metric = service_metrics.setdefault(service, {})
            metric["last_error"] = str(exc.detail)[-1000:]
            metric["last_error_at"] = time.time()
            persist_service_metrics()
            target.reload()
            transient_exit = exc.status_code == 503 and target.status in {"exited", "dead"}
            if attempt == 0 and transient_exit:
                await asyncio.sleep(1)
                service_phase[service] = "starting"
                continue
            raise
    raise last_exc

async def prepare_service(service: str, profile: str = "interactive"):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    check_profile(profile)
    if service not in GPU_SERVICES:
        return

    cancel_idle_stop(service)
    pending_id = secrets.token_urlsafe(12)
    try:
        while True:
            async with lease_condition:
                while has_blocking_active_jobs(service, profile):
                    pending_requests[pending_id] = {
                        "service": service,
                        "profile": profile,
                        "queued_at": time.time(),
                    }
                    await lease_condition.wait()
                pending_requests.pop(pending_id, None)

            async with transition_lock:
                async with lease_condition:
                    other_jobs = has_blocking_active_jobs(service, profile)
                if other_jobs:
                    continue

                for other in services_to_stop_for(service):
                    cancel_idle_stop(other)
                    await stop_service(other)
                await start_and_wait_ready(service)
                return
    finally:
        pending_requests.pop(pending_id, None)

async def acquire_service(
    service: str,
    lease_id: str,
    profile: str = "interactive",
    *,
    exclusive: bool = False,
):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    check_profile(profile)
    started_epoch = lease_epoch
    pending_id = secrets.token_urlsafe(12)
    pending_requests[pending_id] = {
        "service": service,
        "profile": profile,
        "exclusive": exclusive,
        "queued_at": time.time(),
    }
    try:
        if service not in GPU_SERVICES:
            async with lease_condition:
                while has_blocking_active_jobs(
                    service,
                    profile,
                    exclusive=exclusive,
                    pending_id=pending_id,
                ):
                    await lease_condition.wait()
                if started_epoch != lease_epoch:
                    raise HTTPException(409, "lease reset during acquire")
                active_leases[service].add(lease_id)
                active_lease_profiles[service][lease_id] = profile
                if exclusive:
                    active_exclusive_leases[service].add(lease_id)
                sync_active_jobs(service)
                persist_active_jobs()
                lease_condition.notify_all()
                return active_jobs[service]

        cancel_idle_stop(service)
        while True:
            async with lease_condition:
                while has_blocking_active_jobs(
                    service,
                    profile,
                    exclusive=exclusive,
                    pending_id=pending_id,
                ):
                    await lease_condition.wait()

            async with transition_lock:
                async with lease_condition:
                    other_jobs = has_blocking_active_jobs(
                        service,
                        profile,
                        exclusive=exclusive,
                        pending_id=pending_id,
                    )
                if other_jobs:
                    continue

                for other in services_to_stop_for(service):
                    cancel_idle_stop(other)
                    await stop_service(other)
                await start_and_wait_ready(service)

                async with lease_condition:
                    if started_epoch != lease_epoch:
                        schedule_idle_stop(service)
                        raise HTTPException(409, "lease reset during acquire")
                    active_leases[service].add(lease_id)
                    active_lease_profiles[service][lease_id] = profile
                    if exclusive:
                        active_exclusive_leases[service].add(lease_id)
                    sync_active_jobs(service)
                    persist_active_jobs()
                    lease_condition.notify_all()
                    return active_jobs[service]
    finally:
        pending_requests.pop(pending_id, None)

async def release_service(service: str, lease_id: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    async with lease_condition:
        active_leases[service].discard(lease_id)
        active_lease_profiles.get(service, {}).pop(lease_id, None)
        active_exclusive_leases.get(service, set()).discard(lease_id)
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


def safe_runtime_settings(payload):
    safe = json.loads(json.dumps(payload))
    if safe.get("mqtt", {}).get("password"):
        safe["mqtt"]["password"] = "********"
        safe["mqtt"]["password_configured"] = True
    else:
        safe["mqtt"]["password_configured"] = False
    return safe


@app.get("/settings")
def settings():
    return safe_runtime_settings(load_runtime_settings())


@app.put("/settings")
async def update_settings(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "settings body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "settings body must be an object")
    return safe_runtime_settings(save_runtime_settings(payload))


@app.get("/doctor")
def doctor():
    checks = []
    try:
        info = docker_control_request("GET", "/info", timeout=5).json()
        checks.append({
            "id": "docker",
            "label": "Docker Engine",
            "status": "pass",
            "detail": info.get("server_version", "reachable") + " via restricted docker-control",
        })
    except Exception as exc:
        checks.append({"id": "docker", "label": "Docker Engine", "status": "fail", "detail": str(exc)})

    states = {name: container_status(name) for name in SERVICES}
    missing = [name for name, state in states.items() if state == "not-created" and name != "lerobot"]
    checks.append({
        "id": "workers",
        "label": "Worker containers",
        "status": "warn" if missing else "pass",
        "detail": "missing: " + ", ".join(missing) if missing else f"{len(states)} configured",
    })
    running = [name for name in GPU_SERVICES if states.get(name) == "running"]
    scheduler_safe, scheduler_reason = loaded_services_admissible(running)
    checks.append({
        "id": "gpu-exclusivity",
        "label": "GPU exclusivity",
        "status": "pass" if scheduler_safe else "fail",
        "detail": (
            (", ".join(running) if running else "no heavyweight worker running")
            if scheduler_safe else f"{scheduler_reason}: {', '.join(running)}"
        ),
    })
    checks.append({
        "id": "resource-scheduler",
        "label": "Resource scheduler",
        "status": "pass",
        "detail": f"{RESOURCE_SCHEDULER.mode} mode; {len(resource_allocations())} active GPU lease(s)",
    })
    try:
        SERVICE_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe = SERVICE_METRICS_PATH.parent / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append({"id": "state", "label": "Persistent state", "status": "pass", "detail": str(SERVICE_METRICS_PATH.parent)})
    except Exception as exc:
        checks.append({"id": "state", "label": "Persistent state", "status": "fail", "detail": str(exc)})

    overall = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    )
    return {"status": overall, "checks": checks, "services": states}


@app.get("/status")
def status():
    states = {name: container_status(name) for name in SERVICES}
    running = [name for name in GPU_SERVICES if states.get(name) == "running"]
    owner = running[0] if len(running) == 1 else None
    now = time.monotonic()
    semantic = {name: semantic_state(name, state) for name, state in states.items()}
    return {
        "gpu_owner": owner,
        "running_gpu_services": running,
        "active_jobs": dict(active_jobs),
        "lease_epoch": lease_epoch,
        "heartbeat": time.time(),
        "idle_stop_in_seconds": {
            name: max(0, round(deadline - now, 1))
            for name, deadline in idle_deadlines.items()
        },
        "services": states,
        "service_states": semantic,
        "service_metrics": service_metrics,
        "resource_state": {
            **RESOURCE_SCHEDULER.state(
                resource_allocations(), running, pending_requests=list(pending_requests.values())
            ),
            "exclusive_leases": {
                name: sorted(lease_ids)
                for name, lease_ids in active_exclusive_leases.items()
                if lease_ids
            },
        },
    }

@app.post("/acquire/{service}")
async def acquire(
    service: str,
    lease_id: str = Query(..., min_length=8, max_length=128),
    profile: str = Query(default="interactive", min_length=1, max_length=32),
    exclusive: bool = Query(default=False),
):
    count = await acquire_service(service, lease_id, profile, exclusive=exclusive)
    return {
        "service": service,
        "status": "ready",
        "lease_id": lease_id,
        "profile": profile,
        "exclusive": exclusive,
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
        active_lease_profiles.clear()
        active_exclusive_leases.clear()
        persist_active_jobs()
        lease_condition.notify_all()
    for service in await asyncio.to_thread(current_gpu_services):
        schedule_idle_stop(service)
    return {"status": "reset", "previous": previous}

@app.post("/ensure/{service}")
async def ensure(
    service: str,
    profile: str = Query(default="interactive", min_length=1, max_length=32),
):
    await prepare_service(service, profile)
    if service in GPU_SERVICES and active_jobs.get(service, 0) == 0:
        schedule_idle_stop(service)
    return {
        "service": service,
        "status": "ready",
        "idle_timeout": int(SERVICES[service].get("idle_timeout", 0)),
    }

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
