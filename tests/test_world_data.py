from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, StateRule, FunctionOutcome, ViewSpec
from agent_world.world_sdk import install_world
from agent_world.errors import PermissionDenied, RuleViolation, InvalidIdentityToken, CursorExpired, WorldRuntimeError
from agent_world.world_views import ViewResetRequired, ResultRejected
from agent_world.world_views import diff_view

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ARGS = {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "object"}},
        "required": ["key", "value"], "additionalProperties": False}


def change(ctx, args):
    version = ctx.set_state("objects", args["key"], args["value"])
    return FunctionOutcome({"version": version})  # Intentionally NO recipient event.


def erase(ctx, args):
    return FunctionOutcome({"version": ctx.delete_state("objects", args["key"])})


def bad_batch(ctx, args):
    ctx.set_state("objects", "first", {"value": 1})
    ctx.set_state("objects", "second", {"value": 2})
    raise RuleViolation("rollback all changes")


def projection(ctx, args):
    entities = {}
    for item in ctx.list_state("objects", limit=100)["items"]:
        value = item["value"]
        if not value.get("visible_to") or ctx.actor_role_id in value["visible_to"]:
            entities[item["key"]] = {k: v for k, v in value.items() if k != "visible_to"}
    return {"entities": entities, "meta": {"kind": "records"}}


