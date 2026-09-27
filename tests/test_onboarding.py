from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from agent_world.onboarding_app import create_onboarding_app
from agent_world.runtime_core import WorldRuntime


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "onboarding.sqlite3"
        runtime = WorldRuntime(self.db)
        self.role = runtime.create_role("Operator User")
        self.source = runtime.issue_identity_token("u", self.role["role_id"])
        self.operator = "operator-test-key"
        self.client = TestClient(
            create_onboarding_app(
                self.db,
                "u",
                operator_key=self.operator,
            )
        )
        self.addCleanup(self.client.close)
        self.headers = {"X-Operator-Key": self.operator}

    def test_rotation_replay_and_receipt_recover_same_secret(self):
        body = {"operation_id": "rotate-http", "ttl_seconds": 60}
        first = self.client.post(
            f"/v1/identity-tokens/{self.source['token_id']}/rotate",
            headers=self.headers,
            json=body,
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_body = first.json()
        self.assertFalse(first_body["replayed"])

        replay = self.client.post(
            f"/v1/identity-tokens/{self.source['token_id']}/rotate",
            headers=self.headers,
            json=body,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        replay_body = replay.json()
        self.assertTrue(replay_body["replayed"])
        self.assertEqual(replay_body["token_id"], first_body["token_id"])
        self.assertEqual(replay_body["token"], first_body["token"])

        recovered = self.client.get(
            "/v1/identity-token-rotations/rotate-http",
            headers=self.headers,
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        recovered_body = recovered.json()
        self.assertTrue(recovered_body["replayed"])
        self.assertEqual(recovered_body["token_id"], first_body["token_id"])
        self.assertEqual(recovered_body["token"], first_body["token"])

    def test_rotation_requires_operation_id_and_rejects_conflicting_reuse(self):
        missing = self.client.post(
            f"/v1/identity-tokens/{self.source['token_id']}/rotate",
            headers=self.headers,
            json={"ttl_seconds": 60},
        )
        self.assertEqual(missing.status_code, 422)

        first = self.client.post(
            f"/v1/identity-tokens/{self.source['token_id']}/rotate",
            headers=self.headers,
            json={"operation_id": "rotate-conflict", "ttl_seconds": 60},
        )
        self.assertEqual(first.status_code, 200, first.text)

        conflict = self.client.post(
            f"/v1/identity-tokens/{self.source['token_id']}/rotate",
            headers=self.headers,
            json={"operation_id": "rotate-conflict", "ttl_seconds": 120},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"], "OperationConflict")


if __name__ == "__main__":
    unittest.main()
