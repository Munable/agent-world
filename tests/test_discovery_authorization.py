"""Discovery visibility and execution authorization are independent contracts."""
from __future__ import annotations

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from agent_world import (
    EventSpec, FunctionOutcome, FunctionSpec, StateRule, StreamEvent, StreamSpec,
    TimerSpec, WorldDefinition, WorldRuntime,
)
from agent_world.errors import PermissionDenied, ReceiptNotFound, TimerNotFound
from tests.capability_harness import RuntimeCapabilityHarness
from tests.live_server import LiveServer

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
CASES = (
    ("visible.allowed", True, True),
    ("visible.denied", True, False),
    ("hidden.allowed", False, True),
    ("hidden.denied", False, False),
)
VISIBLE = {name for name, visible, _ in CASES if visible}


def command(ctx, args):
    value = {"function_id": ctx.function_id, "operation_id": ctx.operation_id}
    ctx.set_state("commands", ctx.operation_id, value)
    # This fixture exercises scheduling without allowing the live timer worker
    # to introduce an unrelated commit during the authorization assertions.
    ctx.schedule_timer(ctx.operation_id, "noop", {}, due_at=4102444800)
    return FunctionOutcome(value, (
        EventSpec(ctx.actor_role_id, "command.executed", value),
        StreamEvent("commands", "command.executed", value),
    ))


WORLD = WorldDefinition(
    "discovery-authorization", "Discovery authorization fixture",
    tuple(FunctionSpec(
        name, command, EMPTY,
        visible_to=lambda ctx, value=visible: value,
        authorize=lambda ctx, args, value=authorized: value,
    ) for name, visible, authorized in CASES),
    state_rules=(StateRule("commands", "", {"type": "object"}),),
    streams=(StreamSpec("commands", public=True),),
    timers=(TimerSpec("noop", lambda ctx, args: FunctionOutcome({}), EMPTY),),
)


def database_dump(path):
    with closing(sqlite3.connect(path)) as c:
        return list(c.iterdump())


