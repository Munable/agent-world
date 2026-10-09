"""Trusted rule-registration contracts; execution evidence is a separate stage."""
from copy import deepcopy
import unittest

from agent_world.simulation.effects import Transfer
from agent_world.simulation.frame import Frame
from agent_world.simulation import matter
from agent_world.simulation.programs import compile_program, plan_reaction, program_material_semantics
from agent_world.simulation.registry import MaterialDef, Registry, RuleSpec
from agent_world.worlds.shared_grove.definitions import (COMBUSTION_CADENCE_TICKS, REGISTRY,
    bind_material, composition_definition, registry_for)
from agent_world.worlds.shared_grove import model


class RuleRegistrationTests(unittest.TestCase):
    def registry(self):
        return Registry("test-rules-1", ("water", "damage", "failure", "transport"))

    def rule(self, name, phase, **kwargs):
        return RuleSpec(name, 1, phase, lambda snapshot: (), **kwargs)

    def test_explicit_dependencies_not_registration_order(self):
        registry = self.registry()
        registry.register_rule(self.rule("release", "failure", after=("load",)))
        registry.register_rule(self.rule("load", "damage", after=("contact",)))
        registry.register_rule(self.rule("contact", "water"))
        self.assertEqual([rule.id for rule in registry.ordered_rules()], ["contact", "load", "release"])

    def test_invalid_dependency_cannot_install_an_ambiguous_world(self):
        for rules in (
            (self.rule("a", "damage", after=("b",)), self.rule("b", "damage", after=("a",))),
            (self.rule("a", "water", after=("b",)), self.rule("b", "failure")),
        ):
            with self.subTest(rules=[rule.id for rule in rules]):
                registry = self.registry()
                for rule in rules:
                    registry.register_rule(rule)
                with self.assertRaises(ValueError):
                    registry.ordered_rules()

    def test_conflicting_writers_rejected_but_declared_damage_sum_is_valid(self):
        registry = self.registry()
        registry.register_rule(self.rule("burn", "damage", writes=frozenset({"integrity"})))
        registry.register_rule(self.rule("load", "damage", writes=frozenset({"integrity"})))
        with self.assertRaises(ValueError):
            registry.ordered_rules()
        registry = self.registry()
        for name in ("burn", "load"):
            registry.register_rule(self.rule(name, "damage", writes=frozenset({"integrity"}),
                reducers={"integrity": "sum"}))
        self.assertEqual(len(registry.ordered_rules()), 2)


