"""Deferred stream disclosure preserves finalization and terminal ordering."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.diagnostics import CredentialScrubber
from api_agent.testing.inspection_controls import finish_terminal_inspection
from api_agent.testing.terminal_diagnostics import write_successful_diagnostics


def outcome(name='sample', status='passed', **kwargs):
    return TestResult(TestIdentity('base::' + name, 'base', 'test_sample.py', name), status, **kwargs)


@pytest.mark.parametrize('level,stdout', [('warning', False), ('info', True)])
def test_verbosity_selection_order_empty_and_duplicate_records(level, stdout):
    first = outcome('first', local_stderr='first warning\n', local_stdout='first information\n')
    second = outcome('second', local_stdout='second information')
    failed = outcome('failed', 'failed', traceback='failure printed elsewhere', local_stderr='duplicate failure')
    lines = []

    write_successful_diagnostics([first, first, failed, outcome('empty'), second], lines.append,
                                scrubber=CredentialScrubber(), level=level)

    assert len(lines) == (2 if stdout else 1)
    assert lines[0].startswith('Test diagnostics: "base::first" — passed\nstderr:\nfirst warning')
    assert ('stdout:\nfirst information' in lines[0]) is stdout
    assert not any('duplicate failure' in line or 'empty' in line for line in lines)
    if stdout:
        assert 'base::second' in lines[1]


def test_missing_legacy_stream_attributes_and_empty_selection_are_quiet():
    lines = []
    legacy = SimpleNamespace(identity=SimpleNamespace(id='legacy'), status='passed')
    write_successful_diagnostics([legacy], lines.append, scrubber=CredentialScrubber())
    write_successful_diagnostics([], lines.append, scrubber=CredentialScrubber(), level='info')
    assert lines == []


def test_invalid_verbosity_is_rejected_before_output():
    with pytest.raises(ValueError, match='warning or info'):
        write_successful_diagnostics([], lambda text: pytest.fail('unexpected output'),
                                     scrubber=CredentialScrubber(), level='debug')


def test_projected_profile_json_survives_escaped_stdout_without_legacy_remasking():
    payload = 'other: \\u0061\nIntegration test profile: {"LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS": {"value": null}}'
    assert '[REDACTED]' in CredentialScrubber()(payload)
    lines = []
    write_successful_diagnostics([outcome(local_stdout=payload)], lines.append,
                                 scrubber=CredentialScrubber(), level='info')
    assert payload in lines[0] and '[REDACTED]' not in lines[0]


def test_final_registered_credential_check_includes_header_and_both_streams():
    scrubber = CredentialScrubber()
    scrubber.boundary.register_secret('tail\n\nstdout:\nhead')
    lines = []
    write_successful_diagnostics([outcome(local_stderr='tail', local_stdout='head')],
                                 lines.append, scrubber=scrubber, level='info')
    assert lines == ['Test diagnostics withheld: credential protection.']


def test_output_failure_propagates():
    def broken(text):
        raise OSError('terminal unavailable')
    with pytest.raises(OSError, match='terminal unavailable'):
        write_successful_diagnostics([outcome(local_stderr='warning')], broken,
                                     scrubber=CredentialScrubber())


@pytest.mark.parametrize('cancelled,persisted', [(False, True), (True, False)])
def test_integration_completion_uses_final_records_before_summary_publication_and_cost(monkeypatch, cancelled, persisted):
    from api_agent.telemetry import usage_output
    monkeypatch.setattr(usage_output, 'final_usage_text', lambda usage: 'final cost')
    final = outcome(local_stderr='final shared notice', local_stdout='final information')
    recorded = SimpleNamespace(execution=SimpleNamespace(tests=(final,), cancelled=cancelled,
        inspections=()), persisted=persisted, usage={'final': True})
    ordered = ['all test progress finished']
    callback = lambda tests: write_successful_diagnostics(tests, ordered.append,
        scrubber=CredentialScrubber(), level='info')

    assert finish_terminal_inspection(recorded, ordered.append, successful_output=callback)

    assert 'final shared notice' in ordered[1] and 'final information' in ordered[1]
    assert ordered[2].startswith('Integration results:')
    assert ordered[3].startswith('Allure publication:') and ordered[4] == 'final cost'


@pytest.mark.parametrize('level', ['warning', 'info'])
def test_unit_launcher_emits_streams_after_runner_before_coverage(monkeypatch, capsys, level):
    from api_agent.runtime.bootstrap import main
    from api_agent.testing.runner import UnitRunResult, CoverageArea
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *a, **k: [])
    final = outcome(local_stderr='unit warning', local_stdout='unit information')
    def run(*args, **kwargs):
        kwargs['evidence_boundary'].register_secret('unit warning\n\nstdout:\nunit information')
        kwargs['output']('all unit tests completed\n')
        return UnitRunResult(0, (final,), (CoverageArea('base', 'tiny.py', 1, 1, 0, 0),), '')
    monkeypatch.setattr('api_agent.testing.runner.run_unit_tests', run)
    with patch.dict('os.environ', {}, clear=True):
        assert main(['--no-std-in', '--tui-mode=unit',
                     '--successful-test-diagnostics-level=' + level],
                    base_root=Path(__file__).resolve().parents[2]) == 0
    output = capsys.readouterr().out
    if level == 'info':
        assert 'unit information' not in output and 'unit warning' not in output
        marker = 'Test diagnostics withheld: credential protection.'
    else:
        assert 'unit information' not in output
        marker = 'unit warning'
    assert output.index('all unit tests completed') < output.index(marker) < output.index('Unit results:') < output.index('Code coverage:')
