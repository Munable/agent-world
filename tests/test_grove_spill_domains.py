"""Coordinate-owned contact pools through the saved product scheduler.

Legacy positive pools are deliberately restored fixtures, not evidence that
ordinary pouring leaves pools behind. All fixtures use isolated test databases.
"""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from agent_world import FunctionOutcome, FunctionSpec, WorldRuntime
from agent_world.errors import StateConflict
from agent_world.simulation import matter as sm
from agent_world.worlds.shared_grove import activity, commit, domains, model as m, processes, make_world
import test_grove_creative as fixtures
from test_grove_incremental_execution import StoredContext


def cloth(entity_id, x, y, water=12):
    lots = [{"id": "body", "material": "cloth", "quantity": 16,
             "slot": "body", "role": "structural", "burning": False}]
    if water:
        lots.append({"id": "wet", "material": "water", "quantity": water,
            "slot": "absorbed", "role": "substance", "bound_to": "body", "burning": False})
    return {"id": entity_id, "name": entity_id, "material": "cloth", "matter": lots,
        "components": {"absorbent": {"capacity": 32, "rate": 2,
            "supports": {"body": {"quantity": 16, "capacity": 32}}}},
        "capabilities": ["transport", "wettable"], "x": x, "y": y}


def fixture(ctx, args):
    mode = args["mode"]
    if mode == "pool_again":
        chunk = m.get_chunk(ctx, 0, 0)
        chunk.setdefault("water_reservoirs", {})["11,11"] = 7
        m.save_chunk(ctx, chunk)
        commit.record_account(ctx, "sources", "water", 7, "seed")
        row = domains.ensure(ctx, spill_points=["11,11"])
        return FunctionOutcome({"domain_id": row["id"], "ordinal": row["ordinal"]})
    commit.spawn_entity(ctx, cloth("spill_left", 3, 3))
    if mode != "legacy_empty":
        commit.spawn_entity(ctx, cloth("spill_right", 11, 11))
    if mode == "legacy_pair":
        commit.spawn_entity(ctx, cloth("spill_right_peer", 11, 11))
    commit.spawn_entity(ctx, {"id": "spill_break", "name": "公开石件", "material": "stone",
        "capabilities": ["transport"], "x": 4, "y": 3})
    if mode.startswith("legacy"):
        # Restore the old positive-pool representation, then let a fresh
        # Runtime perform the real migration and resume hook.
        rows = ctx.list_state(domains.DOMAINS, limit=100)["items"]
        old = {key: sorted({item for row in rows for item in row["value"].get(key, [])})
               for key in domains.ACTIVITY_KEYS}
        chunk = m.get_chunk(ctx, 0, 0)
        chunk.setdefault("water_reservoirs", {})["11,11"] = 7
        m.save_chunk(ctx, chunk)
        commit.record_account(ctx, "sources", "water", 7, "seed")
        old["spill_chunks"], old["spill_points"] = [chunk["id"]], []
        for scope in (domains.DOMAINS, domains.OWNERS, domains.REGIONS,
                      domains.READY, domains.DIRTY, domains.CONTROL):
            for row in ctx.list_state(scope, limit=100)["items"]:
                ctx.delete_state(scope, row["key"])
        ctx._grove_domain = None
        for key, value in old.items():
            ctx.set_state(m.META, key, value)
    return FunctionOutcome({"prepared": True})


def break_fixture(ctx, args):
    # This is the production break reducer used by normal lifecycle paths,
    # invoked directly so the test adds no UI-only destroy action.
    processes.destroy_entity(ctx, m.get_entity(ctx, "spill_break"))
    return FunctionOutcome({"broken": True})


def test_world():
    world = make_world()
    return replace(world, functions=(*world.functions,
        FunctionSpec("grove.spill_fixture", fixture, {"type": "object", "properties": {
            "mode": {"enum": ["normal", "legacy_receiver", "legacy_pair", "legacy_empty", "pool_again"]}},
            "required": ["mode"], "additionalProperties": False}),
        FunctionSpec("grove.break_fixture", break_fixture,
            {"type": "object", "additionalProperties": False})))


