"""Mac experiment runner regressions: synthetic children, no inference/network."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import signal
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'run_river_camp_models.py'
spec = importlib.util.spec_from_file_location('river_camp_model_runner', SOURCE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

GO_PRICE_FIXTURE = '''<a role="tab" href="#tab-panel-0" id="tab-0">Go</a>
<a role="tab" href="#tab-panel-1" id="tab-1">Go Plus</a>
<div id="tab-panel-0" aria-labelledby="tab-0" role="tabpanel"><table>
<tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr>
<tr><td>LongCat 2.5 Preview Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td>Unlimited<span>limited time</span></td></tr>
</table></div>
<div id="tab-panel-1" aria-labelledby="tab-1" role="tabpanel"><table>
<tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr>
<tr><td>LongCat 2.5 Preview Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td>Unlimited</td></tr>
</table></div>
<a role="tab" href="#tab-panel-2" id="tab-2">Go</a>
<div id="tab-panel-2" aria-labelledby="tab-2" role="tabpanel"><table>
<tr><th>Model</th><th>Requests per minute</th><th>Requests per day</th><th>Requests per month</th></tr>
<tr><td>LongCat 2.5 Preview Free</td><td>Unlimited</td><td>Unlimited</td><td>Unlimited</td></tr>
</table></div>'''


@unittest.skipUnless(sys.platform == 'darwin', 'This is the fixed macOS experiment runner')
class OwnedHostRegression(unittest.TestCase):
    def setUp(self):
        root = runner.TASK / 'test-work'
        if not root.is_dir():
            self.skipTest('External project test-work is unavailable')
        self.temporary = tempfile.TemporaryDirectory(prefix='river-camp-runner-test-', dir=root)
        self.root = Path(self.temporary.name)
        self.item = {'stage': 'synthetic-host', 'errors': [], 'cli_cost': 0, 'finished': False}
        self.budget = {'runs': [self.item], 'cli_cost_total': 0}
        self.ledger = self.root / 'model-runs.json'
        runner.write_json(self.ledger, self.budget, exclusive=True)
        self.handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
        self.children = []
        self.addCleanup(self.reap_fixture_children)

    def reap_fixture_children(self):
        for child in self.children:
            runner.terminate(child)
        if any(child.poll() is None for child in self.children):
            # Keep the profile if the OS could not reap an owned child.
            self.temporary._finalizer.detach()
            raise AssertionError('Synthetic child unreaped; retained ' + str(self.root))
        self.temporary.cleanup()

    def exercise(self, kind):
        real_popen, real_write = runner.subprocess.Popen, runner.write_json
        resources = {}

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            if kind == 'spawn_cancel':
                os.kill(os.getpid(), signal.SIGTERM)
            return child

        def write(path, value, **kwargs):
            if kind == 'ledger_failure' and path == self.ledger:
                raise OSError('synthetic ledger write failure')
            return real_write(path, value, **kwargs)

        expected = {'cancel': KeyboardInterrupt, 'log_failure': OSError,
                    'ledger_failure': KeyboardInterrupt, 'spawn_cancel': runner.HostCancelled,
                    'spawn_deadline': TimeoutError}[kind]
        started = time.monotonic() - (runner.TIMEOUT + 0.1 if kind == 'spawn_deadline' else 0)
        with mock.patch.object(runner.subprocess, 'Popen', side_effect=spawn), \
             mock.patch.object(runner, 'write_json', side_effect=write), self.assertRaises(expected):
            with runner.owned_host([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  self.root, {'PATH': '/usr/bin:/bin'}, self.item, self.budget,
                                  self.ledger, self.root, [], started, resources) as child:
                self.assertEqual(os.getpgid(child.pid), child.pid)
                resources['selector'] = selectors.DefaultSelector()
                resources['selector'].register(child.stdout, selectors.EVENT_READ)
                if kind == 'log_failure':
                    class BrokenLog:
                        def write(self, value):
                            raise OSError('synthetic sanitized log write failure')
                    BrokenLog().write('{}\n')
                raise KeyboardInterrupt('synthetic cancellation')
        self.assertIsNotNone(self.children[0].poll())
        self.assertTrue(self.children[0].stdout.closed)
        self.assertTrue(self.item['finished'])
        self.assertTrue(self.item['owned_process_reaped'])
        self.assertEqual(self.item['errors'][0]['exception_type'], expected.__name__)
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        if 'selector' in resources:
            self.assertIsNone(resources['selector'].get_map())
        if kind == 'ledger_failure':
            saved = json.loads((self.root / 'synthetic-host-host-failure.json').read_text())
            self.assertEqual(saved['original_cause']['exception_type'], 'KeyboardInterrupt')
        else:
            saved = json.loads(self.ledger.read_text())
            self.assertTrue(saved['runs'][0]['owned_process_reaped'])

    def test_keyboard_interrupt_reaps_child(self):
        self.exercise('cancel')

    def test_log_write_failure_reaps_child(self):
        self.exercise('log_failure')

    def test_ledger_failure_keeps_original_cause_in_fallback(self):
        self.exercise('ledger_failure')

    def test_spawn_signal_is_deferred_until_child_handle_exists(self):
        self.exercise('spawn_cancel')

    def test_expired_spawn_deadline_reaps_child(self):
        self.exercise('spawn_deadline')

    def test_normal_eof_allows_child_to_exit_without_sigterm(self):
        code = 'import os,time; os.close(1); os.close(2); time.sleep(0.05)'
        with runner.owned_host([sys.executable, '-c', code], self.root, {'PATH': '/usr/bin:/bin'},
                              self.item, self.budget, self.ledger, self.root, [], time.monotonic(), {}) as child:
            self.children.append(child)
            self.assertEqual(child.stdout.read(), b'')
        self.assertEqual(child.returncode, 0)
        self.assertTrue(self.item['owned_process_reaped'])

    def test_watchdog_deadline_reaps_quiet_child_once_with_alarm_disabled(self):
        real_terminate = runner.terminate
        observed = []
        def reap(child):
            observed.append(child.pid)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGALRM)  # A late queued alarm cannot interrupt wait.
            return real_terminate(child)
        with mock.patch.object(runner, 'TIMEOUT', 0.3), \
                mock.patch.object(runner, 'terminate', side_effect=reap), self.assertRaises(TimeoutError):
            with runner.owned_host([sys.executable, '-c', 'import time; time.sleep(30)'], self.root,
                    {'PATH': '/usr/bin:/bin'}, self.item, self.budget, self.ledger, self.root, [],
                    time.monotonic(), {}) as child:
                self.children.append(child)
                child.stdout.read()
        self.assertEqual(observed, [self.children[0].pid])
        self.assertTrue(self.item['owned_process_reaped'])
        self.assertEqual(self.item['errors'][0]['exception_type'], 'TimeoutError')
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)

    def exercise_autonomous_deadline(self, *, ignore_term):
        # Run the real reader/reap path with a local Python fixture, not OpenCode.
        real_popen, real_terminate, real_timer = runner.subprocess.Popen, runner.terminate, signal.setitimer
        budget = {'runs': [], 'cli_cost_total': 0}
        observed = []
        code = ('import os,signal,time; '
                + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
                + 'os.write(1,b"READY\\n"); time.sleep(30)')
        def spawn(*args, **kwargs):
            child = real_popen([sys.executable, '-c', code], **kwargs)
            self.children.append(child)
            return child
        def timer(which, seconds, interval=0):
            # Suppress automatic delivery to force the monotonic-loop deadline;
            # the independent watchdog test covers automatic SIGALRM delivery.
            return real_timer(which, 0, 0)
        def reap(child):
            observed.append(child.pid)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGALRM)
            return real_terminate(child)
        began = time.monotonic()
        with mock.patch.object(runner, 'TIMEOUT', 0.3), \
                mock.patch.object(runner.subprocess, 'Popen', side_effect=spawn), \
                mock.patch.object(runner, 'provider_preflight', return_value={'allowed': True}), \
                mock.patch.object(runner, 'terminate', side_effect=reap), \
                mock.patch.object(signal, 'setitimer', side_effect=timer), \
                mock.patch.dict(os.environ, {'OPENCODE_API_KEY': 'SYNTHETIC_FIXTURE_NOT_A_CREDENTIAL'}), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            runner.run('synthetic-autonomous-deadline', 'synthetic fixture', [], budget,
                       evidence_root=self.root, ledger_path=self.ledger, runtime_root=self.root / 'profiles')
        self.assertLess(time.monotonic() - began, 7.5)
        item = budget['runs'][0]
        self.assertEqual(observed, [self.children[0].pid])
        self.assertTrue(item['owned_process_reaped'])
        self.assertTrue(item['finished'])
        self.assertTrue(any(error['type'] == 'timeout' for error in item['errors']))
        self.assertFalse(any(error['type'] == 'host_exception' for error in item['errors']))
        self.assertIn('first_stdout_seconds', item)
        self.assertEqual(self.children[0].returncode, -signal.SIGKILL if ignore_term else -signal.SIGTERM)
        self.assertTrue(self.children[0].stdout.closed)
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertTrue(json.loads(self.ledger.read_text())['runs'][0]['owned_process_reaped'])

    def test_autonomous_deadline_reaps_quiet_child_once(self):
        self.exercise_autonomous_deadline(ignore_term=False)

    def test_autonomous_deadline_kills_term_ignoring_child_within_existing_grace(self):
        self.exercise_autonomous_deadline(ignore_term=True)

    def test_permission_failure_preserves_unreaped_diagnostic_and_profile(self):
        child = types.SimpleNamespace(pid=999999, stdout=io.BytesIO(), poll=lambda: None)
        with mock.patch.object(runner.subprocess, 'Popen', return_value=child), \
                mock.patch.object(runner.os, 'killpg', side_effect=PermissionError('synthetic owned group permission denial')), \
                self.assertRaises(RuntimeError):
            with runner.owned_host(['synthetic-only'], self.root, {}, self.item, self.budget,
                    self.ledger, self.root, [], time.monotonic(), {'stop_requested': True}):
                pass
        self.assertFalse(self.item['owned_process_reaped'])
        self.assertFalse(self.item['finished'])
        details = next(error['details'] for error in self.item['errors'] if error['type'] == 'owned_cleanup_errors')
        self.assertEqual(details[0]['exception_type'], 'PermissionError')
        self.assertTrue(any(error['type'] == 'owned_process_unreaped' for error in self.item['errors']))
        self.assertTrue(self.root.is_dir())
        self.assertFalse(json.loads(self.ledger.read_text())['runs'][0]['owned_process_reaped'])
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)

    def test_preflight_redirects_and_default_refuses_frozen_metadata(self):
        original = self.root / 'original'
        continuation = original / 'continuation'
        continuation.mkdir(parents=True)
        ledger = original / 'model-runs.json'
        ledger.write_text('{}')
        frozen = original / 'provider-preflight-latest.json'
        frozen.write_text('original metadata remains frozen')
        with mock.patch.object(runner, 'EVIDENCE', original), \
             mock.patch.object(runner, 'CONTINUATION', continuation), \
             mock.patch.object(runner, 'LEDGER', ledger):
            # The explicit original path must stop before importing HTTP/network code.
            with self.assertRaises(AssertionError):
                runner.provider_preflight(original)
            def refresh(path):
                self.assertEqual(path, continuation / 'metadata-refresh')
                runner.write_json(path / 'provider-preflight-latest.json', {'allowed': True})
                return {'allowed': True}
            with mock.patch.object(runner, 'local_preflight'), \
                 mock.patch.object(runner, 'provider_preflight', side_effect=refresh), \
                 mock.patch.object(sys, 'argv', [str(SOURCE), 'preflight']), \
                 contextlib.redirect_stdout(io.StringIO()):
                runner.main()
        self.assertEqual(frozen.read_text(), 'original metadata remains frozen')
        self.assertTrue((continuation / 'metadata-refresh' / 'provider-preflight-latest.json').is_file())

    def test_internal_storage_rejects_wrong_hash_mode_and_symlink(self):
        root = self.root / 'internal'
        root.mkdir(mode=0o700)
        binary = root / 'opencode'
        binary.write_bytes(b'synthetic non-executable fixture')
        binary.chmod(0o700)
        with mock.patch.object(runner, 'INTERNAL_ROOT', root), \
             mock.patch.object(runner, 'INTERNAL_BINARY', binary), \
             mock.patch.object(runner, 'INTERNAL_PROFILES', root / 'profiles'), \
             mock.patch.object(runner, 'BINARY_SHA', runner.digest(binary)):
            self.assertEqual(runner.verified_internal_storage(), (binary, root / 'profiles'))
            (root / '.git').write_text('gitdir: /synthetic/gitdir')
            with self.assertRaises(AssertionError):
                runner.verified_internal_storage()
            (root / '.git').unlink()
            (self.root / '.git').mkdir()
            with self.assertRaises(AssertionError):
                runner.verified_internal_storage()
            (self.root / '.git').rmdir()
            binary.chmod(0o755)
            with self.assertRaises(AssertionError):
                runner.verified_internal_storage()
            binary.chmod(0o700)
            with mock.patch.object(runner, 'BINARY_SHA', 'incorrect'):
                with self.assertRaises(AssertionError):
                    runner.verified_internal_storage()
            binary.rename(root / 'real-binary')
            binary.symlink_to(root / 'real-binary')
            with self.assertRaises(AssertionError):
                runner.verified_internal_storage()

    def test_storage_recovery_does_not_discard_failure_or_reset_count(self):
        failed = {'stage': 'continuation-b-propose', 'finished': True, 'owned_process_reaped': True,
                  'events_received': 0, 'tools': [], 'errors': [{'type': 'timeout'}]}
        budget = {'prior_processes': 3, 'runs': [{'stage': 'continuation-gm-a', 'errors': []},
                                               {'stage': 'continuation-a-accept', 'errors': []}, failed]}
        before = json.dumps(budget, sort_keys=True)
        runner.permitted_storage_failures(budget, retry=True)
        self.assertEqual(json.dumps(budget, sort_keys=True), before)
        with self.assertRaises(AssertionError):
            runner.permitted_storage_failures(budget)
        budget['runs'].append({'stage': 'continuation-retry-b-storage', 'finished': True,
                              'owned_process_reaped': True, 'errors': []})
        runner.permitted_storage_failures(budget)
        with self.assertRaises(AssertionError):
            runner.permitted_storage_failures(budget, retry=True)
        failed['events_received'] = 1
        with self.assertRaises(AssertionError):
            runner.permitted_storage_failures(budget)

    def test_cleanup_inventory_detects_another_pid_opening_each_root(self):
        roots = [self.root / 'external-profile', self.root / 'internal-profile']
        for root in [*roots, runner.INTERNAL_BINARY]:
            name = str(root) if root == runner.INTERNAL_BINARY else str(root / 'opencode.json')
            listed = runner.subprocess.CompletedProcess([], 0, f'p999999\nf12\ntREG\nn{name}\n'.encode(), b'')
            with mock.patch.object(runner.subprocess, 'run', return_value=listed):
                with self.assertRaises(AssertionError):
                    runner.assert_profiles_not_open(roots)
        listed = runner.subprocess.CompletedProcess([], 0,
            b'p999999\nfcwd\ntDIR\nn/unrelated/cwd\nftxt\ntREG\nn/unrelated/text\nftwd\ntDIR\nn/unrelated/thread\nf12\ntREG\nn/unrelated/file\n'
            b'p999998\nf0\ntREG\nn/unrelated/second-process\n', b'')
        with mock.patch.object(runner.subprocess, 'run', return_value=listed) as command:
            runner.assert_profiles_not_open(roots)
            self.assertEqual(command.call_args.args[0], ['/usr/sbin/lsof', '-nP', '-u', str(os.getuid()), '-Fptfn'])
        listed = runner.subprocess.CompletedProcess([], 1, b'', b'cannot inspect a path')
        with mock.patch.object(runner.subprocess, 'run', return_value=listed):
            with self.assertRaises(AssertionError):
                runner.assert_profiles_not_open(roots)
        for code, output in [(1, b''), (0, b''), (0, b'p999999\n')]:
            listed = runner.subprocess.CompletedProcess([], code, output, b'')
            with mock.patch.object(runner.subprocess, 'run', return_value=listed):
                with self.assertRaises(AssertionError):
                    runner.assert_profiles_not_open(roots)

    def test_cleanup_inventory_rejects_malformed_or_unassociated_file_fields(self):
        roots = [self.root / 'owned-profile']
        for output in [b'f12\ntREG\nn/unrelated/file\n', b'p999999\nn/unrelated/file\n',
                       b'p999999\nf\ntREG\nn/unrelated/file\n', b'p999999\nf12x\ntREG\nn/unrelated/file\n',
                       b'p999999\nfunknown\ntREG\nn/unrelated/file\n', b'p999999\nf12\ntREG\nn\n',
                       b'p999999\nf12\n', b'p999999\nf12\nf13\ntREG\nn/unrelated/file\n',
                       b'p999999\nf12\ntREG\nn/unrelated/file\np999998\nn/unrelated/second\n',
                       b'p999999\nf12\ntREG\nn/unrelated/file\nn/unrelated/duplicate\n',
                       b'p999999\nf12\ntREG\nn/unrelated/file\nxunknown\n',
                       b'p999999\ntREG\nf12\nn/unrelated/file\n', b'p999999\nf12\nn/unrelated/file\n',
                       b'p999999\nf12\nt\nn/unrelated/file\n', b'p999999\nf12\ntbad type\nn/unrelated/file\n',
                       b'p999999\nf12\ntREG\ntREG\nn/unrelated/file\n',
                       b'p999999\nf12\ntREG\nn/unrelated/file\ntREG\n']:
            with self.subTest(output=output), mock.patch.object(runner.subprocess, 'run',
                    return_value=runner.subprocess.CompletedProcess([], 0, output, b'')):
                with self.assertRaises(AssertionError):
                    runner.assert_profiles_not_open(roots)

    def test_cleanup_inventory_allows_only_observed_unnamed_numeric_nonfilesystem_types(self):
        roots = [self.root / 'owned-profile']
        base = b'p999999\nfcwd\ntDIR\nn/unrelated/cwd\n'
        valid = base + b''.join(f'f{number}\nt{kind}\nn\n'.encode()
                for number, kind in enumerate(['NPOLICY', 'NEXUS', 'PIPE', 'rte'], 10))
        with mock.patch.object(runner.subprocess, 'run',
                return_value=runner.subprocess.CompletedProcess([], 0, valid, b'')):
            runner.assert_profiles_not_open(roots)
        for descriptor, kind in [('12', 'REG'), ('12', 'DIR'), ('12', 'UNKNOWN'),
                                 ('cwd', 'NPOLICY'), ('txt', 'NEXUS'), ('twd', 'PIPE')]:
            with self.subTest(descriptor=descriptor, kind=kind), mock.patch.object(runner.subprocess, 'run',
                    return_value=runner.subprocess.CompletedProcess([], 0,
                        base + f'f{descriptor}\nt{kind}\nn\n'.encode(), b'')):
                with self.assertRaises(AssertionError):
                    runner.assert_profiles_not_open(roots)

    def test_second_subset_batch_stops_before_any_preflight(self):
        with mock.patch.object(runner, 'local_preflight') as preflight:
            with self.assertRaises(AssertionError):
                runner.subset_probe(2)
            preflight.assert_not_called()

    def test_metadata_recovery_retries_only_read_timeout_once_and_records_endpoint(self):
        class ReadTimeout(Exception):
            pass
        class Response:
            def __init__(self, url):
                self.url, self.status_code = url, 200
                self.text = GO_PRICE_FIXTURE
            def raise_for_status(self):
                pass
            def json(self):
                if '/v1/models' in self.url:
                    return {'data': [{'id': 'longcat-2.5-preview-free'}]}
                return {'opencode-go': {'models': {'longcat-2.5-preview-free': {
                    'cost': {'input': 0, 'output': 0, 'cache_read': 0}, 'tool_call': True}}}}
        attempts = []
        outcomes = [ReadTimeout('synthetic read timeout')]
        class Client:
            def __init__(self, **kwargs):
                self.options = kwargs
                assert kwargs == {'timeout': 15, 'trust_env': False, 'follow_redirects': False}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get(self, url):
                attempts.append(url)
                if outcomes:
                    raise outcomes.pop(0)
                return Response(url)
        fake_http = types.SimpleNamespace(Client=Client, ReadTimeout=ReadTimeout)
        with mock.patch.dict(sys.modules, {'httpx': fake_http}):
            result = runner.provider_preflight(self.root, retry_read_timeout_once=True)
        self.assertTrue(result['allowed'])
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(result['host_phases'][1]['exception_type'], 'ReadTimeout')
        self.assertEqual(result['host_phases'][2]['attempt'], 2)
        self.assertFalse((self.root / 'provider-preflight-failure.json').exists())
        attempts.clear()
        outcomes.extend([ReadTimeout('first'), ReadTimeout('second')])
        with mock.patch.dict(sys.modules, {'httpx': fake_http}), self.assertRaises(ReadTimeout):
            runner.provider_preflight(self.root, retry_read_timeout_once=True)
        saved = json.loads((self.root / 'provider-preflight-failure.json').read_text())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(saved['failed_endpoint'], attempts[-1])
        self.assertFalse(saved['model_host_started'])
        self.assertEqual(saved['host_phases'][-1]['attempt'], 2)
        (self.root / 'provider-preflight-failure.json').unlink()
        attempts.clear()
        outcomes.append(ValueError('synthetic unsupported response'))
        with mock.patch.dict(sys.modules, {'httpx': fake_http}), self.assertRaises(ValueError):
            runner.provider_preflight(self.root, retry_read_timeout_once=True)
        self.assertEqual(len(attempts), 1, 'No retries for other exceptions')

    def test_preflight_failure_gate_preserves_input_facts_and_host_count(self):
        continuation = self.root / 'continuation'
        continuation.mkdir(mode=0o700)
        actors = {name: {'role_id': name, 'config': str(self.root / (name + '.json'))}
                  for name in ['A', 'B', 'GM']}
        (self.root / 'B.json').write_text(json.dumps({'mcp': {'agent_world': {'headers': {}}}}))
        _, prompt, allowed = runner.continuation_prompt('b-propose', actors)
        step = {'step': 'b-propose', 'prompt': prompt, 'allowed_tools': allowed}
        runner.write_json(continuation / 'plan.json', {'steps': [step]})
        runner.write_json(continuation / 'storage-retry-plan.json', {
            **step, 'operation_id': 'continuation-b-propose'})
        failed = {'stage': 'continuation-b-propose', 'finished': True, 'owned_process_reaped': True,
                  'events_received': 0, 'tools': [], 'errors': [{'type': 'timeout'}], 'pid': 999993}
        budget = {'prior_processes': 3, 'cli_cost_total': 0, 'prior_evidence_sha256': {'frozen': 'unchanged'},
                  'runs': [{'stage': 'continuation-gm-a', 'errors': [], 'finished': True, 'exit_code': 0, 'pid': 999991},
                           {'stage': 'continuation-a-accept', 'errors': [], 'finished': True, 'exit_code': 0, 'pid': 999992}, failed]}
        runner.write_json(continuation / 'model-runs.json', budget)
        stage = 'continuation-retry-b-storage'
        runner.write_json(continuation / (stage + '-preflight-failure.json'), {
            'exception_type': 'httpx.ReadTimeout', 'model_host_started': False,
            'existing_model_hosts': 6, 'state_unchanged': True, 'failed_endpoint': 'not_retained_by_current_preflight'})
        runner.write_json(continuation / (stage + '-input.json'), {
            'actor_role_id': 'B', 'prompt': prompt, 'allowed_tools': allowed})
        view = {'intents': [{'key': runner.A_INTENT, 'value': {'owner': 'A', 'status': 'executed'}, 'version': 3}],
                'inventory': {'value': {'wood': 4, 'rope': 3, 'stone': 3}, 'version': 2},
                'route': {'value': {'passage': 'foot', 'stability': 1, 'exposure': 2}, 'version': 2},
                'authority': {'epoch': 1}, 'members': ['A', 'B', 'GM'], 'private_notes': [], 'now': 1}
        snapshots = {name: {**view, 'role_id': name} for name in actors}
        for suffix in ['before', 'after']:
            runner.write_json(continuation / (stage + '-' + suffix + '-authority.json'), snapshots)
        class Client:
            def __init__(self, **kwargs):
                assert kwargs['headers'] == {} and kwargs['trust_env'] is False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get(self, url):
                assert url.endswith('/v1/receipts/continuation-b-propose')
                return types.SimpleNamespace(status_code=404)
        with mock.patch.object(runner, 'CONTINUATION', continuation), \
             mock.patch.object(runner, 'INTERNAL_PROFILES', self.root / 'internal-profiles'), \
             mock.patch.object(runner, 'CONTINUATION_RUN_ROOT', self.root / 'external-profiles'), \
             mock.patch.object(runner.subprocess, 'run', return_value=runner.subprocess.CompletedProcess([], 1, b'', b'')) as pid_check, \
             mock.patch.object(runner, 'frozen_model_evidence', return_value={'frozen': 'unchanged'}), \
             mock.patch.object(runner, 'inspect_actor', side_effect=lambda actor: {**snapshots[actor['role_id']], 'now': 99}), \
             mock.patch.dict(sys.modules, {'httpx': types.SimpleNamespace(Client=Client)}):
            before_hash = runner.digest(continuation / 'model-runs.json')
            checked = runner.preflight_failure_gate({'actors': actors})
            self.assertEqual(checked['existing_hosts'], 6)
            self.assertEqual(checked['operation_id'], 'continuation-b-propose')
            self.assertTrue(checked['owned_recorded_pids_currently_absent'])
            self.assertEqual(checked['ledger_sha256'], before_hash)
            self.assertEqual(runner.digest(continuation / 'model-runs.json'), before_hash)
            for name, sha in checked['preserved_failure_sha256'].items():
                self.assertEqual(runner.digest(continuation / name), sha)
            pid_check.return_value = runner.subprocess.CompletedProcess([], 0, b'999993\n', b'')
            with self.assertRaises(AssertionError):
                runner.preflight_failure_gate({'actors': actors})
            pid_check.return_value = runner.subprocess.CompletedProcess([], 1, b'', b'')
            snapshots['B']['inventory'] = {**view['inventory'], 'version': 3}
            with self.assertRaises(AssertionError):
                runner.preflight_failure_gate({'actors': actors})
            snapshots['B']['inventory'] = view['inventory']
            (self.root / 'internal-profiles' / stage).mkdir(parents=True)
            with self.assertRaises(AssertionError):
                runner.preflight_failure_gate({'actors': actors})

    def test_recovery_price_ttl_and_nonzero_price_stop_before_private_profile(self):
        price = {'model': runner.MODEL, 'allowed': True, 'catalog_listed': True,
                 'tool_call': True, 'official_free_row_confirmed': True, 'paid_fallback': False,
                 'catalog_source': 'https://opencode.ai/zen/go/v1/models',
                 'metadata_source': 'https://models.dev/api.json',
                 'official_source': 'https://opencode.ai/docs/go/', 'checked_at': 900,
                 'declared_price': {'input': 0, 'output': 0, 'cache_read': 0}}
        with mock.patch.object(runner.time, 'time', return_value=1000):
            self.assertEqual(runner.verified_recovery_price(price), price)
            for update in [{'checked_at': 699}, {'checked_at': 1001}, {'model': 'other/free'},
                           {'declared_price': {'input': 0, 'output': 1, 'cache_read': 0}},
                           {'declared_price': {'input': 0, 'output': 0}}, {'tool_call': False}]:
                with self.assertRaises(AssertionError):
                    runner.verified_recovery_price({**price, **update})
            profiles = self.root / 'not-created-profiles'
            with mock.patch.object(runner, 'verified_internal_storage', return_value=(self.root / 'fake-binary', profiles)), \
                 mock.patch.object(runner, 'provider_preflight') as metadata, \
                 mock.patch.object(runner.subprocess, 'Popen') as model, self.assertRaises(AssertionError):
                runner.run('continuation-retry-b-storage', 'preserved', [], {'runs': [], 'cli_cost_total': 0},
                           evidence_root=runner.PREFLIGHT_RECOVERY / 'launch', internal_storage=True,
                           validated_price={**price, 'checked_at': 699})
            metadata.assert_not_called()
            model.assert_not_called()
            self.assertFalse(profiles.exists())

    def test_recovery_price_expiring_during_preparation_or_ledger_starts_no_host(self):
        for slow_phase in ['config', 'ledger']:
            with self.subTest(slow_phase=slow_phase):
                recovery = self.root / (slow_phase + '-recovery')
                evidence = recovery / 'launch'
                evidence.mkdir(parents=True)
                profiles = self.root / (slow_phase + '-profiles')
                ledger = self.root / (slow_phase + '-model-runs.json')
                budget = {'prior_processes': 6, 'runs': [], 'cli_cost_total': 0}
                runner.write_json(ledger, budget)
                price = {'model': runner.MODEL, 'allowed': True, 'catalog_listed': True,
                         'tool_call': True, 'official_free_row_confirmed': True, 'paid_fallback': False,
                         'catalog_source': 'https://opencode.ai/zen/go/v1/models',
                         'metadata_source': 'https://models.dev/api.json',
                         'official_source': 'https://opencode.ai/docs/go/', 'checked_at': 900,
                         'declared_price': {'input': 0, 'output': 0, 'cache_read': 0}}
                original_write = runner.write_json
                with mock.patch.object(runner.time, 'time', return_value=1000) as clock:
                    def slow_write(path, value, **kwargs):
                        original_write(path, value, **kwargs)
                        if (slow_phase == 'config' and path.name == 'opencode.json'
                                or slow_phase == 'ledger' and path == ledger):
                            clock.return_value = 1301
                    with mock.patch.object(runner, 'PREFLIGHT_RECOVERY', recovery), \
                         mock.patch.object(runner, 'verified_internal_storage', return_value=(self.root / 'fake-binary', profiles)), \
                         mock.patch.object(runner, 'write_json', side_effect=slow_write), \
                         mock.patch.object(runner, 'provider_preflight') as metadata, \
                         mock.patch.object(runner.subprocess, 'Popen') as model, \
                         mock.patch.dict(os.environ, {'OPENCODE_API_KEY': 'synthetic-not-a-real-key'}), \
                         self.assertRaises(AssertionError):
                        runner.run('continuation-retry-b-storage', 'preserved', [], budget,
                                   evidence_root=evidence, ledger_path=ledger,
                                   internal_storage=True, validated_price=price)
                    metadata.assert_not_called()
                    model.assert_not_called()
                self.assertEqual(budget['runs'], [])
                self.assertEqual(json.loads(ledger.read_text())['runs'], [])
                failure = json.loads((evidence / 'continuation-retry-b-storage-no-host-price-failure.json').read_text())
                self.assertFalse(failure['model_host_started'])
                self.assertEqual(failure['existing_hosts'], 6)
                self.assertTrue(failure['profile_retained'])
                self.assertTrue((profiles / 'continuation-retry-b-storage').is_dir())

    def test_official_toml_cost_columns_base_and_override_are_required(self):
        provider = ('base_model = "meituan/longcat-2.5-preview"\n'
                    '[cost]\ninput = 0\noutput = 0\ncache_read = 0\n')
        base = 'tool_call = true\n'
        metadata = runner.official_toml_metadata(provider, base)
        self.assertTrue(metadata['tool_call'])
        self.assertEqual(metadata['cost'], {'input': 0, 'output': 0, 'cache_read': 0})
        for invalid in [provider.replace('output = 0', 'output = 0.001'),
                        provider.replace('cache_read = 0\n', ''),
                        'tool_call = false\n' + provider,
                        provider.replace('meituan/longcat-2.5-preview', 'some/other-model'),
                        provider + 'cache_write = 1\n']:
            with self.assertRaises(AssertionError):
                runner.official_toml_metadata(invalid, base)
        with self.assertRaises(AssertionError):
            runner.official_toml_metadata(provider, 'tool_call = false\n')
        self.assertTrue(runner.official_toml_metadata('tool_call = true\n' + provider, 'tool_call = false\n')['tool_call'])

    def test_official_github_source_is_explicit_and_scoped(self):
        for args in [['preflight', '--metadata-source', 'official-github'],
                     ['subset-probe', '--batch', '1', '--metadata-source', 'official-github']]:
            with mock.patch.object(sys, 'argv', [str(SOURCE), *args]), \
                 mock.patch.object(runner, 'provider_preflight') as metadata, \
                 self.assertRaises(AssertionError):
                runner.main()
            metadata.assert_not_called()
        with mock.patch.object(sys, 'argv', [str(SOURCE), 'subset-probe', '--batch', '1',
                                           '--internal-storage', '--metadata-source', 'official-github']), \
             mock.patch.object(runner, 'subset_probe') as selected:
            runner.main()
        selected.assert_called_once_with(1, internal_storage=True, metadata_source='official-github')
        with mock.patch.object(sys, 'argv', [str(SOURCE), 'continue-live', '--step', 'gm-b',
                                           '--manifest', 'synthetic-manifest', '--stage-a', 'synthetic-stage-a',
                                           '--internal-storage', '--metadata-source', 'official-github']), \
             mock.patch.object(runner, 'continue_live') as selected:
            runner.main()
        selected.assert_called_once_with(Path('synthetic-manifest'), Path('synthetic-stage-a'), 'gm-b',
                                         internal_storage=True, metadata_source='official-github')

    def test_official_go_cost_table_cannot_use_goplus_or_usage_rows(self):
        result = runner.official_go_price_row(GO_PRICE_FIXTURE)
        self.assertTrue(result['confirmed_free'])
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['matched_price_row']['panel_id'], 'tab-panel-0')
        # Change only the first (Go) price; the Go Plus Free row cannot rescue it.
        paid_go = GO_PRICE_FIXTURE.replace('<td>Free</td>', '<td>$1</td>', 1)
        self.assertFalse(runner.official_go_price_row(paid_go)['confirmed_free'])
        missing_header = GO_PRICE_FIXTURE.replace('<th>Cached Read</th>', '<th>Requests</th>', 1)
        self.assertFalse(runner.official_go_price_row(missing_header)['confirmed_free'])
        wrong_association = GO_PRICE_FIXTURE.replace('href="#tab-panel-0"', 'href="#other-panel"', 1)
        self.assertFalse(runner.official_go_price_row(wrong_association)['confirmed_free'])
        cache_write_paid = GO_PRICE_FIXTURE.replace('<td>-</td>', '<td>$0.001</td>', 1)
        self.assertFalse(runner.official_go_price_row(cache_write_paid)['confirmed_free'])
        cache_write_free = GO_PRICE_FIXTURE.replace('<td>-</td>', '<td>Free</td>', 1)
        self.assertTrue(runner.official_go_price_row(cache_write_free)['confirmed_free'])

    def test_official_github_preflight_uses_four_fixed_public_sources(self):
        requested = []
        class Response:
            def __init__(self, url):
                self.url, self.status_code = url, 200
                self.text = ('base_model = "meituan/longcat-2.5-preview"\n'
                             '[cost]\ninput = 0\noutput = 0\ncache_read = 0\n' if url == runner.GITHUB_PROVIDER
                             else 'tool_call = true\n' if url == runner.GITHUB_BASE else GO_PRICE_FIXTURE)
                self.content = self.text.encode()
            def raise_for_status(self):
                pass
            def json(self):
                return {'data': [{'id': 'longcat-2.5-preview-free'}]}
        class Client:
            def __init__(self, **kwargs):
                assert kwargs == {'timeout': 15, 'trust_env': False, 'follow_redirects': False}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get(self, url):
                requested.append(url)
                return Response(url)
        with mock.patch.dict(sys.modules, {'httpx': types.SimpleNamespace(Client=Client)}), \
             mock.patch.object(runner, 'CONTINUATION', self.root):
            result = runner.provider_preflight(self.root, metadata_source='official-github')
        self.assertTrue(result['allowed'])
        self.assertEqual(requested, ['https://opencode.ai/zen/go/v1/models', runner.GITHUB_PROVIDER,
                                    runner.GITHUB_BASE, 'https://opencode.ai/docs/go/'])
        self.assertEqual(result['metadata_selection'], 'official-github')
        self.assertEqual(result['metadata_base_source'], runner.GITHUB_BASE)
        self.assertEqual(len(result['metadata_source_sha256']), 64)
        self.assertEqual(result['declared_price'], {'input': 0, 'output': 0, 'cache_read': 0})
        self.assertEqual(result['official_price_dom_evidence']['candidate_count'], 1)

    def schema_failure_budget(self):
        server_error = json.dumps({'error': 'SchemaRejected',
                                   'message': 'schema violation at arguments: additionalProperties',
                                   'tool': 'camp.judge'})
        request = {'operation_id': 'continuation-gm-b', 'arguments': {
            'intent_id': runner.B_INTENT, 'expected_intent_version': 1,
            'inventory_version': 2, 'route_version': 2, 'authority_epoch': 1,
            'decision': 'approve', 'reason': 'synthetic public reason',
            'cost': {'wood': 0, 'rope': 0, 'stone': 0},
            'effects': {'passage': 'foot', 'stability': 1, 'exposure': 2},
            'operation_id': 'continuation-gm-b'}}
        return {'prior_processes': 3, 'cli_cost_total': 0, 'prior_evidence_sha256': {'frozen': 'unchanged'},
                'runs': [
                    {'stage': 'continuation-gm-a', 'finished': True, 'errors': [], 'exit_code': 0},
                    {'stage': 'continuation-a-accept', 'finished': True, 'errors': [], 'exit_code': 0},
                    {'stage': 'continuation-b-propose', 'finished': True, 'owned_process_reaped': True,
                     'events_received': 0, 'tools': [], 'errors': [{'type': 'timeout'}]},
                    {'stage': 'continuation-retry-b-storage', 'finished': True,
                     'owned_process_reaped': True, 'errors': [], 'exit_code': 0},
                    {'stage': 'continuation-gm-b', 'finished': True, 'owned_process_reaped': True,
                     'model_host_started': True, 'exit_code': -15,
                     'errors': [{'type': 'world_tool_error', 'tool': 'agent_world_camp_judge', 'error': server_error},
                                {'type': 'missing_or_invalid_final_json'}],
                     'tools': [{'tool': 'agent_world_camp_inspect', 'status': 'completed'},
                               {'tool': 'agent_world_camp_judge', 'status': 'error', 'error': server_error, 'input': request}]}]}

    def test_schema_recovery_accepts_only_original_exact_failure_then_successful_ninth_host(self):
        budget = self.schema_failure_budget()
        original = json.dumps(budget, sort_keys=True)
        runner.permitted_gm_schema_failure(budget, retry=True)
        self.assertEqual(json.dumps(budget, sort_keys=True), original)
        self.assertEqual(budget['prior_processes'] + len(budget['runs']), 8)
        for damage in ['extra_argument', 'wrong_operation', 'new_error', 'unreaped']:
            invalid = json.loads(original)
            gm = invalid['runs'][-1]
            if damage == 'extra_argument':
                gm['tools'][-1]['input']['arguments']['unsupported'] = True
            elif damage == 'wrong_operation':
                gm['tools'][-1]['input']['operation_id'] = 'other-operation'
            elif damage == 'new_error':
                gm['errors'].append({'type': 'timeout'})
            else:
                gm['owned_process_reaped'] = False
            with self.assertRaises(AssertionError):
                runner.permitted_gm_schema_failure(invalid, retry=True)
        corrected = {'stage': runner.GM_SCHEMA_STAGE, 'finished': True, 'owned_process_reaped': True,
                     'model_host_started': True, 'exit_code': 0, 'errors': [],
                     'tools': [{'tool': 'agent_world_camp_inspect', 'status': 'completed'},
                               {'tool': 'agent_world_camp_judge', 'status': 'completed', 'input': {
                                   'operation_id': 'continuation-gm-b', 'arguments': {
                                       key: value for key, value in budget['runs'][4]['tools'][1]['input']['arguments'].items()
                                       if key != 'operation_id'}}}]}
        budget['runs'].append(corrected)
        runner.permitted_gm_schema_failure(budget, retry=False)
        self.assertEqual(budget['prior_processes'] + len(budget['runs']), 9)
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=True)
        request = corrected['tools'][1]['input']
        request['operation_id'] = 'different-op'
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=False)
        request['operation_id'] = 'continuation-gm-b'
        request['arguments']['operation_id'] = 'continuation-gm-b'
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=False)
        del request['arguments']['operation_id']
        request['arguments']['intent_id'] = 'other-intent'
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=False)
        request['arguments']['intent_id'] = runner.B_INTENT
        request['arguments']['unexpected'] = True
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=False)
        del request['arguments']['unexpected']
        corrected['errors'] = [{'type': 'timeout'}]
        with self.assertRaises(AssertionError):
            runner.permitted_gm_schema_failure(budget, retry=False)

    def test_schema_prepare_preserves_all_prior_evidence_and_appends_only_format(self):
        continuation = self.root / 'continuation'
        continuation.mkdir(mode=0o700)
        private = self.root / 'private'
        private.mkdir(mode=0o700)
        actors = {name: {'role_id': name} for name in ['A', 'B', 'GM']}
        manifest = private / 'setup.json'
        runner.write_json(manifest, {'origin': runner.ORIGIN, 'universe': 'river-camp', 'actors': actors})
        gate = self.root / 'stage-a.json'
        runner.write_json(gate, {'passed': True})
        budget = self.schema_failure_budget()
        budget['stage_a'] = {'path': str(gate), 'sha256': runner.digest(gate)}
        runner.write_json(continuation / 'model-runs.json', budget)
        _, base, allowed = runner.continuation_prompt('gm-b', actors)
        runner.write_json(continuation / 'continuation-gm-b-input.json', {
            'actor_role_id': 'GM', 'prompt': base, 'allowed_tools': allowed})
        view = {'intents': [
            {'key': runner.A_INTENT, 'value': {'status': 'executed'}, 'version': 3},
            {'key': runner.B_INTENT, 'value': {'status': 'pending'}, 'version': 1}],
            'inventory': {'value': {'wood': 4, 'rope': 3, 'stone': 3}, 'version': 2},
            'route': {'value': {'passage': 'foot', 'stability': 1, 'exposure': 2}, 'version': 2},
            'members': ['A', 'B', 'GM'], 'authority': {'epoch': 1}, 'private_notes': [], 'now': 1}
        snapshots = {name: {**view, 'role_id': name} for name in actors}
        for suffix in ['before', 'after']:
            runner.write_json(continuation / ('continuation-gm-b-' + suffix + '-authority.json'), snapshots)
        runner.write_json(continuation / 'continuation-gm-b-receipt-lookup.json', {'http_status': 404})
        for name in ['continuation-gm-b-sanitized.ndjson', 'continuation-gm-b-verification.json',
                     'plan.json', 'storage-retry-plan.json']:
            (continuation / name).write_text('{}\n')
        originals = {path.name: runner.digest(path) for path in continuation.iterdir()}
        plan_path = continuation / 'gm-b-schema-recovery-plan.json'
        with mock.patch.object(runner, 'CONTINUATION', continuation), \
             mock.patch.object(runner, 'GM_SCHEMA_PLAN', plan_path), \
             mock.patch.object(runner, 'PRIVATE', private), \
             mock.patch.object(runner, 'EVIDENCE', self.root), \
             mock.patch.object(runner, 'local_preflight'), \
             mock.patch.object(runner, 'frozen_model_evidence', return_value={'frozen': 'unchanged'}), \
             mock.patch.object(runner, 'load_manifest') as world_http, \
             mock.patch.object(runner, 'provider_preflight') as price_http, \
             mock.patch.object(runner, 'run') as model, contextlib.redirect_stdout(io.StringIO()):
            runner.prepare_gm_schema_retry(manifest, gate)
            for name, sha in originals.items():
                self.assertEqual(runner.digest(continuation / name), sha)
            world_http.assert_not_called()
            price_http.assert_not_called()
            model.assert_not_called()
            result = json.loads(plan_path.read_text())
            self.assertEqual(result['prompt'], base + runner.GM_FORMAT_APPEND)
            self.assertEqual(result['existing_hosts'], 8)
            self.assertEqual(result['operation_id'], 'continuation-gm-b')
            self.assertEqual(result['allowed_tools'], allowed)
            snapshots['B']['inventory'] = {'value': view['inventory']['value'], 'version': 3}
            runner.write_json(continuation / 'continuation-gm-b-after-authority.json', snapshots)
            with self.assertRaises(AssertionError):
                runner.gm_schema_evidence()

    def test_schema_plan_blocks_both_subset_batches_before_preflight(self):
        plan = self.root / 'schema-plan.json'
        plan.write_text('{}')
        with mock.patch.object(runner, 'GM_SCHEMA_PLAN', plan), \
             mock.patch.object(runner, 'local_preflight') as local, \
             mock.patch.object(runner, 'load_manifest') as world_http, \
             mock.patch.object(runner, 'provider_preflight') as metadata, \
             mock.patch.object(runner, 'run') as model:
            for batch in [1, 2]:
                with self.assertRaises(AssertionError):
                    runner.subset_probe(batch, internal_storage=True, metadata_source='official-github')
            with self.assertRaises(AssertionError):
                runner.probes_only(self.root / 'manifest.json', self.root / 'stage-a.json')
            with self.assertRaises(AssertionError):
                runner.probes({}, {})
            local.assert_not_called()
            world_http.assert_not_called()
            metadata.assert_not_called()
            model.assert_not_called()
