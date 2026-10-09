"""Task-level continuation over real connector admission/receipt paths.

The native model and world HTTP boundary are deterministic doubles. These do
not claim a real Agent completed the playable-world acceptance scenario.
"""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent_world.grove_connector import (ConnectorError, ConnectorManager, GroveSession, CURRENT_FACTS_INSTRUCTIONS,
    HISTORY_RECOVERY_INSTRUCTIONS, EXECUTION_INSTRUCTIONS, EXECUTOR_AGENT, MCP_NAME, create_connector_app)


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
        self.session.execution_controller.state.update(task_id="test-task", goal="Go to the observed destination",
            segment_id="test-segment", status="starting", max_actions=4, remaining_actions=4, max_requests=4,
            context_reference_attempted=False)

    def attach(self, session):
        session.entered = True
        session.session_id = "test-session"
        session.host_url = "http://127.0.0.1:9000"
        session.catalog = {name: {"access": "read" if name in {"grove.rule_catalog", "grove.control_status"} else "write",
            "input_schema": {"type": "object"}} for name in
            ("grove.control_grant", "grove.control_revoke", "grove.control_status", "grove.rule_catalog", "grove.move")}
        session.world = self.world
        session.current_turn_facts = AsyncMock(side_effect=lambda **kwargs:
            {"player": {"id": session.role_id, "x": self.position, "y": 0, "version": self.position + 1}, "entities": []})
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
        observed = self.session.present_agent_result("map.viewport", await self.session.current_turn_facts())
        await self.session.tool_call("grove_move", {"dx": 1, "dy": 0,
            "observation_id": observed["observation_id"],
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

    async def test_cancel_before_any_native_session_does_not_lock_first_entry(self):
        self.session.session_id = self.session.host_url = ''
        self.session.blueprint_session_id = ''
        self.session.release_opportunity = AsyncMock()
        await self.session.cancel()
        self.assertEqual(self.session.execution['request_status'], 'idle')
        self.session.execution_controller.ensure_native_ready()

    async def test_successful_abort_does_not_confirm_busy_blueprint_session(self):
        self.session.blueprint_session_id = 'original-blueprint'
        async def native(method, path, body=None, **kwargs):
            if path.endswith('/abort'):
                return True  # Cancellation accepted, without a termination verdict.
            if path == '/session/test-session':
                return {'id': 'test-session'}
            if path == '/session/original-blueprint':
                return {'id': 'original-blueprint'}
            if path == '/session/status':
                return {'original-blueprint': {'type': 'busy'}}
            self.fail(path)
        self.session.host = native
        self.assertFalse(await self.session.settle_execution_request(abort=True))
        self.assertEqual(self.session.execution['request_status'], 'unsettled')
        with self.assertRaises(ConnectorError):
            self.session.execution_controller.ensure_native_ready()

    async def test_cancel_notice_follows_original_native_confirmation_and_keeps_manual_proxy_open(self):
        await self.session.invoke("grove.control_grant", {"goal": self.session.execution["goal"], "max_actions": 4})
        self.host_busy = True
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.manager.local_url,
                headers={"Authorization": "Bearer fixture-bearer"}) as client:
            cancelled = await client.post("/connector/cancel")
            self.assertEqual(cancelled.status_code, 200)
            pending = cancelled.json()
            self.assertEqual(self.control["status"], "revoked")
            self.assertEqual(pending["execution"]["request_status"], "unsettled")
            self.assertEqual(pending["progress"][-1]["kind"], "reclaim_pending")
            self.assertIn("仍在确认原请求结束", pending["progress"][-1]["message"])
            self.assertIn("你可以手动操作", pending["progress"][-1]["message"])
            self.assertNotIn("伙伴已停止", str(pending["progress"]))
            blocked = await client.post("/connector/prompt", json={"text": "A different request"})
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.json()["error"], "OriginalRequestUnsettled")

            # Only the upstream HTTP boundary is doubled: the actual human
            # proxy route must remain independent from native admission.
            upstream = AsyncMock()
            upstream.__aenter__.return_value = upstream
            upstream.request.return_value = httpx.Response(200, json={"result": {"x": 1}})
            manual = {"operation_id": "manual-after-reclaim", "arguments": {"dx": 1, "dy": 0}}
            with patch("agent_world.grove_connector.httpx.AsyncClient", return_value=upstream):
                moved = await client.post("/v1/functions/grove.move/invoke", json=manual)
            self.assertEqual(moved.status_code, 200)
            upstream.request.assert_awaited_once()
            forwarded = upstream.request.await_args
            self.assertEqual(forwarded.args[:2], ("POST", self.manager.world_url + "/v1/functions/grove.move/invoke"))
            self.assertEqual(json.loads(forwarded.kwargs["content"]), manual)

        self.host_busy = False
        self.assertTrue(await self.session.settle_execution_request())
        confirmed = self.session.state()
        self.assertEqual(confirmed["execution"]["request_status"], "idle_confirmed")
        self.assertIsNone(confirmed["execution"]["error"])
        self.assertEqual(confirmed["progress"][-1]["kind"], "cancelled")
        self.assertIn("伙伴已停止", confirmed["progress"][-1]["message"])
        self.session.execution_controller.ensure_native_ready()
        self.assertEqual((self.requests, self.session.inference_count, self.session.session_id), (0, 0, "test-session"))

    async def test_cancel_with_saved_session_but_unavailable_host_preserves_uncertainty(self):
        self.session.host_url = ''
        self.session.release_opportunity = AsyncMock()
        await self.session.cancel()
        with self.assertRaises(ConnectorError) as blocked:
            self.session.execution_controller.ensure_native_ready()
        self.assertEqual(blocked.exception.code, 'OriginalRequestUnsettled')

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

    async def test_fast_reclaim_confirms_world_before_slow_abort_and_blocks_new_tasks(self):
        await self.session.invoke('grove.control_grant',{'goal':'Keep exploring','max_actions':4})
        slow_abort=asyncio.Event()
        async def delayed_host(method,path,body=None,**kwargs):
            if path.endswith('/abort'):
                await slow_abort.wait()
            return await self.host(method,path,body)
        self.session.host=delayed_host
        try:
            await asyncio.wait_for(self.session.cancel(background=True),1)
            self.assertEqual(self.control['status'],'revoked')
            self.assertFalse(self.session.execution['delegation_active'])
            self.assertTrue(self.session.state()['reclaim_in_progress'])
            with self.assertRaises(ConnectorError) as blocked:
                self.session.ensure_not_reclaiming()
            self.assertEqual(blocked.exception.code,'ReclaimBusy')
            original=self.session.reclaim_task
            await self.session.cancel(background=True)
            self.assertIs(self.session.reclaim_task,original)
            waiting=asyncio.create_task(self.session.cancel())
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            slow_abort.set()
            await asyncio.wait_for(waiting,1)
            self.assertFalse(self.session.state()['reclaim_in_progress'])
            self.assertEqual(self.requests,0)
        finally:
            slow_abort.set()
            await self.session.cancel()

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
        self.session.execution_controller.stopped = False
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
        self.session.execution_controller.state.update(status="paused", actions_count=1, remaining_actions=3,
            grant_version=99, readback_actions_count=1)
        self.session.remember()
        self.session = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        self.attach(self.session)
        # This fixture deliberately mutates the owner's task; Session reads are snapshots.
        task = self.session.execution_controller.state
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
        self.session.execution_controller.active = True
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
        self.session.execution_controller.state["request_status"] = "idle_confirmed"
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
        self.session.execution_controller.state["request_status"] = "running"
        with self.assertRaises(ConnectorError): await self.session.compact_execution_context()
        self.assertEqual(len(patches),2)

    async def test_early_confirmed_progress_remains_usable_after_many_rejected_attempts(self):
        self.session.execution_receipt("grove.move", "original-confirmed-action", {"commit_seq":7}, status="confirmed")
        # These attempts fit the task's 8 x 16 tool-call allowance. Rejections
        # do not consume its successful-action allowance.
        for number in range(80):
            self.session.execution_receipt("grove.rule_action", f"rejected-{number}", status="rejected")
        self.session.execution_controller.active = True
        self.session.execution_controller.state["status"] = "running"
        await self.session.execution_checkpoint({"task_id":"test-task", "status":"continue",
            "execution_segment_id":"test-segment", "summary":"The original move still matters",
            "next_intent":"Choose a viable action", "evidence_operation_ids":["original-confirmed-action"]})
        self.assertEqual(self.session.execution["progress"]["evidence"][0]["commit_seq"],7)

    async def test_completion_rejects_invented_receipt(self):
        self.session.execution_controller.active = True
        self.session.execution_controller.state["status"] = "running"
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

    def history_host(self, *, finish="stop", interrupted=False):
        original_host = self.session.host
        self.summary_calls = 0
        messages = []
        async def host(method, path, body=None, timeout=15):
            if path == "/config":
                return {"compaction": {"auto": False, "tail_turns": 0}, "agent": {
                    "compaction": {"disable": False, "model": self.session.model,
                        "prompt": HISTORY_RECOVERY_INSTRUCTIONS, "permission": {"*": "deny"}},
                    EXECUTOR_AGENT: {"prompt": EXECUTION_INSTRUCTIONS,
                        "permission": {"*": "deny", MCP_NAME + "_*": "allow"}}}}
            if path.endswith("/message?limit=100"):
                return deepcopy(messages)
            if path.endswith("/summarize"):
                self.summary_calls += 1
                self.assertEqual(body, {"providerID": "provider", "modelID": "model", "auto": False})
                self.assertFalse(self.grants)
                self.assertFalse(self.session.active_execution)
                self.assertEqual(self.session.execution["requests_used"], 1)
                messages.extend([{"info": {"id": "compact-user", "role": "user", "sessionID": "test-session"},
                    "parts": [{"type": "compaction", "auto": False}]},
                    {"info": {"id": "summary-result", "parentID": "compact-user", "role": "assistant",
                        "summary": True, "sessionID": "test-session", "providerID": "provider", "modelID": "model",
                        "finish": finish, "time": {"completed": 1}},
                     "parts": [{"type": "text", "text": "Past plan is not an executed action. Check current facts."}]}])
                if interrupted:
                    raise httpx.ReadTimeout("Original summary response interrupted")
                return True
            return await original_host(method, path, body, timeout)
        self.session.host = host

    async def test_summary_consumes_budget_and_is_verified_before_fresh_facts_and_grant(self):
        self.history_host()
        self.session.execution_controller.state["history_recovery"]["requested"] = True
        self.session.execution_controller.state["max_requests"] = 2
        # The same human goal can be reused. A summary is a model request,
        # never evidence that this new task owns the previous world's grant.
        self.control.update(status="revoked", goal=self.session.execution["goal"],
                            actions_count=4, max_actions=4, version=100)
        async def segment(number):
            self.assertEqual(self.session.execution["history_recovery"]["status"], "completed")
            self.assertTrue(self.session.execution["readback_actual"])
            self.assertEqual(self.session.execution["actions_count"], 0)
            await self.move()
            await self.checkpoint("completed")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.summary_calls, self.requests, len(self.grants), self.position), (1, 1, 1, 1))
        self.assertEqual(self.session.execution["requests_used"], 2)
        self.assertEqual([item["purpose"] for item in self.session.inference_history], ["execution_summary", "execution"])

    async def test_truncated_summary_cannot_grant_or_be_replaced_by_new_goal(self):
        self.history_host(finish="length")
        self.session.execution_controller.state["history_recovery"]["requested"] = True
        with self.assertRaises(ConnectorError):
            await self.session.execute_goal(self.session.execution["goal"])
        self.assertEqual((self.summary_calls, self.requests, len(self.grants)), (1, 0, 0))
        self.assertEqual(self.session.execution["history_recovery"]["status"], "failed")
        self.assertFalse(self.session.execution_can_resume())
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.manager.local_url,
                headers={"Authorization": "Bearer fixture-bearer"}) as client:
            response = await client.post("/connector/execute", json={"goal": "Different goal"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.session.execution["task_id"], "test-task")
        self.assertEqual(self.session.execution["requests_used"], 1)

    async def test_interrupted_summary_reconciles_original_without_spending_again(self):
        self.history_host(interrupted=True)
        with self.assertRaises(ConnectorError):
            await self.session.recover_execution_history()
        self.assertEqual(self.session.execution["history_recovery"]["status"], "uncertain")
        self.session.execution_controller.state.update(status="paused", request_status="idle_confirmed")
        self.assertFalse(self.session.execution_can_resume())
        await self.session.verify_history_recovery()
        self.assertTrue(self.session.execution_can_resume())
        self.assertEqual((self.summary_calls, self.session.execution["requests_used"], self.requests, len(self.grants)), (1, 1, 0, 0))
        self.assertIsNone(self.session.execution["goal_result"])

    async def test_history_option_preserves_task_budget_and_archives_replaced_goal(self):
        self.session.execution_controller.state.update(status="paused", request_status="idle_confirmed", resume_allowed=True,
            requests_used=7, max_requests=8, actions_count=3)
        self.session.schedule = Mock()
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.manager.local_url,
                headers={"Authorization": "Bearer fixture-bearer"}) as client:
            response = await client.post("/connector/execute-resume", json={"task_id": "test-task", "compact_history": True})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.session.execution["requests_used"], 7)
            self.session.schedule.assert_not_called()
            response = await client.post("/connector/execute", json={"goal": "A separately authorized goal",
                "max_actions": 2, "max_requests": 2, "compact_history": True})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.session.execution_archive[-1]["task_id"], "test-task")
        self.assertEqual(self.session.execution_archive[-1]["requests_used"], 7)
        self.assertEqual(self.session.execution_archive[-1]["actions_count"], 3)
        self.assertEqual(self.session.execution["max_requests"], 2)
        self.assertTrue(self.session.execution["history_recovery"]["requested"])

    async def test_explicit_extension_preserves_exhausted_task_and_all_spent_progress(self):
        self.session.execution_controller.state.update(status="paused", request_status="idle_confirmed", resume_allowed=False,
            requests_used=2, max_requests=2, actions_count=3)
        self.session.schedule = Mock()
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.manager.local_url,
                headers={"Authorization": "Bearer fixture-bearer"}) as client:
            rejected = await client.post("/connector/execute-resume", json={"task_id": "test-task"})
            self.assertEqual(rejected.status_code, 409)
            accepted = await client.post("/connector/execute-resume", json={"task_id": "test-task", "additional_requests": 2})
            self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.session.execution["task_id"], "test-task")
        self.assertEqual((self.session.execution["max_requests"], self.session.execution["requests_used"],
            self.session.execution["actions_count"], self.session.execution["max_actions"]), (4, 2, 3, 4))
        self.assertEqual(self.session.execution["budget_extensions"], [{"additional_requests": 2,
            "previous_max_requests": 2, "max_requests": 4, "requests_used": 2, "actions_count": 3}])
        self.session.schedule.assert_called_once_with(self.session.execution["goal"], mode="execute-resume", context_entity_id=None)

    async def test_extension_cannot_exceed_cap_or_bypass_unsettled_summary(self):
        self.session.execution_controller.state.update(status="paused", request_status="idle_confirmed", resume_allowed=False,
            requests_used=2, max_requests=2)
        self.session.schedule = Mock()
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.manager.local_url,
                headers={"Authorization": "Bearer fixture-bearer"}) as client:
            response = await client.post("/connector/execute-resume", json={"task_id": "test-task", "additional_requests": 7})
            self.assertEqual(response.status_code, 400)
            self.session.execution_controller.state["history_recovery"]["status"] = "uncertain"
            response = await client.post("/connector/execute-resume", json={"task_id": "test-task", "additional_requests": 2})
            self.assertEqual(response.status_code, 409)
        self.assertEqual((self.session.execution["max_requests"], self.session.execution["requests_used"]), (2, 2))
        self.session.schedule.assert_not_called()

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


    def contract_facts(self, *, terms=1, status="active", entity="bound-contract"):
        return {"selected_inspection": {"entity": {"id": entity, "version": 7, "name": "交付约定",
            "creative": {"definition_ref": "original-definition", "type_id": "contract", "world_tick": 30,
                "contract": {"terms_version": terms, "material": "original-definition#product",
                    "quantity": 2, "delivered": 2 if status == "complete" else 0,
                    "status": status, "finished_tick": 29 if status == "complete" else None, "released": 0}}}}}

    def test_contract_world_verdict_does_not_prove_extra_goal_or_release(self):
        self.session.execution_controller.state.update(context_entity_id="bound-contract", goal="完成交付并取回收益", status="paused")
        self.session.execution_world_results(self.contract_facts(), establish_reference=True)
        result = self.session.execution_world_results(self.contract_facts(status="complete"))[0]
        self.assertEqual(result["verification"]["status"], "verified_complete")
        self.assertEqual(result["released"], 0)
        self.assertEqual(self.session.execution["status"], "paused")
        self.assertIsNone(self.session.execution["goal_result"])
        self.assertEqual(self.session.execution["goal"], "完成交付并取回收益")

    def test_changed_contract_terms_and_other_object_cannot_replace_original_reference(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        self.session.execution_world_results(self.contract_facts(), establish_reference=True)
        original = deepcopy(self.session.execution["context_result_reference"])
        changed = self.session.execution_world_results(self.contract_facts(terms=2, status="complete"), establish_reference=True)[0]
        self.assertEqual(changed["verification"]["status"], "reference_mismatch")
        self.assertEqual(self.session.execution["context_result_reference"], original)
        self.assertEqual(self.session.execution_world_results(self.contract_facts(entity="easier-contract", status="complete")), [])

    def test_old_task_without_start_reference_cannot_bind_on_resume(self):
        self.session.execution_controller.state.update(context_entity_id="bound-contract", status="paused")
        self.session.execution_controller.state.pop("context_reference_attempted", None)
        self.session.remember()
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        result = restored.execution_world_results(self.contract_facts(status="complete"), establish_reference=True)[0]
        self.assertEqual(result["verification"]["status"], "unbound")
        self.assertIsNone(restored.execution["context_result_reference"])

    def test_missing_initial_terms_cannot_bind_later_terms_after_restart(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        initial = self.contract_facts()
        initial["selected_inspection"]["entity"]["creative"].pop("contract")
        self.assertEqual(self.session.execution_world_results(initial, establish_reference=True), [])
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        result = restored.execution_world_results(self.contract_facts(terms=2, status="complete"), establish_reference=True)[0]
        self.assertEqual(result["verification"]["status"], "unbound")
        self.assertIsNone(restored.execution["context_result_reference"])

    def test_new_task_reference_is_fixed_once_and_persisted_across_restart(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        initial = self.session.execution_world_results(self.contract_facts(), establish_reference=True)[0]
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        result = restored.execution_world_results(self.contract_facts(status="complete"), establish_reference=True)[0]
        self.assertEqual(result["verification"]["status"], "verified_complete")
        self.assertEqual(result["reference"], initial["reference"])
        changed = restored.execution_world_results(self.contract_facts(terms=2), establish_reference=True)[0]
        self.assertEqual(changed["verification"]["status"], "reference_mismatch")
        self.assertEqual(changed["reference"], initial["reference"])

    async def test_definite_first_selection_refusal_cannot_adopt_later_terms(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        self.session.current_turn_facts = AsyncMock(side_effect=ConnectorError("RuleViolation", "物件不在附近视野内。", 409))
        with self.assertRaises(ConnectorError):
            await self.session.turn("原交付目标", executing=True, context_entity_id="bound-contract")
        self.assertEqual(self.requests, 0)
        self.assertTrue(self.session.execution["context_reference_attempted"])
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        result = restored.execution_world_results(self.contract_facts(terms=2), establish_reference=True)[0]
        self.assertEqual(result["verification"]["status"], "unbound")

    async def test_initial_network_outage_keeps_first_valid_pre_execution_binding(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        self.session.current_turn_facts = AsyncMock(side_effect=ConnectorError("WorldUnavailable", "Network read failed", 503))
        with self.assertRaises(ConnectorError):
            await self.session.turn("原交付目标", executing=True, context_entity_id="bound-contract")
        self.assertEqual(self.requests, 0)
        self.assertFalse(self.session.execution["context_reference_attempted"])
        result = self.session.execution_world_results(self.contract_facts(), establish_reference=True)[0]
        self.assertEqual(result["reference"]["terms_version"], 1)

    async def test_recovery_refusal_closes_initial_binding_before_readable_new_terms(self):
        self.session.execution_controller.state["context_entity_id"] = "bound-contract"
        self.session.current_turn_facts = AsyncMock(side_effect=ConnectorError("WorldUnavailable", "Network read failed", 503))
        with self.assertRaises(ConnectorError):
            await self.session.execution_readback()
        self.assertFalse(self.session.execution["context_reference_attempted"])
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "test-user", "x": 0, "y": 0},
            "selected_inspection_unavailable": {"entity_id": "bound-contract", "code": "PermissionDenied"}})
        await self.session.execution_readback()
        self.assertTrue(self.session.execution["context_reference_attempted"])
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Test", "provider/model")
        result = restored.execution_world_results(self.contract_facts(terms=2, status="complete"), establish_reference=True)[0]
        self.assertEqual(result["verification"]["status"], "unbound")
        self.assertIsNone(result["reference"])

    async def test_completed_checkpoint_is_agent_claim_even_with_confirmed_action(self):
        async def segment(number):
            await self.move()
            await self.checkpoint("completed")
        self.model_segment = segment
        await self.session.execute_goal(self.session.execution["goal"])
        result = self.session.execution["goal_result"]
        self.assertEqual(result["completion_source"], "agent_claim")
        self.assertEqual(result["verification"], {"status": "unverified", "scope": "natural_language_goal"})
        self.assertEqual(result["evidence_operation_ids"], self.attempts)

    def test_native_timing_rejects_old_messages_and_deduplicates_reasoning_spans(self):
        import time
        entry = {"session_id": self.session.session_id, "timings": {"first_model_response_seconds": None,
            "first_tool_seconds": None, "reasoning_completed_spans_seconds": None}}
        self.session._active_request_measurement = {"entry": entry, "started": time.monotonic(),
            "started_at_ms": 1000, "assistant_ids": set(), "reasoning_spans": {}}
        self.session.observe_native_timing("message.updated", {"info": {"id": "old", "role": "assistant",
            "sessionID": self.session.session_id, "time": {"created": 999}}})
        self.session.observe_native_timing("message.part.updated", {"part": {"messageID": "old", "type": "text", "text": "old answer"}})
        self.assertIsNone(entry["timings"]["first_model_response_seconds"])
        self.session.observe_native_timing("message.updated", {"info": {"id": "new", "role": "assistant",
            "sessionID": self.session.session_id, "time": {"created": 1001}}})
        event = {"part": {"id": "reason", "messageID": "new", "type": "reasoning", "text": "private",
            "time": {"start": 1001, "end": 2501}}}
        self.session.observe_native_timing("message.part.updated", event)
        self.session.observe_native_timing("message.part.updated", event)
        self.assertEqual(entry["timings"]["reasoning_completed_spans_seconds"], 1.5)
        self.assertIsNotNone(entry["timings"]["first_model_response_seconds"])
        self.assertIsNone(entry["timings"]["first_tool_seconds"])
        self.assertNotIn("private", str(entry))

if __name__ == "__main__":
    unittest.main()
