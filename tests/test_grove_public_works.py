"""Public discovery is bounded, current, read-only and separate from action."""
from copy import deepcopy
from dataclasses import replace
import base64
import json
import unittest
from unittest.mock import patch

import test_grove_creative as fixtures
from agent_world import FunctionOutcome, FunctionSpec
from agent_world.errors import RuleViolation
from agent_world.world_checkpoints import CheckpointNotFound
from agent_world.worlds.shared_grove import make_world, model as m, commit, discovery
from agent_world.worlds.shared_grove.definitions import DEFINITIONS


class DirectoryContext:
    """Small seek-store fixture that refuses global player/entity listing."""
    def __init__(self):
        self.actor_role_id = "visitor"
        self.now = 1000
        self.rows = {}
        self.writes = []
        self.list_reads = []
        self.checkpoints = {}
        self.set_state(m.PLAYERS, "visitor", {"id": "visitor", "name": "访客", "x": 0, "y": 0})
        self.writes.clear()

    def get_state(self, scope, key, default=None):
        return deepcopy(self.rows.get((scope, key), {}).get("value", default))

    def get_state_record(self, scope, key):
        row = self.rows.get((scope, key))
        return {"exists": row is not None, "version": row["version"] if row else 0,
                "value": deepcopy(row["value"]) if row else None}

    def set_state(self, scope, key, value, expected_version=None):
        old = self.get_state_record(scope, key)
        if expected_version is not None:
            assert old["version"] == expected_version
        self.rows[(scope, key)] = {"value": deepcopy(value), "version": old["version"] + 1}
        self.writes.append((scope, key))
        return old["version"] + 1

    def delete_state(self, scope, key, expected_version=None):
        self.rows.pop((scope, key), None)
        self.writes.append((scope, key))

    def list_state(self, scope, *, limit=50, after=None, order="key"):
        assert scope not in {m.ENTITIES, m.PLAYERS}, "no world entity/player scans"
        assert order == "key"
        self.list_reads.append((scope, limit))
        keys = sorted(key for s, key in self.rows if s == scope and (after is None or key > after))
        items = [{"key": key, **deepcopy(self.rows[(scope, key)])} for key in keys[:limit]]
        return {"items": items, "has_more": len(keys) > limit,
                "next_cursor": keys[limit - 1] if len(keys) > limit else None}

    def get_checkpoint(self, key):
        if key not in self.checkpoints:
            raise CheckpointNotFound("missing")
        return deepcopy(self.checkpoints[key])

    def schedule_checkpoint(self, key, handler, arguments, *, due_at, expected_generation=None):
        old = self.checkpoints.get(key, {}).get("generation", 0)
        assert expected_generation == old
        self.checkpoints[key] = {"generation": old + 1, "handler": handler, "due_at": due_at}

    def work(self, identity, author="creator", name="作品", x=40, stamp=100, index=True):
        if self.get_state(m.PLAYERS, author) is None:
            self.set_state(m.PLAYERS, author, {"id": author, "name": author, "x": 80, "y": 90,
                                            "inventory": ["private-do-not-project"]})
        entity = {"id": identity, "name": name, "created_by": author, "created_at": stamp,
                  "source": "fabrication", "owner": None, "physical_parent": None, "container": None,
                  "x": x, "y": 0, "lifecycle": "intact", "components": {},
                  "state": {"retired": False, "destroyed": False, "open": True,
                            "program": {"definition_ref": "pinned", "type_id": "sign",
                                        "fields": {"count": 4, "paused": False}}}}
        self.set_state(m.ENTITIES, identity, entity)
        if index:
            discovery.sync_many(self, {identity: entity})
        return entity


