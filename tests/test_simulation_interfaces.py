"""Declaration contracts for fixed-source exports and derived execution plans."""
from copy import deepcopy
import unittest

from agent_world.simulation.execution import execution_plan
from agent_world.simulation.facts import FactUnavailable
from agent_world.simulation.programs import compile_program, evaluate, exported_interface
from agent_world.simulation.registry import MaterialDef


MATERIALS = {key: MaterialDef(key, key, load, 4, liquid=key == "water")
             for key, load in (("wood", 1), ("stone", 3), ("cloth", 1), ("water", 2))}


def definition(key="plain"):
    return {"key": key, "name": key, "version": 1,
            "types": {"main": {"name": "main", "body_material": "wood", "cost": {"wood": 1}}}}


def compile_definition(raw, **kwargs):
    return compile_program(raw, materials=MATERIALS, body_materials=("wood", "stone", "cloth"),
                           max_custom_materials=4, **kwargs)


def consumer():
    raw = definition("consumer")
    raw["types"]["main"].update(
        fields={"reading": {"type": "integer", "default": 0, "min": 0, "max": 10}},
        ports={"sensor": {"source": {"definition_ref": "program_source", "type_id": "main"},
                          "fields": ["authored.level"]}},
        rules=[{"id": "read", "phase": "after", "when": True,
                "effects": [{"op": "set", "field": "reading",
                             "value": {"object": {"port": "sensor", "field": "authored.level"}}}]}])
    return raw


