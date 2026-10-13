"""Public history retains consequences without duplicating every motion step."""
from copy import deepcopy
import unittest

from agent_world.worlds.shared_grove import creative, generation, model as m
import test_grove_creative as fixtures


class PublicOutcomeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def content(self, entity_id):
        entity = self.h.entity(entity_id)
        region = f"{entity['x'] // 16}:{entity['y'] // 16}"
        return self.h.world.get_state("w", generation.CONTENTS, region)

    def outcomes(self, content):
        value = content["value"]
        return [self.h.world.get_state("w", generation.OUTCOMES,
                                      f"{value['id']}:outcome:{sequence}")
                for sequence in range(1, value["outcome_count"] + 1)]

    def test_continuous_motion_keeps_events_and_observers_without_growing_public_history(self):
        h = self.h
        definition = fixtures.package()
        del definition["types"]["gate"]["barrier"]
        observer = definition["types"]["echo"]
        observer["fields"] = {"count": {"type": "integer", "default": 0, "min": 0, "max": 1000}}
        observer["rules"] = [{"id": "count_motion", "phase": "after",
            "when": {"op": "eq", "left": {"event": "action"}, "right": "mechanical"},
            "effects": [{"op": "set", "field": "count", "value": {
                "op": "add", "left": {"field": "count"}, "right": 1}}]}]
        _, objects = h.publish("A", definition, ["source", "gear", "gate", "echo"])
        h.action("A", objects["echo"], "start_recording")
        h.action("A", objects["source"], "connect", other_id=objects["gear"])
        h.action("A", objects["gear"], "connect", other_id=objects["gate"])
        h.action("A", objects["source"], "fuel", item_id=h.item("A", "木柴")["id"], amount=6)
        h.action("A", objects["source"], "start")
        content_before = deepcopy(self.content(objects["source"]))
        outcomes_before = deepcopy(self.outcomes(content_before))
        energy_before = h.entity(objects["source"])["state"]["program"]["mechanical"]["energy"]
        position_before = h.entity(objects["gate"])["state"]["program"]["mechanical"]["position"]
        recorded_before = h.entity(objects["echo"])["state"]["program"]["recorder"]["recorded_count"]
        journal_before = h.world.get_state("w", m.META, "creative_events")["value"]["sequence"]

        h.ticks(4)

        source = h.entity(objects["source"])["state"]["program"]["mechanical"]
        gate = h.entity(objects["gate"])["state"]["program"]["mechanical"]
        echo = h.entity(objects["echo"])["state"]["program"]
        self.assertLess(source["energy"], energy_before)
        self.assertGreater(gate["position"], position_before)
        self.assertGreater(echo["fields"]["count"], 0)
        self.assertGreater(echo["recorder"]["recorded_count"], recorded_before)
        journal = h.world.get_state("w", m.META, "creative_events")["value"]
        self.assertGreater(journal["sequence"], journal_before)
        self.assertTrue(any(event["action"] == "mechanical" and event["entity_id"] == objects["gate"]
                            and event["mechanical_position"] == gate["position"]
                            for event in journal["events"]))
        record = h.world.get_state("w", creative.RECORDS, objects["echo"])["value"]
        self.assertTrue(any(event["action"] == "mechanical" for event in record["history"]))
        self.assertEqual(self.content(objects["source"]), content_before)
        self.assertEqual(self.outcomes(content_before), outcomes_before)

        h.action("B", objects["source"], "stop")
        content_after = self.content(objects["source"])
        self.assertEqual(content_after["value"]["outcome_count"], content_before["value"]["outcome_count"] + 1)
        stopped = self.outcomes(content_after)[-1]["value"]
        self.assertEqual((stopped["entity_id"], stopped["action"], stopped["author"]),
                         (objects["source"], "stop", h.roles["B"]))

    def test_authored_action_named_mechanical_is_still_retained(self):
        h = self.h
        definition = {"key": "manual_marker", "name": "手动标记", "version": 1, "types": {
            "marker": {"name": "标记牌", "body_material": "wood", "cost": {"wood": 1},
                "fields": {"marked": {"type": "boolean", "default": False}},
                "actions": [{"id": "mechanical", "label": "留下标记", "effects": [
                    {"op": "set", "field": "marked", "value": True}]}]}}}
        _, objects = h.publish("A", definition, ["marker"])
        content_before = self.content(objects["marker"])
        h.action("B", objects["marker"], "mechanical")
        self.assertTrue(h.entity(objects["marker"])["state"]["program"]["fields"]["marked"])
        content_after = self.content(objects["marker"])
        self.assertEqual(content_after["value"]["outcome_count"], content_before["value"]["outcome_count"] + 1)
        outcome = self.outcomes(content_after)[-1]["value"]
        self.assertEqual((outcome["action"], outcome["summary"], outcome["author"]),
                         ("mechanical", "已执行已发布动作：留下标记。", h.roles["B"]))

    def test_real_material_reaction_keeps_its_outcome_and_existing_records(self):
        h = self.h
        _, objects = h.publish("A", fixtures.package(), ["mixer"])
        wood = h.item("A", "木柴")["id"]
        for material in ("amber", "blue"):
            h.action("A", objects["mixer"], "charge", item_id=wood, material=material, amount=1)
        content_before = deepcopy(self.content(objects["mixer"]))
        outcomes_before = deepcopy(self.outcomes(content_before))

        h.ticks(1)

        self.assertEqual(creative._quantities(h.entity(objects["mixer"])), {"resin": 2})
        content_after = self.content(objects["mixer"])
        self.assertEqual(content_after["value"]["outcome_count"], content_before["value"]["outcome_count"] + 1)
        outcomes_after = self.outcomes(content_after)
        self.assertEqual(outcomes_after[:-1], outcomes_before)
        self.assertEqual((outcomes_after[-1]["value"]["entity_id"], outcomes_after[-1]["value"]["action"]),
                         (objects["mixer"], "reaction"))


if __name__ == "__main__":
    unittest.main()
