from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import secrets
from typing import Literal
from uuid import uuid4

import httpx
from core.cancellation import CancellationToken
from core.context import TraceContext
from core.invocation import Principal
from core.tool_broker import ToolBroker, ToolCall, ToolNotFoundError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from observability.propagation import extract_trace_context, inject_trace_context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from outbound import (
    MCPConfigError,
    MCPRemoteToolProvider,
    load_mcp_auth_environment,
    load_mcp_server_configs,
)

_LOGGER = logging.getLogger(__name__)

MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()
MCP_URL_TOKEN = os.getenv("MCP_URL_TOKEN", "").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000").rstrip("/")
VISUAL_IMAGE_MAX_BYTES = 20 * 1024 * 1024

if not MCP_API_KEY:
    raise RuntimeError("MCP_API_KEY must be set")
if not MCP_URL_TOKEN:
    raise RuntimeError("MCP_URL_TOKEN must be set")
if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY must be set")
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:8765",
        "localhost:8765",
        "127.0.0.1:8000",
        "localhost:8000",
        "marcus-computer.taile97c31.ts.net:10000",
    ],
    allowed_origins=[
        "https://marcus-computer.taile97c31.ts.net:10000",
    ],
)

mcp = FastMCP(
    "Local AI",
    instructions=(
        "Private bridge to Marcus Computer's Local AI stack. "
        "Use status/models for discovery and ask_local_ai to delegate work "
        "to the local fast or reasoning model. The local_ai_transcribe_audio and "
        "local_ai_synthesize_speech tools expose the audio API; speech synthesis is English-only. "
        "Visual memory tools index and retrieve images or project text through the authenticated gateway."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)


async def gateway_request(method: str, path: str, *, json_body=None, trace_context: TraceContext | None = None):
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.request(
            method,
            GATEWAY_URL + path,
            headers=headers,
            json=json_body,
        )
    if response.status_code >= 400:
        detail = response.text[-1200:]
        raise RuntimeError(f"Local AI gateway returned HTTP {response.status_code}: {detail}")
    return response.json()


async def gateway_audio_request(
    method: str,
    path: str,
    *,
    json_body=None,
    data=None,
    files=None,
    trace_context: TraceContext | None = None,
):
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    if trace_context is not None:
        inject_trace_context(headers, trace_context)
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.request(
            method,
            GATEWAY_URL + path,
            headers=headers,
            json=json_body,
            data=data,
            files=files,
        )
    if response.status_code >= 400:
        detail = response.text[-1200:]
        raise RuntimeError(f"Local AI gateway returned HTTP {response.status_code}: {detail}")
    return response


def _decode_visual_image(image_base64: str) -> bytes:
    if not isinstance(image_base64, str) or not image_base64:
        raise ValueError("image_base64 must not be empty")
    if len(image_base64) > 28_000_000:
        raise ValueError("image exceeds the 20 MiB MCP limit")
    try:
        image = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 must contain valid base64") from exc
    if not image:
        raise ValueError("image must not be empty")
    if len(image) > VISUAL_IMAGE_MAX_BYTES:
        raise ValueError("image exceeds the 20 MiB MCP limit")
    return image


def _visual_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise ValueError("filename must be a string")
    safe = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:128]
    return safe or "image.png"


def _visual_content_type(content_type: str) -> str:
    if not isinstance(content_type, str) or not (
        content_type.startswith("image/") or content_type == "application/octet-stream"
    ):
        raise ValueError("content_type must be an image media type")
    return content_type


def _visual_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 256:
        raise ValueError("namespace must be non-empty and at most 256 characters")
    return namespace.strip()


def _visual_sequence_fields(
    *,
    session_id: str | None,
    sequence_id: str | None,
    sequence_number: int | None,
    timestamp: str | None,
    tags: list[str] | None,
    metadata: dict | None,
) -> list[tuple[str, str]]:
    fields = []
    for name, value, limit in (
        ("session_id", session_id, 256),
        ("sequence_id", sequence_id, 256),
        ("timestamp", timestamp, 64),
    ):
        if value is not None:
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{name} is invalid or too long")
            fields.append((name, value))
    if sequence_number is not None:
        if isinstance(sequence_number, bool) or not isinstance(sequence_number, int) or sequence_number < 0:
            raise ValueError("sequence_number must be a non-negative integer")
        fields.append(("sequence_number", str(sequence_number)))
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(tag, str) or len(tag) > 256 for tag in tags):
            raise ValueError("tags must contain at most 100 short strings")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    if tags:
        fields.append(("tags_json", json.dumps(tags, ensure_ascii=False, separators=(",", ":"))))
    if metadata:
        fields.append(("metadata_json", json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))))
    return fields
