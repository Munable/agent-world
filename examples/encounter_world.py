"""A small rule-adaptation example, not an implementation of the D&D ruleset."""

from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome, EventSpec
from agent_world.errors import RuleViolation

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ID = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[a-zA-Z0-9_-]+$"}
ENCOUNTER_ARGS = {
    "type": "object",
    "properties": {"encounter_id": ID},
    "required": ["encounter_id"],
    "additionalProperties": False,
}
STATE = {
    "type": "object",
    "properties": {
        "members": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 6,
            "uniqueItems": True,
        },
        "hp": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 100}},
        "turn": {"type": "integer", "minimum": 0},
        "round": {"type": "integer", "minimum": 1},
    },
    "required": ["members", "hp", "turn", "round"],
    "additionalProperties": False,
}


def scope(encounter_id):
    return "encounter:" + encounter_id


def participant(ctx, arguments):
    state = ctx.get_state(scope(arguments["encounter_id"]), "state")
    return state is not None and ctx.actor_role_id in state["members"]


def start(ctx, arguments):
    key = scope(arguments["encounter_id"])
    if ctx.get_state(key, "state") is not None:
        raise RuleViolation("encounter already exists")
    members = arguments["members"]
    if ctx.actor_role_id not in members:
        raise RuleViolation("creator must participate")
    for member in members:
        if ctx.get_role(member)["status"] != "active":
            raise RuleViolation("participant is inactive")
    state = {"members": members, "hp": {member: 20 for member in members}, "turn": 0, "round": 1}
    ctx.set_state(key, "state", state, expected_version=0)
    for member in members:
        ctx.set_state("role:" + member, "encounter", arguments["encounter_id"])
    return FunctionOutcome(
        {"encounter_id": arguments["encounter_id"], "state": state},
        tuple(
            EventSpec(member, "encounter_started", {"encounter_id": arguments["encounter_id"]})
            for member in members
        ),
    )


def inspect(ctx, arguments):
    record = ctx.get_state_record(scope(arguments["encounter_id"]), "state")
    return FunctionOutcome({"state": record["value"], "state_version": record["version"]})


def strike(ctx, arguments):
    key = scope(arguments["encounter_id"])
    record = ctx.get_state_record(key, "state")
    state = record["value"]
    if state["members"][state["turn"]] != ctx.actor_role_id or state["hp"][ctx.actor_role_id] == 0:
        raise RuleViolation("not this role's active turn")
    target = arguments["target_role_id"]
    if target == ctx.actor_role_id or target not in state["members"] or state["hp"][target] == 0:
        raise RuleViolation("invalid target")
    # Randomness is server-generated and recorded in the committed operation receipt.
    roll = ctx.random_int(1, 20)
    damage = ctx.random_int(1, 6) if roll >= 10 else 0
    state["hp"][target] = max(0, state["hp"][target] - damage)
    state["turn"] = (state["turn"] + 1) % len(state["members"])
    if state["turn"] == 0:
        state["round"] += 1
    version = ctx.set_state(key, "state", state, expected_version=record["version"])
    result = {"roll": roll, "damage": damage, "target_hp": state["hp"][target], "state_version": version}
    return FunctionOutcome(
        result, tuple(EventSpec(member, "encounter_strike", result) for member in state["members"])
    )


def bootstrap(ctx):
    encounter = ctx.get_state("role:" + ctx.actor_role_id, "encounter")
    if encounter is None:
        return {"encounter_id": None}
    state = ctx.get_state(scope(encounter), "state")
    if state is None or ctx.actor_role_id not in state["members"]:
        return {"encounter_id": None}
    return {"encounter_id": encounter, "state": state}


WORLD = WorldDefinition(
    world_id="encounter-example",
    display_name="Encounter Rules Example",
    functions=(
        FunctionSpec(
            "encounter.start",
            start,
            {
                "type": "object",
                "properties": {"encounter_id": ID, "members": STATE["properties"]["members"]},
                "required": ["encounter_id", "members"],
                "additionalProperties": False,
            },
            description="Start an encounter with existing roles.",
        ),
        FunctionSpec(
            "encounter.inspect",
            inspect,
            ENCOUNTER_ARGS,
            access="read",
            authorize=participant,
            description="Read an encounter only as a participant.",
        ),
        FunctionSpec(
            "encounter.strike",
            strike,
            {
                "type": "object",
                "properties": {"encounter_id": ID, "target_role_id": {"type": "string", "maxLength": 128}},
                "required": ["encounter_id", "target_role_id"],
                "additionalProperties": False,
            },
            authorize=participant,
            description="Resolve one server-rolled action on the actor's turn.",
            output_schema={
                "type": "object",
                "properties": {
                    "roll": {"type": "integer", "minimum": 1, "maximum": 20},
                    "damage": {"type": "integer", "minimum": 0, "maximum": 6},
                    "target_hp": {"type": "integer", "minimum": 0},
                    "state_version": {"type": "integer", "minimum": 1},
                },
                "required": ["roll", "damage", "target_hp", "state_version"],
                "additionalProperties": False,
            },
        ),
    ),
    state_rules=(StateRule("encounter:", "state", STATE), StateRule("role:", "encounter", ID)),
    bootstrap=bootstrap,
    entry_instructions="Inspect your current encounter. Only act on your own turn. Reuse operation_id when retrying a strike, never to roll again.",
)
