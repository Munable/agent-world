"""Declarative, server-owned timers. These are not persisted bearer credentials."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import re

from .runtime_contracts import identifier, integer, duration, check_schema
from .runtime_errors import WorldDefinitionError, WorldRuntimeError

MAX_TIMER_COMMANDS = 32
MAX_TIMER_ARGUMENT_BYTES = 65536
MAX_PENDING_TIMERS = 10000
TIMER_ACTOR = "system:timer"


class TimerConflict(WorldRuntimeError):
    pass


class TimerNotFound(WorldRuntimeError):
    pass


class RetryTimer(WorldRuntimeError):
    """Explicit request to retry after rolling back this attempt's world effects."""


@dataclass(frozen=True)
class TimerSpec:
    name: str
    handler: object
    input_schema: dict
    version: int = 1
    authorize: object = None
    output_schema: dict | None = None
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0

    def contract(self) -> dict:
        identifier(self.name, "timer handler")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise WorldDefinitionError("invalid timer handler name")
        integer(self.version, "timer version", 1)
        integer(self.max_attempts, "timer max attempts", 1, 10)
        duration(self.retry_delay_seconds, "timer retry delay", 86400)
        for callback in (self.handler, self.authorize):
            if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback)):
                raise WorldDefinitionError("timer callbacks must be synchronous")
        if self.handler is None:
            raise WorldDefinitionError("timer handler is required")
        check_schema(self.input_schema)
        if self.output_schema is not None:
            check_schema(self.output_schema)
        result = {"name": self.name, "version": self.version, "input_schema": self.input_schema,
                  "max_attempts": self.max_attempts, "retry_delay_seconds": self.retry_delay_seconds}
        if self.output_schema is not None:
            result["output_schema"] = self.output_schema
        return result


@dataclass(frozen=True)
class TimerInvocation:
    timer_id: str
    due_at: float
    scheduled_by: str
    scheduled_operation_id: str | None
    created_at: float
    attempt: int


def public_timer(row) -> dict:
    """Bounded metadata for trusted rule code. Arguments/results are not an automatic public feed."""
    return {key: row[key] for key in (
        "timer_id", "handler", "due_at", "available_at", "status", "attempts",
        "created_at", "created_by", "created_operation_id", "completed_at", "last_error_code",
    )}
