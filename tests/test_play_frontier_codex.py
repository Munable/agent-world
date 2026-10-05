from contextlib import redirect_stderr,redirect_stdout
import io,json,os
from pathlib import Path
import subprocess,sys,tempfile,unittest
from tools import play_frontier_codex as play

class WorldLaunchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.config=self.root/'world.json';self.token='awid_synthetic_launcher_test_123456789'
  self.value={'$schema':'https://opencode.ai/config.json','mcp':{'agent_world':{'type':'remote','url':'http://127.0.0.1:8808/mcp','oauth':False,'headers':{'Authorization':'Bearer '+self.token}}}}
  self.write(self.value);self.binary=Path(sys.executable)
 def write(self,value):
  self.config.write_text(json.dumps(value));self.config.chmod(0o600)
 def test_child_receives_original_credential_without_argv_or_parent_env_exposure(self):
  before=self.config.read_bytes();parent=dict(os.environ)
  one,env,info=play.build_launch(self.config,codex=self.binary)
  two,_,other=play.build_launch(self.config,codex=self.binary)
  self.assertNotIn(self.token,' '.join(one)+json.dumps(info));self.assertNotIn('exec',one)
  self.assertNotEqual(info['mcp_server'],other['mcp_server'])
  self.assertEqual([v for k,v in env.items()if k.startswith('AGENT_WORLD_PLAY_TOKEN_')],[self.token])
  self.assertEqual(parent,dict(os.environ));self.assertEqual(before,self.config.read_bytes())
 def test_real_launcher_executes_installed_process_and_preserves_prompt(self):
  output=self.root/'child.json';binary=self.root/'codex-test'
  binary.write_text('#!'+sys.executable+'\nimport os,sys,json\nfrom pathlib import Path\nPath('+repr(str(output))+').write_text(json.dumps({"argv":sys.argv[1:],"tokens":{k:v for k,v in os.environ.items()if k.startswith("AGENT_WORLD_PLAY_TOKEN_")}}))\n')
  binary.chmod(0o700);before=self.config.read_bytes();prompt='我只想看看同一个世界，不要替我行动。'
  r=subprocess.run([sys.executable,str(Path(play.__file__)),'--config',str(self.config),'--codex',str(binary),'--prompt',prompt,'--json','--reasoning-effort','medium'],capture_output=True,text=True,timeout=5)
  self.assertEqual(r.returncode,0,r.stderr);observed=json.loads(output.read_text())
  self.assertEqual(observed['argv'][-1],prompt);self.assertEqual(observed['argv'][0],'exec')
  self.assertIn('--json',observed['argv']);self.assertEqual(list(observed['tokens'].values()),[self.token])
  self.assertNotIn(self.token,json.dumps(observed['argv'])+r.stdout+r.stderr);self.assertEqual(before,self.config.read_bytes())
 def test_check_does_not_run_program_and_invalid_inputs_do_not_echo_secret(self):
  marker=self.root/'started';binary=self.root/'codex-test'
  binary.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');binary.chmod(0o700)
  out=io.StringIO()
  with redirect_stdout(out),redirect_stderr(out):
   self.assertEqual(play.main(['--config',str(self.config),'--codex',str(binary),'--check']),0)
   self.assertEqual(play.main(['--config',str(self.config),'--codex',str(binary),'--prompt',self.token]),2)
   self.assertEqual(play.main(['--config',str(self.config),'--codex',str(binary),'--model',self.token+' invalid']),2)
  self.assertFalse(marker.exists());self.assertNotIn(self.token,out.getvalue())
 def test_unsafe_files_are_refused_without_blocking(self):
  self.config.chmod(0o644)
  with self.assertRaises(play.LaunchError):play.read_connection(self.config)
  self.config.chmod(0o600);link=self.root/'link';link.symlink_to(self.config)
  with self.assertRaises(play.LaunchError):play.read_connection(link)
  fifo=self.root/'pipe';os.mkfifo(fifo,0o600)
  r=subprocess.run([sys.executable,str(Path(play.__file__)),'--config',str(fifo),'--codex',str(self.binary),'--check'],capture_output=True,timeout=3)
  self.assertEqual(r.returncode,2)
 def test_malformed_or_plaintext_remote_configuration_never_launches(self):
  self.value['mcp']['agent_world']['url']='http://remote.example/mcp';self.write(self.value)
  with self.assertRaises(play.LaunchError):play.build_launch(self.config,codex=self.binary)
  self.value['mcp']['agent_world']['url']='https://example.com/mcp?credential='+self.token;self.write(self.value)
  with self.assertRaises(play.LaunchError)as error:play.read_connection(self.config)
  self.assertNotIn(self.token,str(error.exception))
  self.config.write_bytes(b'x'*(play.MAX_CONFIG_BYTES+1))
  with self.assertRaises(play.LaunchError):play.read_connection(self.config)
