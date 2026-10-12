"""Writer handoff fairness, reentry and advisory background snapshots."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, CheckpointSpec
from agent_world.errors import WorldVersionMismatch
from agent_world.writer_gate import WriterGate


class WriterGateTests(unittest.TestCase):
    def wait_for_queue(self, gate, count):
        deadline = time.monotonic() + 2
        while gate.snapshot()["waiting"] < count and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(gate.snapshot()["waiting"], count)

    def test_waiting_action_precedes_background_reentry_and_maintenance_is_not_starved(self):
        gate = WriterGate()
        entered = []
        threads = []
        gate.acquire(kind="checkpoint")
        try:
            def write(kind):
                gate.acquire(kind=kind)
                try:
                    entered.append(kind)
                finally:
                    gate.release()
            for kind in ("action", "maintenance", "checkpoint", "action"):
                thread = threading.Thread(target=write, args=(kind,))
                threads.append(thread)
                thread.start()
                self.wait_for_queue(gate, len(threads))
            self.assertEqual(gate.snapshot()["foreground_waiting"], 2)
            # Nested ownership is the same transaction, not a new queue entry.
            self.assertTrue(gate.acquire(timeout=0))
            self.assertTrue(gate._is_owned())
            gate.release()
            self.assertEqual(entered, [])
        finally:
            gate.release()
            for thread in threads:
                thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(entered, ["action", "maintenance", "checkpoint", "action"])
        self.assertFalse(gate._is_owned())

    def test_timed_out_waiter_leaves_no_abandoned_queue_head(self):
        gate = WriterGate()
        result = []
        gate.acquire()
        thread = threading.Thread(target=lambda: result.append(gate.acquire(timeout=.05)))
        thread.start()
        try:
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
            self.assertEqual(gate.snapshot()["waiting"], 0)
        finally:
            gate.release()
            thread.join(2)
        with gate:
            self.assertTrue(gate._is_owned())

    def test_nonowner_cannot_release_or_take_nonblocking_ownership(self):
        gate = WriterGate()
        result = []
        failures = []
        with gate:
            def contender():
                result.append(gate.acquire(blocking=False))
                try:
                    gate.release()
                except RuntimeError:
                    failures.append("nonowner")
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(gate._is_owned())
        self.assertEqual(result, [False])
        self.assertEqual(failures, ["nonowner"])

    def test_interrupted_waiter_does_not_strand_the_next_writer(self):
        gate = WriterGate()
        failures = []
        with gate, patch.object(gate._condition, "wait", side_effect=RuntimeError("interrupted")):
            def interrupted():
                try:
                    gate.acquire()
                except RuntimeError as exc:
                    failures.append(str(exc))
            thread = threading.Thread(target=interrupted)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(gate.snapshot()["waiting"], 0)
        self.assertEqual(failures, ["interrupted"])
        self.assertTrue(gate.acquire(timeout=0))
        gate.release()


def empty_outcome(ctx, args):
    return FunctionOutcome({})


WORLD = WorldDefinition("worker-read", "Worker snapshots",
    (FunctionSpec("noop", empty_outcome, {"type": "object"}),),
    checkpoints=(CheckpointSpec("background", empty_outcome, {"type": "object"}),))


class BackgroundSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = WorldRuntime(Path(directory.name) / "world.sqlite3")
        self.addCleanup(self.runtime.close)
        self.runtime.install_world("u", WORLD)
        with self.runtime._conn() as c:
            c.execute("CREATE TABLE writer_probe (value INTEGER)")

    def reads(self):
        return (lambda: self.runtime.next_due_work("u"),
                lambda: self.runtime._due_checkpoint_candidates("u", set(), limit=2),
                lambda: self.runtime.run_due_timers("u", limit=2))

    def test_advisory_head_reads_complete_during_uncommitted_writer(self):
        results, failures = [], []
        with self.runtime._conn(serialized=True, purpose="action") as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO writer_probe VALUES (1)")
            for read in self.reads():
                def run():
                    try:
                        results.append(read())
                    except BaseException as exc:
                        failures.append(exc)
                thread = threading.Thread(target=run)
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive(), "advisory snapshot queued for the world writer")
                self.assertEqual(self.runtime._lock.snapshot()["waiting"], 0)
        self.assertEqual(failures, [])
        self.assertEqual(results[:2], [None, []])
        self.assertEqual(results[2]["count"], 0)
        kinds = self.runtime.work_diagnostics()["kinds"]
        self.assertIn("work-next", kinds)
        self.assertIn("checkpoint-select", kinds)

    def test_registry_change_rejects_old_candidate_snapshot_before_dispatch(self):
        previous = self.runtime._capture_registry("u")
        self.runtime.install_world("u", replace(WORLD, version=2))
        with patch.object(self.runtime, "_capture_registry", return_value=previous), \
                patch.object(self.runtime, "_run_one_checkpoint") as dispatch:
            for read in self.reads():
                with self.assertRaises(WorldVersionMismatch):
                    read()
            dispatch.assert_not_called()

    def test_identity_write_joins_the_same_gate_as_direct_runtime_ownership(self):
        completed = []
        failures = []
        def create():
            try:
                completed.append(self.runtime.create_role("queued identity"))
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=create)
        with self.runtime._lock:
            thread.start()
            deadline = time.monotonic() + 2
            while not self.runtime._lock.snapshot()["waiting"] and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(self.runtime._lock.snapshot()["waiting"], 1)
            self.assertEqual(completed, [])
            # Existing direct callers may nest a write without deadlocking
            # behind a different thread already queued for that same gate.
            self.runtime.create_role("nested identity")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(completed[0]["display_name"], "queued identity")


if __name__ == "__main__":
    unittest.main()
