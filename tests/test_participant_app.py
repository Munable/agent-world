"""Formal key admission and recovery of participant-assigned judgment work."""
import json
from pathlib import Path
import stat
import tempfile
import unittest

from starlette.testclient import TestClient

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


if __name__ == '__main__':
    unittest.main()
