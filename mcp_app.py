from __future__ import annotations

import asyncio
import contextvars
import json
import os
import pathlib
from typing import Any

from mcp import types
from mcp.server import Server

from demo_universe import install_demo_universe
from identity_auth import bound_role, resolve_authorization
from universe_loader import get_universe_installer
from runtime_core import (
    AuthenticationRequired,
    IdentityScopeMismatch,
    InvalidIdentityToken,
    WorldRuntime,
    WorldRuntimeError,
)


_request_identity: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("world_request_identity", default=None)
)


class BearerIdentityMiddleware:
    def __init__(self, app, runtime: WorldRuntime, universe: str):
        self.app = app
        self.runtime = runtime
        self.universe = universe

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        try:
            identity = resolve_authorization(
                self.runtime, self.universe, headers.get("authorization")
            )
        except (AuthenticationRequired, InvalidIdentityToken, IdentityScopeMismatch) as exc:
            status = 403 if isinstance(exc, IdentityScopeMismatch) else 401
            payload = json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return
        marker = _request_identity.set(identity)
        try:
            return await self.app(scope, receive, send)
        finally:
            _request_identity.reset(marker)


def _claim_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "activity_id": {"type": "string", "minLength": 1},
            "runtime_id": {"type": "string", "minLength": 1},
            "claim_epoch": {"type": "integer", "minimum": 1},
            "claim_token": {"type": "string", "minLength": 1},
        },
        "required": [
            "activity_id",
            "runtime_id",
            "claim_epoch",
            "claim_token",
        ],
        "additionalProperties": False,
    }


def _role_fields(auth_required: bool) -> tuple[dict[str, Any], list[str]]:
    if auth_required:
        return {}, []
    return (
        {"role_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        ["role_id"],
    )


def _function_tool(
    desc: dict[str, Any],
    universe: str,
    auth_required: bool = False,
) -> types.Tool:
    role_properties, role_required = _role_fields(auth_required)
    properties: dict[str, Any] = {
        **role_properties,
        "expected_version": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional contract version pin.",
        },
        "arguments": desc["input_schema"],
    }
    required = [*role_required, "arguments"]
    if desc["access"] == "write":
        properties["operation_id"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Stable idempotency key for this logical world operation.",
        }
        required.append("operation_id")
    if desc["requires_activity_claim"]:
        properties["activity_claim"] = _claim_schema()
        required.append("activity_claim")

    return types.Tool(
        name=desc["function_id"],
        description=desc["description"],
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        _meta={
            "world_runtime": {
                "universe": universe,
                "function_version": desc["version"],
                "access": desc["access"],
                "availability": desc["availability"],
                "requires_activity_claim": desc["requires_activity_claim"],
            }
        },
    )


