from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_world.application import create_application
from agent_world.runtime_core import InvalidIdentityToken, WorldRuntime
from tests.test_key_identity import keypair, sign


class SelfCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "credentials.sqlite3"
        self.app = create_application(
            self.db, world_profile="commons", universe="commons", timers_enabled=False
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def enter(self, pair=None, *, access_mode="control"):
        private, public = pair or keypair()
        body = {"public_key": public}
        if access_mode != "control":
            body["access_mode"] = access_mode
        issued = self.client.post("/v1/key-identities/challenges", json=body)
        self.assertEqual(issued.status_code, 200, issued.text)
        challenge = issued.json()
        response = self.client.post(
            "/v1/key-identities/exchange",
            json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        return result, {"Authorization": "Bearer " + result["identity"]["token"]}, (private, public)

    def test_only_current_credential_is_listed_and_cannot_be_deleted(self):
        browser, auth, _ = self.enter()
        response = self.client.get("/api/me/credentials", headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["credentials"]), 1)
        self.assertEqual(response.json()["credentials"][0]["token_id"], browser["identity"]["token_id"])
        self.assertEqual(self.client.delete(
            "/api/me/credentials/" + browser["identity"]["token_id"], headers=auth
        ).status_code, 422)

    def test_list_shows_only_own_world_metadata_and_credential_status(self):
        browser, auth, pair = self.enter()
        observer, _, _ = self.enter(pair, access_mode="observe")
        expired, _, _ = self.enter(pair)
        revoked, _, _ = self.enter(pair)
        peer, _, _ = self.enter()
        runtime = self.app.state.runtime
        other = runtime.issue_identity_token("elsewhere", browser["role_profile"]["role_id"])
        runtime.revoke_identity_token(revoked["identity"]["token_id"])
        with runtime._conn() as c:
            c.execute("UPDATE identity_tokens SET expires_at=0 WHERE token_id=?", (expired["identity"]["token_id"],))
        response = self.client.get("/api/me/credentials", headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["universe"], "commons")
        self.assertEqual(body["role_id"], browser["role_profile"]["role_id"])
        self.assertEqual(body["current_token_id"], browser["identity"]["token_id"])
        rows = {row["token_id"]: row for row in body["credentials"]}
        self.assertEqual(set(rows), {identity["identity"]["token_id"] for identity in (browser, observer, expired, revoked)})
        self.assertTrue(rows[browser["identity"]["token_id"]]["is_current"])
        self.assertFalse(rows[observer["identity"]["token_id"]]["is_current"])
        self.assertEqual(rows[observer["identity"]["token_id"]]["access_mode"], "observe")
        self.assertEqual(rows[expired["identity"]["token_id"]]["status"], "expired")
        self.assertEqual(rows[revoked["identity"]["token_id"]]["status"], "revoked")
        self.assertNotIn(peer["identity"]["token_id"], rows)
        self.assertNotIn(other["token_id"], rows)
        self.assertNotIn("awid_", response.text)
        self.assertNotIn("token_hash", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_single_revocation_is_scoped_idempotent_and_keeps_current_connection(self):
        browser, auth, pair = self.enter()
        target, target_auth, _ = self.enter(pair, access_mode="observe")
        peer, peer_auth, _ = self.enter()
        runtime = self.app.state.runtime
        other = runtime.issue_identity_token("elsewhere", browser["role_profile"]["role_id"])
        path = "/api/me/credentials/" + target["identity"]["token_id"]
        first = self.client.delete(path, headers=auth)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), {"token_id": target["identity"]["token_id"], "revoked": True})
        again = self.client.delete(path, headers=auth)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(self.client.get("/api/me", headers=target_auth).status_code, 401)
        missing = self.client.delete("/api/me/credentials/unknown", headers=auth)
        self.assertEqual(missing.status_code, 404, missing.text)
        for token_id in (peer["identity"]["token_id"], other["token_id"]):
            denied = self.client.delete("/api/me/credentials/" + token_id, headers=auth)
            self.assertEqual(denied.status_code, missing.status_code)
            self.assertEqual(denied.json(), missing.json())
        self.assertEqual(self.client.delete(
            "/api/me/credentials/" + browser["identity"]["token_id"], headers=auth
        ).status_code, 422)
        self.assertEqual(self.client.get("/api/me", headers=auth).status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=peer_auth).status_code, 200)
        self.assertEqual(runtime.resolve_identity_token(other["token"])["universe"], "elsewhere")

    def test_management_requires_live_control_and_rejects_duplicate_auth_and_cross_origin(self):
        browser, auth, pair = self.enter()
        _, observe_auth, _ = self.enter(pair, access_mode="observe")
        target, target_auth, _ = self.enter(pair)
        path = "/api/me/credentials/" + target["identity"]["token_id"]
        for headers in ({}, observe_auth):
            expected = 401 if not headers else 403
            self.assertEqual(self.client.get("/api/me/credentials", headers=headers).status_code, expected)
            self.assertEqual(self.client.delete(path, headers=headers).status_code, expected)
        duplicate = [("Authorization", auth["Authorization"])] * 2
        self.assertEqual(self.client.get("/api/me/credentials", headers=duplicate).status_code, 422)
        self.assertEqual(self.client.delete(path, headers=duplicate).status_code, 422)
        self.assertEqual(self.client.delete(path, headers={**auth, "Origin": "https://elsewhere.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/api/me", headers=target_auth).status_code, 200)
        self.app.state.runtime.revoke_identity_token(browser["identity"]["token_id"])
        self.assertEqual(self.client.get("/api/me/credentials", headers=auth).status_code, 401)
        self.assertEqual(self.client.delete(path, headers=auth).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=target_auth).status_code, 200)

    def test_bearer_management_cannot_issue_replacement_credential(self):
        _, auth, _ = self.enter()
        self.assertEqual(self.client.post("/api/me/credentials", headers=auth, json={}).status_code, 405)
        self.assertEqual(self.client.post("/api/me/credentials/refresh", headers=auth, json={}).status_code, 405)

    def test_revoked_caller_cannot_revoke_target_after_waiting_for_database_writer(self):
        browser, _, pair = self.enter()
        target, target_auth, _ = self.enter(pair)
        waiting_runtime = WorldRuntime(self.db)
        original_conn = waiting_runtime._conn
        acquiring_writer = threading.Event()

        @contextmanager
        def traced_conn(*args, **kwargs):
            with original_conn(*args, **kwargs) as c:
                c.set_trace_callback(lambda sql: acquiring_writer.set() if sql == "BEGIN IMMEDIATE" else None)
                yield c

        with patch.object(waiting_runtime, "_conn", traced_conn), ThreadPoolExecutor(max_workers=1) as pool:
            with self.app.state.runtime._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                future = pool.submit(
                    waiting_runtime.revoke_own_identity_token, "commons",
                    browser["identity"]["token"], target["identity"]["token_id"],
                )
                self.assertTrue(acquiring_writer.wait(3), "revocation did not enter its write transaction")
                c.execute("UPDATE identity_tokens SET revoked_at=0 WHERE token_id=?", (browser["identity"]["token_id"],))
            with self.assertRaises(InvalidIdentityToken):
                future.result(timeout=3)
        self.assertEqual(self.client.get("/api/me", headers=target_auth).status_code, 200)

    def test_self_revocation_keeps_this_session_and_other_users_and_worlds(self):
        browser, auth, pair = self.enter()
        agent, agent_auth, _ = self.enter(pair)
        peer, peer_auth, _ = self.enter()
        runtime = self.app.state.runtime
        other_world = runtime.issue_identity_token("other-world", browser["role_profile"]["role_id"])
        response = self.client.post("/api/me/revoke-other-credentials", headers=auth, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revoked_count"], 1)
        self.assertEqual(response.json()["kept_token_id"], browser["identity"]["token_id"])
        self.assertNotIn("awid_", response.text)
        self.assertEqual(self.client.get("/api/me", headers=auth).status_code, 200)
        self.assertEqual(self.client.get("/v1/bootstrap", headers=agent_auth).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=peer_auth).status_code, 200)
        self.assertEqual(runtime.resolve_identity_token(other_world["token"])["universe"], "other-world")
        again = self.client.post("/api/me/revoke-other-credentials", headers=auth, json={})
        self.assertEqual(again.json()["revoked_count"], 0)
        restored, _, _ = self.enter(pair)
        self.assertEqual(restored["role_profile"]["role_id"], browser["role_profile"]["role_id"])

    def test_observe_and_revoked_credentials_cannot_revoke_other_connections(self):
        browser, auth, pair = self.enter()
        agent, agent_auth, _ = self.enter(pair)
        runtime = self.app.state.runtime
        observer = runtime.issue_identity_token(
            "commons", browser["role_profile"]["role_id"], access_mode="observe"
        )
        response = self.client.post(
            "/api/me/revoke-other-credentials",
            headers={"Authorization": "Bearer " + observer["token"]}, json={},
        )
        self.assertEqual(response.status_code, 403, response.text)
        runtime.revoke_identity_token(browser["identity"]["token_id"])
        self.assertEqual(self.client.post("/api/me/revoke-other-credentials", headers=auth, json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=agent_auth).status_code, 200)

    def test_self_revocation_rejects_impersonation_cross_origin_and_duplicate_auth(self):
        browser, auth, pair = self.enter()
        _, agent_auth, _ = self.enter(pair)
        self.assertEqual(self.client.post("/api/me/revoke-other-credentials", json={}).status_code, 401)
        self.assertEqual(self.client.post(
            "/api/me/revoke-other-credentials", headers=auth, json={"role_id": "another-user"}
        ).status_code, 422)
        self.assertEqual(self.client.post(
            "/api/me/revoke-other-credentials", headers={**auth, "Origin": "https://elsewhere.invalid"}, json={}
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/me/revoke-other-credentials",
            headers=[("Authorization", auth["Authorization"]), ("Authorization", auth["Authorization"])], json={}
        ).status_code, 422)
        self.assertEqual(self.client.get("/api/me", headers=agent_auth).status_code, 200)


if __name__ == "__main__":
    unittest.main()
