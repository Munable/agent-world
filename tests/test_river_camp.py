from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, SchemaRejected, StateConflict
from examples.river_camp import WORLD, make_world, required_cost


class RiverCampTests(unittest.TestCase):
    """Domain regressions; multi-key/concurrency/transport evidence is separate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="river-camp-")
        self.addCleanup(self.temp.cleanup)
        self.clock = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.path = Path(self.temp.name) / "camp.sqlite3"
        self.runtime = WorldRuntime(self.path)
        self.roles = {name: self.runtime.create_role(name)["role_id"] for name in ("founder", "judge", "alice", "bob", "outsider")}
        self.world = make_world(founder_role_id=self.roles["founder"], judge_role_ids=[self.roles["judge"]])
        self.runtime.install_world("river", self.world)
        self.n = 0
        for name in ("founder", "judge", "alice", "bob"):
            self.call(name, "camp.join", {})

    def call(self, name, function, args, operation_id=None):
        self.n += 1
        return self.runtime.call_function("river", function, self.roles[name], args,
                                          operation_id=operation_id or "op-" + str(self.n))

    def inspect(self, name="alice"):
        return self.runtime.query_function("river", "camp.inspect", self.roles[name], {})["result"]

    def propose(self, name="alice", intent_id="idea", **extra):
        return self.call(name, "camp.propose", {"intent_id": intent_id, "text": "Reinforce a cargo crossing with anchored timber", **extra})

    def ruling(self, intent_id="idea", effects=None, **extra):
        state = self.inspect("judge")
        intent = next(item for item in state["intents"] if item["key"] == intent_id)
        effects = effects or {"passage": "cargo", "stability": 2, "exposure": 1}
        return {"intent_id": intent_id, "expected_intent_version": intent["version"],
                "inventory_version": state["inventory"]["version"], "route_version": state["route"]["version"],
                "authority_epoch": state["authority"]["value"]["epoch"], "decision": "approve",
                "reason": "The proposed physical effect is feasible under the camp material rule",
                "effects": effects, "cost": required_cost(effects), **extra}

    def approve(self, intent_id="idea", effects=None):
        return self.call("judge", "camp.judge", self.ruling(intent_id, effects))["result"]

    def test_ordinary_entry_and_role_claims_cannot_grant_authority(self):
        self.call("outsider", "camp.join", {})
        self.propose("outsider")
        with self.assertRaises(PermissionDenied):
            self.call("outsider", "camp.judge", self.ruling())
        with self.assertRaises(PermissionDenied):
            self.call("bob", "camp.set_judge", {"role_id": self.roles["bob"], "enabled": True, "authority_epoch": 1})
        with self.assertRaises(SchemaRejected):
            self.call("bob", "camp.join", {"role": "judge"})
        self.assertEqual(self.inspect()["authority"]["value"]["judges"], [self.roles["judge"]])

    def test_unjoined_inspect_is_denied_and_default_world_is_safe(self):
        with self.assertRaises(PermissionDenied):
            self.inspect("outsider")
        self.runtime.install_world("safe-default", WORLD)
        self.runtime.call_function("safe-default", "camp.join", self.roles["outsider"], {}, operation_id="safe-join")
        self.assertEqual(self.runtime.get_state("safe-default", "camp:world", "authority")["value"],
                         {"founder_role_id": None, "judges": [], "epoch": 1})

    def test_public_exploration_does_not_create_materials(self):
        before = self.inspect()["inventory"]
        self.call("alice", "camp.explore", {"site": "bank"})
        state = self.inspect("bob")
        self.assertTrue(state["findings"]["value"]["bank"])
        self.assertEqual(state["inventory"], before)

    def test_proposal_is_pending_and_private_material_is_authorized(self):
        result = self.propose(private_note="PRIVATE surveying material")
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(self.inspect()["intents"][0]["value"]["status"], "pending")
        self.assertEqual(self.inspect("bob")["private_notes"], [])
        self.assertEqual(self.inspect("judge")["private_notes"][0]["value"]["content"], "PRIVATE surveying material")
        self.call("founder", "camp.set_judge", {"role_id": self.roles["judge"], "enabled": False, "authority_epoch": 1})
        self.assertEqual(self.inspect("judge")["private_notes"], [])
        self.assertEqual(self.inspect()["private_notes"][0]["value"]["content"], "PRIVATE surveying material")

    def test_judge_cannot_patch_or_fake_free_cost_and_owner_must_accept(self):
        self.propose()
        plan = self.ruling()
        with self.assertRaises(RuleViolation):
            self.call("judge", "camp.judge", {**plan, "cost": {"wood": 0, "rope": 0, "stone": 0}})
        with self.assertRaises(SchemaRejected):
            self.call("judge", "camp.judge", {**plan, "effects": {**plan["effects"], "bob_agreed": True}})
        approved = self.approve()
        self.assertEqual(self.inspect()["inventory"]["value"], {"wood": 6, "rope": 4, "stone": 3})
        with self.assertRaises(RuleViolation):
            self.call("bob", "camp.accept", {"intent_id": "idea", "expected_intent_version": approved["intent_version"]})
        result = self.call("alice", "camp.accept", {"intent_id": "idea", "expected_intent_version": approved["intent_version"]})
        self.assertEqual(result["result"]["inventory"], {"wood": 2, "rope": 1, "stone": 2})
        self.assertTrue(self.inspect("bob")["goal_reached"])

    def test_distinct_effects_have_distinct_costs_and_persistent_consequences(self):
        self.propose()
        approved = self.approve(effects={"passage": "foot", "stability": 1, "exposure": 2})
        result = self.call("alice", "camp.accept", {"intent_id": "idea", "expected_intent_version": approved["intent_version"]}, "cross")
        self.assertEqual(result["result"]["inventory"], {"wood": 4, "rope": 3, "stone": 3})
        self.assertFalse(result["result"]["goal_reached"])
        self.runtime = WorldRuntime(self.path)
        # Default reinstall preserves stored authority and historical pending/executed work.
        self.runtime.install_world("river", WORLD)
        self.assertEqual(self.inspect()["intents"][0]["value"]["status"], "executed")
        self.assertEqual(self.inspect()["authority"]["value"]["judges"], [self.roles["judge"]])
        replay = self.call("alice", "camp.accept", {"intent_id": "idea", "expected_intent_version": approved["intent_version"]}, "cross")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], result["result"])

    def test_revoked_judge_invalidates_already_approved_plan(self):
        self.propose()
        approved = self.approve()
        self.call("founder", "camp.set_judge", {"role_id": self.roles["judge"], "enabled": False, "authority_epoch": 1})
        before = self.inspect()
        with self.assertRaises(StateConflict):
            self.call("alice", "camp.accept", {"intent_id": "idea", "expected_intent_version": approved["intent_version"]})
        after = self.inspect()
        self.assertEqual(after["inventory"], before["inventory"])
        self.assertEqual(after["route"], before["route"])
        self.assertEqual(after["intents"], before["intents"])

    def test_declined_intent_retains_reason_and_can_receive_fresh_ruling(self):
        self.propose()
        args = self.ruling()
        args.update(decision="decline", reason="Requested effect is outside the bounded world")
        del args["cost"], args["effects"]
        declined = self.call("judge", "camp.judge", args)["result"]
        with self.assertRaises(RuleViolation):
            self.call("alice", "camp.accept", {"intent_id": "idea", "expected_intent_version": declined["intent_version"]})
        approved = self.approve()
        self.assertEqual(approved["intent"]["status"], "approved")

    def test_completed_project_cancels_expiration_and_late_work_expires(self):
        start = self.call("alice", "camp.start_project", {"project_id": "survey", "title": "Finish a survey", "seconds": 2})["result"]
        self.clock.return_value = 1001.0
        self.call("alice", "camp.complete_project", {"project_id": "survey", "expected_project_version": start["project_version"]})
        self.clock.return_value = 1004.0
        self.assertEqual(self.runtime.run_due_timers("river")["count"], 0)
        self.assertEqual(self.runtime.get_timer("river", "camp-project:survey")["status"], "cancelled")
        late = self.call("alice", "camp.start_project", {"project_id": "late", "title": "Prepare markers", "seconds": 1})["result"]
        self.clock.return_value = 1005.0
        with self.assertRaises(RuleViolation):
            self.call("alice", "camp.complete_project", {"project_id": "late", "expected_project_version": late["project_version"]})
        self.runtime = WorldRuntime(self.path)
        self.runtime.install_world("river", WORLD)
        self.assertEqual(self.runtime.run_due_timers("river")["count"], 1)
        self.assertEqual(self.runtime.get_state("river", "camp:projects", "late")["value"]["status"], "expired")
        self.assertEqual(self.runtime.run_due_timers("river")["count"], 0)


if __name__ == "__main__":
    unittest.main()
