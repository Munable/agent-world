"""Authorized runtime summaries preserve the world's evidence boundaries."""
from copy import deepcopy
import unittest

from agent_world.grove_agent_observation import AgentObservations, _companion_scene_index, companion_projection


class GroveAgentRuntimeObservationTests(unittest.TestCase):
    def test_runtime_failure_location_survives_every_first_look(self):
        diagnostic = {"code": "field_bounds", "path": "/types/device/rules/0/effects/0",
            "type_id": "device", "rule_id": "advance", "phase": "reaction", "effect_index": 0,
            "field": "position", "attempted_value": 21, "min": 0, "max": 20, "logical_step": 51}
        entity = {"id": "device", "version": 8, "creative": {"definition_ref": "fixed", "type_id": "device",
            "process": {"status": "error", "steps": 1, "error": "Field position exceeded max",
                "error_diagnostic": diagnostic}}}
        source = {"entities": [entity]}
        original = deepcopy(source)
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                process = _companion_scene_index(source, preflight=preflight)["entities"][0]["creative"]["process"]
                self.assertEqual(process["error_diagnostic"], diagnostic)
        observations = AgentObservations("person")
        for function in ("grove.interact", "execution_checkpoint"):
            with self.subTest(function=function):
                delivered = observations.present(function, {"result": {"entity": entity}, "execution_readback": {
                    "selected_inspection": {"entity": entity}, "entities": [entity]}})
                process = delivered["execution_readback"]["selected_inspection"]["entity"]["creative"]["process"]
                self.assertEqual(process["error_diagnostic"], diagnostic)
        self.assertEqual(source, original)

    def test_missing_exact_runtime_diagnostic_is_explicit_and_not_guessed(self):
        for process in ({"status": "error", "error": "Rule failed", "error_diagnostic_omitted": "capacity"},
                        {"status": "error", "error": "Rule failed"}):
            with self.subTest(process=process):
                summary = _companion_scene_index({"entities": [{"id": "failed", "creative": {
                    "process": process}}]}, preflight=True)["entities"][0]["creative"]["process"]
                self.assertNotIn("error_diagnostic", summary)
                if "error_diagnostic_omitted" in process:
                    self.assertEqual(summary["error_diagnostic_omitted"], "capacity")
                else:
                    self.assertIn("not saved", summary["error_diagnostic_projection"])
                self.assertNotIn("rule_id", summary)

    def test_current_port_bindings_survive_equal_program_deduplication(self):
        bindings = {"input": "nearby-other-device"}
        connections = [{"from": "shaft", "to": "output"}]
        contract = {"status": "active", "beneficiary": "other-person"}
        current = {"port_bindings": bindings, "connections": connections, "contract": contract}
        entity = {"id": "connected", "version": 5, "state": {"program": deepcopy(current)},
            "creative": {"definition_ref": "fixed", "type_id": "connected", **deepcopy(current)}}
        payload = {"result": {"entity": entity}}
        delivered = companion_projection("grove.interact", payload)["result"]["entity"]
        self.assertEqual(delivered["state"]["program"], {})
        for key, value in current.items():
            self.assertEqual(delivered["creative"][key], value)
        self.assertEqual(payload["result"]["entity"], entity)

    def test_mechanical_projection_separates_current_inputs_and_saved_outputs(self):
        inputs = {"energy": 10, "enabled": True, "drive_request": {"applies_from_step": 101}, "observed_tick": 100}
        outputs = {"values": {"speed": 0, "angle": 360, "status": "no_energy"},
            "last_output_change": {"step": 99, "historical": True}, "observed_tick": 100}
        bare = {"energy": 10, "enabled": True, "speed": 0, "angle": 360, "status": "no_energy"}
        entity = {"id": "source", "version": 8, "mechanical": deepcopy(bare),
            "state": {"program": {"mechanical": deepcopy(bare)}},
            "creative": {"definition_ref": "fixed", "type_id": "source", "mechanical": deepcopy(bare),
                "mechanical_observation": {"current_inputs": inputs, "saved_outputs": outputs,
                    "stored_state": deepcopy(bare), "last_solved": {"step": 99, "historical": True},
                    "drive_request": inputs["drive_request"]}}}
        raw = deepcopy(entity)
        inspection = companion_projection("grove.inspect", {"entity": entity})["entity"]
        self.assertNotIn("mechanical", inspection)
        self.assertNotIn("mechanical", inspection["state"]["program"])
        self.assertNotIn("mechanical", inspection["creative"])
        observed = inspection["creative"]["mechanical_observation"]
        self.assertEqual(observed["current_inputs"], inputs)
        self.assertEqual(observed["saved_outputs"], outputs)
        self.assertNotIn("stored_state", observed)
        self.assertNotIn("drive_request", observed)
        self.assertNotIn("last_solved", observed)
        self.assertEqual(entity, raw)

    def test_runtime_summary_survives_repeated_scene_compaction(self):
        source = {"inventory": [{"id": "carried", "version": 7, "owner": "role_a",
            "creative": {"definition_ref": "immutable", "type_id": "motor",
                "automatic_rule_count": 1, "explicit_action_count": 2,
                "automatic_rules": [{"rule_id": "sense", "condition": {"known": True}}],
                "deployment": {"publicly_placed": False, "admitted": True,
                    "ordinary_rules_enabled": True, "public_event_input_available": False,
                    "mechanical_solver_eligible": False, "mechanical_blockers": ["no_public_location"]},
                "process": {"status": "waiting", "steps": 3, "last_tick": 12, "observed_tick": 40,
                    "status_meaning": "保存的过程标记，不代表整个装置当前运行。",
                    "last_tick_meaning": "过程记录时刻，不代表最近机械求解。",
                    "blocked_reason": "unavailable_fact_or_port",
                    "blocked_rules": [{"rule_id": "sense", "phase": "reaction", "reason": "unavailable"}],
                    "error": "saved error",
                    "last_reaction": {"source_last_tick": 12, "historical": True,
                        "current_production": False, "outputs": {"wood": 100}}},
                "mechanical_observation": {"role": "source",
                    "stored_state": {"speed": 5, "position": 9, "status": "running"},
                    "last_solved": {"step": 17, "historical": True},
                    "drive_request": {"enabled": True, "requested_step": 18, "applies_from_step": 19,
                        "source": {"kind": "automatic_rule", "rule_id": "sense", "phase": "reaction"}},
                    "solver_eligible": False, "stored_output_is_history": True,
                    "pending_next_step": False, "observed_tick": 40}},
            "simulation": {"contact": {"candidate_count": 1,
                "process": {"status": "running", "steps": 2}, "process_is_history": True}}}]}
        original = deepcopy(source)
        index = _companion_scene_index(source)
        self.assertEqual(index, _companion_scene_index(index))
        self.assertEqual(source, original)
        row = index["inventory"][0]
        self.assertEqual(row["read_request"], {"tool": "grove_inspect", "arguments": {"entity_id": "carried"}})
        self.assertEqual(row["version"], 7)
        self.assertNotIn("automatic_rules", row["creative"])
        self.assertEqual(row["creative"]["deployment"]["ordinary_rules_enabled"], True)
        mechanical = row["creative"]["mechanical_observation"]
        self.assertEqual(mechanical["stored_state"]["speed"], 5)
        self.assertEqual(mechanical["solver_eligible"], False)
        self.assertEqual(mechanical["drive_request"]["source"]["rule_id"], "sense")
        self.assertNotIn("outputs", row["creative"]["process"]["last_reaction"])
        self.assertEqual(row["creative"]["process"]["blocked_rules"][0]["rule_id"], "sense")
        self.assertEqual(row["creative"]["process"]["error"], "saved error")
        for key in ("status", "last_tick", "status_meaning", "last_tick_meaning"):
            self.assertEqual(row["creative"]["process"][key], source["inventory"][0]["creative"]["process"][key])
        self.assertEqual(row["contact"]["steps"], 2)

    def test_hidden_or_unprojected_program_never_supplies_runtime_facts(self):
        scene = {"entities": [{"id": "closed", "creative": {
            "definition_ref": "immutable", "type_id": "motor", "hidden": True,
            "automatic_rule_count": 4, "deployment": {"publicly_placed": True},
            "mechanical_observation": {"stored_state": {"speed": 99}}}},
            {"id": "raw", "state": {"program": {"automatic_rule_count": 8,
                "mechanical_observation": {"stored_state": {"speed": 77}}}}}]}
        rows = _companion_scene_index(scene)["entities"]
        self.assertEqual(rows[0]["creative"], {"definition_ref": "immutable", "type_id": "motor", "hidden": True})
        self.assertNotIn("creative", rows[1])
        self.assertEqual(rows[1]["read_request"]["arguments"]["entity_id"], "raw")

    def test_unknown_markers_and_missing_runtime_fields_are_not_invented(self):
        creative = {"definition_ref": "immutable", "type_id": "motor", "mechanical_observation": {
            "last_solved": None, "drive_request": None, "stored_state": {"speed": 0}}}
        row = _companion_scene_index({"entities": [{"id": "motor", "creative": creative}]})["entities"][0]
        mechanical = row["creative"]["mechanical_observation"]
        self.assertIsNone(mechanical["last_solved"])
        self.assertIsNone(mechanical["drive_request"])
        self.assertNotIn("solver_eligible", mechanical)
        self.assertNotIn("automatic_rule_count", row["creative"])

    def test_handbook_keeps_authoritative_execution_model_and_read_entry_points(self):
        model = {"automatic_rules": "World-supplied automatic semantics.",
            "readback": {"snapshot_is_execution": False}}
        source = {"result": {"title": "Grove", "execution_model": model}}
        result = companion_projection("grove.handbook", source)["result"]
        self.assertEqual(result["execution_model"], model)
        result["execution_model"]["readback"]["snapshot_is_execution"] = True
        self.assertEqual(source["result"]["execution_model"]["readback"]["snapshot_is_execution"], False)
        language = result["handbook_projection"]["read_creation_rules"]["program_language"]
        for tool in ("grove_rule_catalog", "grove_definition_list", "grove_definition_read", "grove_fabrication_options"):
            self.assertIn(tool, language)


if __name__ == "__main__":
    unittest.main()