class PublicWorksBoundariesTests(unittest.TestCase):
    def test_remote_result_has_real_location_but_no_action_guard_or_private_profile(self):
        ctx = DirectoryContext()
        entity = ctx.work("far-sign", author="小禾", name="循环跳数小牌")
        ctx.writes.clear()
        result = discovery.public_works(ctx, {"author_name": "小禾", "name": "循环跳数小牌"}).result
        row = result["works"][0]
        self.assertEqual(row["location"], {"x": 40, "y": 0})
        self.assertEqual(row["created_by"], entity["created_by"])
        self.assertEqual(row["fields"], {"count": 4, "paused": False})
        self.assertFalse(row["nearby"])
        self.assertEqual(row["read_request"]["requires"], "within_current_nearby_view")
        for field in ("version", "interaction_revision", "action_options", "inventory", "author_location"):
            self.assertNotIn(field, row)
        self.assertEqual(ctx.writes, [])
        self.assertEqual(set(scope for scope, _ in ctx.list_reads), {discovery._scope(discovery.BY_NAME, "循环跳数小牌")})

    def test_current_ownership_position_and_closed_visibility_are_rechecked(self):
        ctx = DirectoryContext()
        entity = ctx.work("sign")
        original_entry = deepcopy(ctx.get_state(discovery.ENTRIES, "sign"))
        entity.update(x=60, y=7)
        entity["state"]["program"]["fields"]["count"] = 9
        ctx.set_state(m.ENTITIES, "sign", entity)
        ctx.writes.clear()
        discovery.sync_many(ctx, {"sign": entity})
        self.assertEqual(ctx.writes, [])
        self.assertEqual(ctx.get_state(discovery.ENTRIES, "sign"), original_entry)
        row = discovery.public_works(ctx, {}).result["works"][0]
        self.assertEqual(row["location"], {"x": 60, "y": 7})
        self.assertEqual(row["fields"]["count"], 9)
        entity["components"]["container"] = {}
        entity["state"]["open"] = False
        ctx.set_state(m.ENTITIES, "sign", entity)
        row = discovery.public_works(ctx, {}).result["works"][0]
        self.assertEqual(row["fields"], {})
        self.assertEqual(row["contents_visibility"], "hidden")
        self.assertEqual(row["fields_omitted_count"], 0)
        entity.update(owner="creator", x=None, y=None)
        ctx.set_state(m.ENTITIES, "sign", entity)
        result = discovery.public_works(ctx, {}).result
        self.assertEqual(result["works"], [])
        self.assertNotIn("private-do-not-project", json.dumps(result))
        ctx.delete_state(m.ENTITIES, "sign")
        self.assertEqual(discovery.public_works(ctx, {}).result["works"], [])

    def test_never_public_and_terrain_objects_never_enter_names_or_author_lookup(self):
        ctx = DirectoryContext()
        for name, changes in (("private", {"owner": "creator", "x": None}),
                              ("child", {"physical_parent": {"id": "closed", "relation": "contained"}}),
                              ("terrain", {"source": "terrain"}), ("destroyed", {}), ("retired", {})):
            entity = ctx.work(name, index=False)
            entity.update(changes)
            if name in {"destroyed", "retired"}:
                entity["state"][name] = True
            discovery.sync_many(ctx, {name: entity})
            self.assertIsNone(ctx.get_state(discovery.ENTRIES, name))
            self.assertIsNone(ctx.get_state(discovery.AUTHORS, "creator"))

    def test_same_names_keep_distinct_ids_and_real_rename_does_not_rewrite_works(self):
        ctx = DirectoryContext()
        for actor in ("one", "two"):
            ctx.work(actor, author=actor, name="小牌")
            player = ctx.get_state(m.PLAYERS, actor)
            player["name"] = "同名"
            ctx.set_state(m.PLAYERS, actor, player)
            discovery.sync_author_name(ctx, player)
        result = discovery.public_works(ctx, {"author_name": "同名"}).result
        self.assertEqual(result["works"], [])
        self.assertEqual({row["author"] for row in result["author_matches"]}, {"one", "two"})
        self.assertTrue(result["author_identity_required"])
        result = discovery.public_works(ctx, {"author_name": "同名", "name": "小牌"}).result
        self.assertEqual({row["created_by"] for row in result["works"]}, {"one", "two"})
        self.assertTrue(result["author_identity_required"])
        before = deepcopy(ctx.get_state(discovery.ENTRIES, "one"))
        player = ctx.get_state(m.PLAYERS, "one")
        player["name"] = "新名字"
        ctx.set_state(m.PLAYERS, "one", player)
        discovery.sync_author_name(ctx, player)
        self.assertEqual(discovery.public_works(ctx, {"author_name": "新名字"}).result["works"][0]["entity_id"], "one")
        self.assertEqual(discovery.public_works(ctx, {"author_name": "同名"}).result["works"][0]["entity_id"], "two")
        self.assertEqual(ctx.get_state(discovery.ENTRIES, "one"), before)

    def test_author_choices_are_bounded_and_last_page_does_not_select_a_name(self):
        ctx = DirectoryContext()
        for index in range(13):
            actor = f"creator-{index:02d}"
            ctx.work(actor, author=actor)
            player = ctx.get_state(m.PLAYERS, actor)
            player["name"] = "共同名"
            ctx.set_state(m.PLAYERS, actor, player)
            discovery.sync_author_name(ctx, player)
        args = {"author_name": "共同名"}
        first = discovery.public_works(ctx, args).result
        self.assertEqual(len(first["author_matches"]), 6)
        self.assertTrue(first["has_more"])
        second = discovery.public_works(ctx, {**args, "after": first["next_cursor"]}).result
        last = discovery.public_works(ctx, {**args, "after": second["next_cursor"]}).result
        self.assertEqual(len(second["author_matches"]), 6)
        self.assertEqual(len(last["author_matches"]), 1)
        self.assertEqual(last["works"], [])
        self.assertFalse(last["has_more"])
        self.assertTrue(last["author_identity_required"])
        self.assertEqual(len(discovery.public_works(ctx, {**args, "limit": 12}).result["author_matches"]), 12)
        with self.assertRaises(RuleViolation):
            discovery.public_works(ctx, {"author_name": "别的名", "after": first["next_cursor"]})

    def test_missing_current_author_name_is_unknown_and_never_an_online_claim(self):
        ctx = DirectoryContext()
        ctx.work("sign")
        ctx.delete_state(m.PLAYERS, "creator")
        self.assertEqual(discovery.public_works(ctx, {"author_name": "creator"}).result["works"], [])
        row = discovery.public_works(ctx, {"author": "creator"}).result["works"][0]
        self.assertIsNone(row["created_by_name"])
        self.assertNotIn("online", row)

    def test_candidate_bound_and_cursor_do_not_turn_an_empty_page_into_absence(self):
        ctx = DirectoryContext()
        ctx.work("still-public", stamp=1)
        for index in range(33):
            entity = ctx.work(f"withdrawn-{index}", stamp=index + 2)
            entity.update(owner="creator", x=None, y=None)
            ctx.set_state(m.ENTITIES, entity["id"], entity)
        ctx.list_reads.clear()
        first = discovery.public_works(ctx, {"author": "creator"}).result
        self.assertEqual(first["works"], [])
        self.assertTrue(first["has_more"])
        self.assertEqual(len(ctx.list_reads), 32)
        second = discovery.public_works(ctx, {"author": "creator", "after": first["next_cursor"]}).result
        self.assertEqual([row["entity_id"] for row in second["works"]], ["still-public"])
        with self.assertRaises(RuleViolation):
            discovery.public_works(ctx, {"author": "changed", "after": first["next_cursor"]})
        for malformed in ("bad", base64.urlsafe_b64encode(b"[]").decode()):
            with self.assertRaises(RuleViolation):
                discovery.public_works(ctx, {"author_name": "missing", "after": malformed})

    def test_backfill_is_seek_paged_resumable_and_does_not_rewrite_source_rows(self):
        ctx = DirectoryContext()
        identities = []
        for index in range(35):
            identities.append(ctx.work(f"legacy-{index}", index=False)["id"])
        ctx.set_state(m.CHUNKS, "chunk_2_0", {"entity_ids": identities})
        sources = deepcopy({key: row for key, row in ctx.rows.items() if key[0] in {m.CHUNKS, m.ENTITIES, m.PLAYERS}})
        discovery.begin(ctx)
        self.assertFalse(discovery.public_works(ctx, {}).result["index_build_complete"])
        for expected in (16, 32, 35):
            if hasattr(ctx, "_grove_discovery_scheduled"):
                del ctx._grove_discovery_scheduled
            discovery.backfill(ctx, {})
            self.assertEqual(sum(scope == discovery.ENTRIES for scope, _ in ctx.rows), expected)
        self.assertTrue(discovery.public_works(ctx, {}).result["index_build_complete"])
        self.assertEqual({key: row for key, row in ctx.rows.items() if key[0] in {m.CHUNKS, m.ENTITIES, m.PLAYERS}}, sources)
        self.assertFalse(any(scope in {m.ENTITIES, m.PLAYERS} for scope, _ in ctx.list_reads))

    def test_new_public_save_during_backfill_is_not_lost_and_completed_build_stays_idle(self):
        ctx = DirectoryContext()
        old_ids = [ctx.work(f"old-{index:02d}", index=False)["id"] for index in range(17)]
        ctx.set_state(m.CHUNKS, "chunk_2_0", {"entity_ids": old_ids})
        discovery.begin(ctx)
        del ctx._grove_discovery_scheduled
        discovery.backfill(ctx, {})
        self.assertFalse(ctx.get_state(discovery.CONTROL, "build")["complete"])
        # This newly materialized spatial key sorts before the build cursor.
        # Its ordinary save hook supplies discovery independently of migration.
        new = ctx.work("new-work", x=-40)
        ctx.set_state(m.CHUNKS, "chunk_-3_0", {"entity_ids": [new["id"]]})
        del ctx._grove_discovery_scheduled
        discovery.backfill(ctx, {})
        self.assertTrue(ctx.get_state(discovery.CONTROL, "build")["complete"])
        self.assertEqual(sum(scope == discovery.ENTRIES for scope, _ in ctx.rows), 18)
        self.assertEqual(discovery.public_works(ctx, {"name": "作品", "limit": 12}).result["works"][0]["entity_id"],
                         "new-work")
        before = deepcopy(ctx.checkpoints)
        ctx.writes.clear()
        discovery.resume(ctx)
        self.assertEqual(ctx.checkpoints, before)
        self.assertEqual(ctx.writes, [])