class MaterialAuthorityTests(unittest.TestCase):
    def material(self, symbol="mix", load=1):
        return MaterialDef("program_test#" + symbol, "混合料", load, 4, True)

    def vessel(self, identifier):
        return model.normalize_entity({**composition_definition({"material": "stone", "construction": "vessel"}),
                                       "id": identifier, "x": 4, "y": 5})

    def test_transfer_carries_facts_to_an_ordinary_vessel_and_preserves_identity(self):
        definition = self.material()
        source, target = self.vessel("source"), self.vessel("target")
        bind_material(source, definition)
        source["matter"].append({"id": "source:mix", "material": definition.id, "quantity": 4,
                                "slot": "contained", "role": "substance", "burning": False})
        registry = registry_for([source, target])
        frame = Frame(registry, {"source": source, "target": target}, bind_material=bind_material)
        frame.apply([Transfer("source", "source:mix", "target", 2)])
        received = frame.objects["target"]
        self.assertEqual(received["state"]["program"]["materials"], {definition.id: definition.snapshot()})
        self.assertEqual(registry_for(received).materials[definition.id], definition)
        self.assertEqual(matter.quantity(received, definition.id), 2)
        self.assertEqual(matter.quantity(frame.objects["source"], definition.id), 2)
        self.assertTrue(frame.verify_balance())
        self.assertNotIn("program", target["state"])

    def test_snapshot_defaults_and_conflicts_match_on_loading_and_binding(self):
        definition = self.material()
        target = self.vessel("target")
        old_snapshot = definition.snapshot()
        old_snapshot.pop("piece_quantity")
        target["state"]["program"] = {"materials": {definition.id: old_snapshot}}
        self.assertEqual(registry_for(target).materials[definition.id], definition)
        bind_material(target, definition)
        self.assertEqual(target["state"]["program"]["materials"][definition.id], old_snapshot)
        conflict = self.material(load=2)
        before = deepcopy(target)
        with self.assertRaises(ValueError):
            bind_material(target, conflict)
        self.assertEqual(target, before)
        other = self.vessel("other")
        bind_material(other, conflict)
        with self.assertRaises(ValueError):
            registry_for([target, other])

    def test_snapshot_codec_rejects_unbounded_or_executable_metadata(self):
        definition = self.material()
        for change in ({"name": "材料\n"}, {"load_per_u": True}, {"piece_quantity": 10001},
                       {"combustible": 1}, {"liquid": "yes"}, {"rules": []}):
            with self.subTest(change=change):
                snapshot = {**definition.snapshot(), **change}
                with self.assertRaises(ValueError):
                    MaterialDef.from_snapshot(definition.id, snapshot)

    def test_grove_budget_and_identity_reject_before_binding_new_metadata(self):
        target = self.vessel("target")
        for index in range(4):
            bind_material(target, self.material("mix_" + str(index)))
        before = deepcopy(target)
        with self.assertRaises(ValueError):
            bind_material(target, self.material("fifth"))
        self.assertEqual(target, before)
        bind_material(target, self.material("mix_0"))
        self.assertEqual(target, before)
        ordinary = self.vessel("ordinary")
        with self.assertRaises(ValueError):
            bind_material(ordinary, MaterialDef("iron", "铁", 3, 4))
        self.assertNotIn("program", ordinary["state"])

    def test_frame_uses_its_material_table_without_grove_metadata(self):
        registry = Registry("other-world", ())
        definition = MaterialDef("metal", "金属", 3, 4)
        registry.register_material(definition)
        source = {"id": "source", "state": {}, "components": {}, "matter": [
            {"id": "source:metal", "material": "metal", "quantity": 4, "slot": "surface", "role": "substance", "burning": False}]}
        target = {"id": "target", "state": {}, "components": {}, "matter": []}
        frame = Frame(registry, {"source": source, "target": target})
        frame.apply([Transfer("source", "source:metal", "target", 2, "surface")])
        self.assertNotIn("program", frame.objects["target"]["state"])
        self.assertEqual(matter.quantity(frame.objects["target"], "metal"), 2)
        self.assertTrue(frame.verify_balance())

    def test_compilation_projection_and_planning_share_the_explicit_material_catalog(self):
        materials = {"metal": MaterialDef("metal", "金属", 3, 40)}
        raw = {"key": "metal_process", "name": "金属加工", "version": 1,
               "materials": {"product": {"name": "产物", "feedstock": "metal", "load_per_u": 3,
                                          "combustible": False, "liquid": False}},
               "types": {"vessel": {"name": "加工器皿", "body_material": "metal", "cost": {"metal": 1}}}}
        compiled = compile_program(raw, materials=materials, body_materials=("metal",), max_custom_materials=1)
        semantics = program_material_semantics(compiled, "program_metal", materials=materials)
        self.assertEqual(semantics["materials"]["metal"], {"kind": "builtin", "material_id": "metal"})
        self.assertEqual(semantics["materials"]["product"]["material_id"], "program_metal#product")
        physics = {**materials, **compiled["materials"]}
        reaction = {"op": "react", "inputs": {"metal": 1}, "outputs": {"product": 1}}
        self.assertEqual(plan_reaction(reaction, {"metal": 2}, physics, 4)["produce"], {"product": 1})
        with self.assertRaises(ValueError):
            plan_reaction({"op": "react", "inputs": {"wood": 1}, "outputs": {"product": 1}},
                          {"wood": 1}, physics, 4)
        changed = deepcopy(raw)
        changed["materials"]["product"]["load_per_u"] = 1
        with self.assertRaises(ValueError):
            compile_program(changed, materials=materials, body_materials=("metal",), max_custom_materials=1)


