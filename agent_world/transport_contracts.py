from __future__ import annotations

import asyncio
import sqlite3

from .identity_auth import resolve_authorization, bound_role
from .runtime_contracts import (
    CORE_TOOL_NAMES,
    arguments_object,
    embedded_arguments_schema,
    identifier,
    validate,
)
from .runtime_errors import (
    AccessModeMismatch,
    ActivityConflict,
    ActivityNotFound,
    AuthenticationRequired,
    ClaimBusy,
    ClaimFenced,
    CursorExpired,
    CursorInvalid,
    FunctionNotFound,
    FunctionVersionMismatch,
    IdentityScopeMismatch,
    InvalidArguments,
    InvalidIdentityToken,
    JoinTicketConsumed,
    JoinTicketExpired,
    JoinTicketInvalid,
    OperationConflict,
    PermissionDenied,
    ReceiptNotFound,
    RegistryConflict,
    RoleInactive,
    RoleInvalid,
    RoleNotFound,
    RuleViolation,
    SchemaRejected,
    StateConflict,
    StorageBusy,
    WorldVersionMismatch,
)

STRING = {"type": "string", "minLength": 1, "maxLength": 128}
INT = {"type": "integer", "minimum": 0}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
CLAIM = {
    "type": "object",
    "properties": {
        "activity_id": STRING,
        "runtime_id": STRING,
        "claim_epoch": {"type": "integer", "minimum": 1},
        "claim_token": STRING,
        "claim_until": {"type": "number"},
        "replayed": {"type": "boolean"},
    },
    "required": ["activity_id", "runtime_id", "claim_epoch", "claim_token"],
    "additionalProperties": False,
}

CORE = {
    "world.bootstrap": (
        "Recover current identity and a bounded world snapshot, not private Agent memory.",
        {"include_catalog": {"type": "boolean"}},
        [],
        False,
    ),
    "world.discover": (
        "Discover world functions in bounded pages; load full schemas only when needed.",
        {
            "prefix": {"type": "string", "maxLength": 128},
            "after": {"type": "string", "maxLength": 128},
            "limit": LIMIT,
            "include_schemas": {"type": "boolean"},
        },
        [],
        True,
    ),
    "world.get_receipt": (
        "Read the committed receipt of one operation without repeating the action.",
        {"operation_id": STRING},
        ["operation_id"],
        True,
    ),
    "world.start_activity": (
        "Start a role activity. Its exclusive group is local to that role.",
        {
            "activity_id": STRING,
            "kind": STRING,
            "exclusive_group": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
            "ttl_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 86400},
        },
        ["activity_id", "kind"],
        False,
    ),
    "world.claim_activity": (
        "Acquire a finite activity lease with fencing against stale runtimes.",
        {
            "activity_id": STRING,
            "runtime_id": STRING,
            "lease_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
        },
        ["activity_id", "runtime_id"],
        False,
    ),
    "world.renew_claim": (
        "Renew a live claim using a stable operation ID; retry returns the prior result.",
        {
            "operation_id": STRING,
            "activity_claim": CLAIM,
            "lease_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
        },
        ["operation_id", "activity_claim"],
        False,
    ),
    "world.finish_activity": (
        "Complete or cancel a claimed activity and release its exclusive group.",
        {
            "operation_id": STRING,
            "activity_claim": CLAIM,
            "status": {"type": "string", "enum": ["completed", "cancelled"]},
        },
        ["operation_id", "activity_claim"],
        False,
    ),
    "world.get_changes": (
        "Read retained recipient events after a cursor; recover with bootstrap if expired.",
        {"after": INT, "limit": LIMIT},
        [],
        True,
    ),
    "world.wait_changes": (
        "Wait at most 30 seconds for events; a timeout is not a new action.",
        {"after": INT, "limit": LIMIT, "timeout": {"type": "number", "minimum": 0, "maximum": 30}},
        [],
        True,
    ),
}
assert frozenset(CORE) == CORE_TOOL_NAMES


def core_schema(name, *, auth_required):
    _, properties, required, _ = CORE[name]
    properties, required = dict(properties), list(required)
    if not auth_required:
        properties["role_id"] = STRING
        required.append("role_id")
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def function_schema(desc, *, auth_required):
    props = {
        "arguments": embedded_arguments_schema(desc["input_schema"]),
        "expected_version": {"type": "integer", "minimum": 1},
    }
    required = ["arguments"]
    if not auth_required:
        props["role_id"] = STRING
        required.append("role_id")
    if desc["access"] == "write":
        props["operation_id"] = STRING
        required.append("operation_id")
    if desc["requires_activity_claim"]:
        props["activity_claim"] = CLAIM
        required.append("activity_claim")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def error_response(exc: Exception):
    if isinstance(exc, (AuthenticationRequired, InvalidIdentityToken)):
        status = 401
    elif isinstance(exc, (IdentityScopeMismatch, PermissionDenied, RoleInactive)):
        status = 403
    elif isinstance(exc, (FunctionNotFound, RoleNotFound, ActivityNotFound, ReceiptNotFound)):
        status = 404
    elif isinstance(exc, (JoinTicketExpired, JoinTicketConsumed)):
        status = 410
    elif isinstance(exc, (InvalidArguments, SchemaRejected, RoleInvalid, AccessModeMismatch)):
        status = 422
    elif isinstance(
        exc,
        (
            RegistryConflict,
            OperationConflict,
            ActivityConflict,
            ClaimBusy,
            ClaimFenced,
            StateConflict,
            FunctionVersionMismatch,
            WorldVersionMismatch,
            CursorExpired,
            CursorInvalid,
            RuleViolation,
        ),
    ):
        status = 409
    elif isinstance(exc, JoinTicketInvalid):
        status = 400
    elif isinstance(exc, StorageBusy) or (
        isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
    ):
        exc, status = StorageBusy("world storage is busy; retry the same operation_id"), 503
    else:
        return 500, {
            "error": "InternalError",
            "message": "world request failed; inspect server logs",
            "retryable": False,
            "recovery": "query_receipt_before_retry",
        }
    payload = {"error": type(exc).__name__, "message": str(exc), "retryable": bool(exc.retryable)}
    if isinstance(exc, CursorExpired):
        payload.update(recovery="bootstrap", event_floor=getattr(exc, "event_floor", None))
    if isinstance(exc, ReceiptNotFound):
        payload["recovery"] = "no_committed_receipt_at_lookup; retry_only_with_same_operation_id"
    return status, payload


