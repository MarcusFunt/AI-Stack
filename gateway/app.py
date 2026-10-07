import asyncio
import io
import json
import logging
import os
import re
import secrets
import struct
import time
import wave
import zlib
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import httpx
import paho.mqtt.client as mqtt
import websockets
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from core.invocation import (
    Invocation,
    InvocationInput,
    InvocationOperation,
    InvocationSource,
    ModelPolicy,
    Modality,
    Principal,
)
from core.router import InvocationRouter, ModelNotFoundError, UnsupportedCapabilityError
from gateway.eval_client import attach_screening_task, report_invocation_screen
from gateway.adapters.openai_audio import OpenAIAudioAdapter
from gateway.adapters.openai_chat import OpenAIChatAdapter
from gateway.adapters.embedding import EmbeddingAdapter
from gateway.adapters.openai_responses import OpenAIResponsesAdapter, ResponsesRequestError, map_chat_stream
from gateway.visual_memory import VisualAnalysisError, VisualAnalysisOrchestrator
from gateway.visual_metrics import VISUAL_METRICS
from observability.propagation import extract_trace_context, inject_trace_context
from observability.openinference import invocation_attributes
from observability.tracing import (
    current_trace_context,
    end_span_handle,
    initialize_tracing,
    start_span,
    start_span_handle,
)

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
EVAL_ROUTER_URL = os.getenv("EVAL_ROUTER_URL", "http://eval-router:8000").rstrip("/")
VOICE_URL = os.getenv("VOICE_URL", "http://voice:8000").rstrip("/")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SERVICES = CONFIG["services"]
MODELS = CONFIG["models"]
ALIASES = {k.lower(): v for k, v in CONFIG.get("aliases", {}).items()}
MODEL_SERVICE = {m["id"].lower(): m["service"] for m in MODELS}
INVOCATION_ROUTER = InvocationRouter(MODELS, aliases=ALIASES)
CHAT_ADAPTER = OpenAIChatAdapter()
AUDIO_ADAPTER = OpenAIAudioAdapter()
EMBEDDING_ADAPTER = EmbeddingAdapter()
RESPONSES_ADAPTER = OpenAIResponsesAdapter()

