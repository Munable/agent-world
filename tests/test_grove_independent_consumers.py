"""Real HTTP/MCP consumers use public contracts without a model or renderer."""
import asyncio
from copy import deepcopy
from pathlib import Path
import unittest

import httpx

from agent_world.grove_agent_client import (
    GroveAgentClient, HttpGroveTransport, JsonOperationJournal, McpGroveTransport,
)
from agent_world.grove_agent_errors import AgentClientError
from tests.live_server import LiveServer


class LostMoveReply:
    def __init__(self, transport):
        self.transport = transport
        self.lost_receipt = None

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def invoke(self, function_id, envelope):
        payload = await self.transport.invoke(function_id, envelope)
        if function_id == "grove.move" and self.lost_receipt is None:
            self.lost_receipt = deepcopy(payload)
            raise AgentClientError("WorldUnavailable", "Response lost after commit.", 503,
                                   recovery="query_receipt_before_retry")
        return payload


class LostConflictReply:
    def __init__(self, transport):
        self.transport = transport

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def invoke(self, function_id, envelope):
        try:
            return await self.transport.invoke(function_id, envelope)
        except AgentClientError as error:
            if error.code == "OperationConflict":
                raise AgentClientError("WorldUnavailable", "Conflict response lost.", 503) from error
            raise


class IndependentGroveConsumersTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_reused_id_refusal_cannot_confirm_the_other_intent(self):
        async with LiveServer("agent_world.worlds.shared_grove:WORLD") as server:
            role, identity, _ = await asyncio.to_thread(server.key_identity)
            journal_path = Path(server.temp.name) / "consumer" / "operations.json"
            port = LostConflictReply(HttpGroveTransport(server.url, identity["token"]))
            client = GroveAgentClient(port, role["role_id"], "network",
                                      journal=JsonOperationJournal(journal_path))
            found = await client.discover()
            await client.write("grove.join", {"name": "Original intent"},
                               observation_id=found["observation_id"], operation_id="occupied-id")
            with self.assertRaises(AgentClientError) as lost:
                await client.write("grove.join", {"name": "Different intent"},
                                   observation_id=found["observation_id"], operation_id="occupied-id")
            self.assertEqual(lost.exception.code, "WorldUnavailable")
            # Stopping new actions must still permit read-only receipt recovery.
            client.before_write = lambda record: False
            recovered = await client.recover()
            self.assertEqual(recovered[0]["status"], "rejected")
            self.assertEqual(recovered[0]["error"]["error"], "OperationConflict")
            self.assertFalse(client.pending_operations)
            current = await client.read("map.viewport")
            self.assertEqual(current["result"]["player"]["name"], "Original intent")

    async def test_headless_entry_http_write_mcp_recovery_and_descriptor_only_human_input(self):
        async with LiveServer("agent_world.worlds.shared_grove:WORLD") as server:
            role, identity, _ = await asyncio.to_thread(server.key_identity)
            role_id = role["role_id"]
            journal_path = Path(server.temp.name) / "consumer" / "operations.json"
            journal = JsonOperationJournal(journal_path)
            port = LostMoveReply(HttpGroveTransport(server.url, identity["token"]))
            agent = GroveAgentClient(port, role_id, "network", journal=journal)
            found = await agent.discover()
            self.assertIn("grove_join", {tool["name"] for tool in agent.tools()})
            self.assertTrue(all("execution_segment_id" not in tool["inputSchema"].get("properties", {})
                                for tool in agent.tools()))
            await agent.write("grove.join", {"name": "Independent visitor"},
                              observation_id=found["observation_id"], operation_id="headless-entry")
            seen = await agent.read("map.viewport")
            # Pick a one-step intent and let the world decide legality. No
            # terrain decoder, private model object or UI rule is consulted.
            for number, (dx, dy) in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1))):
                try:
                    await agent.write("grove.move", {"dx": dx, "dy": dy},
                        observation_id=seen["observation_id"], operation_id=f"headless-step-{number}")
                except AgentClientError as error:
                    if error.code == "WorldUnavailable":
                        break
                    self.assertEqual(error.code, "RuleViolation")
            self.assertIsNotNone(port.lost_receipt)
            original = deepcopy(agent.pending_operations[0])
            with self.assertRaises(AgentClientError) as blocked:
                await agent.write("grove.move", {"dx": 0, "dy": 1},
                    observation_id=seen["observation_id"], operation_id="must-not-send")
            self.assertEqual(blocked.exception.code, "UnknownWrite")

            # A fresh SDK and a different transport keep the original journal;
            # they do not restore old observation handles or invent a new ID.
            async with server.session(identity) as (session, _):
                resumed = GroveAgentClient(McpGroveTransport(session.call_tool), role_id, "network",
                                           journal=JsonOperationJournal(journal_path),
                                           before_write=lambda record: False)
                await resumed.discover()
                recovered = await resumed.recover()
                self.assertEqual(recovered[0]["operation_id"], original["envelope"]["operation_id"])
                self.assertFalse(resumed.pending_operations)
                current = await resumed.read("map.viewport")
                self.assertEqual(current["result"]["player"]["x"], port.lost_receipt["result"]["player"]["x"])
                self.assertEqual(current["result"]["player"]["y"], port.lost_receipt["result"]["player"]["y"])
                replay = await resumed.transport.invoke(original["function_id"], original["envelope"])
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["commit_seq"], port.lost_receipt["commit_seq"])

            # A second renderer consumes the human contract as data. It does
            # not know action names, capability flags or stored program ASTs.
            async with httpx.AsyncClient(base_url=server.url, trust_env=False,
                headers={"Authorization": "Bearer " + identity["token"]}) as http:
                async def query(function, arguments):
                    response = await http.post("/v1/functions/" + function + "/invoke",
                                               json={"arguments": arguments})
                    response.raise_for_status()
                    return response.json()["result"]
                viewport = await query("grove.client_viewport", {})
                self.assertEqual(viewport["context"]["projection_version"], 2)
                point = viewport["player"]
                inspected = await query("grove.client_inspect", {"tile": {"x": point["x"], "y": point["y"]}})
                # Optional inputs may remain unselected. The server's fixed
                # intent and schema define that meaning, not an action name.
                eligible = [option for option in inspected["actions"]
                    if option["available"] and option["expanded"]
                    and not any(control["required"] for control in option["controls"])]
                for item in viewport["inventory"] + viewport["entities"]:
                    if eligible:
                        break
                    detail = await query("grove.client_inspect", {"entity_id": item["id"]})
                    eligible = [option for option in detail["entity"]["actions"]
                        if option["available"] and option["expanded"]
                        and not any(control["required"] for control in option["controls"])]
                self.assertTrue(eligible, "No available server-described intent without extra user input")
                action = eligible[0]
                response = await http.post("/v1/functions/" + action["function_id"] + "/invoke",
                    json={"operation_id": "descriptor-human-intent", "arguments": action["fixed_arguments"]})
                response.raise_for_status()
                self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
