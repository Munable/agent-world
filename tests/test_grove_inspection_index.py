"""Default look facts, explicit action detail and immutable write admission."""
from copy import deepcopy
import json
import unittest

from agent_world.grove_agent_observation import AgentObservations, companion_projection, _companion_inspection_index
from agent_world.grove_connector_errors import ConnectorError


INTERACT_SCHEMA = {"properties": {key: {} for key in (
    "expected_player_version", "expected_target_version", "expected_item_version")},
    "required": ["expected_player_version", "expected_target_version", "expected_item_version"]}


class InspectionIndexTests(unittest.TestCase):
    def test_limit_projection_deduplicates_only_equal_fields_and_process_without_losing_domain_or_conditions(self):
        raw = self.inspection()
        fields = {"signal": 0}
        process = {"status": "waiting", "steps": 7, "observed_tick": 91}
        raw["entity"]["state"]["program"] = {"fields": deepcopy(fields), "process": deepcopy(process)}
        creative = raw["entity"]["creative"]
        creative.update(fields=deepcopy(fields), process=deepcopy(process), definition_ref="pinned-definition", type_id="sensor")
        domain = {"state": "calculating", "last_applied_tick": 90, "pending_target_tick": 91, "failure": None}
        raw["entity"]["execution_domain"] = deepcopy(domain)
        before = deepcopy(raw)
        index = _companion_inspection_index(raw)
        self.assertEqual(index["entity"]["creative"]["fields"], fields)
        self.assertEqual(index["entity"]["creative"]["process"], process)
        self.assertEqual(index["entity"]["execution_domain"], domain)
        self.assertEqual(index["entity"]["state"]["program"], {})
        expected = deepcopy(creative["automatic_rules"])
        expected[0]["condition"].pop("expression")
        self.assertEqual(index["entity"]["creative"]["automatic_rules"], expected)
        self.assertIn("state.program.fields (same values in entity.creative.fields)", index["projection"]["omitted_sections"])
        self.assertEqual(raw, before)
        raw["entity"]["state"]["program"]["fields"]["signal"] = 1
        unequal = _companion_inspection_index(raw)
        self.assertEqual(unequal["entity"]["state"]["program"]["fields"], {"signal": 1})
        self.assertEqual(unequal["entity"]["creative"]["fields"], fields)
        default = companion_projection("grove.inspect", before)
        self.assertEqual(default["entity"]["state"]["program"], {})
        self.assertEqual(default["entity"]["creative"]["fields"], fields)
        self.assertEqual(default["entity"]["creative"]["process"], process)
        observed = AgentObservations("person").present("execution_checkpoint", {"execution_readback": {
            "selected_inspection": before}})
        self.assertEqual(observed["execution_readback"]["selected_inspection"]["entity"]["execution_domain"], domain)

    def inspection(self):
        return {"player": {"id": "person", "version": 5}, "entity": {
            "id": "target", "version": 7, "name": "Public vessel", "owner": None,
            "physical_parent": "support", "location": {"x": 3, "y": 2},
            "components": {"container": {"capacity": 32}, "wettable": {"absorption": 2}},
            "composition": {"material": "stone", "construction": "vessel", "size": 2},
            "materials": {"body": "stone"}, "matter": [{"id": "body-lot", "quantity": 12}],
            "simulation": {"can_float": False, "buoyancy": 0, "supported": 40, "limit": 120},
            "state": {"open": True, "wetness": 0, "water": 5},
            "creative": {"automatic_rules": [{"condition": {
                "expression": {"field": "signal"}, "available": False, "unknown_inputs": ["signal"]}}]},
            "new_fact": {"declared": False}},
            "contents": [{"id": "stored", "version": 4, "owner": None, "state": {"intact": True}}],
            "available_actions": ["store"], "explanation": {"physical": "Actual current values"},
            "new_inspection_fact": {"actual": 1}, "action_options": [{
                "action": "store", "function_id": "grove.interact", "available": True,
                "reason": None, "label": "Store", "preconditions": ["Nearby and open"],
                "condition_observation": {"available": True, "source": "current_read"},
                "input_schema": {"properties": {"item_id": {"type": "string"}}},
                "arguments": {"action": "store", "target_id": "target"},
                "input": {"candidates": [{"id": "source", "name": "Timber", "kind": "entity",
                    "version": 9, "preview": {"version_fingerprint": "a" * 64,
                        "physics": {"known": True, "overloaded": False}}}]}}]}

    def test_default_small_read_keeps_selected_facts_and_child_read_paths_without_expanding_actions(self):
        raw = self.inspection()
        raw["action_options"][0]["input"]["candidates"][0]["preview"]["affected_structures"] = [
            {"id": "support", "current_state": {"diagnostics": "x" * 2000}}]
        payload = {"world_id": "shared-grove", "function_version": 13, "snapshot_seq": 45,
                   "transport_metadata": {"actual": True}, "result": raw}
        original = deepcopy(payload)
        delivered = companion_projection("grove.inspect", payload)
        actual = delivered["result"]
        self.assertEqual(payload, original)
        for field in ("entity", "player", "explanation", "new_inspection_fact", "available_actions"):
            self.assertEqual(actual[field], raw[field])
        self.assertEqual(actual["contents"], [{"id": "stored", "owner": None,
            "read_request": {"tool": "grove_inspect", "arguments": {"entity_id": "stored"}}}])
        self.assertEqual(actual["contents_projection"]["scope"], "visible_child_index")
        self.assertEqual(actual["contents_projection"]["visible_child_count"], 1)
        for field in ("state", "version", "matter", "action_options"):
            self.assertNotIn(field, actual["contents"][0])
        for field in ("world_id", "function_version", "snapshot_seq", "transport_metadata"):
            self.assertEqual(delivered[field], payload[field])
        self.assertTrue(actual["projection"]["complete_object_state"])
        self.assertFalse(actual["projection"]["complete_inspection"])
        option = actual["action_options"][0]
        self.assertTrue(option["available"])
        self.assertEqual(option["condition_observation"], raw["action_options"][0]["condition_observation"])
        self.assertNotIn("input", option)
        self.assertNotIn("input_schema", option)
        self.assertNotIn("arguments", option)
        self.assertEqual(option["candidates_index"], [{"id": "source", "name": "Timber", "kind": "entity",
            "read_request": {"tool": "grove_inspect", "arguments": {
                "entity_id": "target", "action": "store", "candidate_id": "source"}}}])
        self.assertEqual(companion_projection("grove.inspect", delivered), delivered)
        self.assertLess(len(json.dumps(delivered)), len(json.dumps(payload)))
        child = companion_projection("grove.inspect", {"entity": raw["contents"][0]})
        self.assertEqual(child["entity"], raw["contents"][0])
        self.assertEqual(payload, original)

    def test_default_index_never_binds_an_unpresented_version_or_preview(self):
        observations = AgentObservations("person")
        default = observations.present("grove.inspect", self.inspection())
        bundle = observations.get(default["observation_id"])
        self.assertNotIn("source", bundle["versions"]["entity"])
        self.assertFalse(bundle.get("combination_previews"))
        # Even a separately known source version does not admit a hidden preview.
        base = observations.present("map.viewport", {"player": {"id": "person", "version": 5},
            "entities": [{"id": "source", "version": 9}]})
        indexed = observations.present("grove.inspect", self.inspection(), base_id=base["observation_id"])
        with self.assertRaises(ConnectorError) as denied:
            observations.bind("grove.interact", {"action": "store", "target_id": "target", "item_id": "source"},
                              indexed["observation_id"], INTERACT_SCHEMA)
        self.assertEqual(denied.exception.code, "ObservedPreviewRequired")

    def test_explicit_action_or_candidate_preserves_exact_preview_and_binding(self):
        for selection in ({"scope": "selected_declared_action", "action": "store", "expanded_actions": ["store"]},
                          {"scope": "selected_declared_action", "action": "store", "expanded_actions": ["store"],
                           "candidate_selection": {"candidate_id": "source", "expanded_candidate_ids": ["source"]}}):
            with self.subTest(selection=selection):
                observations = AgentObservations("person")
                inspection = self.inspection()
                inspection["action_options_projection"] = selection
                supplied = observations.present("grove.inspect", inspection)
                self.assertEqual(supplied["action_options"][0]["input"], inspection["action_options"][0]["input"])
                bound = observations.bind("grove.interact", {"action": "store", "target_id": "target", "item_id": "source"},
                                          supplied["observation_id"], INTERACT_SCHEMA)
                self.assertEqual((bound["expected_player_version"], bound["expected_target_version"],
                                  bound["expected_item_version"]), (5, 7, 9))
                self.assertEqual(bound["expected_combination_fingerprint"], "a" * 64)
                with self.assertRaises(ConnectorError) as mismatch:
                    observations.bind("grove.interact", {"action": "store", "target_id": "target", "item_id": "source",
                        "expected_combination_fingerprint": "b" * 64}, supplied["observation_id"], INTERACT_SCHEMA)
                self.assertEqual(mismatch.exception.code, "ObservedPreviewMismatch")

    def test_real_choice_groups_keep_supported_re_read_routes_without_lot_bindings(self):
        inspection = self.inspection()
        inspection["action_options"] = [{"action": "offer", "function_id": "grove.rule_action", "available": True,
            "choices": {"items": [{"id": "item", "version": 8, "name": "Product"}],
                        "others": [{"id": "other-person", "version": 6, "kind": "role"}]}},
            {"action": "fabricate", "function_id": "grove.fabricate", "choices": {
                "sources": [{"id": "raw-source", "version": 9, "lots": [{"id": "material-lot", "quantity_u": 2}]}]}}]
        original = deepcopy(inspection)
        observations = AgentObservations("person")
        supplied = observations.present("grove.inspect", inspection)
        offer, fabricate = supplied["action_options"]
        self.assertEqual([row["id"] for row in offer["candidates_index"]], ["item", "other-person"])
        self.assertEqual(offer["candidates_index"][1]["read_request"]["arguments"], {
            "entity_id": "target", "action": "offer", "candidate_id": "other-person"})
        self.assertEqual(fabricate["sources_index"], [{"id": "raw-source", "read_request": {
            "tool": "grove_inspect", "arguments": {"entity_id": "target", "action": "fabricate"}}}])
        bundle = observations.get(supplied["observation_id"])
        self.assertEqual(bundle["versions"]["entity"], {"target": 7})
        self.assertEqual(bundle["versions"]["player"], {"person": 5})
        self.assertEqual(supplied["entity"], inspection["entity"])
        child_index = supplied["contents"][0]
        self.assertEqual(child_index["read_request"], {
            "tool": "grove_inspect", "arguments": {"entity_id": "stored"}})
        for field in ("version", "state", "matter", "action_options"):
            self.assertNotIn(field, child_index)
        child_read = observations.present("grove.inspect", {"entity": inspection["contents"][0]},
            base_id=supplied["observation_id"])
        self.assertEqual(child_read["entity"], inspection["contents"][0])
        self.assertEqual(observations.get(child_read["observation_id"])["versions"]["entity"],
            {"target": 7, "stored": 4})
        self.assertEqual(observations.get(supplied["observation_id"]), bundle)
        self.assertEqual(inspection, original)

    def test_new_default_state_cannot_refresh_an_older_explicit_preview(self):
        observations = AgentObservations("person")
        inspection = self.inspection()
        inspection["action_options_projection"] = {"scope": "selected_declared_action", "action": "store"}
        explicit = observations.present("grove.inspect", inspection)
        original = observations.get(explicit["observation_id"])
        inspection.pop("action_options_projection")
        inspection["entity"]["version"] = 8
        inspection["action_options"][0]["input"]["candidates"][0]["preview"]["version_fingerprint"] = "b" * 64
        indexed = observations.present("grove.inspect", inspection, base_id=explicit["observation_id"])
        with self.assertRaises(ConnectorError) as denied:
            observations.bind("grove.interact", {"action": "store", "target_id": "target", "item_id": "source"},
                              indexed["observation_id"], INTERACT_SCHEMA)
        self.assertEqual(denied.exception.code, "ObservedPreviewRequired")
        self.assertEqual(observations.get(explicit["observation_id"]), original)

    def test_unexpanded_inspection_does_not_become_permission_during_preflight(self):
        observations = AgentObservations("person")
        selected = companion_projection("grove.inspect", self.inspection())
        supplied = observations.present("map.viewport", {"selected_inspection": selected}, preflight=True)
        self.assertTrue(supplied["selected_inspection"]["projection"]["complete_object_state"])
        self.assertNotIn("input", supplied["selected_inspection"]["action_options"][0])
        self.assertFalse(observations.get(supplied["observation_id"]).get("combination_previews"))


if __name__ == "__main__":
    unittest.main()
