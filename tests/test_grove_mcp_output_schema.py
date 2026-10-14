"""Native MCP validates both real observations and explicit error envelopes."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from jsonschema import Draft202012Validator

from agent_world.grove_agent_client import (AgentClientError, GroveAgentClient,
    MemoryOperationJournal, mcp_tool_output_schema, observation_output_schema)
from agent_world.grove_connector import GroveSession, create_connector_app
from agent_world.grove_connector_errors import ConnectorError


class McpToolOutputSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = GroveAgentClient(Mock(), "person", "configured-world", journal=MemoryOperationJournal())
        self.descriptor = {"function_id": "grove.effect_evidence", "access": "read", "version": 1,
            "description": "Read actual public effects.", "input_schema": {"type": "object",
                "properties": {"entity_id": {"type": "string"}, "definition_ref": {"type": "string"}},
                "required": ["entity_id", "definition_ref"], "additionalProperties": False},
            "output_schema": {"type": "object"}}
        self.client.configure_catalog([self.descriptor], {"contract_version": 1,
            "functions": [{"function_id": "grove.effect_evidence", "access": "read"}]})
        # Only exercise the MCP boundary; no host profile, files or model.
        self.session = object.__new__(GroveSession)
        self.session.agent_client = self.client
        self.session.observations = self.client.observations
        self.session.catalog = {"grove.effect_evidence": deepcopy(self.descriptor)}

    def observation(self):
        return self.client.present_query("grove.effect_evidence", {"ok": True, "read_only": True,
            "function_id": "grove.effect_evidence", "function_version": 1, "snapshot_seq": 3,
            "result": {"entity_id": "object", "definition_ref": "immutable-definition",
                "observed_step": 2, "predicates": []}})

    def test_tool_schema_accepts_success_without_weakening_observation_requirements(self):
        observation = self.observation()
        schema = mcp_tool_output_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["anyOf"][0], observation_output_schema())
        for candidate in (schema, observation_output_schema()):
            self.assertTrue(Draft202012Validator(candidate).is_valid(observation))
            for key in ("observation_id", "agent_observation"):
                incomplete = deepcopy(observation)
                incomplete.pop(key)
                self.assertFalse(Draft202012Validator(candidate).is_valid(incomplete))
        invalid = deepcopy(observation)
        invalid["agent_observation"]["scope"] = "untrusted"
        self.assertFalse(Draft202012Validator(schema).is_valid(invalid))

    def test_error_branch_requires_explicit_error_message_and_creates_no_observation(self):
        error = ConnectorError("RuleViolation", "Unknown registered fact.", 409,
            execution_outcome={"stage": "precommit_rejected", "world_action_committed": False}).public_payload()
        self.assertTrue(Draft202012Validator(mcp_tool_output_schema()).is_valid(error))
        self.assertFalse(Draft202012Validator(observation_output_schema()).is_valid(error))
        for incomplete in ({"error": "RuleViolation"}, {"message": "Rejected"},
                           {"error": 4, "message": "Rejected"}, {**error, "observation_id": "invented"},
                           {**error, "agent_observation": {"scope": "client_presented"}}):
            self.assertFalse(Draft202012Validator(mcp_tool_output_schema()).is_valid(incomplete))

    def test_sdk_success_presentation_cannot_escape_through_error_branch(self):
        error = {"error": "RuleViolation", "message": "Rejected before any observation."}
        self.assertTrue(Draft202012Validator(mcp_tool_output_schema()).is_valid(error))
        with patch.object(self.client.observations, "present", return_value=deepcopy(error)):
            with self.assertRaises(AgentClientError) as rejected:
                self.client.present("grove.effect_evidence", {"result": {}})
        self.assertEqual(rejected.exception.code, "AgentOutputMismatch")
        with self.assertRaises(AgentClientError) as rejected:
            self.client.present_query("grove.effect_evidence", error)
        self.assertEqual(rejected.exception.code, "AgentOutputMismatch")
        self.assertEqual(self.client.observations.sequence, 0)

    def test_sdk_and_connector_tools_share_the_same_error_aware_schema(self):
        expected = mcp_tool_output_schema()
        for tool in self.client.tools():
            self.assertEqual(tool["outputSchema"], expected)
        tools = {row["name"]: row for row in self.session.tools()}
        for name in ("world_bootstrap", "execution_checkpoint", "grove_effect_evidence"):
            self.assertEqual(tools[name]["outputSchema"], expected)
        # Tool consumers cannot mutate the strict SDK validator or later lists.
        tools["grove_effect_evidence"]["outputSchema"]["anyOf"][0]["required"].clear()
        self.assertEqual(self.client.tools()[0]["outputSchema"], expected)
        self.assertEqual(observation_output_schema()["required"], ["observation_id", "agent_observation"])

    async def test_connector_errors_survive_declared_native_output_validation(self):
        app = create_connector_app(storage=Path("unused-mcp-schema-fixture"), wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        schema = next(row for row in self.session.tools() if row["name"] == "grove_effect_evidence")["outputSchema"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as caller:
            for failure in (ConnectorError("RuleViolation", "Unknown registered effect fact.", 409),
                            ConnectorError("InvalidArguments", "Predicate operator is invalid.", 422),
                            httpx.ConnectError("fixture disconnect")):
                with self.subTest(error=type(failure).__name__):
                    self.session.tool_call = AsyncMock(side_effect=failure)
                    response = await caller.post("/connector/mcp", json={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "grove_effect_evidence", "arguments": {
                            "entity_id": "object", "definition_ref": "immutable-definition"}}},
                        headers={"Authorization": "Bearer test-capability"})
                    self.assertEqual(response.status_code, 200)
                    result = response.json()["result"]
                    error = result["structuredContent"]
                    self.assertTrue(result["isError"])
                    self.assertTrue(Draft202012Validator(schema).is_valid(error))
                    self.assertFalse(Draft202012Validator(observation_output_schema()).is_valid(error))
                    self.assertEqual(error["error"], failure.code if isinstance(failure, ConnectorError) else "UnknownWorldResult")
                    self.assertEqual(json.loads(result["content"][0]["text"]), error)
                    self.assertNotIn("observation_id", error)
                    self.assertNotIn("agent_observation", error)
                    self.assertEqual(self.client.observations.sequence, 0)
