"""Noninteractive integration diagnostics preserve evidence and scrub credentials."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.runtime.bootstrap import main
from api_agent.runtime.prerequisites import PrerequisiteError
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult

ROOT = Path(__file__).resolve().parents[2]
ARGS = ['--no-std-in', '--tui-mode=integration', '--start=True',
        '--allure-s3-bucket=synthetic', '--log-content=False',
        '--synthetic-api-key=synthetic-credential-value', 'selected.py']


@pytest.fixture
def terminal_failure_launch(tmp_path, monkeypatch):
    declaration = tmp_path / 'config.toml'
    declaration.write_text('version = 1\n[fields.SYNTHETIC_API_KEY]\ntype = "str"\n')
    context = replace(load_launch_context(ROOT), config_path=declaration)
    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', lambda *args: context)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.config.dotenv.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *args, **kwargs: None)
    identities = (TestIdentity('base::selected.py::test_a', 'base', 'selected.py', 'test_a'),
                  TestIdentity('base::selected.py::test_b', 'base', 'selected.py', 'test_b'))
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *args, **kwargs: identities)
    stdin = Mock()
    stdin.read.side_effect = AssertionError('Unexpected stdin read')
    stdin.readline.side_effect = AssertionError('Unexpected stdin read')
    monkeypatch.setattr('sys.stdin', stdin)
    return identities, stdin


def test_terminal_test_failures_emit_identified_details_when_content_logging_disabled(
        terminal_failure_launch, monkeypatch, capsys):
    identities, stdin = terminal_failure_launch
    tests = tuple(TestResult(identity, 'error', traceback='safe failure', local_diagnostic=(
        f'RuntimeError: cause {index}\nFile /synthetic/source.py, line 42\n'
        'Command: synthetic-tool inspect\nExit code: 7\nstdout: useful output\n'
        'stderr: useful error\napi_key=synthetic-credential-value'))
        for index, identity in enumerate(identities))
    recorded = SimpleNamespace(execution=IntegrationRunResult(1, tests, ''), persisted=True,
                               exit_code=1, elapsed=1, usage={})

    def run(context, selected, config, **options):
        assert config['LOG_CONTENT'] is False and options['accepted'] is True
        options['output']('raw path /synthetic/source.py api_key=synthetic-')
        options['output']('credential-value\nlast fragment')
        return recorded

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert main(ARGS, base_root=ROOT) == 1
    output = capsys.readouterr()
    assert identities[0].id in output.err and identities[1].id in output.err
    assert 'cause 0' in output.err and 'cause 1' in output.err
    assert '/synthetic/source.py' in output.err and 'useful error' in output.err
    assert 'Command: synthetic-tool inspect' in output.err and 'Exit code: 7' in output.err
    assert 'useful output' in output.err
    assert '/synthetic/source.py' in output.out and 'last fragment' in output.out
    assert 'synthetic-credential-value' not in output.err + output.out
    stdin.read.assert_not_called()
    stdin.readline.assert_not_called()


@pytest.mark.parametrize('error_type, expected_status', [(PrerequisiteError, 1), (ValueError, 2),
                                                         (OSError, 2), (KeyboardInterrupt, 130)])
def test_terminal_raised_failures_emit_full_safe_chain_and_preserve_exit_status(
        terminal_failure_launch, monkeypatch, capsys, error_type, expected_status):
    error = error_type('outer failure api_key=synthetic-credential-value')
    error.local_details = 'Command: synthetic-tool inspect\nExit code: 7\nstderr: useful failure'

    def run(*args, **kwargs):
        try:
            raise RuntimeError('inner failure /synthetic/source.py')
        except RuntimeError as cause:
            raise error from cause

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert main(ARGS, base_root=ROOT) == expected_status
    output = capsys.readouterr()
    assert 'inner failure /synthetic/source.py' in output.err
    assert 'outer failure' in output.err and 'Command: synthetic-tool inspect' in output.err
    assert 'useful failure' in output.err and 'Exit code: 7' in output.err
    assert 'synthetic-credential-value' not in output.err + output.out


def test_terminal_inspection_failure_keeps_exit_three(terminal_failure_launch, monkeypatch):
    recorded = SimpleNamespace(exit_code=0)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', lambda *args, **kwargs: recorded)
    monkeypatch.setattr('api_agent.testing.inspection_controls.finish_terminal_inspection',
                        lambda *args, **kwargs: False)
    assert main(ARGS, base_root=ROOT) == 3


def test_terminal_failed_acceptance_does_not_execute(terminal_failure_launch, monkeypatch, capsys):
    run = Mock(side_effect=AssertionError('Execution must require acceptance'))
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    args = [argument for argument in ARGS if argument != '--start=True']
    assert main(args, base_root=ROOT) == 2
    run.assert_not_called()
    assert 'requires START=True/E2E or DEBUG=True' in capsys.readouterr().err


@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_terminal_retained_cleanup_raises_safe_wrapper_with_usable_receipt(
        terminal_failure_launch, monkeypatch, capsys, error_type):
    import asyncio
    calls = []

    async def retry():
        calls.append('retried')

    receipt = SimpleNamespace(retry=retry)
    group = SimpleNamespace(active=True)
    handles = (object(),)
    recording = SimpleNamespace(pending_cleanup=receipt)
    error = error_type('cleanup failure synthetic-credential-value')
    error.cleanup_pending = receipt
    error.inspection_group = group
    error.inspections = handles
    error.recording_prerequisites = recording

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(error_type) as caught:
        main(ARGS, base_root=ROOT)
    assert caught.value is not error
    assert caught.value.cleanup_pending is receipt
    assert caught.value.inspection_group is group
    assert caught.value.inspections is handles
    assert caught.value.recording_prerequisites is recording
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
    assert 'synthetic-credential-value' not in str(caught.value)
    assert 'synthetic-credential-value' not in capsys.readouterr().err
    asyncio.run(caught.value.cleanup_pending.retry())
    assert calls == ['retried']


def test_unhandled_terminal_cleanup_traceback_never_reprints_original_credentials(tmp_path):
    import subprocess
    import sys
    import textwrap

    declaration = tmp_path / 'config.toml'
    declaration.write_text('version = 1\n[fields.SYNTHETIC_API_KEY]\ntype = "str"\n')
    script = tmp_path / 'controlled_cli.py'
    script.write_text(textwrap.dedent(f'''
        from dataclasses import replace
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from api_agent.runtime.bootstrap import main
        from api_agent.runtime.resource_pack import load_launch_context
        root = Path({str(ROOT)!r})
        context = replace(load_launch_context(root), config_path=Path({str(declaration)!r}))
        arguments = {ARGS!r}
        def run(*args, **kwargs):
            error = RuntimeError('original source literal synthetic-credential-value')
            error.cleanup_pending = SimpleNamespace(retry=lambda: None)
            raise error
        with patch.dict('os.environ', {{}}, clear=True), \\
             patch('api_agent.runtime.resource_pack.load_launch_context', return_value=context), \\
             patch('api_agent.config.read_dotenv', return_value={{}}), \\
             patch('api_agent.config.dotenv.read_dotenv', return_value={{}}), \\
             patch('api_agent.runtime.worktrees.verify_worktrees'), \\
             patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \\
             patch('api_agent.testing.collection.collect_tests', return_value=[SimpleNamespace(id='base::test.py::test_failure', tags=frozenset())]), \\
             patch('api_agent.testing.launched.run_launched_integration', side_effect=run):
            main(arguments, base_root=root)
    '''))
    process = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True,
                             env={'PYTHONPATH': str(ROOT / 'src')}, timeout=10)
    assert process.returncode == 1
    assert 'original source literal' in process.stderr
    assert 'Noninteractive integration execution' in process.stderr
    assert 'synthetic-credential-value' not in process.stdout + process.stderr


def test_terminal_direct_cleanup_receipt_is_retained(terminal_failure_launch, monkeypatch):
    from api_agent.providers.control import CleanupPendingError

    async def retry():
        return None

    owners = (object(),)
    error = CleanupPendingError(owners, retry)

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(RuntimeError) as caught:
        main(ARGS, base_root=ROOT)
    assert caught.value.cleanup_pending is error
    assert caught.value.owners is error.owners
    assert caught.value.retry is retry


def test_terminal_system_exit_preserves_numeric_status(terminal_failure_launch, monkeypatch):
    def run(*args, **kwargs):
        raise SystemExit(7)

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(SystemExit) as caught:
        main(ARGS, base_root=ROOT)
    assert caught.value.code == 7


def test_terminal_generator_exit_uses_safe_boundary(terminal_failure_launch, monkeypatch):
    def run(*args, **kwargs):
        raise GeneratorExit('synthetic-credential-value')

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(RuntimeError) as caught:
        main(ARGS, base_root=ROOT)
    assert 'synthetic-credential-value' not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('stage', ['dependencies', 'collection'])
def test_terminal_prelaunch_failures_emit_scrubbed_full_chain_without_execution(
        terminal_failure_launch, monkeypatch, capsys, stage):
    error = RuntimeError(f'{stage} failed synthetic-credential-value')

    def fail(*args, **kwargs):
        try:
            raise OSError('cause /synthetic/setup.py unavailable')
        except OSError as cause:
            raise error from cause

    target = ('api_agent.runtime.dependencies.ensure_environment' if stage == 'dependencies'
              else 'api_agent.testing.collection.collect_tests')
    monkeypatch.setattr(target, fail)
    run = Mock(side_effect=AssertionError('Prelaunch failure must not execute'))
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert main(ARGS, base_root=ROOT) == 1
    run.assert_not_called()
    output = capsys.readouterr()
    assert 'Noninteractive integration execution' in output.err
    assert f'{stage} failed' in output.err
    assert 'cause /synthetic/setup.py unavailable' in output.err
    assert 'synthetic-credential-value' not in output.err + output.out


def test_terminal_dependency_handoff_returns_without_collection_or_execution(
        terminal_failure_launch, monkeypatch):
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *args, **kwargs: 7)
    collect = Mock(side_effect=AssertionError('Handoff must not collect'))
    run = Mock(side_effect=AssertionError('Handoff must not execute'))
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', collect)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert main(ARGS, base_root=ROOT) == 7
    collect.assert_not_called()
    run.assert_not_called()


def test_terminal_raw_output_scrubs_known_multiline_credentials_across_chunks(
        terminal_failure_launch, monkeypatch, capsys):
    from api_agent.testing.diagnostics import CredentialScrubber
    scrubber = CredentialScrubber(secret_values=('credential-line-one\ncredential-line-two',))
    monkeypatch.setattr('api_agent.testing.diagnostics.CredentialScrubber', lambda config: scrubber)

    def run(*args, **options):
        options['output']('useful context\ncredential-line-')
        options['output']('one\ncredential-')
        options['output']('line-two\nuseful ending')
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    assert main(ARGS, base_root=ROOT) == 0
    output = capsys.readouterr()
    assert 'useful context' in output.out and 'useful ending' in output.out
    assert 'credential-line-one' not in output.out + output.err
    assert 'credential-line-two' not in output.out + output.err


def test_preconfiguration_oserror_retains_original_cleanup_duty(monkeypatch):
    receipt = SimpleNamespace(owners=(object(),), retry=Mock())
    error = OSError('synthetic launch preparation failure')
    error.cleanup_pending = receipt

    def fail_context(*args, **kwargs):
        raise error

    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', fail_context)
    monkeypatch.setattr('os.environ', {})
    with pytest.raises(OSError) as caught:
        main(['--no-std-in', '--tui-mode=integration', '--start=True'], base_root=ROOT)
    assert caught.value is error
    assert caught.value.cleanup_pending is receipt
    assert caught.value.cleanup_pending.owners is receipt.owners
    receipt.retry.assert_not_called()


@pytest.mark.parametrize('header', [
    'Authorization: Digest username="synthetic-user", nonce="synthetic-nonce", response="synthetic-response"',
    'Authorization: Custom username=synthetic-user, response=synthetic-response',
    'Authorization: Custom synthetic-user, synthetic-response',
    'Authorization: Custom synthetic-user synthetic-response',
    'Proxy-Authorization: Custom synthetic-user, synthetic-response',
    'Proxy-Authorization: Custom synthetic-user synthetic-response',
    'Proxy-Authorization: Digest username="synthetic-user", response="synthetic-response"',
    '{"Authorization": "Digest username=synthetic-user, response=synthetic-response", "reason": "useful reason"}',
    "{'authorization': 'Digest username=synthetic-user, response=synthetic-response', 'reason': 'useful reason'}",
])
def test_actual_terminal_scrubs_complete_multipart_authorization(
        terminal_failure_launch, monkeypatch, capsys, header):
    identities, _ = terminal_failure_launch
    tests = tuple(TestResult(identity, 'error', local_diagnostic=header + '\nuseful reason /tmp/client.py:8')
                  for identity in identities)
    recorded = SimpleNamespace(execution=IntegrationRunResult(1, tests, ''), persisted=True, exit_code=1)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', lambda *a, **k: recorded)
    assert main(ARGS, base_root=ROOT) == 1
    output = capsys.readouterr().err
    assert all(value not in output for value in ('synthetic-user', 'synthetic-nonce', 'synthetic-response'))
    assert '[REDACTED]' in output and 'useful reason' in output and '/tmp/client.py:8' in output


@pytest.mark.parametrize('secret', ["synthetic'credential", 'synthetic"credential', "both'\" credential", "a' b' c"])
def test_actual_terminal_scrubs_complete_shell_quoted_configured_argument(
        terminal_failure_launch, monkeypatch, capsys, secret):
    import shlex
    identities, _ = terminal_failure_launch
    command = shlex.join(('tool', '--api-key', secret, '--root', '/tmp/suite'))
    tests = tuple(TestResult(identity, 'error', local_diagnostic='Command: ' + command) for identity in identities)
    recorded = SimpleNamespace(execution=IntegrationRunResult(1, tests, ''), persisted=True, exit_code=1)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', lambda *a, **k: recorded)
    args = [('--synthetic-api-key=' + secret if value.startswith('--synthetic-api-key=') else value) for value in ARGS]
    assert main(args, base_root=ROOT) == 1
    output = capsys.readouterr().err
    commands = [line.split('Command: ', 1)[1] for line in output.splitlines() if 'Command: ' in line]
    assert commands
    assert all(shlex.split(command) == ['tool', '--api-key', '[REDACTED]', '--root', '/tmp/suite']
               for command in commands)
    assert secret not in output and 'credential' not in output and shlex.quote(secret) not in output


def test_actual_terminal_retains_primary_interrupt_and_secondary_process_cleanup(
        terminal_failure_launch, monkeypatch, tmp_path, capsys):
    import sys
    import time
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.diagnostics import TerminalDiagnosticInterrupt
    primary = KeyboardInterrupt('primary cancellation')
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    def cleanup(process):
        finalize(process)
        raise RuntimeError('secondary cleanup password=synthetic-secret')
    def run(*args, **kwargs):
        prerequisites.run_command((sys.executable, '-c', 'import time; time.sleep(30)'),
            environment={}, directory=tmp_path, deadline=time.monotonic() + 5, cancelled=lambda: False)
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(TerminalDiagnosticInterrupt) as caught:
        main(ARGS, base_root=ROOT)
    output = capsys.readouterr().err
    assert 'KeyboardInterrupt: primary cancellation' in output and 'RuntimeError: secondary cleanup' in output
    assert 'Command:' in output and 'Exit code:' in output and 'synthetic-secret' not in output
    assert caught.value.cleanup_pending is primary.cleanup_pending


def test_actual_terminal_inspection_cleanup_keeps_causes_and_receipt(
        terminal_failure_launch, monkeypatch, capsys):
    from api_agent.testing.inspection_controls import finish_terminal_inspection
    from api_agent.testing.diagnostics import TerminalDiagnosticError
    handle = SimpleNamespace(closed=False, wait_closed=lambda: None)
    handle.end_inspection = Mock(side_effect=RuntimeError('end cause password=synthetic-secret'))
    handle.abort = Mock(side_effect=ValueError('abort cause'))
    identities, _ = terminal_failure_launch
    recorded = SimpleNamespace(execution=IntegrationRunResult(0, tuple(TestResult(test, 'passed') for test in identities), '', inspections=(handle,)), persisted=True)
    def run(*args, **kwargs):
        finish_terminal_inspection(recorded, kwargs['output'])
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    with pytest.raises(TerminalDiagnosticError) as caught:
        main(ARGS, base_root=ROOT)
    output = capsys.readouterr().err
    assert 'RuntimeError: end cause' in output and 'ValueError: abort cause' in output
    assert 'synthetic-secret' not in output and caught.value.inspections == (handle,)
    assert caught.value.cleanup_pending.inspection_group.active


def test_actual_terminal_scrubs_truncated_shell_credential_evidence(
        terminal_failure_launch, monkeypatch, tmp_path, capsys):
    import sys
    import time
    from api_agent.runtime.prerequisites import run_command
    secret = 'synthetic-private-prefix ' + 'x' * 5000
    def run(*args, **kwargs):
        run_command((sys.executable, '-c', 'import sys; sys.exit(1)', '--password', secret),
            environment={}, directory=tmp_path, deadline=time.monotonic() + 5, cancelled=lambda: False)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    args = [('--synthetic-api-key=' + secret if value.startswith('--synthetic-api-key=') else value) for value in ARGS]
    assert main(args, base_root=ROOT) == 1
    output = capsys.readouterr().err
    assert 'synthetic-private-prefix' not in output and '[REDACTED]' in output
    assert 'command characters omitted]' in output and 'Exit code: 1' in output


@pytest.mark.parametrize('syntax', ['separate', 'assignment'])
@pytest.mark.parametrize('prefix', ['synthetic-head,synthetic-tail', 'synthetic-head\nsynthetic-tail'])
def test_actual_terminal_command_truncation_excludes_comma_and_newline_credential_fragments(
        terminal_failure_launch, monkeypatch, tmp_path, capsys, prefix, syntax):
    import sys
    import time
    from api_agent.runtime.prerequisites import run_command
    secret = prefix + 'x' * 5000
    credential_args = ('--password', secret) if syntax == 'separate' else ('PASSWORD=' + secret,)
    def run(*args, **kwargs):
        run_command((sys.executable, '-c', 'import sys; sys.exit(1)', *credential_args, '--root', '/tmp/public'),
            environment={}, directory=tmp_path, deadline=time.monotonic() + 5, cancelled=lambda: False)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    args = [('--synthetic-api-key=' + secret if value.startswith('--synthetic-api-key=') else value) for value in ARGS]
    assert main(args, base_root=ROOT) == 1
    output = capsys.readouterr().err
    assert 'synthetic-head' not in output and 'synthetic-tail' not in output
    assert '[REDACTED]' in output and '--root /tmp/public' in output
    assert 'command characters omitted]' in output and 'Exit code: 1' in output


@pytest.mark.parametrize('secondary_type', [KeyboardInterrupt, __import__('asyncio').CancelledError, GeneratorExit, SystemExit])
def test_actual_terminal_secondary_disposal_interrupt_retains_live_helper_retry(
        terminal_failure_launch, monkeypatch, tmp_path, capsys, secondary_type):
    import asyncio
    import sys
    import time
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.diagnostics import TerminalDiagnosticInterrupt
    primary = KeyboardInterrupt('primary command interruption')
    secondary = secondary_type('secondary disposal interruption password=synthetic-secret')
    checks = iter((None, primary))
    processes = []
    finalize = prerequisites.finalize
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    def interrupted_cleanup(process):
        processes.append(process)
        raise secondary
    def run(*args, **kwargs):
        prerequisites.run_command((sys.executable, '-c', 'import time; time.sleep(30)'),
            environment={}, directory=tmp_path, deadline=time.monotonic() + 5, cancelled=lambda: False)
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', interrupted_cleanup)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    try:
        with pytest.raises(TerminalDiagnosticInterrupt) as caught:
            main(ARGS, base_root=ROOT)
        assert processes[0].poll() is None
        assert caught.value.cleanup_pending is primary.cleanup_pending
        assert caught.value.cleanup_pending.owners == (processes[0],)
        output = capsys.readouterr().err
        assert 'primary command interruption' in output and 'secondary disposal interruption' in output
        assert 'synthetic-secret' not in output
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        asyncio.run(caught.value.cleanup_pending.retry())
        assert processes[0].poll() is not None and caught.value.cleanup_pending.owners == ()
    finally:
        for process in processes:
            finalize(process)


@pytest.mark.parametrize('primary_type', [None, PrerequisiteError, ValueError])
def test_actual_terminal_first_disposal_interrupt_keeps_prior_cause_and_owned_retry(
        terminal_failure_launch, monkeypatch, tmp_path, capsys, primary_type):
    import asyncio
    import sys
    import time
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.diagnostics import TerminalDiagnosticInterrupt
    primary = primary_type('prior ordinary failure') if primary_type else None
    interruption = KeyboardInterrupt('first disposal interruption')
    checks = iter((None, primary))
    processes = []
    finalize = prerequisites.finalize
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    def interrupted_cleanup(process):
        processes.append(process)
        raise interruption
    def run(*args, **kwargs):
        script = 'import sys; sys.exit(0)' if primary is None else 'import time; time.sleep(30)'
        prerequisites.run_command((sys.executable, '-c', script), environment={}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)
    if primary is not None:
        monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', interrupted_cleanup)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    try:
        with pytest.raises(TerminalDiagnosticInterrupt) as caught:
            main(ARGS, base_root=ROOT)
        receipt = caught.value.cleanup_pending
        assert receipt is interruption.cleanup_pending and receipt.owners == (processes[0],)
        assert caught.value is not interruption
        if primary is not None:
            assert processes[0].poll() is None
        output = capsys.readouterr().err
        assert 'KeyboardInterrupt: first disposal interruption' in output
        assert 'Command:' in output and 'Exit code:' in output
        assert (('prior ordinary failure' in output) is (primary is not None))
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        asyncio.run(receipt.retry())
        assert processes[0].poll() is not None and receipt.owners == ()
    finally:
        for process in processes:
            finalize(process)
