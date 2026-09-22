"""Durable one-shot world timers with transactional effects and bounded catch-up."""
from __future__ import annotations

import json
import time
import sqlite3

from .runtime_contracts import identifier, integer, json_text, validate
from .runtime_errors import (
    WorldDefinitionError, WorldVersionMismatch, PermissionDenied, RuleViolation,
    SchemaRejected, InvalidArguments,
)
from .world_timers import (
    TimerConflict, TimerNotFound, RetryTimer, TimerInvocation, public_timer,
    MAX_PENDING_TIMERS, MAX_TIMER_COMMANDS, TIMER_ACTOR,
)


class RuntimeTimers:
    def _init_timers_tx(self, c):
        c.execute("""CREATE TABLE IF NOT EXISTS world_timers(
            universe TEXT NOT NULL, timer_id TEXT NOT NULL,
            handler TEXT NOT NULL, handler_version INTEGER NOT NULL,
            world_id TEXT NOT NULL, world_version INTEGER NOT NULL,
            arguments_json TEXT NOT NULL, intent_hash TEXT NOT NULL,
            due_at REAL NOT NULL, available_at REAL NOT NULL,
            created_at REAL NOT NULL, created_by TEXT NOT NULL, created_operation_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled','rejected','failed','blocked')),
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL, retry_delay REAL NOT NULL,
            completed_at REAL, last_error_code TEXT, receipt_json TEXT,
            PRIMARY KEY(universe,timer_id))""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_timer_due
            ON world_timers(universe,status,available_at,timer_id)""")
        c.execute("""CREATE TABLE IF NOT EXISTS timer_transitions(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            universe TEXT NOT NULL, timer_id TEXT NOT NULL, status TEXT NOT NULL,
            actor_role_id TEXT NOT NULL, operation_id TEXT, created_at REAL NOT NULL,
            attempt INTEGER NOT NULL, error_code TEXT,
            commit_seq INTEGER REFERENCES world_commits(seq),
            FOREIGN KEY(universe,timer_id) REFERENCES world_timers(universe,timer_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_timer_transitions ON timer_transitions(universe,timer_id,seq)")

    @staticmethod
    def _timer_transition_tx(c, universe, timer_id, status, *, actor, operation_id=None,
                             attempt=0, error_code=None, commit_seq=None):
        return c.execute("""INSERT INTO timer_transitions(universe,timer_id,status,actor_role_id,
            operation_id,created_at,attempt,error_code,commit_seq) VALUES(?,?,?,?,?,?,?,?,?)""",
            (universe, timer_id, status, actor, operation_id, time.time(), attempt, error_code, commit_seq)).lastrowid

    def _apply_timer_commands_tx(self, c, ctx, definition=None):
        commands = ctx._timer_commands
        if not commands:
            return []
        if ctx.access != "write" or not ctx._timers_enabled or len(commands) > MAX_TIMER_COMMANDS:
            raise InvalidArguments("invalid timer command batch")
        definition = definition or self._worlds.get(ctx.universe)
        if definition is None:
            raise WorldDefinitionError("timers require an installed world definition")
        json_text(commands)
        specs = {spec.name: spec for spec in definition.timers}
        transitions = []
        for command in commands:
            tid = identifier(command["timer_id"], "timer_id")
            row = c.execute("SELECT * FROM world_timers WHERE universe=? AND timer_id=?", (ctx.universe, tid)).fetchone()
            if command["kind"] == "cancel":
                if row is None:
                    raise TimerNotFound("timer not found in this universe")
                if row["status"] == "completed" or row["status"] == "cancelled":
                    continue
                c.execute("UPDATE world_timers SET status='cancelled',completed_at=? WHERE universe=? AND timer_id=?",
                          (time.time(), ctx.universe, tid))
                transitions.append(self._timer_transition_tx(c, ctx.universe, tid, "cancelled",
                    actor=ctx.actor_role_id, operation_id=ctx.operation_id, attempt=row["attempts"]))
                continue
            if command["kind"] != "schedule":
                raise InvalidArguments("unknown timer command")
            spec = specs.get(command["handler"])
            if spec is None:
                raise WorldDefinitionError("timer handler is not declared by this world")
            validate(spec.input_schema, command["arguments"])
            intent = {"handler": spec.name, "handler_version": spec.version,
                      "world_id": definition.world_id, "world_version": definition.version,
                      "arguments": command["arguments"], "due_at": command["due_at"]}
            digest = self._sha256(intent)
            if row is not None:
                if row["intent_hash"] != digest:
                    raise TimerConflict("timer_id already identifies a different scheduled intent")
                continue  # Identical IDs never reactivate a completed/cancelled timer.
            count = c.execute("SELECT COUNT(*) FROM world_timers WHERE universe=? AND status='pending'",
                              (ctx.universe,)).fetchone()[0]
            if count >= MAX_PENDING_TIMERS:
                raise InvalidArguments("world pending timer capacity reached")
            c.execute("""INSERT INTO world_timers(universe,timer_id,handler,handler_version,world_id,world_version,
                arguments_json,intent_hash,due_at,available_at,created_at,created_by,created_operation_id,
                status,max_attempts,retry_delay) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (ctx.universe, tid, spec.name, spec.version, definition.world_id, definition.version,
                 json_text(command["arguments"]), digest, command["due_at"], command["due_at"],
                 time.time(), ctx.actor_role_id, ctx.operation_id, spec.max_attempts, spec.retry_delay_seconds))
            transitions.append(self._timer_transition_tx(c, ctx.universe, tid, "scheduled",
                actor=ctx.actor_role_id, operation_id=ctx.operation_id))
        return transitions

    @staticmethod
    def _bind_timer_transitions_tx(c, transitions, commit_seq):
        for seq in transitions:
            c.execute("UPDATE timer_transitions SET commit_seq=? WHERE seq=?", (commit_seq, seq))

    def get_timer(self, universe, timer_id):
        """Trusted server inspection. World APIs decide which metadata a client may observe."""
        identifier(universe, "universe")
        identifier(timer_id, "timer_id")
        with self._conn(readonly=True) as c:
            row = c.execute("SELECT * FROM world_timers WHERE universe=? AND timer_id=?", (universe, timer_id)).fetchone()
            if row is None:
                raise TimerNotFound("timer not found in this universe")
            return public_timer(row)

    def get_timer_receipt(self, universe, timer_id):
        """Trusted receipt access; it is not an automatic public-result API."""
        identifier(universe, "universe")
        identifier(timer_id, "timer_id")
        with self._conn(readonly=True) as c:
            row = c.execute("SELECT receipt_json FROM world_timers WHERE universe=? AND timer_id=?", (universe, timer_id)).fetchone()
            if row is None:
                raise TimerNotFound("timer not found in this universe")
            return json.loads(row[0]) if row[0] is not None else None

    def run_due_timers(self, universe, *, limit=50):
        """Trusted worker entry. A fixed candidate list prevents recursive catch-up storms."""
        identifier(universe, "universe")
        integer(limit, "limit", 1, 100)
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            self._check_world_tx(c, universe)
            if universe not in self._worlds:
                raise WorldDefinitionError("worker requires the matching installed world")
            cutoff = time.time()
            candidates = c.execute("""SELECT timer_id FROM world_timers
                WHERE universe=? AND status='pending' AND available_at<=? AND due_at<=?
                ORDER BY available_at,timer_id LIMIT ?""", (universe, cutoff, cutoff, limit)).fetchall()
        processed = []
        for candidate in candidates:
            item = self._run_one_timer(universe, cutoff, candidate[0])
            if item is not None:
                processed.append(item)
        if processed:
            self.wake_waiters()
        return {"processed": processed, "count": len(processed)}

    def _run_one_timer(self, universe, cutoff, timer_id):
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._check_world_tx(c, universe)
            now = min(cutoff, time.time())  # Recheck after lock acquisition; never fire early after a clock rollback.
            row = c.execute("""SELECT * FROM world_timers WHERE universe=? AND status='pending'
                AND available_at<=? AND due_at<=? AND timer_id=?""", (universe, now, now, timer_id)).fetchone()
            if row is None:
                return None
            definition = self._worlds[universe]
            spec = next((s for s in definition.timers if s.name == row["handler"]), None)
            tid = row["timer_id"]
            attempt = row["attempts"] + 1
            operation_id = "timer:" + self._sha256({"universe": universe, "timer_id": tid})
            marker = self._journal_marker_tx(c)
            if (spec is None or row["world_id"] != definition.world_id or row["world_version"] != definition.version
                    or row["handler_version"] != spec.version):
                return self._timer_failure_tx(c, row, marker, "blocked", "TimerVersionMismatch", operation_id,
                                              attempt=row["attempts"])
            ctx = self._context_tx(c, universe, TIMER_ACTOR, "timer:" + spec.name, spec.version,
                                   access="write", operation_id=operation_id, now=time.time())
            ctx.timer = TimerInvocation(tid, row["due_at"], row["created_by"], row["created_operation_id"],
                                        row["created_at"], attempt)
            arguments = json.loads(row["arguments_json"])
            desc = {"output_schema": spec.output_schema}
            c.execute("SAVEPOINT timer_effects")
            try:
                validate(spec.input_schema, arguments)
                if spec.authorize is not None:
                    ctx.access = "read"
                    try:
                        allowed = self._run_user_code(c, spec.authorize, ctx, json.loads(row["arguments_json"]), read_only=True)
                    finally:
                        ctx.access = "write"
                    if allowed is not True:
                        raise PermissionDenied("timer precondition is no longer authorized")
                outcome = self._run_user_code(c, spec.handler, ctx, arguments)
                self._validate_outcome(outcome, desc)
                receipt = self._commit_outcome_tx(c, ctx, outcome, json.loads(row["arguments_json"]),
                    journal_marker=marker, source="timer", extra_receipt={"timer_id": tid, "due_at": row["due_at"],
                        "attempt": attempt, "scheduled_by": row["created_by"]})
                c.execute("""UPDATE world_timers SET status='completed',attempts=?,completed_at=?,
                    last_error_code=NULL,receipt_json=? WHERE universe=? AND timer_id=?""",
                    (attempt, time.time(), json_text(receipt), universe, tid))
                self._timer_transition_tx(c, universe, tid, "completed", actor=TIMER_ACTOR,
                    operation_id=operation_id, attempt=attempt, commit_seq=receipt["commit_seq"])
                c.execute("RELEASE timer_effects")
                return {"timer_id": tid, "status": "completed", "attempt": attempt}
            except (KeyboardInterrupt, SystemExit):
                raise  # The outer transaction rolls back, leaving the timer pending for restart.
            except Exception as exc:
                # Storage errors may have aborted the entire transaction: do not disguise them as rule errors.
                storage_codes = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL,
                                 sqlite3.SQLITE_IOERR, sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_INTERRUPT,
                                 sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CANTOPEN,
                                 sqlite3.SQLITE_READONLY}
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                if not c.in_transaction or (isinstance(exc, sqlite3.DatabaseError) and code in storage_codes):
                    raise
                c.execute("ROLLBACK TO timer_effects")
                c.execute("RELEASE timer_effects")
                if isinstance(exc, RetryTimer):
                    status = "pending" if attempt < row["max_attempts"] else "failed"
                    code = "RetryTimer" if status == "pending" else "TimerAttemptsExhausted"
                elif isinstance(exc, (PermissionDenied, RuleViolation)):
                    status, code = "rejected", type(exc).__name__
                elif isinstance(exc, (SchemaRejected, InvalidArguments, TimerConflict, WorldDefinitionError)):
                    status, code = "failed", type(exc).__name__
                else:
                    status, code = "failed", "TimerHandlerError"
                return self._timer_failure_tx(c, row, marker, status, code, operation_id, attempt=attempt)

    def _timer_failure_tx(self, c, row, marker, status, code, operation_id, *, attempt):
        now = time.time()
        delay = min(86400, row["retry_delay"] * (2 ** max(0, attempt - 1)))
        c.execute("""UPDATE world_timers SET status=?,attempts=?,available_at=?,last_error_code=?,completed_at=?
            WHERE universe=? AND timer_id=?""", (status, attempt, now + delay if status == "pending" else row["available_at"],
                code, None if status == "pending" else now, row["universe"], row["timer_id"]))
        seq = self._record_commit_tx(c, row["universe"], marker, actor_role_id=TIMER_ACTOR,
                function_id="timer:" + row["handler"], operation_id=operation_id, source="timer_" + status,
                world_version=self._worlds[row["universe"]].version)
        self._timer_transition_tx(c, row["universe"], row["timer_id"], status, actor=TIMER_ACTOR,
                                  operation_id=operation_id, attempt=attempt, error_code=code, commit_seq=seq)
        return {"timer_id": row["timer_id"], "status": status, "attempt": attempt, "error_code": code}
