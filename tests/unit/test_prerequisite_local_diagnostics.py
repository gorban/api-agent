"""Synthetic commands retain bounded local evidence without changing safe errors."""
import sys
import time

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError, run_command


def command_args(tmp_path):
    return dict(environment={}, directory=tmp_path, deadline=time.monotonic() + 5,
                cancelled=lambda: False)


def test_nonzero_command_preserves_both_streams_only_in_local_details(tmp_path):
    script = tmp_path / 'synthetic_failure.py'
    script.write_text('import sys\nprint("output evidence")\n'
                      'print("error evidence", file=sys.stderr)\nsys.exit(7)\n')
    command = (sys.executable, str(script))
    with pytest.raises(PrerequisiteError) as caught:
        run_command(command, **command_args(tmp_path))
    error = caught.value
    assert str(error) == 'Prerequisite command failed (exit 7).'
    command_transcript = error.local_details.split('\n', 1)[0]
    assert 'output evidence' not in command_transcript
    assert 'error evidence' not in command_transcript
    stdout_section, stderr_section = error.local_details.split('\nstdout (', 1)[1].split('\nstderr (', 1)
    assert 'output evidence' in stdout_section
    assert 'error evidence' in stderr_section
    assert 'Command:' in error.local_details and 'Exit code: 7' in error.local_details
    assert 'evidence' not in str(error)


def test_nonzero_diagnostics_keep_bounded_tails_and_exact_omission_counts(tmp_path):
    command = (sys.executable, '-c',
               'import os,sys; os.write(1,b"a"*20000+b"OUT-END"); os.write(2,b"b"*21000+b"ERR-END"); sys.exit(4)')
    with pytest.raises(PrerequisiteError) as caught:
        run_command(command, **command_args(tmp_path))
    details = caught.value.local_details
    assert 'stdout (20007 bytes captured; 3623 bytes omitted):' in details
    assert 'stderr (21007 bytes captured; 4623 bytes omitted):' in details
    assert 'OUT-END' in details and 'ERR-END' in details
    assert len(details) < 34000
    assert str(caught.value) == 'Prerequisite command failed (exit 4).'


def test_response_bound_failure_retains_stderr_tail(tmp_path):
    command = (sys.executable, '-c', 'import os; os.write(2,b"x"*(9*1024*1024))')
    with pytest.raises(PrerequisiteError, match='response exceeds') as caught:
        run_command(command, **command_args(tmp_path))
    assert 'stderr (' in caught.value.local_details
    assert 'bytes omitted' in caught.value.local_details
    assert 'x' * 16384 in caught.value.local_details
    assert len(caught.value.local_details) < 17000


@pytest.mark.parametrize('cancelled', [False, True])
def test_interrupted_command_retains_already_captured_output(tmp_path, cancelled, monkeypatch):
    import api_agent.runtime.prerequisites as prerequisites
    original_finalize = prerequisites.finalize
    reaped_status = []

    def record_cleanup(process):
        original_finalize(process)
        reaped_status.append(process.returncode)

    monkeypatch.setattr(prerequisites, 'finalize', record_cleanup)
    started = time.monotonic()
    args = command_args(tmp_path)
    args['deadline'] = started + (5 if cancelled else .4)
    args['cancelled'] = lambda: cancelled and time.monotonic() - started > .4
    command = (sys.executable, '-c',
               'import os,time; os.write(2,b"before interruption"); time.sleep(30)')
    with pytest.raises(PrerequisiteError, match='cancelled' if cancelled else 'deadline exceeded') as caught:
        run_command(command, **args)
    stderr_section = caught.value.local_details.split('\nstderr (', 1)[1]
    assert 'before interruption' in stderr_section
    assert 'before interruption' not in str(caught.value)
    assert len(reaped_status) == 1 and isinstance(reaped_status[0], int)
    assert f'Exit code: {reaped_status[0]}' in caught.value.local_details
    assert 'Exit code: unavailable' not in caught.value.local_details


def test_unavailable_command_retains_attempted_command(tmp_path):
    with pytest.raises(PrerequisiteError, match='unavailable') as caught:
        run_command(('/nonexistent-synthetic-command',), **command_args(tmp_path))
    assert 'Command: /nonexistent-synthetic-command' in caught.value.local_details
    assert 'Exit code: unavailable' in caught.value.local_details


