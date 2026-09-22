from __future__ import annotations

import base64
import hmac
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from runtime_core import (
    JoinTicketInvalid,
    RoleInactive,
    RoleInvalid,
    RoleNotFound,
    WorldRuntime,
    WorldRuntimeError,
)


class CreateRoleRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    avatar_ref: str | None = Field(default=None, max_length=1024)


class JoinPackageRequest(BaseModel):
    ttl_seconds: float = Field(default=600.0, gt=0, le=86400)


class ExchangeJoinTicketRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=256)


def _runtime_status(exc: WorldRuntimeError) -> int:
    if isinstance(exc, RoleNotFound):
        return 404
    if isinstance(exc, RoleInvalid):
        return 422
    if isinstance(exc, RoleInactive):
        return 409
    if isinstance(exc, JoinTicketInvalid):
        return 400
    return 400
def create_product_app(
    db_path: str | pathlib.Path,
    universe: str = "world-zero",
    *,
    mcp_url: str = "/mcp",
    public_base_url: str | None = None,
    web_user: str = "operator",
    web_password: str | None = None,
) -> FastAPI:
    runtime = WorldRuntime(db_path)
    web_dir = pathlib.Path(__file__).resolve().parent / "web"

    app = FastAPI(title="Agent World", version="0.8")
    app.state.runtime = runtime
    app.state.universe = universe

    def require_web_auth(request: Request) -> None:
        if not web_password:
            raise HTTPException(
                status_code=503,
                detail="WORLD_WEB_PASSWORD is not configured",
            )
        authorization = request.headers.get("authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": 'Basic realm="Agent World"'},
            )
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail="invalid authorization",
                headers={"WWW-Authenticate": 'Basic realm="Agent World"'},
            ) from exc
        valid = (
            hmac.compare_digest(supplied_user, web_user)
            and hmac.compare_digest(supplied_password, web_password)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="Agent World"'},
            )
    def exchange_url() -> str:
        if public_base_url:
            return public_base_url.rstrip("/") + "/v1/join/exchange"
        return "/v1/join/exchange"

    def agent_instructions(role: dict[str, Any], issued: dict[str, Any]) -> str:
        base = (
            "Join Open Agent World as this role.\n\n"
            f"Role: {role['display_name']} ({role['role_id']})\n"
            f"Universe: {universe}\n"
            f"Join exchange URL: {exchange_url()}\n"
            f"Join ticket: {issued['ticket']}\n"
            f"MCP URL: {mcp_url}\n\n"
            "1. POST JSON {\"ticket\": \"<join ticket>\"} to the exchange URL.\n"
            "2. Keep the returned identity token secret. Do not print it back to the user.\n"
            "3. Connect to the MCP URL with Authorization: Bearer <identity token>.\n"
            "4. Call world.bootstrap with an empty argument object.\n"
        )
        if universe == "world-zero":
            return base + (
                "5. Call world.list_places and world.observe.\n"
                "6. Choose one available place and call world.visit.\n"
                "7. Call world.leave_mark once with a short non-secret mark that reflects your arrival.\n"
                "8. Call world.observe again, then tell the user where you went and what persistent traces you observed or left.\n"
            )
        return base + (
            "5. Explore only the non-secret world tools exposed by the server, then summarize what you found.\n"
        )

    @app.exception_handler(WorldRuntimeError)
    async def runtime_error(_: Request, exc: WorldRuntimeError):
        return JSONResponse(
            status_code=_runtime_status(exc),
            content={"error": type(exc).__name__, "message": str(exc)},
        )

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "version": "0.8",
            "universe": universe,
            "web_auth_configured": bool(web_password),
        }

    @app.get("/")
    def index(request: Request):
        require_web_auth(request)
        return FileResponse(web_dir / "index.html")
    @app.post("/v1/join/exchange")
    def exchange_join_ticket(body: ExchangeJoinTicketRequest):
        identity = runtime.exchange_join_ticket(body.ticket)
        return {
            "version": "agent-world-identity-v1",
            "replayed": identity["replayed"],
            "identity": {
                "token_id": identity["token_id"],
                "token": identity["token"],
                "universe": identity["universe"],
                "role_id": identity["role_id"],
                "expires_at": identity["expires_at"],
            },
            "role_profile": identity["role_profile"],
            "mcp": {
                "url": mcp_url,
                "authorization_scheme": "Bearer",
                "authorization_token": identity["token"],
            },
            "next": {"tool": "world.bootstrap", "arguments": {}},
        }

    @app.get("/api/config")
    def config(request: Request):
        require_web_auth(request)
        return {
            "universe": universe,
            "mcp_url": mcp_url,
        }

    @app.post("/api/roles")
    def create_role(body: CreateRoleRequest, request: Request):
        require_web_auth(request)
        return runtime.create_role(body.display_name, body.avatar_ref)

    @app.get("/api/roles/{role_id}/status")
    def role_status(role_id: str, request: Request):
        require_web_auth(request)
        return runtime.get_role_entry_status(universe, role_id)

    @app.post("/api/roles/{role_id}/join-package")
    def create_join_package(
        role_id: str,
        body: JoinPackageRequest,
        request: Request,
    ):
        require_web_auth(request)
        role = runtime.get_role(role_id)
        issued = runtime.issue_join_ticket(
            universe,
            role_id,
            ttl_seconds=body.ttl_seconds,
        )
        return {
            "version": "agent-world-web-join-v1",
            "role_profile": role,
            "universe": universe,
            "ticket_id": issued["ticket_id"],
            "expires_at": issued["expires_at"],
            "agent_instructions": agent_instructions(role, issued),
        }

    app.mount("/static", StaticFiles(directory=web_dir), name="static")
    return app


DEFAULT_DB = os.getenv(
    "WORLD_DB",
    str(pathlib.Path(__file__).resolve().parent / "product.sqlite3"),
)
DEFAULT_UNIVERSE = os.getenv("WORLD_UNIVERSE", "world-zero")
MCP_URL = os.getenv("WORLD_MCP_PUBLIC_URL", "/mcp")
PUBLIC_BASE_URL = os.getenv("WORLD_PUBLIC_BASE_URL")
WEB_USER = os.getenv("WORLD_WEB_USER", "operator")
WEB_PASSWORD = os.getenv("WORLD_WEB_PASSWORD")

app = create_product_app(
    DEFAULT_DB,
    DEFAULT_UNIVERSE,
    mcp_url=MCP_URL,
    public_base_url=PUBLIC_BASE_URL,
    web_user=WEB_USER,
    web_password=WEB_PASSWORD,
)
