from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from agent_world import (WorldRuntime, WorldDefinition, FunctionSpec, StateRule, FunctionOutcome,
                         EventSpec, ViewSpec, RetentionPolicy, PresentationCue)
from agent_world.world_sdk import install_world
from agent_world.errors import PermissionDenied, InvalidIdentityToken, CursorExpired, ResultRejected, WorldDefinitionError
from agent_world.world_views import ViewResetRequired

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ARGS = {"type": "object", "properties": {"phase": {"enum": ["start", "finish", "cancel"]},
        "private": {"type": "boolean"}}, "required": ["phase"], "additionalProperties": False}


def action(ctx, args):
    ctx.set_state("public", "actor", {"phase": args["phase"]})
    ctx.set_state("temporary", "progress", {"secret": "NOT_FOR_HISTORY", "phase": args["phase"]})
    cue = PresentationCue("walk-1", "actor", "action", args["phase"], "walk", {"destination": [2, 3]})
    return FunctionOutcome({"phase": args["phase"]}, (cue.event(ctx.actor_role_id),))


def look(ctx, args):
    return FunctionOutcome({"state": ctx.get_state("public", "actor")})


def projection(ctx, args):
    value = ctx.get_state("public", "actor", {"phase": "idle"})
    return {"entities": {} if value.get("hidden") else {"actor": value}, "meta": {}}


