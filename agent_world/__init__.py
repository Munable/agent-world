"""Public SDK for persistent worlds. Rules belong to WorldDefinition packages."""

from .runtime_core import WorldRuntime
from .world_views import ViewSpec
from .world_sdk import WorldDefinition, FunctionSpec, StateRule, FunctionContext, FunctionOutcome, EventSpec

__version__ = "0.10.0"
__all__ = [
    "WorldRuntime",
    "WorldDefinition",
    "FunctionSpec",
    "StateRule",
    "ViewSpec",
    "FunctionContext",
    "FunctionOutcome",
    "EventSpec",
]
