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

    def test_failure_and_slow_log_record_no_exception_detail(self):
        timings = WorkDiagnostics()
        with patch("time.monotonic", side_effect=[100, 102]), \
                self.assertLogs("agent_world.runtime_diagnostics", level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                with timings.transaction("checkpoint"):
                    raise RuntimeError("PRIVATE_ARGUMENT_SECRET")
        sample = timings.snapshot()["recent"][0]
        self.assertEqual(sample["error"], "RuntimeError")
        self.assertNotIn("PRIVATE_ARGUMENT_SECRET", json.dumps(sample)+str(logs.output))

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
