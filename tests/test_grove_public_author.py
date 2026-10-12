"""Current public author names never replace stable IDs or expose profiles."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_grove_creative as fixtures
from agent_world import FunctionOutcome, FunctionSpec
from agent_world.worlds.shared_grove import make_world, model as m, publication
from agent_world.worlds.shared_grove.definitions import DEFINITIONS


class PublicAuthorFactsTests(unittest.TestCase):
    def setUp(self):
        def author_fixture(ctx, args):
            player = m.get_player(ctx, args["player_id"])
            if args.get("delete"):
                ctx.delete_state(m.PLAYERS, player["id"], expected_version=player["version"])
            else:
                player["name"] = args["name"]
                m.save_player(ctx, player)
            return FunctionOutcome({})
        spec = FunctionSpec("grove.public_author_fixture", author_fixture,
            {"type": "object", "additionalProperties": False, "required": ["player_id"], "properties": {
                "player_id": {"type": "string"}, "name": {"type": "string"}, "delete": {"type": "boolean"}}})
        world = make_world()
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        with patch("test_grove_creative.make_world", return_value=replace(world, functions=(*world.functions, spec))):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def publish(self, actor):
        return self.h.publish(actor, {"key": "public_author", "name": "Public author facts", "version": 1,
            "types": {"sign": {"name": "Author sign", "body_material": "cloth", "cost": {"cloth": 1}}}}, ["sign"])

    def author_rows(self, ref, entity_id):
        h = self.h
        page = h.read("B", "grove.definition_list", {"author": h.roles["A"]})
        summary = next(row for row in page["definitions"] if row["definition_ref"] == ref)
        detail = h.read("B", "grove.definition_read", {"definition_ref": ref})
        inspected = h.read("B", "grove.inspect", {"entity_id": entity_id})["entity"]
        mapped = next(row for row in h.view("B")["entities"] if row["id"] == entity_id)
        return summary, detail, inspected, mapped

    def test_current_name_updates_only_projections_not_definitions_instances_or_directory(self):
        h = self.h
        ref, objects = self.publish("A")
        entity_id = objects["sign"]
        definition_before = h.world.get_state("w", DEFINITIONS, ref)
        entity_before = h.world.get_state("w", m.ENTITIES, entity_id)
        sequence = h.world.get_state("w", m.META, publication.LEDGER_KEY)["value"]["sequence"]
        directory_key = f"{(1 << 63) - 1 - sequence:019d}:{ref}"
        directory_before = deepcopy(h.world.get_state("w", publication.DIRECTORY, directory_key))
        for row in self.author_rows(ref, entity_id):
            field = "created_by_name" if "created_by" in row else "author_name"
            identity = "created_by" if "created_by" in row else "author"
            self.assertEqual(row[field], "A")
            self.assertEqual(row[identity], h.roles["A"])
        h.call("A", "grove.public_author_fixture", {"player_id": h.roles["A"], "name": "Current world name"})
        for row in self.author_rows(ref, entity_id):
            field = "created_by_name" if "created_by" in row else "author_name"
            identity = "created_by" if "created_by" in row else "author"
            self.assertEqual(row[field], "Current world name")
            self.assertEqual(row[identity], h.roles["A"])
            self.assertNotIn("author_profile", row)
            self.assertNotIn("author_inventory", row)
            self.assertNotIn("author_location", row)
        self.assertEqual(h.world.get_state("w", DEFINITIONS, ref), definition_before)
        self.assertEqual(h.world.get_state("w", m.ENTITIES, entity_id), entity_before)
        self.assertEqual(h.world.get_state("w", publication.DIRECTORY, directory_key), directory_before)
        self.assertEqual(h.world.get_role(h.roles["A"])["display_name"], "A")

    def test_deleted_author_has_unknown_current_name_without_erasing_stable_identity(self):
        h = self.h
        ref, objects = self.publish("A")
        h.call("A", "grove.public_author_fixture", {"player_id": h.roles["A"], "delete": True})
        for row in self.author_rows(ref, objects["sign"]):
            field = "created_by_name" if "created_by" in row else "author_name"
            identity = "created_by" if "created_by" in row else "author"
            self.assertIsNone(row[field])
            self.assertEqual(row[identity], h.roles["A"])

    def test_equal_public_names_keep_distinct_author_ids_and_filters(self):
        h = self.h
        a_ref, _ = self.publish("A")
        b_ref = h.call("B", "grove.definition_publish", {"definition": {
            "key": "public_author", "name": "Public author facts", "version": 1,
            "types": {"sign": {"name": "Author sign", "body_material": "cloth", "cost": {"cloth": 1}}}}})["definition_ref"]
        for actor in ("A", "B"):
            h.call(actor, "grove.public_author_fixture", {"player_id": h.roles[actor], "name": "Same name"})
        page = h.read("B", "grove.definition_list")
        by_ref = {row["definition_ref"]: row for row in page["definitions"]}
        self.assertEqual(by_ref[a_ref]["author_name"], by_ref[b_ref]["author_name"])
        self.assertNotEqual(by_ref[a_ref]["author"], by_ref[b_ref]["author"])
        filtered = h.read("B", "grove.definition_list", {"author": h.roles["A"]})
        self.assertEqual([row["definition_ref"] for row in filtered["definitions"]], [a_ref])


class PublicAuthorBoundedReadTests(unittest.TestCase):
    def test_name_reads_one_exact_world_player_key_without_role_or_spatial_discovery(self):
        class Context:
            def __init__(self):
                self.reads = []
            def get_state(self, scope, key):
                self.reads.append((scope, key))
                if key == "missing":
                    return None
                return {"id": key, "name": "Remote current name", "x": 900, "y": 900,
                    "inventory": ["private-item"], "backpack_id": "private-backpack"}
            def list_state(self, *args, **kwargs):
                raise AssertionError("Public names must not scan player scopes")
            def get_role(self, *args, **kwargs):
                raise AssertionError("Public world names must not read identity profiles")
        ctx = Context()
        self.assertEqual(m.public_player_name(ctx, "actual-author"), "Remote current name")
        self.assertIsNone(m.public_player_name(ctx, "missing"))
        self.assertIsNone(m.public_player_name(ctx, None))
        self.assertEqual(ctx.reads, [(m.PLAYERS, "actual-author"), (m.PLAYERS, "missing")])


if __name__ == "__main__":
    unittest.main()
