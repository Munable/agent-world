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


if __name__ == "__main__":
    unittest.main()
