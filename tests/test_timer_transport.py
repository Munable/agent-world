from __future__ import annotations

import asyncio
import time
import unittest

import httpx
from agent_world import WorldRuntime
from tests.live_server import LiveServer


class TimerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def wait_status(self, server, tid, wanted):
        runtime = WorldRuntime(server.db)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if runtime.get_timer("network", tid)["status"] == wanted:
                return runtime
            await asyncio.sleep(0.1)
        self.fail("timer did not reach " + wanted)

    async def test_http_player_schedules_and_mcp_reads_after_disconnect(self):
        with LiveServer("examples.timed_worlds:RPG_WORLD") as server:
            role, identity = server.role("Player")
            headers = {"Authorization": "Bearer " + identity["token"]}
            body = {"operation_id": "cast", "arguments": {"id": "shield", "seconds": 2}}
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.post(server.url + "/v1/functions/effect.start/invoke", headers=headers, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                snapshot = client.post(server.url + "/v1/views/object/snapshot", headers=headers,
                                       json={"arguments": {"id": "shield"}}).json()
            async with server.session(identity) as (session, _):
                tools = await session.list_tools()
                self.assertNotIn("settle", {tool.name for tool in tools.tools})
                replay = await session.call_tool("effect.start", arguments=body)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertTrue(replay.structured_content["replayed"])
            # No Agent remains connected; the host's timer driver acts, not this test's client.
            runtime = await self.wait_status(server, "settle:shield", "completed")
            async with server.session(identity) as (session, _):
                view = await session.call_tool("world.view_snapshot", arguments={"view": "object", "arguments": {"id": "shield"}})
                self.assertFalse(view.is_error, view.structured_content)
                self.assertEqual(view.structured_content["snapshot"]["entities"]["shield"]["status"], "expired")
                synced = await session.call_tool("world.view_sync", arguments={"cursor": snapshot["cursor"]})
                self.assertFalse(synced.is_error, synced.structured_content)
                final = synced.structured_content["delta"]["entities"]["upsert"].get("shield", snapshot["snapshot"]["entities"]["shield"])
                self.assertEqual(final["status"], "expired")
            with runtime._conn(readonly=True) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM world_commits WHERE source='timer'").fetchone()[0], 1)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 2)

    async def test_workflow_deadline_does_not_depend_on_live_owner_credential(self):
        with LiveServer("examples.timed_worlds:WORKFLOW_WORLD") as server:
            owner, identity = server.role("Owner")
            other, other_identity = server.role("Other")
            headers = {"Authorization": "Bearer " + identity["token"]}
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.post(server.url + "/v1/functions/request.start/invoke", headers=headers,
                    json={"operation_id": "create", "arguments": {"id": "review", "seconds": 2}})
                self.assertEqual(response.status_code, 200, response.text)
                denied = client.post(server.url + "/v1/functions/request.cancel/invoke",
                    headers={"Authorization": "Bearer " + other_identity["token"]},
                    json={"operation_id": "bad-cancel", "arguments": {"id": "review"}})
                self.assertEqual(denied.status_code, 403, denied.text)
            runtime = WorldRuntime(server.db)
            runtime.revoke_identity_token(identity["token_id"])
            await self.wait_status(server, "settle:review", "completed")
            self.assertEqual(runtime.get_state("network", "objects", "review")["value"]["status"], "closed")
            receipt = runtime.get_timer_receipt("network", "settle:review")
            self.assertEqual(receipt["scheduled_by"], owner["role_id"])
            with httpx.Client(trust_env=False, timeout=5) as client:
                denied = client.get(server.url + "/v1/bootstrap", headers=headers)
                self.assertEqual(denied.status_code, 401)


if __name__ == "__main__":
    unittest.main()
