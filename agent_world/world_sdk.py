from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Callable

from .runtime_contracts import (
    SDK_API_VERSION,
    identifier,
    integer,
    json_text,
    check_schema,
    validate,
    MAX_STATE_BYTES,
)
from .runtime_errors import WorldDefinitionError, WorldVersionMismatch, SchemaRejected
from .world_context import FunctionContext
from .world_types import EventSpec, FunctionOutcome
from .world_views import ViewSpec
from .world_timers import TimerSpec


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    handler: Callable
    input_schema: dict
    description: str = ""
    access: str = "write"
    version: int = 1
    output_schema: dict | None = None
    requires_activity_claim: bool = False
    authorize: Callable | None = None
    visible_to: Callable | None = None

    def contract(self):
        value = {
            "name": self.name,
            "version": self.version,
            "input_schema": self.input_schema,
            "description": self.description,
            "access": self.access,
            "requires_activity_claim": self.requires_activity_claim,
        }
        if self.output_schema is not None:
            value["output_schema"] = self.output_schema
        return value


@dataclass(frozen=True)
class StateRule:
    scope_prefix: str
    key_prefix: str
    schema: dict


@dataclass(frozen=True)
class WorldDefinition:
    world_id: str
    display_name: str
    functions: tuple[FunctionSpec, ...]
    version: int = 1
    state_version: int = 1
    api_version: int = SDK_API_VERSION
    state_rules: tuple[StateRule, ...] = ()
    strict_state: bool = True
    state_authorizer: Callable | None = None
    bootstrap: Callable | None = None
    initialize: Callable | None = None
    migrations: dict[int, Callable] = field(default_factory=dict)
    entry_instructions: str = "Discover the world functions, then inspect the current state before acting."
    views: tuple[ViewSpec, ...] = ()
    timers: tuple[TimerSpec, ...] = ()

    def manifest(self):
        identifier(self.world_id, "world_id")
        identifier(self.display_name, "world name", 128)
        integer(self.version, "world version", 1)
        integer(self.state_version, "state version", 1)
        if self.api_version != SDK_API_VERSION:
            raise WorldDefinitionError("world requires an unsupported SDK API version")
        if len(self.views) > 64 or any(not isinstance(v, ViewSpec) for v in self.views):
            raise WorldDefinitionError("world must declare at most 64 ViewSpec values")
        if len({v.name for v in self.views}) != len(self.views):
            raise WorldDefinitionError("duplicate view names")
        if len(self.timers) > 64 or any(not isinstance(t, TimerSpec) for t in self.timers):
            raise WorldDefinitionError("world must declare at most 64 TimerSpec values")
        if len({t.name for t in self.timers}) != len(self.timers):
            raise WorldDefinitionError("duplicate timer handler names")
        names = [f.name for f in self.functions]
        if len(names) != len(set(names)):
            raise WorldDefinitionError("duplicate function names in world definition")
        for rule in self.state_rules:
            if not isinstance(rule.scope_prefix, str) or not isinstance(rule.key_prefix, str):
                raise WorldDefinitionError("state prefixes must be strings")
            check_schema(rule.schema, object_root=False)
        if not isinstance(self.entry_instructions, str) or len(self.entry_instructions) > 4000:
            raise WorldDefinitionError("invalid world entry instructions")
        value = {
            "world_id": self.world_id,
            "display_name": self.display_name,
            "version": self.version,
            "state_version": self.state_version,
            "api_version": self.api_version,
            "strict_state": self.strict_state,
            "functions": [f.contract() for f in self.functions],
            "state_rules": [
                {"scope_prefix": r.scope_prefix, "key_prefix": r.key_prefix, "schema": r.schema}
                for r in self.state_rules
            ],
            "entry_instructions": self.entry_instructions,
        }
        if self.views:
            value["views"] = [v.contract() for v in self.views]
        if self.timers:
            value["timers"] = [t.contract() for t in self.timers]
        json_text(value)
        return value

    def validate_state(self, ctx, scope, key, value):
        matching = [
            r for r in self.state_rules if scope.startswith(r.scope_prefix) and key.startswith(r.key_prefix)
        ]
        if self.strict_state and not matching:
            raise SchemaRejected("state key is not declared by this world")
        for rule in matching:
            validate(rule.schema, value, maximum=MAX_STATE_BYTES, object_root=False)


def install_world(runtime, universe: str, definition: WorldDefinition) -> None:
    with runtime._lock:
        return _install_world_locked(runtime, universe, definition)


