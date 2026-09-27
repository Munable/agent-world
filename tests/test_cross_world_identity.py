from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from agent_world.errors import InvalidIdentityToken
from agent_world.world_sdk import install_world
from experiments.cross_world_identity import (
    ChallengeRejected,
    DelegationRejected,
    Ed25519Identity,
    IdentityProofDeployment,
    ProofRejected,
    create_delegation,
    create_delegation_revocation,
    create_root_rotation,
    sign_challenge,
)
from tests.fixtures.package_probe_world import WORLD


class MutableClock:
    def __init__(self, value: float = 1_000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


ROOT = Path(__file__).resolve().parents[1]


class CrossWorldIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_dir = Path(self.temp.name)
        self.clock = MutableClock()
        self.a = self.deployment("A")
        self.b = self.deployment("B")
        install_world(self.a.runtime, "world", WORLD)
        install_world(self.b.runtime, "world", WORLD)
        self.root = Ed25519Identity.generate()
        self.device = Ed25519Identity.generate()

    def deployment(self, name: str) -> IdentityProofDeployment:
        base = self.root_dir / name
        return IdentityProofDeployment(
            deployment_id=name,
            universe="world",
            runtime_db=base / "runtime.sqlite3",
            identity_db=base / "identity.sqlite3",
            clock=self.clock,
        )

    def delegation(
        self,
        *,
        root=None,
        device=None,
        delegation_id="device-main",
        audiences=None,
        lifetime=3600,
    ):
        root = root or self.root
        device = device or self.device
        return create_delegation(
            root,
            device,
            delegation_id=delegation_id,
            audiences=audiences or [self.a.audience, self.b.audience],
            issued_at=self.clock(),
            expires_at=self.clock() + lifetime,
        )

    def authenticate(self, deployment, delegation, device=None, display_name="User"):
        challenge = deployment.issue_challenge()
        proof = sign_challenge(
            device or self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
        )
        return deployment.authenticate(
            proof,
            display_name=display_name,
            token_ttl_seconds=600,
        )

    def cli(self, deployment: str, command: str, payload: dict):
        base = self.root_dir / ("process-" + deployment)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "experiments.cross_world_identity_cli",
                command,
                "--deployment",
                deployment,
                "--universe",
                "world",
                "--runtime-db",
                str(base / "runtime.sqlite3"),
                "--identity-db",
                str(base / "identity.sqlite3"),
            ],
            cwd=ROOT,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_two_independent_processes_verify_same_root_without_sharing_bearers(self):
        root = Ed25519Identity.generate()
        device = Ed25519Identity.generate()
        now = time.time()
        delegation = create_delegation(
            root,
            device,
            delegation_id="process-device",
            audiences=["agent-world:A:world", "agent-world:B:world"],
            issued_at=now,
            expires_at=now + 3600,
        )

        challenge_a = self.cli("A", "challenge", {})
        proof_a = sign_challenge(
            device,
            delegation,
            challenge_a,
            issued_at=time.time(),
        )
        result_a = self.cli(
            "A",
            "authenticate",
            {"proof": proof_a, "display_name": "Process A"},
        )

        challenge_b = self.cli("B", "challenge", {})
        proof_b = sign_challenge(
            device,
            delegation,
            challenge_b,
            issued_at=time.time(),
        )
        result_b = self.cli(
            "B",
            "authenticate",
            {"proof": proof_b, "display_name": "Process B"},
        )

        self.assertEqual(result_a["root_id"], result_b["root_id"])
        self.assertNotEqual(result_a["role"]["role_id"], result_b["role"]["role_id"])
        self.assertTrue(
            self.cli(
                "A",
                "resolve",
                {"token": result_a["credential"]["token"]},
            )["valid"]
        )
        self.assertFalse(
            self.cli(
                "B",
                "resolve",
                {"token": result_a["credential"]["token"]},
            )["valid"]
        )

    def test_same_root_proves_same_identity_but_world_credentials_stay_local(self):
        delegation = self.delegation()
        result_a = self.authenticate(self.a, delegation, display_name="User in A")
        result_b = self.authenticate(self.b, delegation, display_name="User in B")

        self.assertEqual(result_a["root_id"], result_b["root_id"])
        self.assertNotEqual(result_a["role"]["role_id"], result_b["role"]["role_id"])
        self.assertNotEqual(
            result_a["credential"]["token"],
            result_b["credential"]["token"],
        )
        self.assertNotEqual(self.a.runtime_db.resolve(), self.b.runtime_db.resolve())
        self.assertNotEqual(self.a.identity_db.resolve(), self.b.identity_db.resolve())

        accepted = self.a.runtime.call_function(
            "world",
            "character.express",
            result_a["role"]["role_id"],
            {
                "text": "hello A",
                "message_id": "a-message",
                "channel": "speech",
            },
            operation_id="a-operation",
            identity_token=result_a["credential"]["token"],
        )
        self.assertTrue(accepted["result"]["accepted"])

        with self.assertRaises(InvalidIdentityToken):
            self.b.runtime.call_function(
                "world",
                "character.express",
                result_b["role"]["role_id"],
                {
                    "text": "wrong bearer",
                    "message_id": "wrong-bearer",
                    "channel": "speech",
                },
                operation_id="wrong-bearer",
                identity_token=result_a["credential"]["token"],
            )

        accepted_b = self.b.runtime.call_function(
            "world",
            "character.express",
            result_b["role"]["role_id"],
            {
                "text": "hello B",
                "message_id": "b-message",
                "channel": "speech",
            },
            operation_id="b-operation",
            identity_token=result_b["credential"]["token"],
        )
        self.assertTrue(accepted_b["result"]["accepted"])

    def test_new_device_and_verifier_restart_reuse_same_local_profile(self):
        first = self.authenticate(self.a, self.delegation(), display_name="Initial")
        restarted = self.deployment("A")
        install_world(restarted.runtime, "world", WORLD)

        new_device = Ed25519Identity.generate()
        new_delegation = self.delegation(
            device=new_device,
            delegation_id="device-second",
            audiences=[restarted.audience],
        )
        second = self.authenticate(
            restarted,
            new_delegation,
            device=new_device,
            display_name="Ignored New Name",
        )

        self.assertEqual(first["root_id"], second["root_id"])
        self.assertEqual(first["role"]["role_id"], second["role"]["role_id"])
        self.assertEqual(second["role"]["display_name"], "Initial")
        self.assertNotEqual(first["credential"]["token_id"], second["credential"]["token_id"])

    def test_challenge_is_one_time_audience_bound_expiring_and_signature_checked(self):
        delegation = self.delegation(audiences=[self.a.audience])
        challenge = self.a.issue_challenge(ttl_seconds=20)
        proof = sign_challenge(
            self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
        )
        first = self.a.authenticate(proof, display_name="User", token_ttl_seconds=60)
        self.assertEqual(first["root_id"], self.root.root_id)
        with self.assertRaises(ChallengeRejected):
            self.a.authenticate(proof, display_name="User", token_ttl_seconds=60)

        challenge = self.a.issue_challenge(ttl_seconds=20)
        wrong_audience = sign_challenge(
            self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
            audience_override=self.b.audience,
        )
        with self.assertRaises(ChallengeRejected):
            self.a.authenticate(
                wrong_audience,
                display_name="User",
                token_ttl_seconds=60,
            )

        challenge = self.a.issue_challenge(ttl_seconds=5)
        expiring_proof = sign_challenge(
            self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
        )
        self.clock.advance(6)
        with self.assertRaises(ChallengeRejected):
            self.a.authenticate(
                expiring_proof,
                display_name="User",
                token_ttl_seconds=60,
            )

        self.clock.advance(-6)
        challenge = self.a.issue_challenge(ttl_seconds=20)
        tampered = sign_challenge(
            self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
        )
        signature = tampered["signature"]
        tampered["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(ProofRejected):
            self.a.authenticate(
                tampered,
                display_name="User",
                token_ttl_seconds=60,
            )

    def test_expired_or_revoked_delegation_cannot_mint_more_local_credentials(self):
        expired = self.delegation(lifetime=5)
        self.clock.advance(6)
        challenge = self.a.issue_challenge()
        proof = sign_challenge(
            self.device,
            expired,
            challenge,
            issued_at=self.clock(),
        )
        with self.assertRaises(DelegationRejected):
            self.a.authenticate(proof, display_name="User", token_ttl_seconds=60)

        self.clock.advance(-6)
        delegation = self.delegation(delegation_id="revocable")
        result_a = self.authenticate(self.a, delegation)
        result_b = self.authenticate(self.b, delegation)

        revocation = create_delegation_revocation(
            self.root,
            "revocable",
            revoked_at=self.clock(),
        )
        local = self.a.accept_delegation_revocation(revocation)
        self.assertEqual(local["revoked_tokens"], 1)
        with self.assertRaises(InvalidIdentityToken):
            self.a.runtime.resolve_identity_token(result_a["credential"]["token"])

        # Revocation has to reach each independent deployment. There is no magic
        # global revocation channel in this experiment.
        self.assertEqual(
            self.b.runtime.resolve_identity_token(result_b["credential"]["token"])["role_id"],
            result_b["role"]["role_id"],
        )
        remote = self.b.accept_delegation_revocation(revocation)
        self.assertEqual(remote["revoked_tokens"], 1)
        with self.assertRaises(InvalidIdentityToken):
            self.b.runtime.resolve_identity_token(result_b["credential"]["token"])

        for deployment in (self.a, self.b):
            challenge = deployment.issue_challenge()
            proof = sign_challenge(
                self.device,
                delegation,
                challenge,
                issued_at=self.clock(),
            )
            with self.assertRaises(DelegationRejected):
                deployment.authenticate(
                    proof,
                    display_name="User",
                    token_ttl_seconds=60,
                )

    def test_delegation_requires_world_audience_and_valid_root_signature(self):
        a_only = self.delegation(audiences=[self.a.audience])

        challenge = self.b.issue_challenge()
        proof = sign_challenge(
            self.device,
            a_only,
            challenge,
            issued_at=self.clock(),
        )
        with self.assertRaises(DelegationRejected):
            self.b.authenticate(proof, display_name="User", token_ttl_seconds=60)

        challenge = self.a.issue_challenge()
        tampered_delegation = deepcopy(a_only)
        signature = tampered_delegation["signature"]
        tampered_delegation["signature"] = (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        proof = sign_challenge(
            self.device,
            tampered_delegation,
            challenge,
            issued_at=self.clock(),
        )
        with self.assertRaises(DelegationRejected):
            self.a.authenticate(proof, display_name="User", token_ttl_seconds=60)

    def test_root_rotation_requires_both_old_and_new_key_signatures(self):
        delegation = self.delegation()
        enrolled = self.authenticate(self.a, delegation)
        new_root = Ed25519Identity.generate()
        rotation = create_root_rotation(
            self.root,
            new_root,
            issued_at=self.clock(),
        )
        tampered = deepcopy(rotation)
        signature = tampered["new_signature"]
        tampered["new_signature"] = (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        from experiments.cross_world_identity import RootRotationRejected

        with self.assertRaises(RootRotationRejected):
            self.a.accept_root_rotation(tampered)

        self.assertEqual(
            self.a.lookup_role(self.root.root_id),
            enrolled["role"]["role_id"],
        )
        self.assertIsNone(self.a.lookup_role(new_root.root_id))

    def test_two_party_root_rotation_preserves_local_profile_and_rejects_old_root(self):
        old_delegation = self.delegation(delegation_id="old-device")
        old_a = self.authenticate(self.a, old_delegation, display_name="A Profile")
        old_b = self.authenticate(self.b, old_delegation, display_name="B Profile")

        new_root = Ed25519Identity.generate()
        rotation = create_root_rotation(
            self.root,
            new_root,
            issued_at=self.clock(),
            invalidate_prior_delegations=True,
        )
        rotated_a = self.a.accept_root_rotation(rotation)
        rotated_b = self.b.accept_root_rotation(rotation)
        self.assertFalse(rotated_a["replayed"])
        self.assertFalse(rotated_b["replayed"])
        self.assertEqual(rotated_a["role_id"], old_a["role"]["role_id"])
        self.assertEqual(rotated_b["role_id"], old_b["role"]["role_id"])
        self.assertEqual(self.a.root_status(self.root.root_id), "superseded")
        self.assertEqual(self.b.root_status(self.root.root_id), "superseded")
        self.assertEqual(self.a.root_status(new_root.root_id), "active")
        self.assertEqual(self.b.root_status(new_root.root_id), "active")
        with self.assertRaises(InvalidIdentityToken):
            self.a.runtime.resolve_identity_token(old_a["credential"]["token"])
        with self.assertRaises(InvalidIdentityToken):
            self.b.runtime.resolve_identity_token(old_b["credential"]["token"])

        challenge = self.a.issue_challenge()
        old_proof = sign_challenge(
            self.device,
            old_delegation,
            challenge,
            issued_at=self.clock(),
        )
        with self.assertRaises(DelegationRejected):
            self.a.authenticate(old_proof, display_name="A Profile", token_ttl_seconds=60)

        new_device = Ed25519Identity.generate()
        new_delegation = create_delegation(
            new_root,
            new_device,
            delegation_id="new-root-device",
            audiences=[self.a.audience, self.b.audience],
            issued_at=self.clock(),
            expires_at=self.clock() + 3600,
        )
        new_a = self.authenticate(
            self.a,
            new_delegation,
            device=new_device,
            display_name="Should Not Replace A",
        )
        new_b = self.authenticate(
            self.b,
            new_delegation,
            device=new_device,
            display_name="Should Not Replace B",
        )
        self.assertEqual(new_a["role"]["role_id"], old_a["role"]["role_id"])
        self.assertEqual(new_b["role"]["role_id"], old_b["role"]["role_id"])
        self.assertEqual(new_a["role"]["display_name"], "A Profile")
        self.assertEqual(new_b["role"]["display_name"], "B Profile")

        replay = self.a.accept_root_rotation(rotation)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["role_id"], old_a["role"]["role_id"])

    def test_proof_material_contains_public_data_only(self):
        delegation = self.delegation()
        challenge = self.a.issue_challenge()
        proof = sign_challenge(
            self.device,
            delegation,
            challenge,
            issued_at=self.clock(),
        )
        encoded = repr(proof)
        self.assertIn(self.root.public_key, encoded)
        self.assertIn(self.device.public_key, encoded)
        self.assertNotIn("_private_key", encoded)
        self.assertNotIn("private", encoded.lower())


if __name__ == "__main__":
    unittest.main()
