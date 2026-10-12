"""Publication admission and consumed fact storage use managed world paths."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agent_world import FunctionOutcome, FunctionSpec, StateRule, WorldDefinition, WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.worlds.shared_grove import make_world, model as m, events, publication
from agent_world.worlds.shared_grove.definitions import DEFINITIONS

EMPTY = {"type": "object", "additionalProperties": False, "properties": {}}


def definition(key="storage", version=1):
    return {"key": key, "name": "共享保留物件", "version": version, "types": {
        "body": {"name": "简洁物件", "body_material": "wood", "cost_u": {"wood": 1}}}}


class PublicationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = WorldRuntime(Path(self.temp.name) / "world.sqlite3")
        self.roles = {name: self.runtime.create_role(name)["role_id"] for name in ("A", "B")}
        self.serial = 0

    def install(self, budget=publication.DEFAULT_BUDGET, *, version=None):
        world = make_world(publication_budget=budget)
        if version is not None:
            world = replace(world, version=version)
        self.runtime.install_world("w", world)
        for name in self.roles:
            if not self.runtime.get_state("w", m.PLAYERS, self.roles[name])["exists"]:
                self.call(name, "grove.join", {"name": name})

    def call(self, name, function, args):
        self.serial += 1
        return self.runtime.call_function("w", function, self.roles[name], args,
            operation_id=f"storage-{self.serial}")["result"]

    def publish(self, name="A", raw=None):
        return self.call(name, "grove.definition_publish", {"definition": raw or definition()})

    def stored(self):
        with self.runtime._conn(readonly=True) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM world_state WHERE universe='w' AND (scope=? OR scope=? OR scope LIKE ?) "
                "ORDER BY scope,state_key", (DEFINITIONS, publication.VERSIONS, publication.AUTHORS + "%"))]

    def usage(self, name="A"):
        return self.runtime.get_state("w", publication.AUTHOR_STORAGE, self.roles[name])["value"]

    def test_per_author_budget_accepts_exact_count_and_does_not_charge_same_definition_twice(self):
        self.install(publication.PublicationBudget(definitions=1))
        published = self.publish()
        usage = deepcopy(self.usage())
        stored = self.stored()
        repeat = self.publish()
        self.assertTrue(repeat["already_published"])
        self.assertEqual(repeat["definition_ref"], published["definition_ref"])
        self.assertEqual(self.usage(), usage)
        with self.assertRaisesRegex(RuleViolation, "存储预算已满"):
            self.publish(raw=definition("another"))
        self.assertEqual(self.usage(), usage)
        self.assertEqual(self.stored(), stored)
        other = self.publish("B")
        self.assertNotEqual(other["definition_ref"], published["definition_ref"])
        self.assertEqual(self.usage("B")["definitions_count"], 1)

    def test_tighter_trusted_budget_preserves_existing_content_and_only_refuses_growth(self):
        self.install()
        first = self.publish()
        second = self.publish(raw=definition("another"))
        stored = self.stored()
        before = deepcopy(self.usage())
        self.install(publication.PublicationBudget(payload_bytes=before["payload_bytes"]-1,
                                                   definitions=1), version=make_world().version+1)
        self.assertEqual(self.stored(), stored)
        self.assertEqual(self.publish()["definition_ref"], first["definition_ref"])
        with self.assertRaisesRegex(RuleViolation, "存储预算已满"):
            self.publish(raw=definition("third"))
        self.assertEqual(self.stored(), stored)
        self.assertEqual(self.usage(), before)
        for reference in (first["definition_ref"], second["definition_ref"]):
            read = self.runtime.query_function("w", "grove.definition_read", self.roles["B"],
                {"definition_ref": reference})["result"]
            self.assertEqual(read["definition_ref"], reference)

    def test_json_charge_is_exact_and_catalogue_explains_it_is_not_total_disk_cost(self):
        self.install()
        reference = self.publish()["definition_ref"]
        record = self.runtime.get_state("w", DEFINITIONS, reference)["value"]
        expected = publication._publication_bytes(reference, record)
        self.assertEqual(self.usage()["payload_bytes"], expected)
        self.install(publication.PublicationBudget(payload_bytes=expected), version=make_world().version+1)
        with self.assertRaisesRegex(RuleViolation, "存储预算已满"):
            self.publish(raw=definition(version=2))
        catalog = self.runtime.query_function("w", "grove.definition_list", self.roles["A"], {})["result"]
        self.assertEqual(catalog["author_storage"], self.usage())
        self.assertEqual(catalog["publication_budget"]["payload_bytes"], expected)
        self.assertIn("不是总磁盘占用", catalog["storage_meaning"])
        self.assertIn("SQLite", catalog["publication_budget"]["excludes"])

    def test_old_catalogue_without_usage_backfills_only_author_and_preserves_refs(self):
        self.install()
        first = self.publish()
        original = self.stored()
        expected = deepcopy(self.usage())
        def represent_old_usage(ctx, args):
            row = ctx.get_state_record(publication.AUTHOR_STORAGE, ctx.actor_role_id)
            ctx.delete_state(publication.AUTHOR_STORAGE, ctx.actor_role_id, expected_version=row["version"])
            return FunctionOutcome({})
        world = make_world()
        self.runtime.install_world("w", replace(world, version=world.version+1,
            functions=(*world.functions, FunctionSpec("grove.fixture_old_usage", represent_old_usage, EMPTY))))
        self.call("A", "grove.fixture_old_usage", {})
        catalog = self.runtime.query_function("w", "grove.definition_list", self.roles["A"], {})["result"]
        self.assertEqual(catalog["author_storage"], expected)
        self.assertEqual(self.stored(), original)
        self.publish(raw=definition("second"))
        self.assertEqual(self.usage()["definitions_count"], 2)
        self.assertTrue(self.runtime.get_state("w", DEFINITIONS, first["definition_ref"])["exists"])


class ConsumedFactStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = WorldRuntime(Path(self.temp.name) / "world.sqlite3")
        self.actor = self.runtime.create_role("consumer")["role_id"]
        self.serial = 0
        event = {"sequence": 1, "action": "move", "x": 2, "y": 2}
        def initialize(ctx):
            ctx.set_state(m.META, "creative_events", {"sequence": 1, "events": [event]})
            ctx.set_state(events.FACTS, "1", {"event": event, "references": 2})
            for name in ("left", "right"):
                ctx.set_state(events.PENDING, name, {"sequences": [1], "payload_bytes": sm.encoded_size(event),
                    "consumed_through": 0, "paused": False, "gap": None})
            ctx.set_state("reusable", "index", {"members": ["left"]})
        def consume(ctx, args):
            events.consume(ctx, args["consumer"], 1)
            return FunctionOutcome({"consumed": args["consumer"]})
        def pause(ctx, args):
            row = ctx.get_state_record(events.PENDING, "right")
            value = row["value"]
            value["paused"] = True
            value["gap"] = {"from_sequence": 2, "reason": "pending_capacity", "last_consumed_sequence": 0}
            ctx.set_state(events.PENDING, "right", value, expected_version=row["version"])
            return FunctionOutcome({})
        def forget(ctx, args):
            row = ctx.get_state_record(args["scope"], args["key"])
            if args.get("delete"):
                ctx.delete_state(args["scope"], args["key"], expected_version=row["version"])
                row = ctx.get_state_record(args["scope"], args["key"])
            ctx.forget_state(args["scope"], args["key"], expected_version=args.get("version", row["version"]))
            return FunctionOutcome({})
        def reusable(ctx, args):
            row = ctx.get_state_record("reusable", "index")
            if args["delete"]:
                ctx.delete_state("reusable", "index", expected_version=row["version"])
            else:
                ctx.set_state("reusable", "index", {"members": ["right"]}, expected_version=row["version"])
            return FunctionOutcome({})
        self.runtime.install_world("w", WorldDefinition("fact-storage", "Fact storage", (
            FunctionSpec("consume", consume, {"type": "object", "properties": {"consumer": {"type": "string"}},
                "required": ["consumer"], "additionalProperties": False}),
            FunctionSpec("pause", pause, EMPTY), FunctionSpec("forget", forget, {"type": "object"}),
            FunctionSpec("reusable", reusable, {"type": "object", "properties": {"delete": {"type": "boolean"}},
                "required": ["delete"], "additionalProperties": False})),
            initialize=initialize, state_rules=(*events.state_specs(), StateRule(m.META, "", {}),
                                                StateRule("reusable", "", {})),
            state_forget_authorizer=events.state_forget_authorizer))

    def call(self, function, args):
        self.serial += 1
        return self.runtime.call_function("w", function, self.actor, args, operation_id=f"fact-{self.serial}")

    def rows(self, scope):
        with self.runtime._conn(readonly=True) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM world_state WHERE universe='w' AND scope=?", (scope,))]

    def test_last_consumer_reclaims_never_reused_fact_without_a_second_delete_history(self):
        self.call("consume", {"consumer": "left"})
        self.assertEqual(self.runtime.get_state("w", events.FACTS, "1")["value"]["references"], 1)
        self.call("consume", {"consumer": "right"})
        self.assertEqual(self.rows(events.FACTS), [])
        self.assertEqual(self.runtime.get_state("w", events.FACTS, "1")["version"], 0)
        with self.runtime._conn(readonly=True) as connection:
            changes = list(connection.execute("SELECT kind,version FROM state_changes WHERE universe='w' AND scope=? ORDER BY seq",
                                              (events.FACTS,)))
        self.assertEqual([tuple(row) for row in changes], [("create", 1), ("update", 2), ("delete", 3)])
        self.call("consume", {"consumer": "right"})
        self.assertEqual(self.rows(events.FACTS), [])

    def test_paused_consumers_preserve_original_unread_body_and_prefix(self):
        self.call("pause", {})
        self.call("consume", {"consumer": "left"})
        body = deepcopy(self.runtime.get_state("w", events.FACTS, "1"))
        prefix = deepcopy(self.runtime.get_state("w", events.PENDING, "right"))
        self.call("consume", {"consumer": "right"})
        self.assertEqual(self.runtime.get_state("w", events.FACTS, "1"), body)
        self.assertEqual(self.runtime.get_state("w", events.PENDING, "right"), prefix)

    def test_live_facts_and_reusable_cas_keys_cannot_be_forgotten(self):
        with self.assertRaises(PermissionDenied):
            self.call("forget", {"scope": events.FACTS, "key": "1"})
        with self.assertRaises(PermissionDenied):
            self.call("forget", {"scope": "reusable", "key": "index", "delete": True})
        self.assertTrue(self.runtime.get_state("w", "reusable", "index")["exists"])
        self.assertEqual(self.runtime.get_state("w", events.FACTS, "1")["value"]["references"], 2)
        with self.assertRaises(StateConflict):
            self.call("forget", {"scope": events.FACTS, "key": "1", "version": 10})

    def test_reusable_indexes_keep_their_cas_tombstone_then_reactivate_at_next_version(self):
        self.call("reusable", {"delete": True})
        tombstone = self.runtime.get_state("w", "reusable", "index")
        self.assertFalse(tombstone["exists"])
        self.assertEqual(tombstone["version"], 2)
        with self.assertRaises(PermissionDenied):
            self.call("forget", {"scope": "reusable", "key": "index"})
        self.assertEqual(self.runtime.get_state("w", "reusable", "index"), tombstone)
        self.call("reusable", {"delete": False})
        self.assertEqual(self.runtime.get_state("w", "reusable", "index")["version"], 3)


if __name__ == "__main__":
    unittest.main()