WORLD = WorldDefinition(
    "data-test", "Data Test",
    (FunctionSpec("object.change", change, ARGS), FunctionSpec("object.erase", erase, ARGS),
     FunctionSpec("object.bad_batch", bad_batch, EMPTY)),
    state_rules=(StateRule("objects", "", {"type": "object"}),),
    views=(ViewSpec("main", projection),),
)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.path)
        install_world(self.runtime, "u", WORLD)
        self.a = self.runtime.create_role("A")["role_id"]
        self.b = self.runtime.create_role("B")["role_id"]
        self.ta = self.runtime.issue_identity_token("u", self.a)["token"]
        self.tb = self.runtime.issue_identity_token("u", self.b)["token"]

    def write(self, key, value, operation="op1", **kw):
        return self.runtime.call_function("u", "object.change", self.a, {"key": key, "value": value},
            operation_id=operation, identity_token=self.ta, **kw)

    def snapshot(self):
        return self.runtime.view_snapshot("u", self.a, "main", identity_token=self.ta)

    def sync(self, cursor):
        return self.runtime.view_sync("u", self.a, cursor, identity_token=self.ta)

    def history(self):
        return self.runtime.read_state_history("u")["changes"]

    def count(self, table):
        with self.runtime._conn(readonly=True) as c:
            return c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_no_notification_still_creates_history_and_view_delta(self):
        snap = self.snapshot()
        receipt = self.write("door", {"open": True})
        self.assertEqual(self.runtime.read_changes("u", self.a), [])
        history = self.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["commit_seq"], receipt["commit_seq"])
        delta = self.sync(snap["cursor"])
        self.assertEqual(delta["delta"]["entities"]["upsert"], {"door": {"open": True}})
        self.assertNotEqual(delta["view_revision"], snap["view_revision"])
        self.assertNotIn("state_revision", delta)

    def test_failed_action_leaves_no_state_history_or_commit(self):
        before = self.count("world_commits")
        with self.assertRaises(RuleViolation):
            self.runtime.call_function("u", "object.bad_batch", self.a, {}, operation_id="bad", identity_token=self.ta)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.count("world_commits"), before)
        self.assertEqual(self.snapshot()["snapshot"]["entities"], {})

    def test_replay_has_same_commit_and_no_new_history(self):
        first = self.write("x", {"n": 1})
        second = self.write("x", {"n": 1})
        self.assertEqual(first["commit_seq"], second["commit_seq"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.history()), 1)

    def test_update_and_delete_keep_previous_contents(self):
        self.write("x", {"n": 1})
        self.write("x", {"n": 2}, "op2")
        snap = self.snapshot()
        self.runtime.call_function("u", "object.erase", self.a, {"key": "x", "value": {}}, operation_id="del", identity_token=self.ta)
        history = self.history()
        self.assertEqual(history[-1]["before"], {"n": 2})
        self.assertEqual(history[-1]["kind"], "delete")
        self.assertEqual(history[-2]["before"], {"n": 1})
        self.assertEqual(self.sync(snap["cursor"])["delta"]["entities"]["remove"], ["x"])

    def test_event_cleanup_does_not_remove_state_history(self):
        self.write("x", {"n": 1})
        self.runtime.cleanup_events("u", 99999)
        self.assertEqual(len(self.history()), 1)

    def test_explicit_history_retention_keeps_current_state(self):
        self.write("x", {"n": 1})
        seq = self.history()[-1]["seq"]
        self.assertEqual(self.runtime.prune_state_history("u", seq), 1)
        with self.assertRaises(CursorExpired):
            self.history()
        snap = self.snapshot()
        self.assertEqual(snap["snapshot"]["entities"]["x"]["n"], 1)
        with self.runtime._conn(readonly=True) as c:
            checkpoint = c.execute("SELECT state_revision FROM view_checkpoints WHERE cursor_hash=?",
                                   (self.runtime._view_cursor_hash(snap["cursor"]),)).fetchone()
        self.assertEqual(checkpoint[0], seq)

    def test_raw_sql_is_journaled_but_cannot_forge_history(self):
        def raw(ctx, args):
            ctx.conn.execute("UPDATE world_state SET value_json='{}',version=version+1 WHERE universe=?", (ctx.universe,))
            return FunctionOutcome({})
        other = WorldRuntime(Path(self.temp.name) / "raw.sqlite3")
        other.register_function("raw", "raw.change", 1, "", EMPTY, raw)
        with other._conn() as c:
            c.execute("INSERT INTO world_state VALUES('raw','objects','x','{}',1,0,0)")
        other.call_function("raw", "raw.change", "A", {}, operation_id="raw")
        self.assertEqual(len(other.read_state_history("raw")["changes"]), 2)
        def forge(ctx, args):
            ctx.conn.execute("DELETE FROM state_changes")
            return FunctionOutcome({})
        other.register_function("raw", "raw.forge", 1, "", EMPTY, forge)
        with self.assertRaises(WorldRuntimeError):
            other.call_function("raw", "raw.forge", "A", {}, operation_id="forge")
        self.assertEqual(len(other.read_state_history("raw")["changes"]), 2)

    def test_cross_universe_raw_write_rolls_back(self):
        def wrong(ctx, args):
            ctx.conn.execute("INSERT INTO world_state VALUES('other','x','x','{}',1,0,0)")
            return FunctionOutcome({})
        other = WorldRuntime(Path(self.temp.name) / "wrong.sqlite3")
        other.register_function("u", "raw.wrong", 1, "", EMPTY, wrong)
        with self.assertRaises(PermissionDenied):
            other.call_function("u", "raw.wrong", "A", {}, operation_id="wrong")
        self.assertEqual(other.read_state_history("other")["changes"], [])

    def test_permission_change_removes_entity_without_sending_private_contents(self):
        self.write("secret", {"visible_to": [self.a], "text": "old"})
        snap = self.snapshot()
        self.write("secret", {"visible_to": [self.b], "text": "HIDDEN_NEW_SECRET"}, "op2")
        update = self.sync(snap["cursor"])
        self.assertEqual(update["delta"]["entities"]["remove"], ["secret"])
        self.assertNotIn("HIDDEN_NEW_SECRET", json.dumps(update))

    def test_hidden_writes_do_not_change_public_view_revision(self):
        before = self.snapshot()
        self.write("hidden", {"visible_to": [self.b], "text": "private"})
        after = self.sync(before["cursor"])
        self.assertEqual(after["view_revision"], before["view_revision"])
        self.assertNotIn("state_revision", after)
        self.assertEqual(after["delta"]["entities"], {"upsert": {}, "remove": []})

    def test_initial_projection_hides_other_role_state(self):
        self.write("secret", {"visible_to": [self.b], "text": "HIDDEN"})
        self.assertNotIn("HIDDEN", json.dumps(self.snapshot()))

    def test_cursor_is_scoped_to_role_universe_and_credential(self):
        snap = self.snapshot()
        with self.assertRaises(ViewResetRequired):
            self.runtime.view_sync("u", self.b, snap["cursor"], identity_token=self.tb)
        second = self.runtime.issue_identity_token("u", self.a)["token"]
        with self.assertRaises(ViewResetRequired):
            self.runtime.view_sync("u", self.a, snap["cursor"], identity_token=second)
        install_world(self.runtime, "other", WORLD)
        token = self.runtime.issue_identity_token("other", self.a)["token"]
        with self.assertRaises(ViewResetRequired):
            self.runtime.view_sync("other", self.a, snap["cursor"], identity_token=token)

    def test_expired_or_forged_cursor_requires_reset(self):
        snap = self.snapshot()
        with self.assertRaises(ViewResetRequired):
            self.sync(snap["cursor"] + "bad")
        with patch("agent_world.runtime_views.time.time", return_value=snap["expires_at"] + 1):
            with self.assertRaises(ViewResetRequired):
                self.sync(snap["cursor"])

    def test_revoked_token_cannot_read_cached_view(self):
        snap = self.snapshot()
        tid = self.runtime.resolve_identity_token(self.ta)["token_id"]
        self.runtime.revoke_identity_token(tid)
        with self.assertRaises(InvalidIdentityToken):
            self.sync(snap["cursor"])

    def test_view_cache_is_not_world_state_or_history(self):
        snap = self.snapshot()
        before = (self.count("world_commits"), self.count("operations"), len(self.history()))
        for _ in range(22):
            snap = self.sync(snap["cursor"])
        self.assertLessEqual(self.count("view_checkpoints"), 16)
        self.assertEqual(before, (self.count("world_commits"), self.count("operations"), len(self.history())))

    def test_cache_survives_restart_but_schema_change_requires_reset(self):
        snap = self.snapshot()
        restart = WorldRuntime(self.path)
        install_world(restart, "u", WORLD)
        update = restart.view_sync("u", self.a, snap["cursor"], identity_token=self.ta)
        self.assertEqual(update["base_cursor"], snap["cursor"])
        install_world(restart, "u", replace(WORLD, version=2))
        with self.assertRaises(ViewResetRequired):
            restart.view_sync("u", self.a, update["cursor"], identity_token=self.ta)

    def test_view_projection_cannot_write(self):
        def bad(ctx, args):
            ctx.set_state("objects", "bad", {})
            return {}
        install_world(self.runtime, "u", replace(WORLD, version=2, views=(ViewSpec("main", bad),)))
        with self.assertRaises(WorldRuntimeError):
            self.snapshot()
        self.assertEqual(self.history(), [])

    def test_view_size_and_resource_schemes_are_bounded(self):
        with self.assertRaises(WorldRuntimeError):
            ViewSpec("big", projection).validate_result({"entities": {"x": {"large": "a" * 100000}}})
        for uri in ("javascript:alert(1)", "file:///etc/passwd", "https://name:password@example.org/a"):
            with self.assertRaises(ResultRejected):
                ViewSpec("bad", projection).validate_result({"resources": {"a": {"uri": uri, "media_type": "image/png"}}})
        body = ViewSpec("ok", projection).validate_result({"resources": {"a": {
            "uri": "asset://pack/portrait", "media_type": "image/png", "sha256": "a" * 64, "version": "1"}}})
        self.assertIn("a", body["resources"])

    def test_diff_includes_resource_removal(self):
        old = {"entities": {"door": {"open": False}}, "resources": {"a": {}}, "meta": {}}
        new = {"entities": {"door": {"open": True}}, "resources": {}, "meta": {"mode": "changed"}}
        self.assertEqual(diff_view(old, new)["resources"]["remove"], ["a"])

    def test_snapshot_revision_and_values_share_one_read_transaction(self):
        self.write("x", {"n": 1})
        self.write("y", {"n": 1}, "op2")
        started, completed = threading.Event(), threading.Event()
        def slow(ctx, args):
            x = ctx.get_state("objects", "x")
            started.set()
            if not completed.wait(3):
                raise RuntimeError("writer did not complete")
            y = ctx.get_state("objects", "y")
            return {"entities": {"x": x, "y": y}}
        definition = replace(WORLD, version=2, views=(ViewSpec("main", slow),))
        install_world(self.runtime, "u", definition)
        other = WorldRuntime(self.path)
        install_world(other, "u", definition)
        before = self.history()[-1]["seq"]
        def writer():
            if not started.wait(3):
                raise RuntimeError("projection did not start")
            other.call_function("u", "object.change", self.a, {"key": "y", "value": {"n": 2}},
                                operation_id="op3", identity_token=self.ta)
            completed.set()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(writer)
            snap = self.snapshot()
            future.result(timeout=3)
        with self.runtime._conn(readonly=True) as c:
            checkpoint = c.execute("SELECT state_revision FROM view_checkpoints WHERE cursor_hash=?",
                                   (self.runtime._view_cursor_hash(snap["cursor"]),)).fetchone()
        self.assertEqual(checkpoint[0], before)
        self.assertEqual(snap["snapshot"]["entities"]["y"], {"n": 1})

    def test_migration_history_is_atomic(self):
        self.write("x", {"n": 1})
        def bad(ctx):
            ctx.set_state("objects", "x", {"n": 2})
            raise RuleViolation("bad migration")
        before = len(self.history())
        with self.assertRaises(RuleViolation):
            install_world(self.runtime, "u", replace(WORLD, version=2, state_version=2, migrations={2: bad}))
        self.assertEqual(len(self.history()), before)
        def good(ctx):
            ctx.set_state("objects", "x", {"n": 2})
        install_world(self.runtime, "u", replace(WORLD, version=2, state_version=2, migrations={2: good}))
        change = self.history()[-1]
        with self.runtime._conn(readonly=True) as c:
            commit = c.execute("SELECT * FROM world_commits WHERE seq=?", (change["commit_seq"],)).fetchone()
        self.assertEqual(commit["source"], "migration")
        self.assertEqual(change["before"], {"n": 1})

    def test_removed_view_requires_new_snapshot(self):
        snap = self.snapshot()
        install_world(self.runtime, "u", replace(WORLD, version=2, views=()))
        with self.assertRaises(ViewResetRequired):
            self.sync(snap["cursor"])

    def test_whole_view_policy_is_rechecked(self):
        def allowed(ctx, args):
            return ctx.get_state("objects", "access", {}).get("allow") is True
        install_world(self.runtime, "u", replace(WORLD, version=2, views=(ViewSpec("main", projection, authorize=allowed),)))
        self.write("access", {"allow": True})
        snap = self.snapshot()
        self.write("access", {"allow": False}, "op2")
        with self.assertRaises(PermissionDenied):
            self.sync(snap["cursor"])

    def test_commit_budget_failure_rolls_back_history(self):
        def too_many(ctx, args):
            for index in range(513):
                ctx.set_state("objects", str(index), {})
            return FunctionOutcome({})
        definition = replace(WORLD, version=2, functions=(FunctionSpec("object.many", too_many, EMPTY),))
        install_world(self.runtime, "u", definition)
        before = self.count("world_commits")
        with self.assertRaises(WorldRuntimeError):
            self.runtime.call_function("u", "object.many", self.a, {}, operation_id="too-many", identity_token=self.ta)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.count("world_commits"), before)

    def test_legacy_baseline_is_explicit_and_not_repeated(self):
        legacy = Path(self.temp.name) / "old.sqlite3"
        with closing(sqlite3.connect(legacy)) as c:
            c.execute("CREATE TABLE world_state(universe TEXT,scope TEXT,state_key TEXT,value_json TEXT,version INTEGER,updated_at REAL,PRIMARY KEY(universe,scope,state_key))")
            c.execute("INSERT INTO world_state VALUES('old','s','k','42',7,0)")
            c.commit()
        runtime = WorldRuntime(legacy)
        rows = runtime.read_state_history("old")["changes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "baseline")
        self.assertEqual(rows[0]["version"], 7)
        self.assertIsNone(rows[0]["before"])
        runtime = WorldRuntime(legacy)
        self.assertEqual(len(runtime.read_state_history("old")["changes"]), 1)


if __name__ == "__main__":
    unittest.main()
