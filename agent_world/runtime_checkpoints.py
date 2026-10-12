"""Bounded current-task generations using the existing managed commit boundary."""
from __future__ import annotations

import json
import sqlite3
import time

from .runtime_contracts import identifier, integer, json_text, validate
from .runtime_errors import (
    WorldDefinitionError, PermissionDenied, RuleViolation, SchemaRejected,
    InvalidArguments, ReceiptNotFound,
)
from .world_checkpoints import (
    CheckpointConflict, CheckpointNotFound, CheckpointReceiptExpired, CheckpointInvocation,
    MAX_CHECKPOINT_COMMANDS, CHECKPOINT_ACTOR, public_checkpoint,
)
from .world_timers import RetryTimer


class RuntimeCheckpoints:
    def _init_checkpoints_tx(self, c):
        c.execute("""CREATE TABLE IF NOT EXISTS world_checkpoints(
            universe TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
            generation INTEGER NOT NULL, completed_through INTEGER NOT NULL DEFAULT 0,
            latest_completed_generation INTEGER, latest_receipt_json TEXT, latest_commit_seq INTEGER,
            handler TEXT NOT NULL, handler_version INTEGER NOT NULL,
            world_id TEXT NOT NULL, world_version INTEGER NOT NULL,
            arguments_json TEXT NOT NULL, due_at REAL NOT NULL, available_at REAL NOT NULL,
            ready_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, created_by TEXT NOT NULL, created_operation_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled','rejected','failed','blocked')),
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, retry_delay REAL NOT NULL,
            completed_at REAL, last_error_code TEXT,
            PRIMARY KEY(universe,checkpoint_key),
            FOREIGN KEY(latest_commit_seq) REFERENCES world_commits(seq))""")
        if "ready_order" not in {r["name"] for r in c.execute("PRAGMA table_info(world_checkpoints)")}:
            c.execute("ALTER TABLE world_checkpoints ADD COLUMN ready_order INTEGER NOT NULL DEFAULT 0")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_checkpoint_ready
            ON world_checkpoints(universe,status,available_at,ready_order,checkpoint_key)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_checkpoint_latest_commit
            ON world_checkpoints(universe,latest_commit_seq) WHERE latest_commit_seq IS NOT NULL""")

    def _apply_checkpoint_commands_tx(self, c, ctx, definition=None):
        commands = ctx._checkpoint_commands
        if not commands:
            return
        if (ctx.access != "write" or not ctx._checkpoints_enabled
                or len(commands) + len(ctx._timer_commands) > MAX_CHECKPOINT_COMMANDS):
            raise InvalidArguments("invalid checkpoint command batch")
        definition = definition or self._worlds.get(ctx.universe)
        if definition is None:
            raise WorldDefinitionError("checkpoints require an installed world definition")
        json_text(commands)
        specs = {spec.name: spec for spec in definition.checkpoints}
        now = time.time()
        # Commit sequence is durable and does not decrease when history is
        # reclaimed. Existing ready keys precede a handler's new successor.
        sequence = c.execute("SELECT seq FROM sqlite_sequence WHERE name='world_commits'").fetchone()
        ready_order = (sequence[0] if sequence else 0) + 1
        for command in commands:
            key = identifier(command["checkpoint_key"], "checkpoint key")
            row = c.execute("SELECT * FROM world_checkpoints WHERE universe=? AND checkpoint_key=?",
                            (ctx.universe, key)).fetchone()
            generation = row["generation"] if row is not None else 0
            if generation != command["expected_generation"]:
                raise CheckpointConflict("checkpoint generation changed before commit")
            if command["kind"] == "cancel":
                if row is None:
                    raise CheckpointNotFound("checkpoint not found in this universe")
                if row["status"] != "cancelled":
                    c.execute("""UPDATE world_checkpoints SET status='cancelled',completed_at=?,
                        completed_through=MAX(completed_through,generation)
                        WHERE universe=? AND checkpoint_key=?""", (time.time(), ctx.universe, key))
                continue
            if command["kind"] != "schedule":
                raise InvalidArguments("unknown checkpoint command")
            spec = specs.get(command["handler"])
            if spec is None:
                raise WorldDefinitionError("checkpoint handler is not declared by this world")
            validate(spec.input_schema, command["arguments"])
            if command["generation"] != generation + 1:
                raise CheckpointConflict("checkpoint successor must advance one generation")
            # Superseded generations are fenced, not proof that their rule ran.
            # The latest exact terminal receipt remains independently retained.
            completed_through = max(row["completed_through"], generation) if row else 0
            c.execute("""INSERT INTO world_checkpoints(universe,checkpoint_key,generation,completed_through,
                handler,handler_version,world_id,world_version,arguments_json,due_at,available_at,ready_order,
                created_at,created_by,created_operation_id,status,max_attempts,retry_delay)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
                ON CONFLICT(universe,checkpoint_key) DO UPDATE SET
                generation=excluded.generation,completed_through=excluded.completed_through,
                handler=excluded.handler,handler_version=excluded.handler_version,world_id=excluded.world_id,
                world_version=excluded.world_version,arguments_json=excluded.arguments_json,
                due_at=excluded.due_at,available_at=excluded.available_at,ready_order=excluded.ready_order,
                created_at=excluded.created_at,
                created_by=excluded.created_by,created_operation_id=excluded.created_operation_id,
                status='pending',attempts=0,max_attempts=excluded.max_attempts,retry_delay=excluded.retry_delay,
                completed_at=NULL,last_error_code=NULL""",
                (ctx.universe, key, command["generation"], completed_through, spec.name, spec.version,
                 definition.world_id, definition.version, json_text(command["arguments"]),
                 command["due_at"], max(command["due_at"], now), ready_order, now, ctx.actor_role_id, ctx.operation_id,
                 spec.max_attempts, spec.retry_delay_seconds))

    def get_checkpoint(self, universe, checkpoint_key):
        identifier(universe, "universe")
        identifier(checkpoint_key, "checkpoint key")
        with self._conn(readonly=True) as c:
            row = c.execute("SELECT * FROM world_checkpoints WHERE universe=? AND checkpoint_key=?",
                            (universe, checkpoint_key)).fetchone()
            if row is None:
                raise CheckpointNotFound("checkpoint not found in this universe")
            return public_checkpoint(row)

    def get_checkpoint_receipt(self, universe, checkpoint_key, *, generation=None):
        """Return only the saved exact terminal receipt; an old absence cannot be replayed."""
        identifier(universe, "universe")
        identifier(checkpoint_key, "checkpoint key")
        if generation is not None:
            integer(generation, "checkpoint generation", 1)
        with self._conn(readonly=True) as c:
            row = c.execute("SELECT * FROM world_checkpoints WHERE universe=? AND checkpoint_key=?",
                            (universe, checkpoint_key)).fetchone()
            if row is None:
                raise CheckpointNotFound("checkpoint not found in this universe")
            requested = row["latest_completed_generation"] if generation is None else generation
            if requested is not None and requested == row["latest_completed_generation"]:
                return json.loads(row["latest_receipt_json"])
            if requested is not None and (requested < row["generation"] or requested <= row["completed_through"]):
                raise CheckpointReceiptExpired("checkpoint generation receipt has expired",
                    details={"checkpoint_key": checkpoint_key, "generation": requested,
                             "completed_through": row["completed_through"],
                             "latest_completed_generation": row["latest_completed_generation"]})
            raise ReceiptNotFound("checkpoint generation has no terminal receipt yet")

    def _due_checkpoint_candidates(self, universe, seen, *, limit):
        # One bounded ready page, not a long-lived snapshot or execution grant.
        # Each writer rechecks its generation/status/time before committing.
        # New successors have later availability/ready order and join after
        # this page; visited retry generations cannot hide an independent key.
        with self._conn(readonly=True, serialized=True) as c:
            c.execute("BEGIN")
            self._check_world_tx(c, universe)
            now = time.time()
            rows = c.execute("""SELECT checkpoint_key,generation FROM world_checkpoints
                WHERE universe=? AND status='pending' AND available_at<=? AND due_at<=?
                ORDER BY available_at,ready_order,checkpoint_key LIMIT ?""", (universe, now, now, limit + len(seen))).fetchall()
            candidates = [(row["checkpoint_key"], row["generation"]) for row in rows]
            return [candidate for candidate in candidates if candidate not in seen][:limit]

    def _run_one_checkpoint(self, universe, checkpoint_key, generation):
        with self._conn(serialized=True) as c:
            c.execute("BEGIN IMMEDIATE")
            self._check_world_tx(c, universe)
            now = time.time()
            row = c.execute("""SELECT * FROM world_checkpoints
                WHERE universe=? AND checkpoint_key=? AND generation=? AND status='pending'
                AND available_at<=? AND due_at<=?""", (universe, checkpoint_key, generation, now, now)).fetchone()
            if row is None:
                return None
            definition = self._worlds[universe]
            spec = next((spec for spec in definition.checkpoints if spec.name == row["handler"]), None)
            operation_id = "checkpoint:" + self._sha256({"universe": universe, "key": checkpoint_key,
                                                       "generation": generation})
            marker = self._journal_marker_tx(c)
            attempt = row["attempts"] + 1
            if (spec is None or row["world_id"] != definition.world_id or row["world_version"] != definition.version
                    or row["handler_version"] != spec.version):
                return self._checkpoint_failure_tx(c, row, marker, "blocked", "CheckpointVersionMismatch",
                                                   operation_id, attempt=row["attempts"], started_at=now)
            ctx = self._context_tx(c, universe, CHECKPOINT_ACTOR, "checkpoint:" + spec.name, spec.version,
                                   access="write", operation_id=operation_id, now=now)
            ctx.checkpoint = CheckpointInvocation(checkpoint_key, generation, row["due_at"], row["created_by"],
                row["created_operation_id"], row["created_at"], attempt)
            arguments = json.loads(row["arguments_json"])
            c.execute("SAVEPOINT checkpoint_effects")
            try:
                validate(spec.input_schema, arguments)
                if spec.authorize is not None:
                    ctx.access = "read"
                    try:
                        allowed = self._run_user_code(c, spec.authorize, ctx, json.loads(row["arguments_json"]), read_only=True)
                    finally:
                        ctx.access = "write"
                    if allowed is not True:
                        raise PermissionDenied("checkpoint precondition is no longer authorized")
                outcome = self._run_user_code(c, spec.handler, ctx, arguments)
                self._validate_outcome(outcome, {"output_schema": spec.output_schema})
                receipt = self._commit_outcome_tx(c, ctx, outcome, json.loads(row["arguments_json"]),
                    journal_marker=marker, source="checkpoint", record_operation=False,
                    extra_receipt={"checkpoint_key": checkpoint_key, "generation": generation,
                                   "due_at": row["due_at"], "attempt": attempt, "scheduled_by": row["created_by"]})
                # The handler may have atomically scheduled this key's successor.
                # Completing generation N must never erase pending generation N+1.
                c.execute("""UPDATE world_checkpoints SET completed_through=MAX(completed_through,?),
                    latest_completed_generation=?,latest_receipt_json=?,latest_commit_seq=?,
                    status=CASE WHEN generation=? THEN 'completed' ELSE status END,
                    attempts=CASE WHEN generation=? THEN ? ELSE attempts END,
                    completed_at=CASE WHEN generation=? THEN ? ELSE completed_at END,
                    last_error_code=CASE WHEN generation=? THEN NULL ELSE last_error_code END
                    WHERE universe=? AND checkpoint_key=?""",
                    (generation, generation, json_text(receipt), receipt["commit_seq"], generation,
                     generation, attempt, generation, time.time(), generation, universe, checkpoint_key))
                c.execute("RELEASE checkpoint_effects")
                return {"checkpoint_key": checkpoint_key, "generation": generation,
                        "status": "completed", "attempt": attempt,
                        "ready_lag_seconds": max(0, now - row["available_at"]),
                        **self._checkpoint_work_tx(c, receipt["commit_seq"],
                                                  len(receipt.get("stream_event_ids", ())))}
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                storage_codes = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL,
                    sqlite3.SQLITE_IOERR, sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_INTERRUPT,
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY}
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                if not c.in_transaction or (isinstance(exc, sqlite3.DatabaseError) and code in storage_codes):
                    raise
                c.execute("ROLLBACK TO checkpoint_effects")
                c.execute("RELEASE checkpoint_effects")
                if isinstance(exc, RetryTimer):
                    status = "pending" if attempt < row["max_attempts"] else "failed"
                    code = "RetryTimer" if status == "pending" else "CheckpointAttemptsExhausted"
                elif isinstance(exc, (PermissionDenied, RuleViolation)):
                    status, code = "rejected", type(exc).__name__
                elif isinstance(exc, (SchemaRejected, InvalidArguments, CheckpointConflict, WorldDefinitionError)):
                    status, code = "failed", type(exc).__name__
                else:
                    status, code = "failed", "CheckpointHandlerError"
                return self._checkpoint_failure_tx(c, row, marker, status, code, operation_id,
                                                   attempt=attempt, started_at=now)

    def _checkpoint_failure_tx(self, c, row, marker, status, code, operation_id, *, attempt, started_at):
        now = time.time()
        delay = min(86400, row["retry_delay"] * (2 ** max(0, attempt - 1)))
        commit_seq = self._record_commit_tx(c, row["universe"], marker, actor_role_id=CHECKPOINT_ACTOR,
            function_id="checkpoint:" + row["handler"], operation_id=operation_id,
            source="checkpoint", world_version=self._worlds[row["universe"]].version)
        c.execute("""UPDATE world_checkpoints SET status=?,attempts=?,available_at=?,last_error_code=?,ready_order=?,
            completed_at=?,completed_through=CASE WHEN ?='pending' THEN completed_through
                ELSE MAX(completed_through,generation) END WHERE universe=? AND checkpoint_key=? AND generation=?""",
            (status, attempt, now + delay if status == "pending" else row["available_at"], code, commit_seq,
             None if status == "pending" else now, status, row["universe"], row["checkpoint_key"], row["generation"]))
        if status != "pending":
            receipt = {"commit_seq": commit_seq, "ok": False, "operation_id": operation_id,
                "actor_role_id": CHECKPOINT_ACTOR, "credential_id": None,
                "function_id": "checkpoint:" + row["handler"], "function_version": row["handler_version"],
                "checkpoint_key": row["checkpoint_key"], "generation": row["generation"],
                "due_at": row["due_at"], "attempt": attempt, "scheduled_by": row["created_by"],
                "result": {"status": status, "error_code": code}, "event_seqs": [], "replayed": False}
            c.execute("""UPDATE world_checkpoints SET latest_completed_generation=?,latest_receipt_json=?,
                latest_commit_seq=? WHERE universe=? AND checkpoint_key=? AND generation=?""",
                (row["generation"], json_text(receipt), commit_seq, row["universe"], row["checkpoint_key"], row["generation"]))
        return {"checkpoint_key": row["checkpoint_key"], "generation": row["generation"],
                "status": status, "attempt": attempt, "error_code": code,
                "ready_lag_seconds": max(0, started_at - row["available_at"]),
                **self._checkpoint_work_tx(c, commit_seq, 0)}

    @staticmethod
    def _checkpoint_work_tx(c, commit_seq, stream_events):
        changes = c.execute("SELECT change_count FROM world_commits WHERE seq=?", (commit_seq,)).fetchone()[0]
        return {"commit_seq": commit_seq, "change_count": changes, "stream_event_count": stream_events}
