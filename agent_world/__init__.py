"""Public SDK for persistent worlds. Rules belong to WorldDefinition packages."""

from .runtime_core import WorldRuntime
from .world_views import ViewSpec
from .retention import RetentionPolicy
from .presentation import PresentationCue
from .world_timers import TimerSpec, TimerInvocation, RetryTimer
from .world_sdk import WorldDefinition, FunctionSpec, StateRule, FunctionContext, FunctionOutcome, EventSpec

__version__ = "0.12.0"
__all__ = [
    "WorldRuntime",
    "WorldDefinition",
    "FunctionSpec",
    "StateRule",
    "RetentionPolicy",
    "PresentationCue",
    "ViewSpec",
    "TimerSpec",
    "TimerInvocation",
    "RetryTimer",
    "FunctionContext",
    "FunctionOutcome",
    "EventSpec",
]
