import asyncio
import io
import json
import os
import secrets
import struct
import time
import wave
import zlib
from collections import deque
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

API_KEY = os.getenv("AI_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("AI_API_KEY must be set")

SUPERVISOR_URL = os.getenv("SUPERVISOR_URL", "http://supervisor:8000").rstrip("/")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "").strip()
if not SUPERVISOR_TOKEN:
    raise RuntimeError("SUPERVISOR_TOKEN must be set")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "").strip()
if not LLAMA_API_KEY:
    raise RuntimeError("LLAMA_API_KEY must be set")
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/models.json"))
TELEMETRY_URL = os.getenv("TELEMETRY_URL", "http://telemetry:8000").rstrip("/")
RUNTIME_SETTINGS_PATH = Path(os.getenv("RUNTIME_SETTINGS_PATH", "/state/runtime-settings.json"))
HOST_AGENT_URL = os.getenv("HOST_AGENT_URL", "http://host.docker.internal:8788").rstrip("/")
HOST_AGENT_TOKEN = os.getenv("HOST_AGENT_TOKEN", "").strip()
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SERVICES = CONFIG["services"]
MODELS = CONFIG["models"]
ALIASES = {k.lower(): v for k, v in CONFIG.get("aliases", {}).items()}
MODEL_SERVICE = {m["id"].lower(): m["service"] for m in MODELS}