class MaterialConsequenceTests(unittest.TestCase):
    """Known admitted compositions; these are not the independent holdout.

    Frames represent logical physical steps, never elapsed play or user rounds.
    """

    def object(self, identifier, material, construction="bundle", **extra):
        spec = {"material": material, "construction": construction, "size": 1}
        if "lining" in extra:
            spec["lining"] = extra.pop("lining")
        value = {**composition_definition(spec), "id": identifier, "x": 4, "y": 5, **extra}
        return model.normalize_entity(value)

    def test_water_moves_from_actual_source_and_changes_supported_load(self):
        source = self.object("source", "stone", "vessel")
        source["matter"].append({"id": "source:water", "material": "water", "quantity": 12,
                                "slot": "contained", "role": "substance", "burning": False})
        target = self.object("target", "wood", "vessel")
        frame = Frame(REGISTRY, {"source": source, "target": target})
        before = matter.load_summary(REGISTRY, target, frame.objects)
        frame.apply([Transfer("source", "source:water", "target", 6)])
        after = matter.load_summary(REGISTRY, frame.objects["target"], frame.objects)
        self.assertEqual(matter.quantity(frame.objects["source"], "water"), 6)
        self.assertEqual(matter.quantity(frame.objects["target"], "water"), 6)
        self.assertEqual(after["supported"]-before["supported"], 12)
        self.assertTrue(frame.verify_balance())
        with self.assertRaises(ValueError):
            frame.apply([Transfer("source", "source:water", "target", 7)])
        self.assertEqual(matter.quantity(frame.objects["source"], "water"), 6)

    def test_burning_lining_does_not_destroy_stone_body(self):
        vessel = self.object("vessel", "stone", "vessel", lining="cloth")
        lining = next(lot for lot in vessel["matter"] if lot["role"] == "lining")
        lining["burning"] = True
        stone = matter.quantity(vessel, "stone")
        frame = Frame(REGISTRY, {"vessel": vessel})
        # Eight fuel settlements, including tick zero, consume the real lining.
        for step in range(7 * COMBUSTION_CADENCE_TICKS + 1):
            frame = Frame(REGISTRY, frame.objects, tick=step).run()
            self.assertTrue(frame.verify_balance())
        result = frame.objects["vessel"]
        self.assertEqual(matter.quantity(result, "stone"), stone)
        self.assertEqual(matter.quantity(result, "cloth"), 0)
        self.assertEqual(matter.component_capacity(result, "absorbent"), 0)
        self.assertEqual(result["lifecycle"], "intact")
        self.assertEqual(result["components"]["structure"]["integrity"], 100)

    def test_inner_failure_keeps_contents_inside_intact_closed_outer(self):
        outer = self.object("outer", "stone", "vessel")
        outer["components"]["container"]["open"] = False
        outer["components"]["structure"]["support_limit"] = 1000
        inner = self.object("inner", "wood", "vessel",
                            physical_parent={"id": "outer", "relation": "contained"})
        inner["components"]["structure"]["integrity"] = 1
        child = self.object("child", "stone", physical_parent={"id": "inner", "relation": "contained"})
        outer["state"]["contents"], inner["state"]["contents"] = ["inner"], ["child"]
        frame = Frame(REGISTRY, {"outer": outer, "inner": inner, "child": child}).run()
        self.assertEqual(frame.objects["outer"]["lifecycle"], "intact")
        self.assertEqual(frame.objects["inner"]["lifecycle"], "broken")
        released = frame.objects["child"]
        self.assertIsNone(released["x"])
        self.assertIsNone(released["y"])
        ancestors, current = set(), released
        while current.get("physical_parent"):
            identifier = current["physical_parent"]["id"]
            self.assertNotIn(identifier, ancestors)
            ancestors.add(identifier)
            current = frame.objects[identifier]
        self.assertIn("outer", ancestors)
        self.assertFalse(matter.exposed(released, released["matter"][0], frame.objects))
        self.assertTrue(frame.verify_balance())

    def test_broken_wood_retains_material_reactions_and_same_identity(self):
        vessel = self.object("vessel", "wood", "vessel")
        vessel["components"]["structure"]["integrity"] = 1
        vessel["matter"][0]["burning"] = True
        first = Frame(REGISTRY, {"vessel": vessel}).run()
        self.assertEqual(first.objects["vessel"]["lifecycle"], "broken")
        remaining = matter.quantity(first.objects["vessel"], "wood")
        self.assertGreater(remaining, 0)
        second = first
        for step in range(1, COMBUSTION_CADENCE_TICKS):
            second = Frame(REGISTRY, second.objects, tick=step).run()
            self.assertEqual(matter.quantity(second.objects["vessel"], "wood"), remaining)
            self.assertTrue(second.verify_balance())
        second = Frame(REGISTRY, second.objects, tick=COMBUSTION_CADENCE_TICKS).run()
        self.assertLess(matter.quantity(second.objects["vessel"], "wood"), remaining)
        self.assertEqual(set(second.objects), {"vessel"})
        self.assertTrue(first.verify_balance())
        self.assertTrue(second.verify_balance())

    def test_material_loss_uses_declared_structural_integrity_scale(self):
        vessel = self.object("vessel", "wood", "vessel")
        vessel["components"]["structure"].update(max_integrity=40, integrity=40)
        vessel["matter"][0]["burning"] = True
        frame = Frame(REGISTRY, {"vessel": vessel}).run()
        # Two of forty structural u burned: 2/40 of the declared 40 points,
        # rather than applying a fixed hundred-point scale to every structure.
        self.assertEqual(matter.quantity(frame.objects["vessel"], "wood"), 38)
        self.assertEqual(frame.objects["vessel"]["components"]["structure"]["integrity"], 38)
        self.assertTrue(frame.verify_balance())


