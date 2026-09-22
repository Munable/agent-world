from __future__ import annotations

import hmac
import os
import pathlib
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from runtime_core import (
    InvalidIdentityToken,
    JoinTicketConsumed,
    JoinTicketExpired,
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


class UpdateRoleRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar_ref: str | None = Field(default=None, max_length=1024)
class SetRoleStatusRequest(BaseModel):
    status: str


class IssueJoinTicketRequest(BaseModel):
    role_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: float = Field(default=600.0, gt=0, le=86400)


class ExchangeJoinTicketRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=256)


class RotateTokenRequest(BaseModel):
    ttl_seconds: float | None = Field(default=None, gt=0)


def _status_for(exc: WorldRuntimeError) -> int:
    if isinstance(exc, (RoleNotFound, InvalidIdentityToken)):
        return 404
    if isinstance(exc, RoleInvalid):
        return 422
    if isinstance(exc, JoinTicketInvalid):
        return 400
    if isinstance(exc, (JoinTicketExpired, JoinTicketConsumed)):
        return 410
    if isinstance(exc, RoleInactive):
        return 409
    return 400
def create_onboarding_app(
    db_path: str | pathlib.Path,
    universe: str = "demo",
    *,
    mcp_url: str = "/mcp",
    public_base_url: str | None = None,
    operator_key: str | None = None,
    identity_ttl_seconds: float | None = None,
) -> FastAPI:
    runtime = WorldRuntime(db_path)
    app = FastAPI(title="Agent World Onboarding", version="0.5")
    app.state.runtime = runtime
    app.state.universe = universe

    def require_operator(value: str | None) -> None:
        if not operator_key:
            raise HTTPException(
                status_code=503,
                detail="operator authorization is not configured",
            )
        if value is None:
            raise HTTPException(status_code=401, detail="operator key required")
        if not hmac.compare_digest(value, operator_key):
            raise HTTPException(status_code=403, detail="invalid operator key")

    def exchange_url() -> str:
        if public_base_url:
            return public_base_url.rstrip("/") + "/v1/join/exchange"
        return "/v1/join/exchange"
    @app.exception_handler(WorldRuntimeError)
    async def runtime_error(_: Request, exc: WorldRuntimeError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={"error": type(exc).__name__, "message": str(exc)},
        )

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "version": "0.5",
            "universe": universe,
            "operator_configured": bool(operator_key),
        }

    @app.post("/v1/roles")
    def create_role(
        body: CreateRoleRequest,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        return runtime.create_role(body.display_name, body.avatar_ref)

    @app.get("/v1/roles/{role_id}")
    def get_role(
        role_id: str,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        return runtime.get_role(role_id)
    @app.patch("/v1/roles/{role_id}")
    def update_role(
        role_id: str,
        body: UpdateRoleRequest,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        fields: dict[str, Any] = {}
        if "display_name" in body.model_fields_set:
            if body.display_name is None:
                raise HTTPException(
                    status_code=422,
                    detail="display_name cannot be null",
                )
            fields["display_name"] = body.display_name
        if "avatar_ref" in body.model_fields_set:
            fields["avatar_ref"] = body.avatar_ref
        return runtime.update_role_profile(role_id, **fields)

    @app.post("/v1/roles/{role_id}/status")
    def set_role_status(
        role_id: str,
        body: SetRoleStatusRequest,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        return runtime.set_role_status(role_id, body.status)
    @app.post("/v1/join-tickets")
    def issue_join_ticket(
        body: IssueJoinTicketRequest,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        role = runtime.get_role(body.role_id)
        issued = runtime.issue_join_ticket(
            universe,
            body.role_id,
            ttl_seconds=body.ttl_seconds,
        )
        return {
            "version": "agent-world-join-v1",
            "universe": universe,
            "role_profile": role,
            "join": {
                "ticket_id": issued["ticket_id"],
                "ticket": issued["ticket"],
                "expires_at": issued["expires_at"],
                "exchange_url": exchange_url(),
            },
            "mcp": {
                "url": mcp_url,
                "authorization": "Bearer <identity_token returned by exchange>",
            },
            "steps": [
                "Exchange the join ticket once at join.exchange_url.",
                "Store the returned identity token as a secret.",
                "Connect to mcp.url using Authorization: Bearer <identity_token>.",
                "Call world.bootstrap with no role_id argument.",
            ],
        }
    @app.post("/v1/join/exchange")
    def exchange_join_ticket(body: ExchangeJoinTicketRequest):
        identity = runtime.exchange_join_ticket(
            body.ticket,
            identity_ttl_seconds=identity_ttl_seconds,
        )
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
            "next": {
                "tool": "world.bootstrap",
                "arguments": {},
            },
        }

    @app.post("/v1/identity-tokens/{token_id}/rotate")
    def rotate_identity_token(
        token_id: str,
        body: RotateTokenRequest,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        return runtime.rotate_identity_token(
            token_id,
            ttl_seconds=body.ttl_seconds,
        )
    @app.delete("/v1/identity-tokens/{token_id}")
    def revoke_identity_token(
        token_id: str,
        x_operator_key: str | None = Header(
            default=None, alias="X-Operator-Key"
        ),
    ):
        require_operator(x_operator_key)
        changed = runtime.revoke_identity_token(token_id)
        if not changed:
            raise InvalidIdentityToken("identity token not found or already revoked")
        return {"revoked": True, "token_id": token_id}

    return app


DEFAULT_DB = os.getenv(
    "WORLD_DB",
    str(pathlib.Path(__file__).resolve().parent / "onboarding.sqlite3"),
)
DEFAULT_UNIVERSE = os.getenv("WORLD_UNIVERSE", "demo")
MCP_URL = os.getenv("WORLD_MCP_PUBLIC_URL", "/mcp")
PUBLIC_BASE_URL = os.getenv("WORLD_PUBLIC_BASE_URL")
OPERATOR_KEY = os.getenv("WORLD_OPERATOR_KEY")
IDENTITY_TTL_RAW = os.getenv("WORLD_IDENTITY_TTL_SECONDS")
IDENTITY_TTL = float(IDENTITY_TTL_RAW) if IDENTITY_TTL_RAW else None

app = create_onboarding_app(
    DEFAULT_DB,
    DEFAULT_UNIVERSE,
    mcp_url=MCP_URL,
    public_base_url=PUBLIC_BASE_URL,
    operator_key=OPERATOR_KEY,
    identity_ttl_seconds=IDENTITY_TTL,
)