app = FastAPI(title="Marcus Local AI Gateway", version="0.5.0")
ACTIVE_JOBS = {}
JOB_HISTORY = deque(maxlen=50)
SELF_TEST_RESULTS = {}
SELF_TEST_TASK = None
SELF_TEST_RUN = {"state": "idle", "services": [], "started_at": None, "finished_at": None}
mqtt_client = None
mqtt_connected = False
mqtt_last_error = None
mqtt_settings_signature = None
mqtt_discovery_published = False
mqtt_task = None
TTS_MAX_INPUT_CHARS = int(os.getenv("TTS_MAX_INPUT_CHARS", "20000"))
TTS_RESPONSE_FORMATS = {"mp3", "wav", "opus", "flac", "pcm"}
CHAT_MAX_BODY_BYTES = int(os.getenv("CHAT_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
TTS_MAX_BODY_BYTES = int(os.getenv("TTS_MAX_BODY_BYTES", str(1 * 1024 * 1024)))
STT_MAX_UPLOAD_BYTES = int(os.getenv("STT_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
VLM_MAX_UPLOAD_BYTES = int(os.getenv("VLM_MAX_UPLOAD_BYTES", str(24 * 1024 * 1024)))
COMFY_MAX_BODY_BYTES = int(os.getenv("COMFY_MAX_BODY_BYTES", str(128 * 1024 * 1024)))
WANGP_MAX_BODY_BYTES = int(os.getenv("WANGP_MAX_BODY_BYTES", str(512 * 1024 * 1024)))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "").strip()[:128] or secrets.token_hex(12)
    started = time.perf_counter()
    public_path = request.url.path in {"/health", "/ready"}

    if not public_path:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {API_KEY}"
        if not secrets.compare_digest(supplied, expected):
            response = JSONResponse(
                status_code=401,
                content={"detail": "invalid API key", "request_id": request_id},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

async def supervisor(method: str, path: str, timeout: float = 600):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            SUPERVISOR_URL + path,
            headers={"X-Supervisor-Token": SUPERVISOR_TOKEN},
        )
    if response.status_code >= 400:
        detail = response.text[-4000:]
        raise HTTPException(response.status_code, detail)
    return response.json()


async def host_agent(method: str, path: str, payload=None, timeout: float = 30):
    if not HOST_AGENT_TOKEN:
        raise HTTPException(503, "host agent token is not configured")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                HOST_AGENT_URL + path,
                headers={"X-Host-Agent-Token": HOST_AGENT_TOKEN},
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(503, f"host agent unavailable: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[-4000:]
        raise HTTPException(response.status_code, detail)
    return response.json()


PHASE_FOR_SERVICE = {
    "llm": "generating",
    "reasoning": "generating",
    "stt": "transcribing",
    "tts": "synthesizing",
    "vlm": "analyzing",
    "comfyui": "generating",
    "wangp": "generating",
    "lerobot": "running policy",
}


def create_job(service: str, path: str):
    job_id = secrets.token_hex(8)
    now = time.time()
    job = {
        "id": job_id,
        "service": service,
        "path": path,
        "state": "waiting",
        "phase": "waiting_for_gpu",
        "started_at": now,
        "last_progress_at": now,
        "last_activity_at": now,
        "progress_units": 0,
        "metrics": {},
    }
    ACTIVE_JOBS[job_id] = job
    return job


def update_job(job, **changes):
    job.update(changes)
    if changes.get("progress_units") is not None or changes.get("phase"):
        now = time.time()
        job["last_progress_at"] = now
        job["last_activity_at"] = now


def finish_job(job, state="complete", error=None):
    job["state"] = state
    job["finished_at"] = time.time()
    job["elapsed_s"] = round(job["finished_at"] - job["started_at"], 3)
    if error:
        job["error"] = str(error)[-1000:]
    ACTIVE_JOBS.pop(job["id"], None)
    JOB_HISTORY.appendleft(dict(job))


def public_job(job):
    item = dict(job)
    reference = item.get("finished_at", time.time())
    if "finished_at" not in item:
        item["elapsed_s"] = round(reference - item["started_at"], 3)
    item["progress_age_s"] = round(reference - item.get("last_progress_at", item["started_at"]), 3)
    item["activity_age_s"] = round(reference - item.get("last_activity_at", item["started_at"]), 3)
    item["stalled_suspected"] = bool(
        item.get("state") == "running"
        and item["activity_age_s"] > 15
        and item["progress_age_s"] > 30
    )
    return item


async def telemetry_snapshot():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(TELEMETRY_URL + "/snapshot")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {"timestamp": time.time(), "gpu": None, "system": None, "error": str(exc)}


async def build_snapshot():
    supervisor_status, machine = await asyncio.gather(
        supervisor("GET", "/status", timeout=5),
        telemetry_snapshot(),
    )
    gpu = machine.get("gpu") or {}
    if float(gpu.get("utilization_percent", 0) or 0) >= 3:
        now = time.time()
        for job in ACTIVE_JOBS.values():
            if job.get("state") == "running":
                job["last_activity_at"] = now
    return {
        "timestamp": time.time(),
        "gateway": {"status": "online", "version": app.version},
        "supervisor": supervisor_status,
        "machine": machine,
        "jobs": {
            "active": [public_job(job) for job in ACTIVE_JOBS.values()],
            "recent": [public_job(job) for job in list(JOB_HISTORY)[:20]],
        },
        "mqtt": {
            "connected": mqtt_connected,
            "last_error": mqtt_last_error,
        },
        "self_tests": {
            "run": dict(SELF_TEST_RUN),
            "results": dict(SELF_TEST_RESULTS),
        },
    }


def read_mqtt_settings():
    default = {
        "enabled": False,
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "home_assistant_discovery": True,
        "discovery_prefix": "homeassistant",
        "publish_interval": 2.0,
    }
    try:
        payload = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("mqtt"), dict):
            default.update(payload["mqtt"])
    except Exception:
        pass
    return default


def mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected, mqtt_last_error, mqtt_discovery_published
    mqtt_connected = str(reason_code) in {"Success", "0"}
    mqtt_last_error = None if mqtt_connected else f"connect: {reason_code}"
    if mqtt_connected:
        mqtt_discovery_published = False
    if mqtt_connected:
        try:
            client.publish("localai/marcus-computer/availability", "online", qos=1, retain=True)
        except Exception as exc:
            mqtt_last_error = str(exc)


def mqtt_on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    global mqtt_connected, mqtt_last_error
    mqtt_connected = False
    if str(reason_code) not in {"Normal disconnection", "0"}:
        mqtt_last_error = f"disconnect: {reason_code}"


def mqtt_publish_offline(client):
    try:
        info = client.publish(
            "localai/marcus-computer/availability",
            "offline",
            qos=1,
            retain=True,
        )
        info.wait_for_publish(timeout=2)
    except Exception:
        pass


def configure_mqtt(settings):
    global mqtt_client, mqtt_connected, mqtt_last_error, mqtt_settings_signature, mqtt_discovery_published
    signature = (
        bool(settings.get("enabled")),
        settings.get("host", ""),
        int(settings.get("port", 1883)),
        settings.get("username", ""),
        settings.get("password", ""),
        bool(settings.get("home_assistant_discovery")),
        settings.get("discovery_prefix", "homeassistant"),
    )
    if signature == mqtt_settings_signature:
        return
    mqtt_settings_signature = signature
    if mqtt_client is not None:
        try:
            if mqtt_connected:
                mqtt_publish_offline(mqtt_client)
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
        except Exception:
            pass
        mqtt_client = None
    mqtt_connected = False
    mqtt_last_error = None
    mqtt_discovery_published = False
    if not settings.get("enabled"):
        return
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="local-ai-gateway")
        client.will_set(
            "localai/marcus-computer/availability",
            payload="offline",
            qos=1,
            retain=True,
        )
        username = str(settings.get("username", ""))
        if username:
            client.username_pw_set(username, str(settings.get("password", "")))
        client.on_connect = mqtt_on_connect
        client.on_disconnect = mqtt_on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(str(settings["host"]), int(settings.get("port", 1883)), keepalive=30)
        client.loop_start()
        mqtt_client = client
    except Exception as exc:
        mqtt_last_error = str(exc)


def publish_home_assistant(settings, root):
    global mqtt_discovery_published
    if not mqtt_client or not mqtt_connected or not settings.get("home_assistant_discovery"):
        return
    if mqtt_discovery_published:
        return
    prefix = str(settings.get("discovery_prefix", "homeassistant")).strip() or "homeassistant"
    availability = root + "/availability"
    device = {
        "identifiers": ["local_ai_marcus_computer"],
        "name": "Local AI - Marcus Computer",
        "manufacturer": "Local AI",
        "model": "RTX 3060 AI workstation",
    }
    entities = [
        ("gpu_utilization", "GPU utilization", "gpu/utilization", "%", None, True),
        ("gpu_vram", "GPU VRAM used", "gpu/vram_used_mb", "MiB", "data_size", True),
        ("gpu_vram_total", "GPU VRAM total", "gpu/vram_total_mb", "MiB", "data_size", True),
        ("gpu_temperature", "GPU temperature", "gpu/temperature_c", "°C", "temperature", True),
        ("gpu_power", "GPU power", "gpu/power_w", "W", "power", True),
        ("cpu_utilization", "CPU utilization", "system/cpu_percent", "%", None, True),
        ("ram_used", "RAM used", "system/ram_used_mb", "MiB", "data_size", True),
        ("ram_total", "RAM total", "system/ram_total_mb", "MiB", "data_size", True),
        ("disk_free", "Model disk free", "system/disk_free_gb", "GiB", "data_size", True),
        ("gpu_owner", "Active AI service", "scheduler/owner", None, None, False),
        ("active_jobs", "Active AI jobs", "scheduler/active_jobs", None, None, True),
        ("queue_depth", "AI queue depth", "scheduler/queue_depth", None, None, True),
        ("job_state", "AI job state", "job/state", None, None, False),
    ]
    for key, name, suffix, unit, device_class, measurement in entities:
        payload = {
            "name": name,
            "unique_id": "local_ai_" + key,
            "state_topic": root + "/" + suffix,
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if measurement:
            payload["state_class"] = "measurement"
        mqtt_client.publish(
            f"{prefix}/sensor/local_ai_{key}/config",
            json.dumps(payload),
            qos=0,
            retain=True,
        )
    for service in SERVICES:
        payload = {
            "name": f"{service} state",
            "unique_id": f"local_ai_service_{service}",
            "state_topic": f"{root}/service/{service}/state",
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            "icon": "mdi:robot-industrial",
        }
        mqtt_client.publish(
            f"{prefix}/sensor/local_ai_service_{service}/config",
            json.dumps(payload),
            qos=0,
            retain=True,
        )
    mqtt_discovery_published = True


def publish_mqtt_snapshot(snapshot, settings):
    if not mqtt_client or not mqtt_connected:
        return
    root = "localai/marcus-computer"
    machine = snapshot.get("machine", {})
    gpu = machine.get("gpu") or {}
    system = machine.get("system") or {}
    supervisor_state = snapshot.get("supervisor", {})
    active_jobs = snapshot.get("jobs", {}).get("active", [])
    current = active_jobs[0] if active_jobs else None
    summary = {
        "online": True,
        "health": "healthy" if machine.get("gpu") else "degraded",
        "gpu_owner": supervisor_state.get("gpu_owner"),
        "job": current,
        "gpu": gpu,
        "system": system,
    }
    mqtt_client.publish(root + "/status", json.dumps(summary), retain=True)
    values = {
        "gpu/utilization": gpu.get("utilization_percent", 0),
        "gpu/vram_used_mb": gpu.get("vram_used_mib", 0),
        "gpu/vram_total_mb": gpu.get("vram_total_mib", 0),
        "gpu/temperature_c": gpu.get("temperature_c", 0),
        "gpu/power_w": gpu.get("power_w", 0),
        "system/cpu_percent": system.get("cpu_percent", 0),
        "system/ram_used_mb": system.get("ram_used_mib", 0),
        "system/ram_total_mb": system.get("ram_total_mib", 0),
        "system/disk_free_gb": system.get("disk_free_gib", 0),
        "scheduler/owner": supervisor_state.get("gpu_owner") or "idle",
        "scheduler/active_jobs": sum(supervisor_state.get("active_jobs", {}).values()),
        "scheduler/queue_depth": sum(1 for job in active_jobs if job.get("state") == "waiting"),
        "job/state": current.get("state") if current else "idle",
    }
    mqtt_client.publish(root + "/availability", "online", qos=1, retain=True)
    for suffix, value in values.items():
        mqtt_client.publish(root + "/" + suffix, str(value), retain=True)
    for service, state in supervisor_state.get("service_states", {}).items():
        mqtt_client.publish(f"{root}/service/{service}/state", str(state), retain=True)
    publish_home_assistant(settings, root)


async def mqtt_publisher_loop():
    last_publish = 0.0
    while True:
        settings = read_mqtt_settings()
        configure_mqtt(settings)
        interval = max(1.0, min(60.0, float(settings.get("publish_interval", 2.0))))
        if settings.get("enabled") and mqtt_connected and time.monotonic() - last_publish >= interval:
            try:
                publish_mqtt_snapshot(await build_snapshot(), settings)
                last_publish = time.monotonic()
            except Exception as exc:
                global mqtt_last_error
                mqtt_last_error = str(exc)
        await asyncio.sleep(1.0)


async def ensure_service(service: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    await supervisor("POST", f"/ensure/{service}")

async def acquire_service(service: str):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    lease_id = secrets.token_urlsafe(18)
    try:
        await supervisor("POST", f"/acquire/{service}?lease_id={lease_id}")
    except Exception:
        # If the acquire reached the supervisor but its response was lost,
        # this idempotent release prevents a persistent orphaned lease.
        try:
            await supervisor(
                "POST",
                f"/release/{service}?lease_id={lease_id}",
                timeout=10,
            )
        except Exception:
            pass
        raise
    return lease_id

async def release_service(service: str, lease_id: str):
    path = f"/release/{service}?lease_id={lease_id}"
    try:
        return await supervisor("POST", path, timeout=30)
    except Exception:
        async def retry_release():
            for _ in range(60):
                await asyncio.sleep(1)
                try:
                    await supervisor("POST", path, timeout=10)
                    return
                except Exception:
                    continue
        asyncio.create_task(retry_release())
        return None


def self_test_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 8000)
    return output.getvalue()


def self_test_png():
    width = height = 64
    raw = b"".join(b"\x00" + (b"\x80\x80\x80" * width) for _ in range(height))
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def run_service_self_test(service: str):
    if service not in SERVICES or service == "lerobot":
        raise ValueError("self-test is not available for this service")
    started = time.time()
    lease_id = None
    detail = "backend responded"
    try:
        lease_id = await acquire_service(service)
        spec = SERVICES[service]
        async with httpx.AsyncClient(timeout=None) as client:
            if service in {"llm", "reasoning"}:
                model_id = "local-fast" if service == "llm" else "local-reasoning"
                body = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": False,
                }
                if service == "llm":
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                response = await client.post(
                    spec["base"] + "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {LLAMA_API_KEY}"},
                )
                response.raise_for_status()
                detail = "chat completion succeeded"
            elif service == "stt":
                response = await client.post(
                    spec["base"] + "/v1/audio/transcriptions",
                    files={"file": ("self-test.wav", self_test_wav(), "audio/wav")},
                    data={"model": "local-stt", "response_format": "json", "language": "en"},
                )
                response.raise_for_status()
                detail = "audio decode/transcription path succeeded"
            elif service == "tts":
                response = await client.post(
                    spec["base"] + "/v1/audio/speech",
                    json={"model": "local-tts", "input": "Self test.", "response_format": "wav"},
                )
                response.raise_for_status()
                if len(response.content) < 100:
                    raise RuntimeError("TTS returned an unexpectedly small response")
                detail = f"TTS generated {len(response.content)} bytes"
            elif service == "vlm":
                response = await client.post(
                    spec["base"] + "/v1/vision/analyze",
                    files={"image": ("self-test.png", self_test_png(), "image/png")},
                    data={"prompt": "Reply with one word: gray", "max_new_tokens": "8"},
                )
                response.raise_for_status()
                detail = "vision inference succeeded"
            elif service == "comfyui":
                response = await client.get(spec["base"] + "/system_stats")
                response.raise_for_status()
                detail = "ComfyUI API responded"
            elif service == "wangp":
                response = await client.get(spec["base"] + "/")
                response.raise_for_status()
                detail = "WanGP UI/API responded"
        return {
            "service": service,
            "state": "pass",
            "detail": detail,
            "started_at": started,
            "finished_at": time.time(),
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "service": service,
            "state": "fail",
            "detail": str(exc)[-1200:],
            "started_at": started,
            "finished_at": time.time(),
            "elapsed_s": round(time.time() - started, 3),
        }
    finally:
        if lease_id is not None:
            await release_service(service, lease_id)


async def self_test_worker(services):
    global SELF_TEST_RUN
    SELF_TEST_RUN = {
        "state": "running", "services": services, "started_at": time.time(),
        "finished_at": None,
    }
    for service in services:
        SELF_TEST_RESULTS[service] = {
            "service": service, "state": "running", "detail": "starting",
            "started_at": time.time(), "finished_at": None, "elapsed_s": 0,
        }
        SELF_TEST_RESULTS[service] = await run_service_self_test(service)
    SELF_TEST_RUN["state"] = (
        "pass" if all(SELF_TEST_RESULTS[s]["state"] == "pass" for s in services) else "fail"
    )
    SELF_TEST_RUN["finished_at"] = time.time()


def start_self_tests(services):
    global SELF_TEST_TASK
    if SELF_TEST_TASK and not SELF_TEST_TASK.done():
        raise HTTPException(409, "a self-test run is already active")
    SELF_TEST_TASK = asyncio.create_task(self_test_worker(services))
    return {"status": "started", "services": services}


@app.on_event("startup")
async def reset_stale_leases():
    global mqtt_task
    await supervisor("POST", "/reset-leases", timeout=10)
    mqtt_task = asyncio.create_task(mqtt_publisher_loop())


@app.on_event("shutdown")
async def shutdown_background_tasks():
    global mqtt_client
    if mqtt_task:
        mqtt_task.cancel()
    if mqtt_client is not None:
        try:
            if mqtt_connected:
                mqtt_publish_offline(mqtt_client)
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
        except Exception:
            pass
        mqtt_client = None

def upstream_headers(request: Request, service: str):
    blocked = HOP_BY_HOP_HEADERS | {"host", "content-length", "authorization"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in blocked}
    if SERVICES[service].get("backend_auth") == "llama":
        headers["Authorization"] = f"Bearer {LLAMA_API_KEY}"
    return headers

def response_headers(response: httpx.Response, buffered: bool = False):
    blocked = HOP_BY_HOP_HEADERS | {"content-length"}
    if buffered:
        blocked.add("content-encoding")
    return {k: v for k, v in response.headers.items() if k.lower() not in blocked}

async def read_body_limited(request: Request, limit: int, label: str):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if declared < 0:
            raise HTTPException(400, "invalid Content-Length")
        if declared > limit:
            raise HTTPException(413, f"{label} exceeds byte limit")

    chunks = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, f"{label} exceeds byte limit")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(499, "client disconnected during request upload") from exc
    return b"".join(chunks)

