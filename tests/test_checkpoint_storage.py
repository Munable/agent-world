"""Bounded system observations preserve original state, exact receipts and sync."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_world import (CheckpointSpec, FunctionOutcome, FunctionSpec, StateRule,
                         StreamEvent, StreamSpec, ViewSpec, WorldDefinition, WorldRuntime)
from agent_world.errors import CursorExpired

EMPTY = {"type": "object", "additionalProperties": False, "properties": {}}


def advance(ctx, args):
    for index in range(args.get("width", 1)):
        key = f"cell-{index}" if index else "value"
        value = ctx.get_state("counter", key, {"n": 0})
        value["n"] += 1
        ctx.set_state("counter", key, value)
    ctx.schedule_checkpoint(ctx.checkpoint.checkpoint_key, "advance", args, due_at=0,
                            expected_generation=ctx.checkpoint.generation)
    publications = ((StreamEvent("shared", "advanced", {"n": value["n"]}, key="advance"),)
                    if args.get("emit") else ())
    return FunctionOutcome(value, events=publications)


def keep_action(ctx, args):
    ctx.set_state("original", "kept", {"value": 7})
    return FunctionOutcome({"accepted": 7})


def project(ctx, args):
    return {"entities": {"counter": ctx.get_state("counter", "value", {"n": 0})}, "meta": {}}


def world(*, emit=False, width=1):
    def initialize(ctx):
        ctx.schedule_checkpoint("running", "advance", {"emit": emit, "width": width}, due_at=0)
    return WorldDefinition("checkpoint-storage", "Checkpoint storage",
        (FunctionSpec("keep", keep_action, EMPTY),), initialize=initialize,
        state_rules=(StateRule("counter", "", {"type": "object"}),
                     StateRule("original", "", {"type": "object"})),
        checkpoints=(CheckpointSpec("advance", advance,
            {"type": "object", "properties": {"emit": {"type": "boolean"},
                "width": {"type": "integer", "minimum": 1, "maximum": 400}}, "required": ["emit"],
             "additionalProperties": False}),),
        streams=(StreamSpec("shared", public=True, max_events=8, retention_seconds=60),),
        views=(ViewSpec("counter", project),))


class CheckpointStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = WorldRuntime(Path(self.temp.name) / "world.sqlite3")
        self.runtime.install_world("u", world())
        self.actor = self.runtime.create_role("reader")["role_id"]
        self.token = self.runtime.issue_identity_token("u", self.actor)["token"]

    def rows(self, sql, params=()):
        with self.runtime._conn(readonly=True) as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def tick(self, count=1):
        for _ in range(count):
            result = self.runtime.run_due_timers("u", limit=1)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["processed"][0]["status"], "completed")

    def sweep(self, **options):
        return self.runtime.apply_checkpoint_retention("u", keep_commits=0, seconds=0, **options)

    def test_system_growth_is_bounded_without_pruning_original_history_or_receipts(self):
        receipt = self.runtime.call_function("u", "keep", self.actor, {}, operation_id="original",
                                            identity_token=self.token)
        original = self.rows("SELECT * FROM state_changes ORDER BY seq")
        original_commits = self.rows("SELECT * FROM world_commits ORDER BY seq")
        snapshot = self.runtime.view_snapshot("u", self.actor, "counter", identity_token=self.token)
        self.tick(12)
        latest = self.runtime.get_checkpoint_receipt("u", "running")
        for _ in range(20):
            self.sweep(limit=2)
        self.assertEqual(self.rows("SELECT * FROM state_changes WHERE commit_seq IN "
            "(SELECT seq FROM world_commits WHERE source!='checkpoint') ORDER BY seq"), original)
        self.assertEqual(self.rows("SELECT * FROM world_commits WHERE source!='checkpoint' ORDER BY seq"), original_commits)
        self.assertEqual(len(self.rows("SELECT * FROM world_commits WHERE source='checkpoint'")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM world_checkpoints")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM world_timers")), 0)
        self.assertEqual(len(self.rows("SELECT * FROM timer_transitions")), 0)
        self.assertEqual(len(self.rows("SELECT * FROM operations")), 1)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "running"), latest)
        self.assertEqual(self.runtime.get_receipt("u", self.actor, "original", identity_token=self.token), receipt)
        replay = self.runtime.call_function("u", "keep", self.actor, {}, operation_id="original",
                                           identity_token=self.token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["commit_seq"], receipt["commit_seq"])
        with self.assertRaises(CursorExpired):
            self.runtime.read_state_history("u")
        retained = self.runtime.read_state_history("u", retained_only=True)
        self.assertFalse(retained["complete"])
        self.assertGreater(retained["checkpoint_completeness_floor"], 0)
        self.assertEqual(retained["history_floor"], 0)
        self.assertTrue(all(row["seq"] in {item["seq"] for item in retained["changes"]} for row in original))
        with self.runtime._conn(readonly=True) as connection:
            revision, _ = self.runtime._state_revision_tx(connection, "u")
        self.assertGreaterEqual(revision, retained["checkpoint_completeness_floor"])
        delta = self.runtime.view_sync("u", self.actor, snapshot["cursor"], identity_token=self.token)
        self.assertEqual(delta["delta"]["entities"]["upsert"]["counter"], {"n": 12})

    def test_retained_stream_references_protect_system_commits_until_stream_expiry(self):
        self.runtime.install_world("stream", world(emit=True))
        for _ in range(4):
            self.assertEqual(self.runtime.run_due_timers("stream", limit=1)["processed"][0]["status"], "completed")
        result = self.runtime.apply_checkpoint_retention("stream", keep_commits=0, seconds=0)
        self.assertEqual(result["checkpoint_commits_removed"], 0)
        self.assertEqual(len(self.rows("SELECT * FROM world_commits WHERE universe='stream' AND source='checkpoint' "
                                      "AND checkpoint_reclaimable=1")), 0)
        future = time.time()+120
        with patch("agent_world.runtime_streams.time.time", return_value=future):
            self.runtime.apply_stream_retention("stream")
        # Even rows skipped during the earlier pass re-enter the seek index in
        # the stream reference's own transaction, without a full history scan.
        self.assertGreater(len(self.rows("SELECT * FROM world_commits WHERE universe='stream' AND source='checkpoint' "
                                        "AND checkpoint_reclaimable=1")), 0)
        for _ in range(3):
            self.runtime.apply_checkpoint_retention("stream", keep_commits=0, seconds=0)
        self.assertEqual(len(self.rows("SELECT * FROM world_commits WHERE universe='stream' AND source='checkpoint'")), 1)
        self.assertEqual(self.runtime.get_checkpoint_receipt("stream", "running")["result"], {"n": 4})

    def test_actual_commit_and_change_budgets_outpace_sustained_high_change_generations(self):
        self.runtime.install_world("wide", world(width=300))
        removed = incoming = 0
        for _ in range(8):
            result = self.runtime.run_due_timers("wide", limit=25)
            work = result["checkpoint_work"]
            self.assertGreaterEqual(work["commits"], 1)
            self.assertEqual(work["changes"], work["commits"] * 300)
            incoming += work["commits"]
            collected = self.runtime.apply_checkpoint_retention("wide", keep_commits=2,
                seconds=86400, limit=2*work["commits"]+8, change_limit=2*work["changes"]+4096)
            removed += collected["checkpoint_commits_removed"]
            self.assertLessEqual(len(self.rows("SELECT seq FROM world_commits WHERE universe='wide' "
                                              "AND source='checkpoint'")), 2)
            self.assertLessEqual(len(self.rows("SELECT seq FROM state_changes WHERE universe='wide' "
                "AND commit_seq IN (SELECT seq FROM world_commits WHERE source='checkpoint')")), 600)
        self.assertEqual(incoming-removed, 2)
        latest = self.runtime.get_checkpoint_receipt("wide", "running")
        self.assertEqual(latest["result"], {"n": incoming})
        # The real query seeks the partial candidate index; all latest receipt
        # references are excluded before applying LIMIT, rather than filtered
        # after scanning the protected lifetime directory.
        with self.runtime._conn(readonly=True) as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT seq FROM world_commits WHERE universe='wide' "
                "AND source='checkpoint' AND checkpoint_reclaimable=1 AND seq>0 ORDER BY seq LIMIT 8").fetchall()
        self.assertTrue(any("idx_checkpoint_reclaimable" in row["detail"] for row in plan))

    def test_protected_prefix_does_not_starve_later_collectable_commits(self):
        # The first completed task's exact latest receipt remains referenced.
        # A second task can keep progressing and have its older history retired.
        self.tick()
        first_receipt = self.runtime.get_checkpoint_receipt("u", "running")
        with self.runtime._conn(serialized=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = self.runtime._journal_marker_tx(connection)
            ctx = self.runtime._context_tx(connection, "u", "system:installer", "fixture.stop", 1,
                                           access="write", operation_id="first-stop")
            row = ctx.get_checkpoint("running")
            ctx.cancel_checkpoint("running", expected_generation=row["generation"])
            self.runtime._commit_outcome_tx(connection, ctx, FunctionOutcome({}), {}, journal_marker=marker)
        self.tick_other_task()
        for _ in range(6):
            self.tick()
        before = len(self.rows("SELECT * FROM world_commits WHERE source='checkpoint'"))
        removed = 0
        for _ in range(20):
            removed += self.sweep(limit=1)["checkpoint_commits_removed"]
        self.assertGreater(removed, 0)
        self.assertLess(len(self.rows("SELECT * FROM world_commits WHERE source='checkpoint'")), before)
        self.assertEqual(self.runtime.get_checkpoint_receipt("u", "running"), first_receipt)

    def tick_other_task(self):
        # A trusted fixture schedules a real current task through the same
        # context/managed commit path, without resetting the world.
        with self.runtime._conn(serialized=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = self.runtime._journal_marker_tx(connection)
            ctx = self.runtime._context_tx(connection, "u", "system:installer", "fixture.start", 1,
                                           access="write", operation_id="second-start")
            ctx.schedule_checkpoint("second", "advance", {"emit": False}, due_at=0)
            self.runtime._commit_outcome_tx(connection, ctx, FunctionOutcome({}), {},
                                            journal_marker=marker)

