"""A non-game world using the same portable SDK and transaction guarantees."""

from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome, EventSpec
from agent_world.errors import RuleViolation

ARGS = {
    "type": "object",
    "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 80}},
    "required": ["task_id"],
    "additionalProperties": False,
}


def claim(ctx, arguments):
    task = ctx.get_state_record("tasks", arguments["task_id"])
    if task["exists"]:
        raise RuleViolation("task has already been claimed")
    ctx.set_state(
        "tasks",
        arguments["task_id"],
        {"owner": ctx.actor_role_id, "status": "claimed"},
        expected_version=task["version"],
    )
    return FunctionOutcome(
        {"task_id": arguments["task_id"], "owner": ctx.actor_role_id},
        (EventSpec(ctx.actor_role_id, "task_claimed", arguments),),
    )


def owned(ctx, arguments):
    task = ctx.get_state("tasks", arguments["task_id"])
    return task is not None and task["owner"] == ctx.actor_role_id


def complete(ctx, arguments):
    task = ctx.get_state("tasks", arguments["task_id"])
    task["status"] = "completed"
    ctx.set_state("tasks", arguments["task_id"], task)
    return FunctionOutcome({"task_id": arguments["task_id"], "status": "completed"})


WORLD = WorldDefinition(
    "workflow-example",
    "Workflow Example",
    (
        FunctionSpec("task.claim", claim, ARGS, description="Claim an unclaimed task."),
        FunctionSpec(
            "task.complete",
            complete,
            ARGS,
            authorize=owned,
            description="Complete a task owned by this role.",
        ),
    ),
    state_rules=(
        StateRule(
            "tasks",
            "",
            {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "status": {"type": "string", "enum": ["claimed", "completed"]},
                },
                "required": ["owner", "status"],
                "additionalProperties": False,
            },
        ),
    ),
)
