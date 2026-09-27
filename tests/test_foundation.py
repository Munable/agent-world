from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import gc
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from agent_world.runtime_core import WorldRuntime, FunctionContext, FunctionOutcome, EventSpec
from agent_world.errors import (
    AccessModeMismatch,
    ClaimFenced,
    CursorExpired,
    CursorInvalid,
    IdentityScopeMismatch,
    InvalidArguments,
    InvalidIdentityToken,
    JoinTicketConsumed,
    JoinTicketExpired,
    JoinTicketInvalid,
    OperationConflict,
    PermissionDenied,
    ReceiptNotFound,
    RegistryConflict,
    ResultRejected,
    RoleInactive,
    RoleInvalid,
    StateConflict,
    WorldRuntimeError,
)
from agent_world.runtime_contracts import CORE_TOOL_NAMES
from agent_world import WorldDefinition, FunctionSpec, StateRule
from agent_world.transport_contracts import WorldGateway, function_schema
from agent_world.universe_loader import get_universe_installer, get_world_definition
from jsonschema import Draft202012Validator

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "world.sqlite3"
        self.w = WorldRuntime(self.path)
        self.role = self.w.create_role("A")["role_id"]

    def tearDown(self):
        gc.collect()
        self.temp.cleanup()  # Do not hide leaked Windows handles.

    def register(self, name="test.write", handler=None, access="write", **kwargs):
        if handler is None:
            handler = lambda c, a: FunctionOutcome({"ok": True})
        self.w.register_function("u", name, 1, name, EMPTY, handler, access=access, **kwargs)
        return name

    def invoke(self, name, op="op", **kwargs):
        return self.w.invoke_function("u", name, self.role, {}, operation_id=op, **kwargs)

    def counts(self):
        with self.w._conn(readonly=True) as c:
            return tuple(
                c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("world_state", "events", "operations")
            )

    def test_connection_context_closes_handle(self):
        with self.w._conn() as connection:
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_context_rolls_back_base_exception(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.w._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO runtime_meta VALUES('rollback-test','x')")
                raise KeyboardInterrupt()
        with self.w._conn(readonly=True) as c:
            self.assertIsNone(
                c.execute("SELECT 1 FROM runtime_meta WHERE meta_key='rollback-test'").fetchone()
            )

    def test_read_cannot_be_invoked_as_write(self):
        def bad(ctx, args):
            ctx.set_state("s", "k", 1)
            return FunctionOutcome({})

        self.register("bad.read", bad, "read")
        with self.assertRaises(AccessModeMismatch):
            self.invoke("bad.read")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_read_cannot_disable_guard(self):
        def bad(ctx, args):
            ctx._conn.execute("PRAGMA query_only=OFF")
            ctx._conn.execute("DELETE FROM roles")
            return FunctionOutcome({})

        self.register("bad.read", bad, "read")
        with self.assertRaises(WorldRuntimeError):
            self.w.query_function("u", "bad.read", self.role, {})
        self.assertEqual(self.w.get_role(self.role)["display_name"], "A")

    def test_write_cannot_commit_partially(self):
        def bad(ctx, args):
            ctx.set_state("s", "k", 1)
            ctx._conn.execute("COMMIT")
            return FunctionOutcome({})

        self.register("bad.commit", bad)
        with self.assertRaises(WorldRuntimeError):
            self.invoke("bad.commit")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_bad_output_schema_rolls_back_everything(self):
        def bad(ctx, args):
            ctx.set_state("s", "k", 1)
            return FunctionOutcome({"count": "not-an-int"}, (EventSpec(self.role, "bad", {}),))

        schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
        self.register("bad.output", bad, output_schema=schema)
        with self.assertRaises(ResultRejected):
            self.invoke("bad.output")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_nonfinite_output_and_events_are_rejected(self):
        for value in (float("nan"), float("inf"), object()):
            self.register("bad.result", lambda c, a, v=value: FunctionOutcome({"value": v}))
            with self.assertRaises(WorldRuntimeError):
                self.invoke("bad.result")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_missing_and_unknown_fields_are_not_coerced(self):
        get_universe_installer("demo")(self.w, "u")
        token = self.w.issue_identity_token("u", self.role)["token"]
        gateway = WorldGateway(self.w, "u", auth_required=True)
        header = "Bearer " + token
        for args in ({"limit": -1}, {"limit": True}, {"after": -1}, {"extra": 1}):
            with self.assertRaises(WorldRuntimeError):
                gateway.call("world.get_changes", args, header)
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(WorldRuntimeError):
                gateway.prepare("world.wait_changes", {"timeout": value}, header)
        with self.assertRaises(WorldRuntimeError):
            gateway.call("counter.increment", {"operation_id": None, "arguments": {"amount": 1}}, header)

    def test_local_schema_refs_survive_tool_envelope(self):
        schema = {
            "type": "object",
            "$defs": {"amount": {"type": "integer", "minimum": 1}},
            "properties": {"amount": {"$ref": "#/$defs/amount"}},
            "required": ["amount"],
            "additionalProperties": False,
        }
        self.w.register_function(
            "u", "ref.read", 1, "refs", schema, lambda c, a: FunctionOutcome(a), access="read"
        )
        envelope = function_schema(self.w.get_function("u", "ref.read"), auth_required=True)
        Draft202012Validator(envelope).validate({"arguments": {"amount": 2}})
        self.assertFalse(Draft202012Validator(envelope).is_valid({"arguments": {"amount": 0}}))

    def test_external_schema_refs_and_reserved_names_rejected(self):
        with self.assertRaises(RegistryConflict):
            self.w.register_function(
                "u",
                "bad.refs",
                1,
                "bad",
                {"type": "object", "$ref": "https://example.invalid/schema"},
                lambda c, a: FunctionOutcome({}),
            )
        for name in CORE_TOOL_NAMES:
            with self.assertRaises(RegistryConflict):
                self.register(name)

    def test_access_mode_is_immutable(self):
        self.register("immutable", access="read")
        with self.assertRaises(RegistryConflict):
            self.w.register_function(
                "u", "immutable", 2, "changed", EMPTY, lambda c, a: FunctionOutcome({}), access="write"
            )

    def test_authorization_failure_has_no_side_effect(self):
        self.register("secret.write", lambda c, a: c.set_state("s", "k", 1), authorize=lambda c, a: False)
        with self.assertRaises(PermissionDenied):
            self.invoke("secret.write")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_authorization_callback_cannot_write(self):
        def policy(ctx, args):
            ctx._conn.execute("DELETE FROM roles")
            return True

        self.register("bad.policy", authorize=policy)
        with self.assertRaises(WorldRuntimeError):
            self.invoke("bad.policy")
        self.assertEqual(self.w.get_role(self.role)["display_name"], "A")

    def test_state_cas_tombstones_prevent_aba(self):
        def handler(ctx, args):
            v1 = ctx.set_state("s", "k", 1, expected_version=0)
            v2 = ctx.delete_state("s", "k", expected_version=v1)
            self.assertFalse(ctx.get_state_record("s", "k")["exists"])
            with self.assertRaises(StateConflict):
                ctx.set_state("s", "k", 2, expected_version=0)
            v3 = ctx.set_state("s", "k", 2, expected_version=v2)
            return FunctionOutcome({"versions": [v1, v2, v3]})

        self.register("cas", handler)
        self.assertEqual(self.invoke("cas")["result"]["versions"], [1, 2, 3])

    def test_receipt_recovers_without_current_function(self):
        self.register("old")
        receipt = self.invoke("old")
        with self.w._conn() as c:
            c.execute("DELETE FROM function_registry WHERE function_id='old'")
        self.assertEqual(self.w.get_receipt("u", self.role, "op"), receipt)
        self.assertTrue(self.w.call_function("u", "old", self.role, {}, operation_id="op")["replayed"])
        with self.assertRaises(ReceiptNotFound):
            self.w.get_receipt("u", "other", "op")

    def test_receipt_is_bound_to_activity_target(self):
        self.register("claimed", requires_activity_claim=True)
        proofs = []
        for activity in ("a", "b"):
            self.w.start_activity("u", activity, self.role, "kind")
            proofs.append(self.w.claim_activity("u", activity, self.role, "r"))
        self.invoke("claimed", activity_claim=proofs[0])
        with self.assertRaises(OperationConflict):
            self.invoke("claimed", activity_claim=proofs[1])

    def test_revocation_while_waiting_for_write_lock(self):
        self.register("queued")
        token = self.w.issue_identity_token("u", self.role)
        with self.w._conn() as blocker, ThreadPoolExecutor(1) as pool:
            blocker.execute("BEGIN IMMEDIATE")
            future = pool.submit(self.invoke, "queued", identity_token=token["token"])
            time.sleep(0.08)
            blocker.execute(
                "UPDATE identity_tokens SET revoked_at=? WHERE token_id=?", (time.time(), token["token_id"])
            )
            blocker.commit()
            with self.assertRaises(InvalidIdentityToken):
                future.result(timeout=5)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_claim_expiry_while_waiting_for_lock(self):
        self.register("queued.claim", requires_activity_claim=True)
        self.w.start_activity("u", "a", self.role, "kind")
        proof = self.w.claim_activity("u", "a", self.role, "r", lease_seconds=0.15)
        with self.w._conn() as blocker, ThreadPoolExecutor(1) as pool:
            blocker.execute("BEGIN IMMEDIATE")
            future = pool.submit(self.invoke, "queued.claim", activity_claim=proof)
            time.sleep(0.20)
            blocker.commit()
            with self.assertRaises(ClaimFenced):
                future.result(timeout=5)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_expiry_during_handler_rolls_back(self):
        def delayed(ctx, args):
            ctx.set_state("s", "k", 1)
            time.sleep(0.20)
            return FunctionOutcome({})

        self.register("slow", delayed)
        token = self.w.issue_identity_token("u", self.role, ttl_seconds=0.10)
        with self.assertRaises(InvalidIdentityToken):
            self.invoke("slow", identity_token=token["token"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_event_page_bounds_and_retention(self):
        self.register("event", lambda c, a: FunctionOutcome({}, (EventSpec(c.actor_role_id, "event", {}),)))
        for i in range(5):
            self.invoke("event", str(i))
        page = self.w.read_changes_page("u", self.role, 0, 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["events"]), 2)
        second = self.w.read_changes_page("u", self.role, page["next_cursor"], 2)
        self.assertEqual(second["events"][0]["seq"], 3)
        self.assertEqual(self.w.read_changes_page("u", "someone-else")["next_cursor"], 5)
        with self.assertRaises(CursorInvalid):
            self.w.read_changes("u", self.role, 99)
        with self.assertRaises(InvalidArguments):
            self.w.read_changes("u", self.role, 0, -1)
        self.w.cleanup_events("u", 99999)
        self.assertEqual(self.w.event_floor("u"), 5)
        with self.assertRaises(CursorExpired):
            self.w.read_changes("u", self.role, 0)

    def test_wait_observes_other_runtime_writer(self):
        self.register("event", lambda c, a: FunctionOutcome({}, (EventSpec(c.actor_role_id, "event", {}),)))
        other = WorldRuntime(self.path)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(other.wait_changes, "u", self.role, 0, 1)
            time.sleep(0.06)
            self.invoke("event")
            self.assertEqual(len(future.result(timeout=2)), 1)

    def test_finish_releases_group_and_is_idempotent(self):
        self.w.start_activity("u", "a", self.role, "kind", exclusive_group="work")
        claim = self.w.claim_activity("u", "a", self.role, "r")
        renewed = self.w.renew_claim("u", self.role, claim, operation_id="renew", lease_seconds=60)
        self.assertEqual(
            self.w.renew_claim("u", self.role, claim, operation_id="renew", lease_seconds=60)["result"],
            renewed["result"],
        )
        result = self.w.finish_activity("u", self.role, claim, operation_id="finish")
        self.assertEqual(result["result"]["status"], "completed")
        self.assertTrue(self.w.finish_activity("u", self.role, claim, operation_id="finish")["replayed"])
        self.w.start_activity("u", "b", self.role, "kind", exclusive_group="work")
        with self.assertRaises(ClaimFenced):
            self.w.claim_activity("u", "a", self.role, "r")

    def test_ticket_exchange_is_bound_to_world_endpoint(self):
        ticket = self.w.issue_join_ticket("u", self.role)
        with self.assertRaises(IdentityScopeMismatch):
            self.w.exchange_join_ticket(ticket["ticket"], expected_universe="other")
        self.assertIsNone(self.w.get_join_ticket_metadata(ticket["ticket_id"])["used_at"])
        self.w.revoke_join_ticket(ticket["ticket_id"])
        with self.assertRaises(JoinTicketInvalid):
            self.w.exchange_join_ticket(ticket["ticket"])

    def test_join_ticket_exchange_is_concurrent_safe_and_secrets_are_hashed(self):
        get_universe_installer("demo")(self.w, "u")
        ticket = self.w.issue_join_ticket("u", self.role, ttl_seconds=60)
        with self.w._conn(readonly=True) as conn:
            stored_ticket_hash = conn.execute(
                "SELECT ticket_hash FROM join_tickets WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()[0]
        self.assertNotEqual(stored_ticket_hash, ticket["ticket"])
        self.assertNotIn(ticket["ticket"], stored_ticket_hash)

        def exchange(_):
            local = WorldRuntime(self.path)
            result = local.exchange_join_ticket(ticket["ticket"])
            return result["replayed"], result["token_id"], result["token"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(exchange, range(8)))

        self.assertEqual(sum(not replayed for replayed, _, _ in rows), 1)
        self.assertEqual(len({token_id for _, token_id, _ in rows}), 1)
        self.assertEqual(len({token for _, _, token in rows}), 1)

        token_id = rows[0][1]
        token = rows[0][2]
        with self.w._conn(readonly=True) as conn:
            stored_token_hash = conn.execute(
                "SELECT token_hash FROM identity_tokens WHERE token_id=?",
                (token_id,),
            ).fetchone()[0]
        self.assertNotEqual(stored_token_hash, token)
        self.assertNotIn(token, stored_token_hash)

    def test_identity_rotation_revokes_old_token_and_ticket_cannot_resurrect_it(self):
        get_universe_installer("demo")(self.w, "u")
        ticket = self.w.issue_join_ticket("u", self.role)
        first = self.w.exchange_join_ticket(ticket["ticket"])
        second = self.w.rotate_identity_token(
            first["token_id"], operation_id="rotate-identity", ttl_seconds=60
        )

        self.assertEqual(second["rotated_from_token_id"], first["token_id"])
        self.assertNotEqual(second["token"], first["token"])
        with self.assertRaises(InvalidIdentityToken):
            self.w.resolve_identity_token(first["token"])
        self.assertEqual(
            self.w.resolve_identity_token(second["token"])["token_id"],
            second["token_id"],
        )
        with self.assertRaises(JoinTicketConsumed):
            self.w.exchange_join_ticket(ticket["ticket"])

    def test_identity_rotation_unknown_result_recovers_by_operation_id(self):
        get_universe_installer("demo")(self.w, "u")
        source = self.w.issue_identity_token("u", self.role)
        first = self.w.rotate_identity_token(
            source["token_id"],
            operation_id="rotate-recover",
            ttl_seconds=60,
        )
        self.assertFalse(first["replayed"])

        restarted = WorldRuntime(self.path)
        get_universe_installer("demo")(restarted, "u")
        recovered = restarted.get_identity_token_rotation("u", "rotate-recover")
        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["token_id"], first["token_id"])
        self.assertEqual(recovered["token"], first["token"])

        replay = restarted.rotate_identity_token(
            source["token_id"],
            operation_id="rotate-recover",
            ttl_seconds=60,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["token_id"], first["token_id"])
        self.assertEqual(replay["token"], first["token"])

        with self.assertRaises(OperationConflict):
            restarted.rotate_identity_token(
                source["token_id"],
                operation_id="rotate-recover",
                ttl_seconds=120,
            )
        other = restarted.issue_identity_token("u", self.role)
        with self.assertRaises(OperationConflict):
            restarted.rotate_identity_token(
                other["token_id"],
                operation_id="rotate-recover",
                ttl_seconds=60,
            )

        with restarted._conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM identity_token_rotations WHERE universe=? AND operation_id=?",
                ("u", "rotate-recover"),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(first["token"], repr(tuple(row)))

    def test_expired_ticket_and_disabled_role_do_not_authenticate(self):
        get_universe_installer("demo")(self.w, "u")
        expired = self.w.issue_join_ticket("u", self.role, ttl_seconds=0.02)
        time.sleep(0.04)
        with self.assertRaises(JoinTicketExpired):
            self.w.exchange_join_ticket(expired["ticket"])

        live = self.w.issue_join_ticket("u", self.role, ttl_seconds=60)
        self.w.set_role_status(self.role, "disabled")
        with self.assertRaises(RoleInactive):
            self.w.exchange_join_ticket(live["ticket"])

        self.w.set_role_status(self.role, "active")
        identity = self.w.exchange_join_ticket(
            self.w.issue_join_ticket("u", self.role)["ticket"]
        )
        self.w.set_role_status(self.role, "disabled")
        with self.assertRaises(InvalidIdentityToken):
            self.w.resolve_identity_token(identity["token"])

    def test_status_does_not_confuse_incoming_event_with_action(self):
        ticket = self.w.issue_join_ticket("u", self.role)
        self.w.exchange_join_ticket(ticket["ticket"])
        self.w.issue_join_ticket("u", self.role)
        self.register("note", lambda c, a: FunctionOutcome({}, (EventSpec(self.role, "note", {}),)))
        self.w.invoke_function("u", "note", "other", {}, operation_id="incoming")
        status = self.w.get_role_entry_status("u", self.role)
        self.assertTrue(status["identity_claimed"])
        self.assertGreater(status["latest_recipient_event_seq"], 0)
        self.assertEqual(status["world_action_count"], 0)
        self.w.bootstrap("u", self.role)
        self.assertFalse(self.w.get_role_entry_status("u", self.role)["entered_world"])

    def test_role_profile_update_and_strict_inputs(self):
        for name in (None, 3, "bad\nname"):
            with self.assertRaises(RoleInvalid):
                self.w.create_role(name)
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(self.w.update_role_profile, self.role, display_name="Updated")
            b = pool.submit(self.w.update_role_profile, self.role, avatar_ref="avatar://new")
            a.result()
            b.result()
        self.assertEqual(self.w.get_role(self.role)["display_name"], "Updated")
        self.assertEqual(self.w.get_role(self.role)["avatar_ref"], "avatar://new")


if __name__ == "__main__":
    unittest.main()
