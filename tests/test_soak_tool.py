"""Acceptance guards for the optional local durability exercise."""
import unittest
from unittest.mock import patch

from tools.check_commons_soak import run


class SoakToolTests(unittest.TestCase):
    def test_startup_does_not_consume_the_workload_window(self):
        report = run(.1, .1, 10, 1)
        self.assertEqual(report["status"], "passed", report)
        self.assertGreater(report["rounds"], 0, report)
        self.assertGreater(report["messages"], 0, report)
        self.assertGreaterEqual(report["workload_elapsed_seconds"], .1, report)

    def test_source_change_invalidates_acceptance(self):
        with patch("tools.check_commons_soak.source_hash", side_effect=["before", "after"]):
            report = run(.1, .1, 10, 1)
        self.assertFalse(report["source_unchanged"])
        self.assertEqual(report["status"], "failed", report)
        self.assertIn("source changed", report["error"])


if __name__ == "__main__":
    unittest.main()
