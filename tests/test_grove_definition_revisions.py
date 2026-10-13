"""Shared rule revisions preserve old publications and real instance identity."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import RuleViolation, StateConflict
from agent_world.world_context import FunctionContext
from agent_world.worlds.shared_grove import make_world, publication
from agent_world.worlds.shared_grove.definitions import DEFINITIONS
import test_grove_creative as fixtures


def counter():
    return {"key": "shared_counter", "name": "共享计数牌", "version": 1, "types": {
        "body": {"name": "计数牌", "body_material": "wood", "cost_u": {"wood": 2},
            "fields": {"count": {"type": "integer", "default": 0, "min": 0, "max": 10000}},
            "rules": [{"id": "count", "phase": "after", "when": True, "effects": [
                {"op": "set", "field": "count", "value": {"op": "add", "left": {"field": "count"}, "right": 1}}]}]}}}


class DefinitionRevisionTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def publish(self, actor, raw, source=None):
        args = {"definition": raw}
        if source is not None:
            args["source_definition_ref"] = source
        return self.h.call(actor, "grove.definition_publish", args)

    def form(self, actor, reference):
        h = self.h
        menu = h.read(actor, "grove.fabrication_options", {"definition_ref": reference, "type_id": "body"})
        args = deepcopy(menu["action"]["arguments"])
        needed = menu["cost_u"]["wood"]
        source = next(row for row in menu["action"]["choices"]["sources"]
                      if any(lot["material"] == "wood" and lot["quantity"] >= needed for lot in row["lots"]))
        lot = next(lot for lot in source["lots"] if lot["material"] == "wood" and lot["quantity"] >= needed)
        args["sources"] = [{"entity_id": source["id"], "expected_entity_version": source["version"],
                            "lot_id": lot["id"], "quantity_u": needed}]
        result = h.call(actor, "grove.fabricate", args)
        entity = result["entity"]["id"]
        h.interact(actor, entity, "drop")
        return entity

    def test_other_author_revises_forms_and_shares_without_upgrading_the_original(self):
        h = self.h
        original = self.publish("A", counter())
        first_ref = original["definition_ref"]
        first = self.form("A", first_ref)
        stored = deepcopy(h.world.get_state("w", DEFINITIONS, first_ref))
        revised = deepcopy(h.read("B", "grove.definition_read", {"definition_ref": first_ref})["definition"])
        spec = revised["types"]["body"]
        spec["fields"]["paused"] = {"type": "boolean", "default": False}
        spec["rules"][0]["when"] = {"op": "not", "value": {"field": "paused"}}
        spec["actions"] = [{"id": key, "label": label, "effects": [{"op": "set", "field": "paused", "value": value}]}
                           for key, label, value in (("pause", "暂停", True), ("resume", "继续", False))]
        published = self.publish("B", revised, first_ref)
        second_ref = published["definition_ref"]
        second = self.form("B", second_ref)
        self.assertNotEqual(first_ref, second_ref)
        self.assertEqual(published["source_definition"]["author"], h.roles["A"])
        self.assertEqual(h.read("A", "grove.definition_read", {"definition_ref": second_ref})["author"], h.roles["B"])
        self.assertEqual(h.entity(first)["state"]["program"]["definition_ref"], first_ref)
        self.assertEqual(h.entity(second)["state"]["program"]["definition_ref"], second_ref)
        h.ticks(2)
        h.action("A", second, "pause")
        first_count = h.entity(first)["state"]["program"]["fields"]["count"]
        second_count = h.entity(second)["state"]["program"]["fields"]["count"]
        h.ticks(3)
        self.assertGreater(h.entity(first)["state"]["program"]["fields"]["count"], first_count)
        self.assertEqual(h.entity(second)["state"]["program"]["fields"]["count"], second_count)
        h.action("A", second, "resume")
        h.ticks(1)
        self.assertGreater(h.entity(second)["state"]["program"]["fields"]["count"], second_count)
        self.assertEqual(h.world.get_state("w", DEFINITIONS, first_ref), stored)
        h.world = WorldRuntime(h.db)
        h.world.install_world("w", make_world())
        read = h.read("A", "grove.definition_read", {"definition_ref": second_ref})
        self.assertEqual(read["source_definition_ref"], first_ref)
        self.assertEqual(read["source_definition"]["read_request"]["arguments"], {"definition_ref": first_ref})
        self.assertEqual(h.world.get_state("w", DEFINITIONS, first_ref), stored)

    def test_old_identity_is_unchanged_and_source_cannot_be_grafted_after_publication(self):
        h = self.h
        first = self.publish("A", counter())
        reference = first["definition_ref"]
        record = deepcopy(h.world.get_state("w", DEFINITIONS, reference))
        expected = "program_" + hashlib.sha256((h.roles["A"] + ":" + record["value"]["hash"]).encode()).hexdigest()
        self.assertEqual(reference, expected)
        self.assertNotIn("source_definition_ref", record["value"])
        repeated = self.publish("A", counter())
        self.assertTrue(repeated["already_published"])
        self.assertEqual(repeated["definition_ref"], reference)
        with self.assertRaises(StateConflict):
            self.publish("A", counter(), reference)
        self.assertEqual(h.world.get_state("w", DEFINITIONS, reference), record)
        revision = counter()
        revision["version"] = 2
        published = self.publish("A", revision, reference)
        self.assertEqual(published["source_definition_ref"], reference)
        usage = deepcopy(h.world.get_state("w", publication.AUTHOR_STORAGE, h.roles["A"]))
        self.assertTrue(self.publish("A", revision, reference)["already_published"])
        self.assertEqual(h.world.get_state("w", publication.AUTHOR_STORAGE, h.roles["A"]), usage)
        with self.assertRaises(StateConflict):
            self.publish("A", revision)
        self.assertEqual(h.world.get_state("w", DEFINITIONS, reference), record)

    def test_missing_source_rejects_without_publication_and_validation_saves_nothing(self):
        h = self.h
        before = deepcopy(h.read("A", "grove.definition_list")["storage"])
        args = {"definition": counter(), "source_definition_ref": "program_missing"}
        checked = h.read("A", "grove.definition_validate", args)
        self.assertFalse(checked["valid"])
        self.assertFalse(checked["world_changed"])
        with self.assertRaises(RuleViolation):
            h.call("A", "grove.definition_publish", args)
        self.assertEqual(h.read("A", "grove.definition_list")["storage"], before)

    def test_revisions_page_uses_one_source_index_and_charges_its_real_storage(self):
        h = self.h
        source = self.publish("A", counter())["definition_ref"]
        references = []
        for index in range(3):
            raw = counter()
            raw["key"] = "revision_" + str(index)
            references.append(self.publish("B", raw, source)["definition_ref"])
        self.publish("B", counter())  # Unrelated publication is not a revision.
        reads = []
        list_state = FunctionContext.list_state

        def observed(ctx, scope, *args, **kwargs):
            reads.append(scope)
            return list_state(ctx, scope, *args, **kwargs)

        with patch.object(FunctionContext, "list_state", observed):
            first = h.read("A", "grove.definition_list", {"source_definition_ref": source, "limit": 2})
            second = h.read("A", "grove.definition_list", {"source_definition_ref": source, "limit": 2, "after": first["next_cursor"]})
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual([row["definition_ref"] for row in first["definitions"] + second["definitions"]], list(reversed(references)))
        self.assertTrue(all(scope == publication._scope(source_definition_ref=source) for scope in reads), reads)
        for row in first["definitions"] + second["definitions"]:
            self.assertEqual(row["source_definition_ref"], source)
            self.assertEqual(row["author_name"], "B")
        usage = h.world.get_state("w", publication.AUTHOR_STORAGE, h.roles["B"])["value"]
        public = h.read("A", "grove.definition_list", {"author": h.roles["B"]})["definitions"]
        actual = sum(publication._publication_bytes(row["definition_ref"],
            h.world.get_state("w", DEFINITIONS, row["definition_ref"])["value"]) for row in public)
        self.assertEqual(usage["payload_bytes"], actual)
        with self.assertRaisesRegex(RuleViolation, "不同时筛选"):
            h.read("A", "grove.definition_list", {"source_definition_ref": source, "author": h.roles["B"]})


if __name__ == "__main__":
    unittest.main()
