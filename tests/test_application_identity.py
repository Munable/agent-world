"""A non-Commons consumer must be connectable through the portable identity helper."""
from pathlib import Path
from contextlib import closing
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from agent_world.application import create_application
from agent_world.connect import connect_world
from agent_world import WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, TimerSpec
from agent_world.world_sdk import install_world
from agent_world.runtime_core import WorldRuntime


@unittest.skipUnless(os.name == "posix", "The connection helper requires POSIX private-file permissions")
class ApplicationIdentityTests(unittest.TestCase):
    def test_http_action_waits_for_same_hosts_slow_timer_and_replays_once(self):
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        writer_started, release = threading.Event(), threading.Event()

        def arm(ctx, args):
            ctx.set_state('values', 'counter', {'timer': 0, 'human': 0})
            ctx.schedule_timer('one', 'settle', {}, due_at=time.time())
            return FunctionOutcome({})

        def settle(ctx, args):
            value = ctx.get_state('values', 'counter')
            value['timer'] += 1
            ctx.set_state('values', 'counter', value)
            writer_started.set()
            if not release.wait(15):
                raise RuntimeError('test writer was not released')
            return FunctionOutcome(value)

        def act(ctx, args):
            value = ctx.get_state('values', 'counter')
            value['human'] += 1
            ctx.set_state('values', 'counter', value)
            return FunctionOutcome(value)

        world = WorldDefinition('shared-runtime-test', 'Shared Runtime',
            (FunctionSpec('test.arm', arm, empty), FunctionSpec('test.act', act, empty)),
            state_rules=(StateRule('values', '', {'type': 'object'}),),
            timers=(TimerSpec('settle', settle, empty),))
        with tempfile.TemporaryDirectory() as temp, \
                patch('agent_world.application.get_universe_installer',
                      return_value=lambda runtime, universe: install_world(runtime, universe, world)):
            app = create_application(Path(temp) / 'world.sqlite3', universe='u', timers_enabled=False)
            runtime = app.state.runtime
            role = runtime.create_role('Player')['role_id']
            token = runtime.issue_identity_token('u', role)['token']
            runtime.call_function('u', 'test.arm', role, {}, operation_id='arm', identity_token=token)
            failures = []

            def run_timer():
                try:
                    runtime.run_due_timers('u')
                except Exception as exc:
                    failures.append(type(exc).__name__)

            worker = threading.Thread(target=run_timer)
            worker.start()
            try:
                self.assertTrue(writer_started.wait(15))
                # Exceed SQLite's five-second busy wait. The composed adapter
                # must queue behind the existing Runtime lock, not compete.
                unlock = threading.Timer(5.4, release.set)
                unlock.start()
                try:
                    with TestClient(app) as client:
                        body = {'arguments': {}, 'operation_id': 'human-action'}
                        headers = {'Authorization': 'Bearer ' + token}
                        first = client.post('/v1/functions/test.act/invoke', headers=headers, json=body)
                        self.assertEqual(first.status_code, 200, first.text)
                        replay = client.post('/v1/functions/test.act/invoke', headers=headers, json=body)
                        self.assertTrue(replay.json()['replayed'])
                        self.assertEqual(first.json()['result'], {'timer': 1, 'human': 1})
                        self.assertEqual(replay.json()['result'], first.json()['result'])
                finally:
                    unlock.cancel()
            finally:
                release.set()
                worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            # The app releases its Runtime on shutdown. Recover through a new
            # handle to the same database, retaining the state/Receipt checks.
            with closing(WorldRuntime(runtime.db_path)) as recovered:
                self.assertEqual(recovered.get_state('u', 'values', 'counter')['value'],
                                 {'timer': 1, 'human': 1})
                self.assertEqual(recovered.get_receipt('u', role, 'human-action', identity_token=token)['result'],
                                 {'timer': 1, 'human': 1})

    def test_external_world_connection_is_bounded_and_same_key_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            app = create_application(
                root / "world.sqlite3", world_profile="examples.river_camp:WORLD",
                universe="camp-connection", public_base_url="http://127.0.0.1:8770",
            )
            with TestClient(app, base_url="http://127.0.0.1:8770") as client:
                first = connect_world(
                    "http://127.0.0.1:8770", key_path=root / "private" / "key.json",
                    output_path=root / "private" / "first.json", new_identity=True, client=client,
                )
                self.assertGreater(first.expires_at, time.time())
                self.assertLessEqual(first.expires_at, time.time() + 86401)
                config = json.loads(first.output_path.read_text())
                auth = config["mcp"]["agent_world"]["headers"]["Authorization"]
                joined = client.post("/v1/functions/camp.join/invoke", headers={"Authorization": auth},
                                     json={"arguments": {}, "operation_id": "camp-connect-join"})
                self.assertEqual(joined.status_code, 200)
                second = connect_world(
                    "http://127.0.0.1:8770", key_path=first.key_path,
                    output_path=root / "private" / "second.json", client=client,
                )
                self.assertEqual(first.role_id, second.role_id)
                self.assertEqual(first.public_key, second.public_key)
                next_auth = json.loads(second.output_path.read_text())["mcp"]["agent_world"]["headers"]["Authorization"]
                state = client.post("/v1/functions/camp.inspect/invoke", headers={"Authorization": next_auth},
                                    json={"arguments": {}})
                self.assertEqual(state.status_code, 200)
                self.assertEqual(state.json()["result"]["role_id"], first.role_id)


if __name__ == "__main__":
    unittest.main()