class EffectAssertions:
    def assert_committed(self, runtime, universe, role, token, function, operation, receipt):
        state = runtime.get_state(universe, "commands", operation)
        self.assertTrue(state["exists"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["value"], {"function_id": function, "operation_id": operation})
        self.assertEqual(runtime.get_receipt(universe, role, operation, identity_token=token), receipt)
        self.assertEqual(runtime.get_timer(universe, operation)["status"], "pending")
        self.assertEqual(len(receipt["event_seqs"]), 1)
        self.assertEqual(len(receipt["stream_event_ids"]), 1)
        events = runtime.read_changes(universe, role, identity_token=token)
        self.assertEqual([e["seq"] for e in events if e["payload"]["operation_id"] == operation],
                         receipt["event_seqs"])
        stream = runtime.read_stream(universe, "commands")
        self.assertEqual([e["event_id"] for e in stream["events"]
                          if e["payload"]["operation_id"] == operation], receipt["stream_event_ids"])

    def assert_denied_without_effects(self, runtime, universe, role, token, operation, before):
        self.assertFalse(runtime.get_state(universe, "commands", operation)["exists"])
        with self.assertRaises(ReceiptNotFound):
            runtime.get_receipt(universe, role, operation, identity_token=token)
        with self.assertRaises(TimerNotFound):
            runtime.get_timer(universe, operation)
        self.assertEqual(database_dump(runtime.db_path), before)


class DiscoveryAuthorizationTests(EffectAssertions, unittest.TestCase):
    def setUp(self):
        root = os.getenv("WORLD_TEST_WORKDIR")
        if root is not None and not Path(root).is_dir():
            raise ValueError("WORLD_TEST_WORKDIR must be an existing directory")
        self.temp = tempfile.TemporaryDirectory(prefix="discovery-authorization-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.harness = RuntimeCapabilityHarness(Path(self.temp.name) / "world.sqlite3", WORLD)
        self.actor = self.harness.enter()

    def assert_case(self, name, visible, authorized):
        h, actor = self.harness, self.actor
        before = database_dump(h.db_path)
        catalog = h.runtime.list_functions(h.universe, actor_role_id=actor.role_id, identity_token=actor.token)
        self.assertEqual({d["function_id"] for d in catalog}, VISIBLE)
        discovered = h.runtime.discover_functions(h.universe, actor_role_id=actor.role_id, identity_token=actor.token)
        self.assertEqual({d["function_id"] for d in discovered["functions"]}, VISIBLE)
        self.assertEqual(name in VISIBLE, visible)
        self.assertEqual(database_dump(h.db_path), before)
        operation = "runtime-" + name
        if authorized:
            receipt = h.call(actor, name, {}, operation)
            self.assertFalse(receipt["replayed"])
            self.assert_committed(h.runtime, h.universe, actor.role_id, actor.token, name, operation, receipt)
        else:
            with self.assertRaises(PermissionDenied):
                h.call(actor, name, {}, operation)
            self.assert_denied_without_effects(h.runtime, h.universe, actor.role_id, actor.token, operation, before)

    def test_discoverable_and_authorized(self):
        self.assert_case("visible.allowed", True, True)

    def test_discoverable_but_execution_denied(self):
        self.assert_case("visible.denied", True, False)

    def test_hidden_but_authorized_direct_call(self):
        self.assert_case("hidden.allowed", False, True)

    def test_hidden_and_execution_denied(self):
        self.assert_case("hidden.denied", False, False)


class DiscoveryAuthorizationTransportTests(EffectAssertions, unittest.IsolatedAsyncioTestCase):
    async def test_http_and_mcp_catalogs_and_first_calls_preserve_all_four_combinations(self):
        async with LiveServer("tests.test_discovery_authorization:WORLD") as server:
            actor, identity, _ = await asyncio.to_thread(server.key_identity)
            token = identity["token"]
            auth = {"Authorization": "Bearer " + token}
            runtime = WorldRuntime(server.db)
            runtime.install_world("network", WORLD)
            async with server.session(identity) as (session, _):
                async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                    before = database_dump(server.db)
                    catalog = await client.get(server.url + "/v1/functions", headers=auth)
                    self.assertEqual(catalog.status_code, 200, catalog.text)
                    self.assertEqual({d["function_id"] for d in catalog.json()["functions"]}, VISIBLE)
                    discovered = await client.get(server.url + "/v1/discover", headers=auth)
                    self.assertEqual(discovered.status_code, 200, discovered.text)
                    self.assertEqual({d["function_id"] for d in discovered.json()["functions"]}, VISIBLE)
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertEqual(names & {name for name, _, _ in CASES}, VISIBLE)
                    self.assertEqual(database_dump(server.db), before)

                    for name, visible, authorized in CASES:
                        self.assertEqual(name in names, visible)
                        for transport in ("http", "mcp"):
                            with self.subTest(function=name, transport=transport):
                                # Each transport gets a FIRST call, including
                                # hidden.allowed; a replay cannot prove authorization.
                                operation = transport + "-" + name
                                body = {"operation_id": operation, "arguments": {}}
                                before = database_dump(server.db)
                                if transport == "http":
                                    response = await client.post(server.url + f"/v1/functions/{name}/invoke",
                                                                 headers=auth, json=body)
                                    self.assertEqual(response.status_code, 200 if authorized else 403, response.text)
                                    result = response.json()
                                else:
                                    response = await session.call_tool(name, arguments=body)
                                    self.assertEqual(response.is_error, not authorized, response.structured_content)
                                    result = response.structured_content
                                if authorized:
                                    self.assertFalse(result["replayed"])
                                    self.assert_committed(runtime, "network", actor["role_id"], token,
                                                          name, operation, result)
                                else:
                                    self.assertEqual(result["error"], "PermissionDenied")
                                    self.assertFalse(result["retryable"])
                                    self.assert_denied_without_effects(runtime, "network", actor["role_id"],
                                                                      token, operation, before)


if __name__ == "__main__":
    unittest.main()
