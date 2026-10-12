"""Presented-observation contracts independent of a host or world transport."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_world.grove_agent_observation import AgentObservations, blueprint_model_input, companion_projection
from agent_world.grove_connector_errors import ConnectorError


INTERACT_SCHEMA = {
    "type": "object",
    "properties": {key: {"type": "integer"} for key in
        ("expected_player_version", "expected_target_version", "expected_item_version")},
    "required": ["expected_player_version", "expected_target_version", "expected_item_version"],
}


class AgentObservationsTests(unittest.TestCase):
    def test_checkpoint_supplies_new_opaque_observation_with_current_mechanical_facts(self):
        old = self.observations.present("map.viewport", self.scene(), preflight=True)["observation_id"]
        facts = self.scene()
        facts["clock"] = {"sequence": 91}
        facts["entities"][0].update(version=23, creative={"mechanical_observation": {
            "role": "source", "angle_unit": "logical_turns", "stored_state": {"angle": 36, "energy": 4}}})
        result = self.observations.present("execution_checkpoint", {"status": "completed", "segment_finished": True,
            "execution_readback": facts}, base_id=old)
        self.assertNotEqual(result["observation_id"], old)
        self.assertEqual(result["execution_readback"]["clock"]["sequence"], 91)
        current = result["execution_readback"]["entities"][0]["creative"]["mechanical_observation"]
        self.assertEqual(current["stored_state"], {"angle": 36, "energy": 4})
        self.assertEqual(current["angle_unit"], "logical_turns")
        self.assertEqual(self.observations.get(old)["versions"]["entity"]["target"], 22)

    def setUp(self):
        self.observations = AgentObservations("person")

    def scene(self):
        return {"player": {"id": "person", "version": 7}, "entities": [
            {"id": "target", "version": 22, "pour_target_slot": "contained"},
            {"id": "source", "version": 8}]}

    def test_only_explicit_base_extends_an_observation_without_mutating_prior_facts(self):
        source = self.scene()
        preflight = self.observations.present("map.viewport", source, preflight=True)
        old = preflight["observation_id"]
        original = self.observations.get(old)
        first = self.observations.present("grove.inspect", {
            "entity": {"id": "selected", "version": 3}}, base_id=old)
        self.observations.present("grove.inspect", {
            "entity": {"id": "selected", "version": 4}}, base_id=old)
        plain = self.observations.present("grove.rule_catalog", {"definitions": []})
        extended = self.observations.present("grove.rule_catalog", {"definitions": []},
            base_id=first["observation_id"])
        self.assertEqual(self.observations.preflight_id, old)
        self.assertNotIn("selected", self.observations.get(plain["observation_id"])["versions"]["entity"])
        self.assertEqual(self.observations.get(extended["observation_id"])["versions"]["entity"]["selected"], 3)
        source["entities"][0]["version"] = 999
        preflight["entities"][0]["version"] = 1000
        detached = self.observations.get(old)
        detached["versions"]["entity"]["target"] = 1001
        self.assertEqual(self.observations.get(old), original)

    def test_sequence_is_reserved_before_publication_and_old_handles_expire_on_restart(self):
        reserved = []

        def reserve(sequence):
            reserved.append(sequence)
            self.assertEqual(observations.sequence, sequence)
            with self.assertRaises(ConnectorError):
                observations.get(f"obs{sequence}-ab1234ef")

        observations = AgentObservations("person", reserve_sequence=reserve)
        with patch("agent_world.grove_agent_observation.secrets.token_hex", return_value="ab1234ef"):
            old = observations.present("map.viewport", self.scene(), preflight=True)["observation_id"]
            restarted = AgentObservations("person", sequence=observations.sequence)
            fresh = restarted.present("map.viewport", self.scene())["observation_id"]
        self.assertEqual(reserved, [1])
        self.assertEqual((old, fresh), ("obs1-ab1234ef", "obs2-ab1234ef"))
        self.assertIsNone(restarted.preflight_id)
        with self.assertRaises(ConnectorError) as expired:
            restarted.bind("grove.interact", {}, old, INTERACT_SCHEMA)
        self.assertEqual(expired.exception.code, "ObservationRequired")

    def test_oversized_inspection_does_not_bind_a_preview_that_was_not_delivered(self):
        inspection = {"entity": {"id": "target", "version": 77}, "action_options": [
            {"action": "store", "input": {"candidates": [{"id": "source", "version": 88,
                "preview": {"version_fingerprint": "a" * 64, "physics": {"large": "x" * 80000}}}]}}]}
        supplied = self.observations.present("grove.inspect", inspection)
        self.assertFalse(supplied["projection"]["complete_inspection"])
        candidate = supplied["action_options"][0]["candidates_index"][0]
        self.assertEqual(candidate["read_request"]["arguments"],
            {"entity_id": "target", "action": "store", "candidate_id": "source"})
        bundle = self.observations.get(supplied["observation_id"])
        self.assertEqual(bundle["versions"]["entity"], {"target": 77})
        self.assertFalse(bundle.get("combination_previews"))
        self.assertEqual(json.loads(self.observations.serialize(supplied)), supplied)
        single = deepcopy(inspection)
        single["action_options_projection"] = {"candidate_selection": {"candidate_id": "source"}}
        sequence = self.observations.sequence
        with self.assertRaises(ConnectorError) as rejected:
            self.observations.present("grove.inspect", single)
        self.assertEqual(rejected.exception.code, "ToolOutputTooLarge")
        self.assertEqual(self.observations.sequence, sequence)
        self.assertEqual(self.observations.get(supplied["observation_id"]), bundle)

    def test_fixed_pour_intent_keeps_observed_versions_and_destination(self):
        old = self.observations.present("map.viewport", self.scene())["observation_id"]
        arguments = {"action": "pour", "amount": 20, "target_id": "target", "item_id": "source",
            "expected_target_version": 9999}
        bound = self.observations.bind("grove.interact", arguments, old, INTERACT_SCHEMA)
        self.assertEqual((bound["amount"], bound["target_slot"], bound["expected_player_version"],
            bound["expected_target_version"], bound["expected_item_version"]), (20, "contained", 7, 22, 8))
        self.assertNotIn("target_slot", arguments)
        fresh = self.observations.present("grove.inspect", {
            "entity": {"id": "target", "version": 23, "pour_target_slot": "surface"}},
            base_id=old)["observation_id"]
        self.assertEqual(self.observations.bind("grove.interact", arguments, old, INTERACT_SCHEMA)["target_slot"],
            "contained")
        with self.assertRaises(ConnectorError) as mismatch:
            self.observations.bind("grove.interact", arguments | {"target_slot": "contained"}, fresh, INTERACT_SCHEMA)
        self.assertEqual(mismatch.exception.code, "ObservedActionMismatch")
        without_slot = self.observations.present("grove.inspect", {
            "entity": {"id": "target", "version": 24, "state": {"open": True}, "capabilities": ["container"]}},
            base_id=fresh)["observation_id"]
        with self.assertRaises(ConnectorError) as absent:
            self.observations.bind("grove.interact", arguments, without_slot, INTERACT_SCHEMA)
        self.assertEqual(absent.exception.code, "ObservedActionRequired")

    def test_automatic_write_readback_cannot_upgrade_an_old_loading_preview(self):
        inspection = {"player": {"id": "person", "version": 11},
            "entity": {"id": "target", "version": 21}, "action_options": [
                {"action": "attach", "input": {"candidates": [{"id": "source", "version": 31,
                    "preview": {"version_fingerprint": "a" * 64}}]}}],
            "action_options_projection": {"scope": "selected_declared_action", "action": "attach",
                                          "expanded_actions": ["attach"]}}
        read = self.observations.present("grove.inspect", inspection)["observation_id"]
        original = self.observations.get(read)
        changed = self.observations.present("grove.interact", {"result": {}, "execution_readback": {
            "player": {"id": "person", "version": 12}, "entities": [
                {"id": "target", "version": 22}, {"id": "source", "version": 31}]}},
            base_id=read)["observation_id"]
        arguments = {"action": "attach", "target_id": "target", "item_id": "source"}
        with self.assertRaises(ConnectorError) as missing:
            self.observations.bind("grove.interact", arguments, changed, INTERACT_SCHEMA)
        self.assertEqual(missing.exception.code, "ObservedPreviewRequired")
        self.assertEqual(self.observations.get(read), original)
        inspection["player"]["version"] = 12
        inspection["entity"]["version"] = 22
        inspection["action_options"][0]["input"]["candidates"][0]["preview"]["version_fingerprint"] = "b" * 64
        fresh = self.observations.present("grove.inspect", inspection, base_id=changed)["observation_id"]
        self.assertEqual(self.observations.bind("grove.interact", arguments, fresh, INTERACT_SCHEMA)
            ["expected_combination_fingerprint"], "b" * 64)

    def test_creation_continuation_keeps_fixed_opportunity_basis_after_later_anchor_reads(self):
        initial = self.observations.present("grove.generation_context", {"opportunity": {
            "id": "opp", "version": 2, "continuation_basis": {
                "content_id": "story", "expected_content_version": 7,
                "anchors": [{"entity_id": "anchor", "observed_anchor_version": 9}]}}})
        newer = self.observations.present("grove.inspect", {"entity": {"id": "anchor", "version": 99}},
            base_id=initial["observation_id"])
        schema = {"properties": {"expected_opportunity_version": {}, "continuation": {
            "properties": {"expected_content_version": {}, "observed_anchor_version": {}}}}}
        bound = self.observations.bind("grove.content_submit", {"opportunity_id": "opp",
            "continuation": {"content_id": "story", "anchor_entity_id": "anchor"}},
            newer["observation_id"], schema)
        self.assertEqual(bound["expected_opportunity_version"], 2)
        self.assertEqual((bound["continuation"]["expected_content_version"],
            bound["continuation"]["observed_anchor_version"]), (7, 9))

    def test_output_limit_preserves_committed_receipt_and_binds_only_delivered_state(self):
        payload = {"operation_id": "original-write", "function_id": "grove.interact", "ok": True,
            "commit_seq": 42, "result": {"entity": {"id": "unshown", "version": 999, "name": "x" * 80000}},
            "execution_control": {"actions_count": 1, "remaining_actions": 7},
            "execution_readback": {"player": {"id": "person", "version": 12},
                "entities": [{"id": "also-unshown", "version": 888, "name": "x" * 80000}],
                "selected_inspection": {"entity": {"id": "selected", "version": 5}}}}
        supplied = self.observations.present("grove.interact", payload)
        self.assertEqual((supplied["operation_id"], supplied["commit_seq"], supplied["ok"]),
            ("original-write", 42, True))
        self.assertEqual(supplied["execution_control"], payload["execution_control"])
        self.assertFalse(supplied["projection"]["expanded_state_included"])
        versions = self.observations.get(supplied["observation_id"])["versions"]
        self.assertEqual(versions["entity"], {"selected": 5})
        self.assertEqual(versions["player"], {"person": 12})
        self.assertEqual(json.loads(self.observations.serialize(supplied)), supplied)

    def test_tool_schema_hides_guards_but_retains_authored_program_parameters(self):
        authored = {"type": "object", "properties": {"expected_heat_version": {"type": "integer"}},
            "required": ["expected_heat_version"]}
        source = {"properties": {"expected_player_version": {}, "params": authored,
            "continuation": {"properties": {"observed_anchor_version": {}, "content_id": {}}}}}
        supplied = self.observations.tool_schema(source)
        self.assertNotIn("expected_player_version", supplied["properties"])
        self.assertNotIn("observed_anchor_version", supplied["properties"]["continuation"]["properties"])
        self.assertEqual(supplied["properties"]["params"], authored)
        self.assertIn("expected_player_version", source["properties"])

    def test_creation_catalog_projection_uses_pure_actual_declarations(self):
        composition = {"material": "wood", "construction": "vessel", "size": 1}
        definition = {"composition": composition, "cost": {"wood": 2}, "state": {"capacity": 4}}
        source = {"catalog": {"compositions": [definition], "objects": [
            {"template": "first", **definition},
            {"template": "second", **definition, "state": {"capacity": 6}}]}}
        folded = blueprint_model_input(source)
        self.assertEqual(folded["catalog"]["objects"][1]["definition_overrides"], {"state": {"capacity": 6}})
        self.assertEqual(source["catalog"]["objects"][0]["cost"], {"wood": 2})
        projected = companion_projection("grove.generation_context", source)
        self.assertEqual(projected["catalog"]["compositions"], [definition])
        self.assertEqual(projected["catalog"]["objects"], folded["catalog"]["objects"])
