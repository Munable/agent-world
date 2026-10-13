"""Local companion gates/recovery, without invoking a real model in unit tests."""
from pathlib import Path
from copy import deepcopy
import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.testclient import TestClient

from agent_world.grove_connector import (
    ConnectorError, ConnectorManager, GroveSession, create_connector_app, session_permissions_match,
    NATIVE_HISTORY_INDEX_PREFIX, NATIVE_CONTEXT_VERSION,
)
from agent_world.grove_agent_observation import (
    companion_projection, _companion_chunk_terrain, _companion_scene_index, blueprint_model_input)
from agent_world.worlds.shared_grove.catalog import CONTENT_SCHEMA
from agent_world.worlds.shared_grove.world import INTERACT_ARGS


class GroveConnectorTests(unittest.IsolatedAsyncioTestCase):
    def test_raw_catalog_without_agent_contract_exposes_no_world_actions(self):
        self.session.catalog = {"grove.move": {"access": "write", "input_schema": {}}}
        self.assertNotIn("grove_move", [tool["name"] for tool in self.session.tools()])

    async def test_action_targets_are_exact_separate_reads_and_do_not_replace_selected_contract(self):
        self.session.entered = True
        self.declare_agent_catalog({name: {"access": "read"} for name in ("map.viewport", "grove.inspect")})
        self.session.world = AsyncMock(return_value={"events": []})
        calls = []
        async def invoke(function, arguments):
            calls.append((function, deepcopy(arguments)))
            if function == "map.viewport":
                return {"result": {"player": {"id": "test-user", "x": 2, "y": 3},
                    "entities": [], "inventory": [], "clock": {"sequence": 90}}}
            identity = arguments["entity_id"]
            if identity == "consumed-material":
                raise ConnectorError("ResourceNotFound", "Original consumed lot", 404)
            entity = {"id": identity, "version": 7, "execution_domain": {"state": "settled", "last_applied_tick": 90}}
            if identity == "wheel":
                entity["creative"] = {"world_tick": 91, "fields": {"signal": 0}, "mechanical_observation": {
                    "stored_state": {"energy": 19, "angle": 3}, "stored_output_is_history": True, "observed_tick": 91}}
            return {"result": {"entity": entity, "action_options": []}}
        self.session.invoke = invoke
        facts = await self.session.current_turn_facts(context_entity_id="original-contract", optional_selected=True,
            action_entity_ids=["wheel", "consumed-material", "original-contract", "wheel"])
        self.assertEqual([arguments.get("entity_id") for function, arguments in calls if function == "grove.inspect"],
            ["original-contract", "wheel", "consumed-material"])
        self.assertEqual(facts["selected_inspection"]["entity"]["id"], "original-contract")
        targets = facts["action_targets"]
        self.assertEqual([row["entity_id"] for row in targets], ["wheel", "consumed-material", "original-contract"])
        self.assertEqual(targets[0]["inspection"]["entity"]["creative"]["mechanical_observation"]["stored_state"]["energy"], 19)
        self.assertEqual(targets[0]["inspection"]["entity"]["execution_domain"]["last_applied_tick"], 90)
        self.assertEqual(targets[1]["unavailable"]["code"], "ResourceNotFound")
        self.assertNotIn("inspection", targets[1])
        self.assertTrue(all(isinstance(row["observed_at"], float) for row in targets))

    async def test_execution_reply_has_original_request_identity_and_reclaim_only_invalidates_current_qualification(self):
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "test-user"}, "entities": []})
        self.session.execution_controller.state.update(task_id="task-original", segment_id="segment-original",
            status="running", request_status="idle_confirmed", requests_used=0, max_requests=2)
        self.session.host = AsyncMock(return_value={"info": {"id": "msg_original", "role": "assistant", "finish": "stop",
            "time": {"completed": 1}}, "parts": [{"type": "text", "text": "The model says the device is running."}]})
        await self.session.turn("Observe current state", executing=True)
        message = deepcopy(self.session.messages[-1])
        self.assertEqual(message["task_id"], "task-original")
        self.assertEqual(message["request_number"], 1)
        self.assertEqual(message["response_message_id"], "msg_original")
        self.assertEqual(message["session_id"], "test-session")
        self.assertTrue(self.session.state()["completed"])
        self.session._close_reclaim_admission()
        self.session.status = "ready"  # A recovered transport cannot revive the old reply.
        self.assertFalse(self.session.state()["completed"])
        self.assertTrue(self.session.response_diagnostics["validated_final"])
        self.assertEqual(self.session.messages[-1], message)
        self.assertEqual(self.session.last_completion["message_id"], "msg_original")

    def test_scene_summary_preserves_current_fields_and_distinct_recorded_consequence(self):
        change = {"step": 9, "fields": {"signal": {"before": 1, "after": 0}}}
        creative = {"definition_ref": "public-definition", "type_id": "sensor",
            "fields": {"signal": 0}, "world_tick": 12, "automatic_field_change": change,
            "automatic_field_change_status": {"available": True, "meaning": "A recorded net change, not complete history"}}
        entity = {"id": "public-object", "version": 4, "state": {"wetness": 0}, "creative": creative}
        scene = {"clock": {"sequence": 12}, "entities": [entity, {"id": "closed-object",
            "creative": {**creative, "hidden": True}}]}
        projected = _companion_scene_index(scene)
        observed = projected["entities"][0]
        self.assertEqual(observed["discovery_state"]["wetness"], 0)
        self.assertEqual(observed["creative"]["fields"], {"signal": 0})
        self.assertEqual(observed["creative"]["world_tick"], 12)
        self.assertEqual(observed["creative"]["automatic_field_change"], change)
        self.assertEqual(_companion_scene_index(projected)["entities"], projected["entities"])
        hidden = projected["entities"][1]["creative"]
        self.assertEqual(hidden, {"definition_ref": "public-definition", "type_id": "sensor", "hidden": True})
        creative["fields"]["signal"] = 7
        self.assertEqual(observed["creative"]["fields"], {"signal": 0})

    def test_agent_terrain_preserves_negative_coordinates_actual_cells_and_unknowns(self):
        tiles=['grass','dirt','water','rock']*64
        chunks=[{"cx":x,"cy":y,"tiles":tiles} for x,y in [(-1,-1),(0,-1),(-1,0),(0,0)]]
        scene=_companion_scene_index({"chunks":chunks,"construction":{"recipes":[
            {"id":"hearth","terrain":["grass","dirt"],"material_cost_u":{"stone":12}}]}})
        self.assertEqual(scene['construction']['recipes'][0]['material_cost_u'],{'stone':12})
        for original,projected in zip(chunks,scene['chunks']):
            encoded=projected['terrain']
            self.assertEqual(encoded['origin'],{'x':original['cx']*16,'y':original['cy']*16})
            decoded=[encoded['legend'][code] for row in encoded['rows'] for code in row]
            self.assertEqual(decoded,tiles)
            self.assertTrue(encoded['complete'])
            self.assertEqual(_companion_chunk_terrain(projected),projected)
        incomplete=_companion_chunk_terrain({'cx':-1,'cy':0,'tiles':['water','unknown']})['terrain']
        self.assertFalse(incomplete['complete'])
        self.assertEqual(incomplete['unknown_indexes'],[1])
        self.assertEqual(incomplete['missing_index_range'],[2,255])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-connector-")
        self.addCleanup(temporary.cleanup)
        self.manager = ConnectorManager(world_url="http://127.0.0.1:8890",
            local_url="http://127.0.0.1:8891", storage=Path(temporary.name),
            binary=Path("unused-opencode"), wrapper=None)
        self.session = GroveSession(self.manager, {"role_id": "test-user", "access_mode": "control"},
                                    "test-private-bearer", "Test", "provider/model")
        self.session.host_url = "http://127.0.0.1:9000"
        self.session.session_id = "test-session"

    def simulated_native_context_loaded(self):
        """Only the loading boundary is doubled; preserve the recovery tests."""
        plugin = self.session.configure_native_context()
        options = plugin[1]
        Path(options["diagnostic_path"]).write_text(json.dumps({"status": "loaded", "version": NATIVE_CONTEXT_VERSION,
            "hook": "experimental.chat.messages.transform", "nonce": options["nonce"],
            "role_id": self.session.role_id, "source_sha256": options["source_sha256"],
            "process_id": 42, "loaded_at_ms": 1, "sdk_guard": {
                "status": "installed", "runtime": "ai_sdk", "provider_id": "provider", "model_id": "model",
                "historical_message_budget_bytes": 65536, "request_budget_bytes": 131072, "native_llm_allowed": False}}))
        return plugin

    def declare_agent_catalog(self, catalog):
        """Declare the fake world's explicit Agent contract alongside its schemas."""
        self.session.catalog = catalog
        functions = [{"function_id": name, **spec} for name, spec in catalog.items()]
        contract = {"contract_version": 1, "world_id": "shared-grove", "functions": [
            {"function_id": name, "access": spec["access"]} for name, spec in catalog.items()]}
        self.session.agent_client.configure_catalog(functions, contract, activate=True)

    @staticmethod
    def authenticated_bootstrap():
        return {"protocol_version": "0.15", "role_id": "test-user", "universe": "fixture-universe",
            "world": {"id": "shared-grove", "version": 3},
            "world_entry_state": {"view": {"player": None}}}

    @staticmethod
    def original_receipt(function, envelope, result):
        canonical = json.dumps({"function_id": function, "arguments": envelope["arguments"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"ok": True, "operation_id": envelope["operation_id"], "function_id": function,
            "function_version": 1, "actor_role_id": "test-user", "commit_seq": 1, "result": result,
            "intent_fingerprint": {"algorithm": "sha256-runtime-canonical-json-v1",
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}}

    def verified_query_catalog(self):
        context = {"type": "object", "properties": {
            "world_instance": {"type": "string"}, "world_id": {"const": "shared-grove"},
            "world_version": {"type": "integer"}, "viewer_role_id": {"type": "string"},
            "snapshot_seq": {"type": "integer"}},
            "required": ["world_instance", "world_id", "world_version", "viewer_role_id", "snapshot_seq"],
            "additionalProperties": False}
        output = {"type": "object", "properties": {
            "player": {"type": "object", "properties": {"version": {"type": "integer"}}, "required": ["version"]},
            "agent_context": context, "read_requests": {"type": "array"}},
            "required": ["player", "agent_context", "read_requests"]}
        self.session.agent_client._check_bootstrap_scope({"universe": "fixture-universe", "role_id": "test-user",
            "world": {"id": "shared-grove", "version": 3}}, rediscover=True)
        self.declare_agent_catalog({
            "map.viewport": {"access": "read", "version": 7, "input_schema": {"type": "object"}, "output_schema": output},
            "grove.inspect": {"access": "read", "version": 7, "input_schema": {"type": "object"}, "output_schema": output}})

    @staticmethod
    def verified_query(function="grove.inspect", snapshot=11):
        return {"ok": True, "read_only": True, "function_id": function, "function_version": 7,
            "snapshot_seq": snapshot, "result": {"player": {"id": "test-user", "version": 4, "x": 0, "y": 0},
                "entity": {"id": "object", "version": 2}, "entities": [], "inventory": [], "read_requests": [],
                "agent_context": {"world_instance": "fixture-universe", "world_id": "shared-grove", "world_version": 3,
                    "viewer_role_id": "test-user", "snapshot_seq": snapshot}}}

    async def test_native_world_read_validates_original_query_before_fixed_observation(self):
        self.verified_query_catalog()
        self.session.status = "thinking"
        self.session.active_native_request = {"purpose": "explain", "world_write_authorized": False,
                                              "session_id": self.session.session_id}
        self.session.world = AsyncMock(return_value=self.verified_query())
        result = await self.session.tool_call("grove_inspect", {"entity_id": "object"})
        self.assertEqual(result["function_version"], 7)
        observation = result["observation_id"]
        original = deepcopy(self.session.agent_observation(observation))
        self.session.world.assert_awaited_once_with("POST", "/v1/functions/grove.inspect/invoke",
            {"arguments": {"entity_id": "object"}, "expected_version": 7})
        self.assertEqual(self.session.world_call_count, 1)
        self.assertEqual(self.session.execution["actions_count"], 0)
        self.assertEqual(self.session.observations.sequence, 1)
        self.assertEqual(original["versions"]["entity"]["object"], 2)

        corruptions = [
            ("role", lambda row: row["result"]["agent_context"].update(viewer_role_id="other"), "IdentityScopeMismatch"),
            ("universe", lambda row: row["result"]["agent_context"].update(world_instance="other"), "IdentityScopeMismatch"),
            ("snapshot", lambda row: row["result"]["agent_context"].update(snapshot_seq=12), "IdentityScopeMismatch"),
            ("world code", lambda row: row["result"]["agent_context"].update(world_id="other-code"), "WorldVersionMismatch"),
            ("world version", lambda row: row["result"]["agent_context"].update(world_version=4), "WorldVersionMismatch"),
            ("function version", lambda row: row.update(function_version=8), "AgentOutputMismatch"),
            ("typed output", lambda row: row["result"]["player"].update(version="forged"), "AgentOutputMismatch"),
            ("old watermark", lambda row: (row.update(snapshot_seq=10), row["result"]["agent_context"].update(snapshot_seq=10)),
                "StaleObservation"),
        ]
        for label, mutate, code in corruptions:
            with self.subTest(label=label):
                forged = self.verified_query()
                forged["result"]["entity"]["version"] = 999
                mutate(forged)
                self.session.world.return_value = forged
                with self.assertRaises(ConnectorError) as denied:
                    await self.session.tool_call("grove_inspect", {"entity_id": "object", "base_observation_id": observation})
                self.assertEqual(denied.exception.code, code)
                self.assertEqual(self.session.observations.sequence, 1)
                self.assertEqual(self.session.agent_observation(observation), original)
                self.assertEqual(self.session.unknown, [])
                self.assertEqual(self.session.execution["actions_count"], 0)

    async def test_preflight_queries_use_same_validation_before_composite_facts_or_model(self):
        self.verified_query_catalog()
        self.session.entered = True
        viewport = self.verified_query("map.viewport", snapshot=10)
        forged_inspection = self.verified_query(snapshot=11)
        forged_inspection["result"]["agent_context"]["viewer_role_id"] = "other"
        self.session.world = AsyncMock(side_effect=[{"events": []}, viewport, forged_inspection])
        with self.assertRaises(ConnectorError) as denied:
            await self.session.current_turn_facts(context_entity_id="object", preflight=True)
        self.assertEqual(denied.exception.code, "IdentityScopeMismatch")
        self.assertEqual(self.session.observations.sequence, 0)
        self.assertEqual(self.session.inference_count, 0)
        self.assertEqual(self.session.unknown, [])

    async def test_bootstrap_special_contract_checks_identity_without_a_query_envelope(self):
        self.verified_query_catalog()
        self.session.status = "thinking"
        bootstrap = {"universe": "fixture-universe", "role_id": "other",
            "world": {"id": "shared-grove", "version": 3}, "world_entry_state": {"view": {"player": None}}}
        self.session.world = AsyncMock(return_value=bootstrap)
        with self.assertRaises(ConnectorError) as denied:
            await self.session.tool_call("world_bootstrap", {})
        self.assertEqual(denied.exception.code, "IdentityScopeMismatch")
        self.assertEqual(self.session.observations.sequence, 0)
        bootstrap["role_id"] = "test-user"
        result = await self.session.tool_call("world_bootstrap", {})
        self.assertEqual(result["agent_observation"]["source_kind"], "bootstrap")
        self.assertEqual(self.session.observations.sequence, 1)

    async def test_host_catalog_sync_cannot_reactivate_invalidated_world_code(self):
        self.verified_query_catalog()
        self.session.status = "thinking"
        changed = self.verified_query()
        changed["result"]["agent_context"]["world_version"] = 4
        self.session.world = AsyncMock(return_value=changed)
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.tool_call("grove_inspect", {"entity_id": "object"})
        self.assertEqual(rejected.exception.code, "WorldVersionMismatch")
        self.assertFalse(self.session.agent_client._catalog_current)
        self.session.tools()
        self.session._sync_agent_client_catalog()
        self.assertFalse(self.session.agent_client._catalog_current)
        with self.assertRaises(ConnectorError) as closed:
            self.session.bind_agent_versions("grove.interact", {}, "missing")
        self.assertEqual(closed.exception.code, "AgentDiscoveryRequired")
        self.declare_agent_catalog(self.session.catalog)  # Explicit new installation is allowed.
        self.assertTrue(self.session.agent_client._catalog_current)

    async def test_original_host_operation_cannot_lookup_or_replay_in_another_universe_at_same_url(self):
        self.verified_query_catalog()
        record = self.session.agent_client.operation_record("grove.control_revoke",
            {"operation_id": "original-control", "arguments": {}})
        self.session.unknown = [deepcopy(record)]
        self.session.world = AsyncMock(return_value={"role_id": "test-user", "universe": "replacement-universe",
            "world": {"id": "shared-grove", "version": 3}})
        with self.assertRaises(ConnectorError) as refused:
            await self.session.recover_receipts(replay=True)
        self.assertEqual(refused.exception.code, "IdentityScopeMismatch")
        self.session.world.assert_awaited_once_with("GET", "/v1/bootstrap")
        self.assertEqual(self.session.unknown, [record])
        self.assertEqual(self.session.observations.sequence, 0)
        self.session.unknown[0].pop("universe")  # Legacy scope must never be guessed.
        self.session.world.reset_mock()
        with self.assertRaises(ConnectorError) as legacy:
            await self.session.recover_receipts(replay=True)
        self.assertEqual(legacy.exception.code, "OriginalOperationInvalid")
        self.session.world.assert_not_awaited()
        self.assertNotIn("universe", self.session.unknown[0])

    async def test_host_wrong_receipt_keeps_unknown_without_observing_or_replaying(self):
        self.verified_query_catalog()
        record = self.session.agent_client.operation_record("grove.control_revoke",
            {"operation_id": "original-control", "arguments": {}})
        self.session.unknown = [deepcopy(record)]
        bootstrap = {"role_id": "test-user", "universe": "fixture-universe",
            "world": {"id": "shared-grove", "version": 3}}
        receipt = {"ok": True, "operation_id": "another-control", "function_id": "grove.control_revoke",
            "function_version": 1, "actor_role_id": "test-user", "commit_seq": 8,
            "result": {"status": "revoked"}, "intent_fingerprint": self.session.agent_client._intent_fingerprint(
                "grove.control_revoke", {})}
        self.session.world = AsyncMock(side_effect=[bootstrap, receipt])
        self.session.observe_outcome = Mock()
        self.session.execution_controller.stopped = True  # Stop does not block the original GET.
        with self.assertRaises(ConnectorError) as refused:
            await self.session.recover_receipts(replay=True)
        self.assertEqual(refused.exception.code, "ReceiptMismatch")
        self.assertEqual(self.session.unknown, [record])
        self.session.observe_outcome.assert_not_called()
        self.assertEqual([call.args[:2] for call in self.session.world.await_args_list],
            [("GET", "/v1/bootstrap"), ("GET", "/v1/receipts/original-control")])
        self.assertEqual(self.session.observations.sequence, 0)

    async def test_missing_transfer_inputs_report_parameters_without_trying_a_write(self):
        self.session.status = "thinking"
        self.session.execution_controller.active = True
        self.session.execution_controller.delegation = {"fixture": True}
        self.session.execution_controller.state.update(task_id="task", segment_id="segment", status="running", delegation_active=True)
        self.session.active_native_request = {"purpose": "execution", "world_write_authorized": True,
            "session_id": self.session.session_id, "task_id": "task", "segment_id": "segment"}
        self.declare_agent_catalog({"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}})
        self.session.invoke = AsyncMock()
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.tool_call("grove_interact", {"action": "transfer", "execution_segment_id": "segment"})
        self.assertEqual(rejected.exception.code, "InvalidToolArguments")
        self.assertIn("target_id", str(rejected.exception))
        self.session.invoke.assert_not_awaited()

    async def test_paused_execution_error_reaches_frontend_without_replacing_session(self):
        error = {"code": "IncompleteResponse", "message": "模型服务返回错误（HTTP 400）。"}
        async def paused(*args, **kwargs):
            self.session.execution_controller.state.update(status="paused", error=error, request_status="idle_confirmed")
        self.session.execution_controller.execute_goal = AsyncMock(side_effect=paused)
        self.session.release_opportunity = AsyncMock()
        await self.session.execute_goal("继续原目标")
        self.assertEqual(self.session.state()["error"], error)
        self.assertEqual(self.session.status, "paused")
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.execution["request_status"], "idle_confirmed")

    def test_action_menu_and_inventory_publish_same_observation_and_segment_contract(self):
        from agent_world.grove_agent_observation import agent_mcp_schema
        self.declare_agent_catalog({"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}})
        inventory = next(row for row in self.session.tools() if row["name"] == "grove_interact")["inputSchema"]
        delivered = self.session.present_agent_result("grove.inspect", {"entity": {"id": "cup", "version": 2,
            "action_options": [{"function_id": "grove.interact", "action": "transfer", "input_schema": INTERACT_ARGS}]},
            "action_options_projection": {"scope": "selected_declared_action", "action": "transfer",
                                          "expanded_actions": ["transfer"]}})
        menu = delivered["entity"]["action_options"][0]
        self.assertEqual(menu["mcp_tool"], "grove_interact")
        self.assertEqual(menu["input_schema"]["required"], inventory["required"])
        self.assertEqual(menu["input_schema"]["allOf"], inventory["allOf"])
        self.assertNotIn("expected_player_version", inventory["properties"])
        self.assertIn("observation_id", menu["input_schema"]["required"])
        self.assertIn("execution_segment_id", menu["input_schema"]["required"])

    async def test_completed_prose_without_actual_world_read_cannot_enter(self):
        self.session.host = AsyncMock(return_value={"info": {"role": "assistant", "time": {"completed": 1}},
                                                    "parts": [{"type": "text", "text": "Hello."}]})
        self.session.invoke = AsyncMock()
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.turn("Hello", initial=True)
        self.assertEqual(rejected.exception.code, "WorldReadRequired")
        self.assertFalse(self.session.entered)
        self.session.invoke.assert_not_awaited()

    async def test_completed_entry_world_timeout_preserves_answer_and_original_receipt(self):
        self.session.world_read = True
        self.declare_agent_catalog({"grove.join": {"access": "write"}})
        self.session.host = AsyncMock(return_value={
            "info": {"role": "assistant", "finish": "stop", "time": {"completed": 1}},
            "parts": [{"type": "text", "text": "先看看溪边能用的材料。"}]})
        async def upstream(method, url, **kwargs):
            if method == "GET" and url.endswith("/v1/bootstrap"):
                return httpx.Response(200, json=self.authenticated_bootstrap())
            self.assertEqual((method, url), ("POST", self.manager.world_url + "/v1/functions/grove.join/invoke"))
            raise httpx.ReadTimeout("private transport details")
        with patch("agent_world.grove_connector.httpx.AsyncClient.request", AsyncMock(side_effect=upstream)):
            with self.assertRaises(ConnectorError) as rejected:
                await self.session.turn("进入世界", initial=True)
        self.assertEqual(rejected.exception.code, "WorldUnavailable")
        self.session.fail(rejected.exception)
        self.assertEqual(self.session.text, "先看看溪边能用的材料。")
        self.assertTrue(self.session.first_response_success)
        self.assertEqual(self.session.inference_count, 1)
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.host.await_count, 1)
        self.assertEqual(self.session.unknown[0]["function_id"], "grove.join")
        envelope = deepcopy(self.session.unknown[0]["envelope"])
        original = envelope["operation_id"]
        self.session.world = AsyncMock(side_effect=[self.authenticated_bootstrap(),
            self.original_receipt("grove.join", envelope, {"joined": True})])
        await self.session.recover_receipts()
        self.assertEqual([call.args for call in self.session.world.await_args_list],
            [("GET", "/v1/bootstrap"), ("GET", "/v1/receipts/" + original)])
        self.assertEqual(self.session.unknown, [])
        self.assertEqual(self.session.inference_count, 1)
        self.assertNotIn("private transport details", json.dumps(self.session.state()))

    async def test_unavailable_model_is_counted_and_keeps_original_history(self):
        original = {"completed": True, "message_id": "original-answer", "session_id": "test-session"}
        self.session.last_completion = original.copy()
        self.session.messages = [{"role": "assistant", "text": "原来已经完成的回答"}]
        response = {"info": {"role": "assistant", "time": {"completed": 1},
                    "error": {"name": "APIError", "data": {
                        "statusCode": 400, "message": "Upstream request failed: Model is unavailable.",
                        "responseBody": "private provider details"}}}, "parts": []}
        entry = self.session.inference_started("explain")
        self.session.inference_finished(entry, response)
        with self.assertRaises(ConnectorError) as rejected:
            self.session.complete_model_answer(response)
        self.assertEqual(rejected.exception.code, "ModelUnavailable")
        self.session.fail(rejected.exception)
        self.assertEqual(self.session.inference_count, 1)
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.last_completion, original)
        self.assertEqual(self.session.messages[0]["text"], "原来已经完成的回答")
        self.assertFalse(self.session.response_diagnostics["validated_final"])
        self.assertNotIn("private provider details", json.dumps(self.session.state()))
        # A different bad request must retain its ordinary error classification.
        response["info"]["error"]["data"]["message"] = "Invalid request payload."
        with self.assertRaises(ConnectorError) as ordinary:
            self.session.complete_model_answer(response)
        self.assertEqual(ordinary.exception.code, "IncompleteResponse")
        self.assertIn("HTTP 400", ordinary.exception.message)
        self.assertNotIn("private provider details", ordinary.exception.message)
        response["info"]["error"]["data"]["message"] = "Upstream request failed: Model is unavailable."
        self.session.current_turn_facts = AsyncMock(return_value={"scene": "current"})
        async def unavailable_native(method, path, body=None, **kwargs):
            if path.endswith('/message'):
                return response
            if path.endswith('/abort'):
                return {}
            if path == '/session/test-session':
                return {'id': 'test-session'}
            if path == '/session/status':
                return {}
            self.fail(path)
        self.session.host = AsyncMock(side_effect=unavailable_native)
        self.assertTrue(await self.session.settle_execution_request(abort=True))
        with self.assertRaises(ConnectorError) as ordinary_turn:
            await self.session.turn("现在还能怎样利用现场这些东西？")
        self.assertEqual(ordinary_turn.exception.code, "ModelUnavailable")
        self.assertEqual(self.session.inference_count, 2)
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.last_completion, original)
        self.assertEqual(sum(call.args[1].endswith('/message') for call in self.session.host.await_args_list), 1)
        self.assertEqual(self.session.execution['request_status'], 'idle_confirmed')

    async def test_internal_history_index_is_not_published_or_retried_and_keeps_native_usage(self):
        body = NATIVE_HISTORY_INDEX_PREFIX + json.dumps({"scope": "historical_tool_location_index",
            "historical_only": True, "session_id": "test-session", "omitted_tool_parts": 208})
        response = {"info": {"id": "msg_artifact", "role": "assistant", "finish": "stop",
            "time": {"completed": 1}, "tokens": {"input": 8, "output": 508,
                "cache": {"read": 73741, "write": 472}}, "cost": 0.001514216},
            "parts": [{"type": "text", "text": body}]}
        original_response = deepcopy(response)
        original_history = {"role": "assistant", "text": "原来已确认的回复与引用"}
        original_completion = {"completed": True, "message_id": "msg_original", "session_id": "test-session"}
        self.session.messages = [deepcopy(original_history)]
        self.session.last_completion = deepcopy(original_completion)
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "test-user"}, "entities": []})
        self.session.execution_controller.state.update(task_id="original-task", segment_id="original-segment",
            status="running", request_status="idle_confirmed", requests_used=0, max_requests=2)
        self.session.host = AsyncMock(return_value=response)
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.turn("继续原目标", executing=True)
        self.assertEqual(rejected.exception.code, "InternalContextResponse")
        self.assertIn("未自动重试", rejected.exception.message)
        self.session.fail(rejected.exception)
        self.assertEqual(response, original_response)
        self.session.host.assert_awaited_once()
        self.assertEqual(self.session.inference_count, 1)
        self.assertEqual(self.session.execution["requests_used"], 1)
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.messages[0], original_history)
        self.assertFalse(any(message.get("text") == body for message in self.session.messages))
        self.assertEqual(self.session.last_completion, original_completion)
        self.assertEqual(self.session.text, "")
        self.assertEqual(self.session.completion_count, 0)
        self.assertTrue(self.session.response_diagnostics["completed"])
        self.assertFalse(self.session.response_diagnostics["validated_final"])
        self.assertEqual(self.session.response_diagnostics["final_rejection"], "internal_history_location_index")
        self.assertEqual(self.session.response_diagnostics["usage"]["cache"], {"read": 73741, "write": 472})
        self.assertEqual(self.session.response_diagnostics["cost"], 0.001514216)

    def test_final_answer_guard_matches_only_the_whole_managed_history_envelope(self):
        envelope = json.dumps({"scope": "historical_tool_location_index", "historical_only": True})
        body = NATIVE_HISTORY_INDEX_PREFIX + envelope
        for ordinary in ("我已经暂停装置；历史索引仅是来源入口。", "这是历史索引示例：\n" + body,
                         body + "\n这不是我的最终行动结论。", "```json\n" + body + "\n```",
                         NATIVE_HISTORY_INDEX_PREFIX + "{broken JSON",
                         NATIVE_HISTORY_INDEX_PREFIX + "[]",
                         NATIVE_HISTORY_INDEX_PREFIX + '{"scope":"another_scope","historical_only":true}',
                         NATIVE_HISTORY_INDEX_PREFIX + '{"scope":"historical_tool_location_index","historical_only":false}',
                         NATIVE_HISTORY_INDEX_PREFIX + '{"scope":"historical_tool_location_index","historical_only":true,"count":NaN}'):
            with self.subTest(answer=ordinary):
                response = {"info": {"id": "msg_ordinary", "role": "assistant", "finish": "stop",
                    "time": {"completed": 1}}, "parts": [{"type": "text", "text": ordinary}]}
                self.session.inference_finished({"session_id": "test-session"}, response)
                self.assertEqual(self.session.complete_model_answer(response), ordinary)
                self.assertTrue(self.session.response_diagnostics["validated_final"])
        for artifact in (body, " \n" + body + "\n "):
            with self.subTest(artifact=artifact):
                response = {"info": {"id": "msg_artifact", "role": "assistant", "finish": "stop",
                    "time": {"completed": 1}}, "parts": [{"type": "text", "text": artifact}]}
                self.session.inference_finished({"session_id": "test-session"}, response)
                with self.assertRaises(ConnectorError) as rejected:
                    self.session.complete_model_answer(response)
                self.assertEqual(rejected.exception.code, "InternalContextResponse")
                self.assertFalse(self.session.response_diagnostics["validated_final"])

    async def test_unscoped_automatic_requires_explicit_goal_without_inference(self):
        self.session.entered = True
        self.session.status = "ready"
        self.session.world = AsyncMock()
        self.session.schedule = Mock()
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8891") as client:
            response = await client.post("/connector/automatic", json={"enabled": True, "max_count": 2},
                                         headers={"Authorization": "Bearer test-private-bearer"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "GoalRequired")
        self.session.world.assert_not_awaited()
        self.session.schedule.assert_not_called()
        self.assertEqual(self.session.inference_count, 0)

    async def test_private_intent_survives_reconnect_without_a_model_or_world_write(self):
        self.session.host = AsyncMock()
        self.session.world = AsyncMock()
        self.session.configure_private_context({"intent": "想看看别人留下的器皿能怎么用",
                                               "preferences": ["先解释真实后果", "别替我决定"]})
        original = self.session.private_context
        restored = GroveSession(self.manager, {"role_id": "test-user", "access_mode": "control"},
                                "test-private-bearer", "Test", "provider/model")
        self.assertEqual(restored.private_context, original)
        self.assertEqual(restored.state()["context"]["intent"], original["intent"])
        self.assertEqual(restored.session_id, "test-session")
        self.assertEqual(restored.inference_count, 0)
        self.session.host.assert_not_awaited()
        self.session.world.assert_not_awaited()
        self.assertNotIn("test-private-bearer", self.session.memory_file.read_text())
        other = GroveSession(self.manager, {"role_id": "other-user", "access_mode": "control"},
                             "other-private-bearer", "Other", "provider/model")
        self.assertEqual(other.private_context["intent"], "")

    async def test_lost_write_keeps_original_operation_until_receipt_recovery(self):
        self.declare_agent_catalog({"grove.content_submit": {"access": "write"}})
        self.session.world = AsyncMock(side_effect=[self.authenticated_bootstrap(), httpx.ReadError("connection lost")])
        with self.assertRaises(httpx.ReadError):
            await self.session.invoke("grove.content_submit", {"content": {"kind": "layout"}})
        original = self.session.unknown[0]["envelope"]
        self.assertTrue(original["operation_id"])
        self.assertNotIn("test-private-bearer", self.session.memory_file.read_text())
        self.session.world = AsyncMock(side_effect=[self.authenticated_bootstrap(),
            ConnectorError("ReceiptNotFound", "Missing", 404), self.authenticated_bootstrap(),
            self.original_receipt("grove.content_submit", original, {"published": True})])
        await self.session.recover_receipts()
        posts = [call for call in self.session.world.await_args_list if call.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        replay = posts[0]
        self.assertEqual(replay.args[:2], ("POST", "/v1/functions/grove.content_submit/invoke"))
        self.assertEqual(replay.args[2], original)
        self.assertEqual(self.session.unknown, [])

    async def test_unready_host_has_real_deadline_and_remains_recoverable(self):
        self.manager.binary = Path('/usr/bin/true')
        owned = Mock(returncode=None)

        observations = []

        async def stalled_health(*args, **kwargs):
            observations.append(time.monotonic())
            await asyncio.sleep(10)

        self.session.host = AsyncMock(side_effect=stalled_health)
        with patch('agent_world.grove_connector.HOST_START_SECONDS', 0.05), \
                patch('asyncio.create_subprocess_exec', AsyncMock(return_value=owned)):
            with self.assertRaises(ConnectorError) as rejected:
                await self.session.launch()
        self.assertEqual(rejected.exception.code, 'HostTimeout')
        self.assertLess(time.monotonic() - observations[0], 1)
        self.assertIs(self.session.process, owned)
        self.assertEqual(self.session.initialization[-1]['last_probe_error_type'], 'TimeoutError')

    async def test_retry_waits_for_owned_host_without_replacing_conversation(self):
        owned = Mock(returncode=None)
        self.session.process = owned
        capability = self.session.capability
        self.session.world_read = self.session.first_response_success = True
        self.session.automatic_started = True
        self.session.inference_count = 5
        self.session.remember()
        self.session.world = AsyncMock(return_value={'functions': []})
        # This host-startup fixture provides an explicit empty Agent contract;
        # public HTTP/MCP discovery is covered by the independent SDK consumer.
        self.session.agent_client.discover = AsyncMock(return_value={})
        self.session.join = AsyncMock()
        self.session.observe_automatic = AsyncMock()
        self.session.start_automatic_loop = Mock()
        self.session.watch_events = AsyncMock()
        self.session.launch = AsyncMock()
        self.session.stop_host = AsyncMock()
        self.session.turn = AsyncMock()
        plugin = self.simulated_native_context_loaded()
        calls = []

        async def recovering(method, path, body=None, timeout=15):
            calls.append((method, path))
            if path == '/global/health':
                if sum(path == item[1] for item in calls) == 1:
                    raise httpx.ReadTimeout('still initializing')
                return {'healthy': True}
            if path == '/config/providers':
                return {'providers': [{'id': 'provider', 'models': {'model': {}}}]}
            if path == '/config':
                return {'plugin': [plugin]}
            if path == '/experimental/tool/ids':
                return ['read']
            if path == '/session/test-session':
                return {'id': 'test-session', 'permission': self.session.permissions()}
            if path == '/session/status':
                return {}
            if path == '/session/test-session/message?limit=4':
                return []
            self.fail('Unexpected host operation: ' + method + ' ' + path)

        self.session.host = recovering
        await self.session.prepare()
        await self.session.event_worker
        self.session.launch.assert_not_awaited()
        self.session.stop_host.assert_not_awaited()
        self.session.turn.assert_not_awaited()
        self.assertIs(self.session.process, owned)
        self.assertEqual(self.session.capability, capability)
        self.assertEqual(self.session.session_id, 'test-session')
        self.assertEqual(self.session.inference_count, 5)
        self.assertFalse(self.session.automatic['enabled'])
        self.assertTrue(all(method == 'GET' for method, _ in calls))
        saved = json.loads(self.session.memory_file.read_text())
        self.assertEqual((saved['session_id'], saved['inference_count']), ('test-session', 5))

    async def test_saved_session_errors_do_not_start_a_replacement_or_spend_request(self):
        self.session.process = Mock(returncode=None)
        self.session.world = AsyncMock(return_value={'functions': []})
        self.session.agent_client.discover = AsyncMock(return_value={})
        self.session.inference_count = 5
        self.session.remember()
        plugin = self.simulated_native_context_loaded()
        saved = self.session.memory_file.read_bytes()
        original_client = httpx.AsyncClient
        for status, code in ((404, 'SavedSessionMissing'), (500, 'OpenCodeError'),
                             (503, 'OpenCodeError'), (401, 'HostAuthenticationFailed')):
            with self.subTest(status=status):
                calls = []

                def upstream(request):
                    calls.append((request.method, request.url.path))
                    if request.url.path == '/global/health':
                        return httpx.Response(200, json={'healthy': True})
                    if request.url.path == '/config/providers':
                        return httpx.Response(200, json={'providers': [
                            {'id': 'provider', 'models': {'model': {}}}]})
                    if request.url.path == '/config':
                        return httpx.Response(200, json={'plugin': [plugin]})
                    if request.url.path == '/experimental/tool/ids':
                        return httpx.Response(200, json=['read'])
                    if request.url.path == '/session/test-session':
                        return httpx.Response(status, json={'detail': 'private upstream material'})
                    self.fail('Unexpected operation: ' + request.method + ' ' + request.url.path)

                with patch('agent_world.grove_connector.httpx.AsyncClient',
                           side_effect=lambda **kwargs: original_client(
                               **kwargs, transport=httpx.MockTransport(upstream))):
                    with self.assertRaises(ConnectorError) as rejected:
                        await self.session.prepare()
                self.assertEqual(rejected.exception.code, code)
                self.assertEqual(self.session.session_id, 'test-session')
                self.assertEqual(self.session.inference_count, 5)
                self.assertEqual(self.session.memory_file.read_bytes(), saved)
                self.assertTrue(all(method == 'GET' for method, _ in calls))
                self.assertNotIn('private upstream material', json.dumps(self.session.state()))

    async def test_explicit_runtime_root_is_absolute_isolated_and_does_not_move_memory(self):
        self.session.remember()
        original = self.session.memory_file.read_bytes()
        relative = Path(os.path.relpath(self.manager.storage / 'runtime', Path.cwd()))
        app = create_connector_app(storage=self.manager.storage, runtime_storage=relative, wrapper=None)
        manager = app.state.connector
        manager.binary = Path('/usr/bin/true')
        session = GroveSession(manager, self.session.identity, '', 'Test', 'provider/model')
        other = GroveSession(manager, {'role_id': 'different-user'}, '', 'Other', 'provider/model')
        self.assertTrue(manager.runtime_storage.is_absolute())
        self.assertEqual(session.profile, self.session.profile)
        self.assertEqual(session.memory_file.read_bytes(), original)
        self.assertEqual(session.session_id, 'test-session')
        self.assertEqual(self.session.runtime_profile, self.session.profile)
        self.assertNotEqual(other.runtime_profile, session.runtime_profile)
        owned = Mock(returncode=None)
        session.host = AsyncMock(return_value={'healthy': True})
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=owned)) as spawn:
            await session.launch()
        env = spawn.await_args.kwargs['env']
        for kind in ('CONFIG', 'CACHE'):
            self.assertEqual(env['XDG_' + kind + '_HOME'], str(session.profile / ('xdg-' + kind.lower())))
        for kind in ('DATA', 'STATE'):
            self.assertEqual(env['XDG_' + kind + '_HOME'], str(session.runtime_profile / ('xdg-' + kind.lower())))
        self.assertEqual(env['TMPDIR'], str(session.runtime_profile / 'tmp'))
        self.assertEqual(env['OPENCODE_CONFIG_DIR'], str(session.profile / 'xdg-config/opencode'))
        for flag in ('OPENCODE_DISABLE_PROJECT_CONFIG', 'OPENCODE_DISABLE_DEFAULT_PLUGINS',
                     'OPENCODE_DISABLE_EXTERNAL_SKILLS', 'OPENCODE_DISABLE_CLAUDE_CODE'):
            self.assertEqual(env[flag], 'true')
        self.assertNotIn('--pure', spawn.await_args.args)
        config = json.loads((session.profile / 'opencode.json').read_text())
        self.assertEqual(config['plugin'], [session._native_context_plugin])
        permission = {'*': 'deny', 'grove_world_*': 'allow', 'invalid': 'allow'}
        tools = {'*': False, 'grove_world_*': True, 'invalid': True}
        self.assertEqual(config['permission'], permission)
        self.assertEqual(config['tools'], tools)
        self.assertEqual(config['agent']['shared-grove-executor']['permission'], permission)
        self.assertEqual(config['agent']['shared-grove-executor']['tools'], tools)
        self.assertEqual(config['agent']['shared-grove']['permission'], session.conversation_permissions())
        self.assertEqual(config['agent']['shared-grove']['tools'], session.conversation_tools())
        self.assertEqual(config['agent']['shared-grove']['permission']['grove_world_*'], 'deny')
        self.assertFalse(config['agent']['shared-grove']['tools']['grove_world_*'])
        self.assertNotIn('grove_world_grove_move', config['agent']['shared-grove']['tools'])
        self.assertNotIn('grove_world_execution_checkpoint', config['agent']['shared-grove']['tools'])
        for agent in ('shared-grove-blueprint', 'compaction'):
            self.assertEqual(config['agent'][agent]['permission'], {'*': 'deny'})
            self.assertEqual(config['agent'][agent]['tools'], {'*': False})
        self.assertEqual(self.session.blueprint_permissions(), [
            {'permission': '*', 'pattern': '*', 'action': 'deny'}])
        self.assertEqual(session.memory_file.read_bytes(), original)

    async def test_host_authentication_failure_is_not_hidden_as_initialization_timeout(self):
        self.manager.binary = Path('/usr/bin/true')
        owned = Mock(returncode=None)
        self.session.host = AsyncMock(side_effect=ConnectorError('HostAuthenticationFailed', 'Rejected', 502))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=owned)):
            with self.assertRaises(ConnectorError) as rejected:
                await self.session.launch()
        self.assertEqual(rejected.exception.code, 'HostAuthenticationFailed')
        self.session.host.assert_awaited_once()
        self.assertIs(self.session.process, owned)

    async def test_return_question_receives_current_stock_and_other_players_facility(self):
        self.session.entered = True
        self.declare_agent_catalog({'map.viewport': {'access': 'read'}})
        self.session.messages.append({'role': 'assistant', 'text': 'There is no bridge and stock is 2.'})
        self.session.world = AsyncMock(return_value={'events': []})
        self.session.invoke = AsyncMock(return_value={
            'player': {'id': 'test-user', 'x': 3, 'y': 1}, 'players': [], 'chunks': [],
            'entities': [
                {'id': 'bridge', 'x': 7, 'y': 0, 'created_by': 'other-player',
                 'state': {'recipe': 'walkway'}, 'capabilities': ['structure']},
                {'id': 'willow', 'x': 4, 'y': 5, 'state': {'kind': 'resource_node', 'stock': 0},
                 'capabilities': ['harvestable']}], 'inventory': []})
        self.session.host = AsyncMock(return_value={'info': {'id': 'msg_return', 'role': 'assistant',
            'finish': 'stop', 'time': {'completed': 1}}, 'parts': [{'type': 'text', 'text': 'A path is present.'}]})
        await self.session.turn('I came back. Can I cross?')
        parts = self.session.host.await_args.args[2]['parts']
        facts = json.loads(parts[1]['text'].rsplit('\n', 1)[1])
        self.assertEqual(facts['entities'][0]['created_by'], 'other-player')
        self.assertEqual(facts['entities'][0]['discovery_state']['recipe'], 'walkway')
        self.assertEqual(facts['entities'][1]['discovery_state']['stock'], 0)
        self.session.host.assert_awaited_once()
        self.assertEqual(self.session.inference_count, 1)

    async def test_completed_ordinary_questions_allow_next_request_without_goal_charge(self):
        controller = self.session.execution_controller
        original = {key: controller.value(key) for key in ("task_id", "goal", "status", "request_status",
            "requests_used", "max_requests", "max_actions", "actions_count", "resume_allowed")}
        self.session.current_turn_facts = AsyncMock(return_value={'player': {'id': 'test-user'}, 'entities': []})
        self.session.host = AsyncMock(return_value={'info': {'role': 'assistant', 'finish': 'stop',
            'time': {'completed': 1}}, 'parts': [{'type': 'text', 'text': 'Look at the actual objects.'}]})
        await self.session.turn('What is here?')
        self.assertEqual(self.session.execution['host_request']['status'], 'idle')
        self.assertEqual({key: controller.value(key) for key in original}, original)
        await self.session.turn('What changed?')
        self.assertEqual({key: controller.value(key) for key in original}, original)
        self.assertEqual(self.session.inference_count, 2)
        self.assertEqual(self.session.execution['requests_used'], 0)
        self.assertEqual(self.session.host.await_count, 2)

    async def test_timed_out_ordinary_question_keeps_original_native_gate_closed(self):
        controller = self.session.execution_controller
        original = {key: controller.value(key) for key in ("task_id", "goal", "status", "request_status",
            "requests_used", "max_requests", "max_actions", "actions_count", "resume_allowed")}
        self.session.current_turn_facts = AsyncMock(return_value={'player': {'id': 'test-user'}, 'entities': []})
        async def native(method, path, body=None, **kwargs):
            if path.endswith('/message'):
                raise httpx.ReadTimeout('lost native response')
            if path.endswith('/abort'):
                return {}
            if path == '/session/test-session':
                return {'id': 'test-session'}
            if path == '/session/status':
                return {'test-session': {'type': 'busy'}}
            self.fail(path)
        self.session.host = native
        with self.assertRaises(ConnectorError) as failure:
            await self.session.turn('What is here?')
        self.assertEqual(failure.exception.code, 'HostUnavailable')
        self.assertEqual(self.session.execution['host_request']['status'], 'unsettled')
        self.assertEqual({key: controller.value(key) for key in original}, original)
        with self.assertRaises(ConnectorError) as blocked:
            await self.session.turn('A different question')
        self.assertEqual(blocked.exception.code, 'OriginalRequestUnsettled')
        self.assertEqual({key: controller.value(key) for key in original}, original)
        self.assertEqual(self.session.inference_count, 1)

    async def test_question_preserves_paused_task_and_does_not_reissue_saved_goals(self):
        controller = self.session.execution_controller
        controller.state.update(task_id="old-task", goal="制作计数牌", status="paused",
            request_status="idle_confirmed", requests_used=1, max_requests=4,
            max_actions=8, actions_count=2, resume_allowed=True)
        self.session.private_context.update(recent_user_requests=[{"text": "制作计数牌", "purpose": "execution",
            "task_id": "old-task", "time": 1}], last_creation_request="让它自己运行")
        self.session.remember()
        original = {key: controller.value(key) for key in ("task_id", "goal", "status", "request_status",
            "requests_used", "max_requests", "max_actions", "actions_count", "resume_allowed")}
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "test-user"}, "entities": []})
        seen = []
        async def native(_method, _path, body=None, **_kwargs):
            seen.append(deepcopy(body))
            self.assertEqual(self.session.active_native_request["purpose"], "explain")
            self.assertFalse(self.session.active_native_request["world_write_authorized"])
            self.assertNotIn("task_id", self.session.active_native_request)
            return {"info": {"role": "assistant", "finish": "stop", "time": {"completed": 1}},
                "parts": [{"type": "text", "text": "现在暂停了。"}]}
        self.session.host = native
        await self.session.turn("现在暂停了吗？")
        self.assertEqual({key: controller.value(key) for key in original}, original)
        self.assertTrue(controller.can_resume())
        self.assertIsNone(self.session.active_native_request)
        self.assertNotIn("制作计数牌", json.dumps(seen[0], ensure_ascii=False))
        self.assertNotIn("让它自己运行", json.dumps(seen[0], ensure_ascii=False))
        self.assertIn("制作计数牌", json.dumps(self.session.private_context, ensure_ascii=False))
        async def settled(_method, path, body=None, **_kwargs):
            if path == "/session/test-session":
                return {"id": "test-session"}
            if path == "/session/status":
                return {}
            self.fail(path)
        self.session.host = settled
        self.assertTrue(await self.session.settle_execution_request(native_owner={"purpose": "explain"}))
        self.assertEqual({key: controller.value(key) for key in original}, original)
        self.assertTrue(controller.can_resume())
        self.assertEqual(controller.value("host_request")["status"], "idle_confirmed")

    async def test_completed_question_failed_settlement_preserves_paused_task(self):
        controller = self.session.execution_controller
        controller.state.update(task_id="old-task", goal="制作计数牌", status="paused",
            request_status="idle_confirmed", requests_used=1, max_requests=4,
            max_actions=8, actions_count=2, resume_allowed=True)
        original = {key: controller.value(key) for key in ("task_id", "goal", "status", "request_status",
            "requests_used", "max_requests", "max_actions", "actions_count", "resume_allowed")}
        self.session.current_turn_facts = AsyncMock(return_value={"player": {"id": "test-user"}, "entities": []})
        self.session.host = AsyncMock(return_value={"info": {"role": "assistant", "finish": "stop", "time": {"completed": 1}},
            "parts": [{"type": "text", "text": "现在暂停了。"}]})
        await self.session.turn("现在暂停了吗？")
        self.assertEqual(controller.value("host_request")["status"], "idle")
        async def still_running(_method, path, body=None, **_kwargs):
            if path == "/session/test-session":
                return {"id": "test-session"}
            if path == "/session/status":
                return {"test-session": {"type": "busy"}}
            self.fail(path)
        self.session.host = still_running
        self.assertFalse(await self.session.settle_execution_request(native_owner={"purpose": "explain"}))
        self.assertEqual({key: controller.value(key) for key in original}, original)
        self.assertEqual(controller.value("host_request")["status"], "unsettled")
        self.assertFalse(controller.native_ready())

    async def test_memory_save_failure_blocks_next_native_request_after_tool_error(self):
        controller = self.session.execution_controller
        controller.active = True
        controller.state.update(task_id="original-task", segment_id="original-segment", status="running",
            request_status="idle_confirmed", resume_allowed=True)
        self.session.status = "thinking"
        self.session.active_native_request = {"purpose": "execution", "world_write_authorized": True,
            "session_id": self.session.session_id, "task_id": "original-task", "segment_id": "original-segment"}
        self.declare_agent_catalog({"grove.rule_catalog": {"access": "read", "input_schema": {}}})
        self.session.invoke = AsyncMock(return_value={"result": {"definitions": []}})
        with patch("agent_world.grove_connector.save_json", side_effect=OSError("private storage details")):
            with self.assertRaises(ConnectorError) as failed:
                await self.session.tool_call("grove_rule_catalog", {})
        self.assertEqual(failed.exception.code, "ExecutionPersistenceFailed")
        self.assertNotIn("private storage details", failed.exception.message)
        self.assertFalse(controller.active)
        self.assertFalse(controller.value("resume_allowed"))
        self.session.host = AsyncMock()
        self.session.current_turn_facts = AsyncMock()
        with self.assertRaises(ConnectorError) as blocked:
            await self.session.turn("继续解释")
        self.assertEqual(blocked.exception.code, "ExecutionPersistenceFailed")
        self.session.host.assert_not_awaited()

    async def test_question_cannot_use_writes_or_checkpoint_even_with_stale_active_controller(self):
        self.session.status = "thinking"
        self.session.execution_controller.active = True
        self.session.execution_controller.state.update(task_id="old-task", segment_id="old-segment", status="running")
        self.session.active_native_request = {"purpose": "explain", "world_write_authorized": False,
            "session_id": self.session.session_id}
        self.declare_agent_catalog({"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}})
        self.session.invoke = AsyncMock()
        self.session.execution_checkpoint = AsyncMock()
        before = self.session.execution
        for name in ("grove_interact", "execution_checkpoint"):
            with self.assertRaises(ConnectorError) as denied:
                await self.session.tool_call(name, {"execution_segment_id": "old-segment"})
            self.assertEqual(denied.exception.code, "ConversationReadOnly")
        self.assertEqual(self.session.execution, before)
        self.session.invoke.assert_not_awaited()
        self.session.execution_checkpoint.assert_not_awaited()

    def test_legacy_requests_preserved_without_replay_or_new_duplicate_memory(self):
        self.assertEqual(set(self.session.private_context), {"intent", "preferences"})
        requests = [{"text": "旧目标", "purpose": "execution", "task_id": "old-task", "time": i}
            for i in range(10)]
        self.session.private_context.update(recent_user_requests=requests, last_creation_request="旧创作")
        self.session.remember()
        restored = GroveSession(self.manager, {"role_id": "test-user", "access_mode": "control"},
            "test-private-bearer", "Test", "provider/model")
        self.assertEqual(restored.private_context["recent_user_requests"], requests)
        self.assertEqual(restored.private_context["last_creation_request"], "旧创作")
        self.assertEqual(restored.state()["private_context"], {"intent": "", "preferences": []})
        restored.configure_private_context({"intent": "新偏好"})
        self.assertEqual(restored.private_context["recent_user_requests"], requests)
        self.assertEqual(restored.private_context["last_creation_request"], "旧创作")

    async def test_question_control_read_does_not_adopt_world_control_into_original_task(self):
        self.session.status = "thinking"
        self.session.active_native_request = {"purpose": "explain", "world_write_authorized": False,
            "session_id": self.session.session_id}
        self.declare_agent_catalog({"grove.control_status": {"access": "read", "input_schema": {}}})
        self.session.execution_controller.state.update(task_id="original-task", goal="继续观察",
            status="paused", grant_id="known-grant", grant_operation_id="known-operation", actions_count=2)
        self.session.invoke = AsyncMock(return_value={"ok": True, "read_only": True, "function_id": "grove.control_status",
            "function_version": 1, "snapshot_seq": 0, "result": {"goal": "继续观察", "status": "active",
            "grant_id": "different-grant", "actions_count": 5, "version": 40}})
        before = self.session.execution
        delivered = await self.session.tool_call("grove_control_status", {})
        self.assertEqual(delivered["result"]["grant_id"], "different-grant")
        self.assertEqual(self.session.execution, before)

    def test_ui_receives_client_inspection_references_without_changing_agent_readback(self):
        inspection = {"entity": {"id": "counter", "name": "计数牌", "state": {"program": {"fields": {"count": 7}}}}}
        readback = {"observed_at": 100, "selected_inspection": inspection,
            "action_targets": [{"entity_id": "counter", "inspection": inspection, "observed_at": 99},
                {"entity_id": "gone", "unavailable": {"code": "ResourceNotFound"}, "observed_at": 101}]}
        self.session.execution_controller.state["readback"] = deepcopy(readback)
        projected = self.session.state()["execution"]["readback"]
        self.assertNotIn("program", json.dumps(projected))
        self.assertEqual(projected["action_targets"][0]["client_read_request"], {
            "function_id": "grove.client_inspect", "arguments": {"entity_id": "counter"}})
        self.assertEqual(projected["action_targets"][0]["observed_at"], 99)
        self.assertEqual(projected["action_targets"][1]["unavailable"], {"code": "ResourceNotFound"})
        self.assertFalse(projected["selected_inspection"]["projection"]["current_detail_delivered"])
        self.assertEqual(self.session.execution["readback"], readback)

    async def test_http_state_keeps_original_display_facts_without_exporting_storage_mirrors(self):
        entity = {"id": "counter", "name": "计数牌", "version": 7, "x": 3, "y": 2,
            "owner": None, "container": None, "authored_description": "共同留下的说明",
            "state": {"water": None, "program": {"fields": {"count": 7}, "mechanical": {"energy": 4},
                "definition_ref": "raw-definition"}}, "simulation": {"raw": "raw-simulation"}}
        action = {"action": "fuel", "stored_energy_after": 4, "target_name": "动力源",
            "transfer": {"source_name": "水袋", "target_name": "水槽", "material": "water", "transferred": 2},
            "costs": [{"entity_id": "fuel", "entity_name": "木料", "material": "wood", "quantity_u": 4}],
            "related_entities": [entity], "recording": {"state": {"program": {"private": "raw-recording"}}}}
        outcome = {"kind": "server_receipt", "changes": ["计数变成七"], "entity": entity,
            "player": {"id": "test-user", "x": 3, "y": 2}, "action_result": action,
            "same_position_public_objects": [{"id": "counter", "name": "计数牌"}]}
        receipt = {"function": "grove.interact", "operation_id": "original-op", "status": "confirmed",
            "commit_seq": 42, "task_id": "original-task", "segment_id": "original-segment",
            "action": "fuel", "action_entity_ids": ["counter"], "confirmed_outcome": outcome}
        readback = {"observed_at": 100, "player": {"id": "test-user", "x": 3, "y": 2},
            "clock": {"sequence": 43}, "entities": [entity], "inventory": [entity],
            "viewport": {"entities": [entity]}, "public_content": {"anchors": [entity]},
            "selected_inspection": {"entity": entity},
            "action_targets": [{"entity_id": "counter", "inspection": {"entity": entity}, "observed_at": 99}]}
        self.session.generation = {"status": "published", "published": True, "attempts": 1,
            "entities": [entity | {"id": "published-" + str(index)} for index in range(12)],
            "process": {"id": "rain", "label": "共同降雨", "status": "running", "state": {"program": "raw-process"}},
            "readback": {"receipt_confirmed": True, "viewport_read": True, "observed_at": 100,
                "visible_entity_ids": ["counter"], "viewport": {"entities": [entity]}},
            "public_content": {"anchors": [entity]}, "contribution": {"entity": entity}}
        self.session.execution_controller.state.update(task_id="original-task", readback=readback,
            readback_actual=True, receipts=[receipt], progress={"summary": "已添燃料", "next_intent": "读回现场", "evidence": [receipt]})
        self.session.execution_controller.archive.append(deepcopy(self.session.execution))
        before = deepcopy((self.session.generation, self.session.execution_controller.state, self.session.execution_archive))
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.authorized = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as client:
            response = await client.get("/connector/state", headers={"Authorization": "Bearer test-bearer"})
        self.assertEqual(response.status_code, 200)
        page = response.json()
        exported = {key: page[key] for key in ("generation", "execution", "execution_archive")}
        self.assertNotIn('"state":', json.dumps(exported))
        self.assertNotIn('"program":', json.dumps(exported))
        self.assertNotIn("raw-", json.dumps(exported))
        self.assertEqual(len(page["generation"]["entities"]), 12)
        self.assertEqual(page["generation"]["entities"][0]["authored_description"], "共同留下的说明")
        self.assertEqual(page["generation"]["process"], {"id": "rain", "label": "共同降雨", "status": "running"})
        self.assertTrue(page["generation"]["readback"]["receipt_confirmed"])
        for execution in (page["execution"], page["execution_archive"][0]):
            facts = execution["receipts"][0]["confirmed_outcome"]
            self.assertEqual(facts["changes"], outcome["changes"])
            self.assertEqual(facts["action_result"]["transfer"], action["transfer"])
            self.assertEqual(facts["action_result"]["costs"], action["costs"])
            self.assertEqual(facts["action_result"]["stored_energy_after"], 4)
            self.assertEqual(facts["action_result"]["related_entities"][0]["name"], "计数牌")
            rows = facts["entity"]["summary_facts"]
            declared = next(row for row in rows if row["id"] == "fields")["rows"][0]
            self.assertEqual((declared["value"], declared["temporal_scope"], declared["source"]),
                (7, "historical", "original_server_receipt"))
            water = next(row for row in rows if row["id"] == "water")
            self.assertEqual((water["value"], water["availability"]), (None, "unknown"))
            self.assertEqual(execution["progress"]["evidence"][0]["confirmed_outcome"], facts)
            self.assertEqual(execution["action_summary"]["groups"][0]["last_confirmed_outcome"]["changes"], ["计数变成七"])
            self.assertEqual(execution["readback"]["clock"], {"sequence": 43})
            self.assertEqual(execution["readback"]["action_targets"][0]["client_read_request"], {
                "function_id": "grove.client_inspect", "arguments": {"entity_id": "counter"}})
        self.assertEqual((self.session.generation, self.session.execution_controller.state, self.session.execution_archive), before)

    async def test_failed_current_read_does_not_spend_model_request(self):
        self.session.entered = True
        self.declare_agent_catalog({'map.viewport': {'access': 'read'}})
        self.session.world = AsyncMock(side_effect=httpx.ReadError('world unavailable'))
        self.session.host = AsyncMock()
        with self.assertRaises(httpx.ReadError):
            await self.session.turn('What has changed?')
        self.session.host.assert_not_awaited()
        self.assertEqual(self.session.inference_count, 0)

    def test_inspect_projection_only_references_strictly_identical_fields(self):
        options = [{"action": "store", "input": {"candidates": [{"id": "item", "preview": {
            "version_fingerprint": "a"*64, "physics": {"known": True, "overloaded": True}}}]}}]
        original = {"result": {"entity": {"id": "box", "version": 7, "simulation": {"total": 160},
            "action_options": options, "available_actions": ["store"]}, "action_options": options,
            "available_actions": ["store"], "contents": [], "public_content": {
                "id": "content:0:0", "region": "0:0", "version": 8,
                "history": {"cursor": "older-page", "outcome_count": 22}},
            "action_options_projection": {"scope": "selected_declared_action", "action": "store",
                                          "expanded_actions": ["store"]}}}
        before = deepcopy(original)
        result = companion_projection("grove.inspect", original)["result"]
        self.assertNotIn("action_options", result["entity"])
        self.assertEqual(result["entity"]["field_references"]["action_options"], "#/action_options")
        self.assertEqual(result["action_options"], options)
        self.assertEqual(result["entity"]["simulation"], {"total": 160})
        self.assertEqual(result["public_content"]["version"], 8)
        self.assertEqual(result["public_content"]["projection"]["scope"], "regional_content_reference")
        self.assertEqual(result["public_content"]["outcome_count"], 22)
        self.assertEqual(result["public_content"]["read_request"], {
            "tool": "grove_content_history", "arguments": {"content_id": "content:0:0"}})
        self.assertNotIn("history", result["public_content"])
        self.assertNotIn("cursor", result["public_content"]["read_request"]["arguments"])
        self.assertEqual(result["public_content"]["discovery_read_request"], {
            "tool": "map_viewport", "arguments": {}})
        self.assertEqual(original, before)
        self.assertIn("action_options", original["result"]["entity"])
        original["result"]["entity"]["available_actions"] = ["close"]
        original["result"]["entity"]["action_options"] = [{"action": "close"}]
        differing = companion_projection("grove.inspect", original)["result"]["entity"]
        self.assertEqual(differing["available_actions"], ["close"])
        self.assertEqual(differing["action_options"], [{"action": "close"}])

    def test_region_reference_keeps_latest_history_and_map_discovery_without_repeating_snapshots(self):
        entity = {"id":"box", "version":7, "name":"Box", "x":3, "y":2, "material":"stone",
            "state":{"open":False,"program":{"definition_ref":"immutable","contract":{"status":"active"}}},
            "matter":[{"id":"original-lot","quantity":2}], "simulation":{"total":160}}
        options = [{"action":"store","available":False,"reason":"Overloaded",
            "input":{"candidates":[{"id":"item","preview":{"available":False,"physics":{"overloaded":True}}}]}}]
        public = {"id":"content:0:0", "region":"0:0", "version":8,
            "history":{"cursor":"older-page", "outcome_count":22},
            "anchors":[{"entity_id":"box","version":7,"entity":entity}],
            "contributions":[{"id":"source","text":"historical text"}], "outcome_refs":[{"id":"receipt"}]}
        original = {"result":{"entity":entity,"action_options":options,"available_actions":[],"public_content":public,
            "action_options_projection": {"scope": "selected_declared_action", "action": "store",
                                          "expanded_actions": ["store"]}}}
        before = deepcopy(original)
        actual = companion_projection("grove.inspect", original)["result"]
        self.assertEqual(actual["entity"],entity)
        self.assertEqual(actual["action_options"],options)
        index = actual["public_content"]
        self.assertEqual({key: index[key] for key in ("id", "region", "version", "outcome_count")},
            {"id": "content:0:0", "region": "0:0", "version": 8, "outcome_count": 22})
        for field in ("history", "anchors", "contributions", "outcome_refs"):
            self.assertNotIn(field, index)
        latest_read = {"tool": "grove_content_history", "arguments": {"content_id": "content:0:0"}}
        self.assertEqual(index["read_request"], latest_read)
        self.assertNotIn("cursor", index["read_request"]["arguments"])
        self.assertIn("latest page", index["projection"]["meaning"])
        self.assertEqual(index["discovery_read_request"], {"tool": "map_viewport", "arguments": {}})
        self.assertEqual(original, before)
        map_source = {"entities": [entity], "public_content": public}
        map_before = deepcopy(map_source)
        viewport = companion_projection("map.viewport", map_source)
        discovered = viewport["entities"][0]
        self.assertEqual({key: discovered[key] for key in ("id", "name", "x", "y", "material")},
            {"id": "box", "name": "Box", "x": 3, "y": 2, "material": "stone"})
        self.assertEqual(discovered["read_request"], {
            "tool": "grove_inspect", "arguments": {"entity_id": "box"}})
        for field in ("state", "simulation", "action_options"):
            self.assertNotIn(field, discovered)
        self.assertEqual(viewport["public_content"]["read_request"], latest_read)
        self.assertEqual(map_source, map_before)
        folded = blueprint_model_input({"public_content":index,"public_entities":[entity]})
        self.assertEqual(folded["public_entities"],[entity])

    async def test_selected_inspection_is_authorized_before_model_request(self):
        self.session.entered = True
        self.declare_agent_catalog({"map.viewport": {"access": "read"}, "grove.inspect": {"access": "read"}})
        self.session.world = AsyncMock(return_value={"events": []})
        preview = {"physics": {"known": True, "overloaded": True}, "version_fingerprint": "a"*64}
        selected = {"entity": {"id": "box", "version": 7, "simulation": {"total": 160}},
                    "action_options": [{"action": "store", "input": {"candidates": [{"id": "item", "preview": preview}]}}],
                    "contents": []}
        scene = {"player": {"id": "test-user", "x": 3, "y": 1}, "entities": [], "inventory": []}
        self.session.invoke = AsyncMock(side_effect=[scene, selected])
        self.session.host = AsyncMock(return_value={"info": {"id": "selected-answer", "role": "assistant",
            "finish": "stop", "time": {"completed": 1}}, "parts": [{"type": "text", "text": "先看组合载荷。"}]})
        await self.session.turn("能装吗？", context_entity_id="box")
        facts = json.loads(self.session.host.await_args.args[2]["parts"][1]["text"].rsplit("\n", 1)[1])
        inspection = facts["selected_inspection"]
        self.assertEqual(inspection["entity"], selected["entity"])
        self.assertEqual(inspection["contents"], [])
        self.assertTrue(inspection["projection"]["complete_object_state"])
        option = inspection["action_options"][0]
        self.assertNotIn("input", option)
        self.assertEqual(option["candidates_index"][0]["read_request"]["arguments"],
                         {"entity_id": "box", "action": "store", "candidate_id": "item"})

        self.session.invoke.assert_any_await("grove.inspect", {"entity_id": "box"})
        self.assertEqual(self.session.inference_count, 1)
        self.session.host.reset_mock()
        self.session.invoke = AsyncMock(side_effect=[scene, ConnectorError("PermissionDenied", "私有物件", 403)])
        with self.assertRaises(ConnectorError) as denied:
            await self.session.turn("看看别人的背包", context_entity_id="private-box")
        self.assertEqual(denied.exception.code, "PermissionDenied")
        self.session.host.assert_not_awaited()
        self.assertEqual(self.session.inference_count, 1)

    async def test_unreadable_previous_selection_keeps_current_scene_but_no_stale_verdict(self):
        self.session.entered = True
        self.declare_agent_catalog({name: {"access": "read"} for name in ("map.viewport", "grove.inspect")})
        self.session.world = AsyncMock(return_value={"events": []})
        scene = {"player": {"id": "test-user", "x": 12, "y": 0, "version": 9}, "entities": [], "inventory": []}
        self.session.invoke = AsyncMock(side_effect=[scene, ConnectorError("RuleViolation", "物件不在附近视野内。", 409)])
        facts = await self.session.current_turn_facts(context_entity_id="old-target", optional_selected=True)
        self.assertEqual(facts["player"], scene["player"])
        self.assertNotIn("selected_inspection", facts)
        self.assertEqual(facts["selected_inspection_unavailable"]["entity_id"], "old-target")
        self.assertEqual(self.session.execution_controller.world_results(facts), [])
        self.session.invoke = AsyncMock(side_effect=[scene, ConnectorError("WorldUnavailable", "Network read failed", 503)])
        with self.assertRaises(ConnectorError) as unavailable:
            await self.session.current_turn_facts(context_entity_id="old-target", optional_selected=True)
        self.assertEqual(unavailable.exception.code, "WorldUnavailable")

    async def test_stable_write_catalog_requires_explicit_person_delegation(self):
        self.declare_agent_catalog({"grove.move": {"access": "write", "input_schema": {}},
                                "grove.handbook": {"access": "read", "input_schema": {}}})
        self.assertIn("grove_move", [t["name"] for t in self.session.tools()])
        self.session.status = "thinking"
        self.session.invoke = AsyncMock()
        with self.assertRaises(ConnectorError) as denied:
            await self.session.tool_call("grove_move", {"dx": 1, "dy": 0})
        self.assertEqual(denied.exception.code, "ConversationReadOnly")
        self.session.invoke.assert_not_awaited()

    async def test_execution_keeps_catalog_on_demand_and_closes_writes_before_goal_settlement(self):
        self.declare_agent_catalog({"grove.rule_catalog": {"access": "read"}})
        self.session.current_turn_facts = AsyncMock(return_value={"scene": {"player": "actual"}})
        self.session.invoke = AsyncMock(return_value={"result": {"limits": {"bound_object_ids": 8}}})
        self.session.release_opportunity = AsyncMock()
        self.session.execution_controller.active = True
        requests = []

        async def host(method, path, body=None, timeout=15):
            requests.append(path)
            if path.endswith('/message'):
                supplied = json.loads(body['parts'][1]['text'].split('\n', 1)[1])
                self.assertNotIn('rule_catalog', supplied)
                self.assertIn('grove_rule_catalog', supplied['detail_reads']['tools'])
                self.assertIn('base_observation_id', supplied['detail_reads']['meaning'])
                raise httpx.ReadTimeout('test timeout')
            self.fail('The goal loop, not an isolated model turn, settles the native request.')

        self.session.host = host
        with self.assertRaises(ConnectorError) as failed:
            await self.session.turn('Publish one rule definition', executing=True)
        self.assertEqual(failed.exception.code, 'HostUnavailable')
        self.assertEqual(self.session.inference_count, 1)
        self.assertEqual(requests, ['/session/test-session/message'])
        self.assertFalse(self.session.execution_controller.active)
        self.session.invoke.assert_not_awaited()

    async def test_discovery_index_keeps_real_material_and_location_without_copying_details(self):
        self.session.entered = True
        self.declare_agent_catalog({name: {"access": "read"} for name in ("map.viewport", "grove.inspect")})
        self.session.world = AsyncMock(return_value={"events": []})
        source = {"id": "foreign-source", "version": 6, "x": None, "y": None,
            "location": {"x": 3, "y": 2}, "owner": None, "capabilities": ["container"],
            "state": {"open": False}, "source": "agent", "creative": {"definition_ref": "foreign-definition",
                "type_id": "mixer", "reaction_observations": ["long detailed condition"]},
            "matter": [{"material": "foreign-definition#product", "slot": "contained", "quantity": 2}],
            "material_quantity": {"unit": "logical_matter"}, "simulation": {"total": 8}}
        preview = {"physics": {"known": True, "overloaded": False}, "version_fingerprint": "f" * 64}
        selected = {"player": {"id": "test-user", "version": 5}, "entity": deepcopy(source),
            "action_options": [{"action": "store", "input": {"candidates": [{"id": "tool", "preview": preview}]}}]}
        self.session.invoke = AsyncMock(side_effect=[{"player": {"id": "test-user", "x": 3, "y": 2},
            "entities": [source], "inventory": [source], "inventory_total": 7, "inventory_next": "next-page"}, selected])
        facts = await self.session.current_turn_facts(context_entity_id="foreign-source")
        indexed = facts["entities"][0]
        self.assertEqual(indexed["matter"][0], source["matter"][0])
        self.assertEqual(indexed["location"], source["location"])
        self.assertEqual(indexed["version"], 6)
        self.assertEqual(indexed["contents_visibility"], "hidden")
        self.assertNotIn("simulation", indexed)
        self.assertNotIn("reaction_observations", indexed["creative"])
        self.assertEqual(facts["inventory_next"], "next-page")
        self.assertEqual(facts["selected_inspection"]["entity"], source)
        selected_option = facts["selected_inspection"]["action_options"][0]
        self.assertNotIn("input", selected_option)
        self.assertEqual(selected_option["candidates_index"][0]["read_request"]["arguments"],
                         {"entity_id": "foreign-source", "action": "store", "candidate_id": "tool"})

        # Write receipts re-project this already compact readback. Current
        # stock/open state and the selected fresh versions must survive it.
        indexed["discovery_state"]["open"] = True
        indexed.pop("contents_visibility", None)
        projected = companion_projection("grove.interact", {"result": {}, "execution_readback": facts})
        readback = projected["execution_readback"]
        self.assertTrue(readback["entities"][0]["discovery_state"]["open"])
        self.assertNotEqual(readback["entities"][0].get("contents_visibility"), "hidden")
        self.assertEqual(readback["selected_inspection"]["entity"], facts["selected_inspection"]["entity"])
        self.assertNotIn("action_options", readback["selected_inspection"])
        self.assertEqual(readback["selected_inspection"]["read_request"]["arguments"], {"entity_id": "foreign-source"})
        self.assertEqual(readback["entities"][0]["matter"], facts["entities"][0]["matter"])

    async def test_preflight_defers_details_without_changing_selected_preview_or_readback(self):
        self.session.entered = True
        self.declare_agent_catalog({name: {"access": "read"} for name in ("map.viewport", "grove.inspect")})
        self.session.world = AsyncMock(return_value={"events": [], "observed_at": 12})
        objects = [{"id": f"public-{number}", "version": number + 3, "name": f"装置 {number}",
            "created_by": f"author-{number}", "x": number, "y": 0, "owner": None,
            "state": {"open": True, "wetness": None}, "capabilities": ["container"],
            "matter": [{"id": "not-a-presented-lot", "material": "wood", "slot": "body", "quantity": 4}],
            "creative": {"definition_ref": f"definition-{number}", "type_id": "mechanism",
                "fields": {"signal": False, "level": None}, "world_tick": 12,
                "automatic_rule_count": 2, "explicit_action_count": 1,
                "mechanical_observation": {"stored_state": {"position": number, "energy": 0},
                    "role_meaning": "A stored observation, not an executed action. " * 20,
                    "last_solved": {"step": 11, "historical": True}},
                "automatic_field_change": {"step": 11, "fields": {"signal": {"before": True, "after": False}}}},
            "simulation": {"local": 4, "total": 4, "supported": 0, "limit": 8}}
            for number in range(6)]
        scene = {"player": {"id": "test-user", "version": 2, "x": 0, "y": 0},
            "entities": objects, "inventory": [], "inventory_total": 3, "inventory_next": "page-2",
            "chunks": [{"cx": -1, "cy": 0, "tiles": ["grass", "unknown"]}], "clock": {"sequence": 12},
            "construction": {"material_totals": {"wood": 4}, "unit": "logical_matter",
                "recipes": [{"id": f"recipe-{n}", "cost": {"wood": 1}, "material_cost_u": {"wood": 4},
                    "use": "真实材料消耗。" * 50} for n in range(8)]},
            "public_content": {"id": "content:0:0", "region": "0:0", "version": 5,
                "anchors": [{"entity_id": "outside-view", "version": 999, "name": "另一件作品"}],
                "history": {"cursor": "past-latest"}, "contribution_count": 7},
            "processes": [{"phase": "old-record", "data": "x" * 1000}]}
        preview = {"version_fingerprint": "a" * 64, "physics": {"known": True, "overloaded": False}}
        selected = {"player": scene["player"], "entity": objects[0], "action_options": [{
            "action": "store", "input": {"candidates": [{"id": "selected-source", "version": 7,
                "preview": preview}]}}], "action_options_projection": {
                    "scope": "selected_declared_action", "action": "store", "expanded_actions": ["store"]}}
        async def invoke(function, arguments):
            return deepcopy(selected if function == "grove.inspect" else scene)
        self.session.invoke = AsyncMock(side_effect=invoke)
        preflight = await self.session.current_turn_facts(context_entity_id="public-0", preflight=True)
        normal = await self.session.current_turn_facts(context_entity_id="public-0")
        self.assertLess(len(json.dumps(preflight, ensure_ascii=False)),
                        len(json.dumps(normal, ensure_ascii=False)) * 0.65)
        for source, row in zip(objects, preflight["entities"]):
            for field in ("id", "version", "name", "created_by", "x", "y", "owner"):
                self.assertEqual(row[field], source[field])
            self.assertEqual(row["creative"]["fields"], source["creative"]["fields"])
            self.assertEqual(row["discovery_state"]["wetness"], None)
            self.assertNotIn("mechanical_observation", row["creative"])
            self.assertNotIn("id", row["matter"][0])
            self.assertEqual(row["read_request"]["arguments"], {"entity_id": source["id"]})
        self.assertEqual(preflight["inventory_next"], "page-2")
        self.assertFalse(preflight["chunks"][0]["terrain"]["complete"])
        self.assertNotIn("recipes", preflight["construction"])
        self.assertNotIn("anchors", preflight["public_content"])
        self.assertEqual(preflight["public_content"]["read_request"]["arguments"], {"content_id": "content:0:0"})
        self.assertIn("unknown", preflight["discovery_projection"]["meaning"])
        delivered = self.session.present_agent_result("map.viewport", preflight, preflight=True)
        self.assertEqual(delivered["selected_inspection"], normal["selected_inspection"])
        self.assertEqual(delivered["selected_inspection"]["action_options"][0]["input"]["candidates"][0]["preview"], preview)
        self.assertIn("mechanical_observation", normal["entities"][0]["creative"])
        self.assertEqual(normal["construction"], scene["construction"])
        self.assertEqual(normal["processes"], scene["processes"])
        normal_map = companion_projection("map.viewport", scene)
        self.assertNotIn("simulation", normal_map["entities"][0])
        self.assertNotIn("anchors", normal_map["public_content"])
        self.assertEqual(normal_map["entities"][0]["read_request"]["arguments"], {"entity_id": "public-0"})
        self.assertEqual(normal_map["construction"]["material_totals"], scene["construction"]["material_totals"])

    async def test_preflight_binds_only_displayed_discovery_and_explicit_selected_details(self):
        self.session.entered = True
        self.declare_agent_catalog({"map.viewport": {"access": "read"}})
        self.session.world = AsyncMock(return_value={"events": []})
        self.session.invoke = AsyncMock(return_value={
            "player": {"id": "test-user", "version": 2, "x": 0, "y": 0},
            "entities": [{"id": "target", "version": 3, "state": {"wetness": None},
                "action_options": [{"action": "store", "input": {"candidates": [{
                    "id": "hidden-candidate", "version": 8, "preview": {"version_fingerprint": "a" * 64}}]}}]}],
            "public_content": {"id": "content:0:0", "version": 5,
                "anchors": [{"entity_id": "hidden-anchor", "version": 9}]}})
        facts = await self.session.current_turn_facts(preflight=True)
        presented = self.session.present_agent_result("map.viewport", facts, preflight=True)
        bound = self.session.agent_observation(presented["observation_id"])
        self.assertEqual(bound["versions"]["entity"], {"target": 3})
        self.assertEqual(bound["versions"]["content"], {"content:0:0": 5})
        self.assertFalse(bound.get("combination_previews"))

    def test_movement_receipt_defers_repeated_scene_but_keeps_actor_and_resource_page(self):
        entities = [{"id": f"nearby-{index}", "version": 3, "name": "公开物件" * 20,
                     "x": index, "y": 2, "material": "wood"} for index in range(40)]
        inventory = [{"id": "own-source", "version": 5, "name": "木材", "owner": "test-user",
            "matter": [{"material": "wood", "slot": "body", "quantity": 12}],
            "action_options": [{"action": "drop", "input": {"unused_expansion": "x" * 5000}}]}]
        scene = {"player": {"id": "test-user", "version": 1, "x": 1, "y": 2},
            "entities": entities, "inventory": inventory, "inventory_total": 7, "inventory_next": "own-source",
            "chunks": [{"cx": x, "cy": y, "tiles": ["grass"] * 256} for x, y in ((0, 0), (0, -1), (-1, 0), (-1, -1))],
            "construction": {"material_totals": {"wood": 12}, "unit": "logical_matter",
                "recipes": [{"id": "example", "name": "配方" * 100}]},
            "public_content": {"id": "content:0:0", "version": 9,
                "anchors": [{"entity_id": row["id"], "version": 3, "entity": row} for row in entities]}}
        base = self.session.present_agent_result("map.viewport", scene, preflight=True)["observation_id"]
        current = deepcopy(scene)
        current["player"].update(version=2, x=2)
        original = {"operation_id": "one-move", "ok": True, "commit_seq": 44,
            "result": {"player": current["player"], "changes": ["走了一格。"], "viewport": current},
            "execution_readback": current, "execution_control": {"actions_count": 1}}
        unchanged = deepcopy(original)
        expanded = companion_projection("grove.move", original)
        delivered = self.session.present_agent_result("grove.move", original, base_id=base)
        readback = delivered["execution_readback"]
        self.assertEqual(delivered["operation_id"], "one-move")
        self.assertEqual(delivered["result"]["changes"], ["走了一格。"])
        self.assertEqual(readback["player"], current["player"])
        # Automatic feedback does not re-send unrelated discovery. The map
        # remains the explicit entrance to those current objects.
        self.assertEqual(readback["entities"], [])
        self.assertEqual(readback["construction"]["material_totals"], {"wood": 12})
        self.assertEqual(readback["inventory"], [])
        self.assertEqual(readback["inventory_discovery"][0]["id"], "own-source")
        self.assertNotIn("version", readback["inventory_discovery"][0])
        self.assertNotIn("matter", readback["inventory_discovery"][0])
        self.assertEqual(readback["inventory_total"], 7)
        self.assertNotIn("inventory_next", readback)
        self.assertEqual(readback["inventory_discovery_projection"]["read_request"],
            {"tool": "map_viewport", "arguments": {}})
        self.assertNotIn("chunks", readback)
        self.assertNotIn("public_content", readback)
        self.assertEqual(delivered["read_requests"], [{"tool": "map_viewport", "arguments": {}}])
        self.assertLess(len(json.dumps(delivered, ensure_ascii=False)), len(json.dumps(expanded, ensure_ascii=False)) // 3)
        versions = self.session.agent_observation(delivered["observation_id"])["versions"]
        self.assertEqual(versions["player"]["test-user"], 2)
        # This old version came from the explicitly presented base map. The
        # unexpanded inventory index cannot refresh or manufacture it.
        self.assertEqual(versions["entity"], self.session.agent_observation(base)["versions"]["entity"])
        self.assertEqual(original, unchanged)

    def test_action_receipt_marks_consumed_commit_lots_without_replacing_evidence(self):
        committed = {"id": "tray", "version": 3, "matter": [
            {"material": "salt", "slot": "contained", "quantity": 2, "lot_id": "used-salt"},
            {"material": "glue", "slot": "contained", "quantity": 2, "lot_id": "old-glue"}]}
        current = {"id": "tray", "version": 4, "matter": [
            {"material": "glue", "slot": "contained", "quantity": 1},
            {"material": "brick", "slot": "contained", "quantity": 1}]}
        base = self.session.present_agent_result("grove.inspect", {"entity": committed})["observation_id"]
        original = {"operation_id": "transfer", "commit_seq": 44, "result": {"entity": committed},
                    "execution_readback": {"entities": [current]}}
        unchanged = deepcopy(original)
        delivered = self.session.present_agent_result("grove.interact", original, base_id=base)
        entity = delivered["result"]["entity"]
        self.assertEqual(entity["matter"], committed["matter"])
        projection = entity["snapshot_projection"]
        self.assertEqual(projection["scope"], "committed_operation_snapshot")
        self.assertEqual(projection["superseded_by_readback"], {
            "version": 4, "entity_ref": "#/execution_readback/entities/0"})
        self.assertEqual(delivered["read_requests"][0], {
            "tool": "grove_inspect", "arguments": {"entity_id": "tray"}})
        self.assertEqual(delivered["action_snapshot_projection"]["changed_entity_ids"], ["tray"])
        self.assertEqual(delivered["execution_readback"]["entities"][0]["matter"], current["matter"])
        self.assertEqual(self.session.agent_observation(delivered["observation_id"])["versions"]["entity"]["tray"], 4)
        self.assertEqual(self.session.agent_observation(base)["versions"]["entity"]["tray"], 3)
        self.assertEqual(original, unchanged)

    def test_action_delta_keeps_explicit_target_and_never_binds_omitted_changes(self):
        old = [{"id": f"nearby-{index}", "version": 1} for index in range(20)]
        base = self.session.present_agent_result("map.viewport", {"entities": old})["observation_id"]
        changed = [{"id": row["id"], "version": 2} for row in old]
        changed.append({"id": "new-but-omitted", "version": 99})
        scene = {"player": {"id": "test-user", "version": 7}, "entities": changed,
            "changes": [{"entity_id": "nearby-19", "summary": "真实附近变化", "action": "interact"}]}
        delivered = self.session.present_agent_result("grove.interact", {
            "operation_id": "actual-action", "result": {"changes": ["真实动作结果"], "viewport": scene},
            "execution_readback": scene}, base_id=base, action_entity_ids=["nearby-19"])
        readback = delivered["execution_readback"]
        self.assertIn({"id": "nearby-19", "version": 2, "discovery_state": {},
            "read_request": {"tool": "grove_inspect", "arguments": {"entity_id": "nearby-19"}}},
            readback["entities"])
        self.assertEqual(len(readback["entities"]), 1)
        self.assertGreater(readback["projection"]["omitted_changed_or_new_entity_count"], 0)
        self.assertEqual(readback["changes"], scene["changes"])
        versions = self.session.agent_observation(delivered["observation_id"])["versions"]["entity"]
        self.assertEqual(versions["nearby-19"], 2)
        self.assertEqual(versions["nearby-18"], 1)
        self.assertNotIn("new-but-omitted", versions)
        self.assertEqual(self.session.agent_observation(base)["versions"]["entity"]["nearby-19"], 1)

    def test_compact_action_keeps_selected_state_and_explicit_inspect_action_arguments(self):
        inspection = {"player": {"id": "test-user", "version": 7},
            "entity": {"id": "hearth", "version": 22, "state": {"temperature": 8}, "pour_target_slot": "surface"},
            "action_options": [{"action": "pour", "arguments": {
                "action": "pour", "target_id": "hearth", "target_slot": "surface"},
                "input_schema": {"type": "object", "properties": {"amount": {"type": "integer"}}}}],
            "action_options_projection": {"scope": "selected_declared_action", "action": "pour",
                                          "expanded_actions": ["pour"]}}
        explicit = self.session.present_agent_result("grove.inspect", inspection)
        self.assertEqual(explicit["action_options"][0]["arguments"]["target_slot"], "surface")
        delivered = self.session.present_agent_result("grove.interact", {"result": {},
            "execution_readback": {"selected_inspection": inspection}}, base_id=explicit["observation_id"])
        selected = delivered["execution_readback"]["selected_inspection"]
        self.assertEqual(selected["entity"]["state"], {"temperature": 8})
        self.assertEqual(selected["entity"]["pour_target_slot"], "surface")
        self.assertEqual(self.session.agent_observation(delivered["observation_id"])["pour_target_slots"], {"hearth": "surface"})
        self.assertNotIn("action_options", selected)
        self.assertEqual(selected["read_request"]["arguments"], {"entity_id": "hearth"})

    def test_pour_binds_original_presented_slot_without_refreshing_versions(self):
        self.declare_agent_catalog({"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}})
        observed = self.session.present_agent_result("map.viewport", {
            "player": {"id": "test-user", "version": 7}, "entities": [
                {"id": "target", "version": 22, "pour_target_slot": "contained"},
                {"id": "source", "version": 8}]})["observation_id"]
        original = self.session.agent_observation(observed)
        arguments = {"action": "pour", "amount": 20, "target_id": "target", "item_id": "source"}
        bound = self.session.bind_agent_versions("grove.interact", arguments, observed)
        self.assertEqual(bound["target_slot"], "contained")
        self.assertEqual((bound["expected_player_version"], bound["expected_target_version"], bound["expected_item_version"]), (7, 22, 8))
        self.assertNotIn("target_slot", arguments)
        explicit = self.session.bind_agent_versions("grove.interact", arguments | {"target_slot": "contained"}, observed)
        self.assertEqual(explicit["target_slot"], "contained")
        fresh = self.session.present_agent_result("grove.inspect", {
            "entity": {"id": "target", "version": 23, "pour_target_slot": "surface"}}, base_id=observed)["observation_id"]
        self.assertEqual(self.session.agent_observation(observed), original)
        self.assertEqual(self.session.bind_agent_versions("grove.interact", arguments, observed)["target_slot"], "contained")
        latest = self.session.bind_agent_versions("grove.interact", arguments, fresh)
        self.assertEqual((latest["target_slot"], latest["expected_target_version"]), ("surface", 23))
        with self.assertRaises(ConnectorError) as rejected:
            self.session.bind_agent_versions("grove.interact", arguments | {"target_slot": "contained"}, fresh)
        self.assertEqual(rejected.exception.code, "ObservedActionMismatch")
        implicit = self.session.bind_agent_versions("grove.interact", {
            "action": "pour", "target_id": "target", "item_id": "source"}, observed)
        self.assertNotIn("target_slot", implicit)
        tool = next(row for row in self.session.tools() if row["name"] == "grove_interact")
        self.assertIn("pour_target_slot", tool["inputSchema"]["properties"]["target_slot"]["description"])

    def test_pour_requires_presented_slot_and_does_not_derive_it_from_state(self):
        self.declare_agent_catalog({"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}})
        observed = self.session.present_agent_result("map.viewport", {
            "player": {"id": "test-user", "version": 7}, "entities": [
                {"id": "target", "version": 22, "capabilities": ["container"],
                    "state": {"open": True, "burning": False, "water_capacity": 40}},
                {"id": "source", "version": 8}]})["observation_id"]
        arguments = {"action": "pour", "amount": 20, "target_id": "target", "item_id": "source"}
        for attempt in (arguments, arguments | {"target_slot": "contained"}):
            with self.subTest(attempt=attempt), self.assertRaises(ConnectorError) as rejected:
                self.session.bind_agent_versions("grove.interact", attempt, observed)
            self.assertEqual(rejected.exception.code, "ObservedActionRequired")
        actual = self.session.present_agent_result("grove.inspect", {
            "entity": {"id": "target", "version": 22, "pour_target_slot": "contained"}}, base_id=observed)["observation_id"]
        newer_without_slot = self.session.present_agent_result("grove.inspect", {
            "entity": {"id": "target", "version": 23}}, base_id=actual)["observation_id"]
        with self.assertRaises(ConnectorError) as rejected:
            self.session.bind_agent_versions("grove.interact", arguments, newer_without_slot)
        self.assertEqual(rejected.exception.code, "ObservedActionRequired")

    async def test_mcp_capacity_rejection_preserves_action_facts_without_new_observation(self):
        transfer = {"source_id": "source", "target_id": "target", "target_slot": "surface",
            "requested": 20, "source_remaining": 20, "target_remaining_capacity": 12,
            "maximum": 12, "unit": "u", "meaning": "Current rejection; no water was transferred."}
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("RuleViolation", "Capacity insufficient", 409,
            world_details={"water_transfer": transfer | {"provider_trace": "private-diagnostic"},
                "upstream": "private-diagnostic"}))
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8891") as client:
            response = await client.post("/connector/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "grove_interact", "arguments": {}}},
                headers={"Authorization": "Bearer test-capability"})
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        error = result["structuredContent"]
        self.assertEqual(error["details"], {"water_transfer": transfer})
        self.assertEqual(json.loads(result["content"][0]["text"]), error)
        self.assertNotIn("private-diagnostic", json.dumps(result))
        self.assertEqual(self.session.observations.sequence, 0)

    async def test_permission_recovery_requires_actual_registered_read(self):
        original_call = self.session.tool_call
        self.session.tool_call = AsyncMock(side_effect=ConnectorError("PermissionDenied", "Source must be held", 403))
        self.declare_agent_catalog({"map.viewport": {"access": "read", "input_schema": {
            "type": "object", "properties": {}, "additionalProperties": False}}})
        app = create_connector_app(storage=self.manager.storage, wrapper=None)
        app.state.connector.capability_session = Mock(return_value=self.session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "grove_rule_action", "arguments": {"target_id": "wheel", "item_id": "wood"}}}
            result = (await client.post("/connector/mcp", json=body,
                headers={"Authorization": "Bearer test-capability"})).json()["result"]
            error = result["structuredContent"]
            self.assertTrue(result["isError"])
            self.assertNotIn("observation_id", error)
            self.assertEqual(self.session.observations.sequence, 0)
            read = error["read_requests"][0]
            self.assertIn(read["tool"], {tool["name"] for tool in self.session.tools()})
            self.session.tool_call = original_call
            self.session.status = "thinking"
            self.session.invoke = AsyncMock(return_value={"ok": True, "read_only": True, "function_id": "map.viewport",
                "function_version": 1, "snapshot_seq": 0, "result": {
                    "player": {"id": "test-user", "version": 7}, "entities": []}})
            body["params"] = {"name": read["tool"], "arguments": read["arguments"]}
            current = (await client.post("/connector/mcp", json=body,
                headers={"Authorization": "Bearer test-capability"})).json()["result"]
        self.assertFalse(current["isError"])
        observation = current["structuredContent"]["observation_id"]
        self.assertEqual(self.session.agent_observation(observation)["versions"]["player"]["test-user"], 7)
        self.session.invoke.assert_awaited_once_with("map.viewport", {})

    def test_pour_slot_is_retained_in_final_indexes_but_never_bound_from_omitted_payload(self):
        visible = {"id": "selected", "version": 5, "pour_target_slot": "surface"}
        supplied = self.session.present_agent_result("grove.interact", {
            "operation_id": "actual-write", "result": {"entity": {
                "id": "unshown", "version": 99, "pour_target_slot": "contained", "state": {"large": "x" * 80000}}},
            "execution_readback": {"selected_inspection": {"entity": visible}}})
        self.assertEqual(supplied["execution_readback"]["selected_inspection"]["entity"]["pour_target_slot"], "surface")
        self.assertEqual(self.session.agent_observation(supplied["observation_id"])["pour_target_slots"], {"selected": "surface"})
        bound = self.session.observations.bind("grove.interact", {
            "action": "pour", "target_id": "selected", "amount": 1}, supplied["observation_id"],
            {"properties": {"expected_target_version": {}}})
        self.assertEqual((bound["expected_target_version"], bound["target_slot"]), (5, "surface"))
        rows = [{"id": f"visible-{index}", "version": 1, "pour_target_slot": "contained"} for index in range(8)]
        rows.append({"id": "omitted", "version": 7, "pour_target_slot": "surface"})
        delta = self.session.present_agent_result("grove.move", {"result": {}, "execution_readback": {"entities": rows}})
        self.assertEqual(delta["execution_readback"]["entities"], [])
        observed = self.session.agent_observation(delta["observation_id"])
        self.assertEqual(observed["versions"]["entity"], {})
        self.assertFalse(observed.get("pour_target_slots"))
        for row in rows:
            with self.subTest(entity_id=row["id"]):
                with self.assertRaises(ConnectorError) as denied:
                    self.session.observations.bind("grove.interact", {
                        "action": "pour", "target_id": row["id"], "amount": 1}, delta["observation_id"],
                        {"properties": {"expected_target_version": {}}})
                self.assertEqual(denied.exception.code, "ObservedActionRequired")

    async def test_unknown_publication_stops_new_generation_without_model_call(self):
        self.session.unknown = [{"envelope": {"operation_id": "original-publication"}}]
        self.session.blueprint_request = AsyncMock()
        self.session.invoke = AsyncMock()
        with self.assertRaises(ConnectorError) as denied:
            await self.session.generate("Make another box", kind="layout")
        self.assertEqual(denied.exception.code, "UnknownWrite")
        self.session.blueprint_request.assert_not_awaited()
        self.session.invoke.assert_not_awaited()

    def test_blueprint_cannot_invent_coordinates_or_publish_invalid_wrapped_text(self):
        schema = next(branch for branch in CONTENT_SCHEMA["oneOf"]
                      if branch["properties"]["kind"]["const"] == "layout")
        payload = {"kind": "layout", "content_schema": schema,
                   "allowed_tiles": [{"x": 1, "y": 2}]}
        with self.assertRaises(ConnectorError):
            GroveSession.parse_blueprint('```json\n{}\n```', payload)
        with self.assertRaises(ConnectorError):
            GroveSession.parse_blueprint('{"kind":"layout","objects":'
                '[{"template":"stone","x":100,"y":100}]}', payload)

    def test_one_complete_provider_wrapper_keeps_schema_and_coordinate_checks(self):
        schema = next(branch for branch in CONTENT_SCHEMA["oneOf"]
                      if branch["properties"]["kind"]["const"] == "layout")
        payload = {"kind": "layout", "content_schema": schema,
                   "allowed_tiles": [{"x": 1, "y": 2}]}
        content = {"kind": "layout", "objects": [{"template": "wood_crate", "x": 1, "y": 2,
                    "contents": [{"template": "stone"}, {"template": "stone"}]}]}
        raw = json.dumps(content)
        wrappers = ['```json\n'+raw+'\n```', '<longcat_tool_call>grove_content_submit\n'
                    '<longcat_arg_key>content</longcat_arg_key>\n<longcat_arg_value>'+raw+
                    '</longcat_arg_value>\n</longcat_tool_call>']
        for wrapped in wrappers:
            self.assertEqual(GroveSession.parse_blueprint(wrapped, payload), content)
            with self.assertRaises(ConnectorError):
                GroveSession.parse_blueprint('Publish this now.\n'+wrapped, payload)
        with self.assertRaises(ConnectorError):
            GroveSession.parse_blueprint(wrappers[1].replace('grove_content_submit', 'grove_move'), payload)

    def test_correction_request_counts_against_automatic_model_budget(self):
        self.session.active_automatic = True
        self.session.automatic.update(enabled=True, max_count=1, count=0)
        self.session.inference_started("generation")
        with self.assertRaises(ConnectorError) as denied:
            self.session.inference_started("generation_correction")
        self.assertEqual(denied.exception.code, "AutomaticBudget")
        self.assertEqual(self.session.inference_count, 1)
        self.assertFalse(self.session.automatic["enabled"])

    async def test_blueprint_history_is_isolated_while_main_identity_and_count_continue(self):
        answer = '{"kind":"layout","objects":[{"template":"stone","x":1,"y":2}]}'
        completed = {"info": {"id": "completed-blueprint", "role": "assistant",
            "finish": "stop", "time": {"completed": 1}},
            "parts": [{"type": "text", "text": answer}]}
        self.session.inference_count = 8
        self.session.opportunity = {"opportunity_id": "original-one"}
        self.session.generation = {"attempts": 0}
        self.session.host = AsyncMock(side_effect=[
            {"id": "blueprint-one", "permission": self.session.blueprint_permissions()},
            completed, completed,
            {"id": "blueprint-two", "permission": self.session.blueprint_permissions()}])
        await self.session.new_blueprint_session("original-one")
        self.assertEqual(await self.session.blueprint_request({}, correction=False, timeout=5), answer)
        await self.session.blueprint_request({}, correction=True, timeout=5)
        self.assertEqual(self.session.session_id, "test-session")
        self.assertEqual(self.session.inference_count, 10)
        calls = self.session.host.await_args_list
        self.assertEqual([call.args[1] for call in calls[1:3]],
                         ["/session/blueprint-one/message"] * 2)
        self.assertNotIn("parentID", calls[0].args[2])
        self.assertTrue(all(item["session_id"] == "blueprint-one"
                            for item in self.session.inference_history))
        self.session.opportunity = {"opportunity_id": "original-two"}
        await self.session.new_blueprint_session("original-two")
        self.assertEqual(self.session.blueprint_session_id, "blueprint-two")
        self.assertEqual(self.session.inference_count, 10)
        self.session.host.side_effect = None
        self.session.host.return_value = {}
        self.session.release_opportunity = AsyncMock()
        await self.session.cancel()
        aborts = {call.args[1] for call in self.session.host.await_args_list if call.args[1].endswith('/abort')}
        self.assertEqual(aborts, {"/session/test-session/abort", "/session/blueprint-two/abort"})
        self.assertEqual(self.session.execution['request_status'], 'unsettled')

    def test_opencode_duplicate_denials_do_not_mask_changed_permissions(self):
        expected = self.session.permissions()
        self.assertTrue(session_permissions_match([expected[0], *expected], expected))
        self.assertFalse(session_permissions_match([*expected, expected[0]], expected))
        self.assertFalse(session_permissions_match([*expected,
            {"permission": "bash", "pattern": "*", "action": "allow"}], expected))

    async def test_saved_session_internal_diagnostic_upgrade_preserves_original_conversation(self):
        expected = self.session.permissions()
        previous = expected[:-1]
        self.session.restore_permissions = True
        self.session.inference_count = 7
        self.session.messages = [{"role": "user", "text": "Keep my original preference."}]
        self.session.remember()
        original = self.session.memory_file.read_bytes()
        # Native PATCH can append rules rather than replacing the old copies.
        self.session.host = AsyncMock(side_effect=[
            {"id": "test-session", "permission": [previous[0], *previous]},
            {"id": "test-session"},
            {"id": "test-session", "permission": [*previous, *expected]}])
        await self.session.recover_session_permissions()
        self.assertEqual([call.args for call in self.session.host.await_args_list], [
            ("GET", "/session/test-session"),
            ("PATCH", "/session/test-session", {"permission": expected}),
            ("GET", "/session/test-session")])
        self.assertEqual(expected[-1], {"permission": "invalid", "pattern": "*", "action": "allow"})
        self.assertEqual((self.session.session_id, self.session.inference_count), ("test-session", 7))
        self.assertEqual(self.session.messages, [{"role": "user", "text": "Keep my original preference."}])
        self.assertEqual(self.session.memory_file.read_bytes(), original)
        self.assertFalse(self.session.restore_permissions)

    async def test_current_session_diagnostic_permissions_are_read_only_and_idempotent(self):
        expected = self.session.permissions()
        self.session.host = AsyncMock(return_value={"id": "test-session", "permission": expected})
        await self.session.recover_session_permissions()
        await self.session.recover_session_permissions()
        self.assertEqual([call.args for call in self.session.host.await_args_list],
            [("GET", "/session/test-session")] * 2)

    async def test_internal_diagnostic_upgrade_rejects_foreign_or_changed_rules_even_with_restore_marker(self):
        expected = self.session.permissions()
        previous = expected[:-1]
        for session in (
                {"id": "foreign-session", "permission": previous},
                {"id": "test-session", "permission": [*previous,
                    {"permission": "bash", "pattern": "*", "action": "allow"}]},
                {"id": "test-session", "permission": [*expected, previous[0]]},
                {"id": "test-session", "permission": [*previous,
                    {"permission": "invalid", "pattern": "*", "action": "deny"}]},
                {"id": "test-session", "permission": [{"permission": "*", "pattern": "*", "action": "allow"}]},
                {"id": "test-session"}, None):
            with self.subTest(session=session):
                self.session.restore_permissions = True
                self.session.host = AsyncMock(return_value=deepcopy(session))
                with self.assertRaises(ConnectorError) as rejected:
                    await self.session.recover_session_permissions()
                self.assertEqual(rejected.exception.code, "UnsafeSession")
                self.session.host.assert_awaited_once_with("GET", "/session/test-session")
                self.assertEqual(self.session.session_id, "test-session")
                self.assertTrue(self.session.restore_permissions)

    async def test_internal_diagnostic_upgrade_requires_readback_not_patch_acknowledgement(self):
        expected = self.session.permissions()
        previous = expected[:-1]
        self.session.restore_permissions = True
        self.session.host = AsyncMock(side_effect=[
            {"id": "test-session", "permission": previous},
            {"id": "test-session", "permission": expected},
            {"id": "test-session", "permission": previous}])
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.recover_session_permissions()
        self.assertEqual(rejected.exception.code, "UnsafeSession")
        self.assertEqual([call.args[0] for call in self.session.host.await_args_list], ["GET", "PATCH", "GET"])
        self.assertTrue(self.session.restore_permissions)
        self.assertEqual(self.session.session_id, "test-session")

    def test_packaged_browser_entry_and_foreign_origin_failure(self):
        app = create_connector_app(storage=self.manager.storage)
        with TestClient(app, base_url="http://127.0.0.1:8891") as client:
            self.assertIn("world-canvas", client.get("/").text)
            self.assertEqual(client.get("/grove-static/grove.js").status_code, 200)
            self.assertEqual(client.get("/health", headers={"Origin": "https://elsewhere.example"}).status_code, 403)
            self.assertEqual(client.get("/connector/state").status_code, 401)


if __name__ == "__main__":
    unittest.main()
