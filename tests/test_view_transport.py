from __future__ import annotations

import unittest
import httpx
from agent_world import WorldRuntime
from tests.live_server import LiveServer


class ViewTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_player_and_mcp_agent_share_rpg_rules_and_views(self):
        with LiveServer("examples.encounter_world:WORLD") as server:
            a, ta = server.role("Player")
            b, tb = server.role("Agent")
            spectator, ts = server.role("Spectator")
            auth = {"Authorization": "Bearer " + ta["token"]}
            selector = {"encounter_id": "battle"}
            with httpx.Client(trust_env=False, timeout=5) as client:
                listing = client.get(server.url + "/v1/views", headers=auth)
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(listing.json()["views"][0]["name"], "scene")
                start = client.post(server.url + "/v1/functions/encounter.start/invoke", headers=auth,
                    json={"operation_id": "start", "arguments": {**selector, "members": [a["role_id"], b["role_id"]]}})
                self.assertEqual(start.status_code, 200, start.text)
                snap = client.post(server.url + "/v1/views/scene/snapshot", headers=auth, json={"arguments": selector})
                self.assertEqual(snap.status_code, 200, snap.text)
                self.assertEqual(snap.headers["cache-control"], "no-store")
                before = snap.json()
                denied = client.post(server.url + "/v1/views/scene/snapshot",
                    headers={"Authorization": "Bearer " + ts["token"]}, json={"arguments": selector})
                self.assertEqual(denied.status_code, 403)
                strike_body = {"operation_id": "player-strike", "arguments": {**selector, "target_role_id": b["role_id"]}}
                strike = client.post(server.url + "/v1/functions/encounter.strike/invoke", headers=auth, json=strike_body)
                self.assertEqual(strike.status_code, 200, strike.text)
                another = client.post(server.url + "/v1/functions/encounter.strike/invoke", headers=auth,
                                      json={**strike_body, "operation_id": "out-of-turn"})
                self.assertEqual(another.status_code, 409)
            async with server.session(ta) as (session, _):
                replay = await session.call_tool("encounter.strike", arguments=strike_body)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertTrue(replay.structured_content["replayed"])
                self.assertEqual(replay.structured_content["commit_seq"], strike.json()["commit_seq"])
                update = await session.call_tool("world.view_sync", arguments={"cursor": before["cursor"]})
                self.assertFalse(update.is_error, update.structured_content)
                delta = update.structured_content
                self.assertEqual(delta["base_cursor"], before["cursor"])
                self.assertFalse(delta["delta"]["entities"]["upsert"][a["role_id"]]["active"])
            async with server.session(tb) as (session, _):
                snap_b = await session.call_tool("world.view_snapshot", arguments={"view": "scene", "arguments": selector})
                self.assertFalse(snap_b.is_error, snap_b.structured_content)
                self.assertEqual(snap_b.structured_content["snapshot"]["meta"]["actions"], ["encounter.strike"])
                acted = await session.call_tool("encounter.strike", arguments={"operation_id": "agent-strike",
                    "arguments": {**selector, "target_role_id": a["role_id"]}})
                self.assertFalse(acted.is_error, acted.structured_content)
            runtime = WorldRuntime(server.db)
            with runtime._conn(readonly=True) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 3)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM world_commits WHERE source='action'").fetchone()[0], 3)

    async def test_non_game_completion_updates_frontend_without_notification(self):
        with LiveServer("examples.workflow_world:WORLD") as server:
            a, ta = server.role("Worker")
            b, tb = server.role("Other")
            auth = {"Authorization": "Bearer " + ta["token"]}
            with httpx.Client(trust_env=False, timeout=5) as client:
                claim = client.post(server.url + "/v1/functions/task.claim/invoke", headers=auth,
                                    json={"operation_id": "claim", "arguments": {"task_id": "one"}})
                self.assertEqual(claim.status_code, 200, claim.text)
                snap = client.post(server.url + "/v1/views/tasks/snapshot", headers=auth, json={"arguments": {"task_id": "one"}}).json()
                event_cursor = client.get(server.url + "/v1/changes", headers=auth).json()["next_cursor"]
            async with server.session(ta) as (session, _):
                completed = await session.call_tool("task.complete", arguments={"operation_id": "complete", "arguments": {"task_id": "one"}})
                self.assertFalse(completed.is_error, completed.structured_content)
                self.assertEqual(completed.structured_content["event_seqs"], [])
            with httpx.Client(trust_env=False, timeout=5) as client:
                update = client.post(server.url + "/v1/views/sync", headers=auth, json={"cursor": snap["cursor"]})
                self.assertEqual(update.status_code, 200, update.text)
                self.assertEqual(update.json()["delta"]["entities"]["upsert"]["one"]["status"], "completed")
                self.assertEqual(client.get(server.url + "/v1/changes", headers=auth).json()["next_cursor"], event_cursor)
                other = client.post(server.url + "/v1/views/tasks/snapshot",
                    headers={"Authorization": "Bearer " + tb["token"]}, json={"arguments": {"task_id": "one"}}).json()
                self.assertEqual(other["snapshot"]["entities"], {})
                forged = client.post(server.url + "/v1/views/tasks/snapshot", headers=auth, json={"role_id": b["role_id"], "arguments": {"task_id": "one"}})
                self.assertEqual(forged.status_code, 403)
            self.assertEqual(len(WorldRuntime(server.db).read_state_history("network")["changes"]), 2)


if __name__ == "__main__":
    unittest.main()
