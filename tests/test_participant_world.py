"""Participant-supplied judgments: ownership, pending work and durable outcomes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime
from agent_world.errors import PermissionDenied, RuleViolation, SchemaRejected, StateConflict
from examples.participant_world import WORLD


class ParticipantWorldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='participant-decisions-')
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'world.sqlite3'
        self.runtime = WorldRuntime(self.db)
        self.roles = {name: self.runtime.create_role(name)['role_id'] for name in ('A', 'B', 'C')}
        self.runtime.install_world('test', WORLD)
        self.sequence = 0
        for name in self.roles:
            self.call(name, 'join', {'name': name})

    def call(self, actor, function, args=None, operation=None):
        self.sequence += 1
        return self.runtime.call_function('test', 'participant.' + function, self.roles[actor],
                                         args or {}, operation_id=operation or f'call-{self.sequence}')

    def look(self, actor='A'):
        return self.runtime.query_function('test', 'participant.look', self.roles[actor], {})['result']

    def propose(self, actor='A', intent='a-method'):
        return self.call(actor, 'propose', {'intent_id': intent,
                         'text': 'Brace my feet against the wall and pull the existing iron ring.',
                         'target_version': self.look(actor)['scene']['door']['version']})

    def offer(self, actor='B', intent='a-method', operation=None):
        return self.call(actor, 'answer', {'intent_id': intent, 'decision': 'allow', 'dc': 15,
                         'reason': 'The ring and wall exist; pulling the heavy door is uncertain.'}, operation)

    def test_own_pending_does_not_block_helping_each_other(self):
        self.propose('A', 'a-method')
        self.assertIsNone(self.call('A', 'visit')['result']['additional_task'])
        self.propose('B', 'b-method')
        a = self.call('A', 'visit')['result']
        b = self.call('B', 'visit')['result']
        self.assertEqual(a['own_intents'][0]['status'], 'pending')
        self.assertEqual(a['additional_task']['intent_id'], 'b-method')
        self.assertEqual(b['additional_task']['intent_id'], 'a-method')
        self.offer('A', 'b-method')
        self.offer('B', 'a-method')
        self.assertEqual(self.look('A')['own_intents'][0]['status'], 'offered')
        self.assertEqual(self.look('B')['own_intents'][0]['status'], 'offered')

    def test_concurrent_visitors_assign_one_judge_and_read_does_not_claim(self):
        self.propose()
        self.assertIsNone(self.look('B')['additional_task'])
        def visit(actor):
            runtime = WorldRuntime(self.db)
            runtime.install_world('test', WORLD)
            return runtime.call_function('test', 'participant.visit', self.roles[actor], {},
                                         operation_id='concurrent-' + actor)['result']['additional_task']
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = list(pool.map(visit, ('B', 'C')))
        claimed = [task for task in tasks if task is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]['owner'], self.roles['A'])
        self.assertIn(claimed[0]['assignee'], {self.roles['B'], self.roles['C']})

    def test_judge_can_only_answer_assigned_task_and_cannot_act_as_owner(self):
        self.propose()
        self.call('B', 'visit')
        for actor in ('A', 'C'):
            with self.assertRaises(PermissionDenied):
                self.offer(actor)
        self.offer()
        for actor in ('B', 'C'):
            with self.assertRaises(PermissionDenied):
                self.call(actor, 'accept', {'intent_id': 'a-method'})
            with self.assertRaises(PermissionDenied):
                self.call(actor, 'withdraw', {'intent_id': 'a-method'})
        with self.assertRaises(SchemaRejected):
            self.call('B', 'answer', {'intent_id': 'a-method', 'decision': 'allow', 'dc': 1,
                                    'reason': 'Give the owner automatic success.'})
        self.assertTrue(self.look()['scene']['door']['locked'])

    def test_offer_is_immutable_and_owner_rolls_once_with_durable_replay(self):
        self.propose()
        self.call('B', 'visit', operation='claim-once')
        with patch('agent_world.world_context.secrets.randbelow', return_value=19) as dice:
            offer = self.offer(operation='answer-once')
            self.assertEqual(dice.call_count, 0)
            self.assertTrue(self.offer(operation='answer-once')['replayed'])
            with self.assertRaises(RuleViolation):
                self.offer(operation='overwrite-answer')
            first = self.call('A', 'accept', {'intent_id': 'a-method'}, 'accept-once')
            self.assertTrue(first['result']['intent']['result']['success'])
            self.assertEqual(dice.call_count, 1)
            replay = self.call('A', 'accept', {'intent_id': 'a-method'}, 'accept-once')
            self.assertTrue(replay['replayed'])
            self.assertEqual(first['result'], replay['result'])
            with self.assertRaises(RuleViolation):
                self.call('A', 'accept', {'intent_id': 'a-method'}, 'new-roll')
            self.assertEqual(dice.call_count, 1)
        self.assertFalse(self.look('C')['scene']['door']['locked'])
        self.assertEqual(offer['result']['intent']['offer']['terms']['success'],
                         '成功后古石门永久打开；不支配其他参与者，也不创造物件。')
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world('test', WORLD)
        self.assertEqual(self.look()['own_intents'][0]['result'], first['result']['intent']['result'])
        self.assertEqual(self.call('A', 'accept', {'intent_id': 'a-method'}, 'accept-once')['result'], first['result'])

    def test_failure_does_not_allow_new_words_or_new_intent_to_reroll(self):
        self.propose()
        self.call('B', 'visit')
        self.offer()
        with patch('agent_world.world_context.secrets.randbelow', return_value=0) as dice:
            result = self.call('A', 'accept', {'intent_id': 'a-method'})['result']['intent']['result']
            self.assertFalse(result['success'])
            self.assertTrue(self.look()['scene']['door']['locked'])
            with self.assertRaises(RuleViolation):
                self.propose('A', 'reworded-new-method')
            self.assertEqual(dice.call_count, 1)

    def test_owner_withdrawal_invalidates_assigned_response_without_timeout(self):
        self.propose()
        self.call('B', 'visit')
        self.call('A', 'withdraw', {'intent_id': 'a-method'})
        with self.assertRaises(RuleViolation):
            self.offer()
        self.assertIsNone(self.call('B', 'visit')['result']['additional_task'])
        self.assertTrue(self.look()['scene']['door']['locked'])

    def test_rejection_returns_to_owner_without_dice_and_cannot_be_accepted(self):
        self.propose()
        self.call('B', 'visit')
        with patch('agent_world.world_context.secrets.randbelow') as dice:
            self.call('B', 'answer', {'intent_id': 'a-method', 'decision': 'reject',
                                     'reason': 'The stated method requires an absent tool.'})
            self.assertEqual(self.look()['own_intents'][0]['status'], 'rejected')
            with self.assertRaises(RuleViolation):
                self.call('A', 'accept', {'intent_id': 'a-method'})
            dice.assert_not_called()

    def test_door_changes_make_other_offer_stale_but_visits_do_not(self):
        self.propose('A', 'a-method')
        self.propose('B', 'b-method')
        version = self.look()['scene']['door']['version']
        self.call('B', 'visit')
        self.call('C', 'visit')
        self.offer('B', 'a-method')
        self.offer('C', 'b-method')
        self.assertEqual(self.look()['scene']['door']['version'], version)
        with patch('agent_world.world_context.secrets.randbelow', return_value=19):
            self.call('A', 'accept', {'intent_id': 'a-method'})
        with patch('agent_world.world_context.secrets.randbelow') as dice:
            with self.assertRaises(StateConflict):
                self.call('B', 'accept', {'intent_id': 'b-method'})
            dice.assert_not_called()

    def test_claim_replay_does_not_claim_new_task_and_assignment_survives_restart(self):
        self.propose()
        original = self.call('B', 'visit', operation='claim-once')
        self.runtime = WorldRuntime(self.db)
        self.runtime.install_world('test', WORLD)
        self.assertEqual(self.look('B')['additional_task']['intent_id'], 'a-method')
        self.offer()
        self.propose('C', 'c-method')
        replay = self.call('B', 'visit', operation='claim-once')
        self.assertTrue(replay['replayed'])
        self.assertEqual(original['result'], replay['result'])
        self.assertIsNone(self.look('B')['additional_task'])
        self.assertEqual(self.call('B', 'visit')['result']['additional_task']['intent_id'], 'c-method')


if __name__ == '__main__':
    unittest.main()
