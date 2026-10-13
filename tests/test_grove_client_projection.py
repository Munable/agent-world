"""A replaceable client can observe and act without persisted program shapes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_grove_creative as fixtures
from test_grove_presentation import definition
from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.world_context import FunctionContext
from agent_world.worlds.shared_grove import model as m
from agent_world.worlds.shared_grove.definitions import DEFINITIONS
from test_grove_interactions import installed_definition


class GroveClientProjectionTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def query(self, actor, function, args=None):
        return self.h.world.query_function("w", function, self.h.roles[actor], args or {})

    def assert_client_entity(self, entity):
        self.assertTrue({"id", "appearance", "summary_facts", "fact_sections", "actions", "custody", "navigation"} <= set(entity))
        self.assertTrue({"state", "program", "components", "capabilities", "creative"}.isdisjoint(entity))

    def test_client_watermark_and_navigation_use_same_read_boundary_without_state_export(self):
        response = self.query("A", "grove.client_viewport")
        view = response["result"]
        self.assertEqual(view["context"]["snapshot_seq"], response["snapshot_seq"])
        self.assertEqual(view["context"]["viewer_role_id"], self.h.roles["A"])
        self.assertEqual(view["context"]["world_instance"], "w")
        self.assertEqual(view["context"]["projection_version"], 2)
        for entity in view["entities"] + view["inventory"]:
            self.assert_client_entity(entity)
        blocked = {(row["x"], row["y"]) for row in view["entities"] if row["navigation"]["blocks"]}
        for cell in view["navigation"]["cells"]:
            if (cell["x"], cell["y"]) in blocked:
                self.assertFalse(cell["passable"])
        self.assertLess(len(json.dumps(view, ensure_ascii=False).encode()), 128 * 1024)

    def test_saved_scheduler_clock_metadata_is_projected_into_the_existing_strict_contract(self):
        read_state = FunctionContext.get_state

        def legacy_clock(ctx, scope, key, default=None):
            value = read_state(ctx, scope, key, default)
            if scope == m.META and key == "clock":
                return {**value, "completed_sequence": value["sequence"],
                    "frame_id": f"{value['epoch']}.{value['sequence']}", "frame_status": "closed",
                    "rules_version": "grove-matter-1"}
            return value

        # Existing worlds retain scheduler fields that are absent from a newly
        # initialized world. Only finite declared clock fields reach the DTO.
        with patch.object(FunctionContext, "get_state", legacy_clock):
            response = self.query("A", "grove.client_viewport")
            view = response["result"]
            point = view["player"]
            inspect = self.query("A", "grove.client_inspect", {"tile": {"x": point["x"], "y": point["y"]}})["result"]
            definitions = self.query("A", "grove.client_definitions")["result"]
        self.assertEqual(view["clock"], view["context"]["observation_clock"])
        self.assertEqual(view["clock"]["frame_status"], "closed")
        self.assertEqual(view["clock"]["completed_sequence"], view["clock"]["sequence"])
        for result in (view, inspect, definitions):
            self.assertNotIn("rules_version", result["context"]["observation_clock"])
        self.assertNotIn("rules_version", view["clock"])
        self.assertEqual(response["snapshot_seq"], view["context"]["snapshot_seq"])

    def test_renamed_declared_action_is_submitted_only_from_returned_fixed_intent(self):
        raw = definition()
        raw["types"]["sign"]["actions"][0]["id"] = "a_completely_new_name"
        ref, objects = self.h.publish("A", raw, ["sign"])
        eid = objects["sign"]
        before = deepcopy(self.h.world.get_state("w", DEFINITIONS, ref))
        self.h.approach("B", eid)
        response = self.query("B", "grove.client_inspect", {"entity_id": eid})
        entity = response["result"]["entity"]
        self.assert_client_entity(entity)
        action = next(row for row in entity["actions"] if row["action"] == "a_completely_new_name")
        self.assertTrue(action["available"])
        self.assertEqual(action["controls"], [])
        # The renderer knows no action-name convention, rule AST or field path.
        receipt = self.h.world.call_function("w", action["function_id"], self.h.roles["B"],
            action["fixed_arguments"], operation_id="client-explicit-intent")
        self.assertTrue(receipt["ok"])
        again = self.h.world.call_function("w", action["function_id"], self.h.roles["B"],
            action["fixed_arguments"], operation_id="client-explicit-intent")
        self.assertTrue(again["replayed"])
        current = self.query("B", "grove.client_inspect", {"entity_id": eid})["result"]["entity"]
        self.assertEqual(current["appearance"]["pose"], "normal")
        self.assertEqual(self.h.world.get_state("w", DEFINITIONS, ref), before)

    def test_hidden_container_produces_unknown_badge_and_no_authored_fields(self):
        raw = definition()
        raw["types"]["sign"]["container_capacity"] = 20
        _, objects = self.h.publish("A", raw, ["sign"])
        eid = objects["sign"]
        self.h.approach("A", eid)
        self.h.interact("A", eid, "close")
        entity = self.query("B", "grove.client_inspect", {"entity_id": eid})["result"]["entity"]
        self.assertFalse(entity["appearance"]["badge"]["known"])
        self.assertIsNone(entity["appearance"]["badge"]["value"])
        self.assertNotIn("fields", [section["id"] for section in entity["fact_sections"]])
        self.assert_client_entity(entity)

    def test_blueprints_offer_server_choices_without_canonical_program_structure(self):
        raw = definition()
        raw["types"]["sign"].pop("cost")
        raw["types"]["sign"]["cost_u"] = {"cloth": 2}
        # Publication is independent of stock and instance creation.
        published = self.h.call("A", "grove.definition_publish", {"definition": raw})
        ref = published["definition_ref"]
        detail = self.query("A", "grove.client_definitions", {"mode": "detail", "definition_ref": ref})["result"]
        self.assertNotIn("definition", detail)
        self.assertEqual(detail["version"], raw["version"])
        choice = next(row for row in detail["types"] if row["id"] == "sign")
        self.assertTrue(choice["can_fabricate"])
        self.assertEqual(choice["creation_request"]["function_id"], "grove.client_definitions")
        self.assertEqual(choice["creation_request"]["arguments"]["definition_ref"], ref)
        self.assertFalse(detail["installable"])
        menu = self.query("A", "grove.client_definitions", {"mode": "fabrication", "definition_ref": ref, "type_id": choice["id"]})["result"]
        self.assertEqual(menu["intent"]["function_id"], "grove.fabricate")
        self.assertEqual(menu["intent"]["fixed_arguments"]["definition_ref"], ref)
        self.assertEqual(menu["inputs"]["path"], ["sources"])
        self.assertEqual(menu["inputs"]["choices"], menu["action"]["choices"])
        self.assertNotIn("sources", menu["intent"]["fixed_arguments"])
        self.assertNotIn("definition", menu)
        self.assertEqual(menu["inputs"]["schema"], menu["intent"]["input_schema"]["properties"]["sources"])
        page = self.query("A", "grove.client_definitions", {"mode": "list", "limit": 1})["result"]
        self.assertTrue({"has_more", "next_cursor", "filters", "filter_scope"} <= set(page))
        self.assertEqual(page["definitions"][0]["read_request"]["arguments"]["definition_ref"], ref)

    def test_fixed_intent_guard_policy_is_explicit_without_action_name_inference(self):
        _, objects = self.h.publish("A", definition(), ["sign"])
        eid = objects["sign"]
        self.h.approach("B", eid)
        value = self.query("B", "grove.client_inspect", {"entity_id": eid})["result"]
        descriptor = next(row for row in value["entity"]["actions"] if row["action"] == "leave_flat")
        self.assertIn("expected_target_interaction_revision", descriptor["confirmation_keys"])
        self.assertNotIn("expected_target_version", descriptor["confirmation_keys"])
        self.assertNotIn("expected_player_version", descriptor["confirmation_keys"])
        narrow = self.query("B", "grove.client_inspect", {"entity_id": eid, "action": descriptor["action"]})["result"]
        self.assertTrue(any(row["expanded"] is False for row in narrow["entity"]["actions"]))
        self.assertTrue(all(row["read_request"]["function_id"] == "grove.client_inspect" for row in narrow["entity"]["actions"]))

    def test_revision_source_and_discovery_are_formal_reads_without_program_parsing(self):
        h = self.h
        original = h.call("A", "grove.definition_publish", {"definition": definition()})
        source_ref = original["definition_ref"]
        revised = deepcopy(original["definition"])
        revised["name"] = "另一人的显示改版"
        reference = h.call("B", "grove.definition_publish", {
            "definition": revised, "source_definition_ref": source_ref})["definition_ref"]
        detail = self.query("A", "grove.client_definitions", {"mode": "detail", "definition_ref": reference})["result"]
        source = detail["source_definition"]
        self.assertEqual((source["definition_ref"], source["author"], source["author_name"]), (source_ref, h.roles["A"], "A"))
        self.assertNotIn("definition", source)
        self.assertNotIn("program", source)
        request = source["read_request"]
        source_detail = self.query("B", request["function_id"], request["arguments"])["result"]
        self.assertEqual(source_detail["definition_ref"], source_ref)
        self.assertIsNone(source_detail["source_definition"])
        request = source_detail["revisions_request"]
        page = self.query("A", request["function_id"], request["arguments"])["result"]
        self.assertEqual(page["filters"], {"source_definition_ref": source_ref})
        self.assertEqual([row["definition_ref"] for row in page["definitions"]], [reference])
        self.assertEqual(page["definitions"][0]["source_definition_ref"], source_ref)
        section = next(row for row in detail["fact_sections"] if row["id"] == "source_definition")
        original_author = next(row for row in section["rows"] if row["id"] == "source_author")
        current_name = next(row for row in section["rows"] if row["id"] == "source_author_name")
        self.assertEqual(original_author["temporal_scope"], "published")
        self.assertEqual(current_name["temporal_scope"], "current")

    def test_foreign_private_entity_is_rejected_by_formal_client_read(self):
        view = self.h.view("B")
        item = view["inventory"][0]
        with self.assertRaises(PermissionDenied):
            self.query("A", "grove.client_inspect", {"entity_id": item["id"]})

    def test_tile_actions_include_authoritative_eligibility_and_exact_point(self):
        view = self.query("A", "grove.client_viewport")["result"]
        point = view["player"]
        tile = self.query("A", "grove.client_inspect", {"tile": {"x": point["x"], "y": point["y"]}})["result"]
        self.assertEqual(tile["context"]["viewer_role_id"], self.h.roles["A"])
        for action in tile["actions"]:
            self.assertEqual((action["fixed_arguments"]["x"], action["fixed_arguments"]["y"]), (point["x"], point["y"]))
            self.assertIn("available", action)
            self.assertIn("reason", action)
            self.assertEqual(action["controls"][0]["kind"], "multi_choice")
            self.assertIn("preview_scope", action)
            self.assertIn("failure", action)

    def test_generic_control_directory_follows_formal_next_and_exact_validation_requests(self):
        h = self.h
        refs = [h.call("A", "grove.definition_publish", {"definition": installed_definition("candidate_"+str(index))})["definition_ref"] for index in range(2)]
        # A non-installable publication can make a selected page empty; the
        # formal next request still represents the rest of the actual directory.
        h.call("A", "grove.definition_publish", {"definition": definition()})
        target = h.item("A", "水壶")["id"]
        inspected = self.query("A", "grove.client_inspect", {"entity_id": target, "action": "install_interaction"})["result"]
        descriptor = next(row for row in inspected["entity"]["actions"] if row["action"] == "install_interaction")
        control = descriptor["controls"][0]
        request = control["discovery_request"]
        self.assertEqual(request["function_id"], "grove.client_choices")
        request["arguments"]["limit"] = 1
        choices = []
        pages = []
        while request:
            response = self.query("A", request["function_id"], request["arguments"])
            page = response["result"]
            pages.append(page)
            self.assertEqual(page["context"]["snapshot_seq"], response["snapshot_seq"])
            self.assertNotIn("definitions", page)
            self.assertNotIn("next_cursor", page)
            choices.extend(page["choices"])
            self.assertEqual(page["complete"], page["next_request"] is None)
            request = page["next_request"]
        self.assertTrue(any(not page["choices"] and not page["complete"] for page in pages))
        self.assertEqual({row["value"] for row in choices}, set(refs))
        selected = choices[0]
        self.assertTrue(selected["validation"]["available"])
        request = selected["read_request"]
        before = deepcopy(h.entity(target))
        checked = self.query("A", request["function_id"], request["arguments"])["result"]
        self.assertEqual(checked["choices"][0]["value"], selected["value"])
        self.assertEqual(checked["choices"][0]["arguments_patch"], selected["arguments_patch"])
        self.assertEqual(h.entity(target), before)
        h.interact("A", target, "close")
        rejected = self.query("A", request["function_id"], request["arguments"])["result"]["choices"][0]
        self.assertFalse(rejected["validation"]["available"])
        self.assertIn("打开", rejected["validation"]["reason"])
        with self.assertRaises(PermissionDenied):
            self.query("B", request["function_id"], request["arguments"])
        with self.assertRaises(RuleViolation):
            self.query("A", "grove.client_choices", {"entity_id": target, "action": "close", "control_id": "definition_ref"})
