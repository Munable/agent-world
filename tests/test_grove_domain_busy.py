"""Temporary domain settlement rejects writes without consuming their intent.

The product fixture advances real saved frames. These checks do not establish
autonomous model recovery or load performance.
"""
from copy import deepcopy
import unittest

from agent_world.errors import ReceiptNotFound, RuleViolation
from agent_world.transport_contracts import error_response
from agent_world.worlds.shared_grove import creative, domains, model as m
import test_grove_domain_contracts as fixtures
from test_grove_incremental_execution import StoredContext


class DomainBusyMetadataContracts(unittest.TestCase):
    def test_pending_boundary_preserves_state_and_public_retry_metadata(self):
        for stage in ("preparing", "open"):
            with self.subTest(stage=stage):
                ctx = StoredContext()
                domain = {"id": "pending", "ordinal": 1, "status": "ready", "last_tick": 3}
                ctx.set_state(domains.DOMAINS, domain["id"], domain)
                ctx.set_state(domains.CONTROL, "frame", {
                    "status": stage, "target_tick": 4, "high_water": 1})
                ctx.set_state(domains.READY, domains.ready_key(domain), {
                    "domain_id": domain["id"], "eligible_tick": 4})
                before = deepcopy(ctx.rows)

                with self.assertRaises(domains.DomainBusy) as raised:
                    domains.assert_boundary(ctx, [domain])

                self.assertIsInstance(raised.exception, RuleViolation)
                status, payload = error_response(raised.exception)
                self.assertEqual(status, 409)
                self.assertEqual(payload, {
                    "error": "DomainBusy",
                    "message": str(raised.exception),
                    "retryable": True,
                    "recovery": "wait_then_retry_same_operation",
                    "retry_after_seconds": 0.1,
                    "details": {"settlement": {
                        "state": "calculating", "target_tick": 4,
                        "operation_committed": False}},
                })
                self.assertEqual(ctx.rows, before)


class DomainBusyProductContracts(unittest.TestCase):
    def setUp(self):
        # Reuse helpers without inheriting/discovering the existing test suite.
        self.contract = fixtures.DomainProductContracts(methodName="runTest")
        self.contract.setUp()
        self.addCleanup(self.contract.doCleanups)
        self.h = self.contract.h
        self.addCleanup(self.h.world.close)

    def test_same_operation_after_settlement_commits_and_counts_once(self):
        h = self.h
        raw = fixtures.counter_definition("busy_boundary")
        raw["types"]["counter"]["ports"] = {
            "peer": {"fields": ["physical.contained_water"]}}
        reference = self.contract.c.publish(raw)
        counter = self.contract.c.form(reference, "counter")
        self.contract.c.place(counter)
        kettle = h.item("A", "水壶")["id"]
        h.interact("A", kettle, "drop")
        h.action("A", counter, "bind_port", port="peer", other_id=kettle)
        h.approach("B", kettle)
        grant = h.call("B", "grove.control_grant", {
            "goal": "关上公开水壶", "max_actions": 1})
        args = {"action": "close", "target_id": kettle,
                "expected_target_version": h.entity(kettle)["version"],
                "expected_player_version": h.view("B")["player"]["version"],
                "delegation": grant["delegation"]}
        original_args = deepcopy(args)
        operation = "close-after-domain-settlement"
        work = self.contract.begin_frame()
        owner = h.world.get_state("w", domains.OWNERS, counter)["value"]["domain_id"]
        pending = h.world.get_state("w", domains.DOMAINS, owner)["value"]
        self.assertLess(pending["last_tick"], work["target_tick"])

        keys = [(domains.CONTROL, "frame"), (domains.DOMAINS, owner),
                (domains.READY, domains.ready_key(pending)),
                (m.PLAYERS, h.roles["B"]), (creative.CONTROL, h.roles["B"]),
                (m.ENTITIES, counter), (m.ENTITIES, kettle)]
        # Include both inventories' actual lots/containers, not just a projected
        # material balance, so a rejected write cannot consume or move them.
        for actor in ("A", "B"):
            keys.extend((m.ENTITIES, item["id"]) for item in h.view(actor)["inventory"])
        before = {key: deepcopy(h.world.get_state("w", *key)) for key in keys}

        with self.assertRaises(domains.DomainBusy) as raised:
            h.world.call_function("w", "grove.interact", h.roles["B"], args,
                                  operation_id=operation)

        self.assertEqual(raised.exception.details["settlement"], {
            "state": "calculating", "target_tick": work["target_tick"],
            "operation_committed": False})
        self.assertEqual({key: h.world.get_state("w", *key) for key in keys}, before)
        self.assertEqual(h.read("B", "grove.control_status")["actions_count"], 0)
        with self.assertRaises(ReceiptNotFound):
            h.world.get_receipt("w", h.roles["B"], operation)

        self.contract.finish_frame(work["id"])
        self.assertEqual(self.contract.field(counter), 1)
        # The counter advances, but the action's observed kettle/player remain
        # unchanged: retrying must not refresh CAS or replace the user's intent.
        for key in ((m.ENTITIES, kettle), (m.PLAYERS, h.roles["B"]),
                    (creative.CONTROL, h.roles["B"])):
            self.assertEqual(h.world.get_state("w", *key), before[key])
        first = h.world.call_function("w", "grove.interact", h.roles["B"], args,
                                      operation_id=operation)
        self.assertTrue(first["ok"])
        self.assertFalse(first["replayed"])
        self.assertFalse(h.entity(kettle)["state"]["open"])
        saved = h.entity(kettle)
        control = h.read("B", "grove.control_status")
        self.assertEqual(control["actions_count"], 1)
        self.assertEqual(control["status"], "exhausted")
        self.assertEqual(saved["matter"], before[m.ENTITIES, kettle]["value"]["matter"])

        replay = h.world.call_function("w", "grove.interact", h.roles["B"], args,
                                       operation_id=operation)
        self.assertEqual(replay, {**first, "replayed": True})
        self.assertEqual(h.world.get_receipt("w", h.roles["B"], operation), first)
        self.assertEqual(h.entity(kettle), saved)
        self.assertEqual(h.read("B", "grove.control_status"), control)
        self.assertEqual(args, original_args)


if __name__ == "__main__":
    unittest.main()
