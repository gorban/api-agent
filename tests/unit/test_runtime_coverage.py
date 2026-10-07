"""Runtime boundary contracts with isolated files and controlled process adapters."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib
import os
import runpy
import sys

import pytest
from api_agent.runtime import bootstrap as boot, dependencies as deps, resource_pack as packs, worktrees as gate
from test_worktrees import GitSimulation

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolate_private_dotenv_reads(monkeypatch):
    # Runtime adapter tests use repository declarations, never developer secrets.
    import api_agent.config as config
    from api_agent.config import dotenv
    monkeypatch.setattr(config, 'read_dotenv', lambda path: {})
    monkeypatch.setattr(dotenv, 'read_dotenv', lambda path: {})


def test_installed_entrypoint_dispatches_only_when_executed():
    with patch.object(boot, 'main', return_value=7) as main:
        importlib.import_module('api_agent.__main__')
        main.assert_not_called()
        with patch.dict(sys.modules):
            sys.modules.pop('api_agent.__main__', None)
            with pytest.raises(SystemExit) as stopped:
                runpy.run_module('api_agent.__main__', run_name='__main__')
        assert stopped.value.code == 7
        main.assert_called_once_with()


@pytest.mark.parametrize('argv', [['--resource-pack-dir'], ['--resource-pack-dir', '--help'], ['--resource-pack-dir=--invalid']])
def test_missing_bootstrap_arguments(argv):
    with pytest.raises(ValueError, match='requires'):
        boot._extract_option(argv, {'--resource-pack-dir'})


@pytest.mark.parametrize('env,argv,expected', [
    ({'RESOURCE_PACK_DIR': ' '}, [], 2),
    ({'API_AGENT_DIR': ' '}, [], 2),
    ({'API_AGENT_DIR': '/missing'}, [], 2),
    ({'API_AGENT_DIR': str(ROOT)}, ['--', 'selector'], 9),
    ({}, ['--resource-pack-dir', str(ROOT / 'demo'), '--api-agent-dir', str(ROOT), '--', 'selector'], 9),
    ({'RESOURCE_PACK_DIR': str(ROOT / 'demo'), 'API_AGENT_DIR': str(ROOT)}, ['--help'], 9),
])
def test_resource_pack_launcher_resolution(env, argv, expected):
    with patch.dict(os.environ, env, clear=True), patch.object(boot.subprocess, 'call', return_value=9) as launch:
        assert boot.resource_pack_main(argv, launcher_root=ROOT / 'demo') == expected
        if expected == 9:
            command = launch.call_args.args[0]
            assert command[1] == str(ROOT / 'launch.py')
            assert '--_launcher-root' in command
        else:
            launch.assert_not_called()


def test_resource_pack_launcher_default_sys_arguments_and_process_error():
    with patch.dict(os.environ, {'API_AGENT_DIR': str(ROOT)}, clear=True), patch.object(sys, 'argv', ['launch', '--help']), patch.object(boot.subprocess, 'call', side_effect=OSError('process unavailable')):
        assert boot.resource_pack_main(launcher_root=ROOT / 'demo') == 2


@pytest.mark.parametrize('argv', [[], ['--tui-mode=unit'], ['--tui-mode=config-cleanup', '--no-std-in'], ['--tui-mode=run-local', '--collect-only'], ['--resource-pack-dir= ']])
def test_launch_invalid_interaction_boundaries(argv):
    with patch.dict(os.environ, {}, clear=True), patch.object(gate, 'verify_worktrees'), patch('sys.stdin.isatty', return_value=False):
        assert boot.main(argv, base_root=ROOT) == 2


def test_source_inference_and_empty_pack_environment():
    with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', ['launch', '--help']), patch.object(gate, 'verify_worktrees'):
        assert boot.main() == 0
    with patch.dict(os.environ, {'RESOURCE_PACK_DIR': ' '}, clear=True):
        assert boot.main(['--help'], base_root=ROOT) == 2
    with patch.object(boot, '__file__', '/installed/api_agent/runtime/bootstrap.py'):
        assert boot.main(['--help']) == 2


@pytest.mark.parametrize('argv', [['--tui-mode=unit', '--collect-only'], ['--tui-mode=unit', '--no-std-in'], []])
def test_dependency_handoff_returns_child_status(argv):
    with patch.dict(os.environ, {}, clear=True), patch.object(gate, 'verify_worktrees'), patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), patch.object(deps, 'ensure_environment', return_value=11):
        assert boot.main(argv, base_root=ROOT) == 11


def test_non_unit_no_stdin_and_stdout_terminal_required():
    with patch.dict(os.environ, {}, clear=True), patch.object(gate, 'verify_worktrees'):
        assert boot.main(['--no-std-in', '--tui-mode=allure'], base_root=ROOT) == 3
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=False):
            assert boot.main([], base_root=ROOT) == 2


@pytest.mark.parametrize('uv', ['uv', None])
def test_missing_venv_creation_and_external_root(tmp_path, uv):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["tool>=1"]\n')
    context = SimpleNamespace(base_root=base, pack_root=pack, external_pack=True)
    def create(command, **kwargs):
        if ('venv' in command):
            (pack / '.venv/bin').mkdir(parents=True)
            (pack / '.venv/bin/python').touch()
    with patch.object(deps.shutil, 'which', return_value=uv), patch.object(deps.subprocess, 'run', side_effect=create) as run, patch.object(deps, '_installed_requirements', return_value=()), patch.object(deps.subprocess, 'call', return_value=4):
        assert deps.ensure_environment(context, []) == 4
        assert run.call_count == 2
        assert (pack / '.venv/.api-agent-dependencies').is_file()


def manifest(tmp_path, text):
    base = tmp_path / 'base'; base.mkdir(exist_ok=True)
    pack = base / 'demo'; pack.mkdir(exist_ok=True)
    (pack / 'config.toml').touch()
    (pack / 'pack.toml').write_text(text)
    return base, pack


@pytest.mark.parametrize('text,match', [
    ('not TOML!', 'Cannot read'),
    ('version=1\nid="x"\nconfig="config.toml"\nunknown=1', 'Unknown fields'),
    ('version=2\nid="x"\nconfig="config.toml"', 'Unsupported'),
    ('version=1\nid=3\nconfig="config.toml"', 'nonempty string'),
    ('version=1\nid=" "\nconfig="config.toml"', 'nonempty string'),
    ('version=1\nid="x"\nowner="absent"\nconfig="config.toml"', 'owner directory'),
    ('version=1\nid="x"\nconfig="absent"', 'configuration does not exist'),
    ('version=1\nid="x"\nconfig="config.toml"\ndefaults=[]', 'TOML tables'),
    ('version=1\nid="x"\nconfig="config.toml"\ntests=[]', 'TOML tables'),
    ('version=1\nid="x"\nconfig="config.toml"\n[tests]\nother=[]', 'unknown test mode'),
    ('version=1\nid="x"\nconfig="config.toml"\n[tests]\nunit="test"', 'array'),
])
def test_invalid_resource_pack_contract(tmp_path, text, match):
    base, _ = manifest(tmp_path, text)
    with pytest.raises(packs.ResourcePackError, match=match):
        packs.load_launch_context(base)


@pytest.mark.parametrize('value', [None, '', ' ', '/absolute'])
def test_resource_paths_must_be_relative(value):
    with pytest.raises(packs.ResourcePackError, match='relative'):
        packs._relative_path(value, Path('/base'), 'owner')


def test_packaged_demo_owner_and_missing_base(tmp_path):
    with pytest.raises(packs.ResourcePackError, match='does not exist'):
        packs.load_launch_context(tmp_path / 'absent')
    base, pack = manifest(tmp_path, 'version=1\nid="demo"\nowner="."\nconfig="config.toml"')
    renamed = tmp_path / 'bundled'; pack.rename(renamed)
    with patch.object(packs, 'files', return_value=SimpleNamespace(joinpath=lambda _: renamed)):
        context = packs.load_launch_context(base)
    assert context.packaged_demo and context.owner_root == base
    assert not context.external_pack
    with pytest.raises(packs.ResourcePackError, match='Unknown test mode'):
        context.suite_paths('unknown')


def test_git_errors_branch_and_records():
    with patch.object(gate.subprocess, 'run', side_effect=OSError('missing')):
        with pytest.raises(gate.WorktreeGateError, match='Cannot run Git'):
            gate._git(Path('/base'), 'status')
    with patch.object(gate.subprocess, 'run', return_value=SimpleNamespace(returncode=3, stderr=b'failure', stdout=b'')):
        with pytest.raises(gate.WorktreeGateError, match='failed: failure'):
            gate._git(Path('/base'), 'status')
    for value, expected in [(b'main\n', 'main'), (b'', 'detached HEAD')]:
        with patch.object(gate, '_git', return_value=value):
            assert gate.checkout_branch(Path('/base')) == expected
    assert len(gate._records(b'worktree /one\0worktree /two\0HEAD abc')) == 2
    assert gate._records(b'\0') == []
    assert gate._date(None) == 'no runnable timestamp'
    bare = gate.CheckoutReport(Path('/admin'), 'bare', '', None, None, None, (), False, bare=True)
    assert bare.timestamp is None and 'bare' in bare.reason
    dirty = gate.replace(bare, bare=False, commit_timestamp=10, dirty_timestamp=20, dirty_path='file')
    assert dirty.reason == "eligible dirty file 'file'"


def test_dirty_path_access_error_and_older_file():
    with patch.object(gate, '_git', side_effect=[b'a\0b\0', b'', b'', b'']), patch.object(Path, 'lstat', side_effect=[SimpleNamespace(st_mtime=20), SimpleNamespace(st_mtime=10)]):
        assert gate._dirty(Path('/base')) == (20, 'a', ())
    with patch.object(gate, '_git', side_effect=[b'a\0', b'', b'']), patch.object(Path, 'lstat', side_effect=PermissionError('denied')):
        with pytest.raises(gate.WorktreeGateError, match='Cannot inspect dirty path'):
            gate._dirty(Path('/base'))


@pytest.mark.parametrize('mutation,match', [('invalid', 'invalid worktree entry'), ('missing_selected', 'absent'), ('foreign_root', 'another Git root'), ('foreign_common', 'unrelated repository')])
def test_invalid_registered_worktree_contract(mutation, match):
    sim = GitSimulation({'base': ['/base', '/other']}, {'/base': 10, '/other': 20})
    original = sim.git
    def git(root, *args, **kwargs):
        if args == ('worktree', 'list', '--porcelain', '-z'):
            if mutation == 'invalid': return b'HEAD abc\0\0'
            if mutation == 'missing_selected': return b'worktree /other\0HEAD abc\0\0'
        if str(root) == '/other':
            if mutation == 'foreign_root' and args == ('rev-parse', '--show-toplevel'): return b'/foreign\n'
            if mutation == 'foreign_common' and args == ('rev-parse', '--path-format=absolute', '--git-common-dir'): return b'/foreign/.git\n'
        return original(root, *args, **kwargs)
    with patch.object(gate.shutil, 'which', return_value='git'), patch.object(gate, '_git', side_effect=git), patch.object(Path, 'lstat', lambda path: sim.stat(path)):
        with pytest.raises(gate.WorktreeGateError, match=match):
            gate.inspect_worktrees([Path('/base')])


def test_empty_root_list_and_format_current():
    with patch.object(gate.shutil, 'which', return_value='git'):
        with pytest.raises(gate.WorktreeGateError, match='No selected'):
            gate.inspect_worktrees([])
    assert gate.format_report([]) == 'WORKTREE APPROVAL'
