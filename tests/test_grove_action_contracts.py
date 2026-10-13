"""Shared action eligibility, recoverable failures, and read-only build plans."""
from copy import deepcopy
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.runtime_contracts import json_text
from agent_world.worlds.shared_grove import action_contracts as ac
from agent_world.worlds.shared_grove import building, commit, creative, domains, model as m, world


class ReadOnlyContext:
    actor_role_id = "self"

    def __init__(self, entities=()):
        self.entities = {value["id"]: deepcopy(value) for value in entities}
        self.rows = {}

    def get_state_record(self, scope, key):
        value = self.entities.get(key) if scope == m.ENTITIES else self.rows.get((scope, key))
        return {"exists": value is not None, "value": deepcopy(value), "version": 1 if value is not None else 0}

    def get_state(self, scope, key, default=None):
        record = self.get_state_record(scope, key)
        return record["value"] if record["exists"] else default

    def set_state(self, *args, **kwargs):
        raise AssertionError("an action preview attempted to write state")


def entity(identifier="target", *, material="wood", owner=None, container=False, **state):
    return {"id": identifier, "name": identifier, "version": 1, "x": 0, "y": 0, "owner": owner,
        "physical_parent": None, "lifecycle": "intact", "material": material,
        "capabilities": ["transport"]+(["container"] if container else []),
        "components": {"container": {"slots": 2}} if container else {},
        "matter": [{"id": identifier+".body", "material": material, "quantity": 2*building.MATERIAL_UNITS[material],
                   "slot": "body", "role": "structural", "burning": False}],
        "state": {"open": True, "burning": False, "retired": False, "wetness": 0, "water": 0,
                  "contents": [], "attachments": [], "capacity": 2, **state}}


class GroveActionFailureContracts(unittest.TestCase):
    def setUp(self):
        self.player = {"id": "self", "x": 0, "y": 0, "version": 1, "inventory": []}
        self.ctx = ReadOnlyContext()

    def test_container_failures_distinguish_component_lifecycle_lid_and_capacity(self):
        cases = []
        cases.append((entity(), "container_missing", False))
        broken = entity(container=True)
        broken["lifecycle"] = "broken"
        cases.append((broken, "container_broken", "broken"))
        cases.append((entity(container=True, open=False), "container_closed", False))
        cases.append((entity(container=True, contents=["first", "second"]), "container_full", 2))
        for value, code, actual in cases:
            with self.subTest(code=code):
                reason = ac.container_reason(self.ctx, self.player, value, capacity=True)
                self.assertEqual(reason.failure["code"], code)
                self.assertEqual(reason.failure["precondition"]["actual"], actual)
                self.assertTrue(reason.failure["precondition"]["known"])
                self.assertFalse(reason.failure["precondition"]["satisfied"])
                self.assertEqual(reason.failure["subjects"], [{"kind": "entity", "id": "target"}])
                self.assertEqual(reason.failure["read_requests"], [ac.read_entity("target")])
                with self.assertRaises(RuleViolation) as raised:
                    ac.reject(reason)
                self.assertEqual(raised.exception.details["code"], code)
                self.assertEqual(raised.exception.recovery, reason.failure["recovery"]["strategy"])

    def test_private_failure_discloses_no_contents_or_location_and_requests_public_read(self):
        hidden = entity(owner="someone_else", container=True, open=False, contents=["private-child-token"])
        hidden.update(x=743, y=827)
        with self.assertRaises(PermissionDenied) as raised:
            ac.accessible(self.ctx, self.player, hidden)
        details = raised.exception.details
        self.assertEqual(details["code"], "entity_not_accessible")
        self.assertFalse(details["action_precondition"]["known"])
        self.assertIsNone(details["action_precondition"]["actual"])
        self.assertNotIn("private-child-token", json.dumps(details))
        self.assertNotIn("743", json.dumps(details))
        self.assertEqual(details["read_requests"], [{"function_id": "map.viewport", "arguments": {}}])

    def test_store_rejects_closed_target_without_moving_item(self):
        target, item = entity(container=True, open=False), entity("item", owner="self")
        before = deepcopy((target, item))
        with patch.object(commit, "contain") as contain:
            with self.assertRaises(RuleViolation) as raised:
                world._store(self.ctx, self.player, item, target)
        contain.assert_not_called()
        self.assertEqual(raised.exception.details["code"], "container_closed")
        self.assertEqual((target, item), before)

    def test_source_distance_failure_names_the_source_subject_and_current_distance(self):
        item = entity("source")
        item.update(x=2, y=0)
        reason = ac.access_reason(self.ctx, self.player, item, subject="item")
        condition = reason.failure["precondition"]
        self.assertEqual(condition["subject"], "item")
        self.assertEqual(condition["entity_id"], "source")
        self.assertEqual(condition["actual"], 2)
        self.assertEqual(condition["required"], {"maximum": 1})

    def test_rule_and_ordinary_menus_export_plain_json_reasons_with_original_failure(self):
        target = entity()
        target["x"] = 2
        # Recorder actions share the same access precondition as every other
        # compiled rule action, without requiring a one-off authored fixture.
        specification = {"recorder": {}, "actions": []}
        with patch.object(creative, "_type", return_value=({"materials": {}}, specification)), \
             patch.object(creative, "_public_location", return_value=(2, 0)), \
             patch.object(m, "public_entities_near", return_value=[]), \
             patch.object(m, "public_players_near", return_value=[]):
            rules = creative.action_options(self.ctx, self.player, target)
        base = ac.access_reason(self.ctx, self.player, target)
        ordinary = building._option("pickup", base, "Pick up the current item")
        for option in [ordinary, *rules]:
            self.assertIs(type(option["reason"]), str)
            self.assertEqual(option["failure"], base.failure)
            self.assertEqual(option["read_requests"], base.failure["read_requests"])
            self.assertFalse(option["available"])
        # Runtime deliberately rejects str subclasses; ordinary json.dumps
        # alone would not detect this public-output regression.
        json_text([ordinary, *rules])


