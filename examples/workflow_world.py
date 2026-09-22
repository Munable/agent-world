"""A non-game world using the same portable SDK and transaction guarantees."""

from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome, EventSpec, ViewSpec
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


def tasks_view(ctx, arguments):
    # Detail selection keeps other owners' keys and pagination metadata private.
    task = ctx.get_state("tasks", arguments["task_id"])
    entities = {}
    if task is not None and task["owner"] == ctx.actor_role_id:
        entities[arguments["task_id"]] = {"kind": "task", **task}
    return {"entities": entities, "meta": {}}


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
    version=2,
    views=(ViewSpec("tasks", tasks_view, ARGS, renderer="task-detail"),),
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
