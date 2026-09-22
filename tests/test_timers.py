from __future__ import annotations

from dataclasses import replace
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, TimerSpec, RetryTimer
from agent_world.world_sdk import install_world
from agent_world.errors import WorldRuntimeError, RuleViolation, PermissionDenied, WorldVersionMismatch, TimerConflict
from agent_world.world_timers import TimerNotFound, TIMER_ACTOR

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ARGS = {"type": "object", "properties": {"id": {"type": "string"}, "due": {"type": "number"},
        "retry": {"type": "integer", "minimum": 0}, "fail": {"type": "boolean"},
        "rollback": {"type": "boolean"}}, "required": ["id", "due"], "additionalProperties": False}
TIMER_ARGS = {"type": "object", "properties": {"id": {"type": "string"}, "retry": {"type": "integer"},
              "fail": {"type": "boolean"}}, "required": ["id"], "additionalProperties": False}


def arm(ctx, args):
    ctx.set_state("items", args["id"], {"status": "active", "owner": ctx.actor_role_id})
    ctx.schedule_timer(args["id"], "finish", {"id": args["id"], "retry": args.get("retry", 0), "fail": args.get("fail", False)}, due_at=args["due"])
    if args.get("rollback"):
        raise RuleViolation("discard both state and timer")
    return FunctionOutcome({"timer_id": args["id"]})


def finish(ctx, args):
    value = ctx.get_state("items", args["id"], {"owner": "system:installer"})
    value["status"] = "done"
    value["actor"] = ctx.actor_role_id
    value["requested_by"] = ctx.timer.scheduled_by
    value["roll"] = ctx.random_int(1, 20)
    ctx.set_state("items", args["id"], value)
    if os.getenv("AGENT_WORLD_TEST_CRASH_TIMER") == "1":
        print("EXIT_INSIDE_UNCOMMITTED_RULE", flush=True)
        os._exit(71)
    if args.get("fail"):
        raise RuntimeError("DO_NOT_LEAK_HANDLER_SECRET")
    if ctx.timer.attempt <= args.get("retry", 0):
        raise RetryTimer("DO_NOT_LEAK_RETRY_SECRET")
    return FunctionOutcome({"id": args["id"], "value": value})


def cancel(ctx, args):
    ctx.cancel_timer(args["id"])
    if args.get("rollback"):
        raise RuleViolation("cancel rolled back")
    return FunctionOutcome({"id": args["id"]})


def can_finish(ctx, args):
    return ctx.get_state("policy", "enabled", True) is True


