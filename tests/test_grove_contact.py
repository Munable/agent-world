"""Current contact contracts: deterministic isolated fixtures, not model play."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from agent_world import FunctionSpec, FunctionOutcome, WorldRuntime
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from agent_world.simulation.programs import compile_program, plan_reaction
from agent_world.worlds.shared_grove import contact, commit, creative, model as m
from agent_world.worlds.shared_grove.definitions import REGISTRY, BODY_MATERIALS, bind_material
from agent_world.worlds.shared_grove.world import make_world, resume_clock
import test_grove_creative as fixtures


def definition(key="contact_material"):
    return {"key": key, "name": "接触材料", "version": 1,
        "materials": {symbol: {"name": name, "feedstock": "wood", "load_per_u": 1,
                                "combustible": False, "liquid": False}
                      for symbol, name in (("starter", "触媒料"), ("product", "成料"))},
        "contact_reactions": [{"id": "mix", "when": True,
            "reaction": {"op": "react", "inputs": {"starter": 1, "cloth": 1}, "outputs": {"product": 2}}}],
        "types": {"prep": {"name": "制备器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20}}}


class MaterialContactTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def compile(self, raw):
        return compile_program(raw, materials=REGISTRY.materials, body_materials=BODY_MATERIALS, max_custom_materials=4)

    def prepare(self, raw=None, amount=4):
        h = self.h
        ref, objects = h.publish("A", raw or definition(), ["prep"])
        prep = objects["prep"]
        h.action("A", prep, "charge", item_id=h.item("A", "木柴")["id"], material="starter", amount=amount)
        h.approach("A", prep)
        h.interact("A", prep, "pickup")
        return ref, prep

    def put(self, target, source, identity, amount=1):
        h = self.h
        if h.entity(target).get("x") is not None:
            h.approach("A", target)
        lot = next(lot for lot in h.entity(source)["matter"] if lot["material"] == identity)
        return h.interact("A", target, "transfer", item_id=source, lot_id=lot["id"], amount=amount)

    def fixture(self, callback):
        h = self.h
        handler = lambda ctx, args: (callback(ctx), FunctionOutcome({}))[1]
        spec = FunctionSpec("grove.contact_fixture", handler, {"type": "object", "additionalProperties": False})
        world = make_world()
        h.world.install_world("w", replace(world, version=world.version + 1, state_version=world.state_version + 1,
            migrations={**world.migrations, world.state_version + 1: resume_clock}, functions=(*world.functions, spec)))
        return h.call("A", "grove.contact_fixture", {})

    def test_compile_restricts_refs_local_input_and_heat_and_preserves_absent_shape(self):
        raw = definition()
        compiled = self.compile(raw)
        self.assertEqual(compiled["contact_reactions"][0]["reaction"]["heat"], 0)
        catalog = self.h.read("A", "grove.rule_catalog", {"topic": "all"})
        self.assertEqual(catalog["builtin_materials"]["water"]["load_per_u"], REGISTRY.materials["water"].load_per_u)
        invalid = definition();invalid["contact_reactions"][0]["reaction"]["inputs"] = {"starter": 1, "water": 1}
        with self.assertRaisesRegex(ValueError, "water: 1u \\* load 2"):
            self.compile(invalid)
        invalid = definition();invalid["materials"]["starter"]["load_per_u"] = 2
        with self.assertRaisesRegex(ValueError, "feedstock wood load_per_u 1, received 2"):
            self.compile(invalid)
        del raw["contact_reactions"]
        self.assertNotIn("contact_reactions", self.compile(raw))
        for ref in ({"mechanical": "energy"}, {"event": "mechanical_position"},
                    {"contract": "delivered"}, {"object": {"id": "anything", "field": "mechanical.position"}}):
            raw = definition();raw["contact_reactions"][0]["when"] = {"op": "gt", "left": ref, "right": 0}
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.compile(raw)
        for change in ("local", "heat"):
            raw = definition();effect = raw["contact_reactions"][0]["reaction"]
            if change == "local":effect.update(inputs={"wood": 1}, outputs={"cloth": 1})
            else:effect["heat"] = 1
            with self.assertRaises(ValueError):self.compile(raw)

    def test_same_immutable_material_reacts_in_two_ordinary_containers_and_survives_restart(self):
        h = self.h;ref, prep = self.prepare()
        kettle = h.item("A", "水壶")["id"]
        crate = next(e["id"] for e in h.view()["entities"] if e["material"] == "wood" and "container" in e["capabilities"])
        for target in (kettle, crate):
            self.put(target, prep, ref + "#starter")
            self.put(target, next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"] and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"])), "cloth")
        h.ticks(1)
        for target in (kettle, crate):
            entity = h.entity(target)
            self.assertEqual(sm.quantity(entity, ref + "#product", "contained"), 2, {"id": target, "lifecycle": entity["lifecycle"], "matter": entity["matter"], "program": entity["state"].get("program")})
            self.assertEqual(sm.quantity(entity, ref + "#starter", "contained"), 0)
            self.assertNotIn("definition_ref", entity["state"].get("program", {}))
            self.assertEqual(entity["state"]["program"]["contact_process"]["steps"], 1)
        before = h.entity(crate)["matter"]
        h.world = WorldRuntime(h.db);h.world.install_world("w", make_world())
        self.assertEqual(h.entity(crate)["matter"], before)
        h.approach("B", crate)
        visible = h.read("B", "grove.inspect", {"entity_id": crate})["entity"]
        self.assertEqual(visible["simulation"]["contact"]["last_actual_reactions"][0]["outputs"], {ref + "#product": 2})

    def test_closed_container_reacts_without_leaking_and_personal_and_child_are_separate(self):
        h = self.h;ref, prep = self.prepare()
        kettle = h.item("A", "水壶")["id"]
        self.put(kettle, prep, ref + "#starter")
        self.put(kettle, next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"] and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"])), "cloth")
        h.interact("A", kettle, "close");h.interact("A", kettle, "drop")
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(kettle), ref + "#product"), 2)
        h.approach("B", kettle)
        hidden = h.read("B", "grove.inspect", {"entity_id": kettle})["entity"]
        self.assertNotIn("contact", hidden["simulation"])
        self.assertNotIn("contact_process", hidden["state"]["program"])
        bag = h.entity(h.view()["player"]["backpack_id"])
        # Explicit fixture only: personal itself never becomes an executor.
        self.fixture(lambda ctx: self.assertEqual(contact.candidates(ctx, bag), []))

    def test_foreign_canonical_supply_in_authored_container_and_next_step_products(self):
        h = self.h;raw = definition();raw["materials"]["finish"] = {**raw["materials"]["product"], "name": "终料"}
        raw["contact_reactions"].append({"id": "refine", "when": True,
            "reaction": {"op": "react", "inputs": {"product": 1}, "outputs": {"finish": 1}}})
        ref, prep = self.prepare(raw)
        foreign = {"key": "foreign_host", "name": "他作者容器", "version": 1,
                   "types": {"prep": {"name": "他作者器皿", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20}}}
        h.ticks(30);_, objects = h.publish("B", foreign, ["prep"]);target = objects["prep"]
        h.approach("A", target)
        self.put(target, prep, ref + "#starter")
        self.put(target, next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"] and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"])), "cloth")
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#product"), 2)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#finish"), 0)
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#finish"), 1)

    def test_concentration_counts_unimported_foreign_material_and_metadata_does_not_trigger(self):
        h = self.h;raw = definition();raw["contact_reactions"][0]["when"] = {"op": "gte", "left": {"concentration": "starter"}, "right": 50}
        ref, prep = self.prepare(raw, amount=1);target = next(e["id"] for e in h.view()["entities"] if e["material"] == "wood" and "container" in e["capabilities"])
        self.put(target, prep, ref + "#starter")
        h.ticks(30)
        foreign = definition("foreign_diluent");foreign.pop("contact_reactions");foreign["materials"] = {"diluent": {**foreign["materials"]["starter"], "name": "第三方稀释料"}}
        ref2, objs = h.publish("B", foreign, ["prep"])
        def add_diluent(ctx):
            # Isolated physical fixture deliberately adds a third-party material.
            e = m.get_entity(ctx, target);mat = contact._material(ctx, ref2 + "#diluent");bind_material(e, mat)
            sm.add_lot(e, mat.id, 8, "contained");commit.save_entity(ctx, e)
        self.fixture(add_diluent)
        self.put(target, next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"] and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"])), "cloth")
        h.ticks(1)
        observed = h.read("A", "grove.inspect", {"entity_id": target})["entity"]["simulation"]["contact"]
        self.assertEqual(observed["reactions"][0]["observed_concentrations"]["starter"], 10)
        self.assertFalse(observed["reactions"][0]["condition"]["satisfied"])
        self.assertEqual(sm.quantity(h.entity(target), ref + "#product"), 0)
        self.assertNotIn("contact", h.read("B", "grove.inspect", {"entity_id": objs["prep"]})["entity"]["simulation"])

    def test_liquid_capacity_counts_liquids_not_solids(self):
        materials = {"solid": {"load_per_u": 1, "liquid": False}, "liquid": {"load_per_u": 1, "liquid": True}}
        effect = {"op": "react", "inputs": {"solid": 1}, "outputs": {"liquid": 1}, "rate": 2}
        self.assertEqual(plan_reaction(effect, {"solid": 100}, materials, None, liquid_capacity=1, liquid_held=0)["extent"], 1)
        self.assertEqual(plan_reaction(effect, {"solid": 100}, materials, None, liquid_capacity=1, liquid_held=1)["extent"], 0)

    def test_contact_error_rolls_back_matter_and_keeps_prior_result(self):
        h = self.h;raw = definition();raw["contact_reactions"][0]["when"] = {"op": "gt", "left": {"op": "div", "left": 1, "right": 0}, "right": 0}
        ref, prep = self.prepare(raw);target = h.item("A", "水壶")["id"]
        self.put(target, prep, ref + "#starter");self.put(target, next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"] and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"])), "cloth")
        before = h.entity(target)["matter"]
        h.ticks(2)
        self.assertEqual(h.entity(target)["matter"], before)
        self.assertEqual(h.entity(target)["state"]["program"]["contact_process"]["status"], "error")
        observed = h.read("A", "grove.inspect", {"entity_id": target})["entity"]["simulation"]["contact"]
        self.assertEqual(observed["reactions"][0]["condition"]["known"], False)
        self.assertIsNone(observed["reactions"][0]["condition"]["satisfied"])

    def frozen_self_return(self, authored):
        h = self.h;raw = definition("frozen_authored" if authored else "frozen_contact")
        raw["materials"]["starter"]["combustible"] = True
        returning = {"op": "react", "inputs": {"starter": 1}, "outputs": {"starter": 1}, "mode": "independent"}
        consuming = {"op": "react", "inputs": {"starter": 1}, "outputs": {"product": 1}, "mode": "independent"}
        raw["contact_reactions"] = [{"id": "b_consume", "when": True, "reaction": consuming}]
        if authored:
            raw["types"]["prep"]["rules"] = [{"id": "return", "phase": "reaction", "when": True, "effects": [returning]}]
        else:
            raw["contact_reactions"].insert(0, {"id": "a_return", "when": True, "reaction": returning})
        ref, prep = self.prepare(raw, amount=1)
        target = prep if authored else next(e["id"] for e in h.view()["entities"] if e["material"] == "wood" and "container" in e["capabilities"])
        if not authored:self.put(target, prep, ref + "#starter")
        def step_without_physics(ctx):
            # A phase boundary fixture isolates freezing from prior combustion.
            entity = m.get_entity(ctx, target)
            sm.add_lot(entity, ref + "#starter", 5, "contained", merge_burning=True, id_factory=lambda: m.identifier("lot"))
            entity["matter"][-1]["burning"] = True
            commit.save_entity(ctx, entity)
            creative.tick(ctx)
        self.fixture(step_without_physics)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#product"), 0)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#starter"), 6)

    def test_same_identity_contact_product_cannot_use_burning_frozen_supply(self):
        self.frozen_self_return(False)

    def test_same_identity_authored_product_cannot_feed_contact_in_same_step(self):
        self.frozen_self_return(True)

    def test_common_admission_rejects_without_deducting_source(self):
        h = self.h;ref, prep = self.prepare();target = h.item("A", "水壶")["id"]
        before_source, before_target = h.entity(prep)["matter"], h.entity(target)["matter"]
        with patch("agent_world.worlds.shared_grove.creative.MAX_COMMIT_BYTES", 1):
            with self.assertRaisesRegex(RuleViolation, "完整物理域"):
                self.put(target, prep, ref + "#starter")
        self.assertEqual(h.entity(prep)["matter"], before_source)
        self.assertEqual(h.entity(target)["matter"], before_target)

    def test_independent_author_consumes_real_foreign_product_and_own_material(self):
        h = self.h;ref, prep = self.prepare()
        crate = next(e["id"] for e in h.view()["entities"] if e["material"] == "wood" and "container" in e["capabilities"])
        self.put(crate, prep, ref + "#starter")
        cloth = next(item["id"] for item in h.view()["inventory"] if "personal" not in item["capabilities"]
                     and any(lot["material"] == "cloth" and lot["slot"] == "body" for lot in item["matter"]))
        self.put(crate, cloth, "cloth");h.ticks(1)
        foreign_lot = next(lot for lot in h.entity(crate)["matter"] if lot["material"] == ref + "#product")
        h.ticks(30)
        raw = definition("second_author")
        raw["material_imports"] = {"previous": {"definition_ref": ref, "symbol": "product"}}
        raw["contact_reactions"][0]["reaction"]["inputs"] = {"previous": 1, "starter": 1}
        ref2, objs = h.publish("B", raw, ["prep"]);source = objs["prep"]
        h.action("B", source, "charge", item_id=h.item("B", "木柴")["id"], material="starter", amount=1)
        h.approach("B", source);h.interact("B", source, "pickup");h.approach("B", crate)
        target = h.item("B", "水壶")["id"]
        starter = next(lot for lot in h.entity(source)["matter"] if lot["material"] == ref2 + "#starter")
        for obj, lot in ((source, starter), (crate, foreign_lot)):
            h.interact("B", target, "transfer", item_id=obj, lot_id=lot["id"], amount=1)
        h.ticks(1)
        self.assertEqual(sm.quantity(h.entity(target), ref2 + "#product"), 2)
        self.assertEqual(sm.quantity(h.entity(target), ref + "#product"), 0)
        self.assertEqual(sm.quantity(h.entity(crate), ref + "#product"), 1)
        actual = h.read("B", "grove.inspect", {"entity_id": target})["entity"]["simulation"]["contact"]["last_actual_reactions"][0]
        self.assertEqual(actual["inputs"], {ref + "#product": 1, ref2 + "#starter": 1})

    def test_authored_future_contact_chain_is_rejected_before_instance_cost(self):
        h = self.h;second = definition("future_second")
        second["contact_reactions"] = [{**deepcopy(second["contact_reactions"][0]), "id": "r" + str(i)} for i in range(6)]
        ref = h.call("B", "grove.definition_publish", {"definition": second})["definition_ref"]
        first = definition("future_first")
        first["material_imports"] = {"later": {"definition_ref": ref, "symbol": "starter"}}
        first["contact_reactions"] = [{"id": "r" + str(i), "when": True,
            "reaction": {"op": "react", "inputs": {"starter": 1}, "outputs": {"later": 1}}} for i in range(5)]
        first["types"]["prep"]["rules"] = [{"id": "produce", "phase": "reaction", "when": True,
            "effects": [{"op": "react", "inputs": {"wood": 1}, "outputs": {"starter": 1}}]}]
        inventory = deepcopy(h.view()["inventory"])
        with self.assertRaisesRegex(RuleViolation, "接触候选超过8"):
            h.publish("A", first, ["prep"])
        self.assertEqual(h.view()["inventory"], inventory)