WORLD = WorldDefinition("reference-check", "Reference Check",
    (FunctionSpec("actor.move", action, ARGS), FunctionSpec("actor.look", look, EMPTY, access="read")),
    state_rules=(StateRule("public", "", {"type": "object"}),
                 StateRule("temporary", "", {"type": "object"}, history="metadata")),
    views=(ViewSpec("scene", projection, timeline=True),),
    retention=RetentionPolicy(event_seconds=60, event_rows=10, history_seconds=120, history_rows=20))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.w = WorldRuntime(Path(self.temp.name) / "test.sqlite3")
        install_world(self.w, "u", WORLD)
        self.a = self.w.create_role("A")["role_id"]
        self.b = self.w.create_role("B")["role_id"]
        self.control = self.w.issue_identity_token("u", self.a)
        self.observe = self.w.issue_identity_token("u", self.a, access_mode="observe")
        self.bt = self.w.issue_identity_token("u", self.b)

    def move(self, phase="start", operation="move-1", token=None):
        return self.w.call_function("u", "actor.move", self.a, {"phase": phase},
                                   operation_id=operation, identity_token=(token or self.control)["token"])

    def snap(self, identity=None):
        return self.w.view_snapshot("u", self.a, "scene", identity_token=(identity or self.control)["token"])

    def timeline(self, cursor, **kw):
        return self.w.view_timeline("u", self.a, cursor, identity_token=self.control["token"], **kw)

    def test_observer_can_read_but_not_control_core(self):
        self.move()
        result = self.w.call_function("u", "actor.look", self.a, {}, identity_token=self.observe["token"])
        self.assertEqual(result["result"]["state"]["phase"], "start")
        self.assertEqual(self.snap(self.observe)["snapshot"]["entities"]["actor"]["phase"], "start")
        with self.assertRaises(PermissionDenied): self.move("finish", "next", self.observe)
        with self.assertRaises(PermissionDenied): self.w.start_activity("u", "activity", self.a, "test", identity_token=self.observe["token"])
        with self.assertRaises(PermissionDenied): self.w.claim_activity("u", "activity", self.a, "worker", identity_token=self.observe["token"])

    def test_observer_cannot_replay_mutation_or_finish_claim(self):
        self.move()
        with self.assertRaises(PermissionDenied): self.move(token=self.observe)
        self.w.start_activity("u", "activity", self.a, "test")
        claim = self.w.claim_activity("u", "activity", self.a, "worker")
        with self.assertRaises(PermissionDenied):
            self.w.finish_activity("u", self.a, claim, operation_id="finish", identity_token=self.observe["token"])
        with self.assertRaises(PermissionDenied):
            self.w.renew_claim("u", self.a, claim, operation_id="renew", identity_token=self.observe["token"])

    def test_observer_bootstrap_does_not_claim_entry(self):
        boot = self.w.bootstrap("u", self.a, identity_token=self.observe["token"], record_presence=True, include_catalog=True)
        self.assertTrue(all(d["access"] == "read" for d in boot["functions"]))
        self.assertFalse(self.w.get_role_entry_status("u", self.a)["entered_world"])

    def test_rotation_preserves_observer_restriction(self):
        rotated = self.w.rotate_identity_token(self.observe["token_id"])
        self.assertEqual(rotated["access_mode"], "observe")
        with self.assertRaises(PermissionDenied): self.move(token=rotated)
        with self.assertRaises(InvalidIdentityToken): self.snap(self.observe)

    def test_revocation_stops_observer_reads(self):
        self.w.revoke_identity_token(self.observe["token_id"])
        with self.assertRaises(InvalidIdentityToken): self.snap(self.observe)

    def test_temporary_history_is_metadata_only_not_current_state(self):
        self.move()
        rows = self.w.read_state_history("u")["changes"]
        self.assertNotIn("NOT_FOR_HISTORY", json.dumps(rows))
        temporary = [r for r in rows if r["scope"] == "temporary"]
        self.assertEqual(temporary[0]["payload_retained"], 0)
        self.assertEqual(temporary[0]["version"], 1)
        self.assertEqual(self.w.get_state("u", "temporary", "progress")["value"]["secret"], "NOT_FOR_HISTORY")
        self.assertEqual([r for r in rows if r["scope"] == "public"][0]["value"]["phase"], "start")

    def test_retention_prunes_history_but_preserves_state_and_receipt(self):
        first = self.move()
        self.clock.return_value = 1201
        result = self.w.apply_retention("u")
        self.assertEqual(result["events_removed"], 1)
        self.assertEqual(result["history_removed"], 2)
        self.assertEqual(self.w.get_state("u", "public", "actor")["value"]["phase"], "start")
        replay = self.move()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["commit_seq"], replay["commit_seq"])
        with self.assertRaises(CursorExpired): self.w.read_changes("u", self.a, 0)
        with self.assertRaises(CursorExpired): self.w.read_state_history("u")

    def test_count_retention_is_bounded_and_universe_local(self):
        install_world(self.w, "other", WORLD)
        self.w.call_function("other", "actor.move", self.a, {"phase": "start"}, operation_id="other")
        for i in range(15): self.move(operation="move-"+str(i))
        first = self.w.apply_retention("u", limit=2)
        self.assertEqual(first["events_removed"], 2)
        self.assertEqual(first["history_removed"], 2)
        for _ in range(5): self.w.apply_retention("u")
        with self.w._conn(readonly=True) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE universe='u'").fetchone()[0], 10)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM state_changes WHERE universe='u'").fetchone()[0], 20)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE universe='other'").fetchone()[0], 1)

    def test_no_retention_policy_never_silently_deletes(self):
        install_world(self.w, "legacy", replace(WORLD, retention=None))
        self.w.call_function("legacy", "actor.move", self.a, {"phase": "start"}, operation_id="legacy")
        self.clock.return_value = 99999
        self.assertFalse(self.w.apply_retention("legacy")["configured"])
        self.assertEqual(len(self.w.read_changes("legacy", self.a)), 1)

    def test_zero_retention_and_invalid_policy(self):
        with self.assertRaises(WorldRuntimeError):
            replace(WORLD, retention=RetentionPolicy(-1, 10, 1, 10)).manifest()
        install_world(self.w, "u", replace(WORLD, version=2, retention=RetentionPolicy(0, 0, 0, 0)))
        self.move()
        self.assertEqual(self.w.apply_retention("u")["events_removed"], 1)

    def test_retention_clock_rollback_keeps_prefix_consistent(self):
        self.move()
        self.clock.return_value = 900
        self.move("finish", "next")
        self.clock.return_value = 1001
        self.w.apply_retention("u")
        self.assertEqual(len(self.w.read_changes("u", self.a)), 2)

    def test_timeline_captures_start_finish_between_two_snapshots(self):
        before = self.snap()
        self.move()
        self.clock.return_value = 1001
        self.move("finish", "next")
        update = self.timeline(before["timeline_cursor"])
        self.assertEqual([e["cue"]["phase"] for e in update["events"]], ["start", "finish"])
        self.assertNotIn("seq", update["events"][0])
        self.assertEqual(self.timeline(update["cursor"])["events"], [])
        same = self.timeline(before["timeline_cursor"])
        self.assertEqual([e["event_id"] for e in same["events"]], [e["event_id"] for e in update["events"]])

    def test_timeline_order_and_pagination_without_duplicate_replay(self):
        before = self.snap()
        self.move()
        self.move()
        self.move("cancel", "next")
        first = self.timeline(before["timeline_cursor"], limit=1)
        self.assertEqual(first["events"][0]["cue"]["phase"], "start")
        self.assertTrue(first["has_more"])
        second = self.timeline(first["cursor"], limit=1)
        self.assertEqual(second["events"][0]["cue"]["phase"], "cancel")
        self.assertFalse(second["has_more"])

    def test_timeline_filters_other_recipients_and_hidden_subjects(self):
        before = self.snap()
        self.w.call_function("u", "actor.move", self.b, {"phase": "start"}, operation_id="other", identity_token=self.bt["token"])
        self.assertEqual(self.timeline(before["timeline_cursor"])["events"], [])
        self.move()
        with self.w._conn() as c:
            c.execute("UPDATE world_state SET value_json=? WHERE universe='u' AND scope='public' AND state_key='actor'", (json.dumps({"hidden": True}),))
        self.assertEqual(self.timeline(before["timeline_cursor"])["events"], [])

    def test_timeline_is_credential_bound(self):
        before = self.snap()
        with self.assertRaises(ViewResetRequired):
            self.w.view_timeline("u", self.a, before["timeline_cursor"], identity_token=self.observe["token"])
        with self.assertRaises(ViewResetRequired):
            self.w.view_timeline("u", self.b, before["timeline_cursor"], identity_token=self.bt["token"])

    def test_timeline_gap_requires_snapshot_not_fabricated_animation(self):
        before = self.snap()
        self.move()
        self.clock.return_value = 1100
        self.w.apply_retention("u")
        with self.assertRaises(ViewResetRequired): self.timeline(before["timeline_cursor"])
        fresh = self.snap()
        self.assertEqual(fresh["snapshot"]["entities"]["actor"]["phase"], "start")
        self.assertEqual(self.timeline(fresh["timeline_cursor"])["events"], [])

    def test_undeclared_timeline_is_not_exposed(self):
        install_world(self.w, "u", replace(WORLD, version=2, views=(ViewSpec("scene", projection),)))
        snap = self.snap()
        self.assertNotIn("timeline_cursor", snap)
        with self.assertRaises(ViewResetRequired): self.timeline(snap["cursor"])

    def test_malformed_cue_rolls_back_state_and_history(self):
        def bad(ctx, args):
            ctx.set_state("public", "bad", {})
            return FunctionOutcome({}, (EventSpec(ctx.actor_role_id, "world.presentation", {"private_thinking": "no"}),))
        install_world(self.w, "bad", replace(WORLD, functions=(FunctionSpec("bad", bad, EMPTY),)))
        with self.assertRaises(ResultRejected): self.w.call_function("bad", "bad", self.a, {}, operation_id="bad")
        self.assertEqual(self.w.read_state_history("bad")["changes"], [])

    def test_metadata_policy_is_used_during_world_migration(self):
        def initialize(ctx):
            ctx.set_state("temporary", "progress", {"secret": "INITIAL_SECRET"})
        install_world(self.w, "new", replace(WORLD, initialize=initialize))
        self.assertNotIn("INITIAL_SECRET", json.dumps(self.w.read_state_history("new")))
        def migrate(ctx):
            ctx.set_state("temporary", "progress", {"secret": "MIGRATION_SECRET"})
        install_world(self.w, "new", replace(WORLD, version=2, state_version=2, migrations={2: migrate}))
        self.assertNotIn("MIGRATION_SECRET", json.dumps(self.w.read_state_history("new")))

    def test_timeline_revalidates_world_policy_and_version(self):
        before = self.snap()
        self.move()
        install_world(self.w, "u", replace(WORLD, version=2))
        with self.assertRaises(ViewResetRequired): self.timeline(before["timeline_cursor"])

    def test_timeline_can_be_consumed_after_runtime_restart(self):
        before = self.snap()
        self.move()
        other = WorldRuntime(self.w.db_path)
        install_world(other, "u", WORLD)
        page = other.view_timeline("u", self.a, before["timeline_cursor"], identity_token=self.control["token"])
        self.assertEqual(page["events"][0]["cue"]["phase"], "start")

    def test_public_intention_is_not_an_internal_thought_channel(self):
        cue = PresentationCue("plan", "actor", "intent", "start", "public-plan", {"text": "I will visit the garden."})
        self.assertEqual(cue.event(self.a).payload["channel"], "intent")
        with self.assertRaises(WorldRuntimeError): PresentationCue("x", "actor", "private_thought", "start", "x").event(self.a)


from agent_world.errors import WorldRuntimeError
if __name__ == "__main__": unittest.main()
