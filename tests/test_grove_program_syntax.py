"""Public grammar discovery helps draft rejection without writing a recipe."""
from copy import deepcopy
import unittest

from jsonschema import Draft202012Validator

from agent_world.errors import RuleViolation
from agent_world.grove_connector_errors import public_world_error_details
from agent_world.worlds.shared_grove import creative
import test_grove_creative as fixtures


class GroveProgramSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.world.close)

    def test_machine_grammar_is_discoverable_and_compiler_accepted_fixture_is_in_its_language(self):
        overview = self.h.read("A", "grove.rule_catalog", {})
        request = overview["syntax_read_request"]
        syntax = self.h.read("A", request["function_id"], request["arguments"])
        self.assertFalse(syntax["world_facts_checked"])
        self.assertFalse(syntax["execution_proven"])
        Draft202012Validator.check_schema(syntax["schema"])
        Draft202012Validator(syntax["schema"]).validate(fixtures.package())
        self.assertNotIn("definition", syntax)
        self.assertNotIn("steps", syntax)

    def test_local_expression_schema_is_self_contained_and_does_not_recommend_const_wrappers(self):
        syntax = self.h.read("A", "grove.rule_catalog", {"topic": "syntax", "node": "expression"})
        validator = Draft202012Validator(syntax["schema"])
        validator.validate({"op": "eq", "left": {"field": "count"}, "right": 0})
        self.assertFalse(validator.is_valid({"const": 0}))
        self.assertFalse(any(set(node.get("required", [])) >= {"key", "name", "version", "types"}
            for node in syntax["schema"]["$defs"].values()))

    def test_invalid_draft_still_reaches_validator_and_reports_its_formal_node(self):
        raw = deepcopy(fixtures.event_counter_package())
        raw["types"]["counter"]["rules"][0]["when"]["right"] = {"const": "move"}
        before = self.h.world.get_state("w", creative.DEFINITIONS, "not_published")
        result = self.h.read("A", "grove.definition_validate", {"definition": raw})
        self.assertFalse(result["valid"])
        self.assertFalse(result["world_changed"])
        self.assertFalse(result["publication_created"])
        issue = result["diagnostics"]["items"][0]
        self.assertEqual(issue["path"], "program.types.counter.rules[0].when.right")
        self.assertEqual(issue["schema_node"], "expression")
        self.assertEqual(result["read_requests"], [creative._syntax_read_request("expression")])
        self.assertEqual(self.h.world.get_state("w", creative.DEFINITIONS, "not_published"), before)

    def test_publish_rejection_retains_same_node_through_safe_host_error_filter(self):
        raw = deepcopy(fixtures.event_counter_package())
        raw["types"]["counter"]["rules"][0]["when"]["right"] = {"const": "move"}
        with self.assertRaises(RuleViolation) as caught:
            self.h.call("A", "grove.definition_publish", {"definition": raw})
        details = public_world_error_details(caught.exception.details)
        self.assertEqual(details["diagnostics"]["items"][0]["schema_node"], "expression")
        self.assertEqual(details["read_requests"], [creative._syntax_read_request("expression")])
        self.assertNotIn("definition", details)

    def test_node_cannot_be_silently_ignored_by_an_unrelated_topic(self):
        with self.assertRaises(RuleViolation):
            self.h.read("A", "grove.rule_catalog", {"topic": "physical", "node": "expression"})


if __name__ == "__main__":
    unittest.main()
