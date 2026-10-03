"""Original Command source survives same-Principal credential changes.

Credential IDs identify the authenticated submission, not an Agent program or
an independent business authority. Trusted and timer submissions have no bearer.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_world import (
    FunctionOutcome, FunctionSpec, StateRule, TimerSpec, WorldDefinition, WorldRuntime,
)
from agent_world.errors import InvalidIdentityToken, ReceiptNotFound
from agent_world.world_timers import TIMER_ACTOR
from tests.live_server import LiveServer


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def auth(identity):
    return {"Authorization": "Bearer " + identity["token"]}


def envelope(operation, amount=3):
    return {"operation_id": operation, "arguments": {"amount": amount}}


class CredentialAttributionTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LiveServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    async def entries(self):
        role, first, key = await asyncio.to_thread(self.server.key_identity)
        returned, second, _ = await asyncio.to_thread(self.server.key_identity, key)
        self.assertEqual(returned["role_id"], role["role_id"])
        self.assertNotEqual(first["token_id"], second["token_id"])
        return role["role_id"], first, second, key

    def receipt(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_http_source_survives_mcp_replay_and_receipt_scope(self):
        actor, first, second, _ = await self.entries()
        operation = "http-original-source"
        runtime = WorldRuntime(self.server.db)
        with runtime._conn(readonly=True) as conn:
            schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        observe = runtime.issue_identity_token("network", actor, access_mode="observe")
        _, peer, _ = await asyncio.to_thread(self.server.key_identity)
        async with httpx.AsyncClient(base_url=self.server.url, trust_env=False, timeout=5) as http:
            original = self.receipt(await http.post(
                "/v1/functions/counter.increment/invoke", headers=auth(first), json=envelope(operation),
            ))
            self.assertEqual(original["actor_role_id"], actor)
            self.assertEqual(original["credential_id"], first["token_id"])
            async with self.server.session(second) as (session, _):
                replay = await session.call_tool("counter.increment", arguments=envelope(operation))
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertEqual(replay.structured_content, {**original, "replayed": True})
                lookup = await session.call_tool("world.get_receipt", arguments={"operation_id": operation})
                self.assertFalse(lookup.is_error, lookup.structured_content)
                self.assertEqual(lookup.structured_content, original)
            self.assertEqual(self.receipt(await http.get(
                f"/v1/receipts/{operation}", headers=auth(observe),
            )), original)
            for attempted in (operation, "observe-new-operation"):
                denied = await http.post("/v1/functions/counter.increment/invoke",
                                         headers=auth(observe), json=envelope(attempted))
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(denied.json()["error"], "PermissionDenied")
            missing = await http.get(f"/v1/receipts/{operation}", headers=auth(peer))
            self.assertEqual(missing.status_code, 404, missing.text)
            self.assertEqual(missing.json()["error"], "ReceiptNotFound")
        with runtime._conn(readonly=True) as conn:
            row = conn.execute("SELECT receipt_json FROM operations WHERE universe='network' "
                               "AND actor_role_id=? AND idempotency_key=?", (actor, operation)).fetchone()
            self.assertEqual(json.loads(row[0]), original)
            for identity in (first, second, observe, peer):
                self.assertNotIn(identity["token"], row[0])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], schema_version)

    async def test_cross_credential_concurrent_retry_has_one_source_and_one_effect(self):
        actor, first, second, _ = await self.entries()
        operation = "concurrent-source"
        async with httpx.AsyncClient(base_url=self.server.url, trust_env=False, timeout=5) as http:
            responses = await asyncio.gather(*[
                http.post("/v1/functions/counter.increment/invoke", headers=auth(identity),
                          json=envelope(operation)) for identity in (first, second)
            ])
            receipts = [self.receipt(response) for response in responses]
            self.assertEqual(sorted(r["replayed"] for r in receipts), [False, True])
            winner = next(index for index, receipt in enumerate(receipts) if not receipt["replayed"])
            original = receipts[winner]
            self.assertEqual(original["actor_role_id"], actor)
            self.assertEqual(original["credential_id"], (first, second)[winner]["token_id"])
            self.assertEqual(receipts[1 - winner], {**original, "replayed": True})
            conflict = await http.post("/v1/functions/counter.increment/invoke", headers=auth(second),
                                       json=envelope(operation, amount=4))
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()["error"], "OperationConflict")
        runtime = WorldRuntime(self.server.db)
        self.assertEqual(runtime.get_state("network", f"role:{actor}", "counter")["value"], 3)
        with runtime._conn(readonly=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM operations WHERE universe='network' "
                               "AND actor_role_id=? AND idempotency_key=?", (actor, operation)).fetchone()
            self.assertEqual(row[0], 1)
            events = conn.execute("SELECT seq FROM events WHERE universe='network' AND actor_role_id=?",
                                  (actor,)).fetchall()
            self.assertEqual([row[0] for row in events], original["event_seqs"])
            changes = conn.execute("SELECT COUNT(*) FROM state_changes WHERE commit_seq=?",
                                   (original["commit_seq"],)).fetchone()
            self.assertEqual(changes[0], 1)

    async def test_restart_and_new_same_key_credential_recover_original_source(self):
        actor, first, second, key = await self.entries()
        operation = "restart-source"
        async with httpx.AsyncClient(base_url=self.server.url, trust_env=False, timeout=5) as http:
            original = self.receipt(await http.post(
                "/v1/functions/counter.increment/invoke", headers=auth(first), json=envelope(operation),
            ))
            self.assertEqual(original["credential_id"], first["token_id"])
            runtime = WorldRuntime(self.server.db)
            runtime.revoke_identity_token(first["token_id"])
            runtime.revoke_identity_token(second["token_id"])
            await asyncio.to_thread(self.server.restart, abrupt=True)
            returned, replacement, _ = await asyncio.to_thread(self.server.key_identity, key)
            self.assertEqual(returned["role_id"], actor)
            self.assertNotIn(replacement["token_id"], (first["token_id"], second["token_id"]))
            self.assertEqual(self.receipt(await http.get(
                f"/v1/receipts/{operation}", headers=auth(replacement),
            )), original)
            self.assertEqual(self.receipt(await http.post(
                "/v1/functions/counter.increment/invoke", headers=auth(replacement), json=envelope(operation),
            )), {**original, "replayed": True})
            revoked = await http.get(f"/v1/receipts/{operation}", headers=auth(first))
            self.assertEqual(revoked.status_code, 401, revoked.text)
        recovered = WorldRuntime(self.server.db)
        self.assertEqual(recovered.get_receipt("network", actor, operation,
                                              identity_token=replacement["token"]), original)
        self.assertEqual(recovered.get_state("network", f"role:{actor}", "counter")["value"], 3)


class CredentialAttributionTrustedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="credential-attribution-")
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.db)
        self.actor = self.runtime.create_role("Writer")["role_id"]

    def test_trusted_submission_has_explicit_null_credential_even_when_tokens_exist(self):
        def write(ctx, args):
            ctx.set_state("world", "value", 1)
            return FunctionOutcome({"value": 1})
        definition = WorldDefinition("source", "Source", (FunctionSpec("write", write, EMPTY),),
                                     state_rules=(StateRule("world", "value", {"type": "integer"}),))
        self.runtime.install_world("u", definition)
        identity = self.runtime.issue_identity_token("u", self.actor)
        original = self.runtime.call_function("u", "write", self.actor, {}, operation_id="trusted")
        self.assertEqual(original["actor_role_id"], self.actor)
        self.assertIn("credential_id", original)
        self.assertIsNone(original["credential_id"])
        recovered = WorldRuntime(self.db)
        recovered.install_world("u", definition)
        replay = recovered.call_function("u", "write", self.actor, {}, operation_id="trusted",
                                         identity_token=identity["token"])
        self.assertEqual(replay, {**original, "replayed": True})

    def test_timer_source_is_system_actor_with_null_credential_not_schedulers_token(self):
        def arm(ctx, args):
            ctx.schedule_timer("settle", "settle", {}, due_at=ctx.now)
            return FunctionOutcome({})
        def settle(ctx, args):
            ctx.set_state("world", "value", 1)
            return FunctionOutcome({"value": 1})
        definition = WorldDefinition("source", "Source", (FunctionSpec("arm", arm, EMPTY),),
                                     state_rules=(StateRule("world", "value", {"type": "integer"}),),
                                     timers=(TimerSpec("settle", settle, EMPTY),))
        self.runtime.install_world("u", definition)
        identity = self.runtime.issue_identity_token("u", self.actor)
        scheduled = self.runtime.call_function("u", "arm", self.actor, {}, operation_id="arm",
                                               identity_token=identity["token"])
        self.assertEqual(scheduled["credential_id"], identity["token_id"])
        self.runtime.revoke_identity_token(identity["token_id"])
        self.assertEqual(self.runtime.run_due_timers("u")["count"], 1)
        fired = self.runtime.get_timer_receipt("u", "settle")
        self.assertEqual(fired["actor_role_id"], TIMER_ACTOR)
        self.assertIn("credential_id", fired)
        self.assertIsNone(fired["credential_id"])
        self.assertEqual(fired["scheduled_by"], self.actor)
        self.assertEqual(self.runtime.get_receipt("u", TIMER_ACTOR, fired["operation_id"]), fired)

    def test_expired_precommit_credential_rolls_back_receipt_source_and_state(self):
        with patch("time.time", return_value=1000.0) as clock:
            def expire(ctx, args):
                ctx.set_state("world", "value", 1)
                clock.return_value = 1002.0
                return FunctionOutcome({})
            definition = WorldDefinition("source", "Source", (FunctionSpec("expire", expire, EMPTY),),
                                         state_rules=(StateRule("world", "value", {"type": "integer"}),))
            self.runtime.install_world("u", definition)
            identity = self.runtime.issue_identity_token("u", self.actor, ttl_seconds=1)
            with self.assertRaises(InvalidIdentityToken):
                self.runtime.call_function("u", "expire", self.actor, {}, operation_id="expired",
                                           identity_token=identity["token"])
        self.assertFalse(self.runtime.get_state("u", "world", "value")["exists"])
        with self.assertRaises(ReceiptNotFound):
            self.runtime.get_receipt("u", self.actor, "expired")
        with self.runtime._conn(readonly=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
