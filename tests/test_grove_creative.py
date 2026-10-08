"""Formal player publication/use paths, separate from real model/UI evidence."""
from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_world import FunctionOutcome, FunctionSpec, WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.worlds.shared_grove import make_world, model as m
from agent_world.worlds.shared_grove import creative
from agent_world.simulation.matter import encoded_size
from agent_world.simulation.programs import solve_mechanical


def package():
    return {"key": "workshop", "name": "共享工坊", "version": 1,
        "materials": {key: {"name": name, "feedstock": "wood", "load_per_u": 1, "liquid": True, "combustible": False}
                      for key, name in (("amber", "琥珀浆"), ("blue", "蓝浆"), ("resin", "绿树脂"))},
        "types": {
            "mixer": {"name": "混合器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20,
                "fields": {"temperature": {"type": "integer", "default": 0, "min": 0, "max": 100}},
                "rules": [{"id": "blend", "phase": "reaction", "when": True, "effects": [
                    {"op": "react", "inputs": {"amber": 1, "blue": 1}, "outputs": {"resin": 2}, "rate": 1, "heat": 4}]}]},
            "polisher": {"name": "产物再加工器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 20,
                "rules": [{"id": "refine", "phase": "reaction", "when": True, "effects": [
                    {"op": "react", "inputs": {"resin": 1}, "outputs": {"blue": 1}, "rate": 1}]}]},
            "source": {"name": "有限动力", "body_material": "wood", "cost": {"wood": 1},
                "mechanical": {"role": "source", "teeth": 16, "torque": 40, "speed": 1}},
            "gear": {"name": "齿轮", "body_material": "wood", "cost": {"wood": 1},
                "mechanical": {"role": "gear", "teeth": 32}},
            "gate": {"name": "可通行执行器", "body_material": "wood", "cost": {"wood": 1},
                "mechanical": {"role": "actuator", "teeth": 16, "pitch": 2, "load": 2, "travel": 20},
                "barrier": {"op": "lt", "left": {"mechanical": "position"}, "right": 4}},
            "echo": {"name": "现场回声", "body_material": "stone", "cost": {"stone": 1},
                "recorder": {"radius": 8, "window_ticks": 50}},
            "pact": {"name": "双方交付契约", "body_material": "stone", "cost": {"stone": 1},
                "contract": {"material": "wood", "quantity": 2, "duration_ticks": 5}}}}


class CreativeWorldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="creative-world-")
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "world.sqlite3"
        self.time = patch("time.time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", make_world())
        self.roles = {name: self.world.create_role(name)["role_id"] for name in "AB"}
        self.serial = 0
        for name in self.roles:
            self.call(name, "grove.join", {"name": name})

    def call(self, actor, function, args, operation=None):
        self.serial += 1
        return self.world.call_function("w", function, self.roles[actor], args,
            operation_id=operation or f"creative-{self.serial}")["result"]

    def read(self, actor, function, args=None):
        return self.world.query_function("w", function, self.roles[actor], args or {})["result"]

    def view(self, actor="A"):
        return self.read(actor, "map.viewport")

    def entity(self, entity_id):
        row = self.world.get_state("w", m.ENTITIES, entity_id)
        return {**row["value"], "version": row["version"]}

    def item(self, actor, name):
        return next(item for item in self.view(actor)["inventory"] if item["name"] == name)

    def approach(self, actor, entity_id):
        entity = self.entity(entity_id)
        target = (entity["x"], entity["y"])
        scene = self.view(actor)
        start = scene["player"]["x"], scene["player"]["y"]
        tiles = {(chunk["cx"]*16+index%16, chunk["cy"]*16+index//16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        blocked = {(item["x"], item["y"]) for item in scene["entities"] if item.get("barrier_active") or "blocking" in item["capabilities"]}
        queue, previous = deque([start]), {start: None}
        goal = None
        while queue:
            point = queue.popleft()
            if abs(point[0]-target[0])+abs(point[1]-target[1]) <= 1:
                goal = point
                break
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                next_point = point[0]+dx, point[1]+dy
                if next_point in previous or next_point in blocked or tiles.get(next_point, "rock") == "rock":
                    continue
                previous[next_point] = point
                queue.append(next_point)
        self.assertIsNotNone(goal, (start, target))
        route = []
        while goal != start:
            route.append(goal)
            goal = previous[goal]
        for destination in reversed(route):
            player = self.view(actor)["player"]
            self.call(actor, "grove.move", {"dx": destination[0]-player["x"], "dy": destination[1]-player["y"], "expected_player_version": player["version"]})

    def action(self, actor, target, action, **extra):
        self.approach(actor, target)
        args = {"action": action, "target_id": target, "expected_target_version": self.entity(target)["version"],
                "expected_player_version": self.view(actor)["player"]["version"], **extra}
        if extra.get("item_id"):
            args["expected_item_version"] = self.entity(extra["item_id"])["version"]
        if extra.get("other_id"):
            if action == "offer":
                args["expected_other_version"] = self.world.get_state("w", m.PLAYERS, extra["other_id"])["version"]
            else:
                args["expected_other_version"] = self.entity(extra["other_id"])["version"]
        return self.call(actor, "grove.rule_action", args)

    def publish(self, actor, definition, types, refs=None):
        ref = self.call(actor, "grove.definition_publish", {"definition": definition})["definition_ref"]
        context = self.call(actor, "grove.generation_context", {"kind": "layout", "reason": "user_request"})
        self.assertIsNotNone(context.get("opportunity"), context)
        available = sorted(context["allowed_tiles"], key=lambda point: (abs(point["x"]-2)+abs(point["y"]-2), point["y"], point["x"]))
        tiles = None
        if len(types) >= 3:
            lookup = {(point["x"], point["y"]): point for point in available}
            for point in available:
                line = [(point["x"]+offset, point["y"]) for offset in range(3)]
                if all(position in lookup for position in line):
                    picked = [lookup[position] for position in line]
                    tiles = picked + [point for point in available if point not in picked][:len(types)-3]
                    break
        tiles = available[:len(types)] if tiles is None else tiles
        objects = [{"definition_ref": (refs or {}).get(key, ref), "type_id": key, **tiles[index]} for index, key in enumerate(types)]
        opportunity = context["opportunity"]
        result = self.call(actor, "grove.content_submit", {"opportunity_id": opportunity["id"],
            "expected_opportunity_version": opportunity["version"], "content": {"kind": "layout", "objects": objects}})
        return ref, {key: result["entities"][index]["id"] for index, key in enumerate(types)}

    def interact(self, actor, target, action, **extra):
        args = {"action": action, "target_id": target, "expected_target_version": self.entity(target)["version"],
                "expected_player_version": self.view(actor)["player"]["version"], **extra}
        if extra.get("item_id"):
            args["expected_item_version"] = self.entity(extra["item_id"])["version"]
        return self.call(actor, "grove.interact", args)

    def ticks(self, count):
        for _ in range(count):
            self.time.return_value += 1
            result = self.world.run_due_timers("w")
            self.assertEqual(result.get("failed", 0), 0, result)
            self.assertEqual(result["count"], 1, result)

    def test_formal_four_capabilities_and_other_identity_continue(self):
        definition = package()
        ref, objects = self.publish("A", definition, ["source", "gear", "gate", "mixer"])
        self.time.return_value += 31  # operational publication lease, not game progress
        self.world.run_due_timers("w")  # resume pauses the missed simulation interval
        _, more = self.publish("B", definition, ["echo", "pact", "polisher"], refs={"polisher": ref})
        objects.update(more)
        self.action("B", objects["echo"], "start_recording")
        self.approach("A", objects["mixer"])
        # Consume the public action projection, including the declared input
        # material, instead of assuming charge means transferring a raw lot.
        source = self.item("A", "木柴")
        before = sum(lot["quantity"] for lot in source["matter"] if lot["material"] == "wood")
        for symbol, amount in (("amber", 2), ("blue", 4)):
            inspected = self.world.query_function("w", "grove.inspect", self.roles["A"],
                                                  {"entity_id": objects["mixer"]})["result"]
            charge = next(option for option in inspected["action_options"] if option["action"] == "charge")
            choice = next(choice for choice in charge["choices"]["materials"] if choice["id"] == symbol)
            item_choice = next(choice for choice in charge["choices"]["items"] if choice["id"] == source["id"])
            self.assertEqual(choice["feedstock"], "wood")
            self.call("A", charge["function_id"], {**charge["arguments"], "material": choice["id"],
                "amount": amount, "item_id": item_choice["id"], "expected_item_version": item_choice["version"],
                "expected_player_version": self.view("A")["player"]["version"]})
        self.assertEqual(sum(lot["quantity"] for lot in self.entity(source["id"])["matter"]
                             if lot["material"] == "wood"), before-6)
        self.assertEqual(creative._quantities(self.entity(objects["mixer"])), {"amber": 2, "blue": 4})
        self.ticks(1)
        mixed = self.entity(objects["mixer"])
        quantities = creative._quantities(mixed)
        self.assertEqual(quantities, {"amber": 1, "blue": 3, "resin": 2})
        self.assertEqual(mixed["state"]["program"]["fields"]["temperature"], 4)
        # Prove the actual reaction was recorded while it is in the retained
        # window. Later unrelated actions may evict this older fact under the
        # shared byte budget, without erasing the cumulative recorded count.
        reaction_record = self.world.get_state("w", creative.RECORDS, objects["echo"])["value"]
        self.assertTrue(any(event["action"] == "reaction" for event in reaction_record["history"]))
        reaction_count = self.entity(objects["echo"])["state"]["program"]["recorder"]["recorded_count"]
        # Another identity takes actual product into its ordinary vessel, then
        # supplies a second published process using the same immutable material.
        self.approach("B", objects["mixer"])
        vessel = self.item("B", "水壶")
        product = next(lot for lot in mixed["matter"] if lot["material"] == ref+"#resin")
        self.interact("B", vessel["id"], "transfer", item_id=mixed["id"], lot_id=product["id"], amount=1)
        self.assertEqual(sum(lot["quantity"] for lot in self.entity(vessel["id"])["matter"] if lot["material"] == ref+"#resin"), 1)
        self.approach("B", objects["polisher"])
        carried_product = next(lot for lot in self.entity(vessel["id"])["matter"] if lot["material"] == ref+"#resin")
        self.interact("B", objects["polisher"], "transfer", item_id=vessel["id"], lot_id=carried_product["id"], amount=1)
        self.ticks(1)
        self.assertEqual(creative._quantities(self.entity(objects["polisher"])), {"blue": 1})
        self.action("A", objects["source"], "connect", other_id=objects["gear"])
        self.action("A", objects["gear"], "connect", other_id=objects["gate"])
        self.action("A", objects["source"], "fuel", item_id=source["id"], amount=6)
        self.action("A", objects["source"], "start")
        self.ticks(2)
        gate = self.entity(objects["gate"])
        self.assertEqual(gate["state"]["program"]["mechanical"]["position"], 4)
        self.assertFalse(gate["components"]["barrier"]["active"])
        self.assertNotIn("blocking", gate["capabilities"])
        self.approach("B", gate["id"])
        player = self.view("B")["player"]
        if abs(gate["x"]-player["x"])+abs(gate["y"]-player["y"]) == 1:
            self.call("B", "grove.move", {"dx": gate["x"]-player["x"], "dy": gate["y"]-player["y"], "expected_player_version": player["version"]})
        self.action("A", objects["source"], "disconnect", other_id=objects["gear"])
        retained = self.entity(objects["gate"])["state"]["program"]["mechanical"]["position"]
        self.ticks(1)
        self.assertEqual(self.entity(objects["gate"])["state"]["program"]["mechanical"]["position"], retained)
        self.approach("A", objects["pact"])
        self.approach("B", objects["pact"])
        result = self.call("A", "grove.rule_action", {"action": "offer", "target_id": objects["pact"],
            "expected_target_version": self.entity(objects["pact"])["version"], "expected_player_version": self.view()["player"]["version"],
            "other_id": self.roles["B"], "expected_other_version": self.view("B")["player"]["version"]})
        terms = result["entity"]["state"]["program"]["contract"]["terms_version"]
        self.action("A", objects["pact"], "accept", terms_version=terms)
        self.assertEqual(self.entity(objects["pact"])["state"]["program"]["contract"]["status"], "awaiting_acceptance")
        self.action("B", objects["pact"], "accept", terms_version=terms)
        other_source = self.item("B", "木柴")
        self.action("B", objects["pact"], "deliver", item_id=other_source["id"], amount=2)
        self.assertEqual(self.entity(objects["pact"])["state"]["program"]["contract"]["status"], "complete")
        self.action("A", objects["pact"], "release", item_id=self.item("A", "水壶")["id"])
        with self.assertRaises(PermissionDenied):
            self.action("B", objects["pact"], "release", item_id=self.item("B", "水壶")["id"])
        record = self.action("A", objects["echo"], "read_records")["records"]
        self.assertGreaterEqual(self.entity(objects["echo"])["state"]["program"]["recorder"]["recorded_count"], reaction_count)
        self.assertLessEqual(encoded_size(record), creative.MAX_RECORD_BYTES)
        self.assertEqual(record["reads"][-1]["actor"], self.roles["A"])
        self.assertTrue(all("recorded_tick" in event and "occurred_at" in event for event in record["history"]))
        self.approach("B", objects["echo"])
        self.interact("B", objects["echo"], "pickup")
        public_reads = deepcopy(self.world.get_state("w", creative.RECORDS, objects["echo"])["value"]["reads"])
        public_changes = deepcopy(self.world.get_state("w", m.META, "changes")["value"])
        private_read = self.call("B", "grove.rule_action", {"action": "read_records", "target_id": objects["echo"],
            "expected_target_version": self.entity(objects["echo"])["version"], "expected_player_version": self.view("B")["player"]["version"]})
        self.assertEqual(private_read["records"]["reads"], public_reads)
        self.assertEqual(self.world.get_state("w", m.META, "changes")["value"], public_changes)
        previous = deepcopy(self.entity(objects["mixer"])["state"]["program"])
        version2 = deepcopy(definition)
        version2["version"] = 2
        version2["types"]["mixer"]["container_capacity"] = 100
        new_ref = self.call("A", "grove.definition_publish", {"definition": version2})["definition_ref"]
        self.assertNotEqual(ref, new_ref)
        self.assertEqual(self.entity(objects["mixer"])["state"]["program"], previous)
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", make_world())
        self.assertEqual(self.entity(objects["gate"])["state"]["program"]["mechanical"]["position"], retained)
        self.assertEqual(self.read("A", "grove.definition_read", {"definition_ref": ref})["definition"]["version"], 1)

    def test_ordered_events_rule_failure_isolated_and_pause(self):
        definition = {"key": "counter", "name": "动作计数", "version": 1, "types": {"counter": {
            "name": "计数物件", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 6,
            "fields": {"count": {"type": "integer", "default": 0, "min": 0, "max": 2}, "opened": {"type": "boolean", "default": True}},
            "recorder": {"radius": 8, "window_ticks": 30},
            "rules": [{"id": "count_moves", "phase": "after", "when": {"op": "eq", "left": {"event": "action"}, "right": "move"},
                "effects": [{"op": "set", "field": "count", "value": {"op": "add", "left": {"field": "count"}, "right": 1}}]},
                {"id": "a_open", "phase": "after", "when": {"op": "eq", "left": {"event": "action"}, "right": "open"}, "effects": [{"op": "set", "field": "opened", "value": True}]},
                {"id": "b_close", "phase": "after", "when": {"op": "eq", "left": {"event": "action"}, "right": "close"}, "effects": [{"op": "set", "field": "opened", "value": False}]}],
            "actions": [{"id": "repair", "label": "重新计数", "parameters": {"value": {"type": "integer", "default": 0, "min": 0, "max": 2}},
                "when": {"op": "lte", "left": {"param": "value"}, "right": 2}, "effects": [{"op": "set", "field": "count", "value": {"param": "value"}}]}]}}}
        _, objects = self.publish("A", definition, ["counter"])
        target = objects["counter"]
        self.approach("A", target)
        self.ticks(1)
        self.action("A", target, "repair", params={"value": 0})
        player = self.view()["player"]
        # Two actual opposite movement operations in the same logical step.
        direction = next((dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                         if (player["x"]+dx, player["y"]+dy) != (self.entity(target)["x"], self.entity(target)["y"]))
        self.call("A", "grove.move", {"dx": direction[0], "dy": direction[1], "expected_player_version": player["version"]})
        self.call("A", "grove.move", {"dx": -direction[0], "dy": -direction[1], "expected_player_version": self.view()["player"]["version"]})
        self.ticks(1)
        self.assertEqual(self.entity(target)["state"]["program"]["fields"]["count"], 2)
        cursor = self.entity(target)["state"]["program"]["event_cursor"]
        self.ticks(1)
        self.assertEqual(self.entity(target)["state"]["program"]["fields"]["count"], 2)
        self.assertEqual(self.entity(target)["state"]["program"]["event_cursor"], cursor)
        self.interact("A", target, "close")
        self.interact("A", target, "open")
        self.ticks(1)
        self.assertTrue(self.entity(target)["state"]["program"]["fields"]["opened"])
        self.call("A", "grove.move", {"dx": direction[0], "dy": direction[1], "expected_player_version": self.view()["player"]["version"]})
        self.ticks(1)
        self.assertEqual(self.entity(target)["state"]["program"]["process"]["status"], "error")
        before = self.view()["clock"]["sequence"]
        self.ticks(1)
        self.assertEqual(self.view()["clock"]["sequence"], before+1)
        self.time.return_value += 10000
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", make_world())
        self.world.run_due_timers("w")
        self.assertEqual(self.view()["clock"]["sequence"], before+1)
        self.ticks(1)
        self.assertEqual(self.view()["clock"]["sequence"], before+2)

    def test_delegation_revoke_and_exact_retry(self):
        grant = self.call("A", "grove.control_grant", {"goal": "走到附近看一眼", "max_actions": 2})
        player = self.view()["player"]
        args = {"dx": 1, "dy": 0, "expected_player_version": player["version"], "delegation": grant["delegation"]}
        first = self.call("A", "grove.move", args, operation="single-step")
        repeated = self.call("A", "grove.move", args, operation="single-step")
        self.assertEqual(first, repeated)
        self.assertEqual(self.read("A", "grove.control_status")["actions_count"], 1)
        self.call("A", "grove.control_revoke", {})
        with self.assertRaises(PermissionDenied):
            self.call("A", "grove.move", {**args, "expected_player_version": self.view()["player"]["version"]})
        self.assertEqual(self.view()["player"], first["player"])

    def test_real_breakage_preserves_surviving_contract_escrow(self):
        definition = {"key": "fragile_pact", "name": "可损坏契约", "version": 1, "types": {"pact": {
            "name": "木制契约盒", "body_material": "wood", "cost": {"wood": 1},
            "contract": {"material": "stone", "quantity": 2, "duration_ticks": 100, "completion_when": False}}}}
        _, objects = self.publish("A", definition, ["pact"])
        target = objects["pact"]
        self.approach("A", target)
        body_lot = next(lot for lot in self.entity(target)["matter"] if lot["material"] == "wood" and lot["slot"] == "body")
        self.interact("A", target, "ignite", item_id=self.item("A", "打火石")["id"], lot_id=body_lot["id"])
        self.approach("B", target)
        offered = self.call("A", "grove.rule_action", {"action": "offer", "target_id": target,
            "expected_target_version": self.entity(target)["version"], "expected_player_version": self.view()["player"]["version"],
            "other_id": self.roles["B"], "expected_other_version": self.view("B")["player"]["version"]})
        terms = offered["entity"]["state"]["program"]["contract"]["terms_version"]
        self.action("A", target, "accept", terms_version=terms)
        self.action("B", target, "accept", terms_version=terms)
        self.action("B", target, "deliver", item_id=self.item("B", "打火石")["id"], amount=2)
        for _ in range(25):
            self.ticks(1)
            if self.entity(target)["lifecycle"] == "broken":
                break
        broken = self.entity(target)
        self.assertEqual(broken["lifecycle"], "broken")
        self.assertEqual(broken["state"]["program"]["contract"]["status"], "failed")
        self.assertEqual(broken["state"]["program"]["contract"]["escrow_remaining"], 2)
        recovered = self.action("B", target, "release", item_id=self.item("B", "水壶")["id"])
        self.assertEqual(recovered["released_quantity"], 2)
        self.assertEqual(recovered["lost_quantity"], 0)
        with self.assertRaises(RuleViolation):
            self.action("B", target, "release", item_id=self.item("B", "水壶")["id"])

    def test_delivery_predicate_binds_public_state_and_requires_new_transition(self):
        _, devices = self.publish("A", package(), ["source", "gear", "gate"])
        wood = self.item("A", "木柴")["id"]
        self.action("A", devices["source"], "connect", other_id=devices["gear"])
        self.action("A", devices["gear"], "connect", other_id=devices["gate"])
        self.action("A", devices["source"], "fuel", item_id=wood, amount=12)
        self.action("A", devices["source"], "start")
        self.ticks(2)
        gate = self.entity(devices["gate"])
        self.assertFalse(gate["components"]["barrier"]["active"])
        old_opening = gate["state"]["program"]["barrier_opened_sequence"]
        predicate = {"op": "all", "args": [
            {"object": {"id": devices["gate"], "field": "intact"}},
            {"op": "not", "value": {"object": {"id": devices["gate"], "field": "barrier.active"}}},
            {"op": "gt", "left": {"object": {"id": devices["gate"], "field": "barrier.opened_sequence"}},
             "right": {"contract": "started_event_sequence"}},
            {"op": "eq", "left": {"object": {"id": devices["source"], "field": "mechanical.speed"}}, "right": 0}]}
        definition = {"key": "public-condition", "name": "公共状态交付条件", "version": 1,
            "types": {"pact": {"name": "有序交付", "body_material": "stone", "cost": {"stone": 1},
                "contract": {"material": "wood", "quantity": 2, "duration_ticks": 100, "delivery_when": predicate}}}}
        self.time.return_value += 31  # Wait for the real publication lease, not player progress.
        self.world.run_due_timers("w")
        ref, objects = self.publish("B", definition, ["pact"])
        target = objects["pact"]
        self.approach("B", target)
        self.approach("A", target)
        offered = self.action("A", target, "offer", other_id=self.roles["B"])
        terms = offered["entity"]["state"]["program"]["contract"]["terms_version"]
        self.action("A", target, "accept", terms_version=terms)
        self.action("B", target, "accept", terms_version=terms)
        started = self.entity(target)["state"]["program"]["contract"]["started_event_sequence"]
        self.assertGreaterEqual(started, old_opening)
        payer = self.item("B", "木柴")["id"]
        remaining = self.entity(payer)["matter"]
        with self.assertRaisesRegex(RuleViolation, "交付条件当前未满足"):
            self.action("B", target, "deliver", item_id=payer, amount=2)
        self.assertEqual(self.entity(payer)["matter"], remaining)
        self.ticks(1)  # More actual movement while still open is not a new opening.
        self.assertEqual(self.entity(devices["gate"])["state"]["program"]["barrier_opened_sequence"], old_opening)
        with self.assertRaises(RuleViolation):
            self.action("B", target, "deliver", item_id=payer, amount=2)
        self.assertEqual(self.entity(target)["state"]["program"]["contract"]["delivered"], 0)
        # Reverse a real network to close, then remesh the two-gear route to open.
        self.action("A", devices["gear"], "disconnect", other_id=devices["gate"])
        self.action("A", devices["source"], "connect", other_id=devices["gate"])
        self.ticks(3)
        self.assertTrue(self.entity(devices["gate"])["components"]["barrier"]["active"])
        self.action("A", devices["source"], "disconnect", other_id=devices["gate"])
        self.action("A", devices["gear"], "connect", other_id=devices["gate"])
        self.approach("B", target)
        # Keep the actual delivery intent read before a moving network step.
        # An unrelated event-log cursor must not make the contract stale;
        # the transaction must reach its latest-state delivery predicate.
        before = self.entity(target)
        delivery = {"action": "deliver", "target_id": target,
            "expected_target_version": before["version"],
            "expected_player_version": self.view("B")["player"]["version"],
            "item_id": payer, "expected_item_version": self.entity(payer)["version"], "amount": 2}
        journal_before = self.world.get_state("w", m.META, "creative_events")["value"]["sequence"]
        self.ticks(2)
        self.assertGreater(self.world.get_state("w", m.META, "creative_events")["value"]["sequence"], journal_before)
        self.assertEqual(self.entity(target)["version"], before["version"])
        self.assertEqual(self.entity(target)["state"]["program"]["event_cursor"], before["state"]["program"]["event_cursor"])
        self.assertGreater(self.entity(devices["gate"])["state"]["program"]["barrier_opened_sequence"], started)
        self.assertNotEqual(self.entity(devices["source"])["state"]["program"]["mechanical"]["speed"], 0)
        with self.assertRaisesRegex(RuleViolation, "交付条件当前未满足"):
            self.call("B", "grove.rule_action", delivery)
        self.assertEqual(self.entity(payer)["matter"], remaining)
        self.action("A", devices["source"], "stop")
        self.ticks(1)
        self.approach("A", devices["gate"])
        self.interact("A", devices["gate"], "pickup")
        with self.assertRaisesRegex(RuleViolation, "无法读取公开事实"):
            self.action("B", target, "deliver", item_id=payer, amount=2)
        self.assertEqual(self.entity(payer)["matter"], remaining)
        self.interact("A", devices["gate"], "drop")
        result = self.action("B", target, "deliver", item_id=payer, amount=2)
        self.assertEqual(result["entity"]["state"]["program"]["contract"]["status"], "complete")
        self.assertEqual(self.read("A", "grove.definition_read", {"definition_ref": ref})["definition"]["types"]["pact"]["contract"]["delivery_when"], predicate)
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", make_world())
        self.assertEqual(self.read("A", "grove.inspect", {"entity_id": target})["entity"]["state"]["program"]["contract"]["status"], "complete")

    def test_mechanical_ratio_cycle_overload_and_energy_boundaries(self):
        nodes = [{"id": "s", "mechanical": {"role": "source", "teeth": 16, "torque": 8, "speed": 1, "energy_per_step": 1}, "state": {"energy": 3, "position": 0}},
                 {"id": "g", "mechanical": {"role": "gear", "teeth": 32}, "state": {}},
                 {"id": "a", "mechanical": {"role": "actuator", "teeth": 16, "pitch": 2, "load": 2, "travel": 20}, "state": {"position": 3}}]
        edges = [{"source": "s", "target": "g", "meshed": True}, {"source": "g", "target": "a", "meshed": True}]
        running = solve_mechanical(nodes, edges)
        self.assertEqual(running["a"]["position"], 5)
        self.assertEqual(sum(result["energy_used"] for result in running.values()), 1)
        reverse = solve_mechanical(list(reversed(nodes)), list(reversed(edges)))
        self.assertEqual(running, reverse)
        nodes[2]["mechanical"]["teeth"] = 32
        self.assertEqual(solve_mechanical(nodes, edges)["a"]["position"], 4)
        nodes[2]["mechanical"]["load"] = 20
        stopped = solve_mechanical(nodes, edges)
        self.assertTrue(all(result["status"] == "overloaded" for result in stopped.values()))
        self.assertEqual(stopped["a"]["position"], 3)
        nodes[2]["mechanical"]["load"] = 2
        bad = solve_mechanical(nodes, [*edges, {"source": "a", "target": "s", "meshed": True}])
        self.assertTrue(all(result["status"] == "inconsistent_cycle" for result in bad.values()))
        nodes[0]["state"]["energy"] = 0
        self.assertEqual(solve_mechanical(nodes, edges)["s"]["energy_used"], 0)
        self.assertEqual(solve_mechanical(nodes, edges)["a"]["position"], 3)

        # The formal world path retains every member's actual position and
        # stored energy when the whole physical network cannot run.
        definition = package()
        definition['key'] = 'jammed-network'
        definition['types'] = {key: definition['types'][key] for key in ('source', 'gear', 'gate')}
        _, objects = self.publish('A', definition, ['source', 'gear', 'gate'])
        for first, second in (('source', 'gear'), ('gear', 'gate'), ('gate', 'source')):
            self.action('A', objects[first], 'connect', other_id=objects[second])
        self.action('A', objects['source'], 'fuel', item_id=self.item('A', '木柴')['id'], amount=3)
        self.action('A', objects['source'], 'start')
        before = {key: deepcopy(self.entity(eid)['state']['program']['mechanical']) for key, eid in objects.items()}
        material_before = self.world.get_state('w', m.META, 'matter_accounting')
        self.assertIsNotNone(material_before)
        self.ticks(1)
        for key, eid in objects.items():
            current = self.entity(eid)['state']['program']['mechanical']
            self.assertEqual(current['status'], 'inconsistent_cycle')
            self.assertEqual(current['speed'], 0)
            for field in ('position', 'angle', 'energy'):
                self.assertEqual(current[field], before[key][field])
        self.assertEqual(self.world.get_state('w', m.META, 'matter_accounting'), material_before)

    def test_authoritative_event_backpressure_refuses_without_losing_unconsumed_facts(self):
        definition = {'key': 'event-budget', 'name': '公开动作计数', 'version': 1, 'types': {'counter': {
            'name': '计数石', 'body_material': 'stone', 'cost': {'stone': 1},
            'fields': {'count': {'type': 'integer', 'default': 0, 'min': 0, 'max': 1000}},
            'recorder': {'radius': 8, 'window_ticks': 50},
            'rules': [{'id': 'moves', 'phase': 'after',
                'when': {'op': 'eq', 'left': {'event': 'action'}, 'right': 'move'},
                'effects': [{'op': 'set', 'field': 'count',
                    'value': {'op': 'add', 'left': {'field': 'count'}, 'right': 1}}]}]}}}
        _, objects = self.publish('A', definition, ['counter'])
        target = objects['counter']
        self.approach('A', target)
        self.ticks(1)
        count_before = self.entity(target)['state']['program']['fields']['count']
        player = self.view()['player']
        direction = next((dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                         if (player['x']+dx, player['y']+dy) != (self.entity(target)['x'], self.entity(target)['y']))
        for index in range(creative.MAX_EVENTS - 164):
            delta = 1 if index % 2 == 0 else -1
            player = self.view()['player']
            self.call('A', 'grove.move', {'dx': direction[0]*delta, 'dy': direction[1]*delta,
                                        'expected_player_version': player['version']})
        journal = self.world.get_state('w', m.META, 'creative_events')
        player = self.view()['player']
        with self.assertRaises(StateConflict):
            self.call('A', 'grove.move', {'dx': direction[0], 'dy': direction[1],
                                        'expected_player_version': player['version']})
        self.assertEqual(self.view()['player'], player)
        self.assertEqual(self.world.get_state('w', m.META, 'creative_events'), journal)
        self.ticks(1)
        state = self.entity(target)['state']['program']
        self.assertEqual(state['fields']['count'], count_before + creative.MAX_EVENTS - 164)
        self.assertEqual(state['event_cursor'], journal['value']['sequence'])
        self.call('A', 'grove.move', {'dx': direction[0], 'dy': direction[1],
                                    'expected_player_version': self.view()['player']['version']})
        self.assertEqual(self.world.get_state('w', m.META, 'creative_events')['value']['sequence'], journal['value']['sequence']+1)


    def test_capacity_limits_positive_reaction_without_reporting_a_blocker(self):
        definition = {"key": "capacity-limited", "name": "有限容纳反应", "version": 1,
            "materials": {"granules": {"name": "粒料", "feedstock": "water", "load_per_u": 2,
                "liquid": False, "combustible": False}},
            "types": {"reactor": {"name": "扩容反应器", "body_material": "stone",
                "cost": {"stone": 1}, "container_capacity": 6,
                "rules": [{"id": "expand", "phase": "reaction", "when": True,
                    "effects": [{"op": "react", "inputs": {"granules": 1},
                        "outputs": {"wood": 2}, "rate": 4}]}]}}}
        _, objects = self.publish("A", definition, ["reactor"])
        target = objects["reactor"]
        self.action("A", target, "charge", material="granules", amount=4,
                    item_id=self.item("A", "水壶")["id"])
        observed = self.read("A", "grove.inspect", {"entity_id": target})["entity"]["creative"]
        reaction = observed["reaction_observations"][0]
        self.assertEqual(reaction["isolated_extent"], 2)
        self.assertTrue(reaction["capacity_limited"])
        self.assertEqual(reaction["blockers"], [])
        self.ticks(1)
        self.assertEqual(creative._quantities(self.entity(target)), {"granules": 2, "wood": 4})
        blocked = self.read("A", "grove.inspect", {"entity_id": target})["entity"]["creative"]["reaction_observations"][0]
        self.assertEqual(blocked["isolated_extent"], 0)
        self.assertIn("capacity", blocked["blockers"])

    def test_current_inputs_exclude_whole_children_and_mark_history(self):
        _, objects = self.publish("A", package(), ["mixer"])
        target = objects["mixer"]
        wood = self.item("A", "木柴")["id"]
        for symbol in ("amber", "blue"):
            self.action("A", target, "charge", material=symbol, amount=1, item_id=wood)
        self.interact("A", target, "store", item_id=wood)
        observed = self.read("A", "grove.inspect", {"entity_id": target})
        inputs = observed["entity"]["creative"]["reaction_inputs"]
        self.assertEqual(inputs["quantities"], {"amber": 1, "blue": 1})
        self.assertFalse(inputs["whole_children_are_inputs"])
        self.assertEqual(observed["contents"][0]["id"], wood)
        self.assertNotIn("materials", observed["entity"]["creative"])
        self.ticks(1)
        self.ticks(1)
        current = self.read("A", "grove.inspect", {"entity_id": target})["entity"]["creative"]
        self.assertEqual(current["reaction_inputs"]["quantities"], {"resin": 2})
        self.assertEqual(current["process"]["status"], "waiting")
        self.assertTrue(current["process"]["last_reaction"]["historical"])
        self.assertFalse(current["process"]["last_reaction"]["current_production"])
        reaction = current["reaction_observations"][0]
        self.assertEqual(reaction["isolated_extent"], 0)
        self.assertIn("insufficient_inputs", reaction["blockers"])
        self.assertEqual([item["missing_for_one_extent"] for item in reaction["inputs"]], [1, 1])
        public = next(item for item in self.view()["entities"] if item["id"] == target)
        self.assertNotIn("reaction_observations", public["creative"])
        self.interact("A", target, "close")
        closed = self.read("B", "grove.inspect", {"entity_id": target})["entity"]
        self.assertTrue(closed["creative"]["hidden"])
        self.assertNotIn("reaction_inputs", closed["creative"])

    def test_cross_author_material_identity_reaction_and_relocated_instance(self):
        original = package()
        ref, objects = self.publish("A", original, ["mixer"])
        self.time.return_value += 31  # operational regional publication cooldown
        self.world.run_due_timers("w")
        importer = {"key": "finisher", "name": "另一人的加工", "version": 1,
            "materials": {"coat": {"name": "绿树脂", "feedstock": "wood", "load_per_u": 1,
                                    "liquid": True, "combustible": False}},
            "material_imports": {"input": {"definition_ref": ref, "symbol": "resin"}},
            "types": {"consumer": {"name": "独立加工器", "body_material": "stone", "cost": {"stone": 1},
                "container_capacity": 20, "rules": [{"id": "finish", "phase": "reaction", "when": True,
                    "effects": [{"op": "react", "inputs": {"input": 1}, "outputs": {"coat": 1}, "rate": 1}]}]}}}
        other_ref, others = self.publish("B", importer, ["consumer"])
        consumer = others["consumer"]
        self.assertNotEqual(ref, other_ref)
        self.assertEqual(self.entity(consumer)["state"]["program"]["material_aliases"], {"input": ref+"#resin"})
        # Identical display names do not provide the imported substance.
        self.action("B", consumer, "charge", material="coat", amount=2, item_id=self.item("B", "木柴")["id"])
        self.ticks(1)
        self.assertEqual(creative._quantities(self.entity(consumer)), {"coat": 2})
        wood = self.item("A", "木柴")["id"]
        self.action("A", objects["mixer"], "charge", material="amber", amount=1, item_id=wood)
        self.action("A", objects["mixer"], "charge", material="blue", amount=2, item_id=wood)
        self.ticks(1)
        vessel = self.item("B", "水壶")["id"]
        def transfer(symbol):
            source = self.entity(objects["mixer"])
            lot = next(lot for lot in source["matter"] if lot["material"] == ref+"#"+symbol)
            self.approach("B", source["id"])
            self.interact("B", vessel, "transfer", item_id=source["id"], lot_id=lot["id"], amount=1)
            self.approach("B", consumer)
            carried = next(lot for lot in self.entity(vessel)["matter"] if lot["material"] == ref+"#"+symbol)
            self.interact("B", consumer, "transfer", item_id=vessel, lot_id=carried["id"], amount=1)
        # A legitimate unreferenced material remains real ballast, not a rule error.
        transfer("blue")
        self.ticks(1)
        self.assertNotEqual(self.entity(consumer)["state"]["program"]["process"]["status"], "error")
        self.assertEqual(creative._quantities(self.entity(consumer)), {"coat": 2, ref+"#blue": 1})
        transfer("resin")
        inspected = self.read("B", "grove.inspect", {"entity_id": consumer})["entity"]["creative"]
        self.assertEqual(inspected["reaction_observations"][0]["inputs"][0]["material_id"], ref+"#resin")
        self.ticks(1)
        self.assertEqual(creative._quantities(self.entity(consumer)), {"coat": 3, ref+"#blue": 1})
        before = deepcopy(self.entity(consumer)["state"]["program"])
        updated = deepcopy(original)
        updated["version"] = 2
        updated["materials"]["resin"]["name"] = "另一版树脂"
        updated_ref = self.call("A", "grove.definition_publish", {"definition": updated})["definition_ref"]
        self.assertNotEqual(ref, updated_ref)
        self.assertEqual(self.entity(consumer)["state"]["program"], before)
        self.interact("B", consumer, "pickup")
        scene = self.view("B")
        player = scene["player"]
        tiles = {(chunk["cx"]*16+index%16, chunk["cy"]*16+index//16): tile
                 for chunk in scene["chunks"] for index, tile in enumerate(chunk["tiles"])}
        blocked = {(item["x"], item["y"]) for item in scene["entities"]
                   if item.get("barrier_active") or "blocking" in item["capabilities"]}
        destination = next((dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                           if tiles.get((player["x"]+dx, player["y"]+dy), "rock") != "rock"
                           and (player["x"]+dx, player["y"]+dy) not in blocked)
        self.call("B", "grove.move", {"dx": destination[0], "dy": destination[1], "expected_player_version": player["version"]})
        self.interact("B", consumer, "drop")
        self.assertEqual(self.entity(consumer)["state"]["program"]["material_aliases"], {"input": ref+"#resin"})
        self.time.return_value += 31  # publication lease only; no experience claim
        self.world.run_due_timers("w")
        _, rebuilt = self.publish("B", importer, ["consumer"])
        self.assertEqual(self.entity(rebuilt["consumer"])["state"]["program"]["material_aliases"], {"input": ref+"#resin"})
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", make_world())
        self.assertEqual(creative._quantities(self.entity(consumer)), {"coat": 3, ref+"#blue": 1})

    def test_transitive_import_pins_origin_and_duplicate_identity_is_rejected(self):
        ref = self.call("A", "grove.definition_publish", {"definition": package()})["definition_ref"]
        middle = {"key": "relay", "name": "中间定义", "version": 1,
            "material_imports": {"shared": {"definition_ref": ref, "symbol": "resin"}},
            "types": {"holder": {"name": "容器", "body_material": "stone", "cost": {"stone": 1}, "container_capacity": 8}}}
        relay = self.call("B", "grove.definition_publish", {"definition": middle})["definition_ref"]
        final = deepcopy(middle)
        final["key"] = "final"
        final["material_imports"] = {"x": {"definition_ref": relay, "symbol": "shared"}}
        final_ref = self.call("A", "grove.definition_publish", {"definition": final})["definition_ref"]
        pinned = self.read("B", "grove.definition_read", {"definition_ref": final_ref})["definition"]
        self.assertEqual(pinned["material_imports"], {"x": {"definition_ref": ref, "symbol": "resin"}})
        final["version"] = 2
        final["material_imports"]["y"] = {"definition_ref": ref, "symbol": "resin"}
        with self.assertRaisesRegex(RuleViolation, "multiple aliases"):
            self.call("A", "grove.definition_publish", {"definition": final})
        invalid = deepcopy(middle)
        invalid["key"] = "bad_mass"
        invalid["types"]["holder"]["rules"] = [{"id": "bad", "phase": "reaction", "when": True,
            "effects": [{"op": "react", "inputs": {"shared": 1}, "outputs": {"stone": 1}, "rate": 1}]}]
        with self.assertRaisesRegex(RuleViolation, "mass|load"):
            self.call("B", "grove.definition_publish", {"definition": invalid})

    def test_one_step_coalesces_event_and_recorder_writes_with_ordered_facts(self):
        definition = package()
        for kind in ("mixer", "polisher"):
            definition["types"][kind]["recorder"] = {"radius": 8, "window_ticks": 100}
        _, objects = self.publish("A", definition, ["mixer", "polisher", "echo"])
        wood = self.item("A", "木柴")["id"]
        for symbol in ("amber", "blue"):
            self.action("A", objects["mixer"], "charge", material=symbol, amount=1, item_id=wood)
        self.action("A", objects["polisher"], "charge", material="resin", amount=1, item_id=wood)
        for eid in objects.values():
            self.action("A", eid, "start_recording")
        versions = {eid: self.world.get_state("w", creative.RECORDS, eid)["version"] for eid in objects.values()}
        journal = self.world.get_state("w", m.META, "creative_events")
        self.ticks(1)
        current = self.world.get_state("w", m.META, "creative_events")
        self.assertEqual(current["version"], journal["version"]+1)
        # The public queue retains only unconsumed facts; recorders keep both
        # actual ordered reactions even when no rule consumes that queue.
        self.assertEqual(current["value"]["sequence"], journal["value"]["sequence"]+2)
        for eid, previous in versions.items():
            record = self.world.get_state("w", creative.RECORDS, eid)
            self.assertEqual(record["version"], previous+1)
            observed = [event for event in record["value"]["history"] if event["sequence"] > journal["value"]["sequence"]]
            self.assertEqual({event["entity_id"] for event in observed}, {objects["mixer"], objects["polisher"]})
            self.assertEqual([event["sequence"] for event in observed],
                             [journal["value"]["sequence"]+1, journal["value"]["sequence"]+2])

    def test_full_definition_and_recorder_capacity_keeps_committing(self):
        # Controlled placement removes travel from this capacity regression.
        # Publication still pays real regional stock through content_submit.
        def visit(ctx, args):
            m.ensure_chunk(ctx, args["cx"], args["cy"])
            player = m.get_player(ctx)
            player.update(x=args["cx"]*16+1, y=args["cy"]*16+1)
            m.save_player(ctx, player)
            return FunctionOutcome({})
        world = make_world()
        fixture = FunctionSpec("grove.fixture_visit", visit, {"type": "object", "additionalProperties": False,
            "required": ["cx", "cy"], "properties": {"cx": {"type": "integer"}, "cy": {"type": "integer"}}}, access="write")
        self.db = self.db.with_name("capacity.sqlite3")
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", replace(world, functions=(*world.functions, fixture)))
        self.roles = {name: self.world.create_role(name)["role_id"] for name in "AB"}
        for name in self.roles:
            self.call(name, "grove.join", {"name": name})
        definitions = []
        for index in range(32):
            definition = {"key": "observer_"+str(index), "name": "独立记录定义", "version": 1,
                "types": {"echo": {"name": "真实事件记录器", "body_material": "wood", "cost": {"wood": 1},
                    "recorder": {"radius": 8, "window_ticks": 3600}}}}
            definitions.append(self.call("A", "grove.definition_publish", {"definition": definition})["definition_ref"])
        self.assertEqual(len(self.read("A", "grove.rule_catalog")["definitions"]), 32)
        regions = [(0, 0)] + [(x, y) for y in range(-3, 4) for x in range(-3, 4)
            if (x, y) != (0, 0) and m.chunk_landscape(x, y) == "willow_grove"][:3]
        self.assertEqual(len(regions), 4)
        ids = []
        for cx, cy in regions:
            self.call("A", "grove.fixture_visit", {"cx": cx, "cy": cy})
            for _ in range(2):
                self.time.return_value += 31  # Only the publication cooldown, not elapsed play evidence.
                context = self.call("A", "grove.generation_context", {"kind": "layout", "reason": "user_request"})
                opportunity = context["opportunity"]
                specs = [{"definition_ref": definitions[len(ids)+i], "type_id": "echo", **tile}
                         for i, tile in enumerate(context["allowed_tiles"][:4])]
                result = self.call("A", "grove.content_submit", {"opportunity_id": opportunity["id"],
                    "expected_opportunity_version": opportunity["version"], "content": {"kind": "layout", "objects": specs}})
                ids.extend(entity["id"] for entity in result["entities"])
        self.assertEqual(len(ids), 32)
        # Histories originate from actual public actions. Reading repeatedly
        # must remain bounded, and a subsequent world step must still commit.
        for eid in ids:
            region = m.chunk_for(self.entity(eid)["x"], self.entity(eid)["y"])
            if m.chunk_for(self.view()["player"]["x"], self.view()["player"]["y"]) != region:
                self.call("A", "grove.fixture_visit", {"cx": region[0], "cy": region[1]})
            self.action("A", eid, "start_recording")
        for _ in range(20):
            self.action("A", ids[-1], "read_records")
        for eid in ids:
            record = self.world.get_state("w", creative.RECORDS, eid)
            if record["exists"]:
                self.assertLessEqual(encoded_size(record["value"]), creative.MAX_RECORD_BYTES)
        self.ticks(2)
        import sqlite3
        with sqlite3.connect(self.db) as connection:
            count, size = connection.execute("SELECT COUNT(*), SUM(length(CAST(COALESCE(before_json,'') AS BLOB))"
                "+length(CAST(value_json AS BLOB))) FROM state_changes WHERE commit_seq=(SELECT MAX(seq) FROM world_commits)").fetchone()
        self.assertLessEqual(count, 512)
        self.assertLessEqual(size, 2*1024*1024)

        # Independent material identities consume separate accounting rows.
        # The ninth full-capability instance exceeds the combination budget,
        # while the 32-instance count and regional stock still allow it.
        self.db = self.db.with_name("combination-capacity.sqlite3")
        self.world = WorldRuntime(self.db)
        self.world.install_world("w", replace(world, functions=(*world.functions, fixture)))
        self.roles = {name: self.world.create_role(name)["role_id"] for name in "AB"}
        for name in self.roles:
            self.call(name, "grove.join", {"name": name})
        refs = []
        for index in range(9):
            definition = {"key": "combined_"+str(index), "name": "独立复合机制", "version": 1,
                "materials": {symbol: {"name": symbol, "feedstock": "wood", "load_per_u": 1,
                    "liquid": True, "combustible": False} for symbol in ("a", "b", "c", "d")},
                "types": {"combined": {"name": "复合物件", "body_material": "wood", "cost": {"wood": 1},
                    "container_capacity": 20, "mechanical": {"role": "source", "teeth": 16, "torque": 4, "speed": 1},
                    "recorder": {"radius": 8, "window_ticks": 3600},
                    "contract": {"material": "wood", "quantity": 1, "duration_ticks": 3600},
                    "rules": [{"id": left+right, "phase": "reaction", "when": True, "effects": [
                        {"op": "react", "inputs": {left: 1}, "outputs": {right: 1}, "rate": 1,
                         "mode": "independent", "channel": left+right}]}
                        for left, right in (("a", "b"), ("b", "a"), ("c", "d"), ("d", "c"))]}}}
            refs.append(self.call("A", "grove.definition_publish", {"definition": definition})["definition_ref"])
        combined_ids = []
        for index in range(0, 8, 2):
            self.time.return_value += 31
            context = self.call("A", "grove.generation_context", {"kind": "layout", "reason": "user_request"})
            self.assertIsNotNone(context["opportunity"], (index, context["availability"], context["budget_remaining"]))
            result = self.call("A", "grove.content_submit", {"opportunity_id": context["opportunity"]["id"],
                "expected_opportunity_version": context["opportunity"]["version"], "content": {"kind": "layout", "objects": [
                    {"definition_ref": ref, "type_id": "combined", **tile}
                    for ref, tile in zip(refs[index:index+2], context["allowed_tiles"])]}})
            combined_ids.extend(entity["id"] for entity in result["entities"])
        self.call("A", "grove.fixture_visit", {"cx": regions[1][0], "cy": regions[1][1]})
        self.time.return_value += 31
        context = self.call("A", "grove.generation_context", {"kind": "layout", "reason": "user_request"})
        with sqlite3.connect(self.db) as connection:
            before = connection.execute("SELECT scope,state_key,value_json,version FROM world_state ORDER BY scope,state_key").fetchall()
        with self.assertRaisesRegex(RuleViolation, "提交容量"):
            self.call("A", "grove.content_submit", {"opportunity_id": context["opportunity"]["id"],
                "expected_opportunity_version": context["opportunity"]["version"], "content": {"kind": "layout", "objects": [
                    {"definition_ref": refs[-1], "type_id": "combined", **context["allowed_tiles"][0]}]}})
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT scope,state_key,value_json,version FROM world_state ORDER BY scope,state_key").fetchall(), before)
        self.assertEqual(len(self.world.get_state("w", m.META, "creative_instances")["value"]), 8)
        self.call("A", "grove.fixture_visit", {"cx": 0, "cy": 0})
        wood = self.item("A", "木柴")["id"]
        for eid in combined_ids:
            for symbol in ("a", "b", "c", "d"):
                self.action("A", eid, "charge", material=symbol, amount=1, item_id=wood)
            self.action("A", eid, "fuel", amount=1, item_id=wood)
            self.action("A", eid, "start")
            self.action("A", eid, "start_recording")
        self.ticks(2)
        for eid in combined_ids:
            self.assertEqual(creative._quantities(self.entity(eid)), {"a": 1, "b": 1, "c": 1, "d": 1})
            self.assertEqual(self.entity(eid)["state"]["program"]["process"]["status"], "running")
            record = self.world.get_state("w", creative.RECORDS, eid)["value"]
            self.assertTrue(any(event["kind"] == "process" for event in record["history"]))
            self.assertLessEqual(encoded_size(record), creative.MAX_RECORD_BYTES)

    def test_world_upgrade_hands_off_clock_without_advancing_saved_state(self):
        db = self.db.with_name("upgrade.sqlite3")
        world = WorldRuntime(db)
        world.install_world("w", replace(make_world(), version=10, state_version=5, migrations={}))
        role = world.create_role("returning")["role_id"]
        world.call_function("w", "grove.join", role, {"name": "returning"}, operation_id="upgrade-join")
        self.time.return_value += 1
        self.assertEqual(world.run_due_timers("w")["count"], 1)
        player = world.get_state("w", m.PLAYERS, role)
        clock = world.get_state("w", m.META, "clock")["value"]
        old_id = f"grove.tick.{clock['epoch']}.{clock['sequence']}"
        world = WorldRuntime(db)
        world.install_world("w", make_world())
        resumed = world.get_state("w", m.META, "clock")["value"]
        self.assertEqual(resumed["sequence"], clock["sequence"])
        self.assertEqual(resumed["last_tick_at"], clock["last_tick_at"])
        self.assertNotEqual(resumed["epoch"], clock["epoch"])
        self.assertEqual(world.get_state("w", m.PLAYERS, role), player)
        self.assertEqual(world.get_timer("w", old_id)["status"], "cancelled")
        next_id = f"grove.tick.{resumed['epoch']}.{resumed['sequence']}"
        self.assertEqual(world.get_timer("w", next_id)["status"], "pending")
        world.install_world("w", make_world())
        self.assertEqual(world.get_state("w", m.META, "clock")["value"], resumed)
        self.time.return_value += 100  # simulated service outage, not elapsed play
        result = world.run_due_timers("w")
        self.assertEqual(result["count"], 1)
        self.assertEqual(world.get_state("w", m.META, "clock")["value"]["sequence"], clock["sequence"])
        self.time.return_value += 1
        self.assertEqual(world.run_due_timers("w")["count"], 1)
        self.assertEqual(world.get_state("w", m.META, "clock")["value"]["sequence"], clock["sequence"]+1)


if __name__ == "__main__":
    unittest.main()
