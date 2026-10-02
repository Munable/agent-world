"""River Camp capability combinations; no model calls or cross-day claims.

Each scenario states a falsifiable hypothesis about existing Runtime mechanisms.
Faults are injected by a test-only WorldDefinition wrapper, never public actions.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import httpx

from agent_world import FunctionOutcome, WorldRuntime
from agent_world.errors import (
    PermissionDenied,
    ReceiptNotFound,
    ResultRejected,
    RuleViolation,
    SchemaRejected,
    StateConflict,
    WorldVersionMismatch,
)
from agent_world.world_sdk import install_world
from examples.river_camp import make_world
from tests.capability_harness import RuntimeCapabilityHarness
from tests.live_server import LiveServer


def managed_records(runtime, universe):
    """Trusted readback of candidate effects, including journal and timer edges."""
    tables = (
        "world_state", "state_changes", "world_commits", "operations",
        "events", "world_timers", "timer_transitions", "stream_events",
    )
    with runtime._conn(readonly=True) as connection:
        connection.execute("BEGIN")
        return {
            table: [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE universe=? ORDER BY rowid", (universe,),
            )]
            for table in tables
        }


def wrapped_world(world, action, handler):
    functions = tuple(
        replace(
            spec,
            handler=handler,
        ) if spec.name == action else spec
        for spec in world.functions
    )
    return replace(world, functions=functions)


class RiverCampHarness(RuntimeCapabilityHarness):
    """Reuse formal entry before the trusted world installer configures its ACL."""

    def __init__(self, db_path, universe="camp"):
        self.db_path = Path(db_path)
        self.universe = universe
        self.runtime = WorldRuntime(self.db_path)
        self.participants = self.spawn(4)
        self.world = make_world(
            founder_role_id=self.participants[0].role_id,
            judge_role_ids=(self.participants[2].role_id,),
        )
        install_world(self.runtime, self.universe, self.world)


class RiverCampLiveServer(LiveServer):
    def __init__(self):
        super().__init__("examples.river_camp:WORLD")

    def start(self):
        if not hasattr(self, "camp"):
            self.camp = RiverCampHarness(self.db, universe="network")
        super().start()


class RiverCampCombinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "river-camp.sqlite3"
        self.h = RiverCampHarness(self.path)
        self.p = self.h.participants
        for index, participant in enumerate(self.p):
            self.h.call(participant, "camp.join", {}, f"join-{index}")

    def inspect(self, participant=None):
        return self.h.query(participant or self.p[0], "camp.inspect", {})["result"]

    def state(self, scope, key):
        return self.h.runtime.get_state(self.h.universe, scope, key)

    def intent(self, intent_id):
        return self.state("camp:intents", intent_id)

    def project(self, project_id):
        return self.state("camp:projects", project_id)

    def propose(self, participant, intent_id, **extra):
        return self.h.call(participant, "camp.propose", {
            "intent_id": intent_id, "text": "Build a safe crossing with the shared supplies.",
            **extra,
        }, "propose-" + intent_id)

    def judgement_args(self, intent_id):
        inventory = self.state("camp:world", "inventory")
        route = self.state("camp:world", "route")
        authority = self.state("camp:world", "authority")
        return {
            "intent_id": intent_id,
            "expected_intent_version": self.intent(intent_id)["version"],
            "inventory_version": inventory["version"],
            "route_version": route["version"],
            "authority_epoch": authority["value"]["epoch"],
            "reason": "A foot crossing uses two wood, four rope, and one stone.",
            "decision": "approve",
            "cost": {"wood": 2, "rope": 4, "stone": 1},
            "effects": {"passage": "foot", "stability": 2, "exposure": 0},
        }

    def approve(self, intent_id, judge=None, operation=None):
        return self.h.call(judge or self.p[2], "camp.judge", self.judgement_args(intent_id),
                           operation or "judge-" + intent_id)

    def accept(self, participant, intent_id, operation=None):
        return self.h.call(participant, "camp.accept", {
            "intent_id": intent_id,
            "expected_intent_version": self.intent(intent_id)["version"],
        }, operation or "accept-" + intent_id)

    def start_project(self, participant, project_id, seconds=0.2):
        return self.h.call(participant, "camp.start_project", {
            "project_id": project_id, "title": "Survey the river bank", "seconds": seconds,
        }, "start-" + project_id)

    def wait_until(self, deadline):
        remaining = deadline - time.time()
        if remaining > 0:
            time.sleep(remaining + 0.015)

    def assert_no_receipt(self, participant, operation):
        with self.assertRaises(ReceiptNotFound):
            self.h.runtime.get_receipt(
                self.h.universe, participant.role_id, operation,
                identity_token=participant.token,
            )

    def timer_for_project(self, project_id):
        with self.h.runtime._conn(readonly=True) as connection:
            rows = connection.execute(
                "SELECT timer_id,arguments_json FROM world_timers WHERE universe=?",
                (self.h.universe,),
            ).fetchall()
        matches = [row["timer_id"] for row in rows
                   if json.loads(row["arguments_json"]).get("project_id") == project_id]
        self.assertEqual(len(matches), 1)
        return self.h.runtime.get_timer(self.h.universe, matches[0])

    def assert_transition_commit(self, timer_id, status, commit_seq):
        with self.h.runtime._conn(readonly=True) as connection:
            rows = connection.execute(
                "SELECT commit_seq FROM timer_transitions "
                "WHERE universe=? AND timer_id=? AND status=?",
                (self.h.universe, timer_id, status),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [commit_seq])

    def test_concurrent_accepts_preserve_shared_stock_and_one_winner(self):
        """H: two separately judged intents cannot spend the same stock snapshot."""
        self.propose(self.p[0], "north")
        self.propose(self.p[1], "south")
        self.approve("north")
        self.approve("south")
        before = self.state("camp:world", "inventory")
        versions = {key: self.intent(key)["version"] for key in ("north", "south")}
        barrier = threading.Barrier(2)

        def accept(pair):
            participant, key = pair
            runtime = WorldRuntime(self.path)
            install_world(runtime, self.h.universe, self.h.world)
            barrier.wait(timeout=5)
            try:
                receipt = runtime.call_function(
                    self.h.universe, "camp.accept", participant.role_id,
                    {"intent_id": key, "expected_intent_version": versions[key]},
                    operation_id="race-" + key, identity_token=participant.token,
                )
                return "accepted", key, receipt
            except StateConflict:
                return "conflict", key, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, ((self.p[0], "north"), (self.p[1], "south"))))
        self.assertEqual(sorted(item[0] for item in results), ["accepted", "conflict"])
        winner = next(item for item in results if item[0] == "accepted")
        loser = next(item for item in results if item[0] == "conflict")
        after = self.state("camp:world", "inventory")
        for resource, cost in {"wood": 2, "rope": 4, "stone": 1}.items():
            self.assertEqual(before["value"][resource], after["value"][resource] + cost)
            self.assertGreaterEqual(after["value"][resource], 0)
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(self.intent(winner[1])["value"]["status"], "executed")
        self.assertEqual(self.intent(loser[1])["value"]["status"], "approved")
        self.assertEqual(len(winner[2]["event_seqs"]), len(self.p))
        for participant in self.p:
            executed = [event for event in self.h.changes(participant)["events"]
                        if event["kind"] == "camp.executed"]
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0]["payload"]["intent_id"], winner[1])
        self.assert_no_receipt(self.p[0] if loser[1] == "north" else self.p[1], "race-" + loser[1])
        with self.h.runtime._conn(readonly=True) as connection:
            changes = connection.execute(
                "SELECT scope,state_key FROM state_changes WHERE universe=? AND commit_seq=?",
                (self.h.universe, winner[2]["commit_seq"]),
            ).fetchall()
        self.assertTrue({("camp:world", "inventory"), ("camp:world", "route"),
                         ("camp:intents", winner[1])}.issubset({tuple(row) for row in changes}))

    def test_rule_output_and_state_schema_failures_leave_no_candidate_effects(self):
        """H: rejected compound writes leave no state, event, timer or receipt."""
        for action in ("camp.accept", "camp.start_project"):
            for fault in ("rule", "output", "state-schema"):
                with self.subTest(action=action, fault=fault):
                    if action == "camp.accept":
                        key = "fault-" + fault
                        self.propose(self.p[0], key)
                        self.approve(key)
                        arguments = {"intent_id": key,
                                     "expected_intent_version": self.intent(key)["version"]}
                    else:
                        key = "fault-project-" + fault
                        arguments = {"project_id": key, "title": "Rejected candidate", "seconds": 10}
                    original = next(spec.handler for spec in self.h.world.functions if spec.name == action)

                    def broken(ctx, args, original=original, fault=fault, action=action):
                        outcome = original(ctx, args)
                        if fault == "rule":
                            raise RuleViolation("test fault after the natural world mutations")
                        if fault == "output":
                            return FunctionOutcome({"invalid": float("nan")}, outcome.events)
                        if action == "camp.start_project":
                            ctx.set_state("camp:projects", args["project_id"], {"status": []})
                        else:
                            invalid = dict(ctx.get_state("camp:world", "inventory"))
                            invalid["wood"] = "invalid"
                            ctx.set_state("camp:world", "inventory", invalid)
                        return outcome

                    install_world(self.h.runtime, self.h.universe, wrapped_world(self.h.world, action, broken))
                    before = managed_records(self.h.runtime, self.h.universe)
                    error = {"rule": RuleViolation, "output": ResultRejected,
                             "state-schema": SchemaRejected}[fault]
                    operation = "reject-" + key
                    with self.assertRaises(error):
                        self.h.call(self.p[0], action, arguments, operation)
                    self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
                    self.assert_no_receipt(self.p[0], operation)
                    install_world(self.h.runtime, self.h.universe, self.h.world)

    def test_stale_resource_snapshot_cannot_create_a_new_ruling(self):
        """H: a judge must decide on current shared facts, not an obsolete read."""
        self.propose(self.p[0], "first")
        self.propose(self.p[1], "stale")
        stale_arguments = self.judgement_args("stale")
        self.approve("first")
        self.accept(self.p[0], "first")
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaises(StateConflict):
            self.h.call(self.p[2], "camp.judge", stale_arguments, "stale-ruling")
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        self.assert_no_receipt(self.p[2], "stale-ruling")

    def test_completed_project_cancels_timer_without_late_duplicate_effect(self):
        """H: completion and its timer cancellation are one durable submission."""
        created = self.start_project(self.p[0], "cancel", seconds=2)
        completed = self.h.call(self.p[0], "camp.complete_project", {
            "project_id": "cancel", "expected_project_version": created["result"]["project_version"],
        }, "complete-cancel")
        timer = self.timer_for_project("cancel")
        self.assert_transition_commit(timer["timer_id"], "scheduled", created["commit_seq"])
        self.assertEqual(self.project("cancel")["value"]["status"], "completed")
        self.assertEqual(self.timer_for_project("cancel")["status"], "cancelled")
        self.assert_transition_commit(timer["timer_id"], "cancelled", completed["commit_seq"])
        before = managed_records(self.h.runtime, self.h.universe)
        self.wait_until(timer["due_at"])
        self.h.restart()
        self.assertEqual(self.h.runtime.run_due_timers(self.h.universe)["count"], 0)
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)

    def test_completion_racing_due_timer_cannot_submit_with_late_authority(self):
        """H: the deadline remains binding when completion and expiry race."""
        self.start_project(self.p[0], "race", seconds=0.2)
        timer = self.timer_for_project("race")
        version = self.project("race")["version"]
        runtimes = [WorldRuntime(self.path), WorldRuntime(self.path)]
        for runtime in runtimes:
            install_world(runtime, self.h.universe, self.h.world)
        self.wait_until(timer["due_at"])
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait(timeout=5)
            try:
                runtimes[0].call_function(
                    self.h.universe, "camp.complete_project", self.p[0].role_id,
                    {"project_id": "race", "expected_project_version": version},
                    operation_id="late-completion", identity_token=self.p[0].token,
                )
            except (RuleViolation, StateConflict) as error:
                return type(error).__name__
            return "accepted"

        def expire():
            barrier.wait(timeout=5)
            return runtimes[1].run_due_timers(self.h.universe)

        with ThreadPoolExecutor(max_workers=2) as pool:
            completion = pool.submit(complete)
            expiry = pool.submit(expire)
            denied = completion.result(timeout=5)
            sweep = expiry.result(timeout=5)
        self.assertIn(denied, {"RuleViolation", "StateConflict"})
        self.assertEqual(sweep["count"], 1)
        self.assertEqual(self.project("race")["value"]["status"], "expired")
        self.assertEqual(self.timer_for_project("race")["status"], "completed")
        self.assert_no_receipt(self.p[0], "late-completion")
        fired = self.h.runtime.get_timer_receipt(self.h.universe, timer["timer_id"])
        self.assert_transition_commit(timer["timer_id"], "completed", fired["commit_seq"])
        before = managed_records(self.h.runtime, self.h.universe)
        self.assertEqual(self.h.runtime.run_due_timers(self.h.universe)["count"], 0)
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)

    def test_judge_revocation_removes_private_material_and_fences_old_ruling(self):
        """H: public stages cannot grant a bystander private evidence or a decision."""
        secret = "PRIVATE_EVIDENCE_4ea794_unsafe_bank_measurement"
        proposal = self.propose(self.p[0], "private", private_note=secret)
        self.assertNotIn(secret, json.dumps(proposal))
        self.assertIn(secret, json.dumps(self.inspect(self.p[0])))
        self.assertIn(secret, json.dumps(self.inspect(self.p[2])))
        public = self.inspect(self.p[3])
        self.assertIn("private", json.dumps(public))
        self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(public["intents"][0]["value"]["status"], "pending")
        for participant in self.p:
            self.assertNotIn(secret, json.dumps(self.h.changes(participant)))
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaises(PermissionDenied):
            self.approve("private", judge=self.p[3], operation="forged-ruling")
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        ruling_arguments = self.judgement_args("private")
        ruling = self.h.call(self.p[2], "camp.judge", ruling_arguments, "judge-private")
        self.assertNotIn(secret, json.dumps(ruling))
        public = self.inspect(self.p[3])
        self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(public["intents"][0]["value"]["status"], "approved")
        for participant in self.p:
            self.assertNotIn(secret, json.dumps(self.h.changes(participant)))
        authority = self.state("camp:world", "authority")["value"]
        self.h.call(self.p[0], "camp.set_judge", {
            "role_id": self.p[2].role_id, "enabled": False, "authority_epoch": authority["epoch"],
        }, "revoke-judge")
        self.assertNotIn(secret, json.dumps(self.inspect(self.p[2])))
        before = managed_records(self.h.runtime, self.h.universe)
        with self.assertRaises(StateConflict):
            self.accept(self.p[0], "private", operation="late-accept")
        self.assertEqual(managed_records(self.h.runtime, self.h.universe), before)
        self.assert_no_receipt(self.p[0], "late-accept")
        # A replay recovers the historical ruling, not fresh judge authority.
        replay = self.h.call(self.p[2], "camp.judge", ruling_arguments, "judge-private")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["commit_seq"], ruling["commit_seq"])

    def test_schema_preserving_upgrade_replay_restart_and_same_key_continue_pending_intent(self):
        """H: a pending world fact survives a code upgrade and a new key session."""
        proposal = self.propose(self.p[1], "continuing")
        arguments = {"intent_id": "continuing", "text": "Build a safe crossing with the shared supplies."}
        old = WorldRuntime(self.path)
        install_world(old, self.h.universe, self.h.world)
        self.h.world = replace(self.h.world, version=self.h.world.version + 1)
        install_world(self.h.runtime, self.h.universe, self.h.world)
        with self.assertRaises(WorldVersionMismatch):
            old.call_function(
                self.h.universe, "camp.explore", self.p[1].role_id, {"site": "bank"},
                operation_id="old-process-new-intent", identity_token=self.p[1].token,
            )
        self.assert_no_receipt(self.p[1], "old-process-new-intent")
        replay = old.call_function(
            self.h.universe, "camp.propose", self.p[1].role_id, arguments,
            operation_id="propose-continuing", identity_token=self.p[1].token,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["commit_seq"], proposal["commit_seq"])
        self.h.restart()
        returned = self.h.reenter(self.p[1])
        self.assertEqual(returned.role_id, self.p[1].role_id)
        self.assertFalse(returned.created_profile)
        self.assertNotEqual(returned.token_id, self.p[1].token_id)
        self.assertIn("continuing", json.dumps(self.inspect(returned)))
        self.approve("continuing")
        accepted = self.accept(returned, "continuing")
        self.h.restart()
        recovered = self.h.runtime.get_receipt(
            self.h.universe, returned.role_id, "accept-continuing", identity_token=returned.token,
        )
        self.assertEqual(recovered["commit_seq"], accepted["commit_seq"])


class RiverCampTransportCombinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_commit_mcp_replay_and_short_deadline_after_process_death(self):
        """H: a real loopback crash cannot lose a deadline or duplicate its effect."""
        with RiverCampLiveServer() as server:
            participant = server.camp.participants[0]
            owner, identity, private_key = server.key_identity(participant.private_key)
            self.assertEqual(owner["role_id"], participant.role_id)
            auth = {"Authorization": "Bearer " + identity["token"]}
            operation = {"operation_id": "network-start", "arguments": {
                "project_id": "network-project", "title": "A bounded offline survey", "seconds": 2,
            }}
            with httpx.Client(trust_env=False, timeout=5) as client:
                joined = client.post(
                    server.url + "/v1/functions/camp.join/invoke", headers=auth,
                    json={"operation_id": "network-join", "arguments": {}},
                )
                self.assertEqual(joined.status_code, 200, joined.text)
                created = client.post(
                    server.url + "/v1/functions/camp.start_project/invoke",
                    headers=auth, json=operation,
                )
                self.assertEqual(created.status_code, 200, created.text)
                receipt = created.json()
            # Kill only this fixture's process while its committed timer is pending.
            server.stop(abrupt=True)
            runtime = server.camp.runtime
            with runtime._conn(readonly=True) as connection:
                timers = connection.execute(
                    "SELECT timer_id,due_at,status FROM world_timers WHERE universe='network'",
                ).fetchall()
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0]["status"], "pending")
            await asyncio.sleep(max(0, timers[0]["due_at"] - time.time()) + 0.03)
            self.assertEqual(runtime.get_timer("network", timers[0]["timer_id"])["status"], "pending")
            server.start()
            returned, replacement, _ = server.key_identity(private_key)
            self.assertEqual(returned["role_id"], owner["role_id"])
            self.assertNotEqual(replacement["token_id"], identity["token_id"])
            async with server.session(replacement) as (session, _):
                replay = await session.call_tool("camp.start_project", arguments=operation)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertTrue(replay.structured_content["replayed"])
                self.assertEqual(replay.structured_content["commit_seq"], receipt["commit_seq"])
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if runtime.get_timer("network", timers[0]["timer_id"])["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(runtime.get_timer("network", timers[0]["timer_id"])["status"], "completed")
                readback = await session.call_tool("camp.inspect", arguments={"arguments": {}})
                self.assertFalse(readback.is_error, readback.structured_content)
                projects = [item for item in readback.structured_content["result"]["projects"]
                            if item["key"] == "network-project"]
                self.assertEqual(len(projects), 1)
                self.assertEqual(projects[0]["value"]["status"], "expired")
            self.assertEqual(runtime.get_state("network", "camp:projects", "network-project")["value"]["status"], "expired")
            with runtime._conn(readonly=True) as connection:
                terminals = connection.execute(
                    "SELECT COUNT(*) FROM timer_transitions "
                    "WHERE universe='network' AND timer_id=? AND status='completed'",
                    (timers[0]["timer_id"],),
                ).fetchone()[0]
                starts = connection.execute(
                    "SELECT COUNT(*) FROM operations WHERE universe='network' "
                    "AND actor_role_id=? AND idempotency_key='network-start'",
                    (owner["role_id"],),
                ).fetchone()[0]
            self.assertEqual((terminals, starts), (1, 1))


if __name__ == "__main__":
    unittest.main()
