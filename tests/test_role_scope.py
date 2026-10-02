from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_world import FunctionOutcome, FunctionSpec, ViewSpec, WorldDefinition
from agent_world.builtin_worlds import get_builtin_definition
from agent_world.errors import RoleNotFound
from tests.capability_harness import RuntimeCapabilityHarness


ROLE_ARGS = {
    "type": "object", "properties": {"role_id": {"type": "string"}},
    "required": ["role_id"], "additionalProperties": False,
}


def profile(ctx, arguments):
    return FunctionOutcome({"profile": ctx.get_role(arguments["role_id"])})


def public_profile(ctx, arguments):
    return {"entities": {"profile": ctx.get_role(arguments["role_id"])}}


PROFILE_WORLD = WorldDefinition(
    "profile-scope", "Profile scope fixture",
    (FunctionSpec("profile.read", profile, ROLE_ARGS, access="read"),),
    views=(ViewSpec("profile", public_profile, input_schema=ROLE_ARGS, public=True),),
)


class RoleScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profiles.sqlite3"
        self.a = RuntimeCapabilityHarness(self.path, PROFILE_WORLD, universe="a")
        self.b = RuntimeCapabilityHarness(self.path, PROFILE_WORLD, universe="b")
        self.alice = self.a.enter(display_name="A local")
        self.bob = self.b.enter(display_name="B private name")

    def test_same_database_key_profile_cannot_be_read_from_another_world(self):
        with self.assertRaises(RoleNotFound):
            self.a.query(self.alice, "profile.read", {"role_id": self.bob.role_id})
        self.assertEqual(self.b.query(self.bob, "profile.read", {"role_id": self.bob.role_id})
                         ["result"]["profile"]["display_name"], "B private name")
        local_bob = self.a.reenter(self.bob)
        self.assertNotEqual(local_bob.role_id, self.bob.role_id)
        self.assertNotEqual(self.a.query(self.alice, "profile.read", {"role_id": local_bob.role_id})
                            ["result"]["profile"]["display_name"], "B private name")

    def test_observer_and_anonymous_projection_cannot_read_another_world_profile(self):
        observer = self.a.runtime.issue_identity_token("a", self.alice.role_id, access_mode="observe")
        with self.assertRaises(RoleNotFound):
            self.a.runtime.query_function("a", "profile.read", self.alice.role_id,
                                          {"role_id": self.bob.role_id}, identity_token=observer["token"])
        with self.assertRaises(RoleNotFound):
            self.a.runtime.public_view_snapshot("a", "profile", {"role_id": self.bob.role_id})
        visible = self.a.runtime.public_view_snapshot("a", "profile", {"role_id": self.alice.role_id})
        self.assertEqual(visible["snapshot"]["entities"]["profile"]["role_id"], self.alice.role_id)

    def test_cross_world_conversation_rejects_peer_without_partial_effects(self):
        commons = get_builtin_definition("commons")
        a = RuntimeCapabilityHarness(self.path, commons, universe="commons-a")
        b = RuntimeCapabilityHarness(self.path, commons, universe="commons-b")
        alice, bob = a.enter(), b.enter()
        with self.assertRaises(RoleNotFound):
            a.call(alice, "commons.conversation.open",
                   {"conversation_id": "foreign", "peer_role_id": bob.role_id}, "foreign-open")
        self.assertEqual(a.query(alice, "commons.conversation.list", {})["result"]["conversations"], [])
        self.assertFalse(a.runtime.get_state("commons-a", "commons:conversations", "conversation:foreign")["exists"])
        with a.runtime._conn(readonly=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operations WHERE universe=? AND idempotency_key=?",
                                          ("commons-a", "foreign-open")).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE universe=? AND recipient_role_id=?",
                                          ("commons-a", bob.role_id)).fetchone()[0], 0)

    def test_operator_entry_records_bind_profiles_without_new_membership_model(self):
        runtime = self.a.runtime
        entry = {
            "token": lambda role: runtime.issue_identity_token("b", role),
            "ticket": lambda role: runtime.issue_join_ticket("b", role),
            "presence": lambda role: runtime.bootstrap("b", role, record_presence=True),
        }
        for name, register in entry.items():
            with self.subTest(entry=name):
                role = runtime.create_role("operator " + name)["role_id"]
                register(role)
                with self.assertRaises(RoleNotFound):
                    self.a.query(self.alice, "profile.read", {"role_id": role})
                self.assertEqual(self.b.query(self.bob, "profile.read", {"role_id": role})
                                 ["result"]["profile"]["role_id"], role)
                runtime.issue_join_ticket("a", role)
                self.assertEqual(self.a.query(self.alice, "profile.read", {"role_id": role})
                                 ["result"]["profile"]["role_id"], role)

    def test_trusted_unbound_sdk_roles_and_global_operator_lookup_remain_available(self):
        runtime = self.a.runtime
        unbound = runtime.create_role("trusted fixture")["role_id"]
        self.assertEqual(runtime.query_function("a", "profile.read", unbound, {"role_id": unbound})
                         ["result"]["profile"]["display_name"], "trusted fixture")
        self.assertEqual(runtime.get_role(self.bob.role_id)["display_name"], "B private name")


if __name__ == "__main__":
    unittest.main()
