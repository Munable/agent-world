from __future__ import annotations

import inspect
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from types import MappingProxyType

from .runtime_contracts import (
    identifier,
    integer,
    json_text,
    arguments_object,
    check_schema,
    validate,
    CORE_TOOL_NAMES,
    MAX_RESULT_BYTES,
    MAX_EVENTS,
)
from .runtime_errors import (
    WorldRuntimeError,
    RegistryConflict,
    FunctionNotFound,
    FunctionVersionMismatch,
    OperationConflict,
    ClaimFenced,
    AccessModeMismatch,
    PermissionDenied,
    ReceiptNotFound,
    ResultRejected,
    WorldVersionMismatch,
    WorldDefinitionError,
    InvalidArguments,
    SchemaRejected,
)
from .world_context import FunctionContext
from .runtime_journal import JOURNAL_TRIGGERS, MAX_COMMIT_CHANGES
from .world_types import FunctionOutcome, EventSpec
from .world_streams import StreamEvent
from .presentation import PRESENTATION_KIND, validate_cue


@dataclass(frozen=True)
class _ReadRegistry:
    definition: object
    handlers: object
    options: object


_EMPTY_READ_REGISTRY = _ReadRegistry(None, MappingProxyType({}), MappingProxyType({}))


class RuntimeFunctions:
    def _capture_registry(self, universe):
        """Borrow one published code/policy generation without the writer lock."""
        with self._registry_lock:
            return self._read_registries.get(universe, _EMPTY_READ_REGISTRY)

    def _begin_registry_read(self, c, universe, registry):
        """Fix the WAL snapshot before accepting this published code generation.

        Registry changes hold their own lock through SQL commit and publication.
        The short fence therefore also covers the commit-to-publication window.
        After this point a reader retains its old complete snapshot, even when a
        later installation publishes new callbacks with the same contract version.
        """
        c.execute("BEGIN")
        c.execute("SELECT world_id FROM world_definitions WHERE universe=?", (universe,)).fetchone()
        with self._registry_lock:
            if self._read_registries.get(universe, _EMPTY_READ_REGISTRY) is not registry:
                raise WorldVersionMismatch("world code changed while starting the read snapshot; retry with its published package")

    def _publish_registry(self, universe, *, definition=None, specs=None, function=None):
        """Publish after SQL commit; readers retain the previous complete bundle.

        Callers hold the writer lock and registry fence from SQL commit through
        publication. Ordinary world writes and user reads do not hold that
        fence for their handlers or durable commits.
        """
        with self._registry_lock:
            if specs is not None:
                handlers = {k: v for k, v in self._handlers.items() if k[0] != universe}
                options = {k: v for k, v in self._function_options.items() if k[0] != universe}
                for spec in specs:
                    key = (universe, spec.name, spec.version)
                    handlers[key] = spec.handler
                    options[key] = {"authorize": spec.authorize, "visible_to": spec.visible_to}
                self._handlers, self._function_options = handlers, options
                self._worlds = {**self._worlds, universe: definition}
            else:
                key, handler, policies = function
                self._handlers = {**self._handlers, key: handler}
                self._function_options = {**self._function_options, key: policies}
            snapshot = _ReadRegistry(self._worlds.get(universe),
                MappingProxyType({k: v for k, v in self._handlers.items() if k[0] == universe}),
                MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self._function_options.items() if k[0] == universe}))
            self._read_registries = {**self._read_registries, universe: snapshot}

    def _register_function_tx(
        self,
        c,
        universe,
        function_id,
        version,
        description,
        input_schema,
        handler,
        *,
        access="write",
        availability="available",
        requires_activity_claim=False,
        output_schema=None,
        authorize=None,
        visible_to=None,
    ):
        identifier(universe, "universe")
        identifier(function_id, "function_id")
        integer(version, "version", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", function_id) or function_id in CORE_TOOL_NAMES:
            raise RegistryConflict("invalid or reserved function_id")
        if not callable(handler) or inspect.iscoroutinefunction(handler):
            raise RegistryConflict("world handlers must be synchronous callables")
        if access not in {"read", "write"} or availability not in {"available", "unavailable"}:
            raise RegistryConflict("invalid function access or availability")
        if access == "read" and requires_activity_claim:
            raise RegistryConflict("read functions use authorization policies, not mutation claims")
        if len(description) > 2000:
            raise RegistryConflict("function description is too long")
        check_schema(input_schema)
        if output_schema is not None:
            check_schema(output_schema)
        desc = {
            "function_id": function_id,
            "version": version,
            "description": description,
            "input_schema": input_schema,
            "access": access,
            "availability": availability,
            "requires_activity_claim": bool(requires_activity_claim),
        }
        if output_schema is not None:
            desc["output_schema"] = output_schema
        digest = self._sha256(desc)
        old = c.execute(
            "SELECT * FROM function_registry WHERE universe=? AND function_id=?", (universe, function_id)
        ).fetchone()
        if old:
            if version < int(old["version"]):
                raise RegistryConflict("cannot register an older function version")
            if version == int(old["version"]) and old["descriptor_hash"] != digest:
                raise RegistryConflict("descriptor changed without a version bump")
            if old["access"] != access:
                raise RegistryConflict("read/write mode is immutable; use a new function_id")
        c.execute(
            "INSERT INTO function_registry(universe,function_id,version,description,input_schema_json,"
            "access,availability,requires_activity_claim,descriptor_hash,updated_at,output_schema_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(universe,function_id) DO UPDATE SET "
            "version=excluded.version,description=excluded.description,input_schema_json=excluded.input_schema_json,"
            "access=excluded.access,availability=excluded.availability,requires_activity_claim=excluded.requires_activity_claim,"
            "descriptor_hash=excluded.descriptor_hash,updated_at=excluded.updated_at,output_schema_json=excluded.output_schema_json",
            (
                universe,
                function_id,
                version,
                description,
                self._canonical(input_schema),
                access,
                availability,
                int(bool(requires_activity_claim)),
                digest,
                time.time(),
                self._canonical(output_schema) if output_schema is not None else None,
            ),
        )
        return desc

    def register_function(
        self,
        universe,
        function_id,
        version,
        description,
        input_schema,
        handler,
        *,
        access="write",
        availability="available",
        requires_activity_claim=False,
        output_schema=None,
        authorize=None,
        visible_to=None,
    ):
        with self._lock, self._registry_lock:
            with self._conn(serialized=True) as c:
                c.execute("BEGIN IMMEDIATE")
                if c.execute("SELECT 1 FROM world_definitions WHERE universe=?", (universe,)).fetchone():
                    raise WorldDefinitionError(
                        "use install_world with a versioned definition to change this universe"
                    )
                desc = self._register_function_tx(
                    c,
                    universe,
                    function_id,
                    version,
                    description,
                    input_schema,
                    handler,
                    access=access,
                    availability=availability,
                    requires_activity_claim=requires_activity_claim,
                    output_schema=output_schema,
                    authorize=authorize,
                    visible_to=visible_to,
                )
            key = (universe, function_id, version)
            self._publish_registry(universe, function=(key, handler,
                {"authorize": authorize, "visible_to": visible_to}))
        return desc

    def _row_to_descriptor(self, row):
        desc = {
            "function_id": row["function_id"],
            "version": int(row["version"]),
            "description": row["description"],
            "input_schema": json.loads(row["input_schema_json"]),
            "access": row["access"],
            "availability": row["availability"],
            "requires_activity_claim": bool(row["requires_activity_claim"]),
        }
        if row["output_schema_json"] is not None:
            desc["output_schema"] = json.loads(row["output_schema_json"])
        return desc

    def _check_world_tx(self, c, universe, *, registry=None):
        definition = registry.definition if registry is not None else self._worlds.get(universe)
        if definition is not None or registry is not None:
            row = c.execute(
                "SELECT world_id,version FROM world_definitions WHERE universe=?", (universe,)
            ).fetchone()
            if definition is None:
                if row is not None:
                    raise WorldVersionMismatch("loaded function code predates the installed world; rediscover with its published package")
                return
            if row is None or row["world_id"] != definition.world_id or row["version"] != definition.version:
                raise WorldVersionMismatch(
                    "loaded world code does not match persisted world version; restart with the matching package"
                )

    def _context_tx(
        self,
        c,
        universe,
        role_id,
        function_id,
        version,
        *,
        access="read",
        activity_id=None,
        operation_id=None,
        now=None,
        registry=None,
    ):
        definition = registry.definition if registry is not None else self._worlds.get(universe)
        ctx = FunctionContext(
            c,
            universe,
            role_id,
            function_id,
            version,
            activity_id=activity_id,
            access=access,
            operation_id=operation_id,
            now=now,
            state_validator=definition.validate_state if definition else None,
            state_authorizer=definition.state_authorizer if definition else None,
            state_forget_authorizer=definition.state_forget_authorizer if definition else None,
            timers_enabled=definition is not None,
            checkpoints_enabled=definition is not None,
            checkpoint_specs=definition.checkpoints if definition else (),
        )
        if registry is not None:
            ctx._function_policies = registry.options.get((universe, function_id, version), {})
        return ctx

    def _run_user_code(self, c, callback, *args, read_only=False):
        denied_access = False

        def guard(action, arg1, arg2, database, trigger):
            nonlocal denied_access
            if action in {
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_SAVEPOINT,
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_PRAGMA,
            }:
                denied_access = True
                return sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                if (not read_only and action == sqlite3.SQLITE_INSERT
                        and arg1 == "state_changes" and trigger in JOURNAL_TRIGGERS):
                    return sqlite3.SQLITE_OK
                if read_only or arg1 != "world_state":
                    denied_access = True
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DELETE and not any(
                        getattr(arg, "_forgetting_state", False) for arg in args if isinstance(arg, FunctionContext)):
                    denied_access = True
                    return sqlite3.SQLITE_DENY
            if action not in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_RECURSIVE,
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            }:
                denied_access = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        contexts = [arg for arg in args if isinstance(arg, FunctionContext)]
        old_guards = [getattr(ctx, "_sql_authorizer", None) for ctx in contexts]
        for ctx in contexts:
            ctx._sql_authorizer = guard
        c.set_authorizer(guard)
        try:
            return callback(*args)
        except sqlite3.DatabaseError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            primary = code & 255 if isinstance(code, int) else None
            if primary == sqlite3.SQLITE_AUTH or (denied_access and primary in {None, sqlite3.SQLITE_ERROR}):
                raise WorldRuntimeError(
                    "world handler attempted a disallowed database write or transaction control"
                ) from exc
            raise
        finally:
            for ctx, previous in zip(contexts, old_guards):
                ctx._sql_authorizer = previous
            c.set_authorizer(old_guards[0] if old_guards else None)

    def _validate_managed_state_changes_tx(
        self, c, ctx, marker, *, definition=None, enforce_authorizer=True, validate_values=True
    ):
        """Revalidate any direct internal state writes before a managed-world commit."""
        definition = definition or self._worlds.get(ctx.universe)
        if definition is None:
            return
        approved = list(ctx._approved_state_changes)
        rows = c.execute(
            "SELECT universe,scope,state_key,kind,before_version,value_json,version,deleted "
            "FROM state_changes WHERE seq>? ORDER BY seq LIMIT ?",
            (marker, MAX_COMMIT_CHANGES + 1),
        ).fetchall()
        if len(rows) > MAX_COMMIT_CHANGES:
            raise InvalidArguments("world transaction exceeds its state change budget")
        for row in rows:
            if row["universe"] != ctx.universe:
                raise PermissionDenied("a world transaction cannot change another universe")
            scope = identifier(row["scope"], "scope", 256)
            key = identifier(row["state_key"], "state key", 256)
            before_version = integer(row["before_version"], "previous state version", 0)
            version = integer(row["version"], "state version", 1)
            if version != before_version + 1:
                raise SchemaRejected("managed world state versions must advance exactly once per change")
            deleted = row["deleted"]
            if type(deleted) is not int or deleted not in {0, 1}:
                raise SchemaRejected("managed world state deletion marker is invalid")
            encoded = row["value_json"]
            if not isinstance(encoded, str):
                raise SchemaRejected("managed world state must contain JSON text")
            try:
                value = json.loads(encoded)
            except (TypeError, ValueError) as exc:
                raise SchemaRejected("managed world state contains invalid JSON") from exc
            if deleted:
                if value is not None:
                    raise SchemaRejected("deleted managed world state must store null")
            elif validate_values:
                definition.validate_state(ctx, scope, key, value)
            signature = (scope, key, version, deleted, encoded)
            if signature in approved:
                approved.remove(signature)
                continue
            policy = definition.state_authorizer if enforce_authorizer else None
            if policy is not None:
                previous = ctx._state_authorizer
                previous_authorizing = ctx._authorizing_state
                previous_access = ctx.access
                ctx.access = "read"
                ctx._state_authorizer = None
                ctx._authorizing_state = True
                try:
                    allowed = self._run_user_code(c, policy, ctx, scope, key, "write", read_only=True)
                finally:
                    ctx._state_authorizer = previous
                    ctx._authorizing_state = previous_authorizing
                    ctx.access = previous_access
                if allowed is not True:
                    raise PermissionDenied("world state access denied")

    def _authorize_function_tx(self, c, ctx, arguments):
        options = getattr(ctx, "_function_policies", None)
        if options is None:
            options = self._function_options.get((ctx.universe, ctx.function_id, ctx.function_version), {})
        policy = options.get("authorize")
        if policy is not None:
            old_access = ctx.access
            ctx.access = "read"
            try:
                allowed = self._run_user_code(c, policy, ctx, json.loads(json_text(arguments)), read_only=True)
            finally:
                ctx.access = old_access
            if allowed is not True:
                raise PermissionDenied("world function access denied")

    def list_functions(self, universe, *, actor_role_id=None, identity_token=None):
        identifier(universe, "universe")
        registry = self._capture_registry(universe)
        with self._conn(readonly=True) as c:
            self._begin_registry_read(c, universe, registry)
            self._check_world_tx(c, universe, registry=registry)
            identity = None
            if actor_role_id is not None:
                identity = self._admit_actor_tx(c, universe, actor_role_id, identity_token)
            catalog = self._catalog_tx(c, universe, actor_role_id, registry=registry)
            return [d for d in catalog if d["access"] == "read"] if identity and identity["access_mode"] == "observe" else catalog

    def _catalog_tx(self, c, universe, actor_role_id, *, registry=None):
        handlers = registry.handlers if registry is not None else self._handlers
        policies = registry.options if registry is not None else self._function_options
        rows = c.execute(
            "SELECT * FROM function_registry WHERE universe=? ORDER BY function_id", (universe,)
        ).fetchall()
        result = []
        for row in rows:
            key = (universe, row["function_id"], int(row["version"]))
            if key not in handlers or row["availability"] != "available":
                continue
            visible = policies.get(key, {}).get("visible_to")
            if actor_role_id is not None and visible is not None:
                ctx = self._context_tx(c, universe, actor_role_id, key[1], key[2], registry=registry)
                if self._run_user_code(c, visible, ctx, read_only=True) is not True:
                    continue
            result.append(self._row_to_descriptor(row))
        return result

    def discover_functions(
        self,
        universe,
        *,
        actor_role_id=None,
        identity_token=None,
        prefix="",
        after="",
        limit=50,
        include_schemas=False,
    ):
        integer(limit, "limit", 1, 200)
        if (
            not isinstance(prefix, str)
            or not isinstance(after, str)
            or len(prefix) > 128
            or len(after) > 128
            or type(include_schemas) is not bool
        ):
            raise InvalidArguments("invalid discovery parameters")
        rows = [
            d
            for d in self.list_functions(universe, actor_role_id=actor_role_id, identity_token=identity_token)
            if d["function_id"].startswith(prefix) and d["function_id"] > after
        ]
        more = len(rows) > limit
        page = rows[:limit]
        if not include_schemas:
            page = [{k: d[k] for k in ("function_id", "version", "description", "access")} for d in page]
        return {"functions": page, "has_more": more, "next_cursor": page[-1]["function_id"] if more else None}

    def get_function(self, universe, function_id):
        identifier(universe, "universe")
        identifier(function_id, "function_id")
        with self._conn(readonly=True) as c:
            row = c.execute(
                "SELECT * FROM function_registry WHERE universe=? AND function_id=?", (universe, function_id)
            ).fetchone()
        if row is None:
            raise FunctionNotFound(f"{universe}/{function_id}")
        return self._row_to_descriptor(row)

    def _validate_outcome(self, outcome, desc, *, read_only=False):
        if inspect.isawaitable(outcome):
            if inspect.iscoroutine(outcome):
                outcome.close()
            raise ResultRejected("world handlers must return synchronously")
        if not isinstance(outcome, FunctionOutcome) or not isinstance(outcome.result, dict):
            raise ResultRejected("handler must return FunctionOutcome with an object result")
        try:
            json_text(outcome.result)
            if desc.get("output_schema") is not None:
                validate(desc["output_schema"], outcome.result, maximum=MAX_RESULT_BYTES)
            if not isinstance(outcome.events, (list, tuple)) or len(outcome.events) > MAX_EVENTS:
                raise InvalidArguments("invalid event batch")
            if read_only and outcome.events:
                raise WorldRuntimeError("read function cannot emit durable events")
            total = 0
            for event in outcome.events:
                if not isinstance(event, (EventSpec,StreamEvent)):
                    raise InvalidArguments("events must be EventSpec or StreamEvent values")
                if isinstance(event, StreamEvent):
                    identifier(event.stream, "stream", 64)
                    json_text(event.payload, maximum=65536)
                    if event.key:
                        identifier(event.key, "event key")
                else:
                    identifier(event.recipient_role_id, "event recipient")
                identifier(event.kind, "event kind")
                if not isinstance(event.payload, dict):
                    raise InvalidArguments("event payload must be an object")
                if event.kind == PRESENTATION_KIND:
                    validate_cue(event.payload)
                total += len(json_text(event.payload).encode())
            if total > MAX_RESULT_BYTES:
                raise InvalidArguments("event batch exceeds size budget")
        except WorldRuntimeError as exc:
            if read_only and outcome.events:
                raise WorldRuntimeError("read function cannot emit durable events") from exc
            raise ResultRejected("handler output or events violate the declared contract") from exc

    def query_function(
        self, universe, function_id, actor_role_id, arguments, *, expected_version=None, identity_token=None
    ):
        arguments_object(arguments)
        identifier(function_id, "function_id")
        if expected_version is not None:
            integer(expected_version, "expected_version", 1)
        registry = self._capture_registry(universe)
        with self._conn(readonly=True) as c:
            self._begin_registry_read(c, universe, registry)
            self._admit_actor_tx(c, universe, actor_role_id, identity_token)
            self._check_world_tx(c, universe, registry=registry)
            row = c.execute(
                "SELECT * FROM function_registry WHERE universe=? AND function_id=?", (universe, function_id)
            ).fetchone()
            if row is None:
                raise FunctionNotFound(function_id)
            desc = self._row_to_descriptor(row)
            if desc["access"] != "read":
                raise AccessModeMismatch("query_function only accepts read functions")
            version = desc["version"]
            if expected_version is not None and version != expected_version:
                raise FunctionVersionMismatch(
                    f"expected function contract version {expected_version}, current version {version}; rediscover the function, not the world object's state version"
                )
            if desc["availability"] != "available":
                raise FunctionNotFound("function is unavailable")
            validate(desc["input_schema"], arguments)
            handler = registry.handlers.get((universe, function_id, version))
            if handler is None:
                raise FunctionNotFound("handler is not loaded in this process")
            ctx = self._context_tx(c, universe, actor_role_id, function_id, version, registry=registry)
            self._authorize_function_tx(c, ctx, arguments)
            outcome = self._run_user_code(c, handler, ctx, arguments, read_only=True)
            self._validate_outcome(outcome, desc, read_only=True)
            result = json.loads(json_text(outcome.result))
            # This is the journal watermark of the SAME read transaction. A
            # logical tick cannot order player writes that happen within it.
            snapshot_seq = ctx.read_snapshot_seq
        return {
            "ok": True,
            "function_id": function_id,
            "function_version": version,
            "result": result,
            "read_only": True,
            "snapshot_seq": snapshot_seq,
        }

    def _replay_tx(self, row, function_id, arguments, activity_claim):
        digest = self._sha256({"function_id": function_id, "arguments": arguments})
        if row["function_id"] != function_id or row["args_hash"] != digest:
            raise OperationConflict("operation_id was used for a different intent")
        receipt = json.loads(row["receipt_json"])
        target = activity_claim.get("activity_id") if activity_claim else None
        previous_target = row["activity_id"]
        if previous_target != target:
            raise OperationConflict("operation_id was used for a different activity")
        receipt["replayed"] = True
        return receipt

    def invoke_function(
        self,
        universe,
        function_id,
        actor_role_id,
        arguments,
        *,
        operation_id,
        expected_version=None,
        activity_claim=None,
        identity_token=None,
    ):
        if not operation_id:
            raise OperationConflict("operation_id is required for write functions")
        identifier(operation_id, "operation_id")
        identifier(function_id, "function_id")
        arguments_object(arguments)
        encoded_arguments = json_text(arguments)
        intent_arguments = json.loads(encoded_arguments)
        arguments = json.loads(encoded_arguments)
        if expected_version is not None:
            integer(expected_version, "expected_version", 1)
        if activity_claim is not None and not isinstance(activity_claim, dict):
            raise InvalidArguments("activity_claim must be an object")
        with self._conn(serialized=True, purpose="action") as c:
            with self._work_timings.phase("begin"):
                c.execute("BEGIN IMMEDIATE")
            now = time.time()  # Admission time is AFTER the database write lock.
            identity = self._admit_actor_tx(c, universe, actor_role_id, identity_token, require_control=True)
            old = c.execute(
                "SELECT * FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                (universe, actor_role_id, operation_id),
            ).fetchone()
            if old:
                return self._replay_tx(old, function_id, arguments, activity_claim)
            journal_marker = self._journal_marker_tx(c)
            self._check_world_tx(c, universe)
            row = c.execute(
                "SELECT * FROM function_registry WHERE universe=? AND function_id=?", (universe, function_id)
            ).fetchone()
            if row is None:
                raise FunctionNotFound(function_id)
            desc = self._row_to_descriptor(row)
            if desc["access"] != "write":
                raise AccessModeMismatch("invoke_function cannot execute a read function as a write")
            version = desc["version"]
            if expected_version is not None and expected_version != version:
                raise FunctionVersionMismatch(
                    f"expected function contract version {expected_version}, current version {version}; rediscover the function, not the world object's state version"
                )
            if desc["availability"] != "available":
                raise FunctionNotFound("function is unavailable")
            validate(desc["input_schema"], arguments)
            activity_id = None
            if desc["requires_activity_claim"]:
                if not activity_claim:
                    raise ClaimFenced("this function requires an activity claim")
                activity_id = self._validate_claim_tx(c, universe, actor_role_id, activity_claim, now)
            elif activity_claim is not None:
                raise InvalidArguments("this function does not accept an activity claim")
            handler = self._handlers.get((universe, function_id, version))
            if handler is None:
                raise FunctionNotFound("handler is not loaded in this process")
            ctx = self._context_tx(
                c,
                universe,
                actor_role_id,
                function_id,
                version,
                access="write",
                activity_id=activity_id,
                operation_id=operation_id,
                now=now,
            )
            self._authorize_function_tx(c, ctx, arguments)
            self._work_timings.annotate(handler=function_id)
            with self._work_timings.phase("handler", cpu=True):
                outcome = self._run_user_code(c, handler, ctx, arguments)
            self._validate_outcome(outcome, desc)
            # Expiry during rule evaluation cannot turn into a late unauthorized commit.
            identity = self._admit_actor_tx(c, universe, actor_role_id, identity_token, require_control=True)
            if activity_claim:
                self._validate_claim_tx(c, universe, actor_role_id, activity_claim, time.time())
            receipt = self._commit_outcome_tx(
                c, ctx, outcome, intent_arguments, journal_marker=journal_marker, activity_id=activity_id,
                credential_id=identity["token_id"] if identity is not None else None,
            )
        self.wake_waiters()
        return receipt

    def _commit_outcome_tx(self, c, ctx, outcome, arguments, *, journal_marker,
                           activity_id=None, source="action", extra_receipt=None, credential_id=None,
                           record_operation=True):
        """One effect/receipt path shared by immediate actions and durable timers."""
        with self._work_timings.phase("managed"):
            return self._commit_outcome_body_tx(c, ctx, outcome, arguments, journal_marker=journal_marker,
                activity_id=activity_id, source=source, extra_receipt=extra_receipt,
                credential_id=credential_id, record_operation=record_operation)

    def _commit_outcome_body_tx(self, c, ctx, outcome, arguments, *, journal_marker,
                                activity_id, source, extra_receipt, credential_id, record_operation):
        self._validate_managed_state_changes_tx(c, ctx, journal_marker)
        transitions = self._apply_timer_commands_tx(c, ctx)
        self._apply_checkpoint_commands_tx(c, ctx)
        event_seqs = []
        publications = []
        for index, event in enumerate(outcome.events):
            if isinstance(event, StreamEvent):
                publications.append(self._append_stream_event_tx(c,ctx,event,index))
                continue
            cursor = c.execute(
                "INSERT INTO events(universe,recipient_role_id,actor_role_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (ctx.universe, event.recipient_role_id, ctx.actor_role_id, event.kind,
                 json_text(event.payload), ctx.now),
            )
            event_seqs.append(int(cursor.lastrowid))
        commit_seq = self._record_commit_tx(
            c, ctx.universe, journal_marker, actor_role_id=ctx.actor_role_id,
            function_id=ctx.function_id, operation_id=ctx.operation_id, source=source,
            world_version=self._worlds[ctx.universe].version if ctx.universe in self._worlds else None,
            event_seqs=event_seqs,
        )
        self._bind_timer_transitions_tx(c, transitions, commit_seq)
        for stream,seq,eid in publications:
            c.execute("UPDATE stream_events SET commit_seq=? WHERE universe=? AND stream=? AND seq=?",
                      (commit_seq,ctx.universe,stream,seq))
        receipt = {
            "commit_seq": commit_seq, "ok": True, "operation_id": ctx.operation_id,
            # This original submission source survives replay under another credential.
            "actor_role_id": ctx.actor_role_id, "credential_id": credential_id,
            "function_id": ctx.function_id, "function_version": ctx.function_version,
            "result": outcome.result, "event_seqs": event_seqs, "replayed": False,
        }
        if publications:
            receipt["stream_event_ids"] = [p[2] for p in publications]
        if extra_receipt:
            receipt.update(extra_receipt)
        if ctx.random_draws:
            receipt["random_draws"] = ctx.random_draws
        encoded = json_text(receipt)
        if record_operation:
            c.execute(
                "INSERT INTO operations(universe,actor_role_id,idempotency_key,function_id,function_version,args_hash,receipt_json,created_at,activity_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ctx.universe, ctx.actor_role_id, ctx.operation_id, ctx.function_id, ctx.function_version,
                 self._sha256({"function_id": ctx.function_id, "arguments": arguments}), encoded, ctx.now, activity_id),
            )
        return json.loads(encoded)

    def get_receipt(self, universe, role_id, operation_id, *, identity_token=None):
        identifier(operation_id, "operation_id")
        with self._conn(readonly=True) as c:
            c.execute("BEGIN")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            row = c.execute(
                "SELECT receipt_json FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                (universe, role_id, operation_id),
            ).fetchone()
            if row is None:
                raise ReceiptNotFound("no committed operation with this ID for this role")
            return json.loads(row["receipt_json"])

    def call_function(
        self,
        universe,
        function_id,
        role_id,
        arguments,
        *,
        operation_id=None,
        expected_version=None,
        activity_claim=None,
        identity_token=None,
    ):
        if operation_id is not None:
            try:
                self.get_receipt(universe, role_id, operation_id, identity_token=identity_token)
            except ReceiptNotFound:
                pass
            else:
                return self.invoke_function(
                    universe,
                    function_id,
                    role_id,
                    arguments,
                    operation_id=operation_id,
                    expected_version=expected_version,
                    activity_claim=activity_claim,
                    identity_token=identity_token,
                )
        desc = self.get_function(universe, function_id)
        if desc["access"] == "read":
            if operation_id is not None or activity_claim is not None:
                raise InvalidArguments("read calls do not take operation_id or activity_claim")
            return self.query_function(
                universe,
                function_id,
                role_id,
                arguments,
                expected_version=expected_version,
                identity_token=identity_token,
            )
        return self.invoke_function(
            universe,
            function_id,
            role_id,
            arguments,
            operation_id=operation_id,
            expected_version=expected_version,
            activity_claim=activity_claim,
            identity_token=identity_token,
        )
