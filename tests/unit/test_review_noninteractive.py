"""Terminal launches validate active requirements without configuration review."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.runtime.bootstrap import main
from api_agent.runtime.resource_pack import load_launch_context

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def terminal_launch(tmp_path, monkeypatch):
    declaration = tmp_path / 'config.toml'
    declaration.write_text('''version = 1
[fields.SYNTHETIC_REQUIRED]
type = "str"
required = true
[fields.SYNTHETIC_REQUIRED.applies_when]
TUI_MODE = ["unit", "integration", "e2e"]
[fields.SYNTHETIC_DEFAULT]
type = "str"
default = "usable-default"
''')
    context = replace(load_launch_context(ROOT), config_path=declaration)
    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', lambda *args: context)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.config.dotenv.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    prepare = Mock(return_value=None)
    collect = Mock(return_value=[SimpleNamespace(id='synthetic-selected')])
    run = Mock(return_value=SimpleNamespace(exit_code=0))
    tui = Mock(side_effect=AssertionError('unexpected interactive review'))
    stdin = Mock()
    stdin.read.side_effect = AssertionError('unexpected stdin read')
    stdin.readline.side_effect = AssertionError('unexpected stdin read')
    monkeypatch.setattr('sys.stdin', stdin)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', prepare)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', collect)
    monkeypatch.setattr('api_agent.testing.runner.run_unit_tests', run)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    monkeypatch.setattr('api_agent.tui.app.run_tui', tui)
    return SimpleNamespace(prepare=prepare, collect=collect, run=run, tui=tui, stdin=stdin)


@pytest.mark.parametrize('mode,start', [('unit', 'True'), ('integration', 'True'), ('e2e', 'E2E')])
@pytest.mark.parametrize('collect_only', [False, True])
def test_missing_active_requirement_stops_terminal_launch(mode, start, collect_only, terminal_launch, capsys):
    args = ['--no-std-in', '--tui-mode=' + mode, '--start=' + start,
            '--allure-s3-bucket=synthetic', '--ui-source-dir=/synthetic/frontend', 'selected.py']
    if collect_only:
        args.append('--collect-only')

    status = main(args, base_root=ROOT)

    assert status == 2
    assert capsys.readouterr().err == (
        'Configuration error: SYNTHETIC_REQUIRED: required; provide '
        'SYNTHETIC_REQUIRED or --synthetic-required\n')
    terminal_launch.prepare.assert_not_called()
    terminal_launch.collect.assert_not_called()
    terminal_launch.run.assert_not_called()
    terminal_launch.tui.assert_not_called()
    terminal_launch.stdin.read.assert_not_called()
    terminal_launch.stdin.readline.assert_not_called()


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_unsaved_defaults_allow_terminal_collection_with_ordered_selectors(mode, terminal_launch, capsys):
    args = ['--no-std-in', '--tui-mode=' + mode, '--collect-only',
            '--synthetic-required=supplied', 'first.py', '--start=False', 'second.py::test_case']

    status = main(args, base_root=ROOT)

    assert status == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert output.out == 'synthetic-selected\n'
    terminal_launch.prepare.assert_called_once()
    terminal_launch.collect.assert_called_once()
    assert terminal_launch.collect.call_args.args[1:3] == (mode, ('first.py', 'second.py::test_case'))
    assert terminal_launch.collect.call_args.kwargs['config']['SYNTHETIC_DEFAULT'] == 'usable-default'
    terminal_launch.run.assert_not_called()
    terminal_launch.tui.assert_not_called()
    terminal_launch.stdin.read.assert_not_called()
    terminal_launch.stdin.readline.assert_not_called()
