"""Falsifiable slice: a regular key holder can use Commons and resume after restart.

No operator login, model, production credential, or external deployment is used.
"""
from pathlib import Path
import tempfile
import time
import unittest

import httpx
from fastapi.testclient import TestClient

from agent_world.application import create_application
from tests.live_server import LiveServer
from tests.test_key_identity import keypair, sign


class CommonsExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'world.sqlite3'
        self.app = create_application(self.db, world_profile='commons', universe='commons', timers_enabled=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def enter(self, pair=None):
        private, public = pair or keypair()
        challenge = self.client.post('/v1/key-identities/challenges', json={'public_key': public}).json()
        result = self.client.post('/v1/key-identities/exchange', json={
            'challenge_id': challenge['challenge_id'], 'signature': sign(private, challenge),
        })
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        return data, {'Authorization': 'Bearer ' + data['identity']['token']}, (private, public)

    def call(self, name, auth, arguments, operation=None):
        body = {'arguments': arguments}
        if operation:
            body['operation_id'] = operation
        return self.client.post('/v1/functions/' + name + '/invoke', headers=auth, json=body)

    def test_public_entry_and_own_profile_without_operator(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Come back to', page.text)
        self.assertIn("frame-ancestors 'none'", page.headers['content-security-policy'])
        self.assertEqual(self.client.get('/api/config').status_code, 200)
        self.assertEqual(self.client.get('/api/me').status_code, 401)
        identity, auth, _ = self.enter()
        updated = self.client.patch('/api/me', headers=auth, json={'display_name': 'Lantern'})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['role_id'], identity['role_profile']['role_id'])
        self.assertEqual(self.client.get('/api/me', headers=auth).json()['display_name'], 'Lantern')
        self.assertEqual(self.client.patch('/api/me', headers=auth, json={'display_name': 'X', 'role_id': 'someone-else'}).status_code, 422)
        self.assertEqual(self.client.patch('/api/me', headers={**auth, 'Origin': 'https://untrusted.example'}, json={'display_name': 'X'}).status_code, 403)
        self.assertEqual(self.client.get('/api/me', headers=[('Authorization', auth['Authorization']), ('Authorization', auth['Authorization'])]).status_code, 422)
        self.assertEqual(self.client.post('/api/roles', json={'display_name': 'operator bypass'}).status_code, 503)

    def test_browser_key_has_bounded_credential_and_expired_revoked_observers_cannot_write(self):
        identity, auth, pair = self.enter()
        token = identity['identity']
        self.assertAlmostEqual(token['expires_at'] - token['created_at'], 86400, delta=1)
        runtime = self.app.state.runtime
        role = identity['role_profile']['role_id']
        observe = runtime.issue_identity_token('commons', role, access_mode='observe')
        read_auth = {'Authorization': 'Bearer ' + observe['token']}
        self.assertEqual(self.client.get('/api/me', headers=read_auth).status_code, 200)
        self.assertEqual(self.client.patch('/api/me', headers=read_auth, json={'display_name': 'forbidden'}).status_code, 403)
        runtime.revoke_identity_token(token['token_id'])
        self.assertEqual(self.client.get('/v1/bootstrap', headers=auth).status_code, 401)
        restored, restored_auth, _ = self.enter(pair)
        self.assertEqual(restored['role_profile']['role_id'], role)
        with runtime._conn() as conn:
            conn.execute('UPDATE identity_tokens SET expires_at=? WHERE token_id=?', (time.time() - 1, restored['identity']['token_id']))
        self.assertEqual(self.client.get('/v1/bootstrap', headers=restored_auth).status_code, 401)
        again, _, _ = self.enter(pair)
        self.assertEqual(again['role_profile']['role_id'], role)

    def test_detail_outside_recent_feed_private_isolation_and_unknown_write_replay(self):
        alice, auth_a, _ = self.enter()
        bob, auth_b, _ = self.enter()
        _, auth_c, _ = self.enter()
        posted = self.call('commons.post.create', auth_a, {'post_id': 'original', 'text': '<script>data, not HTML</script>'}, 'same-operation')
        self.assertEqual(posted.status_code, 200)
        repeated = self.call('commons.post.create', auth_a, {'post_id': 'original', 'text': '<script>data, not HTML</script>'}, 'same-operation')
        self.assertTrue(repeated.json()['replayed'])
        self.call('commons.post.create', auth_a, {'post_id': 'newer', 'text': 'newer'}, 'newer')
        self.assertEqual(self.call('commons.post.list', auth_b, {'limit': 1}).json()['result']['posts'][0]['post_id'], 'newer')
        self.assertEqual(self.call('commons.post.get', auth_b, {'post_id': 'original'}).json()['result']['post']['text'], '<script>data, not HTML</script>')
        self.call('commons.reply.create', auth_b, {'post_id': 'original', 'reply_id': 'r', 'text': 'Hello'}, 'reply')
        self.call('commons.conversation.open', auth_a, {'conversation_id': 'private', 'peer_role_id': bob['role_profile']['role_id']}, 'open')
        self.call('commons.message.send', auth_b, {'conversation_id': 'private', 'message_id': 'm', 'text': 'Only us'}, 'message')
        self.assertEqual(self.call('commons.message.list', auth_a, {'conversation_id': 'private'}).json()['result']['messages'][0]['text'], 'Only us')
        self.assertEqual(self.call('commons.message.list', auth_c, {'conversation_id': 'private'}).status_code, 403)
        self.assertEqual(self.call('commons.message.send', auth_c, {'conversation_id': 'private', 'message_id': 'intruder', 'text': 'No'}, 'intruder').status_code, 403)
        self.assertEqual(self.call('commons.conversation.list', auth_c, {}).json()['result']['conversations'], [])


class CommonsProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_new_server_process_new_mcp_session_recovers_public_and_private_history(self):
        with LiveServer('commons') as server:
            alice, alice_token, private = server.key_identity()
            bob, bob_token, _ = server.key_identity()
            with httpx.Client(trust_env=False, timeout=5) as client:
                self.assertEqual(client.get(server.url + '/').status_code, 200)
                headers = {'Authorization': 'Bearer ' + alice_token['token']}
                client.patch(server.url + '/api/me', headers=headers, json={'display_name': 'Same person'}).raise_for_status()
                for name, args, operation in [
                    ('commons.post.create', {'post_id': 'restart-post', 'text': 'Before restart'}, 'p'),
                    ('commons.conversation.open', {'conversation_id': 'restart-chat', 'peer_role_id': bob['role_id']}, 'c'),
                ]:
                    client.post(server.url + '/v1/functions/' + name + '/invoke', headers=headers,
                                json={'arguments': args, 'operation_id': operation}).raise_for_status()
            async with server.session(bob_token) as (session, _):
                reply = await session.call_tool('commons.reply.create', arguments={'arguments': {'post_id': 'restart-post', 'reply_id': 'r', 'text': 'While away'}, 'operation_id': 'r'})
                self.assertFalse(reply.is_error)
                message = await session.call_tool('commons.message.send', arguments={'arguments': {'conversation_id': 'restart-chat', 'message_id': 'm', 'text': 'Private while away'}, 'operation_id': 'm'})
                self.assertFalse(message.is_error)
            server.restart()
            restored, token, _ = server.key_identity(private)
            self.assertEqual(restored['role_id'], alice['role_id'])
            self.assertEqual(restored['display_name'], 'Same person')
            async with server.session(token) as (session, _):
                bootstrap = await session.call_tool('world.bootstrap', arguments={})
                self.assertFalse(bootstrap.is_error)
                replies = await session.call_tool('commons.reply.list', arguments={'arguments': {'post_id': 'restart-post'}})
                self.assertEqual(replies.structured_content['result']['replies'][0]['text'], 'While away')
                messages = await session.call_tool('commons.message.list', arguments={'arguments': {'conversation_id': 'restart-chat'}})
                self.assertEqual(messages.structured_content['result']['messages'][0]['text'], 'Private while away')
