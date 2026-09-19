import asyncio
import json
import os
import secrets
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SERVICES = CONFIG["services"]
MODELS = CONFIG["models"]
ALIASES = {k.lower(): v for k, v in CONFIG.get("aliases", {}).items()}
MODEL_SERVICE = {m["id"].lower(): m["service"] for m in MODELS}

app = FastAPI(title="Marcus Local AI Gateway", version="0.3.0")
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
    if request.url.path == "/health":
        return await call_next(request)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {API_KEY}"
    if not secrets.compare_digest(supplied, expected):
        return JSONResponse(status_code=401, content={"detail": "invalid API key"})
    return await call_next(request)

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

@app.on_event("startup")
async def reset_stale_leases():
    await supervisor("POST", "/reset-leases", timeout=10)

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

    lease_id = await acquire_service(service)
    try:
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
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers(upstream, buffered=True),
            media_type=upstream.headers.get("content-type"),
        )
    finally:
        await release_service(service, lease_id)

async def forward_streaming(service: str, path: str, request: Request, body: bytes):
    lease_id = await acquire_service(service)
    client = None
    upstream = None
    cleanup_lock = asyncio.Lock()
    released = False

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
                await release_service(service, lease_id)
                released = True

    try:
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
        await cleanup()
        raise HTTPException(502, f"{service} upstream request failed") from exc
    except Exception:
        await cleanup()
        raise

    async def iterator():
        try:
            try:
                async for chunk in upstream.aiter_raw():
                    if await request.is_disconnected():
                        break
                    yield chunk
            except httpx.RequestError:
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
