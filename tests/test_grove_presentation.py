"""Display bindings use actual authorized fields without changing world rules."""
from copy import deepcopy
import unittest

import test_grove_creative as fixtures
from agent_world import WorldRuntime
from agent_world.simulation.presentation import compile_presentation, resolve_presentation
from agent_world.simulation.programs import compile_program
from agent_world.worlds.shared_grove import make_world
from agent_world.worlds.shared_grove.definitions import REGISTRY, BODY_MATERIALS, DEFINITIONS


FIELDS = {"folded": {"type": "boolean", "default": False},
          "count": {"type": "integer", "default": 0, "min": 0, "max": 2}}
DISPLAY = {"shape": "cloth", "color": "#8899aa", "glyph": "◇", "bindings": {
    "color": {"field": "count", "cases": [{"value": 0, "display": "#112233"}, {"value": 1, "display": "#445566"}]},
    "glyph": {"field": "folded", "cases": [{"value": False, "display": "□"}, {"value": True, "display": "■"}]},
    "pose": {"field": "folded", "cases": [{"value": False, "display": "normal"}, {"value": True, "display": "folded"}]},
    "badge": {"field": "count", "label": "次数"}}}


def definition():
    return {"key": "visual_fields", "name": "字段显示", "version": 1, "types": {"sign": {
        "name": "任意显示物件", "body_material": "cloth", "cost": {"cloth": 1},
        "fields": deepcopy(FIELDS), "presentation": deepcopy(DISPLAY),
        "rules": [{"id": "toggle", "phase": "after", "when": True, "effects": [
            {"op": "set", "field": "folded", "value": {"op": "not", "value": {"field": "folded"}}},
            {"op": "set", "field": "count", "value": {"op": "sub", "left": 1, "right": {"field": "count"}}}]}],
        "actions": [{"id": "leave_flat", "label": "展开", "effects": [{"op": "set", "field": "folded", "value": False}]}]}}}