async def forward_buffered(
    service: str,
    path: str,
    request: Request,
    body: bytes | None = None,
    max_body_bytes: int | None = None,
    body_label: str = "request body",
):
    if body is not None:
        payload = body
        if max_body_bytes is not None and len(payload) > max_body_bytes:
            raise HTTPException(413, f"{body_label} exceeds byte limit")
    elif max_body_bytes is not None:
        payload = await read_body_limited(request, max_body_bytes, body_label)
    else:
        payload = await request.body()

    job = create_job(service, path)
    lease_id = None
    try:
        lease_id = await acquire_service(service)
        update_job(job, state="running", phase=PHASE_FOR_SERVICE.get(service, "processing"))
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                upstream = await client.request(
                    request.method,
                    SERVICES[service]["base"] + path,
                    params=request.query_params,
                    content=payload,
                    headers=upstream_headers(request, service),
                )
            except httpx.RequestError as exc:
                raise HTTPException(502, f"{service} upstream request failed") from exc

        metrics = {
            "response_bytes": len(upstream.content),
            "http_status": upstream.status_code,
        }
        content_type = upstream.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                parsed = upstream.json()
                timings = parsed.get("timings") if isinstance(parsed, dict) else None
                if isinstance(timings, dict):
                    metrics.update({
                        "prompt_tps": timings.get("prompt_per_second"),
                        "generation_tps": timings.get("predicted_per_second"),
                        "ms_per_token": timings.get("predicted_per_token_ms"),
                        "prompt_tokens": timings.get("prompt_n"),
                        "generated_tokens": timings.get("predicted_n"),
                    })
            except Exception:
                pass
        job["metrics"] = {k: v for k, v in metrics.items() if v is not None}
        finish_job(job, state="complete" if upstream.status_code < 400 else "failed")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers(upstream, buffered=True),
            media_type=upstream.headers.get("content-type"),
        )
    except Exception as exc:
        if job["id"] in ACTIVE_JOBS:
            finish_job(job, state="failed", error=exc)
        raise
    finally:
        if lease_id is not None:
            await release_service(service, lease_id)

