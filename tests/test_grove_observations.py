"""Model observation binding over the real world admission and receipt paths."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agent_world import WorldRuntime
from agent_world.errors import WorldRuntimeError
from agent_world.grove_connector import ConnectorError, ConnectorManager, GroveSession, create_connector_app, _agent_tool_text
from agent_world.worlds.shared_grove import make_world


class GroveObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="grove-observations-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = WorldRuntime(self.root / "world.sqlite3")
        self.runtime.install_world("w", make_world())
        self.role = self.runtime.create_role("A")["role_id"]
        self.runtime.call_function("w", "grove.join", self.role, {"name": "A"}, operation_id="join")
        self.manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=self.root / "connector", binary=Path("unused-opencode"), wrapper=None)
        self.session = GroveSession(self.manager, {"role_id": self.role, "access_mode": "control"},
            "fixture-bearer", "A", "provider/model")
        self.session.catalog = {f["function_id"]: f for f in self.runtime.list_functions("w")}
        self.session.world = self.world
        self.session.status = "thinking"
        self.session.entered = self.session.active_execution = True
        self.session.execution.update(task_id="task", segment_id="segment", status="running", max_actions=8,
            remaining_actions=8, delegation_active=True, context_entity_id=None, goal="Explore", grant_operation_id="grant")
        self.session.current_turn_facts = AsyncMock(side_effect=lambda **kw: self.scene()["result"])
        grant = self.runtime.call_function("w", "grove.control_grant", self.role,
            {"goal": "Explore", "max_actions": 8}, operation_id="grant")["result"]
        self.session.delegation = grant["delegation"]
        self.calls = []
        self.read_seen = asyncio.Event()
        self.lose_before_commit = self.lose_after_commit = False

    def scene(self):
        return self.runtime.query_function("w", "map.viewport", self.role, {})

    def preflight(self):
        return self.session.present_agent_result("map.viewport", self.scene(), preflight=True)["observation_id"]

    async def world(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        try:
            if method == "GET":
                return self.runtime.get_receipt("w", self.role, path.rsplit("/", 1)[-1])
            function = path.split("/")[-2]
            if self.session.catalog[function]["access"] == "read":
                result = self.runtime.query_function("w", function, self.role, body["arguments"])
                self.read_seen.set()
                return result
            if function == "grove.move" and self.lose_before_commit:
                self.lose_before_commit = False
                raise ConnectorError("WorldUnavailable", "Response interrupted", 503)
            result = self.runtime.call_function("w", function, self.role, body["arguments"],
                operation_id=body["operation_id"])
            if function == "grove.move" and self.lose_after_commit:
                self.lose_after_commit = False
                raise ConnectorError("WorldUnavailable", "Response interrupted", 503)
            return result
        except WorldRuntimeError as exc:
            raise ConnectorError(type(exc).__name__, str(exc), 409 if type(exc).__name__ != "ReceiptNotFound" else 404)

    async def write(self, observation_id):
        return await self.session.tool_call("grove_move", {"dx": 1, "dy": 0,
            "execution_segment_id": "segment", "observation_id": observation_id,
            "expected_player_version": 999999})

    def parallel_client_move(self):
        player = self.scene()["result"]["player"]
        return self.runtime.call_function("w", "grove.move", self.role,
            {"dx": 1, "dy": 0, "expected_player_version": player["version"], "delegation": self.session.delegation}, operation_id="parallel-client-move")

    async def test_parallel_read_does_not_rebase_write_from_old_observation(self):
        old = self.preflight()
        original = self.session.agent_observation(old)
        self.parallel_client_move()

        async def write_in_same_batch():
            await self.read_seen.wait()  # Read finished, but its result is not yet delivered to the model.
            with self.assertRaises(ConnectorError) as rejected:
                await self.write(old)
            self.assertEqual(rejected.exception.code, "StateConflict")
        fresh, _ = await asyncio.gather(self.session.tool_call("map_viewport", {}), write_in_same_batch())
        self.assertEqual(self.session.agent_observation(old), original)
        self.assertEqual(self.scene()["result"]["player"]["x"], 1)
        await self.write(fresh["observation_id"])
        self.assertEqual(self.scene()["result"]["player"]["x"], 2)
        self.assertEqual(self.session.execution["actions_count"], 2)

    async def test_only_explicit_base_carries_previous_object_observations(self):
        old = self.preflight()
        first = self.session.present_agent_result("grove.inspect", {"entity": {"id": "selected", "version": 3}}, base_id=old)
        self.session.present_agent_result("grove.inspect", {"entity": {"id": "selected", "version": 4}}, base_id=old)
        # A defaults to this request's preflight, not the latest result cache.
        plain = await self.session.tool_call("grove_rule_catalog", {})
        extended = await self.session.tool_call("grove_rule_catalog", {"base_observation_id": first["observation_id"]})
        self.assertNotIn("selected", self.session.agent_observation(plain["observation_id"])["versions"]["entity"])
        self.assertEqual(self.session.agent_observation(extended["observation_id"])["versions"]["entity"]["selected"], 3)

    async def test_inspect_presents_current_player_without_upgrading_old_observation(self):
        old = self.preflight()
        original = self.session.agent_observation(old)
        entity_id = self.scene()["result"]["inventory"][0]["id"]
        self.parallel_client_move()
        inspected = await self.session.tool_call("grove_inspect", {"entity_id": entity_id})
        self.assertEqual(inspected["result"]["player"], self.scene()["result"]["player"])
        self.assertEqual(self.session.agent_observation(old), original)
        await self.write(inspected["observation_id"])
        self.assertEqual(self.scene()["result"]["player"]["x"], 2)

    async def test_missing_or_restarted_observation_does_not_submit_a_world_write(self):
        before = len(self.calls)
        for invalid in (None, "another-session-observation"):
            with self.assertRaises(ConnectorError) as rejected:
                await self.write(invalid)
            self.assertEqual(rejected.exception.code, "ObservationRequired")
        old = self.preflight()
        restored = GroveSession(self.manager, self.session.identity, "fixture-bearer", "A", "provider/model")
        with self.assertRaises(ConnectorError):
            restored.agent_observation(old)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.scene()["result"]["player"]["x"], 0)

    def test_restored_short_handle_cannot_reuse_an_old_observation(self):
        # Even a repeated random tail cannot alias the previous process's bundle.
        with patch("agent_world.grove_connector.secrets.token_hex", return_value="ab1234ef"):
            old = self.preflight()
            restored = GroveSession(self.manager, self.session.identity, "fixture-bearer", "A", "provider/model")
            fresh = restored.present_agent_result("map.viewport", self.scene())["observation_id"]
        self.assertEqual(old, "obs1-ab1234ef")
        self.assertEqual(fresh, "obs2-ab1234ef")
        with self.assertRaises(ConnectorError):
            restored.agent_observation(old)
        self.assertEqual(restored.agent_observation(fresh)["versions"]["player"][self.role],
                         self.scene()["result"]["player"]["version"])

    async def test_large_claim_delivers_complete_admission_facts_through_mcp(self):
        scene = self.scene()["result"]
        objects = [{"id": f"object-{i}", "version": i+1, "name": "共同物件" * 20, "x": i, "y": 1,
                    "authored_description": "公共来源" * 150,
                    "state": {"historical_detail": "old public state " * 500}, "matter": []}
                   for i in range(32)]
        basis = {"content_id": "content:0:0", "expected_content_version": 7,
                 "anchors": [{"entity_id": f"object-{i}", "observed_anchor_version": i+1,
                     "semantics": {"owner": None, "container": None, "physical_parent": None, "x": i, "y": 1,
                         "material": "wood", "composition": {"material": "wood", "construction": "vessel", "size": 2},
                         "lifecycle": "intact", "destroyed": False, "retired": False}} for i in range(48)],
                 "outcome_ref_ids": [f"outcome-{i}" for i in range(32)]}
        opportunity = {"id": "opportunity:0:0:layout", "version": 8, "owner": self.role,
                       "status": "claimed", "continuation_basis": basis}
        facts = {"opportunity": opportunity, "continuation": basis, "player": scene["player"],
                 "preconditions": {"resources_are_required": True}, "budget_remaining": {"material_units": 3},
                 "resource_stock": {"wood": 2, "stone": 0}, "allowed_tiles": [{"x": 1, "y": 1}],
                 "public_facts": {"entities": objects}, "catalog": {"meaning": "optional catalog"},
                 "public_content": {"id": "content:0:0", "version": 7, "anchors": [
                     {"entity_id": f"object-{i}", "version": i+1, "entity": objects[i%32]} for i in range(48)]}}
        original = {"operation_id": "original-claim", "ok": True, "result": facts,
                    "execution_readback": {**scene, "entities": objects}}
        self.assertGreater(len(json.dumps(original).encode()), 51200)
        presented = self.session.present_agent_result("grove.generation_context", original)
        app = create_connector_app(storage=self.root / "mcp")
        app.state.connector.sessions[self.role] = self.session
        self.session.tool_call = AsyncMock(return_value=presented)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8891") as client:
            response = await client.post("/connector/mcp", headers={"Authorization": "Bearer " + self.session.capability},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "grove_generation_context", "arguments": {}}})
        self.assertEqual(response.status_code, 200)
        text = response.json()["result"]["content"][0]["text"]
        # Native default limits: none of this necessary response is truncated.
        self.assertLess(len(text.encode()), 51200)
        self.assertLess(len(text.splitlines()), 2000)
        self.assertIn(presented["observation_id"], text[:128])
        self.assertIn("original-claim", text[:128])
        decoded = json.loads(text)["result"]
        for field in ("opportunity", "preconditions", "budget_remaining", "resource_stock", "allowed_tiles"):
            self.assertEqual(decoded[field], facts[field])
        self.assertEqual(decoded["continuation"]["field_reference"], "#/result/opportunity/continuation_basis")
        self.assertEqual(self.session.agent_observation(presented["observation_id"])["continuation_bases"][opportunity["id"]], basis)
        self.assertNotIn("entities", decoded["public_facts"])
        self.assertIn("grove_inspect", decoded["public_facts"]["projection"])
        self.assertEqual(original["result"], facts)

    def test_full_definition_discovery_limit_defers_large_type_details(self):
        definitions = [{"definition_ref": "program_" + f"{i:064x}", "author": self.role,
                        "key": "pack_" + str(i), "version": 1,
                        "types": {"type_" + str(j): {"name": "公开名称" * 40,
                            "cost": {"wood": 4, "stone": 4, "cloth": 4}} for j in range(8)}} for i in range(32)]
        original = {"result": {"definitions": definitions, "definition_shape": {"material_imports": "immutable reference"}}}
        self.assertGreater(len(json.dumps(original, ensure_ascii=False).encode()), 51200)
        presented = self.session.present_agent_result("grove.rule_catalog", original)
        text = json.dumps(presented, ensure_ascii=False, indent=1)
        self.assertLess(len(text.encode()), 51200)
        self.assertLess(len(text.splitlines()), 2000)
        actual = presented["result"]
        self.assertEqual(len(actual["definitions"]), 32)
        self.assertEqual(actual["definitions"][31]["type_ids"], list(definitions[31]["types"]))
        self.assertEqual(actual["definition_shape"], original["result"]["definition_shape"])
        self.assertIn("grove_definition_read", actual["definitions_projection"])
        self.assertEqual(original["result"]["definitions"], definitions)

    def test_write_receipt_never_references_full_state_in_a_scene_index(self):
        entity = {"id": "committed-object", "version": 4, "x": 1, "y": 1,
                  "state": {"contract": {"status": "complete", "delivered": 2}},
                  "matter": [{"material_id": "wood", "quantity": 2}]}
        scene = {"player": self.scene()["result"]["player"], "entities": [entity]}
        original = {"operation_id": "delivery", "result": {"entity": entity,
                    "viewport": scene, "public_facts": {"entities": [entity]}},
                    "execution_readback": scene}
        presented = self.session.present_agent_result("grove.rule_action", original)
        actual = presented["result"]
        self.assertEqual(actual["entity"]["state"], entity["state"])
        self.assertEqual(actual["public_facts"]["entities"][0]["state"], entity["state"])
        self.assertNotIn("entity_ref", actual["public_facts"]["entities"][0])
        index = presented["execution_readback"]
        self.assertNotIn("state", index["entities"][0])
        self.assertEqual(index["projection"]["scope"], "authorized_scene_index")
        for key, reference in actual["viewport"].get("field_references", {}).items():
            self.assertEqual(reference, "#/execution_readback/" + key)
            self.assertIn(key, index)
        self.assertEqual(original["result"]["public_facts"]["entities"], [entity])

    def test_selected_action_text_preserves_nested_facts_without_indentation_overflow(self):
        physics = {key: index for index, key in enumerate(
            ("local", "total", "supported", "limit", "overload", "buoyancy", "water", "fuel"))}
        choices = [{"id": "object_"+f"{i:032x}", "version": i+1, "name": "共有材料"*20,
                    "preview": {"physics": {"before": physics, "after": physics,
                        "affected": [{"id": "object_"+f"{i:032x}", "version": i+1,
                                      "before": physics, "after": physics}]}}} for i in range(24)]
        payload = {"observation_id": "obs1-00112233", "operation_id": "confirmed",
            "result": {"player": {"id": self.role, "version": 3},
                "entity": {"id": "selected", "version": 5, "matter": [],
                           "creative": {"material_aliases": {"shared": "original#shared"}}},
                "action_options": [{"action": action, "choices": choices} for action in ("store", "transfer")]}}
        self.assertGreater(len(json.dumps(payload, ensure_ascii=False, indent=1).encode()), 51200)
        text = _agent_tool_text(payload)
        self.assertEqual(json.loads(text), payload)
        self.assertLess(len(text.encode()), 51200)
        self.assertLess(len(text.splitlines()), 2000)
        self.assertLessEqual(max(len(line.encode()) for line in text.splitlines()), 4096)
        self.assertIn(payload["observation_id"], text[:128])
        # Lossless formatting does not promise that every legal large response
        # fits a client's fixed byte limit; it cannot discard action facts.
        larger = deepcopy(payload)
        for option in larger["result"]["action_options"]:
            option["choices"] += deepcopy(choices[:8])
        larger_text = _agent_tool_text(larger)
        self.assertEqual(json.loads(larger_text), larger)
        self.assertGreater(len(larger_text.encode()), 51200)

    def test_guard_bindings_use_subject_identity_and_leave_semantic_values(self):
        observed = self.session.present_agent_result("map.viewport", {"player": {"id": self.role, "version": 11},
            "players": [{"id": "B", "version": 12}], "entities": [{"id": "target", "version": 21},
            {"id": "item", "version": 31}, {"id": "B", "version": 41}]})["observation_id"]
        args = {"action": "offer", "target_id": "target", "item_id": "item", "other_id": "B", "terms_version": 7}
        bound = self.session.bind_agent_versions("grove.rule_action", args, observed)
        self.assertEqual([bound[k] for k in ("expected_player_version", "expected_target_version", "expected_item_version", "expected_other_version")], [11,21,31,12])
        self.assertEqual(bound["terms_version"], 7)
        args["action"] = "connect"
        self.assertEqual(self.session.bind_agent_versions("grove.rule_action", args, observed)["expected_other_version"], 41)
        drop = self.session.bind_agent_versions("grove.interact", {"action": "drop", "item_id": "item"}, observed)
        self.assertEqual((drop["expected_target_version"], drop["expected_item_version"]), (31,31))
        definition = {"definition": {"version": 5, "types": {"custom": {"params": {"expected_item_version": 88}}}}}
        self.assertEqual(self.session.bind_agent_versions("grove.definition_publish", definition, observed), definition)
        tool = next(t for t in self.session.tools() if t["name"] == "grove_move")
        self.assertNotIn("expected_player_version", tool["inputSchema"]["properties"])
        self.assertIn("expected_player_version", self.session.catalog["grove.move"]["input_schema"]["required"])

    def test_continuation_keeps_opportunity_basis_even_after_live_anchor_read(self):
        initial = self.session.present_agent_result("grove.generation_context", {"opportunity": {"id": "opp", "version": 2,
            "continuation_basis": {"content_id": "story", "expected_content_version": 7,
                "anchors": [{"entity_id": "anchor", "observed_anchor_version": 9}]}}})
        newer = self.session.present_agent_result("grove.inspect", {"entity": {"id": "anchor", "version": 99}}, base_id=initial["observation_id"])
        bound = self.session.bind_agent_versions("grove.content_submit", {"opportunity_id": "opp",
            "continuation": {"content_id": "story", "anchor_entity_id": "anchor"}}, newer["observation_id"])
        self.assertEqual(bound["expected_opportunity_version"], 2)
        self.assertEqual((bound["continuation"]["expected_content_version"], bound["continuation"]["observed_anchor_version"]), (7,9))

    async def test_unknown_write_replay_keeps_original_bound_arguments_after_restart(self):
        old = self.preflight()
        self.lose_before_commit = True
        with self.assertRaises(ConnectorError):
            await self.write(old)
        original = deepcopy(self.session.unknown[0]["envelope"])
        self.parallel_client_move()  # Original request is now stale, even though a newer observation exists.
        self.session.present_agent_result("map.viewport", self.scene(), preflight=True)
        restored = GroveSession(self.manager, self.session.identity, "fixture-bearer", "A", "provider/model")
        restored.world = self.world
        await restored.recover_receipts()
        replay = [body for method,path,body in self.calls if method == "POST" and path.endswith("grove.move/invoke")]
        self.assertEqual(replay, [original, original])
        self.assertEqual(self.scene()["result"]["player"]["x"], 1)
        self.assertEqual(restored.execution["receipts"][-1]["status"], "rejected")

    async def test_committed_unknown_action_is_read_back_without_duplicate(self):
        old = self.preflight()
        self.lose_after_commit = True
        with self.assertRaises(ConnectorError):
            await self.write(old)
        await self.session.recover_receipts()
        self.assertEqual(self.scene()["result"]["player"]["x"], 1)
        self.assertEqual(sum(method == "POST" and path.endswith("grove.move/invoke") for method,path,_ in self.calls), 1)
        self.assertEqual(self.session.execution["receipts"][-1]["status"], "confirmed")