app = FastAPI(title="Marcus Local AI Gateway", version="0.5.0")
_LOGGER = logging.getLogger(__name__)
initialize_tracing("ai-stack-gateway")
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
STT_RESPONSE_FORMATS = {"json", "verbose_json", "text", "srt", "vtt"}
TTS_VOICE_PRESETS = {
    "Aiden", "Ryan", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee",
}
CHAT_MAX_BODY_BYTES = int(os.getenv("CHAT_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
TTS_MAX_BODY_BYTES = int(os.getenv("TTS_MAX_BODY_BYTES", str(1 * 1024 * 1024)))
STT_MAX_UPLOAD_BYTES = int(os.getenv("STT_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
VLM_MAX_UPLOAD_BYTES = int(os.getenv("VLM_MAX_UPLOAD_BYTES", str(24 * 1024 * 1024)))
VISUAL_MEMORY_IMAGE_MAX_BYTES = int(os.getenv("VISUAL_MEMORY_IMAGE_MAX_BYTES", str(20 * 1024 * 1024)))
VISUAL_CONTEXT_MAX_BODY_BYTES = int(os.getenv("VISUAL_CONTEXT_MAX_BODY_BYTES", str(48 * 1024 * 1024)))
VISUAL_ANALYSIS_REQUEST_MAX_BYTES = int(os.getenv("VISUAL_ANALYSIS_REQUEST_MAX_BYTES", str(42 * 1024 * 1024)))
EMBEDDING_MAX_BODY_BYTES = int(os.getenv("EMBEDDING_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
EMBEDDING_MAX_UPLOAD_BYTES = int(os.getenv("EMBEDDING_MAX_UPLOAD_BYTES", str(24 * 1024 * 1024)))
COMFY_MAX_BODY_BYTES = int(os.getenv("COMFY_MAX_BODY_BYTES", str(128 * 1024 * 1024)))
WANGP_MAX_BODY_BYTES = int(os.getenv("WANGP_MAX_BODY_BYTES", str(512 * 1024 * 1024)))
VOICE_SDP_MAX_BYTES = 128 * 1024

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
    trace_context = extract_trace_context(request.headers)
    request_id = trace_context.request_id
    request.state.trace_context = trace_context
    started = time.perf_counter()
    public_path = request.url.path in {"/health", "/ready"}

    async def dispatch():
        if not public_path:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {API_KEY}"
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid API key", "request_id": request_id},
                )
        return await call_next(request)

    async def dispatch_and_screen():
        try:
            response = await dispatch()
        except Exception:
            invocation = getattr(request.state, "invocation", None)
            if invocation is not None:
                async def report_failure() -> None:
                    try:
                        await report_invocation_screen(
                            invocation,
                            getattr(request.state, "model_route", None),
                            http_status=500,
                            started_at=started,
                            response_bytes=0,
                        )
                    except Exception:
                        return None
                asyncio.create_task(report_failure())
            raise
        attach_screening_task(
            response,
            getattr(request.state, "invocation", None),
            getattr(request.state, "model_route", None),
            started_at=started,
        )
        return response

    traced_routes = {
        "/v1/chat/completions": ("gateway.chat", "chat", "openai_chat"),
        "/v1/responses": ("gateway.responses", "generate", "openai_responses"),
        "/v1/audio/transcriptions": ("gateway.audio.transcription", "transcribe", "openai_audio"),
        "/v1/audio/speech": ("gateway.audio.speech", "synthesize", "openai_audio"),
        "/v1/vision/analyze": ("gateway.vision.analyze", "analyze", "openai_vision"),
        "/v1/vision/analyze-detailed": ("gateway.vision.analyze_detailed", "analyze", "openai_vision"),
        "/v1/vision/compare": ("gateway.vision.compare", "analyze", "openai_vision"),
        "/v1/visual-memory/index": ("gateway.visual_memory.index_image", "embed", "visual_memory"),
        "/v1/visual-memory/index/text": ("gateway.visual_memory.index_text", "embed", "visual_memory"),
        "/v1/visual-memory/search/image": ("gateway.visual_memory.search_image", "embed", "visual_memory"),
        "/v1/visual-memory/search/text": ("gateway.visual_memory.search_text", "embed", "visual_memory"),
        "/v1/visual-memory/status": ("gateway.visual_memory.status", "embed", "visual_memory"),
        "/v1/realtime/sessions": ("gateway.voice.session", "session.create", "websocket_voice"),
    }
    traced_route = traced_routes.get(request.url.path)
    if traced_route:
        span_name, operation, transport = traced_route
        with start_span(
            span_name,
            parent=trace_context,
            attributes={
                "gen_ai.operation.name": operation,
                "ai_stack.transport": transport,
                "http.request.method": request.method,
                "http.route": request.url.path,
            },
        ) as span:
            effective_context = current_trace_context(trace_context, span)
            request.state.trace_context = effective_context
            response = await dispatch_and_screen()
    else:
        response = await dispatch_and_screen()
        effective_context = request.state.trace_context

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Trace-ID"] = effective_context.trace_id
    inject_trace_context(response.headers, effective_context)
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


async def _direct_json(url: str, timeout: float = 5):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[-500:]}")
    return response.json()


def _component(name: str, label: str, state: str, detail: str, required: bool = True, **extra):
    item = {
        "name": name,
        "label": label,
        "state": state,
        "detail": detail,
        "required": required,
    }
    item.update(extra)
    return item


async def build_platform_health():
    components = [
        _component("gateway", "Gateway", "ready", f"v{app.version} · request router and API"),
    ]

    async def add_probe(name, label, awaitable, required=True, ok_states=("ok", "pass", "ready", "healthy")):
        try:
            payload = await awaitable
            raw = str(payload.get("status", "ok")).lower() if isinstance(payload, dict) else "ok"
            if raw in ok_states:
                state = "ready"
            elif raw in {"degraded", "warn", "warning"}:
                state = "warn"
            else:
                state = "error"
            detail = raw
            if isinstance(payload, dict):
                version = payload.get("version")
                service = payload.get("service")
                parts = [str(value) for value in (service, f"v{version}" if version else None) if value]
                if parts:
                    detail = " · ".join(parts)
            components.append(_component(name, label, state, detail, required, payload=payload))
            return payload
        except Exception as exc:
            components.append(_component(name, label, "error", str(exc)[-500:], required))
            return None

    supervisor_payload, telemetry_payload, mcp_payload, lab_payload, evaluator_payload, host_payload = await asyncio.gather(
        add_probe("supervisor", "Supervisor", supervisor("GET", "/health", timeout=5)),
        add_probe("telemetry", "Telemetry", _direct_json(TELEMETRY_URL + "/health")),
        add_probe("mcp", "MCP bridge", _direct_json("http://mcp:8000/healthz")),
        add_probe("agent-lab", "Agent Lab", _direct_json("http://agent-lab:8000/health")),
        add_probe("agent-evaluator", "Agent evaluator", _direct_json("http://agent-evaluator:8000/health")),
        add_probe("host-agent", "Windows host agent", host_agent("GET", "/health", timeout=5)),
    )

    try:
        doctor = await supervisor("GET", "/doctor", timeout=8)
        docker_check = next(
            (item for item in doctor.get("checks", []) if item.get("id") == "docker"),
            None,
        )
        if docker_check:
            state = "ready" if docker_check.get("status") == "pass" else (
                "warn" if docker_check.get("status") == "warn" else "error"
            )
            components.append(_component(
                "docker-control", "Docker control", state,
                str(docker_check.get("detail") or "restricted Docker bridge"),
            ))
        else:
            components.append(_component(
                "docker-control", "Docker control", "warn",
                "Docker dependency was not reported by supervisor doctor",
            ))
    except Exception as exc:
        components.append(_component("docker-control", "Docker control", "error", str(exc)[-500:]))

    if isinstance(lab_payload, dict):
        sandbox = str(lab_payload.get("sandbox", "unknown")).lower()
        components.append(_component(
            "agent-lab-sandbox", "Agent sandbox",
            "ready" if sandbox == "ok" else "error",
            "network-isolated harness runner" if sandbox == "ok" else sandbox,
        ))

    try:
        network = await host_agent("GET", "/tailscale/status", timeout=8)
        components.append(_component(
            "tailscale", "Tailscale",
            "ready" if network.get("online") else "error",
            (
                ("online · " + str(network.get("dns_name"))) if network.get("online")
                else str(network.get("status_error") or "offline")
            ),
            True,
            payload={
                "dashboard_enabled": network.get("dashboard_enabled"),
                "studio_enabled": network.get("studio_enabled"),
                "mcp_mode": network.get("mcp_mode"),
            },
        ))
    except Exception as exc:
        components.append(_component("tailscale", "Tailscale", "error", str(exc)[-500:]))

    try:
        open_code = await host_agent("GET", "/opencode/status", timeout=8)
        if not open_code.get("installed"):
            state, detail = "stopped", "not installed"
        elif open_code.get("server_running"):
            state, detail = "ready", "server running" + (f" · {open_code.get('version')}" if open_code.get("version") else "")
        else:
            state, detail = "stopped", "installed · server stopped"
        components.append(_component(
            "opencode", "OpenCode", state, detail, required=False,
            payload={
                "installed": open_code.get("installed"),
                "version": open_code.get("version"),
                "server_running": open_code.get("server_running"),
            },
        ))
    except Exception as exc:
        components.append(_component("opencode", "OpenCode", "warn", str(exc)[-500:], required=False))

    required_states = [item["state"] for item in components if item.get("required")]
    optional_bad = any(item["state"] in {"warn", "error"} for item in components if not item.get("required"))
    if any(state == "error" for state in required_states):
        overall = "error"
    elif any(state == "warn" for state in required_states) or optional_bad:
        overall = "warn"
    else:
        overall = "ready"
    return {
        "status": overall,
        "timestamp": time.time(),
        "components": components,
    }


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


def request_workload_profile(request: Request) -> str:
    """Accept a known workload hint; supervisor still applies resource admission."""
    requested = request.headers.get("x-ai-stack-profile", "interactive").strip()
    profiles = CONFIG.get("workload_profiles", {})
    return requested if requested in profiles else "interactive"


async def acquire_service_for_request(service: str, request: Request):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service: {service}")
    lease_id = secrets.token_urlsafe(18)
    profile = request_workload_profile(request)
    try:
        await supervisor("POST", f"/acquire/{service}?lease_id={lease_id}&profile={profile}")
    except Exception:
        try:
            await supervisor("POST", f"/release/{service}?lease_id={lease_id}", timeout=10)
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
                    json={"model": "local-tts", "input": "Self test.", "voice": "Aiden", "response_format": "wav"},
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
    trace_context = getattr(request.state, "trace_context", None)
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    if SERVICES[service].get("backend_auth") == "llama":
        headers["Authorization"] = f"Bearer {LLAMA_API_KEY}"
    return headers


def provider_span_attributes(request: Request, service: str, path: str) -> dict:
    invocation = getattr(request.state, "invocation", None)
    route = getattr(request.state, "model_route", None)
    attributes = invocation_attributes(
        invocation,
        provider=route.provider_id if route is not None else service,
        service=service,
    ) if invocation is not None else {
        "openinference.span.kind": "LLM",
        "ai_stack.service": service,
    }
    if route is not None:
        attributes["gen_ai.request.model"] = route.model_id
    attributes["http.request.method"] = request.method
    attributes["http.route"] = path
    return attributes

def response_headers(response: httpx.Response, buffered: bool = False):
    blocked = HOP_BY_HOP_HEADERS | {"content-length"}
    if buffered:
        blocked.add("content-encoding")
    return {k: v for k, v in response.headers.items() if k.lower() not in blocked}

async def parse_form_body(request: Request, body: bytes):
    """Parse already size-limited multipart data without mutating request internals."""
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    parser_request = Request(request.scope, receive)
    return await parser_request.form()


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
    content_type: str | None = None,
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
        lease_id = await acquire_service_for_request(service, request)
        update_job(job, state="running", phase=PHASE_FOR_SERVICE.get(service, "processing"))
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                with start_span(
                    f"gateway.provider.{service}",
                    parent=getattr(request.state, "trace_context", None),
                    attributes=provider_span_attributes(request, service, path),
                ) as span:
                    headers = upstream_headers(request, service)
                    if content_type:
                        headers["Content-Type"] = content_type
                    upstream = await client.request(
                        request.method,
                        SERVICES[service]["base"] + path,
                        params=request.query_params,
                        content=payload,
                        headers=headers,
                    )
                    span.set_attribute("http.response.status_code", upstream.status_code)
                    span.set_attribute("http.response.body.size", len(upstream.content))
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
    provider_span = None
    provider_trace = None
    provider_span_started = None
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
                if provider_span is not None:
                    provider_span.set_attribute("ai_stack.stream.status", final_state)
                    if upstream is not None:
                        provider_span.set_attribute("http.response.status_code", upstream.status_code)
                    if final_error is not None:
                        provider_span.set_attribute("ai_stack.stream.error_type", type(final_error).__name__)
                    end_span_handle(provider_span, final_error)
                if job["id"] in ACTIVE_JOBS:
                    finish_job(job, state=final_state, error=final_error)
                released = True

    try:
        lease_id = await acquire_service_for_request(service, request)
        update_job(job, state="running", phase=PHASE_FOR_SERVICE.get(service, "streaming"))
        parent_trace = getattr(request.state, "trace_context", None)
        provider_span = start_span_handle(
            f"gateway.provider.{service}.stream",
            parent=parent_trace,
            attributes=provider_span_attributes(request, service, path),
        )
        provider_trace = current_trace_context(parent_trace, provider_span)
        provider_span_started = time.perf_counter()
        client = httpx.AsyncClient(timeout=None)
        headers = upstream_headers(request, service)
        inject_trace_context(headers, provider_trace)
        upstream_request = client.build_request(
            request.method,
            SERVICES[service]["base"] + path,
            params=request.query_params,
            content=body,
            headers=headers,
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
        first_chunk_at = None
        try:
            try:
                async for chunk in upstream.aiter_raw():
                    if await request.is_disconnected():
                        final_state = "cancelled"
                        break
                    chunks += max(1, chunk.count(b"data:"))
                    byte_count += len(chunk)
                    if first_chunk_at is None:
                        first_chunk_at = time.perf_counter()
                        if provider_span_started is not None:
                            provider_span.set_attribute(
                                "gen_ai.server.time_to_first_token_ms",
                                (first_chunk_at - provider_span_started) * 1000,
                            )
                    provider_span.set_attribute("http.response.body.size", byte_count)
                    update_job(
                        job,
                        progress_units=chunks,
                        metrics={"stream_bytes": byte_count, "chunks": chunks},
                    )
                    yield chunk
            except httpx.RequestError as exc:
                final_state = "failed"
                final_error = exc
                raise
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
        return {"status": "ok", "service": "gateway", "version": app.version}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "service": "gateway", "version": app.version},
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
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            voice_response = await client.get(VOICE_URL + "/metrics")
            voice_response.raise_for_status()
            voice_metrics = voice_response.text.rstrip()
    except Exception:
        voice_metrics = ""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            memory_response = await client.get(SERVICES["visual-memory"]["base"] + "/metrics")
            memory_response.raise_for_status()
            memory_metrics = memory_response.text.rstrip()
    except Exception:
        memory_metrics = ""
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
        voice_metrics,
        memory_metrics,
        VISUAL_METRICS.render_prometheus().rstrip(),
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
            "responses": "/v1/responses",
            "transcription": "/v1/audio/transcriptions",
            "speech": "/v1/audio/speech",
            "vision": "/v1/vision/analyze",
            "vision_detailed": "/v1/vision/analyze-detailed",
            "vision_compare": "/v1/vision/compare",
            "visual_memory": {
                "status": "/v1/visual-memory/status",
                "index_image": "/v1/visual-memory/index",
                "index_text": "/v1/visual-memory/index/text",
                "search_image": "/v1/visual-memory/search/image",
                "search_text": "/v1/visual-memory/search/text",
            },
            "realtime": "/v1/realtime/sessions",
            "models": "/v1/models",
            "status": "/v1/system/status",
        },
        "audio": {
            "transcription": {
                "method": "POST",
                "parameters": ["file", "model", "language", "prompt", "temperature", "response_format", "timestamp_granularities[]"],
                "response_formats": sorted(STT_RESPONSE_FORMATS),
                "timestamp_granularities": ["segment", "word"],
                "max_file_bytes": 20 * 1024 * 1024,
                "language": "Optional Whisper language code; omitted means automatic detection.",
            },
            "speech": {
                "method": "POST",
                "parameters": ["model", "input", "voice", "language", "instruct", "speed", "response_format"],
                "response_formats": sorted(TTS_RESPONSE_FORMATS),
                "voices": sorted(TTS_VOICE_PRESETS),
                "language": "English",
                "expressive_instructions": (
                    "Optional free-form delivery guidance for emotion, tone, pace, and prosody. "
                    "Exact effects such as laughter, sarcasm, or pause timing are model-dependent."
                ),
            },
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


@app.get("/control/platform-health")
async def control_platform_health():
    return await build_platform_health()


@app.get("/control/opencode")
async def control_opencode():
    return await host_agent("GET", "/opencode/status", timeout=10)


@app.post("/control/opencode/start")
async def control_opencode_start():
    return await host_agent("POST", "/opencode/start", payload={}, timeout=140)


@app.post("/control/opencode/stop")
async def control_opencode_stop():
    return await host_agent("POST", "/opencode/stop", payload={}, timeout=30)


@app.get("/control/maintenance")
async def control_maintenance():
    operations, snapshots, open_code = await asyncio.gather(
        host_agent("GET", "/operations", timeout=10),
        host_agent("GET", "/maintenance/snapshots", timeout=10),
        host_agent("GET", "/opencode/status", timeout=10),
    )
    return {
        "operations": operations.get("operations", []),
        "snapshots": snapshots.get("snapshots", []),
        "opencode": open_code,
    }


@app.post("/control/maintenance/start")
async def control_maintenance_start(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "maintenance request must be an object")
    action = str(payload.get("action", "")).strip().lower()
    allowed = {"update", "rollback", "burn-in", "opencode-smoke", "voice-smoke"}
    if action not in allowed:
        raise HTTPException(400, "unsupported maintenance action")
    if action in {"update", "rollback", "burn-in", "voice-smoke"}:
        status = await supervisor("GET", "/status", timeout=10)
        busy = {
            name: count for name, count in status.get("active_jobs", {}).items()
            if int(count or 0) > 0
        }
        if busy:
            raise HTTPException(409, f"active AI jobs prevent maintenance: {busy}")
    return await host_agent("POST", "/operations/start", payload=payload, timeout=20)


@app.get("/control/maintenance/{operation_id}")
async def control_maintenance_operation(operation_id: str):
    if not re.fullmatch(r"op-[a-f0-9]{12}", operation_id):
        raise HTTPException(400, "invalid operation id")
    return await host_agent("GET", f"/operations/{operation_id}", timeout=10)


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


def _is_valid_turn_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".")
    return bool(normalized) and re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
        normalized,
    ) is not None


def _retarget_turn_ice_servers(ice_servers: list, hostname: str) -> bool:
    hostname = hostname.rstrip(".")
    if not _is_valid_turn_hostname(hostname):
        return False

    found_turn = False
    for server in ice_servers:
        if not isinstance(server, dict):
            continue
        urls = server.get("urls")
        is_list = isinstance(urls, list)
        candidates = urls if is_list else [urls]
        if not all(isinstance(url, str) for url in candidates):
            continue
        rewritten = []
        for url in candidates:
            if not url.lower().startswith(("turn:", "turns:")):
                rewritten.append(url)
                continue
            found_turn = True
            replacement, count = re.subn(
                r"^(turns?:)[^:/?#]+",
                lambda match: match.group(1) + hostname,
                url,
                count=1,
                flags=re.IGNORECASE,
            )
            if count != 1:
                return False
            rewritten.append(replacement)
        server["urls"] = rewritten if is_list else rewritten[0]
    return found_turn


def _has_turn_ice_server(ice_servers: list) -> bool:
    for server in ice_servers:
        if not isinstance(server, dict):
            continue
        urls = server.get("urls")
        candidates = [urls] if isinstance(urls, str) else urls if isinstance(urls, list) else []
        if any(
            isinstance(url, str) and url.lower().startswith(("turn:", "turns:"))
            for url in candidates
        ):
            return True
    return False


def _verified_turn_hostname(network) -> tuple[str | None, str | None]:
    if not isinstance(network, dict):
        return None, "invalid-status"
    if network.get("route_state_available") is not True:
        return None, "route-state-unavailable"
    if network.get("online") is not True:
        return None, "tailscale-offline"
    if network.get("voice_turn_route_present") is not True:
        return None, "route-missing"
    if network.get("voice_turn_listener_ready") is not True:
        return None, "listener-not-ready"
    if network.get("voice_turn_funnel_enabled") is not False:
        return None, "funnel-state-unverified-or-enabled"
    if network.get("voice_turn_enabled") is not True:
        return None, "route-disabled"
    hostname = network.get("dns_name")
    if not isinstance(hostname, str) or not _is_valid_turn_hostname(hostname):
        return None, "dns-name-invalid"
    return hostname.rstrip("."), None


def _host_agent_route_reason(network) -> str | None:
    if not isinstance(network, dict):
        return "invalid-status"
    if network.get("route_state_available") is not True:
        return "route-state-unavailable"
    if network.get("online") is not True:
        return "tailscale-offline"
    if not isinstance(network.get("voice_turn_enabled"), bool):
        return "turn-enabled-state-unavailable"
    return None


def _raise_realtime_route_error(
    request: Request,
    session_id: str,
    reason: str,
    host_agent_status: int | None,
    detail: str,
) -> None:
    trace_context = getattr(request.state, "trace_context", None)
    request_id = getattr(trace_context, "request_id", None) or "unavailable"
    _LOGGER.warning(
        "Realtime voice route could not be verified "
        f"session_id={session_id} request_id={request_id} "
        f"reason={reason} host_agent_status={host_agent_status}",
        extra={
            "voice_session_id": session_id,
            "request_id": request_id,
            "voice_route_reason": reason,
            "host_agent_status": host_agent_status,
        },
    )
    raise HTTPException(503, detail)


@app.post("/v1/realtime/sessions")
async def create_realtime_session(request: Request):
    raw = await read_body_limited(request, 16 * 1024, "voice session configuration")
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "session configuration must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "session configuration must be a JSON object")
    if payload.get("language", "en") != "en":
        raise HTTPException(400, "realtime voice currently supports English only")

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    trace_context = getattr(request.state, "trace_context", None)
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            upstream = await client.post(VOICE_URL + "/sessions", headers=headers, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(503, "realtime voice service is unavailable") from exc
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, "realtime voice session could not be created")
    try:
        result = upstream.json()
    except ValueError as exc:
        raise HTTPException(502, "realtime voice service returned an invalid session") from exc
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["id"])
    ):
        raise HTTPException(502, "realtime voice service returned an invalid session")

    ice_servers = result.get("ice_servers")
    ice_route = {"kind": "direct", "transport": None, "port": None}
    try:
        network = await host_agent("GET", "/tailscale/status", timeout=3)
    except HTTPException as exc:
        _raise_realtime_route_error(
            request,
            result["id"],
            "host-agent-unavailable",
            exc.status_code,
            "The TURN route could not be verified because the Tailscale host-agent check failed. Check the host agent and Tailscale network, then retry.",
        )
    except ValueError:
        _raise_realtime_route_error(
            request,
            result["id"],
            "invalid-host-agent-response",
            None,
            "The TURN route could not be verified because the Tailscale host-agent returned an invalid response. Check the host agent, then retry.",
        )

    host_status_reason = _host_agent_route_reason(network)
    if host_status_reason:
        _raise_realtime_route_error(
            request,
            result["id"],
            host_status_reason,
            200,
            "The TURN route could not be verified from the Tailscale host-agent status. Check Tailscale and the host agent, then retry.",
        )

    has_turn = isinstance(ice_servers, list) and _has_turn_ice_server(ice_servers)
    turn_enabled = network["voice_turn_enabled"]
    if has_turn or turn_enabled:
        hostname, route_reason = _verified_turn_hostname(network)
        if hostname is None:
            _raise_realtime_route_error(
                request,
                result["id"],
                route_reason or "turn-route-unverified",
                200,
                "The private TURN route could not be verified. Check the Tailscale voice route and TURN listener, then retry.",
            )
        if not has_turn:
            _raise_realtime_route_error(
                request,
                result["id"],
                "voice-turn-configuration-missing",
                200,
                "Tailscale TURN is enabled, but the voice service returned no TURN relay. Check the voice TURN credentials and restart the voice service before retrying.",
            )
        if not _retarget_turn_ice_servers(ice_servers, hostname):
            _raise_realtime_route_error(
                request,
                result["id"],
                "ice-route-invalid",
                200,
                "The private TURN route could not be verified. Check the Tailscale voice route and TURN listener, then retry.",
            )
        ice_route = {"kind": "tailnet-turn", "transport": "tls/tcp", "port": 8447}

    result["ice_route"] = ice_route

    scheme = "wss" if request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https" else "ws"
    result["ws_url"] = f"{scheme}://{request.url.netloc}/v1/realtime?session_id={result['id']}"
    result["offer_url"] = f"/v1/realtime/sessions/{result['id']}/offer"
    return result


