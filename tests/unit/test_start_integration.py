"""Explicit terminal integration starts preserve recording and execution contracts."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api_agent.runtime.bootstrap import main
from api_agent.testing.recorded import validate_recorded_integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('start', ['True', 'E2E'])
@pytest.mark.parametrize('source', ['cli', 'environment', 'dotenv'])
def test_terminal_start_dispatches_recorded_integration(start, source):
    stdout, stderr = StringIO(), StringIO()
    args = ['--tui-mode=integration', '--no-std-in', '--allure-s3-bucket=synthetic', 'selected.py']
    environ = {'START': start} if source == 'environment' else {}
    dotenv = {'START': start} if source == 'dotenv' else {}
    if source == 'cli':
        args.append('--start=' + start)
    selected = [object()]
    def execute(context, tests, config, **options):
        assert tests is selected and config['START'] == start
        assert not config['DEBUG'] and config['NO_STD_IN']
        assert options['accepted'] is True
        assert validate_recorded_integration(tests, config, accepted=True) == tuple(selected)
        options['output']('synthetic runner output\n')
        return SimpleNamespace(exit_code=1)
    with patch.dict('os.environ', environ, clear=True), patch('sys.stdout', stdout), patch('sys.stderr', stderr), patch('api_agent.config.read_dotenv', return_value=dotenv), patch('api_agent.config.dotenv.read_dotenv', return_value=dotenv), patch('api_agent.runtime.worktrees.verify_worktrees') as gate, patch('api_agent.runtime.dependencies.ensure_environment', return_value=None) as prepare, patch('api_agent.testing.collection.collect_tests', return_value=selected) as collect, patch('api_agent.testing.launched.run_launched_integration', side_effect=execute) as run:
        assert main(args, base_root=ROOT) == 1
    gate.assert_called_once()
    prepare.assert_called_once()
    assert collect.call_args.args[1:3] == ('integration', ('selected.py',))
    run.assert_called_once()
    assert not stderr.getvalue()
    assert 'synthetic runner output' in stdout.getvalue()
    assert 'Integration debug:' not in stdout.getvalue()


@pytest.mark.parametrize('start', ['True', 'E2E'])
def test_recording_start_override_still_requires_accepted_execution(start):
    config = {'TUI_MODE': 'integration', 'NO_STD_IN': True, 'START': start, 'ALLURE_S3_BUCKET': 'synthetic'}
    with pytest.raises(PermissionError):
        validate_recorded_integration([object()], config, accepted=False)
    with pytest.raises(PermissionError):
        validate_recorded_integration([object()], config | {'COLLECT_ONLY': True}, accepted=True)
    with pytest.raises(ValueError, match='empty integration selection'):
        validate_recorded_integration([], config, accepted=True)


@pytest.mark.parametrize('start', ['', 'False'])
def test_disabled_start_does_not_unlock_terminal_recording(start):
    config = {'TUI_MODE': 'integration', 'NO_STD_IN': True, 'START': start, 'ALLURE_S3_BUCKET': 'synthetic'}
    with pytest.raises(PermissionError):
        validate_recorded_integration([object()], config, accepted=True)


@pytest.mark.parametrize('mode,debug', [('e2e', False), ('e2e', True), ('integration', False), ('integration', True)])
def test_rejection_describes_only_the_selected_mode(mode, debug):
    stderr = StringIO()
    args = ['--tui-mode=' + mode, '--no-std-in', '--allure-s3-bucket=synthetic',
            '--ui-source-dir=/tmp/synthetic-ui']
    if mode == 'e2e':
        args.append('--start=True')
    if debug:
        args.append('--debug')
    expected = ('NO_STD_IN=True with TUI_MODE=e2e requires explicit START=E2E. '
                'Use --start=E2E to accept execution, or --no-std-in=False for interactive acceptance; '
                'collection-only is still allowed via --collect-only.' if mode == 'e2e' else
                'NO_STD_IN=True with TUI_MODE=integration requires START=True/E2E. '
                'Use --start to execute, or --no-std-in=False for interactive selection; '
                'collection-only is still allowed via --collect-only.')
    with patch.dict('os.environ', {}, clear=True), patch('sys.stderr', stderr), patch('api_agent.config.read_dotenv', return_value={}), patch('api_agent.config.dotenv.read_dotenv', return_value={}), patch('api_agent.runtime.worktrees.verify_worktrees'), patch('api_agent.runtime.dependencies.ensure_environment') as prepare:
        assert main(args, base_root=ROOT) == 2
    assert stderr.getvalue() == 'Configuration error: ' + expected + '\n'
    prepare.assert_not_called()