async def forward_streaming(service: str, path: str, request: Request, body: bytes):
    job = create_job(service, path)
    lease_id = None
    client = None
    upstream = None
    cleanup_lock = asyncio.Lock()
    released = False
    final_state = "complete"
    final_error = None

    async def cleanup():
        nonlocal released
        async with cleanup_lock:
            if released:
                return
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if client is not None:
                    await client.aclose()
                if lease_id is not None:
                    await release_service(service, lease_id)
                if job["id"] in ACTIVE_JOBS:
                    finish_job(job, state=final_state, error=final_error)
                released = True

    try:
        lease_id = await acquire_service(service)
        update_job(job, state="running", phase=PHASE_FOR_SERVICE.get(service, "streaming"))
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            request.method,
            SERVICES[service]["base"] + path,
            params=request.query_params,
            content=body,
            headers=upstream_headers(request, service),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        final_state = "failed"
        final_error = exc
        await cleanup()
        raise HTTPException(502, f"{service} upstream request failed") from exc
    except Exception as exc:
        final_state = "failed"
        final_error = exc
        await cleanup()
        raise

    async def iterator():
        nonlocal final_state, final_error
        chunks = 0
        byte_count = 0
        try:
            try:
                async for chunk in upstream.aiter_raw():
                    if await request.is_disconnected():
                        final_state = "cancelled"
                        break
                    chunks += max(1, chunk.count(b"data:"))
                    byte_count += len(chunk)
                    update_job(
                        job,
                        progress_units=chunks,
                        metrics={"stream_bytes": byte_count, "chunks": chunks},
                    )
                    yield chunk
            except httpx.RequestError as exc:
                final_state = "failed"
                final_error = exc
                return
        finally:
            try:
                await cleanup()
            except asyncio.CancelledError:
                task = asyncio.create_task(cleanup())
                await asyncio.shield(task)
                raise

    return StreamingResponse(
        iterator(),
        status_code=upstream.status_code,
        headers=response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )

@app.get("/health")
async def health():
    try:
        await supervisor("GET", "/status", timeout=5)
        return {"status": "ok", "version": app.version}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "version": app.version},
        )

@app.get("/ready")
async def ready():
    try:
        status = await supervisor("GET", "/status", timeout=5)
        return {
            "status": "ready",
            "version": app.version,
            "gpu_owner": status.get("gpu_owner"),
        }
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "version": app.version},
        )

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            machine_response = await client.get(TELEMETRY_URL + "/metrics")
            machine_response.raise_for_status()
            machine_metrics = machine_response.text.rstrip()
    except Exception:
        machine_metrics = ""
    status = await supervisor("GET", "/status", timeout=5)
    active_jobs = sum(int(value) for value in status.get("active_jobs", {}).values())
    owner = status.get("gpu_owner") or "idle"
    service_lines = []
    for service, state in status.get("service_states", {}).items():
        value = 1 if state in {"ready", "running", "loading", "starting", "warming"} else 0
        service_lines.append(
            f'localai_service_active{{service="{service}",state="{state}"}} {value}'
        )
    lines = [
        machine_metrics,
        "# TYPE localai_active_jobs gauge",
        f"localai_active_jobs {active_jobs}",
        "# TYPE localai_gpu_owner_info gauge",
        f'localai_gpu_owner_info{{owner="{owner}"}} 1',
        "# TYPE localai_service_active gauge",
        *service_lines,
    ]
    return "\n".join(line for line in lines if line) + "\n"


