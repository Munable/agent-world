"""Current automatic inputs and saved mechanical evidence through real reads."""
from copy import deepcopy
import unittest

from agent_world.errors import PermissionDenied
from agent_world.worlds.shared_grove import observations
import test_grove_creative as fixtures


class AutomaticRuleObservationTests(unittest.TestCase):
    def test_all_eight_rules_include_nonreaction_and_real_event_requirements(self):
        rules = [{"id": "rule_" + str(index), "phase": "after", "when": True,
                  "effects": [{"op": "set", "field": "ready", "value": True}]}
                 for index in range(8)]
        rules[0]["effects"] = [{"op": "drive", "enabled": {"op": "eq", "left": {"event": "action"}, "right": "move"}}]
        rules[1]["effects"] = [{"op": "emit", "name": "signal"}]
        rules[2]["when"] = {"op": "eq", "left": {"event": "kind"}, "right": "movement"}
        context = {"fields": {"ready": False}, "params": {}, "objects": {}, "ports": {},
                   "event": {"action": "none", "kind": "none"}}
        original_rules, original_context = deepcopy(rules), deepcopy(context)
        observed = observations._automatic_rule_observations({"rules": rules}, context)
        self.assertEqual(len(observed), 8)
        self.assertEqual(observed[0]["input_kind"], "public_event")
        self.assertTrue(observed[0]["condition"]["satisfied"])
        enabled = observed[0]["drive_inputs"][0]["observation"]
        self.assertFalse(enabled["known"])
        self.assertIsNone(enabled["satisfied"])
        self.assertEqual(enabled["reason"], "event_required")
        self.assertEqual(observed[1]["effect_ops"], ["emit"])
        self.assertEqual(observed[1]["input_kind"], "logical_step")
        self.assertFalse(observed[2]["condition"]["known"])
        self.assertTrue(all(row["snapshot_only"] for row in observed))
        self.assertEqual(rules, original_rules)
        self.assertEqual(context, original_context)

    def test_saved_control_projection_does_not_alias_persisted_evidence(self):
        binding = {"mechanical": {"enabled": True, "speed": 2, "position": 0, "energy": 3},
                   "mechanical_last_solved": {"step": 5},
                   "drive_request": {"enabled": True, "requested_step": 6, "applies_from_step": 7,
                       "source": {"kind": "automatic_rule", "rule_id": "respond", "phase": "after"}}}
        original = deepcopy(binding)
        observed = observations._mechanical_observation(binding, {"mechanical": {"role": "source"}},
                                                        {"mechanical_solver_eligible": False}, 7)
        self.assertFalse(observed["pending_next_step"])
        self.assertFalse(observed["solver_eligible"])
        self.assertTrue(observed["stored_output_is_history"])
        observed["stored_state"]["speed"] = 100
        observed["last_solved"]["step"] = 100
        observed["drive_request"]["source"]["rule_id"] = "changed"
        self.assertEqual(binding, original)


class AutomaticWorldObservationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def source(self):
        definition = {"key": "observed_control", "name": "真实输入控制", "version": 1,
            "types": {"source": {"name": "观察动力", "body_material": "wood", "cost": {"wood": 1},
                "container_capacity": 20, "mechanical": {"role": "source", "torque": 4},
                "fields": {"observed_stopped": {"type": "boolean", "default": False}},
                "ports": {"water": {"fields": ["physical.contained_water"]}},
                "rules": [{"id": "respond", "phase": "after", "when": True,
                    "effects": [{"op": "drive", "enabled": {"op": "gt", "left": {
                        "object": {"port": "water", "field": "physical.contained_water"}}, "right": 0}}]},
                    {"id": "zz_observe_stop", "phase": "after", "when": True,
                     "effects": [{"op": "set", "field": "observed_stopped", "value": {
                         "op": "eq", "left": {"mechanical": "status"}, "right": "stopped"}}]}]}}}
        return self.h.publish("A", definition, ["source"])[1]["source"]

    def inspect(self, target, actor="A"):
        return self.h.read(actor, "grove.inspect", {"entity_id": target})["entity"]["creative"]

    def test_drive_expression_exposes_unknown_and_last_control_survives_private_input(self):
        h = self.h
        source = self.source()
        unknown = self.inspect(source)
        self.assertEqual(unknown["automatic_rule_count"], 2)
        self.assertEqual(unknown["explicit_action_count"], 0)
        rule = unknown["automatic_rules"][0]
        self.assertTrue(rule["condition"]["satisfied"])
        self.assertFalse(rule["drive_inputs"][0]["observation"]["known"])
        self.assertTrue(unknown["deployment"]["ordinary_rules_enabled"])
        self.assertIsNone(unknown["mechanical_observation"]["last_solved"])
        self.assertIn("不证明整个装置", unknown["process"]["status_meaning"])
        self.assertIn("不是最近自动声明评估或机械求解步", unknown["process"]["last_tick_meaning"])
        stored_process = h.entity(source)["state"]["program"]["process"]
        self.assertNotIn("status_meaning", stored_process)
        self.assertNotIn("last_tick_meaning", stored_process)
        h.ticks(1)
        waiting = self.inspect(source)["process"]
        self.assertEqual(waiting["status"], "waiting")
        kettle = h.item("A", "水壶")["id"]
        h.interact("A", kettle, "drop")
        h.action("A", source, "bind_port", port="water", other_id=kettle)
        h.action("A", source, "fuel", item_id=h.item("A", "木柴")["id"], amount=4)
        h.ticks(1)
        requested = self.inspect(source)["mechanical_observation"]
        self.assertEqual(requested["drive_request"]["source"],
                         {"kind": "automatic_rule", "rule_id": "respond", "phase": "after"})
        self.assertTrue(requested["pending_next_step"])
        h.ticks(1)
        moving = self.inspect(source)
        self.assertGreater(moving["mechanical_observation"]["stored_state"]["speed"], 0)
        self.assertEqual(moving["process"]["steps"], 0)
        self.assertEqual(moving["process"]["status"], "waiting")
        self.assertEqual(moving["process"]["last_tick"], waiting["last_tick"])
        self.assertNotIn("blocked_reason", moving["process"])
        self.assertNotIn("blocked_rules", moving["process"])
        h.approach("B", kettle)
        h.interact("B", kettle, "pickup")
        h.ticks(1)
        unavailable = self.inspect(source)
        self.assertFalse(unavailable["automatic_rules"][0]["drive_inputs"][0]["observation"]["known"])
        self.assertTrue(unavailable["mechanical_observation"]["stored_state"]["enabled"])
        self.assertEqual(unavailable["mechanical_observation"]["drive_request"], requested["drive_request"])
        h.interact("B", kettle, "drop")
        resumed = self.inspect(source)
        self.assertTrue(resumed["automatic_rules"][0]["drive_inputs"][0]["observation"]["known"])
        self.assertEqual(resumed["process"]["steps"], 0)
        water = next(lot for lot in h.entity(kettle)["matter"] if lot["material"] == "water" and lot["slot"] == "contained")
        h.approach("B", source)
        h.interact("B", source, "transfer", item_id=kettle, lot_id=water["id"], amount=water["quantity"])
        h.ticks(1)
        disabled = self.inspect(source)["mechanical_observation"]["stored_state"]
        self.assertFalse(disabled["enabled"])
        self.assertGreater(disabled["energy"], 0)
        h.ticks(1)
        stopped = self.inspect(source)
        actual = stopped["mechanical_observation"]["stored_state"]
        self.assertEqual((actual["status"], actual["speed"]), ("stopped", 0))
        for field in ("energy", "position", "angle"):
            self.assertEqual(actual[field], disabled[field])
        self.assertTrue(h.entity(source)["state"]["program"]["fields"]["observed_stopped"])

    def test_eight_complex_legal_conditions_survive_native_inspection_formatting(self):
        import json
        from agent_world.grove_agent_observation import _agent_tool_text, _agent_tool_text_fits, _bounded_agent_projection
        h = self.h
        names = ["field_"+str(index)+"_"+"x"*40 for index in range(8)]
        condition = {"op": "all", "args": [{"op": "eq", "left": {"field": name}, "right": 0}
                                              for name in (names*3)[:21]]}
        definition = {"key": "complex_observation", "name": "合法复杂条件", "version": 1,
            "types": {"device": {"name": "复杂条件装置", "body_material": "wood", "cost": {"wood": 1},
                "fields": {name: {"type": "integer", "default": 0, "min": 0, "max": 1} for name in names},
                "rules": [{"id": "rule_"+str(index), "phase": "after", "when": deepcopy(condition),
                           "effects": [{"op": "set", "field": names[0], "value": 0}]} for index in range(8)]}}}
        _, instances = h.publish("A", definition, ["device"])
        payload = h.world.query_function("w", "grove.inspect", h.roles["A"], {"entity_id": instances["device"]})
        rules = payload["result"]["entity"]["creative"]["automatic_rules"]
        self.assertEqual(len(rules), 8)
        for rule in rules:
            self.assertTrue(rule["condition"]["known"])
            self.assertTrue(rule["condition"]["satisfied"])
            self.assertEqual(len(rule["condition"]["facts"]), 8)
            self.assertEqual(len(rule["condition"]["checks"]), 21)
            self.assertEqual(rule["condition"]["expression"], condition)
        text = _agent_tool_text(payload)
        self.assertGreater(len(text.encode()), 51200)
        self.assertEqual(json.loads(text), payload)
        before = deepcopy(payload)
        fallback = _bounded_agent_projection("grove.inspect", payload)
        observed = fallback["result"]["entity"]["creative"]
        self.assertTrue(_agent_tool_text_fits(_agent_tool_text(fallback), native=True))
        self.assertEqual(len(observed["automatic_rules"]), 8)
        for original, retained in zip(rules, observed["automatic_rules"]):
            expected = deepcopy(original)
            expected["condition"].pop("expression")
            self.assertEqual(retained, expected)
        request = observed["automatic_rules_expression_projection"]["read_request"]
        self.assertEqual(request["tool"], "grove_definition_read")
        source = h.read("A", "grove.definition_read", request["arguments"])
        self.assertEqual(source["definition"]["types"]["device"]["rules"][0]["when"], condition)
        self.assertEqual(payload, before)
        from agent_world.grove_agent_observation import AgentObservations, _checked_agent_tool_text
        checkpoint = AgentObservations(h.roles["A"]).present("execution_checkpoint", {
            "segment_finished": True, "status": "blocked", "execution_readback": {
                **h.view("A"), "selected_inspection": payload["result"]}})
        _checked_agent_tool_text(checkpoint)
        current = checkpoint["execution_readback"]["selected_inspection"]["entity"]["creative"]
        self.assertEqual(len(current["automatic_rules"]), 8)
        self.assertTrue(all(row["condition"]["satisfied"] for row in current["automatic_rules"]))

    def test_backpack_marks_saved_speed_historical_and_closed_projection_hides_details(self):
        h = self.h
        source = self.source()
        h.action("A", source, "fuel", item_id=h.item("A", "木柴")["id"], amount=4)
        h.action("A", source, "start")
        explicit = self.inspect(source)["mechanical_observation"]["drive_request"]["source"]
        self.assertEqual(explicit, {"kind": "explicit_action", "action_id": "start", "actor_role_id": h.roles["A"]})
        h.ticks(1)
        solved = self.inspect(source)["mechanical_observation"]
        self.assertGreater(solved["stored_state"]["speed"], 0)
        self.assertTrue(solved["last_solved"]["historical"])
        h.interact("A", source, "pickup")
        h.ticks(1)
        carried = self.inspect(source)
        self.assertFalse(carried["deployment"]["publicly_placed"])
        self.assertTrue(carried["deployment"]["ordinary_rules_enabled"])
        mechanical = carried["mechanical_observation"]
        self.assertFalse(mechanical["solver_eligible"])
        self.assertTrue(mechanical["stored_output_is_history"])
        self.assertEqual(mechanical["stored_state"]["speed"], solved["stored_state"]["speed"])
        self.assertEqual(mechanical["last_solved"], solved["last_solved"])
        with self.assertRaises(PermissionDenied):
            self.inspect(source, "B")
        h.interact("A", source, "drop")
        h.interact("A", source, "close")
        hidden = self.inspect(source, "B")
        self.assertTrue(hidden["hidden"])
        for key in ("automatic_rules", "deployment", "mechanical_observation"):
            self.assertNotIn(key, hidden)


if __name__ == "__main__":
    unittest.main()
