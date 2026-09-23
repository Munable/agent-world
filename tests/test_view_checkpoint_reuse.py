"""Fresh authorized reads may reuse an unchanged non-timeline checkpoint."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from agent_world import WorldRuntime, ViewSpec, FunctionSpec, FunctionOutcome, StreamSpec, PresentationCue
from agent_world.world_sdk import install_world
from agent_world.errors import InvalidIdentityToken, PermissionDenied
from agent_world.world_views import ViewResetRequired
from tests.test_world_data import WORLD, EMPTY, projection

def publish(ctx,args):
    cue=PresentationCue(ctx.stream_event_id('shared','line'),ctx.actor_role_id,'speech','start','line',{'text':'hello'})
    return FunctionOutcome({},(cue.publish('shared',key='line'),))

class CheckpointReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=WorldRuntime(Path(self.tmp.name)/'world.sqlite3')
        self.world=replace(WORLD,functions=WORLD.functions+(FunctionSpec('publish',publish,EMPTY),),
            streams=(StreamSpec('shared',public=True),),views=(
                ViewSpec('main',projection,public=True,streams=('shared',)),
                ViewSpec('timeline',projection,timeline=True),
                ViewSpec('clock',lambda c,a:{'meta':{'now':c.now}},public=True),
                ViewSpec('guarded',projection,authorize=lambda c,a:c.get_state('objects','gate',{}).get('open',True))))
        install_world(self.w,'u',self.world)
        self.role=self.w.create_role('A')['role_id'];self.cred=self.w.issue_identity_token('u',self.role)
        self.clock=patch('agent_world.runtime_views.time.time',return_value=2000).start();self.addCleanup(patch.stopall)
    def snapshot(self,name='main'):
        return self.w.public_view_snapshot('u',name)
    def sync(self,snap):
        return self.w.public_view_sync('u',snap['cursor'])
    def call(self,name,args,op):
        return self.w.call_function('u',name,self.role,args,operation_id=op,identity_token=self.cred['token'])
    def write(self,key,value,op='write'):
        return self.call('object.change',{'key':key,'value':value},op)
    def count(self):
        with self.w._conn(readonly=True) as c:return c.execute('SELECT count(*) FROM view_checkpoints').fetchone()[0]

    def test_unchanged_sync_has_no_writable_connection_and_fresh_time(self):
        snap=self.snapshot();self.clock.return_value+=2
        with patch.object(self.w,'_conn',wraps=self.w._conn) as conn:
            for _ in range(20):result=self.sync(snap)
        self.assertTrue(conn.call_args_list)
        self.assertTrue(all(c.kwargs.get('readonly') for c in conn.call_args_list))
        self.assertEqual(result['cursor'],snap['cursor'])
        self.assertEqual(result['base_cursor'],snap['cursor'])
        self.assertEqual(result['expires_at'],snap['expires_at'])
        self.assertEqual(result['observed_at'],2002)
        self.assertEqual(result['delta']['entities'],{'upsert':{},'remove':[]})
        self.assertEqual(self.count(),1)

    def test_changed_state_is_visible_immediately(self):
        snap=self.snapshot();self.write('door',{'open':True})
        update=self.sync(snap)
        self.assertNotEqual(update['cursor'],snap['cursor'])
        self.assertEqual(update['delta']['entities']['upsert'],{'door':{'open':True}})
        self.assertEqual(self.sync(update)['cursor'],update['cursor'])
    def test_hidden_write_does_not_leak_via_cursor_rotation(self):
        snap=self.snapshot();self.write('secret',{'visible_to':[self.role],'text':'private'})
        result=self.sync(snap)
        self.assertEqual(result['cursor'],snap['cursor'])
        self.assertNotIn('secret',result['delta']['entities']['upsert'])
    def test_time_dependent_projection_is_recomputed(self):
        snap=self.snapshot('clock');self.clock.return_value+=1
        result=self.sync(snap)
        self.assertNotEqual(result['cursor'],snap['cursor'])
        self.assertEqual(result['delta']['meta']['now'],2001)
    def test_near_expiry_renews_checkpoint(self):
        snap=self.snapshot();self.clock.return_value=snap['expires_at']-30
        result=self.sync(snap)
        self.assertNotEqual(result['cursor'],snap['cursor'])
        self.assertGreater(result['expires_at'],snap['expires_at'])
    def test_expired_checkpoint_is_not_revived(self):
        snap=self.snapshot();self.clock.return_value=snap['expires_at']+1
        with self.assertRaises(ViewResetRequired):self.sync(snap)
    def test_role_credential_revocation_is_rechecked(self):
        snap=self.w.view_snapshot('u',self.role,'main',identity_token=self.cred['token'])
        self.w.revoke_identity_token(self.cred['token_id'])
        with self.assertRaises(InvalidIdentityToken):
            self.w.view_sync('u',self.role,snap['cursor'],identity_token=self.cred['token'])
    def test_view_authorization_is_rechecked(self):
        snap=self.w.view_snapshot('u',self.role,'guarded',identity_token=self.cred['token'])
        self.write('gate',{'open':False})
        with self.assertRaises(PermissionDenied):
            self.w.view_sync('u',self.role,snap['cursor'],identity_token=self.cred['token'])
    def test_role_checkpoint_cannot_be_reused_by_anonymous(self):
        snap=self.w.view_snapshot('u',self.role,'main',identity_token=self.cred['token'])
        with self.assertRaises(ViewResetRequired):self.sync(snap)
    def test_event_only_publication_keeps_live_stream_continuity(self):
        snap=self.snapshot();old=snap['streams']['shared']['cursor']
        self.call('publish',{},'say')
        result=self.sync(snap)
        self.assertEqual(result['cursor'],snap['cursor'])
        events=self.w.read_stream('u','shared',cursor=old)['events']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['payload']['data']['text'],'hello')
        self.assertNotEqual(result['streams']['shared']['cursor'],old)
    def test_legacy_timeline_checkpoint_still_advances(self):
        snap=self.w.view_snapshot('u',self.role,'timeline',identity_token=self.cred['token'])
        update=self.w.view_sync('u',self.role,snap['cursor'],identity_token=self.cred['token'])
        self.assertNotEqual(update['cursor'],snap['cursor'])
        self.assertEqual(update['timeline_cursor'],update['cursor'])
    def test_independent_baselines_do_not_cross(self):
        first=self.snapshot();second=self.snapshot()
        self.assertNotEqual(first['cursor'],second['cursor'])
        self.assertEqual(self.sync(first)['cursor'],first['cursor'])
        self.assertEqual(self.sync(second)['cursor'],second['cursor'])
        self.assertEqual(self.count(),2)
    def test_deleted_checkpoint_requires_reset(self):
        snap=self.snapshot()
        with self.w._conn() as c:c.execute('DELETE FROM view_checkpoints')
        with self.assertRaises(ViewResetRequired):self.sync(snap)
