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
        self.assertEqual(read["object"]["description"], old["description"])
        self.assertEqual(read["object"]["state"], "read")
        self.assertEqual(read["object"]["discovery"]["status"], "not_authored")
        self.assertIsNone(read["object"]["discovery"]["text"])

    def test_registered_prior_contract_upgrades_without_losing_state_or_original_receipt(self):
        legacy_functions = []
        for function in WORLD.functions:
            schema = deepcopy(function.input_schema)
            if function.name == "frontier.expand":
                object_schema = schema["properties"]["package"]["properties"]["objects"]["items"]
                object_schema["properties"].pop("discovery_text")
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
        self.assertEqual(after["function_version"], 2)
        self.assertEqual(after["result"], before)
        replay = call("expand", args, "prior-place")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["function_version"], 1)
        self.assertEqual(replay["result"], original["result"])
        with self.assertRaises(SchemaRejected):
            call("expand", args, "new-place-with-old-schema")

    def test_registered_discovery_contract_updates_function_versions_without_reset(self):
        prior = replace(WORLD, version=2, functions=tuple(replace(f, version=1) for f in WORLD.functions))
        runtime = WorldRuntime(self.db.with_name("discovery-version.sqlite3"))
        role = runtime.create_role("Returning reader")["role_id"]
        runtime.install_world("reader", prior)
        runtime.call_function("reader", "frontier.join", role,
                              {"name": "Returning reader", "class_id": "rogue"}, operation_id="reader-join")
        before = runtime.query_function("reader", "frontier.look", role, {})["result"]
        runtime.install_world("reader", WORLD)
        after = runtime.query_function("reader", "frontier.look", role, {})
        self.assertEqual(after["function_version"], 2)
        self.assertEqual(after["result"], before)

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
        created, _ = self.expand()
        destination = created["created_scene"]["id"]
        self.move("A", destination)
        self.move("B", "bell-market")
        self.move("B", destination)
        cache = next(o for o in self.look()["scene"]["objects"] if o["kind"] == "cache")
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.object_action("B", "check", cache["id"])
        open_cache = next(o for o in self.look("B")["scene"]["objects"] if o["id"] == cache["id"])
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
        self.assertEqual(self.query("B", "examine", {"target_id": cache["id"]})["target"]["owner"], self.roles["B"])
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
