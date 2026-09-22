from __future__ import annotations
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import concurrent.futures
import unittest

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, ViewSpec, StreamSpec, StreamEvent, PresentationCue
from agent_world.world_sdk import install_world
from agent_world.errors import PermissionDenied, WorldRuntimeError, RuleViolation, InvalidIdentityToken
from agent_world.world_streams import StreamResetRequired

EMPTY={'type':'object','properties':{},'additionalProperties':False}
ARGS={'type':'object','properties':{'text':{'type':'string'},'fail':{'type':'boolean'}},'required':['text'],'additionalProperties':False}

def speak(ctx,args):
    ctx.set_state('shared','last',{'text':args['text']})
    if args.get('fail'):
        raise RuleViolation('discard')
    eid=ctx.stream_event_id('shared','speech')
    cue=PresentationCue(eid,ctx.actor_role_id,'speech','start','utterance',{'text':args['text']})
    return FunctionOutcome({'message_id':eid},(cue.publish('shared',key='speech'),))

def project(ctx,args):
    return {'entities':{'notice':ctx.get_state('shared','last',{})},'meta':{'public':True}}

def members(ctx):
    return ctx.actor_role_id=='member'

WORLD=WorldDefinition('observation-fixture','Observation fixture',
    (FunctionSpec('speak',speak,ARGS),),
    state_rules=(StateRule('shared','',{'type':'object'}),),
    streams=(StreamSpec('shared',public=True,max_events=10,retention_seconds=60),
             StreamSpec('private',authorize=members,max_events=10,retention_seconds=60)),
    views=(ViewSpec('public',project,public=True,streams=('shared',)),
           ViewSpec('private',project)),
)

