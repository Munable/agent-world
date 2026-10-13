"""Execution ownership boundaries, with narrow in-memory port doubles.

These assertions cover admission, original receipt ownership and stop/budget
semantics. They do not claim a model or player completed the world experience.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, AsyncMock

from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_execution import ExecutionController, ExecutionPorts, confirmed_outcome


GRANT_SCHEMA = {"type": "object", "required": ["goal", "max_actions"],
    "properties": {"goal": {"type": "string", "minLength": 1},
        "max_actions": {"type": "integer", "minimum": 1, "maximum": 64}}, "additionalProperties": False}
CATALOG = {"grove.control_grant": {"input_schema": GRANT_SCHEMA},
    "grove.control_revoke": {}, "grove.control_status": {}}
CHECKPOINT_SCHEMA = {"type": "object", "required": ["task_id", "execution_segment_id", "status", "summary",
    "next_intent", "evidence_operation_ids"], "properties": {
        "task_id": {"type": "string"}, "execution_segment_id": {"type": "string"},
        "status": {"enum": ["continue", "completed", "blocked"]}, "summary": {"type": "string"},
        "next_intent": {"type": "string"}, "evidence_operation_ids": {"type": "array", "items": {"type": "string"}}},
    "additionalProperties": False}


class ExecutionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def test_task_action_summary_keeps_fuel_after_moves_without_inferring_from_current_energy(self):
        self.start(max_actions=64)
        task = self.controller.state["task_id"]
        arguments = {"action": "fuel", "target_id": "wheel", "item_id": "wood", "amount": 5}
        record = {"task_id": task, "segment_id": "fuel-segment", "envelope": {"arguments": arguments}}
        payload = {"commit_seq": 7, "result": {"entity": {"id": "wheel", "version": 2,
            "state": {"program": {"mechanical": {"energy": 5}}}},
            "action_result": {"action": "fuel", "stored_energy_after": 5,
                "costs": [{"entity_id": "wood", "quantity_u": 5}]}}}
        self.controller.receipt("grove.rule_action", "original-fuel", payload,
            status="committed", operation_record=record)
        # Recovery reads have no original argument envelope; classification and
        # both real source/target identities must survive the confirmation.
        self.controller.receipt("grove.rule_action", "original-fuel", payload, status="confirmed")
        for index in range(12):
            self.controller.receipt("grove.move", "move-" + str(index), {"commit_seq": 8 + index},
                status="confirmed", operation_record={"task_id": task, "envelope": {"arguments": {"dx": 1}}})
        self.controller.state["readback"] = {"entity": {"id": "wheel", "energy": 0}}
        before = deepcopy(self.controller.state)
        summary = self.controller.action_summary()
        self.assertEqual(summary["scope"], "current_task_retained_confirmed_actions")
        self.assertEqual((summary["confirmed_count"], summary["total_groups"], summary["omitted_groups"]), (13, 2, 0))
        fuel, moves = summary["groups"]
        self.assertEqual((fuel["function"], fuel["action"], fuel["action_entity_ids"]),
            ("grove.rule_action", "fuel", ["wheel", "wood"]))
        self.assertEqual((fuel["count"], fuel["last_operation_id"], fuel["last_commit_seq"]), (1, "original-fuel", 7))
        self.assertEqual(fuel["last_confirmed_outcome"]["action_result"]["stored_energy_after"], 5)
        self.assertEqual(moves["count"], 12)
        self.assertEqual(self.controller.state, before)
        fuel["last_confirmed_outcome"]["action_result"]["stored_energy_after"] = 99
        self.assertEqual(self.controller.action_summary()["groups"][0]["last_confirmed_outcome"]["action_result"]["stored_energy_after"], 5)
        self.assertEqual(self.controller.value("action_summary"), self.controller.snapshot()["action_summary"])

    def test_task_action_summary_excludes_old_tasks_unconfirmed_and_host_internal_work(self):
        self.start()
        old_task = self.controller.state["task_id"]
        old_record = {"task_id": old_task, "envelope": {"arguments": {"action": "fuel", "target_id": "old-wheel"}}}
        self.controller.receipt("grove.rule_action", "old-fuel", {"commit_seq": 1},
            status="confirmed", operation_record=old_record)
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="Current exploration")
        task = self.controller.state["task_id"]
        self.controller.receipt("grove.rule_action", "late-old-fuel", {"commit_seq": 2},
            status="confirmed", operation_record=old_record)
        for status in ("committed", "unknown", "rejected"):
            self.controller.receipt("grove.rule_action", status, {"commit_seq": 3}, status=status,
                operation_record={"task_id": task, "envelope": {"arguments": {"action": "fuel", "target_id": "wheel"}}})
        for function in ("grove.control_grant", "grove.control_revoke", "map.viewport"):
            self.controller.receipt(function, function, {"commit_seq": 4}, status="confirmed",
                operation_record={"task_id": task})
        # Legacy records without action classification still group by their
        # actual function and object identities, not by guessed action names.
        self.controller.receipt("grove.interact", "legacy-action", {"commit_seq": 5,
            "result": {"entity": {"id": "cup"}}}, status="confirmed", task_id=task)
        summary = self.controller.action_summary()
        self.assertEqual(summary["confirmed_count"], 1)
        self.assertEqual(summary["groups"][0]["action_entity_ids"], ["cup"])
        self.assertNotIn("action", summary["groups"][0])
        self.assertNotIn("action_id", summary["groups"][0])
        self.assertEqual(len(self.controller.archive[-1]["receipts"]), 2)

    def test_task_action_summary_has_explicit_group_limit_and_preserves_original_action_ids(self):
        self.start(max_actions=64)
        task = self.controller.state["task_id"]
        for index in range(11):
            self.controller.receipt("grove.rule_action", "switch-" + str(index), {"commit_seq": index + 1},
                status="confirmed", operation_record={"task_id": task, "envelope": {"arguments": {
                    "action_id": "switch", "target_id": "marker-" + str(index)}}})
        self.controller.receipt("grove.rule_action", "switch-again", {"commit_seq": 12}, status="confirmed",
            operation_record={"task_id": task, "envelope": {"arguments": {"action_id": "switch", "target_id": "marker-0"}}})
        summary = self.controller.action_summary()
        self.assertEqual((summary["confirmed_count"], summary["total_groups"], summary["omitted_groups"]), (12, 11, 3))
        self.assertEqual(len(summary["groups"]), 8)
        self.assertEqual(summary["groups"][-1]["action_entity_ids"], ["marker-0"])
        self.assertEqual(summary["groups"][-1]["action_id"], "switch")
        self.assertEqual(summary["groups"][-1]["count"], 2)
        self.assertEqual(summary["groups"][-1]["last_operation_id"], "switch-again")
        self.assertIn("bounded receipt ledger", summary["meaning"])
        self.assertIn("do not mean an action never happened", summary["meaning"])

    async def test_action_summary_readback_is_host_owned_and_restore_recomputes_it(self):
        await self.running()
        action = await self.controller.write("grove.move", {}, segment_id="current-segment")
        summary = action["execution_readback"]["task_action_summary"]
        self.assertEqual(summary["confirmed_count"], 1)
        self.assertEqual(summary["groups"][0]["last_operation_id"], action["operation_id"])
        saved = self.controller.snapshot()
        saved["action_summary"] = {"confirmed_count": 999}
        self.controller.restore(saved)
        self.assertNotIn("action_summary", self.controller.state)
        self.assertEqual(self.controller.snapshot()["action_summary"], summary)

    def test_task_action_summary_omits_large_recording_body_but_keeps_action_evidence(self):
        self.start()
        task = self.controller.state["task_id"]
        oversized = {"commit_seq": 1, "result": {"action_result": {"recorded_text": "真实" * 3000}}}
        self.controller.receipt("grove.rule_action", "large-original", oversized, status="confirmed",
            operation_record={"task_id": task, "envelope": {"arguments": {"action_id": "record", "target_id": "recorder"}}})
        self.controller.receipt("grove.move", "small-original", {"commit_seq": 2}, status="confirmed",
            operation_record={"task_id": task, "envelope": {"arguments": {"dx": 1}}})
        before = deepcopy(self.controller.state)
        summary = self.controller.action_summary()
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")), 8 * 1024)
        self.assertEqual((summary["confirmed_count"], summary["total_groups"], summary["omitted_groups"]), (2, 2, 0))
        recording = summary["groups"][0]
        self.assertEqual(recording["last_operation_id"], "large-original")
        self.assertEqual(recording["last_confirmed_outcome"]["action_result_omitted_fields"], ["recorded_text"])
        self.assertNotIn("recorded_text", recording["last_confirmed_outcome"]["action_result"])
        self.assertEqual(summary["groups"][1]["last_operation_id"], "small-original")
        self.assertEqual(self.controller.state, before)
        # The large latest group stays attributable; original text is untouched
        # and its explicit omission cannot masquerade as an empty recording.
        self.controller.receipt("grove.rule_action", "large-latest", oversized, status="confirmed",
            operation_record={"task_id": task, "envelope": {"arguments": {"action_id": "record", "target_id": "recorder"}}})
        summary = self.controller.action_summary()
        self.assertEqual((summary["confirmed_count"], summary["total_groups"], summary["omitted_groups"]), (3, 2, 0))
        self.assertEqual(summary["groups"][-1]["last_operation_id"], "large-latest")
        self.assertEqual(summary["groups"][-1]["last_confirmed_outcome"]["action_result_omitted_fields"], ["recorded_text"])
        self.assertEqual(self.controller.state["receipts"][-1]["confirmed_outcome"]["action_result"]["recorded_text"], "真实" * 3000)

    def test_task_costs_add_distinct_actual_receipts_across_segments_not_last_cost_times_count(self):
        self.start()
        task = self.controller.state["task_id"]
        for index, (segment, amount) in enumerate((("first", 5), ("second", 10), ("second", 15))):
            payload = {"commit_seq": 100 + index, "result": {"action_result": {"action": "fuel",
                "costs": [{"material": "wood", "quantity_u": amount}], "stored_energy_after": amount}}}
            record = {"task_id": task, "segment_id": segment, "envelope": {"arguments": {
                "action": "fuel", "target_id": "source", "item_id": "wood"}}}
            self.controller.receipt("grove.rule_action", "fuel-" + str(index), payload,
                status="confirmed", operation_record=record)
        # Transport recovery/replay and a duplicated retained historical row
        # must neither add a fourth action nor repeat a confirmed material cost.
        self.controller.receipt("grove.rule_action", "fuel-2", payload, status="confirmed")
        self.controller.state["receipts"].append(deepcopy(self.controller.state["receipts"][-1]))
        before = deepcopy(self.controller.state)
        summary = self.controller.action_summary()
        self.assertEqual(summary["confirmed_count"], 3)
        self.assertEqual(summary["groups"][0]["count"], 3)
        self.assertEqual(summary["groups"][0]["last_confirmed_outcome"]["action_result"]["stored_energy_after"], 15)
        self.assertEqual(summary["material_costs"], [{"material": "wood", "quantity_u": 30}])
        self.assertEqual(summary["segments"], [{"segment_id": "first", "confirmed_count": 1},
            {"segment_id": "second", "confirmed_count": 2}])
        self.assertTrue(summary["coverage"]["receipt_history_complete"])
        self.assertEqual(summary["coverage"]["structured_cost_operations"], 3)
        self.assertEqual(self.controller.state, before)

    def test_cost_summary_never_guesses_from_names_body_or_transfers_and_reports_missing_facts(self):
        self.start()
        task = self.controller.state["task_id"]
        def receipt(operation, result, *, status="confirmed"):
            self.controller.receipt("grove.rule_action", operation, {"commit_seq": 4, "result": result},
                status=status, operation_record={"task_id": task, "envelope": {"arguments": {"target_id": "item"}}})
        receipt("unstructured", {"changes": ["消耗了999u木料。"], "entity": {
            "id": "item", "material_quantity": {"wood": 999}}, "action_result": {"transfer": {"transferred": 12}}})
        receipt("missing", {})
        receipt("invalid", {"action_result": {"costs": [{"material": "wood", "quantity_u": True}]}})
        for status in ("committed", "unknown", "rejected"):
            receipt(status, {"action_result": {"costs": [{"material": "wood", "quantity_u": 100}]}}, status=status)
        receipt("actual", {"action_result": {"costs": [{"material": "canonical_wood", "quantity_u": 3},
            {"material": "canonical_wood", "quantity_u": 4}, {"material": "cloth", "quantity_u": 2}]}})
        summary = self.controller.action_summary()
        self.assertEqual(summary["material_costs"], [{"material": "canonical_wood", "quantity_u": 7},
            {"material": "cloth", "quantity_u": 2}])
        self.assertEqual(summary["confirmed_count"], 4)
        self.assertEqual(summary["coverage"]["structured_cost_operations"], 1)
        self.assertEqual(summary["coverage"]["invalid_cost_operations"], 1)
        self.assertEqual(summary["coverage"]["operations_without_structured_costs"], 2)
        self.assertEqual(summary["coverage"]["confirmed_operations_without_outcome"], 1)
        self.assertEqual(summary["coverage"]["committed_unconfirmed_count"], 1)
        self.assertEqual(summary["coverage"]["unknown_count"], 1)

    def test_task_summary_coverage_preserves_old_uncertainty_and_marks_evicted_action_history(self):
        self.start()
        old = self.controller.snapshot()
        del old["receipt_history_complete"]
        self.controller.restore(old)
        self.assertFalse(self.controller.action_summary()["coverage"]["receipt_history_complete"])
        # A genuinely new task has complete provenance until its actual bounded
        # action history is evicted. This is not a derived cumulative ledger.
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="New goal")
        self.assertTrue(self.controller.action_summary()["coverage"]["receipt_history_complete"])
        self.controller.request_limit = self.controller.world_call_limit = 1
        task = self.controller.state["task_id"]
        for operation in ("old-cost", "new-cost"):
            self.controller.receipt("grove.rule_action", operation, {"commit_seq": 5, "result": {
                "action_result": {"costs": [{"material": "wood", "quantity_u": 10}]}}},
                status="confirmed", operation_record={"task_id": task})
        summary = self.controller.action_summary()
        self.assertEqual(summary["confirmed_count"], 1)
        self.assertEqual(summary["material_costs"], [{"material": "wood", "quantity_u": 10}])
        self.assertFalse(summary["coverage"]["receipt_history_complete"])
        restored = self.controller.snapshot()
        self.controller.restore(restored)
        self.assertFalse(self.controller.action_summary()["coverage"]["receipt_history_complete"])

    def test_task_cost_aggregation_survives_omitted_group_details(self):
        self.start(max_actions=64)
        task = self.controller.state["task_id"]
        for index in range(12):
            self.controller.receipt("grove.rule_action", "fuel-" + str(index), {"commit_seq": index + 1,
                "result": {"action_result": {"costs": [{"material": "wood", "quantity_u": index + 1}]}}},
                status="confirmed", operation_record={"task_id": task, "envelope": {"arguments": {
                    "action": "fuel", "target_id": "source-" + str(index)}}})
        summary = self.controller.action_summary()
        self.assertEqual(summary["material_costs"], [{"material": "wood", "quantity_u": 78}])
        self.assertEqual(summary["confirmed_count"], 12)
        self.assertGreaterEqual(summary["omitted_groups"], 4)
        self.assertEqual(summary["coverage"]["omitted_material_cost_rows"], 0)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")), 8 * 1024)

    def test_large_distinct_cost_catalog_omits_rows_explicitly_not_as_a_complete_total(self):
        self.start(max_actions=64)
        task = self.controller.state["task_id"]
        for index in range(64):
            material = "material-" + str(index) + "-" + "a" * 120
            self.controller.receipt("grove.rule_action", "cost-" + str(index), {"commit_seq": index + 1,
                "result": {"action_result": {"costs": [{"material": material, "quantity_u": 1}]}}},
                status="confirmed", operation_record={"task_id": task})
        before = deepcopy(self.controller.state)
        summary = self.controller.action_summary()
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")), 8 * 1024)
        self.assertGreater(summary["coverage"]["omitted_material_cost_rows"], 0)
        self.assertEqual(len(summary["material_costs"]) + summary["coverage"]["omitted_material_cost_rows"], 64)
        self.assertEqual(summary["coverage"]["structured_cost_operations"], 64)
        self.assertEqual(summary["confirmed_count"], 64)
        self.assertTrue(all(row["quantity_u"] == 1 for row in summary["material_costs"]))
        self.assertEqual(self.controller.state, before)

    async def test_actual_action_readback_keeps_selection_and_distinguishes_commit_from_later_state(self):
        await self.running()
        self.controller.state["context_entity_id"] = "original-contract"
        current_energy = 19
        reads = []
        original_invoke, original_read = self.invoke, self.read
        async def invoke(function, arguments, **kwargs):
            if function != "grove.rule_action":
                return await original_invoke(function, arguments, **kwargs)
            self.control["actions_count"] += 1
            payload = {"ok": True, "operation_id": "actual-fuel", "commit_seq": 77,
                "result": {"entity": {"id": "actual-wheel", "version": 4,
                    "state": {"program": {"fields": {"signal": 0}, "mechanical": {"energy": 20, "angle": 0}}}},
                    "action_result": {"stored_energy_after": 20}}}
            self.receipts["actual-fuel"] = deepcopy(payload)
            owner = self.controller.operation_owner(function, arguments, model_write=True)
            self.controller.receipt(function, "actual-fuel", payload, status="committed",
                operation_record={"envelope": {"arguments": arguments}, **owner})
            return payload
        async def read(kind, **kwargs):
            if kind == "receipt":
                return await original_read(kind, **kwargs)
            reads.append(deepcopy(kwargs))
            return {"selected_inspection": {"entity": {"id": "original-contract", "version": 3}},
                "action_targets": [{"entity_id": "actual-wheel", "observed_at": 123,
                    "inspection": {"entity": {"id": "actual-wheel", "version": 5,
                        "state": {"program": {"mechanical": {"energy": current_energy, "angle": 3}}}}}},
                    {"entity_id": "spent-lot", "unavailable": {"code": "ResourceNotFound"}, "observed_at": 124}],
                "observed_at": 125}
        self.controller.ports = replace(self.controller.ports, invoke=invoke, read=read)
        reply = await self.controller.write("grove.rule_action", {"target_id": "actual-wheel", "item_id": "spent-lot"},
            segment_id="current-segment")
        self.assertEqual(reads[0], {"context_entity_id": "original-contract", "action_entity_ids": ["actual-wheel", "spent-lot"]})
        self.assertEqual(reply["confirmed_outcome"]["entity"]["state"]["program"]["mechanical"]["energy"], 20)
        self.assertEqual(reply["execution_readback"]["action_targets"][0]["inspection"]["entity"]["state"]["program"]["mechanical"]["energy"], 19)
        self.assertEqual(self.controller.state["context_entity_id"], "original-contract")
        current_energy = 17
        checkpoint = await self.controller.checkpoint({"task_id": self.controller.state["task_id"],
            "execution_segment_id": "current-segment", "status": "completed", "summary": "The model says energy is still 20",
            "next_intent": "", "evidence_operation_ids": ["actual-fuel"]}, schema=CHECKPOINT_SCHEMA)
        self.assertEqual(checkpoint["execution_readback"]["action_targets"][0]["inspection"]["entity"]["state"]["program"]["mechanical"]["energy"], 17)
        self.assertEqual(checkpoint["goal_result"]["verification"]["status"], "unverified")
        self.assertEqual(checkpoint["goal_result"]["summary"], "The model says energy is still 20")
        self.assertEqual(len(reads), 2)

    def test_recovered_action_targets_stay_with_original_task_and_are_bounded(self):
        self.start()
        task = self.controller.state["task_id"]
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="Replacement")
        current = self.controller.state["task_id"]
        record = {"task_id": task, "envelope": {"arguments": {"target_id": "old-target", "item_id": "old-source"}}}
        self.controller.receipt("grove.interact", "recovered-original", {"commit_seq": 9},
            status="confirmed", operation_record=record)
        self.assertEqual(self.controller.state["task_id"], current)
        self.assertEqual(self.controller.state["action_entity_ids"], [])
        self.assertEqual(self.controller.archive[-1]["action_entity_ids"], ["old-target", "old-source"])
        for index in range(9):
            self.controller.receipt("grove.interact", "new-" + str(index), {"commit_seq": 10 + index},
                status="confirmed", operation_record={"task_id": current,
                    "envelope": {"arguments": {"target_id": "target-" + str(index)}}})
        self.assertEqual(self.controller.state["action_entity_ids"], ["target-8", "target-7", "target-6", "target-5"])

    async def running(self):
        self.start()
        await self.invoke("grove.control_grant", {"goal": self.controller.state["goal"], "max_actions": 4})
        self.controller.state.update(status="running", segment_id="current-segment")
        self.controller.active = True

    async def test_each_checkpoint_returns_latest_facts_and_seals_only_its_segment(self):
        for status in ("continue", "blocked", "completed"):
            with self.subTest(status=status):
                self.setUp()
                await self.running()
                action = await self.controller.write("grove.move", {"dx": 1}, segment_id="current-segment")
                fresh = {"player": {"id": "same-role", "version": 9}, "clock": {"sequence": 91},
                         "entities": [{"id": "wheel", "version": 8, "angle": 36, "energy": 4}]}
                original_read = self.read
                async def read(kind, **kwargs):
                    return deepcopy(fresh) if kind == "facts" else await original_read(kind, **kwargs)
                self.controller.ports = replace(self.controller.ports, read=read)
                result = await self.controller.checkpoint({"task_id": self.controller.state["task_id"],
                    "execution_segment_id": "current-segment", "status": status, "summary": "old fuel 16",
                    "next_intent": "", "evidence_operation_ids": [action["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
                self.assertEqual(result["execution_readback"], fresh | {"task_action_summary": self.controller.action_summary()})
                self.assertFalse(result["world_facts_changed"])
                self.assertFalse(self.controller.active)
                with self.assertRaises(ConnectorError) as sealed:
                    self.controller.admit_write("current-segment")
                self.assertEqual(sealed.exception.code, "ExecutionSegmentFinished")
                await self.controller.revoke()
                with self.assertRaises(ConnectorError) as revoked:
                    self.controller.admit_write("current-segment")
                self.assertNotIn("person reclaimed", revoked.exception.message)
                if status == "completed":
                    self.assertEqual(result["goal_result"]["verification"]["status"], "unverified")

    async def test_failed_checkpoint_read_does_not_reuse_old_freshness_or_seal(self):
        await self.running()
        action = await self.controller.write("grove.move", {}, segment_id="current-segment")
        self.controller.ports = replace(self.controller.ports, read=AsyncMock(side_effect=OSError("read unavailable")))
        with self.assertRaises(ConnectorError) as failed:
            await self.controller.checkpoint({"task_id": self.controller.state["task_id"],
                "execution_segment_id": "current-segment", "status": "completed", "summary": "claim",
                "next_intent": "", "evidence_operation_ids": [action["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
        self.assertFalse(self.controller.state["readback_actual"])
        self.assertFalse(failed.exception.execution_outcome["checkpoint_recorded"])
        self.assertIsNone(self.controller.state.get("checkpoint"))
        self.assertTrue(self.controller.active)
        self.controller.ports = replace(self.controller.ports, read=self.read)
        result = await self.controller.checkpoint({"task_id": self.controller.state["task_id"],
            "execution_segment_id": "current-segment", "status": "completed", "summary": "claim",
            "next_intent": "", "evidence_operation_ids": [action["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
        self.assertTrue(result["segment_finished"])

    async def test_postcommit_read_or_persist_failure_keeps_original_receipt(self):
        for failure in ("read", "persist"):
            with self.subTest(failure=failure):
                self.setUp()
                await self.running()
                if failure == "read":
                    import httpx
                    self.controller.ports = replace(self.controller.ports, read=AsyncMock(side_effect=httpx.ReadError("disconnected")))
                else:
                    async def invoke(*args, **kwargs):
                        result = await self.invoke(*args, **kwargs)
                        self.persist.side_effect = OSError("storage unavailable")
                        return result
                    self.controller.ports = replace(self.controller.ports, invoke=invoke)
                with self.assertRaises(ConnectorError) as failed:
                    await self.controller.write("grove.move", {}, segment_id="current-segment")
                outcome = failed.exception.execution_outcome
                self.assertTrue(outcome["world_action_committed"])
                self.assertIn(outcome["operation_id"], self.receipts)
                self.assertEqual(outcome["commit_seq"], self.receipts[outcome["operation_id"]]["commit_seq"])
                self.assertEqual(sum(function == "grove.move" for function, _ in self.calls), 1)

    def test_snapshot_and_unsettled_admission_keep_task_authority(self):
        self.start()
        self.controller.state.update(status="paused", request_status="unsettled", readback={"nested": {"x": 1}})
        snapshot = self.controller.snapshot()
        snapshot["readback"]["nested"]["x"] = 99
        snapshot["request_status"] = "idle_confirmed"
        self.assertEqual(self.controller.state["readback"]["nested"]["x"], 1)
        decisions = self.controller.admission(entered=True, native_connected=True,
            worker_busy=False, catalog=CATALOG)
        self.assertTrue(all(not item["allowed"] for item in decisions.values()))
        self.assertEqual(decisions["conversation"]["code"], "OriginalRequestUnsettled")

    def test_late_settlement_cannot_release_replacement_task(self):
        self.start()
        owner = self.controller.settlement_started("session_identity")
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="Replacement goal")
        before = deepcopy(self.controller.state)
        self.assertIsNone(self.controller.settlement_finished(stage="session_status", elapsed_seconds=0, owner=owner))
        self.assertEqual(self.controller.state, before)

    def setUp(self):
        self.persist = Mock()
        self.calls, self.receipts, self.replays = [], {}, []
        self.control = {"status": "idle", "actions_count": 0}
        self.unknown_operations = []
        self.native_action = None
        self.controller = ExecutionController("same-role", max_requests=8, max_world_calls=16, max_seconds=150,
            ports=ExecutionPorts(invoke=self.invoke, read=self.read, recover=self.recover, unknown_operations=lambda: self.unknown_operations,
                native_segment=self.native_segment, settle=self.settle, persist=self.persist))

    def start(self, **options):
        return self.controller.start({"goal": "Explore the actual shared consequence", "max_actions": 4,
            "max_requests": 4} | options, entered=True, native_connected=True,
            worker_busy=False, catalog=CATALOG)

    def test_unconfirmed_old_request_cannot_be_replaced_after_reclaim_worker_finishes(self):
        self.start()
        self.controller.state.update(status="paused", request_status="unsettled")
        original = deepcopy(self.controller.state)
        self.controller.begin_reclaim()
        self.controller.finish_reclaim()
        with self.assertRaises(ConnectorError) as rejected:
            self.start(goal="A different new intention")
        self.assertEqual(rejected.exception.code, "OriginalRequestUnsettled")
        self.assertEqual(self.controller.state["task_id"], original["task_id"])
        self.assertEqual(self.controller.state["goal"], original["goal"])
        self.assertFalse(self.controller.archive)
        self.controller.state["request_status"] = "idle_confirmed"
        self.start(goal="A different new intention")
        self.assertNotEqual(self.controller.state["task_id"], original["task_id"])

    def test_goal_without_any_started_request_can_be_replaced(self):
        self.start()
        previous = self.controller.state['task_id']
        self.controller.state.update(request_status='not_started', requests_used=0)
        self.start(goal='A new intention before any request was sent')
        self.assertNotEqual(self.controller.state['task_id'], previous)
        self.assertEqual(self.controller.archive[-1]['task_id'], previous)

    async def invoke(self, function, arguments, *, model_write=False):
        self.calls.append((function, deepcopy(arguments)))
        if function == "grove.control_status":
            return {"result": deepcopy(self.control)}
        operation = "original-" + str(len(self.calls))
        owner = self.controller.operation_owner(function, arguments, model_write=model_write)
        record = {"function_id": function, "envelope": {"operation_id": operation}} | owner
        if function == "grove.control_grant":
            self.control = {"goal": arguments["goal"], "status": "active", "actions_count": 0,
                "delegation": "private-original-grant", "grant_id": "grove-grant-" + operation,
                "max_actions": arguments["max_actions"], "version": 1}
        elif function == "grove.control_revoke":
            self.control.update(status="revoked", delegation="")
        elif function == "grove.move":
            self.assertEqual(arguments["delegation"], "private-original-grant")
            self.control["actions_count"] += 1
        else:
            raise AssertionError(function)
        payload = {"operation_id": operation, "commit_seq": len(self.calls), "result": deepcopy(self.control)}
        self.receipts[operation] = payload
        self.controller.observe_control(function, payload, operation_record=record)
        if model_write:
            self.controller.receipt(function, operation, payload, status="committed", operation_record=record)
        return payload

    async def read(self, kind, *, operation_id=None, context_entity_id=None):
        if kind == "receipt":
            return deepcopy(self.receipts[operation_id])
        self.assertEqual(kind, "facts")
        return {"player": {"id": "same-role", "x": self.control["actions_count"], "y": 0}, "entities": []}

    async def recover(self):
        # The controller port settles original operations and cannot replay.
        self.replays.append(False)

    async def settle(self, *, abort=False):
        owner = self.controller.settlement_started("session_identity", native_owner=self.controller.state["host_request"])
        self.controller.native_status_observed("idle", owner=owner)
        self.controller.settlement_finished(stage="write_drain", elapsed_seconds=0, owner=owner)
        return True

    async def native_segment(self, kind, goal, *, context_entity_id=None):
        self.assertEqual(kind, "execute")
        self.controller.request_started("execution")
        if self.native_action is not None:
            await self.native_action()

    def test_new_task_is_initialized_by_controller_and_old_task_is_archived(self):
        self.start()
        first = deepcopy(self.controller.state)
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="Another explicitly granted choice")
        self.assertNotEqual(self.controller.state["task_id"], first["task_id"])
        self.assertEqual(self.controller.archive[0]["task_id"], first["task_id"])
        self.assertEqual(self.controller.state["receipts"], [])
        self.assertFalse(self.controller.active)

    def test_invalid_start_preserves_original_task(self):
        self.start()
        original = deepcopy(self.controller.state)
        with self.assertRaises(ConnectorError) as blocked:
            self.start(max_requests=True)
        self.assertEqual(blocked.exception.code, "InvalidRequestBudget")
        self.assertEqual(self.controller.state, original)

    def test_manual_ready_reclaim_does_not_admit_a_new_partner_goal(self):
        self.start()
        original = deepcopy(self.controller.state)
        self.controller.begin_reclaim()
        self.controller.record_reclaimed()
        with self.assertRaises(ConnectorError) as blocked:
            self.start(goal="Late replacement")
        self.assertEqual(blocked.exception.code, "ReclaimBusy")
        self.assertEqual(self.controller.state["task_id"], original["task_id"])
        self.controller.record_reclaimed()
        with self.assertRaises(ConnectorError):
            self.start(goal="Still cleaning up the original abort")
        self.controller.state["request_status"] = "idle_confirmed"
        self.controller.finish_reclaim()
        self.start(goal="After whole original cleanup")

    def test_plain_request_stop_also_waits_for_native_idle_without_a_goal(self):
        self.controller.begin_reclaim()
        self.controller.finish_reclaim()
        with self.assertRaises(ConnectorError) as rejected:
            self.controller.ensure_native_ready()
        self.assertEqual(rejected.exception.code, "OriginalRequestUnsettled")
        self.assertFalse(self.controller.state["task_id"])
        self.controller.state["request_status"] = "idle_confirmed"
        self.controller.ensure_native_ready()
        self.start()

    def test_late_receipt_is_attributed_to_original_task_and_segment(self):
        self.start()
        task_id = self.controller.state["task_id"]
        self.controller.state["segment_id"] = "original-segment"
        record = {"task_id": task_id, "segment_id": "original-segment", "execution_write": True}
        self.controller.receipt("grove.move", "original-write", status="unknown", operation_record=record)
        self.controller.state.update(status="paused", request_status="idle_confirmed")
        self.start(goal="Different authorized task")
        self.controller.receipt("grove.move", "original-write", {"commit_seq": 7}, status="confirmed", operation_record=record)
        self.assertEqual(self.controller.state["receipts"], [])
        receipt = self.controller.archive[-1]["receipts"][0]
        self.assertEqual((receipt["task_id"], receipt["segment_id"], receipt["status"]),
            (task_id, "original-segment", "confirmed"))

    def test_old_unknown_receipt_cannot_turn_current_task_unknown(self):
        self.start()
        old = self.controller.state["task_id"]
        self.start(goal="Current choice")
        self.controller.receipt("grove.move", "late-old-write", status="unknown",
            operation_record={"task_id": old, "segment_id": "old-segment"})
        self.assertEqual(self.controller.state["status"], "starting")
        self.assertEqual(self.controller.state["receipts"], [])

    def test_late_old_grant_cannot_authorize_current_task(self):
        self.start()
        old = self.controller.state["task_id"]
        self.start()
        self.controller.observe_control("grove.control_grant", {"result": {
            "goal": self.controller.state["goal"], "status": "active", "actions_count": 3,
            "delegation": "late-grant", "version": 9}}, operation_record={"task_id": old,
                "envelope": {"operation_id": "old-grant"}})
        self.assertFalse(self.controller.state["delegation_active"])
        self.assertIsNone(self.controller.state["grant_operation_id"])
        self.assertEqual(self.controller.state["actions_count"], 0)
        with self.assertRaises(ConnectorError):
            self.start(goal="Must revoke the late original grant first")

    def test_restart_keeps_same_task_and_closes_action_admission(self):
        self.start()
        original = deepcopy(self.controller.state)
        original.update(status="running", segment_id="saved-segment")
        restored = ExecutionController("same-role", max_requests=8, max_world_calls=16, max_seconds=150)
        restored.restore(original, delegation="saved-private-grant")
        self.assertEqual(restored.state["task_id"], original["task_id"])
        self.assertEqual(restored.state["segment_id"], "saved-segment")
        self.assertEqual(restored.state["status"], "paused")
        self.assertFalse(restored.active)
        self.assertFalse(restored.can_resume())

    def test_execution_timeout_restores_its_own_scope_after_previous_ordinary_request(self):
        for purpose in ("execution", "execution_summary"):
            with self.subTest(purpose=purpose):
                self.setUp()
                self.start()
                self.controller.request_started("entry")
                self.controller.request_returned("entry")
                self.controller.state["segment_id"] = "original-segment"
                owner = self.controller.request_started(purpose)
                self.controller.native_interrupted(purpose, owner=owner)
                saved = self.controller.snapshot()
                restored = ExecutionController("same-role", max_requests=8, max_world_calls=16, max_seconds=150)
                restored.restore(saved)
                self.assertEqual(restored.state["host_request"]["purpose"], purpose)
                self.assertEqual(restored.state["host_request"]["status"], "unsettled")
                settlement = restored.settlement_started("abort", native_owner=restored.value("host_request"))
                self.assertEqual(settlement["native_purpose"], "execution")
                restored.native_status_observed("idle", owner=settlement)
                restored.settlement_finished(stage="write_drain", elapsed_seconds=0, owner=settlement)
                self.assertEqual(restored.state["request_status"], "idle_confirmed")
                self.assertEqual(restored.state["task_id"], saved["task_id"])
                self.assertEqual(restored.state["requests_used"], 1)
                self.assertTrue(restored.can_resume())

    def test_explicit_resume_keeps_task_action_ledger_and_extends_only_requests(self):
        self.start(max_requests=2)
        task = self.controller.state
        original = task["task_id"]
        task.update(status="paused", request_status="idle_confirmed", requests_used=2,
            actions_count=1, resume_allowed=False)
        self.controller.receipt("grove.move", "owned-action", {"commit_seq": 1}, status="confirmed",
            task_id=original, segment_id="previous-segment")
        result = self.controller.resume({"task_id": original, "additional_requests": 1}, entered=True,
            native_connected=True, worker_busy=False)
        self.assertEqual(result["mode"], "execute-resume")
        self.assertEqual((task["task_id"], task["max_actions"], task["actions_count"], task["max_requests"], task["requests_used"]),
            (original, 4, 1, 3, 2))
        self.assertEqual(task["receipts"][0]["operation_id"], "owned-action")
        self.assertEqual(task["budget_extensions"][0]["additional_requests"], 1)

    async def test_unknown_original_stops_with_no_replay_or_new_native_segment(self):
        self.start()
        async def action():
            self.unknown_operations.append({"function_id": "grove.move",
                "envelope": {"operation_id": "original-unknown"}})
        self.native_action = action
        await self.controller.execute_goal(self.controller.state["goal"])
        self.assertEqual(self.controller.state["requests_used"], 1)
        self.assertEqual(self.controller.state["status"], "unknown")
        self.assertFalse(self.controller.state["resume_allowed"])
        self.assertEqual(self.replays, [False])

    async def test_native_failure_pause_preserves_actual_error_and_original_goal(self):
        self.start()
        original_task = self.controller.state["task_id"]
        message = "模型服务返回错误（HTTP 400），没有完成有效回应。"
        async def failed():
            raise ConnectorError("IncompleteResponse", message, 502)
        self.native_action = failed
        await self.controller.execute_goal(self.controller.state["goal"])
        self.assertEqual(self.controller.state["stop_reason"], message)
        self.assertEqual(self.controller.state["error"], {"code": "IncompleteResponse", "message": message})
        self.assertEqual(self.controller.state["status"], "paused")
        self.assertEqual(self.controller.state["request_status"], "idle_confirmed")
        self.assertEqual(self.controller.state["task_id"], original_task)
        self.assertEqual(self.controller.state["requests_used"], 1)
        self.assertEqual(self.controller.state["actions_count"], 0)

    async def test_internal_index_after_real_progress_pauses_without_another_native_request(self):
        self.start()
        original_task = self.controller.state["task_id"]
        original_goal = self.controller.state["goal"]
        message = "模型返回了内部历史索引，没有形成可展示的回答；已确认行动保留，未自动重试。"
        async def action_then_artifact():
            await self.controller.write("grove.move", {"dx": 1, "dy": 0},
                segment_id=self.controller.state["segment_id"])
            raise ConnectorError("InternalContextResponse", message, 502)
        self.native_action = AsyncMock(side_effect=action_then_artifact)
        await self.controller.execute_goal(original_goal)
        self.native_action.assert_awaited_once()
        task = self.controller.state
        self.assertEqual((task["requests_used"], task["max_requests"], task["actions_count"]), (1, 4, 1))
        self.assertEqual(task["task_id"], original_task)
        self.assertEqual(task["goal"], original_goal)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["request_status"], "idle_confirmed")
        self.assertEqual(task["error"], {"code": "InternalContextResponse", "message": message})
        self.assertEqual(task["stop_reason"], message)
        self.assertEqual(task["segments"][-1]["error"], "InternalContextResponse")
        self.assertTrue(task["segments"][-1]["progress"])
        self.assertEqual(len(task["receipts"]), 1)
        self.assertEqual(task["receipts"][0]["status"], "confirmed")
        self.assertIn(task["receipts"][0]["operation_id"], self.receipts)
        self.assertEqual(self.replays, [False])

    async def test_stale_segment_and_reclaimed_authority_do_not_invoke_world(self):
        self.start()
        task = self.controller.state
        await self.invoke("grove.control_grant", {"goal": task["goal"], "max_actions": 4})
        task.update(status="running", segment_id="current-segment")
        self.controller.active = True
        before = len(self.calls)
        with self.assertRaises(ConnectorError) as stale:
            await self.controller.write("grove.move", {"dx": 1, "dy": 0}, segment_id="old-segment")
        self.assertEqual(stale.exception.code, "StaleExecutionSegment")
        self.controller.begin_reclaim()
        with self.assertRaises(ConnectorError):
            await self.controller.write("grove.move", {"dx": 1, "dy": 0}, segment_id="current-segment")
        self.assertEqual(len(self.calls), before)

    async def test_two_native_segments_share_goal_and_confirmed_original_evidence(self):
        self.start()
        task = self.controller.state
        async def action():
            await self.controller.write("grove.move", {"dx": 1, "dy": 0}, segment_id=task["segment_id"])
            evidence = [item["operation_id"] for item in task["receipts"] if item["status"] == "confirmed"]
            await self.controller.checkpoint({"task_id": task["task_id"], "execution_segment_id": task["segment_id"],
                "status": "continue" if task["requests_used"] == 1 else "completed", "summary": "Actual public progress",
                "next_intent": "Inspect the consequence", "evidence_operation_ids": evidence}, schema=CHECKPOINT_SCHEMA)
        self.native_action = action
        await self.controller.execute_goal(task["goal"])
        self.assertEqual((task["requests_used"], task["actions_count"], task["status"]), (2, 2, "completed"))
        self.assertEqual(len(task["goal_result"]["evidence_operation_ids"]), 2)
        self.assertEqual(task["goal_result"]["verification"]["status"], "unverified")
        self.assertTrue(all(item["task_id"] == task["task_id"] and item["segment_id"] for item in task["receipts"]))
        self.assertEqual(self.replays, [False, False])

    def test_request_counter_enforces_original_total_and_stopped_boundary(self):
        self.start(max_requests=1)
        first = self.controller.request_started("execution")
        self.assertEqual(first["request_number"], 1)
        with self.assertRaises(ConnectorError) as exhausted:
            self.controller.request_started("execution")
        self.assertEqual(exhausted.exception.code, "ExecutionRequestBudget")
        self.controller.begin_reclaim()
        with self.assertRaises(asyncio.CancelledError):
            self.controller.request_started("execution_summary")

    def segment_yield(self, **changes):
        return ConnectorError("ExecutionSegmentYield", "Original request safely yielded.", 409,
            execution_outcome={"stage": "native_segment_yield", "task_id": self.controller.state["task_id"],
                "segment_id": self.controller.state["segment_id"], "safe": True, "reason": "request_headroom",
                "request_id": "grove-request-" + "a" * 32,
                "current_terminal_tool_parts": 1, "current_unfinished_tool_parts": 0,
                "body_bytes": 98304, **changes})

    async def test_safe_yield_preserves_task_goal_grant_and_receipts_after_settlement(self):
        self.start(max_requests=2)
        task = self.controller.state
        identity, goal = task["task_id"], task["goal"]
        segments = []
        async def action():
            segments.append(task["segment_id"])
            if task["requests_used"] == 1:
                await self.controller.write("grove.move", {"dx": 1}, segment_id=task["segment_id"])
                raise self.segment_yield()
            self.assertEqual(task["request_status"], "running")
            self.assertTrue(task["readback_actual"])
            await self.controller.checkpoint({"task_id": identity, "execution_segment_id": task["segment_id"],
                "status": "completed", "summary": "Actual receipt retained", "next_intent": "",
                "evidence_operation_ids": [task["receipts"][0]["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
        self.native_action = action
        await self.controller.execute_goal(goal)
        self.assertEqual((task["task_id"], task["goal"], task["requests_used"], task["actions_count"]), (identity, goal, 2, 1))
        self.assertEqual(task["status"], "completed")
        self.assertNotEqual(*segments)
        self.assertEqual(sum(function == "grove.control_grant" for function, _ in self.calls), 1)
        self.assertEqual(task["segments"][0]["outcome"], "context_yield")
        self.assertIsNone(task["segments"][0]["error"])
        self.assertEqual(self.replays, [False, False])

    async def test_yield_never_bypasses_unknown_original_or_exhausted_budget(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                self.setUp()
                self.start(max_requests=1)
                async def action():
                    if unknown:
                        self.unknown_operations.append({"function_id": "grove.move",
                            "envelope": {"operation_id": "original-unknown"}})
                    raise self.segment_yield()
                self.native_action = action
                await self.controller.execute_goal(self.controller.state["goal"])
                self.assertEqual(self.controller.state["requests_used"], 1)
                self.assertEqual(self.controller.state["max_requests"], 1)
                self.assertEqual(self.controller.state["status"], "unknown" if unknown else "paused")
                self.assertFalse(self.controller.state["resume_allowed"])

    async def test_forged_or_unfinished_yield_pauses_without_a_new_request(self):
        for changes in ({"task_id": "old-task"}, {"safe": False}, {"current_unfinished_tool_parts": 1},
                        {"current_terminal_tool_parts": 0}, {"body_bytes": 98303}):
            with self.subTest(changes=changes):
                self.setUp()
                self.start()
                async def action():
                    raise self.segment_yield(**changes)
                self.native_action = action
                await self.controller.execute_goal(self.controller.state["goal"])
                self.assertEqual(self.controller.state["requests_used"], 1)
                self.assertEqual(self.controller.state["error"]["code"], "ExecutionSegmentYield")

    async def test_matching_goal_or_legacy_grant_cannot_replace_owned_grant(self):
        self.start()
        await self.invoke("grove.control_grant", {"goal": self.controller.state["goal"], "max_actions": 4})
        owned = deepcopy(self.control)
        self.controller.update_control({**owned, "grant_id": "another-grant", "actions_count": 3})
        self.assertFalse(self.controller.state["delegation_active"])
        self.assertEqual(self.controller.state["actions_count"], 0)
        self.controller.update_control({key: value for key, value in owned.items() if key != "grant_id"})
        self.assertFalse(self.controller.state["delegation_active"])
        self.controller.update_control(owned)
        self.assertTrue(self.controller.state["delegation_active"])
        self.assertEqual(self.controller.state["grant_id"], owned["grant_id"])

    def test_explicit_batches_reach_32_without_resetting_spent_request_or_action_budget(self):
        self.start(max_requests=8)
        task = self.controller.state
        identity = task["task_id"]
        task.update(status="paused", request_status="idle_confirmed", requests_used=8, actions_count=1,
            grant_operation_id="owned-original", resume_allowed=False)
        for total in (16, 24, 32):
            self.controller.resume({"task_id": identity, "additional_requests": 8}, entered=True,
                native_connected=True, worker_busy=False)
            self.assertEqual((task["task_id"], task["max_requests"], task["requests_used"], task["actions_count"], task["max_actions"]),
                (identity, total, 8, 1, 4))
            task["status"] = "paused"
        for additional in (1, 9):
            with self.assertRaises(ConnectorError) as exceeded:
                self.controller.resume({"task_id": identity, "additional_requests": additional}, entered=True,
                    native_connected=True, worker_busy=False)
            self.assertEqual(exceeded.exception.code, "InvalidRequestBudget")

    def test_ordinary_request_preserves_paused_task_after_success_and_interruption(self):
        for returned in (False, True):
            with self.subTest(returned=returned):
                self.setUp()
                self.start()
                self.controller.state.update(status="paused", request_status="idle_confirmed", resume_allowed=True)
                original = {key: deepcopy(value) for key, value in self.controller.state.items() if key != "host_request"}
                self.controller.request_started("explain")
                self.assertEqual({key: value for key, value in self.controller.state.items() if key != "host_request"}, original)
                self.assertFalse(self.controller.can_resume())
                if returned:
                    self.controller.request_returned("explain")
                else:
                    self.controller.native_interrupted("explain")
                owner = self.controller.settlement_started("session_identity", native_owner={"purpose": "explain"})
                self.controller.native_status_observed("idle", owner=owner)
                self.controller.settlement_finished(stage="write_drain", elapsed_seconds=0, owner=owner)
                self.assertEqual({key: value for key, value in self.controller.state.items() if key != "host_request"}, original)
                self.assertTrue(self.controller.can_resume())

    async def test_checkpoint_save_failure_cannot_complete_or_yield_until_original_recovery(self):
        await self.running()
        task = self.controller.state
        action = await self.controller.write("grove.move", {"dx": 1}, segment_id=task["segment_id"])
        def save():
            if task.get("checkpoint"):
                raise OSError("checkpoint save unavailable")
        self.persist.side_effect = save
        with self.assertRaises(ConnectorError) as failed:
            await self.controller.checkpoint({"task_id": task["task_id"], "execution_segment_id": task["segment_id"],
                "status": "completed", "summary": "Actual action, unpersisted claim", "next_intent": "",
                "evidence_operation_ids": [action["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
        self.assertFalse(failed.exception.execution_outcome["checkpoint_recorded"])
        self.assertIsNone(task["checkpoint"])
        self.assertIsNone(task["goal_result"])
        self.assertFalse(self.controller.active)
        self.assertEqual(task["persistence_failure"]["error_type"], "OSError")
        self.persist.side_effect = None
        for call in (self.controller.ensure_persistence_ready,
                     lambda: self.controller.request_started("execution"),
                     lambda: self.controller.admit_write(task["segment_id"])):
            with self.assertRaises(ConnectorError) as blocked:
                call()
            self.assertEqual(blocked.exception.code, "ExecutionPersistenceFailed")
        self.controller.recovery_finished()
        self.assertIsNotNone(task["persistence_failure"])
        await self.settle()
        await self.controller.revoke()
        await self.controller.readback()
        self.controller.recovered_world_authority()
        self.controller.recovery_finished()
        self.assertIsNone(task["persistence_failure"])
        self.assertEqual(task["actions_count"], 1)
        self.assertEqual(len(task["receipts"]), 1)
        self.assertTrue(self.controller.can_resume())

    async def test_effective_preparation_can_continue_normal_and_safe_yield_segments(self):
        self.start(max_requests=3)
        task = self.controller.state
        async def action():
            owner = {"purpose": "execution", "task_id": task["task_id"], "segment_id": task["segment_id"]}
            if task["requests_used"] <= 2:
                topic = "materials" if task["requests_used"] == 1 else "presentation"
                self.assertTrue(self.controller.preparation_read_delivered("grove.rule_catalog", {"topic": topic},
                    owner=owner, delivered={"result": {"topic": topic}}))
                if task["requests_used"] == 2:
                    raise self.segment_yield()
                return
            await self.controller.write("grove.move", {"dx": 1}, segment_id=task["segment_id"])
            await self.controller.checkpoint({"task_id": task["task_id"], "execution_segment_id": task["segment_id"],
                "status": "completed", "summary": "Actual consequence", "next_intent": "",
                "evidence_operation_ids": [task["receipts"][0]["operation_id"]]}, schema=CHECKPOINT_SCHEMA)
        self.native_action = action
        await self.controller.execute_goal(task["goal"])
        self.assertEqual((task["requests_used"], task["preparation_count"], task["actions_count"], task["status"]), (3, 2, 1, "completed"))
        self.assertEqual(task["segments"][1]["outcome"], "context_yield")

    async def test_preparation_does_not_retry_ordinary_failure_or_count_repeated_empty_pages(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.setUp()
                self.start()
                task = self.controller.state
                async def action():
                    owner = {"purpose": "execution", "task_id": task["task_id"], "segment_id": task["segment_id"]}
                    self.controller.preparation_read_delivered("grove.public_works", {"author": "person", "after": str(task["requests_used"])},
                        owner=owner, delivered={"result": {"works": [], "has_more": True}})
                    if failure:
                        raise ConnectorError("IncompleteResponse", "Real model HTTP 400", 502)
                self.native_action = action
                await self.controller.execute_goal(task["goal"])
                self.assertEqual(task["preparation_count"], 1)
                self.assertEqual(task["actions_count"], 0)
                self.assertEqual(task["requests_used"], 1 if failure else 3)
                self.assertEqual(task["status"], "paused")
                self.assertIsNone(task["goal_result"])

    async def test_main_entry_and_explanation_require_confirmed_idle_without_spending_goal_budget(self):
        self.start(max_requests=1)
        task = self.controller.state
        task.update(status="paused", request_status="idle_confirmed", requests_used=1)
        original = {key: self.controller.value(key) for key in
            ("task_id", "goal", "segment_id", "actions_count", "remaining_actions", "requests_used", "max_requests", "receipts")}
        for purpose in ("entry", "explain"):
            fields = self.controller.request_started(purpose)
            self.assertEqual(fields, {})
            self.assertEqual(task["request_status"], "idle_confirmed")
            self.assertEqual(task["host_request"]["status"], "running")
            self.assertEqual({key: self.controller.value(key) for key in original}, original)
            with self.assertRaises(ConnectorError) as unsettled:
                self.controller.ensure_native_ready()
            self.assertEqual(unsettled.exception.code, "OriginalRequestUnsettled")
            with self.assertRaises(ConnectorError):
                self.controller.request_started(purpose)
            # An interrupted request needs the original-session status check;
            # unverified prose or ending a waiter cannot reopen admission.
            await self.settle()
            self.controller.ensure_native_ready()
            self.assertEqual(task["request_status"], "idle_confirmed")
            self.assertEqual(self.controller.request_started(purpose), {})
            self.assertEqual({key: self.controller.value(key) for key in original}, original)
            await self.settle()

    def test_isolated_blueprint_purposes_do_not_mutate_main_native_or_goal_budget(self):
        self.start(max_requests=1)
        self.controller.state["request_status"] = "idle_confirmed"
        original = deepcopy(self.controller.state)
        for purpose in ("generation", "generation_correction"):
            self.assertEqual(self.controller.request_started(purpose), {})
            self.assertEqual(self.controller.state, original)
        self.controller.request_started("explain")
        original = deepcopy(self.controller.state)
        for purpose in ("generation", "generation_correction"):
            self.assertEqual(self.controller.request_started(purpose), {})
            self.assertEqual(self.controller.state, original)

    def test_ordinary_main_request_cannot_reopen_local_stop_boundary(self):
        self.controller.begin_reclaim()
        for purpose in ("entry", "explain"):
            with self.assertRaises(ConnectorError) as busy:
                self.controller.request_started(purpose)
            self.assertEqual(busy.exception.code, "ReclaimBusy")
        self.controller.confirm_no_native_request()
        self.controller.finish_reclaim()
        with self.assertRaises(asyncio.CancelledError):
            self.controller.request_started("explain")
        self.assertEqual(self.controller.state["request_status"], "idle")

    def test_verified_ordinary_return_reopens_idle_without_claiming_metadata_or_goal_success(self):
        self.start(max_requests=1)
        task = self.controller.state
        task.update(status="paused", requests_used=1)
        original = {key: self.controller.value(key) for key in
            ("task_id", "goal", "status", "goal_result", "actions_count", "requests_used", "max_requests")}
        for purpose in ("entry", "explain"):
            self.controller.request_started(purpose)
            self.assertTrue(self.controller.request_returned(purpose))
            self.assertEqual(task["request_status"], "idle")
            self.assertEqual({key: self.controller.value(key) for key in original}, original)
            self.controller.ensure_native_ready()

    def test_late_ordinary_return_cannot_release_reclaim_or_stop(self):
        self.controller.request_started("explain")
        self.controller.begin_reclaim()
        self.assertFalse(self.controller.request_returned("explain"))
        self.assertEqual(self.controller.state["request_status"], "settling")
        self.controller.finish_reclaim(error={"code": "NativeSessionBusy", "message": "Original request remains busy"})
        self.assertFalse(self.controller.request_returned("explain"))
        self.assertEqual(self.controller.state["request_status"], "unsettled")
        with self.assertRaises(ConnectorError):
            self.controller.ensure_native_ready()

    def test_execution_and_blueprint_returns_do_not_close_main_execution_request(self):
        self.start()
        self.controller.request_started("execution")
        original = deepcopy(self.controller.state)
        for purpose in ("execution", "execution_summary", "generation", "generation_correction"):
            self.assertFalse(self.controller.request_returned(purpose))
            self.assertEqual(self.controller.state, original)

class ConfirmedOutcomeTests(unittest.TestCase):
    def test_receipt_facts_are_copied_without_model_completion_claim(self):
        payload={"commit_seq":7,"result":{"changes":["转入1u水"],"player":{"id":"me","x":2,"y":3},
            "entity":{"id":"cup","name":"杯","version":4,"x":2,"y":3},
            "action_result":{"transfer":{"source_name":"水壶","target_name":"杯","transferred":1,"material":"water"}},
            "summary":"模型声称都完成了","viewport":{"entities":[{"id":"cup","name":"杯","owner":None,"x":2,"y":3}]}}}
        facts=confirmed_outcome(payload)
        self.assertEqual(facts['action_result']['transfer']['transferred'],1)
        self.assertNotIn('summary',facts)
        self.assertEqual(facts['same_position_public_objects'],[{"id":"cup","name":"杯"}])
        payload['result']['action_result']['transfer']['transferred']=99
        self.assertEqual(facts['action_result']['transfer']['transferred'],1)
        self.assertIsNone(confirmed_outcome({"status":"unknown"}))
