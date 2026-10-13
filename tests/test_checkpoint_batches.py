"""Checkpoint durability groups preserve logical atomicity and recovery."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_world import (WorldRuntime, WorldDefinition, FunctionOutcome,
                         StateRule, CheckpointSpec, StreamSpec, StreamEvent)
from agent_world.errors import RuleViolation, ReceiptNotFound
from agent_world.timer_worker import TimerWorker


ARGS = {"type": "object"}


def advance(ctx, args):
    key = args["key"]
    old = ctx.get_state("values", key, {"n": 0})
    value = {"n": old["n"] + 1}
    if args.get("payload"):
        value["payload"] = args["payload"]
    ctx.set_state("values", key, value)
    if args.get("child"):
        ctx.schedule_checkpoint(args["child"], "work", {"key": args["child"]}, due_at=0)
    if args.get("reject"):
        raise RuleViolation("private rejection detail")
    return FunctionOutcome(value, events=(StreamEvent("shared", "advanced", {"n": value["n"]}),))


def world(entries, handler=advance, *, history="full"):
    def initialize(ctx):
        for key, arguments in entries:
            ctx.schedule_checkpoint(key, "work", {"key": key, **arguments}, due_at=0)
    return WorldDefinition("batches", "Batch durability", (), initialize=initialize,
        state_rules=(StateRule("values", "", {"type": "object"}, history=history),),
        checkpoints=(CheckpointSpec("work", handler, ARGS),),
        streams=(StreamSpec("shared", public=True, max_events=32),))


class CheckpointBatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.path)
        self.addCleanup(self.runtime.close)

    def install(self, entries, handler=advance):
        definition = world(entries, handler)
        self.runtime.install_world("u", definition)
        return definition

    def rows(self, table):
        with self.runtime._conn(readonly=True) as c:
            return [dict(row) for row in c.execute("SELECT * FROM " + table)]

    def value(self, key):
        return self.runtime.get_state("u", "values", key)

    def batch_totals(self):
        return self.runtime.work_diagnostics()["kinds"]["checkpoint-batch"]

    def test_eight_results_share_full_commit_but_keep_each_exact_receipt_and_journal(self):
        keys = ["key-" + str(i) for i in range(8)]
        self.install([(key, {}) for key in keys])
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=8)
        self.assertEqual(result["count"], 8)
        commits = {item["commit_seq"] for item in result["processed"]}
        self.assertEqual(len(commits), 8)
        for key, item in zip(keys, result["processed"]):
            receipt = self.runtime.get_checkpoint_receipt("u", key, generation=1)
            self.assertEqual(receipt["commit_seq"], item["commit_seq"])
            self.assertEqual(receipt["result"], {"n": 1})
        self.assertEqual({row["commit_seq"] for row in self.rows("state_changes")}, commits)
        self.assertEqual(self.batch_totals()["physical_commits"], 1)
        self.assertEqual(self.batch_totals()["logical_results_committed"], 8)

    def test_workers_first_opportunity_groups_ready_work_when_full_commit_spends_the_slice(self):
        keys = ["key-" + str(i) for i in range(8)]
        self.install([(key, {}) for key in keys])
        now = [100.0]
        run_batch = self.runtime._run_checkpoint_batch
        def slow_commit(*args, **kwargs):
            result = run_batch(*args, **kwargs)
            # Storage has already made the complete group durable. The delay
            # prevents a second worker call from hiding a one-item first call.
            now[0] = 100.2
            return result
        worker = TimerWorker(self.runtime, "u")
        with patch("time.monotonic", side_effect=lambda: now[0]), \
                patch.object(self.runtime, "_run_checkpoint_batch", side_effect=slow_commit):
            worker._run_ready_slice()
        self.assertEqual([self.value(key)["value"]["n"] for key in keys], [1] * 8)
        self.assertEqual(self.batch_totals()["physical_commits"], 1)
        self.assertEqual(self.batch_totals()["logical_results_committed"], 8)

    def test_bad_handler_rolls_back_its_child_and_effects_while_neighbor_commits(self):
        self.install([("bad", {"reject": True, "child": "absent"}), ("good", {})])
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual([item["status"] for item in result["processed"]], ["rejected", "completed"])
        self.assertFalse(self.value("bad")["exists"])
        self.assertEqual(self.value("good")["value"], {"n": 1})
        self.assertEqual({row["checkpoint_key"] for row in self.rows("world_checkpoints")}, {"bad", "good"})
        self.assertEqual(len(self.rows("stream_events")), 1)
        self.assertFalse(self.runtime.get_checkpoint_receipt("u", "bad")["ok"])
        self.assertEqual(self.batch_totals()["physical_commits"], 1)

    def test_earlier_item_receipt_state_and_publication_stay_invisible_before_outer_commit(self):
        observations = []
        def handler(ctx, args):
            if args["key"] == "b":
                observations.append(self.value("a")["exists"])
                observations.append(len(self.rows("stream_events")))
                with self.assertRaises(ReceiptNotFound):
                    self.runtime.get_checkpoint_receipt("u", "a", generation=1)
            return advance(ctx, args)
        self.install([("a", {}), ("b", {})], handler)
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(observations, [False, 0])
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "a")["result"], {"n": 1})
        self.assertEqual(len(self.rows("stream_events")), 2)

    def test_database_fault_after_first_item_aborts_whole_group_and_preserves_pending_generations(self):
        broken = [True]
        def handler(ctx, args):
            outcome = advance(ctx, args)
            if args["key"] == "b" and broken[0]:
                raise sqlite3.OperationalError("injected storage fault")
            return outcome
        self.install([("a", {}), ("b", {})], handler)
        before = self.rows("world_commits")
        with patch.object(self.runtime, "wake_waiters") as wake, \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.run_due_timers("u", limit=2)
            wake.assert_not_called()
        self.assertEqual(self.rows("world_commits"), before)
        self.assertEqual(self.rows("stream_events"), [])
        self.assertTrue(all(row["status"] == "pending" and row["attempts"] == 0
                            and row["completed_through"] == 0 for row in self.rows("world_checkpoints")))
        self.assertFalse(self.value("a")["exists"])
        broken[0] = False
        self.assertEqual(self.runtime.run_due_timers("u", limit=2)["count"], 2)
        self.assertEqual(self.value("a")["value"], {"n": 1})

    def test_base_exception_aborts_first_staged_success_instead_of_recording_handler_failure(self):
        def handler(ctx, args):
            outcome = advance(ctx, args)
            if args["key"] == "b":
                raise SystemExit("interrupt")
            return outcome
        self.install([("a", {}), ("b", {})], handler)
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60), \
                self.assertRaises(SystemExit):
            self.runtime.run_due_timers("u", limit=2)
        self.assertFalse(self.value("a")["exists"])
        self.assertTrue(all(row["status"] == "pending" for row in self.rows("world_checkpoints")))

    def test_true_readonly_storage_fault_is_not_wrapped_as_handler_rejection(self):
        def handler(ctx, args):
            outcome = advance(ctx, args)
            if args["key"] == "b":
                fault = sqlite3.OperationalError("attempt to write a readonly database")
                fault.sqlite_errorcode = sqlite3.SQLITE_READONLY
                raise fault
            return outcome
        self.install([("a", {}), ("b", {})], handler)
        before = self.rows("world_commits")
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60), \
                self.assertRaises(sqlite3.OperationalError) as failed:
            self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(failed.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        self.assertEqual(self.rows("world_commits"), before)
        self.assertEqual(self.rows("stream_events"), [])
        self.assertFalse(self.value("a")["exists"])
        self.assertTrue(all(row["status"] == "pending" and row["attempts"] == 0
                            for row in self.rows("world_checkpoints")))

    def commit_fault_runtime(self, *, after_commit):
        self.runtime.close()
        flag = {"enabled": False}
        class FaultConnection(sqlite3.Connection):
            def commit(self):
                if flag["enabled"] and not after_commit:
                    raise sqlite3.OperationalError("COMMIT did not complete")
                super().commit()
                if flag["enabled"]:
                    raise ConnectionError("response lost after actual COMMIT")
        original = sqlite3.connect
        def connect(*args, **kwargs):
            kwargs["factory"] = FaultConnection
            return original(*args, **kwargs)
        with patch("agent_world.runtime_core.sqlite3.connect", side_effect=connect):
            self.runtime = WorldRuntime(self.path)
            self.addCleanup(self.runtime.close)
            definition = self.install([("a", {}), ("b", {})])
        return flag, definition

    def test_outer_commit_fault_returns_no_results_notification_or_durable_effects(self):
        flag, _ = self.commit_fault_runtime(after_commit=False)
        flag["enabled"] = True
        with patch.object(self.runtime, "wake_waiters") as wake, \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.run_due_timers("u", limit=2)
            wake.assert_not_called()
        self.assertFalse(self.value("a")["exists"])
        self.assertEqual(self.rows("stream_events"), [])
        self.assertEqual(self.batch_totals()["physical_commits"], 0)
        self.assertEqual(self.batch_totals()["physical_commit_attempts"], 1)
        self.assertEqual(self.batch_totals()["logical_results_committed"], 0)
        self.assertEqual(self.runtime.work_diagnostics()["recent"][-1]["batch_status"], "rolled_back")

    def test_response_loss_after_full_commit_recovers_original_receipts_without_reexecution(self):
        flag, definition = self.commit_fault_runtime(after_commit=True)
        flag["enabled"] = True
        with patch.object(self.runtime, "wake_waiters") as wake, \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            with self.assertRaises(ConnectionError):
                self.runtime.run_due_timers("u", limit=2)
            wake.assert_not_called()
        receipts = [self.runtime.get_checkpoint_receipt("u", key) for key in ("a", "b")]
        self.assertEqual(self.runtime.work_diagnostics()["recent"][-1]["batch_status"], "unknown")
        flag["enabled"] = False
        self.runtime.close()
        resumed = WorldRuntime(self.path)
        self.addCleanup(resumed.close)
        resumed.install_world("u", definition)
        self.assertEqual(resumed.run_due_timers("u")["count"], 0)
        self.assertEqual([resumed.get_checkpoint_receipt("u", key) for key in ("a", "b")], receipts)
        self.assertEqual(resumed.get_state("u", "values", "a")["value"], {"n": 1})

    def test_aggregate_history_bound_defers_whole_item_with_its_original_generation(self):
        self.install([(key, {"payload": "x" * 60}) for key in ("a", "b")])
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_HISTORY_BYTES", 120), \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual([item["generation"] for item in result["processed"]], [1, 1])
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertEqual(len(self.rows("stream_events")), 2)
        self.assertEqual([self.value(key)["value"]["n"] for key in ("a", "b")], [1, 1])

    def test_large_legal_item_exceeding_physical_byte_policy_commits_alone_then_neighbor_progresses(self):
        def handler(ctx, args):
            writes = 8 if args["key"] == "large" else 1
            for index in range(writes):
                ctx.set_state("values", args["key"] + "-" + str(index),
                              {"n": 1, "payload": "x" * 65536})
            return FunctionOutcome({"writes": writes}, events=(StreamEvent("shared", "advanced", {"writes": writes}),))
        self.install([("large", {}), ("neighbor", {})], handler)
        # Keep the original 512-change/2MiB logical limits. Tighten only the
        # physical grouping policy around a real half-MiB state transaction.
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_HISTORY_BYTES", 128 * 1024), \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertGreater(result["processed"][0]["history_bytes"], 512 * 1024)
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertEqual(self.batch_totals()["logical_results_committed"], 2)
        samples = [sample for sample in self.runtime.work_diagnostics()["recent"]
                   if sample["kind"] == "checkpoint-batch"]
        self.assertEqual(samples[0]["batch_stop_reason"], "oversized-singleton")
        self.assertEqual(samples[0]["logical_results"], 1)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "large")["result"], {"writes": 8})
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "neighbor")["result"], {"writes": 1})
        self.assertTrue(all(row["attempts"] == 1 and row["generation"] == 1
                            for row in self.rows("world_checkpoints")))
        self.assertEqual(self.value("large-7")["value"]["n"], 1)
        self.assertEqual(self.value("neighbor-0")["value"]["n"], 1)
        self.assertEqual(len(self.rows("stream_events")), 2)

    def test_first_item_exceeding_only_physical_change_policy_keeps_one_logical_receipt(self):
        def handler(ctx, args):
            for index in range(2 if args["key"] == "large" else 1):
                ctx.set_state("values", args["key"] + "-" + str(index), {"n": 1})
            return FunctionOutcome({"key": args["key"]})
        self.install([("large", {}), ("neighbor", {})], handler)
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_CHANGES", 1), \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual([item["change_count"] for item in result["processed"]], [2, 1])
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "large")["attempt"], 1)
        samples = [sample for sample in self.runtime.work_diagnostics()["recent"]
                   if sample["kind"] == "checkpoint-batch"]
        self.assertEqual(samples[0]["batch_stop_reason"], "oversized-singleton")
        self.assertEqual(samples[0]["logical_results"], 1)

    def test_aggregate_change_bound_keeps_each_item_whole_and_does_not_count_rolled_back_attempt(self):
        self.install([(key, {}) for key in ("a", "b", "c")])
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_CHANGES", 2), \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=3)
        self.assertEqual(result["checkpoint_work"], {"commits": 3, "changes": 3, "stream_events": 3})
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertEqual([self.runtime.get_checkpoint("u", key)["attempts"] for key in ("a", "b", "c")], [1, 1, 1])

    def test_metadata_history_cannot_hide_actual_item_bytes_from_batch_bound(self):
        self.runtime.install_world("u", world([(key, {"payload": "x" * 60})
                                               for key in ("a", "b")], history="metadata"))
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_HISTORY_BYTES", 120), \
                patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertTrue(all(row["payload_retained"] == 0 for row in self.rows("state_changes")))
        self.assertTrue(all(item["history_bytes"] > 60 for item in result["processed"]))
        self.assertEqual([self.value(key)["value"]["n"] for key in ("a", "b")], [1, 1])

    def test_new_child_cannot_join_its_parents_physical_commit(self):
        self.install([("parent", {"child": "child"}), ("waiting", {})])
        with patch("agent_world.runtime_checkpoints.CHECKPOINT_BATCH_SECONDS", 60):
            result = self.runtime.run_due_timers("u", limit=3)
        self.assertEqual([item["checkpoint_key"] for item in result["processed"]], ["parent", "waiting", "child"])
        self.assertEqual(self.batch_totals()["physical_commits"], 2)
        self.assertEqual(self.batch_totals()["logical_results_committed"], 3)

    def test_same_path_runtimes_share_gate_and_waiting_foreground_closes_batch_between_items(self):
        order, errors, threads = [], [], []
        other = WorldRuntime(self.path)
        self.addCleanup(other.close)
        self.assertIs(self.runtime._lock, other._lock)
        def foreground():
            try:
                with other._lock:
                    other.create_role("foreground")
                    order.append("foreground")
            except BaseException as exc:
                errors.append(exc)
        def handler(ctx, args):
            order.append(args["key"])
            if args["key"] == "a":
                thread = threading.Thread(target=foreground)
                threads.append(thread)
                thread.start()
                until = time.monotonic() + 2
                while not self.runtime._lock.snapshot()["foreground_waiting"] and time.monotonic() < until:
                    time.sleep(.001)
                if not self.runtime._lock.snapshot()["foreground_waiting"]:
                    raise AssertionError("foreground never queued")
            return advance(ctx, args)
        self.install([("a", {}), ("b", {})], handler)
        try:
            result = self.runtime.run_due_timers("u", limit=2)
        finally:
            for thread in threads:
                thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(order, ["a", "foreground", "b"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.batch_totals()["physical_commits"], 2)


if __name__ == "__main__":
    unittest.main()
