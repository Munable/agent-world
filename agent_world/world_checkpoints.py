"""Current-key scheduled work. Generations replace work, never one-shot identity."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import re

from .runtime_contracts import identifier, integer, duration, check_schema
from .runtime_errors import WorldDefinitionError, WorldRuntimeError

MAX_CHECKPOINT_COMMANDS = 32
MAX_CHECKPOINT_ARGUMENT_BYTES = 65536
CHECKPOINT_ACTOR = "system:checkpoint"


class CheckpointConflict(WorldRuntimeError):
    recovery = "read_checkpoint"


class CheckpointNotFound(WorldRuntimeError):
    pass


class CheckpointReceiptExpired(WorldRuntimeError):
    """An old generation has no retained exact receipt; never permission to replay."""
    recovery = "read_checkpoint"


@dataclass(frozen=True)
class CheckpointSpec:
    name: str
    handler: object
    input_schema: dict
    version: int = 1
    authorize: object = None
    output_schema: dict | None = None
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0

    def contract(self):
        identifier(self.name, "checkpoint handler")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise WorldDefinitionError("invalid checkpoint handler name")
        integer(self.version, "checkpoint version", 1)
        integer(self.max_attempts, "checkpoint max attempts", 1, 10)
        duration(self.retry_delay_seconds, "checkpoint retry delay", 86400)
        for callback in (self.handler, self.authorize):
            if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback)):
                raise WorldDefinitionError("checkpoint callbacks must be synchronous")
        if self.handler is None:
            raise WorldDefinitionError("checkpoint handler is required")
        check_schema(self.input_schema)
        if self.output_schema is not None:
            check_schema(self.output_schema)
        value = {"name": self.name, "version": self.version, "input_schema": self.input_schema,
                 "max_attempts": self.max_attempts, "retry_delay_seconds": self.retry_delay_seconds}
        if self.output_schema is not None:
            value["output_schema"] = self.output_schema
        return value


@dataclass(frozen=True)
class CheckpointInvocation:
    checkpoint_key: str
    generation: int
    due_at: float
    scheduled_by: str
    scheduled_operation_id: str | None
    created_at: float
    attempt: int


def public_checkpoint(row):
    """Trusted current-work inspection, not an automatically public feed."""
    result = {key: row[key] for key in (
        "checkpoint_key", "generation", "completed_through", "latest_completed_generation",
        "latest_commit_seq", "handler", "handler_version", "due_at", "available_at", "status",
        "attempts", "created_at", "created_by", "created_operation_id", "completed_at", "last_error_code",
    )}
    result["arguments"] = json.loads(row["arguments_json"])
    return result
