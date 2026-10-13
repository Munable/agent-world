"""Delivered preparation reads are task progress, not committed world actions."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock

from agent_world.grove_execution import ExecutionController, ExecutionPorts


class PreparationProgressTests(unittest.TestCase):
    def setUp(self):
        self.persist = Mock()
        self.unknown = []
        self.controller = self.make_controller()
        self.controller.state.update(task_id="original-task", segment_id="original-segment",
                                     status="running", actions_count=0)
        self.controller.active = True
        self.owner = {"purpose": "execution", "task_id": "original-task",
                      "segment_id": "original-segment"}

    def make_controller(self):
        return ExecutionController("actor", max_requests=8, max_world_calls=16, max_seconds=150,
            ports=ExecutionPorts(invoke=AsyncMock(), read=AsyncMock(), recover=AsyncMock(),
                unknown_operations=lambda: self.unknown, native_segment=AsyncMock(),
                settle=AsyncMock(), persist=self.persist))

    def delivered(self, function="grove.inspect", arguments=None, *, owner=None, result=None):
        pages = {"grove.definition_list": {"definitions": []}, "grove.public_works": {"works": []},
            "grove.content_history": {"contributions": [], "outcome_refs": []}}
        return self.controller.preparation_read_delivered(function,
            {"entity_id": "actual-object"} if arguments is None else arguments,
            owner=self.owner if owner is None else owner,
            delivered={"result": pages.get(function, {}) if result is None else result})

    def test_formal_preparation_reads_add_progress_without_actions_or_receipts(self):
        before_actions = self.controller.state["actions_count"]
        before_receipts = deepcopy(self.controller.state["receipts"])
        functions = ("map.viewport", "grove.inspect", "grove.rule_catalog", "grove.definition_read",
            "grove.definition_list", "grove.definition_validate", "grove.fabrication_options",
            "grove.public_works", "grove.content_history")
        for function in functions:
            with self.subTest(function=function):
                self.assertTrue(self.delivered(function, {}))
        self.assertEqual(self.controller.state["preparation_count"], len(functions))
        self.assertEqual(len(set(self.controller.state["preparation_keys"])), len(functions))
        self.assertEqual(self.controller.state["actions_count"], before_actions)
        self.assertEqual(self.controller.state["receipts"], before_receipts)
        self.assertIsNone(self.controller.state["goal_result"])

    def test_handles_page_size_and_argument_order_do_not_make_new_progress(self):
        first = {"entity_id": "actual-object", "action": "store", "candidate_id": "actual-item",
                 "base_observation_id": "first-observation", "limit": 1}
        original = deepcopy(first)
        self.assertTrue(self.delivered(arguments=first))
        repeated = {"limit": 20, "base_observation_id": "new-observation", "candidate_id": "actual-item",
                    "action": "store", "entity_id": "actual-object"}
        self.assertFalse(self.delivered(arguments=repeated))
        self.assertEqual(first, original)
        self.assertEqual(self.controller.state["preparation_count"], 1)
        self.persist.assert_called_once_with()

    def test_different_action_candidate_and_new_page_records_are_distinct_progress(self):
        for arguments in ({"entity_id": "object", "action": "store", "candidate_id": "one"},
                          {"entity_id": "object", "action": "store", "candidate_id": "two"},
                          {"entity_id": "object", "action": "attach", "candidate_id": "two"}):
            self.assertTrue(self.delivered(arguments=arguments))
        self.assertTrue(self.delivered("grove.public_works", {"author": "actual-author"},
            result={"works": [{"entity_id": "first-object"}]}))
        self.assertTrue(self.delivered("grove.public_works", {"author": "actual-author", "after": "next-page"},
            result={"works": [{"entity_id": "second-object"}]}))
        self.assertEqual(self.controller.state["preparation_count"], 5)

    def test_changed_cursor_offset_tick_and_empty_pages_do_not_manufacture_progress(self):
        query = {"author": "actual-author"}
        self.assertTrue(self.delivered("grove.public_works", query, result={"works": []}))
        for arguments in (query | {"after": "page-two"}, query | {"cursor": "page-three"}, query | {"offset": 90}):
            self.assertFalse(self.delivered("grove.public_works", arguments,
                result={"works": [], "world_tick": 900, "has_more": True, "next_cursor": "another-page"}))
        self.assertTrue(self.delivered("grove.public_works", query | {"after": "page-four"},
            result={"works": [{"entity_id": "new-object", "fields": {"count": 1}}]}))
        self.assertFalse(self.delivered("grove.public_works", query | {"after": "page-five"},
            result={"works": [{"entity_id": "new-object", "fields": {"count": 9}}], "world_tick": 901}))
        self.assertFalse(self.delivered("grove.public_works", query | {"after": "page-six"}, result={"works": []}))
        self.assertEqual(self.controller.state["preparation_count"], 2)

    def test_definition_and_history_pages_only_count_new_stable_records(self):
        for function, arguments, field, identity in (
                ("grove.definition_list", {"author": "person"}, "definitions", "definition_ref"),
                ("grove.content_history", {"content_id": "public-site"}, "outcome_refs", "id")):
            with self.subTest(function=function):
                self.assertTrue(self.delivered(function, arguments, result={field: [{identity: "original-record"}]}))
                self.assertFalse(self.delivered(function, arguments | {"cursor": "next"},
                    result={field: [{identity: "original-record"}]}))
                self.assertFalse(self.delivered(function, arguments | {"cursor": "empty"}, result={field: []}))
                self.assertTrue(self.delivered(function, arguments | {"cursor": "new"},
                    result={field: [{identity: "later-record"}]}))
        self.assertEqual(self.controller.state["preparation_count"], 4)

    def test_persistence_failure_rolls_back_undelivered_progress_and_latches_admission(self):
        self.persist.side_effect = OSError("save unavailable")
        with self.assertRaises(Exception):
            self.delivered()
        self.assertEqual(self.controller.state["preparation_keys"], [])
        self.assertEqual(self.controller.state["preparation_count"], 0)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.state["persistence_failure"]["code"], "ExecutionPersistenceFailed")
        self.persist.side_effect = None
        with self.assertRaises(Exception):
            self.controller.request_started("execution")

    def test_later_segment_cannot_recount_the_same_query(self):
        self.assertTrue(self.delivered())
        self.controller.state["segment_id"] = "next-segment"
        current = self.owner | {"segment_id": "next-segment"}
        self.assertFalse(self.delivered(owner=current))
        self.assertEqual(self.controller.state["preparation_count"], 1)

    def test_confirmed_action_epoch_allows_learning_again(self):
        self.assertTrue(self.delivered())
        self.assertFalse(self.delivered())
        self.controller.state["actions_count"] += 1
        self.assertTrue(self.delivered())
        self.assertFalse(self.delivered())
        self.assertEqual(self.controller.state["preparation_count"], 2)

    def test_restore_retains_all_deduplication_keys(self):
        self.assertTrue(self.delivered())
        saved = self.controller.snapshot()
        restored = self.make_controller()
        restored.restore(saved)
        restored.active = True
        restored.state["status"] = "running"
        self.controller = restored
        self.assertFalse(self.delivered())
        self.assertEqual(restored.state["preparation_keys"], saved["preparation_keys"])
        self.assertEqual(restored.state["preparation_count"], 1)

    def test_other_purposes_and_stale_task_or_segment_do_not_mutate_progress(self):
        before = deepcopy(self.controller.state)
        for owner in (self.owner | {"purpose": "entry"}, self.owner | {"purpose": "explain"},
                      self.owner | {"purpose": "preflight"}, self.owner | {"task_id": "old-task"},
                      self.owner | {"segment_id": "old-segment"}, {}):
            with self.subTest(owner=owner):
                self.assertFalse(self.delivered(owner=owner))
                self.assertEqual(self.controller.state, before)
        self.persist.assert_not_called()

    def test_stopped_reclaimed_sealed_or_unresolved_execution_cannot_gain_progress(self):
        for flag in ("stopped", "reclaiming"):
            setattr(self.controller, flag, True)
            self.assertFalse(self.delivered())
            setattr(self.controller, flag, False)
        self.controller.active = False
        self.assertFalse(self.delivered())
        self.controller.active = True
        self.controller.state["status"] = "paused"
        self.assertFalse(self.delivered())
        self.controller.state["status"] = "running"
        self.controller.state["checkpoint"] = {"status": "continue"}
        self.assertFalse(self.delivered())
        self.controller.state["checkpoint"] = None
        self.unknown.append({"operation_id": "unresolved-original"})
        self.assertFalse(self.delivered())
        self.assertEqual(self.controller.state.get("preparation_count", 0), 0)
        self.assertEqual(self.controller.state.get("preparation_keys", []), [])
        self.persist.assert_not_called()

    def test_control_bootstrap_handbook_native_history_and_writes_are_not_preparation(self):
        for function in ("grove.control_status", "world_bootstrap", "grove.handbook", "native_tool_history",
                         "grove.control_grant", "grove.move", "execution_checkpoint"):
            with self.subTest(function=function):
                self.assertFalse(self.delivered(function, {}))
        self.assertEqual(self.controller.state.get("preparation_count", 0), 0)
        self.persist.assert_not_called()

    def test_non_json_query_cannot_make_a_persisted_progress_key(self):
        before = deepcopy(self.controller.state)
        for arguments in ({"entity_id": object()}, {"quantity": float("nan")}):
            self.assertFalse(self.delivered(arguments=arguments))
            self.assertEqual(self.controller.state, before)
        self.persist.assert_not_called()
