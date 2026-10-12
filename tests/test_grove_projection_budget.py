"""Everyday observations stay small without manufacturing action authority."""
from copy import deepcopy
import unittest

from agent_world.grove_agent_observation import AgentObservations, companion_projection, _companion_inspection_index
from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_native_history import TOOL_SCHEMA as HISTORY_SCHEMA
from jsonschema import Draft202012Validator


class GroveProjectionBudgetTests(unittest.TestCase):
    def scene(self):
        return {"player": {"id": "person", "version": 4, "x": 0, "y": 0},
            "clock": {"sequence": 100}, "bounds": {"min_x": -8, "min_y": -8, "max_x": 8, "max_y": 8},
            "chunks": [{"cx": x, "cy": y, "tiles": ["grass", "water", "rock", "dirt"] * 64}
                       for x, y in ((-1, -1), (0, -1), (-1, 0), (0, 0))],
            "entities": [{"id": f"public-{i}", "version": i + 1, "name": "Public material",
                "owner": None, "x": i % 8, "y": i // 8, "material": "wood",
                "matter": [{"id": f"unpresented-lot-{i}", "material": "wood", "slot": "body", "quantity": i + 1}],
                "state": {"wetness": 0, "program": {"declaration": "x" * 5000}},
                "simulation": {"large_definition": "x" * 5000}} for i in range(80)],
            "inventory": [{"id": f"own-{i}", "version": i + 1, "name": "Raw cloth", "owner": "person",
                "matter": [{"id": f"own-lot-{i}", "material": "cloth", "slot": "body", "quantity": 5}]}
                for i in range(12)], "inventory_total": 15, "inventory_next": "own-11"}

    def test_dense_map_and_preflight_share_budget_and_only_bind_presented_rows(self):
        raw = self.scene()
        original = deepcopy(raw)
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                observations = AgentObservations("person")
                delivered = observations.present("map.viewport", raw, preflight=preflight)
                self.assertLessEqual(len(observations.serialize(delivered).encode()), 8192)
                self.assertTrue(delivered["fact_budget"]["omitted_rows"])
                self.assertFalse(delivered["fact_budget"]["mandatory_facts_exceed_target"])
                self.assertEqual(len(delivered["chunks"]), 4)
                self.assertEqual(delivered["chunks"][0]["terrain"]["origin"], {"x": -16, "y": -16})
                presented_ids = {row["id"] for row in delivered["entities"] + delivered["inventory"]}
                versions = observations.get(delivered["observation_id"])["versions"]["entity"]
                self.assertEqual(set(versions), presented_ids)
                self.assertNotIn("public-79", versions)
                self.assertFalse(observations.get(delivered["observation_id"]).get("presented_lot_ids"))
                for row in delivered["inventory"]:
                    self.assertEqual(row["matter"][0]["quantity"], 5)
                if len(delivered["inventory"]) < 12:
                    self.assertEqual(delivered["inventory_next"], delivered["inventory"][-1]["id"])
        self.assertEqual(raw, original)

    def test_complete_selected_preview_can_exceed_daily_target_without_losing_binding(self):
        inspection = {"player": {"id": "person", "version": 4}, "entity": {"id": "vessel", "version": 8},
            "action_options_projection": {"scope": "selected_declared_action", "action": "store"},
            "action_options": [{"action": "store", "input": {"candidates": [{"id": "source", "version": 9,
                "preview": {"version_fingerprint": "a" * 64,
                    "physics": {"conditions": "trusted exact facts " * 800}}}]}}]}
        observations = AgentObservations("person")
        delivered = observations.present("grove.inspect", inspection)
        self.assertGreater(len(observations.serialize(delivered).encode()), 8192)
        self.assertEqual(delivered["action_options"][0]["input"], inspection["action_options"][0]["input"])
        schema = {"properties": {key: {} for key in
            ("expected_player_version", "expected_target_version", "expected_item_version")}}
        bound = observations.bind("grove.interact", {"action": "store", "target_id": "vessel", "item_id": "source"},
                                  delivered["observation_id"], schema)
        self.assertEqual(bound["expected_combination_fingerprint"], "a" * 64)
        self.assertEqual(bound["expected_item_version"], 9)

    def test_preflight_no_longer_bypasses_native_hard_boundary(self):
        observations = AgentObservations("person")
        inspection = {"entity": {"id": "vessel", "version": 8},
            "action_options_projection": {"scope": "selected_declared_action", "candidate_selection": {"candidate_id": "source"}},
            "action_options": [{"action": "store", "input": {"candidates": [{"id": "source", "version": 9,
                "preview": {"version_fingerprint": "a" * 64, "facts": "x" * 80000}}]}}]}
        with self.assertRaises(ConnectorError) as rejected:
            observations.present("map.viewport", {"selected_inspection": inspection}, preflight=True)
        self.assertEqual(rejected.exception.code, "ToolOutputTooLarge")
        self.assertEqual(observations.sequence, 0)

    def test_selected_action_without_candidate_cannot_be_silently_downgraded(self):
        observations = AgentObservations("person")
        inspection = {"entity": {"id": "vessel", "version": 8},
            "action_options_projection": {"scope": "selected_declared_action", "action": "store"},
            "action_options": [{"action": "store", "input": {"candidates": [{"id": "source", "version": 9,
                "preview": {"facts": "x" * 80000}}]}}]}
        with self.assertRaises(ConnectorError) as rejected:
            observations.present("grove.inspect", inspection)
        self.assertEqual(rejected.exception.code, "ToolOutputTooLarge")
        self.assertEqual(observations.sequence, 0)

    def test_soft_inspect_target_does_not_discard_current_authorized_facts(self):
        entity = {"id": "full", "version": 8, "components": {"container": {"capacity": 100}},
            "new_current_fact": {"known": True, "values": "actual values " * 800}}
        inspection = {"entity": entity, "action_options": [{"action": "store", "available": False,
            "reason": "Not currently within reach"}]}
        delivered = AgentObservations("person").present("grove.inspect", inspection)
        self.assertEqual(delivered["entity"], entity)
        self.assertTrue(delivered["projection"]["complete_object_state"])
        self.assertTrue(delivered["fact_budget"]["mandatory_facts_exceed_target"])
        self.assertEqual(delivered["action_options"][0]["reason"], "Not currently within reach")

    def literal_conditions(self):
        conditions = [{"expression": literal, "known": True, "satisfied": literal,
            "facts": [], "checks": []} for literal in (True, False)]
        rules = [{"rule_id": f"constant-{index}", "phase": "after", "condition": deepcopy(condition),
            "drive_inputs": [{"field": "enabled", "observation": deepcopy(condition)}]}
            for index, condition in enumerate(conditions)]
        rules.append({"rule_id": "conditional", "phase": "reaction", "condition": {
            "expression": {"and": [{"field": "signal"}] * 800},
            "known": False, "satisfied": None, "unknown_inputs": ["signal"],
            "facts": [{"path": "fields.signal", "actual": None}], "checks": [{"known": False}]}})
        return {"player": {"id": "person", "version": 4}, "entity": {
            "id": "literal-target", "version": 8, "state": {"wetness": 0},
            "creative": {"definition_ref": "pinned-definition", "type_id": "device",
                "fields": {"enabled": False}, "automatic_rules": rules}},
            "action_options": [{"action": "store", "available": False, "reason": "Out of reach",
                "input": {"candidates": [{"id": "hidden-source", "version": 9,
                    "preview": {"version_fingerprint": "a" * 64}}]}}]}

    def assert_literal_conditions(self, inspection, original):
        rules = inspection["entity"]["creative"]["automatic_rules"]
        actual = original["entity"]["creative"]["automatic_rules"]
        self.assertEqual(rules[:2], actual[:2])
        expected = deepcopy(actual[2])
        expected["condition"].pop("expression")
        self.assertEqual(rules[2], expected)
        projection = inspection["entity"]["creative"]["automatic_rules_expression_projection"]
        self.assertEqual(projection["read_request"], {"tool": "grove_definition_read",
            "arguments": {"definition_ref": "pinned-definition"}})

    def test_compact_inspection_keeps_actual_boolean_literals_and_unknown_verdicts(self):
        original = self.literal_conditions()
        before = deepcopy(original)
        for preserve_facts in (False, True):
            with self.subTest(preserve_facts=preserve_facts):
                indexed = _companion_inspection_index(original, preserve_facts=preserve_facts,
                    compact_expressions=True)
                self.assert_literal_conditions(indexed, original)
        self.assertEqual(original, before)

    def test_soft_inspect_and_selected_readback_keep_literals_without_hidden_preview_authority(self):
        original = self.literal_conditions()
        before = deepcopy(original)
        cases = [("grove.inspect", original, False),
            ("map.viewport", {"selected_inspection": original}, False),
            ("map.viewport", {"selected_inspection": original}, True),
            ("execution_checkpoint", {"execution_readback": {"selected_inspection": original}}, False)]
        for function, payload, preflight in cases:
            with self.subTest(function=function, preflight=preflight):
                observations = AgentObservations("person")
                delivered = observations.present(function, payload, preflight=preflight)
                inspection = (delivered if function == "grove.inspect" else
                    delivered.get("execution_readback", delivered)["selected_inspection"])
                self.assert_literal_conditions(inspection, original)
                bundle = observations.get(delivered["observation_id"])
                self.assertEqual(bundle["versions"]["entity"], {"literal-target": 8})
                self.assertFalse(bundle.get("combination_previews"))
                self.assertNotIn("hidden-source", bundle["versions"]["entity"])
        self.assertEqual(original, before)

    def test_literal_only_rules_need_no_definition_omission_marker(self):
        original = self.literal_conditions()
        original["entity"]["creative"]["automatic_rules"].pop()
        indexed = _companion_inspection_index(original, compact_expressions=True)
        self.assertEqual(indexed["entity"]["creative"]["automatic_rules"],
            original["entity"]["creative"]["automatic_rules"])
        self.assertNotIn("automatic_rules_expression_projection", indexed["entity"]["creative"])

    def selected_scene(self, *, fact_size=16000, discovery_name_size=0):
        scene = self.scene()
        scene["entities"] = scene["entities"][:12]
        for index, row in enumerate(scene["entities"]):
            row["name"] = f"Actual object {index}" + "n" * discovery_name_size
            if index >= 9:
                row["created_by"] = f"other-{index}"
                row["creative"] = {"definition_ref": f"definition-{index}", "type_id": "device",
                    "fields": {"private_unexpanded": 12}}
        selected = {"entity": {"id": "selected", "version": 90,
            "matter": [{"id": "selected-lot", "material": "water", "quantity": 7}],
            "current_authorized_fact": "x" * fact_size},
            "action_options_projection": {"scope": "selected_declared_action", "action": "store"},
            "action_options": [{"action": "store", "available": False, "reason": "No actual compatible source"}]}
        scene["selected_inspection"] = selected
        scene["action_targets"] = [{"entity_id": "selected", "inspection": deepcopy(selected), "observed_at": 100}]
        scene["task_action_summary"] = {"confirmed_count": 1,
            "coverage": {"receipt_history_complete": False, "unknown_count": 1}}
        return scene

    def test_selected_facts_over_soft_target_keep_bounded_current_discovery_without_action_authority(self):
        raw = self.selected_scene()
        original = deepcopy(raw)
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                observations = AgentObservations("person")
                delivered = observations.present("map.viewport", raw, preflight=preflight)
                self.assertGreater(len(observations.serialize(delivered).encode()), 8192)
                self.assertTrue(delivered["fact_budget"]["mandatory_facts_exceed_target"])
                self.assertEqual(delivered["entities"], [])
                self.assertEqual(delivered["selected_inspection"], raw["selected_inspection"])
                self.assertEqual(delivered["action_targets"], raw["action_targets"])
                self.assertEqual(delivered["task_action_summary"], raw["task_action_summary"])
                entries = delivered["nearby_discovery"]
                self.assertEqual([row["id"] for row in entries],
                    ["public-9", "public-10", "public-11", "public-0", "public-1", "public-2"])
                projection = delivered["nearby_discovery_projection"]
                self.assertEqual((projection["authorized_entity_count"], projection["retained_count"],
                    projection["omitted_count"], projection["hard_limit_omitted_count"]), (12, 6, 6, 0))
                actual_by_id = {row["id"]: row for row in raw["entities"]}
                versions = observations.get(delivered["observation_id"])["versions"]["entity"]
                for entry in entries:
                    actual = actual_by_id[entry["id"]]
                    for key in ("name", "x", "y", "created_by"):
                        if key in actual:
                            self.assertEqual(entry[key], actual[key])
                    self.assertNotIn("version", entry)
                    self.assertNotIn("matter", entry)
                    self.assertNotIn("fields", entry.get("creative", {}))
                    self.assertNotIn(entry["id"], versions)
                    self.assertEqual(entry["read_request"], {"tool": "grove_inspect",
                        "arguments": {"entity_id": entry["id"]}})
                self.assertFalse(observations.get(delivered["observation_id"]).get("combination_previews"))
                with self.assertRaises(ConnectorError) as missing:
                    observations.bind("grove.interact", {"action": "pickup", "target_id": entries[0]["id"]},
                        delivered["observation_id"], {"properties": {"expected_target_version": {}}})
                self.assertEqual(missing.exception.code, "ObservedVersionRequired")
        self.assertEqual(raw, original)

    def test_discovery_entries_do_not_invent_absent_positions_or_authors(self):
        raw = self.selected_scene()
        raw["entities"] = [{"id": "known-id", "name": "Actual name", "version": 10,
            "owner": None, "creative": {"definition_ref": "actual-ref", "type_id": "actual-type"}}]
        delivered = AgentObservations("person").present("map.viewport", raw)
        entry = delivered["nearby_discovery"][0]
        for key in ("x", "y", "location", "created_by", "version"):
            self.assertNotIn(key, entry)
        self.assertEqual(entry["creative"], raw["entities"][0]["creative"])

    def test_discovery_floor_uses_latest_readback_without_two_parallel_world_indexes(self):
        raw = {"result": self.selected_scene(), "execution_readback": {"entities": [
            {"id": "later-object", "name": "Later actual name",
             "owner": None, "x": 7, "y": 3, "version": 20, "created_by": "actual-author"}]}}
        delivered = AgentObservations("person").present("map.viewport", raw, preflight=True)
        self.assertNotIn("nearby_discovery", delivered["result"])
        self.assertEqual([row["id"] for row in delivered["execution_readback"]["nearby_discovery"]],
            ["later-object"])

    def test_hard_limit_sheds_discovery_entries_before_exact_selected_facts(self):
        raw = self.selected_scene(fact_size=20000, discovery_name_size=1400)
        raw["chunks"] = []
        observations = AgentObservations("person")
        delivered = observations.present("map.viewport", raw, preflight=True)
        self.assertLessEqual(len(observations.serialize(delivered).encode()), 48 * 1024)
        self.assertEqual(delivered["selected_inspection"], raw["selected_inspection"])
        self.assertEqual(delivered["action_targets"], raw["action_targets"])
        projection = delivered["nearby_discovery_projection"]
        retained = len(delivered["nearby_discovery"])
        self.assertLess(retained, 6)
        self.assertEqual(projection["retained_count"], retained)
        self.assertEqual(projection["omitted_count"], 12 - retained)
        self.assertEqual(projection["hard_limit_omitted_count"], 6 - retained)

    def test_discovery_cannot_hide_oversized_selected_facts_or_bind_a_failed_observation(self):
        raw = self.selected_scene(fact_size=30000)
        observations = AgentObservations("person")
        with self.assertRaises(ConnectorError) as rejected:
            observations.present("map.viewport", raw, preflight=True)
        self.assertEqual(rejected.exception.code, "ToolOutputTooLarge")
        self.assertEqual(observations.sequence, 0)

    def test_current_visible_names_survive_soft_budget_without_locations_or_write_authority(self):
        raw = self.selected_scene()
        raw["players"] = [{"id": f"other-{index}", "name": f"Actual person {index}",
            "x": index, "y": 1, "version": 20 + index, "unprovided_profile": "not copied"}
            for index in range(6)]
        original = deepcopy(raw)
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                observations = AgentObservations("person")
                delivered = observations.present("map.viewport", raw, preflight=preflight)
                self.assertEqual(delivered["players"], [])
                names = delivered["visible_player_names"]
                self.assertEqual(names, [{"id": row["id"], "name": row["name"]} for row in raw["players"][:4]])
                projection = delivered["visible_player_names_projection"]
                self.assertEqual((projection["authorized_player_count"], projection["retained_count"],
                    projection["omitted_count"], projection["hard_limit_omitted_count"]), (6, 4, 2, 0))
                self.assertEqual(delivered["fact_budget"]["omitted_rows"]["players"], 6)
                self.assertEqual(delivered["selected_inspection"], raw["selected_inspection"])
                self.assertEqual(delivered["action_targets"], raw["action_targets"])
                self.assertEqual(delivered["task_action_summary"], raw["task_action_summary"])
                self.assertEqual(len(delivered["nearby_discovery"]), 6)
                bundle = observations.get(delivered["observation_id"])
                self.assertEqual(bundle["versions"]["player"], {"person": 4})
                with self.assertRaises(ConnectorError) as missing:
                    observations.bind("grove.rule_action", {"action": "offer", "other_id": names[0]["id"]},
                        delivered["observation_id"], {"properties": {"expected_other_version": {}}})
                self.assertEqual(missing.exception.code, "ObservedVersionRequired")
                again = companion_projection("map.viewport", delivered)
                self.assertEqual(again["visible_player_names"], names)
                self.assertEqual(again["visible_player_names_projection"], projection)
        self.assertEqual(raw, original)

    def test_current_name_floor_uses_only_actual_names_in_the_latest_visible_list(self):
        earlier = self.selected_scene()
        earlier["players"] = [{"id": "earlier-player", "name": "Earlier name", "version": 2}]
        raw = {"result": earlier, "execution_readback": {"entities": [], "players": [
            {"id": "nameless-player", "version": 3},
            {"id": "latest-player", "name": "Latest actual name", "version": 4}],
            "nearby_players": [{"id": "unrelated-list", "name": "Do not substitute", "version": 5}]}}
        delivered = AgentObservations("person").present("map.viewport", raw, preflight=True)
        self.assertNotIn("visible_player_names", delivered["result"])
        current = delivered["execution_readback"]
        self.assertEqual(current["visible_player_names"], [{"id": "latest-player", "name": "Latest actual name"}])
        projection = current["visible_player_names_projection"]
        self.assertEqual((projection["authorized_player_count"], projection["retained_count"],
            projection["omitted_count"]), (2, 1, 1))

    def test_hard_budget_sheds_visible_names_before_exact_selected_facts(self):
        raw = self.selected_scene(fact_size=20000)
        raw["entities"] = []
        raw["chunks"] = []
        raw["players"] = [{"id": f"other-{index}", "name": "Actual name " + "n" * 7000,
            "x": index, "y": 1, "version": index + 1} for index in range(6)]
        observations = AgentObservations("person")
        delivered = observations.present("map.viewport", raw, preflight=True)
        self.assertLessEqual(len(observations.serialize(delivered).encode()), 48 * 1024)
        self.assertEqual(delivered["selected_inspection"], raw["selected_inspection"])
        self.assertEqual(delivered["action_targets"], raw["action_targets"])
        self.assertEqual(delivered["task_action_summary"], raw["task_action_summary"])
        projection = delivered["visible_player_names_projection"]
        retained = len(delivered["visible_player_names"])
        self.assertLess(retained, 4)
        self.assertEqual(projection["retained_count"], retained)
        self.assertEqual(projection["omitted_count"], 6 - retained)
        self.assertEqual(projection["hard_limit_omitted_count"], 4 - retained)
        self.assertEqual(observations.get(delivered["observation_id"])["versions"]["player"], {"person": 4})

    def test_formal_public_viewport_preserves_the_same_author_as_inspection(self):
        from test_grove_creative import CreativeWorldTests
        fixture = CreativeWorldTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, objects = fixture.publish("A", {"key": "public-author", "name": "Public author fixture", "version": 1,
            "types": {"sign": {"name": "Authored sign", "body_material": "cloth", "cost": {"cloth": 1}}}}, ["sign"])
        scene = fixture.view("B")
        public = next(row for row in scene["entities"] if row["id"] == objects["sign"])
        inspection = fixture.read("B", "grove.inspect", {"entity_id": objects["sign"]})
        self.assertEqual(public["created_by"], fixture.roles["A"])
        self.assertEqual(public["created_by"], inspection["entity"]["created_by"])
        indexed = companion_projection("map.viewport", scene)
        self.assertEqual(next(row for row in indexed["entities"] if row["id"] == objects["sign"])["created_by"],
            fixture.roles["A"])

    def test_current_author_name_remains_with_stable_id_in_scene_floor_and_selected_state(self):
        raw = self.selected_scene()
        for index, row in enumerate(raw["entities"][-3:]):
            row["created_by_name"] = None if index == 1 else "Same current name"
        selected = raw["selected_inspection"]["entity"]
        selected.update(created_by="selected-author", created_by_name="Same current name")
        raw["action_targets"][0]["inspection"] = deepcopy(raw["selected_inspection"])
        before = deepcopy(raw)
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                observations = AgentObservations("person")
                delivered = observations.present("map.viewport", raw, preflight=preflight)
                actual = {row["id"]: row for row in raw["entities"]}
                for entry in delivered["nearby_discovery"][:3]:
                    self.assertEqual(entry["created_by"], actual[entry["id"]]["created_by"])
                    self.assertEqual(entry["created_by_name"], actual[entry["id"]]["created_by_name"])
                    self.assertNotIn("version", entry)
                self.assertEqual(delivered["selected_inspection"], raw["selected_inspection"])
                self.assertEqual(delivered["action_targets"], raw["action_targets"])
                versions = observations.get(delivered["observation_id"])["versions"]["entity"]
                # The protected inventory row is actually presented and may
                # bind; name-only public discovery still grants no version.
                presented = {row["id"]: row["version"] for row in delivered["entities"] + delivered["inventory"]}
                self.assertEqual(versions, {**presented, "selected": 90})
                self.assertTrue(set(versions).isdisjoint(entry["id"] for entry in delivered["nearby_discovery"]))
        default_inspection = {"entity": selected, "action_options": [{"action": "store", "available": False}]}
        compact = _companion_inspection_index(default_inspection)
        self.assertEqual(compact["entity"]["created_by"], "selected-author")
        self.assertEqual(compact["entity"]["created_by_name"], "Same current name")
        self.assertEqual(raw, before)

    def test_publish_and_checkpoint_do_not_repeat_unrelated_scene(self):
        scene = self.scene()
        observations = AgentObservations("person")
        definition = {"name": "Player designed", "types": {"body": {"body_material": "cloth", "cost_u": {"cloth": 2}}}}
        publish = observations.present("grove.definition_publish", {"ok": True, "operation_id": "publish-op",
            "commit_seq": 123, "result": {"definition_ref": "immutable-ref", "definition": definition},
            "execution_readback": scene})
        self.assertEqual(publish["result"]["definition"], definition)
        self.assertEqual(publish["operation_id"], "publish-op")
        self.assertEqual(publish["execution_readback"]["entities"], [])
        self.assertNotIn("chunks", publish["execution_readback"])
        self.assertLessEqual(len(observations.serialize(publish).encode()), 8192)
        task = {"task_id": "task", "confirmed_count": 3, "material_costs": [{"material": "wood", "quantity_u": 30}]}
        scene["task_action_summary"] = task
        checkpoint = observations.present("execution_checkpoint", {"task_id": "task", "status": "continue",
            "execution_readback": scene, "segment_finished": True})
        self.assertEqual(checkpoint["execution_readback"]["task_action_summary"], task)
        self.assertEqual(checkpoint["execution_readback"]["entities"], [])
        self.assertLessEqual(len(observations.serialize(checkpoint).encode()), 8192)

    def test_commit_and_current_material_states_remain_separate_with_stable_refs(self):
        committed = {"id": "vessel", "version": 4, "matter": [{"material": "wood", "quantity": 10}]}
        current = {"id": "vessel", "version": 5, "matter": [{"material": "wood", "quantity": 7}]}
        scene = self.scene()
        scene["entities"].append(current)
        observations = AgentObservations("person")
        delivered = observations.present("grove.interact", {"ok": True, "operation_id": "transfer", "commit_seq": 9,
            "confirmed_outcome": {"action_result": {"quantity_u": 10}}, "result": {"entity": committed},
            "execution_readback": scene}, action_entity_ids=["vessel"])
        self.assertEqual(delivered["result"]["entity"]["matter"][0]["quantity"], 10)
        self.assertEqual(delivered["execution_readback"]["entities"][0]["id"], "vessel")
        self.assertEqual(delivered["execution_readback"]["entities"][0]["matter"][0]["quantity"], 7)
        self.assertEqual(delivered["result"]["entity"]["snapshot_projection"]["superseded_by_readback"]["entity_ref"],
                         "#/execution_readback/entities/0")
        self.assertEqual(observations.get(delivered["observation_id"])["versions"]["entity"]["vessel"], 5)
        self.assertEqual(delivered["confirmed_outcome"]["action_result"]["quantity_u"], 10)

    def test_current_inventory_snapshot_reference_cannot_be_trimmed(self):
        scene = self.scene()
        current = {"id": "vessel", "version": 5, "matter": [{"material": "wood", "quantity": 7}]}
        scene["inventory"].append(current)
        scene["selected_inspection"] = {"entity": {"id": "other-selection", "version": 1}}
        delivered = AgentObservations("person").present("grove.interact", {"result": {"entity": {
            "id": "vessel", "version": 4, "matter": [{"material": "wood", "quantity": 10}]}},
            "execution_readback": scene}, action_entity_ids=["vessel"])
        reference = delivered["result"]["entity"]["snapshot_projection"]["superseded_by_readback"]["entity_ref"]
        self.assertEqual(reference, "#/execution_readback/inventory/12")
        self.assertEqual(delivered["execution_readback"]["inventory"][12]["matter"][0]["quantity"], 7)

    def test_progress_is_historical_operation_index_without_changing_saved_evidence(self):
        evidence = [{"function": "grove.interact", "operation_id": "old-op", "status": "confirmed",
            "commit_seq": 4, "task_id": "original-task", "segment_id": "old-segment", "action": "pour",
            "action_entity_ids": ["vessel"], "confirmed_outcome": {"entity": {"id": "vessel", "version": 4,
                "matter": [{"material": "water", "quantity": 50}]}}},
            {"function": "grove.interact", "operation_id": "unknown-op", "status": "unknown", "commit_seq": None}]
        summary = {"task_id": "original-task", "confirmed_count": 1, "material_costs": [],
            "coverage": {"receipt_history_complete": False, "unknown_count": 1,
                "committed_unconfirmed_count": 0, "operations_without_structured_costs": 1}}
        selected = {"entity": {"id": "vessel", "version": 9, "matter": [{"material": "water", "quantity": 7}]}}
        source = {"discovery_projection": {"scope": "authorized_preflight_discovery"},
            "execution_task": {"task_id": "original-task", "segment_id": "current-segment", "goal": "Original goal",
                "progress": {"summary": "Agent claim", "next_intent": "Agent plan", "evidence": evidence}},
            "task_action_summary": summary, "selected_inspection": selected,
            "action_targets": [{"entity_id": "vessel", "inspection": selected, "observed_at": 100}]}
        original = deepcopy(source)
        delivered = companion_projection("map.viewport", source)
        progress = delivered["execution_task"]["progress"]
        self.assertEqual(source, original)
        self.assertEqual(delivered["task_action_summary"], summary)
        self.assertEqual(delivered["selected_inspection"], selected)
        self.assertEqual(delivered["action_targets"], source["action_targets"])
        self.assertEqual(progress["summary"], "Agent claim")
        self.assertEqual(progress["next_intent"], "Agent plan")
        self.assertEqual(progress["evidence_projection"]["evidence_count"], 2)
        self.assertEqual(progress["evidence_projection"]["omitted_confirmed_outcome_count"], 1)
        projection = progress["evidence_projection"]
        self.assertEqual(projection["read_tool"], "native_tool_history")
        self.assertEqual(projection["filter_field"], "operation_id")
        for projected, saved in zip(progress["evidence"], evidence):
            for field, value in saved.items():
                if field != "confirmed_outcome":
                    self.assertEqual(projected[field], value)
            self.assertNotIn("confirmed_outcome", projected)
            self.assertNotIn("read_request", projected)
            arguments = {"mode": projection["mode"], projection["filter_field"]: projected["operation_id"]}
            self.assertTrue(Draft202012Validator(HISTORY_SCHEMA).is_valid(arguments))
            self.assertEqual(arguments, {"mode": "list", "operation_id": saved["operation_id"]})
        self.assertEqual(companion_projection("map.viewport", delivered), delivered)

    def test_progress_projection_never_searches_by_an_invented_operation(self):
        source = {"execution_task": {"progress": {"evidence": [
            {"function": "grove.interact", "status": "unknown"},
            {"operation_id": "x" * 181, "status": "committed"}]}}}
        delivered = companion_projection("map.viewport", source)
        for row in delivered["execution_task"]["progress"]["evidence"]:
            self.assertNotIn("read_request", row)
            self.assertIn("history_filter_unavailable", row)

    def test_new_task_with_no_historical_evidence_gets_no_extra_index(self):
        source = {"discovery_projection": {"scope": "authorized_preflight_discovery"},
            "execution_task": {"progress": {"summary": "", "next_intent": "", "evidence": []}}}
        self.assertEqual(companion_projection("map.viewport", source), source)


if __name__ == "__main__":
    unittest.main()