@app.post("/v1/realtime/sessions/{session_id}/offer")
async def proxy_realtime_offer(request: Request, session_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise HTTPException(400, "invalid realtime session id")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "offer must use application/json")
    ticket = request.headers.get("x-voice-session-ticket", "")
    if not ticket or len(ticket) > 128:
        raise HTTPException(401, "invalid realtime session ticket")

    raw = await read_body_limited(request, VOICE_SDP_MAX_BYTES, "WebRTC offer")
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "offer must be valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "offer"
        or not isinstance(payload.get("sdp"), str)
        or not payload["sdp"].strip()
    ):
        raise HTTPException(400, "offer must include type=offer and non-empty sdp")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-Voice-Session-Ticket": ticket,
    }
    trace_context = getattr(request.state, "trace_context", None)
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.post(
                f"{VOICE_URL}/sessions/{session_id}/offer",
                headers=headers,
                json={"type": "offer", "sdp": payload["sdp"]},
            )
    except httpx.RequestError as exc:
        raise HTTPException(503, "realtime voice service is unavailable") from exc
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, "realtime voice offer was rejected")
    if len(upstream.content) > VOICE_SDP_MAX_BYTES:
        raise HTTPException(502, "realtime voice returned an oversized answer")
    try:
        answer = upstream.json()
    except ValueError as exc:
        raise HTTPException(502, "realtime voice returned an invalid answer") from exc
    if (
        not isinstance(answer, dict)
        or answer.get("type") != "answer"
        or not isinstance(answer.get("sdp"), str)
        or not answer["sdp"].strip()
        or len(answer["sdp"].encode("utf-8")) > VOICE_SDP_MAX_BYTES
    ):
        raise HTTPException(502, "realtime voice returned an invalid answer")
    return {"sdp": answer["sdp"], "type": "answer"}