class WorldGateway:
    def __init__(self, runtime, universe, *, auth_required):
        self.runtime, self.universe, self.auth_required = runtime, universe, auth_required

    def identity(self, authorization):
        if self.auth_required:
            identity = resolve_authorization(self.runtime, self.universe, authorization)
            token = authorization.partition(" ")[2].strip()
            return identity, token
        return None, None

    def prepare(self, name, arguments, authorization):
        arguments_object(arguments)
        identity, token = self.identity(authorization)
        supplied_role = arguments.get("role_id")
        if supplied_role is not None:
            identifier(supplied_role, "role_id")
        role = bound_role(identity, supplied_role, auth_required=self.auth_required)
        args = dict(arguments)
        if self.auth_required:
            args.pop("role_id", None)  # Matching legacy role is tolerated but not advertised.
        if name in CORE:
            validate(core_schema(name, auth_required=self.auth_required), args)
        else:
            # A committed intent remains replayable after a schema upgrade or removal.
            replay = False
            if args.get("operation_id") is not None:
                try:
                    self.runtime.get_receipt(self.universe, role, args["operation_id"], identity_token=token)
                    replay = True
                except ReceiptNotFound:
                    pass
            if replay:
                props = {
                    "arguments": {"type": "object"},
                    "operation_id": STRING,
                    "expected_version": {"type": "integer", "minimum": 1},
                    "activity_claim": CLAIM,
                }
                if not self.auth_required:
                    props["role_id"] = STRING
                validate(
                    {
                        "type": "object",
                        "properties": props,
                        "required": ["arguments", "operation_id"],
                        "additionalProperties": False,
                    },
                    args,
                )
            else:
                desc = self.runtime.get_function(self.universe, name)
                if desc["access"] == "write" and not args.get("operation_id"):
                    raise OperationConflict("operation_id is required for write functions")
                validate(function_schema(desc, auth_required=self.auth_required), args)
        args.pop("role_id", None)
        return role, token, args

    def call(self, name, arguments, authorization=None):
        role, token, args = self.prepare(name, arguments, authorization)
        r, u = self.runtime, self.universe
        if name == "world.bootstrap":
            return r.bootstrap(
                u,
                role,
                identity_token=token,
                include_catalog=args.get("include_catalog", False),
                record_presence=self.auth_required,
            )
        if name == "world.discover":
            return r.discover_functions(u, actor_role_id=role, identity_token=token, **args)
        if name == "world.get_receipt":
            return r.get_receipt(u, role, args["operation_id"], identity_token=token)
        if name == "world.start_activity":
            return r.start_activity(
                u,
                args["activity_id"],
                role,
                args["kind"],
                exclusive_group=args.get("exclusive_group"),
                ttl_seconds=args.get("ttl_seconds"),
                identity_token=token,
            )
        if name == "world.claim_activity":
            return r.claim_activity(
                u,
                args["activity_id"],
                role,
                args["runtime_id"],
                lease_seconds=args.get("lease_seconds", 30),
                identity_token=token,
            )
        if name == "world.renew_claim":
            return r.renew_claim(
                u,
                role,
                args["activity_claim"],
                operation_id=args["operation_id"],
                lease_seconds=args.get("lease_seconds", 30),
                identity_token=token,
            )
        if name == "world.finish_activity":
            return r.finish_activity(
                u,
                role,
                args["activity_claim"],
                operation_id=args["operation_id"],
                status=args.get("status", "completed"),
                identity_token=token,
            )
        if name == "world.get_changes":
            return r.read_changes_page(
                u, role, args.get("after", 0), args.get("limit", 50), identity_token=token
            )
        if name == "world.wait_changes":
            raise InvalidArguments("bounded wait requires the asynchronous gateway")
        return r.call_function(
            u,
            name,
            role,
            args["arguments"],
            operation_id=args.get("operation_id"),
            expected_version=args.get("expected_version"),
            activity_claim=args.get("activity_claim"),
            identity_token=token,
        )

    async def wait(self, arguments, authorization=None, *, cancelled=None, disconnected=None):
        role, token, args = await asyncio.to_thread(
            self.prepare, "world.wait_changes", arguments, authorization
        )
        deadline = asyncio.get_running_loop().time() + args.get("timeout", 5.0)
        after, limit = args.get("after", 0), args.get("limit", 50)
        while True:
            page = await asyncio.to_thread(
                self.runtime.read_changes_page, self.universe, role, after, limit, identity_token=token
            )
            if page["events"]:
                return {**page, "timed_out": False, "cancelled": False}
            if (cancelled is not None and cancelled()) or (disconnected is not None and await disconnected()):
                return {**page, "timed_out": False, "cancelled": True}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {**page, "timed_out": True, "cancelled": False}
            await asyncio.sleep(min(0.1, remaining))