class ExportedInterfaceContracts(unittest.TestCase):
    def test_old_shape_keeps_exact_canonical_data_and_no_new_defaults(self):
        canonical = compile_definition(definition())
        expected = {"key": "plain", "name": "plain", "version": 1, "materials": {},
                    "types": {"main": {"name": "main", "body_material": "wood", "fields": {},
                                       "rules": [], "actions": [],
                                       "cost": {"wood": 1, "stone": 0, "cloth": 0}}}}
        self.assertEqual(canonical, expected)
        original = deepcopy(canonical)
        plan = execution_plan(canonical)
        self.assertEqual(plan["semantics"], "grove.program.v1")
        self.assertEqual(canonical, original)
        self.assertTrue(plan["types"]["main"]["sleep_safe"])

    def test_exports_only_declared_fields_and_carries_types_and_ranges(self):
        raw = definition()
        raw["types"]["main"].update(fields={
            "level": {"type": "integer", "default": 2, "min": 0, "max": 10},
            "ready": {"type": "boolean", "default": False},
            "internal": {"type": "integer", "default": 3}}, exports=["level", "ready"])
        spec = compile_definition(raw)["types"]["main"]
        self.assertEqual(exported_interface(spec), {
            "authored.level": {"type": "integer", "min": 0, "max": 10},
            "authored.ready": {"type": "boolean"}})
        for names in (["owner"], ["level", "level"], list(str(i) for i in range(9)), [{}]):
            invalid = deepcopy(raw)
            invalid["types"]["main"]["exports"] = names
            with self.subTest(names=names), self.assertRaises(ValueError):
                compile_definition(invalid)

    def test_pinned_source_resolves_authored_types_without_global_catalog(self):
        interfaces = {"program_source": {"main": {
            "authored.level": {"type": "integer", "min": 0, "max": 10}}}}
        raw = consumer()
        canonical = compile_definition(raw, imported_interfaces=interfaces)
        self.assertEqual(canonical["types"]["main"]["ports"]["sensor"], raw["types"]["main"]["ports"]["sensor"])
        for catalog in (None, {}, {"program_source": {"main": {}}},
                        {"program_source": {"main": {"authored.level": {"type": "boolean"}}}}):
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                compile_definition(raw, imported_interfaces=catalog)
        missing_source = deepcopy(raw)
        del missing_source["types"]["main"]["ports"]["sensor"]["source"]
        with self.assertRaises(ValueError):
            compile_definition(missing_source, imported_interfaces=interfaces)

    def test_same_named_field_can_have_different_types_at_different_ports(self):
        raw = consumer()
        spec = raw["types"]["main"]
        spec["fields"]["ready"] = {"type": "boolean", "default": False}
        spec["ports"]["switch"] = {"source": {"definition_ref": "program_switch", "type_id": "main"},
                                    "fields": ["authored.level"]}
        spec["rules"][0]["effects"].append({"op": "set", "field": "ready",
            "value": {"object": {"port": "switch", "field": "authored.level"}}})
        compile_definition(raw, imported_interfaces={
            "program_source": {"main": {"authored.level": {"type": "integer", "min": 0, "max": 10}}},
            "program_switch": {"main": {"authored.level": {"type": "boolean"}}}})

    def test_evaluation_requires_real_exported_values_and_preserves_unknown(self):
        expression = {"object": {"port": "sensor", "field": "authored.level"}}
        context = {"ports": {"sensor": "real_source"},
                   "objects": {"real_source": {"authored.level": 4}},
                   "port_interfaces": {"sensor": {"authored.level": {"type": "integer", "min": 0, "max": 10}}}}
        self.assertEqual(evaluate(expression, context), 4)
        for field in ("objects", "port_interfaces", "ports"):
            missing = deepcopy(context)
            missing[field] = {}
            with self.subTest(field=field), self.assertRaises(FactUnavailable):
                evaluate(expression, missing)
        for value in (True, 11, "4"):
            invalid = deepcopy(context)
            invalid["objects"]["real_source"]["authored.level"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                evaluate(expression, invalid)
        with self.assertRaises(ValueError):
            evaluate({"object": {"id": "real_source", "field": "authored.level"}}, context)

    def test_installed_contacts_allow_existing_inputs_but_not_unbounded_effects(self):
        raw = definition()
        raw["contact_reactions"] = [{"id": "mix", "activation": "installed", "when": True,
            "reaction": {"op": "react", "inputs": {"wood": 1}, "outputs": {"cloth": 1}}}]
        result = compile_definition(raw)
        self.assertEqual(result["contact_reactions"][0]["activation"], "installed")
        ordinary = deepcopy(raw)
        del ordinary["contact_reactions"][0]["activation"]
        with self.assertRaisesRegex(ValueError, "locally declared"):
            compile_definition(ordinary)
        for update in ({"heat": 1}, {"outputs": {"cloth": 2}}, {"rate": 1000}):
            invalid = deepcopy(raw)
            invalid["contact_reactions"][0]["reaction"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                compile_definition(invalid)

    def test_installed_contacts_pin_imported_materials_and_keep_material_default(self):
        raw = definition()
        material = {"name": "previous", "feedstock": "wood", "load_per_u": 1,
                    "combustible": False, "liquid": False}
        raw["material_imports"] = {"previous": {"definition_ref": "program_previous", "symbol": "original"}}
        raw["contact_reactions"] = [{"id": "combine", "activation": "installed", "when": True,
            "reaction": {"op": "react", "inputs": {"previous": 1, "wood": 1}, "outputs": {"cloth": 2}}}]
        compiled = compile_definition(raw, imported_materials={"previous": material})
        self.assertEqual(compiled["materials"], {})
        self.assertEqual(compiled["material_imports"], raw["material_imports"])
        with self.assertRaises(ValueError):
            compile_definition(raw)
        ordinary = definition()
        ordinary["materials"] = {"starter": material}
        ordinary["contact_reactions"] = [{"id": "carried", "when": True,
            "reaction": {"op": "react", "inputs": {"starter": 1}, "outputs": {"cloth": 1}}}]
        self.assertNotIn("activation", compile_definition(ordinary)["contact_reactions"][0])


class ExecutionPlanContracts(unittest.TestCase):
    def test_solver_and_reaction_implicit_fields_are_dependencies(self):
        raw = definition()
        raw["types"]["main"].update(
            fields={"temperature": {"type": "integer", "default": 0},
                    "load": {"type": "integer", "default": 2, "min": 0, "max": 100},
                    "speed": {"type": "integer", "default": 1, "min": 1, "max": 10}},
            mechanical={"role": "source", "load_field": "load", "speed_field": "speed"},
            container_capacity=10,
            rules=[{"id": "heat", "phase": "reaction", "when": True,
                    "effects": [{"op": "react", "inputs": {"wood": 1}, "outputs": {"cloth": 1}, "heat": 1}]}])
        plan = execution_plan(compile_definition(raw))["types"]["main"]
        self.assertEqual(plan["reads"]["fields"], ["load", "speed", "temperature"])
        self.assertEqual(plan["writes"]["fields"], ["temperature"])
        self.assertTrue(plan["continuous"])

    def test_external_dependencies_and_self_writes_are_derived_not_predicted(self):
        interfaces = {"program_source": {"main": {"authored.level": {"type": "integer", "min": 0, "max": 10}}}}
        canonical = compile_definition(consumer(), imported_interfaces=interfaces)
        original = deepcopy(canonical)
        plan = execution_plan(canonical, type_id="main")["types"]["main"]
        self.assertEqual(plan["reads"]["ports"], {"sensor": ["authored.level"]})
        self.assertEqual(plan["writes"]["fields"], ["reading"])
        self.assertTrue(plan["sleep_safe"])
        self.assertFalse(plan["self_feedback"])
        self.assertEqual(canonical, original)

    def test_feedback_emit_and_physics_cannot_be_silently_slept(self):
        for value, effects in (
            ({"field": "counter"}, [{"op": "set", "field": "counter", "value": {"op": "add", "left": {"field": "counter"}, "right": 1}}]),
            (True, [{"op": "emit", "name": "signal"}]),
            ({"physical": "wetness"}, [{"op": "set", "field": "counter", "value": {"physical": "wetness"}}]),
        ):
            raw = definition()
            raw["types"]["main"].update(fields={"counter": {"type": "integer", "default": 0}},
                rules=[{"id": "running", "phase": "after", "when": True, "effects": effects}])
            plan = execution_plan(compile_definition(raw))["types"]["main"]
            with self.subTest(value=value):
                self.assertTrue(plan["continuous"])
                self.assertFalse(plan["sleep_safe"])

    def test_event_reads_require_event_wakes_and_actions_do_not_create_polling(self):
        raw = definition()
        raw["types"]["main"].update(fields={"ready": {"type": "boolean", "default": False}},
            rules=[{"id": "notice", "phase": "after", "when": {"op": "eq", "left": {"event": "action"}, "right": "move"},
                    "effects": [{"op": "set", "field": "ready", "value": True}]}],
            actions=[{"id": "increment", "label": "mark", "effects": [{"op": "set", "field": "ready", "value": {"field": "ready"}}]}])
        plan = execution_plan(compile_definition(raw))["types"]["main"]
        self.assertTrue(plan["event_driven"])
        self.assertTrue(plan["sleep_safe"])
        self.assertFalse(plan["self_feedback"])

    def test_literal_object_reads_and_solver_liveness_are_not_dropped(self):
        raw = definition()
        spec = raw["types"]["main"]
        spec.update(mechanical={"role": "source"}, fields={"ready": {"type": "boolean", "default": False}},
            rules=[{"id": "gate", "phase": "after", "when": {"object": {"id": "gate", "field": "intact"}},
                    "effects": [{"op": "drive", "enabled": True}]}])
        plan = execution_plan(compile_definition(raw))["types"]["main"]
        self.assertEqual(plan["reads"]["objects"], {"gate": ["intact"]})
        self.assertEqual(plan["literal_objects"], ["gate"])
        self.assertEqual(plan["writes"]["mechanical"], ["enabled"])
        self.assertTrue(plan["continuous"])


if __name__ == "__main__":
    unittest.main()
