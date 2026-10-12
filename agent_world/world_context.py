from __future__ import annotations

import base64
import json
import math
import secrets
import sqlite3
import time
from typing import Any

from .runtime_contracts import identifier, integer, json_text, MAX_STATE_BYTES
from .runtime_errors import InvalidArguments, PermissionDenied, RoleNotFound, StateConflict, WorldRuntimeError


class FunctionContext:
    # Trusted world code receives a universe-scoped storage API, not credentials.
    def __init__(
        self,
        conn,
        universe,
        actor_role_id,
        function_id,
        function_version,
        activity_id=None,
        access="write",
        *,
        now=None,
        operation_id=None,
        state_validator=None,
        state_authorizer=None,
        state_forget_authorizer=None,
        timers_enabled=False,
        checkpoints_enabled=False,
        checkpoint_specs=(),
        timer=None,
    ):
        self._conn = conn  # Internal transaction handle; not part of the World SDK.
        self.universe = universe
        self.actor_role_id = actor_role_id
        self.function_id = function_id
        self.function_version = function_version
        self.activity_id = activity_id
        self.access = access
        self.now = time.time() if now is None else now
        self.operation_id = operation_id
        self.random_draws: list[dict[str, int]] = []
        self._state_validator = state_validator
        self._state_authorizer = state_authorizer
        self._state_forget_authorizer = state_forget_authorizer
        self._authorizing_state = False
        self._forgetting_state = False
        self._timers_enabled = timers_enabled
        self._timer_commands = []
        self._checkpoints_enabled = checkpoints_enabled
        self._checkpoint_specs = {spec.name: spec for spec in checkpoint_specs}
        self._checkpoint_commands = []
        self._checkpoint_rows = {}
        self.timer = timer
        self.checkpoint = None
        # A migration that already resumed scheduled work can suppress the
        # first-attach hook in the same managed installation transaction.
        self.world_resumed = False
        # Mutations performed through the scoped API are recorded so commit-time
        # validation can distinguish them from unexpected direct internal writes.
        self._approved_state_changes: list[tuple[str, str, int, int, str]] = []

    def schedule_timer(self, timer_id, handler, arguments, *, due_at):
        from .world_timers import MAX_TIMER_COMMANDS, MAX_TIMER_ARGUMENT_BYTES
        from .runtime_contracts import duration, arguments_object

        if self.access != "write" or not self._timers_enabled:
            raise WorldRuntimeError("timers require a managed world write")
        identifier(timer_id, "timer_id")
        identifier(handler, "timer handler")
        due_at = duration(due_at, "due_at", 253402300799, zero=True)
        arguments_object(arguments)
        if len(self._timer_commands) + len(self._checkpoint_commands) >= MAX_TIMER_COMMANDS:
            raise InvalidArguments("too many timer changes in one transaction")
        self._timer_commands.append({"kind": "schedule", "timer_id": timer_id, "handler": handler,
                                     "arguments": json.loads(json_text(arguments, maximum=MAX_TIMER_ARGUMENT_BYTES)),
                                     "due_at": due_at})
        json_text(self._timer_commands)
        return timer_id

    def cancel_timer(self, timer_id):
        from .world_timers import MAX_TIMER_COMMANDS

        if self.access != "write" or not self._timers_enabled:
            raise WorldRuntimeError("timers require a managed world write")
        identifier(timer_id, "timer_id")
        if self.timer is not None and self.timer.timer_id == timer_id:
            raise InvalidArguments("a firing timer cannot cancel itself")
        if len(self._timer_commands) + len(self._checkpoint_commands) >= MAX_TIMER_COMMANDS:
            raise InvalidArguments("too many timer changes in one transaction")
        self._timer_commands.append({"kind": "cancel", "timer_id": timer_id})

    def get_timer(self, timer_id):
        from .world_timers import TimerNotFound, public_timer

        identifier(timer_id, "timer_id")
        row = self._conn.execute("SELECT * FROM world_timers WHERE universe=? AND timer_id=?",
                                (self.universe, timer_id)).fetchone()
        if row is None:
            raise TimerNotFound("timer not found in this universe")
        return public_timer(row)

    def _checkpoint_record(self, checkpoint_key):
        if checkpoint_key not in self._checkpoint_rows:
            row = self._conn.execute("SELECT * FROM world_checkpoints WHERE universe=? AND checkpoint_key=?",
                                     (self.universe, checkpoint_key)).fetchone()
            self._checkpoint_rows[checkpoint_key] = dict(row) if row is not None else None
        return self._checkpoint_rows[checkpoint_key]

    def get_checkpoint(self, checkpoint_key):
        from .world_checkpoints import CheckpointNotFound, public_checkpoint
        identifier(checkpoint_key, "checkpoint key")
        row = self._checkpoint_record(checkpoint_key)
        if row is None:
            raise CheckpointNotFound("checkpoint not found in this universe")
        return public_checkpoint(row)

    def schedule_checkpoint(self, checkpoint_key, handler, arguments, *, due_at, expected_generation=None):
        from .world_checkpoints import (
            CheckpointConflict, MAX_CHECKPOINT_COMMANDS, MAX_CHECKPOINT_ARGUMENT_BYTES,
        )
        from .runtime_contracts import duration, arguments_object, validate
        from .runtime_errors import WorldDefinitionError

        if self.access != "write" or not self._checkpoints_enabled:
            raise WorldRuntimeError("checkpoints require a managed world write")
        identifier(checkpoint_key, "checkpoint key")
        identifier(handler, "checkpoint handler")
        spec = self._checkpoint_specs.get(handler)
        if spec is None:
            raise WorldDefinitionError("checkpoint handler is not declared by this world")
        due_at = duration(due_at, "due_at", 253402300799, zero=True)
        arguments_object(arguments)
        validate(spec.input_schema, arguments)
        previous = self._checkpoint_record(checkpoint_key)
        current = previous["generation"] if previous is not None else 0
        if expected_generation is not None:
            integer(expected_generation, "expected checkpoint generation")
            if current != expected_generation:
                raise CheckpointConflict("checkpoint generation changed before scheduling")
        if len(self._checkpoint_commands) + len(self._timer_commands) >= MAX_CHECKPOINT_COMMANDS:
            raise InvalidArguments("too many scheduled changes in one transaction")
        encoded = json_text(arguments, maximum=MAX_CHECKPOINT_ARGUMENT_BYTES)
        generation = current + 1
        self._checkpoint_commands.append({"kind": "schedule", "checkpoint_key": checkpoint_key,
            "handler": handler, "arguments": json.loads(encoded), "due_at": due_at,
            "expected_generation": current, "generation": generation})
        json_text(self._checkpoint_commands)
        # Read-your-writes includes staged successors. Runtime rechecks the same
        # generations and applies these commands only with the outcome commit.
        self._checkpoint_rows[checkpoint_key] = {
            **(previous or {}), "checkpoint_key": checkpoint_key, "generation": generation,
            "completed_through": max((previous or {}).get("completed_through", 0), current),
            "latest_completed_generation": (previous or {}).get("latest_completed_generation"),
            "latest_commit_seq": (previous or {}).get("latest_commit_seq"),
            "handler": handler, "handler_version": spec.version, "arguments_json": encoded,
            "due_at": due_at, "available_at": due_at, "status": "pending", "attempts": 0,
            "created_at": self.now, "created_by": self.actor_role_id,
            "created_operation_id": self.operation_id, "completed_at": None, "last_error_code": None,
        }
        return generation

    def cancel_checkpoint(self, checkpoint_key, *, expected_generation=None):
        from .world_checkpoints import CheckpointConflict, CheckpointNotFound, MAX_CHECKPOINT_COMMANDS
        if self.access != "write" or not self._checkpoints_enabled:
            raise WorldRuntimeError("checkpoints require a managed world write")
        identifier(checkpoint_key, "checkpoint key")
        previous = self._checkpoint_record(checkpoint_key)
        if previous is None:
            raise CheckpointNotFound("checkpoint not found in this universe")
        if expected_generation is not None:
            integer(expected_generation, "expected checkpoint generation")
            if previous["generation"] != expected_generation:
                raise CheckpointConflict("checkpoint generation changed before cancellation")
        if (self.checkpoint is not None and checkpoint_key == self.checkpoint.checkpoint_key
                and previous["generation"] == self.checkpoint.generation):
            raise InvalidArguments("a firing checkpoint cannot cancel itself")
        if len(self._checkpoint_commands) + len(self._timer_commands) >= MAX_CHECKPOINT_COMMANDS:
            raise InvalidArguments("too many scheduled changes in one transaction")
        self._checkpoint_commands.append({"kind": "cancel", "checkpoint_key": checkpoint_key,
                                          "expected_generation": previous["generation"]})
        json_text(self._checkpoint_commands)
        self._checkpoint_rows[checkpoint_key] = {**previous, "status": "cancelled", "completed_at": self.now,
            "completed_through": max(previous["completed_through"], previous["generation"])}
        return previous["generation"]

    def stream_event_id(self, stream, key):
        from .world_streams import event_id
        return event_id(self.universe,self.actor_role_id,self.function_id,self.operation_id,stream,key)

    def get_stream_event(self, stream, event_id):
        """Trusted rule lookup within this universe. Transport reads enforce StreamSpec policy."""
        identifier(stream,'stream',64); identifier(event_id,'event ID')
        row=self._conn.execute('SELECT event_id,actor_role_id,kind,payload_json,created_at FROM stream_events WHERE universe=? AND stream=? AND event_id=?',
                              (self.universe,stream,event_id)).fetchone()
        if row is None:
            return None
        return {'event_id':row['event_id'],'actor_role_id':row['actor_role_id'],'kind':row['kind'],
                'payload':json.loads(row['payload_json']),'occurred_at':row['created_at']}

    def recent_stream_events(self, stream, limit=20):
        identifier(stream,'stream',64); integer(limit,'stream rule lookup',1,100)
        rows=self._conn.execute('SELECT event_id FROM stream_events WHERE universe=? AND stream=? ORDER BY seq DESC LIMIT ?',
                               (self.universe,stream,limit)).fetchall()
        return [self.get_stream_event(stream,row['event_id']) for row in reversed(rows)]

    def authorization_state(self, scope: str, key: str, default: Any = None) -> Any:
        """Read one universe-scoped state value only while state_authorizer decides access."""
        if not self._authorizing_state:
            raise PermissionDenied("authorization_state is only available inside state_authorizer")
        identifier(scope, "scope", 256)
        identifier(key, "state key", 256)
        row = self._conn.execute(
            "SELECT value_json,deleted FROM world_state WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        if row is None or row["deleted"]:
            return default
        return json.loads(row["value_json"])

    def _check(self, scope, key, access):
        identifier(scope, "scope", 256)
        identifier(key, "state key", 256)
        if access == "write" and self.access != "write":
            raise WorldRuntimeError("read function cannot modify world state")
        if self._state_authorizer is not None:
            previous = self._authorizing_state
            self._authorizing_state = True
            try:
                allowed = self._state_authorizer(self, scope, key, access)
            finally:
                self._authorizing_state = previous
            if allowed is not True:
                raise PermissionDenied("world state access denied")

    def get_state_record(self, scope: str, key: str, default: Any = None) -> dict:
        self._check(scope, key, "read")
        row = self._conn.execute(
            "SELECT value_json,version,updated_at,deleted FROM world_state "
            "WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        if row is None:
            return {"exists": False, "value": default, "version": 0, "updated_at": None}
        return {
            "exists": not bool(row["deleted"]),
            "value": default if row["deleted"] else json.loads(row["value_json"]),
            "version": int(row["version"]),
            "updated_at": float(row["updated_at"]),
        }

    def get_state(self, scope: str, key: str, default: Any = None) -> Any:
        return self.get_state_record(scope, key, default)["value"]

    def set_state(self, scope: str, key: str, value: Any, *, expected_version=None) -> int:
        self._check(scope, key, "write")
        encoded = json_text(value, maximum=MAX_STATE_BYTES)
        if self._state_validator is not None:
            self._state_validator(self, scope, key, value)
        row = self._conn.execute(
            "SELECT version FROM world_state WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        current = int(row["version"]) if row else 0
        if expected_version is not None:
            integer(expected_version, "expected state version")
            if current != expected_version:
                raise StateConflict(f"expected state version {expected_version}, current is {current}")
        version = current + 1
        self._conn.execute(
            "INSERT INTO world_state(universe,scope,state_key,value_json,version,updated_at,deleted) "
            "VALUES(?,?,?,?,?,?,0) ON CONFLICT(universe,scope,state_key) DO UPDATE SET "
            "value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at,deleted=0",
            (self.universe, scope, key, encoded, version, self.now),
        )
        self._approved_state_changes.append((scope, key, version, 0, encoded))
        return version

    def delete_state(self, scope: str, key: str, *, expected_version=None) -> int:
        self._check(scope, key, "write")
        row = self._conn.execute(
            "SELECT version,deleted FROM world_state WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        current = int(row["version"]) if row else 0
        if expected_version is not None:
            integer(expected_version, "expected state version")
            if current != expected_version:
                raise StateConflict("state changed before deletion")
        if row is None or row["deleted"]:
            return current
        version = current + 1
        self._conn.execute(
            "UPDATE world_state SET value_json='null',deleted=1,version=?,updated_at=? "
            "WHERE universe=? AND scope=? AND state_key=?",
            (version, self.now, self.universe, scope, key),
        )
        self._approved_state_changes.append((scope, key, version, 1, "null"))
        return version

    def forget_state(self, scope: str, key: str, *, expected_version: int) -> int:
        """Physically remove only a reviewed never-reused tombstone, with CAS."""
        self._check(scope, key, "write")
        integer(expected_version, "expected tombstone version", 1)
        row = self._conn.execute(
            "SELECT version,deleted FROM world_state WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        if row is None or row["version"] != expected_version:
            raise StateConflict("tombstone changed before forgetting")
        if not row["deleted"] or self._state_forget_authorizer is None:
            raise PermissionDenied("only explicitly authorized tombstones can be forgotten")
        def read_guard(action, arg1, arg2, database, trigger):
            return (sqlite3.SQLITE_OK if action in {
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
            } else sqlite3.SQLITE_DENY)
        old_access, old_authorizing = self.access, self._authorizing_state
        self.access, self._authorizing_state = "read", True
        self._conn.set_authorizer(read_guard)
        try:
            allowed = self._state_forget_authorizer(self, scope, key)
        finally:
            self._conn.set_authorizer(getattr(self, "_sql_authorizer", None))
            self.access, self._authorizing_state = old_access, old_authorizing
        if allowed is not True:
            raise PermissionDenied("world has not authorized forgetting this tombstone")
        self._forgetting_state = True
        try:
            self._conn.execute("DELETE FROM world_state WHERE universe=? AND scope=? AND state_key=? AND version=? AND deleted=1",
                               (self.universe, scope, key, expected_version))
        finally:
            self._forgetting_state = False
        # An already committed tombstone is not deleted a second time as a
        # world fact. The delete trigger skips it; never-reuse is the policy's
        # responsibility rather than fabricating a CAS version after removal.
        return expected_version

    def list_state(
        self, scope: str, *, prefix: str = "", limit: int = 50, after: str | None = None, order: str = "key"
    ) -> dict:
        identifier(scope, "scope", 256)
        integer(limit, "limit", 1, 200)
        if not isinstance(prefix, str) or len(prefix) > 256 or order not in {"key", "updated_desc"}:
            raise InvalidArguments("invalid state listing parameters")
        clauses = ["universe=?", "scope=?", "deleted=0", "substr(state_key,1,?)=?"]
        params: list[Any] = [self.universe, scope, len(prefix), prefix]
        if after is not None:
            try:
                if not isinstance(after, str) or len(after) > 4096:
                    raise ValueError()
                cursor = json.loads(base64.urlsafe_b64decode(after.encode()).decode())
                if [cursor[k] for k in ("universe", "scope", "prefix", "order")] != [
                    self.universe,
                    scope,
                    prefix,
                    order,
                ]:
                    raise ValueError()
                identifier(cursor["key"], "cursor key", 256)
                json_text(cursor)
                if order == "key":
                    clauses.append("state_key>?")
                    params.append(cursor["key"])
                else:
                    timestamp = cursor["updated_at"]
                    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
                        raise ValueError()
                    clauses.append("(updated_at<? OR (updated_at=? AND state_key<?))")
                    params.extend([cursor["updated_at"], cursor["updated_at"], cursor["key"]])
            except Exception as exc:
                raise InvalidArguments("invalid or differently scoped state cursor") from exc
        ordering = "state_key" if order == "key" else "updated_at DESC,state_key DESC"
        sql = "SELECT state_key,value_json,version,updated_at FROM world_state WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(sql + " ORDER BY " + ordering + " LIMIT ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            self._check(scope, row["state_key"], "read")
            items.append(
                {
                    "key": row["state_key"],
                    "value": json.loads(row["value_json"]),
                    "version": int(row["version"]),
                    "updated_at": float(row["updated_at"]),
                }
            )
        next_cursor = None
        if more and items:
            last = items[-1]
            cursor = {
                "universe": self.universe,
                "scope": scope,
                "prefix": prefix,
                "order": order,
                "key": last["key"],
                "updated_at": last["updated_at"],
            }
            next_cursor = base64.urlsafe_b64encode(json_text(cursor).encode()).decode()
        return {"items": items, "has_more": more, "next_cursor": next_cursor}

    def get_role(self, role_id: str | None = None) -> dict:
        role_id = self.actor_role_id if role_id is None else identifier(role_id, "role_id")
        row = self._conn.execute(
            "SELECT role_id,display_name,avatar_ref,status,created_at,updated_at FROM roles WHERE role_id=?",
            (role_id,),
        ).fetchone()
        if row is None:
            raise RoleNotFound("role not found in this universe")
        # Public-key profiles and operator entry records are world-local.
        # Completely unbound roles remain available to trusted SDK fixtures;
        # the operator's Runtime.get_role is the explicit global lookup.
        tables = ("identity_keys", "identity_tokens", "join_tickets", "role_world_presence")
        local = any(self._conn.execute(
            f"SELECT 1 FROM {table} WHERE universe=? AND role_id=? LIMIT 1",
            (self.universe, role_id),
        ).fetchone() is not None for table in tables)
        if not local and any(self._conn.execute(
            f"SELECT 1 FROM {table} WHERE role_id=? LIMIT 1", (role_id,),
        ).fetchone() is not None for table in tables):
            raise RoleNotFound("role not found in this universe")
        return dict(row)

    def get_activity(self, activity_id: str) -> dict:
        from .runtime_errors import ActivityNotFound

        identifier(activity_id, "activity_id")
        row = self._conn.execute(
            "SELECT activity_id,role_id,kind,exclusive_group,status,expires_at,created_at FROM activities "
            "WHERE universe=? AND activity_id=?",
            (self.universe, activity_id),
        ).fetchone()
        if row is None:
            raise ActivityNotFound(activity_id)
        return dict(row)

    def random_int(self, low: int, high: int) -> int:
        if self.access != "write":
            raise WorldRuntimeError("random draws belong to write operations")
        integer(low, "low", -(2**63))
        integer(high, "high", low)
        if len(self.random_draws) >= 128:
            raise InvalidArguments("too many random draws in one operation")
        value = low + secrets.randbelow(high - low + 1)
        self.random_draws.append({"low": low, "high": high, "value": value})
        return value
