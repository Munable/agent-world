from __future__ import annotations
import asyncio
import time
import unittest
import httpx
from agent_world import WorldRuntime
from tests.live_server import LiveServer


class ReferenceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_agent_observer_and_timed_timeline(self):
        with LiveServer("tests.fixtures.reference_world:WORLD") as server:
            role, identity = server.role("Player")
            runtime = WorldRuntime(server.db)
            observer = runtime.issue_identity_token("network", role["role_id"], access_mode="observe")
            auth = {"Authorization": "Bearer " + identity["token"]}
            observer_auth = {"Authorization": "Bearer " + observer["token"]}
            with httpx.Client(trust_env=False, timeout=10) as client:
                before = client.post(server.url + "/v1/views/scene/snapshot", headers=observer_auth, json={})
                self.assertEqual(before.status_code, 200, before.text)
                cursor = before.json()["timeline_cursor"]
                args = {"operation_id": "go", "arguments": {"move_id": "move-one", "destination": 3}}
                denied = client.post(server.url + "/v1/functions/character.move/invoke", headers=observer_auth, json=args)
                self.assertEqual(denied.status_code, 403, denied.text)
                action = client.post(server.url + "/v1/functions/character.move/invoke", headers=auth, json=args)
                self.assertEqual(action.status_code, 200, action.text)
            async with server.session(identity) as (session, _):
                replay = await session.call_tool("character.move", arguments=args)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertTrue(replay.structured_content["replayed"])
            async with server.session(observer, terminate=False) as (session, captured):
                tools = await session.list_tools()
                self.assertNotIn("character.move", {t.name for t in tools.tools})
                self.assertIn("world.view_timeline", {t.name for t in tools.tools})
                phases = []
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and "finish" not in phases:
                    page = await session.call_tool("world.view_timeline", arguments={"cursor": cursor})
                    self.assertFalse(page.is_error, page.structured_content)
                    phases.extend(e["cue"]["phase"] for e in page.structured_content["events"])
                    cursor = page.structured_content["cursor"]
                    await asyncio.sleep(0.15)
                self.assertEqual(phases, ["start", "finish"])
                final = await session.call_tool("world.view_snapshot", arguments={"view": "scene"})
                self.assertEqual(final.structured_content["snapshot"]["entities"][role["role_id"]]["position"], 3)
                runtime.revoke_identity_token(observer["token_id"])
                async with httpx.AsyncClient(trust_env=False) as client:
                    denied = await client.post(server.url + "/mcp", headers={
                        **observer_auth, "Mcp-Session-Id": captured["session_id"],
                        "MCP-Protocol-Version": "2025-11-25", "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 88, "method": "tools/list", "params": {}})
                    self.assertEqual(denied.status_code, 401, denied.text)


if __name__ == "__main__": unittest.main()