def test_cleanup_failure_retains_process_evidence_and_primary_chain(tmp_path, monkeypatch):
    import api_agent.runtime.prerequisites as prerequisites
    original_finalize = prerequisites.finalize

    def failed_cleanup(process):
        original_finalize(process)
        raise RuntimeError('synthetic cleanup failure')

    monkeypatch.setattr(prerequisites, 'finalize', failed_cleanup)
    command = (sys.executable, '-c', 'import os,sys; os.write(2,b"primary evidence"); sys.exit(2)')
    with pytest.raises(PrerequisiteError, match='cleanup failed') as caught:
        run_command(command, **command_args(tmp_path))
    stderr_section = caught.value.local_details.split('\nstderr (', 1)[1]
    assert 'primary evidence' in stderr_section
    assert 'Exit code: 2' in caught.value.local_details
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert isinstance(caught.value.__cause__.__context__, PrerequisiteError)
    primary_stderr = caught.value.__cause__.__context__.local_details.split('\nstderr (', 1)[1]
    assert 'primary evidence' in primary_stderr


def test_long_command_evidence_has_explicit_bounded_truncation(tmp_path):
    argument = 'z' * 10000
    command = (sys.executable, '-c', 'import sys; sys.exit(1)', argument)
    with pytest.raises(PrerequisiteError) as caught:
        run_command(command, **command_args(tmp_path))
    assert 'command characters omitted]' in caught.value.local_details
    assert argument not in caught.value.local_details
    assert len(caught.value.local_details) < 4400


@pytest.mark.parametrize('cancelled, deadline, expected_message', [
    (True, 10, 'Prerequisite operation cancelled.'),
    (False, 0, 'Prerequisite deadline exceeded.'),
])
def test_prelaunch_interruption_retains_attempt_without_starting_process(
        tmp_path, monkeypatch, cancelled, deadline, expected_message):
    import api_agent.runtime.prerequisites as prerequisites
    launches = []

    def forbidden_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError('Prelaunch interruption must not start a process')

    monkeypatch.setattr(prerequisites.subprocess, 'Popen', forbidden_launch)
    with pytest.raises(PrerequisiteError) as caught:
        run_command(('synthetic-tool', 'inspect'), environment={}, directory=tmp_path,
                    deadline=deadline, cancelled=lambda: cancelled, now=lambda: 1)
    assert launches == []
    assert str(caught.value) == expected_message
    assert caught.value.local_details == (
        'Command: synthetic-tool inspect\nExit code: unavailable\n'
        'stdout (0 bytes captured; 0 bytes omitted):\n(no output captured)\n'
        'stderr (0 bytes captured; 0 bytes omitted):\n(no output captured)')


@pytest.mark.parametrize('failure', [KeyboardInterrupt, __import__('asyncio').CancelledError, GeneratorExit])
def test_secondary_cleanup_preserves_primary_interruption_and_retry_owner(tmp_path, monkeypatch, failure):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.diagnostics import CredentialScrubber, format_exception
    primary = failure('primary synthetic interruption')
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    original_finalize = prerequisites.finalize
    owned = []
    def cleanup(process):
        owned.append(process)
        original_finalize(process)
        raise RuntimeError('secondary cleanup API_KEY=synthetic-secret')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(failure) as caught:
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    assert caught.value is primary
    diagnostic = CredentialScrubber()(format_exception(primary))
    assert 'primary synthetic interruption' in diagnostic and 'secondary cleanup' in diagnostic
    assert 'Command:' in diagnostic and 'Exit code:' in diagnostic and 'synthetic-secret' not in diagnostic
    assert primary.cleanup_pending.owners == ()
    monkeypatch.setattr(prerequisites, 'finalize', original_finalize)
    asyncio.run(primary.cleanup_pending.retry())


