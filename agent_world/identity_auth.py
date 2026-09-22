from __future__ import annotations

from typing import Any

from .runtime_core import (
    AuthenticationRequired,
    IdentityScopeMismatch,
    InvalidIdentityToken,
    WorldRuntime,
)


def resolve_authorization(
    runtime: WorldRuntime,
    universe: str,
    authorization: str | None,
) -> dict[str, Any]:
    if not authorization:
        raise AuthenticationRequired("missing Authorization header")
    scheme, sep, value = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer" or not value.strip():
        raise AuthenticationRequired("expected Authorization: Bearer <identity token>")
    identity = runtime.resolve_identity_token(value.strip())
    if identity["universe"] != universe:
        raise IdentityScopeMismatch("identity token is not valid for this universe")
    return identity


def bound_role(
    identity: dict[str, Any] | None,
    supplied_role_id: str | None,
    *,
    auth_required: bool,
) -> str:
    if auth_required:
        if identity is None:
            raise AuthenticationRequired("authenticated identity is required")
        role_id = str(identity["role_id"])
        if supplied_role_id is not None and supplied_role_id != role_id:
            raise IdentityScopeMismatch("request role_id does not match authenticated identity")
        return role_id
    if not supplied_role_id:
        raise InvalidIdentityToken("role_id is required when authentication is disabled")
    return supplied_role_id
