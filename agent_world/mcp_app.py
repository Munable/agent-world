from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time

from mcp import types
from mcp.server import Server

from .runtime_core import WorldRuntime
from .runtime_contracts import PROTOCOL_VERSION, json_text, embedded_arguments_schema
from .transport_contracts import CORE, WorldGateway, core_schema, function_schema, error_response
from .universe_loader import get_universe_installer
from .runtime_errors import IdentityScopeMismatch, AuthenticationRequired
from .identity_auth import resolve_authorization

logger = logging.getLogger(__name__)


class BearerIdentityMiddleware:
    def __init__(self, app, runtime: WorldRuntime, universe: str, *, max_sessions=10000, idle_timeout=1800):
        self.app, self.runtime, self.universe = app, runtime, universe
        self.sessions: dict[str, tuple[str, float]] = {}
        self.max_sessions, self.idle_timeout = max_sessions, idle_timeout

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        raw = scope.get("headers", [])
        for sensitive in (b"authorization", b"mcp-session-id"):
            if sum(key.lower() == sensitive for key, _ in raw) > 1:
                await self._reject(
                    send,
                    400,
                    {"error": "InvalidArguments", "message": "duplicate authentication or session header"},
                )
                return
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        sid = headers.get("mcp-session-id")
        try:
            identity = await asyncio.to_thread(
                resolve_authorization, self.runtime, self.universe, headers.get("authorization")
            )
            now = time.monotonic()
            for key in [key for key, (_, seen) in self.sessions.items() if now - seen > self.idle_timeout]:
                self.sessions.pop(key, None)
            if sid:
                binding = self.sessions.get(sid)
                if binding is None:
                    await self._reject(
                        send, 404, {"error": "SessionExpired", "message": "initialize a new MCP session"}
                    )
                    return
                if binding[0] != identity["token_id"]:
                    raise IdentityScopeMismatch("MCP session belongs to another identity credential")
                self.sessions[sid] = (binding[0], now)
            elif len(self.sessions) >= self.max_sessions:
                await self._reject(
                    send, 503, {"error": "SessionCapacity", "message": "session limit reached"}
                )
                return
        except Exception as exc:
            status, payload = error_response(exc)
            await self._reject(send, status, payload)
            return

        async def bound_send(message):
            if message["type"] == "http.response.start":
                pairs = list(message.get("headers", []))
                for key, value in pairs:
                    if key.lower() == b"mcp-session-id":
                        self.sessions[value.decode("latin1")] = (identity["token_id"], time.monotonic())
                pairs.append((b"cache-control", b"no-store"))
                message = {**message, "headers": pairs}
                if scope.get("method") == "DELETE" and sid and message["status"] < 400:
                    self.sessions.pop(sid, None)
            await send(message)

        await self.app(scope, receive, bound_send)

    async def _reject(self, send, status, payload):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


def _annotations(read_only, *, idempotent=True):
    return types.ToolAnnotations(
        readOnlyHint=read_only, destructiveHint=not read_only, idempotentHint=idempotent, openWorldHint=False
    )


def _core_tools(auth_required=False):
    return [
        types.Tool(
            name=name,
            description=desc,
            inputSchema=core_schema(name, auth_required=auth_required),
            annotations=_annotations(
                read_only, idempotent=name not in {"world.bootstrap", "world.claim_activity"}
            ),
        )
        for name, (desc, _, _, read_only) in CORE.items()
    ]


def _function_tool(desc, universe, auth_required=False):
    output = None
    if desc.get("output_schema") is not None:
        output = {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "result": embedded_arguments_schema(desc["output_schema"], "result"),
            },
            "required": ["ok", "result"],
        }
    return types.Tool(
        name=desc["function_id"],
        description=desc["description"],
        inputSchema=function_schema(desc, auth_required=auth_required),
        outputSchema=output,
        annotations=_annotations(desc["access"] == "read"),
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


def create_mcp_server(db_path: str | pathlib.Path, universe="demo", *, auth_required=False, installer=None):
    runtime = WorldRuntime(db_path)
    (installer or get_universe_installer("demo"))(runtime, universe)
    gateway = WorldGateway(runtime, universe, auth_required=auth_required)

    def request_authorization(ctx):
        # The SDK attaches HTTP headers to EACH inbound message. A session/task ContextVar
        # is not an authorization boundary and may retain the initialize request's identity.
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            headers = getattr(getattr(ctx, "transport", None), "headers", None)
        authorization = headers.get("authorization") if headers else None
        if auth_required and not authorization:
            raise AuthenticationRequired("authenticated MCP request headers are required")
        return authorization

    async def list_tools(ctx, params):
        authorization = request_authorization(ctx)
        identity, token = await asyncio.to_thread(gateway.identity, authorization)
        descs = await asyncio.to_thread(
            runtime.list_functions,
            universe,
            actor_role_id=identity["role_id"] if identity else None,
            identity_token=token,
        )
        core = _core_tools(auth_required)
        if identity and identity["access_mode"] == "observe":
            core = [t for t in core if CORE[t.name][3] or t.name == "world.bootstrap"]
        tools = core + [_function_tool(desc, universe, auth_required) for desc in descs]
        tools.sort(key=lambda tool: tool.name)
        cursor = getattr(params, "cursor", None) or ""
        if not isinstance(cursor, str) or len(cursor) > 128:
            from .runtime_errors import InvalidArguments

            raise InvalidArguments("invalid tool discovery cursor")
        tools = [tool for tool in tools if tool.name > cursor]
        page = tools[:100]
        return types.ListToolsResult(tools=page, nextCursor=page[-1].name if len(tools) > 100 else None)

    async def call_tool(ctx, params):
        try:
            authorization = request_authorization(ctx)
            args = params.arguments if params.arguments is not None else {}
            if params.name == "world.wait_changes":
                event = getattr(ctx, "cancel_requested", None)
                result = await gateway.wait(args, authorization, cancelled=event.is_set if event else None)
            else:
                result = await asyncio.to_thread(gateway.call, params.name, args, authorization)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json_text(result))],
                structuredContent=result,
                isError=False,
            )
        except Exception as exc:
            status, payload = error_response(exc)
            if status == 500:
                logger.error("MCP world request failed: %s", type(exc).__name__)
            payload["tool"] = params.name
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json_text(payload))],
                structuredContent=payload,
                isError=True,
            )

    return Server(
        "agent-world",
        version=PROTOCOL_VERSION,
        description="World functions backed by persistent, authenticated state.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    ), runtime


def create_mcp_app(db_path, universe="demo", *, auth_required=True, installer=None, host="127.0.0.1"):
    server, runtime = create_mcp_server(db_path, universe, auth_required=auth_required, installer=installer)
    base = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        max_request_body_size=262144,
        session_idle_timeout=1800,
        max_sessions=10000,
        host=host,
    )
    app = BearerIdentityMiddleware(base, runtime, universe) if auth_required else base
    return app, server, runtime


from .asgi_lifecycle import LazyASGI


def _from_environment():
    db = os.getenv("WORLD_DB", str(pathlib.Path.cwd() / "agent-world.sqlite3"))
    universe = os.getenv("WORLD_UNIVERSE", "demo")
    return create_mcp_app(
        db,
        universe,
        auth_required=os.getenv("WORLD_AUTH_REQUIRED", "1") == "1",
        installer=get_universe_installer(os.getenv("WORLD_PROFILE", "demo")),
        host=os.getenv("WORLD_HOST", "127.0.0.1"),
    )[0]


app = LazyASGI(_from_environment)
