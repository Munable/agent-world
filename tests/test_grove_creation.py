"""Creation and linked continuation through formal world calls, no model claims."""
from copy import deepcopy
import unittest

from agent_world import WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.worlds.shared_grove import make_world, model as m
import test_grove_creative as fixtures


def formation_definition(key="formed"):
    return {"key": key, "name": "真实材料装配", "version": 1, "types": {
        "body": {"name": "双材器皿", "body_material": "wood", "cost_u": {"wood": 3, "cloth": 2},
            "components": {"container": {"slots": 3, "liquid_capacity": 0, "open": True},
                "absorbent": {"capacity": 6, "rate": 1}, "buoyant": {"capacity": 20},
                "combustion": {"rate": 3, "spread": False}}}}}


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def publish(self, raw):
        return self.h.call("A", "grove.definition_publish", {"definition": raw})["definition_ref"]

    def plan(self, ref, type_id, actor="A"):
        menu = self.h.read(actor, "grove.fabrication_options", {"definition_ref": ref, "type_id": type_id})
        args = deepcopy(menu["action"]["arguments"])
        for identity, quantity in menu["cost_u"].items():
            left = quantity
            for entity in menu["action"]["choices"]["sources"]:
                for lot in entity["lots"]:
                    if lot["material"] == identity and left:
                        take = min(left, lot["quantity"])
                        args["sources"].append({"entity_id": entity["id"], "expected_entity_version": entity["version"],
                                              "lot_id": lot["id"], "quantity_u": take})
                        left -= take
            self.assertEqual(left, 0, menu)
        return menu, args

    def form(self, ref, type_id):
        _, args = self.plan(ref, type_id)
        return self.h.call("A", "grove.fabricate", args)["entity"]["id"]

    def place(self, eid, actor="A"):
        self.h.interact(actor, eid, "drop")

    def test_admission_reserves_control_growth_without_double_charging_saved_evidence(self):
        h = self.h
        raw = {"key": "bounded_drive", "name": "有界动力", "version": 1, "types": {
            "source": {"name": "动力", "body_material": "wood", "cost_u": {"wood": 1},
                "mechanical": {"role": "source"}}}}
        entity = h.entity(self.form(self.publish(raw), "source"))
        lower, upper = 0, sm.MAX_ENTITY_BYTES
        while lower < upper:
            middle = (lower+upper+1)//2
            candidate = deepcopy(entity)
            candidate["state"]["padding"] = "x"*middle
            try:
                m.normalize_entity(candidate)
                lower = middle
            except RuleViolation:
                upper = middle-1
        candidate = deepcopy(entity)
        candidate["state"]["padding"] = "x"*lower
        accepted = m.normalize_entity(candidate)
        program = accepted["state"]["program"]
        before = m.future_control_bytes(program)
        self.assertGreater(before, 0)
        program["drive_request"] = {"enabled": False, "requested_step": 10**20,
            "applies_from_step": 10**20+1, "source": {"kind": "explicit_action",
                "action_id": "a"*80, "actor_role_id": "r"*128}}
        program["mechanical_last_solved"] = {"step": 10**20}
        self.assertEqual(m.future_control_bytes(program), 0)
        self.assertLessEqual(sm.encoded_size(m.normalize_entity(accepted)), sm.MAX_ENTITY_BYTES)
        candidate["state"]["padding"] += "x"
        with self.assertRaisesRegex(RuleViolation, "8KiB"):
            m.normalize_entity(candidate)
        self.assertEqual(h.entity(entity["id"]), entity)

    def test_actual_multisource_formation_remainders_supports_receipt_and_restart(self):
        h = self.h
        ref = self.publish(formation_definition())
        menu, args = self.plan(ref, "body")
        self.assertEqual(menu["cost_u"], {"wood": 3, "cloth": 2})
        for material in menu["materials"]:
            actual = sum(lot["quantity"] for source in menu["action"]["choices"]["sources"]
                         for lot in source["lots"] if lot["material"] == material["material_id"])
            self.assertEqual(material["available_u"], actual)
            self.assertEqual(material["missing_u"], 0)
        foreign = h.read("B", "grove.fabrication_options", {"definition_ref": ref, "type_id": "body"})
        self.assertTrue(set(row["id"] for row in menu["action"]["choices"]["sources"]).isdisjoint(
            row["id"] for row in foreign["action"]["choices"]["sources"]))
        initial = {row["entity_id"]: h.entity(row["entity_id"]) for row in args["sources"]}
        result = h.call("A", "grove.fabricate", args, operation="form-once")
        formed = h.entity(result["entity"]["id"])
        self.assertEqual(sm.quantity(formed, slot="body"), 5)
        self.assertEqual(formed["components"]["combustion"], {"rate": 3, "spread": False})
        self.assertEqual(formed["components"]["container"]["liquid_capacity"], 0)
        self.assertEqual(formed["state"]["initial_structural_quantity"], 5)
        self.assertEqual(sum(row["capacity"] for row in formed["components"]["buoyant"]["supports"].values()), 20)
        for row in args["sources"]:
            source = h.entity(row["entity_id"])
            self.assertEqual(sm.quantity(source), sm.quantity(initial[source["id"]])-row["quantity_u"])
            self.assertTrue(any(lot.get("origin_object") == source["id"] for lot in formed["matter"]))
        self.assertEqual(h.call("A", "grove.fabricate", args, operation="form-once"), result)
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual(h.entity(formed["id"]), formed)
        self.place(formed["id"])
        h.approach("B", formed["id"])
        shared = h.read("B", "grove.inspect", {"entity_id": formed["id"]})["entity"]
        self.assertEqual(sm.quantity(shared, slot="body"), 5)
        inspected = h.read("B", "grove.inspect", {"entity_id": formed["id"]})
        pickup = next(option for option in inspected["action_options"] if option["action"] == "pickup")
        h.call("B", "grove.interact", {**pickup["arguments"], "expected_player_version": h.view("B")["player"]["version"]})
        self.assertEqual(h.entity(formed["id"])["owner"], h.roles["B"])

    def test_stale_foreign_and_constructed_sources_do_not_consume(self):
        h = self.h;ref = self.publish(formation_definition())
        _, args = self.plan(ref, "body")
        original = {row["entity_id"]: h.entity(row["entity_id"]) for row in args["sources"]}
        bad = deepcopy(args);bad["sources"][0]["expected_entity_version"] += 1
        with self.assertRaises(StateConflict):h.call("A", "grove.fabricate", bad)
        bad = deepcopy(args);bad["sources"][0]["quantity_u"] += 1
        with self.assertRaises(RuleViolation):h.call("A", "grove.fabricate", bad)
        bad = deepcopy(args);bad["expected_player_version"] = h.view("B")["player"]["version"]
        with self.assertRaises((RuleViolation, PermissionDenied)):h.call("B", "grove.fabricate", bad)
        for eid, snapshot in original.items():self.assertEqual(h.entity(eid), snapshot)
        backpack = h.view()["player"]["backpack_id"]
        with self.assertRaises(RuleViolation) as rejected:h.interact("A", backpack, "pickup")
        self.assertEqual(rejected.exception.details, {"reason": "action_not_offered", "action": "pickup",
            "target_id": backpack, "target_name": "随身背包"})
        formed = self.form(ref, "body")
        menu = h.read("A", "grove.fabrication_options", {"definition_ref": ref, "type_id": "body"})
        self.assertNotIn(formed, [row["id"] for row in menu["action"]["choices"]["sources"]])
        for material in menu["materials"]:
            actual = sum(lot["quantity"] for source in menu["action"]["choices"]["sources"]
                         for lot in source["lots"] if lot["material"] == material["material_id"])
            self.assertEqual(material["available_u"], actual)
            self.assertEqual(material["missing_u"], max(0, material["quantity_u"]-actual))

    def test_imported_qualified_fuel_body_and_live_port_drive_continue_across_actors(self):
        h = self.h
        raw = {"key": "qualified", "name": "用途材料", "version": 1, "materials": {
            "coal": {"name": "炭粒", "feedstock": "wood", "load_per_u": 1, "combustible": True,
                     "liquid": False, "uses": {"mechanical_fuel": True, "hearth_fuel": True}}},
            "types": {"prep": {"name": "制炭器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20}}}
        material_ref, objects = h.publish("A", raw, ["prep"])
        prep = objects["prep"]
        h.action("A", prep, "charge", item_id=h.item("A", "木柴")["id"], material="coal", amount=8)
        h.interact("A", prep, "pickup")
        controller = {"key": "water_driven", "name": "现场条件驱动", "version": 1,
            "material_imports": {"original_coal": {"definition_ref": material_ref, "symbol": "coal"}},
            "types": {"source": {"name": "见水动力", "body_material": "original_coal",
                "cost_u": {"original_coal": 2, "wood": 2}, "mechanical": {"role": "source", "torque": 4},
                "ports": {"water": {"fields": ["physical.contained_water"]}},
                "rules": [{"id": "respond", "phase": "after", "when": True, "effects": [
                    {"op": "drive", "enabled": {"op": "gt", "left": {"object": {"port": "water", "field": "physical.contained_water"}}, "right": 0}}]}]}}}
        ref = self.publish(controller)
        menu, args = self.plan(ref, "source")
        self.assertEqual(menu["cost_u"], {material_ref+"#coal": 2, "wood": 2})
        source = h.call("A", "grove.fabricate", args)["entity"]["id"]
        self.place(source)
        kettle = h.item("A", "水壶")["id"];self.place(kettle)
        h.action("A", source, "bind_port", port="water", other_id=kettle)
        fuel_lot = next(lot for lot in h.entity(prep)["matter"] if lot["material"] == material_ref+"#coal")
        h.action("A", source, "fuel", item_id=prep, lot_id=fuel_lot["id"], amount=4)
        h.ticks(1)
        first = h.entity(source)["state"]["program"]["mechanical"]
        self.assertTrue(first["enabled"]);self.assertEqual(first["position"], 0)
        h.ticks(1)
        moving = h.entity(source)["state"]["program"]["mechanical"]
        self.assertGreater(moving["angle"], 0);self.assertGreater(moving["speed"], 0)
        self.assertEqual(moving["energy"], 3)
        automatic = deepcopy(h.entity(source)["state"]["program"]["drive_request"])
        self.assertEqual(automatic["source"], {"kind": "automatic_rule", "rule_id": "respond", "phase": "after"})
        self.assertEqual(automatic["applies_from_step"], automatic["requested_step"]+1)
        self.assertIn("step", h.entity(source)["state"]["program"]["mechanical_last_solved"])
        h.approach("B", kettle);h.interact("B", kettle, "close")
        h.ticks(1)
        blocked = h.entity(source)["state"]["program"]
        self.assertEqual(blocked["process"]["status"], "waiting")
        self.assertEqual(blocked["port_bindings"], {"water": kettle})
        self.assertIn("blocked_reason", blocked["process"])
        self.assertEqual(blocked["drive_request"], automatic)
        self.assertTrue(blocked["mechanical"]["enabled"])
        self.assertEqual(blocked["mechanical"]["energy"], 2)
        self.assertEqual(blocked["process"]["steps"], 0)
        h.interact("B", kettle, "open")
        h.action("B", source, "unbind_port", port="water")
        h.action("B", source, "bind_port", port="water", other_id=kettle)
        h.ticks(1)
        self.assertNotIn("blocked_reason", h.entity(source)["state"]["program"]["process"])
        self.assertEqual(h.entity(source)["state"]["program"]["port_binding_version"], 3)
        self.assertEqual(h.entity(source)["state"]["program"]["drive_request"], automatic)
        h.action("B", source, "stop")
        saved = deepcopy(h.entity(source))
        self.assertEqual(saved["state"]["program"]["drive_request"]["source"],
                         {"kind": "explicit_action", "action_id": "stop", "actor_role_id": h.roles["B"]})
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual(h.entity(source), saved)

    def test_attached_buoyancy_on_nonbuoyant_root_is_real_known_fact(self):
        h = self.h
        raw = {"key": "floats", "name": "附着浮力", "version": 1, "types": {
            "raft": {"name": "干布货兜", "body_material": "cloth", "cost_u": {"cloth": 2},
                "components": {"container": {"slots": 2, "liquid_capacity": 0, "open": True}}},
            "float": {"name": "真实浮件", "body_material": "wood", "cost_u": {"wood": 3},
                "components": {"buoyant": {"capacity": 20}, "attachment": {"kind": "buoyancy", "fits": "container", "max_children": 4}}},
            "probe": {"name": "现场浮力指示", "body_material": "wood", "cost_u": {"wood": 1},
                "fields": {"ready": {"type": "boolean", "default": False}},
                "ports": {"load": {"fields": ["physical.can_float", "physical.load.total"]}},
                "rules": [{"id": "observe", "phase": "after", "when": True, "effects": [{"op": "set", "field": "ready",
                    "value": {"object": {"port": "load", "field": "physical.can_float"}}}]}]}}}
        ref = self.publish(raw)
        raft, part, probe = [self.form(ref, kind) for kind in ("raft", "float", "probe")]
        self.place(raft);self.place(probe)
        h.interact("A", raft, "attach", item_id=part)
        h.action("A", probe, "bind_port", port="load", other_id=raft)
        h.ticks(1)
        self.assertNotIn("buoyant", h.entity(raft)["components"])
        self.assertTrue(h.entity(probe)["state"]["program"]["fields"]["ready"])
        h.approach("B", probe)
        observed = h.read("B", "grove.inspect", {"entity_id": probe})["entity"]
        self.assertTrue(observed["creative"]["fields"]["ready"])
        h.approach("B", raft);h.interact("B", part, "detach")
        h.ticks(1)
        self.assertFalse(h.entity(probe)["state"]["program"]["fields"]["ready"])

    def test_same_type_two_ports_drive_independent_old_gear_load_stroke_chains(self):
        h = self.h
        fuel_ref, prep = h.publish("A", {"key": "coal_for_two", "name": "实炭", "version": 1,
            "materials": {"coal": {"name": "炭粒", "feedstock": "wood", "load_per_u": 1,
                "combustible": True, "liquid": False, "uses": {"mechanical_fuel": True}}},
            "types": {"prep": {"name": "制炭器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20}}}, ["prep"])
        prep = prep["prep"]
        h.action("A", prep, "charge", item_id=h.item("A", "木柴")["id"], material="coal", amount=12)
        h.interact("A", prep, "pickup")
        controller = {"key": "two_sites", "name": "同型两处", "version": 1,
            "material_imports": {"coal": {"definition_ref": fuel_ref, "symbol": "coal"}},
            "types": {"source": {"name": "见水动力", "body_material": "coal", "cost_u": {"coal": 2, "wood": 2},
                "mechanical": {"role": "source", "teeth": 16, "torque": 8, "speed": 1, "energy_per_step": 1},
                "ports": {"site": {"fields": ["physical.contained_water"]}},
                "rules": [{"id": "respond", "phase": "after", "when": True, "effects": [{"op": "drive",
                    "enabled": {"op": "gt", "left": {"object": {"port": "site", "field": "physical.contained_water"}}, "right": 0}}]}]},
                "vessel": {**formation_definition()["types"]["body"],
                    "components": {"container": {"slots": 3, "liquid_capacity": 20, "open": True}}}}}
        ref = self.publish(controller)
        sources = [self.form(ref, "source") for _ in range(2)]
        empty = self.form(ref, "vessel")
        old = fixtures.package()
        old = {"key": "old_networks", "name": "真实齿轮执行链", "version": 1,
               "types": {key: old["types"][key] for key in ("gear", "gate")}}
        old_ref = self.publish(old)
        h.ticks(30)
        context = h.call("A", "grove.generation_context", {"kind": "layout", "reason": "user_request"})
        allowed = {(p["x"], p["y"]): p for p in context["allowed_tiles"]}
        candidates = sorted(allowed, key=lambda p: (abs(p[0]-2)+abs(p[1]-2), p[1], p[0]))
        line = next([(p[0]+i, p[1]) for i in range(4)] for p in candidates
                    if all((p[0]+i, p[1]) in allowed for i in range(4)))
        opportunity = context["opportunity"]
        result = h.call("A", "grove.content_submit", {"opportunity_id": opportunity["id"],
            "expected_opportunity_version": opportunity["version"], "content": {"kind": "layout", "objects": [
                {"definition_ref": old_ref, "type_id": kind, **allowed[p]} for p, kind in zip(line, ("gear", "gate", "gear", "gate"))]}})
        gears = [result["entities"][i]["id"] for i in (0, 2)]
        gates = [result["entities"][i]["id"] for i in (1, 3)]
        kettle = h.item("A", "水壶")["id"]
        for source, gear, gate in zip(sources, gears, gates):
            h.approach("A", gear);self.place(source)
            h.action("A", source, "connect", other_id=gear)
            h.action("A", gear, "connect", other_id=gate)
        self.place(kettle);self.place(empty)
        for source, site in zip(sources, (kettle, empty)):
            h.action("A", source, "bind_port", port="site", other_id=site)
            lot = next(lot for lot in h.entity(prep)["matter"] if lot["material"] == fuel_ref+"#coal")
            h.action("A", source, "fuel", item_id=prep, lot_id=lot["id"], amount=3)
        def positions():return [h.entity(eid)["state"]["program"]["mechanical"]["position"] for eid in gates]
        def energy():return [h.entity(eid)["state"]["program"]["mechanical"]["energy"] for eid in sources]
        h.ticks(1);self.assertEqual(positions(), [0, 0]);self.assertEqual(energy(), [3, 3])
        h.ticks(1);self.assertEqual(positions(), [2, 0]);self.assertEqual(energy(), [2, 3])
        h.approach("B", empty);h.interact("B", empty, "pickup")
        h.approach("B", kettle);h.interact("B", kettle, "fill", item_id=empty, amount=20)
        self.place(empty, "B")
        self.assertEqual(sm.quantity(h.entity(kettle), "water", "contained"), 0)
        self.assertEqual(sm.quantity(h.entity(empty), "water", "contained"), 20)
        h.ticks(1);self.assertEqual(positions(), [4, 0]);self.assertEqual(energy(), [1, 3])
        h.ticks(1);self.assertEqual(positions(), [4, 2]);self.assertEqual(energy(), [1, 2])
        h.ticks(1);self.assertEqual(positions(), [4, 4])
        self.assertFalse(h.entity(gates[1])["components"]["barrier"]["active"])
        h.action("B", sources[1], "fuel", item_id=h.item("B", "木柴")["id"], amount=2)
        h.ticks(1);self.assertEqual(positions(), [4, 6])
        for source, site in zip(sources, (kettle, empty)):
            self.assertEqual(h.entity(source)["state"]["program"]["port_bindings"], {"site": site})
        h.approach("B", gates[1])
        player = h.view("B")["player"];gate = h.entity(gates[1])
        h.call("B", "grove.move", {"dx": gate["x"]-player["x"], "dy": gate["y"]-player["y"], "expected_player_version": player["version"]})
        self.assertEqual((h.view("B")["player"]["x"], h.view("B")["player"]["y"]), (gate["x"], gate["y"]))

    def test_fact_port_projection_uses_read_range_instead_of_mechanical_distance(self):
        h = self.h
        ref = self.publish({"key": "read_range", "name": "远处实物读数", "version": 1,
            "types": {"probe": {"name": "读水托架", "body_material": "wood", "cost_u": {"wood": 1},
                "ports": {"cup": {"fields": ["physical.contained_water"]}}}}})
        probe = self.form(ref, "probe");self.place(probe)
        anchor = h.entity(probe)
        destination = next(item for item in h.view()["entities"]
            if 4 <= max(abs(item["x"]-anchor["x"]), abs(item["y"]-anchor["y"])) <= 6)
        h.approach("A", destination["id"])
        kettle = h.item("A", "水壶")["id"];self.place(kettle)
        distance = max(abs(h.entity(kettle)["x"]-anchor["x"]), abs(h.entity(kettle)["y"]-anchor["y"]))
        self.assertGreater(distance, 2);self.assertLessEqual(distance, 8)
        h.approach("A", probe)
        inspected = h.read("A", "grove.inspect", {"entity_id": probe})
        binding = next(row for row in inspected["action_options"] if row["action"] == "bind_port")
        candidate = next(row for row in binding["choices"]["others"] if row["id"] == kettle)
        fact = candidate["preconditions"][0]
        self.assertEqual(fact["condition"], "public_fact_range")
        self.assertEqual(fact["required_max"], 8);self.assertEqual(fact["required_actor_max"], 8)
        self.assertTrue(fact["satisfied"])
        h.action("A", probe, "bind_port", port="cup", other_id=kettle)
        self.assertEqual(h.entity(probe)["state"]["program"]["port_bindings"], {"cup": kettle})

    def test_seek_pages_exceed_old_publication_seats_and_keep_immutable_refs(self):
        h = self.h
        refs = []
        for index in range(35):
            raw = formation_definition("library_"+str(index))
            raw["types"]["body"]["cost_u"] = {"wood": 1}
            refs.append(self.publish(raw))
            if index % 8 == 7:h.ticks(1)
        observed, cursor = [], None
        while True:
            args = {"limit": 7}
            if cursor:args["after"] = cursor
            page = h.read("B", "grove.definition_list", args)
            observed.extend(row["definition_ref"] for row in page["definitions"])
            if not page["has_more"]:break
            cursor = page["next_cursor"]
        self.assertEqual(observed, list(reversed(refs)))
        self.assertEqual(page["storage"]["definitions_count"], 35)
        self.assertEqual(h.read("B", "grove.definition_read", {"definition_ref": refs[0]})["definition"]["key"], "library_0")
        catalog = h.read("B", "grove.rule_catalog")
        self.assertNotIn("definitions", catalog)
        self.assertEqual(catalog["definition_directory"]["function_id"], "grove.definition_list")
        for _ in range(32):self.place(self.form(refs[0], "body"))
        _, args = self.plan(refs[-1], "body")
        before = {row["entity_id"]: h.entity(row["entity_id"]) for row in args["sources"]}
        with self.assertRaisesRegex(RuleViolation, "32"):
            h.call("A", "grove.fabricate", args)
        for eid, snapshot in before.items():self.assertEqual(h.entity(eid), snapshot)
        self.assertEqual(h.read("B", "grove.definition_list")["storage"]["definitions_count"], 35)

    def test_ordinary_receiver_releases_consumed_snapshots_for_new_material(self):
        h = self.h
        raw = {"key": "four_solids", "name": "四种实料", "version": 1,
            "materials": {key: {"name": key, "feedstock": "wood", "load_per_u": 1,
                "liquid": False, "combustible": False} for key in ("a", "b", "c", "d")},
            "types": {"prep": {"name": "制料器", "body_material": "stone",
                "cost": {"stone": 1}, "container_capacity": 20}}}
        ref, objects = h.publish("A", raw, ["prep"])
        receiver = h.item("A", "水壶")["id"]
        bowl = next(item["id"] for item in h.view()["entities"] if item["name"] == "石碗")
        h.approach("A", bowl)
        h.interact("A", bowl, "pour", item_id=receiver, amount=sm.quantity(h.entity(receiver), "water", "contained"))
        for symbol in raw["materials"]:
            h.action("A", objects["prep"], "charge", item_id=h.item("A", "木柴")["id"], material=symbol, amount=1)
            lot = next(lot for lot in h.entity(objects["prep"])["matter"] if lot["material"] == ref+"#"+symbol)
            h.interact("A", receiver, "transfer", item_id=objects["prep"], lot_id=lot["id"], amount=1)
        self.assertEqual(len(h.entity(receiver)["state"]["program"]["materials"]), 4)
        carried = next(item for item in h.view()["inventory"] if item["id"] == receiver)
        for lot in carried["matter"]:
            if "#" in lot["material"]:
                self.assertEqual(lot["material_import"], {"definition_ref": ref, "symbol": lot["material"].split("#")[1]})
        from agent_world.grove_agent_observation import _companion_scene_index
        projected = _companion_scene_index({"inventory": [carried]})["inventory"][0]
        self.assertEqual(projected["matter"], carried["matter"])
        self.place(receiver);h.interact("A", receiver, "close");h.approach("B", receiver)
        hidden = h.read("B", "grove.inspect", {"entity_id": receiver})["entity"]
        self.assertFalse(any("material_import" in lot for lot in hidden["matter"]))
        h.interact("A", receiver, "open");h.interact("A", receiver, "pickup")
        formed_ref = self.publish({"key": "four_body", "name": "实料合成", "version": 1,
            "material_imports": {key: {"definition_ref": ref, "symbol": key} for key in raw["materials"]},
            "types": {"body": {"name": "合成小样", "body_material": "a",
                "cost_u": {key: 1 for key in raw["materials"]}}}})
        body = self.form(formed_ref, "body")
        self.assertEqual(sm.quantity(h.entity(body), slot="body"), 4)
        self.assertEqual(h.entity(receiver)["state"]["program"]["materials"], {})
        h.ticks(30)
        raw["key"] = "fifth_solid";raw["materials"] = {"e": raw["materials"]["a"]}
        fifth_ref, objects = h.publish("A", raw, ["prep"])
        h.action("A", objects["prep"], "charge", item_id=h.item("A", "木柴")["id"], material="e", amount=1)
        lot = next(lot for lot in h.entity(objects["prep"])["matter"] if lot["material"] == fifth_ref+"#e")
        h.interact("A", receiver, "transfer", item_id=objects["prep"], lot_id=lot["id"], amount=1)
        self.assertEqual(set(h.entity(receiver)["state"]["program"]["materials"]), {fifth_ref+"#e"})
        self.assertEqual(h.read("B", "grove.definition_read", {"definition_ref": ref})["definition"]["key"], "four_solids")

    def test_contract_port_offer_pins_sources_and_binding_edit_requires_new_consent(self):
        h = self.h
        ref = self.publish({"key": "bound_pact", "name": "现场交付", "version": 1,
            "types": {"pact": {"name": "水旁契约", "body_material": "wood", "cost_u": {"wood": 1},
                "ports": {"site": {"fields": ["physical.contained_water"]}},
                "contract": {"material": "wood", "quantity": 2, "duration_ticks": 100,
                    "delivery_when": {"op": "gt", "left": {"object": {"port": "site", "field": "physical.contained_water"}}, "right": 0}}}}})
        pact = self.form(ref, "pact");self.place(pact)
        first, second = [h.item(actor, "水壶")["id"] for actor in "AB"]
        self.place(first);self.place(second, "B")
        h.action("A", pact, "bind_port", port="site", other_id=first)
        first_version = h.entity(first)["version"]
        h.action("A", pact, "offer", other_id=h.roles["B"])
        terms = h.entity(pact)["state"]["program"]["contract"]
        self.assertEqual(terms["pinned_ports"], {"site": {"entity_id": first, "entity_version": first_version}})
        self.assertEqual(terms["port_binding_version"], 1)
        h.action("A", pact, "accept", terms_version=terms["terms_version"])
        h.action("A", pact, "bind_port", port="site", other_id=second)
        changed = h.entity(pact)["state"]["program"]["contract"]
        self.assertEqual(changed["accepted"], []);self.assertEqual(changed["status"], "unoffered")
        h.action("A", pact, "offer", other_id=h.roles["B"])
        current = h.entity(pact)["state"]["program"]["contract"]["terms_version"]
        with self.assertRaises(StateConflict):h.action("B", pact, "accept", terms_version=terms["terms_version"])
        for actor in "AB":h.action(actor, pact, "accept", terms_version=current)
        with self.assertRaises(RuleViolation):h.action("A", pact, "bind_port", port="site", other_id=first)
        h.action("B", pact, "deliver", item_id=h.item("B", "木柴")["id"], amount=2)
        self.assertEqual(h.entity(pact)["state"]["program"]["contract"]["status"], "complete")
        h.interact("A", first, "pickup")
        h.action("A", pact, "release", item_id=first)
        self.assertEqual(sm.quantity(h.entity(first), "wood", "contained"), 2)

    def test_large_legal_definition_can_be_read_completely_without_directory_expansion(self):
        from agent_world.grove_agent_observation import companion_projection, _bounded_agent_projection, _agent_tool_text, _agent_tool_text_fits
        kind = {"name": "完整的公开类型名称"*5, "body_material": "wood", "cost_u": {"wood": 1},
            "fields": {"v"+str(i): {"type": "integer", "default": 0, "min": 0, "max": 100} for i in range(8)},
            "rules": [{"id": "r"+str(i), "phase": "after", "when": True,
                "effects": [{"op": "set", "field": "v0", "value": i}]} for i in range(8)],
            "actions": [{"id": "a"+str(i), "label": "公开动作"*10, "effects": [{"op": "set", "field": "v1", "value": i}]} for i in range(8)]}
        ref = self.publish({"key": "large_legal", "name": "按需完整读取", "version": 1,
            "types": {"t"+str(i): deepcopy(kind) for i in range(8)}})
        read = self.h.read("B", "grove.definition_read", {"definition_ref": ref})
        self.assertGreater(sm.encoded_size(read["definition"]), 16000)
        projected = _bounded_agent_projection("grove.definition_read", companion_projection("grove.definition_read", {"result": read}))
        self.assertTrue(_agent_tool_text_fits(_agent_tool_text(projected), native=True))
        self.assertEqual(projected["result"], read)
        self.assertEqual(self.h.read("B", "grove.definition_list")["definitions"][0]["type_ids"], list(read["definition"]["types"]))

    def test_large_derived_explanations_do_not_hide_complete_definition_or_publish_receipt(self):
        from agent_world.grove_agent_observation import companion_projection, _bounded_agent_projection, _agent_tool_text, _agent_tool_text_fits
        kind = {"name": "可读大型反应定义", "body_material": "wood", "cost_u": {"wood": 1}, "container_capacity": 100,
            "rules": [{"id": "r"+str(i)+"x"*78, "phase": "reaction", "when": True,
                "effects": [{"op": "react", "inputs": {"wood": 1}, "outputs": {"cloth": 1},
                    "rate": 1, "channel": "c", "mode": "exclusive"} for _ in range(7)]} for i in range(8)]}
        raw = {"key": "large_react", "name": "派生解释读取", "version": 1,
            "types": {"t"+str(i): deepcopy(kind) for i in range(3)}}
        published = self.h.world.call_function("w", "grove.definition_publish", self.h.roles["A"],
            {"definition": raw}, operation_id="large-definition-publish")
        ref = published["result"]["definition_ref"]
        read = self.h.read("B", "grove.definition_read", {"definition_ref": ref})
        self.assertLess(sm.encoded_size(read["definition"]), 24*1024)
        for function, payload in (("grove.definition_read", {"result": read}), ("grove.definition_publish", published)):
            original = companion_projection(function, payload)
            self.assertFalse(_agent_tool_text_fits(_agent_tool_text(original)))
            projected = _bounded_agent_projection(function, original)
            self.assertTrue(_agent_tool_text_fits(_agent_tool_text(projected), native=True))
            self.assertEqual(projected["result"]["definition"], read["definition"])
            self.assertEqual(projected["result"]["definition_ref"], ref)
            for key in ("author", "hash", "published_tick", "published_at"):
                if key in payload["result"]:self.assertEqual(projected["result"][key], payload["result"][key])
            self.assertEqual(projected["result"]["projection"]["omitted_sections"], ["material_semantics", "declaration_semantics"])
            self.assertTrue(projected["result"]["projection"]["complete_definition"])
            if function.endswith("publish"):
                for key in ("operation_id", "ok", "commit_seq", "replayed"):
                    if key in published:self.assertEqual(projected[key], published[key])


if __name__ == "__main__":
    unittest.main()
