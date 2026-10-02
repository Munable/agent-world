"""Exercise public error metadata through the actual Runtime and adapters."""
from __future__ import annotations

from agent_world import FunctionSpec, WorldDefinition
from agent_world.errors import StorageBusy, TimerConflict, TimerNotFound, ViewNotFound, ViewResetRequired
from agent_world.world_streams import StreamResetRequired


ERROR_TYPES = {
    error.__name__: error
    for error in (StreamResetRequired, ViewResetRequired, ViewNotFound, TimerConflict, TimerNotFound, StorageBusy)
}


def fail(ctx, args):
    error_type = ERROR_TYPES[args["error"]]
    raise error_type(
        "public failure context",
        retryable=error_type is not StorageBusy,
        recovery="retry_after_condition",
        retry_after_seconds=1.25,
        details={"scope": "probe", "remaining": 0, "context": [None, True]},
    )


WORLD = WorldDefinition(
    "error-metadata",
    "Error Metadata",
    functions=(FunctionSpec(
        "error.fail",
        fail,
        {
            "type": "object",
            "properties": {"error": {"type": "string", "enum": list(ERROR_TYPES)}},
            "required": ["error"],
            "additionalProperties": False,
        },
        access="read",
    ),),
)