class PresentationCompilerTests(unittest.TestCase):
    def test_legacy_canonical_definition_is_exactly_unchanged(self):
        raw = {"key": "legacy", "name": "Legacy", "version": 1,
               "types": {"body": {"name": "Body", "body_material": "wood", "cost": {"wood": 1}}}}
        compiled = compile_program(raw, materials=REGISTRY.materials, body_materials=BODY_MATERIALS, max_custom_materials=4)
        self.assertEqual(compiled, {"key": "legacy", "name": "Legacy", "version": 1, "materials": {},
            "types": {"body": {"name": "Body", "body_material": "wood", "fields": {}, "rules": [], "actions": [],
                               "cost": {"wood": 1, "stone": 0, "cloth": 0}}}})
        self.assertEqual(resolve_presentation(None, {}, {}, size=2), {"size": 2})

    def test_bindings_resolve_exact_values_and_honestly_show_unknown_or_unmapped(self):
        before = deepcopy(DISPLAY)
        compiled = compile_presentation(DISPLAY, FIELDS)
        initial = resolve_presentation(compiled, FIELDS, {"count": 0, "folded": False}, detailed=True, tick=9)
        self.assertEqual((initial["color"], initial["glyph"], initial["pose"]), ("#112233", "□", "normal"))
        self.assertEqual(initial["badge"]["value"], 0)
        changed = resolve_presentation(compiled, FIELDS, {"count": 1, "folded": True})
        self.assertEqual((changed["color"], changed["glyph"], changed["pose"]), ("#445566", "■", "folded"))
        unmapped = resolve_presentation(compiled, FIELDS, {"count": 2, "folded": False})
        self.assertEqual((unmapped["color"], unmapped["badge"]["value"], unmapped["binding_status"]), ("#8899aa", 2, "unmapped"))
        for actual, visible in (({"count": 1, "folded": True}, False), ({"count": True, "folded": 1}, True), ({}, True)):
            with self.subTest(actual=actual, visible=visible):
                unknown = resolve_presentation(compiled, FIELDS, actual, visible=visible, detailed=True)
                self.assertEqual((unknown["color"], unknown["glyph"], unknown["pose"]), ("#8899aa", "◇", "normal"))
                self.assertFalse(unknown["badge"]["known"])
                self.assertIsNone(unknown["badge"]["value"])
                self.assertTrue(all(not row["known"] and "value" not in row for row in unknown["observation"]["bindings"]))
        self.assertEqual(DISPLAY, before)

    def test_invalid_or_unsupported_bindings_are_rejected_before_publication(self):
        invalid = [
            {"size": 9}, {"color": "url(secret)"}, {"glyph": "abcd"}, {"glyph": "\u202e"},
            {"bindings": {"color": {"field": "missing", "cases": [{"value": 0, "display": "#123456"}]}}},
            {"bindings": {"pose": {"field": "folded", "cases": [{"value": 0, "display": "folded"}]}}},
            {"bindings": {"color": {"field": "count", "cases": [{"value": True, "display": "#123456"}]}}},
            {"bindings": {"color": {"field": "count", "cases": [{"value": 3, "display": "#123456"}]}}},
            {"bindings": {"color": {"field": "count", "cases": [{"value": 0, "display": "#123456"}]*2}}},
            {"bindings": {"pose": {"field": "folded", "cases": [{"value": True, "display": "teleport"}]}}},
            {"bindings": {"badge": {"field": "count", "label": "过长的显示标签"*3}}},
            {"bindings": {"badge": {"field": "count", "source": {"object": "private"}}}},
            {"bindings": {"color": {"field": "count", "cases": []}}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                compile_presentation(value, FIELDS)


class PresentationWorldTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_map_inspection_and_other_player_follow_formal_automatic_state(self):
        h = self.h
        ref, objects = h.publish("A", definition(), ["sign"])
        eid = objects["sign"]
        immutable = h.world.get_state("w", DEFINITIONS, ref)

        def snapshot(actor):
            inspected = h.read(actor, "grove.inspect", {"entity_id": eid})["entity"]
            mapped = next(row for row in h.view(actor)["entities"] if row["id"] == eid)
            self.assertEqual(mapped["presentation"], {key: value for key, value in inspected["presentation"].items() if key != "observation"})
            actual = inspected["creative"]["fields"]
            self.assertEqual(inspected["presentation"]["badge"]["value"], actual["count"])
            self.assertEqual(inspected["presentation"]["pose"], "folded" if actual["folded"] else "normal")
            return inspected

        first = snapshot("A")
        self.assertEqual(snapshot("B")["presentation"], first["presentation"])
        h.ticks(1)
        changed = snapshot("B")
        self.assertNotEqual(changed["presentation"]["color"], first["presentation"]["color"])
        self.assertNotEqual(changed["presentation"]["pose"], first["presentation"]["pose"])
        # An ordinary other-player menu action remains available; display does
        # not become an alternate gameplay path or change the object's matter.
        h.action("B", eid, "leave_flat")
        continued = snapshot("B")
        self.assertEqual(continued["presentation"]["pose"], "normal")
        self.assertEqual(continued["matter"], changed["matter"])
        saved = h.entity(eid)
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        self.assertEqual(h.entity(eid), saved)
        self.assertEqual(snapshot("A")["presentation"], continued["presentation"])
        self.assertEqual(h.world.get_state("w", DEFINITIONS, ref), immutable)

    def test_closed_public_container_never_reveals_display_source_fields(self):
        h = self.h
        raw = definition()
        raw["types"]["sign"]["container_capacity"] = 20
        _, objects = h.publish("A", raw, ["sign"])
        eid = objects["sign"]
        h.approach("A", eid)
        h.interact("A", eid, "close")
        inspected = h.read("B", "grove.inspect", {"entity_id": eid})["entity"]
        display = inspected["presentation"]
        self.assertEqual(display["binding_status"], "unavailable")
        self.assertFalse(display["badge"]["known"])
        self.assertIsNone(display["badge"]["value"])
        self.assertTrue(all(not row["known"] and "value" not in row for row in display["observation"]["bindings"]))
        self.assertNotIn("fields", inspected["creative"])


if __name__ == "__main__":
    unittest.main()
