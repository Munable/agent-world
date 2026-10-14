"""Draft grammar matches compiler syntax without replacing semantic admission."""
from copy import deepcopy
import hashlib
import json
import unittest

from jsonschema import Draft202012Validator

from agent_world.simulation import programs as p
from agent_world.simulation.program_schema import (MAX_PROGRAM_SCHEMA_BYTES, PROGRAM_SCHEMA_NODES,
    embed_program_schema, program_schema, program_schema_node_for_path)
from agent_world.worlds.shared_grove import creative
from agent_world.worlds.shared_grove.definitions import BODY_MATERIALS, MAX_CUSTOM_MATERIALS, REGISTRY
import test_grove_creative as fixtures


def context():
    return {"materials": REGISTRY.materials, "body_materials": BODY_MATERIALS,
        "max_custom_materials": MAX_CUSTOM_MATERIALS,
        "component_catalog": {name: REGISTRY.components[name] for name in creative.PROGRAM_COMPONENTS}}


def basic_definition():
    return {"key": "draft_contract", "name": "声明", "version": 1,
        "types": {"body": {"name": "主体", "body_material": "wood", "cost": {"wood": 1}}}}


class ProgramSchemaTests(unittest.TestCase):
    def validator(self, node="definition", **overrides):
        schema = program_schema(node=node, **{**context(), **overrides})
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_every_node_has_self_contained_local_refs_and_bounded_output(self):
        for node in PROGRAM_SCHEMA_NODES:
            with self.subTest(node=node):
                schema = program_schema(node=node, **context())
                Draft202012Validator.check_schema(schema)
                self.assertLessEqual(len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()), MAX_PROGRAM_SCHEMA_BYTES)
                def check(value):
                    if isinstance(value, dict):
                        if "$ref" in value:
                            self.assertTrue(value["$ref"].startswith("#/$defs/"))
                            self.assertIn(value["$ref"].removeprefix("#/$defs/"), schema["$defs"])
                        for child in value.values():
                            check(child)
                    elif isinstance(value, list):
                        for child in value:
                            check(child)
                check(schema)
        local = program_schema(node="material", **context())
        definition_ref = program_schema(**context())["$ref"].removeprefix("#/$defs/")
        self.assertNotIn(definition_ref, local["$defs"])
        self.assertLess(len(json.dumps(local).encode()), 8 * 1024)

    def test_actual_compiler_accepted_packages_and_canonical_results_fit_grammar(self):
        validator = self.validator()
        raw = basic_definition()
        raw["types"]["body"].update(components={"absorbent": {"capacity": 10}, "combustion": {}},
            fields={"value": {"type": "integer", "default": 0, "min": 0, "max": 2}},
            exports=["value"], actions=[{"id": "mark", "label": "标记", "effects": [
                {"op": "set", "field": "value", "value": 1}, {"op": "emit", "name": "authored_signal"}]}],
            presentation={"color": "#abcdef", "shape": "part", "bindings": {
                "badge": {"field": "value"}, "pose": {"field": "value", "cases": [{"value": 0, "display": "folded"}]}}})
        for proposed in (basic_definition(), raw, fixtures.package(), fixtures.event_counter_package()):
            with self.subTest(proposed=proposed["key"]):
                canonical = p.compile_program(proposed, **context())
                self.assertTrue(validator.is_valid(proposed), list(validator.iter_errors(proposed)))
                self.assertTrue(validator.is_valid(canonical), list(validator.iter_errors(canonical)))

    def test_material_uses_are_actual_boolean_compiler_flags(self):
        node = self.validator("material")
        material = {"name": "注册材料", "feedstock": "wood", "load_per_u": REGISTRY.materials["wood"].load_per_u,
            "combustible": True, "liquid": False,
            "uses": {"hearth_fuel": True, "mechanical_fuel": False}}
        self.assertTrue(node.is_valid(material))
        raw = basic_definition()
        raw["materials"] = {"local_fuel": material}
        p.compile_program(raw, **context())
        self.assertFalse(node.is_valid({**material, "uses": {"fuel": {"energy_per_u": 10}}}))
        self.assertFalse(node.is_valid({**material, "uses": {"mechanical_fuel": 1}}))

    def test_recursive_expression_shape_depth_and_reference_catalog(self):
        node = self.validator("expression")
        atoms = [True, 1, "", {"physical": "wetness"}, {"mechanical": "energy"},
            {"event": "kind"}, {"contract": "status"}, {"quantity": "wood"},
            {"concentration": "wood"}, {"field": "declared_value"}, {"param": "chosen_value"},
            {"object": {"id": "declared-source", "field": "physical.burning"}},
            {"object": {"port": "source", "field": "authored.value"}}]
        for atom in atoms:
            with self.subTest(atom=atom):
                self.assertTrue(node.is_valid(atom))
        for invalid in ({"literal": 1}, {"physical": "invented"}, {"event": "world_identity"},
                {"op": "add", "args": [1, 2]}, {"field": "value", "param": "other"},
                {"object": {"id": "a", "port": "b", "field": "intact"}},
                {"object": {"id": "a", "field": "authored.value"}}, {"op": "all", "args": []}):
            self.assertFalse(node.is_valid(invalid), invalid)
        expression = True
        for _ in range(p.MAX_EXPRESSION_DEPTH - 1):
            expression = {"op": "not", "value": expression}
        self.assertTrue(node.is_valid(expression))
        self.assertFalse(node.is_valid({"op": "not", "value": expression}))

    def test_expression_depths_share_operator_forms_and_single_fact_catalogs(self):
        schema = program_schema(**context())
        definitions = schema["$defs"]
        self.assertEqual(definitions["p_bin"]["properties"]["op"]["enum"], sorted(p._BINARY))
        for depth in range(1, p.MAX_EXPRESSION_DEPTH):
            forms = definitions["p_e" + str(depth)]["anyOf"][1:]
            self.assertEqual([form["allOf"][0]["$ref"] for form in forms], [
                "#/$defs/p_bin", "#/$defs/p_log", "#/$defs/p_not"])
        serialized = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(serialized.count(json.dumps("physical.wetness")), 1)

    def test_component_schema_uses_registry_bounds_and_forbids_authored_supports(self):
        node = self.validator("components")
        self.assertTrue(node.is_valid({"absorbent": {}, "structure": {"integrity": 100},
            "attachment": {"kind": "", "fits": ""}}))
        for invalid in ({"absorbent": {"capacity": 201}}, {"structure": {"integrity": True}},
                {"absorbent": {"supports": {}}}, {"undeclared": {}}, {"combustion": {"spread": 1}}):
            self.assertFalse(node.is_valid(invalid), invalid)

    def test_type_shape_cost_choice_and_module_bounds(self):
        node = self.validator("type")
        body = basic_definition()["types"]["body"]
        self.assertTrue(node.is_valid(body))
        self.assertFalse(node.is_valid({**body, "cost_u": {"wood": 1}}))
        missing = deepcopy(body)
        missing.pop("cost")
        self.assertFalse(node.is_valid(missing))
        for update in ({"container_capacity": 201}, {"mechanical": {"role": "source", "speed": 11}},
                {"recorder": {"radius": 9, "window_ticks": 1}},
                {"contract": {"material": "wood", "quantity": 101, "duration_ticks": 1}},
                {"actions": [{"id": "accept", "label": "伪造", "effects": []}]}):
            self.assertFalse(node.is_valid({**body, **update}), update)

    def test_contact_predicates_retain_the_restricted_reference_scope(self):
        node = self.validator("contact_reaction")
        rule = {"id": "contact_rule", "activation": "installed", "when": {
            "op": "gt", "left": {"quantity": "wood"}, "right": 0},
            "reaction": {"op": "react", "inputs": {"wood": 1}, "outputs": {"wood": 1}}}
        self.assertTrue(node.is_valid(rule))
        for reference in ({"event": "passable"}, {"physical": "burning"}, {"field": "status"}):
            self.assertFalse(node.is_valid({**rule, "when": reference}))
        self.assertFalse(node.is_valid({**rule, "reaction": {**rule["reaction"], "heat": 1}}))

    def test_semantic_checks_remain_compiler_authority(self):
        raw = basic_definition()
        raw["types"]["body"].update(fields={"level": {"type": "integer", "default": 2, "min": 0, "max": 1}})
        self.assertTrue(self.validator().is_valid(raw))
        with self.assertRaisesRegex(ValueError, "default"):
            p.compile_program(raw, **context())
        constraints = program_schema(**context())["x-program-compiler"]["constraints"]
        self.assertEqual(constraints["expression_max_total_nodes"], p.MAX_EXPRESSION_NODES)
        self.assertIn("reaction_mass_balance", constraints["semantic_checks"])

    def test_envelope_embedding_resolves_refs_at_root_without_mutation(self):
        outer = {"type": "object", "properties": {"definition": {"type": "object"},
            "source_definition_ref": {"type": "string"}}, "required": ["definition"], "additionalProperties": False}
        before = deepcopy(outer)
        embedded = embed_program_schema(outer, **context())
        Draft202012Validator.check_schema(embedded)
        self.assertTrue(Draft202012Validator(embedded).is_valid({"definition": basic_definition()}))
        self.assertNotIn("$defs", embedded["properties"]["definition"])
        self.assertEqual(outer, before)
        self.assertFalse(Draft202012Validator(embedded).is_valid({"definition": {"literal": 1}}))

    def test_diagnostic_paths_choose_a_self_contained_context_appropriate_node(self):
        cases = {"program": "definition", "program.types": "types", "program.types.sign": "type",
            "program.types.sign.rules[0]": "rule", "program.types.sign.actions[0].parameters.amount": "field",
            "program.types.sign.rules[0].when.args[0].right": "expression",
            "program.types.sign.rules[0].effects[0].value.literal": "expression",
            "program.types.sign.rules[0].effects[0].inputs.wood": "stoichiometry",
            "program.types.sign.rules[0].effects[0].enabled": "condition",
            "program.types.sign.ports.sensor.source.type_id": "source",
            "program.contact_reactions[0].when.left": "contact_reaction",
            "program.contact_reactions[0].reaction.heat": "contact_reaction",
            "program.materials.local.uses": "material"}
        for path, node in cases.items():
            self.assertEqual(program_schema_node_for_path(path), node)
        self.assertIsNone(program_schema_node_for_path("external.error"))

    def test_grammar_generation_preserves_raw_and_canonical_bytes(self):
        raw = basic_definition()
        expected = {"key": raw["key"], "name": raw["name"], "version": 1, "materials": {},
            "types": {"body": {"name": "主体", "body_material": "wood", "fields": {}, "rules": [], "actions": [],
                "cost": {"wood": 1, "stone": 0, "cloth": 0}}}}
        before = deepcopy(raw)
        canonical = p.compile_program(raw, **context())
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        program_schema(**context())
        self.assertEqual(raw, before)
        self.assertEqual(canonical, expected)
        after = p.compile_program(raw, **context())
        self.assertEqual(hashlib.sha256(encoded).digest(), hashlib.sha256(
            json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).digest())

    def test_empty_trusted_fact_catalog_still_produces_a_valid_schema(self):
        schema = program_schema(node="expression", **context(), fact_catalog={})
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"quantity": "wood"}))
        self.assertFalse(validator.is_valid({"physical": "wetness"}))


if __name__ == "__main__":
    unittest.main()