@app.post("/internal/voice-evaluations")
async def report_voice_evaluation(request: Request):
    raw = await read_body_limited(request, 16 * 1024, "voice evaluation event")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "voice evaluation event must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "voice evaluation event must be a JSON object")

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    trace_context = getattr(request.state, "trace_context", None)
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            upstream = await client.post(EVAL_ROUTER_URL + "/v1/voice-evaluations", headers=headers, json=payload)
        if upstream.status_code >= 400:
            _LOGGER.warning("voice evaluation router rejected an event", extra={"http_status": upstream.status_code})
            return JSONResponse({"status": "dropped"}, status_code=202)
        return JSONResponse({"status": "queued"}, status_code=202)
    except Exception as exc:
        _LOGGER.warning("voice evaluation forwarding failed", extra={"error_type": type(exc).__name__})
        return JSONResponse({"status": "unavailable"}, status_code=202)


@app.websocket("/v1/realtime")
async def realtime_proxy(websocket: WebSocket):
    session_id = websocket.query_params.get("session_id", "")
    subprotocols = websocket.scope.get("subprotocols", [])
    if not session_id or len(session_id) > 64 or "ai-stack.voice.v1" not in subprotocols:
        await websocket.close(code=4401)
        return
    ticket = next((value for value in subprotocols if value.startswith("ai-stack.ticket.")), "")
    if not ticket or len(ticket) > 144:
        await websocket.close(code=4401)
        return

    upstream_url = VOICE_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    upstream_url += f"/ws/{session_id}"
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            proxy=None,
        ) as upstream:
            await websocket.accept(subprotocol="ai-stack.voice.v1")

            async def client_to_voice():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    value = message.get("text") if message.get("text") is not None else message.get("bytes")
                    if value is not None:
                        await upstream.send(value)

            async def voice_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(client_to_voice()),
                asyncio.create_task(voice_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, websockets.ConnectionClosed, RuntimeError):
                    pass
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                pass
    except (OSError, websockets.WebSocketException):
        try:
            await websocket.close(code=1013, reason="realtime voice service unavailable")
        except RuntimeError:
            pass


