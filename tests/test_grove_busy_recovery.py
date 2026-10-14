"""Bounded transport recovery; fixtures do not claim autonomous model play."""
from copy import deepcopy
from pathlib import Path
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent_world.grove_connector import (
    ConnectorManager, GroveSession, DOMAIN_BUSY_MAX_ATTEMPTS, MAX_SECONDS, create_connector_app,
)
from agent_world.grove_connector_errors import ConnectorError
from agent_world.worlds.shared_grove.world import INSPECT_ARGS, VIEW_ARGS


def busy():
    return ConnectorError("DomainBusy", "This candidate did not commit.", 409,
        retryable=True, recovery="wait_then_retry_same_operation", retry_after_seconds=0.1,
        world_details={"settlement": {"state": "calculating", "target_tick": 12, "operation_committed": False}})


class GroveBusyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-busy-")
        self.addCleanup(temporary.cleanup)
        self.manager = ConnectorManager(world_url="http://127.0.0.1:8890",
            local_url="http://127.0.0.1:8891", storage=Path(temporary.name), binary=Path("unused"), wrapper=None)
        self.identity = {"role_id": "same-user", "access_mode": "control"}
        self.session = GroveSession(self.manager, self.identity, "fixture-bearer", "Player", "provider/model")
        self.session.catalog = {"grove.rule_action": {"access": "write", "version": 1}}
        self.session.status = "thinking"
        self.session.execution_controller.state.update(task_id="original-task", segment_id="original-segment",
            status="running", delegation_active=True, max_actions=8, max_requests=2)
        self.session.execution_controller.active = True
        self.session.execution_controller.delegation = "original-grant"
        self.arguments = {"target_id": "source", "item_id": "owned-item", "lot_id": "owned-lot", "amount": 10,
            "expected_player_version": 3, "expected_target_version": 4, "expected_item_version": 3,
            "terms_version": 0, "action": "fuel", "delegation": "original-grant"}
        self.attempts = []
        self.bootstrap_checks = []
        self.receipt_lookups = []
        self.entry = {"timings": {"world_requests": 0, "world_request_seconds": 0.0}}
        self.session.inference_history = [self.entry]
        self.session._active_request_measurement = {"entry": self.entry, "started": time.monotonic()}

    def receipt(self, envelope):
        canonical = json.dumps({"function_id": "grove.rule_action", "arguments": envelope["arguments"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"ok": True, "operation_id": envelope["operation_id"], "function_id": "grove.rule_action",
            "function_version": 1, "actor_role_id": self.identity["role_id"], "commit_seq": 23,
            "intent_fingerprint": {"algorithm": "sha256-runtime-canonical-json-v1",
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()},
            "result": {"action_result": {"action": "fuel", "stored_energy_after": 10}}}

    def world_fixture(self, post, *, receipt_lookup=None):
        async def world(method, path, envelope=None):
            if path == "/v1/bootstrap":
                self.assertEqual(method, "GET")
                self.assertIsNone(envelope)
                self.bootstrap_checks.append(len(self.attempts))
                return {"universe": "busy-fixture-universe", "role_id": self.identity["role_id"],
                    "world": {"id": "shared-grove", "version": 1}}
            if method == "GET" and path.startswith("/v1/receipts/"):
                self.assertIsNone(envelope)
                self.assertIsNotNone(receipt_lookup)
                operation = path.rsplit("/", 1)[-1]
                self.receipt_lookups.append(operation)
                return await receipt_lookup(operation)
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/functions/grove.rule_action/invoke")
            self.attempts.append(deepcopy(envelope))
            return await post(envelope)
        return AsyncMock(side_effect=world)

    def assert_post_checks(self, count):
        self.assertEqual(len(self.attempts), count)
        self.assertEqual(self.bootstrap_checks, list(range(count)))
        self.assertEqual(self.session.world.await_count, 2 * count)
        self.assertEqual(self.receipt_lookups, [])

    async def invoke(self):
        return await self.session.invoke("grove.rule_action", deepcopy(self.arguments), model_write=True)

    async def test_transient_rejection_reuses_exact_original_intent_and_reports_each_rejection(self):
        async def world(envelope):
            if len(self.attempts) < 3:
                raise busy()
            return self.receipt(envelope)
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            result = await self.invoke()
        self.assert_post_checks(3)
        self.assertTrue(all(attempt == self.attempts[0] for attempt in self.attempts))
        self.assertEqual(self.attempts[0]["arguments"], self.arguments)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.execution["requests_used"], 0)
        self.assertEqual(self.session.world_call_count, 0)
        ledger = self.session.execution["receipts"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["operation_id"], result["operation_id"])
        self.assertEqual(ledger[0]["status"], "committed")
        self.assertEqual(result["busy_recovery"]["busy_rejections"], 2)
        self.assertEqual(result["busy_recovery"]["attempts"], 3)
        self.assertEqual([row["attempt"] for row in result["busy_recovery"]["rejections"]], [1, 2])
        self.assertEqual(self.entry["timings"]["world_busy_rejections"], 2)
        self.assertTrue(all(row["operation_id"] == result["operation_id"]
            for row in self.entry["timings"]["recent_world_busy_rejections"]))

    async def test_persistent_busy_exhausts_finite_transport_budget_without_becoming_unknown(self):
        async def world(envelope):
            raise busy()
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError) as rejected:
                await self.invoke()
        self.assert_post_checks(DOMAIN_BUSY_MAX_ATTEMPTS)
        self.assertTrue(all(attempt == self.attempts[0] for attempt in self.attempts))
        self.assertEqual(self.session.unknown, [])
        self.assertFalse(rejected.exception.execution_outcome["world_action_committed"])
        self.assertEqual(rejected.exception.execution_outcome["attempts"], DOMAIN_BUSY_MAX_ATTEMPTS)
        self.assertTrue(rejected.exception.retryable)
        restored = GroveSession(self.manager, self.identity, "fixture-bearer", "Player", "provider/model")
        self.assertEqual(restored.unknown, [])
        self.assertEqual(restored.execution["status"], "paused")
        self.assertFalse(restored.execution_controller.active)

    async def test_waiting_preserves_the_admitted_snapshot_if_the_callers_mapping_changes(self):
        intent = deepcopy(self.arguments)
        async def world(envelope):
            if len(self.attempts) == 1:
                raise busy()
            return self.receipt(envelope)
        async def wait(delay):
            intent.update(amount=999, expected_target_version=99)
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.asyncio.sleep", new=wait):
            await self.session.invoke("grove.rule_action", intent, model_write=True)
        self.assertEqual(self.attempts, [self.attempts[0], self.attempts[0]])
        self.assert_post_checks(2)
        self.assertEqual(self.attempts[0]["arguments"], self.arguments)
        self.assertEqual(intent["amount"], 999)

    async def test_changed_state_returns_conflict_without_refreshing_guards_or_retrying_it(self):
        async def world(envelope):
            if len(self.attempts) == 1:
                raise busy()
            raise ConnectorError("StateConflict", "Read and decide from current facts.", 409, retryable=False)
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError) as rejected:
                await self.invoke()
        self.assertEqual(rejected.exception.code, "StateConflict")
        self.assertEqual(self.attempts, [self.attempts[0], self.attempts[0]])
        self.assert_post_checks(2)
        self.assertEqual(self.attempts[0]["arguments"], self.arguments)
        self.assertEqual(self.session.unknown, [])
        self.assertFalse(rejected.exception.execution_outcome["world_action_committed"])

    async def test_original_native_deadline_is_not_extended_by_transport_wait(self):
        self.session._active_request_measurement["started"] -= MAX_SECONDS
        self.session.world = self.world_fixture(AsyncMock(side_effect=busy()))
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()) as wait:
            with self.assertRaises(ConnectorError) as rejected:
                await self.invoke()
        self.assert_post_checks(1)
        wait.assert_not_awaited()
        self.assertEqual(self.session.unknown, [])
        self.assertFalse(rejected.exception.execution_outcome["world_action_committed"])
        self.assertEqual(rejected.exception.execution_outcome["attempts"], 1)

    async def test_lost_retry_reply_remains_unknown_and_recovers_only_original_receipt(self):
        async def world(envelope):
            if len(self.attempts) == 1:
                raise busy()
            raise ConnectorError("WorldUnavailable", "The retry reply was lost.", 503)
        async def receipt_lookup(operation):
            self.assertEqual(operation, self.attempts[0]["operation_id"])
            return self.receipt(self.attempts[0])
        self.session.world = self.world_fixture(world, receipt_lookup=receipt_lookup)
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError) as unknown:
                await self.invoke()
        self.assertEqual(unknown.exception.code, "WorldUnavailable")
        self.assert_post_checks(2)
        self.assertEqual(self.attempts[0], self.attempts[1])
        self.assertEqual(len(self.session.unknown), 1)
        self.assertEqual(self.session.unknown[0]["envelope"], self.attempts[0])
        self.assertEqual(self.session.execution["status"], "unknown")
        self.assertEqual(unknown.exception.execution_outcome["operation_id"], self.attempts[0]["operation_id"])
        self.assertNotIn("world_action_committed", unknown.exception.execution_outcome)
        await self.session.recover_receipts(replay=False)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.bootstrap_checks, [0, 1, 2])
        self.assertEqual(self.receipt_lookups, [self.attempts[0]["operation_id"]])
        self.assertEqual(self.session.world.await_count, 6)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.execution["receipts"][0]["status"], "confirmed")

    async def test_cancel_during_known_rejection_wait_leaves_no_unknown_operation(self):
        self.session.world = self.world_fixture(AsyncMock(side_effect=busy()))
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.invoke()
        self.assert_post_checks(1)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.execution["receipts"][0]["status"], "rejected")

    async def test_retry_timeout_does_not_misreport_a_known_rejection(self):
        async def world(envelope):
            if len(self.attempts) == 1:
                raise busy()
            raise asyncio.TimeoutError()
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError) as unknown:
                await self.invoke()
        self.assertEqual(unknown.exception.code, "WorldUnavailable")
        self.assertEqual(unknown.exception.recovery, "query_receipt_before_retry")
        self.assertEqual(self.attempts, [self.attempts[0], self.attempts[0]])
        self.assert_post_checks(2)
        self.assertEqual(self.session.unknown[0]["envelope"], self.attempts[0])
        self.assertEqual(self.session.execution["receipts"][0]["status"], "unknown")
        self.assertEqual(unknown.exception.execution_outcome["stage"], "unknown_result")
        self.assertEqual(unknown.exception.execution_outcome["operation_id"], self.attempts[0]["operation_id"])
        self.assertNotIn("world_action_committed", unknown.exception.execution_outcome)

    async def test_retry_window_does_not_cancel_an_already_sent_original_post(self):
        real_sleep = asyncio.sleep
        async def world(envelope):
            if len(self.attempts) == 1:
                raise busy()
            await real_sleep(0.2)  # Still inside the world's own transport limit.
            return self.receipt(envelope)
        self.session.world = self.world_fixture(world)
        with patch("agent_world.grove_connector.DOMAIN_BUSY_RETRY_SECONDS", 0.1), \
                patch("agent_world.grove_connector.asyncio.sleep", new=AsyncMock()):
            result = await self.invoke()
        self.assertEqual(self.attempts, [self.attempts[0], self.attempts[0]])
        self.assert_post_checks(2)
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(result["busy_recovery"]["attempts"], 2)
        self.assertEqual(self.session.execution["receipts"][0]["status"], "committed")

    async def test_stop_reclaim_timeout_task_segment_or_grant_change_prevents_next_send(self):
        controller = self.session.execution_controller
        original = deepcopy(controller.state)
        changes = (
            lambda: setattr(controller, "stopped", True),
            lambda: setattr(controller, "reclaiming", True),
            lambda: controller.native_interrupted("execution"),
            lambda: controller.state.update(task_id="different-task"),
            lambda: controller.state.update(segment_id="different-segment"),
            lambda: setattr(controller, "delegation", "different-grant"),
        )
        for change in changes:
            with self.subTest(change=change):
                controller.state = deepcopy(original)
                controller.active, controller.stopped, controller.reclaiming = True, False, False
                controller.delegation = "original-grant"
                self.session.unknown = []
                self.attempts, self.bootstrap_checks, self.receipt_lookups = [], [], []
                self.session.world = self.world_fixture(AsyncMock(side_effect=busy()))
                async def wait(delay):
                    change()
                with patch("agent_world.grove_connector.asyncio.sleep", new=wait):
                    with self.assertRaises(ConnectorError) as stopped:
                        await self.invoke()
                self.assert_post_checks(1)
                self.assertEqual(self.session.unknown, [])
                self.assertFalse(stopped.exception.execution_outcome["world_action_committed"])

    async def test_only_exact_trusted_busy_semantics_enter_recovery(self):
        for error in (
            ConnectorError("RuleViolation", "Real rule rejection.", 409),
            ConnectorError("DomainBusy", "Not confirmed uncommitted.", 409, retryable=True,
                recovery="wait_then_retry_same_operation"),
            ConnectorError("DomainBusy", "Missing retry permission.", 409, world_details=busy().world_details),
            ConnectorError("StorageBusy", "Storage result uncertain.", 503, retryable=True,
                recovery="retry_same_operation"),
        ):
            with self.subTest(error=error.code):
                # An uncertain response closes that original controller and
                # journal. Each transport case needs its own admitted session.
                self.setUp()
                self.session.world = self.world_fixture(AsyncMock(side_effect=error))
                with self.assertRaises(ConnectorError) as rejected:
                    await self.invoke()
                self.assertEqual(rejected.exception.code, error.code)
                self.assert_post_checks(1)

    async def test_world_http_preserves_safe_recovery_metadata_without_private_details(self):
        payload = busy().public_payload()
        payload["details"]["secret_domain_members"] = ["hidden-object"]
        client = AsyncMock()
        client.request.return_value = httpx.Response(409, json=payload)
        with patch("agent_world.grove_connector.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(ConnectorError) as rejected:
                await self.session.world("POST", "/v1/functions/grove.rule_action/invoke", {})
        error = rejected.exception.public_payload()
        self.assertTrue(error["retryable"])
        self.assertEqual(error["recovery"], payload["recovery"])
        self.assertEqual(error["retry_after_seconds"], 0.1)
        self.assertEqual(error["details"], {"settlement": payload["details"]["settlement"]})
        self.assertNotIn("hidden-object", str(error))

    async def test_mcp_busy_error_preserves_uncommitted_original_operation_without_new_observation(self):
        reads = {"map.viewport": {"access": "read", "version": 1, "input_schema": deepcopy(VIEW_ARGS)},
            "grove.inspect": {"access": "read", "version": 1, "input_schema": deepcopy(INSPECT_ARGS)}}
        self.session.catalog.update(reads)
        self.session.agent_client.configure_catalog(
            [{"function_id": name, **spec} for name, spec in self.session.catalog.items()],
            {"contract_version": 1, "functions": [
                {"function_id": name, "access": "read"} for name in reads]})
        rejected = busy()
        rejected.execution_outcome = {"stage": "definite_rejection", "operation_id": "original-operation",
            "function_id": "grove.rule_action", "world_action_committed": False,
            "receipt_confirmed": False, "attempts": 6, "busy_rejections": 6, "wait_seconds": 2.1}
        self.session.tool_call = AsyncMock(side_effect=rejected)
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=self.manager.local_url) as client:
            response = await client.post("/connector/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "grove_rule_action", "arguments": {
                    "target_id": "source", "item_id": "owned-item"}}},
                headers={"Authorization": "Bearer fixture-capability"})
        result = response.json()["result"]
        error = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertTrue(error["retryable"])
        self.assertEqual(error["recovery"], "wait_then_retry_same_operation")
        self.assertEqual(error["retry_after_seconds"], 0.1)
        self.assertEqual(error["execution_outcome"], rejected.execution_outcome)
        self.assertEqual(error["details"], rejected.world_details)
        self.assertEqual(json.loads(result["content"][0]["text"]), error)
        self.assertIn("before commit", error["recovery_meaning"])
        self.assertEqual(error["read_requests"], [{"function_id": "map.viewport", "tool": "map_viewport",
            "native_tool": "grove_world_map_viewport", "arguments": {}},
            {"function_id": "grove.inspect", "tool": "grove_inspect", "native_tool": "grove_world_grove_inspect",
                "arguments": {"entity_id": "source"}},
            {"function_id": "grove.inspect", "tool": "grove_inspect", "native_tool": "grove_world_grove_inspect",
                "arguments": {"entity_id": "owned-item"}}])
        self.assertNotIn("observation_id", error)
        self.assertEqual(self.session.observations.sequence, 0)


if __name__ == "__main__":
    unittest.main()
