"""Real compiler, runtime-error and local-source contracts; no model claims."""
from copy import deepcopy
import hashlib
import json
import unittest

import test_grove_creative as fixtures
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from agent_world.simulation.program_diagnostics import runtime_diagnostic, ProgramEvaluationError
from agent_world.worlds.shared_grove import model as m


def bounded_definition():
    return {"key": "bounded_advice", "name": "范围检查", "version": 1, "types": {"body": {
        "name": "声明物件", "body_material": "wood", "cost_u": {"wood": 1},
        "fields": {"level": {"type": "integer", "default": 0, "min": 0, "max": 2}},
        "rules": [{"id": "advance", "phase": "reaction", "when": True,
            "effects": [{"op": "set", "field": "level", "value": {"op": "add", "left": {"field": "level"}, "right": 1}}]}],
        "actions": [{"id": "clear_level", "label": "归零", "effects": [{"op": "set", "field": "level", "value": 0}]}]}}}


class CreationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def form(self, ref, type_id="body"):
        menu = self.h.read("A", "grove.fabrication_options", {"definition_ref": ref, "type_id": type_id})
        args = deepcopy(menu["action"]["arguments"])
        for material, quantity in menu["cost_u"].items():
            left = quantity
            for entity in menu["action"]["choices"]["sources"]:
                for lot in entity["lots"]:
                    if lot["material"] == material and left:
                        take = min(left, lot["quantity"])
                        args["sources"].append({"entity_id": entity["id"], "expected_entity_version": entity["version"],
                                              "lot_id": lot["id"], "quantity_u": take})
                        left -= take
            self.assertEqual(left, 0)
        return self.h.call("A", "grove.fabricate", args)["entity"]["id"]

    def test_validate_is_read_only_and_publication_hash_stays_canonical(self):
        raw = bounded_definition()
        before = deepcopy(raw)
        with self.h.world._conn() as conn:
            rows_before = conn.execute("SELECT count(*) FROM world_state WHERE universe=?", ("w",)).fetchone()[0]
        report = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertTrue(report["valid"])
        self.assertFalse(report["world_changed"])
        self.assertFalse(report["publication_created"])
        self.assertFalse(report["diagnostics"]["safety_proven"])
        self.assertEqual(report["diagnostics"]["items"][0]["code"], "field_range_possible")
        self.assertEqual(raw, before)
        with self.h.world._conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM world_state WHERE universe=?", ("w",)).fetchone()[0], rows_before)
        published = self.h.call("A", "grove.definition_publish", {"definition": raw})
        canonical = published["definition"]
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        expected = "program_"+hashlib.sha256((self.h.roles["A"]+":"+digest).encode()).hexdigest()
        self.assertEqual(published["definition_ref"], expected)
        read = self.h.read("B", "grove.definition_read", {"definition_ref": expected})
        self.assertEqual(read["definition"], canonical)
        self.assertEqual(read["diagnostics"], report["diagnostics"])
        invalid = deepcopy(raw)
        invalid["types"]["body"]["rules"][0]["effects"][0]["value"] = {"literal": 1}
        rejected = self.h.read("A", "grove.definition_validate", {"definition": invalid})
        self.assertFalse(rejected["valid"])
        self.assertIn("effects[0].value", rejected["diagnostics"]["items"][0]["path"])

    def test_simple_mutually_exclusive_guards_and_frozen_write_risks(self):
        raw = bounded_definition()
        spec = raw["types"]["body"]
        spec["rules"][0]["when"] = {"op": "lt", "left": {"field": "level"}, "right": 2}
        spec["rules"].append({"id": "wrap", "phase": "reaction",
            "when": {"op": "eq", "left": {"field": "level"}, "right": 2},
            "effects": [{"op": "set", "field": "level", "value": 0}]})
        valid = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertEqual(valid["diagnostics"]["items"], [])
        # This earlier candidate write may change the value even though the
        # later rule's predicate saw a value below2 in the frozen snapshot.
        spec["rules"].append({"id": "a_raise", "phase": "reaction", "when": True,
            "effects": [{"op": "set", "field": "level", "value": 2}]})
        risky = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertTrue(any(item.get("rule_id") == "advance" and item["code"] == "field_range_possible"
                            for item in risky["diagnostics"]["items"]))

    def test_real_runtime_error_records_failed_value_and_other_work_continues(self):
        raw = bounded_definition()
        raw["types"]["safe"] = deepcopy(raw["types"]["body"])
        raw["types"]["safe"]["rules"][0]["when"] = {"op": "lt", "left": {"field": "level"}, "right": 2}
        ref = self.h.call("A", "grove.definition_publish", {"definition": raw})["definition_ref"]
        bad, safe = self.form(ref), self.form(ref, "safe")
        self.h.ticks(4)
        failed = self.h.entity(bad)["state"]["program"]
        self.assertEqual(failed["fields"]["level"], 2)
        self.assertEqual(failed["process"]["status"], "error")
        detail = failed["process"]["error_diagnostic"]
        self.assertEqual((detail["rule_id"], detail["phase"], detail["effect_index"]), ("advance", "reaction", 0))
        self.assertEqual((detail["attempted_value"], detail["min"], detail["max"]), (3, 0, 2))
        self.assertIn("level", failed["process"]["error"])
        self.assertLessEqual(sm.encoded_size(detail), 512)
        self.assertNotEqual(self.h.entity(safe)["state"]["program"]["process"]["status"], "error")
        self.assertEqual(self.h.entity(safe)["state"]["program"]["fields"]["level"], 2)

    def test_reaction_heat_is_a_candidate_field_write(self):
        raw = bounded_definition()
        spec = raw["types"]["body"]
        spec["fields"] = {"temperature": {"type": "integer", "default": 0, "min": 0, "max": 20}}
        spec["actions"] = []
        rule = spec["rules"][0]
        rule["when"] = {"op": "lt", "left": {"field": "temperature"}, "right": 20}
        rule["effects"] = [{"op": "react", "inputs": {"wood": 1}, "outputs": {"wood": 1}, "heat": 1},
            {"op": "set", "field": "temperature", "value": {"op": "add", "left": {"field": "temperature"}, "right": 1}}]
        report = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertTrue(report["valid"])
        self.assertTrue(any(item["code"] == "field_range_possible" and item["path"].endswith("effects[1].value")
                            and item["inferred_max"] == 21 for item in report["diagnostics"]["items"]))

    def test_error_detail_cannot_charge_old_near_capacity_instance_again(self):
        raw = bounded_definition()
        ref = self.h.call("A", "grove.definition_publish", {"definition": raw})["definition_ref"]
        entity = self.h.entity(self.form(ref))
        entity["state"]["program"]["process"].update(status="error", error="规则越界")
        lo, hi = 0, sm.MAX_ENTITY_BYTES
        while lo < hi:
            middle = (lo+hi+1)//2
            candidate = deepcopy(entity)
            candidate["state"]["padding"] = "x"*middle
            try:
                m.normalize_entity(candidate)
                lo = middle
            except RuleViolation:
                hi = middle-1
        entity["state"]["padding"] = "x"*lo
        before = deepcopy(entity)
        detail = runtime_diagnostic(ProgramEvaluationError("规则越界", {
            "code": "field_range", "rule_id": "advance", "phase": "reaction", "effect_index": 0,
            "field": "level", "attempted_value": 3, "min": 0, "max": 2}), logical_step=4)
        self.assertFalse(m.fit_program_error_diagnostic(entity, detail))
        self.assertEqual(entity, before)
        m.normalize_entity(entity)

    def test_missing_recipe_explains_structure_and_finds_actual_public_sources(self):
        raw = {"key": "raw_stock", "name": "投料来源", "version": 1, "types": {
            "body": {"name": "耗料物件", "body_material": "wood", "cost_u": {"wood": 40}},
            "next": {"name": "下一件", "body_material": "wood", "cost_u": {"wood": 4}}}}
        ref = self.h.call("A", "grove.definition_publish", {"definition": raw})["definition_ref"]
        formed = self.form(ref)
        menu = self.h.read("A", "grove.fabrication_options", {"definition_ref": ref, "type_id": "next"})
        self.assertFalse(menu["action"]["available"])
        self.assertEqual(menu["materials"][0]["available_u"], 0)
        self.assertIn(formed, [row["id"] for row in menu["unavailable_sources"]])
        sources = menu["acquisition_sources"]
        self.assertTrue(any(row["materials"] and row["next_action"] in {"pickup", "harvest"} for row in sources))
        self.assertNotIn(formed, [row["id"] for row in sources])
        self.assertFalse(menu["source_discovery_projection"]["world_scan"])
        self.assertLessEqual(len(sources), 12)

    def test_catalog_default_is_discovery_and_all_retains_full_mechanisms(self):
        overview = self.h.read("A", "grove.rule_catalog")
        self.assertEqual(overview["topic"], "overview")
        self.assertEqual(overview["validation"]["function_id"], "grove.definition_validate")
        self.assertNotIn("mechanical", overview)
        all_topics = self.h.read("A", "grove.rule_catalog", {"topic": "all"})
        for key in ("mechanical", "contract", "recorder", "component_schema", "public_facts", "builtin_materials"):
            self.assertIn(key, all_topics)
        for row in overview["topics"]:
            self.assertEqual(self.h.read("A", "grove.rule_catalog", row["read_request"]["arguments"])["topic"], row["topic"])
