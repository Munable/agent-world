"""Task-level continuation over real connector admission/receipt paths.

The native model and world HTTP boundary are deterministic doubles. These do
not claim a real Agent completed the playable-world acceptance scenario.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from agent_world.grove_connector import ConnectorError, ConnectorManager, GroveSession, CURRENT_FACTS_INSTRUCTIONS


class GroveExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-execution-")
        self.addCleanup(temporary.cleanup)
        self.manager = ConnectorManager(world_url="http://127.0.0.1:8890",
            local_url="http://127.0.0.1:8891", storage=Path(temporary.name),
            binary=Path("unused-opencode"), wrapper=None)
        self.identity = {"role_id": "test-user", "access_mode": "control"}
        self.session = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        self.control = {"status": "idle", "actions_count": 0}
        self.receipts, self.attempts, self.grants, self.requests, self.aborts = {}, [], [], 0, 0
        self.position = 0
        self.uncertain_write, self.host_busy = False, False
        self.attach(self.session)
        self.session.execution.update(task_id="test-task", goal="Go to the observed destination",
            segment_id="test-segment", status="starting", max_actions=4, remaining_actions=4, max_requests=4)

    def attach(self, session):
        session.entered = True
        session.session_id = "test-session"
        session.host_url = "http://127.0.0.1:9000"
        session.catalog = {name: {"access": "read" if name in {"grove.rule_catalog", "grove.control_status"} else "write",
            "input_schema": {"type": "object"}} for name in
            ("grove.control_grant", "grove.control_revoke", "grove.control_status", "grove.rule_catalog", "grove.move")}
        session.world = self.world
        session.current_turn_facts = AsyncMock(side_effect=lambda **kwargs:
            {"player": {"x": self.position, "y": 0, "version": self.position + 1}, "entities": []})
        session.host = self.host

    async def world(self, method, path, body=None):
        if method == "GET":
            operation = path.rsplit("/", 1)[-1]
            if operation not in self.receipts:
                raise ConnectorError("ReceiptNotFound", "Not committed", 404)
            return deepcopy(self.receipts[operation])
        function = path.split("/")[-2]
        args = body["arguments"]
        if function == "grove.control_status":
            return {"result": deepcopy(self.control)}
        if function == "grove.rule_catalog":
            return {"result": {"limits": {"bound_object_ids": 8}}}
        operation = body["operation_id"]
        if operation in self.receipts:
            return deepcopy(self.receipts[operation])
        if function == "grove.control_grant":
            self.grants.append(deepcopy(args))
            self.control = {"status": "active", "goal": args["goal"], "max_actions": args["max_actions"],
                "actions_count": 0, "delegation": "fixture-grant", "version": len(self.grants)}
        elif function == "grove.control_revoke":
            self.control.update(status="revoked", delegation="")
        elif function == "grove.move":
            self.attempts.append(operation)
            if self.uncertain_write:
                raise ConnectorError("WorldUnavailable", "Original write response interrupted", 503)
            if self.control["status"] != "active" or args.get("delegation") != "fixture-grant":
                raise ConnectorError("PermissionDenied", "Control reclaimed", 403)
            self.control["actions_count"] += 1
            self.position += args["dx"]
            if self.control["actions_count"] >= self.control["max_actions"]:
                self.control["status"] = "exhausted"
        else:
            raise AssertionError(function)
        result = {"operation_id": operation, "commit_seq": len(self.receipts) + 1,
                  "result": deepcopy(self.control) if function.startswith("grove.control_") else {"x": self.position}}
        self.receipts[operation] = result
        return deepcopy(result)

    async def host(self, method, path, body=None, timeout=15):
        if path.endswith("/abort"):
            self.assertFalse(self.session.active_execution)
            self.aborts += 1
            return True
        if path == "/session/status":
            return {"test-session": {"type": "busy"}} if self.host_busy else {}
        if path.endswith("/message?limit=100"):
            return []
        if method == "GET":
            return {"id": "test-session"}
        self.assertTrue(path.endswith("/message"))
        self.requests += 1
        await self.model_segment(self.requests)
        return {"info": {"id": "msg_fixture" + str(self.requests), "role": "assistant", "finish": "stop",
            "time": {"completed": self.requests}}, "parts": [{"type": "text", "text": "Observed progress."}]}

    async def checkpoint(self, status):
        evidence = [item["operation_id"] for item in self.session.execution["receipts"]
                    if item["status"] == "confirmed"][-8:]
        return await self.session.tool_call("execution_checkpoint", {"task_id": "test-task", "status": status,
            "execution_segment_id": self.session.execution["segment_id"],
            "summary": "Observed destination" if status == "completed" else "Observed progress",
            "next_intent": "" if status == "completed" else "Inspect the next actual choice",
            "evidence_operation_ids": evidence})

    async def move(self):
        await self.session.tool_call("grove_move", {"dx": 1, "dy": 0,
            "execution_segment_id": self.session.execution["segment_id"]})

    async def test_one_goal_spans_two_requests_and_completion_has_real_evidence(self):
        async def segment(number):
            await self.move()
            await self.checkpoint("continue" if number == 1 else "completed")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.requests, len(self.grants), len(self.attempts), self.position), (2, 1, 2, 2))
        task = self.session.execution
        self.assertEqual((task["status"], task["actions_count"], task["requests_used"]), ("completed", 2, 2))
        self.assertEqual(task["goal_result"]["evidence_operation_ids"], self.attempts)
        self.assertTrue(task["goal_result"]["readback_actual"])
        self.assertFalse(task["resume_allowed"])

    async def test_timeout_after_confirmed_action_continues_without_repeating_action(self):
        async def segment(number):
            if number == 1:
                await self.move()
                raise httpx.ReadTimeout("Interrupted response")
            await self.checkpoint("completed")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.requests, len(self.attempts), self.position), (2, 1, 1))
        self.assertEqual(self.session.execution["status"], "completed")
        self.assertEqual(self.session.execution["segments"][0]["error"], "HostUnavailable")
        self.assertEqual(self.aborts, 1)

    async def test_unknown_original_stops_without_new_operation_or_request(self):
        self.uncertain_write = True
        async def segment(number):
            with self.assertRaises(ConnectorError):
                await self.move()
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.requests, len(self.attempts), self.position), (1, 1, 0))
        self.assertEqual(self.session.execution["status"], "unknown")
        self.assertEqual(self.session.unknown[0]["envelope"]["operation_id"], self.attempts[0])
        self.assertFalse(self.session.execution["resume_allowed"])

    async def test_busy_native_request_does_not_reopen_admission(self):
        async def segment(number):
            await self.move()
            self.host_busy = True
            raise httpx.ReadTimeout("Interrupted response")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.session.execution["request_status"], "unsettled")
        self.assertFalse(self.session.active_execution)
        self.assertFalse(self.session.execution["resume_allowed"])

    async def test_prose_without_checkpoint_or_actions_is_not_goal_completion(self):
        self.model_segment = AsyncMock()
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual(self.requests, 2)
        self.assertEqual(self.session.execution["status"], "paused")
        self.assertIsNone(self.session.execution["goal_result"])
        self.assertEqual(self.session.execution["no_progress_segments"], 2)

    async def test_reclaim_stops_later_writes_and_automatic_segments(self):
        async def segment(number):
            await self.move()
            await self.session.cancel()
            with self.assertRaises(ConnectorError):
                await self.move()
        self.model_segment = segment
        with self.assertRaises(asyncio.CancelledError):
            await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.requests, len(self.attempts)), (1, 1))
        self.assertFalse(self.session.active_execution)

    async def test_saved_task_resume_keeps_budget_receipts_and_original_identity(self):
        async def first(number):
            await self.move()
            await self.checkpoint("blocked")
        self.model_segment = first
        await self.session.execute_goal(self.session.execution["goal"])
        original = self.attempts[0]
        self.assertTrue(self.session.execution["resume_allowed"])
        self.session = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        self.attach(self.session)
        self.session.cancelled = False
        async def resumed(number):
            await self.move()
            await self.checkpoint("completed")
        self.model_segment = resumed
        await self.session.execute_goal(self.session.execution["goal"], resume=True)
        self.assertEqual(self.session.execution["task_id"], "test-task")
        self.assertEqual((self.session.execution["actions_count"], self.session.execution["requests_used"]), (2, 2))
        self.assertEqual(self.grants[1]["max_actions"], 3)
        self.assertIn(original, self.session.execution["goal_result"]["evidence_operation_ids"])
        self.assertEqual(self.session.execution["status"], "completed")

    async def test_untouched_task_restores_own_zero_and_ignores_other_control_counts(self):
        self.session.execution.update(status="paused", actions_count=1, remaining_actions=3,
            grant_version=99, readback_actions_count=1)
        self.session.remember()
        self.session = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        self.attach(self.session)
        task = self.session.execution
        self.assertEqual((task["task_id"], task["goal"], task["max_actions"], task["max_requests"]),
            ("test-task", "Go to the observed destination", 4, 4))
        self.assertEqual((task["actions_count"], task["remaining_actions"], task["requests_used"]), (0, 4, 0))
        self.assertEqual(task["receipts"], [])
        self.assertIsNone(task["grant_operation_id"])
        # A matching goal string also does not grant this new task ownership of
        # a prior control record. It needs its own original grant receipt.
        for old_goal in ("Previous goal", task["goal"]):
            self.session.update_execution_control({"goal": old_goal, "status": "active",
                "actions_count": 7, "max_actions": 8, "version": 100})
            self.assertEqual(task["actions_count"], 0)
            self.assertFalse(task["delegation_active"])
            self.assertIsNone(task["grant_version"])
        await self.session.invoke("grove.control_grant", {"goal": task["goal"], "max_actions": 4})
        self.assertIn(task["grant_operation_id"], self.receipts)
        self.session.active_execution = True
        self.session.status = "thinking"
        task["status"] = "running"
        await self.move()
        self.assertEqual(task["actions_count"], 1)
        self.session.update_execution_control({"goal": "Different current goal", "status": "active",
            "actions_count": 7, "max_actions": 8, "version": 101})
        self.assertEqual((task["actions_count"], task["remaining_actions"]), (1, 3))
        self.assertFalse(task["delegation_active"])

    async def test_metadata_timeout_identifies_unconfirmed_stage_without_grant_or_model(self):
        original_task = self.session.execution["task_id"]
        for stage, failed_path in (("session_identity", "/session/test-session"),
                ("session_status", "/session/status")):
            async def interrupted(method, path, body=None, timeout=15):
                self.assertEqual(timeout, 5 if method == "POST" else 10)
                if path == failed_path:
                    raise httpx.ReadTimeout("Provider text and private fixture must not be retained")
                return await self.host(method, path, body, timeout=timeout)
            self.session.host = interrupted
            await self.session.execute_goal(self.session.execution["goal"])
            task = self.session.execution
            self.assertEqual(task["task_id"], original_task)
            self.assertEqual((self.requests, len(self.grants), task["actions_count"], task["requests_used"]), (0, 0, 0, 0))
            self.assertEqual(task["request_status"], "unsettled")
            self.assertFalse(task["resume_allowed"])
            diagnostic = task["settlement"]
            self.assertEqual((diagnostic["stage"], diagnostic["error_type"]), (stage, "ReadTimeout"))
            self.assertGreaterEqual(diagnostic["elapsed_seconds"], 0)
            self.assertNotIn("native_status", diagnostic)
            self.assertIn("状态未确认", task["error"]["message"])
            self.assertNotIn("仍在运行", task["error"]["message"])
            self.assertNotIn("private fixture", str(task))
            phase = task["phase_timings"][-1]
            self.assertEqual((phase["phase"], phase["error_type"]), ("settle_" + stage, "ReadTimeout"))
        self.session.host = self.host
        self.host_busy = True
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual(self.session.execution["settlement"]["native_status"], "busy")
        self.assertEqual(self.session.execution["error"]["code"], "NativeSessionBusy")
        self.assertIn("仍在运行", self.session.execution["error"]["message"])
        self.assertEqual((self.requests, len(self.grants)), (0, 0))

    async def test_native_context_pruning_keeps_original_audit_and_never_changes_writes(self):
        task = deepcopy(self.session.execution)
        parts = [
            {"id":"facts", "sessionID":"test-session", "messageID":"u", "type":"text", "text":CURRENT_FACTS_INSTRUCTIONS+"\n{}"},
            {"id":"goal", "sessionID":"test-session", "messageID":"u", "type":"text", "text":task["goal"]},
            {"id":"read", "sessionID":"test-session", "messageID":"a", "type":"tool", "tool":"grove_world_grove_inspect",
             "state":{"status":"completed", "input":{"entity_id":"original"}, "output":"original snapshot", "time":{"start":1,"end":2}}},
            {"id":"write", "sessionID":"test-session", "messageID":"a", "type":"tool", "tool":"grove_world_grove_rule_action",
             "state":{"status":"completed", "input":{"action":"accept"}, "output":"original receipt", "time":{"start":1,"end":2}}},
            {"id":"claim", "sessionID":"test-session", "messageID":"a", "type":"tool", "tool":"grove_world_grove_generation_context",
             "state":{"status":"completed", "input":{"entity_id":"original"},
                      "output":"{\"opportunity\":{\"opportunity_id\":\"original-claim\",\"version\":3}}",
                      "time":{"start":1,"end":2}}}]
        original = deepcopy(parts)
        records = [{"info":{"id":mid,"sessionID":"test-session","role":role},"parts":[p for p in parts if p["messageID"]==mid]}
                   for mid,role in (("u","user"),("a","assistant"))]
        patches = []
        async def native(method,path,body=None,timeout=15):
            if method == "PATCH":
                patches.append(deepcopy(body))
                part = next(p for p in parts if p["id"]==body["id"])
                part.clear(); part.update(deepcopy(body)); return body
            return records if path.endswith("?limit=100") else next(r for r in records if r["info"]["id"]==path.rsplit("/",1)[-1])
        self.session.host = native
        self.session.execution["request_status"] = "idle_confirmed"
        await self.session.compact_execution_context()
        self.assertEqual([p["id"] for p in patches],["facts","read"])
        self.assertEqual(parts[0]["text"],original[0]["text"])
        self.assertTrue(parts[0]["ignored"])
        self.assertEqual(parts[1],original[1])
        self.assertEqual(parts[2]["state"]["output"],original[2]["state"]["output"])
        self.assertEqual(parts[2]["state"]["input"],original[2]["state"]["input"])
        self.assertIn("compacted",parts[2]["state"]["time"])
        self.assertEqual(parts[3],original[3])
        # The next segment can submit its existing claim without spending
        # another delegated action to rediscover the original id/version.
        self.assertEqual(parts[4],original[4])
        self.assertEqual((self.session.execution["task_id"],self.session.execution["requests_used"]),(task["task_id"],task["requests_used"]))
        await self.session.compact_execution_context()
        self.assertEqual(len(patches),2)
        self.session.execution["request_status"] = "running"
        with self.assertRaises(ConnectorError): await self.session.compact_execution_context()
        self.assertEqual(len(patches),2)

    async def test_early_confirmed_progress_remains_usable_after_many_rejected_attempts(self):
        self.session.execution_receipt("grove.move", "original-confirmed-action", {"commit_seq":7}, status="confirmed")
        # These attempts fit the task's 8 x 16 tool-call allowance. Rejections
        # do not consume its successful-action allowance.
        for number in range(80):
            self.session.execution_receipt("grove.rule_action", f"rejected-{number}", status="rejected")
        self.session.active_execution = True
        self.session.execution["status"] = "running"
        await self.session.execution_checkpoint({"task_id":"test-task", "status":"continue",
            "execution_segment_id":"test-segment", "summary":"The original move still matters",
            "next_intent":"Choose a viable action", "evidence_operation_ids":["original-confirmed-action"]})
        self.assertEqual(self.session.execution["progress"]["evidence"][0]["commit_seq"],7)

    async def test_completion_rejects_invented_receipt(self):
        self.session.active_execution = True
        self.session.execution["status"] = "running"
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.execution_checkpoint({"task_id": "test-task", "status": "completed",
                "execution_segment_id": self.session.execution["segment_id"],
                "summary": "Done", "next_intent": "", "evidence_operation_ids": ["invented-operation"]})
        self.assertEqual(rejected.exception.code, "UnconfirmedEvidence")
        self.assertIsNone(self.session.execution["goal_result"])

    async def test_late_previous_segment_callback_is_denied_after_next_segment_opens(self):
        previous = None
        async def segment(number):
            nonlocal previous
            if number == 1:
                previous = self.session.execution["segment_id"]
            else:
                with self.assertRaises(ConnectorError) as stale:
                    await self.session.tool_call("grove_move", {"dx": 1, "dy": 0,
                        "execution_segment_id": previous})
                self.assertEqual(stale.exception.code, "StaleExecutionSegment")
                self.assertEqual(len(self.attempts), 1)
            await self.move()
            await self.checkpoint("continue" if number == 1 else "completed")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.requests, len(self.attempts), self.position), (2, 2, 2))

    async def test_completed_checkpoint_survives_later_native_timeout(self):
        async def segment(number):
            await self.move()
            await self.checkpoint("completed")
            raise httpx.ReadTimeout("Final prose interrupted after verified completion")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.session.execution["status"], "completed")
        self.assertEqual(self.session.execution["goal_result"]["evidence_operation_ids"], self.attempts)
        self.assertFalse(self.session.execution["resume_allowed"])


if __name__ == "__main__":
    unittest.main()