@app.post("/control/start/{service}")
async def control_start(service: str):
    return await supervisor("POST", f"/ensure/{service}")

@app.post("/control/stop/{service}")
async def control_stop(service: str):
    return await supervisor("POST", f"/stop/{service}", timeout=60)

@app.post("/control/stop-all")
async def control_stop_all():
    return await supervisor("POST", "/stop-all", timeout=120)

@app.get("/control/logs/{service}")
async def control_logs(service: str, tail: int = Query(default=200, ge=1, le=1000)):
    return await supervisor("GET", f"/logs/{service}?tail={tail}", timeout=15)

@app.api_route("/v1/chat/completions", methods=["POST"])
async def chat(request: Request):
    raw = await read_body_limited(request, CHAT_MAX_BODY_BYTES, "chat request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")

    invocation = CHAT_ADAPTER.to_invocation(payload, request.state.trace_context)
    request.state.invocation = invocation
    requested = invocation.requested_model.lower()
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, f"unknown model: {requested!r}") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, f"model {requested!r} is not a chat model") from exc

    request.state.model_route = route
    canonical = route.model_id.lower()
    service = route.provider_id
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

@app.post("/v1/responses")
async def responses(request: Request):
    raw = await read_body_limited(request, CHAT_MAX_BODY_BYTES, "responses request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")
    try:
        chat_payload = RESPONSES_ADAPTER.to_chat_request(payload)
    except ResponsesRequestError as exc:
        raise HTTPException(400, str(exc)) from exc

    invocation = CHAT_ADAPTER.to_invocation(
        chat_payload,
        request.state.trace_context,
        source=InvocationSource.OPENAI_RESPONSES,
    )
    request.state.invocation = invocation
    requested = invocation.requested_model.lower()
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, f"unknown model: {requested!r}") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, f"model {requested!r} is not a chat model") from exc

    request.state.model_route = route
    service = route.provider_id
    chat_payload["model"] = route.model_id.lower()
    # Responses metadata belongs to the response and canonical invocation, not the chat backend.
    chat_payload.pop("metadata", None)
    if service == "llm":
        kwargs = chat_payload.get("chat_template_kwargs")
        if kwargs is None:
            chat_payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif isinstance(kwargs, dict):
            kwargs.setdefault("enable_thinking", False)
    body = json.dumps(chat_payload).encode("utf-8")
    if chat_payload.get("stream"):
        upstream = await forward_streaming(service, "/v1/chat/completions", request, body)
        if upstream.status_code >= 400:
            return upstream
        headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length", "content-type"}
        }
        response_id = f"resp_{secrets.token_hex(16)}"
        return StreamingResponse(
            map_chat_stream(
                upstream.body_iterator,
                adapter=RESPONSES_ADAPTER,
                request_payload=payload,
                response_id=response_id,
            ),
            status_code=upstream.status_code,
            headers=headers,
            media_type="text/event-stream",
        )

    upstream = await forward_buffered(service, "/v1/chat/completions", request, body)
    if upstream.status_code >= 400:
        return upstream
    try:
        chat_response = json.loads(upstream.body)
        response_payload = RESPONSES_ADAPTER.from_chat_response(chat_response, payload)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError, ResponsesRequestError) as exc:
        raise HTTPException(502, "backend returned an invalid chat completion") from exc
    headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length", "content-type"}
    }
    return JSONResponse(response_payload, status_code=upstream.status_code, headers=headers)


