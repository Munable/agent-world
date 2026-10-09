"""Trusted rule-registration contracts; execution evidence is a separate stage."""
import unittest

from agent_world.simulation.effects import Transfer
from agent_world.simulation.frame import Frame
from agent_world.simulation import matter
from agent_world.simulation.registry import Registry, RuleSpec
from agent_world.worlds.shared_grove.definitions import COMBUSTION_CADENCE_TICKS, REGISTRY, composition_definition
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


if __name__ == "__main__":
    unittest.main()
