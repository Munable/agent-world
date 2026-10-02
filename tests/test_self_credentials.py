from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_world.application import create_application
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

    def enter(self, pair=None):
        private, public = pair or keypair()
        challenge = self.client.post(
            "/v1/key-identities/challenges", json={"public_key": public}
        ).json()
        response = self.client.post(
            "/v1/key-identities/exchange",
            json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        return result, {"Authorization": "Bearer " + result["identity"]["token"]}, (private, public)

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