@mcp.tool(description="Get Local AI gateway, GPU scheduler, and service status.")
async def local_ai_status(ctx: Context) -> dict:
    return await gateway_request("GET", "/v1/system/status", trace_context=_request_trace_context(ctx))


@mcp.tool(description="List the Local AI models and their advertised capabilities.")
async def local_ai_models(ctx: Context) -> dict:
    return await gateway_request("GET", "/v1/models", trace_context=_request_trace_context(ctx))


@mcp.tool(description="Describe the stable Local AI API capabilities and endpoint map.")
async def local_ai_capabilities(ctx: Context) -> dict:
    return await gateway_request("GET", "/v1/capabilities", trace_context=_request_trace_context(ctx))


@mcp.tool(
    description=(
        "Ask a model running locally on Marcus Computer. "
        "Use mode='fast' for normal work and mode='reasoning' for difficult coding or analysis."
    )
)
async def ask_local_ai(
    prompt: str,
    mode: Literal["fast", "reasoning"] = "fast",
    system_prompt: str = "",
    max_tokens: int = 1200,
    *,
    ctx: Context,
) -> str:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    max_tokens = max(32, min(int(max_tokens), 4096))
    model = "local-fast" if mode == "fast" else "local-reasoning"

    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": prompt.strip()})

    result = await gateway_request(
        "POST",
        "/v1/chat/completions",
        json_body={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        },
        trace_context=_request_trace_context(ctx),
    )
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected chat response") from exc


@mcp.tool(
    description=(
        "Transcribe an audio file through Local AI. Provide the file contents as base64; "
        "optionally select a Whisper language, prompt, temperature, timestamps, and output format."
    )
)
async def local_ai_transcribe_audio(
    audio_base64: str,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
    language: str = "",
    prompt: str = "",
    temperature: float = 0.0,
    timestamp_granularities: list[Literal["segment", "word"]] | None = None,
    response_format: Literal["json", "verbose_json", "text", "srt", "vtt"] = "json",
    *,
    ctx: Context,
) -> dict:
    if not audio_base64:
        raise ValueError("audio_base64 must not be empty")
    if len(audio_base64) > 28_000_000:
        raise ValueError("audio file exceeds the 20 MiB MCP limit")
    try:
        audio = base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("audio_base64 must contain valid base64") from exc
    if not audio:
        raise ValueError("audio file must not be empty")
    if len(audio) > 20 * 1024 * 1024:
        raise ValueError("audio file exceeds the 20 MiB MCP limit")
    if response_format not in {"json", "verbose_json", "text", "srt", "vtt"}:
        raise ValueError("unsupported response_format")
    if len(prompt) > 4000:
        raise ValueError("prompt exceeds 4000 characters")
    if not 0.0 <= temperature <= 1.0:
        raise ValueError("temperature must be between 0 and 1")
    safe_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:128] or "audio.wav"
    if not isinstance(content_type, str) or not (
        content_type.startswith("audio/") or content_type == "application/octet-stream"
    ):
        raise ValueError("content_type must be an audio media type")
    form = [
        ("model", "local-stt"),
        ("response_format", response_format),
        ("temperature", str(temperature)),
    ]
    if language:
        form.append(("language", language))
    if prompt:
        form.append(("prompt", prompt))
    if timestamp_granularities:
        form.extend(("timestamp_granularities[]", value) for value in timestamp_granularities)
    response = await gateway_audio_request(
        "POST",
        "/v1/audio/transcriptions",
        data=form,
        files={"file": (safe_filename, audio, content_type)},
        trace_context=_request_trace_context(ctx),
    )
    if response_format in {"text", "srt", "vtt"}:
        return {"text": response.text}
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected transcription response") from exc


