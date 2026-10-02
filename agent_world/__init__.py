"""Public SDK for persistent worlds. Rules belong to WorldDefinition packages."""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING

if _TYPE_CHECKING:
    from .runtime_core import WorldRuntime
    from .world_views import ViewSpec
    from .retention import RetentionPolicy
    from .presentation import PresentationCue
    from .world_streams import StreamSpec, StreamEvent
    from .world_timers import TimerSpec, TimerInvocation, RetryTimer
    from .world_sdk import WorldDefinition, FunctionSpec, StateRule, FunctionContext, FunctionOutcome, EventSpec

# Identity setup needs the client and crypto libraries, not the world engine.
# Resolve the public SDK when used so invoking the connection CLI does not load
# the Runtime's schema validator and storage modules first.
_EXPORT_MODULES = {
    "WorldRuntime": "runtime_core",
    "ViewSpec": "world_views",
    "RetentionPolicy": "retention",
    "PresentationCue": "presentation",
    **dict.fromkeys(("StreamSpec", "StreamEvent"), "world_streams"),
    **dict.fromkeys(("TimerSpec", "TimerInvocation", "RetryTimer"), "world_timers"),
    **dict.fromkeys(("WorldDefinition", "FunctionSpec", "StateRule", "FunctionContext", "FunctionOutcome", "EventSpec"), "world_sdk"),
}


def __getattr__(name):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORT_MODULES))

__version__ = "0.20.0"
__all__ = [
    "WorldRuntime",
    "WorldDefinition",
    "FunctionSpec",
    "StateRule",
    "RetentionPolicy",
    "PresentationCue",
    "StreamSpec",
    "StreamEvent",
    "ViewSpec",
    "TimerSpec",
    "TimerInvocation",
    "RetryTimer",
    "FunctionContext",
    "FunctionOutcome",
    "EventSpec",
]
