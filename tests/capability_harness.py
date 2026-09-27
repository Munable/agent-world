from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_world import WorldRuntime
from agent_world.world_sdk import install_world


@dataclass(frozen=True)
class Participant:
    role_id: str
    token_id: str
    token: str


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

    def spawn(self, count: int, prefix: str = "P") -> list[Participant]:
        participants = []
        for index in range(count):
            role = self.runtime.create_role(f"{prefix}{index}")
            identity = self.runtime.issue_identity_token(
                self.universe,
                role["role_id"],
            )
            participants.append(
                Participant(
                    role["role_id"],
                    identity["token_id"],
                    identity["token"],
                )
            )
        return participants

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
