"""Meaningful shared-adventure outcomes and rule/commit regressions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, SchemaRejected, StateConflict
from examples.dnd_world import CHARACTERS, SCENES, WORLD_STATE, WORLD, make_world


class DndWorldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dnd-world-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "world.sqlite3"
        self.runtime = WorldRuntime(self.db)
        self.roles = {name: self.runtime.create_role(name)["role_id"] for name in ("A", "B", "C", "GM")}
        self.runtime.install_world("test", make_world(judge_role_ids=(self.roles["GM"],)))
        self.n = 0
        for actor, class_id in (("A", "fighter"), ("B", "rogue"), ("C", "rogue")):
            self.call(actor, "dnd.join", {"name": actor, "class_id": class_id})

    def call(self, actor, function, args, operation=None):
        self.n += 1
        return self.runtime.call_function("test", function, self.roles[actor], args, operation_id=operation or f"test-{self.n}")

    def look(self, actor="A"):
        return self.runtime.query_function("test", "dnd.look", self.roles[actor], {})["result"]

    def act(self, actor, action, target=None, **extra):
        seen = self.look(actor)
        args = {"action": action, "expected_character_version": seen["character"]["version"],
                "expected_scene_version": seen["scene"]["version"], **extra}
        if target is not None:
            args["target"] = target
        return self.call(actor, "dnd.act", args)

    def move(self, actor, *rooms):
        for room in rooms:
            self.act(actor, "move", room)

    def rope_route(self, actor="A"):
        self.move(actor, "entry", "store")
        self.act(actor, "search")
        self.act(actor, "take", "rope")
        self.move(actor, "entry", "gallery")
        self.act(actor, "set_rope")
        self.move(actor, "vault")

    def propose(self, actor="A", intent="method"):
        seen = self.look(actor)
        return self.call(actor, "dnd.propose", {"intent_id": intent, "text": "Brace my shield against the old door and lift its jammed hinge",
                         "expected_character_version": seen["character"]["version"], "expected_scene_version": seen["scene"]["version"]})

    def offer(self, intent="method", **extra):
        return self.call("GM", "dnd.rule", {"intent_id": intent, "ability": "strength", "dc": 10,
                         "success_effect": "open_bandit_door", "failure_effect": "damage_1d6",
                         "reason": "The shield is existing equipment and the door is unresolved",
                         "risk": "Success opens this door; failure causes 1d6 injury", **extra})

    def test_two_goals_share_unique_relic_and_persistent_cooperative_route(self):
        self.rope_route()
        self.act("A", "take", "beacon_seal")
        self.move("B", "entry", "store")
        self.act("B", "take", "ledger_page")
        self.move("B", "entry", "gallery", "vault")
        self.assertNotIn("beacon_seal", [i["id"] for i in self.look("B")["scene"]["objects"]])
        self.act("A", "transfer", "beacon_seal", recipient=self.roles["B"])
        self.assertNotIn("beacon_seal", self.look()["character"]["inventory"])
        self.assertIn("beacon_seal", self.look("B")["character"]["inventory"])
        self.move("B", "gallery", "entry", "camp")
        self.act("B", "deliver")
        self.call("C", "dnd.say", {"text": "I recorded B's return; I did not personally retrieve the seal."})
        self.assertEqual(self.look("C")["progress"]["returned_by"], self.roles["B"])
        self.assertTrue(self.look()["progress"]["seal_returned"])
        with self.assertRaises(RuleViolation):
            self.act("B", "deliver")

    def test_negotiation_route_uses_real_evidence_without_harming_npcs(self):
        self.move("B", "entry", "store")
        self.act("B", "search")
        self.act("B", "take", "ledger_page")
        self.move("B", "entry")
        with patch("agent_world.world_context.secrets.randbelow", side_effect=lambda n: min(9, n-1)):
            self.act("B", "unlock")
            self.move("B", "bandit_room")
            result = self.act("B", "negotiate", option="ledger")["result"]
        self.assertTrue(result["check"]["success"])
        self.assertTrue(all(npc["hp"] == 11 for npc in self.look("B")["scene"]["npcs"]))
        self.move("B", "vault")
        self.act("B", "take", "beacon_seal")
        self.move("B", "bandit_room", "entry", "camp")
        self.act("B", "deliver")
        self.assertTrue(self.look()["progress"]["seal_returned"])

    def test_same_object_concurrent_take_has_one_winner(self):
        self.move("A", "entry", "store")
        self.move("B", "entry", "store")
        self.act("A", "search")
        snapshots = {actor: self.look(actor) for actor in ("A", "B")}
        def take(actor):
            seen = snapshots[actor]
            try:
                runtime = WorldRuntime(self.db)
                runtime.install_world("test", WORLD)
                runtime.call_function("test", "dnd.act", self.roles[actor],
                                      {"action": "take", "target": "healing_potion", "expected_character_version": seen["character"]["version"],
                                       "expected_scene_version": seen["scene"]["version"]}, operation_id="concurrent-" + actor)
                return "won"
            except (StateConflict, RuleViolation):
                return "lost"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(take, ("A", "B")))
        self.assertEqual(sorted(outcomes), ["lost", "won"])
        self.assertEqual(sum("healing_potion" in self.look(actor)["character"]["inventory"] for actor in ("A", "B")), 1)

    def test_failure_cannot_be_rerolled_by_chat_move_rejoin_or_new_operation(self):
        self.move("A", "entry")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0) as dice:
            result = self.act("A", "force")["result"]
            self.assertFalse(result["check"]["success"])
            calls = dice.call_count
            self.call("B", "dnd.say", {"text": "No new physical evidence."})
            self.move("A", "camp", "entry")
            self.call("A", "dnd.join", {"name": "Reset attempt", "class_id": "rogue"})
            with self.assertRaises(RuleViolation):
                self.act("A", "force")
            self.assertEqual(dice.call_count, calls)

    def test_offer_then_commit_owner_roll_once_and_cannot_cancel_or_replace_terms(self):
        self.move("A", "entry")
        with patch("agent_world.world_context.secrets.randbelow", return_value=0) as dice:
            self.propose()
            self.offer()
            self.assertEqual(dice.call_count, 0)
            with self.assertRaises(RuleViolation):
                self.offer(dc=18)
            with self.assertRaises(PermissionDenied):
                self.call("B", "dnd.commit", {"intent_id": "method"})
            first = self.call("A", "dnd.commit", {"intent_id": "method"}, "committed-once")
            self.assertFalse(first["result"]["check"]["success"])
            calls = dice.call_count
            replay = self.call("A", "dnd.commit", {"intent_id": "method"}, "committed-once")
            self.assertTrue(replay["replayed"])
            self.assertEqual(first["result"], replay["result"])
            self.assertEqual(dice.call_count, calls)
        with self.assertRaises(RuleViolation):
            self.call("A", "dnd.withdraw", {"intent_id": "method"})

    def test_chat_does_not_invalidate_quote_but_changed_scene_does(self):
        self.move("A", "entry")
        self.propose()
        self.offer()
        self.call("B", "dnd.say", {"text": "An utterance alone changes no door."})
        with patch("agent_world.world_context.secrets.randbelow", return_value=19):
            self.assertTrue(self.call("A", "dnd.commit", {"intent_id": "method"})["result"]["check"]["success"])
        self.move("A", "store")
        self.propose(intent="search-new")
        self.offer(intent="search-new", success_effect="reveal_store", failure_effect="none")
        self.move("B", "entry", "store")
        self.act("B", "search")
        with patch("agent_world.world_context.secrets.randbelow") as dice:
            with self.assertRaises(StateConflict):
                self.call("A", "dnd.commit", {"intent_id": "search-new"})
            dice.assert_not_called()

    def test_unauthorized_ruling_and_unknown_effects_do_not_change_world(self):
        self.move("A", "entry")
        self.propose()
        with self.assertRaises(PermissionDenied):
            self.call("B", "dnd.rule", {"intent_id": "method", "ability": "strength", "dc": 10,
                       "success_effect": "none", "failure_effect": "none", "reason": "Pretend to be referee", "risk": "none"})
        with self.assertRaises(SchemaRejected):
            self.offer(success_effect="duplicate_beacon_seal")
        self.assertTrue(self.look()["scene"]["locked"])

    def test_fixed_quote_discloses_rope_cost_even_if_referee_text_denies_it(self):
        self.move("A", "entry", "store")
        self.act("A", "search")
        self.act("A", "take", "rope")
        self.move("A", "entry", "gallery")
        self.propose(intent="rope-method")
        offered = self.offer(intent="rope-method", success_effect="secure_gap", failure_effect="none",
                             risk="No material is consumed", reason="Existing rope can span this gap")["result"]
        self.assertEqual(offered["ruling"]["terms"]["success"]["cost_items"], ["rope"])
        self.assertIn("rope", self.look()["character"]["inventory"])
        with patch("agent_world.world_context.secrets.randbelow", return_value=9):
            self.call("A", "dnd.commit", {"intent_id": "rope-method"})
        self.assertNotIn("rope", self.look()["character"]["inventory"])
        self.assertTrue(self.look()["scene"]["gap_secured"])

    def test_two_opt_in_players_finish_initiative_combat_without_forcing_bystander(self):
        self.move("A", "entry")
        with patch("agent_world.world_context.secrets.randbelow", side_effect=lambda n: min(9, n-1)):
            self.act("A", "force")
            self.move("A", "bandit_room")
            start = self.act("A", "attack", "bandit_1")["result"]
            self.assertTrue(start["combat_started"])
            self.assertTrue(all(n["hp"] == 11 for n in self.look()["scene"]["npcs"]))
            self.move("B", "entry", "bandit_room")
            self.act("B", "join_combat")
            with self.assertRaises(RuleViolation):
                self.act("B", "attack", "bandit_1")
            first = self.act("A", "attack", "bandit_1")["result"]
            self.assertTrue(first["attack"]["hit"])
            second = self.act("B", "attack", "bandit_2")["result"]
        self.assertTrue(second["attack"]["sneak_attack_rolls"])
        self.assertFalse(self.look("B")["combat"]["active"])
        self.assertEqual(self.look("C")["scene"]["id"], "camp")
        self.assertEqual(self.look("C")["character"]["hp"], 10)
        self.move("B", "vault")
        self.act("B", "take", "beacon_seal")

    def test_skill_naturals_advantage_cancellation_and_critical_sneak_dice(self):
        from types import SimpleNamespace
        from examples.dnd_world import _check, _npc_attack
        char = deepcopy(self.look("B")["character"])
        char["skills"]["test_skill"] = 9
        ctx = SimpleNamespace(actor_role_id=self.roles["B"], random_int=lambda low, high: 1)
        check = _check(ctx, char, "dexterity", 10, skill="test_skill")
        self.assertTrue(check["success"], "Natural 1 is not an automatic skill failure")
        rolls = iter([1, 20])
        ctx.random_int = lambda low, high: next(rolls)
        check = _check(ctx, char, "dexterity", 25, advantage=True, disadvantage=True)
        self.assertEqual(check["rolls"], [1], "Opposed advantage/disadvantage cancels")
        char["help"] = {"scene_id": "camp"}
        dice = iter([5, 20, 6, 6, 6, 6])
        ctx.random_int = lambda low, high: next(dice)
        room = {"npcs": {"target": {"ac": 100, "hp": 100}}, "combat": None}
        attack = _npc_attack(ctx, char, room, "target")
        self.assertTrue(attack["hit"], "Natural 20 hits despite AC")
        self.assertTrue(attack["critical"])
        self.assertEqual(attack["damage_rolls"], [6, 6])
        self.assertEqual(attack["sneak_attack_rolls"], [6, 6])
        self.assertEqual(attack["damage"], 27)

    def test_zero_hp_instant_death_stability_and_natural_twenty_recovery(self):
        from types import SimpleNamespace
        from examples.dnd_world import _damage, _death_save, _heal
        char = deepcopy(self.look()["character"])
        _damage(char, 12)
        self.assertEqual((char["hp"], char["status"]), (0, "unconscious"))
        ctx = SimpleNamespace(random_int=lambda low, high: 1)
        _death_save(ctx, char)
        self.assertEqual(char["death_saves"]["failures"], 2)
        ctx.random_int = lambda low, high: 20
        _death_save(ctx, char)
        self.assertEqual((char["hp"], char["status"]), (1, "alive"))
        _damage(char, 1)
        ctx.random_int = lambda low, high: 10
        for _ in range(3):
            _death_save(ctx, char)
        self.assertEqual((char["hp"], char["status"]), (0, "stable"))
        _damage(char, 24)
        self.assertEqual(char["status"], "dead")
        with self.assertRaises(RuleViolation):
            _heal(char, 100)

    def test_damage_preserves_unstable_death_save_successes_and_breaks_stability(self):
        from types import SimpleNamespace
        from examples.dnd_world import _damage, _death_save
        char = deepcopy(self.look()["character"])
        _damage(char, char["hp"])
        ctx = SimpleNamespace(random_int=lambda low, high: 10)
        _death_save(ctx, char)
        _death_save(ctx, char)
        _damage(char, 1)
        self.assertEqual(char["death_saves"], {"successes": 2, "failures": 1})
        _death_save(ctx, char)
        self.assertEqual(char["status"], "stable")
        _damage(char, 0)
        self.assertEqual(char["status"], "stable")
        _damage(char, 1, critical=True)
        self.assertEqual(char["status"], "unconscious")
        self.assertEqual(char["death_saves"], {"successes": 0, "failures": 2})

    def test_history_beyond_recent_window_restarts_and_backup_recovers_same_facts(self):
        self.rope_route()
        self.act("A", "take", "beacon_seal")
        for i in range(110):
            self.call("C", "dnd.say", {"text": f"Record {i}: observer's statement, no item grant."})
        before = self.look()
        cursor, entries = 0, []
        while True:
            page = self.runtime.query_function("test", "dnd.history", self.roles["A"], {"after": cursor, "limit": 23})["result"]
            entries += page["items"]
            cursor = page["next_after"]
            if not page["has_more"]:
                break
        self.assertGreater(len(entries), 100)
        self.assertEqual([e["seq"] for e in entries], list(range(1, cursor+1)))
        restored = self.db.with_name("restored.sqlite3")
        with sqlite3.connect(self.db) as source, sqlite3.connect(restored) as target:
            source.backup(target)
            self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.runtime = WorldRuntime(restored)
        self.runtime.install_world("test", WORLD)
        self.assertEqual(self.look()["character"], before["character"])
        self.assertEqual(self.look()["progress"], before["progress"])
        self.assertTrue(self.runtime.query_function("test", "dnd.handbook", self.roles["GM"], {"chapter":"gm"})["result"]["text"])


if __name__ == "__main__":
    unittest.main()