@mcp.tool(
    description=(
        "Index one image in the shared visual memory through the authenticated Local AI gateway. "
        "Provide image bytes as base64 and a namespace; optional sequence fields support temporal recall."
    )
)
async def index_visual_frame(
    image_base64: str,
    namespace: str,
    filename: str = "frame.png",
    content_type: str = "image/png",
    source: str = "mcp",
    session_id: str | None = None,
    sequence_id: str | None = None,
    sequence_number: int | None = None,
    timestamp: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    *,
    ctx: Context,
) -> dict:
    image = _decode_visual_image(image_base64)
    namespace = _visual_namespace(namespace)
    filename = _visual_filename(filename)
    content_type = _visual_content_type(content_type)
    if not isinstance(source, str) or not source.strip() or len(source) > 128:
        raise ValueError("source must be non-empty and at most 128 characters")
    data = [("namespace", namespace), ("source", source.strip())]
    data.extend(_visual_sequence_fields(
        session_id=session_id,
        sequence_id=sequence_id,
        sequence_number=sequence_number,
        timestamp=timestamp,
        tags=tags,
        metadata=metadata,
    ))
    response = await gateway_audio_request(
        "POST",
        "/v1/visual-memory/index",
        data=data,
        files={"image": (filename, image, content_type)},
        trace_context=_request_trace_context(ctx),
    )
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected visual-memory response") from exc


@mcp.tool(
    description=(
        "Search visual memory using either text or an image. Results come from the shared EmbeddingGemma index "
        "through the authenticated Local AI gateway."
    )
)
async def search_visual_memory(
    namespace: str,
    query: str | None = None,
    image_base64: str | None = None,
    filename: str = "query.png",
    content_type: str = "image/png",
    top_k: int = 8,
    source: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    include_crops: bool = True,
    expand_temporal: int = 0,
    *,
    ctx: Context,
) -> dict:
    namespace = _visual_namespace(namespace)
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be a string")
    has_query = isinstance(query, str) and bool(query.strip())
    if has_query == bool(image_base64):
        raise ValueError("provide exactly one non-empty query or image_base64")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")
    if isinstance(expand_temporal, bool) or not isinstance(expand_temporal, int) or not 0 <= expand_temporal <= 2:
        raise ValueError("expand_temporal must be between 0 and 2")
    if tags is not None and (
        not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(tag, str) or len(tag) > 256 for tag in tags)
    ):
        raise ValueError("tags must contain at most 100 short strings")
    if source is not None and (not isinstance(source, str) or len(source) > 128):
        raise ValueError("source is invalid or too long")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 256):
        raise ValueError("session_id is invalid or too long")
    trace_context = _request_trace_context(ctx)
    if image_base64:
        image = _decode_visual_image(image_base64)
        form = [
            ("namespace", namespace),
            ("top_k", str(top_k)),
            ("include_crops", str(bool(include_crops)).lower()),
            ("expand_temporal", str(expand_temporal)),
        ]
        if source:
            form.append(("source", source))
        if session_id:
            form.append(("session_id", session_id))
        if tags:
            form.append(("tags_json", json.dumps(tags, ensure_ascii=False, separators=(",", ":"))))
        response = await gateway_audio_request(
            "POST",
            "/v1/visual-memory/search/image",
            data=form,
            files={"image": (_visual_filename(filename), image, _visual_content_type(content_type))},
            trace_context=trace_context,
        )
    else:
        if not isinstance(query, str) or not query.strip() or len(query) > 32_000:
            raise ValueError("query must be non-empty and at most 32000 characters")
        payload = {
            "query": query.strip(),
            "namespace": namespace,
            "top_k": top_k,
            "include_crops": include_crops,
            "expand_temporal": expand_temporal,
        }
        if source:
            payload["source"] = source
        if session_id:
            payload["session_id"] = session_id
        if tags:
            payload["tags"] = tags
        return await gateway_request(
            "POST",
            "/v1/visual-memory/search/text",
            json_body=payload,
            trace_context=trace_context,
        )
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected visual-memory response") from exc


