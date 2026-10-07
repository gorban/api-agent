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
                    self.assertIn('incompatible', stderr.getvalue())
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
                        self.assertEqual(resolved.fields['API_AGENT_DIR'].source, 'dotenv')
                        self.assertEqual(resolved.values['API_AGENT_DIR'], (pack / '../chosen-base').resolve())
                        self.assertEqual(gate.call_args.args[0], [ROOT.resolve(), pack.resolve(), owner.resolve()])
