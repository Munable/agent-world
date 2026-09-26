from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, TimerSpec
from agent_world.errors import PermissionDenied, ReceiptNotFound, SchemaRejected

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def raw_count(ctx, value="2", version_step=1):
    ctx.conn.execute(
        "UPDATE world_state SET value_json=?,version=version+?,updated_at=? "
        "WHERE universe=? AND scope='world' AND state_key='count'",
        (value, version_step, ctx.now, ctx.universe),
    )
    return FunctionOutcome({})


class ManagedStateBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.w = WorldRuntime(Path(temp.name) / "state.sqlite3")
        self.actor = self.w.create_role("Writer")["role_id"]

    def definition(self, handler, *, allowed=True, policy=None):
        def initialize(ctx):
            ctx.set_state("world", "count", 1)
            ctx.set_state("permissions", "edit", allowed)
        return WorldDefinition(
            "boundary-test", "Boundary Test", (FunctionSpec("change", handler, EMPTY),),
            state_rules=(StateRule("world", "count", {"type": "integer"}),
                         StateRule("permissions", "edit", {"type": "boolean"})),
            initialize=initialize, state_authorizer=policy,
        )

    def call(self, universe="u"):
        return self.w.call_function(universe, "change", self.actor, {}, operation_id="change")

    def test_raw_policy_can_use_authorization_state_and_deny_access(self):
        def policy(ctx, scope, key, access):
            return ctx.authorization_state("permissions", "edit", False)
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                universe = str(allowed)
                self.w.install_world(universe, self.definition(lambda c, a: raw_count(c),
                                                               allowed=allowed, policy=policy))
                if allowed:
                    self.call(universe)
                else:
                    with self.assertRaises(PermissionDenied):
                        self.call(universe)
                self.assertEqual(self.w.get_state(universe, "world", "count")["value"],
                                 2 if allowed else 1)

    def test_sdk_self_revocation_is_not_reauthorized_after_the_write(self):
        def policy(ctx, scope, key, access):
            return ctx.authorization_state("permissions", "edit", False)
        def revoke(ctx, args):
            ctx.set_state("permissions", "edit", False)
            return FunctionOutcome({})
        self.w.install_world("u", self.definition(revoke, policy=policy))
        self.call()
        self.assertFalse(self.w.get_state("u", "permissions", "edit")["value"])

    def test_raw_version_skip_rolls_back(self):
        self.w.install_world("u", self.definition(lambda c, a: raw_count(c, version_step=2)))
        revision = self.w.read_state_history("u")["latest_revision"]
        with self.assertRaises(SchemaRejected):
            self.call()
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], 1)
        self.assertEqual(self.w.read_state_history("u")["latest_revision"], revision)
        with self.assertRaises(ReceiptNotFound):
            self.w.get_receipt("u", self.actor, "change")

    def test_undeclared_raw_state_is_rejected(self):
        def undeclared(ctx, args):
            ctx.conn.execute("INSERT INTO world_state VALUES(?,?,?,?,?,?,?)",
                             (ctx.universe, "unknown", "key", "1", 1, ctx.now, 0))
            return FunctionOutcome({})
        self.w.install_world("u", self.definition(undeclared))
        with self.assertRaises(SchemaRejected):
            self.call()

    def test_migration_validates_final_schema_not_transient_shapes(self):
        first = self.definition(lambda c, a: raw_count(c))
        self.w.install_world("u", first)
        def migrate(ctx):
            ctx.set_state("world", "count", {"temporary": 1})
            ctx.set_state("world", "count", "migrated")
        second = replace(first, version=2, state_version=2, migrations={2: migrate},
                         state_rules=(StateRule("world", "count", {"type": "string"}),
                                      StateRule("permissions", "edit", {"type": "boolean"})))
        self.w.install_world("u", second)
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], "migrated")

    def test_raw_initialization_bad_version_rolls_back_manifest(self):
        def initialize(ctx):
            ctx.conn.execute("INSERT INTO world_state VALUES(?,?,?,?,?,?,?)",
                             (ctx.universe, "world", "count", "1", 7, ctx.now, 0))
        definition = replace(self.definition(lambda c, a: raw_count(c)), initialize=initialize)
        with self.assertRaises(SchemaRejected):
            self.w.install_world("u", definition)
        self.assertIsNone(self.w.get_world_manifest("u"))
        self.assertEqual(self.w.read_state_history("u")["changes"], [])

    def test_timer_raw_schema_failure_cannot_commit_state(self):
        def arm(ctx, args):
            ctx.schedule_timer("bad-timer", "bad", {}, due_at=0)
            return FunctionOutcome({})
        definition = replace(self.definition(arm),
                             timers=(TimerSpec("bad", lambda c, a: raw_count(c, '"bad"'), EMPTY),))
        self.w.install_world("u", definition)
        self.call()
        revision = self.w.read_state_history("u")["latest_revision"]
        self.w.run_due_timers("u")
        self.assertEqual(self.w.get_state("u", "world", "count")["value"], 1)
        self.assertEqual(self.w.read_state_history("u")["latest_revision"], revision)
        self.assertIn(self.w.get_timer("u", "bad-timer")["status"], {"failed", "rejected"})


if __name__ == "__main__":
    unittest.main()
