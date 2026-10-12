"""Durability, pinned readers and independent host WAL backfill."""
import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
import sqlite3
from unittest.mock import Mock, patch

from agent_world import WorldRuntime
from agent_world.maintenance import retention_lifespan, _WalBackfillSchedule


class WalMaintenanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.db)
        self.addCleanup(self.runtime.close)
        with self.runtime._conn() as c:
            c.execute("CREATE TABLE wal_probe (value INTEGER)")
            c.execute("INSERT INTO wal_probe VALUES (0)")

    def write(self, value):
        with self.runtime._conn(serialized=True) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE wal_probe SET value=?", (value,))

    def test_managed_lifetime_preserves_full_and_restores_automatic_policy(self):
        with self.runtime._conn() as c:
            self.assertEqual(c.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
        with self.runtime.managed_wal_checkpoints():
            with self.runtime._conn() as reused:
                self.assertIs(reused, c)
                self.assertEqual(reused.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
                self.assertEqual(reused.execute("PRAGMA synchronous").fetchone()[0], 2)
            with self.assertRaises(RuntimeError):
                with self.runtime.managed_wal_checkpoints():
                    pass
            self.write(1)
            result = self.runtime.passive_wal_checkpoint()
            self.assertEqual(result["busy"], 0)
            self.assertEqual(result["wal_frames"], result["checkpointed_frames"])
        with self.runtime._conn() as c:
            self.assertEqual(c.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
            self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 2)
        with closing(WorldRuntime(self.db)) as reopened:
            with reopened._conn(readonly=True) as c:
                self.assertEqual(c.execute("SELECT value FROM wal_probe").fetchone()[0], 1)

    def test_pinned_snapshot_does_not_block_writes_and_backfill_resumes(self):
        with self.runtime.managed_wal_checkpoints():
            self.runtime.passive_wal_checkpoint()
            with self.runtime._conn(readonly=True) as reader:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT value FROM wal_probe").fetchone()[0], 0)
                self.write(1)
                pinned = self.runtime.passive_wal_checkpoint()
                self.assertGreater(pinned["wal_frames"], pinned["checkpointed_frames"])
                self.assertGreater(pinned["pending_frames"], 0)
                self.write(2)
                self.assertEqual(reader.execute("SELECT value FROM wal_probe").fetchone()[0], 0)
            settled = self.runtime.passive_wal_checkpoint()
            self.assertEqual(settled["busy"], 0)
            self.assertEqual(settled["wal_frames"], settled["checkpointed_frames"])
            with self.runtime._conn(readonly=True) as reader:
                self.assertEqual(reader.execute("SELECT value FROM wal_probe").fetchone()[0], 2)

    def test_noop_reads_pending_frames_without_backfilling_or_waiting_for_runtime_writer(self):
        with self.runtime.managed_wal_checkpoints():
            self.runtime.passive_wal_checkpoint()
            self.write(9)
            results = []
            with self.runtime._lock:
                thread = threading.Thread(target=lambda: results.append(self.runtime.wal_checkpoint_status()))
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive(), "NOOP waited for the application writer")
            self.assertIsNotNone(results[0], "this runtime must implement SQLite's NOOP checkpoint mode")
            before = results[0]
            self.assertGreater(before["pending_frames"], 0)
            self.assertEqual(self.runtime.wal_checkpoint_status(), before)
            self.assertEqual(self.runtime.passive_wal_checkpoint()["pending_frames"], 0)
            self.assertEqual(self.runtime.wal_checkpoint_status()["pending_frames"], 0)
            with self.runtime._conn() as c:
                self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_unknown_noop_mode_falls_back_without_masking_other_storage_errors(self):
        from contextlib import nullcontext
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("unknown checkpoint mode: NOOP")
        with patch.object(self.runtime, "_conn", return_value=nullcontext(connection)):
            self.assertIsNone(self.runtime.wal_checkpoint_status())
            self.assertIsNone(self.runtime.wal_checkpoint_status())
        connection.execute.assert_called_once_with("PRAGMA wal_checkpoint(NOOP)")
        self.runtime._wal_noop_supported = None
        connection.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        with patch.object(self.runtime, "_conn", return_value=nullcontext(connection)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                self.runtime.wal_checkpoint_status()
        self.assertIsNone(self.runtime._wal_noop_supported)

    def test_passive_backfill_is_independent_of_runtime_writer(self):
        acquired = threading.Event()
        release = threading.Event()
        result = []
        def writer():
            with self.runtime._conn(serialized=True) as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE wal_probe SET value=3")
                acquired.set()
                release.wait(3)
        with self.runtime.managed_wal_checkpoints():
            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(acquired.wait(2))
            backfill = threading.Thread(target=lambda: result.append(self.runtime.passive_wal_checkpoint()))
            backfill.start()
            try:
                backfill.join(1)
                self.assertFalse(backfill.is_alive(), "PASSIVE waited for the application writer lock")
                self.assertEqual(len(result), 1)
            finally:
                release.set()
                thread.join(2)
                backfill.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["busy"], 0)

    def test_lifespan_joins_its_existing_maintenance_thread_before_restoring_policy(self):
        entered = threading.Event()
        release = threading.Event()
        actual = self.runtime.passive_wal_checkpoint
        def backfill():
            entered.set()
            release.wait(3)
            return actual()
        async def exercise():
            finish_serving = asyncio.Event()
            with patch.object(self.runtime, "passive_wal_checkpoint", side_effect=backfill), \
                    patch.object(self.runtime, "wal_checkpoint_status", return_value={
                        "busy": 0, "wal_frames": 1, "checkpointed_frames": 0, "pending_frames": 1}):
                async def serve():
                    async with retention_lifespan(self.runtime, "unused"):
                        await finish_serving.wait()
                task = asyncio.create_task(serve())
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # End the actual lifespan only after the worker has entered
                # maintenance. An already-ended lifespan may correctly skip IO.
                finish_serving.set()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(self.runtime._wal_autocheckpoint, 0)
                release.set()
                await task
            self.assertEqual(self.runtime._wal_autocheckpoint, 1000)
        asyncio.run(exercise())

    def test_unexpected_maintenance_exit_restores_writer_fallback_while_host_is_alive(self):
        exited = threading.Event()
        restore = self.runtime.restore_auto_wal_checkpoints
        def fallback():
            restore()
            exited.set()
        async def exercise():
            with patch.object(self.runtime, "passive_wal_checkpoint", side_effect=KeyboardInterrupt), \
                    patch.object(self.runtime, "restore_auto_wal_checkpoints", side_effect=fallback), \
                    self.assertLogs("agent_world.maintenance", level="CRITICAL"):
                async with retention_lifespan(self.runtime, "unused"):
                    self.assertTrue(await asyncio.to_thread(exited.wait, 2))
                    self.write(4)
                    with self.runtime._conn() as c:
                        self.assertEqual(c.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
                        self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 2)
        asyncio.run(exercise())


class WalBackfillScheduleTests(unittest.TestCase):
    def status(self, frames, checkpointed):
        return {"wal_frames": frames, "checkpointed_frames": checkpointed,
                "pending_frames": frames-checkpointed}

    def test_empty_and_unchanged_pinned_wal_do_not_trigger_repeated_passive(self):
        schedule = _WalBackfillSchedule(max_defer=5)
        self.assertFalse(schedule.ready(self.status(12, 12), writer_busy=False, now=0))
        pending = self.status(15, 12)
        self.assertTrue(schedule.ready(pending, writer_busy=False, now=1))
        schedule.completed(pending, now=1)
        self.assertFalse(schedule.ready(pending, writer_busy=False, now=2))
        self.assertFalse(schedule.ready(pending, writer_busy=False, now=5.9))
        # A released reader does not necessarily alter NOOP's counts; it still
        # receives another PASSIVE opportunity at the bounded retry deadline.
        self.assertTrue(schedule.ready(pending, writer_busy=False, now=6))
        schedule.completed(self.status(15, 15), now=6)
        self.assertFalse(schedule.ready(self.status(15, 15), writer_busy=False, now=20))

    def test_continuous_writers_yield_one_maintenance_opportunity_by_deadline(self):
        schedule = _WalBackfillSchedule(max_defer=5)
        self.assertFalse(schedule.ready(self.status(15, 12), writer_busy=True, now=0))
        self.assertFalse(schedule.ready(self.status(16, 12), writer_busy=True, now=4))
        self.assertTrue(schedule.ready(self.status(17, 12), writer_busy=True, now=5))
        schedule.completed(self.status(17, 17), now=5.5)
        self.assertFalse(schedule.ready(self.status(18, 17), writer_busy=True, now=6))
        self.assertTrue(schedule.ready(self.status(19, 17), writer_busy=False, now=7))

    def test_unsupported_noop_keeps_the_existing_passive_fallback(self):
        schedule = _WalBackfillSchedule()
        self.assertTrue(schedule.ready(None, writer_busy=True, now=0))


if __name__ == "__main__":
    unittest.main()
