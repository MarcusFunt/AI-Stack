from __future__ import annotations

import contextlib
import os
import secrets
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

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


async def gateway_request(method: str, path: str, *, json_body=None):
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
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
async def local_ai_status() -> dict:
    return await gateway_request("GET", "/v1/system/status")


@mcp.tool(description="List the Local AI models and their advertised capabilities.")
async def local_ai_models() -> dict:
    return await gateway_request("GET", "/v1/models")


@mcp.tool(description="Describe the stable Local AI API capabilities and endpoint map.")
async def local_ai_capabilities() -> dict:
    return await gateway_request("GET", "/v1/capabilities")


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
    )
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Local AI returned an unexpected chat response") from exc
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
