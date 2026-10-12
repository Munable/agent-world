"""Fixed intent survives autonomous outputs, never unseen protected changes.

Product checks retain original operation IDs and observed guards. They do not
establish model acceptance, multiplayer load, or permissions from a preview.
"""
from collections import deque
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import ReceiptNotFound, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.worlds.shared_grove import make_world, model as m
from agent_world.worlds.shared_grove import domains, execution
from agent_world.worlds.shared_grove.definitions import COMBUSTION_CADENCE_TICKS
import test_grove_domain_contracts as fixtures


class InteractionRevisionGuardContracts(unittest.TestCase):
    def test_external_recorder_tail_is_metadata_but_material_and_field_edits_are_not(self):
        before = {"id": "outside-recorder", "version": 7, "interaction_revision": 4,
            "owner": None, "matter": [{"quantity": 2}],
            "state": {"program": {"fields": {"signal": False},
                "process": {"status": "idle", "last_tick": 5},
                "recorder": {"active": True, "until_tick": 100, "recorded_count": 3}}}}
        recorded = deepcopy(before)
        recorded.update(version=8, interaction_revision=8)
        recorded["state"]["program"]["recorder"]["recorded_count"] = 4
        # A public reaction may append evidence to an observer in another
        # domain without granting that reaction control of the observer.
        active = {"id": "reaction-domain", "objects": ["mixer"]}
        context = SimpleNamespace(_grove_domain_work=True, _grove_batch=SimpleNamespace(
            entity_records={before["id"]: {"exists": True, "value": before}}))
        with patch.object(domains, "installed", return_value=True), \
                patch.object(domains, "current", return_value=active):
            self.assertEqual(domains.sync_entities(context, {before["id"]: recorded}), [active])
            for change in (lambda value: value["matter"][0].update(quantity=1),
                           lambda value: value["state"]["program"]["fields"].update(signal=True),
                           lambda value: value.update(owner="another-player")):
                edited = deepcopy(recorded)
                change(edited)
                with self.assertRaisesRegex(domains.CapacityPressure, "超出已冻结相互作用域"):
                    domains.sync_entities(context, {before["id"]: edited})

    def test_server_guard_and_recorder_bookkeeping_do_not_wake_unchanged_inputs(self):
        source = {"id": "source", "version": 7, "interaction_revision": 4,
            "matter": [{"quantity": 2}], "state": {"program": {
                "fields": {"signal": False}, "process": {"status": "idle", "last_tick": 5},
                "recorder": {"active": True, "recorded_count": 3}}}}
        with patch.object(m, "get_entity", side_effect=lambda *args, **kwargs: deepcopy(source)):
            original = execution._input_fingerprint(SimpleNamespace(), ["source"])
            source.update(version=8, interaction_revision=8)
            source["state"]["program"]["process"]["last_tick"] = 6
            source["state"]["program"]["recorder"]["recorded_count"] = 4
            self.assertEqual(execution._input_fingerprint(SimpleNamespace(), ["source"]), original)
            source["matter"][0]["quantity"] = 1
            self.assertNotEqual(execution._input_fingerprint(SimpleNamespace(), ["source"]), original)
            source["matter"][0]["quantity"] = 2
            source["state"]["program"]["fields"]["signal"] = True
            self.assertNotEqual(execution._input_fingerprint(SimpleNamespace(), ["source"]), original)

    def test_revision_is_a_floor_and_legacy_records_keep_strict_cas(self):
        current = {"version": 50, "interaction_revision": 40}
        m.check_entity_version(current, 40, observed_interaction_revision=40, fixed_intent=True)
        m.check_entity_version(current, 49, observed_interaction_revision=40, fixed_intent=True)
        for expected, revision, fixed in ((39, 40, True), (51, 40, True),
                                           (49, 39, True), (49, 40, False), (49, None, True)):
            with self.subTest(expected=expected, revision=revision, fixed=fixed):
                with self.assertRaises(StateConflict):
                    m.check_entity_version(current, expected,
                        observed_interaction_revision=revision, fixed_intent=fixed)
        legacy = {"version": 50}
        self.assertEqual(m.interaction_revision(legacy), 50)
        m.check_entity_version(legacy, 50)
        with self.assertRaises(StateConflict):
            m.check_entity_version(legacy, 49, observed_interaction_revision=50, fixed_intent=True)

    def test_only_known_autonomous_outputs_preserve_the_guard(self):
        previous = {"version": 20, "interaction_revision": 11,
            "owner": None, "x": 4, "y": 3, "lifecycle": "intact",
            "components": {"container": {"slots": 2}, "barrier": {"active": False}},
            "matter": [{"quantity": 8, "burning": False}],
            "state": {"burning": False, "program": {"definition_ref": "immutable-1",
                "fields": {"count": 0}, "mechanical": {"angle": 0, "energy": 8},
                "connections": [], "port_bindings": {},
                "process": {"status": "idle", "steps": 0, "last_tick": 0}}}}
        evolved = deepcopy(previous)
        evolved["state"]["program"]["fields"]["count"] = 1
        evolved["state"]["program"]["mechanical"].update(angle=3, energy=7)
        evolved["state"]["program"]["process"].update(steps=1, last_tick=1)
        self.assertEqual(m._entity_write_revision(previous, evolved, origin="autonomous"), 11)
        # An external write of those exact same fields has a different source.
        self.assertEqual(m._entity_write_revision(previous, evolved, origin="interaction"), 21)

        protected = {
            "material": lambda value: value["matter"][0].update(quantity=7),
            "burning": lambda value: value["matter"][0].update(burning=True),
            "integrity": lambda value: value.update(lifecycle="broken"),
            "ownership": lambda value: value.update(owner="another-player"),
            "position": lambda value: value.update(x=5),
            "capacity": lambda value: value["components"]["container"].update(slots=1),
            "barrier": lambda value: value["components"]["barrier"].update(active=True),
            "definition": lambda value: value["state"]["program"].update(definition_ref="immutable-2"),
            "ports": lambda value: value["state"]["program"]["port_bindings"].update(peer="other"),
            "topology": lambda value: value["state"]["program"]["connections"].append({"other_id": "other"}),
            "unknown_metadata": lambda value: value.update(new_mechanism={"protected": True}),
        }
        for name, change in protected.items():
            with self.subTest(change=name):
                value = deepcopy(evolved)
                change(value)
                self.assertEqual(m._entity_write_revision(previous, value, origin="autonomous"), 21)
        forged = deepcopy(previous)
        forged["interaction_revision"] = 999
        self.assertEqual(m._entity_write_revision(previous, forged, origin="interaction"), 11)
        legacy = deepcopy(previous)
        legacy.pop("interaction_revision")
        self.assertEqual(m._entity_write_revision(legacy, evolved, origin="autonomous"), 20)


class InteractionRevisionProductContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DomainProductContracts(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.addCleanup(lambda: self.h.world.close())

    def counter(self, key, *, placed=True, extra_actions=()):
        raw = fixtures.counter_definition(key)
        raw["types"]["counter"]["actions"].extend(deepcopy(list(extra_actions)))
        reference = self.fixture.c.publish(raw)
        entity = self.fixture.c.form(reference, "counter")
        if placed:
            self.fixture.c.place(entity)
        return entity

    def intent(self, actor, entity_id, action, **extra):
        entity = self.h.entity(entity_id)
        return {"action": action, "target_id": entity_id,
            "expected_target_version": entity["version"],
            "expected_target_interaction_revision": m.interaction_revision(entity),
            "expected_player_version": self.h.view(actor)["player"]["version"], **extra}

    def call_original(self, actor, function, args, operation):
        return self.h.world.call_function("w", function, self.h.roles[actor], args,
                                          operation_id=operation)

    def test_autonomous_inventory_counter_can_be_dropped_with_its_original_intent(self):
        entity_id = self.counter("revision_inventory", placed=False)
        args = self.intent("A", entity_id, "drop")
        original = deepcopy(args)
        self.h.ticks(5)
        evolved = deepcopy(self.h.entity(entity_id))
        self.assertGreater(evolved["version"], args["expected_target_version"])
        self.assertEqual(m.interaction_revision(evolved), args["expected_target_interaction_revision"])
        count = evolved["state"]["program"]["fields"]["count"]

        receipt = self.call_original("A", "grove.interact", args, "drop-evolving-original")
        saved = deepcopy(self.h.entity(entity_id))
        self.assertIsNone(saved["owner"])
        self.assertEqual(saved["state"]["program"]["fields"]["count"], count)
        self.assertEqual(m.interaction_revision(saved), saved["version"])
        self.assertEqual(args, original)
        self.h.world.close()
        self.h.world = WorldRuntime(self.h.db)
        self.h.world.install_world("w", make_world())
        replay = self.call_original("A", "grove.interact", args, "drop-evolving-original")
        self.assertEqual(replay, {**receipt, "replayed": True})
        self.assertEqual(self.h.entity(entity_id), saved)

    def test_other_player_picks_up_an_evolving_object_with_the_displayed_guard_once(self):
        for identity_field in ("target_id", "item_id"):
            with self.subTest(identity_field=identity_field):
                entity_id = self.counter("revision_pickup_" + identity_field)
                self.h.approach("B", entity_id)
                observed = self.h.read("B", "grove.inspect", {"entity_id": entity_id, "action": "pickup"})
                option = next(row for row in observed["action_options"] if row["action"] == "pickup")
                shown = observed["entity"]
                self.assertTrue(option["available"])
                self.assertEqual(option["arguments"]["expected_target_interaction_revision"], shown["interaction_revision"])
                self.assertIn("expected_target_interaction_revision", option["input_schema"]["properties"])
                self.assertIn("expected_item_interaction_revision", option["input_schema"]["properties"])
                args = {**deepcopy(option["arguments"]),
                    "expected_player_version": observed["player"]["version"]}
                if identity_field == "item_id":
                    args["item_id"] = args.pop("target_id")
                original = deepcopy(args)
                self.h.ticks(5)
                evolved = deepcopy(self.h.entity(entity_id))
                self.assertGreater(evolved["version"], shown["version"])
                self.assertEqual(m.interaction_revision(evolved), shown["interaction_revision"])
                count = evolved["state"]["program"]["fields"]["count"]
                operation = "pickup-evolving-original-" + identity_field
                receipt = self.call_original("B", "grove.interact", args, operation)
                saved = deepcopy(self.h.entity(entity_id))
                self.assertEqual(saved["owner"], self.h.roles["B"])
                self.assertEqual(saved["physical_parent"],
                    {"id": observed["player"]["backpack_id"], "relation": "contained"})
                self.assertEqual(saved["state"]["program"]["fields"]["count"], count)
                self.assertEqual(saved["matter"], evolved["matter"])
                self.assertEqual(m.interaction_revision(saved), saved["version"])
                self.assertEqual(args, original)
                self.h.world.close()
                self.h.world = WorldRuntime(self.h.db)
                self.h.world.install_world("w", make_world())
                replay = self.call_original("B", "grove.interact", args, operation)
                self.assertEqual(replay, {**receipt, "replayed": True})
                self.assertEqual(self.h.entity(entity_id), saved)

    def test_pickup_rejects_another_players_field_aba_without_moving_the_object(self):
        entity_id = self.counter("revision_pickup_external_aba", extra_actions=[{
            "id": "one", "label": "设为一", "effects": [{"op": "set", "field": "count", "value": 1}]}])
        self.h.approach("B", entity_id)
        args = self.intent("A", entity_id, "pickup")
        self.h.action("B", entity_id, "one")
        self.h.action("B", entity_id, "reset")
        changed = deepcopy(self.h.entity(entity_id))
        self.assertGreater(m.interaction_revision(changed), args["expected_target_interaction_revision"])
        self.assertEqual(changed["state"]["program"]["fields"]["count"], 0)
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.interact", args, "pickup-stale-external-aba")
        self.assertEqual(self.h.entity(entity_id), changed)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["A"], "pickup-stale-external-aba")

    def test_pickup_guard_does_not_remove_current_distance_checks(self):
        entity_id = self.counter("revision_pickup_distance")
        args = self.intent("A", entity_id, "pickup")
        scene = self.h.view("A")
        target = self.h.entity(entity_id)
        tiles = {(chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        blocked = {(row["x"], row["y"]) for row in scene["entities"]
                   if row.get("barrier_active") or "blocking" in row["capabilities"]}
        start = scene["player"]["x"], scene["player"]["y"]
        queue, visited, destination = deque([start]), {start}, None
        while queue:
            point = queue.popleft()
            if abs(point[0] - target["x"]) + abs(point[1] - target["y"]) >= 3:
                destination = point
                break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                step = point[0] + dx, point[1] + dy
                if step not in visited and step not in blocked and tiles.get(step, "rock") != "rock":
                    visited.add(step)
                    queue.append(step)
        self.assertIsNotNone(destination)
        self.fixture.move_to(destination)
        args["expected_player_version"] = self.h.view("A")["player"]["version"]
        self.h.ticks(2)
        evolved = deepcopy(self.h.entity(entity_id))
        self.assertEqual(m.interaction_revision(evolved), args["expected_target_interaction_revision"])
        with self.assertRaises(RuleViolation):
            self.call_original("A", "grove.interact", args, "pickup-out-of-reach")
        self.assertEqual(self.h.entity(entity_id), evolved)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["A"], "pickup-out-of-reach")

    def test_other_player_field_aba_invalidates_the_old_revision(self):
        entity_id = self.counter("revision_external_aba", extra_actions=[{
            "id": "one", "label": "设为一", "effects": [{"op": "set", "field": "count", "value": 1}]}])
        self.h.approach("B", entity_id)
        args = self.intent("A", entity_id, "reset")
        before = deepcopy(self.h.entity(entity_id))
        self.h.action("B", entity_id, "one")
        self.h.action("B", entity_id, "reset")
        after = deepcopy(self.h.entity(entity_id))
        self.assertEqual(after["state"]["program"]["fields"], before["state"]["program"]["fields"])
        self.assertGreater(m.interaction_revision(after), args["expected_target_interaction_revision"])
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.rule_action", args, "stale-before-another-player-aba")
        self.assertEqual(self.h.entity(entity_id), after)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["A"], "stale-before-another-player-aba")

    def test_legacy_observation_adopts_its_original_storage_version_as_the_floor(self):
        entity_id = self.counter("revision_legacy_record")
        # Represent the saved pre-revision schema in this isolated fixture.
        # No migration, world action or caller supplies the new guard value.
        value = deepcopy(self.h.world.get_state("w", m.ENTITIES, entity_id)["value"])
        value.pop("interaction_revision")
        with self.h.world._conn() as connection:
            connection.execute("UPDATE world_state SET value_json=? WHERE universe=? AND scope=? AND state_key=?",
                (json.dumps(value), "w", m.ENTITIES, entity_id))
        observed = self.h.read("A", "grove.inspect", {"entity_id": entity_id})["entity"]
        self.assertEqual(observed["interaction_revision"], observed["version"])
        args = self.intent("A", entity_id, "reset")
        self.h.ticks(1)
        evolved = deepcopy(self.h.entity(entity_id))
        self.assertEqual(evolved["interaction_revision"], observed["version"])
        self.assertGreater(evolved["version"], observed["version"])
        mixed = {**args, "expected_target_version": observed["version"] - 1}
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.rule_action", mixed, "legacy-mixed-observation")
        self.assertEqual(self.h.entity(entity_id), evolved)
        result = self.call_original("A", "grove.rule_action", args, "legacy-original-observation")
        self.assertTrue(result["ok"])
        self.assertEqual(self.h.entity(entity_id)["state"]["program"]["fields"]["count"], 0)

    def test_near_limit_legacy_record_keeps_running_after_lazy_guard_initialization(self):
        entity_id = self.counter("revision_legacy_byte_limit")
        legacy = deepcopy(self.h.entity(entity_id))
        legacy.pop("interaction_revision")
        lower, upper = 0, sm.MAX_ENTITY_BYTES
        while lower < upper:
            middle = (lower + upper + 1) // 2
            candidate = deepcopy(legacy)
            candidate["state"]["padding"] = "x" * middle
            try:
                m.normalize_entity(candidate)
                lower = middle
            except RuleViolation:
                upper = middle - 1
        legacy["state"]["padding"] = "x" * lower
        accepted = m.normalize_entity(legacy)
        larger = deepcopy(legacy)
        larger["state"]["padding"] += "x"
        with self.assertRaisesRegex(RuleViolation, "8KiB"):
            m.normalize_entity(larger)
        with self.h.world._conn() as connection:
            connection.execute("UPDATE world_state SET value_json=? WHERE universe=? AND scope=? AND state_key=?",
                (json.dumps(accepted), "w", m.ENTITIES, entity_id))
        old_version = self.h.entity(entity_id)["version"]
        # The second save is essential: after the first lazy stamp exists,
        # normalization must not reserve its metadata a second time.
        self.h.ticks(2)
        saved = self.h.entity(entity_id)
        self.assertEqual(saved["state"]["program"]["fields"]["count"], 2)
        self.assertEqual(saved["state"]["padding"], legacy["state"]["padding"])
        self.assertEqual(saved["interaction_revision"], old_version)
        self.assertLessEqual(sm.encoded_size(m.normalize_entity(saved)), sm.MAX_ENTITY_BYTES)

    def test_source_control_revalidates_evolution_but_fuel_keeps_exact_lot_cas(self):
        raw = fixtures.counter_definition("revision_source_control")
        raw["types"]["counter"]["mechanical"] = {"role": "source"}
        reference = self.fixture.c.publish(raw)
        source = self.fixture.c.form(reference, "counter")
        other = self.fixture.c.form(reference, "counter")
        self.fixture.c.place(source)
        self.fixture.c.place(other)
        wood_id = self.h.item("A", "木柴")["id"]
        lot_id = next(lot["id"] for lot in self.h.entity(wood_id)["matter"] if lot["material"] == "wood")
        fuel = self.intent("A", source, "fuel", item_id=wood_id,
            expected_item_version=self.h.entity(wood_id)["version"], lot_id=lot_id, amount=1)
        # The second action consumes this real source without changing this
        # player's position/inventory membership or the first target's fields.
        self.h.action("A", other, "fuel", item_id=wood_id, lot_id=lot_id, amount=1)
        self.assertEqual(self.h.view("A")["player"]["version"], fuel["expected_player_version"])
        before = deepcopy(self.h.entity(source))
        remaining = deepcopy(self.h.entity(wood_id))
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.rule_action", fuel, "stale-actual-fuel-source")
        self.assertEqual(self.h.entity(source), before)
        self.assertEqual(self.h.entity(wood_id), remaining)
        self.h.action("A", source, "fuel", item_id=wood_id, lot_id=lot_id, amount=4)
        self.h.action("A", source, "start")
        stop = self.intent("A", source, "stop")
        self.h.ticks(2)
        evolved = deepcopy(self.h.entity(source))
        self.assertGreater(evolved["version"], stop["expected_target_version"])
        self.assertEqual(m.interaction_revision(evolved), stop["expected_target_interaction_revision"])
        self.assertGreater(evolved["state"]["program"]["fields"]["count"], 0)
        result = self.call_original("A", "grove.rule_action", stop, "stop-evolving-source")
        self.assertTrue(result["ok"])
        self.assertFalse(self.h.entity(source)["state"]["program"]["mechanical"]["enabled"])
        self.assertEqual(self.h.entity(source)["state"]["program"]["mechanical"]["energy"],
                         evolved["state"]["program"]["mechanical"]["energy"])

    def test_pending_original_operation_survives_evolution_and_commits_once(self):
        entity_id = self.counter("revision_busy_original")
        self.h.approach("B", entity_id)
        grant = self.h.call("B", "grove.control_grant", {"goal": "重设计数", "max_actions": 1})
        args = self.intent("B", entity_id, "reset", delegation=grant["delegation"])
        original = deepcopy(args)
        operation = "same-revision-after-busy"
        frame = self.fixture.begin_frame()
        with self.assertRaises(domains.DomainBusy):
            self.call_original("B", "grove.rule_action", args, operation)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["B"], operation)
        self.assertEqual(self.h.read("B", "grove.control_status")["actions_count"], 0)
        self.fixture.finish_frame(frame["id"])
        evolved = self.h.entity(entity_id)
        self.assertGreater(evolved["version"], args["expected_target_version"])
        self.assertEqual(m.interaction_revision(evolved), args["expected_target_interaction_revision"])
        receipt = self.call_original("B", "grove.rule_action", args, operation)
        saved = deepcopy(self.h.entity(entity_id))
        self.assertEqual(saved["state"]["program"]["fields"]["count"], 0)
        self.assertEqual(self.h.read("B", "grove.control_status")["actions_count"], 1)
        replay = self.call_original("B", "grove.rule_action", args, operation)
        self.assertEqual(replay, {**receipt, "replayed": True})
        self.assertEqual(self.h.entity(entity_id), saved)
        self.assertEqual(self.h.read("B", "grove.control_status")["actions_count"], 1)
        self.assertEqual(args, original)

    def test_unchanged_revision_does_not_grant_a_now_false_condition(self):
        entity_id = self.counter("revision_live_condition", extra_actions=[{
            "id": "only_zero", "label": "仅零时设为九",
            "when": {"op": "eq", "left": {"field": "count"}, "right": 0},
            "effects": [{"op": "set", "field": "count", "value": 9}]}])
        args = self.intent("A", entity_id, "only_zero")
        self.h.ticks(1)
        evolved = deepcopy(self.h.entity(entity_id))
        self.assertEqual(m.interaction_revision(evolved), args["expected_target_interaction_revision"])
        with self.assertRaisesRegex(RuleViolation, "实际前提"):
            self.call_original("A", "grove.rule_action", args, "condition-now-false")
        self.assertEqual(self.h.entity(entity_id), evolved)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["A"], "condition-now-false")

    def test_autonomous_combustion_invalidates_the_observed_material_state(self):
        raw = fixtures.counter_definition("revision_burning")
        raw["types"]["counter"]["cost_u"] = {"wood": 8}
        raw["types"]["counter"]["components"] = {"combustion": {"rate": 1, "spread": False}}
        entity_id = self.fixture.c.form(self.fixture.c.publish(raw), "counter")
        scene = self.h.view()
        tiles = {(chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        zones = [zone for chunk in scene["chunks"]
                 for zone in self.h.world.get_state("w", m.CHUNKS, chunk["id"])["value"]["safe_zones"]]
        occupied = {(item["x"], item["y"]) for item in scene["entities"]}
        player = scene["player"]
        location = next((point for point in sorted(tiles)
            if tiles[point] in {"grass", "dirt"} and point not in occupied
            and max(abs(point[0] - player["x"]), abs(point[1] - player["y"])) <= 7
            and all(max(abs(point[0] - zone["x"]), abs(point[1] - zone["y"])) > zone["radius"] for zone in zones)), None)
        self.assertIsNotNone(location)
        self.fixture.move_to(location)
        self.fixture.c.place(entity_id)
        self.h.interact("A", entity_id, "ignite", item_id=self.h.item("A", "打火石")["id"])
        args = self.intent("A", entity_id, "reset")
        pickup = self.intent("A", entity_id, "pickup")
        before = deepcopy(self.h.entity(entity_id))
        sequence = self.h.world.get_state("w", m.META, "clock")["value"]["sequence"]
        self.h.ticks(COMBUSTION_CADENCE_TICKS - sequence % COMBUSTION_CADENCE_TICKS)
        burnt = deepcopy(self.h.entity(entity_id))
        self.assertNotEqual(burnt["matter"], before["matter"])
        self.assertGreater(m.interaction_revision(burnt), args["expected_target_interaction_revision"])
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.rule_action", args, "old-intent-after-real-burning")
        self.assertEqual(self.h.entity(entity_id), burnt)
        with self.assertRaises(StateConflict):
            self.call_original("A", "grove.interact", pickup, "pickup-after-real-burning")
        self.assertEqual(self.h.entity(entity_id), burnt)
        with self.assertRaises(ReceiptNotFound):
            self.h.world.get_receipt("w", self.h.roles["A"], "pickup-after-real-burning")


if __name__ == "__main__":
    unittest.main()
