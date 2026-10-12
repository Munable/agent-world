"""Concise model protocol preserves historical markers and formal schema semantics."""
from copy import deepcopy
import hashlib
import unittest

from agent_world.grove_agent_observation import agent_mcp_schema
from agent_world.grove_connector import (
    BLUEPRINT_INSTRUCTIONS, CURRENT_FACTS_INSTRUCTIONS, EXECUTION_INSTRUCTIONS,
    HISTORY_RECOVERY_INSTRUCTIONS, HOST_INSTRUCTIONS, PRIVATE_CONTEXT_INSTRUCTIONS,
    GroveSession, _CURRENT_CONTEXT_PREFIXES, _EARLY_CURRENT_FACTS_INSTRUCTIONS,
    _LEGACY_CURRENT_FACTS_INSTRUCTIONS, _LEGACY_PRIVATE_CONTEXT_INSTRUCTIONS,
    _PREVIOUS_CURRENT_FACTS_INSTRUCTIONS,
)
from agent_world.worlds.shared_grove.fabrication import ARGS as FABRICATE_ARGS
from agent_world.worlds.shared_grove.world import INSPECT_ARGS, INTERACT_ARGS


def validation_semantics(value):
    """Ignore only textual JSON Schema annotations, not a property named description."""
    if isinstance(value, list):
        return [validation_semantics(item) for item in value]
    if isinstance(value, dict):
        return {key: validation_semantics(item) for key, item in value.items()
                if not (key == "description" and isinstance(item, str))}
    return value


class GroveConnectorProtocolTests(unittest.TestCase):
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

    def test_short_mcp_descriptions_do_not_change_schema_or_catalog(self):
        session = GroveSession.__new__(GroveSession)
        session.catalog = {
            "grove.inspect": {"access": "read", "description": "Original world description",
                "input_schema": deepcopy(INSPECT_ARGS)},
            "grove.interact": {"description": "Original world description",
                "input_schema": deepcopy(INTERACT_ARGS)},
            "grove.fabricate": {"description": "Original world description",
                "input_schema": deepcopy(FABRICATE_ARGS)},
        }
        original = deepcopy(session.catalog)
        tools = {item["name"]: item for item in session.tools()}
        for function, spec in original.items():
            actual = tools[function.replace(".", "_")]["inputSchema"]
            expected = agent_mcp_schema(spec["input_schema"], function=function)
            self.assertEqual(validation_semantics(actual), validation_semantics(expected))
        self.assertEqual(session.catalog, original)
        interact = tools["grove_interact"]
        self.assertIn("matching candidate.preview", interact["description"])
        self.assertIn("pour_target_slot", interact["inputSchema"]["properties"]["target_slot"]["description"])
        self.assertNotIn("pour_target_slot", interact["description"])
        fabricate = tools["grove_fabricate"]["inputSchema"]["properties"]["sources"]["items"]
        self.assertEqual(set(fabricate["required"]), {"entity_id", "lot_id", "quantity_u"})
        self.assertEqual(fabricate["properties"]["quantity_u"]["maximum"], 200)
        self.assertFalse(fabricate["additionalProperties"])
