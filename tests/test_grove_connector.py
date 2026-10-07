"""Local companion gates/recovery, without invoking a real model in unit tests."""
from pathlib import Path
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
from agent_world.worlds.shared_grove.catalog import CONTENT_SCHEMA


class GroveConnectorTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(facts['entities'][0]['state']['recipe'], 'walkway')
        self.assertEqual(facts['entities'][1]['state']['stock'], 0)
        self.session.host.assert_awaited_once()
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

    async def test_companion_cannot_move_or_act_as_person(self):
        self.session.catalog = {"grove.move": {"access": "write", "input_schema": {}},
                                "grove.handbook": {"access": "read", "input_schema": {}}}
        self.assertNotIn("grove_move", [t["name"] for t in self.session.tools()])
        self.session.status = "thinking"
        with self.assertRaises(ConnectorError) as denied:
            await self.session.tool_call("grove_move", {"dx": 1, "dy": 0})
        self.assertEqual(denied.exception.code, "ToolDenied")

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
        self.assertEqual({call.args[1] for call in self.session.host.await_args_list[-2:]},
                         {"/session/test-session/abort", "/session/blueprint-two/abort"})

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
