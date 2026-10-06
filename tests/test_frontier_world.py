"""Authored places become shared, playable facts with durable rule outcomes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from agent_world import WorldRuntime
from agent_world.connect import connect_world
from agent_world.errors import PermissionDenied, RuleViolation, SchemaRejected, StateConflict
from examples.frontier_app import create_frontier_application, initialize_local, existing_world
from examples.frontier_world import WORLD, CHAPTERS


def package(title="Willow crossing", *, barrier=False):
    return {"title": title, "description": "A willow leans over a narrow ford; a traveller waits beside the path.",
            "facts": ["The ford connects to the path we arrived on."],
            "npcs": [{"name": "The ferryman", "description": "A patient traveller with an empty walking stick."}],
            "objects": [{"name": "A tied travel case", "description": "One case, with no currency reward.", "kind": "cache"},
                        {"name": "Fallen willow" if barrier else "Boundary inscription",
                         "description": "A willow blocks the forward path." if barrier else "A worn inscription beside the ford.",
                         "kind": "barrier" if barrier else "inscription",
                         **({"blocks_exit": 0} if barrier else {"discovery_text": "The upstream trail returns along the north bank; travellers should mark the ford before leaving."})}],
            "exits": [{"label": "Upstream trail", "description": "An uncharted trail follows the river."}]}


class FrontierWorldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontier-rules-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.db)
        self.roles = {name: self.runtime.create_role(name)["role_id"] for name in ("A", "B", "C")}
        self.runtime.install_world("test", WORLD)
        self.n = 0
        for actor in self.roles:
            self.call(actor, "join", {"name": actor, "class_id": "fighter" if actor == "A" else "rogue"})

    def call(self, actor, function, args=None, operation=None):
        self.n += 1
        return self.runtime.call_function("test", "frontier." + function, self.roles[actor], args or {},
                                          operation_id=operation or f"test-{self.n}")

    def query(self, actor, function, args=None):
        return self.runtime.query_function("test", "frontier." + function, self.roles[actor], args or {})["result"]

    def look(self, actor="A"):
        return self.query(actor, "look")

    def move(self, actor, destination):
        return self.call(actor, "move", {"destination_id": destination,
                         "expected_character_version": self.look(actor)["character"]["version"]})["result"]

    def expand(self, actor="A", spec=None, operation=None):
        self.move(actor, "bell-market")
        seen = self.look(actor)
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        args = {"scene_id": seen["scene"]["id"], "exit_id": exit["id"],
                "expected_exit_version": exit["version"], "package": spec or package()}
        return self.call(actor, "expand", args, operation)["result"], args

    def object_action(self, actor, action, object_id, operation=None):
        seen = self.look(actor)
        obj = next(o for o in seen["scene"]["objects"] if o["id"] == object_id)
        return self.call(actor, action, {"object_id": object_id, "expected_object_version": obj["version"],
                         "expected_character_version": seen["character"]["version"]}, operation)

    def request(self, actor="A", request_id="method", target="market-cache"):
        obj = self.query(actor, "examine", {"target_id": target})["target"]
        return self.call(actor, "request", {"request_id": request_id, "target_id": target,
                         "expected_target_version": obj["version"], "text": "Untie the existing copper clasp by hand."})

    def offer(self, actor="B", request_id="method", operation=None):
        return self.call(actor, "answer", {"request_id": request_id, "decision": "allow", "reason": "Use the established clasp; no extra materials.",
                         "ability": "dexterity", "dc": 12, "effect": "resolve_object"}, operation)

    def test_current_scene_creation_continues_a_dead_end_without_rewriting_old_facts(self):
        spec = package()
        spec["exits"] = []
        created, _ = self.expand(spec=spec)
        scene_id = created["created_scene"]["id"]
        self.move("A", scene_id)
        self.move("B", "bell-market")
        self.move("B", scene_id)
        original = self.look()["scene"]
        old_cache = next(o for o in original["objects"] if o["kind"] == "cache")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0):
            self.object_action("A", "check", old_cache["id"])
        attempts = self.look()["character"]["attempts"]
        args = {"scene_id": scene_id, "expected_scene_version": original["version"], "objects": [
            {"name": "A readable water marker", "description": "A new participant-authored note beside the faded marks.",
             "kind": "inscription", "initial_state": "read", "discovery_text": "This note was composed during this visit, not a recovered old secret."},
            {"name": "A small token", "description": "One object a visitor can take or leave.",
             "kind": "cache", "initial_state": "open", "discovery_text": "A newly authored keepsake, with no extra numerical power."}]}
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("Appending public content does not roll")):
            added = self.call("A", "add_objects", args, operation="current-scene-note")["result"]
        self.assertEqual(len(added["created_objects"]), 2)
        self.assertEqual(added["author"], self.roles["A"])
        self.assertEqual(added["authorship_kind"], "present_participant_creation")
        after = self.look("B")["scene"]
        for field in ("title", "authored_description", "facts", "exits", "npcs", "creator", "created_at"):
            self.assertEqual(after[field], original[field])
        self.assertGreater(after["version"], original["version"])
        self.assertEqual(len(after["objects"]), 4)
        for obj in added["created_objects"]:
            self.assertEqual(obj["creator"], self.roles["A"])
            self.assertEqual(obj["discovery"]["status"], "revealed")
            self.assertIsNone(obj["discovery"]["discovered_by"])
        preserved = self.query("A", "examine", {"target_id": old_cache["id"]})["target"]
        self.assertEqual(preserved, old_cache)
        with self.assertRaises(RuleViolation):
            self.object_action("A", "check", old_cache["id"])
        replay = self.call("A", "add_objects", args, operation="current-scene-note")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], added)
        with self.assertRaises(StateConflict):
            self.call("B", "add_objects", args)
        forbidden = dict(args, expected_scene_version=after["version"], objects=[{
            "name": "No old exit modification", "description": "A barrier is outside this creation ability.",
            "kind": "barrier", "initial_state": "obstructed", "blocks_exit": 0}])
        with self.assertRaises(SchemaRejected):
            self.call("A", "add_objects", forbidden)
        portable = next(o for o in added["created_objects"] if o["kind"] == "cache")
        self.object_action("B", "take", portable["id"])
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        self.assertEqual(self.look("B")["character"]["inventory"], [portable["id"]])
        self.assertEqual(self.look()["character"]["attempts"], attempts)
        self.assertEqual(self.query("A", "examine", {"target_id": old_cache["id"]})["target"], old_cache)

    def test_public_authored_objects_enable_other_player_choices_without_reroll(self):
        self.move("A", "bell-market")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0):
            failure = self.object_action("A", "check", "market-cache")["result"]
        old_attempts = self.look()["character"]["attempts"]
        self.assertFalse(failure["result"]["success"])
        self.move("A", "elm-inn")
        spec = package()
        spec["objects"][0].update(initial_state="open", discovery_text="A single traveller's keepsake, left here to be carried or left behind.")
        spec["objects"][1]["initial_state"] = "read"
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("Public creation does not roll")):
            created, args = self.expand(spec=spec, operation="public-place")
        destination = created["created_scene"]["id"]
        objects = created["created_scene"]["objects"]
        portable = next(o for o in objects if o["kind"] == "cache")
        writing = next(o for o in objects if o["kind"] == "inscription")
        self.assertEqual(portable["state"], "open")
        self.assertIsNone(portable["owner"])
        self.assertEqual(writing["state"], "read")
        for obj in objects:
            self.assertEqual(obj["creator"], self.roles["A"])
            self.assertEqual(obj["discovery"]["status"], "revealed")
            self.assertIsNone(obj["discovery"]["discovered_by"])
            self.assertIsNone(obj["discovery"]["discovered_at"])
        self.move("B", "bell-market")
        self.move("B", destination)
        actions = self.look("B")["available_actions"]
        self.assertTrue(any(a["function_id"] == "frontier.take" and a["arguments"]["object_id"] == portable["id"] for a in actions))
        self.assertFalse(any(a["function_id"] == "frontier.check" for a in actions))
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("Taking an open object does not roll")):
            self.object_action("B", "take", portable["id"], operation="public-keepsake")
        self.move("A", destination)
        self.assertFalse(any(o["id"] == portable["id"] for o in self.look()["scene"]["objects"]))
        self.assertEqual(self.look("B")["character"]["inventory"], [portable["id"]])
        self.assertEqual(self.look()["character"]["attempts"], old_attempts)
        self.move("A", "bell-market")
        self.assertEqual(self.query("A", "examine", {"target_id": "market-cache"})["target"]["state"], "sealed")
        with self.assertRaises(RuleViolation):
            self.object_action("A", "check", "market-cache")
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        replay = self.call("A", "expand", args, operation="public-place")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"]["created_scene"]["id"], destination)
        self.assertEqual(self.look("B")["character"]["inventory"], [portable["id"]])
        self.assertEqual(self.look()["character"]["attempts"], old_attempts)

    def test_creation_rejects_initial_states_of_another_object_kind(self):
        self.move("A", "bell-market")
        seen = self.look()
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        for kind, state in (("cache", "read"), ("inscription", "open"), ("barrier", "resolved")):
            spec = package(barrier=kind == "barrier")
            obj = spec["objects"][1 if kind != "cache" else 0]
            obj["initial_state"] = state
            with self.assertRaises(SchemaRejected):
                self.call("A", "expand", {"scene_id": seen["scene"]["id"], "exit_id": exit["id"],
                          "expected_exit_version": exit["version"], "package": spec})
            self.assertEqual(next(e for e in self.look()["scene"]["exits"] if e["id"] == exit["id"]), exit)

    def test_authored_scene_is_an_actual_other_player_destination(self):
        result, args = self.expand(spec=package(barrier=True), operation="create-crossing")
        destination = result["created_scene"]["id"]
        scene = self.move("A", destination)["scene"]
        self.move("B", "bell-market")
        self.move("B", destination)
        self.assertEqual(self.look("B")["scene"], scene)
        self.assertEqual(scene["creator"], self.roles["A"])
        self.assertEqual(scene["exits"][0]["destination_id"], "bell-market")
        self.assertEqual(len(scene["npcs"]), 1)
        barrier = next(o for o in scene["objects"] if o["kind"] == "barrier")
        self.assertTrue(scene["exits"][1]["blocked"])
        npc = scene["npcs"][0]
        told = self.call("B", "tell", {"npc_id": npc["id"], "text": "Which way did you come?",
                         "response": "I heard the market bell beyond the ford."})["result"]["entry"]
        self.assertEqual(told["payload"]["response_author"], self.roles["B"])
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            resolved = self.object_action("B", "check", barrier["id"])["result"]
        self.assertEqual(resolved["object"]["state"], "resolved")
        self.assertFalse(self.look("A")["scene"]["exits"][1]["blocked"])
        child_exit = self.look("B")["scene"]["exits"][1]
        child = self.call("B", "expand", {"scene_id": destination, "exit_id": child_exit["id"],
                          "expected_exit_version": child_exit["version"], "package": package("River terrace")})["result"]
        self.move("B", child["created_scene"]["id"])
        self.move("B", destination)
        self.move("B", "bell-market")
        with self.assertRaises(RuleViolation):
            self.call("B", "tell", {"npc_id": npc["id"], "text": "Speaking from another room"})
        replay = self.call("A", "expand", args, "create-crossing")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], result)
        self.assertEqual(self.query("C", "map")["total"], 5)

    def keyed_place(self):
        spec = package(barrier=True)
        spec["objects"][0]["discovery_text"] = "The case holds the sole iron release handle."
        spec["objects"][1].update(key_object=0, discovery_text="The gate releases onto the upstream trail.")
        created, _ = self.expand(spec=spec)
        destination = created["created_scene"]["id"]
        for actor in self.roles:
            if actor != "A":
                self.move(actor, "bell-market")
            self.move(actor, destination)
        scene = self.look()["scene"]
        key, barrier = scene["objects"]
        path = next(e for e in scene["exits"] if e["destination_id"] is None)
        child = self.call("A", "expand", {"scene_id": destination, "exit_id": path["id"],
                         "expected_exit_version": path["version"], "package": package("North terrace")})["result"]
        return destination, key["id"], barrier["id"], child["created_scene"]["id"]

    def unlock_args(self, actor, key_id, barrier_id):
        seen = self.look(actor)
        target = self.query(actor, "examine", {"target_id": barrier_id})["target"]
        return {"object_id": barrier_id, "key_object_id": key_id,
                "expected_character_version": seen["character"]["version"],
                "expected_object_version": target["version"],
                "expected_key_version": target["key_requirement"]["version"]}

    def open_take_key(self, actor, key_id):
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.object_action(actor, "check", key_id)
        self.object_action(actor, "take", key_id)

    def world_rows(self):
        with sqlite3.connect(self.db) as database:
            return database.execute("SELECT scope,state_key,value_json,version,updated_at,deleted FROM world_state WHERE universe='test' ORDER BY scope,state_key").fetchall()

    def test_dropped_keepsake_can_be_taken_by_someone_else_and_spent_for_a_shared_path(self):
        # Reproduce the actual running world's saved initial description,
        # including its obsolete claim that the disc is still unowned.
        with sqlite3.connect(self.db) as database:
            raw = database.execute("SELECT value_json FROM world_state WHERE universe=? AND scope=? AND state_key=?",
                                   ("test", "frontier:scenes", "bell-market")).fetchone()[0]
            scene = json.loads(raw)
            scene["description"] = "一枚渡鸦铜盘嵌在褪色的绒布上，还没有主人。"
            database.execute("UPDATE world_state SET value_json=? WHERE universe=? AND scope=? AND state_key=?",
                             (json.dumps(scene), "test", "frontier:scenes", "bell-market"))
        self.move("A", "bell-market")
        self.object_action("A", "take", "raven-disc")
        market = self.look("A")["scene"]
        # The saved prose still describes the initial unowned disc. Current
        # presence must explicitly contradict that prose after the real take.
        self.assertIn("还没有主人", market["authored_description"])
        self.assertNotIn("description", market)
        self.assertEqual(market["current_ground"]["objects"], [{"id": "market-cache", "name": "铜扣旅行匣", "state": "sealed", "owner": None}])
        self.assertEqual(market["current_ground"]["count"], 1)
        self.assertTrue(market["current_ground"]["complete"])
        held = self.query("A", "examine", {"target_id": "raven-disc"})["target"]
        self.move("A", "elm-inn")
        args = {"object_id": "raven-disc", "expected_object_version": held["version"],
                "expected_character_version": self.look("A")["character"]["version"]}
        dropped = self.call("A", "drop", args, "leave-keepsake")
        self.assertNotIn("raven-disc", self.look("A")["character"]["inventory"])
        self.assertEqual(dropped["result"]["scene"]["current_ground"]["objects"], [
            {"id": "inn-ledger", "name": "旅人簿", "state": "unread", "owner": None},
            {"id": "raven-disc", "name": "渡鸦铜盘", "state": "open", "owner": None}])
        self.move("A", "bell-market")
        self.assertEqual(self.look("A")["scene"]["current_ground"]["count"], 1)
        self.move("A", "moss-edge")
        self.object_action("B", "take", "raven-disc")
        self.assertFalse(any(o["id"] == "raven-disc" for o in self.look("C")["scene"]["objects"]))
        self.assertEqual(self.look("C")["scene"]["current_ground"]["objects"], [
            {"id": "inn-ledger", "name": "旅人簿", "state": "unread", "owner": None}])
        self.assertEqual(self.look("C")["scene"]["current_ground"]["count"], 1)
        local = self.look("C")["recent_scene_activity"]
        self.assertEqual(local["items"][0]["kind"], "take")
        self.assertEqual(local["items"][0]["actor_name"], "B")
        self.assertEqual(local["items"][0]["detail_arguments"], {"after": local["items"][0]["seq"]-1, "limit": 1})
        self.assertFalse(local["scene_history_complete"])
        self.assertIn("当前", self.look("C")["scene"]["narrative_scope"])
        replay = self.call("A", "drop", args, "leave-keepsake")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], dropped["result"])
        self.assertEqual(self.query("B", "examine", {"target_id": "raven-disc"})["target"]["owner"], self.roles["B"])
        with self.assertRaises(PermissionDenied):
            self.call("A", "unlock", self.unlock_args("A", "raven-disc", "forest-seal"))
        self.move("B", "bell-market")
        self.move("B", "moss-edge")
        unlock_args = self.unlock_args("B", "raven-disc", "forest-seal")
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("possession must not roll")):
            unlocked = self.call("B", "unlock", unlock_args, "spend-keepsake")
        result = unlocked["result"]
        self.assertTrue(result["result"]["key_consumed"])
        self.assertNotIn("raven-disc", result["character"]["inventory"])
        self.assertEqual(result["key_object"]["state"], "spent")
        self.assertEqual(result["key_object"]["spent_by"], self.roles["B"])
        self.assertEqual(result["key_object"]["spent_on_barrier_id"], "forest-seal")
        self.assertEqual(result["key_object"]["version"], unlock_args["expected_key_version"] + 1)
        path = next(e for e in self.look("B")["scene"]["exits"] if e["id"] == "forest-east")
        self.assertFalse(path["blocked"])
        destination = self.call("B", "expand", {"scene_id": "moss-edge", "exit_id": path["id"],
            "expected_exit_version": path["version"], "package": package("East-bank hideaway")})["result"]["created_scene"]["id"]
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        replay = self.call("B", "unlock", unlock_args, "spend-keepsake")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], result)
        self.move("C", "bell-market")
        self.assertEqual(self.look("C")["scene"]["current_ground"]["count"], 1)
        self.assertNotIn("raven-disc", [o["id"] for o in self.look("C")["scene"]["current_ground"]["objects"]])
        self.move("C", "moss-edge")
        self.move("C", destination)
        events = self.query("C", "history", {"limit": 50})["items"]
        self.assertEqual(len([e for e in events if e["kind"] == "drop"]), 1)
        self.assertEqual(len([e for e in events if e["kind"] == "unlock"]), 1)

    def test_required_keepsake_cannot_be_replaced_by_a_roll_or_a_free_method(self):
        self.move("A", "bell-market")
        self.move("A", "moss-edge")
        before = self.world_rows()
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("required possession must not roll")):
            with self.assertRaises(RuleViolation):
                self.object_action("A", "check", "forest-seal")
            with self.assertRaises(RuleViolation):
                self.request(target="forest-seal")
        self.assertEqual(self.world_rows(), before)
        self.assertFalse(any(a["function_id"] in {"frontier.check", "frontier.request"} and
                             a.get("arguments", {}).get("object_id", a.get("arguments", {}).get("target_id")) == "forest-seal"
                             for a in self.look()["available_actions"]))
        spec = package(barrier=True)
        spec["objects"][1].update(key_required=True, consume_key=True)
        self.move("A", "bell-market")
        before = self.world_rows()
        exit = next(e for e in self.look()["scene"]["exits"] if e["id"] == "market-road")
        args = {"scene_id": "bell-market", "exit_id": exit["id"], "expected_exit_version": exit["version"], "package": spec}
        with self.assertRaises(RuleViolation):
            self.call("A", "expand", args)
        self.assertEqual(self.world_rows(), before)
        spec["objects"][1]["key_object"] = 0
        created = self.call("A", "expand", args)["result"]
        barrier = next(o for o in created["created_scene"]["objects"] if o["kind"] == "barrier")
        self.assertTrue(barrier["key_required"])
        self.assertTrue(barrier["consume_key"])

    def test_real_key_gift_opens_shared_path_once_and_survives_restart(self):
        destination, key_id, barrier_id, child = self.keyed_place()
        before = self.query("C", "examine", {"target_id": barrier_id})["target"]
        self.assertEqual(before["key_object_id"], key_id)
        self.assertEqual(before["key_requirement"]["state"], "sealed")
        self.assertNotIn("discovery", before["key_requirement"])
        self.assertIsNone(before["discovery"]["text"])
        with self.assertRaises(RuleViolation):
            self.move("C", child)
        self.open_take_key("B", key_id)
        self.call("B", "transfer", {"object_id": key_id, "recipient_id": self.roles["C"],
                  "expected_character_version": self.look("B")["character"]["version"]})
        args = self.unlock_args("C", key_id, barrier_id)
        char_before = self.look("C")["character"]
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("key use must not roll")):
            receipt = self.call("C", "unlock", args, "unlock-once")
        result = receipt["result"]
        self.assertEqual(result["result"]["method"], "key_object")
        self.assertEqual(result["character"], char_before)
        self.assertEqual(result["key_object"]["version"], args["expected_key_version"])
        self.assertEqual(result["object"]["discovery"]["text"], "The gate releases onto the upstream trail.")
        self.assertEqual(result["object"]["discovery"]["discovered_by"], self.roles["C"])
        self.assertFalse(next(e for e in self.look("A")["scene"]["exits"] if e["destination_id"] == child)["blocked"])
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        replay = self.call("C", "unlock", args, "unlock-once")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], result)
        self.assertEqual(self.look("C")["character"]["inventory"], [key_id])
        self.move("A", child)
        self.move("B", child)
        events = self.query("C", "history", {"limit": 50})["items"]
        unlocks = [e for e in events if e["kind"] == "unlock"]
        self.assertEqual(len(unlocks), 1)
        self.assertEqual(unlocks[0]["payload"]["operation_id"], "unlock-once")
        self.assertEqual(unlocks[0]["payload"]["discovery"], result["object"]["discovery"])

    def test_failed_strength_check_can_later_use_real_key_without_reroll(self):
        _, key_id, barrier_id, _ = self.keyed_place()
        with patch("agent_world.world_context.secrets.randbelow", return_value=0):
            failed = self.object_action("A", "check", barrier_id)["result"]
        self.assertFalse(failed["result"]["success"])
        self.open_take_key("A", key_id)
        with patch("agent_world.world_context.secrets.randbelow", side_effect=AssertionError("no reroll")):
            result = self.call("A", "unlock", self.unlock_args("A", key_id, barrier_id))["result"]
        self.assertEqual(result["object"]["state"], "resolved")
        history = self.query("A", "history", {"limit": 50})["items"]
        self.assertEqual(len([e for e in history if e["kind"] == "check" and e["payload"]["object_before_version"] == failed["result"]["object_before_version"] and e["payload"]["dc"] == 15]), 1)

    def test_key_rejects_non_owner_wrong_location_and_stale_gift_versions_atomically(self):
        destination, key_id, barrier_id, _ = self.keyed_place()
        args = self.unlock_args("A", key_id, barrier_id)
        before = self.world_rows()
        with self.assertRaises(PermissionDenied):
            self.call("A", "unlock", args)
        self.assertEqual(self.world_rows(), before)
        self.open_take_key("A", key_id)
        held = self.unlock_args("A", key_id, barrier_id)
        self.call("A", "transfer", {"object_id": key_id, "recipient_id": self.roles["B"],
                  "expected_character_version": held["expected_character_version"]})
        before = self.world_rows()
        with self.assertRaises(StateConflict):
            self.call("A", "unlock", held)
        with self.assertRaises(PermissionDenied):
            self.call("A", "unlock", self.unlock_args("A", key_id, barrier_id))
        self.assertEqual(self.world_rows(), before)
        b_args = self.unlock_args("B", key_id, barrier_id)
        self.move("B", "bell-market")
        b_args["expected_character_version"] = self.look("B")["character"]["version"]
        before = self.world_rows()
        with self.assertRaises(RuleViolation):
            self.call("B", "unlock", b_args)
        carried = self.query("B", "examine", {"target_id": key_id})
        self.assertFalse(any(a["function_id"] == "frontier.unlock" for a in carried["available_actions"]))
        self.assertEqual(self.world_rows(), before)
        self.open_take_key("B", "market-cache")
        self.move("B", destination)
        wrong = self.unlock_args("B", key_id, barrier_id)
        wrong["key_object_id"] = "market-cache"
        wrong["expected_key_version"] = self.query("B", "examine", {"target_id": "market-cache"})["target"]["version"]
        before = self.world_rows()
        with self.assertRaises(RuleViolation):
            self.call("B", "unlock", wrong)
        self.assertEqual(self.world_rows(), before)

    def test_generated_key_relation_rejects_invalid_references_without_partial_world(self):
        self.move("A", "bell-market")
        seen = self.look()
        path = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        for index, key_index, kind in ((1, 1, "barrier"), (1, 2, "barrier"), (1, 0, "inscription"), (0, 1, "cache")):
            spec = package(barrier=True)
            spec["objects"][index]["key_object"] = key_index
            if kind == "inscription":
                spec["objects"][0]["kind"] = kind
                spec["objects"][0]["discovery_text"] = "Existing words"
            before = self.world_rows()
            with self.assertRaises((RuleViolation, SchemaRejected)):
                self.call("A", "expand", {"scene_id": "bell-market", "exit_id": path["id"],
                          "expected_exit_version": path["version"], "package": spec})
            self.assertEqual(self.world_rows(), before)
        self.assertIsNone(self.query("A", "examine", {"target_id": "market-cache"})["target"]["key_object_id"])

    def test_same_key_concurrent_unlock_has_one_shared_consequence(self):
        _, key_id, barrier_id, _ = self.keyed_place()
        self.open_take_key("A", key_id)
        args = self.unlock_args("A", key_id, barrier_id)
        def execute(number):
            runtime = WorldRuntime(self.db)
            runtime.install_world("test", WORLD)
            try:
                return runtime.call_function("test", "frontier.unlock", self.roles["A"], args,
                                             operation_id="concurrent-unlock-" + str(number))["result"]
            except (StateConflict, RuleViolation):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, (1, 2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.look()["character"]["inventory"], [key_id])
        self.assertEqual(len([e for e in self.query("C", "history", {"limit": 50})["items"] if e["kind"] == "unlock"]), 1)

    def test_concurrent_creation_has_one_connected_winner_without_partial_entities(self):
        for actor in ("A", "B"):
            self.move(actor, "bell-market")
        seen = self.look()
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        args = {"scene_id": "bell-market", "exit_id": exit["id"], "expected_exit_version": exit["version"], "package": package()}
        def create(actor):
            runtime = WorldRuntime(self.db)
            runtime.install_world("test", WORLD)
            try:
                return runtime.call_function("test", "frontier.expand", self.roles[actor], args,
                                             operation_id="concurrent-create-" + actor)["result"]
            except StateConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, ("A", "B")))
        winner = [v for v in outcomes if v]
        self.assertEqual(len(winner), 1)
        self.assertEqual(self.query("C", "map")["total"], 4)
        target = next(e for e in self.look()["scene"]["exits"] if e["id"] == exit["id"])["destination_id"]
        self.assertEqual(target, winner[0]["created_scene"]["id"])
        self.move("C", "bell-market")
        self.move("C", target)
        self.assertEqual(len(self.look("C")["scene"]["objects"]), 2)
        self.assertEqual(len(self.look("C")["scene"]["npcs"]), 1)

    def test_inscription_requires_authored_words_and_failed_read_does_not_invent_them(self):
        spec = package()
        spec["objects"][1].pop("discovery_text")
        self.move("A", "bell-market")
        seen = self.look()
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        with self.assertRaises(SchemaRejected):
            self.call("A", "expand", {"scene_id": "bell-market", "exit_id": exit["id"],
                      "expected_exit_version": exit["version"], "package": spec})
        self.assertEqual(self.query("B", "map")["total"], 3)
        self.move("A", "elm-inn")
        created, _ = self.expand()
        place = created["created_scene"]["id"]
        words = package()["objects"][1]["discovery_text"]
        self.assertNotIn(words, json.dumps(created))
        self.move("B", "bell-market")
        self.move("B", place)
        object_id = next(o["id"] for o in self.look("B")["scene"]["objects"] if o["kind"] == "inscription")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0):
            failed = self.object_action("B", "check", object_id)["result"]
        self.assertFalse(failed["result"]["success"])
        self.assertEqual(failed["object"]["discovery"]["status"], "unrevealed")
        self.assertIsNone(failed["object"]["discovery"]["text"])
        for visible in (self.query("B", "examine", {"target_id": object_id}), self.look("B"), self.query("C", "history")):
            self.assertNotIn(words, json.dumps(visible))

    def test_actual_discovery_is_shared_history_and_guides_a_returning_other_identity(self):
        spec = package()
        words = "The path named Upstream trail follows this bank, not the water: leave the ford by that marked exit."
        spec["objects"][1]["discovery_text"] = words
        created, _ = self.expand(spec=spec)
        place = created["created_scene"]["id"]
        for actor in ("B", "C"):
            self.move(actor, "bell-market")
            self.move(actor, place)
        obj = next(o for o in self.look("B")["scene"]["objects"] if o["kind"] == "inscription")
        seen = self.look("B")
        args = {"object_id": obj["id"], "expected_object_version": obj["version"],
                "expected_character_version": seen["character"]["version"]}
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            committed = self.call("B", "check", args, "discover-once")
        discovery = committed["result"]["object"]["discovery"]
        self.assertEqual(discovery["text"], words)
        self.assertEqual(discovery["discovered_by"], self.roles["B"])
        self.assertEqual(discovery["status"], "revealed")
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        self.assertEqual(self.query("C", "examine", {"target_id": obj["id"]})["target"]["discovery"], discovery)
        history = self.query("C", "history", {"after": committed["result"]["history_seq"] - 1, "limit": 1})
        self.assertIn(words, json.dumps(history))
        replay = self.call("B", "check", args, "discover-once")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], committed["result"])
        # The other identity can choose an actual named exit from the finding;
        # authoring its missing destination is still a separate authorized act.
        clue_exit = next(e for e in self.look("C")["scene"]["exits"] if e["label"] == "Upstream trail")
        next_place = self.call("C", "expand", {"scene_id": place, "exit_id": clue_exit["id"],
                              "expected_exit_version": clue_exit["version"], "package": package("North bank marker")})["result"]["created_scene"]
        self.move("C", next_place["id"])
        self.move("C", place)
        self.assertEqual(self.query("C", "examine", {"target_id": obj["id"]})["target"]["discovery"], discovery)

    def test_peer_quote_none_cannot_reveal_words_but_real_resolution_can(self):
        created, _ = self.expand()
        place = created["created_scene"]["id"]
        self.move("A", place)
        self.move("C", "bell-market")
        self.move("C", place)
        obj = next(o for o in self.look()["scene"]["objects"] if o["kind"] == "inscription")
        self.request("A", "record-only", obj["id"])
        task = self.call("B", "visit")["result"]["additional_task"]
        self.assertIsNone(task["target"]["discovery"]["text"])
        self.call("B", "answer", {"request_id": "record-only", "decision": "allow", "reason": "Record an attempt without changing the target.",
                  "ability": "intelligence", "dc": 10, "effect": "none"})
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            recorded = self.call("A", "accept", {"request_id": "record-only"})["result"]
        self.assertTrue(recorded["request"]["result"]["success"])
        self.assertIsNone(self.query("A", "examine", {"target_id": obj["id"]})["target"]["discovery"]["text"])
        self.request("C", "read-words", obj["id"])
        self.call("B", "visit")
        self.call("B", "answer", {"request_id": "read-words", "decision": "allow", "reason": "Read the authored existing marks; no new facts or rewards.",
                  "ability": "intelligence", "dc": 10, "effect": "resolve_object"})
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            accepted = self.call("C", "accept", {"request_id": "read-words"})["result"]
        self.assertTrue(accepted["request"]["result"]["success"])
        self.assertEqual(self.query("A", "examine", {"target_id": obj["id"]})["target"]["discovery"]["text"], package()["objects"][1]["discovery_text"])

    def test_existing_object_without_authored_content_never_inherits_new_initial_lore(self):
        # A pre-discovery live record has no new fields. Loading newer code must
        # preserve it rather than silently write a new answer to an old clue.
        with sqlite3.connect(self.db) as database:
            raw = database.execute("SELECT value_json FROM world_state WHERE universe=? AND scope=? AND state_key=?",
                                   ("test", "frontier:objects", "inn-ledger")).fetchone()[0]
            old = json.loads(raw)
            for field in ("discovery_text", "discovered_by", "discovered_at"):
                old.pop(field, None)
            database.execute("UPDATE world_state SET value_json=? WHERE universe=? AND scope=? AND state_key=?",
                             (json.dumps(old), "test", "frontier:objects", "inn-ledger"))
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        before = self.query("A", "examine", {"target_id": "inn-ledger"})["target"]
        self.assertEqual(before["discovery"]["status"], "not_authored")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            read = self.object_action("A", "check", "inn-ledger")["result"]
        self.assertEqual(read["object"]["authored_description"], old["description"])
        self.assertNotIn("description", read["object"])
        self.assertEqual(read["object"]["state"], "read")
        self.assertEqual(read["object"]["discovery"]["status"], "not_authored")
        self.assertIsNone(read["object"]["discovery"]["text"])

    def test_registered_prior_contract_upgrades_without_losing_state_or_original_receipt(self):
        legacy_functions = []
        for function in WORLD.functions:
            if function.name in {"frontier.unlock", "frontier.drop", "frontier.add_objects"}:
                continue
            schema = deepcopy(function.input_schema)
            if function.name == "frontier.expand":
                object_schema = schema["properties"]["package"]["properties"]["objects"]["items"]
                object_schema["properties"].pop("discovery_text")
                object_schema["properties"].pop("key_object")
                object_schema["properties"].pop("key_required")
                object_schema["properties"].pop("consume_key")
                object_schema["properties"].pop("initial_state")
                object_schema.pop("allOf", None)
            legacy_functions.append(replace(function, version=1, input_schema=schema, description="Prior discovery-free contract"))
        legacy_world = replace(WORLD, version=1, functions=tuple(legacy_functions))
        runtime = WorldRuntime(self.db.with_name("prior-version.sqlite3"))
        role = runtime.create_role("Earlier visitor")["role_id"]
        runtime.install_world("prior", legacy_world)
        def call(function, args, operation):
            return runtime.call_function("prior", "frontier." + function, role, args, operation_id=operation)
        call("join", {"name": "Earlier visitor", "class_id": "rogue"}, "prior-join")
        call("move", {"destination_id": "bell-market", "expected_character_version": 1}, "prior-move")
        old_package = package()
        old_package["objects"][1].pop("discovery_text")
        args = {"scene_id": "bell-market", "exit_id": "market-road", "expected_exit_version": 1, "package": old_package}
        original = call("expand", args, "prior-place")
        before = runtime.query_function("prior", "frontier.look", role, {})["result"]
        runtime.install_world("prior", WORLD)
        after = runtime.query_function("prior", "frontier.look", role, {})
        self.assertEqual(after["function_version"], 7)
        # Installing a new capability changes discovery, not the saved world.
        self.assertEqual({k: v for k, v in after["result"].items() if k != "directory"},
                         {k: v for k, v in before.items() if k != "directory"})
        replay = call("expand", args, "prior-place")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["function_version"], 1)
        self.assertEqual(replay["result"], original["result"])
        with self.assertRaises(SchemaRejected):
            call("expand", args, "new-place-with-old-schema")

    def test_registered_discovery_contract_updates_function_versions_without_reset(self):
        prior = replace(WORLD, version=3, functions=tuple(replace(f, version=2) for f in WORLD.functions if f.name not in {"frontier.unlock", "frontier.drop", "frontier.add_objects"}))
        runtime = WorldRuntime(self.db.with_name("discovery-version.sqlite3"))
        role = runtime.create_role("Returning reader")["role_id"]
        runtime.install_world("reader", prior)
        runtime.call_function("reader", "frontier.join", role,
                              {"name": "Returning reader", "class_id": "rogue"}, operation_id="reader-join")
        before = runtime.query_function("reader", "frontier.look", role, {})["result"]
        runtime.install_world("reader", WORLD)
        after = runtime.query_function("reader", "frontier.look", role, {})
        self.assertEqual(after["function_version"], 7)
        self.assertEqual({k: v for k, v in after["result"].items() if k != "directory"},
                         {k: v for k, v in before.items() if k != "directory"})

    def test_discovered_carried_content_has_exact_reader_after_inventory_summary(self):
        spec = package()
        words = "渡" * 1200
        spec["objects"][0]["discovery_text"] = words
        created, _ = self.expand(spec=spec)
        self.move("A", created["created_scene"]["id"])
        cache = next(o for o in self.look()["scene"]["objects"] if o["kind"] == "cache")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.object_action("A", "check", cache["id"])
        self.object_action("A", "take", cache["id"])
        self.move("A", "bell-market")
        carried = self.look()["inventory_objects"][0]
        self.assertTrue(carried["discovery"]["text_truncated"])
        self.assertEqual(carried["discovery"]["text"], words[:300])
        self.assertEqual(self.query("A", "examine", {"target_id": cache["id"]})["target"]["discovery"]["text"], words)

    def test_description_cannot_grant_resources_rules_or_prior_state(self):
        self.move("A", "bell-market")
        seen = self.look()
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        original = package()
        original["description"] = "The author claims every player gains 999 coins, all old gates open, and B agrees to everything."
        base = {"scene_id": "bell-market", "exit_id": exit["id"], "expected_exit_version": exit["version"], "package": original}
        for field, value in (("rewards", {"coins": 999}), ("state_patch", {"B.hp": 0})):
            invalid = deepcopy(base)
            invalid["package"][field] = value
            with self.assertRaises(SchemaRejected):
                self.call("A", "expand", invalid)
        invalid = deepcopy(base)
        invalid["package"]["objects"][0]["blocks_exit"] = 0
        with self.assertRaises(RuleViolation):
            self.call("A", "expand", invalid)
        self.assertEqual(self.query("A", "map")["total"], 3)
        before = self.look("B")["character"]
        created = self.call("A", "expand", base)["result"]["created_scene"]
        self.assertEqual(self.look("B")["character"], before)
        self.assertFalse(created["rest_allowed"])
        self.assertTrue(all(o["owner"] is None for o in created["objects"]))
        self.assertNotIn("coins", self.look()["character"])

    def test_unique_generated_object_is_taken_once_and_given_without_duplicate(self):
        spec = package()
        old_prose = "铃舌卡住，轻晃时只发出一声闷响。"
        spec["objects"][0]["description"] = old_prose
        created, _ = self.expand(spec=spec)
        destination = created["created_scene"]["id"]
        self.move("A", destination)
        self.move("B", "bell-market")
        self.move("B", destination)
        cache = next(o for o in self.look()["scene"]["objects"] if o["kind"] == "cache")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.object_action("B", "check", cache["id"])
        open_cache = next(o for o in self.look("B")["scene"]["objects"] if o["id"] == cache["id"])
        self.assertEqual(open_cache["state"], "open")
        self.assertEqual(open_cache["authored_description"], old_prose)
        self.assertNotIn("description", open_cache)
        b_version = self.look("B")["character"]["version"]
        self.object_action("A", "take", cache["id"], "take-once")
        with self.assertRaises((RuleViolation, StateConflict)):
            self.call("B", "take", {"object_id": cache["id"], "expected_object_version": open_cache["version"],
                      "expected_character_version": b_version})
        self.call("A", "transfer", {"object_id": cache["id"], "recipient_id": self.roles["B"],
                  "expected_character_version": self.look()["character"]["version"]}, "give-once")
        self.assertEqual(self.look("B")["character"]["inventory"], [cache["id"]])
        self.assertEqual(self.look()["character"]["inventory"], [])
        self.assertNotIn(cache["id"], [o["id"] for o in self.look()["scene"]["objects"]])
        self.move("B", "bell-market")
        self.move("B", "elm-inn")
        held = self.query("B", "examine", {"target_id": cache["id"]})["target"]
        self.assertEqual(held["owner"], self.roles["B"])
        self.assertEqual(held["state"], "taken")
        self.assertEqual(held["authored_description"], old_prose)
        self.assertNotIn("description", held)
        with sqlite3.connect(self.db) as database:
            stored = json.loads(database.execute(
                "SELECT value_json FROM world_state WHERE universe=? AND scope=? AND state_key=?",
                ("test", "frontier:objects", cache["id"])).fetchone()[0])
        self.assertEqual(stored["description"], old_prose)
        self.assertNotIn("authored_description", stored)
        with self.assertRaises(RuleViolation):
            self.call("A", "transfer", {"object_id": cache["id"], "recipient_id": self.roles["B"],
                      "expected_character_version": self.look()["character"]["version"]})

    def test_pending_owners_can_help_each_other_and_continue_elsewhere(self):
        self.move("A", "bell-market")
        self.move("B", "bell-market")
        self.request("A", "a-method")
        self.assertIsNone(self.call("A", "visit")["result"]["additional_task"])
        self.request("B", "b-method")
        a = self.call("A", "visit")["result"]["additional_task"]
        b = self.call("B", "visit")["result"]["additional_task"]
        self.assertEqual(a["request"]["owner"], self.roles["B"])
        self.assertEqual(b["request"]["owner"], self.roles["A"])
        self.move("A", "elm-inn")
        self.offer("A", "b-method")
        self.offer("B", "a-method")
        self.assertEqual(self.look()["own_requests"][0]["status"], "offered")
        with self.assertRaises(RuleViolation):
            self.call("A", "accept", {"request_id": "a-method"})
        self.move("A", "bell-market")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.call("A", "accept", {"request_id": "a-method"})
        with self.assertRaises(StateConflict):
            self.call("B", "accept", {"request_id": "b-method"})

    def test_assigned_other_answer_and_only_owner_accept_or_withdraw(self):
        self.move("A", "bell-market")
        self.request()
        self.call("B", "visit")
        for actor in ("A", "C"):
            with self.assertRaises(PermissionDenied):
                self.offer(actor)
        self.offer()
        for actor in ("B", "C"):
            with self.assertRaises(PermissionDenied):
                self.call(actor, "accept", {"request_id": "method"})
            with self.assertRaises(PermissionDenied):
                self.call(actor, "withdraw", {"request_id": "method"})
        self.call("A", "withdraw", {"request_id": "method"})
        with self.assertRaises(RuleViolation):
            self.call("A", "accept", {"request_id": "method"})
        self.assertIsNone(self.look("B")["additional_task"])

    def test_offer_and_roll_replay_survive_restart_and_same_actor_cannot_reroll(self):
        self.move("A", "bell-market")
        self.request()
        self.call("B", "visit")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0) as dice:
            self.offer(operation="quote-once")
            dice.assert_not_called()
            accepted = self.call("A", "accept", {"request_id": "method"}, "roll-once")
            self.assertFalse(accepted["result"]["result"]["success"])
            self.assertEqual(dice.call_count, 1)
            self.runtime = WorldRuntime(self.db)
            self.runtime.install_world("test", WORLD)
            replay = self.call("A", "accept", {"request_id": "method"}, "roll-once")
            self.assertEqual(replay["result"], accepted["result"])
            self.assertTrue(replay["replayed"])
            with self.assertRaises(RuleViolation):
                self.object_action("A", "check", "market-cache")
            with self.assertRaises(RuleViolation):
                self.request("A", "new-words")
            self.assertEqual(dice.call_count, 1)
        self.assertEqual(self.look()["own_requests"][0]["result"], accepted["result"]["result"])

    def test_history_and_terminal_requests_are_complete_after_one_hundred_entries(self):
        self.move("A", "bell-market")
        for index in range(105):
            self.call("A", "say", {"text": f"Field note {index}"})
        for index in range(12):
            self.request("A", f"withdrawn-{index}")
            self.call("A", "withdraw", {"request_id": f"withdrawn-{index}"})
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world("test", WORLD)
        seen = self.look()
        self.assertTrue(seen["own_requests_has_more"])
        cursor, entries = 0, []
        while True:
            page = self.query("C", "history", {"after": cursor, "limit": 17})
            entries.extend(page["items"])
            cursor = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual([e["seq"] for e in entries], list(range(1, seen["history_latest_seq"] + 1)))
        cursor, requests = 0, []
        while True:
            page = self.query("A", "requests", {"box": "own", "after": cursor, "limit": 5})
            requests.extend(page["items"])
            cursor = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual(len(requests), 12)
        self.assertTrue(all(r["status"] == "withdrawn" for r in requests))

    def test_resolved_target_can_be_rejected_and_helper_can_claim_next(self):
        self.move("A", "bell-market")
        self.request("A", "old-target")
        self.call("B", "visit")
        self.move("C", "bell-market")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.object_action("C", "check", "market-cache")
        with self.assertRaises(StateConflict):
            self.offer("B", "old-target")
        rejected = self.call("B", "answer", {"request_id": "old-target", "decision": "reject",
                             "reason": "The target was resolved while I was reading it."})["result"]
        self.assertEqual(rejected["request"]["status"], "rejected")
        self.move("C", "elm-inn")
        self.request("C", "next-target", "inn-ledger")
        self.assertEqual(self.call("B", "visit")["result"]["additional_task"]["request"]["request_id"], "next-target")

    def test_long_valid_location_title_still_allows_a_child_and_return(self):
        first, _ = self.expand(spec=package("渡" * 80))
        self.move("A", first["created_scene"]["id"])
        seen = self.look()
        exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
        child = self.call("A", "expand", {"scene_id": seen["scene"]["id"], "exit_id": exit["id"],
                          "expected_exit_version": exit["version"], "package": package("Child")})["result"]["created_scene"]
        self.move("A", child["id"])
        self.assertLessEqual(len(child["exits"][0]["label"]), 60)
        self.move("A", first["created_scene"]["id"])

    def test_maximum_dialogue_history_pages_fit_without_losing_text(self):
        for index in range(22):
            self.call("A", "tell", {"npc_id": "mira", "text": "言" * 1200, "response": "答" * 1200})
        cursor, dialogue = 0, []
        while True:
            page = self.query("B", "history", {"after": cursor, "limit": 50})
            self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 262144)
            dialogue.extend(e for e in page["items"] if e["kind"] == "tell")
            cursor = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual(len(dialogue), 22)
        self.assertTrue(all(e["payload"]["text"] == "言" * 1200 and e["payload"]["response"] == "答" * 1200 for e in dialogue))

    def test_map_pagination_preserves_created_graph_beyond_look_slice(self):
        self.move("A", "bell-market")
        for index in range(31):
            seen = self.look()
            exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
            minimal = {"title": f"Trail {index}", "description": "A modest connected trail.", "facts": [], "npcs": [], "objects": [],
                       "exits": [{"label": "Continue trail", "description": "The trail continues beyond the known map."}]}
            created = self.call("A", "expand", {"scene_id": seen["scene"]["id"], "exit_id": exit["id"],
                                "expected_exit_version": exit["version"], "package": minimal})["result"]["created_scene"]
            self.move("A", created["id"])
        self.assertTrue(self.look()["map_has_more"])
        self.assertEqual(self.look()["map_total"], 34)
        cursor, items = 0, []
        while True:
            page = self.query("B", "map", {"after": cursor, "limit": 7})
            items.extend(page["items"])
            cursor = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual(len({i["id"] for i in items}), 34)
        self.assertEqual([i["ordinal"] for i in items], list(range(1, 35)))


class FrontierApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontier-entry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db, self.private = self.root / "data" / "world.sqlite3", self.root / "keys"
        self.setup = initialize_local(self.db, self.private)
        self.origin = "http://127.0.0.1:8805"

    def connect(self, client, actor="A", access="control", suffix="one"):
        conn = connect_world(self.origin, key_path=Path(self.setup["participants"][actor]["key_path"]),
                             output_path=self.private / f"{actor}-{suffix}.json", access_mode=access, client=client)
        return conn, json.loads(conn.output_path.read_text())["mcp"]["agent_world"]["headers"]

    def invoke(self, client, headers, function, args=None, operation=None):
        return client.post("/v1/functions/frontier." + function + "/invoke", headers=headers,
                           json={"arguments": args or {}, **({"operation_id": operation} if operation else {})})

    def test_actual_entry_assets_and_formal_control_observe_boundary(self):
        with TestClient(create_frontier_application(self.db), base_url=self.origin) as client:
            for route in ("/", "/frontier-static/frontier.js", "/frontier-static/frontier-client.js", "/frontier-static/frontier-history.js", "/frontier-static/styles.css", "/static/commons-client.js"):
                response = client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertTrue(response.content)
            self.assertEqual(client.get("/v1/bootstrap").status_code, 401)
            _, control = self.connect(client)
            for chapter in CHAPTERS:
                response = self.invoke(client, control, "handbook", {"chapter": chapter})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.invoke(client, control, "join", {"name": "A", "class_id": "rogue"}, "join").status_code, 200)
            _, observe = self.connect(client, access="observe", suffix="observe")
            self.assertEqual(self.invoke(client, observe, "look").status_code, 200)
            self.assertEqual(self.invoke(client, observe, "say", {"text": "Cannot write"}, "observe-write").status_code, 403)
            self.assertEqual(set(self.setup["participants"]), {"A", "B", "C", "D"})

    def test_fresh_credential_same_key_resumes_generated_world_and_backup(self):
        with TestClient(create_frontier_application(self.db), base_url=self.origin) as client:
            original, headers = self.connect(client)
            self.assertEqual(self.invoke(client, headers, "join", {"name": "Returning traveller", "class_id": "rogue"}, "join-once").status_code, 200)
            moved = self.invoke(client, headers, "move", {"destination_id": "bell-market", "expected_character_version": 1}, "move-once")
            self.assertEqual(moved.status_code, 200, moved.text)
            seen = self.invoke(client, headers, "look").json()["result"]
            exit = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None)
            args = {"scene_id": "bell-market", "exit_id": exit["id"], "expected_exit_version": exit["version"], "package": package()}
            written = self.invoke(client, headers, "expand", args, "world-created")
            self.assertEqual(written.status_code, 200, written.text)
            created = written.json()["result"]
            before = self.invoke(client, headers, "look").json()["result"]
        # A real SQLite online backup is opened independently; this is not a
        # fabricated cross-day observation or a production restore.
        backup = self.root / "restore" / "world.sqlite3"
        backup.parent.mkdir(mode=0o700)
        with sqlite3.connect(self.db) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        backup.chmod(0o600)
        for database, suffix in ((self.db, "restart"), (backup, "restore")):
            with TestClient(create_frontier_application(database), base_url=self.origin) as client:
                resumed, headers = self.connect(client, suffix=suffix)
                self.assertEqual(resumed.role_id, original.role_id)
                joined = self.invoke(client, headers, "join", {"name": "Ignored replacement", "class_id": "fighter"}, "resume-" + suffix)
                self.assertTrue(joined.json()["result"]["resumed"])
                self.assertEqual(self.invoke(client, headers, "look").json()["result"]["character"], before["character"])
                replay = self.invoke(client, headers, "expand", args, "world-created")
                self.assertTrue(replay.json()["replayed"])
                self.assertEqual(replay.json()["result"], created)

    def test_provision_never_resets_existing_world(self):
        before = {p: p.read_bytes() for p in self.private.rglob("*.json")}
        with self.assertRaises(ValueError):
            initialize_local(self.db, self.private)
        self.assertEqual(before, {p: p.read_bytes() for p in self.private.rglob("*.json")})
        with self.assertRaises(ValueError):
            existing_world(self.db, "not-the-world")


if __name__ == "__main__":
    unittest.main()
