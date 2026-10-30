"""Final command diagnostics use current authority after the cleanup attempt."""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

import pytest

from api_agent.runtime import prerequisites
from api_agent.testing.diagnostics import CredentialScrubber
from api_agent.testing.processes import disposal_complete


def command(tmp_path, exit_code=7, *, wait=False):
    script = tmp_path / 'writer.py'
    script.write_text('import os, sys, time\nprint(os.environ["PAYLOAD"], flush=True)\n' +
                      ('time.sleep(30)\n' if wait else f'sys.exit({exit_code})\n'))
    return (sys.executable, str(script))


def test_finalized_failure_renders_once_with_late_secret_registration(tmp_path, monkeypatch):
    secret = 'synthetic-late-registered-credential'
    scrubber = CredentialScrubber()
    original_capture, original_finalize = scrubber.capture, prerequisites.finalize
    phases, captures = [], []
    processes = []
    def finalize(process):
        processes.append(process)
        original_finalize(process)
        scrubber.register_secret(secret)
        phases.append('finalized')
    def capture(data):
        assert phases == ['finalized'] and disposal_complete(processes[0])
        captures.append(len(data))
        return original_capture(data)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    monkeypatch.setattr(scrubber, 'capture', capture)

    with pytest.raises(prerequisites.PrerequisiteError, match='exit 7') as caught:
        prerequisites.run_command(command(tmp_path), environment={'PAYLOAD': secret}, directory=tmp_path,
            deadline=time.monotonic() + 10, cancelled=lambda: False, output_scrubber=scrubber)

    assert captures == [len(secret) + 1, 0]
    assert secret not in caught.value.local_details and '[REDACTED]' in caught.value.local_details
    assert 'Exit code: 7' in caught.value.local_details
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize('stage', ['selector', 'stdout', 'stderr'])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_close_failure_keeps_one_final_diagnostic_on_primary_and_escaping_error(
        tmp_path, monkeypatch, stage, exit_code):
    secret = 'synthetic-close-failure-credential'
    scrubber = CredentialScrubber(secret_values=(secret,))
    original_capture = scrubber.capture
    original_popen, original_selector = prerequisites.subprocess.Popen, prerequisites.selectors.DefaultSelector
    original_finalize = prerequisites.finalize
    failure = OSError('synthetic close failure')
    processes, pipes, captures, closed = [], [], [], []
    class Pipe:
        def __init__(self, pipe):
            self.pipe = pipe
        def fileno(self):
            return self.pipe.fileno()
        def close(self):
            self.pipe.close()
            closed.append(stage)
            raise failure
        def __getattr__(self, name):
            return getattr(self.pipe, name)
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        pipes.extend((process.stdout, process.stderr))
        if stage != 'selector':
            setattr(process, stage, Pipe(getattr(process, stage)))
        return process
    def selector():
        selected = original_selector()
        if stage == 'selector':
            original_close = selected.close
            def close():
                original_close()
                closed.append(stage)
                raise failure
            monkeypatch.setattr(selected, 'close', close)
        return selected
    def capture(data):
        assert closed == [stage]
        captures.append(len(data))
        return original_capture(data)
    monkeypatch.setattr(prerequisites.subprocess, 'Popen', popen)
    monkeypatch.setattr(prerequisites.selectors, 'DefaultSelector', selector)
    monkeypatch.setattr(scrubber, 'capture', capture)

    try:
        with pytest.raises(OSError) as caught:
            prerequisites.run_command(command(tmp_path, exit_code), environment={'PAYLOAD': secret}, directory=tmp_path,
                deadline=time.monotonic() + 10, cancelled=lambda: False, output_scrubber=scrubber)

        assert caught.value is failure
        assert captures == [len(secret) + 1, 0]
        assert f'Exit code: {exit_code}' in failure.local_details
        assert secret not in failure.local_details and '[REDACTED]' in failure.local_details
        if exit_code:
            primary = failure.__context__
            assert isinstance(primary, prerequisites.PrerequisiteError)
            assert primary.local_details is failure.local_details
        else:
            assert failure.__context__ is None
    finally:
        # This optimization preserves the existing close-error sequencing.
        # Retire the real child independently when that sequence stops early.
        for pipe in pipes:
            pipe.close()
        for process in processes:
            original_finalize(process)