class SpillDomainProductContracts(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.CreativeWorldTests(methodName="runTest")
        # Install the declared fixture contract on the first attachment; a
        # same-version second install must never mutate a world contract.
        with patch.object(fixtures, "make_world", return_value=test_world()):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def prepare(self, mode):
        self.h.call("A", "grove.spill_fixture", {"mode": mode})
        if mode.startswith("legacy"):
            self.h.world = WorldRuntime(self.h.db)
            self.h.world.install_world("w", test_world())

    def owner(self, entity_id):
        return self.h.world.get_state("w", domains.OWNERS, entity_id)["value"]["domain_id"]

    def domain(self, domain_id):
        return self.h.world.get_state("w", domains.DOMAINS, domain_id)["value"]

    def pools(self):
        return self.h.world.get_state("w", m.CHUNKS, "chunk_0_0")["value"].get("water_reservoirs", {})

    def water(self, entity_id):
        return sm.quantity(self.h.entity(entity_id), "water", "absorbed")

    def test_normal_same_chunk_wet_objects_advance_without_shared_pool_ownership(self):
        self.prepare("normal")
        self.assertNotEqual(self.owner("spill_left"), self.owner("spill_right"))
        self.h.ticks(1)
        self.assertEqual((self.water("spill_left"), self.water("spill_right")), (10, 10))
        self.assertEqual(self.pools(), {})
        self.assertEqual(self.domain(self.owner("spill_left"))["spill_chunks"], [])

    def test_restored_positive_pool_joins_only_its_actual_coordinate_receiver(self):
        self.prepare("legacy_receiver")
        left, right = self.owner("spill_left"), self.owner("spill_right")
        self.assertNotEqual(left, right)
        self.assertEqual(self.domain(left)["spill_points"], [])
        self.assertEqual(self.domain(right)["spill_points"], ["11,11"])
        self.h.ticks(1)
        self.assertEqual(self.water("spill_left"), 10)
        self.assertEqual(self.water("spill_right"), 17)
        self.assertEqual(self.pools(), {})

    def test_pressure_domain_pool_is_neither_consumed_nor_claimed_by_same_chunk_peer(self):
        self.prepare("legacy_receiver")
        right = self.owner("spill_right")
        before = deepcopy(self.h.entity("spill_right"))
        original = processes.advance_domain

        def capacity_fault(ctx, target_tick, domain):
            if domain["id"] == right:
                raise domains.CapacityPressure("precise test capacity fault")
            return original(ctx, target_tick, domain)

        with patch.object(processes, "advance_domain", side_effect=capacity_fault):
            self.h.ticks(1)
        self.h.ticks(1)
        self.assertEqual(self.domain(right)["status"], "pressure")
        self.assertEqual(self.pools(), {"11,11": 7})
        self.assertEqual(self.h.entity("spill_right"), before)
        self.assertEqual(self.water("spill_left"), 8)
        self.assertEqual(self.domain(self.owner("spill_left"))["spill_chunks"], [])

    def test_actual_same_coordinate_pool_receivers_share_one_finite_budget(self):
        self.prepare("legacy_pair")
        self.assertEqual(self.owner("spill_right"), self.owner("spill_right_peer"))
        self.assertNotEqual(self.owner("spill_left"), self.owner("spill_right"))
        self.h.ticks(1)
        self.assertEqual((self.water("spill_right"), self.water("spill_right_peer")), (17, 10))
        self.assertEqual(self.pools(), {})

    def test_normal_break_does_not_drain_an_unrelated_restored_pool(self):
        self.prepare("legacy_receiver")
        before = deepcopy(self.h.entity("spill_right"))
        self.h.call("A", "grove.break_fixture", {})
        self.assertEqual(self.pools(), {"11,11": 7})
        self.assertEqual(self.h.entity("spill_right"), before)
        self.assertNotEqual(self.h.entity("spill_break")["lifecycle"], "intact")

    def test_restored_pool_without_receiver_runs_and_reuses_its_stable_ordinal(self):
        self.prepare("legacy_empty")
        with self.h.world._conn(readonly=True) as connection:
            rows_before = connection.execute("SELECT COUNT(*) FROM world_state WHERE universe=? AND scope=?",
                ("w", domains.READY)).fetchone()[0]
        self.h.ticks(1)
        self.assertEqual(self.pools(), {})
        self.h.world = WorldRuntime(self.h.db)
        self.h.world.install_world("w", test_world())
        first = self.h.call("A", "grove.spill_fixture", {"mode": "pool_again"})
        self.h.ticks(1)
        second = self.h.call("A", "grove.spill_fixture", {"mode": "pool_again"})
        self.assertEqual(first, second)
        self.h.ticks(1)
        self.assertEqual(self.pools(), {})
        with self.h.world._conn(readonly=True) as connection:
            rows_after = connection.execute("SELECT COUNT(*) FROM world_state WHERE universe=? AND scope=?",
                ("w", domains.READY)).fetchone()[0]
        self.assertEqual(rows_after, rows_before)


class SpillPointStorageContracts(unittest.TestCase):
    def test_pool_commit_preserves_foreign_keys_and_checks_its_real_before_quantity(self):
        ctx = StoredContext()
        chunk = m.get_chunk(ctx, 0, 0)
        chunk["water_reservoirs"] = {"3,3": 2, "11,11": 7}
        m.save_chunk(ctx, chunk)
        commit.commit_results(ctx, {}, set(), spills={"3,3": 1}, spills_before={"3,3": 2},
                              chunks=[chunk["id"]])
        self.assertEqual(m.get_chunk(ctx, 0, 0)["water_reservoirs"], {"3,3": 1, "11,11": 7})
        with self.assertRaises(StateConflict):
            commit.commit_results(ctx, {}, set(), spills={}, spills_before={"3,3": 2}, chunks=[chunk["id"]])
        self.assertEqual(m.get_chunk(ctx, 0, 0)["water_reservoirs"], {"3,3": 1, "11,11": 7})

    def test_empty_source_and_receiver_without_absorption_room_are_not_dependencies(self):
        for held, absorbed, expected_source in ((0, 0, False), (9, 32, False), (9, 0, True)):
            with self.subTest(held=held, absorbed=absorbed):
                ctx = StoredContext()
                ctx.access = "write"
                receiver = m.normalize_entity(cloth("contact", 7, 4, absorbed))
                source = m.normalize_entity({"id": "finite_water", "name": "有限水源", "material": "stone",
                    "capabilities": ["water_source"], "x": 7, "y": 5,
                    "state": {"water_remaining": held, "water_source_capacity": 20}})
                for obj in (receiver, source):
                    ctx.set_state(m.ENTITIES, obj["id"], obj)
                chunk = m.get_chunk(ctx, 0, 0)
                chunk["entity_ids"] = [receiver["id"], source["id"]]
                m.save_chunk(ctx, chunk)
                graph = domains.closure(ctx, [receiver["id"]])
                physical, _ = activity.physical_closure(ctx, [receiver])
                self.assertEqual(source["id"] in graph["objects"], expected_source)
                self.assertEqual(source["id"] in physical, expected_source)


if __name__ == "__main__":
    unittest.main()
