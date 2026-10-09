"""Complete user paths through shared materials; model/UI evidence is separate."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_world import FunctionOutcome, FunctionSpec, WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.worlds.shared_grove import WORLD
from agent_world.worlds.shared_grove import model as m
from agent_world.worlds.shared_grove import processes
from agent_world.worlds.shared_grove.catalog import template
from agent_world.worlds.shared_grove.generation import COOLDOWN_SECONDS


class SharedGroveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="shared-grove-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = self.base / "world.sqlite3"
        self.clock = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.world = WorldRuntime(self.db)
        self.world.install_world("u", WORLD)
        self.roles = {name: self.world.create_role(name)["role_id"] for name in "ABCD"}
        self.number = 0
        for actor in self.roles:
            self.call(actor, "grove.join", {"name": actor})

    def call(self, actor, function, arguments, operation=None):
        self.number += 1
        return self.world.call_function("u", function, self.roles[actor], arguments,
            operation_id=operation or f"grove-{self.number}")["result"]

    def view(self, actor="A"):
        return self.world.query_function("u", "map.viewport", self.roles[actor], {})["result"]

    def entity(self, entity_id):
        row = self.world.get_state("u", m.ENTITIES, entity_id)
        return {**row["value"], "version": row["version"]}

    def quantity(self, entity_id, material, slot=None):
        return sum(lot["quantity"] for lot in self.entity(entity_id)["matter"]
                   if lot["material"] == material and (slot is None or lot["slot"] == slot))

    def item(self, actor, name):
        return next(e for e in self.view(actor)["inventory"] if e["name"] == name)

    def move(self, actor, dx, dy):
        return self.call(actor, "grove.move", {"dx": dx, "dy": dy,
            "expected_player_version": self.view(actor)["player"]["version"]})

    def walk(self, actor, x, y):
        # Explicit human-selected cardinal route: vertical first, then horizontal.
        while self.view(actor)["player"]["y"] != y:
            self.move(actor, 0, 1 if y > self.view(actor)["player"]["y"] else -1)
        while self.view(actor)["player"]["x"] != x:
            self.move(actor, 1 if x > self.view(actor)["player"]["x"] else -1, 0)

    def act(self, actor, action, target=None, item=None, amount=None, operation=None):
        args = {"action": action, "expected_player_version": self.view(actor)["player"]["version"]}
        if target:
            args.update(target_id=target, expected_target_version=self.entity(target)["version"])
        if item:
            args["item_id"] = item
            args["expected_item_version"] = self.entity(item)["version"]
        if amount is not None:
            args["amount"] = amount
        return self.call(actor, "grove.interact", args, operation)

    def ticks(self, count):
        for _ in range(count):
            self.clock.return_value += 1
            result = self.world.run_due_timers("u")
            self.assertEqual(result["count"], 1, result)
            self.assertEqual(result.get("failed", 0), 0, result)

    def publish(self, actor, content):
        context = self.call(actor, "grove.generation_context", {"reason": "user_request", "kind": content["kind"]})
        opportunity = context["opportunity"]
        return self.call(actor, "grove.content_submit", {"opportunity_id": opportunity["id"],
            "expected_opportunity_version": opportunity["version"], "content": content})

    def test_four_entry_views_and_private_ownership(self):
        for actor in self.roles:
            view = self.view(actor)
            self.assertTrue(any("water_source" in e["capabilities"] for e in view["entities"]))
            self.assertTrue(any("blocking" in e["capabilities"] for e in view["entities"]))
            self.assertTrue(any("container" in e["capabilities"] for e in view["entities"]))
            self.assertTrue(all(c["seed"] == m.SEED and c["generator_version"] == m.GENERATOR_VERSION for c in view["chunks"]))
            self.assertTrue(all(e["owner"] == self.roles[actor] for e in view["inventory"]))
        other = self.item("B", "水壶")
        with self.assertRaises(PermissionDenied):
            self.world.query_function("u", "grove.inspect", self.roles["A"], {"entity_id": other["id"]})
        with self.assertRaises(RuleViolation):
            self.move("A", 1, 1)
        old = self.view()["player"]["version"]
        self.move("A", 1, 0)
        with self.assertRaises(StateConflict):
            self.call("A", "grove.move", {"dx": 1, "dy": 0, "expected_player_version": old})

    def test_other_author_continues_same_public_anchor_after_ordinary_wet_tick(self):
        # This deterministic contract fixture checks a leased observation. Its
        # controlled cooldown/ticks are not human play or elapsed-day evidence.
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        opportunity = context["opportunity"]
        tile = next(point for point in context["allowed_tiles"] if point["x"] <= 5)
        first = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [{"composition": {
                "material": "wood", "construction": "vessel", "size": 1}, **tile}]}})
        anchor_id = first["entities"][0]["id"]
        content_id = self.view()["public_content"]["id"]
        self.clock.return_value += COOLDOWN_SECONDS+1
        self.publish("C", {"kind": "rain", "rain": {"coverage_template": "local_region",
            "duration_ticks": 4, "intensity": 10, "start_delay_ticks": 2}})
        self.clock.return_value += COOLDOWN_SECONDS+1
        continuation = self.call("B", "grove.generation_context", {"kind": "layout"})
        compatible = next(item for item in continuation["compatible_parts"]
                          if item["anchor_entity_id"] == anchor_id)
        observed_version = compatible["observed_anchor_version"]
        # The first due timer pauses across the controlled fixture gap; only
        # the following three steps advance rain, with no offline catch-up.
        self.ticks(4)
        self.assertGreater(self.entity(anchor_id)["version"], observed_version)
        self.assertGreater(self.entity(anchor_id)["state"]["wetness"], 0)
        next_tile = next(point for point in continuation["allowed_tiles"] if point["x"] <= 5)
        basis = continuation["continuation"]
        lease = continuation["opportunity"]
        second = self.call("B", "grove.content_submit", {
            "opportunity_id": lease["id"], "expected_opportunity_version": lease["version"],
            "continuation": {"content_id": basis["content_id"],
                "expected_content_version": basis["expected_content_version"],
                "anchor_entity_id": anchor_id, "observed_anchor_version": observed_version},
            "content": {"kind": "layout", "objects": [{
                "composition": compatible["compositions"][0], **next_tile}]}})
        public = self.view("B")["public_content"]
        self.assertEqual(public["id"], content_id)
        self.assertIn(anchor_id, [item["entity_id"] for item in public["anchors"]])
        self.assertIn(second["entities"][0]["id"], [item["entity_id"] for item in public["anchors"]])
        record = public["contributions"][-1]
        self.assertEqual(record["author"], self.roles["B"])
        self.assertEqual(record["based_on"]["anchor_entity_id"], anchor_id)
        self.assertIsNone(self.entity(second["entities"][0]["id"])["physical_parent"])

    def test_two_users_contend_for_one_public_object(self):
        self.walk("A", 3, 4)
        self.walk("B", 4, 3)
        target = "seed_0_0_4_4"
        args = {actor: {"action": "pickup", "target_id": target,
                "expected_target_version": self.entity(target)["version"],
                "expected_player_version": self.view(actor)["player"]["version"]} for actor in "AB"}
        def take(actor):
            try:
                self.world.call_function("u", "grove.interact", self.roles[actor], args[actor], operation_id="take-"+actor)
                return actor
            except (StateConflict, RuleViolation, PermissionDenied):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [v for v in pool.map(take, "AB") if v]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertEqual(self.entity(target)["owner"], self.roles[winner])
        self.assertEqual(sum(target in [e["id"] for e in self.view(a)["inventory"]] for a in "AB"), 1)
        self.act(winner, "drop", item=target)
        self.assertIsNone(self.entity(target)["owner"])
        self.assertTrue(any(e["id"] == target for e in self.view("A")["entities"]))

    def test_container_handoff_and_fill_pour_extinguish(self):
        self.walk("A", 4, 5)
        crate = "seed_0_0_5_5"
        wood = self.item("A", "木柴")["id"]
        self.act("A", "store", crate, wood)
        self.act("A", "close", crate)
        seen = self.world.query_function("u", "grove.inspect", self.roles["A"], {"entity_id": crate})["result"]
        self.assertEqual(seen["contents"], [])
        with self.assertRaises(RuleViolation):
            self.act("A", "retrieve", crate, wood)
        self.walk("B", 5, 3)
        self.move("B", 0, 1)
        self.act("B", "open", crate)
        self.act("B", "retrieve", crate, wood)
        self.assertEqual(self.entity(wood)["owner"], self.roles["B"])
        self.act("B", "drop", item=wood)
        self.act("B", "ignite", wood, self.item("B", "打火石")["id"])
        self.ticks(1)
        self.assertTrue(self.entity(wood)["state"]["burning"])
        self.act("B", "pour", wood, self.item("B", "水壶")["id"], 5)
        self.assertFalse(self.entity(wood)["state"]["burning"])
        with self.assertRaises(RuleViolation):
            self.act("B", "ignite", wood, self.item("B", "打火石")["id"])
        self.walk("B", 7, 1)
        source, bottle = "water_0_0", self.item("B", "水壶")["id"]
        before = self.entity(source)["state"]["water_remaining"]
        self.act("B", "fill", source, bottle, 7)
        self.assertEqual(self.entity(source)["state"]["water_remaining"], before-7)

    def test_pour_surface_offer_caps_to_remaining_room_and_default_extinguishes(self):
        wood = self.item("A", "木柴")["id"]
        bottle = self.item("A", "水壶")["id"]
        self.walk("A", 5, -2)
        self.act("A", "drop", item=wood)
        self.act("A", "ignite", wood, self.item("A", "打火石")["id"])
        self.ticks(3)  # One ordinary combustion settlement, not 3 burns.
        result = self.act("A", "pour", wood, bottle, 18)
        self.assertIn("本次燃烧已熄灭。", result["changes"][0])
        self.assertFalse(self.entity(wood)["state"]["burning"])
        seen = self.world.query_function("u", "grove.inspect", self.roles["A"], {"entity_id": wood})["result"]
        option = next(option for option in seen["action_options"] if option["action"] == "pour")
        self.assertEqual(option["input"]["quantity"]["max"], 1)
        before = self.quantity(bottle, "water")
        with self.assertRaises(RuleViolation):
            self.act("A", "pour", wood, bottle, 2)
        self.assertEqual(self.quantity(bottle, "water"), before)
        result = self.act("A", "pour", wood, bottle)
        self.assertNotIn("本次燃烧已熄灭。", result["changes"][0])
        self.assertEqual(self.quantity(bottle, "water"), before-1)
        self.assertEqual(self.quantity(wood, "water", "surface"), 19)
        seen = self.world.query_function("u", "grove.inspect", self.roles["A"], {"entity_id": wood})["result"]
        option = next(option for option in seen["action_options"] if option["action"] == "pour")
        self.assertFalse(option["available"])
        self.assertNotIn("quantity", option["input"])

    def test_explicit_pour_revalidates_fixed_intent_after_physics_without_double_debit(self):
        wood = self.item("A", "木柴")["id"]
        bottle = self.item("A", "水壶")["id"]
        cloth = self.item("A", "干布")["id"]
        self.walk("A", 5, -2)
        self.act("A", "drop", item=wood)
        self.act("A", "ignite", wood, self.item("A", "打火石")["id"])
        original = {"action": "pour", "target_id": wood, "item_id": bottle,
            "amount": 5, "target_slot": "surface",
            "expected_target_version": self.entity(wood)["version"],
            "expected_item_version": self.entity(bottle)["version"]}
        # A previous water use and a normal fuel settlement change both versions.
        # The named 5u intent is still legal, and does not need a new target.
        self.act("A", "pour", cloth, bottle, 1)
        self.ticks(2)
        self.assertEqual(self.quantity(wood, "wood"), 40)
        seen = self.world.query_function("u", "grove.inspect", self.roles["A"], {"entity_id": wood})["result"]
        self.assertEqual(seen["entity"]["simulation"]["combustion"]["cadence_ticks"], 3)
        self.assertEqual(seen["entity"]["simulation"]["combustion"]["ticks_until_next_consumption"], 1)
        self.ticks(1)
        self.assertEqual(self.quantity(wood, "wood"), 38)
        original["expected_player_version"] = self.view()["player"]["version"]
        receipt = self.call("A", "grove.interact", original, "fixed-pour")
        self.assertFalse(self.entity(wood)["state"]["burning"])
        self.assertEqual(self.quantity(bottle, "water"), 14)
        self.assertEqual(self.call("A", "grove.interact", original, "fixed-pour"), receipt)
        self.assertEqual(self.quantity(bottle, "water"), 14)
        # No amount adjustment when the source cannot satisfy the original intent.
        args = {**original, "amount": 15, "expected_player_version": self.view()["player"]["version"]}
        with self.assertRaises(RuleViolation) as denied:
            self.call("A", "grove.interact", args)
        self.assertEqual(denied.exception.details["water_transfer"]["source_remaining"], 14)
        self.assertEqual(denied.exception.details["water_transfer"]["requested"], 15)
        self.assertEqual(self.quantity(bottle, "water"), 14)
        # The ordinary strict version contract remains for whole-object movement.
        with self.assertRaises(StateConflict):
            self.call("A", "grove.interact", {**original, "action": "pickup",
                "expected_player_version": self.view()["player"]["version"]})

    def test_explicit_pour_refuses_changed_slot_capacity_distance_or_ownership(self):
        bottle = self.item("A", "水壶")["id"]
        self.walk("A", 4, 5)
        crate = "seed_0_0_5_5"
        original = {"action": "pour", "target_id": crate, "item_id": bottle, "amount": 5,
            "target_slot": "contained", "expected_target_version": self.entity(crate)["version"],
            "expected_item_version": self.entity(bottle)["version"]}
        self.act("A", "close", crate)
        original["expected_player_version"] = self.view()["player"]["version"]
        with self.assertRaises(StateConflict):
            self.call("A", "grove.interact", original)
        self.assertEqual(self.quantity(bottle, "water"), 20)
        self.act("A", "open", crate)
        # Full current capacity is enforced even with an older observed version.
        bowl = "seed_0_0_2_6"
        self.walk("A", 2, 5)
        self.act("A", "pour", bowl, bottle, 20)
        original.update(target_id=bowl, expected_target_version=1,
            expected_player_version=self.view()["player"]["version"])
        with self.assertRaises(RuleViolation):
            self.call("A", "grove.interact", original)
        self.assertEqual(self.quantity(bowl, "water"), 20)
        self.walk("A", 7, 1)
        self.act("A", "fill", "water_0_0", bottle, 10)
        original.update(target_id=crate, expected_player_version=self.view()["player"]["version"])
        with self.assertRaises(RuleViolation):
            self.call("A", "grove.interact", original)
        self.assertEqual(self.quantity(bottle, "water"), 10)
        original.update(target_id=self.item("B", "干布")["id"])
        with self.assertRaises(PermissionDenied):
            self.call("A", "grove.interact", original)
        self.assertEqual(self.quantity(bottle, "water"), 10)

    def test_pour_container_default_respects_one_remaining_unit(self):
        self.walk("B", 2, 5)
        bowl, bottle = "seed_0_0_2_6", self.item("B", "水壶")["id"]
        self.act("B", "pour", bowl, bottle, 19)
        seen = self.world.query_function("u", "grove.inspect", self.roles["B"], {"entity_id": bowl})["result"]
        option = next(option for option in seen["action_options"] if option["action"] == "pour")
        self.assertEqual(option["input"]["quantity"]["max"], 1)
        self.act("B", "pour", bowl, bottle)
        self.assertEqual(self.quantity(bowl, "water", "contained"), 20)
        self.assertEqual(self.quantity(bottle, "water", "contained"), 0)

    def test_destroy_container_once_and_clear_material_obstacle(self):
        self.walk("A", 4, 5)
        crate, barrier = "seed_0_0_5_5", "seed_0_0_4_4"
        stone = self.item("A", "打火石")["id"]
        self.act("A", "store", crate, stone)
        wood = self.item("A", "木柴")["id"]
        self.act("A", "store", crate, wood)
        contained = self.world.query_function("u", "grove.inspect", self.roles["A"],
            {"entity_id": wood})["result"]
        self.assertNotIn("ignite", contained["available_actions"])
        # B retains its own ignition tool; ownership is independent of container contents.
        self.walk("B", 5, 3)
        self.move("B", 0, 1)
        cloth = self.item("B", "干布")["id"]
        self.act("B", "drop", item=cloth)
        self.act("B", "ignite", crate, self.item("B", "打火石")["id"])
        burning = self.world.query_function("u", "grove.inspect", self.roles["B"],
            {"entity_id": crate})["result"]
        self.assertNotIn("store", burning["available_actions"])
        self.assertNotIn("retrieve", burning["available_actions"])
        self.assertIn("pour", burning["available_actions"])
        self.act("B", "ignite", barrier, self.item("B", "打火石")["id"])
        self.ticks(3)
        self.assertTrue(self.entity(cloth)["state"]["burning"])
        # Ordinary fuel and spread settle together every 3 physical steps;
        # the unchanged 2u rate takes 20 batches to consume the 40u crate.
        self.ticks(57)
        self.assertTrue(self.entity(crate)["state"]["destroyed"])
        released = self.entity(stone)
        self.assertIsNone(released["container"])
        self.assertEqual((released["x"], released["y"]), (5, 5))
        version = released["version"]
        self.ticks(3)
        self.assertEqual(self.entity(stone)["version"], version)
        # Both containers and obstacles use the same material combustion rules.
        self.assertTrue(self.entity(barrier)["state"]["destroyed"])
        self.move("A", 0, -1)
        self.assertEqual((self.view()["player"]["x"], self.view()["player"]["y"]), (4, 4))

    def test_generated_second_layout_same_rules_and_atomic_retry(self):
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        opportunity = context["opportunity"]
        x, y = context["allowed_tiles"][0]["x"], context["allowed_tiles"][0]["y"]
        args = {"opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
                "content": {"kind": "layout", "objects": [{"template": "wood_crate", "x": x, "y": y,
                    "name": "别人的箱子", "contents": [{"template": "stone"}]}]}}
        original = self.call("A", "grove.content_submit", args, "publish-one")
        retry = self.call("A", "grove.content_submit", args, "publish-one")
        self.assertEqual(original, retry)
        remaining = {material: sum(self.entity(node["id"])["state"]["stock"]
            for node in context["resource_nodes"] if node["resource_kind"] == material)
            for material in ("wood", "stone")}
        # The registered size-two wood vessel contains80u body, paid as two
        # actual40u pieces. Occupancy material_units is a different budget.
        self.assertEqual(template("wood_crate")["material_cost"], {"wood": 2})
        self.assertEqual(self.quantity(original["entities"][0]["id"], "wood", "body"), 80)
        self.assertEqual(remaining["wood"], context["resource_stock"]["wood"]-2)
        self.assertEqual(remaining["stone"], context["resource_stock"]["stone"]-1)
        entity = original["entities"][0]
        self.assertEqual(entity["source"], "agent")
        self.walk("B", x, y)
        self.act("B", "open", entity["id"])
        child = self.entity(entity["id"])["state"]["contents"][0]
        self.act("B", "retrieve", entity["id"], child)
        self.assertEqual(self.entity(child)["owner"], self.roles["B"])

    def test_rain_actual_moisture_and_drift_real_contents(self):
        rain = self.publish("A", {"kind": "rain", "rain": {"coverage_template": "local_region",
            "duration_ticks": 4, "intensity": 20, "start_delay_ticks": 2}})
        self.ticks(4)
        self.assertGreater(self.entity("seed_0_0_4_4")["state"]["wetness"], 0)
        # B explores another public region instead of waiting for a daily gate.
        self.walk("B", 2, 18)
        # The willow grove can pay wood and cloth, but has no stone node.
        drift = self.publish("B", {"kind": "drift", "drift": {"template": "wood_crate",
            "route_template": "local_bank", "duration_ticks": 8, "start_delay_ticks": 2,
            "contents": [{"template": "wood_stick"}, {"template": "cloth_roll"}]}})
        crate = drift["entities"][0]
        self.ticks(12)
        after = self.entity(crate["id"])
        self.assertNotEqual(m.terrain(after["x"], after["y"]), "water")
        self.assertEqual(len(after["state"]["contents"]), 2)
        self.walk("B", after["x"], after["y"])
        self.act("B", "open", after["id"])
        child = after["state"]["contents"][0]
        self.act("B", "retrieve", after["id"], child)
        self.assertEqual(self.entity(child)["owner"], self.roles["B"])

    def test_restart_downtime_pause_and_online_backup_recovery(self):
        self.walk("A", 3, 2)
        wood = self.item("A", "木柴")["id"]
        self.act("A", "drop", item=wood, operation="drop-durable")
        self.act("A", "ignite", wood, self.item("A", "打火石")["id"])
        self.ticks(2)
        fuel = self.entity(wood)["state"]["fuel"]
        self.clock.return_value += 300
        self.world = WorldRuntime(self.db)
        self.world.install_world("u", WORLD)
        self.world.run_due_timers("u")
        self.assertEqual(self.entity(wood)["state"]["fuel"], fuel)
        self.ticks(1)
        self.assertEqual(self.entity(wood)["state"]["fuel"], fuel-2)
        backup = self.base / "recovered.sqlite3"
        with sqlite3.connect(self.db) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        previous = self.view()
        self.world = WorldRuntime(backup)
        self.world.install_world("u", WORLD)
        self.assertEqual(self.view()["player"], previous["player"])
        self.assertEqual(self.entity(wood)["owner"], None)
        self.assertTrue(self.call("A", "grove.join", {"name": "new name ignored"})["resumed"])

    def test_legal_rain_over_many_objects_does_not_stop_world_processes(self):
        # A developer-authored second layout, below per-chunk/entity limits.
        # The fixture changes no runtime/material rules and is not shipped.
        ids = []
        def prepare(ctx, args):
            positions = ((2, 0), (3, 0), (2, 1), (3, 1))
            chunks = [m.ensure_chunk(ctx, cx, cy)["id"] for cx, cy in positions]
            seeded = sum(len(m.get_chunk(ctx, cx, cy)["entity_ids"]) for cx, cy in positions)
            count, extra = divmod(processes.MAX_WET_ENTITIES-seeded+bool(args.get("exceed")), len(positions))
            for index, (cx, cy) in enumerate(positions):
                land = [(cx*16+x, cy*16+y) for y in range(16) for x in range(16)
                        if m.tile_at(ctx, cx*16+x, cy*16+y) in {"grass", "dirt"}]
                for x, y in land[:count+(index < extra)]:
                    entity = m.spawn_entity(ctx, {"name": "雨中布料", "material": "cloth",
                        "capabilities": ["transport", "wettable"], "x": x, "y": y, "source": "terrain"})
                    ids.append(entity["id"])
            processes.start_rain(ctx, chunks, ticks=4, intensity=20, start_delay=0)
            return FunctionOutcome({"objects": len(ids), "physical_objects": seeded+len(ids)})
        self.world = WorldRuntime(self.base / "rain-layout.sqlite3")
        definition = replace(WORLD, functions=(*WORLD.functions, FunctionSpec("grove.fixture", prepare,
                              {"type": "object", "properties": {"exceed": {"type": "boolean"}},
                               "additionalProperties": False})))
        self.world.install_world("u", definition)
        self.roles["A"] = self.world.create_role("A")["role_id"]
        # Admission rejects excess physical work atomically; it must not slow
        # down already accepted seconds or poison the world's timer.
        with self.assertRaises(RuleViolation):
            self.call("A", "grove.fixture", {"exceed": True})
        ids.clear()
        prepared = self.call("A", "grove.fixture", {})
        self.assertEqual(prepared["physical_objects"], processes.MAX_WET_ENTITIES)
        self.ticks(7)
        self.assertTrue(all(self.quantity(eid, "water", "absorbed") == 26 for eid in ids))
        self.assertTrue(all(self.entity(eid)["state"]["wetness"] > 0 for eid in ids))
        self.assertEqual(self.world.get_state("u", m.META, "active_processes")["value"], [])

    def test_same_seconds_for_rain_drying_and_fuel_at_two_legal_densities(self):
        def simulate(dense):
            ids = {}
            def prepare(ctx, args):
                for cx, cy in ((2, 0), (3, 0), (2, 1)):
                    m.ensure_chunk(ctx, cx, cy)
                def land(cx, cy):
                    return [(cx*16+x, cy*16+y) for y in range(16) for x in range(16)
                            if m.tile_at(ctx, cx*16+x, cy*16+y) in {"grass", "dirt"}]
                def cloth(name, position, water=0):
                    matter = [{"id": "body", "material": "cloth", "quantity": 16,
                               "slot": "body", "role": "structural", "burning": False}]
                    if water:
                        matter.append({"id": "wet", "material": "water", "quantity": water,
                            "slot": "absorbed", "role": "substance", "bound_to": "body", "burning": False})
                    return m.spawn_entity(ctx, {"name": name, "material": "cloth", "capabilities": ["wettable"],
                        "matter": matter, "components": {"absorbent": {"capacity": 32, "rate": 2,
                            "supports": {"body": {"quantity": 16, "capacity": 32}}}}, "x": position[0], "y": position[1]})["id"]
                def fire(name, position):
                    return m.spawn_entity(ctx, {"name": name, "material": "wood", "capabilities": ["flammable"],
                        "matter": [{"id": "body", "material": "wood", "quantity": 20,
                            "slot": "body", "role": "structural", "burning": True}],
                        "components": {"combustion": {"rate": 2, "spread": False}, "absorbent": {"capacity": 10, "rate": 2,
                            "supports": {"body": {"quantity": 20, "capacity": 10}}}}, "x": position[0], "y": position[1]})["id"]
                rain_land, dry_land = land(2, 0), land(3, 0)
                fire_land = [point for point in land(2, 1)
                             if not m.public_entities_near(ctx, *point, radius=0)]
                seeded_rain = len(m.get_chunk(ctx, 2, 0)["entity_ids"])
                ids["rain"] = cloth("同雨布料", rain_land[0])
                ids["dry"] = cloth("同湿布料", dry_land[0], 13)
                ids["fire"] = fire("同燃木柴", fire_land[-1])
                if dense:
                    extra_fires = processes.MAX_BURNING_ENTITIES-1
                    remaining = processes.MAX_WET_ENTITIES-seeded_rain-3-extra_fires
                    rain_count = remaining//2
                    for position in rain_land[1:rain_count+1]:
                        cloth("雨区密度物件", position)
                    for position in dry_land[1:remaining-rain_count+1]:
                        cloth("额外湿物", position, 13)
                    for position in fire_land[:extra_fires]:
                        fire("隔开的额外火", position)
                ids["process"] = processes.start_rain(ctx, [m.chunk_id(2, 0)],
                    ticks=4, intensity=10, start_delay=0)["id"]
                objects, _ = processes._physical_closure(ctx)
                return FunctionOutcome({"prepared": True, "physical_objects": len(objects),
                    "fires": sum(any(lot["burning"] for lot in entity["matter"]) for entity in objects.values())})
            self.clock.return_value = 1000.0
            self.world = WorldRuntime(self.base / ("dense.sqlite3" if dense else "sparse.sqlite3"))
            definition = replace(WORLD, functions=(*WORLD.functions, FunctionSpec("grove.density", prepare,
                {"type": "object", "additionalProperties": False})))
            self.world.install_world("u", definition)
            self.roles["A"] = self.world.create_role("A")["role_id"]
            prepared = self.call("A", "grove.density", {})
            if dense:
                self.assertEqual(prepared["physical_objects"], processes.MAX_WET_ENTITIES)
                self.assertEqual(prepared["fires"], processes.MAX_BURNING_ENTITIES)
            samples = []
            def sample():
                rain = self.world.get_state("u", m.PROCESSES, ids["process"])["value"]
                fire = self.entity(ids["fire"])["state"]
                return (self.quantity(ids["rain"], "water", "absorbed"),
                    self.quantity(ids["dry"], "water", "absorbed"), self.quantity(ids["fire"], "wood", "body"),
                    fire["destroyed"], rain["status"], self.entity(ids["rain"])["state"]["wetness"],
                    self.entity(ids["dry"])["state"]["wetness"], fire["fuel"])
            for _ in range(12):
                self.ticks(1)
                samples.append(sample())
            # Rain and drying advance each physical step, independently of
            # ordinary combustion's 3-step settlement cadence.
            self.ticks(18)
            samples.append(sample())
            return samples
        sparse, dense = simulate(False), simulate(True)
        self.assertEqual(sparse, dense)
        self.assertEqual(sparse[0], (10, 11, 20, False, "active", 31, 34, 20))
        self.assertEqual(sparse[3], (32, 5, 18, False, "complete", 100, 15, 18))
        self.assertEqual(sparse[9][2:4], (14, False))
        self.assertEqual(sparse[-1][2:4], (0, True))
        self.assertEqual(sparse[-1][7], 0)

    def route_to(self, actor, x, y):
        from collections import deque
        current = self.view(actor)
        start = (current["player"]["x"], current["player"]["y"])
        tiles = {(c["cx"]*16+dx, c["cy"]*16+dy): c["tiles"][dy*16+dx]
                 for c in current["chunks"] for dy in range(16) for dx in range(16)}
        blocked = {(e["x"], e["y"]) for e in current["entities"]
                   if "blocking" in e["capabilities"] and not e["state"]["destroyed"]}
        queue, paths = deque([start]), {start: []}
        while queue:
            point = queue.popleft()
            if point == (x, y):
                for dx, dy in paths[point]:
                    self.move(actor, dx, dy)
                return
            for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                dest = (point[0]+dx, point[1]+dy)
                if dest not in paths and dest in tiles and tiles[dest] != "rock" and dest not in blocked:
                    paths[dest] = paths[point]+[(dx, dy)]
                    queue.append(dest)
        self.fail(f"No actual cardinal path to {(x,y)}")

    def resource(self, actor, material):
        return next(e for e in self.view(actor)["entities"]
                    if e["state"].get("resource_kind") == material and e["state"].get("stock", 0) > 0)

    def harvest(self, actor, material, count=1):
        node = self.resource(actor, material)
        self.route_to(actor, node["x"], node["y"])
        for _ in range(count):
            self.act(actor, "harvest", node["id"])
        return node["id"]

    def build(self, actor, recipe, x, y, operation=None):
        return self.call(actor, "grove.build", {"recipe": recipe, "x": x, "y": y,
            "expected_player_version": self.view(actor)["player"]["version"]}, operation)["entity"]

    def test_shared_shelter_collection_and_costly_resource_recovery(self):
        self.harvest("A", "wood")
        self.route_to("A", 3, 2)
        canopy = self.build("A", "canopy", 3, 2)
        invested = [v["id"] for v in canopy["state"]["invested_materials"]]
        self.assertEqual(len(invested), 3)
        self.assertFalse(set(invested).intersection(e["id"] for e in self.view()["inventory"]))
        self.route_to("B", 4, 2)
        wood, cloth, bottle = (self.item("B", n)["id"] for n in ("木柴", "干布", "水壶"))
        self.act("B", "drop", item=wood)
        self.move("B", 1, 0)
        self.act("B", "drop", item=cloth)
        self.publish("A", {"kind": "rain", "rain": {"coverage_template": "local_region",
            "duration_ticks": 4, "intensity": 20, "start_delay_ticks": 2}})
        self.ticks(4)
        self.assertEqual(self.entity(wood)["state"]["wetness"], 0)
        self.assertGreater(self.entity(cloth)["state"]["wetness"], 0)
        self.ticks(31)
        self.route_to("B", 3, 2)
        args = {"action": "switch_mode", "target_id": canopy["id"], "mode": "collect",
                "expected_target_version": self.entity(canopy["id"])["version"],
                "expected_player_version": self.view("B")["player"]["version"]}
        self.call("B", "grove.interact", args)
        self.publish("B", {"kind": "rain", "rain": {"coverage_template": "local_region",
            "duration_ticks": 4, "intensity": 20, "start_delay_ticks": 2}})
        self.ticks(6)  # Finish the actual rain before measuring later water use.
        water = self.entity(canopy["id"])["state"]["water"]
        self.assertGreater(water, 0)
        self.assertGreater(self.entity(wood)["state"]["wetness"], 0)
        before = self.entity(bottle)["state"]["water"]
        self.act("B", "fill", canopy["id"], bottle, 5)
        self.assertEqual(self.entity(bottle)["state"]["water"], before+5)
        self.assertEqual(self.entity(canopy["id"])["state"]["water"], water-5)
        node = self.resource("B", "wood")
        self.route_to("B", node["x"], node["y"])
        for _ in range(node["state"]["stock"]):
            self.act("B", "harvest", node["id"])
        before = self.entity(bottle)["state"]["water"]
        self.act("B", "tend", node["id"], bottle)
        self.assertEqual(self.entity(bottle)["state"]["water"], before-5)
        with self.assertRaises(RuleViolation):
            self.act("B", "tend", node["id"], bottle)
        self.ticks(6)
        self.assertEqual(self.entity(node["id"])["state"]["stock"], 1)
        self.act("B", "harvest", node["id"])
        self.assertEqual(self.entity(node["id"])["state"]["stock"], 0)
        self.assertTrue(self.call("A", "grove.join", {"name": "ignored"})["resumed"])
        self.assertEqual(self.entity(canopy["id"])["state"]["water"], water-5)

    def test_water_walkway_changes_cost_and_salvage_is_unique(self):
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        opportunity = context["opportunity"]
        dry_bank = sorted((p for p in context["allowed_tiles"] if p["x"] <= 5),
                          key=lambda p: abs(p["x"])+abs(p["y"]))[:2]
        published = self.call("A", "grove.content_submit", {"opportunity_id": opportunity["id"],
            "expected_opportunity_version": opportunity["version"], "content": {"kind": "layout", "objects": [
                {"template": "wood_stick", "x": p["x"], "y": p["y"]} for p in dry_bank]}})
        materials = [e["id"] for e in published["entities"]]
        for entity in published["entities"]:
            self.route_to("A", entity["x"], entity["y"])
            self.act("A", "pickup", entity["id"])
        self.route_to("A", 5, 0)
        walkway = self.call("A", "grove.build", {"recipe": "walkway", "x": 6, "y": 0,
            "expected_player_version": self.view()["player"]["version"],
            "material_ids": materials}, "build-one-walkway")["entity"]
        invested = [v["id"] for v in walkway["state"]["invested_materials"]]
        self.assertEqual(len(invested), 2)
        self.assertEqual(sum(v["quantity"] for v in walkway["state"]["invested_materials"]), 80)
        self.assertTrue(all(self.entity(eid)["state"]["generation_allocation_retired"] for eid in invested))
        cloth = self.item("A", "干布")["id"]
        source = next(e["id"] for e in self.view()["entities"] if "water_source" in e["capabilities"])
        source_before = self.quantity(source, "water", "contained")
        held_before = sum(self.quantity(e["id"], "water", "absorbed") for e in self.view()["inventory"])
        self.move("A", 1, 0)
        self.assertEqual(self.entity(cloth)["state"]["wetness"], 0)
        self.assertEqual(self.quantity(source, "water", "contained"), source_before)
        self.move("A", 1, 0)
        self.assertEqual(self.quantity(cloth, "water", "absorbed"), 2)
        self.assertEqual(self.entity(cloth)["state"]["wetness"], 6)
        held_after = sum(self.quantity(e["id"], "water", "absorbed") for e in self.view()["inventory"])
        self.assertEqual(held_after-held_before, 6)
        self.assertEqual(source_before-self.quantity(source, "water", "contained"), held_after-held_before)
        self.move("A", -1, 0)
        self.assertEqual(self.entity(cloth)["state"]["wetness"], 6)
        self.assertEqual(source_before-self.quantity(source, "water", "contained"), 6)
        args = {"action": "dismantle", "target_id": walkway["id"],
                "expected_target_version": self.entity(walkway["id"])["version"],
                "expected_player_version": self.view()["player"]["version"]}
        first = self.call("A", "grove.interact", args, "salvage-once")
        self.assertEqual(first, self.call("A", "grove.interact", args, "salvage-once"))
        inventory = [e["id"] for e in self.view()["inventory"]]
        self.assertEqual(sum(eid in inventory for eid in invested), 2)
        self.assertEqual(sum(self.entity(node["id"])["state"]["stock"] for node in context["resource_nodes"]
            if node["resource_kind"] == "wood"), context["resource_stock"]["wood"]-2)
        self.assertFalse(any(e["id"] == walkway["id"] for e in self.view()["entities"]))
        with self.assertRaises(RuleViolation):
            self.act("A", "dismantle", walkway["id"])

    def dense_container(self, count, *, nested=False):
        """Trusted layout fixture; subsequent ignition/drop/ticks use real rules."""
        def prepare(ctx, args):
            # Keep the seeded40u rather than overwrite a derived fuel field.
            # The density fixture declares its own real2u combustible shell.
            original = m.get_entity(ctx, "seed_0_0_5_5")
            saved = m.spawn_entity(ctx, {"name": "保留原箱木料", "material": "wood", "capabilities": ["transport"],
                "matter": [], "x": 14, "y": 14, "state": {"kind": "raw_material"}})
            for lot in list(original["matter"]):
                m.transfer_material(ctx, m.get_entity(ctx, original["id"]), m.get_entity(ctx, saved["id"]),
                    lot["id"], lot["quantity"], target_slot="body", reason="fixture_material_relocation")
            m.retire_material(ctx, m.get_entity(ctx, original["id"]), saved["id"])
            crate = m.spawn_entity(ctx, {"name": "剩2u木的测试容器", "material": "wood", "capabilities": ["transport", "container"],
                "x": 5, "y": 5, "matter": [{"id": "body", "material": "wood", "quantity": 2,
                    "slot": "body", "role": "structural", "burning": False}],
                "components": {"container": {"slots": 8, "liquid_capacity": 12, "open": True},
                    "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 96, "overload_rate": 1},
                    "absorbent": {"capacity": 1, "rate": 2, "supports": {"body": {"quantity": 2, "capacity": 1}}},
                    "buoyant": {"capacity": 4, "supports": {"body": {"quantity": 2, "capacity": 4}}},
                    "combustion": {"rate": 2, "spread": True}}})
            contents = []
            for number in range(2 if nested else 3):
                inner = nested and number == 0
                item = m.spawn_entity(ctx, {"name": "留存石料", "material": "stone",
                    "capabilities": ["transport", "container"] if inner else ["transport"], "container": crate["id"],
                    "matter": [{"id": "body", "material": "stone", "quantity": 1,
                        "slot": "body", "role": "structural", "burning": False}],
                    "components": {"container": {"slots": 1, "liquid_capacity": 0, "open": True},
                        "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 48, "overload_rate": 1}} if inner else {}})
                contents.append(item["id"])
                if inner:
                    child = m.spawn_entity(ctx, {"name": "内层石料", "material": "stone",
                        "capabilities": ["transport"], "container": item["id"],
                        "matter": [{"id": "body", "material": "stone", "quantity": 1,
                            "slot": "body", "role": "structural", "burning": False}]})
                    contents.append(child["id"])
            while len(m.get_chunk(ctx, 0, 0)["entity_ids"]) < count:
                m.spawn_entity(ctx, {"name": "地面石料", "material": "stone",
                    "capabilities": ["transport"], "x": 14, "y": 14})
            return FunctionOutcome({"crate": crate["id"], "contents": contents, "saved_material": saved["id"]})
        self.world = WorldRuntime(self.base / "dense-container.sqlite3")
        definition = replace(WORLD, functions=(*WORLD.functions,
            FunctionSpec("grove.fixture", prepare,
                {"type": "object", "properties": {}, "additionalProperties": False})))
        self.world.install_world("u", definition)
        self.roles["A"] = self.world.create_role("A")["role_id"]
        self.call("A", "grove.join", {"name": "A"})
        self.route_to("A", 4, 5)
        return self.call("A", "grove.fixture", {})

    def test_container_ignition_rejects_without_room_to_release_contents(self):
        layout = self.dense_container(95)
        self.assertEqual(self.quantity(layout["crate"], "wood", "body"), 2)
        self.assertEqual(self.quantity(layout["saved_material"], "wood", "body"), 40)
        self.assertEqual(len(self.world.get_state("u", m.CHUNKS, "chunk_0_0")["value"]["entity_ids"]), 95)
        flint = self.entity(self.item("A", "打火石")["id"])
        with self.assertRaises(RuleViolation):
            self.act("A", "ignite", layout["crate"], flint["id"])
        self.assertFalse(self.entity(layout["crate"])["state"]["burning"])
        self.assertEqual(self.entity(flint["id"])["state"]["uses"], flint["state"]["uses"])
        self.ticks(2)

    def test_burning_container_reserves_release_space_against_later_drop(self):
        # Only the two direct children become public roots; the stone child's
        # healthy inner vessel keeps it contained. Reserve the actual 94 + 2.
        layout = self.dense_container(94, nested=True)
        self.assertEqual(self.quantity(layout["crate"], "wood", "body"), 2)
        self.assertEqual(len(self.world.get_state("u", m.CHUNKS, "chunk_0_0")["value"]["entity_ids"]), 94)
        self.act("A", "ignite", layout["crate"], self.item("A", "打火石")["id"])
        cloth = self.item("A", "干布")["id"]
        with self.assertRaises(RuleViolation):
            self.act("A", "drop", item=cloth)
        self.assertEqual(self.entity(cloth)["owner"], self.roles["A"])
        self.ticks(3)
        self.assertTrue(self.entity(layout["crate"])["state"]["destroyed"])
        self.assertEqual(self.quantity(layout["crate"], "wood"), 0)
        self.assertTrue(all(self.quantity(eid, "stone", "body") == 1 for eid in layout["contents"]))
        inner, child, stone = [self.entity(eid) for eid in layout["contents"]]
        self.assertIsNone(inner["container"])
        self.assertEqual(child["container"], inner["id"])
        self.assertIsNone(stone["container"])
        versions = [self.entity(eid)["version"] for eid in layout["contents"]]
        self.ticks(3)
        self.assertEqual(versions, [self.entity(eid)["version"] for eid in layout["contents"]])
        self.act("A", "drop", item=cloth)
        roots = self.world.get_state("u", m.CHUNKS, "chunk_0_0")["value"]["entity_ids"]
        self.assertLessEqual(len(roots), m.MAX_CHUNK_ENTITIES)
        self.assertEqual(len(roots), len(set(roots)))

    def test_stone_hearth_spends_fuel_without_spreading_and_dries_faster(self):
        self.harvest("A", "stone", 2)
        self.route_to("A", 4, 3)
        hearth = self.build("A", "hearth", 4, 3)
        invested = hearth["state"]["invested_materials"]
        stone_ids = [entry["id"] for entry in invested]
        self.assertEqual(len(stone_ids), 2)
        self.assertEqual([entry["quantity"] for entry in invested], [40, 40])
        # Two harvests share provenance and merge into one80u structural lot.
        self.assertEqual(len({lot_id for entry in invested for lot_id in entry["lot_ids"]}), 1)
        fuel = self.item("A", "木柴")["id"]
        self.act("A", "fuel", hearth["id"], fuel)
        self.act("A", "ignite", hearth["id"], self.item("A", "打火石")["id"])
        self.route_to("B", 4, 2)
        wet, bottle = (self.item("B", n)["id"] for n in ("木柴", "水壶"))
        self.act("B", "drop", item=wet)
        self.act("B", "pour", wet, bottle, 5)
        self.route_to("C", 2, 2)
        air, air_bottle = (self.item("C", n)["id"] for n in ("木柴", "水壶"))
        self.act("C", "drop", item=air)
        self.act("C", "pour", air, air_bottle, 5)
        self.assertEqual((self.quantity(wet, "wood", "body"), self.quantity(air, "wood", "body")), (40, 40))
        self.assertEqual((self.quantity(wet, "water"), self.quantity(air, "water")), (5, 5))
        self.assertEqual((self.quantity(bottle, "water", "contained"), self.quantity(air_bottle, "water", "contained")), (15, 15))
        self.ticks(1)
        # Equal wood bodies and equal actual water: heat evaporates3u while
        # air evaporates2u. Both first absorb2u, so percentages alone hide it.
        self.assertEqual((self.quantity(wet, "water"), self.quantity(air, "water")), (2, 3))
        self.assertEqual((self.quantity(wet, "water", "surface"), self.quantity(air, "water", "surface")), (0, 1))
        self.assertEqual((self.quantity(wet, "water", "absorbed"), self.quantity(air, "water", "absorbed")), (2, 2))
        self.assertFalse(self.entity(wet)["state"]["burning"])
        self.assertFalse(self.entity(air)["state"]["burning"])
        self.assertEqual(self.quantity(hearth["id"], "stone", "body"), 80)
        self.assertEqual(self.quantity(hearth["id"], "wood", "contained"), 40)
        self.assertEqual(self.entity(hearth["id"])["state"]["hearth_fuel"], 40)
        with self.assertRaises(RuleViolation):
            self.act("A", "dismantle", hearth["id"])
        self.act("A", "pour", hearth["id"], self.item("A", "水壶")["id"], 5)
        self.assertFalse(self.entity(hearth["id"])["state"]["lit"])
        # Quenching does not erase the remaining40u. Unload it through the
        # same material-transfer action before dismantling the stone shell.
        with self.assertRaises(RuleViolation):
            self.act("A", "dismantle", hearth["id"])
        pot = self.item("A", "水壶")
        remaining = next(lot for lot in self.entity(hearth["id"])["matter"]
                         if lot["material"] == "wood" and lot["slot"] == "contained")
        self.call("A", "grove.interact", {"action": "transfer", "target_id": pot["id"],
            "expected_target_version": pot["version"], "item_id": hearth["id"],
            "expected_item_version": self.entity(hearth["id"])["version"], "lot_id": remaining["id"], "amount": 40,
            "expected_player_version": self.view()["player"]["version"]})
        self.assertEqual(self.quantity(hearth["id"], "wood"), 0)
        self.assertEqual(self.quantity(pot["id"], "wood", "contained"), 40)
        self.act("A", "dismantle", hearth["id"])
        self.assertEqual([self.quantity(eid, "stone", "body") for eid in stone_ids], [40, 40])
        self.assertTrue(all(self.entity(eid)["owner"] == self.roles["A"] for eid in stone_ids))
        self.assertTrue(set(stone_ids).issubset({e["id"] for e in self.view()["inventory"]}))
        self.assertEqual(self.quantity(hearth["id"], "stone", "body"), 0)
        self.assertEqual(self.quantity(pot["id"], "wood", "contained"), 40)
        self.assertNotIn(fuel, [e["id"] for e in self.view()["inventory"]])


if __name__ == "__main__":
    unittest.main()
