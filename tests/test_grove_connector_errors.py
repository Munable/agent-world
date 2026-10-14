"""Public machine failures survive the host boundary without upstream data."""
from copy import deepcopy
import json
import unittest

from agent_world.grove_connector_errors import ConnectorError, public_world_error_details
from agent_world.worlds.shared_grove.action_contracts import failure


class GroveConnectorErrorContracts(unittest.TestCase):
    def test_formal_failure_preserves_conditions_read_requests_and_recovery(self):
        reason = failure("material_quantity_insufficient", "Need more material.", "material_quantity",
            subject="materials", actual=4, required=12, material="wood", unit="logical_matter",
            reads=[{"function_id": "grove.build_options", "arguments": {"x": 1, "y": 0, "recipe": "canopy"}}],
            instruction="Read the available materials, then choose again.")
        source = deepcopy(reason.details)
        supplied = ConnectorError("RuleViolation", str(reason), 409,
            world_details=source, recovery=reason.recovery).public_payload()
        self.assertEqual(supplied["details"], source)
        self.assertEqual(supplied["recovery"], "refresh_then_choose_action")
        supplied["details"]["failure"]["precondition"]["actual"] = 99
        self.assertEqual(source["failure"]["precondition"]["actual"], 4)

    def test_unknown_actual_stays_unknown_and_raw_locations_are_not_disclosed(self):
        reason = failure("entity_not_accessible", "Not observable.", "accessible",
                         entity_id="selected", known=False, required=True)
        source = deepcopy(reason.details)
        for condition in (source["action_precondition"], source["failure"]["precondition"]):
            condition.update(actual={"token": "private-value"}, actual_distance=45, satisfied=True,
                             location={"x": 27, "y": 18}, player_location={"x": 0, "y": 0})
        result = public_world_error_details(source)
        for condition in (result["action_precondition"], result["failure"]["precondition"]):
            self.assertIsNone(condition["actual"])
            self.assertFalse(condition["satisfied"])
            for field in ("actual_distance", "location", "player_location"):
                self.assertNotIn(field, condition)
        self.assertEqual(result["read_requests"], [{"function_id": "map.viewport", "arguments": {}}])
        self.assertNotIn("private-value", json.dumps(result))

    def test_legacy_distance_water_and_noncommitted_settlement_remain_public(self):
        source = {"action_precondition": {"condition": "adjacent", "known": True, "satisfied": False,
                    "subject": "item", "entity_id": "source", "metric": "manhattan", "required_max": 1,
                    "actual_distance": 3, "location": {"x": 3, "y": 0}, "player_location": {"x": 0, "y": 0}},
                  "water_transfer": {"source_id": "source", "target_id": "target", "target_slot": "surface",
                    "requested": 20, "maximum": 12, "unit": "u"},
                  "settlement": {"state": "calculating", "target_tick": 7, "operation_committed": False}}
        self.assertEqual(public_world_error_details(source), source)

    def test_formal_runtime_and_compiler_diagnostics_preserve_actual_location_and_values(self):
        runtime = {"code": "field_range", "phase": "manual", "action_id": "advance", "field": "count",
                   "path": "program.types.body.actions.advance.effects[0].value", "effect_index": 0,
                   "logical_step": 9, "attempted_value": 3, "min": 0, "max": 2}
        report = {"items": [{"severity": "warning", "code": "field_range_possible", "type_id": "body",
                    "rule_id": "advance", "phase": "reaction", "field": "count",
                    "path": "program.types.body.rules[0].effects[0].value", "inferred_min": 1,
                    "inferred_max": 3, "min": 0, "max": 2, "message": "Range may exceed the declaration."}],
                  "truncated": False, "omitted_count": 0, "safety_proven": False,
                  "analysis_scope": "Local declaration advice; this is not execution permission."}
        source = {"program_diagnostic": runtime, "diagnostics": report}
        expected = deepcopy(source)
        expected["diagnostics"]["items"][0].pop("message")
        expected["diagnostics"].pop("analysis_scope")
        self.assertEqual(ConnectorError("RuleViolation", "Not executed.", 409,
                                       world_details=source).public_payload()["details"], expected)

    def test_upstream_fields_and_queries_with_credentials_are_filtered_without_rewriting(self):
        reason = failure("container_closed", "Open first.", "container_open", entity_id="target",
                         actual=False, required=True)
        source = deepcopy(reason.details)
        source.update(upstream={"authorization": "private-secret"}, provider_trace="private-secret",
                      recovery={"strategy": "refresh_then_choose_action", "instruction": "Read first.",
                                "headers": {"Authorization": "private-secret"}},
                      program_diagnostic={"code": "field_range", "logical_step": 3,
                                          "raw": "private-secret", "authorization": "private-secret",
                                          "message": "private-secret", "path": "/private-secret"},
                      diagnostics={"items": [{"code": "invalid_definition", "severity": "error",
                                              "path": "program", "message": "private-secret"}],
                                   "analysis_scope": "private-secret", "safety_proven": False})
        source["failure"]["raw"] = "private-secret"
        source["failure"]["precondition"]["provider_trace"] = "private-secret"
        source["read_requests"].append({"function_id": "grove.inspect",
            "arguments": {"entity_id": "target", "bearer": "private-secret"}})
        result = public_world_error_details(source)
        self.assertEqual(result["read_requests"], [{"function_id": "grove.inspect", "arguments": {"entity_id": "target"}}])
        self.assertEqual(result["program_diagnostic"], {"code": "field_range", "logical_step": 3})
        self.assertNotIn("private-secret", json.dumps(result))

    def test_combined_public_details_stay_within_eight_kibibytes(self):
        reason = failure("declared_action_preconditions", "Blocked.", "accessible", entity_id="target",
                         actual=False, required=True, instruction="读" * 2048)
        source = deepcopy(reason.details)
        source["diagnostics"] = {"items": [{"code": "field_range_possible", "message": "诊" * 512,
                                          "path": "program." + "a" * 230 + f".rules[{index}].value"}
                                         for index in range(16)], "safety_proven": False}
        result = public_world_error_details(source)
        self.assertEqual(result["failure"]["recovery"]["instruction"], "读" * 2048)
        self.assertNotIn("diagnostics", result)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), 8192)

    def test_effect_fact_diagnostic_and_public_type_catalog_survive_without_instance_values(self):
        source = {"code": "unknown_fact", "effect_evidence_diagnostic": {
            "code": "unknown_fact", "path": "predicates[1].fact", "predicate_index": 1,
            "supplied_fact": "repeatedfield:wet_count", "supplied_operator": "gt",
            "actual": "private-secret", "source_body": "private-secret"},
            "fact_catalog": {"physical.wetness": {"type": "integer", "unit": "%",
                "scope": "registered_current_fact", "actual": "private-secret"},
                "authored.count": {"type": "integer", "min": 0, "max": 100,
                    "scope": "explicit_authored_export", "default": "private-secret", "value": "private-secret"}},
            "read_requests": [
                {"function_id": "grove.definition_read", "arguments": {"definition_ref": "fixed-ref"}},
                {"function_id": "grove.effect_evidence", "arguments": {"entity_id": "selected", "definition_ref": "fixed-ref"}}]}
        original = deepcopy(source)
        result = ConnectorError("RuleViolation", "Unknown registered fact.", 409, world_details=source).public_payload()["details"]
        self.assertEqual(result["effect_evidence_diagnostic"], {"code": "unknown_fact", "path": "predicates[1].fact",
            "predicate_index": 1, "supplied_fact": "repeatedfield:wet_count", "supplied_operator": "gt"})
        self.assertEqual(result["fact_catalog"], {"physical.wetness": {"type": "integer", "unit": "%",
            "scope": "registered_current_fact"}, "authored.count": {"type": "integer", "min": 0, "max": 100,
            "scope": "explicit_authored_export"}})
        self.assertEqual(result["read_requests"], source["read_requests"])
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertEqual(source, original)

    def test_effect_predicate_requirements_preserve_only_matching_paths_types_and_ranges(self):
        diagnostic = {"code": "predicate_type_mismatch", "path": "predicates[0].value", "predicate_index": 0,
            "supplied_fact": "authored.count", "supplied_operator": "gte", "expected_type": "integer",
            "supplied_type": "boolean", "min": 0, "max": 100,
            "allowed_operators": ["eq", "ne", "lt", "lte", "gt", "gte"], "value": "private-secret"}
        result = public_world_error_details({"effect_evidence_diagnostic": diagnostic})
        expected = deepcopy(diagnostic)
        expected.pop("value")
        self.assertEqual(result["effect_evidence_diagnostic"], expected)
        for changed in ({"path": "predicates[1].value"}, {"path": "predicates[0].fact"},
                        {"predicate_index": 8}, {"predicate_index": True}, {"code": "private-secret"},
                        {"code": {"raw": "private-secret"}}):
            with self.subTest(changed=changed):
                self.assertIsNone(public_world_error_details({"effect_evidence_diagnostic": {**diagnostic, **changed}}))
        mismatch = public_world_error_details({"effect_evidence_diagnostic": {
            "code": "definition_reference_mismatch", "path": "definition_ref", "actual_ref": "private-secret"}})
        self.assertEqual(mismatch, {"effect_evidence_diagnostic": {"code": "definition_reference_mismatch", "path": "definition_ref"}})

    def test_effect_catalog_rejects_private_paths_invalid_declarations_and_unbounded_lists(self):
        for catalog in (
            {"state.program.private": {"type": "integer", "scope": "registered_current_fact"}},
            {"authored.count": {"type": "integer", "scope": "registered_current_fact"}},
            {"authored.count": {"type": "integer", "scope": "explicit_authored_export", "min": 2, "max": 1}},
            {"authored.flag": {"type": "boolean", "scope": "explicit_authored_export", "min": 0, "max": 1}},
            {"physical.wetness": {"type": "object", "scope": "registered_current_fact"}},
            {"physical.wetness": {"type": "integer", "scope": {"raw": "private-secret"}}},
            {"physical." + f"field_{index}": {"type": "integer", "scope": "registered_current_fact"} for index in range(41)},
        ):
            with self.subTest(catalog=catalog):
                self.assertIsNone(public_world_error_details({"fact_catalog": catalog}))

    def test_syntax_discovery_node_is_bounded_and_does_not_relay_raw_compiler_text(self):
        source = {"diagnostics": {"items": [{"code": "invalid_definition", "severity": "error",
            "path": "program.types.body.rules[0]", "schema_node": "rule", "message": "private-secret"}],
            "safety_proven": False}, "read_requests": [
                {"function_id": "grove.rule_catalog", "arguments": {"topic": "program_syntax", "node": "rule"}},
                {"function_id": "grove.rule_catalog", "arguments": {"topic": "program_syntax", "node": {"raw": "private-secret"}}}]}
        result = public_world_error_details(source)
        self.assertEqual(result["diagnostics"]["items"][0]["schema_node"], "rule")
        self.assertEqual(result["read_requests"], [source["read_requests"][0]])
        self.assertNotIn("private-secret", json.dumps(result))
        source["diagnostics"]["items"][0]["schema_node"] = "node includes private-secret"
        self.assertNotIn("schema_node", public_world_error_details(source)["diagnostics"]["items"][0])

    def test_large_type_catalog_cannot_expand_existing_eight_kibibyte_failure_details(self):
        source = deepcopy(failure("declared_action_preconditions", "Blocked.", "accessible", entity_id="target",
            actual=False, required=True, instruction="读" * 2048).details)
        source["fact_catalog"] = {"physical." + "a" * 40 + f"_{index}": {
            "type": "integer", "scope": "registered_current_fact", "unit": "u" * 16,
            "min": -(2**63 - 1), "max": 2**63 - 1} for index in range(40)}
        result = public_world_error_details(source)
        self.assertNotIn("fact_catalog", result)
        self.assertEqual(result["failure"]["recovery"]["instruction"], "读" * 2048)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), 8192)


if __name__ == "__main__":
    unittest.main()
