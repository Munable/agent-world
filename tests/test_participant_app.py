"""Formal key admission and recovery of participant-assigned judgment work."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from agent_world import WorldRuntime
from agent_world.connect import connect_world
from examples.participant_app import create_participant_application, existing_world, initialize_local


class ParticipantApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='participant-http-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.db, self.private = root / 'data' / 'world.sqlite3', root / 'private'
        self.setup = initialize_local(self.db, self.private)
        self.origin = 'http://127.0.0.1:8802'

    def connect(self, client, label, suffix='one', access='control'):
        result = connect_world(self.origin, key_path=Path(self.setup['participants'][label]['key_path']),
                               output_path=self.private / f'{label}-{suffix}.json', access_mode=access, client=client)
        headers = json.loads(result.output_path.read_text())['mcp']['agent_world']['headers']
        return result, headers

    def call(self, client, headers, name, arguments=None, operation=None):
        body = {'arguments': arguments or {}}
        if operation:
            body['operation_id'] = operation
        return client.post('/v1/functions/participant.' + name + '/invoke', headers=headers, json=body)

    def test_formal_identity_normal_visit_assigns_work_and_same_key_recovers_it(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, a = self.connect(client, 'A')
            first_b, b = self.connect(client, 'B')
            for label, headers in (('A', a), ('B', b)):
                response = self.call(client, headers, 'join', {'name': label}, 'join-' + label)
                self.assertEqual(response.status_code, 200, response.text)
            seen = self.call(client, a, 'look').json()['result']
            proposed = self.call(client, a, 'propose', {'intent_id': 'formal-request', 'text': 'Pull the existing ring.',
                                'target_version': seen['scene']['door']['version']}, 'propose-A')
            self.assertEqual(proposed.status_code, 200, proposed.text)
            assigned = self.call(client, b, 'visit', operation='visit-B')
            self.assertEqual(assigned.status_code, 200, assigned.text)
            self.assertEqual(assigned.json()['result']['additional_task']['owner'], self.setup['participants']['A']['role_id'])
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            next_b, b = self.connect(client, 'B', 'resumed')
            self.assertEqual(first_b.role_id, next_b.role_id)
            pending = self.call(client, b, 'look').json()['result']['additional_task']
            self.assertEqual(pending['intent_id'], 'formal-request')
            response = self.call(client, b, 'answer', {'intent_id': 'formal-request', 'decision': 'reject',
                                 'reason': 'A bounded rejection, without changing the door.'}, 'answer-B')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['result']['intent']['status'], 'rejected')

    def test_observe_can_read_but_cannot_claim_or_answer_and_anonymous_is_denied(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, control = self.connect(client, 'A')
            self.assertEqual(self.call(client, control, 'join', {'name': 'A'}, 'join-A').status_code, 200)
            _, observe = self.connect(client, 'A', 'observe', access='observe')
            self.assertEqual(self.call(client, observe, 'look').status_code, 200)
            self.assertEqual(self.call(client, observe, 'visit', operation='observe-claim').status_code, 403)
            self.assertEqual(client.get('/v1/bootstrap').status_code, 401)
            self.assertEqual(client.get('/').status_code, 200)
            self.assertFalse(hasattr(client.app.state, 'timer_worker'))

    def test_new_private_setup_never_resets_existing_db_and_has_no_gm(self):
        before = self.db.read_bytes()
        self.assertEqual(set(self.setup['participants']), {'A', 'B', 'C'})
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertNotIn('token', json.dumps(self.setup))
        with self.assertRaises(ValueError):
            initialize_local(self.db, self.private)
        self.assertEqual(self.db.read_bytes(), before)
        with self.assertRaises(ValueError):
            existing_world(self.db, 'another-world')

    def test_answer_wakes_owner_and_replay_does_not_duplicate_private_notification(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, a = self.connect(client, 'A')
            _, b = self.connect(client, 'B')
            _, c = self.connect(client, 'C')
            for label, headers in (('A', a), ('B', b), ('C', c)):
                self.call(client, headers, 'join', {'name': label}, 'join-' + label).raise_for_status()
            self.call(client, a, 'propose', {'intent_id': 'awake', 'text': 'Pull and break the existing lock.',
                      'target_version': 1}, 'propose').raise_for_status()
            claimed = self.call(client, b, 'visit', operation='claim')
            assigned = client.get('/v1/changes', headers=a).json()
            self.assertEqual(assigned['events'][-1]['kind'], 'participant.intent_assigned')
            anchor = assigned['next_cursor']
            empty_page_read = threading.Event()
            read_changes_page = WorldRuntime.read_changes_page
            def observe_empty_page(runtime, universe, role_id, after_seq=0, limit=50, *, identity_token=None):
                page = read_changes_page(runtime, universe, role_id, after_seq, limit,
                                         identity_token=identity_token)
                if (role_id == self.setup['participants']['A']['role_id']
                        and after_seq == anchor and not page['events']):
                    empty_page_read.set()
                return page
            def wait():
                return client.get('/v1/changes/wait', headers=a, params={'after': anchor, 'timeout': 1})
            args = {'intent_id': 'awake', 'decision': 'allow', 'dc': 18, 'reason': 'A difficult pull on existing material.'}
            with patch.object(WorldRuntime, 'read_changes_page', observe_empty_page):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(wait)
                    self.assertTrue(empty_page_read.wait(1))
                    self.assertFalse(pending.done())
                    first = self.call(client, b, 'answer', args, 'answer')
                    response = pending.result(timeout=2)
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertFalse(page['timed_out'])
            self.assertEqual([x['kind'] for x in page['events']], ['participant.intent_answered'])
            payload = page['events'][0]['payload']
            self.assertEqual(payload, {'intent_id': 'awake', 'status': 'offered', 'intent_version': 3,
                             'target_id': 'ancient-stone-door', 'target_version': 1})
            self.assertNotIn('reason', payload)
            replay = self.call(client, b, 'answer', args, 'answer')
            self.assertTrue(replay.json()['replayed'])
            self.assertEqual(first.json()['event_seqs'], replay.json()['event_seqs'])
            self.assertEqual(len(client.get('/v1/changes', headers=a, params={'after': anchor}).json()['events']), 1)
            outsider = client.get('/v1/changes', headers=c, params={'after': anchor}).json()
            self.assertEqual(outsider['events'], [])
            self.assertEqual(self.call(client, a, 'look').json()['result']['own_intents'][0]['status'], 'offered')

    def test_no_answer_timeout_preserves_assigned_intent_and_before_wait_answer_is_readable(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, a = self.connect(client, 'A')
            _, b = self.connect(client, 'B')
            for label, headers in (('A', a), ('B', b)):
                self.call(client, headers, 'join', {'name': label}, 'join-' + label).raise_for_status()
            self.call(client, a, 'propose', {'intent_id': 'waiting', 'text': 'Use the existing ring.',
                      'target_version': 1}, 'propose').raise_for_status()
            self.call(client, b, 'visit', operation='claim').raise_for_status()
            anchor = client.get('/v1/changes', headers=a).json()['next_cursor']
            timed = client.get('/v1/changes/wait', headers=a, params={'after': anchor, 'timeout': 0}).json()
            self.assertTrue(timed['timed_out'])
            self.assertEqual(self.call(client, a, 'look').json()['result']['own_intents'][0]['status'], 'assigned')
            self.call(client, b, 'answer', {'intent_id': 'waiting', 'decision': 'reject',
                      'reason': 'Method is not sufficient.'}, 'answer').raise_for_status()
            ready = client.get('/v1/changes/wait', headers=a, params={'after': timed['next_cursor'], 'timeout': 0}).json()
            self.assertFalse(ready['timed_out'])
            self.assertEqual(ready['events'][0]['payload']['status'], 'rejected')

    def test_withdraw_and_execution_notify_assignee_without_global_broadcast(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, a = self.connect(client, 'A')
            _, b = self.connect(client, 'B')
            _, c = self.connect(client, 'C')
            for label, headers in (('A', a), ('B', b), ('C', c)):
                self.call(client, headers, 'join', {'name': label}, 'join-' + label).raise_for_status()
            self.call(client, a, 'propose', {'intent_id': 'cancel', 'text': 'Pull the ring.', 'target_version': 1}, 'propose-1')
            self.call(client, b, 'visit', operation='claim-1')
            self.call(client, a, 'withdraw', {'intent_id': 'cancel'}, 'withdraw')
            self.call(client, a, 'propose', {'intent_id': 'execute', 'text': 'Pull the ring.', 'target_version': 1}, 'propose-2')
            self.call(client, b, 'visit', operation='claim-2')
            self.call(client, b, 'answer', {'intent_id': 'execute', 'decision': 'allow', 'dc': 18, 'reason': 'Try it.'}, 'answer')
            accepted = self.call(client, a, 'accept', {'intent_id': 'execute'}, 'accept')
            self.assertEqual(accepted.status_code, 200, accepted.text)
            events = client.get('/v1/changes', headers=b).json()['events']
            self.assertEqual([x['kind'] for x in events], ['participant.intent_withdrawn', 'participant.intent_executed'])
            self.assertEqual(events[1]['payload']['status'], 'executed')
            self.assertEqual(self.call(client, a, 'accept', {'intent_id': 'execute'}, 'accept').json()['event_seqs'], accepted.json()['event_seqs'])
            current_b = self.call(client, b, 'look').json()['result']
            received = {item['intent_id']: item for item in current_b['received_intents']}
            self.assertEqual(set(received), {'cancel', 'execute'})
            self.assertEqual(received['cancel']['status'], 'withdrawn')
            self.assertIsNone(received['cancel']['result'])
            self.assertEqual(received['execute']['status'], 'executed')
            self.assertEqual(received['execute']['result'], accepted.json()['result']['intent']['result'])
            self.assertIsNone(current_b['additional_task'])
            current_c = self.call(client, c, 'look').json()['result']
            self.assertEqual(current_c['received_intents'], [])
            self.assertEqual(current_c['own_intents'], [])
            self.assertIsNone(current_c['additional_task'])

    def test_expired_notifications_recover_current_offer_after_application_restart(self):
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, a = self.connect(client, 'A')
            _, b = self.connect(client, 'B')
            _, c = self.connect(client, 'C')
            for label, headers in (('A', a), ('B', b), ('C', c)):
                self.call(client, headers, 'join', {'name': label}, 'join-' + label).raise_for_status()
            self.call(client, a, 'propose', {'intent_id': 'retained', 'text': 'Force the existing door.', 'target_version': 1}, 'propose')
            self.call(client, b, 'visit', operation='claim')
            self.call(client, b, 'answer', {'intent_id': 'retained', 'decision': 'allow', 'dc': 18, 'reason': 'Difficult.'}, 'answer')
            latest = client.get('/v1/changes', headers=a).json()['next_cursor']
        runtime = WorldRuntime(self.db)
        runtime.cleanup_events('participant-decisions', latest)
        with TestClient(create_participant_application(self.db), base_url=self.origin) as client:
            _, recovered = self.connect(client, 'A', 'after-expiry')
            expired = client.get('/v1/changes/wait', headers=recovered, params={'after': 0, 'timeout': 0})
            self.assertEqual(expired.status_code, 409, expired.text)
            self.assertEqual(expired.json()['error'], 'CursorExpired')
            self.assertEqual(expired.json()['recovery'], 'bootstrap')
            bootstrap = client.get('/v1/bootstrap', headers=recovered).json()
            restored = self.call(client, recovered, 'look').json()['result']['own_intents'][0]
            self.assertEqual(restored['status'], 'offered')
            self.assertIsNone(restored['result'])
            self.assertEqual(bootstrap['latest_event_seq'], latest)
            _, recovered_b = self.connect(client, 'B', 'after-expiry')
            client.get('/v1/bootstrap', headers=recovered_b).raise_for_status()
            current_b = self.call(client, recovered_b, 'look').json()['result']
            self.assertEqual(len(current_b['received_intents']), 1)
            retained = current_b['received_intents'][0]
            self.assertEqual(retained['intent_id'], 'retained')
            self.assertEqual(retained['status'], 'offered')
            self.assertIsNone(retained['result'])
            self.assertIsNone(current_b['additional_task'])
            _, recovered_c = self.connect(client, 'C', 'after-expiry')
            current_c = self.call(client, recovered_c, 'look').json()['result']
            self.assertEqual(current_c['received_intents'], [])
            self.assertEqual(current_c['own_intents'], [])
            self.assertIsNone(current_c['additional_task'])


if __name__ == '__main__':
    unittest.main()
