from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest

import httpx

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, StateRule, FunctionOutcome
from agent_world.errors import InvalidArguments, OperationConflict, SchemaRejected
from agent_world.runtime_contracts import duration, identifier, json_text, validate
from tests.live_server import LiveServer


class InputBoundaryTests(unittest.TestCase):
    def test_unknown_field_diagnostic_identifies_name_without_rejected_values(self):
        schema = {'type': 'object', 'properties': {'arguments': {
            'type': 'object', 'properties': {'request_id': {'type': 'string'}},
            'patternProperties': {'^extension_': {'type': 'string'}},
            'additionalProperties': False}}, 'additionalProperties': False}
        good = {'arguments': {'request_id': 'r', 'extension_note': 'allowed'}}
        validate(schema, good)
        bad = {'arguments': {**good['arguments'], 'expected_request_version': 'private-value'}}
        with self.assertRaises(SchemaRejected) as raised:
            validate(schema, bad)
        message = str(raised.exception)
        self.assertIn('arguments: additionalProperties', message)
        self.assertIn('"expected_request_version"', message)
        self.assertNotIn('private-value', message)
        self.assertNotIn('extension_note', message)

    def test_unknown_field_diagnostic_bounds_untrusted_names(self):
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        names = ['0unsafe\nname', 'A' * 65] + [f'field_{i}' for i in range(10)]
        with self.assertRaises(SchemaRejected) as raised:
            validate(schema, {name: 'never-echo-this' for name in names})
        message = str(raised.exception)
        self.assertIn('<redacted field name>', message)
        self.assertNotIn('unsafe', message)
        self.assertNotIn('A' * 65, message)
        self.assertNotIn('never-echo-this', message)
        self.assertLess(len(message), 350)

    def test_duration_rejects_unbounded_integers_without_overflow(self):
        for value in (10**400, -(10**400), math.inf, -math.inf, math.nan, True, -1, 86401):
            with self.subTest(value_type=type(value).__name__, negative=value < 0):
                with self.assertRaises(InvalidArguments):
                    duration(value, 'duration')
        self.assertEqual(duration(86400, 'duration'), 86400.0)
        self.assertEqual(duration(0, 'duration', zero=True), 0.0)

    def test_identifier_rejects_unencodable_unicode_before_database_access(self):
        with self.assertRaises(InvalidArguments):
            identifier('op-\ud800', 'operation_id')
        self.assertEqual(identifier('任务-🌍', 'operation_id'), '任务-🌍')
        with tempfile.TemporaryDirectory() as td:
            w = WorldRuntime(Path(td) / 'world.sqlite3')
            role = w.create_role('Caller')['role_id']
            with self.assertRaises(InvalidArguments):
                w.get_receipt('u', role, 'op-\ud800')
            with self.assertRaises(InvalidArguments):
                w.start_activity('u', 'activity', role, 'test', ttl_seconds=10**400)
            with w._conn(readonly=True) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM activities').fetchone()[0], 0)

    def test_json_rejects_unencodable_unicode_without_raw_encoding_error(self):
        for value in ({'text': '\ud800'}, {'text': ['\udfff']}, {'\ud800': 'value'}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(InvalidArguments):
                    json_text(value)
        value={'text': '世界 🌍'}
        encoded=json_text(value)
        size=len(encoded.encode('utf-8'))
        self.assertEqual(json.loads(json_text(value, maximum=size)), value)
        with self.assertRaises(InvalidArguments):
            json_text(value, maximum=size-1)

    def test_write_copies_keep_caller_and_committed_intent_independent(self):
        schema={'type':'object','properties':{'items':{'type':'array','items':{'type':'integer'}}},
                'required':['items'],'additionalProperties':False}
        def mutate(ctx, args):
            args['items'].append(2)
            ctx.set_state('values', 'last', args['items'])
            return FunctionOutcome({'items': args['items']})
        with tempfile.TemporaryDirectory() as td:
            w=WorldRuntime(Path(td)/'world.sqlite3')
            w.install_world('u',WorldDefinition('copies','Copies',(FunctionSpec('values.write',mutate,schema),),
                            state_rules=(StateRule('values','',{'type':'array','items':{'type':'integer'}}),)))
            role=w.create_role('Caller')['role_id']; original={'items':[1]}
            result=w.call_function('u','values.write',role,original,operation_id='same')
            self.assertEqual(original, {'items':[1]})
            self.assertEqual(result['result'], {'items':[1,2]})
            result['result']['items'].append(3)
            replay=w.call_function('u','values.write',role,{'items':[1]},operation_id='same')
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['result'], {'items':[1,2]})
            with self.assertRaises(OperationConflict):
                w.call_function('u','values.write',role,{'items':[1,2]},operation_id='same')
            self.assertEqual(w.get_state('u','values','last')['value'], [1,2])


class InputBoundaryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_unicode_rejection_and_valid_intent_match_python_http_mcp(self):
        with LiveServer('commons') as server:
            alice, alice_identity, _=server.key_identity()
            bob, bob_identity, _=server.key_identity()
            _, outsider_identity, _=server.key_identity()
            alice_auth={'Authorization':'Bearer '+alice_identity['token']}
            bob_auth={'Authorization':'Bearer '+bob_identity['token']}
            with httpx.Client(trust_env=False,timeout=10) as client:
                created=client.post(server.url+'/v1/functions/commons.post.create/invoke',headers=alice_auth,
                                    json={'operation_id':'post','arguments':{'post_id':'p1','text':'hello'}})
                self.assertEqual(created.status_code,200,created.text)
                body={'operation_id':'reply','arguments':{'post_id':'p1','reply_id':'r1','text':'\ud800'}}
                bad=client.post(server.url+'/v1/functions/commons.reply.create/invoke',
                                headers={**bob_auth,'Content-Type':'application/json'},content=json.dumps(body).encode('ascii'))
                self.assertEqual(bad.status_code,422,bad.text)
                self.assertEqual(bad.json()['error'],'InvalidArguments')
                self.assertEqual(bad.json()['recovery'],'fix_request')
                missing=client.get(server.url+'/v1/receipts/reply',headers=bob_auth)
                self.assertEqual(missing.status_code,404,missing.text)
                body['arguments']['text']='世界 🌍'
                good=client.post(server.url+'/v1/functions/commons.reply.create/invoke',headers=bob_auth,json=body)
                self.assertEqual(good.status_code,200,good.text)
                receipt=good.json()
                python_receipt=WorldRuntime(server.db).get_receipt('network',bob['role_id'],'reply',
                                                                  identity_token=bob_identity['token'])
                self.assertEqual(python_receipt,receipt)
                private=client.get(server.url+'/v1/receipts/reply',headers={'Authorization':'Bearer '+outsider_identity['token']})
                self.assertEqual(private.status_code,404,private.text)
            async with server.session(bob_identity) as (session,_):
                replay=await session.call_tool('commons.reply.create',arguments=body)
                self.assertFalse(replay.is_error,replay.structured_content)
                self.assertEqual(replay.structured_content,{**receipt,'replayed':True})
                changed={**body,'arguments':{**body['arguments'],'text':'different'}}
                conflict=await session.call_tool('commons.reply.create',arguments=changed)
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content['error'],'OperationConflict')
            with httpx.Client(trust_env=False,timeout=10) as client:
                changes=client.get(server.url+'/v1/changes',headers=alice_auth).json()
                self.assertEqual([e['kind'] for e in changes['events']],['commons.reply.created'])


if __name__=='__main__':unittest.main()
