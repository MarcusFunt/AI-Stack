from __future__ import annotations

import contextlib
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
        "to the local fast or reasoning model."
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
