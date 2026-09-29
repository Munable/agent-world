from __future__ import annotations

from agent_world import EventSpec, FunctionOutcome, FunctionSpec, StateRule, WorldDefinition
from agent_world.errors import PermissionDenied, RoleInactive, RuleViolation

ID = {"type": "string", "minLength": 1, "maxLength": 128}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
BROADCAST = {
    "type": "object",
    "properties": {
        "message_id": ID,
        "recipients": {
            "type": "array",
            "items": ID,
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
    },
    "required": ["message_id", "recipients"],
    "additionalProperties": False,
}
CONTRIBUTE = {
    "type": "object",
    "properties": {
        "target_role_id": ID,
        "contribution_id": ID,
        "value": {"type": "integer"},
    },    "required": ["target_role_id", "contribution_id", "value"],
    "additionalProperties": False,
}
AGGREGATE = {
    "type": "object",
    "properties": {"target_role_id": ID},
    "required": ["target_role_id"],
    "additionalProperties": False,
}
CLAIM_SLOT = {
    "type": "object",
    "properties": {
        "slot_id": ID,
        "expected_version": {"type": "integer", "minimum": 0},
    },
    "required": ["slot_id", "expected_version"],
    "additionalProperties": False,
}


def _require_active(ctx, role_id):
    role = ctx.get_role(role_id)
    if role["status"] != "active":
        raise RoleInactive(role_id)


def broadcast(ctx, args):
    for role_id in args["recipients"]:
        _require_active(ctx, role_id)
    events = tuple(
        EventSpec(
            role_id,
            "capability.broadcast",
            {
                "message_id": args["message_id"],
                "sender_role_id": ctx.actor_role_id,
            },
        )
        for role_id in args["recipients"]
    )
    return FunctionOutcome({"recipient_count": len(events)}, events)

def contribute(ctx, args):
    target = args["target_role_id"]
    _require_active(ctx, target)
    scope = "aggregate:" + target
    key = "contribution:" + ctx.actor_role_id + ":" + args["contribution_id"]
    if ctx.get_state(scope, key, None) is not None:
        raise RuleViolation("contribution already exists")
    value = {
        "contribution_id": args["contribution_id"],
        "from_role_id": ctx.actor_role_id,
        "value": args["value"],
    }
    ctx.set_state(scope, key, value)
    return FunctionOutcome(
        value,
        (EventSpec(target, "capability.contribution", value),),
    )


def aggregate(ctx, args):
    target = args["target_role_id"]
    if target != ctx.actor_role_id:
        raise PermissionDenied("aggregate is private to its target")
    page = ctx.list_state(
        "aggregate:" + target,
        prefix="contribution:",
        limit=200,
    )
    return FunctionOutcome(
        {"contributions": [item["value"] for item in page["items"]]}
    )

def claim_slot(ctx, args):
    value = {
        "owner_role_id": ctx.actor_role_id,
        "slot_id": args["slot_id"],
    }
    version = ctx.set_state(
        "contest:shared",
        "slot:" + args["slot_id"],
        value,
        expected_version=args["expected_version"],
    )
    return FunctionOutcome({"version": version, **value})


def structured_failure(ctx, args):
    raise RuleViolation(
        "probe capacity reached",
        retryable=True,
        recovery="retry_after_condition",
        retry_after_seconds=1.25,
        details={
            "scope": "capability_probe",
            "limit": 3,
            "used": 3,
            "remaining": 0,
            "reset_condition": "external_signal",
        },
    )


WORLD = WorldDefinition(
    "capability-matrix",
    "Capability Matrix",
    functions=(
        FunctionSpec("topology.broadcast", broadcast, BROADCAST),
        FunctionSpec("topology.contribute", contribute, CONTRIBUTE),
        FunctionSpec("topology.aggregate", aggregate, AGGREGATE, access="read"),
        FunctionSpec("topology.claim_slot", claim_slot, CLAIM_SLOT),
        FunctionSpec("topology.structured_failure", structured_failure, EMPTY),
    ),
    state_rules=(
        StateRule("aggregate:", "contribution:", {"type": "object"}),
        StateRule("contest:", "slot:", {"type": "object"}),
    ),
)
