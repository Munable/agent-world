"""Original-write recovery against the real world's revocable control boundary.

The native metadata boundary is doubled; no model or service runs here. These
contracts prove exact recovery and rejection, not autonomous model acceptance.
"""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from agent_world.errors import ReceiptNotFound, WorldRuntimeError
from agent_world.grove_connector import ConnectorError, ConnectorManager, GroveSession
from agent_world.transport_contracts import error_response
from agent_world.worlds.shared_grove import model as m
import test_grove_creative as fixtures


class RevokedExecutionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.world.close)
        manager = ConnectorManager(world_url="http://127.0.0.1:8890",
            local_url="http://127.0.0.1:8891", storage=self.h.db.parent / "connector",
            binary=self.h.db.parent / "unused", wrapper=None)
        self.session = GroveSession(manager, {"role_id": self.h.roles["A"], "access_mode": "control"},
            "fixture-bearer", "A", "provider/model")
        self.session.entered = True
        self.session.host_url, self.session.session_id = "http://127.0.0.1:9000", "original-native"
        self.session.world, self.session.host = self.world, self.host
        self.session.current_turn_facts = AsyncMock(side_effect=lambda **kw:
            self.h.world.query_function("w", "map.viewport", self.session.role_id, {})["result"])
        self.session.messages = [{"role": "assistant", "text": "An old model claim", "task_id": "old-task"}]
        self.original_messages = deepcopy(self.session.messages)
        self.calls, self.host_calls = [], []
        self.lose_reply, self.commit_before_loss = False, False
        self.native_busy, self.recovery_503 = False, False
        self.session.execution_controller.state.update(task_id="original-task", segment_id="original-segment",
            goal="把自己的木柴放在这里", status="starting", max_actions=4, max_requests=2, requests_used=1)
        await self.session.agent_client.discover()
        self.session.catalog = deepcopy(self.session.agent_client.catalog)
        self.session._agent_contract_discovered = True
        await self.session.invoke("grove.control_grant", {"goal": self.session.execution["goal"], "max_actions": 4})
        self.session.execution_controller.state.update(status="running", request_status="returned")
        self.session.execution_controller.active = True
        self.wood = self.h.item("A", "木柴")["id"]
        self.arguments = {"action": "drop", "target_id": self.wood,
            "expected_target_version": self.h.entity(self.wood)["version"],
            "expected_player_version": self.h.view("A")["player"]["version"],
            "delegation": self.session.delegation}

    async def world(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        try:
            if method == "GET":
                url = urlsplit(path)
                if url.path == "/v1/bootstrap":
                    return self.h.world.bootstrap("w", self.session.role_id)
                if url.path == "/v1/discover":
                    query = parse_qs(url.query)
                    return self.h.world.discover_functions("w", actor_role_id=self.session.role_id,
                        after=query.get("after", [""])[0], limit=int(query.get("limit", ["50"])[0]),
                        include_schemas=query.get("include_schemas", ["false"])[0] == "true")
                if url.path.startswith("/v1/receipts/"):
                    return self.h.world.get_receipt("w", self.session.role_id, url.path.rsplit("/", 1)[-1])
                self.fail("Unexpected world read: " + path)
            if path == "/v1/streams/grove.changes/read":
                return self.h.world.read_stream("w", "grove.changes", self.session.role_id,
                    cursor=body.get("cursor"), limit=body.get("limit", 50))
            function = path.split("/")[-2]
            if self.h.world.get_function("w", function)["access"] == "read":
                return self.h.world.query_function("w", function, self.session.role_id, body["arguments"],
                    expected_version=body.get("expected_version"))
            lost = function == "grove.interact" and self.lose_reply
            self.lose_reply = False if lost else self.lose_reply
            if (lost and not self.commit_before_loss) or (function == "grove.interact" and self.recovery_503):
                raise ConnectorError("WorldUnavailable", "Original response was lost.", 503)
            result = self.h.world.call_function("w", function, self.session.role_id, body["arguments"],
                operation_id=body["operation_id"], expected_version=body.get("expected_version"))
            if lost:
                raise ConnectorError("WorldUnavailable", "Committed response was lost.", 503)
            return result
        except WorldRuntimeError as exc:
            status, payload = error_response(exc)
            raise ConnectorError(payload["error"], payload["message"], status,
                world_details=payload.get("details"), retryable=payload.get("retryable"),
                recovery=payload.get("recovery"), retry_after_seconds=payload.get("retry_after_seconds")) from None

    async def host(self, method, path, body=None, **kwargs):
        self.host_calls.append((method, path))
        if path.endswith("/abort"):
            return True
        if path == "/session/status":
            return {"original-native": {"type": "busy"}} if self.native_busy else {}
        if path == "/config/providers":
            return {"providers": [{"id": "provider", "models": {"model": {}}}]}
        if "/message?" in path:
            return []
        if method == "GET" and path == "/session/original-native":
            return {"id": "original-native", "permission": self.session.permissions()}
        self.fail("Unexpected native operation: " + method + " " + path)

    async def lose_original(self, *, committed=False):
        self.lose_reply, self.commit_before_loss = True, committed
        with self.assertRaises(ConnectorError):
            await self.session.invoke("grove.interact", deepcopy(self.arguments), model_write=True)
        self.assertEqual(len(self.session.unknown), 1)
        return deepcopy(self.session.unknown[0])

    def action_posts(self):
        return [body for method, path, body in self.calls if method == "POST" and "/grove.interact/" in path]

    async def test_cancelled_uncommitted_original_is_rejected_under_its_revoked_grant(self):
        original = await self.lose_original()
        before = {key: deepcopy(self.h.world.get_state("w", *key)) for key in
            ((m.ENTITIES, self.wood), (m.PLAYERS, self.session.role_id))}
        await self.session.cancel()
        self.assertEqual(self.action_posts(), [original["envelope"], original["envelope"]])
        self.assertEqual(self.session.unknown, [])
        self.assertEqual({key: self.h.world.get_state("w", *key) for key in before}, before)
        control = self.h.read("A", "grove.control_status")
        self.assertEqual((control["status"], control["actions_count"]), ("revoked", 0))
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.session.role_id, original["envelope"]["operation_id"])
        ledger = self.session.execution["receipts"]
        self.assertEqual([(item["task_id"], item["segment_id"], item["status"]) for item in ledger],
            [("original-task", "original-segment", "rejected")])
        self.assertEqual(self.session.execution["request_status"], "idle_confirmed")
        self.assertEqual(self.session.messages, self.original_messages)
        self.assertEqual(self.session.inference_count, 0)

    async def test_lost_committed_reply_is_read_once_without_repeating_action_after_cancel(self):
        original = await self.lose_original(committed=True)
        committed = deepcopy(self.h.entity(self.wood))
        await self.session.cancel()
        self.assertEqual(self.action_posts(), [original["envelope"]])
        self.assertEqual(self.h.entity(self.wood), committed)
        self.assertIsNone(committed["owner"])
        self.assertEqual(self.h.read("A", "grove.control_status")["actions_count"], 1)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.execution["receipts"][0]["status"], "confirmed")
        self.assertEqual(self.session.execution["receipts"][0]["operation_id"], original["envelope"]["operation_id"])
        self.assertEqual(self.session.messages, self.original_messages)

    async def test_native_busy_keeps_missing_original_unknown_without_replay(self):
        original = await self.lose_original()
        self.native_busy = True
        await self.session.cancel()
        self.assertEqual(self.action_posts(), [original["envelope"]])
        self.assertEqual(self.session.unknown[0]["envelope"], original["envelope"])
        self.assertEqual(self.session.execution["request_status"], "unsettled")

    async def test_original_replay_network_uncertainty_stays_unknown_and_can_retry_only_original(self):
        original = await self.lose_original()
        self.recovery_503 = True
        await self.session.cancel()
        self.assertEqual(self.action_posts(), [original["envelope"], original["envelope"]])
        self.assertEqual(self.session.unknown[0]["envelope"], original["envelope"])
        self.assertEqual(self.session.execution["receipts"][0]["status"], "unknown")
        self.recovery_503 = False
        await self.session.cancel()
        self.assertEqual(self.action_posts(), [original["envelope"]] * 3)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.h.read("A", "grove.control_status")["actions_count"], 0)

    async def test_reconnect_settles_native_even_when_original_world_write_is_unknown(self):
        original = await self.lose_original()
        self.session.world_read = self.session.first_response_success = True
        self.session.launch = AsyncMock()
        # This test doubles the native boundary. Hook proof gates have their
        # own contract tests and must still be called during recovery.
        self.session.verify_native_context = AsyncMock()
        self.session.join = AsyncMock()
        self.session.watch_events = AsyncMock()
        await self.session.prepare()
        self.session.verify_native_context.assert_awaited_once_with(initialize=True)
        await self.session.event_worker
        self.assertEqual(self.action_posts(), [original["envelope"], original["envelope"]])
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.execution["request_status"], "idle_confirmed")
        self.assertEqual(self.session.execution["receipts"][0]["status"], "rejected")
        self.assertEqual(self.session.session_id, "original-native")
        self.assertFalse(any(method == "POST" and path.endswith("/message") for method, path in self.host_calls))
        self.assertEqual(self.session.messages, self.original_messages)

    async def test_other_tasks_tokens_and_current_world_authority_never_allow_original_replay(self):
        original = await self.lose_original()
        await self.session.execution_controller.revoke()
        self.assertTrue(await self.session.settle_execution_request())
        for field, value in (("task_id", "other-task"), ("segment_id", "other-segment"),
                             ("execution_write", False)):
            changed = deepcopy(original)
            changed[field] = value
            self.session.unknown = [changed]
            await self.session.recover_revoked_execution_writes()
            self.assertEqual(self.session.unknown, [changed])
            self.assertEqual(len(self.action_posts()), 1)
        changed = deepcopy(original)
        changed["envelope"]["arguments"]["delegation"] = "a-different-token"
        self.session.unknown = [changed]
        with self.assertRaises(ConnectorError) as invalid:
            await self.session.recover_revoked_execution_writes()
        self.assertEqual(invalid.exception.code, "OriginalOperationInvalid")
        self.assertEqual(self.session.unknown, [changed])
        self.assertEqual(len(self.action_posts()), 1)
        self.session.unknown = [deepcopy(original)]
        # Another client can own a new grant; suppressed local active flags do
        # not authorize recovery while the actual world is active.
        self.h.call("A", "grove.control_grant", {"goal": "Another goal", "max_actions": 4})
        await self.session.recover_revoked_execution_writes()
        self.assertEqual(self.session.unknown, [original])
        self.assertEqual(len(self.action_posts()), 1)

    async def test_mismatched_receipt_never_clears_or_reattributes_the_original_unknown(self):
        original = await self.lose_original()
        await self.session.execution_controller.revoke()
        self.assertTrue(await self.session.settle_execution_request())
        adapter = self.session.world
        async def wrong_actor(method, path, body=None):
            if method == "GET" and path.endswith(original["envelope"]["operation_id"]):
                return {"operation_id": original["envelope"]["operation_id"], "function_id": "grove.interact",
                    "actor_role_id": self.h.roles["B"], "commit_seq": 1, "result": {}}
            return await adapter(method, path, body)
        self.session.world = wrong_actor
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.recover_revoked_execution_writes()
        self.assertEqual(rejected.exception.code, "ReceiptMismatch")
        self.assertEqual(self.session.unknown, [original])
        self.assertEqual(len(self.action_posts()), 1)
        ledger = self.session.execution["receipts"][0]
        self.assertEqual((ledger["task_id"], ledger["segment_id"], ledger["status"]),
            ("original-task", "original-segment", "unknown"))


if __name__ == "__main__":
    unittest.main()
