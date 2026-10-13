"""Public Agent contracts preserve Grove facts and share world discovery."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from agent_world import FunctionOutcome, FunctionSpec, WorldDefinition
from agent_world.errors import SchemaRejected
from agent_world.runtime_contracts import MAX_RESULT_BYTES, check_schema, validate
from agent_world.worlds.shared_grove.agent_contract import (
    BINDING_POLICY, TYPED_READ_FUNCTIONS, attach_agent_contract, output_schema,
)
from agent_world.worlds.shared_grove.definitions import DEFINITIONS
import test_grove_creative as fixtures


class GroveAgentContractTests(unittest.TestCase):
    def test_existing_untyped_read_installs_as_a_new_function_contract(self):
        original = WorldDefinition("shared-grove", "Contract upgrade", (
            FunctionSpec("grove.definition_list", lambda ctx, args: FunctionOutcome({}),
                         {"type": "object"}, version=7, access="read"),), version=1)
        self.h.world.install_world("contract-upgrade", original)
        updated = replace(attach_agent_contract(original), version=2)
        self.h.world.install_world("contract-upgrade", updated)
        actual = self.h.world.get_function("contract-upgrade", "grove.definition_list")
        self.assertEqual(actual["version"], 8)
        self.assertIn("agent_context", actual["output_schema"]["properties"])
        self.assertIs(attach_agent_contract(updated), updated)

    def test_authored_parameter_names_do_not_become_observation_guards(self):
        schema = {"type": "object", "properties": {
            "expected_player_version": {"type": "integer"},
            "params": {"type": "object", "properties": {
                "expected_temperature_version": {"type": "integer"}}}},
            "required": ["params"]}
        handler = lambda ctx, arguments: FunctionOutcome(arguments)
        definition = WorldDefinition("shared-grove", "Guard names", (
            FunctionSpec("grove.rule_action", handler, schema),))
        attached = attach_agent_contract(definition)
        contract_spec = next(spec for spec in attached.functions if spec.name == "grove.agent_contract")
        contract = contract_spec.handler(None, {}).result
        action = next(row for row in contract["functions"] if row["function_id"] == "grove.rule_action")
        self.assertEqual(action["guard_paths"], [["expected_player_version"]])
        self.assertIs(attached.functions[0].handler, handler)

    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        from agent_world.worlds.shared_grove import make_world
        self.definition = make_world()
        self.h.world.install_world("w", self.definition)

    def query(self, function_id, arguments=None):
        return self.h.world.query_function("w", function_id, self.h.roles["A"], arguments or {})

    def assert_output(self, function_id, response):
        schema = self.h.world.get_function("w", function_id)["output_schema"]
        check_schema(schema)
        validate(schema, response["result"], maximum=MAX_RESULT_BYTES)
        context = response["result"]["agent_context"]
        self.assertEqual(context, {"world_instance": "w", "world_id": "shared-grove",
            "world_version": self.definition.version, "viewer_role_id": self.h.roles["A"],
            "snapshot_seq": response["snapshot_seq"]})
        self.assertLessEqual(len(response["result"]["read_requests"]), 24)

    def test_bootstrap_advertises_contract_and_agent_surface_has_no_human_or_host_fields(self):
        boot = self.h.world.bootstrap("w", self.h.roles["A"])
        self.assertEqual(boot["world_entry_state"]["view"]["agent_contract_request"],
                         {"function_id": "grove.agent_contract", "arguments": {}})
        contract = self.query("grove.agent_contract")["result"]
        self.assertEqual(contract["contract_version"], 1)
        self.assertEqual(contract["world_id"], "shared-grove")
        self.assertEqual(contract["observation"], {"version": 1, "scope": "client_presented",
            "write_field": "observation_id", "read_base_field": "base_observation_id",
            "restart_invalidates": True})
        self.assertEqual(contract["recovery"], {"receipt_first": True,
            "same_operation_envelope": True, "unknown_blocks_new_writes": True,
            "receipt_matches_original_intent": True})
        self.assertTrue(all(not row["function_id"].startswith("grove.client_") for row in contract["functions"]))
        self.assertTrue({"grove.control_grant", "grove.control_revoke"}.isdisjoint(
            row["function_id"] for row in contract["functions"]))
        join = next(row for row in contract["functions"] if row["function_id"] == "grove.join")
        self.assertEqual(join["access"], "write")
        self.assertEqual(join["guard_paths"], [])
        self.assertNotIn("execution_segment_id", json.dumps(contract))
        self.assertNotIn("input_schema", json.dumps(contract["functions"]))
        self.assertNotIn("output_schema", json.dumps(contract["functions"]))
        write = next(row for row in contract["functions"] if row["function_id"] == "grove.fabricate")
        self.assertEqual(write["binding_policy"], BINDING_POLICY)
        self.assertIn(["sources", "*", "expected_entity_version"], write["guard_paths"])
        interact = next(row for row in contract["functions"] if row["function_id"] == "grove.interact")
        self.assertIn(["expected_combination_fingerprint"], interact["guard_paths"])
        self.assertIn(["expected_target_interaction_revision"], interact["guard_paths"])
        self.assertIs(attach_agent_contract(self.definition), self.definition)

    def test_discovery_exposes_typed_read_schemas_and_rejects_malformed_output(self):
        discovery = self.h.world.discover_functions("w", actor_role_id=self.h.roles["A"],
                                                   include_schemas=True, limit=200)
        catalog = {row["function_id"]: row for row in discovery["functions"]}
        self.assertTrue(TYPED_READ_FUNCTIONS <= catalog.keys())
        for name in TYPED_READ_FUNCTIONS:
            with self.subTest(function_id=name):
                schema = catalog[name]["output_schema"]
                self.assertIn("agent_context", schema["properties"])
                self.assertIn("read_requests", schema["required"])
                self.assertGreater(len(schema["required"]), 2)
                self.assertNotIn("execution_segment_id", json.dumps(schema))
                check_schema(schema)
        response = self.query("map.viewport")
        self.assert_output("map.viewport", response)
        invalid = deepcopy(response["result"])
        invalid["inventory"] = "a description is not an inventory page"
        with self.assertRaises(SchemaRejected):
            validate(catalog["map.viewport"]["output_schema"], invalid, maximum=MAX_RESULT_BYTES)

    def test_current_raw_facts_and_immutable_definition_are_preserved(self):
        scene = self.query("map.viewport")
        entity_id = scene["result"]["inventory"][0]["id"]
        from agent_world.worlds.shared_grove import model as m
        saved = deepcopy(self.h.world.get_state("w", m.ENTITIES, entity_id))
        inspection = self.query("grove.inspect", {"entity_id": entity_id})
        self.assert_output("grove.inspect", inspection)
        actual = inspection["result"]["entity"]
        self.assertEqual(actual["id"], saved["value"]["id"])
        self.assertEqual([{key: row[key] for key in ("id", "material", "quantity", "slot")}
                          for row in actual["matter"]],
                         [{key: row[key] for key in ("id", "material", "quantity", "slot")}
                          for row in saved["value"]["matter"]])
        self.assertEqual(self.h.world.get_state("w", m.ENTITIES, entity_id), saved)
        point = scene["result"]["player"]
        build = self.query("grove.build_options", {"x": point["x"], "y": point["y"]})
        self.assert_output("grove.build_options", build)
        self.assertEqual(self.h.world.get_state("w", m.ENTITIES, entity_id), saved)
        raw = fixtures.package()
        raw["key"] = "agent-contract-preservation"
        raw["types"]["source"].pop("cost")
        raw["types"]["source"]["cost_u"] = {"wood": 2}
        publication = self.h.call("A", "grove.definition_publish", {"definition": raw})
        reference = publication["definition_ref"]
        original = deepcopy(self.h.world.get_state("w", DEFINITIONS, reference))
        detail = self.query("grove.definition_read", {"definition_ref": reference})
        listing = self.query("grove.definition_list", {"limit": 1})
        materials = self.query("grove.fabrication_options", {"definition_ref": reference, "type_id": "source"})
        for name, response in (("grove.definition_read", detail),
                               ("grove.definition_list", listing),
                               ("grove.fabrication_options", materials)):
            self.assert_output(name, response)
        self.assertEqual(detail["result"]["definition"], original["value"]["definition"])
        self.assertEqual(detail["result"]["hash"], original["value"]["hash"])
        self.assertEqual(self.h.world.get_state("w", DEFINITIONS, reference), original)
        self.assertTrue(any(row["function_id"] == "grove.definition_read"
                            for row in listing["result"]["read_requests"]))

    def test_no_input_and_empty_choices_preserve_their_actual_action_semantics(self):
        jug = self.h.item("A", "水壶")["id"]
        response = self.query("grove.inspect", {"entity_id": jug})
        self.assert_output("grove.inspect", response)
        no_input = next(row for row in response["result"]["action_options"]
                        if "input" in row and row["input"] is None)
        self.assertIsNone(no_input["input"])
        malformed = deepcopy(response["result"])
        next(row for row in malformed["action_options"] if row["action"] == no_input["action"])["input"] = ""
        with self.assertRaises(SchemaRejected):
            validate(output_schema("grove.inspect"), malformed, maximum=MAX_RESULT_BYTES)
        _, objects = self.h.publish("A", fixtures.package(), ["source"])
        self.h.approach("A", objects["source"])
        authored = self.query("grove.inspect", {"entity_id": objects["source"]})
        self.assert_output("grove.inspect", authored)
        self.assertTrue(any(row.get("choices") == {} for row in authored["result"]["action_options"]
                            if row["function_id"] == "grove.rule_action"))

    def test_empty_directory_and_hidden_container_keep_null_and_empty_values(self):
        page = self.query("grove.definition_list")
        self.assert_output("grove.definition_list", page)
        self.assertEqual(page["result"]["definitions"], [])
        self.assertEqual(page["result"]["filters"], {})
        self.assertIsNone(page["result"]["next_cursor"])
        malformed = deepcopy(page["result"])
        malformed["filters"] = None
        with self.assertRaises(SchemaRejected):
            validate(output_schema("grove.definition_list"), malformed, maximum=MAX_RESULT_BYTES)
        jug = self.h.item("A", "水壶")["id"]
        self.h.interact("A", jug, "close")
        self.h.interact("A", jug, "drop")
        inspection = self.query("grove.inspect", {"entity_id": jug})
        self.assert_output("grove.inspect", inspection)
        self.assertIsNone(inspection["result"]["entity"]["state"]["water"])
        self.assertIsNone(inspection["result"]["entity"]["simulation"]["water"])
        self.assertEqual(inspection["result"]["contents"], [])

    def test_thin_wrapper_preserves_original_result_events_and_write_handlers(self):
        raw = {"player": {"id": "a", "version": 2, "x": 0, "y": 0},
               "chunks": [], "entities": [], "players": [], "inventory": [],
               "inventory_total": 0, "inventory_next": None, "clock": {"sequence": 1},
               "bounds": {"min_x": 0, "min_y": 0, "max_x": 0, "max_y": 0}, "truncated": False}
        original = deepcopy(raw)
        event = SimpleNamespace(marker="original")
        def read(ctx, args):
            return FunctionOutcome(raw, (event,))
        def write(ctx, args):
            raise AssertionError("the contract must never invoke a write")
        definition = WorldDefinition("shared-grove", "Grove", (
            FunctionSpec("map.viewport", read, {"type": "object"}, access="read"),
            FunctionSpec("grove.move", write, {"type": "object", "properties": {
                "expected_player_version": {"type": "integer"}}})), version=7)
        attached = attach_agent_contract(definition)
        context = SimpleNamespace(universe="u", actor_role_id="a", read_snapshot_seq=11)
        outcome = next(spec for spec in attached.functions if spec.name == "map.viewport").handler(context, {})
        self.assertEqual({key: value for key, value in outcome.result.items()
                          if key not in {"agent_context", "read_requests"}}, original)
        self.assertEqual(raw, original)
        self.assertEqual(outcome.events, (event,))
        self.assertIs(next(spec.handler for spec in attached.functions if spec.name == "grove.move"), write)
        validate(output_schema("map.viewport"), outcome.result, maximum=MAX_RESULT_BYTES)
