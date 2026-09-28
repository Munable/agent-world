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


if __name__ == "__main__":
    unittest.main()