class SharedMatterBoundaryTests(unittest.TestCase):
    def vessel(self, identifier):
        return model.normalize_entity({**composition_definition({"material": "stone", "construction": "vessel"}),
                                       "id": identifier, "x": 4, "y": 5})

    def test_preview_allocation_and_final_validation_share_mixture_room(self):
        from agent_world.errors import RuleViolation
        from agent_world.worlds.shared_grove.definitions import mixture_capacity
        source, target = self.vessel("source"), self.vessel("target")
        target["state"]["program"] = {"mixture_capacity": 5}
        matter.add_lot(target, "wood", 3, "contained")
        water = matter.add_lot(source, "water", 4, "contained")
        registry = registry_for([source, target])
        self.assertEqual(model.water_transfer_room(target), 2)
        frame = Frame(registry, {"source": source, "target": target}, mixture_capacity=mixture_capacity)
        with self.assertRaises(ValueError):
            frame.apply([Transfer("source", water["id"], "target", 3)])
        self.assertEqual(frame.objects["target"]["matter"], target["matter"])
        frame.apply([Transfer("source", water["id"], "target", 3)], allocate=True)
        self.assertEqual(matter.quantity(frame.objects["target"], slot="contained"), 5)
        self.assertTrue(frame.verify_balance())
        overfull = deepcopy(frame.objects["target"])
        matter.add_lot(overfull, "wood", 1, "contained")
        with self.assertRaises(RuleViolation):
            model.normalize_entity(overfull)

    def test_reaction_inputs_exclude_burning_escrow_and_same_phase_products(self):
        from agent_world.worlds.shared_grove.definitions import reaction_available_quantities
        entity = self.vessel("reactor")
        entity["state"]["program"] = {"definition_ref": "program_x", "contract": {
            "material": "wood", "delivered": 2, "released": 0}}
        matter.add_lot(entity, "wood", 5, "contained")
        matter.add_lot(entity, "cloth", 4, "contained")["burning"] = True
        self.assertEqual(reaction_available_quantities(entity), {"wood": 3})
        self.assertEqual(reaction_available_quantities(entity, supply={"wood": 4, "cloth": 4}),
                         {"wood": 2, "cloth": 0})
        before = deepcopy(entity)
        with self.assertRaises(ValueError):
            matter.take_material(entity, "cloth", 1)
        self.assertEqual(entity, before)


if __name__ == "__main__":
    unittest.main()
