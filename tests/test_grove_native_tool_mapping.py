"""Adapter names and formal recovery reads are discoverable without aliases."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from agent_world.grove_agent_client import GroveAgentClient, MemoryOperationJournal
from agent_world.grove_agent_play import GroveAgentPlay
from agent_world.grove_connector import (EXECUTION_INSTRUCTIONS, GroveSession, MCP_NAME,
    MAX_NATIVE_TOOL_MAPPING_BYTES, MAX_NATIVE_TOOL_MAPPING_ROWS, create_connector_app)
from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_native_history import REQUEST_CONTRACT_PREFIX, TOOL_NAME as HISTORY_TOOL


class NativeToolMappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = GroveAgentClient(Mock(), "person", "configured-world", journal=MemoryOperationJournal())
        self.session = object.__new__(GroveSession)
        self.session.agent_client = self.client
        self.session.observations = self.client.observations
        self.session.play = GroveAgentPlay(self.client)
        self.session.opportunity = None
        self.session.catalog = {
            "map.viewport": self.spec({"radius": {"type": "integer", "minimum": 0, "maximum": 4}}),
            "grove.inspect": self.spec({"entity_id": {"type": "string"}}, ["entity_id"]),
            "grove.definition_read": self.spec({"definition_ref": {"type": "string"}}, ["definition_ref"]),
            "grove.effect_evidence": self.spec({"entity_id": {"type": "string"},
                "definition_ref": {"type": "string"}, "predicates": {"type": "array", "maxItems": 8}},
                ["entity_id", "definition_ref"]),
            "grove.rule_action": self.spec({"target_id": {"type": "string"}}, ["target_id"], access="write"),
            "grove.undeclared_read": self.spec({})}
        self.declare(list(self.session.catalog)[:-1])

    @staticmethod
    def spec(properties, required=(), access="read"):
        return {"access": access, "version": 1, "description": "Fixture declared capability.",
            "input_schema": {"type": "object", "properties": properties, "required": list(required),
                "additionalProperties": False}}

    def declare(self, functions):
        self.client.configure_catalog([{"function_id": name, **spec} for name, spec in self.session.catalog.items()],
            {"contract_version": 1, "functions": [{"function_id": name,
                "access": self.session.catalog[name]["access"]} for name in functions]})

    def test_mapping_uses_actual_native_names_and_only_declared_world_capabilities(self):
        original = deepcopy(self.session.catalog)
        mapping = self.session.native_tool_mapping(interaction_mode="conversation")
        rows = {row["tool"]: row for row in mapping["tools"]}
        self.assertEqual(rows["map_viewport"], {"function_id": "map.viewport", "tool": "map_viewport",
            "native_tool": "grove_world_map_viewport", "access": "read"})
        self.assertEqual(rows["grove_inspect"]["native_tool"], "grove_world_grove_inspect")
        for tool in ("world_bootstrap", "execution_checkpoint", HISTORY_TOOL):
            self.assertEqual(rows[tool]["native_tool"], MCP_NAME + "_" + tool)
        self.assertNotIn("grove_map_viewport", rows)
        self.assertNotIn("grove_undeclared_read", rows)
        self.assertNotIn("grove.map_viewport", self.client.function_ids)
        self.assertEqual(self.session.catalog, original)
        self.assertEqual(mapping["omitted_tools"], 0)
        self.assertLess(len(EXECUTION_INSTRUCTIONS.encode()), 1800)

    def test_mapping_is_bounded_and_recomputed_after_real_contract_change(self):
        self.session.catalog = {"grove.long_" + str(index) + "_" + "x" * 160: self.spec({}) for index in range(100)}
        self.declare(list(self.session.catalog))
        mapping = self.session.native_tool_mapping(interaction_mode="conversation")
        self.assertLessEqual(len(mapping["tools"]), MAX_NATIVE_TOOL_MAPPING_ROWS)
        self.assertLessEqual(len(json.dumps(mapping, ensure_ascii=False, separators=(",", ":")).encode()), MAX_NATIVE_TOOL_MAPPING_BYTES)
        self.assertEqual(mapping["omitted_tools"], len(self.session.tools()) - 3 - len(mapping["tools"]))
        self.declare([])
        self.assertEqual({row["tool"] for row in self.session.native_tool_mapping(interaction_mode="conversation")["tools"]},
            {"world_bootstrap", "execution_checkpoint", HISTORY_TOOL, "play_look", "play_act"})

    def test_name_mapping_requires_no_tool_schema_and_intersects_the_current_directory(self):
        for spec in self.session.catalog.values():
            spec.pop("input_schema")
        self.session.tools = Mock(side_effect=AssertionError("Naming cannot construct RPC schemas"))
        self.client.tools = Mock(side_effect=AssertionError("Naming cannot construct SDK schemas"))
        mapping = self.session.native_tool_mapping(interaction_mode="conversation")
        rows = {row["function_id"]: row for row in mapping["tools"] if "function_id" in row}
        self.assertEqual(rows["map.viewport"]["native_tool"], "grove_world_map_viewport")
        self.assertNotIn("grove.undeclared_read", rows)
        self.assertEqual(self.session.native_read_requests([
            {"function_id": "map.viewport", "arguments": {}}]), [])
        self.session.catalog.pop("grove.inspect")
        rows = {row.get("function_id") for row in self.session.native_tool_mapping(interaction_mode="conversation")["tools"]}
        self.assertNotIn("grove.inspect", rows)
        self.session.tools.assert_not_called()
        self.client.tools.assert_not_called()

    def test_full_mapping_and_formal_next_read_use_only_generic_dispatch(self):
        self.assertEqual({row["tool"] for row in self.session.native_tool_mapping()["tools"]},
            {"capability_read", "world_read", "world_act", "play_look", "play_act",
                "execution_checkpoint", HISTORY_TOOL})
        self.session.active_native_request = {"purpose": "execution", "interaction_mode": "full"}
        self.assertEqual(self.session.native_read_requests([{ "function_id": "grove.inspect",
            "arguments": {"entity_id": "actual"}}]), [{"function_id": "grove.inspect", "tool": "world_read",
            "native_tool": "grove_world_world_read", "arguments": {"function_id": "grove.inspect",
                "arguments": {"entity_id": "actual"}}}])

    def test_formal_reads_keep_exact_arguments_and_do_not_admit_writes_unknowns_or_handles(self):
        requests = [
            {"function_id": "grove.effect_evidence", "arguments": {"entity_id": "object",
                "definition_ref": "immutable-ref", "predicates": []}},
            {"function_id": "grove.definition_read", "arguments": {"definition_ref": "immutable-ref"}},
            {"function_id": "grove.rule_action", "arguments": {"target_id": "object"}},
            {"function_id": "grove.undeclared_read", "arguments": {}},
            {"function_id": "grove.inspect", "arguments": {}},
            {"function_id": "grove.inspect", "arguments": {"entity_id": "object", "observation_id": "old"}},
            {"function_id": "map.viewport", "arguments": {"radius": 100}},
            {"function_id": "grove_world_map_viewport", "arguments": {}},
            {"function_id": "grove.inspect", "arguments": {"entity_id": "object", "delegation": "grant"}}]
        original = deepcopy(requests)
        mapped = self.session.native_read_requests(requests)
        self.assertEqual([row["function_id"] for row in mapped], ["grove.effect_evidence", "grove.definition_read"])
        self.assertEqual(mapped[0]["native_tool"], "grove_world_grove_effect_evidence")
        self.assertEqual(mapped[0]["arguments"], original[0]["arguments"])
        self.assertNotIn("observation_id", mapped[0])
        mapped[0]["arguments"]["entity_id"] = "changed-copy"
        self.assertEqual(requests, original)
        self.assertEqual(self.client.observations.sequence, 0)

    async def test_rule_error_prefers_server_formal_read_over_generic_map_and_old_inspection(self):
        formal = [{"function_id": "grove.definition_read", "arguments": {"definition_ref": "actual-ref"}}]
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("RuleViolation", "Unknown requested fact.", 409,
            world_details={"read_requests": formal}))
        app = create_connector_app(storage=Path("unused-native-mapping-fixture"), wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as caller:
            result = (await caller.post("/connector/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "grove_effect_evidence", "arguments": {
                    "entity_id": "old-selection", "definition_ref": "actual-ref"}}},
                headers={"Authorization": "Bearer fixture-capability"})).json()["result"]
        self.assertTrue(result["isError"])
        error = result["structuredContent"]
        self.assertEqual(error["error"], "RuleViolation")
        self.assertEqual(error["message"], "Unknown requested fact.")
        self.assertEqual(error["details"]["read_requests"], formal)
        self.assertEqual(error["read_requests"], [{"function_id": "grove.definition_read",
            "tool": "grove_definition_read", "native_tool": "grove_world_grove_definition_read",
            "arguments": {"definition_ref": "actual-ref"}}])
        self.assertNotIn("observation_id", error)
        self.assertEqual(self.client.observations.sequence, 0)
        self.session.tool_call.assert_awaited_once()

    async def test_each_native_request_carries_current_task_and_adapter_mapping_without_invoking_reads(self):
        self.session.disconnecting = False
        self.session.unknown = {}
        self.session.execution_controller = Mock(reclaiming=False, stopped=False)
        values = {"goal": "Read the actual facts", "status": "running", "requests_used": 0, "max_requests": 8,
            "remaining_actions": 8, "action_entity_ids": [], "effect_evidence_reads": []}
        self.session.execution_controller.value.side_effect = lambda name, default=None: values.get(name, default)
        self.session.execution_controller.action_summary.return_value = {}
        self.session.execution_controller.effect_read_references.return_value = []
        self.session._native_context_plugin = None
        self.session.active_automatic = False
        self.session.messages = []
        self.session.note = Mock()
        self.session.remember = Mock()
        self.session.private_context = {}
        self.session.model = "provider/model"
        self.session.session_id = "owned-session"
        self.session.active_native_request = None
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "person"}, "entities": []})
        self.session.role_id = "person"
        captured = []

        async def native(method, path, body, **kwargs):
            captured.append((deepcopy(body), deepcopy(self.session.active_native_request)))
            raise RuntimeError("fixture stops before model execution")

        self.session.host = AsyncMock(side_effect=native)
        for task in ("task-a", "task-b"):
            values.update(task_id=task, segment_id=task + "-segment")
            self.session.inference_started = Mock(return_value={"purpose": "execution", "task_id": task,
                "segment_id": task + "-segment", "status": "started"})
            with self.assertRaisesRegex(RuntimeError, "fixture stops"):
                await self.session.turn("Read actual facts", executing=True)
            body, owner = captured[-1]
            part = next(row for row in body["parts"] if row["text"].startswith(REQUEST_CONTRACT_PREFIX + "\n"))
            contract = json.loads(part["text"].split("\n", 1)[1])
            self.assertEqual((contract["task_id"], contract["segment_id"]), (task, task + "-segment"))
            self.assertEqual(contract, owner)
            self.assertEqual(contract["native_tool_mapping"], self.session.native_tool_mapping())
            self.assertIsNone(self.session.active_native_request)
        self.assertEqual(self.session.host.await_count, 2)
