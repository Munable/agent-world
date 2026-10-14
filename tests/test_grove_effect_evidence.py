"""Server-read evidence separates current truth from retained transitions."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from agent_world import FunctionOutcome, FunctionSpec
from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.worlds.shared_grove import effect_evidence as evidence
from agent_world.worlds.shared_grove import creative, generation, model as m, make_world
import test_grove_creative as fixtures


def package(*, automatic=False, container=False):
    spec = {"name": "A name is not evidence", "body_material": "stone", "cost": {"stone": 1},
        "fields": {"count": {"type": "integer", "default": 2, "min": 0, "max": 100},
                   "private_counter": {"type": "integer", "default": 55, "min": 0, "max": 100}},
        "exports": ["count"]}
    if automatic:
        spec["rules"] = [{"id": "increment", "phase": "after", "when": True,
            "effects": [{"op": "set", "field": "count", "value": {"op": "add", "left": {"field": "count"}, "right": 1}}]}]
    if container:
        spec["container_capacity"] = 20
    return {"key": "evidence", "name": "Declared purpose is not a result", "version": 1, "types": {"counter": spec}}


class EffectEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.world.close)
        self.fixture_definition = make_world()
        self.assertIn("grove.effect_evidence", {spec.name for spec in self.fixture_definition.functions})

    def create(self, **kwargs):
        reference, entities = self.h.publish("A", package(**kwargs), ["counter"])
        return reference, entities["counter"]

    def read(self, reference, entity, *, actor="A", after_step=0, predicates=None, **extra):
        return self.h.read(actor, "grove.effect_evidence", {"definition_ref": reference, "entity_id": entity,
            "after_step": after_step, "predicates": predicates or [{"fact": "authored.count", "operator": "gte", "value": 2}],
            **extra})

    def patch_metadata(self, scope, key, value):
        def patch(ctx, arguments):
            ctx.set_state(scope, key, deepcopy(value))
            return FunctionOutcome({"patched": True})
        name = "grove.fixture_effect_metadata"
        self.install_fixture_function(name, patch, "Trusted metadata fixture")
        self.h.call("A", name, {})

    def install_fixture_function(self, name, handler, description):
        # Managed worlds change only through a versioned definition. This
        # trusted test function simulates legacy metadata/retention gaps; it
        # does not add a public management bypass or relax commit validation.
        version = self.fixture_definition.version + 1
        functions = tuple(replace(spec, handler=evidence.make_handler(version))
            if spec.name == "grove.effect_evidence" else spec for spec in self.fixture_definition.functions)
        fixture = FunctionSpec(name, handler, {"type": "object", "properties": {}, "additionalProperties": False},
                               description=description)
        self.fixture_definition = replace(self.fixture_definition, version=version, functions=(*functions, fixture))
        self.h.world.install_world("w", self.fixture_definition)

    def test_published_created_and_currently_satisfied_does_not_prove_effect_or_action(self):
        reference, entity = self.create()
        before = self.h.world.get_state("w", m.ENTITIES, entity)
        result = self.read(reference, entity, operation_id="original-action")
        self.assertEqual(result["definition_ref"], reference)
        self.assertEqual(result["entity_id"], entity)
        self.assertEqual(result["entity_version"], before["version"])
        self.assertEqual(result["agent_context"]["world_instance"], "w")
        self.assertEqual(result["agent_context"]["viewer_role_id"], self.h.roles["A"])
        self.assertEqual(result["stages"]["publication"]["status"], "published")
        self.assertEqual(result["stages"]["instance_creation"]["status"], "exists")
        self.assertEqual(result["stages"]["action_commit"]["status"], "unknown")
        predicate = result["predicates"][0]
        self.assertTrue(predicate["current"]["satisfied"])
        self.assertEqual(predicate["effect_observed"]["status"], "unknown")
        self.assertEqual(result["history"]["transition_records"], [])
        self.assertIn({"function_id": "world.get_receipt", "arguments": {"operation_id": "original-action"}}, result["read_requests"])
        self.assertEqual(self.h.world.get_state("w", m.ENTITIES, entity), before)
        self.assertNotIn("goal", result)

    def test_actual_typed_change_is_witnessed_only_after_requested_logical_step(self):
        reference, entity = self.create(automatic=True)
        before_step = self.h.world.get_state("w", m.META, "clock")["value"]["sequence"]
        self.h.ticks(2)
        result = self.read(reference, entity, after_step=before_step)
        verdict = result["predicates"][0]["effect_observed"]
        self.assertEqual(verdict["status"], "observed")
        witness = verdict["witnesses"][0]
        self.assertGreater(witness["step"], before_step)
        self.assertLessEqual(witness["step"], result["observed_step"])
        self.assertEqual((witness["entity_id"], witness["definition_ref"]), (entity, reference))
        self.assertEqual(witness["after"] - witness["before"], 1)
        self.assertEqual(witness["causal_attribution"], "not_established")
        old = self.read(reference, entity, after_step=witness["step"])
        self.assertTrue(old["predicates"][0]["current"]["satisfied"])
        self.assertEqual(old["predicates"][0]["effect_observed"]["status"], "unknown")
        self.assertFalse(old["history"]["complete"])
        no_window = self.h.read("A", "grove.effect_evidence", {"definition_ref": reference, "entity_id": entity,
            "predicates": [{"fact": "authored.count", "operator": "gte", "value": 2}]})
        self.assertEqual(no_window["predicates"][0]["effect_observed"]["reason"], "after_step_required_for_effect_window")

    def test_missing_step_or_unexported_historical_values_never_become_a_witness(self):
        reference, entity = self.create()
        self.patch_metadata(creative.FIELD_CHANGES, entity,
            {"before": {"count": 0, "private_counter": 1}, "after": {"count": 2, "private_counter": 99}})
        result = self.read(reference, entity)
        self.assertEqual(result["predicates"][0]["effect_observed"]["status"], "unknown")
        self.assertEqual(result["history"]["coverage"]["transition_status"], "transition_step_unverified")
        self.assertEqual(result["history"]["transition_records"], [])
        self.assertNotIn("private_counter", repr(result))

    def test_predicates_allow_only_registered_or_explicit_exported_typed_facts(self):
        reference, entity = self.create()
        for predicate in (
            {"fact": "state.program.fields.private_counter", "operator": "eq", "value": 55},
            {"fact": "authored.private_counter", "operator": "eq", "value": 55},
            {"fact": "authored.count", "operator": "gte", "value": True},
        ):
            with self.subTest(predicate=predicate), self.assertRaises(RuleViolation):
                self.read(reference, entity, predicates=[predicate])
        result = self.read(reference, entity, predicates=[{"fact": "mechanical.speed", "operator": "gt", "value": 0}])
        self.assertFalse(result["predicates"][0]["current"]["known"])
        self.assertNotIn("actual", result["predicates"][0]["current"])
        self.assertEqual(result["predicates"][0]["effect_observed"]["status"], "unknown")

    def test_unknown_fact_reports_exact_index_and_typed_discovery_without_guessing_aliases(self):
        reference, entity = self.create()
        before = self.h.world.get_state("w", m.ENTITIES, entity)
        predicates = [{"fact": "intact", "operator": "eq", "value": True},
                      {"fact": "repeatedfield:wet_count", "operator": "gt", "value": 0}]
        with self.assertRaises(RuleViolation) as raised:
            self.read(reference, entity, predicates=predicates)
        details = raised.exception.details
        diagnostic = details["effect_evidence_diagnostic"]
        self.assertEqual(details["code"], "unknown_fact")
        self.assertEqual(diagnostic, {"code": "unknown_fact", "path": "predicates[1].fact",
            "predicate_index": 1, "supplied_fact": "repeatedfield:wet_count", "supplied_operator": "gt"})
        self.assertEqual(details["fact_catalog"]["authored.count"],
                         {"type": "integer", "min": 0, "max": 100, "scope": "explicit_authored_export"})
        self.assertNotIn("authored.private_counter", details["fact_catalog"])
        self.assertNotIn("authored.wet_count", details["fact_catalog"])
        self.assertEqual(details["fact_catalog"]["physical.wetness"]["type"], "integer")
        self.assertEqual(details["read_requests"], [
            {"function_id": "grove.definition_read", "arguments": {"definition_ref": reference}},
            {"function_id": "grove.effect_evidence", "arguments": {"entity_id": entity, "definition_ref": reference}}])
        discovered = self.h.read("A", "grove.effect_evidence", details["read_requests"][1]["arguments"])
        self.assertEqual(discovered["predicates"], [])
        self.assertEqual(discovered["fact_catalog"], details["fact_catalog"])
        self.assertNotIn("observation_id", details)
        self.assertNotIn("actual", json.dumps(details))
        self.assertLessEqual(len(json.dumps(details, ensure_ascii=False, separators=(",", ":")).encode()), 8192)
        self.assertEqual(self.h.world.get_state("w", m.ENTITIES, entity), before)

    def test_type_range_and_operator_errors_describe_declared_requirements_without_answers(self):
        reference, entity = self.create()
        cases = (({"fact": "authored.count", "operator": "gte", "value": True},
                  "predicate_type_mismatch", "value", "integer", "boolean", ["eq", "ne", "lt", "lte", "gt", "gte"]),
                 ({"fact": "authored.count", "operator": "gte", "value": 101},
                  "predicate_range_mismatch", "value", "integer", "integer", ["eq", "ne", "lt", "lte", "gt", "gte"]),
                 ({"fact": "physical.burning", "operator": "gt", "value": False},
                  "predicate_operator_mismatch", "operator", "boolean", "boolean", ["eq", "ne"]))
        for predicate, code, field, expected, supplied, operators in cases:
            with self.subTest(code=code), self.assertRaises(RuleViolation) as raised:
                self.read(reference, entity, predicates=[predicate])
            details = raised.exception.details
            diagnostic = details["effect_evidence_diagnostic"]
            self.assertEqual(diagnostic["code"], code)
            self.assertEqual(diagnostic["path"], f"predicates[0].{field}")
            self.assertEqual(diagnostic["predicate_index"], 0)
            self.assertEqual(diagnostic["expected_type"], expected)
            self.assertEqual(diagnostic["supplied_type"], supplied)
            self.assertEqual(diagnostic["allowed_operators"], operators)
            if expected == "integer":
                self.assertEqual((diagnostic["min"], diagnostic["max"]), (0, 100))
            self.assertNotIn("actual", diagnostic)
            self.assertNotIn("value", diagnostic)
            self.assertNotIn("fact_catalog", details)
            self.assertEqual(details["read_requests"][1]["arguments"], {"entity_id": entity, "definition_ref": reference})

    def test_wrong_definition_and_another_roles_private_instance_are_refused(self):
        reference, entity = self.create()
        with self.assertRaises(RuleViolation) as raised:
            self.read(reference + ":wrong", entity)
        self.assertEqual(raised.exception.details, {"code": "definition_reference_mismatch",
            "effect_evidence_diagnostic": {"code": "definition_reference_mismatch", "path": "definition_ref"},
            "read_requests": [{"function_id": "grove.inspect", "arguments": {"entity_id": entity}}]})
        self.assertNotIn(reference, repr(raised.exception.details))
        self.h.approach("A", entity)
        self.h.interact("A", entity, "pickup")
        with self.assertRaises(PermissionDenied) as denied:
            self.read(reference, entity, actor="B", predicates=[{"fact": "wet_count", "operator": "eq", "value": 55}])
        self.assertNotIn("effect_evidence_diagnostic", denied.exception.details)
        self.assertNotIn("fact_catalog", denied.exception.details)

    def test_closed_public_vessel_hides_values_history_and_private_blocker_sources(self):
        reference, entity = self.create(container=True)
        self.h.approach("A", entity)
        self.h.interact("A", entity, "close")
        result = self.read(reference, entity, actor="B")
        self.assertFalse(result["predicates"][0]["current"]["known"])
        self.assertNotIn("actual", result["predicates"][0]["current"])
        self.assertEqual(result["history"]["transition_records"], [])
        self.assertEqual(result["history"]["coverage"]["outcomes"]["reason"], "program_state_hidden")
        self.assertEqual(result["process"]["status"], "unknown")
        self.assertIsNone(result["process"]["blocked"])
        with self.assertRaises(RuleViolation) as raised:
            self.read(reference, entity, actor="B", predicates=[{"fact": "wet_count", "operator": "eq", "value": 55}])
        details = raised.exception.details
        self.assertIn("authored.count", details["fact_catalog"])
        self.assertNotIn("authored.private_counter", details["fact_catalog"])
        self.assertNotIn("actual", json.dumps(details))
        discovered = self.h.read("B", "grove.effect_evidence", details["read_requests"][1]["arguments"])
        self.assertEqual(discovered["predicates"], [])
        self.assertEqual(discovered["history"]["transition_records"], [])
        self.assertEqual(discovered["process"]["status"], "unknown")

    def test_public_outcome_gap_and_old_summary_do_not_prove_selected_fact_effect(self):
        reference, entity = self.create()
        point = self.h.entity(entity)
        region = generation._region_key(point["x"], point["y"])
        content = deepcopy(self.h.world.get_state("w", generation.CONTENTS, region)["value"])
        content["outcome_count"] = 2
        self.patch_metadata(generation.CONTENTS, region, content)
        def save(ctx, args):
            missing = ctx.get_state_record(generation.OUTCOMES, f"{content['id']}:outcome:1")
            if missing["exists"]:
                ctx.delete_state(generation.OUTCOMES, f"{content['id']}:outcome:1", expected_version=missing["version"])
            ctx.set_state(generation.OUTCOMES, f"{content['id']}:outcome:2", {
                "id": f"{content['id']}:outcome:2", "entity_id": entity, "entity_version": point["version"],
                "action": "a_name_is_not_a_result", "summary": "The goal was completed", "at": 1000,
                "author": self.h.roles["A"], "process_id": None})
            return FunctionOutcome({})
        self.install_fixture_function("grove.fixture_outcome", save, "Public historical row")
        self.h.call("A", "grove.fixture_outcome", {})
        self.h.approach("A", entity)
        result = self.read(reference, entity)
        coverage = result["history"]["coverage"]["outcomes"]
        self.assertEqual(coverage["missing_sequences"], [1])
        self.assertFalse(result["history"]["complete"])
        self.assertEqual(result["predicates"][0]["effect_observed"]["status"], "unknown")
        self.assertEqual(result["history"]["outcomes"][0]["entity_id"], entity)
        self.assertNotIn("summary", result["history"]["outcomes"][0])
        self.assertNotIn("The goal was completed", repr(result))
