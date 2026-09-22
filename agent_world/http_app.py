from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Any

from fastapi import FastAPI, Request, Query, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .runtime_core import WorldRuntime, WorldRuntimeError
from .runtime_contracts import PROTOCOL_VERSION
from .transport_contracts import WorldGateway, error_response
from .universe_loader import get_universe_installer

logger = logging.getLogger(__name__)


def create_app(
    db_path: str | pathlib.Path,
    universe: str = "demo",
    *,
    test_mode=False,
    auth_required=False,
    installer=None,
) -> FastAPI:
    runtime = WorldRuntime(db_path)
    (installer or get_universe_installer("demo"))(runtime, universe)
    gateway = WorldGateway(runtime, universe, auth_required=auth_required)
    app = FastAPI(title="Agent World HTTP", version=PROTOCOL_VERSION)
    app.state.runtime, app.state.universe = runtime, universe
    app.state.auth_required, app.state.test_mode = auth_required, test_mode

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(WorldRuntimeError)
    async def world_error_handler(request: Request, exc: WorldRuntimeError):
        status, payload = error_response(exc)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
        return JSONResponse(payload, status_code=status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def request_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {"error": "InvalidArguments", "message": "invalid request shape", "retryable": False},
            status_code=422,
        )

    async def call(name, args, request):
        try:
            return await asyncio.to_thread(gateway.call, name, args, request.headers.get("authorization"))
        except Exception as exc:
            status, payload = error_response(exc)
            if status == 500:
                logger.error("world request failed: %s", type(exc).__name__)
            return JSONResponse(
                payload, status_code=status, headers={"WWW-Authenticate": "Bearer"} if status == 401 else {}
            )

    def role_args(role_id):
        return {} if role_id is None else {"role_id": role_id}

    @app.get("/health")
    def health():
        return {"ok": True, "version": PROTOCOL_VERSION, "universe": universe, "auth_required": auth_required}

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        from .runtime_errors import AuthenticationRequired

        identity, _ = await asyncio.to_thread(gateway.identity, request.headers.get("authorization"))
        if identity is None:
            raise AuthenticationRequired("identity endpoint requires authentication")
        return identity

    @app.get("/v1/functions")
    async def functions(request: Request, role_id: str | None = None):
        # Compatibility endpoint; discover provides bounded, compact retrieval.
        identity, token = await asyncio.to_thread(gateway.identity, request.headers.get("authorization"))
        if identity is not None:
            from .identity_auth import bound_role

            role_id = bound_role(identity, role_id, auth_required=True)
        functions = await asyncio.to_thread(
            runtime.list_functions, universe, actor_role_id=role_id, identity_token=token
        )
        return {"universe": universe, "functions": functions}

    @app.get("/v1/discover")
    async def discover(
        request: Request,
        role_id: str | None = None,
        prefix: str = "",
        after: str = "",
        limit: int = Query(default=50, ge=1, le=200),
        include_schemas: bool = False,
    ):
        return await call(
            "world.discover",
            {
                **role_args(role_id),
                "prefix": prefix,
                "after": after,
                "limit": limit,
                "include_schemas": include_schemas,
            },
            request,
        )

    @app.get("/v1/bootstrap")
    async def bootstrap(request: Request, role_id: str | None = None, include_catalog: bool = False):
        return await call(
            "world.bootstrap", {**role_args(role_id), "include_catalog": include_catalog}, request
        )

    @app.get("/v1/bootstrap/{role_id}")
    async def bootstrap_legacy(role_id: str, request: Request, include_catalog: bool = False):
        return await call(
            "world.bootstrap", {"role_id": role_id, "include_catalog": include_catalog}, request
        )

    @app.get("/v1/views")
    async def views(request: Request, role_id: str | None = None, after: str = "",
                    limit: int = Query(default=50, ge=1, le=64), include_schemas: bool = False):
        return await call("world.list_views", {**role_args(role_id), "after": after,
                          "limit": limit, "include_schemas": include_schemas}, request)

    @app.post("/v1/views/timeline")
    async def view_timeline(body: dict[str, Any], request: Request):
        return await call("world.view_timeline", body, request)

    @app.post("/v1/views/sync")
    async def sync_view(body: dict[str, Any], request: Request):
        return await call("world.view_sync", body, request)

    @app.post("/v1/views/{view}/snapshot")
    async def snapshot_view(view: str, body: dict[str, Any], request: Request):
        return await call("world.view_snapshot", {**body, "view": view}, request)

    @app.get("/v1/receipts/{operation_id}")
    async def receipt(operation_id: str, request: Request, role_id: str | None = None):
        return await call("world.get_receipt", {**role_args(role_id), "operation_id": operation_id}, request)

    @app.post("/v1/activities")
    async def start_activity(body: dict[str, Any], request: Request):
        return await call("world.start_activity", body, request)

    @app.post("/v1/activities/{activity_id}/claim")
    async def claim_activity(activity_id: str, body: dict[str, Any], request: Request):
        return await call("world.claim_activity", {**body, "activity_id": activity_id}, request)

    @app.post("/v1/activities/renew")
    async def renew(body: dict[str, Any], request: Request):
        return await call("world.renew_claim", body, request)

    @app.post("/v1/activities/finish")
    async def finish(body: dict[str, Any], request: Request):
        return await call("world.finish_activity", body, request)

    @app.post("/v1/functions/{function_id}/invoke")
    async def invoke(
        function_id: str,
        body: dict[str, Any],
        request: Request,
        x_test_response_delay_ms: int | None = Header(default=None, alias="X-Test-Response-Delay-Ms"),
    ):
        result = await call(function_id, body, request)
        if test_mode and x_test_response_delay_ms:
            await asyncio.sleep(max(0, min(x_test_response_delay_ms, 5000)) / 1000)
        return result

    @app.get("/v1/changes")
    async def changes(
        request: Request,
        role_id: str | None = None,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return await call(
            "world.get_changes", {**role_args(role_id), "after": after, "limit": limit}, request
        )

    @app.get("/v1/changes/wait")
    async def wait(
        request: Request,
        role_id: str | None = None,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        timeout: float = Query(default=5, ge=0, le=30),
    ):
        try:
            return await gateway.wait(
                {**role_args(role_id), "after": after, "limit": limit, "timeout": timeout},
                request.headers.get("authorization"),
                disconnected=request.is_disconnected,
            )
        except Exception as exc:
            status, payload = error_response(exc)
            return JSONResponse(payload, status_code=status)

    return app


from .asgi_lifecycle import LazyASGI


def _from_environment():
    db = os.getenv("WORLD_DB", str(pathlib.Path.cwd() / "agent-world.sqlite3"))
    universe = os.getenv("WORLD_UNIVERSE", "demo")
    return create_app(
        db,
        universe,
        test_mode=os.getenv("WORLD_TEST_MODE", "0") == "1",
        auth_required=os.getenv("WORLD_AUTH_REQUIRED", "1") == "1",
        installer=get_universe_installer(os.getenv("WORLD_PROFILE", "demo")),
    )


app = LazyASGI(_from_environment)