@app.api_route("/v1/audio/transcriptions", methods=["POST"])
async def transcribe(request: Request):
    raw = await read_body_limited(request, STT_MAX_UPLOAD_BYTES, "STT upload")
    try:
        form = await parse_form_body(request, raw)
    except Exception:
        form = {}
    if form:
        response_format = form.get("response_format", "json")
        if response_format not in STT_RESPONSE_FORMATS:
            raise HTTPException(400, f"unsupported response_format: {response_format!r}")
        prompt = form.get("prompt")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > 4000):
            raise HTTPException(400, "prompt must be a string of at most 4000 characters")
        try:
            temperature = float(form.get("temperature", "0"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "temperature must be a number between 0 and 1") from exc
        if not 0.0 <= temperature <= 1.0:
            raise HTTPException(400, "temperature must be between 0 and 1")
        granularities = form.getlist("timestamp_granularities[]") if hasattr(form, "getlist") else []
        if any(value not in {"segment", "word"} for value in granularities):
            raise HTTPException(400, "timestamp_granularities[] values must be 'segment' or 'word'")
    invocation = AUDIO_ADAPTER.to_transcription(form, request.state.trace_context)
    request.state.invocation = invocation
    route = INVOCATION_ROUTER.resolve(invocation)
    request.state.model_route = route
    if hasattr(form, "close"):
        await form.close()
    return await forward_buffered(
        route.provider_id,
        "/v1/audio/transcriptions",
        request,
        body=raw,
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

    voice = payload.get("voice", "Aiden")
    if not isinstance(voice, str):
        raise HTTPException(400, "voice must be a string")
    if voice not in TTS_VOICE_PRESETS:
        raise HTTPException(400, f"unsupported voice: {voice!r}")

    language = payload.get("language", "English")
    if not isinstance(language, str) or language.strip().casefold() not in {"english", "en", "en-us", "en-gb"}:
        raise HTTPException(400, "TTS language is English-only")
    payload["voice"] = voice
    payload["language"] = "English"

    instruct = payload.get("instruct")
    if instruct is not None:
        if not isinstance(instruct, str):
            raise HTTPException(400, "instruct must be a string")
        if len(instruct) > 2000:
            raise HTTPException(413, "instruct exceeds 2000 characters")

    speed = payload.get("speed")
    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise HTTPException(400, "speed must be a number")
        if not 0.25 <= float(speed) <= 4.0:
            raise HTTPException(400, "speed must be between 0.25 and 4.0")

    invocation = AUDIO_ADAPTER.to_speech(payload, request.state.trace_context)
    request.state.invocation = invocation
    route = INVOCATION_ROUTER.resolve(invocation)
    request.state.model_route = route
    body = json.dumps(payload).encode("utf-8")
    return await forward_buffered(route.provider_id, "/v1/audio/speech", request, body)

@app.post("/v1/embeddings/text")
async def embeddings_text(request: Request):
    raw = await read_body_limited(request, EMBEDDING_MAX_BODY_BYTES, "embedding request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")
    try:
        invocation = EMBEDDING_ADAPTER.to_text(payload, request.state.trace_context)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    request.state.invocation = invocation
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, "no configured embedding model") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, "requested model does not support embeddings") from exc
    request.state.model_route = route
    return await forward_buffered(
        route.provider_id,
        "/v1/embeddings/text",
        request,
        body=raw,
        max_body_bytes=EMBEDDING_MAX_BODY_BYTES,
        body_label="embedding request",
    )


@app.post("/v1/embeddings/image")
async def embeddings_image(request: Request):
    raw = await read_body_limited(request, EMBEDDING_MAX_UPLOAD_BYTES, "embedding image upload")
    try:
        form = await parse_form_body(request, raw)
    except Exception as exc:
        raise HTTPException(400, "request must be valid multipart form data") from exc
    try:
        try:
            invocation = EMBEDDING_ADAPTER.to_image(form, request.state.trace_context)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        if hasattr(form, "close"):
            await form.close()
    request.state.invocation = invocation
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, "no configured embedding model") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, "requested model does not support embeddings") from exc
    request.state.model_route = route
    return await forward_buffered(
        route.provider_id,
        "/v1/embeddings/image",
        request,
        body=raw,
        max_body_bytes=EMBEDDING_MAX_UPLOAD_BYTES,
        body_label="embedding image upload",
    )


