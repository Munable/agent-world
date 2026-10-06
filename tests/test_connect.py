from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_world.connect import (
    CONFIG_SCHEMA, ConnectionSetupError, IdentityKey, connect_world, main, validate_origin,
)
from agent_world.runtime_core import WorldRuntime


@unittest.skipUnless(os.name == "posix", "The connection helper requires POSIX private-file permissions")
class ConnectionHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key_path = self.root / "private" / "identity.json"
        self.output_path = self.root / "private" / "opencode.json"
        self.runtime = WorldRuntime(self.root / "world.sqlite3")
        self.requests = []
        self.origin = "https://world.example"

    def response(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path == "/v1/key-identities/challenges":
            result = self.runtime.issue_identity_key_challenge("commons", body["public_key"],
                                                               access_mode=body.get("access_mode", "control"))
        elif request.url.path == "/v1/key-identities/exchange":
            result = self.runtime.exchange_identity_key_challenge(
                body["challenge_id"], body["signature"], expected_universe="commons", identity_ttl_seconds=86400,
            )
            result["mcp"] = {
                "url": self.origin + "/mcp", "authorization_scheme": "Bearer",
                "authorization_token": result["identity"]["token"],
            }
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=result)

    def connect(self, handler=None, **kwargs):
        arguments = {"key_path": self.key_path, "output_path": self.output_path, **kwargs}
        with httpx.Client(transport=httpx.MockTransport(handler or self.response)) as client:
            return connect_world(self.origin, client=client, **arguments)

    def saved_config(self):
        return json.loads(self.output_path.read_text())

    def test_explicit_new_identity_writes_private_remote_config(self):
        result = self.connect(new_identity=True)
        self.assertEqual(result.universe, "commons")
        self.assertGreater(result.expires_at, time.time())
        self.assertLess(result.expires_at, time.time() + 86401)
        config = self.saved_config()
        self.assertEqual(config["$schema"], CONFIG_SCHEMA)
        mcp = config["mcp"]["agent_world"]
        self.assertEqual(set(mcp), {"type", "url", "oauth", "headers"})
        self.assertEqual({k: mcp[k] for k in ("type", "url", "oauth")}, {
            "type": "remote", "url": self.origin + "/mcp", "oauth": False,
        })
        token = mcp["headers"]["Authorization"].removeprefix("Bearer ")
        self.assertEqual(self.runtime.resolve_identity_token(token)["role_id"], result.role_id)
        from tools.play_frontier_codex import read_connection
        launch_url, launch_token, _ = read_connection(self.output_path)
        self.assertEqual((launch_url, launch_token), (self.origin + "/mcp", token))
        for path in (self.key_path, self.output_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.key_path.parent.glob(".agent-world-*")), [])
        self.assertNotIn("awid_", repr(result))

    def test_saved_identity_survives_runtime_and_client_restart(self):
        first = self.connect(new_identity=True)
        key_bytes = self.key_path.read_bytes()
        first_config = self.saved_config()
        self.runtime = WorldRuntime(self.root / "world.sqlite3")
        second = self.connect(replace_config=True)
        self.assertEqual((first.role_id, first.public_key), (second.role_id, second.public_key))
        self.assertEqual(key_bytes, self.key_path.read_bytes())
        self.assertNotEqual(first_config, self.saved_config())
        from tools.play_frontier_codex import read_connection
        launch_url, launch_token, _ = read_connection(self.output_path)
        self.assertEqual(launch_url, self.origin + "/mcp")
        self.assertEqual(self.runtime.resolve_identity_token(launch_token)["role_id"], first.role_id)

    def test_observe_connection_retains_identity_and_requested_server_authority(self):
        first = self.connect(new_identity=True)
        second = self.connect(replace_config=True, access_mode="observe")
        self.assertEqual(first.role_id, second.role_id)
        self.assertEqual(second.access_mode, "observe")
        token = self.saved_config()["mcp"]["agent_world"]["headers"]["Authorization"].removeprefix("Bearer ")
        self.assertEqual(self.runtime.resolve_identity_token(token)["access_mode"], "observe")

    def test_requested_mode_cannot_be_changed_by_challenge_or_exchange(self):
        for stage in ("challenges", "exchange"):
            def handler(request):
                response = self.response(request)
                if request.url.path.endswith(stage):
                    data = response.json()
                    target = data if stage == "challenges" else data["identity"]
                    target["access_mode"] = "control"
                    return httpx.Response(200, json=data)
                return response
            with self.subTest(stage=stage), self.assertRaises(ConnectionSetupError):
                self.connect(handler, new_identity=not self.key_path.exists(), access_mode="observe")
            self.assertFalse(self.output_path.exists())

    def test_helper_accepts_actual_commons_product_exchange(self):
        from fastapi.testclient import TestClient
        from agent_world.product_app import create_product_app

        app = create_product_app(self.root / "product.sqlite3", "commons", commons_ui=True,
                                 mcp_url=self.origin + "/mcp")
        with TestClient(app, base_url=self.origin) as client:
            first = connect_world(self.origin, key_path=self.key_path, output_path=self.output_path,
                                  new_identity=True, client=client)
            second = connect_world(self.origin, key_path=self.key_path, output_path=self.output_path,
                                   replace_config=True, client=client)
        self.assertEqual(first.role_id, second.role_id)
        self.assertEqual(first.public_key, second.public_key)
        self.assertLess(second.expires_at, time.time() + 86401)

    def test_browser_recovery_import_and_export_preserve_identity(self):
        browser_key = IdentityKey(Ed25519PrivateKey.generate())
        recovery_path = self.root / "browser-recovery.json"
        recovery_path.write_text(json.dumps(browser_key.recovery()))
        export_path = self.root / "recovery-copy.json"
        result = self.connect(import_key=recovery_path, export_key=export_path)
        self.assertEqual(result.public_key, browser_key.public_key)
        self.assertEqual(json.loads(export_path.read_text()), browser_key.recovery())
        self.assertEqual(export_path.stat().st_mode & 0o777, 0o600)
        again = self.connect(import_key=recovery_path, replace_config=True)
        self.assertEqual(again.role_id, result.role_id)

    def test_no_saved_key_requires_explicit_identity_choice(self):
        with self.assertRaisesRegex(ConnectionSetupError, "--import-key"):
            self.connect()
        self.assertFalse(self.key_path.exists())
        self.assertFalse(self.requests)

    def test_different_import_cannot_replace_saved_identity(self):
        first = self.connect(new_identity=True)
        recovery_path = self.root / "other-key.json"
        recovery_path.write_text(json.dumps(IdentityKey(Ed25519PrivateKey.generate()).recovery()))
        self.requests.clear()
        with self.assertRaisesRegex(ConnectionSetupError, "differs"):
            self.connect(import_key=recovery_path, replace_config=True)
        with self.assertRaisesRegex(ConnectionSetupError, "already exists"):
            self.connect(new_identity=True, replace_config=True)
        self.assertEqual(json.loads(self.key_path.read_text())["public_key"], first.public_key)
        self.assertFalse(self.requests)

    def test_malformed_recovery_keys_are_rejected_without_network(self):
        seed = IdentityKey(Ed25519PrivateKey.generate()).recovery()
        other = IdentityKey(Ed25519PrivateKey.generate()).public_key
        variants = [
            {**seed, "version": "unknown"}, {**seed, "private_key": "short"},
            {**seed, "private_key": seed["private_key"] + "="}, {**seed, "public_key": other},
            {**seed, "private_key": "!"}, {**seed, "unexpected": "field"},
        ]
        for data in variants:
            with self.subTest(data_keys=list(data)):
                source = self.root / "browser.json"
                source.write_text(json.dumps(data))
                with self.assertRaises(ConnectionSetupError):
                    self.connect(import_key=source)
        self.assertFalse(self.requests)
        self.assertFalse(self.key_path.exists())

    def test_origin_validation_rejects_insecure_or_credential_bearing_urls(self):
        invalid = [
            "http://world.example", "http://localhost.evil", "http://127.0.0.1.evil", "file:///tmp/x",
            "https://user:password@world.example", "https://world.example/world", "https://world.example?x=1",
            "https://world.example#part", "https://world.example?", "https://world.example#",
            "https://world.example:invalid", "https://world.example:70000", "https://world.example\n",
            "https://world.example\\@other.example", "//world.example", "https://[broken",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ConnectionSetupError):
                connect_world(url, key_path=self.key_path, output_path=self.output_path, new_identity=True)
        self.assertFalse(self.key_path.exists())
        self.assertEqual(validate_origin("https://WORLD.example:443/"), "https://world.example")
        self.assertEqual(validate_origin("http://127.0.0.1:8888"), "http://127.0.0.1:8888")
        self.assertEqual(validate_origin("http://[::1]:8888"), "http://[::1]:8888")
        self.assertEqual(validate_origin("http://localhost:8000"), "http://localhost:8000")

    def test_cross_origin_mcp_and_malformed_urls_are_not_saved(self):
        for address in ("https://other.example/mcp", "http://world.example/mcp", "//other.example/mcp",
                        "https://user:pw@world.example/mcp", "/mcp?token=secret", "https://[bad"):
            def handler(request):
                response = self.response(request)
                if request.url.path.endswith("exchange"):
                    body = response.json()
                    body["mcp"]["url"] = address
                    return httpx.Response(200, json=body)
                return response
            with self.subTest(address=address), self.assertRaises(ConnectionSetupError):
                self.connect(handler, new_identity=not self.key_path.exists())
            self.assertFalse(self.output_path.exists())

    def test_redirects_are_not_followed(self):
        for status in (301, 302, 307, 308):
            calls = []
            def handler(request):
                calls.append(request.url)
                return httpx.Response(status, headers={"location": "https://other.example"})
            with self.subTest(status=status), self.assertRaisesRegex(ConnectionSetupError, f"HTTP {status}"):
                self.connect(handler, new_identity=not self.key_path.exists())
            self.assertEqual(len(calls), 1)

    def test_arbitrary_messages_are_never_signed(self):
        def handler(request):
            response = self.response(request)
            data = response.json()
            data["message"] = "dW5yZWxhdGVkLW1lc3NhZ2U"
            return httpx.Response(200, json=data)
        with self.assertRaisesRegex(ConnectionSetupError, "unexpected signing message"):
            self.connect(handler, new_identity=True)
        self.assertEqual(len(self.requests), 1)

    def test_wrong_key_and_expired_challenges_are_rejected_before_exchange(self):
        for field, value in (("public_key", "unrelated"), ("expires_at", 1), ("expires_at", time.time() + 900)):
            def handler(request):
                response = self.response(request)
                data = response.json()
                data[field] = value
                return httpx.Response(200, json=data)
            self.requests.clear()
            with self.subTest(field=field, value=value), self.assertRaises(ConnectionSetupError):
                self.connect(handler, new_identity=not self.key_path.exists())
            self.assertEqual(len(self.requests), 1)

    def test_unbounded_or_mismatched_sessions_are_rejected(self):
        mutations = (
            lambda data: data["identity"].update(expires_at=None),
            lambda data: data["identity"].update(expires_at=time.time() + 172800),
            lambda data: data["identity"].update(expires_at=1),
            lambda data: data["identity"].update(universe="another-world"),
            lambda data: data["identity"].update(role_id="someone-else"),
            lambda data: data["mcp"].update(authorization_token="different"),
        )
        for mutate in mutations:
            def handler(request):
                response = self.response(request)
                if request.url.path.endswith("exchange"):
                    data = response.json()
                    mutate(data)
                    return httpx.Response(200, json=data)
                return response
            with self.assertRaises(ConnectionSetupError):
                self.connect(handler, new_identity=not self.key_path.exists())
            self.assertFalse(self.output_path.exists())

    def test_lost_exchange_response_replays_same_challenge_and_signature(self):
        dropped = False
        issued_tokens = []
        def handler(request):
            nonlocal dropped
            response = self.response(request)
            if request.url.path.endswith("exchange"):
                issued_tokens.append(response.json()["identity"]["token"])
                if not dropped:
                    dropped = True
                    raise httpx.ReadTimeout("test-only lost response")
            return response
        self.connect(handler, new_identity=True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[1], self.requests[2])
        self.assertEqual(issued_tokens[0], issued_tokens[1])

    def test_existing_config_needs_explicit_refresh_and_other_config_is_preserved(self):
        self.connect(new_identity=True)
        before = self.output_path.read_bytes()
        self.requests.clear()
        with self.assertRaisesRegex(ConnectionSetupError, "--replace-config"):
            self.connect()
        self.assertEqual(before, self.output_path.read_bytes())
        self.output_path.write_text('{"model": "user-model"}')
        with self.assertRaisesRegex(ConnectionSetupError, "standalone Agent World"):
            self.connect(replace_config=True)
        self.assertEqual(self.output_path.read_text(), '{"model": "user-model"}')
        self.assertFalse(self.requests)

    def test_distinct_paths_and_no_recovery_overwrite(self):
        source = self.root / "recovery.json"
        source.write_text(json.dumps(IdentityKey(Ed25519PrivateKey.generate()).recovery()))
        for changes in ({"output_path": self.key_path}, {"export_key": self.key_path},
                        {"export_key": source}, {"output_path": source, "import_key": source}):
            with self.subTest(changes=list(changes)), self.assertRaises(ConnectionSetupError):
                self.connect(new_identity=True, **changes)
        self.assertFalse(self.requests)

    def test_symlinks_insecure_keys_and_git_paths_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ConnectionSetupError, "symbolic"):
            self.connect(new_identity=True, key_path=linked / "identity.json")
        repository = self.root / "repo"
        repository.mkdir()
        (repository / ".git").write_text("gitdir: somewhere")
        with self.assertRaisesRegex(ConnectionSetupError, "Git"):
            self.connect(new_identity=True, output_path=repository / "opencode.json")
        self.connect(new_identity=True)
        self.key_path.chmod(0o644)
        with self.assertRaisesRegex(ConnectionSetupError, "0600"):
            self.connect(replace_config=True)

    def test_global_opencode_config_is_never_modified(self):
        with patch("agent_world.connect.Path.home", return_value=self.root):
            with self.assertRaisesRegex(ConnectionSetupError, "global OpenCode"):
                self.connect(new_identity=True, output_path=self.root / ".config/opencode/opencode.json")
        self.assertFalse(self.requests)

    def test_failed_atomic_refresh_preserves_existing_config_and_cleans_temp(self):
        self.connect(new_identity=True)
        original = self.output_path.read_bytes()
        with patch("agent_world.connect.os.replace", side_effect=OSError("test-only interrupted write")):
            with self.assertRaises(OSError):
                self.connect(replace_config=True)
        self.assertEqual(original, self.output_path.read_bytes())
        self.assertFalse(list(self.output_path.parent.glob(".agent-world-*")))

    def test_errors_never_echo_response_secrets(self):
        sentinel = "awid_test-only-secret"
        for response in (httpx.Response(403, text=sentinel), httpx.Response(200, text=sentinel)):
            with contextlib.closing(httpx.Client(transport=httpx.MockTransport(lambda request: response))) as client:
                with patch("agent_world.connect.httpx.Client", return_value=client):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        status = main(["--url", self.origin, "--key", str(self.key_path), "--output",
                                       str(self.output_path)] + ([] if self.key_path.exists() else ["--new-identity"]))
            self.assertEqual(status, 1)
            self.assertNotIn(sentinel, output.getvalue())
            self.assertNotIn(json.loads(self.key_path.read_text())["private_key"], output.getvalue())

    def test_real_process_restarts_reuse_key_without_real_agent_or_model(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                response = owner.response(httpx.Request("POST", owner.origin + self.path, content=payload))
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cmd = [sys.executable, "-m", "agent_world.connect", "--url", self.origin,
               "--key", str(self.key_path), "--output", str(self.output_path)]
        first = subprocess.run(cmd + ["--new-identity"], capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        key = self.key_path.read_bytes()
        token = self.saved_config()["mcp"]["agent_world"]["headers"]["Authorization"].split(" ")[1]
        role = self.runtime.resolve_identity_token(token)["role_id"]
        second = subprocess.run(cmd + ["--replace-config"], capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(key, self.key_path.read_bytes())
        token2 = self.saved_config()["mcp"]["agent_world"]["headers"]["Authorization"].split(" ")[1]
        self.assertEqual(role, self.runtime.resolve_identity_token(token2)["role_id"])
        self.assertNotEqual(token, token2)
        self.assertNotIn(token, first.stdout + first.stderr)
        self.assertNotIn(token2, second.stdout + second.stderr)
        self.assertNotIn(json.loads(key)["private_key"], first.stdout + second.stdout)
        self.assertIn("OPENCODE_CONFIG=", first.stdout)

    def test_importing_module_has_no_key_or_config_side_effects(self):
        env = {**os.environ, "HOME": str(self.root),
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        result = subprocess.run([sys.executable, "-c", "import agent_world.connect, sys; assert 'agent_world.runtime_core' not in sys.modules; assert 'jsonschema' not in sys.modules"],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / ".config").exists())
        self.assertEqual(result.stdout, "")

    def test_launch_refreshes_connection_and_starts_host_without_secret_arguments(self):
        for first in (True, False):
            with contextlib.closing(httpx.Client(transport=httpx.MockTransport(self.response))) as client:
                with patch("agent_world.connect.httpx.Client", return_value=client), \
                     patch("agent_world.connect.shutil.which", return_value="/official/opencode"), \
                     patch("agent_world.connect.subprocess.run", return_value=subprocess.CompletedProcess([], 7)) as run:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result = main(["--url", self.origin, "--key", str(self.key_path), "--output", str(self.output_path),
                                       "--launch-opencode"] + (["--new-identity"] if first else []))
            self.assertEqual(result, 7)
            self.assertEqual(run.call_args.args[0], ["/official/opencode"])
            self.assertEqual(run.call_args.kwargs["env"]["OPENCODE_CONFIG"], str(self.output_path))
            token = self.saved_config()["mcp"]["agent_world"]["headers"]["Authorization"]
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn(token, str(run.call_args))

    def test_missing_host_does_not_create_identity_or_credential(self):
        with patch("agent_world.connect.shutil.which", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()):
            result = main(["--url", self.origin, "--key", str(self.key_path), "--output", str(self.output_path),
                           "--new-identity", "--launch-opencode"])
        self.assertEqual(result, 1)
        self.assertFalse(self.key_path.exists())
        self.assertFalse(self.output_path.exists())


if __name__ == "__main__":
    unittest.main()