@mcp.tool(
    description=(
        "Analyze an image with retrieval-backed website, game, or generic context. "
        "Optionally compare it with a saved frame ID or another uploaded image."
    )
)
async def analyze_visual(
    image_base64: str,
    filename: str = "image.png",
    content_type: str = "image/png",
    analysis_profile: Literal["generic", "website", "game"] = "generic",
    namespace: str | None = None,
    context: dict | None = None,
    prompt: str = "",
    code_namespace: str | None = None,
    max_new_tokens: int = 384,
    reference_id: str | None = None,
    reference_namespace: str | None = None,
    reference_image_base64: str | None = None,
    reference_filename: str = "reference.png",
    reference_content_type: str = "image/png",
    *,
    ctx: Context,
) -> dict:
    current = _decode_visual_image(image_base64)
    filename = _visual_filename(filename)
    content_type = _visual_content_type(content_type)
    if not isinstance(analysis_profile, str) or analysis_profile not in {"generic", "website", "game"}:
        raise ValueError("analysis_profile must be generic, website, or game")
    if namespace is None:
        namespace = {"generic": "visual:default", "website": "website:default", "game": "game:default"}[analysis_profile]
    namespace = _visual_namespace(namespace)
    if context is not None and not isinstance(context, dict):
        raise ValueError("context must be an object")
    context_json = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))
    if len(context_json.encode("utf-8")) > 64_000:
        raise ValueError("context exceeds the 64 KiB MCP limit")
    if not isinstance(prompt, str) or len(prompt) > 16_000:
        raise ValueError("prompt exceeds 16000 characters")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or not 1 <= max_new_tokens <= 2048:
        raise ValueError("max_new_tokens must be between 1 and 2048")
    if reference_id is not None and (not isinstance(reference_id, str) or not reference_id.strip() or len(reference_id) > 128):
        raise ValueError("reference_id is invalid")
    if reference_namespace is not None:
        reference_namespace = _visual_namespace(reference_namespace)
    if bool(reference_id) and bool(reference_image_base64):
        raise ValueError("provide only one reference ID or reference image")
    if reference_namespace and not reference_id:
        raise ValueError("reference_namespace requires reference_id")

    data = [
        ("analysis_profile", analysis_profile),
        ("namespace", namespace),
        ("context_json", context_json),
        ("prompt", prompt),
        ("max_new_tokens", str(max_new_tokens)),
    ]
    if code_namespace:
        data.append(("code_namespace", _visual_namespace(code_namespace)))
    files = {"image": (filename, current, content_type)}
    path = "/v1/vision/analyze-detailed"
    if reference_id:
        path = "/v1/vision/compare"
        data.append(("reference_id", reference_id.strip()))
        if reference_namespace:
            data.append(("reference_namespace", reference_namespace))
    elif reference_image_base64:
        path = "/v1/vision/compare"
        reference = _decode_visual_image(reference_image_base64)
        files["reference"] = (
            _visual_filename(reference_filename),
            reference,
            _visual_content_type(reference_content_type),
        )
    response = await gateway_audio_request(
        "POST",
        path,
        data=data,
        files=files,
        trace_context=_request_trace_context(ctx),
    )
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected visual-analysis response") from exc


@mcp.tool(
    description=(
        "Generate English speech through the Qwen3-TTS CustomVoice model. "
        "Use instruct for free-form tone, emotion, pacing, or prosody guidance; "
        "the returned audio is base64 with a MIME type."
    )
)
async def local_ai_synthesize_speech(
    input: str,
    voice: Literal[
        "Aiden", "Ryan", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee",
    ] = "Aiden",
    instruct: str = "",
    speed: float = 1.0,
    response_format: Literal["mp3", "wav", "opus", "flac", "pcm"] = "mp3",
    *,
    ctx: Context,
) -> dict:
    if not input.strip():
        raise ValueError("input must not be empty")
    response = await gateway_audio_request(
        "POST",
        "/v1/audio/speech",
        json_body={
            "model": "local-tts",
            "input": input,
            "voice": voice,
            "language": "English",
            "instruct": instruct,
            "speed": speed,
            "response_format": response_format,
        },
        trace_context=_request_trace_context(ctx),
    )
    fallback_mime = {
        "mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/opus",
        "flac": "audio/flac", "pcm": "audio/pcm",
    }[response_format]
    return {
        "language": "English",
        "voice": voice,
        "mime_type": response.headers.get("content-type", fallback_mime).split(";", 1)[0],
        "audio_base64": base64.b64encode(response.content).decode("ascii"),
    }


@mcp.tool(name="ai.system.status", description="Get AI-Stack gateway, GPU scheduler, and service status.")
async def ai_system_status(ctx: Context) -> dict:
    return await local_ai_status(ctx)


@mcp.tool(name="ai.models.list", description="List AI-Stack models and their advertised capabilities.")
async def ai_models_list(ctx: Context) -> dict:
    return await local_ai_models(ctx)


