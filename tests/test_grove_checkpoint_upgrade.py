"""Real checkpoint revision fences and bounded restart recovery.

Product fixtures use isolated storage and formal publication/formation. They
do not establish model capability, actual-user acceptance or live data repair.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_world import WorldRuntime
from agent_world.errors import RuleViolation
from agent_world.world_timers import RetryTimer
from agent_world.worlds.shared_grove import domains, make_world, model as m, scheduling
from agent_world.worlds.shared_grove import processes
import test_grove_domain_contracts as contracts
from test_grove_incremental_execution import StoredContext


class CheckpointUpgradeProductContracts(unittest.TestCase):
    def setUp(self):
        self.contract = contracts.DomainProductContracts(methodName="runTest")
        self.previous = replace(make_world(), version=make_world().version - 1)
        with patch("test_grove_creative.make_world", return_value=self.previous):
            self.contract.setUp()
        self.addCleanup(self.contract.doCleanups)
        self.h = self.contract.h
        self.addCleanup(lambda: self.h.world.close())

    def restart(self, definition=None):
        self.h.world.close()
        self.h.world = WorldRuntime(self.h.db)
        self.h.world.install_world("w", definition or make_world())

    @staticmethod
    def resume_frame_only(ctx):
        # Reproduce the verified old first-attach path: it handed off the
        # coordinator but retained old-version pending child tasks.
        scheduling._schedule_frame(ctx, ctx.now)
        ctx.world_resumed = True

    def test_world_revision_only_upgrade_reissues_unfinished_target_once(self):
        counters = self.contract.two_counters()
        work = self.contract.begin_frame()
        first, second = self.contract.wait_one_counter(counters)
        saved = deepcopy(self.h.entity(first))
        operation = f"creative-{self.h.serial}"
        receipt = self.h.world.get_receipt("w", self.h.roles["A"], operation)
        unfinished = self.contract.frame()["pending"]
        generations = {entry["domain_id"]: entry["generation"] for entry in unfinished}
        self.h.time.return_value += 3600

        self.restart()

        resumed = self.contract.frame()
        for key in ("id", "target_tick", "opened_at", "event_cutoff"):
            self.assertEqual(resumed[key], work[key])
        self.assertEqual(self.h.entity(first), saved)
        for entry in resumed["pending"]:
            domain = self.h.world.get_state("w", domains.DOMAINS, entry["domain_id"])["value"]
            if domain["last_tick"] < work["target_tick"]:
                self.assertGreater(entry["generation"], generations[entry["domain_id"]])
        self.contract.finish_frame(work["id"])
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [1, 1])
        self.assertEqual(self.h.world.get_receipt("w", self.h.roles["A"], operation), receipt)
        self.h.ticks(1)
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [2, 2])

    def test_blocked_child_is_requeued_in_original_open_frame_without_fault(self):
        counters = self.contract.two_counters()
        work = self.contract.begin_frame()
        first, second = self.contract.wait_one_counter(counters)
        saved = deepcopy(self.h.entity(first))
        pending = next(entry for entry in self.contract.frame()["pending"]
                       if self.h.world.get_state("w", domains.DOMAINS, entry["domain_id"])["value"]["last_tick"] < work["target_tick"])
        key = scheduling.DOMAIN_PREFIX + pending["domain_id"]
        self.restart(replace(make_world(), resume=self.resume_frame_only))
        for _ in range(20):
            self.h.world.run_due_timers("w", limit=1)
            if self.h.world.get_checkpoint("w", key)["status"] == "blocked":
                break
        else:
            self.fail("The runtime did not fence the stale checkpoint.")
        blocked = self.h.world.get_checkpoint_receipt("w", key)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["result"]["error_code"], "CheckpointVersionMismatch")

        closed = self.contract.finish_frame(work["id"])

        self.assertEqual((closed["target_tick"], closed["event_cutoff"]),
                         (work["target_tick"], work["event_cutoff"]))
        self.assertEqual(closed["faults"], 0)
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [1, 1])
        self.assertEqual(self.h.entity(first), saved)
        self.assertGreater(self.h.world.get_checkpoint("w", key)["generation"], pending["generation"])

    def test_closed_uncomputed_version_fault_recovers_next_frame_without_backfill(self):
        counters = self.contract.two_counters()
        work = self.contract.begin_frame()
        first, second = self.contract.wait_one_counter(counters)
        self.restart(replace(make_world(), resume=self.resume_frame_only))
        # The old coordinator converted the exact runtime fence into a domain
        # fault. Recreate that old policy, without altering saved world rows.
        with patch.object(scheduling, "_version_blocked", return_value=False):
            self.contract.finish_frame(work["id"])
        owner = self.h.world.get_state("w", domains.OWNERS, second)["value"]["domain_id"]
        before = self.h.world.get_state("w", domains.DOMAINS, owner)["value"]
        self.assertEqual(before["failure"], {
            "tick": work["target_tick"], "code": "CheckpointVersionMismatch", "computed": False})
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [1, 0])
        clock = self.h.world.get_state("w", m.META, "clock")["value"]
        saved_entities = [deepcopy(self.h.entity(eid)) for eid in counters]
        self.h.time.return_value += 3600

        self.restart()

        self.assertEqual([self.h.entity(eid) for eid in counters], saved_entities)
        self.assertEqual(self.h.world.get_state("w", m.META, "clock")["value"]["sequence"], clock["sequence"])
        self.h.ticks(1)
        after = self.h.world.get_state("w", domains.DOMAINS, owner)["value"]
        self.assertEqual(after["recovered_failure"], before["failure"])
        self.assertNotIn("failure", after)
        self.assertEqual(after["last_applied_tick"], work["target_tick"] + 1)
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [2, 1])

    def assert_terminal_error_survives_restart(self, error, status, code):
        counters = self.contract.two_counters()
        work = self.contract.begin_frame()
        first, second = self.contract.wait_one_counter(counters)
        pending = next(entry for entry in self.contract.frame()["pending"]
                       if self.h.world.get_state("w", domains.DOMAINS, entry["domain_id"])["value"]["last_tick"] < work["target_tick"])
        key = scheduling.DOMAIN_PREFIX + pending["domain_id"]
        with patch.object(processes, "advance_domain", side_effect=error):
            for _ in range(20):
                self.h.world.run_due_timers("w", limit=1)
                terminal = self.h.world.get_checkpoint("w", key)
                if terminal["status"] == status:
                    break
            else:
                self.fail("The real handler did not reach the expected terminal failure.")
        receipt = self.h.world.get_checkpoint_receipt("w", key)
        self.assertEqual(receipt["result"]["error_code"], code)
        domain_before = self.h.world.get_state("w", domains.DOMAINS, pending["domain_id"])["value"]
        self.assertNotIn("failure", domain_before)
        entities = [deepcopy(self.h.entity(eid)) for eid in counters]

        self.restart()

        self.assertEqual(self.h.world.get_checkpoint("w", key), terminal)
        self.assertEqual(self.h.world.get_checkpoint_receipt("w", key), receipt)
        self.assertEqual([self.h.entity(eid) for eid in counters], entities)
        closed = self.contract.finish_frame(work["id"])
        self.assertEqual(closed["faults"], 1)
        self.assertEqual(self.h.world.get_checkpoint("w", key)["generation"], terminal["generation"])
        domain_after = self.h.world.get_state("w", domains.DOMAINS, pending["domain_id"])["value"]
        self.assertEqual(domain_after["failure"], {
            "tick": work["target_tick"], "code": code, "computed": False})
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [1, 0])
        self.h.ticks(1)
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [2, 0])

    def test_failed_handler_receipt_before_coordinator_survives_revision_restart(self):
        self.assert_terminal_error_survives_restart(ValueError("real computation failed"),
            "failed", "CheckpointHandlerError")

    def test_rejected_handler_receipt_before_coordinator_survives_revision_restart(self):
        self.assert_terminal_error_survives_restart(RuleViolation("real rule rejected"),
            "rejected", "RuleViolation")

    def test_pending_retry_keeps_attempt_and_availability_across_same_revision_restart(self):
        counters = self.contract.two_counters()
        work = self.contract.begin_frame()
        first, second = self.contract.wait_one_counter(counters)
        pending = next(entry for entry in self.contract.frame()["pending"]
                       if self.h.world.get_state("w", domains.DOMAINS, entry["domain_id"])["value"]["last_tick"] < work["target_tick"])
        key = scheduling.DOMAIN_PREFIX + pending["domain_id"]
        with patch.object(processes, "advance_domain", side_effect=RetryTimer("wait for real retry")):
            for _ in range(20):
                self.h.world.run_due_timers("w", limit=1)
                checkpoint = self.h.world.get_checkpoint("w", key)
                if checkpoint["attempts"] == 1:
                    break
            else:
                self.fail("The real handler did not save a retry attempt.")
        self.assertEqual(checkpoint["status"], "pending")
        self.assertGreater(checkpoint["available_at"], self.h.time.return_value)
        saved = deepcopy(self.h.entity(second))

        self.restart(self.previous)

        self.assertEqual(self.h.world.get_checkpoint("w", key), checkpoint)
        self.h.world.run_due_timers("w", limit=1)
        self.assertEqual(self.h.world.get_checkpoint("w", key), checkpoint)
        self.assertEqual(self.h.entity(second), saved)
        self.contract.finish_frame(work["id"])
        self.assertEqual([self.contract.field(first), self.contract.field(second)], [1, 1])


class CheckpointUpgradeRecoveryContracts(unittest.TestCase):
    def facts(self):
        domain = {"id": "saved", "ordinal": 1, "topology_revision": 3, "status": "fault",
            "last_tick": 11, "last_applied_tick": 10, "creative_instances": ["object"],
            "topology_dirty": False,
            "failure": {"tick": 11, "code": "CheckpointVersionMismatch", "computed": False}}
        checkpoint = {"status": "blocked", "last_error_code": "CheckpointVersionMismatch",
            "handler": "grove.domain", "arguments": {"domain_id": "saved", "target_tick": 11,
                "topology_revision": 3, "phase": "advance", "frame_id": "old.11"}}
        return domain, checkpoint

    def test_recovery_requires_matching_runtime_fence_and_never_changes_applied_tick(self):
        ctx = StoredContext()
        ctx.set_state(m.META, "clock", {"sequence": 20})
        domain, checkpoint = self.facts()
        self.assertTrue(domains.recover_checkpoint_version(ctx, domain, checkpoint))
        restored = domains.get(ctx, domain["id"])
        self.assertEqual((restored["last_tick"], restored["last_applied_tick"]), (11, 10))
        self.assertEqual(restored["recovered_failure"], domain["failure"])
        self.assertEqual(ctx.get_state(domains.READY, domains.ready_key(domain))["eligible_tick"], 21)
        for mutate in (
            lambda d, c: d["failure"].update(computed=True),
            lambda d, c: d["failure"].update(code="CheckpointHandlerError"),
            lambda d, c: d.update(last_applied_tick=11),
            lambda d, c: c.update(status="failed"),
            lambda d, c: c.update(last_error_code="CheckpointHandlerError"),
            lambda d, c: c["arguments"].update(target_tick=12),
            lambda d, c: c["arguments"].update(topology_revision=4),
            lambda d, c: c["arguments"].update(domain_id="different"),
            lambda d, c: c["arguments"].update(phase="prepare"),
        ):
            candidate, source = self.facts()
            mutate(candidate, source)
            rows = deepcopy(ctx.rows)
            self.assertFalse(domains.recover_checkpoint_version(ctx, candidate, source))
            self.assertEqual(ctx.rows, rows)

    def test_resume_does_not_reissue_already_prepared_domain(self):
        ctx = SimpleNamespace(now=1000, function_version=35, world_resumed=False,
            get_state=Mock(return_value={"sequence": 10}), set_state=Mock())
        domain = {"id": "prepared", "status": "ready", "last_tick": 10, "prepared_frame": "saved.11"}
        work = {"id": "saved.11", "status": "preparing", "target_tick": 11,
            "pending": [{"domain_id": "prepared", "generation": 5}]}
        with patch.object(domains, "migrate"), patch.object(domains, "frame", return_value=work), \
                patch.object(domains, "get", return_value=domain), patch.object(domains, "_get", return_value=None), \
                patch.object(domains, "_set"), patch.object(scheduling, "_schedule_frame"), \
                patch.object(scheduling, "_schedule_domain") as schedule:
            scheduling.resume(ctx)
        schedule.assert_not_called()
        self.assertEqual(work["pending"][0]["generation"], 5)

    def test_resume_preserves_terminal_and_other_blocked_generations(self):
        for status, code, attempts in (("failed", "CheckpointHandlerError", 1), ("rejected", "RuleViolation", 1),
                                      ("completed", None, 1), ("blocked", "OtherVersionFailure", 0),
                                      ("pending", "RetryTimer", 1)):
            with self.subTest(status=status):
                ctx = SimpleNamespace(now=1000, function_version=35, world_resumed=False,
                    get_state=Mock(return_value={"sequence": 11}), set_state=Mock())
                domain = {"id": "saved", "status": "ready", "last_tick": 10}
                work = {"id": "saved.11", "status": "open", "target_tick": 11,
                    "pending": [{"domain_id": "saved", "generation": 5}]}
                checkpoint = {"generation": 5, "status": status, "last_error_code": code, "attempts": attempts}
                with patch.object(domains, "migrate"), patch.object(domains, "frame", return_value=work), \
                        patch.object(domains, "get", return_value=domain), patch.object(domains, "_get", return_value=None), \
                        patch.object(domains, "_set"), patch.object(scheduling, "_schedule_frame"), \
                        patch.object(scheduling, "_checkpoint", return_value=checkpoint), \
                        patch.object(scheduling, "_schedule_domain") as schedule:
                    scheduling.resume(ctx)
                schedule.assert_not_called()
                self.assertEqual(work["pending"][0]["generation"], 5)

    def test_recovery_scan_is_bounded_and_persists_the_next_cursor(self):
        domain, checkpoint = self.facts()
        ctx = SimpleNamespace(list_state=Mock(return_value={"items": [{"value": domain}],
            "has_more": True, "next_cursor": "next"}))
        progress = {"world_revision": 35, "cursor": "previous", "complete": False}
        with patch.object(domains, "_get", return_value=progress), patch.object(domains, "_set") as save, \
                patch.object(scheduling, "_checkpoint", return_value=checkpoint), \
                patch.object(domains, "recover_checkpoint_version", return_value=True) as recover:
            scheduling._recover_version_faults(ctx)
        ctx.list_state.assert_called_once_with(domains.DOMAINS, limit=scheduling.PAGE_SIZE, after="previous")
        recover.assert_called_once_with(ctx, domain, checkpoint)
        save.assert_called_once_with(ctx, domains.CONTROL, "checkpoint_recovery", {
            "world_revision": 35, "cursor": "next", "complete": False})


if __name__ == "__main__":
    unittest.main()