class PublicWorksSaveIntegrationTests(unittest.TestCase):
    def setUp(self):
        def fixture(ctx, args):
            if "name" in args:
                player = m.get_player(ctx)
                player["name"] = args["name"]
                m.save_player(ctx, player)
            else:
                entity = m.get_entity(ctx, args["entity_id"])
                commit.relocate_tree(ctx, entity, owner=ctx.actor_role_id if args.get("private") else None,
                                     x=None if args.get("private") else args["x"], y=None if args.get("private") else args["y"])
            return FunctionOutcome({})
        spec = FunctionSpec("grove.discovery_fixture", fixture, {"type": "object", "properties": {
            "name": {"type": "string"}, "entity_id": {"type": "string"}, "private": {"type": "boolean"},
            "x": {"type": "integer"}, "y": {"type": "integer"}}, "additionalProperties": False})
        world = make_world()
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        with patch("test_grove_creative.make_world", return_value=replace(world, functions=(*world.functions, spec))):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_saved_public_work_is_discoverable_remote_without_authorizing_inspect(self):
        h = self.h
        ref, objects = h.publish("A", {"key": "far_sign", "name": "Far public work", "version": 1,
            "types": {"sign": {"name": "远处小牌", "body_material": "cloth", "cost": {"cloth": 1}}}}, ["sign"])
        identity = objects["sign"]
        raw_definition = deepcopy(h.world.get_state("w", DEFINITIONS, ref))
        h.call("A", "grove.discovery_fixture", {"entity_id": identity, "x": 50, "y": 50})
        current = deepcopy(h.world.get_state("w", m.ENTITIES, identity))
        found = h.read("B", "grove.public_works", {"author_name": "A", "name": "远处小牌"})
        self.assertEqual(found["works"][0]["location"], {"x": 50, "y": 50})
        self.assertEqual(found["works"][0]["definition_ref"], ref)
        with self.assertRaises(RuleViolation):
            h.read("B", "grove.inspect", {"entity_id": identity})
        h.call("A", "grove.discovery_fixture", {"name": "新作者名"})
        renamed = h.read("B", "grove.public_works", {"author_name": "新作者名"})
        self.assertEqual(renamed["works"][0]["entity_id"], identity)
        self.assertEqual(h.world.get_state("w", m.ENTITIES, identity), current)
        self.assertEqual(h.world.get_state("w", DEFINITIONS, ref), raw_definition)
        h.call("A", "grove.discovery_fixture", {"entity_id": identity, "private": True})
        self.assertEqual(h.read("B", "grove.public_works", {"name": "远处小牌"})["works"], [])