WORLD = WorldDefinition(
    "timers-test", "Timers Test",
    (FunctionSpec("item.arm", arm, ARGS), FunctionSpec("item.cancel", cancel, ARGS)),
    state_rules=(StateRule("items", "", {"type": "object"}), StateRule("policy", "", {"type": "boolean"})),
    timers=(TimerSpec("finish", finish, TIMER_ARGS, authorize=can_finish, max_attempts=3, retry_delay_seconds=1),),
)


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.db = Path(self.temp.name) / "timer.sqlite3"
        self.w = WorldRuntime(self.db)
        install_world(self.w, "u", WORLD)
        self.role = self.w.create_role("Player")["role_id"]
        self.identity = self.w.issue_identity_token("u", self.role)
        self.n = 0

    def arm(self, tid="one", due=1002, **extra):
        self.n += 1
        return self.w.call_function("u", "item.arm", self.role, {"id": tid, "due": due, **extra},
                                   operation_id="arm-" + str(self.n), identity_token=self.identity["token"])

    def cancel(self, tid="one", **extra):
        self.n += 1
        return self.w.call_function("u", "item.cancel", self.role, {"id": tid, "due": 0, **extra},
                                   operation_id="cancel-" + str(self.n), identity_token=self.identity["token"])

    def state(self, tid="one"):
        return self.w.get_state("u", "items", tid)["value"]

    def count(self, table):
        with self.w._conn(readonly=True) as c:
            return c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_not_before_due_and_once_after_due(self):
        self.arm()
        self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        self.clock.return_value = 1002
        self.assertEqual(self.w.run_due_timers("u")["count"], 1)
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        receipt = self.w.get_timer_receipt("u", "one")
        self.assertEqual(receipt["timer_id"], "one")
        self.assertEqual(len(receipt["random_draws"]), 1)
        self.assertEqual(self.count("operations"), 2)

    def test_world_state_and_timer_creation_roll_back_together(self):
        before = self.count("world_commits")
        with self.assertRaises(RuleViolation):
            self.arm(rollback=True)
        self.assertEqual(self.count("world_timers"), 0)
        self.assertEqual(self.count("timer_transitions"), 0)
        self.assertEqual(self.count("world_commits"), before)
        self.assertEqual(self.w.read_state_history("u")["changes"], [])

    def test_creation_retry_preserves_timer_and_commit(self):
        first = self.arm()
        again = self.w.call_function("u", "item.arm", self.role, {"id": "one", "due": 1002},
                                    operation_id="arm-1", identity_token=self.identity["token"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["commit_seq"], again["commit_seq"])
        self.assertEqual(self.count("world_timers"), 1)
        self.assertEqual(self.count("timer_transitions"), 1)

    def test_same_id_different_schedule_conflicts(self):
        self.arm()
        with self.assertRaises(TimerConflict):
            self.arm(due=1003)
        self.assertEqual(self.w.get_timer("u", "one")["due_at"], 1002)
        self.assertEqual(self.count("operations"), 1)

    def test_cancel_prevents_execution_and_is_not_undo(self):
        self.arm()
        self.cancel()
        self.clock.return_value = 1005
        self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        self.assertEqual(self.w.get_timer("u", "one")["status"], "cancelled")
        self.arm("other", due=1005)
        self.w.run_due_timers("u")
        self.cancel("other")
        self.assertEqual(self.w.get_timer("u", "other")["status"], "completed")

    def test_cancelled_timer_id_is_never_reactivated(self):
        self.arm()
        self.cancel()
        self.arm()
        self.assertEqual(self.w.get_timer("u", "one")["status"], "cancelled")

    def test_failed_cancellation_does_not_cancel_timer(self):
        self.arm()
        with self.assertRaises(RuleViolation):
            self.cancel(rollback=True)
        self.assertEqual(self.w.get_timer("u", "one")["status"], "pending")

    def test_no_identity_token_is_stored_in_timer(self):
        self.arm()
        with self.w._conn(readonly=True) as c:
            row = dict(c.execute("SELECT * FROM world_timers").fetchone())
        self.assertNotIn(self.identity["token"], json.dumps(row))
        self.assertEqual(row["created_by"], self.role)
        self.w.revoke_identity_token(self.identity["token_id"])
        self.w.set_role_status(self.role, "disabled")
        self.clock.return_value = 1005
        self.w.run_due_timers("u")
        self.assertEqual(self.state()["actor"], TIMER_ACTOR)
        self.assertEqual(self.state()["requested_by"], self.role)

    def test_timer_policy_is_checked_at_execution(self):
        self.arm()
        with self.w._conn() as c:
            c.execute("INSERT INTO world_state(universe,scope,state_key,value_json,version,updated_at) VALUES('u','policy','enabled','false',1,0)")
        self.clock.return_value = 1005
        row = self.w.run_due_timers("u")["processed"][0]
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(self.state()["status"], "active")
        self.assertIsNone(self.w.get_timer_receipt("u", "one"))

    def test_retry_rolls_back_effects_then_succeeds(self):
        self.arm(retry=1)
        self.clock.return_value = 1002
        self.w.run_due_timers("u")
        self.assertEqual(self.state()["status"], "active")
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 1)
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        self.clock.return_value = 1003
        self.w.run_due_timers("u")
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 2)
        self.assertEqual(self.count("operations"), 2)

    def test_retry_has_bounded_attempts_and_backoff(self):
        self.arm(retry=10)
        for now in (1002, 1003, 1005):
            self.clock.return_value = now
            self.w.run_due_timers("u")
        metadata = self.w.get_timer("u", "one")
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["attempts"], 3)
        self.assertEqual(metadata["last_error_code"], "TimerAttemptsExhausted")
        self.clock.return_value = 9999
        self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        self.assertEqual(self.state()["status"], "active")

    def test_unexpected_error_is_visible_but_never_leaks_message(self):
        self.arm(fail=True)
        self.clock.return_value = 1002
        result = self.w.run_due_timers("u")
        self.assertEqual(result["processed"][0]["error_code"], "TimerHandlerError")
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        self.assertEqual(self.state()["status"], "active")

    def test_restart_catches_up_without_agent_request(self):
        self.arm()
        self.clock.return_value = 2000
        other = WorldRuntime(self.db)
        install_world(other, "u", WORLD)
        other.run_due_timers("u")
        self.assertEqual(self.state()["status"], "done")

    def test_concurrent_workers_commit_once(self):
        self.arm()
        self.clock.return_value = 1005
        runtimes = [WorldRuntime(self.db) for _ in range(8)]
        for runtime in runtimes:
            install_world(runtime, "u", WORLD)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda runtime: runtime.run_due_timers("u"), runtimes))
        self.assertEqual(sum(r["count"] for r in results), 1)
        self.assertEqual(self.count("operations"), 2)
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 1)

    def test_process_death_rolls_back_then_restart_commits_once(self):
        self.arm(due=0)
        code = "print('BOOT',flush=True); from agent_world import WorldRuntime; from agent_world.world_sdk import install_world; from tests.test_timers import WORLD; import sys; w=WorldRuntime(sys.argv[1]); install_world(w,'u',WORLD); print('DISPATCH',flush=True); w.run_due_timers('u')"
        logfile = Path(self.temp.name) / "crash.log"
        with logfile.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.db)],
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "AGENT_WORLD_TEST_CRASH_TIMER": "1"},
                stdout=log, stderr=subprocess.STDOUT)
            try:
                proc.wait(timeout=120)
            finally:
                if proc.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
        output = logfile.read_text(encoding="utf-8")
        self.assertEqual(proc.returncode, 71, output)
        self.assertIn("EXIT_INSIDE_UNCOMMITTED_RULE", output)
        self.assertEqual(self.state()["status"], "active")
        self.assertEqual(self.w.get_timer("u", "one")["status"], "pending")
        self.w.run_due_timers("u")
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(self.count("operations"), 2)

    def test_version_change_blocks_old_timer_and_old_worker(self):
        self.arm()
        new = WorldRuntime(self.db)
        install_world(new, "u", replace(WORLD, version=2))
        self.clock.return_value = 1005
        with self.assertRaises(WorldVersionMismatch):
            self.w.run_due_timers("u")
        result = new.run_due_timers("u")["processed"][0]
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.state()["status"], "active")
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 0)

    def test_removed_timer_handler_never_executes(self):
        self.arm()
        install_world(self.w, "u", replace(WORLD, version=2, timers=()))
        self.clock.return_value = 1005
        self.assertEqual(self.w.run_due_timers("u")["processed"][0]["status"], "blocked")

    def test_batch_limit_and_stable_due_order(self):
        for tid, due in (("c", 1002), ("a", 1001), ("b", 1002)):
            self.arm(tid, due)
        self.clock.return_value = 1010
        result = self.w.run_due_timers("u", limit=2)
        self.assertEqual([r["timer_id"] for r in result["processed"]], ["a", "b"])
        self.assertEqual(self.w.run_due_timers("u")["processed"][0]["timer_id"], "c")

    def test_new_immediate_timer_waits_for_next_sweep(self):
        def chain(ctx, args):
            ctx.schedule_timer(ctx.timer.timer_id + "x", "finish", {"id": "child"}, due_at=0)
            return FunctionOutcome({})
        definition = replace(WORLD, timers=(TimerSpec("finish", chain, TIMER_ARGS),))
        install_world(self.w, "chain", definition)
        self.w.call_function("chain", "item.arm", self.role, {"id": "a", "due": 0}, operation_id="arm")
        result = self.w.run_due_timers("chain", limit=100)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.w.get_timer("chain", "ax")["status"], "pending")

    def test_initializer_can_schedule_and_migration_is_atomic(self):
        def initialize(ctx):
            ctx.schedule_timer("initial", "finish", {"id": "initial"}, due_at=1001)
        definition = replace(WORLD, initialize=initialize)
        install_world(self.w, "init", definition)
        install_world(self.w, "init", definition)
        self.clock.return_value = 1002
        self.assertEqual(self.w.run_due_timers("init")["count"], 1)
        def migration(ctx):
            ctx.schedule_timer("bad", "finish", {"id": "bad"}, due_at=0)
            raise RuleViolation("rollback")
        with self.assertRaises(RuleViolation):
            install_world(self.w, "init", replace(definition, version=2, state_version=2, migrations={2: migration}))
        with self.assertRaises(TimerNotFound):
            self.w.get_timer("init", "bad")

    def test_read_hook_cannot_schedule_timer(self):
        def read(ctx, args):
            ctx.schedule_timer("bad", "finish", {"id": "bad"}, due_at=0)
            return FunctionOutcome({})
        install_world(self.w, "u", replace(WORLD, version=2, functions=(FunctionSpec("item.read", read, EMPTY, access="read"),)))
        with self.assertRaises(WorldRuntimeError):
            self.w.call_function("u", "item.read", self.role, {})
        self.assertEqual(self.count("world_timers"), 0)

    def test_timer_handlers_are_not_callable_world_tools(self):
        self.assertNotIn("finish", {f["function_id"] for f in self.w.list_functions("u")})
        with self.assertRaises(WorldRuntimeError):
            self.w.call_function("u", "finish", self.role, {"id": "x"}, operation_id="bypass")

    def test_unknown_handler_and_nan_are_rejected(self):
        with self.assertRaises(WorldRuntimeError):
            self.arm(due=float("nan"))
        def unknown(ctx, args):
            ctx.schedule_timer("x", "missing", {}, due_at=0)
            return FunctionOutcome({})
        install_world(self.w, "u", replace(WORLD, version=2, functions=(FunctionSpec("bad", unknown, EMPTY),)))
        with self.assertRaises(WorldRuntimeError):
            self.w.call_function("u", "bad", self.role, {}, operation_id="bad")
        self.assertEqual(self.count("world_timers"), 0)

    def test_raw_queue_mutation_is_rejected(self):
        self.arm()
        def raw(ctx, args):
            ctx.conn.execute("DELETE FROM world_timers")
            return FunctionOutcome({})
        install_world(self.w, "u", replace(WORLD, version=2, functions=(FunctionSpec("bad", raw, EMPTY),)))
        with self.assertRaises(WorldRuntimeError):
            self.w.call_function("u", "bad", self.role, {}, operation_id="raw")
        self.assertEqual(self.count("world_timers"), 1)

    def test_cross_universe_cancellation_cannot_find_timer(self):
        self.arm()
        install_world(self.w, "other", WORLD)
        with self.assertRaises(TimerNotFound):
            self.w.call_function("other", "item.cancel", self.role, {"id": "one", "due": 0}, operation_id="cancel")
        self.assertEqual(self.w.get_timer("u", "one")["status"], "pending")

    def test_clock_rollback_does_not_fire_early(self):
        self.arm()
        self.clock.side_effect = [1002.0, 1001.0]
        try:
            self.assertEqual(self.w.run_due_timers("u")["count"], 0)
        finally:
            self.clock.side_effect = None
        self.assertEqual(self.w.get_timer("u", "one")["status"], "pending")

    def test_creation_and_terminal_transitions_reference_commits(self):
        created = self.arm()
        self.clock.return_value = 1005
        self.w.run_due_timers("u")
        receipt = self.w.get_timer_receipt("u", "one")
        with self.w._conn(readonly=True) as c:
            rows = c.execute("SELECT status,commit_seq FROM timer_transitions ORDER BY seq").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [("scheduled", created["commit_seq"]), ("completed", receipt["commit_seq"])])

    def test_followup_schedule_rolls_back_on_retry(self):
        def followup(ctx, args):
            ctx.schedule_timer("child", "finish", {"id": "child"}, due_at=0)
            raise RetryTimer()
        definition = replace(WORLD, timers=(TimerSpec("finish", followup, TIMER_ARGS),))
        install_world(self.w, "follow", definition)
        self.w.call_function("follow", "item.arm", self.role, {"id": "parent", "due": 0}, operation_id="arm")
        self.w.run_due_timers("follow")
        with self.assertRaises(TimerNotFound):
            self.w.get_timer("follow", "child")

    def test_rule_argument_normalization_cannot_change_receipt_intent(self):
        def normalize(ctx, args):
            args["added"] = "internal"
            return FunctionOutcome({})
        install_world(self.w, "u", replace(WORLD, version=2, functions=(FunctionSpec("normalize", normalize, EMPTY),)))
        first = self.w.call_function("u", "normalize", self.role, {}, operation_id="normalize")
        again = self.w.call_function("u", "normalize", self.role, {}, operation_id="normalize")
        self.assertTrue(again["replayed"])
        self.assertEqual(first["commit_seq"], again["commit_seq"])

    def test_tiny_retry_delay_cannot_repeat_in_the_same_sweep(self):
        install_world(self.w, "u", replace(WORLD, version=2, timers=(TimerSpec("finish", finish, TIMER_ARGS, retry_delay_seconds=1e-30),)))
        self.arm(due=0, retry=10)
        result = self.w.run_due_timers("u", limit=100)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 1)

    def test_bad_sql_in_one_handler_does_not_starve_other_timers(self):
        def bad_sql(ctx, args):
            if args.get("fail"):
                ctx.conn.execute("SELECT no_such_column FROM no_such_table")
            return finish(ctx, args)
        install_world(self.w, "u", replace(WORLD, version=2, timers=(TimerSpec("finish", bad_sql, TIMER_ARGS),)))
        self.arm("a", due=0, fail=True)
        self.arm("b", due=0)
        result = self.w.run_due_timers("u")
        self.assertEqual([r["status"] for r in result["processed"]], ["failed", "completed"])

    def test_storage_busy_keeps_timer_pending_without_consuming_attempt(self):
        def busy(ctx, args):
            exc = sqlite3.OperationalError("simulated storage busy")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc
        install_world(self.w, "u", replace(WORLD, version=2, timers=(TimerSpec("finish", busy, TIMER_ARGS),)))
        self.arm(due=0)
        with self.assertRaises(sqlite3.OperationalError):
            self.w.run_due_timers("u")
        self.assertEqual(self.w.get_timer("u", "one")["status"], "pending")
        self.assertEqual(self.w.get_timer("u", "one")["attempts"], 0)

    def test_timer_failure_does_not_prevent_next_due_item(self):
        self.arm("a", due=0, fail=True)
        self.arm("b", due=0)
        result = self.w.run_due_timers("u")
        self.assertEqual([r["status"] for r in result["processed"]], ["failed", "completed"])
        self.assertEqual(self.state("b")["status"], "done")


if __name__ == "__main__":
    unittest.main()
