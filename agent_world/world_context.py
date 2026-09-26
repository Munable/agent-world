from __future__ import annotations

import base64
import json
import math
import secrets
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
        timers_enabled=False,
        timer=None,
    ):
        self.conn = conn  # Legacy escape hatch; not part of the portable World SDK.
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
        self._authorizing_state = False
        self._timers_enabled = timers_enabled
        self._timer_commands = []
        self.timer = timer
        # Mutations performed through the scoped API are recorded so commit-time
        # validation can distinguish them from legacy raw-SQL writes via conn.
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
        if len(self._timer_commands) >= MAX_TIMER_COMMANDS:
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
        if len(self._timer_commands) >= MAX_TIMER_COMMANDS:
            raise InvalidArguments("too many timer changes in one transaction")
        self._timer_commands.append({"kind": "cancel", "timer_id": timer_id})

    def get_timer(self, timer_id):
        from .world_timers import TimerNotFound, public_timer

        identifier(timer_id, "timer_id")
        row = self.conn.execute("SELECT * FROM world_timers WHERE universe=? AND timer_id=?",
                                (self.universe, timer_id)).fetchone()
        if row is None:
            raise TimerNotFound("timer not found in this universe")
        return public_timer(row)

    def stream_event_id(self, stream, key):
        from .world_streams import event_id
        return event_id(self.universe,self.actor_role_id,self.function_id,self.operation_id,stream,key)

    def get_stream_event(self, stream, event_id):
        """Trusted rule lookup within this universe. Transport reads enforce StreamSpec policy."""
        identifier(stream,'stream',64); identifier(event_id,'event ID')
        row=self.conn.execute('SELECT event_id,actor_role_id,kind,payload_json,created_at FROM stream_events WHERE universe=? AND stream=? AND event_id=?',
                              (self.universe,stream,event_id)).fetchone()
        if row is None:
            return None
        return {'event_id':row['event_id'],'actor_role_id':row['actor_role_id'],'kind':row['kind'],
                'payload':json.loads(row['payload_json']),'occurred_at':row['created_at']}

    def recent_stream_events(self, stream, limit=20):
        identifier(stream,'stream',64); integer(limit,'stream rule lookup',1,100)
        rows=self.conn.execute('SELECT event_id FROM stream_events WHERE universe=? AND stream=? ORDER BY seq DESC LIMIT ?',
                               (self.universe,stream,limit)).fetchall()
        return [self.get_stream_event(stream,row['event_id']) for row in reversed(rows)]

    def authorization_state(self, scope: str, key: str, default: Any = None) -> Any:
        """Read one universe-scoped state value only while state_authorizer decides access."""
        if not self._authorizing_state:
            raise PermissionDenied("authorization_state is only available inside state_authorizer")
        identifier(scope, "scope", 256)
        identifier(key, "state key", 256)
        row = self.conn.execute(
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
        row = self.conn.execute(
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
        row = self.conn.execute(
            "SELECT version FROM world_state WHERE universe=? AND scope=? AND state_key=?",
            (self.universe, scope, key),
        ).fetchone()
        current = int(row["version"]) if row else 0
        if expected_version is not None:
            integer(expected_version, "expected state version")
            if current != expected_version:
                raise StateConflict(f"expected state version {expected_version}, current is {current}")
        version = current + 1
        self.conn.execute(
            "INSERT INTO world_state(universe,scope,state_key,value_json,version,updated_at,deleted) "
            "VALUES(?,?,?,?,?,?,0) ON CONFLICT(universe,scope,state_key) DO UPDATE SET "
            "value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at,deleted=0",
            (self.universe, scope, key, encoded, version, self.now),
        )
        self._approved_state_changes.append((scope, key, version, 0, encoded))
        return version

    def delete_state(self, scope: str, key: str, *, expected_version=None) -> int:
        self._check(scope, key, "write")
        row = self.conn.execute(
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
        self.conn.execute(
            "UPDATE world_state SET value_json='null',deleted=1,version=?,updated_at=? "
            "WHERE universe=? AND scope=? AND state_key=?",
            (version, self.now, self.universe, scope, key),
        )
        self._approved_state_changes.append((scope, key, version, 1, "null"))
        return version

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
        rows = self.conn.execute(sql + " ORDER BY " + ordering + " LIMIT ?", (*params, limit + 1)).fetchall()
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
        row = self.conn.execute(
            "SELECT role_id,display_name,avatar_ref,status,created_at,updated_at FROM roles WHERE role_id=?",
            (role_id,),
        ).fetchone()
        if row is None:
            raise RoleNotFound(role_id)
        return dict(row)

    def get_activity(self, activity_id: str) -> dict:
        from .runtime_errors import ActivityNotFound

        identifier(activity_id, "activity_id")
        row = self.conn.execute(
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