async def _gateway_visual_memory_proxy(request: Request, path: str, *, image_upload: bool):
    body_limit = EMBEDDING_MAX_UPLOAD_BYTES if image_upload else EMBEDDING_MAX_BODY_BYTES
    raw = await read_body_limited(request, body_limit, "visual memory request")
    data: dict = {"path": path}
    if image_upload:
        try:
            form = await parse_form_body(request, raw)
        except Exception as exc:
            raise HTTPException(400, "request must be valid multipart form data") from exc
        try:
            images = form.getlist("image")
            if len(images) != 1 or not callable(getattr(images[0], "read", None)):
                raise HTTPException(400, "exactly one image upload is required")
            image = images[0]
            image_bytes = await image.read(VISUAL_MEMORY_IMAGE_MAX_BYTES + 1)
            if not image_bytes:
                raise HTTPException(400, "image upload is empty")
            if len(image_bytes) > VISUAL_MEMORY_IMAGE_MAX_BYTES:
                raise HTTPException(413, "image upload exceeds byte limit")
            data["image"] = {
                "filename": getattr(image, "filename", None),
                "mime_type": getattr(image, "content_type", None),
                "size_bytes": len(image_bytes),
            }
        finally:
            await form.close()
    else:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        for key in ("text", "query"):
            if isinstance(payload.get(key), str):
                data[key] = payload[key][:32_000]

    invocation = Invocation(
        trace_context=getattr(request.state, "trace_context", None),
        operation=InvocationOperation.EMBED,
        modality={Modality.IMAGE if image_upload else Modality.TEXT},
        source=InvocationSource.INTERNAL,
        principal=Principal(
            id="gateway-visual-memory-client",
            kind="api_key",
            scopes=frozenset({"visual-memory:read", "visual-memory:write"}),
        ),
        model_policy=ModelPolicy(capability="embedding"),
        input=InvocationInput(text=data.get("query") or data.get("text"), data=data),
    )
    request.state.invocation = invocation
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, "no configured embedding model") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, "configured model does not support embeddings") from exc
    request.state.model_route = route
    return await forward_buffered(
        route.provider_id,
        path,
        request,
        body=raw,
        max_body_bytes=body_limit,
        body_label="visual memory request",
    )


@app.post("/v1/visual-memory/index")
async def gateway_visual_memory_index_image(request: Request):
    return await _gateway_visual_memory_proxy(request, "/v1/visual-memory/index", image_upload=True)


@app.post("/v1/visual-memory/index/text")
async def gateway_visual_memory_index_text(request: Request):
    return await _gateway_visual_memory_proxy(request, "/v1/visual-memory/index/text", image_upload=False)


@app.post("/v1/visual-memory/search/image")
async def gateway_visual_memory_search_image(request: Request):
    return await _gateway_visual_memory_proxy(request, "/v1/visual-memory/search/image", image_upload=True)


@app.post("/v1/visual-memory/search/text")
async def gateway_visual_memory_search_text(request: Request):
    return await _gateway_visual_memory_proxy(request, "/v1/visual-memory/search/text", image_upload=False)


@app.get("/v1/visual-memory/status")
async def gateway_visual_memory_status(request: Request):
    invocation = Invocation(
        trace_context=getattr(request.state, "trace_context", None),
        operation=InvocationOperation.EMBED,
        modality={Modality.STRUCTURED},
        source=InvocationSource.INTERNAL,
        principal=Principal(id="gateway-visual-memory-status", kind="service", scopes=frozenset({"visual-memory:read"})),
        model_policy=ModelPolicy(capability="embedding"),
        input=InvocationInput(data={"diagnostics": True}),
    )
    request.state.invocation = invocation
    try:
        route = INVOCATION_ROUTER.resolve(invocation)
    except ModelNotFoundError as exc:
        raise HTTPException(404, "no configured embedding model") from exc
    except UnsupportedCapabilityError as exc:
        raise HTTPException(400, "configured model does not support embeddings") from exc
    request.state.model_route = route
    return await forward_buffered(
        route.provider_id,
        "/diagnostics",
        request,
        max_body_bytes=0,
        body_label="visual-memory diagnostics request",
    )


def _visual_form_text(form, name: str, default: str = "") -> str:
    value = form.get(name, default)
    return value if isinstance(value, str) else default


def _visual_form_bool(form, name: str, default: bool = False) -> bool:
    value = form.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise HTTPException(400, f"{name} must be true or false")


def _visual_optional_text(form, name: str, limit: int) -> str | None:
    value = form.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HTTPException(400, f"{name} is invalid or too long")
    return value


def _visual_context_json(form) -> dict:
    raw = _visual_form_text(form, "context_json", "{}")
    if len(raw.encode("utf-8")) > 64_000:
        raise HTTPException(413, "context_json exceeds byte limit")
    try:
        context = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "context_json must be valid JSON") from exc
    if not isinstance(context, dict):
        raise HTTPException(400, "context_json must be a JSON object")
    return context


def _visual_tags(form) -> list[str]:
    raw = _visual_form_text(form, "tags_json", "[]")
    if len(raw.encode("utf-8")) > 32_000:
        raise HTTPException(413, "tags_json exceeds byte limit")
    try:
        tags = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "tags_json must be valid JSON") from exc
    if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(tag, str) or len(tag) > 256 for tag in tags):
        raise HTTPException(400, "tags_json must be an array of at most 100 short strings")
    return tags


async def _visual_upload(form, name: str, *, required: bool, limit: int):
    upload = form.get(name)
    if upload is None:
        if required:
            raise HTTPException(400, f"{name} image is required")
        return None
    if isinstance(upload, str) or not callable(getattr(upload, "read", None)):
        raise HTTPException(400, f"{name} must be an uploaded image")
    data = await upload.read(limit + 1)
    if not data:
        raise HTTPException(400, f"{name} image is empty")
    if len(data) > limit:
        raise HTTPException(413, f"{name} image exceeds byte limit")
    filename = getattr(upload, "filename", None) or f"{name}.img"
    media_type = getattr(upload, "content_type", None) or "application/octet-stream"
    return filename, media_type, data


