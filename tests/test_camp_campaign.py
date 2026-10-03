"""Finite continuation evidence under formal Key Identity, without model calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from agent_world import FunctionOutcome, WorldRuntime
from agent_world.errors import PermissionDenied, ResultRejected, RuleViolation, SchemaRejected, StateConflict
from examples.camp_campaign import WORLD, make_world
from examples.river_camp import required_cost
from tests.capability_harness import RuntimeCapabilityHarness
from tests.test_river_camp_combinations import managed_records


class CampaignHarness(RuntimeCapabilityHarness):
    def __init__(self, db_path):
        self.db_path, self.universe = Path(db_path), "campaign"
        self.runtime = WorldRuntime(self.db_path)
        self.participants = self.spawn(4)
        self.world = make_world(founder_role_id=self.participants[0].role_id,
                               judge_role_ids=(self.participants[3].role_id,))
        self.runtime.install_world(self.universe, self.world)


class CampCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="camp-campaign-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "world.sqlite3"
        self.reset_world(self.path)

    def reset_world(self, path):
        self.path = Path(path)
        self.h = CampaignHarness(self.path)
        self.p = self.h.participants
        self.n = 0
        for index, participant in enumerate(self.p):
            self.h.call(participant, "camp.join", {}, "join-" + str(index))

    def call(self, actor, function, args, operation_id=None):
        self.n += 1
        return self.h.call(self.p[actor], function, args, operation_id or "op-" + str(self.n))

    def inspect(self, actor=0):
        return self.h.query(self.p[actor], "camp.inspect", {})["result"]

    def action_args(self, function, actor=0, **selector):
        actions = self.inspect(actor)["campaign"]["available_actions"]
        return next(item["arguments"] for item in actions if item["function"] == function
                    and all(item["arguments"].get(key) == value for key, value in selector.items()))

    def action(self, actor, function, **selector):
        return self.call(actor, function, self.action_args(function, actor, **selector))

    def bridge(self, *, foot_first=True, effects=None):
        effects = effects or {"passage": "cargo", "stability": 2, "exposure": 1}
        plans = ([{"passage": "foot", "stability": 1, "exposure": 2}] if foot_first else []) + [effects]
        for index, effect in enumerate(plans):
            self.call(index, "camp.propose", {"intent_id": "bridge-" + str(index), "text": "Commit a physical crossing with timber and anchors", "private_note": "PRIVATE-material"})
            state = self.inspect(3)
            intent = next(item for item in state["intents"] if item["key"] == "bridge-" + str(index))
            approved = self.call(3, "camp.judge", {"intent_id": intent["key"], "expected_intent_version": intent["version"],
                "inventory_version": state["inventory"]["version"], "route_version": state["route"]["version"],
                "authority_epoch": state["authority"]["value"]["epoch"], "decision": "approve", "reason": "Within the finite material formula",
                "effects": effect, "cost": required_cost(effect)})["result"]
            self.call(index, "camp.accept", {"intent_id": intent["key"], "expected_intent_version": approved["intent_version"]})

    def flood(self, order=("timber", "quarry")):
        for actor, name in enumerate(order):
            self.action(actor, "campaign.supply", shipment_id=name)

    def test_default_authority_membership_and_client_completion_are_safe(self):
        self.h.runtime.install_world("default", WORLD)
        default = RuntimeCapabilityHarness(self.path, WORLD, "default")
        participant = default.enter()
        default.call(participant, "camp.join", {}, "default-join")
        self.assertEqual(self.h.runtime.get_state("default", "camp:world", "authority")["value"],
                         {"founder_role_id": None, "judges": [], "epoch": 1})
        outsider = self.h.enter()
        with self.assertRaises(PermissionDenied):
            self.h.call(outsider, "campaign.observe_water", {"expected_campaign_version": 1}, "unjoined")
        with self.assertRaises(SchemaRejected):
            self.call(0, "campaign.choose_goal", {**self.action_args("campaign.choose_goal", choice="build"), "completed": True})
        with self.assertRaises(RuleViolation):
            self.action(0, "campaign.supply", shipment_id="timber")
        self.assertFalse(self.inspect()["campaign"]["goal"]["reached"])

    def test_prepared_path_legal_sources_costs_personals_and_terminal_facts(self):
        self.bridge()
        self.assertTrue(self.inspect()["route_goal_reached"])
        self.assertFalse(self.inspect()["goal_reached"])
        self.assertEqual(self.inspect()["inventory"]["value"], {"wood": 0, "rope": 0, "stone": 2})
        for actor, choice in enumerate(("supply", "build", "watch")):
            self.action(actor, "campaign.choose_goal", choice=choice)
        self.action(2, "campaign.observe_water")
        self.action(0, "campaign.supply", shipment_id="timber")
        self.action(1, "campaign.build_storehouse")
        self.action(0, "campaign.supply", shipment_id="quarry")
        state = self.inspect()
        self.assertEqual(state["inventory"]["value"], {"wood": 3, "rope": 2, "stone": 3})
        self.assertEqual(state["route"]["value"], {"passage": "blocked", "stability": 1, "exposure": 2})
        event = state["campaign"]["activity"][-1]
        self.assertEqual(event["details"]["loss"], {"wood": 0, "rope": 0, "stone": 0})
        self.assertEqual(event["actor_role_id"], self.p[0].role_id)
        self.action(2, "campaign.repair", plan="anchored")
        final = self.inspect(1)
        self.assertTrue(final["campaign"]["goal"]["reached"])
        self.assertEqual(final["inventory"]["value"], {"wood": 2, "rope": 0, "stone": 1})
        self.assertEqual(final["route"]["value"], {"passage": "cargo", "stability": 3, "exposure": 0})
        personals = {item["role_id"]: item for item in final["campaign"]["personals"]}
        for participant in self.p[:3]:
            self.assertTrue(personals[participant.role_id]["completed"])
        self.assertFalse(personals[self.p[3].role_id]["completed"])
        self.assertNotIn("PRIVATE-material", str(final["campaign"]))
        self.assertNotIn("profile", str(final["campaign"]))
        self.assertEqual(len([item for item in final["campaign"]["activity"] if item["details"].get("story_event")]), 1)

    def test_unprepared_losses_are_real_both_repairs_then_crate_and_warehouse_can_finish(self):
        for plan, after_repair, after_finish in (
            ("quick", {"wood": 2, "rope": 1, "stone": 3}, {"wood": 1, "rope": 1, "stone": 2}),
            ("anchored", {"wood": 3, "rope": 0, "stone": 2}, {"wood": 2, "rope": 0, "stone": 1}),
        ):
            with self.subTest(plan=plan):
                if plan == "anchored":
                    self.reset_world(Path(self.temp.name) / "anchored.sqlite3")
                self.bridge()
                self.flood(order=("quarry", "timber"))
                state = self.inspect()
                self.assertEqual(state["inventory"]["value"], {"wood": 4, "rope": 2, "stone": 4})
                self.assertEqual(state["campaign"]["activity"][-1]["details"]["loss"], {"wood": 2, "rope": 1, "stone": 0})
                self.action(2, "campaign.repair", plan=plan)
                state = self.inspect()
                self.assertEqual(state["campaign"]["stage"], "repaired")
                self.assertFalse(state["campaign"]["goal"]["reached"])
                self.assertEqual(state["inventory"]["value"], after_repair)
                with self.assertRaises(RuleViolation):
                    self.action(1, "campaign.build_storehouse")
                self.action(1, "campaign.salvage_crate")
                self.action(1, "campaign.build_storehouse")
                self.assertEqual(self.inspect()["inventory"]["value"], after_finish)
                self.assertTrue(self.inspect()["campaign"]["goal"]["reached"])
                with self.assertRaises(RuleViolation):
                    self.action(0, "campaign.salvage_crate")
                with self.assertRaises(RuleViolation):
                    self.action(0, "campaign.repair", plan=plan)

    def test_build_after_flood_temporary_shortage_recovers_with_one_crate(self):
        self.bridge()
        self.flood()
        self.action(0, "campaign.build_storehouse")
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaises(RuleViolation):
            self.action(1, "campaign.repair", plan="quick")
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        self.action(2, "campaign.salvage_crate")
        self.action(1, "campaign.repair", plan="quick")
        self.assertTrue(self.inspect()["campaign"]["goal"]["reached"])

    def test_all_supply_orders_warehouse_timings_and_repairs_have_finite_completion(self):
        cases = [(order, timing, plan) for order in (("timber", "quarry"), ("quarry", "timber"))
                 for timing in ("before_flood", "after_flood", "after_repair") for plan in ("quick", "anchored")]
        for index, (order, timing, plan) in enumerate(cases):
            with self.subTest(order=order, timing=timing, plan=plan):
                self.reset_world(Path(self.temp.name) / ("branch-" + str(index) + ".sqlite3"))
                self.bridge()
                self.action(0, "campaign.supply", shipment_id=order[0])
                prepared = timing == "before_flood" and order[0] == "timber"
                if prepared:
                    self.action(1, "campaign.build_storehouse")
                elif timing == "before_flood":
                    # Quarry-first has too little timber to protect before the flood.
                    before = managed_records(self.h.runtime, self.h.universe)
                    with self.assertRaises(RuleViolation):
                        self.action(1, "campaign.build_storehouse")
                    self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
                self.action(0, "campaign.supply", shipment_id=order[1])
                if not prepared and timing != "after_repair":
                    self.action(1, "campaign.salvage_crate")
                    self.action(1, "campaign.build_storehouse")
                self.action(2, "campaign.repair", plan=plan)
                if not prepared and timing == "after_repair":
                    self.action(1, "campaign.salvage_crate")
                    self.action(1, "campaign.build_storehouse")
                final = self.inspect()
                self.assertTrue(final["goal_reached"])
                self.assertTrue(final["route_goal_reached"])
                self.assertEqual(final["inventory"]["value"], {"wood": 1, "rope": 1, "stone": 2} if plan == "quick"
                                 else {"wood": 2, "rope": 0, "stone": 1})
                activities = final["campaign"]["activity"]
                self.assertEqual(sum(item["action"] == "campaign.supply" for item in activities), 2)
                self.assertEqual(sum(item["action"] == "campaign.build_storehouse" for item in activities), 1)
                self.assertEqual(sum(item["action"] == "campaign.repair" for item in activities), 1)
                self.assertEqual(sum(item["action"] == "campaign.salvage_crate" for item in activities), 0 if prepared else 1)
                # Every unit has a declared finite source or an executed cost/loss.
                ledger = {"wood": 6, "rope": 4, "stone": 3}
                for item in activities:
                    details = item["details"]
                    for name in ledger:
                        ledger[name] += details.get("received", {}).get(name, 0)
                        ledger[name] -= details.get("cost", {}).get(name, 0) + details.get("loss", {}).get(name, 0)
                self.assertEqual(ledger, final["inventory"]["value"])

    def test_route_milestone_receipt_and_events_do_not_claim_campaign_complete(self):
        self.bridge()
        self.assertTrue(self.inspect()["route_goal_reached"])
        self.assertFalse(self.inspect()["goal_reached"])
        events = self.h.changes(self.p[0])["events"]
        executed = [event["payload"] for event in events if event["kind"] == "camp.executed"]
        self.assertEqual(len(executed), 2)
        self.assertFalse(any(event["goal_reached"] for event in executed))
        self.assertTrue(executed[-1]["route_goal_reached"])

    def test_replay_and_same_key_restart_do_not_double_sources_or_flood(self):
        self.bridge()
        self.action(0, "campaign.supply", shipment_id="timber")
        args = self.action_args("campaign.supply", shipment_id="quarry")
        original = self.call(1, "campaign.supply", args, "last-shipment")
        state = self.inspect()
        self.h.runtime = WorldRuntime(self.path)
        self.h.runtime.install_world(self.h.universe, WORLD)
        self.p[1] = self.h.reenter(self.p[1])
        replay = self.call(1, "campaign.supply", args, "last-shipment")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], original["result"])
        self.assertEqual(self.inspect()["campaign"], state["campaign"])
        self.assertEqual(self.inspect()["authority"], state["authority"])
        fresh = self.action_args("campaign.supply", shipment_id="quarry")
        with self.assertRaises(RuleViolation):
            self.call(0, "campaign.supply", fresh)

    def test_concurrent_same_batch_only_one_source_and_contribution_commits(self):
        self.bridge()
        args = self.action_args("campaign.supply", shipment_id="timber")
        barrier = threading.Barrier(2)

        def claim(actor):
            barrier.wait()
            try:
                return self.call(actor, "campaign.supply", args, "claim-" + str(actor))
            except StateConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (0, 1)))
        self.assertEqual(sum(isinstance(item, StateConflict) for item in results), 1)
        state = self.inspect()
        self.assertEqual(state["inventory"]["value"], {"wood": 4, "rope": 2, "stone": 2})
        self.assertEqual(sum(person["contributions"]["shipments"] for person in state["campaign"]["personals"]), 1)
        self.assertEqual(len([item for item in state["campaign"]["activity"] if item["action"] == "campaign.supply"]), 1)

    def test_every_object_version_and_failed_result_preserve_all_effects(self):
        self.bridge()
        self.action(0, "campaign.supply", shipment_id="timber")
        args = self.action_args("campaign.supply", shipment_id="quarry")
        for field in ("expected_campaign_version", "inventory_version", "route_version"):
            with self.subTest(field=field), self.assertRaises(StateConflict):
                self.call(1, "campaign.supply", {**args, field: args[field] + 1})
        original = next(spec.handler for spec in self.h.world.functions if spec.name == "campaign.supply")

        def invalid_output(ctx, arguments):
            outcome = original(ctx, arguments)
            return FunctionOutcome({"invalid": float("nan")}, outcome.events)

        bad = replace(self.h.world, functions=tuple(replace(spec, handler=invalid_output)
                      if spec.name == "campaign.supply" else spec for spec in self.h.world.functions))
        self.h.runtime.install_world(self.h.universe, bad)
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaises(ResultRejected):
            self.call(1, "campaign.supply", args, "rejected-shipment")
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        self.h.runtime.install_world(self.h.universe, self.h.world)
        self.call(1, "campaign.supply", args, "rejected-shipment")
        self.assertTrue(self.inspect()["campaign"]["flood_applied"])

    def test_personal_completion_is_contribution_derived_and_goal_choice_is_once(self):
        self.bridge()
        self.action(0, "campaign.choose_goal", choice="watch")
        self.action(0, "campaign.observe_water")
        self.flood()
        self.action(1, "campaign.repair", plan="quick")
        personal = next(item for item in self.inspect()["campaign"]["personals"] if item["role_id"] == self.p[0].role_id)
        self.assertFalse(personal["completed"])
        with self.assertRaises(RuleViolation):
            self.action(0, "campaign.choose_goal", choice="supply")
        with self.assertRaises(RuleViolation):
            self.action(0, "campaign.observe_water")

    def test_old_bridge_accept_is_locked_after_first_shipment(self):
        self.bridge(foot_first=False)
        self.call(0, "camp.propose", {"intent_id": "late", "text": "Make another bridge"})
        state = self.inspect(3)
        approved = self.call(3, "camp.judge", {"intent_id": "late", "expected_intent_version": 1,
            "inventory_version": state["inventory"]["version"], "route_version": state["route"]["version"],
            "authority_epoch": 1, "decision": "approve", "reason": "A foot route is affordable", "effects": {"passage": "foot", "stability": 1, "exposure": 2},
            "cost": {"wood": 2, "rope": 1, "stone": 0}})["result"]
        self.action(1, "campaign.supply", shipment_id="timber")
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaisesRegex(RuleViolation, "closed bridge planning"):
            self.call(0, "camp.accept", {"intent_id": "late", "expected_intent_version": approved["intent_version"]})
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        with self.assertRaisesRegex(RuleViolation, "bridge proposal stage closed"):
            self.call(0, "camp.propose", {"intent_id": "impossible-new", "text": "A new bridge after the stage closed"})
        with self.assertRaisesRegex(RuleViolation, "bridge proposal stage closed"):
            self.call(3, "camp.judge", {"intent_id": "late", "expected_intent_version": approved["intent_version"],
                "inventory_version": self.inspect()["inventory"]["version"], "route_version": state["route"]["version"],
                "authority_epoch": 1, "decision": "decline", "reason": "A historical intent"})
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)


if __name__ == "__main__":
    unittest.main()
