from __future__ import annotations

import asyncio
import os
import pathlib
import time
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from demo_universe import install_demo_universe
from identity_auth import bound_role, resolve_authorization
from universe_loader import get_universe_installer
from runtime_core import (
    ActivityConflict,
    AuthenticationRequired,
    ActivityNotFound,
    ClaimBusy,
    ClaimFenced,
    CursorExpired,
    FunctionNotFound,
    IdentityScopeMismatch,
    InvalidIdentityToken,
    FunctionVersionMismatch,
    OperationConflict,
    RegistryConflict,
    SchemaRejected,
    WorldRuntime,
    WorldRuntimeError,
)


class InvokeRequest(BaseModel):
    role_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    arguments: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)
    activity_claim: dict[str, Any] | None = None


class StartActivityRequest(BaseModel):
    role_id: str | None = Field(default=None, min_length=1, max_length=128)
    activity_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    exclusive_group: str | None = Field(default=None, max_length=128)
    ttl_seconds: float | None = Field(default=None, gt=0)


class ClaimActivityRequest(BaseModel):
    role_id: str | None = Field(default=None, min_length=1, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    lease_seconds: float = Field(default=30.0, gt=0, le=3600)


def _status_for(exc: WorldRuntimeError) -> int:
    if isinstance(exc, (AuthenticationRequired, InvalidIdentityToken)):
        return 401
    if isinstance(exc, IdentityScopeMismatch):
        return 403
    if isinstance(exc, (FunctionNotFound, ActivityNotFound)):
        return 404
    if isinstance(exc, SchemaRejected):
        return 422
    if isinstance(
        exc,
        (
            FunctionVersionMismatch,
            OperationConflict,
            ActivityConflict,
            ClaimBusy,
            ClaimFenced,
            RegistryConflict,
            CursorExpired,
        ),
    ):
        return 409
    return 500


def create_app(
    db_path: str | pathlib.Path,
    universe: str = "demo",
    *,
    test_mode: bool = False,
    auth_required: bool = False,
    installer=None,
) -> FastAPI:
    runtime = WorldRuntime(db_path)
    (installer or install_demo_universe)(runtime, universe)

    app = FastAPI(title="Agent World Runtime v0.6 HTTP", version="0.6")
    app.state.runtime = runtime
    app.state.universe = universe
    app.state.test_mode = test_mode
    app.state.auth_required = auth_required

    def request_identity(request: Request) -> dict[str, Any] | None:
        if not auth_required:
            return None
        return resolve_authorization(
            runtime, universe, request.headers.get("authorization")
        )

    @app.exception_handler(WorldRuntimeError)
    async def world_error_handler(_: Request, exc: WorldRuntimeError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={"error": type(exc).__name__, "message": str(exc)},
        )

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "version": "0.6",
            "universe": universe,
            "auth_required": auth_required,
        }

    @app.get("/v1/functions")
    def list_functions(request: Request):
        request_identity(request)
        return {"universe": universe, "functions": runtime.list_functions(universe)}

    @app.get("/v1/whoami")
    def whoami(request: Request):
        identity = request_identity(request)
        if identity is None:
            raise AuthenticationRequired("identity endpoint requires auth mode")
        return identity

    @app.get("/v1/bootstrap")
    def bootstrap_authenticated(
        request: Request,
        role_id: str | None = Query(default=None, min_length=1, max_length=128),
    ):
        role = bound_role(
            request_identity(request), role_id, auth_required=auth_required
        )
        return runtime.bootstrap(universe, role)

    @app.get("/v1/bootstrap/{role_id}")
    def bootstrap(role_id: str, request: Request):
        role = bound_role(
            request_identity(request), role_id, auth_required=auth_required
        )
        return runtime.bootstrap(universe, role)

    @app.post("/v1/activities")
    def start_activity(body: StartActivityRequest, request: Request):
        role = bound_role(
            request_identity(request), body.role_id, auth_required=auth_required
        )
        return runtime.start_activity(
            universe,
            body.activity_id,
            role,
            body.kind,
            exclusive_group=body.exclusive_group,
            ttl_seconds=body.ttl_seconds,
        )

    @app.post("/v1/activities/{activity_id}/claim")
    def claim_activity(
        activity_id: str, body: ClaimActivityRequest, request: Request
    ):
        role = bound_role(
            request_identity(request), body.role_id, auth_required=auth_required
        )
        return runtime.claim_activity(
            universe,
            activity_id,
            role,
            body.runtime_id,
            lease_seconds=body.lease_seconds,
        )


    @app.post("/v1/functions/{function_id}/invoke")
    def invoke_function(
        function_id: str,
        body: InvokeRequest,
        request: Request,
        x_test_response_delay_ms: int | None = Header(
            default=None, alias="X-Test-Response-Delay-Ms"
        ),
    ):
        role = bound_role(
            request_identity(request), body.role_id, auth_required=auth_required
        )
        desc = runtime.get_function(universe, function_id)
        if desc["access"] == "read":
            result = runtime.query_function(
                universe,
                function_id,
                role,
                body.arguments,
                expected_version=body.expected_version,
            )
        else:
            result = runtime.invoke_function(
                universe,
                function_id,
                role,
                body.arguments,
                operation_id=body.operation_id,
                expected_version=body.expected_version,
                activity_claim=body.activity_claim,
            )
            if test_mode and x_test_response_delay_ms:
                delay = max(0, min(int(x_test_response_delay_ms), 5000))
                time.sleep(delay / 1000.0)
        result["registry_version"] = desc["version"]
        return result

    @app.get("/v1/changes")
    def get_changes(
        request: Request,
        role_id: str | None = Query(default=None, min_length=1, max_length=128),
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        role = bound_role(
            request_identity(request), role_id, auth_required=auth_required
        )
        rows = runtime.read_changes(universe, role, after, limit)
        return {
            "events": rows,
            "next_cursor": rows[-1]["seq"] if rows else after,
            "event_floor": runtime.event_floor(universe),
        }

    @app.get("/v1/changes/wait")
    async def wait_changes(
        request: Request,
        role_id: str | None = Query(default=None, min_length=1, max_length=128),
        after: int = Query(default=0, ge=0),
        timeout: float = Query(default=5.0, ge=0, le=30),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        role = bound_role(
            request_identity(request), role_id, auth_required=auth_required
        )
        # HTTP clients can disappear without delivering an application-level
        # cancellation callback. Do not dedicate one worker thread to the
        # entire wait: a disconnected client would hold that thread until the
        # original timeout and many such clients could exhaust the pool.
        #
        # Instead, HTTP fallback uses short durable-log checks with an async
        # sleep between them. This is server-side waiting, not model polling:
        # it consumes no additional LLM turns/tokens and works across multiple
        # server processes because the database remains the source of truth.
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        poll_interval = 0.10
        rows = []
        while True:
            rows = runtime.read_changes(universe, role, after, limit)
            if rows:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            if await request.is_disconnected():
                # No response can be delivered, so stop immediately and leave
                # the durable event log untouched for the next client/session.
                return JSONResponse(
                    status_code=499,
                    content={
                        "error": "ClientDisconnected",
                        "message": "bounded wait cancelled because the HTTP client disconnected",
                    },
                )
            await asyncio.sleep(min(poll_interval, remaining))

        return {
            "events": rows,
            "next_cursor": rows[-1]["seq"] if rows else after,
            "timed_out": not bool(rows),
            "event_floor": runtime.event_floor(universe),
        }

    return app


DEFAULT_DB = os.getenv(
    "WORLD_DB",
    str(pathlib.Path(__file__).resolve().parent / "runtime_http.sqlite3"),
)
DEFAULT_UNIVERSE = os.getenv("WORLD_UNIVERSE", "demo")
WORLD_PROFILE = os.getenv("WORLD_PROFILE", "demo")
TEST_MODE = os.getenv("WORLD_TEST_MODE", "0") == "1"
AUTH_REQUIRED = os.getenv("WORLD_AUTH_REQUIRED", "0") == "1"
app = create_app(
    DEFAULT_DB,
    DEFAULT_UNIVERSE,
    test_mode=TEST_MODE,
    auth_required=AUTH_REQUIRED,
    installer=get_universe_installer(WORLD_PROFILE),
)
