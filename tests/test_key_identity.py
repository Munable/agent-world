from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import time
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from agent_world.errors import (
    IdentityChallengeExpired,
    IdentityChallengeInvalid,
    InvalidIdentityToken,
    RoleInactive,
)
from agent_world.application import create_application
from agent_world.onboarding_app import create_onboarding_app
from agent_world.runtime_core import WorldRuntime


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, b64url(public)


def sign(private: Ed25519PrivateKey, challenge: dict) -> str:
    return b64url(private.sign(b64url_decode(challenge["message"])))


class KeyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private, self.public_key = keypair()

    def runtime(self, name: str) -> WorldRuntime:
        return WorldRuntime(self.root / f"{name}.sqlite3")

    def authenticate(self, runtime: WorldRuntime, universe: str, private, public_key):
        challenge = runtime.issue_identity_key_challenge(universe, public_key)
        return runtime.exchange_identity_key_challenge(
            challenge["challenge_id"],
            sign(private, challenge),
            expected_universe=universe,
            identity_ttl_seconds=600,
        )

    def test_unknown_key_creates_local_profile_and_same_key_returns_to_it(self):
        runtime = self.runtime("world")
        first = self.authenticate(runtime, "u", self.private, self.public_key)
        self.assertTrue(first["created_profile"])
        self.assertFalse(first["replayed"])
        role_id = first["role_profile"]["role_id"]

        second = self.authenticate(runtime, "u", self.private, self.public_key)
        self.assertFalse(second["created_profile"])
        self.assertEqual(second["role_profile"]["role_id"], role_id)
        self.assertNotEqual(second["identity"]["token_id"], first["identity"]["token_id"])

        mapping = runtime.get_identity_key_profile("u", self.public_key)
        self.assertEqual(mapping["role_profile"]["role_id"], role_id)

    def test_same_challenge_replays_same_local_credential(self):
        runtime = self.runtime("replay")
        challenge = runtime.issue_identity_key_challenge("u", self.public_key)
        signature = sign(self.private, challenge)
        first = runtime.exchange_identity_key_challenge(
            challenge["challenge_id"],
            signature,
            expected_universe="u",
            identity_ttl_seconds=600,
        )
        replay = runtime.exchange_identity_key_challenge(
            challenge["challenge_id"],
            signature,
            expected_universe="u",
            identity_ttl_seconds=600,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["created_profile"], first["created_profile"])
        self.assertEqual(replay["identity"]["token_id"], first["identity"]["token_id"])
        self.assertEqual(replay["identity"]["token"], first["identity"]["token"])
        self.assertEqual(replay["role_profile"]["role_id"], first["role_profile"]["role_id"])

    def test_access_mode_is_bound_to_key_proof_and_survives_replay_and_restart(self):
        runtime = self.runtime("observe")
        challenge = runtime.issue_identity_key_challenge(
            "u", self.public_key, access_mode="observe"
        )
        self.assertEqual(challenge["access_mode"], "observe")
        self.assertEqual(
            b64url_decode(challenge["message"]),
            "\x00".join([
                "agent-world-key-identity-v2", "u", challenge["challenge_id"],
                challenge["nonce"], "observe",
            ]).encode(),
        )
        changed = {**challenge, "message": b64url(
            b64url_decode(challenge["message"]).replace(b"observe", b"control")
        )}
        with self.assertRaises(IdentityChallengeInvalid):
            runtime.exchange_identity_key_challenge(challenge["challenge_id"], sign(self.private, changed))
        self.assertIsNone(runtime.get_identity_key_profile("u", self.public_key))
        signature = sign(self.private, challenge)
        first = runtime.exchange_identity_key_challenge(challenge["challenge_id"], signature)
        reopened = self.runtime("observe")
        replay = reopened.exchange_identity_key_challenge(challenge["challenge_id"], signature)
        self.assertEqual(first["identity"]["access_mode"], "observe")
        self.assertEqual(replay["identity"], first["identity"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(reopened.resolve_identity_token(first["identity"]["token"])["access_mode"], "observe")

    def test_public_surfaces_select_mode_only_in_challenge_and_reject_exchange_override(self):
        apps = [
            create_onboarding_app(self.root / "observe-onboarding.sqlite3", "u"),
            create_application(self.root / "observe-product.sqlite3", world_profile="commons", universe="u", timers_enabled=False),
        ]
        for index, app in enumerate(apps):
            with self.subTest(surface=index), TestClient(app) as client:
                issued = client.post("/v1/key-identities/challenges", json={
                    "public_key": self.public_key, "access_mode": "observe",
                })
                self.assertEqual(issued.status_code, 200, issued.text)
                challenge = issued.json()
                proof = {"challenge_id": challenge["challenge_id"], "signature": sign(self.private, challenge)}
                self.assertEqual(client.post(
                    "/v1/key-identities/exchange", json={**proof, "access_mode": "control"}
                ).status_code, 422)
                exchanged = client.post("/v1/key-identities/exchange", json=proof)
                self.assertEqual(exchanged.status_code, 200, exchanged.text)
                self.assertEqual(exchanged.json()["identity"]["access_mode"], "observe")
                default = client.post("/v1/key-identities/challenges", json={"public_key": self.public_key})
                self.assertEqual(default.json()["access_mode"], "control")
                self.assertEqual(client.post("/v1/key-identities/challenges", json={
                    "public_key": self.public_key, "access_mode": "admin",
                }).status_code, 422)

    def test_schema_migration_preserves_profile_bearer_and_stored_key_proofs(self):
        runtime = self.runtime("migration")
        used = runtime.issue_identity_key_challenge("u", self.public_key)
        first = runtime.exchange_identity_key_challenge(used["challenge_id"], sign(self.private, used))
        pending = runtime.issue_identity_key_challenge("u", self.public_key)

        def stored_v1_signature(challenge):
            message = "\x00".join([
                "agent-world-key-identity-v1", "u", challenge["challenge_id"], challenge["nonce"],
            ]).encode()
            return b64url(self.private.sign(message))

        with runtime._conn() as c:
            c.execute("ALTER TABLE identity_key_challenges DROP COLUMN access_mode")
            c.execute("PRAGMA user_version=7")
        reopened = self.runtime("migration")
        with reopened._conn(readonly=True) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertEqual(reopened.resolve_identity_token(first["identity"]["token"])["role_id"], first["role_profile"]["role_id"])
        replay = reopened.exchange_identity_key_challenge(used["challenge_id"], stored_v1_signature(used))
        self.assertEqual(replay["identity"], first["identity"])
        self.assertEqual(replay["version"], "agent-world-key-identity-v1")
        restored = reopened.exchange_identity_key_challenge(pending["challenge_id"], stored_v1_signature(pending))
        self.assertEqual(restored["role_profile"]["role_id"], first["role_profile"]["role_id"])
        self.assertEqual(restored["identity"]["access_mode"], "control")
        fresh = reopened.issue_identity_key_challenge("u", self.public_key)
        self.assertEqual(fresh["version"], "agent-world-key-identity-v2")
        self.assertEqual(fresh["access_mode"], "control")

    def test_same_key_across_independent_worlds_has_local_profiles_and_local_bearers(self):
        world_a = self.runtime("a")
        world_b = self.runtime("b")

        a = self.authenticate(world_a, "world", self.private, self.public_key)
        b = self.authenticate(world_b, "world", self.private, self.public_key)

        self.assertEqual(a["public_key"], b["public_key"])
        self.assertNotEqual(a["role_profile"]["role_id"], b["role_profile"]["role_id"])
        self.assertNotEqual(a["identity"]["token"], b["identity"]["token"])

        self.assertEqual(
            world_a.resolve_identity_token(a["identity"]["token"])["role_id"],
            a["role_profile"]["role_id"],
        )
        with self.assertRaises(InvalidIdentityToken):
            world_b.resolve_identity_token(a["identity"]["token"])

    def test_new_key_is_new_identity_and_new_local_profile(self):
        runtime = self.runtime("new-key")
        first = self.authenticate(runtime, "u", self.private, self.public_key)
        other_private, other_public = keypair()
        second = self.authenticate(runtime, "u", other_private, other_public)

        self.assertNotEqual(first["public_key"], second["public_key"])
        self.assertNotEqual(first["role_profile"]["role_id"], second["role_profile"]["role_id"])

    def test_profile_data_is_world_local_not_carried_by_key(self):
        world_a = self.runtime("profile-a")
        world_b = self.runtime("profile-b")

        a = self.authenticate(world_a, "world", self.private, self.public_key)
        world_a.update_role_profile(a["role_profile"]["role_id"], display_name="Alice in A")

        b = self.authenticate(world_b, "world", self.private, self.public_key)
        self.assertEqual(
            world_a.get_role(a["role_profile"]["role_id"])["display_name"],
            "Alice in A",
        )
        self.assertNotEqual(b["role_profile"]["display_name"], "Alice in A")
        self.assertTrue(b["role_profile"]["display_name"].startswith("key-"))
    def test_wrong_private_key_cannot_claim_existing_key(self):
        runtime = self.runtime("wrong-key")
        challenge = runtime.issue_identity_key_challenge("u", self.public_key)
        wrong_private, _ = keypair()

        with self.assertRaises(IdentityChallengeInvalid):
            runtime.exchange_identity_key_challenge(
                challenge["challenge_id"],
                sign(wrong_private, challenge),
                expected_universe="u",
            )
        self.assertIsNone(runtime.get_identity_key_profile("u", self.public_key))

    def test_expired_challenge_does_not_create_profile(self):
        runtime = self.runtime("expired")
        challenge = runtime.issue_identity_key_challenge(
            "u",
            self.public_key,
            ttl_seconds=0.01,
        )
        time.sleep(0.03)

        with self.assertRaises(IdentityChallengeExpired):
            runtime.exchange_identity_key_challenge(
                challenge["challenge_id"],
                sign(self.private, challenge),
                expected_universe="u",
            )
        self.assertIsNone(runtime.get_identity_key_profile("u", self.public_key))

    def test_disabled_local_profile_rejects_same_key_without_changing_identity(self):
        runtime = self.runtime("disabled")
        first = self.authenticate(runtime, "u", self.private, self.public_key)
        runtime.set_role_status(first["role_profile"]["role_id"], "disabled")

        challenge = runtime.issue_identity_key_challenge("u", self.public_key)
        with self.assertRaises(RoleInactive):
            runtime.exchange_identity_key_challenge(
                challenge["challenge_id"],
                sign(self.private, challenge),
                expected_universe="u",
            )
        mapping = runtime.get_identity_key_profile("u", self.public_key)
        self.assertEqual(mapping["role_profile"]["role_id"], first["role_profile"]["role_id"])
        self.assertEqual(mapping["role_profile"]["status"], "disabled")

    def test_public_onboarding_api_is_key_challenge_then_world_local_credential(self):
        db = self.root / "api.sqlite3"
        with TestClient(
            create_onboarding_app(
                db,
                "u",
                operator_key="operator-only-for-admin",
                identity_ttl_seconds=600,
            )
        ) as client:
            issued = client.post(
                "/v1/key-identities/challenges",
                json={"public_key": self.public_key},
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            challenge = issued.json()

            exchanged = client.post(
                "/v1/key-identities/exchange",
                json={
                    "challenge_id": challenge["challenge_id"],
                    "signature": sign(self.private, challenge),
                },
            )
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            body = exchanged.json()
            self.assertTrue(body["created_profile"])
            self.assertEqual(body["public_key"], self.public_key)
            self.assertEqual(body["identity"]["universe"], "u")
            self.assertEqual(
                body["identity"]["role_id"],
                body["role_profile"]["role_id"],
            )
            self.assertEqual(
                body["mcp"]["authorization_scheme"],
                "Bearer",
            )

    def test_combined_application_uses_key_identity_as_public_default_entry(self):
        db = self.root / "combined.sqlite3"
        with TestClient(
            create_application(
                db,
                world_profile="demo",
                universe="u",
                public_base_url="http://127.0.0.1:8000",
                web_password=None,
                timers_enabled=False,
            )
        ) as client:
            operator_only = client.post(
                "/api/roles",
                json={"display_name": "operator-created"},
            )
            self.assertEqual(operator_only.status_code, 503, operator_only.text)

            issued = client.post(
                "/v1/key-identities/challenges",
                json={"public_key": self.public_key},
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            challenge = issued.json()
            self.assertEqual(challenge["universe"], "u")
            self.assertEqual(issued.headers["cache-control"], "no-store")

            exchanged = client.post(
                "/v1/key-identities/exchange",
                json={
                    "challenge_id": challenge["challenge_id"],
                    "signature": sign(self.private, challenge),
                },
            )
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            first = exchanged.json()
            self.assertTrue(first["created_profile"])
            self.assertEqual(first["public_key"], self.public_key)
            self.assertEqual(first["next"], {"tool": "world.bootstrap", "arguments": {}})

            auth = {"Authorization": "Bearer " + first["identity"]["token"]}
            boot = client.get("/v1/bootstrap", headers=auth)
            self.assertEqual(boot.status_code, 200, boot.text)
            self.assertEqual(boot.json()["role_id"], first["role_profile"]["role_id"])

            issued_again = client.post(
                "/v1/key-identities/challenges",
                json={"public_key": self.public_key},
            )
            self.assertEqual(issued_again.status_code, 200, issued_again.text)
            challenge_again = issued_again.json()
            exchanged_again = client.post(
                "/v1/key-identities/exchange",
                json={
                    "challenge_id": challenge_again["challenge_id"],
                    "signature": sign(self.private, challenge_again),
                },
            )
            self.assertEqual(exchanged_again.status_code, 200, exchanged_again.text)
            second = exchanged_again.json()
            self.assertFalse(second["created_profile"])
            self.assertEqual(
                second["role_profile"]["role_id"],
                first["role_profile"]["role_id"],
            )
            self.assertNotEqual(
                second["identity"]["token_id"],
                first["identity"]["token_id"],
            )


if __name__ == "__main__":
    unittest.main()
