"""Current-task fencing, atomic continuation and exact receipt retention."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_world import (
    WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, StateRule,
    TimerSpec, CheckpointSpec, RetryTimer,
)
from agent_world.errors import (
    CheckpointConflict, CheckpointNotFound, CheckpointReceiptExpired,
    PermissionDenied, StateConflict, RuleViolation, WorldRuntimeError,
)
from agent_world.world_sdk import install_world
from agent_world.timer_worker import TimerWorker

ARGS = {"type": "object"}


class CheckpointMaintenanceTests(unittest.TestCase):
    def worker(self, *, enabled=True):
        runtime = Mock()
        runtime._worlds = {"u": SimpleNamespace(checkpoints=("work",) if enabled else ())}
        runtime.apply_work_retention.return_value = {"stream_backlog": False, "checkpoint_backlog": False,
            "stream_after": None, "stream_events_scanned": 0, "checkpoint_commits_scanned": 0,
            "checkpoint_changes_removed": 0}
        return runtime, TimerWorker(runtime, "u")

    def test_active_sweep_reclaims_in_proportion_to_actual_commits_and_changes(self):
        runtime, worker = self.worker()
        worker._maintain_checkpoint_history({"commits": 25, "changes": 12800, "stream_events": 75})
        kwargs = runtime.apply_work_retention.call_args.kwargs
        self.assertEqual((kwargs["limit"], kwargs["change_limit"], kwargs["stream_limit"]), (50, 25600, 150))
        runtime.apply_work_retention.assert_called_once()
        runtime.apply_stream_retention.assert_not_called()
        runtime.apply_checkpoint_retention.assert_not_called()

    def test_small_continuous_production_accumulates_before_a_maintenance_batch(self):
        runtime, worker = self.worker()
        with patch("time.monotonic", return_value=100):
            worker._next_checkpoint_retention = 130
            for _ in range(31):
                worker._maintain_checkpoint_history({"commits": 1, "changes": 3})
            runtime.apply_work_retention.assert_not_called()
            worker._maintain_checkpoint_history({"commits": 1, "changes": 3})
        self.assertEqual(runtime.apply_work_retention.call_args.kwargs["limit"], 64)
        self.assertEqual(worker._maintenance_credit, {"commits": 0, "changes": 0, "stream_events": 0})

    def test_idle_retention_is_bounded_and_only_runs_at_the_slow_cadence(self):
        runtime, worker = self.worker()
        worker._next_checkpoint_retention = 130
        with patch("time.monotonic", return_value=100):
            worker._maintain_checkpoint_history({})
        runtime.apply_work_retention.assert_not_called()
        with patch("time.monotonic", return_value=130):
            worker._maintain_checkpoint_history({})
        runtime.apply_work_retention.assert_called_once_with("u", stream_limit=8, limit=8,
            change_limit=4096, time_budget=0.025, stream_after=None)
        self.assertEqual(worker._next_checkpoint_retention, 160)

    def test_world_without_checkpoints_does_not_opt_into_new_history_retention(self):
        runtime, worker = self.worker(enabled=False)
        worker._next_checkpoint_retention = 0
        worker._maintain_checkpoint_history({"commits": 1, "changes": 1})
        runtime.apply_work_retention.assert_not_called()

    def test_stream_cleanup_keeps_up_with_the_maximum_legal_event_production(self):
        runtime, worker = self.worker()
        base = runtime.apply_work_retention.return_value
        runtime.apply_work_retention.side_effect = [
            {**base, "stream_backlog": True, "stream_events_scanned": 5000} for _ in range(5)
        ] + [{**base, "stream_events_scanned": 600}]
        with patch("time.monotonic", return_value=100):
            worker._maintain_checkpoint_history({"commits": 100, "changes": 51200, "stream_events": 12800})
        self.assertEqual([call.kwargs["stream_limit"] for call in runtime.apply_work_retention.call_args_list],
                         [5000, 5000, 5000, 5000, 5000, 600])
        self.assertEqual(worker._maintenance_credit["stream_events"], 0)

    def test_elapsed_budget_carries_unspent_credit_and_stream_cursor_into_next_pass(self):
        runtime, worker = self.worker()
        runtime.apply_work_retention.return_value = {"stream_backlog": True, "checkpoint_backlog": True,
            "stream_after": "second", "stream_events_scanned": 4, "checkpoint_commits_scanned": 2,
            "checkpoint_changes_removed": 10}
        with patch("time.monotonic", side_effect=[100, 100, 100.1, 100.1]):
            worker._maintain_checkpoint_history({"commits": 40, "changes": 100, "stream_events": 300})
        self.assertEqual(worker._maintenance_credit, {"commits": 78, "changes": 190, "stream_events": 596})
        self.assertTrue(worker._maintenance_backlog)
        runtime.apply_work_retention.return_value = {"stream_backlog": False, "checkpoint_backlog": False,
            "stream_after": "first", "stream_events_scanned": 1, "checkpoint_commits_scanned": 1,
            "checkpoint_changes_removed": 1}
        with patch("time.monotonic", return_value=101):
            worker._maintain_checkpoint_history({})
        self.assertEqual(runtime.apply_work_retention.call_args.kwargs["stream_after"], "second")
        self.assertFalse(worker._maintenance_backlog)

    def test_failed_maintenance_does_not_spend_credit_or_advance_cursor(self):
        runtime, worker = self.worker()
        runtime.apply_work_retention.side_effect = RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            worker._maintain_checkpoint_history({"commits": 40, "changes": 100})
        self.assertEqual(worker._maintenance_credit["commits"], 80)
        self.assertIsNone(worker._maintenance_stream_after)

    def test_ready_queue_gets_another_batch_without_a_full_poll_delay(self):
        runtime, worker = self.worker(enabled=False)
        runtime.run_due_timers.side_effect = [{"count": 1}, {"count": 8}]
        runtime.next_due_work.side_effect = [1000, 1001]
        with patch("time.time", return_value=1000), patch("time.monotonic", return_value=100):
            self.assertEqual(worker._run_ready_slice(), 1001)
        self.assertEqual(runtime.run_due_timers.call_count, 2)
        self.assertEqual([call.kwargs["limit"] for call in runtime.run_due_timers.call_args_list], [1, 25])
        self.assertNotIn("deadline", runtime.run_due_timers.call_args_list[0].kwargs)
        self.assertIn("deadline", runtime.run_due_timers.call_args_list[1].kwargs)

    def test_ready_time_slice_has_both_work_count_and_elapsed_time_bounds(self):
        runtime, worker = self.worker(enabled=False)
        runtime.run_due_timers.return_value = {"count": 25}
        runtime.next_due_work.return_value = 1000
        with patch("time.time", return_value=1000), patch("time.monotonic", return_value=100):
            worker._run_ready_slice()
        self.assertEqual(runtime.run_due_timers.call_count, 5)
        self.assertEqual(sum(call.kwargs["limit"] for call in runtime.run_due_timers.call_args_list), 100)
        runtime.reset_mock()
        with patch("time.time", return_value=1000), patch("time.monotonic", side_effect=[100, 100.2]):
            worker._run_ready_slice()
        self.assertEqual(runtime.run_due_timers.call_count, 1)


def arm(ctx, args):
    generation = ctx.schedule_checkpoint(args["key"], "work", args,
        due_at=args.get("due", 0), expected_generation=args.get("expected"))
    if args.get("rollback"):
        raise RuleViolation("ROLLBACK_SCHEDULE_SECRET")
    return FunctionOutcome({"generation": generation, "current": ctx.get_checkpoint(args["key"])})


def cancel(ctx, args):
    ctx.cancel_checkpoint(args["key"], expected_generation=args.get("expected"))
    return FunctionOutcome({"current": ctx.get_checkpoint(args["key"])})


def work(ctx, args):
    key = args["key"]
    value = ctx.get_state("values", key, 0) + 1
    ctx.set_state("values", key, value)
    for child in args.get("children", []):
        ctx.schedule_checkpoint(child, "work", {"key": child}, due_at=ctx.now, expected_generation=0)
    if args.get("cancel_self"):
        ctx.cancel_checkpoint(key, expected_generation=ctx.checkpoint.generation)
    if args.get("fail") == "rule":
        raise RuleViolation("RULE_REJECTION_SECRET")
    if args.get("fail") == "handler":
        raise RuntimeError("HANDLER_SECRET")
    if args.get("fail") == "busy":
        error = sqlite3.OperationalError("storage busy")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error
    if ctx.checkpoint.attempt <= args.get("retry", 0):
        raise RetryTimer("RETRY_SECRET")
    remaining = args.get("remaining", 1)
    if remaining > 1:
        ctx.schedule_checkpoint(key, "work", {**args, "remaining": remaining - 1}, due_at=ctx.now,
                                expected_generation=ctx.checkpoint.generation)
    return FunctionOutcome({"value": value, "generation": ctx.checkpoint.generation})


def arm_once(ctx, args):
    ctx.schedule_timer(args["key"], "once", args, due_at=0)
    return FunctionOutcome({})


def once(ctx, args):
    ctx.set_state("values", args["key"], ctx.get_state("values", args["key"], 0) + 1)
    if args.get("child"):
        ctx.schedule_timer(args["child"], "once", {"key": args["child"]}, due_at=0)
    return FunctionOutcome({})


def state_action(ctx, args):
    scope, key = args.get("scope", "facts"), args["key"]
    action = args["action"]
    if action == "put":
        version = ctx.set_state(scope, key, {"value": 1})
    elif action == "delete":
        version = ctx.delete_state(scope, key, expected_version=args["version"])
    elif action == "forget":
        version = ctx.forget_state(scope, key, expected_version=args["version"])
    elif action == "raw_delete":
        ctx._conn.execute("DELETE FROM world_state WHERE universe=? AND scope=? AND state_key=?",
                          (ctx.universe, scope, key))
        version = 0
    else:
        raise RuleViolation("unknown state action")
    return FunctionOutcome({"version": version})


def may_forget(ctx, scope, key):
    return scope == "facts" and key.isdigit() and 0 < int(key) <= 10


def resume_world(ctx):
    ctx.set_state("values", "resumptions", ctx.get_state("values", "resumptions", 0) + 1)


WORLD = WorldDefinition("checkpoint-test", "Checkpoint Test", (
    FunctionSpec("arm", arm, ARGS), FunctionSpec("cancel", cancel, ARGS),
    FunctionSpec("arm_once", arm_once, ARGS), FunctionSpec("state", state_action, ARGS),
), state_rules=(StateRule("values", "", {"type": "integer"}), StateRule("facts", "", ARGS)),
    timers=(TimerSpec("once", once, ARGS),),
    checkpoints=(CheckpointSpec("work", work, ARGS),), state_forget_authorizer=may_forget)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        self.clock = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.runtime = WorldRuntime(self.path)
        install_world(self.runtime, "u", WORLD)
        self.role = self.runtime.create_role("Author")["role_id"]
        self.token = self.runtime.issue_identity_token("u", self.role)["token"]
        self.operation = 0

    def call(self, function, arguments, *, operation_id=None):
        self.operation += 1
        return self.runtime.call_function("u", function, self.role, arguments,
            operation_id=operation_id or "op-" + str(self.operation), identity_token=self.token)

    def arm(self, key="current", **extra):
        return self.call("arm", {"key": key, **extra})

    def count(self, table):
        with self.runtime._conn(readonly=True) as c:
            return c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def value(self, key):
        return self.runtime.get_state("u", "values", key)["value"]

    def test_due_and_exact_receipt_without_timer_or_operation_growth(self):
        created = self.arm(due=1001)
        current = created["result"]["current"]
        self.assertEqual((current["generation"], current["handler_version"], current["status"]), (1, 1, "pending"))
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 0)

        self.clock.return_value = 1001
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 1)
        self.assertEqual(self.value("current"), 1)
        receipt = self.runtime.get_checkpoint_receipt("u", "current", generation=1)
        self.assertEqual(receipt["result"], {"value": 1, "generation": 1})
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(self.count("world_timers"), 0)
        self.assertEqual(self.count("timer_transitions"), 0)
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 0)

    def test_worker_wake_time_reads_the_two_indexed_queue_heads(self):
        self.arm(due=1004)
        self.assertEqual(self.runtime.next_due_work("u"), 1004)
        self.call("arm_once", {"key": "once"})
        self.assertEqual(self.runtime.next_due_work("u"), 0)
        self.runtime.run_due_timers("u", limit=1)
        self.assertEqual(self.runtime.next_due_work("u"), 1004)
        self.call("cancel", {"key": "current", "expected": 1})
        self.assertIsNone(self.runtime.next_due_work("u"))

    def test_ordinary_operation_replay_does_not_reschedule_a_generation(self):
        args = {"key": "current", "remaining": 3}
        first = self.call("arm", args, operation_id="stable")
        again = self.call("arm", args, operation_id="stable")
        self.assertTrue(again["replayed"])
        self.assertEqual(first["commit_seq"], again["commit_seq"])
        self.assertEqual(self.runtime.get_checkpoint("u", "current")["generation"], 1)

    def test_staged_creation_rolls_back_with_rejected_action(self):
        before = self.count("world_commits")
        with self.assertRaises(RuleViolation):
            self.arm(rollback=True)
        with self.assertRaises(CheckpointNotFound):
            self.runtime.get_checkpoint("u", "current")
        self.assertEqual(self.count("world_commits"), before)

    def test_same_key_pipeline_is_bounded_and_retains_only_latest_exact_receipt(self):
        self.arm(remaining=7)
        self.assertEqual(self.runtime.run_due_timers("u", limit=3)["count"], 3)
        current = self.runtime.get_checkpoint("u", "current")
        self.assertEqual((current["generation"], current["completed_through"], current["status"]), (4, 3, "pending"))
        self.assertEqual(self.value("current"), 3)
        latest = self.runtime.get_checkpoint_receipt("u", "current", generation=3)
        with self.assertRaises(CheckpointReceiptExpired):
            self.runtime.get_checkpoint_receipt("u", "current", generation=1)
        self.assertEqual(self.runtime._run_one_checkpoint("u", "current", 1), None)
        self.assertEqual(self.runtime.run_due_timers("u", limit=10)["count"], 4)
        self.assertEqual(self.value("current"), 7)
        self.assertEqual(self.count("world_checkpoints"), 1)
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(latest["result"]["value"], 3)

    def test_successor_cannot_starve_an_already_ready_independent_key(self):
        self.arm("a", remaining=100)
        self.arm("b")
        result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual([row["checkpoint_key"] for row in result["processed"]], ["a", "b"])
        self.assertEqual((self.value("a"), self.value("b")), (1, 1))
        self.assertEqual(result["checkpoint_work"], {"commits": 2, "changes": 2, "stream_events": 0})
        self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(self.value("a"), 3)

    def test_ready_page_uses_one_reader_for_eight_independent_logical_results(self):
        keys = ["work-" + str(index) for index in range(8)]
        for key in keys:
            self.arm(key)
        with patch.object(self.runtime, "_due_checkpoint_candidates",
                          wraps=self.runtime._due_checkpoint_candidates) as pages:
            result = self.runtime.run_due_timers("u", limit=8)
        self.assertEqual(pages.call_count, 1)
        self.assertEqual([row["checkpoint_key"] for row in result["processed"]], keys)
        self.assertEqual([self.value(key) for key in keys], [1] * 8)
        self.assertEqual(len({row["commit_seq"] for row in result["processed"]}), 8)

    def test_children_join_after_cached_ready_keys_without_waiting_for_a_new_sweep(self):
        self.arm("parent", children=["a-child", "b-child"])
        self.arm("waiting")
        result = self.runtime.run_due_timers("u", limit=4)
        self.assertEqual([row["checkpoint_key"] for row in result["processed"]],
                         ["parent", "waiting", "a-child", "b-child"])
        self.assertEqual([self.value(key) for key in ("parent", "waiting", "a-child", "b-child")], [1] * 4)

    def interleave_after_first_checkpoint(self, mutation):
        for key in ("a", "b", "c"):
            self.arm(key)
        execute = self.runtime._run_one_checkpoint

        def interleaved(universe, key, generation):
            item = execute(universe, key, generation)
            if key == "a":
                # An ordinary client edits the already-read candidate after
                # the first writer commits, before the next writer claims it.
                mutation()
            return item

        # Force a physical boundary where this concurrency regression inserts
        # the client mutation. Batching itself is covered by batch regressions.
        with patch("agent_world.runtime_timers.CHECKPOINT_BATCH_ITEMS", 1), \
                patch.object(self.runtime, "_run_one_checkpoint", side_effect=interleaved):
            return self.runtime.run_due_timers("u", limit=8)

    def test_cancelled_cached_generation_never_runs_or_claims_completion(self):
        result = self.interleave_after_first_checkpoint(
            lambda: self.call("cancel", {"key": "b", "expected": 1}))
        self.assertEqual([row["checkpoint_key"] for row in result["processed"]], ["a", "c"])
        self.assertFalse(self.runtime.get_state("u", "values", "b")["exists"])
        self.assertEqual(self.runtime.get_checkpoint("u", "b")["status"], "cancelled")

    def test_replaced_cached_generation_joins_later_page_and_executes_once(self):
        result = self.interleave_after_first_checkpoint(lambda: self.arm("b", expected=1))
        self.assertEqual([(row["checkpoint_key"], row["generation"]) for row in result["processed"]],
                         [("a", 1), ("c", 1), ("b", 2)])
        self.assertEqual(self.value("b"), 1)
        with self.assertRaises(CheckpointReceiptExpired):
            self.runtime.get_checkpoint_receipt("u", "b", generation=1)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "b", generation=2)["result"]["value"], 1)

    def test_postponed_cached_generation_stays_pending_until_its_actual_due_time(self):
        result = self.interleave_after_first_checkpoint(lambda: self.arm("b", expected=1, due=1005))
        self.assertEqual([row["checkpoint_key"] for row in result["processed"]], ["a", "c"])
        self.assertFalse(self.runtime.get_state("u", "values", "b")["exists"])
        checkpoint = self.runtime.get_checkpoint("u", "b")
        self.assertEqual((checkpoint["status"], checkpoint["generation"]), ("pending", 2))
        self.clock.return_value = 1005
        self.assertEqual(self.runtime.run_due_timers("u", limit=8)["count"], 1)
        self.assertEqual(self.value("b"), 1)

    def test_two_workers_with_the_same_candidate_page_commit_each_generation_once(self):
        keys = ["work-" + str(index) for index in range(8)]
        for key in keys:
            self.arm(key)
        other = WorldRuntime(self.path)
        install_world(other, "u", WORLD)
        barrier = threading.Barrier(2)

        def synchronized_page(read):
            def fetch(*args, **kwargs):
                candidates = read(*args, **kwargs)
                # Both read snapshots finish before either writer starts;
                # connections and Runtime locks are already released here.
                barrier.wait(timeout=5)
                return candidates
            return fetch

        with patch.object(self.runtime, "_due_checkpoint_candidates",
                          side_effect=synchronized_page(self.runtime._due_checkpoint_candidates)), \
             patch.object(other, "_due_checkpoint_candidates",
                          side_effect=synchronized_page(other._due_checkpoint_candidates)), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda runtime: runtime.run_due_timers("u", limit=8), (self.runtime, other)))
        processed = [item for result in results for item in result["processed"]]
        self.assertEqual(len(processed), 8)
        self.assertEqual({(item["checkpoint_key"], item["generation"]) for item in processed},
                         {(key, 1) for key in keys})
        self.assertEqual([self.value(key) for key in keys], [1] * 8)

    def test_retry_workload_counts_commits_but_not_rolled_back_changes(self):
        self.arm("a", retry=2)
        result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(result["checkpoint_work"], {"commits": 1, "changes": 0, "stream_events": 0})
        item = result["processed"][0]
        self.assertEqual(item["change_count"], 0)
        self.assertGreater(item["commit_seq"], 0)
        self.assertFalse(self.runtime.get_state("u", "values", "a")["exists"])

    def test_worker_deadline_yields_between_tasks_without_losing_pending_work(self):
        self.arm("a")
        self.arm("b")
        clock = [100.0]
        original = self.runtime._run_one_checkpoint_tx
        def advance_after_item(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 100.2
            return result
        with patch("time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.runtime, "_run_one_checkpoint_tx", side_effect=advance_after_item):
            result = self.runtime.run_due_timers("u", limit=25, deadline=100.1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.runtime.get_checkpoint("u", "b")["status"], "pending")
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 1)
        self.assertEqual((self.value("a"), self.value("b")), (1, 1))

    def test_worker_selection_wait_cannot_use_up_every_atomic_work_opportunity(self):
        self.arm("a")
        self.arm("b")
        clock = [100.0]
        worker = TimerWorker(self.runtime, "u")
        original = self.runtime._conn
        @contextmanager
        def delayed_select(**kwargs):
            with original(**kwargs) as connection:
                yield connection
            if kwargs.get("purpose") == "work-select":
                clock[0] = 100.2
        with patch("time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.runtime, "_conn", side_effect=delayed_select):
            worker._run_ready_slice()
        self.assertEqual(self.value("a"), 1)
        self.assertEqual(self.runtime.get_checkpoint("u", "b")["status"], "pending")
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 1)

    def test_generation_cas_replacement_cancel_and_restart_do_not_replay_old_work(self):
        self.arm(due=1005)
        with self.assertRaises(CheckpointConflict):
            self.arm(expected=0)
        self.arm(expected=1, remaining=2)
        self.runtime.run_due_timers("u", limit=1)
        self.assertEqual(self.value("current"), 1)
        resumed = WorldRuntime(self.path)
        install_world(resumed, "u", WORLD)
        self.runtime = resumed
        self.assertEqual(self.runtime._run_one_checkpoint("u", "current", 1), None)
        self.runtime.run_due_timers("u")
        self.assertEqual(self.value("current"), 2)
        generation = self.runtime.get_checkpoint("u", "current")["generation"]
        self.arm(expected=generation, due=1005)
        self.call("cancel", {"key": "current", "expected": generation + 1})
        self.clock.return_value = 1006
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 0)
        self.arm(expected=generation + 1)
        self.runtime.run_due_timers("u")
        self.assertEqual(self.value("current"), 3)

    def test_current_generation_cannot_cancel_itself(self):
        self.arm(cancel_self=True)
        result = self.runtime.run_due_timers("u")["processed"][0]
        self.assertEqual((result["status"], result["error_code"]), ("failed", "InvalidArguments"))
        self.assertFalse(self.runtime.get_state("u", "values", "current")["exists"])

    def test_bad_domain_does_not_rollback_independent_domain_or_leave_child(self):
        self.arm("a", fail="rule", children=["uncommitted"])
        self.arm("b")
        result = self.runtime.run_due_timers("u")
        self.assertEqual([item["status"] for item in result["processed"]], ["rejected", "completed"])
        self.assertEqual(self.value("b"), 1)
        with self.assertRaises(CheckpointNotFound):
            self.runtime.get_checkpoint("u", "uncommitted")
        self.assertFalse(self.runtime.get_state("u", "values", "a")["exists"])
        receipt = self.runtime.get_checkpoint_receipt("u", "a", generation=1)
        self.assertFalse(receipt["ok"])
        self.assertNotIn("SECRET", str(receipt))

    def test_ready_children_can_run_in_this_sweep_but_one_shot_children_cannot(self):
        self.call("arm_once", {"key": "once", "child": "once-child"})
        self.arm("parent", children=["child"])
        result = self.runtime.run_due_timers("u", limit=8)
        self.assertEqual(result["count"], 3)
        self.assertEqual(self.value("child"), 1)
        self.assertEqual(self.runtime.get_timer("u", "once-child")["status"], "pending")
        self.runtime.run_due_timers("u")
        self.assertEqual(self.value("once-child"), 1)

    def test_limit_one_alternates_one_shot_and_checkpoint_work(self):
        self.call("arm_once", {"key": "once"})
        self.arm("current", remaining=4)
        first = self.runtime.run_due_timers("u", limit=1)["processed"][0]
        second = self.runtime.run_due_timers("u", limit=1)["processed"][0]
        self.assertIn("timer_id", first)
        self.assertIn("checkpoint_key", second)

    def test_retry_rolls_back_effects_and_is_once_per_generation_per_sweep(self):
        tiny = replace(WORLD, checkpoints=(CheckpointSpec("work", work, ARGS,
                                                          retry_delay_seconds=1e-30),), version=2)
        install_world(self.runtime, "u", tiny)
        self.arm(retry=10)
        self.assertEqual(self.runtime.run_due_timers("u", limit=100)["count"], 1)
        self.assertEqual(self.runtime.get_checkpoint("u", "current")["attempts"], 1)
        self.runtime.run_due_timers("u", limit=100)
        self.runtime.run_due_timers("u", limit=100)
        current = self.runtime.get_checkpoint("u", "current")
        self.assertEqual((current["status"], current["attempts"]), ("failed", 3))
        self.assertFalse(self.runtime.get_state("u", "values", "current")["exists"])

    def test_storage_busy_keeps_generation_pending_without_consuming_attempt(self):
        self.arm(fail="busy")
        with self.assertRaises(sqlite3.OperationalError):
            self.runtime.run_due_timers("u")
        current = self.runtime.get_checkpoint("u", "current")
        self.assertEqual((current["status"], current["attempts"], current["completed_through"]), ("pending", 0, 0))

    def test_changed_world_blocks_old_generation_and_explicit_successor_recovers(self):
        self.arm()
        install_world(self.runtime, "u", replace(WORLD, version=2))
        result = self.runtime.run_due_timers("u")["processed"][0]
        self.assertEqual((result["status"], result["error_code"]), ("blocked", "CheckpointVersionMismatch"))
        self.arm(expected=1)
        self.runtime.run_due_timers("u")
        self.assertEqual(self.value("current"), 1)
        self.assertEqual(self.runtime._run_one_checkpoint("u", "current", 1), None)

    def test_resume_runs_once_on_first_existing_world_attachment_and_is_journaled(self):
        definition = replace(WORLD, version=2, resume=resume_world)
        install_world(self.runtime, "u", definition)
        self.assertFalse(self.runtime.get_state("u", "values", "resumptions")["exists"])
        restarted = WorldRuntime(self.path)
        install_world(restarted, "u", definition)
        self.assertEqual(restarted.get_state("u", "values", "resumptions")["value"], 1)
        install_world(restarted, "u", definition)
        self.assertEqual(restarted.get_state("u", "values", "resumptions")["value"], 1)
        with restarted._conn(readonly=True) as c:
            self.assertEqual(c.execute("SELECT source FROM world_commits ORDER BY seq DESC LIMIT 1").fetchone()[0], "resume")

    def test_new_initialize_does_not_also_invoke_the_resume_hook(self):
        definition = replace(WORLD, resume=resume_world)
        fresh = WorldRuntime(self.path.parent / "fresh.sqlite3")
        install_world(fresh, "fresh", definition)
        self.assertFalse(fresh.get_state("fresh", "values", "resumptions")["exists"])

    def test_failed_resume_rolls_back_state_and_scheduling_before_runtime_publication(self):
        def fail_resume(ctx):
            resume_world(ctx)
            ctx.schedule_checkpoint("resume-child", "work", {"key": "resume-child"}, due_at=ctx.now)
            raise RuleViolation("resume cannot finish")
        definition = replace(WORLD, version=2, resume=fail_resume)
        install_world(self.runtime, "u", definition)
        restarted = WorldRuntime(self.path)
        with self.assertRaises(RuleViolation):
            install_world(restarted, "u", definition)
        self.assertNotIn("u", restarted._worlds)
        self.assertFalse(restarted.get_state("u", "values", "resumptions")["exists"])
        with self.assertRaises(CheckpointNotFound):
            restarted.get_checkpoint("u", "resume-child")

    def test_migration_can_explicitly_mark_resumption_to_avoid_running_the_hook_twice(self):
        def migrate(ctx):
            resume_world(ctx)
            ctx.world_resumed = True
        definition = replace(WORLD, version=2, state_version=2, migrations={2: migrate}, resume=resume_world)
        restarted = WorldRuntime(self.path)
        install_world(restarted, "u", definition)
        self.assertEqual(restarted.get_state("u", "values", "resumptions")["value"], 1)

    def state(self, action, key="1", **extra):
        return self.call("state", {"action": action, "key": key, **extra})

    def test_reviewed_tombstone_can_be_forgotten_without_second_history_change(self):
        self.state("put")
        self.state("delete", version=1)
        before = self.count("state_changes")
        forgotten = self.state("forget", version=2)
        self.assertEqual(forgotten["result"]["version"], 2)
        self.assertEqual(self.count("state_changes"), before)
        with self.runtime._conn(readonly=True) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM world_state WHERE universe='u' AND scope='facts' AND state_key='1'").fetchone())
        with self.assertRaises(StateConflict):
            self.state("forget", version=2)

    def test_forgetting_live_unapproved_or_changed_tombstones_is_rejected(self):
        self.state("put")
        with self.assertRaises(PermissionDenied):
            self.state("forget", version=1)
        self.state("delete", version=1)
        with self.assertRaises(StateConflict):
            self.state("forget", version=1)
        self.state("put", key="11")
        self.state("delete", key="11", version=1)
        with self.assertRaises(PermissionDenied):
            self.state("forget", key="11", version=2)
        with self.assertRaises(WorldRuntimeError):
            self.state("raw_delete", version=2)

    def test_forget_authorizer_cannot_write_database_side_effects(self):
        self.state("put")
        self.state("delete", version=1)
        def bad_policy(ctx, scope, key):
            ctx._conn.execute("UPDATE world_state SET version=99 WHERE universe='u'")
            return True
        install_world(self.runtime, "u", replace(WORLD, version=2, state_forget_authorizer=bad_policy))
        with self.assertRaises(WorldRuntimeError):
            self.state("forget", version=2)
        record = self.runtime.get_state("u", "facts", "1")
        self.assertEqual(record["version"], 2)


if __name__ == "__main__":
    unittest.main()
