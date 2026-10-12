"""Independent-domain contracts through ordinary product calls.

These checks exercise shared-world execution, not autonomous Agent acceptance
or a claim about production load. Fixtures provide finite original material;
formation, placement, binding and other-player actions use the real API.
"""
from collections import deque
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import RuleViolation
from agent_world.worlds.shared_grove import make_world, model as m
from agent_world.worlds.shared_grove import domains, scheduling
import test_grove_creation as fixtures


def counter_definition(key="domain_counter"):
    return {"key": key, "name": "持续计数木件", "version": 1, "types": {
        "counter": {"name": "独立持续计数器", "body_material": "wood", "cost_u": {"wood": 1},
            "fields": {"count": {"type": "integer", "default": 0, "min": 0, "max": 1000}},
            "exports": ["count"],
            "rules": [{"id": "one_per_step", "phase": "after", "when": True,
                "effects": [{"op": "set", "field": "count", "value": {
                    "op": "add", "left": {"field": "count"}, "right": 1}}]}],
            "actions": [{"id": "reset", "label": "重设计数", "effects": [
                {"op": "set", "field": "count", "value": 0}]}]}}}


class DomainProductContracts(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.CreationTests(methodName="runTest")
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.h = self.c.h

    def field(self, entity_id, name="count"):
        return self.h.entity(entity_id)["state"]["program"]["fields"][name]

    def restart(self):
        self.h.world = WorldRuntime(self.h.db)
        self.h.world.install_world("w", make_world())

    def frame(self):
        row = self.h.world.get_state("w", domains.CONTROL, "frame")
        return row["value"] if row is not None else None

    def begin_frame(self):
        self.h.time.return_value += 1
        for _ in range(40):
            result = self.h.world.run_due_timers("w", limit=1)
            if not result["count"]:
                self.h.time.return_value += 0.01
            work = self.frame()
            if work and work["status"] == "open":
                return deepcopy(work)
        self.fail("A public world frame was not opened by the real worker.")

    def finish_frame(self, frame_id):
        for _ in range(200):
            work = self.frame()
            if work["id"] == frame_id and work["status"] != "open":
                return deepcopy(work)
            result = self.h.world.run_due_timers("w", limit=1)
            if not result["count"]:
                self.h.time.return_value += 0.01
        self.fail("The current world frame did not settle its bounded work.")

    def two_counters(self):
        raw = counter_definition("frame_counters")
        raw["types"]["counter"]["ports"] = {"peer": {"fields": ["physical.contained_water"]}}
        reference = self.c.publish(raw)
        result = []
        for _ in range(2):
            entity_id = self.c.form(reference, "counter")
            self.c.place(entity_id)
            result.append(entity_id)
        return result

    def wait_one_counter(self, counters):
        for _ in range(80):
            counts = [self.field(eid) for eid in counters]
            if sorted(counts) == [0, 1]:
                return counters[counts.index(1)], counters[counts.index(0)]
            self.h.world.run_due_timers("w", limit=1)
        self.fail("Independent domains did not expose an atomic partial frame.")

    def system_row_counts(self):
        # Read-only measurement of this test's isolated database includes
        # tombstones: hiding retired rows is not bounded physical storage.
        with self.h.world._conn(readonly=True) as connection:
            state = connection.execute(
                "SELECT COUNT(*) FROM world_state WHERE universe=? AND scope IN (?,?,?,?,?,?)",
                ("w", domains.DOMAINS, domains.OWNERS, domains.REGIONS, domains.READY, domains.DIRTY, domains.CONTROL),
            ).fetchone()[0]
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM world_checkpoints WHERE universe=?", ("w",)
            ).fetchone()[0]
        return state, checkpoints

    def move_without_touching(self, entity_id):
        h = self.h
        scene = h.view()
        player = scene["player"]
        location = h.entity(entity_id)["x"], h.entity(entity_id)["y"]
        tiles = {(chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        blocked = {(item["x"], item["y"]) for item in scene["entities"]
                   if item.get("barrier_active") or "blocking" in item["capabilities"]}
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            destination = player["x"] + dx, player["y"] + dy
            if (destination != location and destination not in blocked
                    and tiles.get(destination) in {"grass", "dirt"}
                    and max(abs(destination[0] - location[0]), abs(destination[1] - location[1])) <= 8):
                return h.call("A", "grove.move", {"dx": dx, "dy": dy,
                    "expected_player_version": player["version"]})
        self.fail("No legal nearby ordinary movement was available.")

    def move_to(self, destination):
        scene = self.h.view()
        player = scene["player"]
        start = player["x"], player["y"]
        tiles = {(chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        blocked = {(item["x"], item["y"]) for item in scene["entities"]
                   if item.get("barrier_active") or "blocking" in item["capabilities"]}
        queue, previous = deque([start]), {start: None}
        while queue and destination not in previous:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                point = x + dx, y + dy
                if point in previous or point in blocked or tiles.get(point, "rock") == "rock":
                    continue
                previous[point] = (x, y)
                queue.append(point)
        self.assertIn(destination, previous)
        route, point = [], destination
        while point != start:
            route.append(point)
            point = previous[point]
        for point in reversed(route):
            player = self.h.view()["player"]
            self.h.call("A", "grove.move", {"dx": point[0] - player["x"], "dy": point[1] - player["y"],
                        "expected_player_version": player["version"]})

    def test_event_created_after_frame_cutoff_is_consumed_in_the_next_frame_once(self):
        from agent_world.worlds.shared_grove import events
        raw = counter_definition("cutoff_observer")
        kind = raw["types"]["counter"]
        kind["fields"]["moves"] = {"type": "integer", "default": 0, "min": 0, "max": 1000}
        kind["recorder"] = {"radius": 8, "window_ticks": 50}
        kind["rules"].append({"id": "observe_move", "phase": "after",
            "when": {"op": "eq", "left": {"event": "action"}, "right": "move"},
            "effects": [{"op": "set", "field": "moves", "value": {
                "op": "add", "left": {"field": "moves"}, "right": 1}}]})
        reference = self.c.publish(raw)
        observer = self.c.form(reference, "counter")
        self.c.place(observer)
        self.move_without_touching(observer)
        self.h.ticks(1)
        before_moves = self.field(observer, "moves")
        before_count = self.field(observer)
        work = self.begin_frame()
        self.move_without_touching(observer)
        sequence = self.h.world.get_state("w", m.META, "creative_events")["value"]["sequence"]
        self.assertGreater(sequence, work["event_cutoff"])
        queued = self.h.world.get_state("w", events.PENDING, observer)["value"]
        self.assertIn(sequence, queued["sequences"])
        self.finish_frame(work["id"])
        self.assertEqual(self.field(observer), before_count + 1)
        self.assertEqual(self.field(observer, "moves"), before_moves)
        self.h.ticks(1)
        self.assertEqual(self.field(observer, "moves"), before_moves + 1)
        self.h.ticks(1)
        self.assertEqual(self.field(observer, "moves"), before_moves + 1)

    def test_partial_frame_restart_preserves_completed_domain_and_finishes_the_other_once(self):
        counters = self.two_counters()
        work = self.begin_frame()
        first, second = self.wait_one_counter(counters)
        saved = deepcopy(self.h.entity(first))
        self.assertEqual(self.frame()["id"], work["id"])
        self.assertEqual(self.frame()["status"], "open")
        self.h.time.return_value += 3600
        resumed_at = self.h.time.return_value
        self.restart()
        self.assertEqual(self.h.entity(first), saved)
        self.assertEqual(self.frame()["id"], work["id"])
        resumed = self.frame()
        self.assertEqual(resumed["opened_at"], work["opened_at"])
        self.assertEqual(resumed["cadence_anchor_at"], resumed_at)
        self.assertEqual(resumed["event_cutoff"], work["event_cutoff"])
        closed = self.finish_frame(work["id"])
        self.assertEqual(self.h.world.get_checkpoint("w", scheduling.FRAME_KEY)["due_at"],
                         max(closed["closed_at"], resumed_at+1))
        self.assertEqual(self.field(first), 1)
        self.assertEqual(self.field(second), 1)
        self.h.ticks(1)
        self.assertEqual([self.field(eid) for eid in counters], [2, 2])

    def test_revision_upgrade_replaces_only_unfinished_old_version_checkpoints(self):
        previous = replace(make_world(), version=33, state_version=23)
        old = fixtures.CreationTests(methodName="runTest")
        with patch("test_grove_creative.make_world", return_value=previous):
            old.setUp()
        self.addCleanup(old.doCleanups)
        self.c, self.h = old, old.h
        counters = self.two_counters()
        work = self.begin_frame()
        first, second = self.wait_one_counter(counters)
        saved = deepcopy(self.h.entity(first))
        unfinished = self.frame()["pending"]
        self.assertTrue(unfinished)
        prior_generations = {row["domain_id"]: row["generation"] for row in unfinished}

        self.h.time.return_value += 3600
        self.h.world = WorldRuntime(self.h.db)
        self.h.world.install_world("w", make_world())
        resumed = self.frame()
        self.assertEqual(resumed["id"], work["id"])
        self.assertEqual(resumed["target_tick"], work["target_tick"])
        self.assertEqual(resumed["event_cutoff"], work["event_cutoff"])
        self.assertEqual(self.h.entity(first), saved)
        for row in resumed["pending"]:
            domain = self.h.world.get_state("w", domains.DOMAINS, row["domain_id"])
            if domain["value"]["last_tick"] < work["target_tick"]:
                self.assertGreater(row["generation"], prior_generations[row["domain_id"]])
        self.finish_frame(work["id"])
        self.assertEqual([self.field(first), self.field(second)], [1, 1])
        self.h.ticks(1)
        self.assertEqual([self.field(eid) for eid in counters], [2, 2])

    def test_fast_frame_waits_only_the_remaining_opening_period(self):
        self.two_counters()
        work = self.begin_frame()
        closed = self.finish_frame(work["id"])
        due = self.h.world.get_checkpoint("w", scheduling.FRAME_KEY)["due_at"]
        self.assertEqual(due, max(closed["closed_at"], work["opened_at"]+1))
        self.assertLessEqual(due, closed["closed_at"]+1)

    def test_slow_frame_schedules_one_prompt_successor_without_replaying_missed_steps(self):
        counters = self.two_counters()
        work = self.begin_frame()
        self.h.time.return_value += 20
        closed = self.finish_frame(work["id"])
        self.assertEqual(self.h.world.get_checkpoint("w", scheduling.FRAME_KEY)["due_at"], closed["closed_at"])
        self.assertEqual([self.field(eid) for eid in counters], [1, 1])
        result = self.h.world.run_due_timers("w", limit=1)
        self.assertEqual(result["count"], 1)
        successor = self.frame()
        self.assertEqual(successor["target_tick"], work["target_tick"]+1)
        self.assertEqual(successor["opened_at"], self.h.time.return_value)
        self.assertEqual([self.field(eid) for eid in counters], [1, 1])

    def test_cross_domain_binding_waits_for_both_participants_current_frame(self):
        counters = self.two_counters()
        work = self.begin_frame()
        settled, pending = self.wait_one_counter(counters)
        before = {eid: deepcopy(self.h.entity(eid)) for eid in counters}
        with self.assertRaisesRegex(RuleViolation, "结算|轮结束"):
            self.h.action("B", settled, "bind_port", port="peer", other_id=pending)
        self.assertEqual({eid: self.h.entity(eid) for eid in counters}, before)
        self.finish_frame(work["id"])
        self.h.action("B", settled, "bind_port", port="peer", other_id=pending)
        self.h.ticks(1)
        self.assertEqual([self.field(eid) for eid in counters], [2, 2])

    def test_rebinding_existing_content_does_not_accumulate_retired_domain_keys(self):
        counters = self.two_counters()
        self.h.ticks(1)

        def reconnect():
            self.h.action("B", counters[0], "bind_port", port="peer", other_id=counters[1])
            self.h.ticks(1)
            self.h.action("B", counters[0], "unbind_port", port="peer")
            self.h.ticks(1)

        reconnect()  # Admit both split identities and their saved task fences.
        rows_before = self.system_row_counts()
        matter_before = {eid: deepcopy(self.h.entity(eid)["matter"]) for eid in counters}
        for _ in range(8):
            reconnect()
        self.assertEqual(self.system_row_counts(), rows_before)
        self.assertEqual({eid: self.h.entity(eid)["matter"] for eid in counters}, matter_before)
        self.assertEqual([self.field(eid) for eid in counters], [19, 19])

    def test_capacity_failure_is_local_and_actual_split_recovers_without_catch_up(self):
        from agent_world.worlds.shared_grove import creative
        counters = self.two_counters()
        independent = self.c.form(self.h.entity(counters[0])["state"]["program"]["definition_ref"], "counter")
        self.c.place(independent)
        self.h.action("B", counters[0], "bind_port", port="peer", other_id=counters[1])
        before = {eid: deepcopy(self.h.entity(eid)) for eid in counters}
        ordinary_check = creative.check_step_capacity

        def one_member_capacity(ctx):
            # Inject one explicit finite local compute budget, not a success
            # result or world state. The same restriction stays in force while
            # an ordinary unbind really splits the affected graph.
            if len(domains.current(ctx)["creative_instances"]) > 1:
                raise domains.CapacityPressure("测试的本域执行预算只接纳一名参与者。")
            return ordinary_check(ctx)

        with patch.object(creative, "check_step_capacity", side_effect=one_member_capacity):
            work = self.begin_frame()
            closed = self.finish_frame(work["id"])
            self.assertEqual(closed["pressure"], 1)
            self.assertEqual({eid: self.h.entity(eid) for eid in counters}, before)
            self.assertEqual(self.field(independent), 1)
            owner = self.h.world.get_state("w", domains.OWNERS, counters[0])["value"]["domain_id"]
            failure = self.h.world.get_state("w", domains.DOMAINS, owner)["value"]
            self.assertEqual(failure["status"], "pressure")
            self.assertEqual(failure["last_tick"], work["target_tick"])
            self.assertLess(failure["last_applied_tick"], work["target_tick"])
            self.restart()
            self.assertEqual({eid: self.h.entity(eid) for eid in counters}, before)
            self.h.action("B", counters[0], "unbind_port", port="peer")
            self.h.ticks(1)
            self.assertEqual([self.field(eid) for eid in counters], [1, 1])
            self.assertEqual(self.field(independent), 2)
            self.h.ticks(1)
            self.assertEqual([self.field(eid) for eid in counters], [2, 2])
            self.assertEqual(self.field(independent), 3)

    def test_33_independent_continuous_creations_share_a_chunk_and_each_advance_once(self):
        h = self.h
        raw = counter_definition()
        raw["types"]["counter"]["ports"] = {"peer": {"fields": ["physical.contained_water"]}}
        reference = self.c.publish(raw)
        available = h.read("A", "grove.fabrication_options", {
            "definition_ref": reference, "type_id": "counter"})["materials"][0]["available_u"]
        self.assertGreaterEqual(available, 33)
        created = []
        for _ in range(33):
            entity_id = self.c.form(reference, "counter")
            self.c.place(entity_id)
            created.append(entity_id)
        # Shared location is not a physical, material or declared-read edge.
        locations = {(h.entity(eid)["x"], h.entity(eid)["y"]) for eid in created}
        self.assertEqual(len(locations), 1)
        remaining = h.read("A", "grove.fabrication_options", {
            "definition_ref": reference, "type_id": "counter"})["materials"][0]["available_u"]
        self.assertEqual(remaining, available - 33)
        self.assertEqual([self.field(eid) for eid in created], [0] * 33)
        start_tick = h.world.get_state("w", m.META, "clock")["value"]["sequence"]
        h.ticks(1)
        self.assertEqual(h.world.get_state("w", m.META, "clock")["value"]["sequence"], start_tick + 1)
        self.assertEqual([self.field(eid) for eid in created], [1] * 33)
        saved = {eid: deepcopy(h.entity(eid)) for eid in created}
        self.restart()
        self.assertEqual({eid: h.entity(eid) for eid in created}, saved)
        h.ticks(1)
        self.assertEqual([self.field(eid) for eid in created], [2] * 33)
        # Another identity changes one real object through its declared action;
        # subsequent autonomous rules do not replay the prior world frame.
        h.action("B", created[0], "reset")
        self.assertEqual(self.field(created[0]), 0)
        h.ticks(1)
        self.assertEqual(self.field(created[0]), 1)
        self.assertEqual([self.field(eid) for eid in created[1:]], [3] * 32)
        # The same product supports more than 32 independent rules, while a
        # real mutable-read chain still has one finite atomic domain budget.
        for index in range(31):
            h.action("B", created[index], "bind_port", port="peer", other_id=created[index + 1])
        owners = {h.world.get_state("w", domains.OWNERS, eid)["value"]["domain_id"] for eid in created[:32]}
        self.assertEqual(len(owners), 1)
        chain = h.world.get_state("w", domains.DOMAINS, next(iter(owners)))["value"]
        self.assertEqual(len(chain["creative_instances"]), 32)
        snapshots = {eid: deepcopy(h.entity(eid)) for eid in created}
        inventory = deepcopy(h.view("B")["inventory"])
        with self.assertRaisesRegex(domains.CapacityPressure, "32|前沿|相互作用域"):
            h.action("B", created[31], "bind_port", port="peer", other_id=created[32])
        self.assertEqual({eid: h.entity(eid) for eid in created}, snapshots)
        self.assertEqual(h.view("B")["inventory"], inventory)
        self.assertNotIn(h.world.get_state("w", domains.OWNERS, created[32])["value"]["domain_id"], owners)
        h.ticks(1)
        self.assertEqual(self.field(created[0]), 2)
        self.assertEqual([self.field(eid) for eid in created[1:]], [4] * 32)

    def test_fire_uses_old_sources_then_next_preparation_admits_the_new_frontier(self):
        from agent_world.simulation import matter as sm
        from agent_world.worlds.shared_grove.definitions import COMBUSTION_CADENCE_TICKS
        h = self.h
        reference = self.c.publish({"key": "frontier_fire", "name": "真实火前沿", "version": 1,
            "types": {"wood": {"name": "干木件", "body_material": "wood", "cost_u": {"wood": 8},
                "components": {"combustion": {"rate": 1, "spread": True}}}}})
        # Stable sort makes newly ignited B follow A in the old dynamic loop;
        # positions are reached and populated solely through ordinary moves.
        source, neighbor, next_neighbor, diagonal = sorted(self.c.form(reference, "wood") for _ in range(4))
        scene = h.view()
        tiles = {(chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        safe = [zone for chunk in scene["chunks"]
                for zone in h.world.get_state("w", m.CHUNKS, chunk["id"])["value"]["safe_zones"]]
        occupied = {(item["x"], item["y"]) for item in scene["entities"]}
        player = scene["player"]
        positions = None
        for x, y in sorted(tiles, key=lambda point: (abs(point[0] - player["x"]) + abs(point[1] - player["y"]), point)):
            picked = [(x, y), (x + 1, y), (x + 2, y), (x - 1, y + 1)]
            if (all(tiles.get(point) in {"grass", "dirt"} for point in picked)
                    and all(max(abs(px - player["x"]), abs(py - player["y"])) <= 7 for px, py in picked)
                    and all(max(abs(px - zone["x"]), abs(py - zone["y"])) > zone["radius"] for px, py in picked for zone in safe)
                    and all(max(abs(px - ox), abs(py - oy)) > 3 for px, py in picked for ox, oy in occupied)):
                positions = picked
                break
        self.assertIsNotNone(positions, "The formal viewport needs one isolated dry fire layout.")
        for eid, point in zip((source, neighbor, next_neighbor, diagonal), positions):
            self.move_to(point)
            self.c.place(eid)
        h.approach("A", source)
        h.interact("A", source, "ignite", item_id=h.item("A", "打火石")["id"])
        owner = h.world.get_state("w", domains.OWNERS, source)["value"]["domain_id"]
        admitted = h.world.get_state("w", domains.DOMAINS, owner)["value"]["objects"]
        self.assertIn(neighbor, admitted)
        self.assertNotIn(next_neighbor, admitted)
        self.assertNotIn(diagonal, admitted)
        diagonal_before = deepcopy(h.entity(diagonal)["matter"])
        next_before = deepcopy(h.entity(next_neighbor)["matter"])
        sequence = h.world.get_state("w", m.META, "clock")["value"]["sequence"]
        h.ticks(COMBUSTION_CADENCE_TICKS - sequence % COMBUSTION_CADENCE_TICKS)
        self.assertTrue(h.entity(neighbor)["state"]["burning"])
        self.assertEqual(sm.quantity(h.entity(neighbor), "wood"), 8)
        self.assertEqual(h.entity(next_neighbor)["matter"], next_before)
        self.assertFalse(h.entity(next_neighbor)["state"]["burning"])
        self.assertEqual(h.entity(diagonal)["matter"], diagonal_before)
        h.ticks(1)  # No combustion cadence; only the next-frame graph preparation.
        owner = h.world.get_state("w", domains.OWNERS, source)["value"]["domain_id"]
        admitted = h.world.get_state("w", domains.DOMAINS, owner)["value"]["objects"]
        self.assertIn(next_neighbor, admitted)
        self.assertNotIn(diagonal, admitted)
        self.assertEqual(h.entity(next_neighbor)["matter"], next_before)
        h.ticks(COMBUSTION_CADENCE_TICKS - 1)
        self.assertTrue(h.entity(next_neighbor)["state"]["burning"])
        self.assertEqual(sm.quantity(h.entity(next_neighbor), "wood"), 8)
        self.assertEqual(h.entity(diagonal)["matter"], diagonal_before)
        self.assertFalse(h.entity(diagonal)["state"]["burning"])

    def test_active_export_source_and_reader_keep_the_same_frozen_after_input(self):
        h = self.h
        reference = self.c.publish(counter_definition("source_counter"))
        source = self.c.form(reference, "counter")
        self.c.place(source)
        reader_ref = self.c.publish({"key": "domain_reader", "name": "读取真实公开条件", "version": 1,
            "types": {"reader": {"name": "计数读取器", "body_material": "wood", "cost_u": {"wood": 1},
                "fields": {"reading": {"type": "integer", "default": 0, "min": 0, "max": 1000}},
                "ports": {"input": {"source": {"definition_ref": reference, "type_id": "counter"},
                    "fields": ["authored.count"]}},
                "rules": [{"id": "read_previous_input", "phase": "after", "when": True,
                    "effects": [{"op": "set", "field": "reading", "value": {
                        "object": {"port": "input", "field": "authored.count"}}}]}]}}})
        reader = self.c.form(reader_ref, "reader")
        self.c.place(reader)
        h.action("A", reader, "bind_port", port="input", other_id=source)
        for expected in (1, 2):
            h.ticks(1)
            self.assertEqual(self.field(source), expected)
            self.assertEqual(self.field(reader, "reading"), expected - 1)
        # Removing/re-adding the real read edge must not replay source motion or
        # grant access to the source while the port is unbound.
        h.action("B", reader, "unbind_port", port="input")
        h.ticks(1)
        self.assertEqual(self.field(source), 3)
        self.assertEqual(self.field(reader, "reading"), 1)
        h.action("B", reader, "bind_port", port="input", other_id=source)
        h.ticks(1)
        self.assertEqual(self.field(source), 4)
        self.assertEqual(self.field(reader, "reading"), 3)
        saved = {eid: deepcopy(h.entity(eid)) for eid in (source, reader)}
        self.restart()
        self.assertEqual({eid: h.entity(eid) for eid in saved}, saved)
        h.ticks(1)
        self.assertEqual(self.field(source), 5)
        self.assertEqual(self.field(reader, "reading"), 4)


if __name__ == "__main__":
    unittest.main()
