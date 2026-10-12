"""Bounded live host timings are independent of world facts and writer locks."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent_world import WorldRuntime
from agent_world.http_app import create_app
from agent_world.runtime_diagnostics import WorkDiagnostics


class WorkDiagnosticsTests(unittest.TestCase):
    def test_samples_are_bounded_and_snapshot_is_a_copy(self):
        timings = WorkDiagnostics(capacity=4)
        for generation in range(10):
            with timings.transaction("checkpoint"):
                timings.annotate(generation=generation)
                with timings.phase("handler", cpu=True):
                    pass
        snapshot = timings.snapshot(4)
        self.assertEqual(snapshot["transactions"], 10)
        self.assertEqual([item["generation"] for item in snapshot["recent"]], [6, 7, 8, 9])
        self.assertEqual(snapshot["phases"]["handler"]["count"], 10)
        self.assertIn("handler_cpu", snapshot["phases"])
        snapshot["recent"].clear()
        snapshot["phases"]["handler"]["count"] = 0
        self.assertEqual(len(timings.snapshot(4)["recent"]), 4)
        self.assertEqual(timings.snapshot(4)["phases"]["handler"]["count"], 10)
        self.assertEqual(snapshot["kinds"]["checkpoint"]["count"], 10)
        self.assertEqual(snapshot["kinds"]["checkpoint"]["phases"]["handler"]["count"], 10)
        snapshot["kinds"]["checkpoint"]["count"] = 0
        self.assertEqual(timings.snapshot(4)["kinds"]["checkpoint"]["count"], 10)

    def test_failure_and_slow_log_record_no_exception_detail(self):
        timings = WorkDiagnostics()
        with patch("time.time", return_value=0.25), \
                patch("time.monotonic", side_effect=[100, 102]), \
                self.assertLogs("agent_world.runtime_diagnostics", level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                with timings.transaction("checkpoint"):
                    timings.annotate(handler="grove.domain", generation=2)
                    raise RuntimeError("PRIVATE_ARGUMENT_SECRET")
        sample = timings.snapshot()["recent"][0]
        self.assertEqual(sample["error"], "RuntimeError")
        self.assertEqual(sample["started_at"], 0.25)
        self.assertIn("started_at=0.250000", logs.output[0])
        self.assertIn("started_utc=1970-01-01T00:00:00.250000+00:00", logs.output[0])
        self.assertIn("function=grove.domain", logs.output[0])
        self.assertNotIn("PRIVATE_ARGUMENT_SECRET", json.dumps(sample)+str(logs.output))

    def test_slow_wal_backfill_logs_original_start_and_its_separate_phase(self):
        timings = WorkDiagnostics()
        with patch("time.time", return_value=0.5) as wall_clock, \
                patch("time.monotonic", side_effect=[100, 101, 103, 104]), \
                self.assertLogs("agent_world.runtime_diagnostics", level="WARNING") as logs:
            with timings.transaction("wal-checkpoint"):
                with timings.phase("wal_backfill"):
                    pass
                wall_clock.return_value = 10.5
                timings.annotate(wal_frames=100, checkpointed_frames=80, pending_frames=20)
        sample = timings.snapshot()["recent"][0]
        self.assertEqual(sample["started_at"], 0.5)
        self.assertEqual(sample["phases"]["wal_backfill"], 2)
        self.assertIn("started_at=0.500000", logs.output[0])
        self.assertIn("started_utc=1970-01-01T00:00:00.500000+00:00", logs.output[0])
        self.assertIn("kind=wal-checkpoint function=-", logs.output[0])
        self.assertIn("wal_backfill=2.000", logs.output[0])
        self.assertEqual(sample["pending_frames"], 20)

    def test_live_health_optional_diagnostics_preserve_default_and_ignore_writer_lock(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Path(directory.name)/"world.sqlite3"
        app = create_app(db)
        runtime = app.state.runtime
        self.addCleanup(runtime.close)
        with runtime._conn(serialized=True, purpose="action") as connection:
            with runtime._work_timings.phase("begin"):
                connection.execute("BEGIN IMMEDIATE")
        with TestClient(app, client=("127.0.0.1", 55001)) as client:
            ordinary = client.get("/health").json()
            self.assertNotIn("work_diagnostics", ordinary)
            runtime._lock.acquire()
            result = []
            thread = threading.Thread(target=lambda: result.append(client.get("/health?diagnostics=true")))
            thread.start()
            try:
                thread.join(2)
                self.assertFalse(thread.is_alive(), "host diagnostics waited for the world writer")
            finally:
                runtime._lock.release()
                thread.join(2)
            self.assertEqual(result[0].status_code, 200)
            diagnostic = result[0].json()
            self.assertEqual({key: value for key, value in diagnostic.items() if key != "work_diagnostics"}, ordinary)
            data = diagnostic["work_diagnostics"]
            self.assertLessEqual(len(data["recent"]), 32)
            self.assertIn("writer_wait", data["phases"])
            self.assertIn("begin", data["phases"])
            self.assertIn("sqlite_commit", data["phases"])


if __name__ == "__main__":
    unittest.main()
