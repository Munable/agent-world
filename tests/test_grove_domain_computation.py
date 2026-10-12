"""Phase-local caching preserves current candidates and immutable analyses."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_world.errors import CheckpointNotFound, RuleViolation
from agent_world.worlds.shared_grove import creative, definitions, model as m, scheduling
from agent_world.worlds.shared_grove.domains import CapacityPressure
from test_grove_incremental_execution import StoredContext, declaration, entity


class DomainComputationContracts(unittest.TestCase):
    def test_chunk_read_view_tracks_candidates_and_reads_anew_next_batch(self):
        ctx = StoredContext()
        chunk = m.get_chunk(ctx, 0, 0)
        ctx.set_state(m.CHUNKS, chunk["id"], {k: v for k, v in chunk.items() if k != "version"})
        ctx.reads.clear()
        ctx._grove_batch = m.CandidateBatch()
        current = m.get_chunk(ctx, 0, 0)
        self.assertEqual(m.get_chunk(ctx, 0, 0), current)
        self.assertEqual(ctx.reads.count((m.CHUNKS, chunk["id"])), 1)
        current["water_reservoirs"] = {"0,0": 2}
        m.save_chunk(ctx, current)
        self.assertEqual(m.get_chunk(ctx, 0, 0)["water_reservoirs"], {"0,0": 2})
        m.write_candidates(ctx, ctx._grove_batch)
        del ctx._grove_batch
        saved = ctx.get_state(m.CHUNKS, chunk["id"])
        saved["water_reservoirs"] = {"0,0": 1}
        ctx.set_state(m.CHUNKS, chunk["id"], saved)
        ctx._grove_batch = m.CandidateBatch()
        self.assertEqual(m.get_chunk(ctx, 0, 0)["water_reservoirs"], {"0,0": 1})

    def test_optional_history_only_suppresses_known_capacity_pressure(self):
        ctx = StoredContext()
        with patch.object(creative, "_instance_capacity_budget", side_effect=CapacityPressure("full")):
            self.assertEqual(creative._check_instance_capacity(ctx, [], reject=False), {"changes": 0, "bytes": 0})
        with patch.object(creative, "_instance_capacity_budget", side_effect=RuleViolation("invalid facts")):
            with self.assertRaisesRegex(RuleViolation, "invalid facts"):
                creative._check_instance_capacity(ctx, [], reject=False)

    def test_definition_miss_can_be_published_and_existing_reads_are_shared(self):
        ctx = StoredContext()
        self.assertIsNone(definitions.read_definition(ctx, "new"))
        ctx.set_state(definitions.DEFINITIONS, "new", {"definition": declaration()})
        first = definitions.read_definition(ctx, "new")
        second = definitions.read_definition(ctx, "new")
        self.assertEqual(first, second)
        self.assertEqual(ctx.reads.count((definitions.DEFINITIONS, "new")), 2)
        # Another world's same reference can contain a different immutable
        # payload. Pure analysis uses canonical bytes, not a global ref lookup.
        other = StoredContext()
        other.set_state(definitions.DEFINITIONS, "new", {"definition": declaration(event=True)})
        self.assertFalse(definitions.execution_type_plan(ctx, "new", "tag")["event_driven"])
        self.assertTrue(definitions.execution_type_plan(other, "new", "tag")["event_driven"])

    def test_two_candidate_moves_use_current_spatial_before_but_original_cas(self):
        ctx = StoredContext()
        original = m.normalize_entity({"id": "moving", "name": "石件", "material": "stone",
            "capabilities": ["transport"], "x": 0, "y": 0, "state": {}})
        ctx.set_state(m.ENTITIES, "moving", original)
        start_chunk = m.get_chunk(ctx, 0, 0)
        start_chunk["entity_ids"] = ["moving"]
        ctx.set_state(m.CHUNKS, start_chunk["id"], {k: v for k, v in start_chunk.items() if k != "version"})
        ctx._grove_batch = m.CandidateBatch()
        moved = m.get_entity(ctx, "moving")
        original_version = moved["version"]
        moved["x"] = 17
        m.store_entity(ctx, moved)
        moved = m.get_entity(ctx, "moving")
        moved["x"] = 1
        m.store_entity(ctx, moved)
        self.assertEqual(m.get_entity(ctx, "moving")["version"], original_version)
        self.assertEqual(ctx.get_state(m.ENTITIES, "moving")["x"], 0)
        m.write_candidates(ctx, ctx._grove_batch)
        del ctx._grove_batch
        self.assertEqual(ctx.get_state(m.ENTITIES, "moving")["x"], 1)
        self.assertEqual(ctx.get_state_record(m.ENTITIES, "moving")["version"], original_version + 1)
        self.assertEqual(m.get_chunk(ctx, 0, 0)["entity_ids"], ["moving"])
        self.assertNotIn("moving", m.get_chunk(ctx, 1, 0)["entity_ids"])

    def test_port_rebinding_invalidates_phase_capacity_even_with_same_version(self):
        ctx = StoredContext()
        current = entity("reader")
        current["version"] = 4
        ctx._grove_batch = m.CandidateBatch()
        ctx._grove_batch.entities["reader"] = deepcopy(current)
        ctx._grove_batch.dirty_entities.add("reader")
        ctx._grove_step_plan = {"members": ["reader"], "objects": {"reader": deepcopy(current)},
                                "chunks": set(), "changes": 100, "bytes": 10000}
        self.assertIsNotNone(creative._unchanged_step_boundary(ctx, ["reader"]))
        ctx._grove_batch.entities["reader"]["state"]["program"]["port_bindings"] = {"input": "other"}
        self.assertEqual(ctx._grove_batch.entities["reader"]["version"], 4)
        self.assertIsNone(creative._unchanged_step_boundary(ctx, ["reader"]))


class FrameWakeContracts(unittest.TestCase):
    def context(self, checkpoint):
        ctx = SimpleNamespace(now=1000, get_checkpoint=Mock(return_value=checkpoint),
                              schedule_checkpoint=Mock(return_value=8))
        return ctx

    def test_children_share_an_already_available_immediate_frame_generation(self):
        ctx = self.context({"generation": 7, "status": "pending", "due_at": 999, "available_at": 1000})
        self.assertEqual(scheduling._wake_frame(ctx), 7)
        self.assertEqual(scheduling._wake_frame(ctx), 7)
        ctx.schedule_checkpoint.assert_not_called()

    def test_delayed_poll_and_retry_availability_each_require_an_immediate_wake(self):
        for due_at, available_at in ((1000.1, 1000.1), (999, 1001)):
            with self.subTest(due_at=due_at, available_at=available_at):
                ctx = self.context({"generation": 7, "status": "pending", "due_at": due_at,
                                    "available_at": available_at})
                self.assertEqual(scheduling._wake_frame(ctx), 8)
                ctx.schedule_checkpoint.assert_called_once_with(scheduling.FRAME_KEY, "grove.frame", {},
                    due_at=1000, expected_generation=7)

    def test_terminal_or_missing_frame_still_creates_a_successor(self):
        for status in ("completed", "rejected", "failed", "blocked", "cancelled"):
            with self.subTest(status=status):
                ctx = self.context({"generation": 7, "status": status, "due_at": 999, "available_at": 999})
                self.assertEqual(scheduling._wake_frame(ctx), 8)
                ctx.schedule_checkpoint.assert_called_once_with(scheduling.FRAME_KEY, "grove.frame", {},
                    due_at=1000, expected_generation=7)
        ctx = self.context(None)
        ctx.get_checkpoint.side_effect = CheckpointNotFound("missing")
        self.assertEqual(scheduling._wake_frame(ctx), 8)
        ctx.schedule_checkpoint.assert_called_once_with(scheduling.FRAME_KEY, "grove.frame", {},
            due_at=1000, expected_generation=0)


if __name__ == "__main__":
    unittest.main()