def _core_tools(auth_required: bool = False) -> list[types.Tool]:
    role_properties, role_required = _role_fields(auth_required)
    return [
        types.Tool(
            name="world.bootstrap",
            description="Get the minimum current context needed to operate in this universe.",
            inputSchema={
                "type": "object",
                "properties": dict(role_properties),
                "required": list(role_required),
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="world.start_activity",
            description="Start a world activity. Exclusivity applies only to its declared group.",
            inputSchema={
                "type": "object",
                "properties": {
                    **role_properties,
                    "activity_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "kind": {"type": "string", "minLength": 1, "maxLength": 128},
                    "exclusive_group": {"type": ["string", "null"]},
                    "ttl_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
                },
                "required": [*role_required, "activity_id", "kind"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="world.claim_activity",
            description="Claim an activity for one runtime with a finite lease and fencing epoch.",
            inputSchema={
                "type": "object",
                "properties": {
                    **role_properties,
                    "activity_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "runtime_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "lease_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 3600,
                    },
                },
                "required": [*role_required, "activity_id", "runtime_id"],
                "additionalProperties": False,
            },
        ),

        types.Tool(
            name="world.get_changes",
            description="Read durable world changes after a cursor.",
            inputSchema={
                "type": "object",
                "properties": {
                    **role_properties,
                    "after": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": list(role_required),
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="world.wait_changes",
            description="Wait a bounded time for durable world changes; never loops indefinitely.",
            inputSchema={
                "type": "object",
                "properties": {
                    **role_properties,
                    "after": {"type": "integer", "minimum": 0},
                    "timeout": {"type": "number", "minimum": 0, "maximum": 30},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": list(role_required),
                "additionalProperties": False,
            },
        ),
    ]


def create_mcp_server(
    db_path: str | pathlib.Path,
    universe: str = "demo",
    *,
    auth_required: bool = False,
    installer=None,
) -> tuple[Server, WorldRuntime]:
    runtime = WorldRuntime(db_path)
    (installer or install_demo_universe)(runtime, universe)

    async def list_tools(_ctx, _params):
        tools = _core_tools(auth_required)
        tools.extend(
            _function_tool(desc, universe, auth_required)
            for desc in runtime.list_functions(universe)
        )
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx, params: types.CallToolRequestParams):
        name = params.name
        args = params.arguments or {}
        try:
            identity = _request_identity.get() if auth_required else None
            supplied_role_id = (
                str(args["role_id"]) if args.get("role_id") is not None else None
            )
            role_id = bound_role(
                identity, supplied_role_id, auth_required=auth_required
            )
            if name == "world.bootstrap":
                result = runtime.bootstrap(universe, role_id)
            elif name == "world.start_activity":
                result = runtime.start_activity(
                    universe,
                    str(args["activity_id"]),
                    role_id,
                    str(args["kind"]),
                    exclusive_group=args.get("exclusive_group"),
                    ttl_seconds=args.get("ttl_seconds"),
                )
            elif name == "world.claim_activity":
                result = runtime.claim_activity(
                    universe,
                    str(args["activity_id"]),
                    role_id,
                    str(args["runtime_id"]),
                    lease_seconds=float(args.get("lease_seconds", 30.0)),
                )
            elif name == "world.get_changes":
                after = int(args.get("after", 0))
                limit = int(args.get("limit", 50))
                rows = runtime.read_changes(universe, role_id, after, limit)
                result = {
                    "events": rows,
                    "next_cursor": rows[-1]["seq"] if rows else after,
                    "event_floor": runtime.event_floor(universe),
                }
            elif name == "world.wait_changes":
                after = int(args.get("after", 0))
                timeout = float(args.get("timeout", 5.0))
                limit = int(args.get("limit", 50))

                # The MCP adapter may run in a different process from the
                # writer (HTTP adapter or another MCP instance). An in-memory
                # Condition cannot wake across those processes. Keep the
                # durable event log authoritative and re-check it on a short
                # async interval. This is server-side waiting: it creates no
                # extra model turns/tokens, it is naturally cancellable, and
                # it notices cross-process writes promptly.
                deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
                poll_interval = 0.10
                rows = []
                while True:
                    rows = runtime.read_changes(universe, role_id, after, limit)
                    if rows:
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(poll_interval, remaining))

                result = {
                    "events": rows,
                    "next_cursor": rows[-1]["seq"] if rows else after,
                    "timed_out": not bool(rows),
                    "event_floor": runtime.event_floor(universe),
                }
            else:
                desc = runtime.get_function(universe, name)
                expected_version = (
                    int(args["expected_version"])
                    if args.get("expected_version") is not None
                    else None
                )
                if desc["access"] == "read":
                    result = runtime.query_function(
                        universe,
                        name,
                        role_id,
                        dict(args.get("arguments") or {}),
                        expected_version=expected_version,
                    )
                else:
                    result = runtime.invoke_function(
                        universe,
                        name,
                        role_id,
                        dict(args.get("arguments") or {}),
                        operation_id=str(args["operation_id"]),
                        expected_version=expected_version,
                        activity_claim=(
                            dict(args["activity_claim"])
                            if args.get("activity_claim") is not None
                            else None
                        ),
                    )
                result["registry_version"] = desc["version"]

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(result, ensure_ascii=False),
                    )
                ],
                structuredContent=result,
                isError=False,
            )
        except (WorldRuntimeError, KeyError, TypeError, ValueError) as exc:
            payload = {
                "error": type(exc).__name__,
                "message": str(exc),
                "tool": name,
            }
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False),
                    )
                ],
                structuredContent=payload,
                isError=True,
            )

    server = Server(
        "agent-world-runtime-v06",
        version="0.6",
        description="Authenticated Remote MCP adapter for the Agent World runtime.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    return server, runtime


DEFAULT_DB = os.getenv(
    "WORLD_DB",
    str(pathlib.Path(__file__).resolve().parent / "runtime_mcp.sqlite3"),
)
DEFAULT_UNIVERSE = os.getenv("WORLD_UNIVERSE", "demo")
WORLD_PROFILE = os.getenv("WORLD_PROFILE", "demo")
AUTH_REQUIRED = os.getenv("WORLD_AUTH_REQUIRED", "0") == "1"
server, runtime = create_mcp_server(
    DEFAULT_DB,
    DEFAULT_UNIVERSE,
    auth_required=AUTH_REQUIRED,
    installer=get_universe_installer(WORLD_PROFILE),
)
_base_app = server.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=False,
    host=os.getenv("WORLD_HOST", "127.0.0.1"),
)
app = (
    BearerIdentityMiddleware(_base_app, runtime, DEFAULT_UNIVERSE)
    if AUTH_REQUIRED
    else _base_app
)
