import asyncio
from pathlib import Path
import tempfile
import unittest
import json
from fastapi.testclient import TestClient
from agent_world.http_app import create_app
from agent_world.world_sdk import install_world
from agent_world.transport_contracts import WorldGateway
from agent_world.diagnostics import SafeRequestTrace
from tests.test_shared_observation import WORLD

class ObserverTransport(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        app=create_app(Path(self.temp.name)/'world.sqlite3','u',auth_required=True,installer=lambda w,u:install_world(w,u,WORLD))
        self.w=app.state.runtime;self.records=[]
        self.client=TestClient(SafeRequestTrace(app,functions={'speak'},sink=self.records.append));self.addCleanup(self.client.close)
        self.role=self.w.create_role('A')['role_id'];self.token=self.w.issue_identity_token('u',self.role)['token']
        self.auth={'Authorization':'Bearer '+self.token}
    def test_guest_public_view_and_stream_work_without_role(self):
        response=self.client.post('/v1/public/views/public/snapshot',json={})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['viewer_role_id'])
        r=self.client.post('/v1/public/views/private/snapshot',json={})
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.client.get('/v1/public/views').status_code,200)
        self.assertEqual(self.client.post('/v1/functions/speak/invoke',json={'arguments':{'text':'bad'},'operation_id':'x'}).status_code,401)
    def test_no_role_injection_into_public_routes(self):
        self.assertEqual(self.client.post('/v1/public/views/public/snapshot',json={'role_id':self.role}).status_code,422)
        self.assertEqual(self.client.post('/v1/public/streams/shared/read',json={'role_id':self.role}).status_code,422)
    def test_http_write_and_anonymous_read_share_one_publication(self):
        r=self.client.post('/v1/functions/speak/invoke',headers=self.auth,json={'arguments':{'text':'hello'},'operation_id':'one'})
        self.assertEqual(r.status_code,200,r.text)
        page=self.client.post('/v1/public/streams/shared/read',json={}).json()
        self.assertEqual(page['events'][0]['event_id'],r.json()['stream_event_ids'][0])
        self.assertEqual(self.client.post('/v1/public/streams/private/read',json={}).status_code,403)
    def test_sanitized_trace_never_contains_credentials_bodies_or_raw_unknown_path(self):
        self.client.get('/LEAK_RAW_PATH?ticket=LEAK_QUERY',headers={**self.auth,'Cookie':'LEAK_COOKIE'})
        self.client.post('/v1/functions/speak/invoke',headers=self.auth,json={'arguments':{'text':'LEAK_BODY'},'operation_id':'op'})
        serialized=json.dumps(self.records)
        for secret in ('LEAK_RAW_PATH','LEAK_QUERY','LEAK_COOKIE','LEAK_BODY',self.token):self.assertNotIn(secret,serialized)
        self.assertEqual(self.records[-1]['http_status'],200)
        self.assertEqual(self.records[-1]['route'],'/v1/functions/speak/invoke')
    def test_wait_stream_returns_cross_runtime_publication_and_timeout(self):
        gateway=WorldGateway(self.w,'u',auth_required=True)
        cursor=self.w.read_stream('u','shared',self.role,identity_token=self.token)['cursor']
        async def run():
            async def publish():
                await asyncio.sleep(.15)
                await asyncio.to_thread(self.w.call_function,'u','speak',self.role,{'text':'wake'},operation_id='wake',identity_token=self.token)
            task=asyncio.create_task(publish())
            result=await gateway.wait_stream({'stream':'shared','cursor':cursor,'timeout':2},'Bearer '+self.token)
            await task
            self.assertFalse(result['timed_out']);self.assertEqual(len(result['events']),1)
            later=await gateway.wait_stream({'stream':'shared','cursor':result['cursor'],'timeout':0},'Bearer '+self.token)
            self.assertTrue(later['timed_out'])
        asyncio.run(run())
    def test_trace_sink_failure_cannot_change_committed_action(self):
        def broken(record):raise RuntimeError('log disk failed')
        app=create_app(self.w.db_path,'u',auth_required=True,installer=lambda w,u:install_world(w,u,WORLD))
        with TestClient(SafeRequestTrace(app,functions={'speak'},sink=broken)) as c:
            r=c.post('/v1/functions/speak/invoke',headers=self.auth,json={'arguments':{'text':'committed'},'operation_id':'survive'})
            self.assertEqual(r.status_code,200)
        self.assertEqual(self.w.get_receipt('u',self.role,'survive')['result']['message_id'],r.json()['result']['message_id'])

if __name__=='__main__':unittest.main()
