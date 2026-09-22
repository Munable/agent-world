"""A conformance world. Copied alone into a separate project for wheel acceptance."""
from agent_world import (WorldDefinition, FunctionSpec, StateRule, FunctionOutcome, ViewSpec,
                         TimerSpec, PresentationCue, RetentionPolicy)
from agent_world.errors import RuleViolation

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ID = {"type": "string", "minLength": 1, "maxLength": 64}
MOVE = {"type": "object", "properties": {"move_id": ID, "destination": {"type": "integer", "minimum": 0, "maximum": 10}},
        "required": ["move_id", "destination"], "additionalProperties": False}
SETTLE = {"type": "object", "properties": {"role_id": ID, "move_id": ID},
          "required": ["role_id", "move_id"], "additionalProperties": False}
TEXT = {"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 120},
        "message_id": ID, "channel": {"enum": ["speech", "intent"]}},
        "required": ["text", "message_id", "channel"], "additionalProperties": False}


def move(ctx, args):
    scope = "role:" + ctx.actor_role_id
    state = ctx.get_state(scope, "position", {"position": 0, "active": None})
    if state["active"] is not None:
        raise RuleViolation("this role is already moving")
    state["active"] = {"move_id": args["move_id"], "destination": args["destination"], "started_at": ctx.now}
    ctx.set_state(scope, "position", state)
    ctx.schedule_timer("move:" + ctx.actor_role_id + ":" + args["move_id"], "arrive",
                       {"role_id": ctx.actor_role_id, "move_id": args["move_id"]}, due_at=ctx.now + 1)
    cue = PresentationCue(args["move_id"], ctx.actor_role_id, "action", "start", "move", state["active"])
    return FunctionOutcome({"accepted": True}, (cue.event(ctx.actor_role_id),))


def arrive(ctx, args):
    scope = "role:" + args["role_id"]
    state = ctx.get_state(scope, "position")
    active = state["active"]
    if active is None or active["move_id"] != args["move_id"]:
        return FunctionOutcome({"changed": False})
    state["position"] = active["destination"]
    state["active"] = None
    ctx.set_state(scope, "position", state)
    return FunctionOutcome({"arrived": True}, (PresentationCue(args["move_id"], args["role_id"],
        "action", "finish", "move", {"position": state["position"]}).event(args["role_id"]),))


def cancel(ctx, args):
    scope = "role:" + ctx.actor_role_id
    state = ctx.get_state(scope, "position")
    if state is None or state["active"] is None:
        return FunctionOutcome({"cancelled": False})
    active = state["active"]
    ctx.cancel_timer("move:" + ctx.actor_role_id + ":" + active["move_id"])
    state["active"] = None
    ctx.set_state(scope, "position", state)
    return FunctionOutcome({"cancelled": True}, (PresentationCue(active["move_id"], ctx.actor_role_id,
        "action", "cancel", "move").event(ctx.actor_role_id),))


def express(ctx, args):
    cue = PresentationCue(args["message_id"], ctx.actor_role_id, args["channel"], "start", "public-expression", {"text": args["text"]})
    return FunctionOutcome({"accepted": True}, (cue.event(ctx.actor_role_id),))


def scene(ctx, args):
    state = ctx.get_state("role:" + ctx.actor_role_id, "position", {"position": 0, "active": None})
    return {"entities": {ctx.actor_role_id: {"kind": "character", **state}}, "meta": {}}


WORLD = WorldDefinition("reference-acceptance", "Reference Acceptance",
    functions=(FunctionSpec("character.move", move, MOVE), FunctionSpec("character.cancel", cancel, EMPTY),
               FunctionSpec("character.express", express, TEXT)),
    state_rules=(StateRule("role:", "position", {"type": "object"}, history="metadata"),),
    views=(ViewSpec("scene", scene, timeline=True),), timers=(TimerSpec("arrive", arrive, SETTLE),),
    retention=RetentionPolicy(event_seconds=3600, event_rows=1000, history_seconds=86400, history_rows=5000),
)
