"""All formal Agent writes belong to one task's confirmed evidence boundary."""
from copy import deepcopy
import unittest

from agent_world.grove_agent_observation import AGENT_WRITE_FUNCTIONS
from agent_world.grove_execution import ExecutionController


class TaskWriteSummaryContracts(unittest.TestCase):
    def setUp(self):
        self.controller = ExecutionController("person", max_requests=4, max_world_calls=16, max_seconds=150)
        self.controller.state.update(task_id="current-task", status="running", segment_id="second-segment",
            actions_count=2, max_actions=8, requests_used=2, delegation_active=True)
        self.controller.active = True
        self.controller.delegation = "original-grant"

    def receipt(self, function, operation, *, segment="first-segment", status="confirmed", payload=None, task="current-task"):
        value = payload or {"commit_seq": 7, "result": {}}
        self.controller.receipt(function, operation, value, status=status,
            operation_record={"task_id": task, "segment_id": segment, "envelope": {"arguments": {}}})

    def protected(self):
        return deepcopy((self.controller.state, self.controller.archive, self.controller.delegation,
            self.controller.active, self.controller.stopped, self.controller.reclaiming))

    def test_every_formal_write_is_counted_while_host_control_reads_and_other_tasks_are_excluded(self):
        for index, function in enumerate(sorted(AGENT_WRITE_FUNCTIONS)):
            self.receipt(function, "formal-" + str(index))
        for function in ("grove.control_grant", "grove.control_revoke", "map.viewport", "grove.definition_validate"):
            self.receipt(function, function)
        self.receipt("grove.content_submit", "another-task-publish", task="another-task")
        saved = self.protected()
        summary = self.controller.action_summary()
        self.assertEqual(summary["confirmed_count"], len(AGENT_WRITE_FUNCTIONS))
        self.assertEqual(summary["total_groups"], len(AGENT_WRITE_FUNCTIONS))
        self.assertEqual(summary["omitted_groups"], len(AGENT_WRITE_FUNCTIONS) - len(summary["groups"]))
        self.assertEqual(summary["coverage"]["operations_without_structured_costs"], len(AGENT_WRITE_FUNCTIONS))
        self.assertEqual(summary["material_costs"], [])
        self.assertTrue(summary["coverage"]["receipt_history_complete"])
        self.assertEqual(summary["coverage"]["receipt_history_scope"], "formal_agent_writes")
        self.assertIn("Opportunity claims/releases", summary["meaning"])
        self.assertIn("definition publications", summary["meaning"])
        self.assertIn("content submissions", summary["meaning"])
        self.assertEqual(self.protected(), saved)

    def test_cross_segment_rain_creation_and_object_operation_are_distinct_deduplicated_original_writes(self):
        claim = {"commit_seq": 10, "result": {"opportunity": {"id": "shared-rain", "kind": "rain"}}}
        publication = {"commit_seq": 11, "result": {"published": True, "content_id": "actual-rain",
            "content": {"kind": "rain", "duration_seconds": 3}}}
        definition = {"commit_seq": 12, "result": {"definition_ref": "immutable-reference",
            "definition": {"name": "Original marker", "program": {"rules": []}}}}
        action = {"commit_seq": 14, "result": {"action_result": {"action": "move", "costs": []}}}
        payloads = [claim, publication, definition, action]
        original_payloads = deepcopy(payloads)
        self.receipt("grove.generation_context", "original-claim", payload=claim)
        self.receipt("grove.content_submit", "original-rain", payload=publication)
        self.receipt("grove.definition_publish", "original-definition", payload=definition, segment="second-segment")
        self.receipt("grove.content_release", "original-release", segment="second-segment")
        self.receipt("grove.move", "original-move", payload=action, segment="second-segment")
        # A recovery confirms the original operation, never a second action.
        self.controller.receipt("grove.content_submit", "original-rain", publication, status="confirmed")
        self.controller.state["receipts"].append(deepcopy(self.controller.state["receipts"][1]))
        self.receipt("grove.content_submit", "not-yet-confirmed", status="committed")
        saved = self.protected()
        summary = self.controller.action_summary()
        self.assertEqual(summary["confirmed_count"], 5)
        self.assertEqual(summary["segments"], [{"segment_id": "first-segment", "confirmed_count": 2},
            {"segment_id": "second-segment", "confirmed_count": 3}])
        groups = {row["function"]: row for row in summary["groups"]}
        self.assertEqual(groups["grove.generation_context"]["last_operation_id"], "original-claim")
        self.assertEqual(groups["grove.content_submit"]["last_operation_id"], "original-rain")
        self.assertEqual(groups["grove.content_submit"]["count"], 1)
        self.assertEqual(groups["grove.definition_publish"]["last_operation_id"], "original-definition")
        self.assertEqual(groups["grove.content_release"]["last_operation_id"], "original-release")
        self.assertEqual(groups["grove.move"]["last_operation_id"], "original-move")
        self.assertEqual(summary["coverage"]["committed_unconfirmed_count"], 1)
        self.assertEqual(summary["coverage"]["structured_cost_operations"], 1)
        self.assertEqual(summary["coverage"]["operations_without_structured_costs"], 4)
        self.assertEqual(summary["material_costs"], [])
        self.assertEqual(self.protected(), saved)
        self.assertEqual(payloads, original_payloads)

    def test_eviction_uses_the_same_full_formal_write_scope(self):
        for function in sorted(AGENT_WRITE_FUNCTIONS):
            for status in ("confirmed", "committed", "unknown"):
                with self.subTest(function=function, status=status):
                    self.setUp()
                    self.controller.request_limit = self.controller.world_call_limit = 1
                    self.receipt(function, "evicted-original", status=status)
                    self.receipt("grove.move", "retained-original")
                    summary = self.controller.action_summary()
                    self.assertFalse(summary["coverage"]["receipt_history_complete"])
                    self.assertEqual(summary["confirmed_count"], 1)
                    self.assertEqual(summary["groups"][0]["last_operation_id"], "retained-original")

    def test_evicted_host_work_and_explicitly_rejected_write_do_not_claim_missing_commits(self):
        for function, status in (("grove.control_grant", "confirmed"), ("grove.control_revoke", "confirmed"),
                                 ("grove.content_submit", "rejected")):
            with self.subTest(function=function, status=status):
                self.setUp()
                self.controller.request_limit = self.controller.world_call_limit = 1
                self.receipt(function, "evicted-noncommit", status=status)
                self.receipt("grove.content_submit", "retained-publication")
                self.assertTrue(self.controller.action_summary()["coverage"]["receipt_history_complete"])

    def test_old_complete_flag_is_not_promoted_to_full_write_history_or_restored_summary(self):
        self.receipt("grove.generation_context", "original-claim")
        self.receipt("grove.content_submit", "original-publication", segment="second-segment")
        saved = self.controller.snapshot()
        del saved["receipt_history_scope"]
        saved["action_summary"] = {"confirmed_count": 999, "coverage": {"receipt_history_complete": True}}
        original = deepcopy(saved)
        restored = ExecutionController("person", max_requests=4, max_world_calls=16, max_seconds=150)
        restored.restore(saved, delegation="original-grant")
        self.assertEqual(saved, original)
        self.assertEqual(restored.state["receipts"], original["receipts"])
        self.assertNotIn("action_summary", restored.state)
        summary = restored.action_summary()
        self.assertEqual(summary["confirmed_count"], 2)
        self.assertFalse(summary["coverage"]["receipt_history_complete"])
        self.assertEqual(summary["coverage"]["receipt_history_scope"], "legacy_write_scope_unverified")
        self.assertIn("full formal-write completeness is unknown", summary["meaning"])
        restored.restore(restored.snapshot(), delegation="original-grant")
        self.assertFalse(restored.action_summary()["coverage"]["receipt_history_complete"])
        # A newly authorized task has a new ledger with explicit full scope;
        # starting it archives rather than rewrites the old uncertain records.
        restored.delegation = ""
        restored.state.update(status="paused", request_status="idle_confirmed", delegation_active=False)
        restored.start({"goal": "A new actual request", "max_actions": 4, "max_requests": 4},
            entered=True, native_connected=True, worker_busy=False,
            catalog={"grove.control_grant": {"input_schema": {"type": "object",
                "required": ["goal", "max_actions"], "properties": {"goal": {"type": "string"},
                    "max_actions": {"type": "integer", "minimum": 1, "maximum": 64}}}},
                "grove.control_revoke": {}, "grove.control_status": {}})
        new_summary = restored.action_summary()
        self.assertTrue(new_summary["coverage"]["receipt_history_complete"])
        self.assertEqual(new_summary["coverage"]["receipt_history_scope"], "formal_agent_writes")
        self.assertEqual(new_summary["confirmed_count"], 0)
        self.assertEqual(restored.archive[-1]["receipts"], original["receipts"])


if __name__ == "__main__":
    unittest.main()
