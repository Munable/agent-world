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
        facts = {"map.viewport": {"player": {"id": "actor", "x": 0, "y": 0}, "entities": []},
            "grove.inspect": {"entity": {"id": "actual-object", "name": "Counter", "state": {"wetness": 0}}},
            "grove.rule_catalog": {"topic": "materials", "materials": {"wood": {"combustible": True}}},
            "grove.definition_read": {"definition_ref": "immutable-definition", "definition": {"fields": {"count": 0}}},
            "grove.definition_validate": {"valid": True, "errors": []},
            "grove.fabrication_options": {"definition_ref": "immutable-definition", "sources": []},
            "grove.definition_list": {"definitions": []}, "grove.public_works": {"works": []},
            "grove.content_history": {"contributions": [], "outcome_refs": []}}
        return self.controller.preparation_read_delivered(function,
            {"entity_id": "actual-object"} if arguments is None else arguments,
            owner=self.owner if owner is None else owner,
            delivered={"result": facts.get(function, {}) if result is None else result})

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
        self.assertEqual(len(self.controller.state["preparation_fingerprints"]), len(functions))
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
        self.assertTrue(self.delivered("grove.public_works", query | {"after": "page-five"},
            result={"works": [{"entity_id": "new-object", "fields": {"count": 9}}], "world_tick": 901}))
        self.assertFalse(self.delivered("grove.public_works", query | {"after": "page-six"}, result={"works": []}))
        self.assertEqual(self.controller.state["preparation_count"], 3)

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
        self.assertEqual(self.controller.state["preparation_fingerprints"], {})
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

    def test_action_count_alone_does_not_manufacture_new_facts(self):
        self.assertTrue(self.delivered())
        self.assertFalse(self.delivered())
        self.controller.state["actions_count"] += 1
        self.assertFalse(self.delivered())
        self.assertTrue(self.delivered(result={"entity": {"id": "actual-object", "name": "Counter", "state": {"wetness": 20}}}))
        self.assertFalse(self.delivered(result={"entity": {"id": "actual-object", "name": "Counter", "state": {"wetness": 20}}}))
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
        self.assertEqual(restored.state["preparation_fingerprints"], saved["preparation_fingerprints"])
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
        self.assertEqual(self.controller.state.get("preparation_fingerprints", {}), {})
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

    def test_formal_paused_inspection_watermarks_do_not_bypass_empty_segment_limit(self):
        # This is the formal detailed-inspect shape: combustion and display
        # observations carry the live clock even when the object is paused.
        original = {"entity": {"id": "paused-counter", "version": 40, "name": "Counter",
            "state": {"wetness": 0, "burning": False, "program": {"fields": {"count": 30, "paused": True,
                "tick": 7}, "process": {"status": "idle", "steps": 0, "last_tick": 19}}},
            "matter": [{"material": "wood", "quantity": 12, "slot": "body", "burning": False}],
            "simulation": {"combustion": {"fuel_quantity": 12, "burning_quantity": 0, "rate": 2,
                "cadence_ticks": 3, "clock_sequence": 40, "next_consumption_tick": None,
                "ticks_until_next_consumption": None}},
            "creative": {"world_tick": 40, "fields": {"count": 30, "paused": True, "tick": 7},
                "process": {"status": "idle", "steps": 0, "last_tick": 19, "observed_tick": 40}},
            "presentation": {"badge": {"label": "Count", "value": 30, "known": True},
                "observation": {"tick": 40, "bindings": [
                    {"field": "count", "value": 30, "known": True, "status": "resolved"}]}},
            "execution_domain": {"state": "settled", "last_applied_tick": 40,
                "pending_target_tick": None, "failure": None}}}
        self.assertTrue(self.delivered(result=original))
        for tick in (41, 42):
            updated = deepcopy(original)
            entity = updated["entity"]
            entity["version"] = tick
            entity["creative"]["world_tick"] = tick
            entity["creative"]["process"]["observed_tick"] = tick
            entity["simulation"]["combustion"]["clock_sequence"] = tick
            entity["presentation"]["observation"]["tick"] = tick
            entity["execution_domain"]["last_applied_tick"] = tick
            self.controller.state["segment_id"] = "next-segment-" + str(tick)
            owner = self.owner | {"segment_id": self.controller.state["segment_id"]}
            self.assertFalse(self.delivered(result=updated, owner=owner))
        self.assertEqual(self.controller.state["preparation_count"], 1)

        for change in ("fuel", "quantity", "badge", "binding", "failure", "domain_state", "authored_tick"):
            updated = deepcopy(original)
            entity = updated["entity"]
            if change == "fuel":
                entity["simulation"]["combustion"]["fuel_quantity"] = 11
            elif change == "quantity":
                entity["matter"][0]["quantity"] = 11
            elif change == "badge":
                entity["presentation"]["badge"]["value"] = 31
            elif change == "binding":
                entity["presentation"]["observation"]["bindings"][0]["value"] = 31
            elif change == "failure":
                entity["execution_domain"]["failure"] = {"reason": "capacity", "tick": 42}
            elif change == "domain_state":
                entity["execution_domain"]["state"] = "error"
            else:
                entity["state"]["program"]["fields"]["tick"] = 8
            self.assertTrue(self.delivered(result=updated, owner=owner), change)
            self.assertTrue(self.delivered(result=original, owner=owner), change)