@app.get("/v1/capabilities")
async def capabilities():
    return {
        "name": "Local AI",
        "version": app.version,
        "transport": "HTTP",
        "authentication": "Bearer",
        "models": [model["id"] for model in MODELS],
        "endpoints": {
            "chat": "/v1/chat/completions",
            "transcription": "/v1/audio/transcriptions",
            "speech": "/v1/audio/speech",
            "vision": "/v1/vision/analyze",
            "models": "/v1/models",
            "status": "/v1/system/status",
        },
    }

@app.get("/v1/system/status")
async def system_status():
    return await supervisor("GET", "/status", timeout=10)

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": model["id"],
                "object": "model",
                "owned_by": model.get("owned_by", "local"),
                "capabilities": model.get("capabilities", []),
                "metadata": {
                    key: value for key, value in model.items()
                    if key not in {"id", "owned_by", "capabilities", "service"}
                },
            }
            for model in MODELS
        ],
    }

@app.get("/control/status")
async def control_status():
    return await supervisor("GET", "/status", timeout=10)


@app.get("/control/snapshot")
async def control_snapshot():
    return await build_snapshot()


@app.get("/control/jobs")
async def control_jobs():
    return {
        "active": [public_job(job) for job in ACTIVE_JOBS.values()],
        "recent": [public_job(job) for job in list(JOB_HISTORY)[:30]],
    }


@app.get("/control/telemetry/history")
async def telemetry_history(seconds: int = 120, max_points: int = 900):
    seconds = max(5, min(seconds, 7 * 24 * 3600))
    max_points = max(60, min(max_points, 2000))
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(
            TELEMETRY_URL + "/history",
            params={"seconds": seconds, "max_points": max_points},
        )
        response.raise_for_status()
        return response.json()


