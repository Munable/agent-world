"""Two small conformance worlds: an expiring RPG effect and a workflow deadline."""
from agent_world import WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, ViewSpec, TimerSpec
from agent_world.errors import RuleViolation

ID = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_-]+$"}
LOOKUP = {"type": "object", "properties": {"id": ID}, "required": ["id"], "additionalProperties": False}
START = {"type": "object", "properties": {"id": ID, "seconds": {"type": "number", "minimum": 0.1, "maximum": 86400}},
         "required": ["id", "seconds"], "additionalProperties": False}


def timed_world(world_id, noun, final_status):
    def owner(ctx, args):
        value = ctx.get_state("objects", args["id"])
        return value is not None and value["owner"] == ctx.actor_role_id

    def start(ctx, args):
        if ctx.get_state("objects", args["id"]) is not None:
            raise RuleViolation("object already exists")
        due_at = ctx.now + args["seconds"]
        tid = "settle:" + args["id"]
        value = {"owner": ctx.actor_role_id, "status": "active", "due_at": due_at, "timer_id": tid}
        ctx.set_state("objects", args["id"], value, expected_version=0)
        ctx.schedule_timer(tid, "settle", {"id": args["id"]}, due_at=due_at)
        return FunctionOutcome({"id": args["id"], **value})

    def settle(ctx, args):
        value = ctx.get_state("objects", args["id"])
        if value is None or value["status"] != "active":
            return FunctionOutcome({"changed": False})
        value["status"] = final_status
        ctx.set_state("objects", args["id"], value)
        return FunctionOutcome({"id": args["id"], "status": final_status})

    def cancel(ctx, args):
        value = ctx.get_state("objects", args["id"])
        if value["status"] == "active":
            ctx.cancel_timer(value["timer_id"])
            value["status"] = "cancelled"
            ctx.set_state("objects", args["id"], value)
        return FunctionOutcome({"id": args["id"], "status": value["status"]})

    def inspect(ctx, args):
        value = ctx.get_state("objects", args["id"])
        return FunctionOutcome({"object": value, "timer": ctx.get_timer(value["timer_id"])})

    def project(ctx, args):
        value = ctx.get_state("objects", args["id"])
        return {"entities": {args["id"]: {"kind": noun, **value}}, "meta": {}}

    return WorldDefinition(
        world_id, noun.title() + " Timer Example",
        (FunctionSpec(noun + ".start", start, START),
         FunctionSpec(noun + ".cancel", cancel, LOOKUP, authorize=owner),
         FunctionSpec(noun + ".inspect", inspect, LOOKUP, access="read", authorize=owner)),
        state_rules=(StateRule("objects", "", {"type": "object", "properties": {
            "owner": {"type": "string"}, "status": {"enum": ["active", "cancelled", final_status]},
            "due_at": {"type": "number"}, "timer_id": {"type": "string"}},
            "required": ["owner", "status", "due_at", "timer_id"], "additionalProperties": False}),),
        timers=(TimerSpec("settle", settle, LOOKUP),),
        views=(ViewSpec("object", project, LOOKUP, authorize=owner),),
        entry_instructions="Create an object with a lifetime. The server applies its deadline even after you disconnect.",
    )


RPG_WORLD = timed_world("timed-rpg-example", "effect", "expired")
WORKFLOW_WORLD = timed_world("timed-workflow-example", "request", "closed")
