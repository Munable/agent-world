"""Typed dependency advice and immutable publication; no inferred achievements."""
from copy import deepcopy
import json
import unittest

from agent_world.simulation.program_diagnostics import MAX_DIAGNOSTIC_BYTES, program_diagnostics
from agent_world.simulation.programs import compile_program
from agent_world.worlds.shared_grove import creative
from agent_world.worlds.shared_grove.definitions import BODY_MATERIALS, MAX_CUSTOM_MATERIALS, REGISTRY
import test_grove_creative as fixtures


def definition(condition=True):
    return {"key": "dependency_advice", "name": "声明物件", "version": 1, "types": {"body": {
        "name": "任意名称", "body_material": "wood", "cost_u": {"wood": 1},
        "fields": {"signal": {"type": "boolean", "default": False}},
        "rules": [{"id": "observe", "phase": "after", "when": condition,
                   "effects": [{"op": "set", "field": "signal", "value": True}]}]}}}


def compiled(raw, **kwargs):
    return compile_program(raw, materials=REGISTRY.materials, body_materials=BODY_MATERIALS,
        max_custom_materials=MAX_CUSTOM_MATERIALS,
        component_catalog={name: REGISTRY.components[name] for name in creative.PROGRAM_COMPONENTS}, **kwargs)


def comparison(reference, value=0, op="gt"):
    return {"op": op, "left": reference, "right": value}


