"""Local companion gates/recovery, without invoking a real model in unit tests."""
from pathlib import Path
from copy import deepcopy
import asyncio
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
)
from agent_world.grove_agent_observation import (
    companion_projection, _companion_chunk_terrain, _companion_scene_index, blueprint_model_input)
from agent_world.worlds.shared_grove.catalog import CONTENT_SCHEMA
from agent_world.worlds.shared_grove.world import INTERACT_ARGS


class GroveConnectorTests(unittest.IsolatedAsyncioTestCase):
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
        self.session.catalog = {"grove.join": {"access": "write"}}
        self.session.host = AsyncMock(return_value={
            "info": {"role": "assistant", "finish": "stop", "time": {"completed": 1}},
            "parts": [{"type": "text", "text": "先看看溪边能用的材料。"}]})
        with patch("agent_world.grove_connector.httpx.AsyncClient.request",
                   AsyncMock(side_effect=httpx.ReadTimeout("private transport details"))):
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
        original = self.session.unknown[0]["envelope"]["operation_id"]
        self.session.world = AsyncMock(return_value={"ok": True, "operation_id": original,
                                                   "result": {"joined": True}})
        await self.session.recover_receipts()
        self.session.world.assert_awaited_once_with("GET", "/v1/receipts/" + original)
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
        self.session.remember_user_request("这件东西为什么变重了？")
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
        self.session.catalog = {"grove.content_submit": {"access": "write"}}
        self.session.world = AsyncMock(side_effect=httpx.ReadError("connection lost"))
        with self.assertRaises(httpx.ReadError):
            await self.session.invoke("grove.content_submit", {"content": {"kind": "layout"}})
        original = self.session.unknown[0]["envelope"]
        self.assertTrue(original["operation_id"])
        self.assertNotIn("test-private-bearer", self.session.memory_file.read_text())
        self.session.world = AsyncMock(side_effect=[ConnectorError("ReceiptNotFound", "Missing", 404),
                                                   {"ok": True, "result": {"published": True}}])
        await self.session.recover_receipts()
        replay = self.session.world.await_args_list[1]
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
        self.session.join = AsyncMock()
        self.session.observe_automatic = AsyncMock()
        self.session.start_automatic_loop = Mock()
        self.session.watch_events = AsyncMock()
        self.session.launch = AsyncMock()
        self.session.stop_host = AsyncMock()
        self.session.turn = AsyncMock()
        calls = []

        async def recovering(method, path, body=None, timeout=15):
            calls.append((method, path))
            if path == '/global/health':
                if sum(path == item[1] for item in calls) == 1:
                    raise httpx.ReadTimeout('still initializing')
                return {'healthy': True}
            if path == '/config/providers':
                return {'providers': [{'id': 'provider', 'models': {'model': {}}}]}
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
        self.session.inference_count = 5
        self.session.remember()
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
        self.session.catalog = {'map.viewport': {'access': 'read'}}
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
        self.session.current_turn_facts = AsyncMock(return_value={'player': {'id': 'test-user'}, 'entities': []})
        self.session.host = AsyncMock(return_value={'info': {'role': 'assistant', 'finish': 'stop',
            'time': {'completed': 1}}, 'parts': [{'type': 'text', 'text': 'Look at the actual objects.'}]})
        await self.session.turn('What is here?')
        self.assertEqual(self.session.execution['request_status'], 'idle')
        await self.session.turn('What changed?')
        self.assertEqual(self.session.inference_count, 2)
        self.assertEqual(self.session.execution['requests_used'], 0)
        self.assertEqual(self.session.host.await_count, 2)

    async def test_timed_out_ordinary_question_keeps_original_native_gate_closed(self):
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
        self.assertEqual(self.session.execution['request_status'], 'unsettled')
        with self.assertRaises(ConnectorError) as blocked:
            await self.session.turn('A different question')
        self.assertEqual(blocked.exception.code, 'OriginalRequestUnsettled')
        self.assertEqual(self.session.inference_count, 1)

    async def test_failed_current_read_does_not_spend_model_request(self):
        self.session.entered = True
        self.session.catalog = {'map.viewport': {'access': 'read'}}
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
            "available_actions": ["store"], "contents": [], "public_content": {"version": 8}}}
        result = companion_projection("grove.inspect", original)["result"]
        self.assertNotIn("action_options", result["entity"])
        self.assertEqual(result["entity"]["field_references"]["action_options"], "#/action_options")
        self.assertEqual(result["action_options"], options)
        self.assertEqual(result["entity"]["simulation"], {"total": 160})
        self.assertEqual(result["public_content"]["version"], 8)
        self.assertEqual(result["public_content"]["projection"]["scope"], "public_content_index")
        self.assertIn("action_options", original["result"]["entity"])
        original["result"]["entity"]["available_actions"] = ["close"]
        original["result"]["entity"]["action_options"] = [{"action": "close"}]
        differing = companion_projection("grove.inspect", original)["result"]["entity"]
        self.assertEqual(differing["available_actions"], ["close"])
        self.assertEqual(differing["action_options"], [{"action": "close"}])

    def test_region_index_preserves_discovery_without_repeating_snapshots_or_action_previews(self):
        entity = {"id":"box", "version":7, "name":"Box", "x":3, "y":2,
            "state":{"open":False,"program":{"definition_ref":"immutable","contract":{"status":"active"}}},
            "matter":[{"id":"original-lot","quantity":2}], "simulation":{"total":160}}
        options = [{"action":"store","available":False,"reason":"Overloaded",
            "input":{"candidates":[{"id":"item","preview":{"available":False,"physics":{"overloaded":True}}}]}}]
        public = {"id":"content:0:0", "region":"0:0", "version":8,
            "history":{"cursor":"older-page", "outcome_count":22},
            "anchors":[{"entity_id":"box","version":7,"entity":entity}],
            "contributions":[{"id":"source","text":"historical text"}], "outcome_refs":[{"id":"receipt"}]}
        original = {"result":{"entity":entity,"action_options":options,"available_actions":[],"public_content":public}}
        actual = companion_projection("grove.inspect", original)["result"]
        self.assertEqual(actual["entity"],entity)
        self.assertEqual(actual["action_options"],options)
        index = actual["public_content"]
        self.assertEqual(index["history"],public["history"])
        self.assertEqual(index["anchors"][0]["entity_id"],"box")
        self.assertNotIn("entity",index["anchors"][0])
        self.assertNotIn("contributions",index)
        self.assertIn("no cursor",index["projection"]["meaning"])
        self.assertEqual(original["result"]["public_content"],public)
        folded = blueprint_model_input({"public_content":index,"public_entities":[entity]})
        self.assertEqual(folded["public_entities"],[entity])

    async def test_selected_inspection_is_authorized_before_model_request(self):
        self.session.entered = True
        self.session.catalog = {"map.viewport": {"access": "read"}, "grove.inspect": {"access": "read"}}
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
        self.assertEqual(facts["selected_inspection"]["action_options"][0]["input"]["candidates"][0]["preview"], preview)

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
        self.session.catalog = {name: {"access": "read"} for name in ("map.viewport", "grove.inspect")}
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
        self.session.catalog = {"grove.move": {"access": "write", "input_schema": {}},
                                "grove.handbook": {"access": "read", "input_schema": {}}}
        self.assertIn("grove_move", [t["name"] for t in self.session.tools()])
        self.session.status = "thinking"
        self.session.invoke = AsyncMock()
        with self.assertRaises(ConnectorError) as denied:
            await self.session.tool_call("grove_move", {"dx": 1, "dy": 0})
        self.assertEqual(denied.exception.code, "DelegationRequired")
        self.session.invoke.assert_not_awaited()

    async def test_execution_keeps_catalog_on_demand_and_closes_writes_before_goal_settlement(self):
        self.session.catalog = {"grove.rule_catalog": {"access": "read"}}
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
        self.session.catalog = {name: {"access": "read"} for name in ("map.viewport", "grove.inspect")}
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
        self.assertEqual(facts["selected_inspection"]["action_options"][0]["input"]["candidates"][0]["preview"], preview)

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
        self.assertEqual(readback["entities"], [])
        self.assertEqual(readback["construction"]["material_totals"], {"wood": 12})
        self.assertEqual(readback["inventory"][0]["matter"][0]["quantity"], 12)
        self.assertNotIn("action_options", readback["inventory"][0])
        self.assertEqual(readback["inventory_total"], 7)
        self.assertEqual(readback["inventory_next"], "own-source")
        self.assertNotIn("chunks", readback)
        self.assertNotIn("public_content", readback)
        self.assertEqual(delivered["read_requests"], [{"tool": "map_viewport", "arguments": {}}])
        self.assertLess(len(json.dumps(delivered, ensure_ascii=False)), len(json.dumps(expanded, ensure_ascii=False)) // 3)
        versions = self.session.agent_observation(delivered["observation_id"])["versions"]
        self.assertEqual(versions["player"]["test-user"], 2)
        self.assertEqual(versions["entity"]["own-source"], 5)
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
        self.assertIn({"id": "nearby-19", "version": 2, "discovery_state": {}}, readback["entities"])
        self.assertEqual(len(readback["entities"]), 9)
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
                "input_schema": {"type": "object", "properties": {"amount": {"type": "integer"}}}}]}
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
        self.session.catalog = {"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}}
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
        self.session.catalog = {"grove.interact": {"access": "write", "input_schema": INTERACT_ARGS}}
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

    def test_pour_slot_is_retained_in_final_indexes_but_never_bound_from_omitted_payload(self):
        visible = {"id": "selected", "version": 5, "pour_target_slot": "surface"}
        supplied = self.session.present_agent_result("grove.interact", {
            "operation_id": "actual-write", "result": {"entity": {
                "id": "unshown", "version": 99, "pour_target_slot": "contained", "state": {"large": "x" * 80000}}},
            "execution_readback": {"selected_inspection": {"entity": visible}}})
        self.assertEqual(supplied["execution_readback"]["selected_inspection"]["entity"]["pour_target_slot"], "surface")
        self.assertEqual(self.session.agent_observation(supplied["observation_id"])["pour_target_slots"], {"selected": "surface"})
        rows = [{"id": f"visible-{index}", "version": 1, "pour_target_slot": "contained"} for index in range(8)]
        rows.append({"id": "omitted", "version": 7, "pour_target_slot": "surface"})
        delta = self.session.present_agent_result("grove.move", {"result": {}, "execution_readback": {"entities": rows}})
        self.assertEqual(delta["execution_readback"]["entities"][0]["pour_target_slot"], "contained")
        self.assertNotIn("omitted", self.session.agent_observation(delta["observation_id"])["pour_target_slots"])

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

    def test_packaged_browser_entry_and_foreign_origin_failure(self):
        app = create_connector_app(storage=self.manager.storage)
        with TestClient(app, base_url="http://127.0.0.1:8891") as client:
            self.assertIn("world-canvas", client.get("/").text)
            self.assertEqual(client.get("/grove-static/grove.js").status_code, 200)
            self.assertEqual(client.get("/health", headers={"Origin": "https://elsewhere.example"}).status_code, 403)
            self.assertEqual(client.get("/connector/state").status_code, 401)


if __name__ == "__main__":
    unittest.main()
