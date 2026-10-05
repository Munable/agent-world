"""Local evidence recovery and finite-host bounds, without any model calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("dnd_runner_test_subject", Path(__file__).resolve().parents[1] / "tools" / "run_dnd_models.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class DndRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dnd-runner-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.evidence = root / "evidence"
        self.ledger = self.evidence / "model-runs.json"
        patches = patch.multiple(runner, TASK=root, EVIDENCE=self.evidence, LEDGER=self.ledger)
        patches.start()
        self.addCleanup(patches.stop)

    def test_large_tool_result_can_be_finalized_without_loosening_credentials(self):
        row = {"run_id": "actual-large-result", "host": "codex", "cli_cost_usd": 0,
               "tools": [{"result": "public scene " * 7000}], "stop_reason": None}
        runner.record_run(row, reserve=True)
        self.assertGreater(self.ledger.stat().st_size, 65536)
        runner.record_run({**row, "stop_reason": "host_completed"})
        saved = json.loads(self.ledger.read_text())
        self.assertEqual(len(saved["runs"]), 1)
        self.assertEqual(saved["runs"][0]["stop_reason"], "host_completed")
        with self.assertRaises(runner.RunRefused):
            runner.read_private(self.ledger)  # Ordinary credential reads retain their smaller limit.

    def test_host_budget_rejects_next_launch_without_resetting_existing_records(self):
        with patch.object(runner, "MAX_RUNS", 2):
            for name in ("first", "second"):
                runner.record_run({"run_id": name, "host": "codex", "cli_cost_usd": 0}, reserve=True)
            with self.assertRaises(runner.RunRefused):
                runner.record_run({"run_id": "third", "host": "codex", "cli_cost_usd": 0}, reserve=True)
        self.assertEqual([x["run_id"] for x in json.loads(self.ledger.read_text())["runs"]], ["first", "second"])

    def test_provider_length_is_incomplete_even_when_the_cli_can_exit_zero(self):
        item = {"stop_reason": None, "tools": [], "tool_errors": 0, "host_errors": [], "cli_cost_usd": 0,
                "tokens": {k: 0 for k in ("input", "output", "reasoning", "cache_read", "cache_write")}}
        runner.observe_event({"type": "step_finish", "part": {"reason": "length", "tokens": {}, "cost": 0}}, item, [], [], set())
        self.assertEqual(item["stop_reason"], "provider_output_limit")
        self.assertEqual(item["task_status"], "not_completed")


if __name__ == "__main__":
    unittest.main()
