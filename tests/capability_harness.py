from __future__ import annotations

from dataclasses import dataclass
import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_world import WorldRuntime
from agent_world.world_sdk import install_world


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _public_key(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64url(raw)


@dataclass(frozen=True)
class Participant:
    role_id: str
    token_id: str
    token: str
    public_key: str
    private_key: Ed25519PrivateKey
    created_profile: bool


class RuntimeCapabilityHarness:
    """Deterministic multi-participant test helper, not a product domain model."""

    def __init__(
        self,
        db_path: str | Path,
        world,
        universe: str = "capability",
    ):
        self.db_path = Path(db_path)
        self.world = world
        self.universe = universe
        self.runtime = WorldRuntime(self.db_path)
        install_world(self.runtime, self.universe, self.world)

    def enter(
        self,
        private_key: Ed25519PrivateKey | None = None,
        *,
        display_name: str | None = None,
    ) -> Participant:
        private_key = private_key or Ed25519PrivateKey.generate()
        public_key = _public_key(private_key)
        challenge = self.runtime.issue_identity_key_challenge(
            self.universe,
            public_key,
        )
        signature = _b64url(private_key.sign(base64.urlsafe_b64decode(
            challenge["message"] + "=" * (-len(challenge["message"]) % 4)
        )))
        result = self.runtime.exchange_identity_key_challenge(
            challenge["challenge_id"],
            signature,
            expected_universe=self.universe,
        )
        role = result["role_profile"]
        if display_name is not None and result["created_profile"]:
            role = self.runtime.update_role_profile(
                role["role_id"],
                display_name=display_name,
            )
        identity = result["identity"]
        return Participant(
            role["role_id"],
            identity["token_id"],
            identity["token"],
            public_key,
            private_key,
            bool(result["created_profile"]),
        )

    def spawn(self, count: int, prefix: str = "P") -> list[Participant]:
        return [
            self.enter(display_name=f"{prefix}{index}")
            for index in range(count)
        ]

    def reenter(self, participant: Participant) -> Participant:
        return self.enter(participant.private_key)

    def call(
        self,
        participant: Participant,
        function_id: str,
        arguments: dict,
        operation_id: str,
    ):
        return self.runtime.call_function(
            self.universe,
            function_id,
            participant.role_id,
            arguments,
            operation_id=operation_id,
            identity_token=participant.token,
        )

    def query(
        self,
        participant: Participant,
        function_id: str,
        arguments: dict,
    ):
        return self.runtime.call_function(
            self.universe,
            function_id,
            participant.role_id,
            arguments,
            identity_token=participant.token,
        )

    def changes(
        self,
        participant: Participant,
        after: int = 0,
        limit: int = 200,
    ):
        return self.runtime.read_changes_page(
            self.universe,
            participant.role_id,
            after,
            limit,
            identity_token=participant.token,
        )

    def revoke(self, participant: Participant):
        self.runtime.revoke_identity_token(participant.token_id)

    def restart(self):
        self.runtime = WorldRuntime(self.db_path)
        install_world(self.runtime, self.universe, self.world)
        return self.runtime
