"""Executable entry points select one process owner; library calls retain status returns."""
from pathlib import Path
import os
import runpy
import sys
import tomllib
from unittest.mock import patch

import pytest

from api_agent.runtime import bootstrap
from test_terminal_failure_diagnostics import terminal_failure_launch

ROOT = Path(__file__).resolve().parents[2]


def test_source_entrypoint_opts_into_same_process_handoff():
    original_path = sys.path[:]
    try:
        with patch.object(bootstrap, 'main', return_value=7) as main:
            with pytest.raises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'src/api_agent/runtime/entry.py'), run_name='__main__')
    finally:
        sys.path[:] = original_path
    assert stopped.value.code == 7
    main.assert_called_once_with(base_root=ROOT, _replace_process=True)


def test_console_entrypoint_uses_the_cli_owner_and_preserves_arguments():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert project['project']['scripts']['api-agent'] == 'api_agent.runtime.bootstrap:cli_main'
    with patch.object(bootstrap, 'main', return_value=4) as main:
        assert bootstrap.cli_main(['--help']) == 4
    main.assert_called_once_with(['--help'], _replace_process=True)


def test_pack_executable_handoff_preserves_argv_and_environment():
    with patch.dict(os.environ, {'API_AGENT_DIR': str(ROOT)}, clear=True), \
            patch.object(bootstrap.os, 'execve', side_effect=SystemExit(8)) as execute, \
            patch.object(bootstrap.subprocess, 'call') as call:
        with pytest.raises(SystemExit) as stopped:
            bootstrap.resource_pack_main(['--help'], launcher_root=ROOT / 'demo', replace_process=True)
    assert stopped.value.code == 8
    executable, argv, environment = execute.call_args.args
    assert executable == '/bin/sh' == argv[0]
    assert argv[1] == str(ROOT / 'launch.sh')
    assert argv[-1] == '--help'
    assert argv[argv.index('--resource-pack-dir') + 1] == str(ROOT / 'demo')
    assert argv[argv.index('--_launcher-root') + 1] == str(ROOT / 'demo')
    assert environment == {'API_AGENT_DIR': str(ROOT)}
    call.assert_not_called()


def test_terminal_completions_are_visible_and_flushed_before_run_returns(
        terminal_failure_launch, monkeypatch, capsys):
    from types import SimpleNamespace
    from api_agent.auth.terminal_output import TerminalOutput
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from test_terminal_failure_diagnostics import ARGS

    from dataclasses import replace

    identities, _ = terminal_failure_launch
    identities = (replace(identities[0], id=identities[0].id + '[synthetic-credential-value]'), identities[1])
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *args, **kwargs: identities)
    flushes = []
    stream = sys.stdout
    progress_pending = False

    def observe_write(text):
        nonlocal progress_pending
        progress_pending = text.startswith('Test progress:')
        stream.write(text)

    def observe_flush():
        nonlocal progress_pending
        if progress_pending:
            flushes.append(True)
            progress_pending = False
        stream.flush()

    presentation = TerminalOutput(stdout=SimpleNamespace(write=observe_write, flush=observe_flush))
    monkeypatch.setattr('api_agent.auth.terminal_output.TerminalOutput', lambda: presentation)

    def run(context, selected, config, **options):
        completed = RunnerEvent('test_finished', identity=identities[0], status='passed',
                                traceback='synthetic-credential-value')
        options['on_event'](completed)
        visible = capsys.readouterr().out
        assert 'Test progress: 1/2 completed' in visible
        assert 'base::selected.py::test_a' in visible
        assert '[REDACTED]' in visible
        assert 'synthetic-credential-value' not in visible
        assert flushes == [True]
        options['on_event'](completed)
        assert flushes == [True]
        options['on_event'](RunnerEvent('test_finished', identity=identities[1], status='passed'))
        assert 'Test progress: 2/2 completed' in capsys.readouterr().out
        results = tuple(TestResult(identity, 'passed') for identity in identities)
        return SimpleNamespace(execution=IntegrationRunResult(0, results, ''),
                               persisted=True, exit_code=0, usage={})

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert bootstrap.main(ARGS, base_root=ROOT, _replace_process=True) == 0
    assert flushes == [True, True]
