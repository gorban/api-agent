"""Foundation launch contract checks; no services or live tests are invoked."""
from pathlib import Path
import os
from io import StringIO
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch
from api_agent.runtime.bootstrap import _extract_option, _supports_color, generated_help, main
from api_agent.config import load_shared_schema, schema_from_dict

ROOT = Path(__file__).resolve().parents[2]

class LaunchTests(unittest.TestCase):
    def test_terminal_units_use_override_or_inherit_global_workers(self):
        from api_agent.testing.runner import UnitRunResult
        for extra, expected in (([], 32), (['--unit-test-parallel-jobs='], 11),
                                (['--unit-test-parallel-jobs=0'], 0),
                                (['--unit-test-parallel-jobs=12'], 12)):
            with self.subTest(extra=extra), patch.dict(os.environ, {}, clear=True), \
                    patch('sys.stdout', StringIO()), \
                    patch('api_agent.config.read_dotenv', return_value={}), \
                    patch('api_agent.runtime.worktrees.verify_worktrees'), \
                    patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
                    patch('api_agent.testing.collection.collect_tests', return_value=[]), \
                    patch('api_agent.testing.runner.run_unit_tests', return_value=UnitRunResult(0, (), (), '')) as run:
                self.assertEqual(main(['--no-std-in', '--tui-mode=unit',
                                       '--test-parallel-jobs=11', *extra], base_root=ROOT), 0)
                self.assertEqual(run.call_args.kwargs['parallel_jobs'], expected)
                self.assertIs(run.call_args.kwargs['coverage_full'], False)

    def test_terminal_unit_full_coverage_option_reaches_runner(self):
        from api_agent.testing.runner import UnitRunResult
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdout', StringIO()), \
                patch('api_agent.config.read_dotenv', return_value={}), \
                patch('api_agent.runtime.worktrees.verify_worktrees'), \
                patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
                patch('api_agent.testing.collection.collect_tests', return_value=[]), \
                patch('api_agent.testing.runner.run_unit_tests', return_value=UnitRunResult(0, (), (), '')) as run:
            self.assertEqual(main(['--no-std-in', '--tui-mode=unit', '--unit-coverage-full'], base_root=ROOT), 0)
            self.assertIs(run.call_args.kwargs['coverage_full'], True)

    def test_raw_unit_summary_has_separator_without_changing_shared_formatter(self):
        from api_agent.testing.runner import CoverageArea, UnitRunResult, format_unit_summary
        result = UnitRunResult(0, (), (CoverageArea('base', 'tiny.py', 1, 1, 0, 0),), '')
        stream = StringIO()
        def runner(*args, **kwargs):
            kwargs['output']('pytest output\n')
            return result
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdout', stream), patch('api_agent.runtime.worktrees.verify_worktrees'), patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), patch('api_agent.testing.collection.collect_tests', return_value=[]), patch('api_agent.testing.runner.run_unit_tests', side_effect=runner):
            self.assertEqual(main(['--no-std-in', '--tui-mode', 'unit'], base_root=ROOT), 0)
        self.assertIn('pytest output\n\nUnit results:', stream.getvalue())
        self.assertEqual(stream.getvalue().count('Code coverage: lines 100% · branches 100%.'), 1)
        self.assertNotIn('Production coverage', stream.getvalue())
        self.assertTrue(format_unit_summary(result).startswith('Unit results:'))

    def test_internal_interpreter_handoff_does_not_repeat_worktree_gate(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdout', StringIO()), patch('api_agent.runtime.worktrees.verify_worktrees') as gate:
            self.assertEqual(main(['--help'], base_root=ROOT, _worktrees_verified=True), 0)
            gate.assert_not_called()
            self.assertEqual(main(['--help'], base_root=ROOT), 0)
            gate.assert_called_once()

    def test_help_is_curated_and_links_full_configuration(self):
        output = generated_help(load_shared_schema())
        for option in ('--help', '--no-std-in', '--collect-only', '--tui-mode',
                       '--e2e-debug'):
            self.assertIn(option, output)
        self.assertIn(os.path.relpath(ROOT / 'cfg/config.toml'), output)
        self.assertNotIn('[shared.toml]', output)
        self.assertNotIn('\033[', output)
        self.assertNotIn('--ui-source-dir', output)
        self.assertNotIn('--unattended-user-index', output)
        for option in ('--auth-browser-debug', '--unattended-secondary-user-index',
                       '--resource-pack-dir', '--api-agent-dir'):
            self.assertNotIn(option, output)
        self.assertNotIn('--e2e-browser-resolution', output)
        self.assertNotIn('Configuration options:', output)
        self.assertFalse(output.endswith('\n'))
        for text in ('Positional test selectors require', 'Explicit unit mode can bypass',
                     'validates collection requirements', 'NO_STD_IN=True requires', 'E2E_RERUNS'):
            self.assertNotIn(text, output)

    def test_terminal_colors_for_paths_and_bootstrap(self):
        stream = StringIO()
        stream.isatty = lambda: True
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), patch('sys.stdout', stream), patch('api_agent.runtime.worktrees.verify_worktrees'):
            self.assertTrue(_supports_color(stream))
            self.assertEqual(main(['--help'], base_root=ROOT), 0)
            self.assertIn('\033[34m' + os.path.relpath(ROOT / 'cfg/config.toml') + '\033[0m', stream.getvalue())
            self.assertIn('\033[31mBootstrap:', stream.getvalue())
            with patch.dict(os.environ, {'NO_COLOR': '1'}):
                self.assertFalse(_supports_color(stream))
            with patch.dict(os.environ, {'TERM': 'dumb'}):
                self.assertFalse(_supports_color(stream))
        self.assertFalse(_supports_color(StringIO()))

    def test_option_consumption_preserves_selectors(self):
        self.assertEqual(_extract_option(['--resource-pack-dir=/a b', '--help', 'x'],
                                       {'--resource-pack-dir'}), (['--help', 'x'], '/a b'))
        self.assertEqual(_extract_option(['--', '--resource-pack-dir', 'x'],
                                       {'--resource-pack-dir'}),
                         (['--', '--resource-pack-dir', 'x'], None))

    def test_empty_bootstrap_value_fails(self):
        with self.assertRaises(ValueError):
            _extract_option(['--resource-pack-dir='], {'--resource-pack-dir'})

    def test_duplicate_options_fail_before_starting_operations(self):
        stderr = StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('sys.stderr', stderr), \
                patch('api_agent.config.read_dotenv', return_value={}), \
                patch('api_agent.runtime.worktrees.verify_worktrees'):
            self.assertEqual(main(['--tui-mode', 'unattended-auth', '--tui-mode', 'unit'],
                                  base_root=ROOT), 2)
        self.assertEqual(stderr.getvalue(), 'Configuration error: Duplicate option --tui-mode\n')
        for names in ({'--resource-pack-dir', '--resource_pack_dir'},
                      {'--api-agent-dir', '--api_agent_dir'}):
            first, second = sorted(names)
            with self.assertRaisesRegex(ValueError, 'Duplicate option'):
                _extract_option([first, '/one', second + '=/two'], names)

    def test_help_from_unrelated_cwd(self):
        result = subprocess.run([sys.executable, str(ROOT / 'launch.py'), '--help'],
                                cwd='/', capture_output=True, text=True, input='y\n',
                                env={k: v for k, v in os.environ.items()
                                     if k not in {'HELP', 'NO_STD_IN', 'TUI_MODE'}})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('--resource-pack-dir', result.stdout)
        self.assertNotIn('Segment B', result.stdout)
        self.assertNotIn('Foundation only:', result.stdout)

    def test_bootstrap_hint_only_when_pack_not_selected(self):
        with tempfile.TemporaryDirectory() as directory, patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True):
            resource_pack = Path(directory) / "pack"
            shutil.copytree(ROOT / "demo", resource_pack)
            cases = [(['--help'], {}, True),
                     (['--help', '--resource-pack-dir', str(ROOT / 'demo')], {}, False),
                     (['--help', '--resource-pack-dir', str(resource_pack)], {}, False),
                     (['--help'], {'RESOURCE_PACK_DIR': str(resource_pack)}, False)]
            for argv, env, expected in cases:
                with self.subTest(argv=argv, env=env), patch.dict(os.environ, env), patch('sys.stdout', new_callable=StringIO) as output:
                    self.assertEqual(main(argv, base_root=ROOT), 0)
                    self.assertEqual('Bootstrap:' in output.getvalue(), expected)
                    if expected:
                        self.assertIn('\n\nBootstrap:', output.getvalue())
                        self.assertNotIn('\n\n\nBootstrap:', output.getvalue())

    def test_configured_collection_shortcuts_interaction(self):
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True), patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), patch('api_agent.testing.collection.collect_tests', return_value=[type('Test', (), {'id': 'base::test_example.py::test_one'})()]):
            self.assertEqual(main(['--tui-mode=unit', '--collect-only'], base_root=ROOT), 0)

    def test_explicit_collection_checks_missing_values_without_no_std_in(self):
        schema = schema_from_dict({'version': 1, 'fields': {
            'HELP': {'type': 'bool', 'default': False},
            'TUI_MODE': {'type': 'str'}, 'COLLECT_ONLY': {'type': 'bool', 'default': False},
            'NEEDED': {'type': 'str', 'required_when': {'COLLECT_ONLY': True}}}})
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True), patch('api_agent.config.compose_schema', return_value=schema), patch('api_agent.testing.collection.collect_tests') as collect, patch('api_agent.runtime.dependencies.ensure_environment') as prepare:
            self.assertEqual(main(['--tui-mode=unit', '--collect-only'], base_root=ROOT), 2)
            collect.assert_not_called()
            prepare.assert_not_called()

    def test_gate_precedes_help_and_invalid_options(self):
        from api_agent.runtime.worktrees import WorktreeGateError
        with patch('api_agent.runtime.worktrees.verify_worktrees', side_effect=WorktreeGateError('stale')):
            with patch('api_agent.config.resolve_config') as resolve:
                self.assertEqual(main(['--help', '--bad-mode'], base_root=ROOT), 2)
                resolve.assert_not_called()

    def test_noninteractive_live_modes_are_rejected(self):
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True):
            for mode in ('integration', 'e2e'):
                with self.subTest(mode=mode), patch('sys.stderr', new_callable=StringIO) as stderr:
                    self.assertEqual(main(['--tui-mode=' + mode, '--no-std-in'], base_root=ROOT), 2)
                    self.assertIn('NO_STD_IN=True with TUI_MODE=' + mode + ' requires', stderr.getvalue())
                    self.assertNotIn('not implemented yet', stderr.getvalue())

    def test_noninteractive_live_collection_remains_nonexecuting(self):
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True), patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), patch('api_agent.testing.collection.collect_tests', return_value=[]):
            for mode in ('integration', 'e2e'):
                with self.subTest(mode=mode):
                    self.assertEqual(main(['--tui-mode=' + mode, '--no-std-in', '--collect-only'], base_root=ROOT), 0)

    def test_selectors_need_explicit_test_mode(self):
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True), patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), patch('api_agent.tui.app.run_tui', return_value=3):
            for argv in (['selected_test'], ['--tui-mode=run-local', 'selected_test']):
                with self.subTest(argv=argv), patch('sys.stderr', new_callable=StringIO) as stderr:
                    self.assertEqual(main(argv, base_root=ROOT), 2)
                    self.assertIn('Positional test selectors', stderr.getvalue())
            self.assertEqual(main(['--tui-mode=unit', 'selected_test'], base_root=ROOT), 3)
            with patch.dict(os.environ, {'TUI_MODE': 'unit'}):
                self.assertEqual(main(['selected_test'], base_root=ROOT), 3)

    def test_help_does_not_hide_unknown_options(self):
        with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(['--help', '--unknown'], base_root=ROOT), 2)

    def test_installed_copy_does_not_infer_unrelated_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / 'lib/python/site-packages'
            shutil.copytree(ROOT / 'src/api_agent', site / 'api_agent',
                            ignore=shutil.ignore_patterns('__pycache__'))
            script = ("from unittest.mock import patch; "
                      "from api_agent.runtime.bootstrap import main; "
                      "p=patch('api_agent.runtime.worktrees.verify_worktrees'); "
                      "gate=p.start(); result=main(['--help']); "
                      "assert result == 2; gate.assert_not_called()")
            result = subprocess.run([sys.executable, '-c', script], cwd='/',
                                    env={**os.environ, 'PYTHONPATH': str(site)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('no verified source checkout', result.stderr)
            self.assertIn('checkout/launch.py', result.stderr)

    def test_launcher_dotenv_precedence_is_retained_with_demo_pack(self):
        from api_agent.config import resolve_config
        with tempfile.TemporaryDirectory() as directory:
            origin = Path(directory)
            def dotenv(path):
                return {'NO_STD_IN': 'True'} if Path(path) == origin / '.env' else {'NO_STD_IN': 'False'}
            with patch('api_agent.runtime.worktrees.verify_worktrees'), patch.dict(os.environ, {}, clear=True):
                with patch('api_agent.config.read_dotenv', side_effect=dotenv):
                    with patch('api_agent.config.resolve_config', wraps=resolve_config) as resolve:
                        self.assertEqual(main(['--help', '--_launcher-root', str(origin)],
                                              base_root=ROOT, resource_pack_dir=ROOT / 'demo'), 0)
                        self.assertEqual(resolve.call_args.kwargs['dotenv']['NO_STD_IN'], 'True')

    def test_external_pack_dotenv_and_conditional_extension_provenance(self):
        from api_agent.config import resolve_config, read_dotenv
        with tempfile.TemporaryDirectory() as directory:
            pack = Path(directory)
            owner = pack / 'test-owner'
            owner.mkdir()
            (pack / 'pack.toml').write_text('version = 1\nid = "external"\nowner = "test-owner"\nconfig = "config.toml"\n')
            (pack / 'config.toml').write_text('version = 1\n[fields.PACK_FLAG]\ntype = "bool"\ndefault = true\n[fields.PACK_FLAG.applies_when]\nTUI_MODE = "unit"\n')
            (pack / '.env').write_text('TUI_MODE=unit\nAPI_AGENT_DIR=../chosen-base\n')
            (owner / '.env').write_text('TUI_MODE=integration\n')
            def layered_dotenv(path):
                if Path(path) == ROOT / '.env':
                    return {'TUI_MODE': 'integration', 'NO_STD_IN': 'False'}
                return read_dotenv(path)
            with patch.dict(os.environ, {}, clear=True), patch('api_agent.config.read_dotenv', side_effect=layered_dotenv), patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), patch('api_agent.tui.app.run_tui', return_value=3):
                with patch('api_agent.runtime.worktrees.verify_worktrees') as gate:
                    with patch('api_agent.config.resolve_config', wraps=resolve_config) as resolve:
                        self.assertEqual(main(['selected_test'], base_root=ROOT, resource_pack_dir=pack), 3)
                        kwargs = resolve.call_args.kwargs
                        self.assertEqual(kwargs['dotenv']['TUI_MODE'], 'unit')
                        self.assertEqual(kwargs['dotenv']['NO_STD_IN'], 'False')
                        resolved = resolve_config(resolve.call_args.args[0], **kwargs)
                        self.assertEqual(resolved.fields['PACK_FLAG'].source, 'pack_default')
                        self.assertFalse(resolved.fields['PACK_FLAG'].explicit)
                        self.assertEqual(kwargs['overrides']['API_AGENT_DIR'], str(ROOT.resolve()))
                        self.assertEqual(resolved.values['API_AGENT_DIR'], ROOT.resolve())
                        self.assertEqual(gate.call_args.args[0], [ROOT.resolve(), pack.resolve(), owner.resolve()])


def test_noninteractive_debug_integration_dispatch_and_prepare_failure(monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    from api_agent.testing.collection import TestIdentity
    selected = TestIdentity('base::tests/integration/test_demo.py::test_selected', 'base',
                            'tests/integration/test_demo.py', 'tests/integration/test_demo.py::test_selected')
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *a, **k: [selected])
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *a, **k: None)
    calls = []
    def run(context, tests, config, **kwargs):
        assert tuple(tests) == (selected,) and config['DEBUG'] and config['NO_STD_IN']
        assert kwargs['accepted'] is True
        assert kwargs['unattended_index_source'] == 'cli'
        kwargs['output']('raw evidence\n')
        calls.append('run')
        return SimpleNamespace(exit_code=1)
    monkeypatch.setattr('api_agent.testing.recorded.run_recorded_integration', run)
    monkeypatch.setattr(os, 'environ', {})
    args = ['--tui-mode=integration', '--no-std-in', '--debug', '--allure-s3-bucket=synthetic', '--unattended-user-index=0']
    assert main(args, base_root=ROOT) == 1
    assert calls == ['run'] and 'raw evidence' in capsys.readouterr().out
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *a, **k: 3)
    assert main(args, base_root=ROOT) == 3
    assert calls == ['run']


def test_launch_keyboard_interrupt_has_clean_exit(monkeypatch, capsys):
    def interrupted(*args, **kwargs): raise KeyboardInterrupt
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', interrupted)
    assert main(['--help'], base_root=ROOT) == 130
    assert capsys.readouterr().err == 'Run interrupted.\n'
