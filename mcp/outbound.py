from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from core.context import TraceContext
from core.tool_broker import ToolCall, ToolExecutionContext, ToolProviderError
from core.tools import ToolDefinition
from observability.propagation import inject_trace_context

_LOGGER = logging.getLogger(__name__)
_SERVER_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_SECRET_ENV_BYTES = 64 * 1024
_MAX_SERVERS = 16
_MAX_DISCOVERED_TOOLS = 500


class MCPConfigError(ValueError):
    """Invalid outbound MCP configuration, without exposing credential values."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    server_id: str
    url: str
    allowed_tools: frozenset[str]
    auth_env: str | None = None
    enabled: bool = True

    @property
    def provider_id(self) -> str:
        return f"mcp:{self.server_id}"


def _validated_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise MCPConfigError("MCP server URL must be a non-empty URL")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise MCPConfigError("MCP server URL is invalid") from exc
    if parsed.scheme not in {"https", "http"} or not hostname or parsed.username or parsed.password:
        raise MCPConfigError("MCP server URL must use HTTPS and must not contain credentials")
    if parsed.query or parsed.fragment:
        raise MCPConfigError("MCP server URL must not contain query parameters or a fragment")
    if parsed.scheme == "http":
        try:
            loopback = hostname.lower() == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise MCPConfigError("plain HTTP is allowed only for loopback MCP servers")
    if port is not None and not 1 <= port <= 65535:
        raise MCPConfigError("MCP server URL port is invalid")
    return value.strip()


def load_mcp_server_configs(
    raw_json: str | None,
) -> tuple[MCPServerConfig, ...]:
    """Parse MCP_SERVERS_JSON; credentials are referenced by env var name only."""
    if raw_json is None or not raw_json.strip():
        return ()
    if len(raw_json.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise MCPConfigError("MCP server configuration is too large")
    try:
        payload = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MCPConfigError("MCP server configuration must be valid JSON") from exc
    if not isinstance(payload, list) or len(payload) > _MAX_SERVERS:
        raise MCPConfigError("MCP server configuration must be a list of at most 16 servers")

    configs: list[MCPServerConfig] = []
    seen_ids: set[str] = set()
    allowed_keys = {"id", "url", "enabled", "allowed_tools", "auth_env"}
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) - allowed_keys:
            raise MCPConfigError("each MCP server must contain only supported configuration fields")
        server_id = entry.get("id")
        if not isinstance(server_id, str) or not _SERVER_ID.fullmatch(server_id):
            raise MCPConfigError("MCP server id must contain 1-48 letters, digits, underscores, or hyphens")
        if server_id in seen_ids:
            raise MCPConfigError("MCP server ids must be unique")
        seen_ids.add(server_id)

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MCPConfigError("MCP server enabled must be a boolean")
        names = entry.get("allowed_tools")
        if not isinstance(names, list) or not names or any(
            not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) for name in names
        ):
            raise MCPConfigError("each MCP server requires a non-empty exact allowed_tools list")
        if len(set(names)) != len(names):
            raise MCPConfigError("MCP server allowed_tools entries must be unique")
        auth_env = entry.get("auth_env")
        if auth_env is not None and (not isinstance(auth_env, str) or not _ENV_NAME.fullmatch(auth_env)):
            raise MCPConfigError("auth_env must name an uppercase environment variable")

        configs.append(MCPServerConfig(
            server_id=server_id,
            url=_validated_url(entry.get("url")),
            allowed_tools=frozenset(names),
            auth_env=auth_env,
            enabled=enabled,
        ))
    return tuple(configs)


def load_mcp_auth_environment(raw_json: str | None) -> dict[str, str]:
    """Read a host-injected name/value map for credentials passed as environment."""
    if raw_json is None or not raw_json.strip():
        return {}
    if len(raw_json.encode("utf-8")) > _MAX_SECRET_ENV_BYTES:
        raise MCPConfigError("MCP credential environment is too large")
    try:
        payload = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MCPConfigError("MCP credential environment must be valid JSON") from exc
    if not isinstance(payload, dict) or len(payload) > 64:
        raise MCPConfigError("MCP credential environment must be an object of at most 64 values")
    if any(
        not isinstance(name, str)
        or not _ENV_NAME.fullmatch(name)
        or not isinstance(value, str)
        or not value.strip()
        for name, value in payload.items()
    ):
        raise MCPConfigError("MCP credential environment contains an invalid entry")
    return dict(payload)


@asynccontextmanager
async def _sdk_session(url: str, headers: dict[str, str], timeout: float) -> AsyncIterator[Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers, timeout=timeout) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


def _field(value: Any, name: str, alias: str | None = None, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        return value.get(alias, default) if alias else default
    result = getattr(value, name, None)
    return result if result is not None else (getattr(value, alias, default) if alias else default)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _schema_is_supported(schema: Any, *, root: bool = False) -> bool:
    if not isinstance(schema, Mapping):
        return False
    if root and schema.get("type") != "object":
        return False
    validation_keywords = {
        "type", "enum", "required", "properties", "additionalProperties", "items",
        "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    }
    annotation_keywords = {"$schema", "title", "description", "default", "examples", "deprecated"}
    if set(schema) - validation_keywords - annotation_keywords:
        return False
    supported_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if "type" in schema and (
        not isinstance(schema["type"], str) or schema["type"] not in supported_types
    ):
        return False
    if "enum" in schema and not isinstance(schema["enum"], list):
        return False
    if "required" in schema and (
        not isinstance(schema["required"], list)
        or any(not isinstance(key, str) for key in schema["required"])
    ):
        return False
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping) or any(
            not isinstance(key, str) or not _schema_is_supported(value)
            for key, value in properties.items()
        ):
            return False
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        return False
    if "items" in schema and not _schema_is_supported(schema["items"]):
        return False
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (
            not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0
        ):
            return False
    for key in ("minimum", "maximum"):
        if key in schema and (
            not isinstance(schema[key], (int, float))
            or isinstance(schema[key], bool)
            or not math.isfinite(schema[key])
        ):
            return False
    return True


class MCPRemoteToolProvider:
    """Discover and call configured remote MCP tools under broker permissions."""

    def __init__(
        self,
        configs: tuple[MCPServerConfig, ...] | list[MCPServerConfig],
        *,
        environ: Mapping[str, str] | None = None,
        session_factory=None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self.configs = tuple(configs)
        self.environ = os.environ if environ is None else environ
        self.session_factory = session_factory or _sdk_session
        self.timeout_seconds = timeout_seconds
        self._configs = {config.server_id: config for config in self.configs}
        self._definitions: dict[str, ToolDefinition] = {}
        self._remote_names: dict[str, tuple[str, str]] = {}
        self._status = {
            config.server_id: {
                "state": "pending" if config.enabled else "disabled",
                "tool_count": 0,
            }
            for config in self.configs
        }

    def _headers(self, config: MCPServerConfig, trace_context: TraceContext | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if config.auth_env:
            credential = self.environ.get(config.auth_env, "").strip()
            if not credential:
                raise MCPConfigError("configured authentication environment variable is missing")
            headers["Authorization"] = f"Bearer {credential}"
        if trace_context is not None:
            inject_trace_context(headers, trace_context)
        return headers

    @asynccontextmanager
    async def _session(self, config: MCPServerConfig, headers: dict[str, str]) -> AsyncIterator[Any]:
        async with self.session_factory(config.url, headers, self.timeout_seconds) as session:
            await session.initialize()
            yield session

    async def _list_remote_tools(self, session: Any) -> list[Any]:
        tools: list[Any] = []
        cursor = None
        for _ in range(10):
            response = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            page = _field(response, "tools", default=[])
            if not isinstance(page, list):
                raise ToolProviderError("remote MCP server returned an invalid tool list")
            tools.extend(page)
            if len(tools) > _MAX_DISCOVERED_TOOLS:
                raise ToolProviderError("remote MCP server returned too many tools")
            cursor = _field(response, "nextCursor", "next_cursor")
            if not cursor:
                return tools
        raise ToolProviderError("remote MCP tool listing exceeded its page limit")

    async def discover(self) -> tuple[ToolDefinition, ...]:
        self._definitions.clear()
        self._remote_names.clear()
        await asyncio.gather(*(
            self._discover_server(config) for config in self.configs if config.enabled
        ))
        return tuple(self._definitions.values())

    async def _discover_server(self, config: MCPServerConfig) -> None:
        try:
            definitions = await asyncio.wait_for(
                self._fetch_definitions(config), timeout=self.timeout_seconds
            )
            for definition, remote_name in definitions:
                self._definitions[definition.name] = definition
                self._remote_names[definition.name] = (config.server_id, remote_name)
            self._status[config.server_id] = {"state": "ready", "tool_count": len(definitions)}
        except Exception as exc:
            self._status[config.server_id] = {"state": "unavailable", "tool_count": 0}
            _LOGGER.warning(
                "outbound MCP discovery failed",
                extra={"mcp_server_id": config.server_id, "error_type": type(exc).__name__},
            )

    async def _fetch_definitions(
        self, config: MCPServerConfig
    ) -> list[tuple[ToolDefinition, str]]:
        headers = self._headers(config)
        async with self._session(config, headers) as session:
            remote_tools = await self._list_remote_tools(session)
        discovered: list[tuple[ToolDefinition, str]] = []
        for remote_tool in remote_tools:
            remote_name = _field(remote_tool, "name")
            if remote_name not in config.allowed_tools:
                continue
            if not isinstance(remote_name, str) or not _TOOL_NAME.fullmatch(remote_name):
                continue
            schema = _field(remote_tool, "inputSchema", "input_schema", {})
            if not _schema_is_supported(schema, root=True):
                continue
            description = _field(remote_tool, "description", default="")
            if not isinstance(description, str):
                description = ""
            public_name = f"mcp.{config.server_id}.{remote_name}"
            definition = ToolDefinition(
                name=public_name,
                description=description[:2048],
                input_schema=dict(schema),
                provider=config.provider_id,
                permissions={f"mcp:{config.server_id}:{remote_name}"},
                metadata={"server_id": config.server_id, "remote_name": remote_name},
            )
            discovered.append((definition, remote_name))
        return discovered

    def server_status(self) -> dict[str, dict[str, Any]]:
        return {server_id: dict(status) for server_id, status in self._status.items()}

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> Any:
        remote = self._remote_names.get(call.tool_name)
        if remote is None:
            raise ToolProviderError("remote MCP tool is unavailable")
        server_id, remote_name = remote
        config = self._configs[server_id]
        headers = self._headers(config, context.trace_context)
        async with self._session(config, headers) as session:
            result = await session.call_tool(remote_name, dict(call.arguments))
        if _field(result, "isError", "is_error", False):
            raise ToolProviderError("remote MCP tool reported an error")
        structured = _field(result, "structuredContent", "structured_content")
        if structured is not None:
            return _json_value(structured)
        content = _field(result, "content", default=[])
        if not isinstance(content, list):
            raise ToolProviderError("remote MCP tool returned an invalid result")
        return {"content": [_json_value(item) for item in content]}