class ProgramDependencyTests(unittest.TestCase):
    def rows(self, raw, **kwargs):
        result = creative._definition_diagnostics(compiled(raw, **kwargs))
        self.assertFalse(result["dependency_analysis"]["execution_proven"])
        self.assertFalse(result["dependency_analysis"]["world_facts_checked"])
        return result["dependency_analysis"]["items"]

    def test_positive_wetness_requires_effective_support_but_zero_is_legitimate(self):
        raw = definition(comparison({"physical": "wetness"}))
        missing = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual((missing["code"], missing["status"]), ("cannot_change_without_support", "missing"))
        self.assertEqual(missing["initial"], {"scope": "declared_initial", "known": True, "value": 0})
        self.assertEqual(missing["requirements"][0]["actual"], 0)
        self.assertIn({"function_id": "grove.rule_catalog", "arguments": {"topic": "components"}}, missing["read_requests"])
        self.assertIn("component_schema.absorbent", missing["remediation"]["paths"])
        raw["types"]["body"]["rules"][0]["when"] = comparison({"physical": "wetness"}, op="eq")
        zero = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual((zero["status"], zero["severity"]), ("provided", "info"))
        raw["types"]["body"]["components"] = {"absorbent": {"capacity": 8}}
        raw["types"]["body"]["rules"][0]["when"] = comparison({"physical": "wetness"})
        supported = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual(supported["status"], "provided")
        self.assertTrue(supported["requirements"][0]["satisfied"])
        self.assertIsNone(supported["requirements"][1]["satisfied"])

    def test_body_material_names_fields_and_prose_do_not_create_capabilities(self):
        raw = definition(comparison({"physical": "wetness"}))
        raw["types"]["body"].update(name="自动吸水燃烧机械", body_material="cloth", cost_u={"cloth": 1})
        raw["types"]["body"]["fields"]["fuel"] = {"type": "integer", "default": 100}
        row = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["initial"]["value"], 0)

    def test_negated_dry_condition_does_not_recommend_absorbent_capability(self):
        raw = definition({"op": "not", "value": comparison({"physical": "wetness"})})
        row = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual((row["status"], row["severity"]), ("provided", "info"))
        self.assertEqual(row["initial"]["value"], 0)
        self.assertEqual(row["remediation"]["strategy"], "inspect_actual_instance")

    def test_or_requires_all_alternatives_to_need_positive_wetness(self):
        wet = comparison({"physical": "wetness"})
        dry_alternative = {"op": "eq", "left": {"field": "signal"}, "right": False}
        raw = definition({"op": "any", "args": [wet, dry_alternative]})
        row = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual((row["status"], row["severity"]), ("provided", "info"))
        raw["types"]["body"]["rules"][0]["when"] = {
            "op": "any", "args": [wet, comparison({"physical": "wetness"}, value=5)]}
        rows = [row for row in self.rows(raw) if row["kind"] == "self_physical"]
        self.assertTrue(all(row["status"] == "missing" for row in rows))
        raw["types"]["body"]["rules"][0]["when"] = {"op": "all", "args": [wet, dry_alternative]}
        row = next(row for row in self.rows(raw) if row["kind"] == "self_physical")
        self.assertEqual(row["status"], "missing")

    def test_arithmetic_nested_comparison_does_not_guess_positive_requirement(self):
        conditions = [{"op": "lt", "left": {"op": "sub", "left": 100,
            "right": {"physical": "wetness"}}, "right": 101},
            {"op": "eq", "left": comparison({"physical": "wetness"}), "right": False}]
        for condition in conditions:
            with self.subTest(condition=condition):
                row = next(row for row in self.rows(definition(condition)) if row["kind"] == "self_physical")
                self.assertEqual((row["status"], row["severity"]), ("provided", "info"))

    def test_surface_water_burning_and_aggregate_buoyancy_are_distinct_dependencies(self):
        raw = definition({"op": "all", "args": [comparison({"physical": "surface_water"}),
            {"physical": "burning"}, {"physical": "can_float"}]})
        rows = {row["reference"]["physical"]: row for row in self.rows(raw) if row["kind"] == "self_physical"}
        for name in ("surface_water", "burning"):
            self.assertNotEqual(rows[name]["status"], "missing")
            self.assertFalse(any(item.get("component") in {"absorbent", "combustion"} for item in rows[name]["requirements"]))
        self.assertEqual(rows["can_float"]["status"], "deferred")
        self.assertFalse(rows["can_float"]["initial"]["known"])

    def test_implicit_structure_and_missing_module_defaults_share_constructor(self):
        raw = definition({"op": "all", "args": [comparison({"physical": "structure.integrity"}),
            comparison({"mechanical": "speed"}, op="eq"), comparison({"contract": "delivered"}, op="eq")]})
        rows = self.rows(raw)
        physical = next(row for row in rows if row["kind"] == "self_physical")
        self.assertEqual(physical["status"], "provided")
        self.assertEqual(physical["initial"]["value"], 100)
        for kind in ("self_mechanical", "self_contract"):
            row = next(row for row in rows if row["kind"] == kind)
            self.assertEqual((row["resolution"], row["initial"]["value"]), ("default_constant", 0))
            self.assertEqual(row["status"], "provided")

    def test_ports_and_fixed_sources_never_infer_consumer_component_failure(self):
        raw = definition({"op": "all", "args": [comparison({"object": {"port": "sensor", "field": "physical.wetness"}}),
            comparison({"object": {"id": "explicit-source", "field": "mechanical.speed"}})]})
        raw["types"]["body"]["ports"] = {"sensor": {"fields": ["physical.wetness"]}}
        rows = [row for row in self.rows(raw) if row["kind"] in {"port_fact", "literal_object_fact"}]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "deferred" and not row["initial"]["known"] for row in rows))
        self.assertEqual([row["unavailable_behavior"] for row in rows], ["fact_unavailable", "evaluation_error"])
        self.assertIn({"function_id": "grove.inspect", "arguments": {"entity_id": "explicit-source"}}, rows[1]["read_requests"])

    def test_pinned_exported_source_has_formal_schema_read_without_binding_claim(self):
        raw = definition(comparison({"object": {"port": "sensor", "field": "authored.level"}}))
        raw["types"]["body"]["ports"] = {"sensor": {"fields": ["authored.level"],
            "source": {"definition_ref": "immutable-source", "type_id": "source"}}}
        rows = self.rows(raw, imported_interfaces={"immutable-source": {"source": {
            "authored.level": {"type": "integer", "min": 0, "max": 10}}}})
        port = next(row for row in rows if row["kind"] == "port_fact")
        self.assertEqual(port["status"], "deferred")
        self.assertIn({"function_id": "grove.definition_read", "arguments": {"definition_ref": "immutable-source"}}, port["read_requests"])

    def test_events_need_actual_journal_input_without_requiring_recorder(self):
        raw = definition({"op": "eq", "left": {"event": "kind"}, "right": "mechanical"})
        event = next(row for row in self.rows(raw) if row["kind"] == "event_context")
        self.assertEqual((event["status"], event["resolution"]), ("deferred", "runtime_event"))
        self.assertFalse(event["initial"]["known"])
        self.assertFalse(any(item.get("component") == "recorder" for item in event["requirements"]))

    def test_imports_and_installed_contact_reactions_require_real_inputs_without_rejection(self):
        raw = definition()
        raw["material_imports"] = {"foreign": {"definition_ref": "immutable-stock", "symbol": "stock"}}
        raw["types"]["body"].update(body_material="foreign", cost_u={"foreign": 1})
        raw["contact_reactions"] = [{"id": "shared_conversion", "activation": "installed",
            "when": comparison({"quantity": "wood"}),
            "reaction": {"op": "react", "inputs": {"wood": 1}, "outputs": {"wood": 1}}}]
        rows = self.rows(raw, imported_materials={"foreign": {
            "name": "实际外部材料", "load_per_u": REGISTRY.materials["wood"].load_per_u,
            "combustible": True, "liquid": False}})
        imported = next(row for row in rows if row["kind"] == "material_import")
        self.assertEqual(imported["status"], "deferred")
        self.assertIn({"function_id": "grove.definition_read", "arguments": {"definition_ref": "immutable-stock"}}, imported["read_requests"])
        contact = next(row for row in rows if row["kind"] == "contact_reaction_input")
        self.assertEqual(contact["status"], "deferred")
        self.assertTrue(any(item["condition"] == "actual_local_installation" for item in contact["requirements"]))

    def test_zero_contained_quantity_does_not_claim_material_is_required(self):
        rows = self.rows(definition(comparison({"quantity": "wood"}, op="eq")))
        quantity = next(row for row in rows if row["kind"] == "self_quantity")
        self.assertEqual(quantity["code"], "contained_material_fact")
        self.assertEqual(quantity["initial"]["value"], 0)
        self.assertEqual(quantity["requirements"][0]["purpose"], "read")
        self.assertIsNone(quantity["requirements"][0]["actual"])

    def test_reaction_rules_get_implicit_container_but_manual_only_effect_needs_one(self):
        raw = definition()
        effect = {"op": "react", "inputs": {"wood": 1}, "outputs": {"wood": 1}}
        raw["types"]["body"]["rules"][0]["effects"] = [effect]
        automatic = next(row for row in self.rows(raw) if row["kind"] == "reaction_input")
        self.assertEqual(automatic["status"], "deferred")
        self.assertTrue(next(item for item in automatic["requirements"] if item["condition"] == "effective_reaction_container")["satisfied"])
        raw["types"]["body"]["rules"] = []
        raw["types"]["body"]["actions"] = [{"id": "convert", "label": "投入", "effects": [effect]}]
        manual = next(row for row in self.rows(raw) if row["kind"] == "reaction_input")
        self.assertEqual((manual["status"], manual["code"]), ("missing", "reaction_container_missing"))

    def test_initial_fields_are_not_physical_energy_and_mechanisms_need_real_inputs(self):
        raw = definition(comparison({"field": "fuel"}))
        raw["types"]["body"]["fields"]["fuel"] = {"type": "integer", "default": 12}
        raw["types"]["body"]["mechanical"] = {"role": "source"}
        rows = self.rows(raw)
        field = next(row for row in rows if row["kind"] == "self_field")
        self.assertEqual(field["initial"]["value"], 12)
        mechanism = next(row for row in rows if row["kind"] == "mechanism")
        self.assertEqual(mechanism["status"], "deferred")
        self.assertTrue(any(item["condition"] == "enabled_source_and_actual_energy" and not item["known"]
                            for item in mechanism["requirements"]))

    def test_dependency_budget_reports_omissions_without_mutation_or_safety_claim(self):
        raw = definition({"op": "all", "args": [comparison({"physical": "wetness"}) for _ in range(8)]})
        raw["types"] = {"body" + str(i): deepcopy(raw["types"]["body"]) for i in range(8)}
        canonical = compiled(raw)
        before = deepcopy(canonical)
        result = creative._definition_diagnostics(canonical)
        self.assertEqual(canonical, before)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), MAX_DIAGNOSTIC_BYTES)
        self.assertTrue(result["dependency_analysis"]["truncated"])
        self.assertGreater(result["dependency_analysis"]["omitted_count"], 0)
        self.assertFalse(result["safety_proven"])
        core = program_diagnostics(canonical)
        self.assertTrue(core["dependency_analysis"]["static_only"])


class GroveDependencyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_missing_support_warning_preserves_validity_hash_and_read_only_state(self):
        raw = definition(comparison({"physical": "wetness"}))
        before = deepcopy(raw)
        with self.h.world._conn() as conn:
            state_rows = conn.execute("SELECT count(*) FROM world_state WHERE universe=?", ("w",)).fetchone()[0]
        validated = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertTrue(validated["valid"])
        self.assertFalse(validated["world_changed"])
        self.assertEqual(raw, before)
        with self.h.world._conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM world_state WHERE universe=?", ("w",)).fetchone()[0], state_rows)
        published = self.h.call("A", "grove.definition_publish", {"definition": raw})
        read = self.h.read("B", "grove.definition_read", {"definition_ref": published["definition_ref"]})
        self.assertEqual(read["definition"], published["definition"])
        self.assertEqual(read["diagnostics"], validated["diagnostics"])
        again = self.h.call("A", "grove.definition_publish", {"definition": raw})
        self.assertEqual(again["definition_ref"], published["definition_ref"])
        self.assertTrue(again["already_published"])


if __name__ == "__main__":
    unittest.main()
