"""Concise model protocol preserves historical markers and formal schema semantics."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from agent_world.grove_agent_observation import agent_mcp_schema
from agent_world.grove_connector import (
    BLUEPRINT_INSTRUCTIONS, CURRENT_FACTS_INSTRUCTIONS, EXECUTION_INSTRUCTIONS,
    HISTORY_RECOVERY_INSTRUCTIONS, HOST_INSTRUCTIONS, PRIVATE_CONTEXT_INSTRUCTIONS,
    ConnectorManager, GroveSession, _CURRENT_CONTEXT_PREFIXES, _EARLY_CURRENT_FACTS_INSTRUCTIONS,
    _LEGACY_CURRENT_FACTS_INSTRUCTIONS, _LEGACY_PRIVATE_CONTEXT_INSTRUCTIONS,
    _PREVIOUS_CURRENT_FACTS_INSTRUCTIONS,
)
from agent_world.worlds.shared_grove.world import make_world


def validation_semantics(value):
    """Ignore only textual JSON Schema annotations, not a property named description."""
    if isinstance(value, list):
        return [validation_semantics(item) for item in value]
    if isinstance(value, dict):
        return {key: validation_semantics(item) for key, item in value.items()
                if not (key == "description" and isinstance(item, str))}
    return value


class GroveConnectorProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_active_protocol_has_a_bounded_fixed_cost_per_role(self):
        for prompt, maximum in ((HOST_INSTRUCTIONS, 1024), (EXECUTION_INSTRUCTIONS, 1800),
                (BLUEPRINT_INSTRUCTIONS, 800), (HISTORY_RECOVERY_INSTRUCTIONS, 512),
                (CURRENT_FACTS_INSTRUCTIONS, 400), (PRIVATE_CONTEXT_INSTRUCTIONS, 192)):
            with self.subTest(maximum=maximum, prefix=prompt[:40]):
                self.assertLessEqual(len(prompt.encode("utf-8")), maximum)
        # Only the active role is included, not the sum of every configured agent.
        for prompt in (HOST_INSTRUCTIONS, EXECUTION_INSTRUCTIONS):
            self.assertLessEqual(len((prompt + CURRENT_FACTS_INSTRUCTIONS +
                PRIVATE_CONTEXT_INSTRUCTIONS).encode("utf-8")), 2304)

    def test_original_live_session_headers_remain_byte_exact_recognized_markers(self):
        expected = {
            _LEGACY_CURRENT_FACTS_INSTRUCTIONS:
                "01697ed6030ebc8ccabc1ac0795360a481b0927646f3b2fc9afe12098afd3dd6",
            _LEGACY_PRIVATE_CONTEXT_INSTRUCTIONS:
                "a23f0488fe38c5de6a7987c95ccbedb343646a281d6beb7b9757c4e8f8d68a70",
        }
        for header, digest in expected.items():
            self.assertEqual(hashlib.sha256(header.encode("utf-8")).hexdigest(), digest)
            self.assertIn(header, _CURRENT_CONTEXT_PREFIXES)
        for header in (CURRENT_FACTS_INSTRUCTIONS, PRIVATE_CONTEXT_INSTRUCTIONS,
                       _PREVIOUS_CURRENT_FACTS_INSTRUCTIONS, _EARLY_CURRENT_FACTS_INSTRUCTIONS):
            self.assertIn(header, _CURRENT_CONTEXT_PREFIXES)
        self.assertEqual(len(set(_CURRENT_CONTEXT_PREFIXES)), len(_CURRENT_CONTEXT_PREFIXES))

    async def test_short_mcp_descriptions_do_not_change_schema_or_catalog(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-connector-protocol-")
        self.addCleanup(temporary.cleanup)
        manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=Path(temporary.name), binary=Path("unused-opencode"), wrapper=None)
        session = GroveSession(manager, {"role_id": "person", "access_mode": "control"},
            "fixture-bearer", "User", "provider/model")
        definition = make_world()
        descriptors = [{"function_id": spec.name, "access": spec.access, "version": spec.version,
            "input_schema": deepcopy(spec.input_schema), "output_schema": deepcopy(spec.output_schema),
            "description": spec.description} for spec in definition.functions]
        contract_spec = next(spec for spec in definition.functions if spec.name == "grove.agent_contract")
        bootstrap = {"universe": "fixture-connector-protocol-world", "role_id": session.role_id,
            "world": {"id": definition.world_id, "version": definition.version},
            "world_entry_state": {"view": {"player": None,
                "agent_contract_request": {"function_id": contract_spec.name, "arguments": {}}}}}

        async def world_response(method, path, body=None):
            if method == "GET" and path == "/v1/bootstrap":
                return deepcopy(bootstrap)
            if method == "GET" and path.startswith("/v1/discover?"):
                return {"functions": deepcopy(descriptors), "has_more": False, "next_cursor": None}
            if method == "POST" and path == "/v1/functions/grove.agent_contract/invoke":
                self.assertEqual(body, {"arguments": {}})
                return {"ok": True, "read_only": True, "function_id": contract_spec.name,
                    "function_version": contract_spec.version, "snapshot_seq": 0,
                    "result": contract_spec.handler(None, {}).result}
            raise AssertionError(f"Unexpected fixture world request: {method} {path}")

        session.world = AsyncMock(side_effect=world_response)
        await session.agent_client.discover()
        session.catalog = deepcopy(session.agent_client.catalog)
        session._agent_contract_discovered = True
        original = deepcopy(session.catalog)
        tools = {item["name"]: item for item in session.tools()}
        for function in ("grove.inspect", "grove.interact", "grove.fabricate"):
            spec = original[function]
            tool = tools[function.replace(".", "_")]
            actual = deepcopy(tool["inputSchema"])
            expected = agent_mcp_schema(spec["input_schema"], function=function, access=spec["access"],
                guard_paths=session.agent_client.function_contract(function).get("guard_paths"))
            if spec["access"] == "write":
                self.assertIn("execution_segment_id", actual["required"])
                self.assertEqual(validation_semantics(actual["properties"].pop("execution_segment_id")),
                    {"type": "string", "minLength": 1, "maxLength": 100})
                actual["required"].remove("execution_segment_id")
                self.assertEqual(tool["description"], spec["description"] +
                    " Guards bind from observation_id; terms_version remains explicit.")
            else:
                self.assertNotIn("execution_segment_id", actual["properties"])
                self.assertEqual(tool["description"], spec["description"])
            self.assertEqual(validation_semantics(actual), validation_semantics(expected))
        self.assertEqual(session.catalog, original)
        interact = tools["grove_interact"]
        self.assertIn("candidate and combination previews", session.agent_client.contract["binding_semantics"]["candidate_previews"])
        self.assertIn("pour_target_slot", interact["inputSchema"]["properties"]["target_slot"]["description"])
        self.assertNotIn("pour_target_slot", interact["description"])
        fabricate = tools["grove_fabricate"]["inputSchema"]["properties"]["sources"]["items"]
        self.assertEqual(set(fabricate["required"]), {"entity_id", "lot_id", "quantity_u"})
        self.assertEqual(fabricate["properties"]["quantity_u"]["maximum"], 200)
        self.assertFalse(fabricate["additionalProperties"])
