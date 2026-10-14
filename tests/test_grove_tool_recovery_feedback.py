"""Repair feedback names the real input contract without acting for the model."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from agent_world.grove_connector import (
    ConnectorError, ConnectorManager, EXECUTION_INSTRUCTIONS, GroveSession, create_connector_app,
)
from agent_world.worlds.shared_grove.world import make_world


class GroveToolRecoveryFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-tool-feedback-")
        self.addCleanup(temporary.cleanup)
        self.manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=Path(temporary.name), binary=Path("unused-opencode"), wrapper=None)
        self.session = GroveSession(self.manager, {"role_id": "person", "access_mode": "control"},
            "fixture-bearer", "User", "provider/model")
        self.definition = make_world()
        self.session.status = "thinking"
        self.session.session_id = "fixture-native-session"
        self.session.execution_controller.active = True
        self.session.execution_controller.delegation = "fixture-delegation"
        self.session.execution_controller.state.update(task_id="task", segment_id="segment", status="running",
            request_status="running", requests_used=1, max_actions=8, delegation_active=True,
            host_request={"purpose": "execution", "status": "running", "generation": 1})
        # Input-repair feedback is exercised inside an already authorized
        # current Native request; an active old controller alone grants nothing.
        self.session.active_native_request = {"purpose": "execution", "world_write_authorized": True,
            "session_id": self.session.session_id, "request_id": "grove-request-" + "a" * 32,
            "task_id": "task", "segment_id": "segment"}
        self.session.invoke = AsyncMock()
        self.session.execution_controller.write = AsyncMock()

    async def asyncSetUp(self):
        descriptors = [{"function_id": spec.name, "access": spec.access, "version": spec.version,
            "input_schema": deepcopy(spec.input_schema), "output_schema": deepcopy(spec.output_schema),
            "description": spec.description} for spec in self.definition.functions]
        contract_spec = next(spec for spec in self.definition.functions if spec.name == "grove.agent_contract")
        bootstrap = {"universe": "fixture-feedback-world", "role_id": self.session.role_id,
            "world": {"id": self.definition.world_id, "version": self.definition.version},
            "world_entry_state": {"view": {"player": None,
                "agent_contract_request": {"function_id": contract_spec.name, "arguments": {}}}}}

        async def world_response(method, path, body=None):
            if method == "GET" and path == "/v1/bootstrap":
                return deepcopy(bootstrap)
            if method == "GET" and path.startswith("/v1/discover?"):
                return {"functions": deepcopy(descriptors), "has_more": False, "next_cursor": None}
            if method == "POST" and path == "/v1/functions/grove.agent_contract/invoke":
                self.assertEqual(body, {"arguments": {}})
                return {"ok": True, "read_only": True, "function_id": contract_spec.name,
                    "function_version": contract_spec.version, "snapshot_seq": 0,
                    "result": contract_spec.handler(None, {}).result}
            raise AssertionError(f"Unexpected fixture world request: {method} {path}")

        self.session.world = AsyncMock(side_effect=world_response)
        await self.session.agent_client.discover()
        self.session.catalog = deepcopy(self.session.agent_client.catalog)
        self.session._agent_contract_discovered = True
        self.observation = self.session.present_agent_result("map.viewport", {
            "player": {"id": "person", "version": 7, "x": 0, "y": 0}, "entities": []},
            preflight=True)["observation_id"]

    def protected(self):
        return {"execution": deepcopy(self.session.execution_controller.state),
            "delegation": deepcopy(self.session.execution_controller.delegation),
            "unknown": deepcopy(self.session.unknown), "sequence": self.session.observations.sequence,
            "observed": self.session.agent_observation(self.observation)}

    async def rpc(self, name, arguments):
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as client:
            response = await client.post("/connector/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                headers={"Authorization": "Bearer test-capability"})
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result

    async def test_read_base_on_write_is_not_silently_converted_or_treated_as_an_expired_id(self):
        before = self.protected()
        result = await self.rpc("grove_move", {"dx": 1, "dy": 0, "execution_segment_id": "segment",
            "base_observation_id": self.observation})
        self.assertTrue(result["isError"])
        error = result["structuredContent"]
        self.assertEqual(error["error"], "ObservationRequired")
        self.assertIn("缺少有效observation_id", error["message"])
        self.assertIn("base_observation_id只用于", error["message"])
        self.assertNotIn("未知", error["message"])
        self.assertNotIn("observation_id", error)
        self.assertNotIn(self.observation, json.dumps(error))
        self.assertEqual(self.protected(), before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_write_with_both_fields_rejects_the_read_only_extra_without_changing_intent(self):
        arguments = {"dx": 1, "dy": 0, "execution_segment_id": "segment",
            "observation_id": self.observation, "base_observation_id": self.observation}
        original, before = deepcopy(arguments), self.protected()
        result = await self.rpc("grove_move", arguments)
        self.assertEqual(result["structuredContent"]["error"], "InvalidToolArguments")
        self.assertIn("不接受base_observation_id", result["structuredContent"]["message"])
        self.assertEqual(arguments, original)
        self.assertEqual(self.protected(), before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_missing_nested_source_field_names_its_real_path_without_echoing_values(self):
        before = self.protected()
        result = await self.rpc("grove_fabricate", {"definition_ref": "own-definition", "type_id": "body",
            "placement": {"kind": "inventory"}, "sources": [{"entity_id": "private-source-marker", "lot_id": "lot"}],
            "execution_segment_id": "segment", "observation_id": self.observation})
        error = result["structuredContent"]
        self.assertEqual(error["error"], "InvalidToolArguments")
        self.assertIn("sources[0].quantity_u", error["message"])
        self.assertNotIn("private-source-marker", json.dumps(error))
        self.assertEqual(self.protected(), before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_valid_write_uses_its_selected_observation_even_after_another_read(self):
        original = self.session.agent_observation(self.observation)
        self.session.present_agent_result("map.viewport", {"player": {"id": "person", "version": 9}, "entities": []})
        committed_arguments = {"dx": 1, "dy": 0, "expected_player_version": 7,
                               "delegation": self.session.delegation}
        canonical = json.dumps({"function_id": "grove.move", "arguments": committed_arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.session.execution_controller.write.return_value = {"ok": True, "operation_id": "original-operation",
            "function_id": "grove.move", "function_version": self.session.catalog["grove.move"]["version"],
            "actor_role_id": self.session.role_id, "credential_id": "fixture-credential", "commit_seq": 5,
            "intent_fingerprint": {"algorithm": "sha256-runtime-canonical-json-v1",
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()},
            "result": {"player": {"id": "person", "version": 8}}}
        result = await self.rpc("grove_move", {"dx": 1, "dy": 0, "execution_segment_id": "segment",
            "observation_id": self.observation, "expected_player_version": 999})
        self.assertFalse(result["isError"])
        self.session.execution_controller.write.assert_awaited_once_with("grove.move",
            {"dx": 1, "dy": 0, "expected_player_version": 7}, segment_id="segment")
        self.assertEqual(self.session.agent_observation(self.observation), original)
        self.session.invoke.assert_not_awaited()

    async def test_unknown_explicit_observation_still_requires_a_real_read(self):
        before = self.protected()
        result = await self.rpc("grove_move", {"dx": 1, "dy": 0, "execution_segment_id": "segment",
            "observation_id": "unknown-observation"})
        error = result["structuredContent"]
        self.assertEqual(error["error"], "ObservationRequired")
        self.assertIn("未知", error["message"])
        self.assertEqual(error["read_requests"][0], {"function_id": "map.viewport", "tool": "map_viewport",
            "native_tool": "grove_world_map_viewport", "arguments": {}})
        self.assertNotIn("observation_id", error)
        self.assertEqual(self.protected(), before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    def test_formal_schemas_and_execution_guidance_distinguish_returned_id_from_read_parameter(self):
        tools = {tool["name"]: tool for tool in self.session.tools()}
        write = tools["grove_move"]["inputSchema"]
        read = tools["map_viewport"]["inputSchema"]
        self.assertIn("observation_id", write["required"])
        self.assertNotIn("base_observation_id", write["properties"])
        self.assertIn("reads only", write["properties"]["observation_id"]["description"])
        self.assertIn("returned observation_id", read["properties"]["base_observation_id"]["description"])
        self.assertNotIn("observation_id", read["properties"])
        self.assertIn("Reads return observation_id", EXECUTION_INSTRUCTIONS)
        self.assertIn("base_observation_id is read-only", EXECUTION_INSTRUCTIONS)

    async def test_definition_rejections_offer_formal_syntax_and_validation_without_a_repaired_draft(self):
        self.session.tool_call = AsyncMock()
        arguments = {"definition": {"private_draft_marker": "never echo my draft"},
            "observation_id": self.observation, "execution_segment_id": "segment"}
        before = self.protected()
        for code in ("InvalidToolArguments", "InvalidArguments", "SchemaRejected", "RuleViolation", "StateConflict"):
            with self.subTest(code=code):
                self.session.tool_call.side_effect = ConnectorError(code, "Actual rejection reason", 422,
                    execution_outcome={"stage": "definite_rejection", "operation_id": "original-publication",
                        "function_id": "grove.definition_publish", "world_action_committed": False})
                result = await self.rpc("grove_definition_publish", arguments)
                error = result["structuredContent"]
                self.assertTrue(result["isError"])
                self.assertEqual(error["message"], "Actual rejection reason")
                self.assertEqual(error["read_requests"], [{"function_id": "grove.rule_catalog", "tool": "grove_rule_catalog",
                    "native_tool": "grove_world_grove_rule_catalog", "arguments": {"topic": "program"}}])
                self.assertEqual(error["validation_hint"]["tool"], "grove_definition_validate")
                self.assertNotIn("arguments", error["validation_hint"])
                self.assertIn("resources", error["recovery_meaning"])
                self.assertNotIn("private_draft_marker", json.dumps(error))
                self.assertNotIn("never echo my draft", json.dumps(error))
                self.assertNotIn("observation_id", error)
                self.assertEqual(error["execution_outcome"]["operation_id"], "original-publication")
                for read in error["read_requests"]:
                    self.assertIn(read["tool"], {tool["name"] for tool in self.session.tools()})
                self.assertEqual(self.protected(), before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_unknown_and_committed_publications_keep_original_recovery_instead_of_draft_repair(self):
        self.session.tool_call = AsyncMock()
        cases = [
            ConnectorError("WorldUnavailable", "Unknown original result", 503,
                recovery="query_receipt_before_retry", execution_outcome={"stage": "unknown_result",
                    "operation_id": "unknown-publication", "function_id": "grove.definition_publish"}),
            ConnectorError("ReadbackPending", "Original publication committed", 409,
                execution_outcome={"stage": "confirmed_feedback_unavailable", "operation_id": "committed-publication",
                    "function_id": "grove.definition_publish", "world_action_committed": True, "receipt_confirmed": True}),
            ConnectorError("RuleViolation", "Committed result must be kept", 409,
                execution_outcome={"stage": "committed_receipt_feedback_pending", "operation_id": "committed-with-error",
                    "function_id": "grove.definition_publish", "world_action_committed": True}),
        ]
        before = self.protected()
        for error in cases:
            with self.subTest(code=error.code):
                self.session.tool_call.side_effect = error
                result = await self.rpc("grove_definition_publish", {"definition": {"keep": "original"}})
                delivered = result["structuredContent"]
                self.assertEqual(delivered["execution_outcome"], error.public_payload()["execution_outcome"])
                self.assertNotIn("validation_hint", delivered)
                self.assertNotIn("grove_rule_catalog", json.dumps(delivered.get("read_requests")))
                if error.recovery:
                    self.assertEqual(delivered["recovery"], error.recovery)
                self.assertEqual(self.protected(), before)

    async def test_unparsed_native_wrapper_is_not_misdiagnosed_as_an_effect_value_or_executed(self):
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("InvalidArguments", "Actual top-level schema rejection", 422))
        arguments = {"value": '{"definition":{"key":"private-source","types":[}}'}
        original, protected = deepcopy(arguments), self.protected()
        error = (await self.rpc("grove_definition_validate", arguments))["structuredContent"]
        diagnostic = error["argument_diagnostic"]
        self.assertEqual(diagnostic["submitted_shape"], "top_level_value_string")
        self.assertEqual(diagnostic["json_parse"], "rejected")
        self.assertGreater(diagnostic["line"], 0)
        self.assertGreater(diagnostic["column"], 0)
        self.assertIn("not an effect's value field", diagnostic["meaning"])
        self.assertNotIn("private-source", json.dumps(error))
        self.assertEqual(arguments, original)
        self.assertEqual(self.protected(), protected)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_decodable_argument_string_still_is_not_converted_into_a_definition(self):
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("InvalidArguments", "Actual top-level schema rejection", 422))
        arguments = {"value": json.dumps({"definition": {"key": "do-not-invoke-this"}})}
        protected = self.protected()
        error = (await self.rpc("grove_definition_validate", arguments))["structuredContent"]
        self.assertEqual(error["argument_diagnostic"]["json_parse"], "decoder_accepted_only")
        self.assertNotIn("do-not-invoke-this", json.dumps(error))
        self.assertEqual(self.protected(), protected)
        self.session.invoke.assert_not_awaited()
        self.session.execution_controller.write.assert_not_awaited()

    async def test_ordinary_domain_busy_rejection_keeps_existing_same_operation_recovery(self):
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("DomainBusy", "Actual temporary settlement", 409,
            retryable=True, recovery="wait_then_retry_same_operation", execution_outcome={"stage": "definite_rejection",
                "operation_id": "same-operation", "function_id": "grove.move", "world_action_committed": False}))
        result = await self.rpc("grove_move", {"dx": 1, "dy": 0})
        error = result["structuredContent"]
        self.assertEqual(error["execution_outcome"]["operation_id"], "same-operation")
        self.assertEqual(error["recovery"], "wait_then_retry_same_operation")
        self.assertIn("before commit", error["recovery_meaning"])
        self.assertNotIn("validation_hint", error)
        self.assertEqual(error["read_requests"][0], {"function_id": "map.viewport", "tool": "map_viewport",
            "native_tool": "grove_world_map_viewport", "arguments": {}})


if __name__ == "__main__":
    unittest.main()
