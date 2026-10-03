"""New semantic stage: fixture-only tests, no provider, model, world or lsof."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'run_camp_semantics.py'
spec = importlib.util.spec_from_file_location('camp_semantics_stage_test', SOURCE)
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def row(case_id, *, decision='decline'):
    return {'case_id': case_id, 'decision': decision, 'cost': None, 'effects': None,
            'reason': 'Synthetic fixture reason', 'verified_facts': [], 'unverified_claims': []}


class CampSemanticsStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='camp-semantic-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        continuation = self.root / 'continuation'
        continuation.mkdir(mode=0o700)
        self.baseline = {'proposer_role_id': 'a', 'judge_role_id': 'gm',
            'world_description': {'world': 'fixture rules'}, 'action_schemas': {'functions': []},
            'authorized_snapshot': {'role_id': 'gm',
                'authority': {'exists': True, 'version': 1, 'updated_at': 1,
                              'value': {'epoch': 1, 'founder_role_id': 'founder', 'judges': ['gm']}},
                'members': [{'key': name, 'version': 1, 'value': {'joined_at': 1}} for name in ['a', 'b', 'c', 'gm', 'founder']],
                'inventory': {'exists': True, 'version': 1, 'value': {'wood': 6, 'rope': 4, 'stone': 3}},
                'route': {'exists': True, 'version': 1, 'value': {'passage': 'blocked', 'stability': 0, 'exposure': 2}},
                'rules': {'cost_formula': 'fixture'}, 'goal': 'cargo', 'goal_reached': False,
                'findings': {'fixture': True}, 'projects': [], 'now': 1,
                'intents': [{'key': 'prior-a', 'version': 1, 'value': {'owner': 'a', 'status': 'pending',
                            'text': 'ANCHOR_REFUND_LONG_TEXT', 'ruling': None, 'created_at': 1}}],
                'private_notes': [{'key': 'prior-a', 'exists': True, 'version': 1,
                                   'value': {'owner': 'a', 'content': 'synthetic note'}}]}}
        baseline_path = self.root / 'baseline.json'
        baseline_path.write_text(json.dumps(self.baseline))
        cases = [{'case_id': source, 'intent_zh': f'Original bytes {source}', 'intent_en': 'unused language',
                  'allowed_handling': 'GOLD_SENTINEL_ONLY_IN_EXPECTATIONS'} for source in stage.FROZEN_SELECTION.values()]
        case_path = self.root / 'cases.json'
        case_path.write_text(json.dumps({'cases': cases}))
        world_path = self.root / 'world.py'
        world_path.write_text('fixture world')
        gate_path = self.root / 'gate.json'
        gate_path.write_text(json.dumps({'passed': True, 'source_hashes': {'examples/river_camp.py': stage.digest(world_path)}}))
        originals = [self.root / 'original-ledger.json', self.root / 'continuation-ledger.json']
        for path in originals:
            path.write_text('{"frozen_ten_hosts":true}')
        archive_path = self.root / 'executed.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('tools/run_river_camp_models.py', 'historical executed source')
        values = {'EVIDENCE': continuation / 'semantics-20261003', 'RUNTIME_ROOT': self.root / 'runtime',
                  'BASELINE': baseline_path, 'CASES': case_path, 'STAGE_A': gate_path, 'WORLD_SOURCE': world_path,
                  'ORIGINAL_LEDGER': originals[0], 'CONTINUATION_LEDGER': originals[1], 'EXECUTED_SOURCE': archive_path,
                  'BASELINE_SHA': stage.digest(baseline_path), 'CASES_SHA': stage.digest(case_path),
                  # Synthetic preparation seals the current fixture helper. The
                  # actual executed stage retains its immutable 7f helper seal.
                  'HELPER_SHA': stage.digest(stage.HELPER_SOURCE),
                  'EXECUTED_SHA': hashlib.sha256(b'historical executed source').hexdigest()}
        for name, value in values.items():
            patch = mock.patch.object(stage, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.original_bytes = {path: path.read_bytes() for path in originals + [baseline_path, case_path, archive_path]}

    def prepare(self):
        return stage.prepare()

    def fake_host(self, *, failure=None, final=None):
        helper = stage.host_module()
        def run(name, prompt, allowed, budget, **kwargs):
            self.assertEqual(allowed, [])
            self.assertIsNone(kwargs.get('actor'))
            self.assertTrue(kwargs['internal_storage'])
            self.assertEqual(kwargs['metadata_source'], 'official-github')
            item = {'stage': name, 'finished': True, 'owned_process_reaped': True, 'model_host_started': True,
                    'cli_cost': 0, 'pid': 999999, 'errors': [], 'owned_runtime_path': str(stage.RUNTIME_ROOT / 'profiles' / name),
                    'final_text': final or json.dumps({'results': [row('sem01'), row('sem02')]})}
            budget['runs'].append(item)
            if failure:
                item['errors'] = [{'type': 'timeout'}, {'type': 'missing_or_invalid_final_json'}]
                raise failure
            return item
        helper.run = mock.Mock(side_effect=run)
        return helper

    def test_isolated_execution_bindings_do_not_modify_a_fresh_old_module(self):
        first, second = stage.host_module(), stage.host_module()
        self.assertIsNot(first, second)
        self.assertEqual((first.MAX_RUNS, first.STEPS, first.OUTPUT_LIMIT, first.TIMEOUT), (6, 2, 2048, 120))
        first.MAX_RUNS = 1
        self.assertEqual(second.MAX_RUNS, 6)
        original_spec = importlib.util.spec_from_file_location('unchanged_old_runner_fixture', stage.HELPER_SOURCE)
        original = importlib.util.module_from_spec(original_spec)
        original_spec.loader.exec_module(original)
        self.assertEqual((original.MAX_RUNS, original.STEPS, original.OUTPUT_LIMIT), (10, 10, 4096))
        self.assertNotEqual(original.INTERNAL_ROOT, stage.RUNTIME_ROOT)

    def test_prepare_separates_gold_neutral_ids_and_counterfactual_contexts(self):
        result = self.prepare()
        self.assertFalse(result['model_started'])
        prompt = (stage.EVIDENCE / 'batch-1-prompt.txt').read_text()
        self.assertNotIn('GOLD_SENTINEL', prompt)
        self.assertNotIn('ANCHOR_REFUND_LONG_TEXT', prompt)
        self.assertNotIn('source_case_id', prompt)
        self.assertNotIn('expected_effects', prompt)
        projection = json.loads((stage.EVIDENCE / 'baseline-projection.json').read_text())['projection']
        self.assertEqual(projection['proposer_role_id'], 'b')
        self.assertEqual(projection['judge_role_id'], 'gm')
        self.assertNotIn('findings', projection['authorized_snapshot'])
        cases = json.loads((stage.EVIDENCE / 'batch-3-input.json').read_text())['cases']
        self.assertEqual(cases[0]['intent'], cases[1]['intent'])
        self.assertEqual(cases[0]['authorized_snapshot']['inventory']['value']['wood'], 6)
        self.assertEqual(cases[1]['authorized_snapshot']['inventory']['value']['wood'], 1)
        normalized = copy.deepcopy(cases[1]['authorized_snapshot'])
        normalized['inventory']['value']['wood'] = 6
        self.assertEqual(normalized, cases[0]['authorized_snapshot'])
        plan = json.loads((stage.EVIDENCE / 'plan.json').read_text())
        with zipfile.ZipFile(stage.EVIDENCE / 'execution-source.zip') as saved:
            for name in ['tools/run_camp_semantics.py', 'tests/test_camp_semantics.py', 'tools/run_river_camp_models.py']:
                actual = SOURCE.parents[1] / name
                self.assertEqual(hashlib.sha256(saved.read(name)).hexdigest(), stage.digest(actual))
        self.assertEqual(stage.digest(stage.EVIDENCE / 'execution-source.zip'), plan['execution_archive_sha256'])
        for path, before in self.original_bytes.items():
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(AssertionError):
            self.prepare()

    def test_one_invocation_starts_only_one_fake_host_and_refuses_repeat(self):
        self.prepare()
        helper = self.fake_host()
        with mock.patch.object(stage, 'host_module', return_value=helper):
            result = stage.run_one(1)
            self.assertEqual(result['valid_structured_outputs'], 2)
            with self.assertRaises(AssertionError):
                stage.run_one(1)
        self.assertEqual(helper.run.call_count, 1)
        budget = json.loads((stage.EVIDENCE / 'model-runs.json').read_text())
        self.assertEqual(len(budget['runs']), 1)
        self.assertEqual(len(budget['attempts']), 1)
        for path, before in self.original_bytes.items():
            self.assertEqual(path.read_bytes(), before)

    def test_host_timeout_is_preserved_and_not_a_case_error_or_automatic_retry(self):
        self.prepare()
        helper = self.fake_host(failure=SystemExit('synthetic host timeout'))
        with mock.patch.object(stage, 'host_module', return_value=helper):
            with self.assertRaises(SystemExit):
                stage.run_one(1)
        result = json.loads((stage.EVIDENCE / 'batch-1' / 'result.json').read_text())
        self.assertEqual(result['classification'], 'host_timeout')
        self.assertEqual(result['valid_structured_outputs'], 0)
        self.assertTrue(result['owned_process_reaped'])
        self.assertEqual(helper.run.call_count, 1)

    def test_pinned_original_change_stops_before_any_preflight_or_host(self):
        self.prepare()
        stage.ORIGINAL_LEDGER.write_text('changed source guard')
        with mock.patch.object(stage, 'host_module') as host:
            with self.assertRaises(AssertionError):
                stage.run_one(1)
        host.assert_not_called()
        self.assertFalse((stage.EVIDENCE / 'batch-1').exists())

    def test_six_host_limit_and_unreaped_host_stop_before_preflight(self):
        self.prepare()
        ledger = stage.EVIDENCE / 'model-runs.json'
        original = json.loads(ledger.read_text())
        for rows in [[{'finished': True, 'owned_process_reaped': True}] * 6,
                     [{'finished': False, 'owned_process_reaped': False}]]:
            budget = {**original, 'runs': rows}
            ledger.write_text(json.dumps(budget))
            with mock.patch.object(stage, 'host_module') as host:
                with self.assertRaises(AssertionError):
                    stage.run_one(1)
                host.assert_not_called()

    def test_strict_json_rejects_fences_duplicate_keys_ids_and_bool_cost(self):
        good = json.dumps({'results': [row('sem05')]})
        self.assertEqual(stage.validate_final(good, ['sem05'])[0]['case_id'], 'sem05')
        bad = ['```json\n' + good + '\n```', good.replace('"decision": "decline"', '"decision":"approve","decision":"decline"'),
               json.dumps({'results': [row('sem05'), row('sem05')]}), good.replace('"cost": null', '"cost": NaN')]
        approved = row('sem05', decision='approve')
        approved.update({'cost': {'wood': True, 'rope': 1, 'stone': 0}, 'effects': {'passage': 'foot', 'stability': 1, 'exposure': 2}})
        bad.append(json.dumps({'results': [approved]}))
        for value in bad:
            with self.subTest(value=value), self.assertRaises((ValueError, AssertionError)):
                stage.validate_final(value, ['sem05'])

    def test_formula_and_stock_checks_use_each_sealed_case_not_shared_baseline(self):
        self.prepare()
        inputs = json.loads((stage.EVIDENCE / 'batch-3-input.json').read_text())['cases']
        approved = row('sem05', decision='approve')
        approved.update({'cost': {'wood': 2, 'rope': 1, 'stone': 0}, 'effects': {'passage': 'foot', 'stability': 1, 'exposure': 2}})
        insufficient = {**copy.deepcopy(approved), 'case_id': 'sem06'}
        rows = stage.validate_final(json.dumps({'results': [approved, insufficient]}), ['sem05', 'sem06'])
        self.assertEqual(stage.constraint_findings(rows, inputs), [{'case_id': 'sem06', 'type': 'insufficient_inventory'}])
        approved['cost']['rope'] = 0
        self.assertEqual(stage.constraint_findings([approved], inputs)[0]['type'], 'cost_formula_mismatch')

    def test_explicit_correction_is_fresh_single_host_and_does_not_supply_gold(self):
        self.prepare()
        helper = self.fake_host(final='```json\n{}\n```')
        with mock.patch.object(stage, 'host_module', return_value=helper):
            failed = stage.run_one(1)
        self.assertEqual(failed['classification'], 'invalid_json_or_schema')
        helper = self.fake_host()
        with mock.patch.object(stage, 'host_module', return_value=helper):
            corrected = stage.run_one(1, correction_of='batch-1')
        self.assertEqual(corrected['valid_structured_outputs'], 2)
        prompt = helper.run.call_args.args[1]
        self.assertTrue(prompt.endswith(stage.FORMAT_REMINDER))
        self.assertNotIn('GOLD_SENTINEL', prompt)
        ledger = json.loads((stage.EVIDENCE / 'model-runs.json').read_text())
        self.assertEqual(len(ledger['runs']), 2)
        self.assertEqual(ledger['attempts'][1]['correction_of'], 'batch-1')

    def test_cleanup_refuses_profile_outside_new_stage_before_any_inventory(self):
        self.prepare()
        ledger = stage.EVIDENCE / 'model-runs.json'
        budget = json.loads(ledger.read_text())
        budget['attempts'] = [{'stage': 'batch-1', 'status': 'failed'}]
        budget['runs'] = [{'stage': 'batch-1', 'finished': True, 'owned_process_reaped': True,
                           'owned_runtime_path': '/unrelated/profile'}]
        ledger.write_text(json.dumps(budget))
        with mock.patch.object(stage, 'host_module') as host:
            with self.assertRaises(AssertionError):
                stage.cleanup()
            host.return_value.cleanup.assert_not_called()

    def test_single_flight_lock_refuses_independent_fd_before_ledger_or_host(self):
        self.prepare()
        ledger = stage.EVIDENCE / 'model-runs.json'
        before = ledger.read_bytes()
        with stage.phase_lock(), mock.patch.object(stage, 'load_plan') as load, \
                mock.patch.object(stage, 'load_plan_for_cleanup') as clean_load, \
                mock.patch.object(stage, 'host_module') as host:
            with self.assertRaises(BlockingIOError):
                stage.run_one(1)
            with self.assertRaises(BlockingIOError):
                stage.run_one(2)
            with self.assertRaises(BlockingIOError):
                stage.cleanup()
            load.assert_not_called()
            clean_load.assert_not_called()
            host.assert_not_called()
        self.assertEqual(before, ledger.read_bytes())
        with stage.phase_lock():
            pass


if __name__ == '__main__':
    unittest.main()
