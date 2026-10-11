"""Execution ownership boundaries, with narrow in-memory port doubles.

These assertions cover admission, original receipt ownership and stop/budget
semantics. They do not claim a model or player completed the world experience.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
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
                self.assertEqual(result["execution_readback"], fresh)
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
                "delegation": "private-original-grant", "max_actions": arguments["max_actions"], "version": 1}
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
        self.controller.active = False
        self.controller.state["request_status"] = "idle_confirmed"
        return True

    async def native_segment(self, kind, goal, *, context_entity_id=None):
        if kind == "compact":
            return
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

    async def test_main_entry_and_explanation_require_confirmed_idle_without_spending_goal_budget(self):
        self.start(max_requests=1)
        task = self.controller.state
        task.update(status="paused", request_status="idle_confirmed", requests_used=1)
        original = {key: self.controller.value(key) for key in
            ("task_id", "goal", "segment_id", "actions_count", "remaining_actions", "requests_used", "max_requests", "receipts")}
        for purpose in ("entry", "explain"):
            fields = self.controller.request_started(purpose)
            self.assertEqual(fields, {})
            self.assertEqual(task["request_status"], "running")
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
