from __future__ import annotations

import importlib
import re
from typing import Callable

from .world_sdk import WorldDefinition, install_world
from .runtime_errors import WorldDefinitionError


def get_world_definition(profile: str) -> WorldDefinition:
    if not isinstance(profile, str):
        raise WorldDefinitionError("world profile must be a string")
    profile = profile.strip()
    if ":" not in profile:
        from .builtin_worlds import get_builtin_definition

        return get_builtin_definition(profile.lower())
    # This value is trusted server configuration, NEVER an Agent/tool argument.
    module, symbol = profile.split(":", 1)
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) or not symbol.isidentifier():
        raise WorldDefinitionError("expected an installed Python module:object")
    value = getattr(importlib.import_module(module), symbol)
    definition = value() if callable(value) and not isinstance(value, WorldDefinition) else value
    if not isinstance(definition, WorldDefinition):
        raise WorldDefinitionError("external world must be a WorldDefinition or zero-argument factory")
    definition.manifest()
    return definition


def get_universe_installer(profile: str) -> Callable:
    definition = get_world_definition(profile)

    def installer(runtime, universe):
        install_world(runtime, universe, definition)

    return installer
