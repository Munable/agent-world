"""Stored creations outlive execution seats through ordinary product calls."""
from copy import deepcopy
import unittest

from agent_world import WorldRuntime
from agent_world.worlds.shared_grove import execution, make_world, model as m
import test_grove_creation as fixtures


class CreationGrowthTests(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.CreationTests(methodName="runTest")
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)

    def test_more_than_32_saved_static_creations_sleep_and_remain_usable_after_restart(self):
        h = self.c.h
        reference = self.c.publish({"key": "saved_markers", "name": "保存与执行分离", "version": 1,
            "types": {"marker": {"name": "可标记木件", "body_material": "wood", "cost_u": {"wood": 2},
                "fields": {"marked": {"type": "boolean", "default": False}},
                "actions": [{"id": "mark", "label": "留下标记", "effects": [
                    {"op": "set", "field": "marked", "value": True}]}]}}})

        # Obtain finite stock through the existing harvest action. Formation
        # pays 66 actual wood units; a publication or timer creates none of it.
        before_stock = h.read("A", "grove.fabrication_options", {
            "definition_ref": reference, "type_id": "marker"})["materials"][0]["available_u"]
        node = next(entity for entity in h.view()["entities"]
                    if entity["state"].get("resource_kind") == "wood" and entity["state"].get("stock", 0) > 0)
        stock = node["state"]["stock"]
        h.approach("A", node["id"])
        h.interact("A", node["id"], "harvest")
        self.assertEqual(h.entity(node["id"])["state"]["stock"], stock-1)
        available = h.read("A", "grove.fabrication_options", {
            "definition_ref": reference, "type_id": "marker"})["materials"][0]["available_u"]
        self.assertGreater(available, before_stock)
        self.assertGreaterEqual(available, 33*2)

        created = []
        for index in range(33):
            created.append(self.c.form(reference, "marker"))
            # A real drop frees the protected backpack slot and stores the
            # actual public object. The objects have no blocking capability.
            self.c.place(created[-1])
            if (index+1) % 8 == 0:
                h.ticks(1)
                self.assertTrue(set(created).isdisjoint(
                    h.world.get_state("w", m.META, "creative_instances")["value"]))
        h.ticks(1)
        remaining = h.read("A", "grove.fabrication_options", {
            "definition_ref": reference, "type_id": "marker"})["materials"][0]["available_u"]
        self.assertEqual(remaining, available-33*2)
        self.assertEqual(len(set(created)), 33)
        for entity_id in created:
            self.assertEqual(h.world.get_state("w", execution.MEMBERS, entity_id)["value"]["status"], "sleeping")
            self.assertIsNone(h.entity(entity_id)["owner"])

        # The first saved object remains discoverable and actionable after the
        # former world-wide instance limit has been exceeded.
        first = created[0]
        h.action("B", first, "mark")
        self.assertTrue(h.entity(first)["state"]["program"]["fields"]["marked"])
        h.ticks(1)
        self.assertEqual(h.world.get_state("w", execution.MEMBERS, first)["value"]["status"], "sleeping")
        saved = {entity_id: deepcopy(h.entity(entity_id)) for entity_id in created}
        self.assertTrue(set(saved).isdisjoint(
            h.world.get_state("w", m.META, "creative_instances")["value"]))

        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual({entity_id: h.entity(entity_id) for entity_id in created}, saved)
        inspected = h.read("B", "grove.inspect", {"entity_id": first})
        self.assertTrue(inspected["entity"]["creative"]["fields"]["marked"])
        self.assertTrue(any(row["action"] == "mark" and row["available"] for row in inspected["action_options"]))
