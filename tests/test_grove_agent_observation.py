"""Presented-observation contracts independent of a host or world transport."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_world.grove_agent_observation import (AgentObservations, blueprint_model_input,
    companion_projection, execution_private_context, nearby_public_events)
from agent_world.grove_connector_errors import ConnectorError


INTERACT_SCHEMA = {
    "type": "object",
    "properties": {key: {"type": "integer"} for key in
        ("expected_player_version", "expected_target_version", "expected_item_version")},
    "required": ["expected_player_version", "expected_target_version", "expected_item_version"],
}


class AgentObservationsTests(unittest.TestCase):
    def test_public_rule_action_preserves_actual_actor_without_guessing_from_author(self):
        page = {"events": [
            {"event_id": "reset", "kind": "rule_action", "actor_role_id": "visitor", "seq": 4,
                "payload": {"entity_id": "counter", "action": "reset", "summary": "归零"}},
            {"event_id": "unseen", "kind": "rule_action", "actor_role_id": "elsewhere", "seq": 5,
                "payload": {"entity_id": "unseen", "action": "reset"}},
            {"event_id": "private", "kind": "move", "actor_role_id": "visitor", "seq": 6,
                "payload": {"x": 0, "y": 0}}]}
        events = nearby_public_events(page, {"x": 0, "y": 0}, [{"id": "counter", "created_by": "author"}])
        self.assertEqual([row["event_id"] for row in events], ["reset"])
        self.assertEqual(events[0]["actor_role_id"], "visitor")
        self.assertNotIn("created_by", events[0])

    def test_current_execution_context_does_not_reissue_historical_creation_intent(self):
        stored = {"intent": "喜欢探索", "preferences": ["简短回复"],
            "last_creation_request": "做个信号牌", "recent_user_requests": [
                {"text": "做个信号牌", "purpose": "creation"}, {"text": "去附近走走"}]}
        original = deepcopy(stored)
        context = execution_private_context(stored, "去附近走走")
        self.assertEqual(context["current_execution_goal"], "去附近走走")
        self.assertEqual(context["preferences"], original["preferences"])
        self.assertNotIn("last_creation_request", context)
        self.assertNotIn("recent_user_requests", context)
        self.assertEqual(context["projection"]["omitted_historical_request_count"], 2)
        self.assertEqual(stored, original)

    def test_actual_target_state_survives_action_and_checkpoint_projection_and_binds_only_presented_version(self):
        prior = self.observations.present("map.viewport", self.scene(), preflight=True)["observation_id"]
        actual = {"id": "target", "version": 24, "execution_domain": {"state": "settled", "last_applied_tick": 91},
            "creative": {"fields": {"signal": 0}, "world_tick": 92, "mechanical_observation": {
                "stored_state": {"angle": 3, "energy": 19}, "stored_output_is_history": True,
                "observed_tick": 92, "meaning": "Saved output is not realtime motion"}}}
        facts = {"player": {"id": "person", "version": 8}, "entities": [], "observed_at": 123,
            "action_targets": [{"entity_id": "target", "observed_at": 122, "inspection": {"entity": actual}},
                {"entity_id": "consumed", "observed_at": 122.5, "unavailable": {"code": "ResourceNotFound"}}],
            "action_targets_meaning": "Separate current reads",
            "task_action_summary": {"task_id": "task", "groups": [{"function": "grove.rule_action",
                "action": "fuel", "count": 6, "last_operation_id": "original-fuel", "last_commit_seq": 17,
                "last_confirmed_outcome": {"action_result": {"energy": 10}}}]}}
        action = self.observations.present("grove.interact", {"result": {"entity": {"id": "target", "version": 23}},
            "execution_readback": deepcopy(facts)}, base_id=prior, action_entity_ids=["target"])
        self.assertEqual(action["execution_readback"]["action_targets"], facts["action_targets"])
        self.assertEqual(action["execution_readback"]["task_action_summary"], facts["task_action_summary"])
        self.assertEqual(self.observations.get(action["observation_id"])["versions"]["entity"]["target"], 24)
        self.assertNotIn("consumed", self.observations.get(action["observation_id"])["versions"]["entity"])
        self.assertEqual(self.observations.get(prior)["versions"]["entity"]["target"], 22)
        checkpoint = self.observations.present("execution_checkpoint", {"status": "completed", "segment_finished": True,
            "execution_readback": deepcopy(facts)}, base_id=action["observation_id"])
        self.assertEqual(checkpoint["execution_readback"]["action_targets"], facts["action_targets"])
        self.assertEqual(checkpoint["execution_readback"]["task_action_summary"], facts["task_action_summary"])
        self.assertEqual(checkpoint["execution_readback"]["observed_at"], 123)
        self.assertEqual(self.observations.get(checkpoint["observation_id"])["versions"]["entity"]["target"], 24)

    def test_checkpoint_supplies_new_opaque_observation_with_current_mechanical_facts(self):
        old = self.observations.present("map.viewport", self.scene(), preflight=True)["observation_id"]
        facts = self.scene()
        facts["clock"] = {"sequence": 91}
        facts["entities"][0].update(version=23, creative={"mechanical_observation": {
            "role": "source", "angle_unit": "logical_turns", "stored_state": {"angle": 36, "energy": 4}}})
        result = self.observations.present("execution_checkpoint", {"status": "completed", "segment_finished": True,
            "execution_readback": facts}, base_id=old, action_entity_ids=["target"])
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

    def test_interaction_guard_comes_from_the_chosen_observation_not_a_later_read_or_model_number(self):
        scene = self.scene()
        scene["entities"][0]["interaction_revision"] = 12
        old = self.observations.present("map.viewport", scene, preflight=True)["observation_id"]
        schema = {"properties": {"expected_player_version": {}, "expected_target_version": {},
            "expected_target_interaction_revision": {}},
            "required": ["expected_player_version", "expected_target_version"]}
        latest = self.observations.present("grove.inspect", {
            "entity": {"id": "target", "version": 25, "interaction_revision": 25}}, base_id=old)["observation_id"]
        intent = {"action": "reset", "target_id": "target", "expected_target_version": 999,
            "expected_target_interaction_revision": 999}
        bound = self.observations.bind("grove.rule_action", intent, old, schema)
        self.assertEqual((bound["expected_target_version"], bound["expected_target_interaction_revision"]), (22, 12))
        fresh = self.observations.bind("grove.rule_action", intent, latest, schema)
        self.assertEqual((fresh["expected_target_version"], fresh["expected_target_interaction_revision"]), (25, 25))
        identity_only = self.observations.present("grove.inspect", {
            "entity": {"id": "target", "version": 26}}, base_id=latest)["observation_id"]
        legacy = self.observations.bind("grove.rule_action", intent, identity_only, schema)
        self.assertEqual(legacy["expected_target_version"], 26)
        self.assertNotIn("expected_target_interaction_revision", legacy)
        self.assertEqual(intent["expected_target_interaction_revision"], 999)

    def test_drop_item_shape_binds_the_same_presented_guard_and_legacy_does_not_invent_one(self):
        schema = deepcopy(INTERACT_SCHEMA)
        schema["required"] = ["expected_player_version", "expected_item_version"]
        schema["properties"].update(expected_target_interaction_revision={}, expected_item_interaction_revision={})
        scene = self.scene()
        scene["entities"][0]["interaction_revision"] = 12
        observation = self.observations.present("map.viewport", scene, preflight=True)["observation_id"]
        bound = self.observations.bind("grove.interact", {"action": "drop", "item_id": "target"}, observation, schema)
        self.assertEqual(bound["expected_item_version"], 22)
        self.assertEqual(bound["expected_item_interaction_revision"], 12)
        old = self.observations.present("map.viewport", self.scene(), preflight=True)["observation_id"]
        legacy = self.observations.bind("grove.interact", {"action": "drop", "item_id": "target"}, old, schema)
        self.assertNotIn("expected_item_interaction_revision", legacy)

    def test_material_lot_in_an_entity_field_is_diagnosed_only_from_presented_facts(self):
        observation = self.observations.present("grove.inspect", {
            "player": {"id": "person", "version": 7},
            "entity": {"id": "target", "version": 22},
            "action_options": [{"action": "transfer", "input": {"candidates": [
                {"id": "source", "version": 8, "lots": [{"id": "source:real", "quantity": 3}]}]}}]})["observation_id"]
        with self.assertRaises(ConnectorError) as wrong:
            self.observations.bind("grove.interact", {"action": "transfer", "target_id": "target",
                "item_id": "source:real", "lot_id": "source:real"}, observation, INTERACT_SCHEMA)
        self.assertEqual(wrong.exception.code, "ObservedVersionRequired")
        self.assertIn("是材料lot_id", str(wrong.exception))
        self.assertNotIn("source:real", self.observations.get(observation)["versions"]["entity"])
        self.assertNotIn("source", self.observations.get(observation)["versions"]["entity"])
        with self.assertRaises(ConnectorError) as unknown:
            self.observations.bind("grove.interact", {"action": "transfer", "target_id": "target",
                "item_id": "unknown:object"}, observation, INTERACT_SCHEMA)
        self.assertNotIn("是材料lot_id", str(unknown.exception))

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
        authored = {"type": "object", "properties": {"expected_heat_version": {"type": "integer"},
            "expected_target_interaction_revision": {"type": "integer"}},
            "required": ["expected_heat_version", "expected_target_interaction_revision"]}
        source = {"properties": {"expected_player_version": {}, "expected_target_interaction_revision": {}, "params": authored,
            "continuation": {"properties": {"observed_anchor_version": {}, "content_id": {}}}}}
        supplied = self.observations.tool_schema(source)
        self.assertNotIn("expected_player_version", supplied["properties"])
        self.assertNotIn("expected_target_interaction_revision", supplied["properties"])
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
