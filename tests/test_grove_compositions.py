"""Coordinator-supplied data combinations through ordinary world actions.

Controlled timer steps are deterministic physics evidence, not play rounds.
No state injection, private memory sharing or administrator material grants.
"""
import json
import sqlite3
import unittest
from dataclasses import replace

import test_shared_grove as fixtures
from agent_world.worlds.shared_grove import model as m
from agent_world.runtime_errors import RuleViolation, StateConflict
from agent_world import FunctionOutcome, FunctionSpec, WorldRuntime
from agent_world.worlds.shared_grove import WORLD


class ComposableWorldTests(unittest.TestCase):
    setUp = fixtures.SharedGroveTests.setUp
    call = fixtures.SharedGroveTests.call
    view = fixtures.SharedGroveTests.view
    entity = fixtures.SharedGroveTests.entity
    item = fixtures.SharedGroveTests.item
    move = fixtures.SharedGroveTests.move
    walk = fixtures.SharedGroveTests.walk
    act = fixtures.SharedGroveTests.act
    ticks = fixtures.SharedGroveTests.ticks
    publish = fixtures.SharedGroveTests.publish

    def inspect(self, actor, identifier):
        return self.world.query_function("u", "grove.inspect", self.roles[actor],
                                         {"entity_id": identifier})["result"]

    def material_balance(self):
        # Read the complete isolated world, including finite sources and private
        # inventories. This observes conservation without injecting world state.
        with sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT value_json FROM world_state WHERE universe=? AND scope=?",
                ("u", m.ENTITIES)).fetchall()
        totals = {}
        for row in rows:
            for lot in json.loads(row[0])["matter"]:
                totals[lot["material"]] = totals.get(lot["material"], 0) + lot["quantity"]
        account = self.world.get_state("u", m.META, "matter_accounting")["value"]
        return totals, account

    def assert_material_balance(self, before):
        totals, account = self.material_balance()
        old_totals, old_account = before
        for material in set(totals) | set(old_totals):
            def difference(category):
                keys = set(account[category]) | set(old_account[category])
                return sum(account[category].get(key, 0)-old_account[category].get(key, 0)
                           for key in keys if key.split(":", 1)[0] == material)
            self.assertEqual(totals.get(material, 0), old_totals.get(material, 0)
                             + difference("sources") - difference("sinks"), material)
        return totals, account

    def inspect_trace(self, stage, identifier):
        value = self.inspect("A", identifier)
        print(json.dumps({"stage": stage, "id": identifier,
                          "matter": value["entity"]["matter"],
                          "simulation": value["entity"]["simulation"],
                          "relations": value["entity"]["relations"],
                          "lifecycle": value["entity"]["lifecycle"],
                          "location": value["entity"]["location"],
                          "actions": [{key: item.get(key) for key in ("action", "available", "reason")}
                                      for item in value["action_options"]]},
                         ensure_ascii=False), flush=True)
        return value

    def composition_objects(self, specs):
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        player = self.view("A")["player"]
        tiles = sorted((tile for tile in context["allowed_tiles"]
                        if tile["x"] <= 5 and abs(tile["x"]-player["x"])+abs(tile["y"]-player["y"]) >= 3),
                       key=lambda tile: (abs(tile["x"]-player["x"])+abs(tile["y"]-player["y"]), tile["y"], tile["x"]))
        opportunity = context["opportunity"]
        result = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [
                {"composition": spec, **tile} for spec, tile in zip(specs, tiles)]}})
        return [entity["id"] for entity in result["entities"]]

    def combination_preview(self, actor, target, item, action):
        option = next(value for value in self.inspect(actor, target)["action_options"]
                      if value["action"] == action)
        return next(value["preview"] for value in option["input"]["candidates"] if value["id"] == item)

    def test_full_backpack_reparents_existing_child_without_counting_it_twice(self):
        # Reach the real tree bound using finite public sources, rather than
        # granting objects or modifying the persisted relationship graph.
        sources = [entity for entity in self.view("A")["entities"]
                   if "resource_node" in entity.get("capabilities", [])]
        self.assertGreaterEqual(len(sources), 3)
        for source in sources[:3]:
            self.walk("A", source["x"], source["y"])
            for _ in range(4):
                self.act("A", "harvest", source["id"])
        backpack = self.view("A")["player"]["backpack_id"]
        def inventory_ids():
            found, arguments = set(), {"radius": 1, "inventory_limit": 16}
            while True:
                page = self.world.query_function("u", "map.viewport", self.roles["A"], arguments)["result"]
                found.update(entity["id"] for entity in page["inventory"])
                if not page["inventory_next"]:
                    self.assertEqual(len(found), page["inventory_total"])
                    return found
                arguments["inventory_after"] = page["inventory_next"]
        original_ids = inventory_ids()
        self.assertEqual(len(original_ids)-1, 16)
        bottle, cloth = self.item("A", "水壶")["id"], self.item("A", "干布")["id"]
        self.act("A", "store", bottle, cloth)
        self.assertEqual(len(self.entity(backpack)["state"]["contents"]), 15)
        preview = self.combination_preview("A", backpack, cloth, "store")
        self.assertTrue(preview["available"], preview)
        balance = self.material_balance()
        self.call("A", "grove.interact", {
            "action": "store", "target_id": backpack, "item_id": cloth,
            "expected_player_version": self.view("A")["player"]["version"],
            "expected_target_version": self.entity(backpack)["version"],
            "expected_item_version": self.entity(cloth)["version"],
            "expected_combination_fingerprint": preview["version_fingerprint"]})
        self.assertEqual(len(self.entity(backpack)["state"]["contents"]), 16)
        self.assertEqual(self.entity(cloth)["physical_parent"],
                         {"id": backpack, "relation": "contained"})
        self.assertNotIn(cloth, self.entity(bottle)["state"]["contents"])
        self.assertEqual(inventory_ids(), original_ids)
        self.assert_material_balance(balance)

    def test_store_preview_separates_proximity_overload_and_private_contents(self):
        pouch, vessel = self.composition_objects([
            {"material": "cloth", "construction": "side_pouch", "size": 1},
            {"material": "stone", "construction": "vessel", "size": 1}])
        bottle = self.item("A", "水壶")["id"]
        ids = (pouch, bottle, self.view("A")["player"]["backpack_id"])
        before = {identifier: self.entity(identifier) for identifier in ids}
        preview = self.combination_preview("A", pouch, bottle, "store")
        self.assertFalse(preview["available"])
        self.assertTrue(preview["physics"]["known"])
        # Stone's registered load density is 3: 40u stone + 20u water
        # weighs 160 logical units, rather than its unweighted 60u matter.
        self.assertEqual(preview["physics"]["after"]["total"], 176)
        self.assertEqual(preview["physics"]["after"]["supported"], 160)
        self.assertEqual(preview["physics"]["after"]["limit"], 32)
        self.assertEqual(preview["physics"]["support_margin"], -128)
        self.assertTrue(preview["physics"]["overloaded"])
        self.assertEqual(before, {identifier: self.entity(identifier) for identifier in ids})
        self.walk("A", self.entity(pouch)["x"], self.entity(pouch)["y"])
        nearby = self.combination_preview("A", pouch, bottle, "store")
        self.assertTrue(nearby["available"], nearby)
        self.assertNotEqual(nearby["version_fingerprint"], preview["version_fingerprint"])
        self.act("A", "store", pouch, bottle)
        actual = self.inspect("A", pouch)["entity"]["simulation"]
        for field, expected in nearby["physics"]["after"].items():
            self.assertEqual(actual[field], expected, field)

        # The aggregate remains readable, but the privately supplied original
        # wood and its lot identities must stay hidden in a closed public vessel.
        wood = self.item("A", "木柴")["id"]
        wood_lots = [lot["id"] for lot in self.entity(wood)["matter"]]
        self.walk("A", self.entity(vessel)["x"], self.entity(vessel)["y"])
        self.act("A", "pickup", vessel)
        self.act("A", "store", vessel, wood)
        self.act("A", "close", vessel)
        self.act("A", "drop", vessel)
        other = self.item("B", "水壶")["id"]
        hidden = self.combination_preview("B", vessel, other, "store")
        self.assertFalse(hidden["available"])
        serialized = json.dumps(hidden)
        self.assertNotIn(wood, serialized)
        for identifier in wood_lots:
            self.assertNotIn(identifier, serialized)

    def test_attach_preview_matches_real_action_and_cancelled_extra_load(self):
        bag, floating = self.composition_objects([
            {"material": "cloth", "construction": "sling", "size": 1},
            {"material": "wood", "construction": "float", "size": 1}])
        for identifier in (bag, floating):
            self.walk("A", self.entity(identifier)["x"], self.entity(identifier)["y"])
            self.act("A", "pickup", identifier)
        self.act("A", "store", bag, self.item("A", "木柴")["id"])
        before = {identifier: self.entity(identifier) for identifier in (bag, floating)}
        preview = self.combination_preview("A", bag, floating, "attach")
        self.assertTrue(preview["available"])
        self.assertFalse(preview["physics"]["before"]["can_float"])
        self.assertTrue(preview["physics"]["can_float"])
        self.assertEqual(preview["physics"]["after"]["total"], 96)
        self.assertEqual(preview["physics"]["support_margin"], 16)
        self.assertEqual(before, {identifier: self.entity(identifier) for identifier in before})
        arguments = {"action": "attach", "target_id": bag, "item_id": floating,
                     "expected_player_version": self.view("A")["player"]["version"],
                     "expected_target_version": self.entity(bag)["version"],
                     "expected_item_version": self.entity(floating)["version"]}
        with self.assertRaises(StateConflict):
            self.call("A", "grove.interact", {**arguments, "expected_combination_fingerprint": "0"*64})
        self.assertEqual(before, {identifier: self.entity(identifier) for identifier in before})
        self.call("A", "grove.interact", {**arguments, "expected_combination_fingerprint": preview["version_fingerprint"]})
        actual = self.inspect("A", bag)["entity"]["simulation"]
        for field, expected in preview["physics"]["after"].items():
            self.assertEqual(actual[field], expected, field)
        before = {identifier: self.entity(identifier) for identifier in before}
        bottle = self.item("A", "水壶")["id"]
        extra = self.combination_preview("A", bag, bottle, "store")
        self.assertTrue(extra["available"])
        self.assertTrue(extra["physics"]["overloaded"])
        self.assertFalse(extra["physics"]["can_float"])
        self.assertEqual(extra["physics"]["support_margin"], -144)
        self.assertEqual(before, {identifier: self.entity(identifier) for identifier in before})

    def test_relation_fingerprint_rejects_descendant_only_change_without_mutation(self):
        box, pouch = self.composition_objects([
            {"material": "stone", "construction": "vessel", "size": 1},
            {"material": "cloth", "construction": "side_pouch", "size": 1}])
        self.walk("A", self.entity(box)["x"], self.entity(box)["y"])
        self.act("A", "pickup", box)
        wood = self.item("A", "木柴")["id"]
        self.act("A", "store", box, wood)
        self.act("A", "pour", wood, self.item("A", "水壶")["id"], 5)
        self.walk("A", self.entity(pouch)["x"], self.entity(pouch)["y"])
        old = self.combination_preview("A", pouch, box, "store")
        versions = (self.entity(box)["version"], self.view("A")["player"]["version"])
        self.ticks(1)
        current = self.combination_preview("A", pouch, box, "store")
        self.assertEqual(versions, (self.entity(box)["version"], self.view("A")["player"]["version"]))
        self.assertNotEqual(old["physics"]["after"]["supported"], current["physics"]["after"]["supported"])
        self.assertNotEqual(old["version_fingerprint"], current["version_fingerprint"])
        ids = (box, pouch, wood, self.view("A")["player"]["backpack_id"])
        before = {identifier: self.entity(identifier) for identifier in ids}
        arguments = {"action": "store", "target_id": pouch, "item_id": box,
                     "expected_player_version": self.view("A")["player"]["version"],
                     "expected_target_version": self.entity(pouch)["version"],
                     "expected_item_version": self.entity(box)["version"]}
        with self.assertRaises(StateConflict):
            self.call("A", "grove.interact", {**arguments, "expected_combination_fingerprint": old["version_fingerprint"]})
        self.assertEqual(before, {identifier: self.entity(identifier) for identifier in ids})
        unbounded = next(row for row in current["physics"]["affected"]
                         if row["id"] == self.view("A")["player"]["backpack_id"])
        self.assertFalse(unbounded["support_limit_applicable"])
        self.assertIsNone(unbounded["support_margin"])
        self.call("A", "grove.interact", {**arguments, "expected_combination_fingerprint": current["version_fingerprint"]})
        self.assertEqual(current["physics"]["after"]["total"], self.inspect("A", pouch)["entity"]["simulation"]["total"])

    def test_data_sling_uses_attached_float_and_real_cargo(self):
        balance = self.material_balance()
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        compositions = context["availability"]["layout"]["affordable_compositions"]
        sling = {"material": "cloth", "construction": "sling", "size": 1}
        self.assertIn(sling, compositions)
        tiles = sorted((tile for tile in context["allowed_tiles"] if tile["x"] <= 5),
                       key=lambda point: (point["x"]+point["y"], point["y"], point["x"]))[:3]
        self.assertEqual(len(tiles), 3)
        specs = [sling, {"material": "wood", "construction": "float", "size": 1},
                 {"material": "wood", "construction": "bundle", "size": 1}]
        opportunity = context["opportunity"]
        result = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [
                {"composition": spec, **tile} for spec, tile in zip(specs, tiles)]}})
        ids = [value["id"] for value in result["entities"]]
        root, floating, cargo = ids
        self.assertNotIn("buoyant", self.entity(root)["components"])
        stock = result["resource_stock"]
        self.assertEqual(context["resource_stock"]["wood"]-stock["wood"], 2)
        self.assertEqual(context["resource_stock"]["cloth"]-stock["cloth"], 1)
        for identifier in ids:
            entity = self.entity(identifier)
            self.walk("A", entity["x"], entity["y"])
            self.act("A", "pickup", identifier)
        self.act("A", "store", root, cargo)
        self.act("A", "attach", root, floating)
        dry = self.inspect_trace("dry-assembled", root)["entity"]["simulation"]
        self.assertEqual(dry["total"], 96)
        self.assertEqual(dry["supported"], 80)
        self.assertEqual(dry["buoyancy"], 120)
        self.assertTrue(dry["can_float"])
        creek_before = self.entity("water_0_0")["state"]["water_remaining"]
        self.walk("A", 6, 2)
        self.act("A", "drop", root)
        water = self.inspect_trace("water-before-launch", root)
        expected_load = sum(lot["quantity"] * (2 if lot["material"] == "water" else 1)
                            for identifier in ids for lot in self.entity(identifier)["matter"])
        self.assertEqual(water["entity"]["simulation"]["total"], expected_load)
        self.assertTrue(water["entity"]["simulation"]["can_float"])
        self.assertIn("launch", water["available_actions"])
        self.act("A", "launch", root)
        # Real finite creek contact changes the material loads. No administrator
        # wetness assignment or elapsed-time shortcut supplies these quantities.
        before = {identifier: self.entity(identifier) for identifier in ids}
        self.ticks(2)
        wet = self.inspect_trace("after-real-water-contact", root)["entity"]["simulation"]
        self.assertGreater(wet["total"], water["entity"]["simulation"]["total"])
        self.assertGreater(wet["overload"], 0)
        self.assertFalse(wet["can_float"])
        creek_after = self.entity("water_0_0")["state"]["water_remaining"]
        water_held = sum(lot["quantity"] for identifier in ids
                         for lot in self.entity(identifier)["matter"] if lot["material"] == "water")
        self.assertLess(creek_after, creek_before)
        self.assertGreater(water_held, 0)
        remaining = []
        for _ in range(105):
            self.ticks(1)
            remaining.append(self.entity(root)["lifecycle"])
            if remaining[-1] == "broken":
                break
        self.assertIn("broken", remaining)
        released = {identifier: self.entity(identifier) for identifier in ids}
        self.assertEqual(released[root]["lifecycle"], "broken")
        self.assertIsNone(released[floating]["physical_parent"])
        self.assertIsNone(released[cargo]["physical_parent"])
        for material in ("wood", "cloth"):
            quantity = lambda values: sum(lot["quantity"] for entity in values.values()
                                         for lot in entity["matter"] if lot["material"] == material)
            self.assertEqual(quantity(released), quantity(before))
        self.assertEqual(set(released), set(ids))
        broken = self.inspect_trace("broken-original-objects", root)
        self.assertEqual(broken["entity"]["simulation"]["integrity"], 0)
        # The other person can continue with a released original object; a
        # detached copy or an unreachable visual outcome would fail this path.
        self.walk("B", released[cargo]["x"], released[cargo]["y"])
        self.act("B", "pickup", cargo)
        self.assertEqual(self.entity(cargo)["owner"], self.roles["B"])
        totals, account = self.assert_material_balance(balance)
        print(json.dumps({"stage": "actual-water-account", "finite_creek_before_u": creek_before,
                          "finite_creek_after_contact_u": creek_after,
                          "assembly_water_after_contact_u": water_held,
                          "world_water_before_u": balance[0].get("water", 0),
                          "world_water_after_u": totals.get("water", 0),
                          "declared_water_sources_delta": {key: amount-balance[1]["sources"].get(key, 0)
                              for key, amount in account["sources"].items() if key.startswith("water:")},
                          "declared_water_sinks_delta": {key: amount-balance[1]["sinks"].get(key, 0)
                              for key, amount in account["sinks"].items() if key.startswith("water:")}},
                         ensure_ascii=False), flush=True)

    def test_formal_ignition_burns_lining_without_inventing_body_damage(self):
        balance = self.material_balance()
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        tile = next(point for point in context["allowed_tiles"] if point["x"] <= 5)
        opportunity = context["opportunity"]
        result = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [{"composition": {
                "material": "stone", "construction": "vessel", "size": 1,
                "lining": "cloth"}, **tile}]}})
        identifier = result["entities"][0]["id"]
        before = self.entity(identifier)
        self.walk("A", before["x"], before["y"])
        self.act("A", "ignite", identifier, self.item("A", "打火石")["id"])
        # The 16u lining burns at the unchanged 2u rate every 3 physical steps.
        self.ticks(24)
        after = self.inspect("A", identifier)["entity"]
        amount = lambda value, material: sum(lot["quantity"] for lot in value["matter"]
                                             if lot["material"] == material)
        self.assertEqual(amount(after, "stone"), amount(before, "stone"))
        self.assertEqual(amount(after, "cloth"), 0)
        self.assertEqual(after["lifecycle"], "intact")
        self.assertEqual(after["simulation"]["integrity"], 100)
        self.assertEqual(after["id"], identifier)
        self.assertNotIn("ignite", self.inspect("A", identifier)["available_actions"])
        self.assert_material_balance(balance)

    def test_formal_inner_failure_does_not_open_closed_outer_container(self):
        balance = self.material_balance()
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        tiles = sorted((point for point in context["allowed_tiles"] if point["x"] <= 5),
                       key=lambda point: (point["x"]+point["y"], point["y"], point["x"]))[:3]
        specs = [{"material": "stone", "construction": "vessel", "size": 2},
                 {"material": "wood", "construction": "vessel", "size": 1},
                 {"material": "wood", "construction": "bundle", "size": 1}]
        opportunity = context["opportunity"]
        result = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [{"composition": spec, **tile}
                                                        for spec, tile in zip(specs, tiles)]}})
        outer, inner, child = [value["id"] for value in result["entities"]]
        for identifier in (outer, inner, child):
            entity = self.entity(identifier)
            self.walk("A", entity["x"], entity["y"])
            self.act("A", "pickup", identifier)
        self.act("A", "store", inner, child)
        # Store actual liquid inside the inner vessel. Pour wets the surface
        # and then evaporates; it is not a sustained contained-water load.
        bottle = self.inspect("A", self.item("A", "水壶")["id"])["entity"]
        water = next(lot for lot in bottle["matter"]
                     if lot["material"] == "water" and lot["slot"] == "contained")
        self.call("A", "grove.interact", {
            "action": "transfer", "target_id": inner,
            "expected_target_version": self.entity(inner)["version"],
            "item_id": bottle["id"], "expected_item_version": bottle["version"],
            "lot_id": water["id"], "amount": 6,
            "expected_player_version": self.view("A")["player"]["version"]})
        loaded = self.inspect_trace("inner-loaded-with-actual-water", inner)
        self.assertGreater(loaded["entity"]["simulation"]["overload"], 0)
        self.act("A", "store", outer, inner)
        self.act("A", "close", outer)
        self.act("A", "drop", outer)
        for _ in range(60):
            self.ticks(1)
            if self.entity(inner)["lifecycle"] == "broken":
                break
        print(json.dumps({"stage": "nested-after-physical-steps",
                          "objects": [{"id": identifier, "lifecycle": self.entity(identifier)["lifecycle"],
                                       "structure": self.entity(identifier)["components"].get("structure"),
                                       "parent": self.entity(identifier)["physical_parent"]}
                                      for identifier in (outer, inner, child)],
                          "physical_active": self.world.get_state("u", m.META, "physical_active")["value"]},
                         ensure_ascii=False), flush=True)
        self.assertEqual(self.entity(inner)["lifecycle"], "broken")
        self.assertEqual(self.entity(outer)["lifecycle"], "intact")
        self.assertEqual(self.inspect("A", outer)["contents"], [])
        with self.assertRaisesRegex(RuleViolation, "关闭的容器"):
            self.inspect("B", child)
        self.act("A", "open", outer)
        self.act("A", "retrieve", outer, child)
        self.assertEqual(self.entity(child)["owner"], self.roles["A"])
        self.assertIn(child, [value["id"] for value in self.view("A")["inventory"]])
        self.assert_material_balance(balance)

    def test_data_side_pouch_failure_keeps_cargo_outside_closed_vessel(self):
        balance = self.material_balance()
        context = self.call("A", "grove.generation_context", {"kind": "layout"})
        specs = [{"material": "cloth", "construction": "side_pouch", "size": 1},
                 {"material": "stone", "construction": "vessel", "size": 2},
                 {"material": "wood", "construction": "bundle", "size": 1}]
        self.assertIn(specs[0], context["availability"]["layout"]["affordable_compositions"])
        tiles = sorted((point for point in context["allowed_tiles"] if point["x"] <= 5),
                       key=lambda point: (point["x"]+point["y"], point["y"], point["x"]))[:3]
        opportunity = context["opportunity"]
        result = self.call("A", "grove.content_submit", {
            "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
            "content": {"kind": "layout", "objects": [{"composition": spec, **tile}
                                                        for spec, tile in zip(specs, tiles)]}})
        pouch, vessel, cargo = [value["id"] for value in result["entities"]]
        for material, cost in (("cloth", 1), ("stone", 2), ("wood", 1)):
            self.assertEqual(context["resource_stock"][material]-result["resource_stock"][material], cost)
        for identifier in (pouch, vessel, cargo):
            entity = self.entity(identifier)
            self.walk("A", entity["x"], entity["y"])
            self.act("A", "pickup", identifier)
        self.act("A", "store", pouch, cargo)
        self.act("A", "attach", vessel, pouch)
        self.act("A", "close", vessel)
        self.walk("A", 2, 2)
        self.act("A", "drop", vessel)
        initial_pouch = self.inspect_trace("side-pouch-outside-closed-vessel", pouch)
        initial_vessel = self.inspect("A", vessel)
        self.assertEqual(initial_pouch["entity"]["physical_parent"], {"id": vessel, "relation": "attached"})
        self.assertEqual(initial_pouch["entity"]["simulation"]["supported"], 40)
        self.assertEqual(initial_pouch["entity"]["simulation"]["limit"], 32)
        self.assertEqual(initial_vessel["entity"]["simulation"]["supported"], 56)
        self.assertEqual(initial_vessel["entity"]["simulation"]["limit"], 96)
        before_cargo = self.entity(cargo)["matter"]
        for _ in range(60):
            self.ticks(1)
            if self.entity(pouch)["lifecycle"] == "broken":
                break
        facts = {identifier: self.entity(identifier) for identifier in (pouch, vessel, cargo)}
        print(json.dumps({"stage": "side-pouch-after-real-overload", "objects": [
            {"id": identifier, "lifecycle": value["lifecycle"], "parent": value["physical_parent"],
             "matter": value["matter"], "container": value["components"].get("container")}
            for identifier, value in facts.items()]}, ensure_ascii=False), flush=True)
        self.assertEqual(facts[pouch]["lifecycle"], "broken")
        self.assertEqual(facts[vessel]["lifecycle"], "intact")
        self.assertFalse(facts[vessel]["components"]["container"]["open"])
        self.assertNotEqual(facts[cargo]["physical_parent"], {"id": vessel, "relation": "contained"})
        self.assertEqual(facts[cargo]["matter"], before_cargo)
        self.assert_material_balance(balance)
        self.walk("B", 2, 2)
        public = self.inspect("B", cargo)
        self.assertIn("pickup", public["available_actions"])
        self.act("B", "pickup", cargo)
        self.assertEqual(self.entity(cargo)["owner"], self.roles["B"])
        self.assertFalse(self.entity(vessel)["components"]["container"]["open"])
        self.assert_material_balance(balance)

    def test_attached_failure_cannot_stall_a_full_ground_chunk(self):
        # Explicit trusted background fixture, separate from the player/model
        # evidence. All assembly, placement and timer transitions use real rules.
        for label in ("外侧工具袋", "改名的挂包"):
            with self.subTest(name=label):
                def background(ctx, args):
                    while len(m.get_chunk(ctx, 0, 0)["entity_ids"]) < 95:
                        count = len(m.get_chunk(ctx, 0, 0)["entity_ids"])
                        m.spawn_entity(ctx, {"name": "静态背景石料", "material": "stone",
                            "capabilities": ["transport"], "x": 12 + count % 4,
                            "y": 10 + (count // 4) % 6})
                    return FunctionOutcome({"roots": 95})
                definition = replace(WORLD, functions=(*WORLD.functions,
                    FunctionSpec("grove.fixture_background", background,
                        {"type": "object", "properties": {}, "additionalProperties": False})))
                self.world = WorldRuntime(self.base / ("dense-" + label + ".sqlite3"))
                self.world.install_world("u", definition)
                self.roles = {actor: self.world.create_role(actor)["role_id"] for actor in "AB"}
                for actor in self.roles:
                    self.call(actor, "grove.join", {"name": actor})
                context = self.call("A", "grove.generation_context", {"kind": "layout"})
                tiles = sorted((tile for tile in context["allowed_tiles"] if tile["x"] <= 5),
                               key=lambda tile: (tile["x"] + tile["y"], tile["y"], tile["x"]))[:3]
                specs = [{"material": "cloth", "construction": "side_pouch", "size": 1},
                         {"material": "stone", "construction": "vessel", "size": 2},
                         {"material": "wood", "construction": "bundle", "size": 1}]
                opportunity = context["opportunity"]
                result = self.call("A", "grove.content_submit", {
                    "opportunity_id": opportunity["id"], "expected_opportunity_version": opportunity["version"],
                    "content": {"kind": "layout", "objects": [
                        {"composition": spec, "name": label if index == 0 else "原物件", **tile}
                        for index, (spec, tile) in enumerate(zip(specs, tiles))]}})
                pouch, vessel, cargo = [entity["id"] for entity in result["entities"]]
                for identifier in (pouch, vessel, cargo):
                    entity = self.entity(identifier)
                    self.walk("A", entity["x"], entity["y"])
                    self.act("A", "pickup", identifier)
                self.act("A", "store", pouch, cargo)
                self.act("A", "attach", vessel, pouch)
                self.act("A", "close", vessel)
                self.walk("A", 2, 2)
                self.call("A", "grove.fixture_background", {})
                balance = self.material_balance()
                before = {identifier: self.entity(identifier) for identifier in (pouch, vessel, cargo)}
                try:
                    self.act("A", "drop", vessel)
                except RuleViolation:
                    self.assertEqual(before, {identifier: self.entity(identifier)
                                              for identifier in (pouch, vessel, cargo)})
                    self.ticks(2)
                else:
                    for _ in range(60):
                        self.clock.return_value += 1
                        step = self.world.run_due_timers("u")
                        if (step["count"] != 1 or any(item.get("status") != "completed"
                                for item in step["processed"]) or self.entity(pouch)["lifecycle"] == "broken"):
                            break
                    print(json.dumps({"stage": "dense-attached-release", "name": label,
                        "timer": step, "pouch": self.entity(pouch)["lifecycle"],
                        "roots": len(self.world.get_state("u", m.CHUNKS, "chunk_0_0")["value"]["entity_ids"])}), flush=True)
                    self.assertEqual(step["count"], 1, "Accepted assembly stalled its timer")
                    self.assertTrue(all(item.get("status") == "completed" for item in step["processed"]), step)
                    self.assertEqual(self.entity(pouch)["lifecycle"], "broken")
                    self.ticks(2)
                self.assertLessEqual(len(self.world.get_state("u", m.CHUNKS, "chunk_0_0")["value"]["entity_ids"]), 96)
                self.assert_material_balance(balance)


if __name__ == "__main__":
    unittest.main()