def _visual_integer(form, name: str, *, default: int | None, minimum: int, maximum: int) -> int | None:
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str) or not raw.isdecimal():
        raise HTTPException(400, f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise HTTPException(400, f"{name} must be between {minimum} and {maximum}")
    return value


async def _detailed_visual_analysis(request: Request, *, compare: bool):
    request_limit = VISUAL_ANALYSIS_REQUEST_MAX_BYTES if compare else VLM_MAX_UPLOAD_BYTES
    raw = await read_body_limited(request, request_limit, "visual analysis upload")
    try:
        form = await parse_form_body(request, raw)
    except Exception as exc:
        raise HTTPException(400, "request must be valid multipart form data") from exc

    try:
        if len(form.getlist("image")) != 1:
            raise HTTPException(400, "exactly one current image is required")
        if len(form.getlist("reference")) > 1 or len(form.getlist("reference_id")) > 1:
            raise HTTPException(400, "compare accepts only one reference")
        profile = _visual_form_text(form, "analysis_profile", "generic")
        if profile not in {"generic", "website", "game"}:
            raise HTTPException(400, "analysis_profile must be generic, website, or game")
        skip_if_identical_frame = _visual_form_bool(form, "skip_if_identical_frame")
        retention_policy = _visual_form_text(form, "retention_policy", "full-image")
        structured_context = _visual_context_json(form)
        image = await _visual_upload(form, "image", required=True, limit=VISUAL_MEMORY_IMAGE_MAX_BYTES)
        assert image is not None
        reference_image = await _visual_upload(
            form, "reference", required=False, limit=VISUAL_MEMORY_IMAGE_MAX_BYTES
        )
        reference_id = _visual_optional_text(form, "reference_id", 128)
        reference_namespace = _visual_optional_text(form, "reference_namespace", 256)
        if compare and bool(reference_id) == bool(reference_image):
            raise HTTPException(400, "compare requires exactly one of reference_id or reference image")
        if not compare and (reference_id or reference_image or reference_namespace):
            raise HTTPException(400, "reference fields are only accepted by /v1/vision/compare")
        if compare and skip_if_identical_frame:
            raise HTTPException(400, "skip_if_identical_frame is only available for detailed analysis")

        namespace = _visual_optional_text(form, "namespace", 256)
        namespace = namespace or {
            "generic": "visual:default",
            "website": "website:default",
            "game": "game:default",
        }[profile]
        code_namespace = _visual_optional_text(form, "code_namespace", 256)
        source = _visual_optional_text(form, "source", 128) or profile
        session_id = _visual_optional_text(form, "session_id", 256)
        sequence_id = _visual_optional_text(form, "sequence_id", 256)
        sequence_number = _visual_integer(form, "sequence_number", default=None, minimum=0, maximum=2**63 - 1)
        timestamp = _visual_optional_text(form, "timestamp", 64)
        tags = _visual_tags(form)
        prompt = _visual_form_text(form, "prompt", "")
        if len(prompt) > 16_000:
            raise HTTPException(413, "prompt exceeds character limit")
        if compare and not prompt.strip():
            prompt = "Compare the current image with the selected reference. Describe meaningful differences."
        reference_prompt = _visual_optional_text(form, "reference_prompt", 16_000)
        max_new_tokens = _visual_integer(form, "max_new_tokens", default=384, minimum=1, maximum=2048)
        assert max_new_tokens is not None
    except Exception:
        await form.close()
        raise

    trace_context = getattr(request.state, "trace_context", None)

    def route_for(invocation: Invocation):
        request.state.invocation = invocation
        try:
            route = INVOCATION_ROUTER.resolve(invocation)
        except ModelNotFoundError as exc:
            raise HTTPException(404, "no configured model for this visual operation") from exc
        except UnsupportedCapabilityError as exc:
            raise HTTPException(400, "configured model does not support this visual operation") from exc
        request.state.model_route = route
        return route

    async def memory_call(path: str, body: bytes, content_type: str):
        modality = Modality.TEXT if path.endswith("/search/text") else Modality.IMAGE
        invocation = Invocation(
            trace_context=trace_context,
            operation=InvocationOperation.EMBED,
            modality={modality},
            source=InvocationSource.INTERNAL,
            principal=Principal(
                id="gateway-visual-analysis",
                kind="service",
                scopes=frozenset({"visual-memory:read", "visual-memory:write"}),
            ),
            model_policy=ModelPolicy(capability="embedding"),
            input=InvocationInput(data={"path": path}),
        )
        route = route_for(invocation)
        is_json = content_type.startswith("application/json")
        limit = EMBEDDING_MAX_BODY_BYTES if is_json else EMBEDDING_MAX_UPLOAD_BYTES
        return await forward_buffered(
            route.provider_id,
            path,
            request,
            body=body,
            max_body_bytes=limit,
            body_label="visual memory request",
            content_type=content_type,
        )

    async def vlm_call(path: str, body: bytes, content_type: str):
        filename, media_type, image_bytes = image
        image_metadata = SimpleNamespace(filename=filename, content_type=media_type, size=len(image_bytes))
        invocation = AUDIO_ADAPTER.to_vision(
            {"image": image_metadata, "prompt": prompt, "max_new_tokens": str(max_new_tokens)},
            trace_context,
        )
        route = route_for(invocation)
        return await forward_buffered(
            route.provider_id,
            path,
            request,
            body=body,
            max_body_bytes=VISUAL_CONTEXT_MAX_BODY_BYTES,
            body_label="contextual vision request",
            content_type=content_type,
        )

    orchestrator = VisualAnalysisOrchestrator(memory_call, vlm_call)
    try:
        result = await orchestrator.analyze(
            image_bytes=image[2],
            image_filename=image[0],
            image_content_type=image[1],
            namespace=namespace,
            source=source,
            session_id=session_id,
            sequence_id=sequence_id,
            sequence_number=sequence_number,
            timestamp=timestamp,
            tags=tags,
            analysis_profile=profile,
            structured_context=structured_context,
            prompt=prompt,
            max_new_tokens=max_new_tokens,
            code_namespace=code_namespace,
            reference_id=reference_id,
            reference_namespace=reference_namespace,
            reference_image=reference_image,
            reference_prompt=reference_prompt,
            skip_if_identical_frame=skip_if_identical_frame,
            retention_policy=retention_policy,
        )
        if result.get("analysis_skipped"):
            VISUAL_METRICS.increment("qwen_escalations_avoided_total")
        else:
            VISUAL_METRICS.increment("qwen_escalations_total")
        return result
    except VisualAnalysisError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    finally:
        await form.close()


@app.post("/v1/vision/analyze-detailed")
async def vision_analyze_detailed(request: Request):
    return await _detailed_visual_analysis(request, compare=False)


@app.post("/v1/vision/compare")
async def vision_compare(request: Request):
    return await _detailed_visual_analysis(request, compare=True)


@app.api_route("/v1/vision/analyze", methods=["POST"])
async def vision(request: Request):
    raw = await read_body_limited(request, VLM_MAX_UPLOAD_BYTES, "VLM upload")
    try:
        form = await parse_form_body(request, raw)
    except Exception:
        form = {}
    invocation = AUDIO_ADAPTER.to_vision(form, request.state.trace_context)
    request.state.invocation = invocation
    route = INVOCATION_ROUTER.resolve(invocation)
    request.state.model_route = route
    if hasattr(form, "close"):
        await form.close()
    return await forward_buffered(
        route.provider_id,
        "/v1/vision/analyze",
        request,
        body=raw,
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