@app.get("/control/doctor")
async def control_doctor():
    supervisor_result, machine = await asyncio.gather(
        supervisor("GET", "/doctor", timeout=10),
        telemetry_snapshot(),
    )
    checks = list(supervisor_result.get("checks", []))
    gpu = machine.get("gpu")
    system = machine.get("system")
    checks.append({
        "id": "gpu-telemetry",
        "label": "GPU telemetry",
        "status": "pass" if gpu else "fail",
        "detail": (
            f"{gpu.get('name')} · {gpu.get('vram_total_mib', 0) / 1024:.1f} GiB"
            if gpu else str(machine.get("error") or machine.get("gpu_error") or "unavailable")
        ),
    })
    checks.append({
        "id": "storage",
        "label": "Model storage",
        "status": "pass" if system and system.get("disk_free_gib", 0) >= 50 else "warn",
        "detail": f"{system.get('disk_free_gib', 0):.1f} GiB free" if system else "unavailable",
    })
    try:
        network = await host_agent("GET", "/tailscale/status", timeout=10)
        checks.append({
            "id": "host-agent",
            "label": "Host integration",
            "status": "pass",
            "detail": (
                f"Tailscale {'online' if network.get('online') else 'offline'}"
                + (f" · {network.get('dns_name')}" if network.get("dns_name") else "")
            ),
        })
    except Exception as exc:
        checks.append({
            "id": "host-agent",
            "label": "Host integration",
            "status": "warn",
            "detail": str(exc)[-300:],
        })
    overall = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    )
    return {"status": overall, "checks": checks}


@app.get("/control/settings")
async def control_settings():
    return await supervisor("GET", "/settings", timeout=10)


@app.put("/control/settings")
async def control_settings_update(request: Request):
    payload = await request.json()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.put(
            SUPERVISOR_URL + "/settings",
            headers={"X-Supervisor-Token": SUPERVISOR_TOKEN},
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text[-4000:])
    return response.json()


@app.get("/control/network")
async def control_network():
    return await host_agent("GET", "/tailscale/status", timeout=20)


@app.post("/control/network/tailscale")
async def control_network_tailscale(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "network settings must be an object")
    return await host_agent("POST", "/tailscale/configure", payload=payload, timeout=60)


@app.get("/control/model-management")
async def control_model_management():
    return await host_agent("GET", "/models/config", timeout=20)


@app.post("/control/model-management/config")
async def control_model_management_config(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "model configuration must be an object")
    service = str(payload.get("service", "")).lower()
    if service not in SERVICES:
        raise HTTPException(400, "unknown model service")
    status = await supervisor("GET", "/status", timeout=10)
    jobs = int(status.get("active_jobs", {}).get(service, 0) or 0)
    if jobs:
        raise HTTPException(409, f"{service} has {jobs} active job(s); configuration was not changed")
    if payload.get("recreate", True):
        state = status.get("service_states", {}).get(service, "unknown")
        if state != "stopped":
            raise HTTPException(
                409,
                f"{service} is {state}; stop/unload it before recreating its worker",
            )
    return await host_agent("POST", "/models/config", payload=payload, timeout=660)


@app.post("/control/model-management/install")
async def control_model_management_install(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "install request must be an object")
    return await host_agent("POST", "/models/install", payload=payload, timeout=30)


@app.get("/control/model-management/install/{job_id}")
async def control_model_management_install_status(job_id: str):
    safe_id = "".join(ch for ch in job_id if ch.isalnum() or ch in {"-", "_"})
    if safe_id != job_id or not safe_id:
        raise HTTPException(400, "invalid install id")
    return await host_agent("GET", f"/models/install/{safe_id}", timeout=20)


@app.get("/control/self-tests")
async def control_self_tests():
    return {"run": SELF_TEST_RUN, "results": SELF_TEST_RESULTS}


@app.post("/control/self-test/{service}")
async def control_self_test(service: str):
    if service not in SERVICES or service == "lerobot":
        raise HTTPException(400, "self-test is unavailable for this service")
    return start_self_tests([service])


@app.post("/control/self-test-all")
async def control_self_test_all():
    services = [
        name for name in ("llm", "reasoning", "stt", "tts", "vlm", "comfyui", "wangp")
        if name in SERVICES
    ]
    return start_self_tests(services)


@app.post("/control/mqtt/test")
async def control_mqtt_test():
    if not mqtt_client or not mqtt_connected:
        raise HTTPException(409, "MQTT is not connected")
    try:
        info = mqtt_client.publish(
            "localai/marcus-computer/test",
            json.dumps({"ok": True, "timestamp": time.time()}),
            qos=1,
            retain=False,
        )
        info.wait_for_publish(timeout=3)
        if not info.is_published():
            raise RuntimeError("broker did not acknowledge publish")
        return {
            "status": "pass",
            "topic": "localai/marcus-computer/test",
            "message_id": info.mid,
        }
    except Exception as exc:
        raise HTTPException(502, f"MQTT publish test failed: {exc}") from exc