class GroveBuildPlanContracts(unittest.TestCase):
    def setUp(self):
        materials = [entity("wood", owner="self"), entity("cloth", owner="self", material="cloth")]
        self.ctx = ReadOnlyContext(materials)
        self.player = {"id": "self", "version": 4, "x": 0, "y": 0, "inventory": ["wood", "cloth"]}
        # Owned material locations resolve through the actual owning player,
        # even when the material fixture itself has coordinates.
        self.ctx.rows[(m.PLAYERS, "self")] = deepcopy(self.player)
        self.arguments = {"recipe": "canopy", "x": 0, "y": 0, "expected_player_version": 4}
        self.stack = self.enterContext(ExitStack())
        for owner, name, value in ((m, "get_chunk", {"version": 1}), (m, "tile_at", "grass"),
                                   (m, "public_entities_near", []), (m, "blocking_at", False), (m, "is_safe", False)):
            self.stack.enter_context(patch.object(owner, name, return_value=value))
        self.stack.enter_context(patch.object(domains, "guard_action", return_value=[]))
        self.stack.enter_context(patch.object(domains, "guard_tiles", return_value=None))

    def test_successful_preview_spends_nothing_and_labels_commit_only_checks_unknown(self):
        before = deepcopy(self.ctx.entities)
        with patch.object(commit, "ensure_chunk", side_effect=AssertionError("preview materialized a chunk")):
            plan = building.build_plan(self.ctx, self.player, self.arguments)
            menu = building.build_options(self.ctx, self.player, {"x": 0, "y": 0, "recipe": "canopy"}, world.BUILD_ARGS)
        option = menu["options"][0]
        self.assertTrue(plan["available"])
        self.assertEqual(option["available"], plan["available"])
        self.assertEqual(option["preconditions"], plan["preconditions"])
        self.assertEqual(option["arguments"], self.arguments)
        self.assertFalse(option["preview_scope"]["complete"])
        self.assertEqual(option["preview_scope"]["unknown"], ["final_load", "commit_capacity"])
        self.assertEqual(option["input_schema"]["properties"]["recipe"], {"const": "canopy"})
        self.assertEqual(self.ctx.entities, before)

    def test_blocked_plan_and_commit_share_the_identical_machine_failure(self):
        with patch.object(m, "blocking_at", return_value=True), patch.object(commit, "spawn_entity") as spawn:
            plan = building.build_plan(self.ctx, self.player, self.arguments)
            with self.assertRaises(RuleViolation) as raised:
                building.build(self.ctx, self.player, self.arguments)
        self.assertEqual(plan["failure"]["code"], "build_site_blocked")
        self.assertEqual(raised.exception.details["failure"], plan["failure"])
        spawn.assert_not_called()

    def test_material_failure_reuses_real_quantity_and_does_not_invest_any_lot(self):
        self.ctx.entities["wood"]["matter"][0]["quantity"] = 1
        before = deepcopy(self.ctx.entities)
        plan = building.build_plan(self.ctx, self.player, self.arguments)
        self.assertEqual(plan["failure"]["code"], "material_quantity_insufficient")
        self.assertEqual(plan["failure"]["precondition"]["actual"], 1)
        self.assertEqual(plan["failure"]["precondition"]["required"], 2*building.MATERIAL_UNITS["wood"])
        self.assertEqual(self.ctx.entities, before)

    def test_unmaterialized_resource_layout_is_unknown_and_not_created(self):
        with patch.object(m, "get_chunk", return_value={"version": 0}), patch.object(commit, "ensure_chunk") as ensure:
            plan = building.build_plan(self.ctx, self.player, self.arguments)
        ensure.assert_not_called()
        self.assertIn("generated_resource_layout", plan["preview_scope"]["unknown"])
        occupancy = next(row for row in plan["preconditions"] if row["condition"] == "build_site_unoccupied")
        self.assertFalse(occupancy["known"])
        self.assertFalse(occupancy["satisfied"])
        self.assertIsNone(occupancy["actual"])

    def test_materialized_layout_is_rechecked_before_construction(self):
        resource = entity("node")
        resource["state"]["kind"] = "resource_node"
        with patch.object(m, "get_chunk", side_effect=[{"version": 0}, {"version": 1}]), \
             patch.object(m, "public_entities_near", side_effect=[[], [resource]]), \
             patch.object(commit, "ensure_chunk") as ensure, patch.object(commit, "spawn_entity") as spawn:
            with self.assertRaises(RuleViolation) as raised:
                building.build(self.ctx, self.player, self.arguments)
        ensure.assert_called_once_with(self.ctx, 0, 0)
        spawn.assert_not_called()
        self.assertEqual(raised.exception.details["code"], "build_site_occupied")

    def test_domain_busy_keeps_settlement_and_same_operation_recovery(self):
        busy = domains.DomainBusy("any display wording", retry_after_seconds=0.1,
            details={"settlement": {"state": "calculating", "target_tick": 7, "operation_committed": False}})
        with patch.object(domains, "guard_action", side_effect=busy):
            plan = building.build_plan(self.ctx, self.player, self.arguments)
            with self.assertRaises(domains.DomainBusy) as raised:
                building.build(self.ctx, self.player, self.arguments)
        self.assertEqual(plan["failure"]["code"], "domain_settlement_pending")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.recovery, "wait_then_retry_same_operation")
        self.assertEqual(raised.exception.details["settlement"], busy.details["settlement"])