@mcp.tool(
    name="ai.generate",
    description="Generate a response with the local fast model through the unified AI-Stack gateway.",
)
async def ai_generate(prompt: str, system_prompt: str = "", max_tokens: int = 1200, *, ctx: Context) -> str:
    return await ask_local_ai(prompt, "fast", system_prompt, max_tokens, ctx=ctx)


@mcp.tool(
    name="ai.reason",
    description="Reason through a difficult task with the local reasoning model through the unified gateway.",
)
async def ai_reason(prompt: str, system_prompt: str = "", max_tokens: int = 1200, *, ctx: Context) -> str:
    return await ask_local_ai(prompt, "reasoning", system_prompt, max_tokens, ctx=ctx)


tool_broker = ToolBroker()
remote_tool_provider: MCPRemoteToolProvider | None = None
outbound_config_error = False


@mcp.tool(description="List external MCP servers and the exact tools admitted through the Tool Broker.")
async def mcp_list_tools() -> dict:
    definitions = tool_broker.list_tools()
    return {
        "servers": remote_tool_provider.server_status() if remote_tool_provider is not None else {},
        "configuration_state": "invalid" if outbound_config_error else "ready",
        "tools": [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in definitions
        ],
    }


def _request_trace_context(ctx: Context) -> TraceContext:
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        return extract_trace_context(headers)
    return TraceContext(request_id=str(ctx.request_id))


@mcp.tool(description="Call an exact tool listed by mcp_list_tools through the permission-checked Tool Broker.")
async def mcp_call_tool(tool_name: str, arguments: dict, ctx: Context) -> dict:
    definitions = tool_broker.list_tools()
    scopes = frozenset(scope for definition in definitions for scope in definition.all_permissions)
    call = ToolCall(
        id=f"mcp-{uuid4().hex}",
        invocation_id=str(uuid4()),
        tool_name=tool_name,
        arguments=arguments,
        trace_context=_request_trace_context(ctx),
    )
    try:
        result = await tool_broker.execute(
            call,
            principal=Principal(id="authenticated-mcp-client", kind="service", scopes=scopes),
            cancellation=CancellationToken(),
        )
    except ToolNotFoundError:
        return {"success": False, "error": "tool not found"}
    except PermissionError:
        return {"success": False, "error": "tool is not authorized"}
    except ValueError:
        return {"success": False, "error": "tool arguments are invalid"}
    if not result.success:
        return {"success": False, "error": result.error, "call_id": result.call_id}
    return {"success": True, "content": result.content, "call_id": result.call_id}


async def health(_: Request):
    return JSONResponse({"status": "ok", "service": "local-ai-mcp"})


CAPABILITY_PREFIX = f"/{MCP_URL_TOKEN}"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)
        if path == CAPABILITY_PREFIX or path.startswith(CAPABILITY_PREFIX + "/"):
            return await call_next(request)

        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {MCP_API_KEY}"
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                {"detail": "invalid MCP credential"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


mcp_http_app = mcp.streamable_http_app()
@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    global tool_broker, remote_tool_provider, outbound_config_error
    tool_broker = ToolBroker()
    outbound_config_error = False
    try:
        configs = load_mcp_server_configs(os.getenv("MCP_SERVERS_JSON", ""))
        auth_environment = load_mcp_auth_environment(os.getenv("MCP_SERVER_TOKENS_JSON", ""))
    except MCPConfigError as exc:
        configs = ()
        auth_environment = {}
        outbound_config_error = True
        _LOGGER.warning("outbound MCP configuration is invalid", extra={"error_type": type(exc).__name__})
    provider_environment = dict(os.environ)
    provider_environment.update(auth_environment)
    remote_tool_provider = MCPRemoteToolProvider(configs, environ=provider_environment)
    for config in configs:
        tool_broker.register_provider(config.provider_id, remote_tool_provider)
    for definition in await remote_tool_provider.discover():
        try:
            tool_broker.register_tool(definition)
        except (TypeError, ValueError) as exc:
            _LOGGER.warning(
                "outbound MCP tool registration failed",
                extra={"tool_name": definition.name, "error_type": type(exc).__name__},
            )
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/healthz", health, methods=["GET"]),
        Mount(CAPABILITY_PREFIX, app=mcp_http_app),
        Mount("/", app=mcp_http_app),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=lifespan,
)
