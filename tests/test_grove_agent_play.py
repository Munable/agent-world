"""Everyday choices remain public SDK intents, with only delivered evidence."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent_world.grove_agent_client import GroveAgentClient, HttpGroveTransport, MemoryOperationJournal
from agent_world.grove_agent_errors import AgentClientError
from agent_world.grove_agent_play import GroveAgentPlay
from agent_world.grove_connector import ConnectorManager, GroveSession, ConnectorError, EXECUTION_CHECKPOINT_SCHEMA
from agent_world.grove_execution import _preparation_facts
from agent_world.grove_execution import ExecutionController, ExecutionPorts
from tests.test_grove_agent_client import PublicWorldPort
from tests.test_grove_execution_controller import CATALOG


ACTION = {"type": "object", "properties": {
    "target_id": {"type": "string"}, "action": {"const": "greet"},
    "text": {"type": "string", "minLength": 1},
    "expected_player_version": {"type": "integer"}, "expected_target_version": {"type": "integer"}},
    "required": ["target_id", "action", "text", "expected_player_version", "expected_target_version"],
    "additionalProperties": False}


class PlayBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = PublicWorldPort()
        next(row for row in self.port.descriptors if row["function_id"] == "grove.rule_action")["input_schema"] = ACTION
        original = httpx.AsyncClient
        mocked = patch("agent_world.grove_agent_ports.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(self.port.http)))
        mocked.start()
        self.addCleanup(mocked.stop)
        self.client = GroveAgentClient(HttpGroveTransport("https://world.example", "private-test-credential"),
            self.port.role, "configured-world-endpoint", journal=MemoryOperationJournal())
        await self.client.discover()
        self.play = GroveAgentPlay(self.client)
        self.play.begin_request("original-request")

    def delivery(self, **extra):
        value = {"player": {"id": self.port.role, "version": 3},
            "action_options_projection": {"scope": "selected_declared_action"},
            "entity": {"id": "public-piece", "version": 2, "name": "A participant's piece",
                "creative": {"fields": {"count": 1}, "automatic_field_change": {"step": 9, "after": 1}}},
            "action_options": [{"action": "greet", "function_id": "grove.rule_action", "available": True,
                "arguments": {"target_id": "public-piece", "action": "greet"}, "input_schema": ACTION}]}
        return self.client.present("grove.inspect", {"ok": True, "read_only": True,
            "function_id": "grove.inspect", "function_version": 1, "snapshot_seq": 7,
            "result": value | extra})

    def present(self, raw):
        return self.play.present(raw, request_scope="original-request", source_function="grove.inspect")

    async def test_choice_submits_exact_intent_through_original_journal_and_receipt(self):
        raw = self.delivery()
        shown = self.present(raw)
        menu = shown["result"]["action_options"][0]
        self.assertNotEqual(shown["observation_id"], raw["observation_id"])
        self.assertNotIn("expected_target_version", menu["parameter_schema"]["properties"])
        with self.assertRaises(AgentClientError) as failed:
            self.play.resolve(menu["choice_ref"], {"text": "Hello", "target_id": "another"}, request_scope="original-request")
        self.assertEqual(failed.exception.code, "FixedPlayIntent")
        choice = self.play.resolve(menu["choice_ref"], {"text": "Hello"}, request_scope="original-request")
        await self.client.write(choice["function_id"], choice["arguments"], observation_id=choice["observation_id"], operation_id="original-greeting")
        function, envelope = self.port.sent[-1]
        self.assertEqual(function, "grove.rule_action")
        self.assertEqual(envelope["arguments"], {"target_id": "public-piece", "action": "greet", "text": "Hello",
            "expected_player_version": 3, "expected_target_version": 2})
        self.assertEqual(self.client.pending_operations, [])
        self.assertIn("original-greeting", self.port.receipts)
        with self.assertRaises(AgentClientError):
            self.play.resolve(menu["choice_ref"], {"text": "Again"}, request_scope="original-request")

    def test_omitted_object_cannot_borrow_raw_version_or_modify_original_bundle(self):
        raw = self.delivery(entities=[{"id": "candidate-" + str(i), "version": i + 5,
            "name": "piece", "creative": {"fields": {"long": "x" * 250}}} for i in range(30)])
        original = self.client.observations.get(raw["observation_id"])
        shown = self.present(raw)
        shown_ids = {row["id"] for row in shown["result"].get("entities", [])} | {"public-piece"}
        hidden = next(identity for identity in original["versions"]["entity"] if identity not in shown_ids)
        self.assertEqual(set(self.client.observations.get(shown["observation_id"])["versions"]["entity"]), shown_ids)
        intent = {"target_id": hidden, "action": "greet", "text": "Hello"}
        with self.assertRaises(AgentClientError) as failed:
            self.client.bind("grove.rule_action", intent, shown["observation_id"])
        self.assertEqual(failed.exception.code, "ObservedVersionRequired")
        self.assertEqual(self.client.bind("grove.rule_action", intent, raw["observation_id"])["expected_target_version"],
            original["versions"]["entity"][hidden])
        self.assertEqual(self.client.observations.get(raw["observation_id"]), original)

    def test_incomplete_menu_requires_formal_read_and_new_request_expires_refs(self):
        raw = self.delivery()
        raw["result"]["action_options"][0].pop("input_schema")
        outline = self.present(raw)["result"]["action_options"][0]
        self.assertNotIn("choice_ref", outline)
        self.assertEqual(outline["read_request"]["arguments"], {"entity_id": "public-piece", "action": "greet"})
        reference = self.present(self.delivery())["result"]["action_options"][0]["choice_ref"]
        self.play.begin_request("next-request")
        with self.assertRaises(AgentClientError):
            self.play.resolve(reference, {"text": "Hello"}, request_scope="original-request")
        with self.assertRaises(AgentClientError):
            self.play.resolve(reference, {"text": "Hello"}, request_scope="next-request")

    def test_receipt_and_historical_change_preserved_without_claiming_causation(self):
        raw = self.delivery()
        raw.update(operation_id="original-op", commit_seq=12, confirmed_outcome={"action_result": {"text": "Hello"}})
        # This test supplies an SDK receipt boundary, not a query with a forged operation.
        raw = self.client.present("grove.rule_action", {key: value for key, value in raw.items()
            if key not in {"observation_id", "agent_observation"}})
        shown = self.play.present(raw, request_scope="original-request", source_function="grove.rule_action")
        self.assertEqual(shown["confirmed_outcome"], raw["confirmed_outcome"])
        self.assertEqual(shown["commit_seq"], 12)
        self.assertEqual(shown["result"]["entity"]["creative"]["fields"], {"count": 1})
        self.assertEqual(shown["result"]["entity"]["creative"]["automatic_field_change"]["step"], 9)
        changed = deepcopy(shown)
        changed["commit_seq"] = 99
        with self.assertRaises(AgentClientError) as failed:
            self.client.present_derived_view(raw, changed)
        self.assertEqual(failed.exception.code, "DerivedObservationReceiptMismatch")

    def test_random_choice_handles_do_not_count_as_progress_but_authored_values_do(self):
        first = self.present(self.delivery())["result"]
        second = self.present(self.delivery())["result"]
        self.assertNotEqual(first["action_options"][0]["choice_ref"], second["action_options"][0]["choice_ref"])
        self.assertEqual(_preparation_facts(first), _preparation_facts(second))
        second["entity"]["creative"]["fields"]["choice_ref"] = "authored-new-value"
        self.assertNotEqual(_preparation_facts(first), _preparation_facts(second))
        second = deepcopy(first)
        second["action_options"][0]["parameter_schema"]["properties"]["text"]["minLength"] = 2
        self.assertNotEqual(_preparation_facts(first), _preparation_facts(second))

    def test_nearby_read_entries_survive_a_scene_without_full_entity_snapshots(self):
        raw = self.delivery(nearby_discovery=[{"id": "nearby-work", "name": "Someone's work", "x": 2, "y": 1,
            "read_request": {"tool": "grove_inspect", "arguments": {"entity_id": "nearby-work"}}}])
        shown = self.present(raw)
        row = shown["result"]["nearby_discovery"][0]
        self.assertEqual(row["read_request"], {"function_id": "grove.inspect", "arguments": {"entity_id": "nearby-work"}})
        self.assertNotIn("nearby-work", self.client.observations.get(shown["observation_id"])["versions"]["entity"])

    def test_complete_selected_action_expands_within_sdk_limit_without_another_read(self):
        raw = self.delivery()
        # A real complete selected-action condition must remain whole; giving
        # the same read pointer forever would make an otherwise legal choice unusable.
        raw["result"]["action_options_projection"] = {"scope": "selected_declared_action"}
        condition = {"public_declared_condition": "x" * 10000}
        raw["result"]["action_options"][0]["preconditions"] = condition
        shown = self.present(raw)
        menu = shown["result"]["action_options"][0]
        self.assertEqual(menu["preconditions"], condition)
        self.assertIn("choice_ref", menu)
        size = len(self.client.observations.serialize(shown).encode())
        self.assertGreater(size, 8192)
        self.assertLessEqual(size, 48 * 1024)
        self.assertTrue(shown["play_projection"]["byte_budget"]["expanded_selected_details"])
        self.assertEqual(self.play.resolve(menu["choice_ref"], {"text": "Hello"}, request_scope="original-request")["observation_id"],
            shown["observation_id"])


class PlayHostBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=Path(directory.name), binary=Path("unused"), wrapper=None)
        self.session = GroveSession(manager, {"role_id": "person", "access_mode": "control"}, "test-token", "Reader", "provider/model")
        self.session.session_id = "original-session"
        self.owner = {"request_id": "old-request", "session_id": "original-session", "purpose": "explain", "world_write_authorized": False}
        self.session.active_native_request = deepcopy(self.owner)
        self.session.play.begin_request("old-request")

    def test_large_fixed_goal_falls_back_exactly_and_scope_errors_still_fail(self):
        raw = self.session.agent_client.present("map.viewport", {"player": {"id": "person", "version": 1},
            "execution_task": {"goal": "长目标" * 1300}})
        result = self.session.present_play_result("map.viewport", raw, request_scope="old-request")
        self.assertEqual(result, raw)
        with self.assertRaises(ConnectorError):
            self.session.present_play_result("map.viewport", raw, request_scope="other-request")

    async def test_readonly_choice_never_calls_controller_write(self):
        self.session.execution_controller.write = AsyncMock()
        with self.assertRaises(ConnectorError) as failed:
            await self.session.play_act({"choice_ref": "play-anything"}, owner=self.owner)
        self.assertEqual(failed.exception.code, "ConversationReadOnly")
        self.session.execution_controller.write.assert_not_awaited()

    async def test_late_read_does_not_register_facts_under_replacement_request(self):
        self.session._sync_agent_client_catalog = lambda: None
        self.session.agent_client._functions = {"map.viewport": {"access": "read"}}
        self.session.catalog = {"map.viewport": {"access": "read"}}
        async def delayed(function, arguments):
            self.session.active_native_request = self.owner | {"request_id": "next-request"}
            self.session.play.begin_request("next-request")
            await asyncio.sleep(0)
            return {"result": {"player": {"id": "person", "version": 99}}}
        self.session.invoke = delayed
        self.session.present_agent_result = AsyncMock()
        with self.assertRaises(ConnectorError) as failed:
            await self.session.play_look({}, owner=self.owner)
        self.assertEqual(failed.exception.code, "StalePlayRequest")
        self.session.present_agent_result.assert_not_called()

    async def test_explicit_play_mode_denies_raw_tools_before_any_world_call(self):
        self.session.status = "thinking"
        self.session.active_native_request = self.owner | {"purpose": "execution", "interaction_mode": "play"}
        self.session.invoke = AsyncMock()
        for tool in ("grove_inspect", "grove_move", "grove_rule_action", "world_bootstrap"):
            with self.assertRaises(ConnectorError) as failed:
                await self.session.tool_call(tool, {})
            self.assertEqual(failed.exception.code, "PlayModeToolDenied")
        self.session.invoke.assert_not_awaited()
        self.assertEqual(self.session.world_call_count, 0)
        permitted = self.session.play_execution_tools()
        self.assertFalse(permitted["grove_world_*"])
        self.assertTrue(permitted["grove_world_play_act"])
        self.assertNotIn("grove_world_grove_move", permitted)

    def test_interaction_mode_persists_and_cannot_change_through_resume(self):
        def controller():
            return ExecutionController("person", max_requests=2, max_world_calls=16, max_seconds=150,
                ports=ExecutionPorts(invoke=AsyncMock(), read=AsyncMock(), recover=AsyncMock(),
                    unknown_operations=lambda: [], native_segment=AsyncMock(), settle=AsyncMock(), persist=Mock()))
        original = controller()
        original.start({"goal": "Try one public work", "max_actions": 2, "max_requests": 2, "interaction_mode": "play"},
            entered=True, native_connected=True, worker_busy=False, catalog=CATALOG)
        self.assertEqual(original.value("interaction_mode"), "play")
        saved = original.snapshot()
        restored = controller()
        restored.restore(saved)
        self.assertEqual(restored.value("interaction_mode"), "play")
        with self.assertRaises(ConnectorError) as failed:
            restored.resume({"task_id": saved["task_id"], "interaction_mode": "full"},
                entered=True, native_connected=True, worker_busy=False)
        self.assertEqual(failed.exception.code, "InvalidTask")
        saved.pop("interaction_mode")
        legacy = controller()
        legacy.restore(saved)
        self.assertEqual(legacy.value("interaction_mode"), "full")

    async def test_checkpoint_diagnoses_long_summary_without_echoing_or_accepting_it(self):
        report = {"task_id": "task", "execution_segment_id": "segment", "status": "completed",
            "summary": "private-report-sentinel" * 40, "next_intent": "", "evidence_operation_ids": []}
        with self.assertRaises(ConnectorError) as failed:
            await self.session.execution_controller.checkpoint(report, schema=EXECUTION_CHECKPOINT_SCHEMA)
        self.assertEqual(failed.exception.code, "InvalidCheckpoint")
        issue = failed.exception.world_details["schema_errors"][0]
        self.assertEqual((issue["path"], issue["constraint"], issue["limit"]), (["summary"], "maxLength", 600))
        self.assertNotIn("private-report-sentinel", str(failed.exception.public_payload()))

    def configure_dispatch(self):
        self.session.catalog = {"grove.inspect": {"function_id": "grove.inspect", "access": "read", "version": 1,
            "input_schema": {"type": "object", "required": ["entity_id"], "additionalProperties": False,
                "properties": {"entity_id": {"type": "string"}}}},
            "grove.rule_action": {"function_id": "grove.rule_action", "access": "write", "version": 1, "input_schema": ACTION},
            "grove.hidden": {"access": "read", "version": 1, "input_schema": {"type": "object"}}}
        self.session.agent_contract = {"contract_version": 1, "functions": [
            {"function_id": "grove.inspect", "access": "read"},
            {"function_id": "grove.rule_action", "access": "write", "guard_paths": [
                ["expected_player_version"], ["expected_target_version"]]}]}
        self.session.agent_client.configure_catalog([
            {"function_id": name, **spec} for name, spec in self.session.catalog.items()], self.session.agent_contract)

    def test_capability_discovery_is_exact_declared_schema_without_world_observation(self):
        self.configure_dispatch()
        before = self.session.observations.sequence
        result = self.session.capability_read({})
        self.assertEqual({r["function_id"] for r in result["functions"]}, {"grove.inspect", "grove.rule_action"})
        schema = self.session.capability_read({"function_id": "grove.rule_action"})["input_schema"]
        self.assertEqual(set(schema["properties"]), {"target_id", "action", "text"})
        self.assertEqual(set(schema["required"]), {"target_id", "action", "text"})
        self.assertEqual(self.session.observations.sequence, before)
        with self.assertRaises(ConnectorError):
            self.session.capability_read({"function_id": "grove.hidden"})

    async def test_generic_read_rejects_late_owner_without_registering_observation(self):
        self.configure_dispatch()
        before = self.session.observations.sequence
        async def late(*args):
            self.session.active_native_request = {**self.owner, "request_id": "new-request"}
            return {"entity": {"id": "actual", "version": 3}}
        self.session.invoke = AsyncMock(side_effect=late)
        with self.assertRaises(ConnectorError):
            await self.session.read_world_tool({"function_id": "grove.inspect", "arguments": {"entity_id": "actual"}}, owner=self.owner)
        self.assertEqual(self.session.observations.sequence, before)

    async def test_generic_write_keeps_exact_semantic_arguments_and_original_observation(self):
        self.configure_dispatch()
        self.session.require_execution_request = Mock()
        self.session._write_agent_tool = AsyncMock(return_value={"operation_id": "original-op"})
        owner = {**self.owner, "purpose": "execution", "world_write_authorized": True, "segment_id": "original-segment"}
        self.session.active_native_request = owner
        semantic = {"target_id": "actual", "action": "greet", "text": "My exact choice"}
        result = await self.session.act_world_tool({"function_id": "grove.rule_action", "arguments": semantic,
            "observation_id": "original-observation"}, owner=owner)
        self.assertEqual(result, {"operation_id": "original-op"})
        self.assertEqual(self.session._write_agent_tool.await_args.args, ("grove.rule_action", {
            **semantic, "observation_id": "original-observation", "execution_segment_id": "original-segment"}))
        with self.assertRaises(ConnectorError):
            await self.session.act_world_tool({"function_id": "grove.rule_action", "arguments": {**semantic,
                "expected_target_version": 999}, "observation_id": "original-observation"}, owner=owner)
        self.assertEqual(self.session._write_agent_tool.await_count, 1)