def test_secondary_process_cleanup_preserves_existing_receipt_duties(tmp_path, monkeypatch):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    owner = object()
    retry = AsyncMock()
    prior = CleanupPendingError((owner,), retry)
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    processes = []
    def cleanup(process):
        processes.append(process)
        finalize(process)
        raise RuntimeError('process cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    assert caught.value is primary
    assert primary.cleanup_pending.owners == (owner,)
    assert primary.cleanup_pending.receipts == (prior,) and prior.retry is retry
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    asyncio.run(primary.cleanup_pending.retry())
    retry.assert_awaited_once()


def test_existing_failed_retry_still_attempts_new_process_cleanup(tmp_path, monkeypatch, owned_pending_helpers):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock, Mock
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = CleanupPendingError((object(),), AsyncMock(side_effect=ValueError('prior retry failed')))
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    def cleanup(process):
        raise RuntimeError('new cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    retried = Mock(wraps=finalize)
    monkeypatch.setattr(prerequisites, 'finalize', retried)
    with pytest.raises(ValueError, match='prior retry failed'):
        asyncio.run(primary.cleanup_pending.retry())
    retried.assert_called_once()


def test_existing_retry_successor_receipt_survives_combined_retry(tmp_path, monkeypatch):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    next_owner = object()
    successor = CleanupPendingError((next_owner,), AsyncMock())
    old_retry = AsyncMock(side_effect=successor)
    prior = CleanupPendingError((object(),), old_retry)
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    def cleanup(process):
        finalize(process)
        raise RuntimeError('process cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    with pytest.raises(CleanupPendingError) as caught:
        asyncio.run(receipt.retry())
    assert caught.value is successor and successor.cleanup_pending is receipt
    assert receipt.receipts == (prior, successor)
    assert receipt.owners[0] is next_owner
    asyncio.run(receipt.retry())
    successor.retry.assert_awaited_once()
    old_retry.assert_awaited_once()


@pytest.mark.parametrize('successor_pending', [False, True])
def test_process_retry_failure_retains_complete_combined_ownership(tmp_path, monkeypatch, owned_pending_helpers, successor_pending):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    prior = CleanupPendingError((object(),), AsyncMock())
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    def cleanup(process):
        raise RuntimeError('initial cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    successor = CleanupPendingError((object(),), AsyncMock()) if successor_pending else RuntimeError('retry cleanup failed')
    monkeypatch.setattr(prerequisites, 'finalize', lambda process: (_ for _ in ()).throw(successor))
    with pytest.raises(type(successor)) as caught:
        asyncio.run(receipt.retry())
    assert caught.value is successor and successor.cleanup_pending is receipt
    assert prior.owners[0] not in receipt.owners and prior in receipt.receipts
    if successor_pending:
        assert receipt.owners == successor.owners
        assert receipt.receipts == (prior, successor)
        asyncio.run(receipt.retry())
        successor.retry.assert_awaited_once()
    else:
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        asyncio.run(receipt.retry())


def test_truncated_shell_credential_argument_is_scrubbed_with_omission_marker(tmp_path):
    from api_agent.testing.diagnostics import CredentialScrubber
    secret = 'synthetic-private-prefix ' + 'x' * 5000
    with pytest.raises(PrerequisiteError) as caught:
        run_command((sys.executable, '-c', 'import sys; sys.exit(1)', '--password', secret), **command_args(tmp_path))
    diagnostic = CredentialScrubber(secret_values=(secret,))(caught.value.local_details)
    assert 'synthetic-private-prefix' not in diagnostic and '[REDACTED]' in diagnostic
    assert 'command characters omitted]' in diagnostic and 'Exit code: 1' in diagnostic


def test_combined_retry_settles_successful_duty_and_is_idempotent(tmp_path, monkeypatch, owned_pending_helpers):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock, Mock
    owner = object()
    prior_retry = AsyncMock(side_effect=[None, AssertionError('Acknowledged duty repeated')])
    prior = CleanupPendingError((owner,), prior_retry)
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    owned = []
    def cleanup(process):
        owned.append(process)
        raise RuntimeError('initial cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    process_retry = Mock(side_effect=[RuntimeError('retry cleanup failed'), None, AssertionError('Acknowledged process repeated')])
    monkeypatch.setattr(prerequisites, 'finalize', process_retry)
    with pytest.raises(RuntimeError, match='retry cleanup failed'):
        asyncio.run(receipt.retry())
    assert receipt.owners == (owned[0],) and receipt.receipts == (prior,)
    asyncio.run(receipt.retry())
    assert receipt.owners == ()
    asyncio.run(receipt.retry())
    prior_retry.assert_awaited_once()
    assert process_retry.call_count == 2 and prior.retry is prior_retry


def test_combined_retry_does_not_adopt_itself_as_successor(tmp_path, monkeypatch):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    prior_retry = AsyncMock()
    prior = CleanupPendingError((object(),), prior_retry)
    primary = KeyboardInterrupt('outer interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    def cleanup(process):
        finalize(process)
        raise RuntimeError('initial cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    prior_retry.side_effect = primary
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(receipt.retry())
    assert receipt.receipts == (prior,) and receipt.owners == prior.owners
    prior_retry.side_effect = None
    asyncio.run(receipt.retry())
    assert receipt.owners == () and prior_retry.await_count == 2


@pytest.mark.parametrize('failure', [KeyboardInterrupt, __import__('asyncio').CancelledError, GeneratorExit, SystemExit])
def test_combined_retry_preserves_prior_interrupt_when_process_retry_also_fails(tmp_path, monkeypatch, owned_pending_helpers, failure):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.diagnostics import CredentialScrubber, format_exception
    from unittest.mock import AsyncMock
    interruption = failure('primary retry interruption')
    prior_retry = AsyncMock(side_effect=interruption)
    owner = object()
    prior = CleanupPendingError((owner,), prior_retry)
    primary = KeyboardInterrupt('initial interruption')
    primary.cleanup_pending = prior
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    processes = []
    def cleanup(process):
        processes.append(process)
        raise RuntimeError('initial process cleanup failed')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    secondary = RuntimeError('secondary retry failure password=synthetic-secret')
    monkeypatch.setattr(prerequisites, 'finalize', lambda process: (_ for _ in ()).throw(secondary))
    with pytest.raises(failure) as caught:
        asyncio.run(receipt.retry())
    assert caught.value is interruption and interruption.cleanup_pending is receipt
    assert receipt.owners == (owner, processes[0])
    diagnostic = CredentialScrubber()(format_exception(interruption))
    assert 'primary retry interruption' in diagnostic and 'secondary retry failure' in diagnostic
    assert 'synthetic-secret' not in diagnostic
    prior_retry.side_effect = None
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    asyncio.run(receipt.retry())
    assert receipt.owners == ()


@pytest.mark.parametrize('primary_type', [KeyboardInterrupt, PrerequisiteError])
@pytest.mark.parametrize('nested', [False, True])
def test_command_disposal_retains_both_receipt_chains_once(tmp_path, monkeypatch, primary_type, nested):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    owners = (object(), object(), object())
    callbacks = tuple(AsyncMock(side_effect=[None, AssertionError('Settled duty repeated')]) for _ in owners)
    receipts = tuple(CleanupPendingError((owner,), callback) for owner, callback in zip(owners, callbacks))
    primary = primary_type('initial failure')
    secondary = KeyboardInterrupt('disposal interruption')
    primary.cleanup_pending, secondary.cleanup_pending = receipts[:2]
    if nested:
        receipts[0].cleanup_pending = receipts[1]
        receipts[1].cleanup_pending = receipts[2]
        receipts[2].cleanup_pending = receipts[0]
        receipts[2].owners = (owners[0], owners[2])
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    processes = []
    def cleanup(process):
        processes.append(process)
        raise secondary
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
        assert caught.value is (primary if primary_type is KeyboardInterrupt else secondary)
        receipt = caught.value.cleanup_pending
        expected = receipts if nested else receipts[:2]
        assert receipt.receipts == expected
        assert receipt.owners == (*owners[:len(expected)], processes[0])
        assert processes[0].poll() is None
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        asyncio.run(receipt.retry())
        assert receipt.owners == () and processes[0].poll() is not None
        asyncio.run(receipt.retry())
        for callback in callbacks[:len(expected)]:
            callback.assert_awaited_once()
    finally:
        for process in processes:
            finalize(process)


def test_failed_receipt_retry_attempts_siblings_and_settles_their_duties(tmp_path, monkeypatch):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock
    owners = (object(), object())
    interruption = KeyboardInterrupt('first retry interrupted')
    first_retry = AsyncMock(side_effect=interruption)
    second_retry = AsyncMock(side_effect=[None, AssertionError('Settled sibling repeated')])
    first, second = (CleanupPendingError((owner,), callback) for owner, callback in zip(owners, (first_retry, second_retry)))
    primary = KeyboardInterrupt('initial interruption')
    secondary = KeyboardInterrupt('disposal interruption')
    primary.cleanup_pending, secondary.cleanup_pending = first, second
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    processes = []
    def cleanup(process):
        processes.append(process)
        raise secondary
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
        receipt = primary.cleanup_pending
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        with pytest.raises(KeyboardInterrupt) as caught:
            asyncio.run(receipt.retry())
        assert caught.value is interruption
        assert receipt.owners == (owners[0],) and processes[0].poll() is not None
        first_retry.side_effect = None
        asyncio.run(receipt.retry())
        assert receipt.owners == ()
        second_retry.assert_awaited_once()
    finally:
        for process in processes:
            finalize(process)


def test_self_rethrow_with_child_receipt_keeps_outer_duty_pending_until_acknowledged(tmp_path, monkeypatch):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.providers.control import CleanupPendingError
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    outer_owner, child_owner = SimpleNamespace(closed=False), SimpleNamespace(closed=False)
    child_retry = AsyncMock(side_effect=lambda: setattr(child_owner, 'closed', True))
    child = CleanupPendingError((child_owner,), child_retry)
    async def close_outer():
        if not child_owner.closed:
            raise outer
        outer_owner.closed = True
    outer_retry = AsyncMock(side_effect=close_outer)
    outer = CleanupPendingError((outer_owner,), outer_retry)
    outer.cleanup_pending = child
    primary = KeyboardInterrupt('initial interruption')
    primary.cleanup_pending = outer
    checks = iter((None, primary))
    def check(*args):
        value = next(checks)
        if value is not None:
            raise value
    finalize = prerequisites.finalize
    processes = []
    def cleanup(process):
        processes.append(process)
        raise RuntimeError('owned process cleanup pending')
    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites, 'finalize', cleanup)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
        receipt = primary.cleanup_pending
        assert receipt.owners == (outer_owner, child_owner, processes[0])
        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        with pytest.raises(CleanupPendingError) as caught:
            asyncio.run(receipt.retry())
        assert caught.value is outer
        assert child_owner.closed and not outer_owner.closed
        assert receipt.owners == (outer_owner,) and processes[0].poll() is not None
        asyncio.run(receipt.retry())
        assert outer_owner.closed and receipt.owners == ()
        asyncio.run(receipt.retry())
        assert outer_retry.await_count == 2
        child_retry.assert_awaited_once()
    finally:
        for process in processes:
            finalize(process)


@pytest.mark.parametrize('prefix', ['synthetic-head,synthetic-tail', 'synthetic-head\nsynthetic-tail'])
def test_truncated_raw_assignment_redacts_whole_value_in_command_evidence(tmp_path, prefix):
    secret = prefix + 'x' * 5000
    command = (sys.executable, '-c', 'import sys; sys.exit(1)', 'PASSWORD=' + secret, '--root', '/tmp/public')

    with pytest.raises(PrerequisiteError) as caught:
        run_command(command, **command_args(tmp_path))

    diagnostic = caught.value.local_details
    assert 'synthetic-head' not in diagnostic and 'synthetic-tail' not in diagnostic
    assert 'PASSWORD=[REDACTED]' in diagnostic
    assert '--root /tmp/public' in diagnostic
    assert 'command characters omitted]' in diagnostic and 'Exit code: 1' in diagnostic


@pytest.fixture
def interrupted_owned_command(tmp_path, monkeypatch):
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.processes import disposal_complete, finalize
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import AsyncMock

    primary = KeyboardInterrupt('synthetic primary interruption')
    independent_owner = object()
    independent_retry = AsyncMock()
    primary.cleanup_pending = CleanupPendingError((independent_owner,), independent_retry)
    checks = iter((None, primary))
    processes = []

    def check(*args):
        error = next(checks)
        if error is not None:
            raise error

    monkeypatch.setattr(prerequisites, '_check', check)
    yield prerequisites, primary, independent_owner, independent_retry, processes, finalize
    for process in processes:
        if not disposal_complete(process):
            finalize(process)


def test_initial_confirmed_disposal_preserves_independent_duty_without_refinalizing(
        tmp_path, monkeypatch, interrupted_owned_command):
    import asyncio
    from api_agent.testing.processes import disposal_complete
    from api_agent.testing.diagnostics import format_exception
    prerequisites, primary, owner, independent_retry, processes, finalize = interrupted_owned_command
    secondary = KeyboardInterrupt('synthetic interruption after confirmed disposal')

    def interrupted_finalization(process):
        processes.append(process)
        finalize(process)
        assert disposal_complete(process)
        raise secondary

    monkeypatch.setattr(prerequisites, 'finalize', interrupted_finalization)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    attempts = []
    monkeypatch.setattr(prerequisites, 'finalize', attempts.append)

    assert caught.value is primary
    assert receipt.owners == (owner,)
    assert processes[0].poll() is not None
    assert 'synthetic interruption after confirmed disposal' in format_exception(primary)
    assert 'Command:' in primary.local_details
    asyncio.run(receipt.retry())
    asyncio.run(receipt.retry())
    independent_retry.assert_awaited_once()
    assert receipt.owners == () and attempts == []


def test_cancelled_drained_disposal_preserves_pending_independent_duty_without_refinalizing(
        tmp_path, monkeypatch, interrupted_owned_command):
    import asyncio
    import threading
    from api_agent.testing.processes import disposal_complete
    prerequisites, primary, owner, independent_retry, processes, finalize = interrupted_owned_command

    def pending_finalization(process):
        processes.append(process)
        raise RuntimeError('synthetic initial pending disposal')

    monkeypatch.setattr(prerequisites, 'finalize', pending_finalization)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending
    independent_retry.side_effect = [RuntimeError('synthetic independent release still pending'), None]
    entered, release = threading.Event(), threading.Event()
    calls, attempts = [], []

    def blocked_finalization(process):
        calls.append(process)
        finalize(process)
        assert disposal_complete(process)
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(prerequisites, 'finalize', blocked_finalization)

    async def scenario():
        task = asyncio.create_task(receipt.retry())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value.cleanup_pending is receipt
        assert receipt.owners == (owner,)
        assert 'synthetic independent release still pending' in str(cancelled.value.diagnostic_errors[0])
        monkeypatch.setattr(prerequisites, 'finalize', attempts.append)
        await receipt.retry()
        await receipt.retry()

    assert caught.value is primary
    asyncio.run(scenario())
    assert disposal_complete(processes[0]) and processes[0].poll() is not None
    assert calls == [processes[0]] and attempts == []
    assert independent_retry.await_count == 2 and receipt.owners == ()


def test_independent_receipt_disposal_acknowledges_process_before_own_retry(
        tmp_path, monkeypatch, interrupted_owned_command):
    import asyncio
    prerequisites, primary, owner, independent_retry, processes, finalize = interrupted_owned_command

    def pending_finalization(process):
        processes.append(process)
        raise RuntimeError('synthetic pending disposal')

    monkeypatch.setattr(prerequisites, 'finalize', pending_finalization)
    with pytest.raises(KeyboardInterrupt):
        run_command((sys.executable, '-c', 'import time; time.sleep(30)'), **command_args(tmp_path))
    receipt = primary.cleanup_pending

    async def independently_dispose():
        finalize(processes[0])

    independent_retry.side_effect = independently_dispose
    attempts = []
    monkeypatch.setattr(prerequisites, 'finalize', attempts.append)
    asyncio.run(receipt.retry())

    independent_retry.assert_awaited_once()
    assert receipt.owners == () and attempts == []


@pytest.fixture
def owned_pending_helpers(monkeypatch):
    """Reap real helpers left pending by intentionally mocked retry outcomes."""
    import api_agent.runtime.prerequisites as prerequisites
    from api_agent.testing.processes import disposal_complete, finalize

    popen = prerequisites.subprocess.Popen
    owned = []

    def capture_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(prerequisites.subprocess, 'Popen', capture_process)
    yield owned
    for process in owned:
        if not disposal_complete(process):
            finalize(process)
