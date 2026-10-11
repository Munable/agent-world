"""Stored incremental scheduling and exact local event retention contracts.

These are bounded storage/runner contracts, not real Agent or load evidence.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from agent_world.errors import RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.simulation.programs import compile_program
from agent_world.worlds.shared_grove import activity, events, execution, model as m
from agent_world.worlds.shared_grove.definitions import DEFINITIONS, REGISTRY


class StoredContext:
    """CAS storage without a server, timer, model or active database."""
    now = 1000
    actor_role_id = "actor"

    def __init__(self):
        self.rows = {}
        self.reads = []

    def get_state_record(self, scope, key, default=None):
        self.reads.append((scope, key))
        return deepcopy(self.rows.get((scope, key), {
            "exists": False, "value": default, "version": 0, "updated_at": None}))

    def get_state(self, scope, key, default=None):
        row = self.get_state_record(scope, key)
        return deepcopy(row["value"] if row["exists"] else default)

    def set_state(self, scope, key, value, *, expected_version=None):
        before = self.rows.get((scope, key), {"version": 0})
        if expected_version is not None and expected_version != before["version"]:
            raise StateConflict("changed")
        version = before["version"] + 1
        self.rows[scope, key] = {"exists": True, "value": deepcopy(value),
                               "version": version, "updated_at": self.now}
        return version

    def delete_state(self, scope, key, *, expected_version=None):
        before = self.rows.get((scope, key), {"version": 0})
        if expected_version is not None and expected_version != before["version"]:
            raise StateConflict("changed")
        version = before["version"] + 1
        self.rows[scope, key] = {"exists": False, "value": None,
                               "version": version, "updated_at": self.now}
        return version


def declaration(event=False):
    spec = {"name": "标签", "body_material": "stone", "cost": {"stone": 1},
            "fields": {"seen": {"type": "boolean", "default": False}}}
    if event:
        spec["rules"] = [{"id": "notice", "phase": "after",
            "when": {"op": "eq", "left": {"event": "action"}, "right": "use"},
            "effects": [{"op": "set", "field": "seen", "value": True}]}]
    return compile_program({"key": "tag", "name": "标签", "version": 1,
                            "types": {"tag": spec}}, materials=REGISTRY.materials,
                           body_materials=("wood", "stone", "cloth"), max_custom_materials=4)


def entity(eid, *, x=0, owner=None):
    return {"id": eid, "name": eid, "x": x, "y": 0, "owner": owner,
            "container": None, "physical_parent": None, "lifecycle": "intact",
            "capabilities": [], "components": {}, "matter": [],
            "state": {"open": True, "retired": False, "contents": [], "attachments": [],
                "program": {"definition_ref": "fixed", "type_id": "tag", "fields": {"seen": False},
                    "connections": [], "event_cursor": 0,
                    "process": {"status": "idle", "steps": 0, "last_tick": 0}}}}


def fact(sequence):
    return {"sequence": sequence, "tick": sequence, "x": 0, "y": 0, "action": "use"}


class IncrementalExecutionContracts(unittest.TestCase):
    def setUp(self):
        self.ctx = StoredContext()
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration()})
        self.capacity = patch("agent_world.worlds.shared_grove.creative._check_instance_capacity", return_value={})
        self.capacity.start()
        self.addCleanup(self.capacity.stop)

    def save(self, value):
        self.ctx.set_state(m.ENTITIES, value["id"], value)

    def settle(self, eid):
        execution.begin_step(self.ctx, [eid])
        execution.after_step(self.ctx, [eid])

    def test_sleeping_members_do_not_permanently_occupy_the_active_limit(self):
        for index in range(40):
            current = entity("tag_" + str(index), x=index)
            self.save(current)
            execution.register(self.ctx, current)
            self.settle(current["id"])
        self.assertEqual(activity.meta_get(self.ctx, "creative_instances", []), [])
        self.assertEqual(execution.get_member(self.ctx, "tag_0")["status"], "sleeping")
        self.assertEqual(execution.get_member(self.ctx, "tag_39")["status"], "sleeping")
        self.assertEqual(len([key for scope, key in self.ctx.rows if scope == execution.MEMBERS]), 40)

    def test_runner_bookkeeping_does_not_wake_but_actual_input_does(self):
        current = entity("tag")
        self.save(current)
        execution.register(self.ctx, current)
        self.settle("tag")
        current["state"]["program"]["process"].update(steps=100, last_tick=100, status="waiting")
        current["state"]["program"]["event_cursor"] = 100
        self.save(current)
        execution.reconcile(self.ctx, current)
        self.assertEqual(activity.meta_get(self.ctx, "creative_instances", []), [])
        current["state"]["program"]["fields"]["seen"] = True
        self.save(current)
        execution.reconcile(self.ctx, current)
        self.assertEqual(activity.meta_get(self.ctx, "creative_instances", []), ["tag"])

    def test_retirement_removes_subscriptions_without_deleting_instance(self):
        current = entity("tag")
        self.save(current)
        execution.register(self.ctx, current)
        self.settle("tag")
        current["state"]["retired"] = True
        self.save(current)
        execution.reconcile(self.ctx, current)
        self.assertEqual(execution.subscribers(self.ctx, "tag"), [])
        self.assertEqual(execution.get_member(self.ctx, "tag")["status"], "retired")
        self.assertTrue(self.ctx.get_state_record(m.ENTITIES, "tag")["exists"])

    def test_admission_checks_transitive_not_just_direct_fanout(self):
        for index in range(33):
            eid = "chain_" + str(index)
            execution._set(self.ctx, execution.MEMBERS, eid, {"sources": [eid]})
            if index:
                execution._set(self.ctx, execution.SOURCES, "chain_" + str(index-1), [eid])
        with self.assertRaises(RuleViolation):
            execution._admit_dependency_graph(self.ctx, execution.get_member(self.ctx, "chain_0") | {"entity_id": "chain_0"})

    def test_reservation_uses_actual_consumers_and_source_fact_bound(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        current = entity("tag")
        self.save(current)
        execution.register(self.ctx, current)
        reserved = execution.reservation(self.ctx, ["tag"], max_facts=3)
        self.assertEqual(reserved["active_members"], 1)
        self.assertEqual(reserved["event_consumers"], 1)
        self.assertLess(reserved["changes"], 16)

    def test_reservation_includes_sleeping_local_consumers_outside_frontier(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        for eid in ("one", "two"):
            current = entity(eid)
            self.save(current)
            execution.register(self.ctx, current)
            self.settle(eid)
        reserved = execution.reservation(self.ctx, [], max_facts=2,
                                         consumer_ids=["one", "two"])
        self.assertEqual(reserved["active_members"], 0)
        self.assertEqual(reserved["event_consumers"], 2)
        self.assertEqual(reserved["possible_members"], ["one", "two"])
        self.assertEqual(reserved["entity_writes"], ["one", "two"])
        self.assertGreaterEqual(reserved["changes"], 6)  # two members/queues/new facts

    def test_event_reservation_includes_transitive_wake_without_running_it(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        self.ctx.set_state(DEFINITIONS, "passive", {"definition": declaration()})
        for eid, reference in (("notice", "fixed"), ("follower", "passive")):
            current = entity(eid)
            current["state"]["program"]["definition_ref"] = reference
            self.save(current)
            execution.register(self.ctx, current)
            self.settle(eid)
        member = execution.get_member(self.ctx, "follower")
        member["sources"] = ["follower", "notice"]
        execution._set(self.ctx, execution.MEMBERS, "follower", member)
        execution._set(self.ctx, execution.SOURCES, "notice", ["follower", "notice"])
        before = deepcopy(self.ctx.rows)
        reserved = execution.reservation(self.ctx, [], max_facts=1, consumer_ids=["notice"])
        self.assertEqual(reserved["possible_members"], ["follower", "notice"])
        self.assertEqual(reserved["entity_writes"], ["notice"])
        self.assertEqual(self.ctx.rows, before)

    def test_possible_physical_wake_reads_non_event_watchers_only_from_sources(self):
        for eid in ("source", "follower"):
            current = entity(eid)
            self.save(current)
            execution.register(self.ctx, current)
            self.settle(eid)
        execution._set(self.ctx, execution.SOURCES, "source", ["follower", "source"])
        before = deepcopy(self.ctx.rows)
        self.assertEqual(execution.potential_members(self.ctx, [], source_ids=["source"]),
                         ["follower", "source"])
        self.assertEqual(self.ctx.rows, before)

    def test_event_wake_union_rejects_over32_before_publication(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        observer = entity("notice")
        self.save(observer)
        execution.register(self.ctx, observer)
        self.settle("notice")
        active = []
        for index in range(32):
            current = entity("active_" + str(index), x=index+10)
            self.save(current)
            execution.register(self.ctx, current)
            active.append(current["id"])
        before = deepcopy(self.ctx.rows)
        with self.assertRaisesRegex(RuleViolation, "成员并集超过32"):
            execution.reservation(self.ctx, active, max_facts=1, consumer_ids=["notice"])
        self.assertEqual(self.ctx.rows, before)

    def test_full_queue_reserves_error_save_without_waking_consumer(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        current = entity("notice")
        self.save(current)
        execution.register(self.ctx, current)
        self.settle("notice")
        for sequence in range(1, events.MAX_EVENTS+1):
            events._enqueue(self.ctx, current, fact(sequence), migrating=True)
        reserved = execution.reservation(self.ctx, [], max_facts=1, consumer_ids=["notice"])
        self.assertEqual(reserved["entity_writes"], ["notice"])
        self.assertEqual(reserved["possible_members"], [])

    def test_consumer_error_save_shares_the64_entity_pool_without_extra_physics(self):
        from agent_world.worlds.shared_grove import creative
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        observer = entity("notice")
        self.save(observer)
        execution.register(self.ctx, observer)
        self.settle("notice")
        physical = {"physical_" + str(index): entity("physical_" + str(index)) for index in range(64)}
        contact = {eid: [] for eid in physical}
        contact["physical_0"] = [{"reaction": {"inputs": {"wood": 1}, "outputs": {"wood": 1}}}]
        with patch.object(activity, "physical_closure", return_value=(physical, {"chunk_0_0"})), \
                patch("agent_world.worlds.shared_grove.contact.reserve_forest", return_value=contact), \
                patch("agent_world.worlds.shared_grove.commit.commit_frame") as commit_frame:
            with self.assertRaisesRegex(RuleViolation, "保存并集超过64"):
                creative._instance_capacity_budget(self.ctx, [])
        commit_frame.assert_not_called()
        self.assertEqual(execution.get_member(self.ctx, "notice")["status"], "sleeping")

    def test_potential_watcher_forest_is_closed_before_accepting64_domain(self):
        from agent_world.worlds.shared_grove import creative
        watcher = entity("watcher")
        self.save(watcher)
        execution.register(self.ctx, watcher)
        self.settle("watcher")
        baseline = {"physical_" + str(index): entity("physical_" + str(index)) for index in range(48)}
        execution._set(self.ctx, execution.SOURCES, "physical_0", ["watcher"])
        calls = []

        def forest(ctx, extra):
            calls.append({value["id"] for value in extra})
            if "watcher" in calls[-1]:
                raise RuleViolation("活动物理闭包最多64个对象")  #48 + actual17-object watcher tree
            return baseline, {"chunk_0_0"}

        before = deepcopy(self.ctx.rows)
        with patch.object(activity, "physical_closure", side_effect=forest), \
                patch("agent_world.worlds.shared_grove.commit.commit_frame") as commit_frame:
            with self.assertRaisesRegex(RuleViolation, "物理闭包最多64"):
                creative._instance_capacity_budget(self.ctx, [])
        self.assertEqual(calls, [set(), {"watcher"}])
        self.assertEqual(self.ctx.rows, before)
        commit_frame.assert_not_called()

    def test_legal_pending_prefix_reserves_immutable_bodies_at_their_actual_size(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        current = entity("tag")
        self.save(current)
        execution.register(self.ctx, current)
        for sequence in range(1, 201):
            event = {"sequence": sequence, "tick": sequence, "occurred_at": self.ctx.now,
                     "kind": "action", "action": "move", "entity_id": "",
                     "actor": "role_" + "a" * 32, "x": 0, "y": 0,
                     "mechanical_position": 0, "passable": False}
            events._enqueue(self.ctx, current, event, migrating=True)
        prefix = events._queue(self.ctx, "tag")
        self.assertLess(prefix["payload_bytes"], events.MAX_PENDING_BYTES)
        self.assertLess(sm.encoded_size(prefix), events.MAX_QUEUE_BYTES)
        reserved = execution.reservation(self.ctx, ["tag"], max_facts=1)
        #200 small valid bodies must not reserve164 full-size BEFORE/AFTER
        # records. Keep the distinct shared-row cap and the real pending prefix.
        self.assertLess(reserved["size"], 128 * 1024)
        self.assertLessEqual(reserved["changes"], events.MAX_FACT_WRITES + 16)
        self.assertEqual(events._queue(self.ctx, "tag"), prefix)
        self.assertEqual(events.consumer_status(self.ctx, "tag")["pending_count"], 200)

    def test_reservation_rejects_a_missing_original_pending_fact(self):
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})
        current = entity("tag")
        self.save(current)
        execution.register(self.ctx, current)
        events._enqueue(self.ctx, current, fact(1))
        self.ctx.delete_state(events.FACTS, "1")
        prefix = events._queue(self.ctx, "tag")
        with self.assertRaisesRegex(RuleViolation, "原事实缺失"):
            execution.reservation(self.ctx, ["tag"], max_facts=1)
        self.assertEqual(events._queue(self.ctx, "tag"), prefix)

    def test_local_queue_writer_limit_is_checked_before_commit(self):
        for index in range(33):
            eid = "local_" + str(index)
            execution._set(self.ctx, execution.MEMBERS, eid, {"event_driven": True})
        with self.assertRaisesRegex(RuleViolation, "队列写入预算"):
            execution.reservation(self.ctx, [], max_facts=1,
                                  consumer_ids=["local_" + str(index) for index in range(33)])


class LocalEventRetentionContracts(unittest.TestCase):
    def setUp(self):
        self.ctx = StoredContext()
        self.ctx.set_state(DEFINITIONS, "fixed", {"definition": declaration(event=True)})

    def save(self, value):
        self.ctx.set_state(m.ENTITIES, value["id"], value)

    def test_shared_fact_and_saved_cursor_preserve_newer_input(self):
        a, b = entity("a"), entity("b")
        self.save(a)
        self.save(b)
        for current in (a, b):
            events._enqueue(self.ctx, current, fact(1))
        events._enqueue(self.ctx, a, fact(2))
        self.assertEqual(execution._get(self.ctx, events.FACTS, "1")["references"], 2)
        events.consume(self.ctx, "a", 1)
        self.assertEqual([row["sequence"] for row in events.pending_events(self.ctx, "a")], [2])
        self.assertEqual(execution._get(self.ctx, events.FACTS, "1")["references"], 1)
        events.consume(self.ctx, "b", 1)
        self.assertIsNone(execution._get(self.ctx, events.FACTS, "1"))
        self.assertEqual(events.consumer_status(self.ctx, "a")["pending_count"], 1)

    def test_overflow_preserves_original_prefix_cursor_and_explicit_gap(self):
        current = entity("a")
        self.save(current)
        for sequence in range(1, 257):
            events._enqueue(self.ctx, current, fact(sequence), migrating=True)
        before = deepcopy(events._queue(self.ctx, "a"))
        stopped = events._enqueue(self.ctx, current, fact(257))
        self.assertEqual(stopped["state"]["program"]["process"]["status"], "error")
        after = events._queue(self.ctx, "a")
        self.assertEqual(after["sequences"], before["sequences"])
        self.assertEqual(after["consumed_through"], before["consumed_through"])
        self.assertEqual(after["gap"]["from_sequence"], 257)
        events.consume(self.ctx, "a", 256)
        self.assertEqual(events._queue(self.ctx, "a")["sequences"], before["sequences"])
        self.assertIsNone(execution._get(self.ctx, events.FACTS, "257"))

    def test_private_and_closed_visibility_does_not_consume_pending(self):
        current = entity("a", owner="actor")
        self.save(current)
        events._enqueue(self.ctx, current, fact(1))
        self.assertEqual(events.pending_events(self.ctx, "a"), [])
        self.assertEqual(events.consumer_status(self.ctx, "a")["pending_count"], 1)
        current.update(owner=None, physical_parent={"id": "box", "relation": "contained"})
        box = entity("box")
        box["state"]["open"] = False
        self.save(current)
        self.save(box)
        self.assertEqual(events.pending_events(self.ctx, "a"), [])
        box["state"]["open"] = True
        self.save(box)
        self.assertEqual(events.pending_events(self.ctx, "a"), [fact(1)])

    def test_source_event_reads_only_local_subscribers(self):
        near, far = entity("near"), entity("far", x=100)
        self.save(near)
        self.save(far)
        execution._set(self.ctx, execution.CHUNKS, m.chunk_id(0, 0), ["near"])
        execution._set(self.ctx, execution.CHUNKS, m.chunk_id(6, 0), ["far"])
        self.ctx.reads.clear()
        with patch("agent_world.worlds.shared_grove.execution.wake") as wake:
            events.record_event(self.ctx, action="use", x=0, y=0)
        self.assertEqual(wake.call_args.args[1], ["near"])
        self.assertNotIn((m.ENTITIES, "far"), self.ctx.reads)
        self.assertEqual(events.consumer_status(self.ctx, "near")["pending_count"], 1)
        self.assertEqual(events.consumer_status(self.ctx, "far")["pending_count"], 0)

    def test_error_event_only_member_sleeps_without_discarding_queue(self):
        current = entity("a")
        current["state"]["program"]["process"]["status"] = "error"
        self.save(current)
        member = {"entity_id": "a", "sources": ["a"], "chunks": [], "status": "active",
                  "sleep_safe": True, "continuous": False, "wake_revision": 1}
        execution._set(self.ctx, execution.MEMBERS, "a", member)
        activity.meta_set(self.ctx, "creative_instances", ["a"])
        events._enqueue(self.ctx, current, fact(1))
        execution.begin_step(self.ctx, ["a"])
        execution.after_step(self.ctx, ["a"])
        self.assertEqual(activity.meta_get(self.ctx, "creative_instances", []), [])
        self.assertEqual(execution.get_member(self.ctx, "a")["status"], "error")
        self.assertEqual(events.consumer_status(self.ctx, "a")["pending_count"], 1)


if __name__ == "__main__":
    unittest.main()