@app.post("/control/benchmark/{service}")
async def control_benchmark(service: str):
    if service not in {"llm", "reasoning"}:
        raise HTTPException(400, "benchmark is currently supported for LLM services")
    model_id = "local-fast" if service == "llm" else "local-reasoning"
    lease_id = None
    started = time.perf_counter()
    try:
        lease_id = await acquire_service(service)
        acquired_s = time.perf_counter() - started
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with exactly: benchmark ok"}],
            "max_tokens": 24,
            "temperature": 0,
            "stream": False,
        }
        if service == "llm":
            body["chat_template_kwargs"] = {"enable_thinking": False}
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                SERVICES[service]["base"] + "/v1/chat/completions",
                headers={"Authorization": f"Bearer {LLAMA_API_KEY}", "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        timings = payload.get("timings", {})
        return {
            "service": service,
            "model": model_id,
            "acquire_s": round(acquired_s, 3),
            "prompt_tps": timings.get("prompt_per_second"),
            "generation_tps": timings.get("predicted_per_second"),
            "ms_per_token": timings.get("predicted_per_token_ms"),
            "prompt_tokens": timings.get("prompt_n"),
            "generated_tokens": timings.get("predicted_n"),
        }
    finally:
        if lease_id is not None:
            await release_service(service, lease_id)


@app.websocket("/events")
async def events(websocket: WebSocket):
    supplied = websocket.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, f"Bearer {API_KEY}"):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await build_snapshot())
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        return


@app.post("/control/start/{service}")
async def control_start(service: str):
    return await supervisor("POST", f"/ensure/{service}")

@app.post("/control/stop/{service}")
async def control_stop(service: str):
    return await supervisor("POST", f"/stop/{service}", timeout=60)

@app.post("/control/stop-all")
async def control_stop_all():
    return await supervisor("POST", "/stop-all", timeout=120)

@app.api_route("/v1/chat/completions", methods=["POST"])
async def chat(request: Request):
    raw = await read_body_limited(request, CHAT_MAX_BODY_BYTES, "chat request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")

    requested = str(payload.get("model", "local-fast")).strip().lower()
    canonical = ALIASES.get(requested, requested)
    service = MODEL_SERVICE.get(canonical)
    if service is None:
        raise HTTPException(404, f"unknown model: {requested!r}")
    if service not in {"llm", "reasoning"}:
        raise HTTPException(400, f"model {requested!r} is not a chat model")

    payload["model"] = canonical
    if service == "llm":
        kwargs = payload.get("chat_template_kwargs")
        if kwargs is None:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif isinstance(kwargs, dict):
            kwargs.setdefault("enable_thinking", False)

    body = json.dumps(payload).encode("utf-8")
    if bool(payload.get("stream")):
        return await forward_streaming(service, "/v1/chat/completions", request, body)
    return await forward_buffered(service, "/v1/chat/completions", request, body)

@app.api_route("/v1/audio/transcriptions", methods=["POST"])
async def transcribe(request: Request):
    return await forward_buffered(
        "stt",
        "/v1/audio/transcriptions",
        request,
        max_body_bytes=STT_MAX_UPLOAD_BYTES,
        body_label="STT upload",
    )

@app.api_route("/v1/audio/speech", methods=["POST"])
async def speech(request: Request):
    raw = await read_body_limited(request, TTS_MAX_BODY_BYTES, "TTS request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")

    text = payload.get("input")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "input must be a non-empty string")
    if len(text) > TTS_MAX_INPUT_CHARS:
        raise HTTPException(413, "TTS input exceeds character limit")

    response_format = payload.get("response_format", "mp3")
    if response_format not in TTS_RESPONSE_FORMATS:
        raise HTTPException(400, f"unsupported response_format: {response_format!r}")

    voice = payload.get("voice")
    if voice is not None and not isinstance(voice, str):
        raise HTTPException(400, "voice must be a string")

    speed = payload.get("speed")
    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise HTTPException(400, "speed must be a number")
        if not 0.25 <= float(speed) <= 4.0:
            raise HTTPException(400, "speed must be between 0.25 and 4.0")

    body = json.dumps(payload).encode("utf-8")
    return await forward_buffered("tts", "/v1/audio/speech", request, body)

@app.api_route("/v1/vision/analyze", methods=["POST"])
async def vision(request: Request):
    return await forward_buffered(
        "vlm",
        "/v1/vision/analyze",
        request,
        max_body_bytes=VLM_MAX_UPLOAD_BYTES,
        body_label="VLM upload",
    )

@app.api_route("/v1/comfy/{path:path}", methods=["GET", "POST"])
async def comfy(path: str, request: Request):
    return await forward_buffered(
        "comfyui",
        "/" + path,
        request,
        max_body_bytes=COMFY_MAX_BODY_BYTES,
        body_label="ComfyUI request",
    )

@app.api_route("/v1/wangp/{path:path}", methods=["GET", "POST"])
async def wan(path: str, request: Request):
    return await forward_buffered(
        "wangp",
        "/" + path,
        request,
        max_body_bytes=WANGP_MAX_BODY_BYTES,
        body_label="WanGP request",
    )