@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_interruption_identity_and_cleanup_receipts_keep_one_final_diagnostic(
        tmp_path, monkeypatch, cleanup_failure):
    from api_agent.providers.control import CleanupPendingError
    secret = 'synthetic-cancellation-credential'
    scrubber = CredentialScrubber()
    primary = asyncio.CancelledError('synthetic command cancellation')
    cleanup_error = OSError('synthetic finalizer failure')
    owner, retry = object(), AsyncMock()
    prior = CleanupPendingError((owner,), retry)
    primary.cleanup_pending = prior
    original_read, original_finalize, original_capture = os.read, prerequisites.finalize, scrubber.capture
    read_output, processes, captures = [], [], []
    def read(fd, size):
        data = original_read(fd, size)
        if data:
            read_output.append(True)
        return data
    def cancelled():
        if read_output:
            raise primary
        return False
    def finalize(process):
        original_finalize(process)
        processes.append(process)
        scrubber.register_secret(secret)
        if cleanup_failure:
            raise cleanup_error
    def capture(data):
        assert len(processes) == 1 and disposal_complete(processes[0])
        captures.append(len(data))
        return original_capture(data)
    monkeypatch.setattr(os, 'read', read)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    monkeypatch.setattr(scrubber, 'capture', capture)

    with pytest.raises(asyncio.CancelledError) as caught:
        prerequisites.run_command(command(tmp_path, wait=True), environment={'PAYLOAD': secret}, directory=tmp_path,
            deadline=time.monotonic() + 10, cancelled=cancelled, output_scrubber=scrubber)

    assert caught.value is primary
    assert captures == [len(secret) + 1, 0]
    assert f'Exit code: {processes[0].returncode}' in primary.local_details
    assert secret not in primary.local_details and '[REDACTED]' in primary.local_details
    assert primary.cleanup_pending.owners == (owner,)
    if cleanup_failure:
        diagnostic, = primary.diagnostic_errors
        assert diagnostic.__cause__ is cleanup_error
        assert diagnostic.local_details is primary.local_details
        assert primary.cleanup_pending.receipts == (prior,)
    else:
        assert primary.cleanup_pending is prior
    asyncio.run(primary.cleanup_pending.retry())
    retry.assert_awaited_once()


def test_finalizer_failure_shares_final_evidence_without_erasing_primary_cause(tmp_path, monkeypatch):
    secret = 'synthetic-finalizer-failure-credential'
    scrubber = CredentialScrubber(secret_values=(secret,))
    original_capture, original_finalize = scrubber.capture, prerequisites.finalize
    cause = OSError('synthetic finalizer failure')
    finalized, captures = [], []
    def finalize(process):
        original_finalize(process)
        finalized.append(process)
        raise cause
    def capture(data):
        assert len(finalized) == 1 and disposal_complete(finalized[0])
        captures.append(len(data))
        return original_capture(data)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    monkeypatch.setattr(scrubber, 'capture', capture)

    with pytest.raises(prerequisites.PrerequisiteError, match='cleanup failed') as caught:
        prerequisites.run_command(command(tmp_path), environment={'PAYLOAD': secret}, directory=tmp_path,
            deadline=time.monotonic() + 10, cancelled=lambda: False, output_scrubber=scrubber)

    assert caught.value.__cause__ is cause
    primary = cause.__context__
    assert isinstance(primary, prerequisites.PrerequisiteError) and 'exit 7' in str(primary)
    assert caught.value.local_details is primary.local_details
    assert captures == [len(secret) + 1, 0]
    assert secret not in caught.value.local_details and '[REDACTED]' in caught.value.local_details
    assert caught.value.cleanup_pending.owners == ()
    asyncio.run(caught.value.cleanup_pending.retry())