def _install_world_locked(runtime, universe: str, definition: WorldDefinition) -> None:
    if not isinstance(definition, WorldDefinition):
        raise WorldDefinitionError("world factory must return WorldDefinition")
    identifier(universe, "universe")
    manifest = definition.manifest()
    encoded = json_text(manifest)
    with runtime._lock, runtime._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM world_definitions WHERE universe=?", (universe,)).fetchone()
        if old is not None:
            if old["world_id"] != definition.world_id:
                raise WorldDefinitionError("a universe cannot silently change its world package")
            if definition.version < old["version"] or definition.state_version < old["state_version"]:
                raise WorldVersionMismatch("world or state schema downgrade is not allowed")
            if definition.version == old["version"] and old["manifest_json"] != encoded:
                raise WorldDefinitionError("world contract changed without a world version bump")
        else:
            existing = {
                row[0]
                for row in c.execute(
                    "SELECT function_id FROM function_registry WHERE universe=?", (universe,)
                )
            }
            if existing and existing != {f.name for f in definition.functions}:
                raise WorldDefinitionError("unmanaged functions already occupy this universe")
        journal_marker = runtime._journal_marker_tx(c)
        # Migrations use only the scoped storage API and run in one transaction.
        migration_ctx = FunctionContext(
            c, universe, "system:installer", "world.migrate", definition.version, access="write",
            timers_enabled=True
        )
        if old is None:
            if definition.initialize is not None:
                runtime._run_user_code(c, definition.initialize, migration_ctx)
        else:
            for target in range(old["state_version"] + 1, definition.state_version + 1):
                migration = definition.migrations.get(target)
                if migration is None:
                    raise WorldDefinitionError(f"missing migration to state version {target}")
                runtime._run_user_code(c, migration, migration_ctx)
        for row in c.execute(
            "SELECT scope,state_key,value_json FROM world_state WHERE universe=? AND deleted=0", (universe,)
        ):
            definition.validate_state(
                migration_ctx, row["scope"], row["state_key"], json.loads(row["value_json"])
            )
        for spec in definition.functions:
            runtime._register_function_tx(
                c,
                universe,
                spec.name,
                spec.version,
                spec.description,
                spec.input_schema,
                spec.handler,
                access=spec.access,
                requires_activity_claim=spec.requires_activity_claim,
                output_schema=spec.output_schema,
                authorize=spec.authorize,
                visible_to=spec.visible_to,
            )
        names = {f.name for f in definition.functions}
        for row in c.execute(
            "SELECT function_id FROM function_registry WHERE universe=?", (universe,)
        ).fetchall():
            if row["function_id"] not in names:
                c.execute(
                    "DELETE FROM function_registry WHERE universe=? AND function_id=?",
                    (universe, row["function_id"]),
                )
        c.execute(
            "INSERT INTO world_definitions(universe,world_id,version,manifest_json,state_version) VALUES(?,?,?,?,?) "
            "ON CONFLICT(universe) DO UPDATE SET version=excluded.version,manifest_json=excluded.manifest_json,state_version=excluded.state_version",
            (universe, definition.world_id, definition.version, encoded, definition.state_version),
        )
        transitions = runtime._apply_timer_commands_tx(c, migration_ctx, definition)
        if old is None or old["manifest_json"] != encoded:
            commit_seq = runtime._record_commit_tx(
                c, universe, journal_marker, actor_role_id="system:installer",
                function_id="world.install", source="migration" if old else "initialize",
                world_version=definition.version,
            )
            runtime._bind_timer_transitions_tx(c, transitions, commit_seq)
    # Publish handlers only after the entire migration and registry transaction commits.
    runtime._handlers = {k: v for k, v in runtime._handlers.items() if k[0] != universe}
    runtime._function_options = {k: v for k, v in runtime._function_options.items() if k[0] != universe}
    for spec in definition.functions:
        key = (universe, spec.name, spec.version)
        runtime._handlers[key] = spec.handler
        runtime._function_options[key] = {"authorize": spec.authorize, "visible_to": spec.visible_to}
    runtime._worlds[universe] = definition


def definition_from_installer(
    installer,
    world_id,
    display_name,
    *,
    state_rules=(),
    strict_state=True,
    bootstrap=None,
    entry_instructions=None,
):
    # Bridge existing register_function installers without making them own the Runtime.
    functions = []

    class Collector:
        def register_function(
            self, universe, function_id, version, description, input_schema, handler, **options
        ):
            options.pop("availability", None)
            functions.append(
                FunctionSpec(
                    function_id, handler, input_schema, description=description, version=version, **options
                )
            )

    installer(Collector(), world_id)
    kwargs = {} if entry_instructions is None else {"entry_instructions": entry_instructions}
    return WorldDefinition(
        world_id,
        display_name,
        tuple(functions),
        state_rules=tuple(state_rules),
        strict_state=strict_state,
        bootstrap=bootstrap,
        **kwargs,
    )


__all__ = [
    "WorldDefinition",
    "FunctionSpec",
    "StateRule",
    "ViewSpec",
    "TimerSpec",
    "FunctionContext",
    "FunctionOutcome",
    "EventSpec",
    "install_world",
    "definition_from_installer",
]
