"""Host references preserve public read evidence without judging free-text goals."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock

from agent_world.grove_connector import EXECUTION_CHECKPOINT_SCHEMA
from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_execution import (ExecutionController, ExecutionPorts,
    MAX_EFFECT_READS, MAX_EFFECT_READ_LEDGER_BYTES)


class EffectEvidenceHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.unknown = []
        self.persist = Mock()
        self.controller = ExecutionController("person", max_requests=8, max_world_calls=16, max_seconds=150,
            ports=ExecutionPorts(invoke=AsyncMock(return_value={"result": {"status": "active"}}),
                read=AsyncMock(return_value={"player": {"id": "person"}, "clock": {"sequence": 50}}),
                recover=AsyncMock(), unknown_operations=lambda: self.unknown,
                native_segment=AsyncMock(), settle=AsyncMock(return_value=True), persist=self.persist))
        self.controller.state.update(task_id="task", segment_id="segment", goal="Explore the effect",
            status="running", max_actions=8)
        self.controller.active = True
        self.owner = {"purpose": "execution", "task_id": "task", "segment_id": "segment"}
        self.query = {"entity_id": "public-object", "definition_ref": "immutable-definition",
            "predicates": [{"fact": "field:count", "operator": "eq", "value": 1}]}

    def delivered(self, observation_id="effect-read"):
        return {"observation_id": observation_id, "snapshot_seq": 41,
            "agent_observation": {"scope": "client_presented", "world_instance": "world",
                "role_id": "person", "source_function": "grove.effect_evidence", "source_kind": "query"},
            "result": {"entity_id": "public-object", "definition_ref": "immutable-definition",
                "type_id": "counter", "entity_version": 3, "observed_step": 12,
                "predicates": [{"fact": "field:count", "operator": "eq", "value": 1,
                    "current": {"known": True, "actual": 0, "satisfied": False},
                    "effect_observed": {"status": "unknown", "reason": "No matching transition",
                        "witnesses": []}}],
                "history": {"scope": "last_change_and_bounded_public_outcomes", "complete": False,
                    "coverage": {"automatic_field_change": "last_saved_change_only"}},
                "fact_catalog": {"private_fields": "not supplied"}}}

    def checkpoint(self, **extra):
        return {"task_id": "task", "execution_segment_id": "segment", "status": "completed",
            "summary": "The Agent claims it reached the goal", "next_intent": "",
            "evidence_operation_ids": [], **extra}

    async def test_report_uses_host_read_and_preserves_false_unknown_and_historical_scope(self):
        delivered = self.delivered()
        self.assertTrue(self.controller.preparation_read_delivered("grove.effect_evidence", self.query,
            owner=self.owner, delivered=delivered))
        self.assertTrue(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=delivered))
        delivered["result"]["predicates"][0]["current"]["actual"] = 99
        self.controller.state.update(grant_id="current-grant", grant_operation_id="original-grant")
        self.controller.ports.invoke.return_value = {"result": {"status": "active", "version": 3,
            "grant_id": "current-grant", "goal": self.controller.state["goal"], "max_actions": 8,
            "actions_count": 2, "delegation_active": True}}
        result = await self.controller.checkpoint(self.checkpoint(evidence_observation_ids=["effect-read"]),
            schema=EXECUTION_CHECKPOINT_SCHEMA)
        claim = result["goal_result"]
        self.assertEqual(claim["verification"], {"status": "unverified", "scope": "natural_language_goal"})
        evidence = claim["evidence_reads"][0]
        self.assertEqual(evidence["newer_task_actions_since_read"], 2)
        predicate = evidence["summary"]["predicates"][0]
        self.assertEqual(predicate["current"], {"known": True, "actual": 0, "satisfied": False})
        self.assertEqual(predicate["effect_observed"]["status"], "unknown")
        self.assertFalse(evidence["summary"]["history"]["complete"])
        self.assertIn("fact_catalog", evidence["summary_omitted_sections"])
        self.assertEqual(len(evidence["result_sha256"]), 64)

    async def test_report_rejects_unknown_other_task_or_ordinary_observation_references(self):
        self.controller.state["preparation_count"] = 1
        self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=self.delivered())
        for identity in ("invented", "ordinary-map-read"):
            with self.subTest(identity=identity), self.assertRaises(ConnectorError) as rejected:
                await self.controller.checkpoint(self.checkpoint(evidence_observation_ids=[identity]),
                    schema=EXECUTION_CHECKPOINT_SCHEMA)
            self.assertEqual(rejected.exception.code, "UnconfirmedEvidenceRead")
        self.controller.state["effect_evidence_reads"][0]["task_id"] = "previous-task"
        with self.assertRaises(ConnectorError) as rejected:
            await self.controller.checkpoint(self.checkpoint(evidence_observation_ids=["effect-read"]),
                schema=EXECUTION_CHECKPOINT_SCHEMA)
        self.assertEqual(rejected.exception.code, "UnconfirmedEvidenceRead")
        self.assertTrue(self.controller.active)

    def test_read_isolation_and_closed_or_unknown_operation_boundaries(self):
        for owner in ({**self.owner, "task_id": "old-task"}, {**self.owner, "segment_id": "old-segment"},
                      {**self.owner, "purpose": "explain"}):
            self.assertFalse(self.controller.effect_read_delivered(self.query, owner=owner, delivered=self.delivered()))
        other_role = self.delivered()
        other_role["agent_observation"]["role_id"] = "someone-else"
        self.assertFalse(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=other_role))
        self.unknown.append({"operation_id": "unresolved-original"})
        self.assertFalse(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=self.delivered()))
        self.unknown.clear()
        self.controller.active = False
        self.assertFalse(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=self.delivered()))
        self.assertEqual(self.controller.state["effect_evidence_reads"], [])

    async def test_ordinary_read_only_task_can_complete_without_effect_reference(self):
        self.assertTrue(self.controller.preparation_read_delivered("grove.rule_catalog", {"topic": "materials"},
            owner=self.owner, delivered={"result": {"topic": "materials"}}))
        result = await self.controller.checkpoint(self.checkpoint(), schema=EXECUTION_CHECKPOINT_SCHEMA)
        self.assertEqual(result["goal_result"]["evidence_reads"], [])
        self.assertEqual(result["goal_result"]["verification"]["status"], "unverified")

    def test_ledger_is_bounded_and_does_not_overwrite_an_already_delivered_id(self):
        first = self.delivered()
        self.assertTrue(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=first))
        original = deepcopy(self.controller.state["effect_evidence_reads"][0])
        first["result"]["predicates"][0]["current"]["actual"] = 10
        self.assertFalse(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=first))
        self.assertEqual(self.controller.state["effect_evidence_reads"][0], original)
        for number in range(40):
            self.assertTrue(self.controller.effect_read_delivered(self.query, owner=self.owner,
                delivered=self.delivered("read-" + str(number))))
        reads = self.controller.state["effect_evidence_reads"]
        self.assertLessEqual(len(reads), MAX_EFFECT_READS)
        self.assertLessEqual(len(json.dumps(reads, ensure_ascii=False, indent=2).encode()), MAX_EFFECT_READ_LEDGER_BYTES)
        self.assertEqual(reads[-1]["observation_id"], "read-39")
        self.assertNotIn("summary", self.controller.effect_read_references()[-1])

    def test_large_section_is_omitted_whole_and_persistence_failure_restores_ledger(self):
        large = self.delivered()
        large["result"]["history"]["outcomes"] = [{"description": "large" * 10000}]
        self.assertTrue(self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=large))
        record = self.controller.state["effect_evidence_reads"][0]
        self.assertNotIn("history", record["summary"])
        self.assertIn("history", record["summary_omitted_sections"])
        previous = deepcopy(self.controller.state["effect_evidence_reads"])
        self.persist.side_effect = OSError("unavailable")
        with self.assertRaises(ConnectorError) as failed:
            self.controller.effect_read_delivered(self.query, owner=self.owner, delivered=self.delivered("later-read"))
        self.assertEqual(failed.exception.code, "ExecutionPersistenceFailed")
        self.assertEqual(self.controller.state["effect_evidence_reads"], previous)
