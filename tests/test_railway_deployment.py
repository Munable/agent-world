"""Deployment adapter tests use only disposable databases and synthetic keys."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_world.application import create_application
from deploy import railway
from tests.test_key_identity import keypair, sign

ORIGIN = 'https://commons.example'


class RailwayConfigurationTests(unittest.TestCase):
    def env(self, **changes):
        return {'RAILWAY_VOLUME_MOUNT_PATH': '/data', 'RAILWAY_PUBLIC_DOMAIN': 'commons.example', **changes}

    def test_public_domain_and_port(self):
        self.assertEqual(railway.configuration(self.env(PORT='43123')), (ORIGIN, 43123))
        self.assertEqual(railway.configuration(self.env()), (ORIGIN, 8080))

    def test_explicit_https_origin_wins(self):
        self.assertEqual(railway.configuration(self.env(WORLD_PUBLIC_URL='https://custom.example:443/')),
                         ('https://custom.example', 8080))

    def test_rejects_missing_or_unsafe_origin(self):
        for value in ('', 'http://commons.example', 'https://localhost', 'https://127.0.0.1',
                      'https://name:secret@commons.example', 'https://@commons.example',
                      'https://commons.example/path', 'https://commons.example?token=secret',
                      'https://commons.example#x', 'https://commons.example:8000',
                      'https://bad..example', 'https://*.example', 'https://bad\n.example'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                railway.configuration({'RAILWAY_VOLUME_MOUNT_PATH': '/data', 'WORLD_PUBLIC_URL': value})

    def test_requires_persistent_mount_configuration(self):
        for value in ('', '/app', '/data/'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                railway.configuration(self.env(RAILWAY_VOLUME_MOUNT_PATH=value))

    def test_rejects_operator_credentials(self):
        for variable in ('WORLD_WEB_PASSWORD', 'WORLD_OPERATOR_KEY'):
            with self.subTest(variable=variable), self.assertRaises(ValueError):
                railway.configuration(self.env(**{variable: 'synthetic-test-value'}))

    def test_rejects_invalid_port(self):
        for value in ('0', '-1', '65536', 'not-a-port'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                railway.configuration(self.env(PORT=value))

    def test_refuses_unmounted_storage_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(railway, 'DATA_ROOT', Path(temp)):
            with self.assertRaisesRegex(ValueError, 'actual mounted'):
                railway.prepare_volume()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_owner_only_storage_and_umask(self):
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as temp, patch.object(railway, 'DATA_ROOT', Path(temp)), \
                patch.object(Path, 'is_mount', return_value=True), patch.object(os, 'geteuid', return_value=10001):
            db = railway.prepare_volume()
            db.write_text('disposable')
            self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)
            self.assertEqual(db, Path(temp) / 'commons' / 'commons.sqlite3')

    def test_root_drops_groups_gid_uid_before_application(self):
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as temp, patch.object(railway, 'DATA_ROOT', Path(temp)), \
                patch.object(Path, 'is_mount', return_value=True), patch.object(os, 'geteuid', return_value=0), \
                patch.object(os, 'chown') as chown, patch.object(os, 'setgroups') as groups, \
                patch.object(os, 'setgid') as gid, patch.object(os, 'setuid') as uid:
            railway.prepare_volume()
            chown.assert_called_once_with(Path(temp) / 'commons', 10001, 10001)
            groups.assert_called_once_with([])
            gid.assert_called_once_with(10001)
            uid.assert_called_once_with(10001)

    def test_refuses_storage_symlinks(self):
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as temp, patch.object(railway, 'DATA_ROOT', Path(temp)), \
                patch.object(Path, 'is_mount', return_value=True):
            (Path(temp) / 'commons').symlink_to(Path(temp), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                railway.prepare_volume()

    def test_server_is_single_process_and_does_not_trust_forwarded_headers(self):
        with patch.dict(os.environ, self.env(PORT='43123'), clear=True), \
                patch.object(railway, 'prepare_volume', return_value=Path('/data/commons/commons.sqlite3')), \
                patch('agent_world.application.create_application') as create, patch('uvicorn.run') as run:
            railway.main()
            self.assertEqual(create.call_args.kwargs['web_password'], None)
            self.assertEqual(create.call_args.kwargs['public_base_url'], ORIGIN)
            self.assertEqual(run.call_args.kwargs['host'], '0.0.0.0')
            self.assertEqual(run.call_args.kwargs['port'], 43123)
            self.assertEqual(run.call_args.kwargs['workers'], 1)
            self.assertFalse(run.call_args.kwargs['proxy_headers'])


class RailwayPublicSurfaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'commons.sqlite3'
        app = create_application(self.db, world_profile='commons', universe='commons',
                                 public_base_url=ORIGIN, timers_enabled=False)
        # Emulate Railway's plain-HTTP hop after TLS termination.
        self.client = self.enterContext(TestClient(railway.CommonsPublicSurface(app, ORIGIN),
                                                  base_url='http://commons.example'))

    def enter(self):
        private, public = keypair()
        challenge = self.client.post('/v1/key-identities/challenges', headers={'Origin': ORIGIN},
                                     json={'public_key': public})
        self.assertEqual(challenge.status_code, 200, challenge.text)
        response = self.client.post('/v1/key-identities/exchange', headers={'Origin': ORIGIN}, json={
            'challenge_id': challenge.json()['challenge_id'], 'signature': sign(private, challenge.json())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), {'Authorization': 'Bearer ' + response.json()['identity']['token']}

    def test_ui_assets_config_and_internal_health(self):
        for path in ('/', '/static/commons.css', '/static/commons.js', '/static/commons-client.js'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get('/api/config').json()['mcp_url'], ORIGIN + '/mcp')
        health = self.client.get('/health', headers={'Host': 'healthcheck.railway.app'})
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()['universe'], 'commons')
        self.assertTrue(health.json()['auth_required'])

    def test_closed_operator_docs_and_unneeded_routes(self):
        for path in ('/docs', '/openapi.json', '/redoc', '/api/roles', '/api/roles/example/status',
                     '/v1/join/exchange', '/static/index.html', '/v1/public/views', '/mcp/anything'):
            for method in ('GET', 'POST'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.client.request(method, path).status_code, 404)

    def test_host_and_origin_guards_apply_before_authentication(self):
        for path in ('/', '/api/config', '/mcp'):
            self.assertEqual(self.client.get(path, headers={'Host': 'attacker.example'}).status_code, 421)
            self.assertEqual(self.client.get(path, headers={'Origin': 'https://attacker.example'}).status_code, 403)
            self.assertEqual(self.client.get(path, headers=[('Host', 'commons.example'),
                                                           ('Host', 'attacker.example')]).status_code, 421)
            self.assertEqual(self.client.get(path, headers=[('Origin', ORIGIN), ('Origin', ORIGIN)]).status_code, 403)

    def test_https_browser_entry_profile_and_write_replay(self):
        result, auth = self.enter()
        self.assertEqual(result['mcp']['url'], ORIGIN + '/mcp')
        headers = {**auth, 'Origin': ORIGIN, 'Host': 'commons.example:443',
                   'X-Forwarded-Proto': 'http', 'X-Forwarded-Host': 'attacker.example'}
        profile = self.client.patch('/api/me', headers=headers, json={'display_name': 'Disposable visitor'})
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(self.client.get('/v1/bootstrap', headers=auth).status_code, 200)
        body = {'arguments': {'post_id': 'deployment-test', 'text': 'Disposable'}, 'operation_id': 'deployment-op'}
        path = '/v1/functions/commons.post.create/invoke'
        first = self.client.post(path, headers=headers, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(self.client.post(path, headers=headers, json=body).json()['replayed'])
        self.assertEqual(self.client.get('/v1/receipts/deployment-op', headers=auth).status_code, 200)

    def test_authenticated_mcp_initialize_and_untrusted_origin_rejection(self):
        _, auth = self.enter()
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'disposable-deployment-test', 'version': '1'}}}
        headers = {**auth, 'Accept': 'application/json, text/event-stream', 'Origin': ORIGIN}
        response = self.client.post('/mcp', headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('result', response.json())
        self.assertIn('mcp-session-id', response.headers)
        response = self.client.post('/mcp', headers={**headers, 'Origin': 'https://attacker.example'}, json=body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/mcp', json=body).status_code, 401)
