from __future__ import annotations

import asyncio
import json
import time
import unittest

import httpx

from agent_world.runtime_core import WorldRuntime
from agent_world.runtime_contracts import CORE_TOOL_NAMES
from tests.live_server import LiveServer


class TransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LiveServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    async def asyncSetUp(self):
        self.role, self.identity = self.server.role()
        self.auth = {"Authorization": "Bearer " + self.identity["token"]}

    async def test_single_origin_full_entry_http_mcp_and_receipt(self):
        async with self.server.session(self.identity) as (session, _):
            tools = await session.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            self.assertEqual(
                set(by_name), CORE_TOOL_NAMES | {"counter.get", "counter.increment", "activity.score.add"}
            )
            self.assertTrue(by_name["counter.get"].annotations.read_only_hint)
            self.assertTrue(by_name["world.describe"].annotations.read_only_hint)
            self.assertFalse(by_name["world.bootstrap"].annotations.read_only_hint)
            boot = await session.call_tool("world.bootstrap", arguments={})
            self.assertFalse(boot.is_error, boot.structured_content)
            self.assertNotIn("functions", boot.structured_content)
            self.assertEqual(boot.structured_content["world_guide"], {"tool": "world.describe"})
            self.assertEqual(boot.structured_content["world_entry_state"]["view"]["counter"], 0)
            described = await session.call_tool("world.describe", arguments={})
            self.assertFalse(described.is_error, described.structured_content)
            self.assertEqual(described.structured_content["world"]["id"], boot.structured_content["world"]["id"])
            self.assertEqual(described.structured_content["world"]["version"], boot.structured_content["world"]["version"])
            self.assertTrue(described.structured_content["entry_instructions"])
            result = await session.call_tool(
                "counter.increment", arguments={"operation_id": "network-op", "arguments": {"amount": 3}}
            )
            self.assertFalse(result.is_error, result.structured_content)
            with httpx.Client(trust_env=False) as client:
                description = client.get(self.server.url + "/v1/describe", headers=self.auth)
                self.assertEqual(description.status_code, 200, description.text)
                self.assertEqual(description.json(), described.structured_content)
                receipt = client.get(self.server.url + "/v1/receipts/network-op", headers=self.auth)
                self.assertEqual(receipt.status_code, 200, receipt.text)
                self.assertEqual(receipt.json()["result"]["value"], 3)
                self.assertEqual(receipt.headers["cache-control"], "no-store")
                replay = client.post(
                    self.server.url + "/v1/functions/counter.increment/invoke",
                    headers=self.auth,
                    json={"operation_id": "network-op", "arguments": {"amount": 3}},
                )
                self.assertTrue(replay.json()["replayed"])
                status = client.get(
                    self.server.url + f"/api/roles/{self.role['role_id']}/status",
                    auth=("operator", self.server.password),
                ).json()
                self.assertEqual(status["world_action_count"], 1)
                self.assertTrue(status["entered_world"])

    async def test_duplicate_authentication_headers_are_rejected(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                self.server.url + "/mcp",
                headers=[
                    ("Authorization", "Bearer " + self.identity["token"]),
                    ("Authorization", "Bearer " + self.identity["token"]),
                ],
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            self.assertEqual(response.status_code, 400, response.text)

    async def test_same_invalid_function_arguments_rejected_on_both_transports(self):
        args = {"operation_id": "bad-op", "arguments": {"amount": 999}}
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                self.server.url + "/v1/functions/counter.increment/invoke", headers=self.auth, json=args
            )
            self.assertEqual(response.status_code, 422)
            http_error = response.json()["error"]
        async with self.server.session(self.identity) as (session, _):
            result = await session.call_tool("counter.increment", arguments=args)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error"], http_error)
            result = await session.call_tool("world.get_changes", arguments={"limit": -1})
            self.assertTrue(result.is_error)
            result = await session.call_tool(
                "counter.get", arguments={"arguments": {}, "operation_id": "unnecessary"}
            )
            self.assertTrue(result.is_error)

    async def test_mcp_session_bound_to_credential_not_just_role_name(self):
        _, other = self.server.role("Other")
        async with self.server.session(self.identity) as (session, captured):
            async with httpx.AsyncClient(trust_env=False) as client:
                headers = {
                    "Authorization": "Bearer " + other["token"],
                    "Mcp-Session-Id": captured["session_id"],
                    "MCP-Protocol-Version": "2025-11-25",
                    "Accept": "application/json, text/event-stream",
                }
                result = await client.post(
                    self.server.url + "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 77,
                        "method": "tools/call",
                        "params": {"name": "world.bootstrap", "arguments": {}},
                    },
                )
                self.assertEqual(result.status_code, 403, result.text)
                fresh = WorldRuntime(self.server.db).issue_identity_token("network", self.role["role_id"])
                headers["Authorization"] = "Bearer " + fresh["token"]
                result = await client.post(
                    self.server.url + "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 78, "method": "tools/list", "params": {}},
                )
                self.assertEqual(result.status_code, 403, result.text)
            boot = await session.call_tool("world.bootstrap", arguments={})
            self.assertEqual(boot.structured_content["role_id"], self.role["role_id"])

    async def test_revoked_token_stops_live_mcp_session(self):
        async with self.server.session(self.identity, terminate=False) as (_, captured):
            WorldRuntime(self.server.db).revoke_identity_token(self.identity["token_id"])
            async with httpx.AsyncClient(trust_env=False) as client:
                headers = {
                    **self.auth,
                    "Mcp-Session-Id": captured["session_id"],
                    "MCP-Protocol-Version": "2025-11-25",
                    "Accept": "application/json, text/event-stream",
                }
                result = await client.post(
                    self.server.url + "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 77, "method": "tools/list", "params": {}},
                )
                self.assertEqual(result.status_code, 401, result.text)
                result = await client.get(self.server.url + "/v1/bootstrap", headers=self.auth)
                self.assertEqual(result.status_code, 401, result.text)

    async def test_wait_is_bounded_and_session_survives_cancel(self):
        async with self.server.session(self.identity) as (session, _):
            start = time.monotonic()
            result = await session.call_tool("world.wait_changes", arguments={"timeout": 0.1})
            self.assertFalse(result.is_error, result.structured_content)
            self.assertTrue(result.structured_content["timed_out"])
            self.assertLess(time.monotonic() - start, 2)
            pending = asyncio.create_task(session.call_tool("world.wait_changes", arguments={"timeout": 5}))
            await asyncio.sleep(0.1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            boot = await session.call_tool("world.bootstrap", arguments={})
            self.assertFalse(boot.is_error, boot.structured_content)

    async def test_new_core_activity_lifecycle_via_mcp(self):
        async with self.server.session(self.identity) as (session, _):
            start = await session.call_tool(
                "world.start_activity",
                arguments={
                    "activity_id": "job-" + self.role["role_id"],
                    "kind": "test",
                    "exclusive_group": "work",
                },
            )
            self.assertFalse(start.is_error, start.structured_content)
            claim = await session.call_tool(
                "world.claim_activity",
                arguments={
                    "activity_id": start.structured_content["activity_id"],
                    "runtime_id": "network-agent",
                },
            )
            self.assertFalse(claim.is_error, claim.structured_content)
            renew = await session.call_tool(
                "world.renew_claim",
                arguments={
                    "operation_id": "renew",
                    "activity_claim": claim.structured_content,
                    "lease_seconds": 60,
                },
            )
            self.assertFalse(renew.is_error, renew.structured_content)
            finish = await session.call_tool(
                "world.finish_activity",
                arguments={"operation_id": "finish", "activity_claim": claim.structured_content},
            )
            self.assertFalse(finish.is_error, finish.structured_content)
            self.assertEqual(finish.structured_content["result"]["status"], "completed")
            receipt = await session.call_tool("world.get_receipt", arguments={"operation_id": "finish"})
            self.assertFalse(receipt.is_error)

    async def test_web_validation_does_not_echo_secret_and_rejects_cross_scope_ticket(self):
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                self.server.url + "/v1/join/exchange", json={"ticket": {"secret": "must-not-echo"}}
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("must-not-echo", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            runtime = WorldRuntime(self.server.db)
            ticket = runtime.issue_join_ticket("different", self.role["role_id"])
            response = client.post(self.server.url + "/v1/join/exchange", json={"ticket": ticket["ticket"]})
            self.assertEqual(response.status_code, 403, response.text)
            self.assertIsNone(runtime.get_join_ticket_metadata(ticket["ticket_id"])["used_at"])
            response = client.post(
                self.server.url + "/api/roles",
                auth=("operator", self.server.password),
                headers={"Origin": "https://untrusted.example"},
                json={"display_name": "forged"},
            )
            self.assertEqual(response.status_code, 403)


class ExternalNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_rules_survive_cross_transport_and_new_session(self):
        with LiveServer("examples.encounter_world:WORLD") as server:
            a, identity_a = server.role("Fighter A")
            b, identity_b = server.role("Fighter B")
            with httpx.Client(trust_env=False) as http:
                response = http.post(
                    server.url + "/v1/functions/encounter.start/invoke",
                    headers={"Authorization": "Bearer " + identity_a["token"]},
                    json={
                        "operation_id": "start",
                        "arguments": {"encounter_id": "arena", "members": [a["role_id"], b["role_id"]]},
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
            args = {
                "operation_id": "strike",
                "arguments": {"encounter_id": "arena", "target_role_id": b["role_id"]},
            }
            async with server.session(identity_a) as (session, _):
                hit = await session.call_tool("encounter.strike", arguments=args)
                self.assertFalse(hit.is_error, hit.structured_content)
            async with server.session(identity_a) as (session, _):
                replay = await session.call_tool("encounter.strike", arguments=args)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertEqual(
                    replay.structured_content["random_draws"], hit.structured_content["random_draws"]
                )
                self.assertEqual(replay.structured_content["result"], hit.structured_content["result"])
            async with server.session(identity_b) as (session, _):
                boot = await session.call_tool("world.bootstrap", arguments={})
                self.assertEqual(boot.structured_content["world_entry_state"]["view"]["state"]["turn"], 1)


if __name__ == "__main__":
    unittest.main()
