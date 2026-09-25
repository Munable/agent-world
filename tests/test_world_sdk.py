from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest

from agent_world.runtime_core import WorldRuntime
from agent_world.errors import (
    InvalidArguments,
    PermissionDenied,
    ReceiptNotFound,
    RuleViolation,
    SchemaRejected,
    WorldDefinitionError,
    WorldVersionMismatch,
)
from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome
from agent_world.universe_loader import get_universe_installer, get_world_definition
from agent_world.transport_contracts import WorldGateway

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


class WorldSDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "world.sqlite3"
        self.w = WorldRuntime(self.path)
        self.a = self.w.create_role("A")["role_id"]
        self.b = self.w.create_role("B")["role_id"]
        self.outsider = self.w.create_role("Outsider")["role_id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_external_module_loads_without_editing_core(self):
        module = Path(self.temp.name) / "independent_world.py"
        module.write_text(
            "from agent_world import WorldDefinition,FunctionSpec,FunctionOutcome\n"
            "def view(ctx,args): return FunctionOutcome({'universe':ctx.universe})\n"
            "WORLD=WorldDefinition('external','External',(FunctionSpec('external.view',view,"
            "{'type':'object','properties':{},'additionalProperties':False},access='read'),))\n",
            encoding="utf-8",
        )
        sys.path.insert(0, self.temp.name)
        try:
            importlib.invalidate_caches()
            get_universe_installer("independent_world:WORLD")(self.w, "custom")
            result = self.w.query_function("custom", "external.view", self.a, {})
            self.assertEqual(result["result"]["universe"], "custom")
        finally:
            sys.path.remove(self.temp.name)
            sys.modules.pop("independent_world", None)

    def test_game_rules_and_random_receipt_live_outside_core(self):
        get_universe_installer("examples.encounter_world:WORLD")(self.w, "campaign")
        self.w.call_function(
            "campaign",
            "encounter.start",
            self.a,
            {"encounter_id": "room", "members": [self.a, self.b]},
            operation_id="start",
        )
        with self.assertRaises(RuleViolation):
            self.w.call_function(
                "campaign",
                "encounter.strike",
                self.b,
                {"encounter_id": "room", "target_role_id": self.a},
                operation_id="wrong-turn",
            )
        strike = self.w.call_function(
            "campaign",
            "encounter.strike",
            self.a,
            {"encounter_id": "room", "target_role_id": self.b},
            operation_id="strike",
        )
        replay = self.w.call_function(
            "campaign",
            "encounter.strike",
            self.a,
            {"encounter_id": "room", "target_role_id": self.b},
            operation_id="strike",
        )
        self.assertEqual(replay["result"], strike["result"])
        self.assertEqual(replay["random_draws"], strike["random_draws"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(PermissionDenied):
            self.w.call_function("campaign", "encounter.inspect", self.outsider, {"encounter_id": "room"})
        restored = WorldRuntime(self.path)
        get_universe_installer("examples.encounter_world:WORLD")(restored, "campaign")
        boot = restored.bootstrap("campaign", self.b)
        self.assertEqual(boot["world_entry_state"]["view"]["encounter_id"], "room")
        self.assertEqual(boot["world_entry_state"]["view"]["state"]["turn"], 1)
        self.assertNotIn("functions", boot)
        self.assertEqual(boot["snapshot_cursor"], boot["latest_event_seq"])

    def test_non_game_world_uses_same_contract_and_atomic_claim(self):
        get_universe_installer("examples.workflow_world:WORLD")(self.w, "work")

        def claim(role):
            try:
                return self.w.call_function(
                    "work", "task.claim", role, {"task_id": "task"}, operation_id="claim"
                )
            except RuleViolation:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(claim, [self.a, self.b]))
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result["result"]["owner"] for result in results if result)
        loser = self.b if winner == self.a else self.a
        with self.assertRaises(PermissionDenied):
            self.w.call_function("work", "task.complete", loser, {"task_id": "task"}, operation_id="complete")
        self.w.call_function("work", "task.complete", winner, {"task_id": "task"}, operation_id="complete")
        self.assertEqual(self.w.get_state("work", "tasks", "task")["value"]["status"], "completed")

    def test_world_instances_are_isolated(self):
        for universe in ("campaign-a", "campaign-b"):
            get_universe_installer("examples.workflow_world:WORLD")(self.w, universe)
        for universe, role in (("campaign-a", self.a), ("campaign-b", self.b)):
            self.w.call_function(universe, "task.claim", role, {"task_id": "same-id"}, operation_id="same-op")
        self.assertEqual(self.w.get_state("campaign-a", "tasks", "same-id")["value"]["owner"], self.a)
        self.assertEqual(self.w.get_state("campaign-b", "tasks", "same-id")["value"]["owner"], self.b)

    def definition(self, version=1, state_version=1, **kwargs):
        def initialize(ctx):
            ctx.set_state("world", "count", 1)

        def read(ctx, args):
            return FunctionOutcome({"value": ctx.get_state("world", "count")})

        return WorldDefinition(
            "migrating",
            "Migrating",
            (FunctionSpec("read.count", read, EMPTY, access="read"),),
            version=version,
            state_version=state_version,
            state_rules=(StateRule("world", "count", {"type": "integer"}),),
            initialize=initialize,
            **kwargs,
        )

    def test_world_description_exposes_entry_instructions_without_inlining_them_in_bootstrap(self):
        definition = self.definition(
            entry_instructions="Inspect current state, then choose only declared actions."
        )
        self.w.install_world("u", definition)
        described = self.w.describe_world("u", self.a)
        self.assertEqual(
            described["entry_instructions"],
            "Inspect current state, then choose only declared actions.",
        )
        self.assertEqual(
            described["world"],
            {
                "id": "migrating",
                "name": "Migrating",
                "version": 1,
                "state_version": 1,
                "api_version": 1,
            },
        )
        boot = self.w.bootstrap("u", self.a)
        self.assertEqual(boot["world_guide"], {"tool": "world.describe"})
        self.assertNotIn("entry_instructions", boot)
        self.assertNotIn("functions", described)

    def test_migration_failure_rolls_back_state_registry_and_version(self):
        first = self.definition()
        self.w.install_world("u", first)

        def fail(ctx):
            ctx.set_state("world", "count", 99)
            raise RuntimeError("injected migration failure")

        second = self.definition(2, 2, migrations={2: fail})
        with self.assertRaises(RuntimeError):
            self.w.install_world("u", second)
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], 1)
        self.assertEqual(self.w.get_world_manifest("u")["version"], 1)
        self.assertEqual(self.w.query_function("u", "read.count", self.a, {})["result"]["value"], 1)

    def test_migration_success_fences_stale_process_and_prevents_downgrade(self):
        first = self.definition()
        self.w.install_world("u", first)
        other = WorldRuntime(self.path)
        other.install_world("u", first)

        def migrate(ctx):
            ctx.set_state("world", "count", ctx.get_state("world", "count") + 1)

        second = self.definition(2, 2, migrations={2: migrate})
        self.w.install_world("u", second)
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], 2)
        with self.assertRaises(WorldVersionMismatch):
            other.query_function("u", "read.count", self.a, {})
        with self.assertRaises(WorldVersionMismatch):
            self.w.install_world("u", first)
        other.install_world("u", second)
        self.assertEqual(other.query_function("u", "read.count", self.a, {})["result"]["value"], 2)

    def test_missing_migration_and_schema_change_without_version_fail(self):
        first = self.definition()
        self.w.install_world("u", first)
        with self.assertRaises(WorldDefinitionError):
            self.w.install_world("u", self.definition(2, 2))
        with self.assertRaises(WorldDefinitionError):
            self.w.install_world("u", replace(first, display_name="Changed"))
        with self.assertRaises(WorldDefinitionError):
            self.w.install_world("u", replace(first, world_id="replacement"))

    def test_invalid_persisted_state_rejects_world_upgrade(self):
        first = self.definition()
        self.w.install_world("u", first)
        second = replace(first, version=2, state_rules=(StateRule("world", "count", {"type": "string"}),))
        with self.assertRaises(SchemaRejected):
            self.w.install_world("u", second)
        self.assertEqual(self.w.get_world_manifest("u")["version"], 1)

    def test_declared_state_schema_enforced_in_transaction(self):
        def invalid(ctx, args):
            ctx.set_state("world", "count", "bad")
            return FunctionOutcome({})

        definition = replace(self.definition(), functions=(FunctionSpec("bad", invalid, EMPTY),))
        self.w.install_world("u", definition)
        with self.assertRaises(SchemaRejected):
            self.w.call_function("u", "bad", self.a, {}, operation_id="bad")
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], 1)
        with self.assertRaises(ReceiptNotFound):
            self.w.get_receipt("u", self.a, "bad")

    def test_state_policy_and_discovery_visibility(self):
        def hidden(ctx, args):
            return FunctionOutcome({"ok": True})

        def own(ctx, scope, key, access):
            return scope == "role:" + ctx.actor_role_id

        def write_other(ctx, args):
            ctx.set_state("role:" + self.b, "value", 1)
            return FunctionOutcome({})

        world = WorldDefinition(
            "policy",
            "Policy",
            (
                FunctionSpec(
                    "hidden",
                    hidden,
                    EMPTY,
                    access="read",
                    visible_to=lambda c: c.actor_role_id == self.a,
                    authorize=lambda c, a: c.actor_role_id == self.a,
                ),
                FunctionSpec("other.write", write_other, EMPTY),
            ),
            state_rules=(StateRule("role:", "value", {"type": "integer"}),),
            state_authorizer=own,
        )
        self.w.install_world("u", world)
        self.assertNotIn(
            "hidden", {d["function_id"] for d in self.w.list_functions("u", actor_role_id=self.b)}
        )
        self.assertNotIn(
            "hidden",
            {d["function_id"] for d in self.w.bootstrap("u", self.b, include_catalog=True)["functions"]},
        )
        with self.assertRaises(PermissionDenied):
            self.w.call_function("u", "hidden", self.b, {})
        with self.assertRaises(PermissionDenied):
            self.w.call_function("u", "other.write", self.a, {}, operation_id="other")

    def test_state_listing_cursor_and_tombstone(self):
        def seed(ctx, args):
            for i in range(5):
                ctx.set_state("s", f"key:{i}", i)
            ctx.delete_state("s", "key:1")
            return FunctionOutcome({})

        def list_(ctx, args):
            page = ctx.list_state("s", prefix="key:", limit=2)
            second = ctx.list_state("s", prefix="key:", limit=2, after=page["next_cursor"])
            with self.assertRaises(InvalidArguments):
                ctx.list_state("other", after=page["next_cursor"])
            return FunctionOutcome({"keys": [r["key"] for r in page["items"] + second["items"]]})

        self.w.register_function("u", "seed", 1, "seed", EMPTY, seed)
        self.w.register_function("u", "list", 1, "list", EMPTY, list_, access="read")
        self.w.call_function("u", "seed", self.a, {}, operation_id="seed")
        self.assertEqual(
            self.w.call_function("u", "list", self.a, {})["result"]["keys"],
            ["key:0", "key:2", "key:3", "key:4"],
        )

    def test_initialization_is_transactional_and_once(self):
        first = self.definition()
        self.w.install_world("u", first)
        self.w.install_world("u", first)
        self.assertEqual(self.w.get_state("u", "world", "count")["version"], 1)
        broken = replace(
            first, world_id="broken", initialize=lambda c: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        with self.assertRaises(RuntimeError):
            self.w.install_world("broken", broken)
        self.assertIsNone(self.w.get_world_manifest("broken"))
        self.assertEqual(self.w.list_functions("broken"), [])

    def test_managed_registry_requires_versioned_world_installation(self):
        self.w.install_world("u", self.definition())
        with self.assertRaises(WorldDefinitionError):
            self.w.register_function(
                "u", "undeclared", 1, "undeclared", EMPTY, lambda c, a: FunctionOutcome({})
            )

    def test_v08_database_upgrades_without_losing_identity_state_or_receipts(self):
        import hashlib

        legacy = Path(self.temp.name) / "legacy.sqlite3"
        token = "awid_" + "legacy-test-credential-not-production"
        receipt = {
            "ok": True,
            "operation_id": "already-done",
            "function_id": "counter.increment",
            "function_version": 1,
            "result": {"value": 5, "state_version": 7},
            "event_seqs": [],
            "replayed": False,
        }
        intent = {"function_id": "counter.increment", "arguments": {"amount": 1}}
        with closing(sqlite3.connect(legacy)) as c:
            c.executescript("""
                CREATE TABLE world_state(universe TEXT NOT NULL,scope TEXT NOT NULL,state_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,version INTEGER NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(universe,scope,state_key));
                CREATE TABLE roles(role_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,avatar_ref TEXT,
                    status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE identity_tokens(token_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,universe TEXT NOT NULL,
                    role_id TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,revoked_at REAL);
                CREATE TABLE operations(universe TEXT NOT NULL,actor_role_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                    function_id TEXT NOT NULL,function_version INTEGER NOT NULL,args_hash TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(universe,actor_role_id,idempotency_key));
                INSERT INTO roles VALUES('legacy-role','Legacy',NULL,'active',1,1);
                INSERT INTO world_state VALUES('legacy','role:legacy-role','counter','5',7,1);
            """)
            c.execute(
                "INSERT INTO identity_tokens VALUES('old-id',?,'legacy','legacy-role',1,NULL,NULL)",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            c.execute(
                "INSERT INTO operations VALUES('legacy','legacy-role','already-done','counter.increment',1,?,?,1)",
                (
                    hashlib.sha256(
                        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    json.dumps(receipt),
                ),
            )
            c.commit()
        upgraded = WorldRuntime(legacy)
        get_universe_installer("demo")(upgraded, "legacy")
        self.assertEqual(upgraded.resolve_identity_token(token)["role_id"], "legacy-role")
        self.assertEqual(upgraded.get_state("legacy", "role:legacy-role", "counter")["version"], 7)
        replay = upgraded.call_function(
            "legacy",
            "counter.increment",
            "legacy-role",
            {"amount": 1},
            operation_id="already-done",
            identity_token=token,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"]["value"], 5)
        boot = upgraded.bootstrap("legacy", "legacy-role", identity_token=token)
        self.assertEqual(boot["world_entry_state"]["view"]["counter"], 5)

    def test_concurrent_database_initialization_and_schema_guard(self):
        fresh = Path(self.temp.name) / "concurrent.sqlite3"
        with ThreadPoolExecutor(8) as pool:
            instances = list(pool.map(lambda _: WorldRuntime(fresh), range(8)))
        self.assertEqual(len(instances), 8)
        with closing(sqlite3.connect(fresh)) as c:
            c.execute("PRAGMA user_version=999")
        with self.assertRaises(WorldVersionMismatch):
            WorldRuntime(fresh)


if __name__ == "__main__":
    unittest.main()