class SharedObservation(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'world.sqlite3'
        self.w=WorldRuntime(self.db); install_world(self.w,'u',WORLD)
        self.role=self.w.create_role('one')['role_id']
        self.cred=self.w.issue_identity_token('u',self.role)
        self.n=0
    def speak(self,text='hello',**kw):
        self.n+=1
        return self.w.call_function('u','speak',self.role,{'text':text,**kw},operation_id='op'+str(self.n),identity_token=self.cred['token'])
    def count(self,table):
        with self.w._conn(readonly=True) as c:
            return c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def test_anonymous_observation_creates_no_player_or_token(self):
        before=[self.count(t) for t in ('roles','identity_tokens','operations','role_world_presence')]
        s=self.w.public_view_snapshot('u','public')
        self.assertIsNone(s['viewer_role_id'])
        self.assertIn('shared',s['streams'])
        self.w.public_view_sync('u',s['cursor'])
        self.assertEqual(before,[self.count(t) for t in ('roles','identity_tokens','operations','role_world_presence')])
    def test_public_visibility_is_opt_in(self):
        with self.assertRaises(PermissionDenied): self.w.public_view_snapshot('u','private')
        with self.assertRaises(PermissionDenied): self.w.read_stream('u','private')
        self.assertEqual([x['name'] for x in self.w.list_views('u',None)['views']],['public'])
        self.assertEqual([x['name'] for x in self.w.list_streams('u')['streams']],['shared'])
    def test_role_checkpoint_cannot_be_used_as_anonymous(self):
        s=self.w.view_snapshot('u',self.role,'private',identity_token=self.cred['token'])
        with self.assertRaises(WorldRuntimeError): self.w.public_view_sync('u',s['cursor'])
    def test_failed_action_does_not_publish(self):
        with self.assertRaises(RuleViolation):self.speak(fail=True)
        self.assertEqual(self.count('stream_events'),0)
        self.assertEqual(self.w.read_state_history('u')['changes'],[])
    def test_replay_has_one_event_and_same_message_id(self):
        first=self.speak()
        replay=self.w.call_function('u','speak',self.role,{'text':'hello'},operation_id='op1',identity_token=self.cred['token'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['stream_event_ids'],replay['stream_event_ids'])
        self.assertEqual(first['result']['message_id'],first['stream_event_ids'][0])
        self.assertEqual(self.count('stream_events'),1)
    def test_new_observer_reads_before_it_existed(self):
        self.speak('older')
        page=self.w.read_stream('u','shared')
        self.assertEqual(page['events'][0]['payload']['data']['text'],'older')
        self.assertEqual(page['mode'],'recent')
    def test_bursts_are_ordered_and_paginated_without_gaps(self):
        start=self.w.read_stream('u','shared')['cursor']
        for i in range(9):self.speak(str(i))
        seen=[]; cursor=start
        while True:
            p=self.w.read_stream('u','shared',cursor=cursor,limit=2)
            seen += [e['payload']['data']['text'] for e in p['events']]
            cursor=p['cursor']
            if not p['has_more']:break
        self.assertEqual(seen,[str(i) for i in range(9)])
        self.assertEqual(self.w.read_stream('u','shared',cursor=cursor)['events'],[])
    def test_history_does_not_overwrite_live_anchor(self):
        for i in range(7):self.speak(str(i))
        p=self.w.read_stream('u','shared',limit=2)
        self.assertEqual([e['seq'] for e in p['events']],[6,7])
        old=self.w.read_stream('u','shared',cursor=p['history_cursor'],limit=2)
        self.assertEqual([e['seq'] for e in old['events']],[4,5])
        self.speak('new')
        forward=self.w.read_stream('u','shared',cursor=p['cursor'])
        self.assertEqual([e['seq'] for e in forward['events']],[8])
    def test_snapshot_anchor_does_not_skip_a_later_publication(self):
        snap=self.w.public_view_snapshot('u','public')
        self.speak('between snapshot and read')
        page=self.w.read_stream('u','shared',cursor=snap['streams']['shared']['cursor'])
        self.assertEqual(len(page['events']),1)
    def test_cursor_is_scoped_and_authenticated(self):
        self.speak()
        p=self.w.read_stream('u','shared',self.role,identity_token=self.cred['token'])
        with self.assertRaises(StreamResetRequired):self.w.read_stream('u','shared',cursor=p['cursor'])
        token2=self.w.issue_identity_token('u',self.role)['token']
        with self.assertRaises(StreamResetRequired):self.w.read_stream('u','shared',self.role,identity_token=token2,cursor=p['cursor'])
        with self.assertRaises(StreamResetRequired):self.w.read_stream('u','shared',cursor=p['cursor']+'bad')
        install_world(self.w,'other',WORLD)
        with self.assertRaises(StreamResetRequired):self.w.read_stream('other','shared',cursor=p['cursor'])
    def test_revocation_is_checked_on_every_read(self):
        p=self.w.read_stream('u','shared',self.role,identity_token=self.cred['token'])
        self.w.revoke_identity_token(self.cred['token_id'])
        with self.assertRaises(InvalidIdentityToken):self.w.read_stream('u','shared',self.role,identity_token=self.cred['token'],cursor=p['cursor'])
    def test_stream_policy_is_rechecked(self):
        self.w.read_stream('u','private','member')
        with self.assertRaises(PermissionDenied):self.w.read_stream('u','private','outsider')
    def test_retention_gap_is_explicit_and_heads_do_not_rewind(self):
        first=self.w.read_stream('u','shared')['cursor']
        for i in range(12):self.speak(str(i))
        self.w.apply_stream_retention('u')
        self.assertEqual(self.count('stream_events'),10)
        with self.assertRaises(StreamResetRequired):self.w.read_stream('u','shared',cursor=first)
        recent=self.w.read_stream('u','shared',limit=100)
        self.assertTrue(recent['history_truncated'])
        self.assertEqual(recent['events'][0]['seq'],3)
        self.speak('next')
        self.assertEqual(self.w.read_stream('u','shared',limit=1)['events'][0]['seq'],13)
    def test_recipient_cleanup_is_not_shared_stream_cleanup(self):
        self.speak()
        self.w.cleanup_events('u',999)
        self.assertEqual(self.count('stream_events'),1)
    def test_restart_and_two_runtimes_share_feed(self):
        start=self.w.read_stream('u','shared')['cursor']
        self.speak()
        other=WorldRuntime(self.db); install_world(other,'u',WORLD)
        self.assertEqual(len(other.read_stream('u','shared',cursor=start)['events']),1)
    def test_world_version_changes_require_new_cursor(self):
        p=self.w.read_stream('u','shared')
        install_world(self.w,'u',replace(WORLD,version=2))
        with self.assertRaises(StreamResetRequired):self.w.read_stream('u','shared',cursor=p['cursor'])
    def test_observers_cannot_write(self):
        observer=self.w.issue_identity_token('u',self.role,access_mode='observe')['token']
        with self.assertRaises(PermissionDenied):self.w.call_function('u','speak',self.role,{'text':'bad'},operation_id='bad',identity_token=observer)
        self.assertEqual(self.count('stream_events'),0)
    def test_read_hook_cannot_emit(self):
        install_world(self.w,'u',replace(WORLD,version=2,functions=(FunctionSpec('readbad',lambda c,a:FunctionOutcome({},(StreamEvent('shared','bad',{}),)),EMPTY,access='read'),)))
        with self.assertRaises(WorldRuntimeError):self.w.call_function('u','readbad',self.role,{})
        self.assertEqual(self.count('stream_events'),0)
    def test_duplicate_publication_keys_roll_back(self):
        def duplicates(c,a):return FunctionOutcome({},(StreamEvent('shared','x',{},key='same'),StreamEvent('shared','x',{},key='same')))
        install_world(self.w,'u',replace(WORLD,version=2,functions=(FunctionSpec('dupe',duplicates,EMPTY),)))
        with self.assertRaises(WorldRuntimeError):self.w.call_function('u','dupe',self.role,{},operation_id='dupe')
        self.assertEqual(self.count('stream_events'),0)
    def test_concurrent_publication_has_unique_stream_local_order(self):
        workers=[WorldRuntime(self.db) for _ in range(4)]
        for w in workers:install_world(w,'u',WORLD)
        def run(i):return workers[i].call_function('u','speak',self.role,{'text':str(i)},operation_id='parallel'+str(i))
        with concurrent.futures.ThreadPoolExecutor() as pool:list(pool.map(run,range(4)))
        events=self.w.read_stream('u','shared')['events']
        self.assertEqual([e['seq'] for e in events],[1,2,3,4])
        self.assertEqual(len({e['event_id'] for e in events}),4)
    def test_public_stream_never_contains_private_stream_payload(self):
        def private(c,a):return FunctionOutcome({},(StreamEvent('private','secret',{'value':'PRIVATE'}),))
        install_world(self.w,'u',replace(WORLD,version=2,functions=(FunctionSpec('private.write',private,EMPTY),)))
        self.w.call_function('u','private.write',self.role,{},operation_id='private')
        self.assertNotIn('PRIVATE',str(self.w.read_stream('u','shared')))
        self.assertEqual(len(self.w.read_stream('u','private','member')['events']),1)

if __name__=='__main__':unittest.main()
