"""Pipe backpressure must preserve execution, bounded memory and cleanup."""
import asyncio
import os
import pytest
from api_agent.runtime.local_terminal import TerminalWriter


def test_writer_drops_with_gap_before_resumed_output_and_restores_flags():
    read, write = os.pipe()
    async def run():
        writer = TerminalWriter((write,), event_limit=2, byte_limit=64)
        writer.submit(write, 'a' * 65)
        writer.submit(write, 'resumed\n')
        await writer.aclose()
        assert writer.bytes == 0
        assert writer.task.done()
        assert os.get_blocking(write)
        writer.submit(write, 'ignored')
        await writer.aclose()
    try:
        asyncio.run(run())
        os.close(write)
        assert os.read(read, 4096) == TerminalWriter.GAP + b'resumed\n'
    finally:
        os.close(read)


def test_blocked_pipe_keeps_loop_responsive_and_cleanup_finite():
    read, write = os.pipe()
    async def run():
        os.set_blocking(write, False)
        while True:
            try:
                os.write(write, b'x' * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write, True)
        writer = TerminalWriter((write,), event_limit=1, byte_limit=64, drain_timeout=.01)
        writer.submit(write, 'first')
        await asyncio.sleep(.001)
        writer.submit(write, 'drop')
        assert writer.gaps == {write}
        assert writer.bytes <= 64
        await asyncio.wait_for(writer.aclose(), .1)
        assert writer.task.cancelled()
        assert os.get_blocking(write)
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_partial_write_and_broken_pipe_are_owned(monkeypatch):
    read, write = os.pipe()
    original = os.write
    async def run():
        writer = TerminalWriter((write,), drain_timeout=.01)
        monkeypatch.setattr(os, 'write', lambda fd, value: original(fd, value[:1]))
        writer.submit(write, 'ok')
        await writer.aclose()
        assert writer.bytes == 0
        assert os.read(read, 2) == b'ok'
        second = TerminalWriter((write,), drain_timeout=.01)
        os.close(read)
        second.submit(write, 'broken')
        second.submit(write, 'discard pending')
        await second.aclose()
        assert second.failed == {write}
        second.submit(write, 'discard future')
    try:
        asyncio.run(run())
    finally:
        os.close(write)


def test_pipe_resumes_after_readiness_callback_and_gap_is_disclosed(monkeypatch):
    read, write = os.pipe()
    original = os.write
    async def run():
        writer = TerminalWriter((write,), byte_limit=64)
        attempts = 0
        def once_blocked(fd, data):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BlockingIOError()
            return original(fd, data)
        def ready_twice(fd, callback):
            callback()
            callback()
        monkeypatch.setattr(os, 'write', once_blocked)
        monkeypatch.setattr(writer.loop, 'add_writer', ready_twice)
        monkeypatch.setattr(writer.loop, 'remove_writer', lambda fd: None)
        writer.submit(write, 'ok')
        await writer.aclose()
        assert attempts == 2
        assert os.read(read, 2) == b'ok'
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_failed_descriptor_pending_gap_is_discarded():
    read, write = os.pipe()
    async def run():
        writer = TerminalWriter((write,))
        writer.failed.add(write)
        writer.gaps.add(write)
        writer.wake.set()
        await writer.aclose()
        assert not writer.gaps
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_default_diagnostic_and_injected_output_use_only_stderr_writer(monkeypatch):
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal
    instances = []
    class Writer:
        def __init__(self, descriptors):
            instances.append(tuple(descriptors))
        def submit(self, fd, text):
            assert fd == 2
        async def finish(self):
            pass
    async def execute(*args, output, diagnostic, **kwargs):
        diagnostic('safe')
        return 0
    monkeypatch.setattr(local_terminal, 'TerminalWriter', Writer)
    monkeypatch.setattr(local_terminal, '_run_local_terminal', execute)
    monkeypatch.setattr(local_terminal.sys, 'stderr', SimpleNamespace(fileno=lambda: 2))
    assert asyncio.run(local_terminal.run_local_terminal(None, None, output=lambda text: None)) == 0
    assert instances == [(2,)]


def test_encoded_byte_bound_rejects_multibyte_payload_independently():
    read, write = os.pipe()
    async def run():
        writer = TerminalWriter((write,), event_limit=256, byte_limit=64)
        writer.submit(write, '\u00e9' * 40)
        assert writer.bytes == 0
        assert not writer.queue
        assert writer.gaps == {write}
        await writer.aclose()
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_blocked_stdout_does_not_starve_stderr():
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    async def run():
        os.set_blocking(stdout_write, False)
        while True:
            try:
                os.write(stdout_write, b'x' * 4096)
            except BlockingIOError:
                break
        os.set_blocking(stdout_write, True)
        os.set_blocking(stderr_read, False)
        writer = TerminalWriter((stdout_write, stderr_write), drain_timeout=.02)
        writer.submit(stdout_write, 'blocked output')
        writer.submit(stderr_write, 'healthy status\n')
        received = bytearray()
        async with asyncio.timeout(.1):
            while not received:
                await asyncio.sleep(.001)
                try:
                    received.extend(os.read(stderr_read, 4096))
                except BlockingIOError:
                    pass
        assert received == b'healthy status\n'
        await writer.aclose()
        assert writer.task.cancelled()
        assert not writer.active
        assert writer.bytes == 0
        assert os.get_blocking(stdout_write)
        assert os.get_blocking(stderr_write)
    try:
        asyncio.run(run())
    finally:
        for fd in (stdout_read, stdout_write, stderr_read, stderr_write):
            os.close(fd)


def test_duplicated_pipe_descriptors_serialize_partial_utf8_events(monkeypatch):
    read, write = os.pipe()
    duplicate = os.dup(write)
    original = os.write
    async def run():
        writer = TerminalWriter((write, duplicate))
        attempts = 0
        def partial(fd, data):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise BlockingIOError()
            return original(fd, data[:1])
        monkeypatch.setattr(os, 'write', partial)
        writer.submit(write, 'é\n')
        writer.submit(duplicate, 'status\n')
        await writer.aclose()
        assert os.read(read, 4096) == 'é\nstatus\n'.encode()
        assert os.get_blocking(write)
        assert os.get_blocking(duplicate)
    try:
        asyncio.run(run())
    finally:
        for fd in (read, write, duplicate):
            os.close(fd)


@pytest.mark.parametrize('failure', ['fstat', 'set_blocking'])
def test_descriptor_acquisition_failure_restores_previous_flags(monkeypatch, failure):
    read, write = os.pipe()
    duplicate = os.dup(write)
    original_stat, original_set = os.fstat, os.set_blocking
    calls = []
    def fail_stat(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError('descriptor unavailable')
        return original_stat(fd)
    def fail_set(fd, blocking):
        calls.append((fd, blocking))
        if len(calls) == 2:
            raise OSError('descriptor unavailable')
        return original_set(fd, blocking)
    async def run():
        monkeypatch.setattr(os, failure, fail_stat if failure == 'fstat' else fail_set)
        with pytest.raises(OSError, match='descriptor unavailable'):
            TerminalWriter((write, duplicate))
        assert os.get_blocking(write)
        assert os.get_blocking(duplicate)
    try:
        asyncio.run(run())
    finally:
        for fd in (read, write, duplicate):
            os.close(fd)


def test_concurrent_close_waits_for_cleanup_after_first_waiter_is_cancelled():
    read, write = os.pipe()
    async def run():
        os.set_blocking(write, False)
        while True:
            try:
                os.write(write, b'x' * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write, True)
        writer = TerminalWriter((write,), drain_timeout=.02)
        writer.submit(write, 'blocked')
        async with asyncio.timeout(.1):
            while not writer.active:
                await asyncio.sleep(.001)
        first = asyncio.create_task(writer.aclose())
        async with asyncio.timeout(.1):
            while not writer.closed:
                await asyncio.sleep(.001)
        second = asyncio.create_task(writer.aclose())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, .1)
        assert writer.close_task.done()
        assert not writer.workers
        assert not writer.active
        assert os.get_blocking(write)
        await writer.aclose()
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_closed_borrowed_descriptor_does_not_prevent_owned_flag_restoration():
    first_read, first_write = os.pipe()
    second_read, second_write = os.pipe()
    async def run():
        writer = TerminalWriter((first_write, second_write))
        os.close(first_write)
        await writer.aclose()
        assert os.get_blocking(second_write)
        assert not writer.pending_flags
        assert not writer.owned
        await writer.aclose()
    try:
        asyncio.run(run())
    finally:
        for fd in (first_read, second_read, second_write):
            os.close(fd)


def test_reused_borrowed_descriptor_is_not_written_or_changed():
    read, write = os.pipe()
    replacement_read, replacement_write = os.pipe()
    async def run():
        writer = TerminalWriter((write,))
        os.close(write)
        os.dup2(replacement_write, write)
        os.set_blocking(write, False)
        os.set_blocking(replacement_read, False)
        writer.submit(write, 'owned output')
        await writer.aclose()
        os.set_blocking(read, False)
        assert os.read(read, 4096) == b'owned output'
        with pytest.raises(BlockingIOError):
            os.read(replacement_read, 4096)
        assert not os.get_blocking(write)
    try:
        asyncio.run(run())
    finally:
        for fd in (read, write, replacement_read, replacement_write):
            os.close(fd)


def test_transient_restoration_failure_can_be_retried_after_cancelled_concurrent_close(monkeypatch):
    read, write = os.pipe()
    second_read, second_write = os.pipe()
    original = os.set_blocking
    attempts = []
    async def run():
        writer = TerminalWriter((write, second_write))
        failed, healthy = tuple(writer.flags)
        failed_owned, healthy_owned = writer.owned[failed], writer.owned[healthy]
        def transient(fd, blocking):
            attempts.append(fd)
            if fd == failed_owned and attempts.count(fd) == 1:
                raise OSError('temporary restoration failure')
            return original(fd, blocking)
        monkeypatch.setattr(os, 'set_blocking', transient)
        first = asyncio.create_task(writer.aclose())
        second = asyncio.create_task(writer.aclose())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(OSError, match='temporary restoration failure'):
            await asyncio.wait_for(second, .1)
        assert os.get_blocking(healthy)
        assert not os.get_blocking(failed)
        assert writer.pending_flags == {failed: True}
        await asyncio.wait_for(writer.aclose(), .1)
        assert os.get_blocking(failed)
        assert not writer.pending_flags
        assert not writer.owned
        assert attempts.count(healthy_owned) == 1
        assert attempts.count(failed_owned) == 2
    try:
        asyncio.run(run())
    finally:
        for fd in (read, write, second_read, second_write):
            os.close(fd)


def test_acquisition_rollback_attempts_all_owned_descriptors(monkeypatch):
    pipes = [os.pipe() for _ in range(3)]
    original_set, original_dup = os.set_blocking, os.dup
    owned, changed, restored = {}, [], []
    def duplicate(fd):
        owned[fd] = original_dup(fd)
        return owned[fd]
    def failing(fd, blocking):
        if not blocking:
            if len(changed) == 2:
                raise OSError('acquisition failed')
            changed.append(fd)
        else:
            restored.append(fd)
            if fd == changed[0] and restored.count(fd) == 1:
                raise OSError('restoration failed')
        original_set(fd, blocking)
    async def run():
        monkeypatch.setattr(os, 'dup', duplicate)
        monkeypatch.setattr(os, 'set_blocking', failing)
        with pytest.raises(OSError, match='acquisition failed'):
            TerminalWriter(tuple(write for _, write in pipes))
        assert restored == [*owned.values(), changed[0]]
        healthy = next(fd for fd, duplicate in owned.items() if duplicate == changed[1])
        assert os.get_blocking(healthy)
        for owned_fd in owned.values():
            with pytest.raises(OSError):
                os.fstat(owned_fd)
    try:
        asyncio.run(run())
    finally:
        for read, write in pipes:
            os.close(read)
            os.close(write)


def test_cancelled_owned_close_task_can_retry_restoration():
    read, write = os.pipe()
    async def run():
        writer = TerminalWriter((write,))
        writer.close_task = asyncio.create_task(writer._close())
        writer.close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer.close_task
        await asyncio.wait_for(writer.aclose(), .1)
        assert os.get_blocking(write)
        assert not writer.owned
    try:
        asyncio.run(run())
    finally:
        os.close(read)
        os.close(write)


def test_same_inode_replacement_keeps_its_flags_and_receives_no_owned_output(tmp_path):
    path = tmp_path / 'output.log'
    write = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    replacement = os.open(path, os.O_WRONLY | os.O_APPEND)
    async def run():
        writer = TerminalWriter((write,))
        os.close(write)
        os.dup2(replacement, write)
        os.set_blocking(write, False)
        writer.submit(write, 'owned')
        await writer.aclose()
        assert not os.get_blocking(write)
        assert os.lseek(replacement, 0, os.SEEK_CUR) == 0
        assert path.read_bytes() == b'owned'
    try:
        asyncio.run(run())
    finally:
        os.close(write)
        os.close(replacement)


def test_close_then_error_retires_number_and_attempts_all_healthy_descriptors(monkeypatch):
    import errno
    pipes = [os.pipe(), os.pipe()]
    original = os.close
    replacement = []
    async def run():
        writer = TerminalWriter(tuple(write for _, write in pipes))
        failed = next(iter(writer.owned.values()))
        attempts = []
        def close(fd):
            attempts.append(fd)
            original(fd)
            if fd == failed:
                replacement.extend(os.pipe())
                assert replacement[0] == failed
                os.set_blocking(replacement[0], False)
                raise OSError(errno.EINTR, 'close interrupted after release')
        monkeypatch.setattr(os, 'close', close)
        with pytest.raises(OSError, match='close interrupted'):
            await writer.aclose()
        assert all(os.get_blocking(write) for _, write in pipes)
        assert not writer.pending_flags
        assert not writer.owned
        await writer.aclose()
        assert attempts.count(failed) == 1
        assert not os.get_blocking(replacement[0])
        os.write(replacement[1], b'replacement data')
        assert os.read(replacement[0], 4096) == b'replacement data'
    try:
        asyncio.run(run())
    finally:
        for fd in [*(fd for pipe in pipes for fd in pipe), *replacement]:
            original(fd)


def test_constructor_close_then_error_preserves_acquisition_and_reused_descriptor(monkeypatch):
    import errno
    pipes = [os.pipe(), os.pipe()]
    original_set, original_close, original_dup = os.set_blocking, os.close, os.dup
    duplicates, changed, close_attempts, replacement = [], [], [], []
    def duplicate(fd):
        result = original_dup(fd)
        duplicates.append(result)
        return result
    acquisition = OSError('acquisition failure')
    def set_blocking(fd, blocking):
        if not blocking:
            changed.append(fd)
            if len(changed) == 2:
                raise acquisition
        original_set(fd, blocking)
    def close(fd):
        close_attempts.append(fd)
        original_close(fd)
        if fd == duplicates[0]:
            replacement.extend(os.pipe())
            assert replacement[0] == fd
            original_set(replacement[0], False)
            raise OSError(errno.EINTR, 'rollback close interrupted after release')
    async def run():
        monkeypatch.setattr(os, 'dup', duplicate)
        monkeypatch.setattr(os, 'set_blocking', set_blocking)
        monkeypatch.setattr(os, 'close', close)
        with pytest.raises(OSError) as caught:
            TerminalWriter(tuple(write for _, write in pipes))
        assert caught.value is acquisition
        assert not getattr(acquisition, 'retained_owners', ())
        assert all(os.get_blocking(write) for _, write in pipes)
        assert close_attempts == duplicates
        assert not os.get_blocking(replacement[0])
        os.write(replacement[1], b'replacement data')
        assert os.read(replacement[0], 4096) == b'replacement data'
    try:
        asyncio.run(run())
    finally:
        for fd in [*(fd for pipe in pipes for fd in pipe), *replacement]:
            original_close(fd)


def test_constructor_retains_only_flag_restoration_duty_before_close_attempt(monkeypatch):
    pipes = [os.pipe(), os.pipe()]
    original_set = os.set_blocking
    changed = []
    acquisition = OSError('acquisition failure')
    def set_blocking(fd, blocking):
        if not blocking:
            changed.append(fd)
            if len(changed) == 2:
                raise acquisition
        elif fd == changed[0]:
            raise OSError('flag restoration unavailable')
        original_set(fd, blocking)
    async def run():
        monkeypatch.setattr(os, 'set_blocking', set_blocking)
        with pytest.raises(OSError) as caught:
            TerminalWriter(tuple(write for _, write in pipes))
        assert caught.value is acquisition
        owner, = acquisition.retained_owners
        assert list(owner.owned.values()) == [changed[0]]
        assert owner.pending_flags
        monkeypatch.setattr(os, 'set_blocking', original_set)
        await owner.aclose()
        assert not owner.owned
        assert not owner.pending_flags
        assert all(os.get_blocking(write) for _, write in pipes)
    try:
        asyncio.run(run())
    finally:
        for read, write in pipes:
            os.close(read)
            os.close(write)


@pytest.mark.parametrize('phase', ['restore', 'close'])
def test_already_closed_owned_descriptor_settles_on_retry(monkeypatch, phase):
    read, write = os.pipe()
    original = os.close
    async def run():
        writer = TerminalWriter((write,))
        owned = writer.owned[write]
        if phase == 'restore':
            original(owned)
        else:
            def close(fd):
                original(fd)
                raise OSError('close status uncertain')
            monkeypatch.setattr(os, 'close', close)
        with pytest.raises(OSError):
            await writer.aclose()
        monkeypatch.setattr(os, 'close', original)
        await writer.aclose()
        assert not writer.owned
        assert not writer.pending_flags
    try:
        asyncio.run(run())
    finally:
        original(read)
        original(write)


@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('operation_error', [None, RuntimeError('operation'), asyncio.CancelledError('operation')])
def test_wrapper_restores_pipe_and_preserves_primary(monkeypatch, persistent, operation_error):
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read, write = os.pipe()
    original = os.set_blocking
    failures = []
    duplicates = []
    previous = RuntimeError('prior pending')
    if operation_error is not None:
        operation_error.cleanup_pending = previous
    def set_blocking(fd, blocking):
        if fd != write and blocking:
            duplicates.append(fd)
            if persistent or not failures:
                error = OSError('restore')
                failures.append(error)
                raise error
        original(fd, blocking)
    async def operation(*args, **kwargs):
        if operation_error is not None:
            raise operation_error
        return 0
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write))
    monkeypatch.setattr(terminal, '_run_local_terminal', operation)
    monkeypatch.setattr(os, 'set_blocking', set_blocking)
    try:
        with pytest.raises(BaseException) as raised:
            asyncio.run(terminal.run_local_terminal(None, None, diagnostic=lambda text: None))
        expected = operation_error if operation_error is not None else failures[0]
        assert raised.value is expected
        assert len(duplicates) == 2
        assert os.get_blocking(write) is not persistent
        if operation_error is not None:
            assert operation_error.cleanup_pending is previous
            assert operation_error.terminal_cleanup_error is failures[0]
        if persistent:
            receipt = previous.cleanup_pending if operation_error is not None else raised.value.cleanup_pending
            with pytest.raises(OSError) as retried:
                asyncio.run(receipt.retry())
            assert retried.value.cleanup_pending.owners == receipt.owners
            monkeypatch.setattr(os, 'set_blocking', original)
            asyncio.run(receipt.retry())
            assert os.get_blocking(write)
        with pytest.raises(OSError):
            os.fstat(duplicates[0])
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('persistent', [False, True])
def test_wrapper_cancellation_during_close_settles_before_loop_exit(monkeypatch, persistent):
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read, write = os.pipe()
    original = os.set_blocking
    entered = None
    release = None
    original_close = TerminalWriter.aclose
    cancellation = None
    async def delayed_close(self):
        entered.set()
        await release.wait()
        await original_close(self)
    def set_blocking(fd, blocking):
        if persistent and fd != write and blocking:
            raise OSError('restore')
        original(fd, blocking)
    async def operation(*args, **kwargs):
        return 0
    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(terminal.run_local_terminal(None, None, diagnostic=lambda text: None))
        await entered.wait()
        task.cancel('close cancellation')
        await asyncio.sleep(0)
        task.cancel('second cancellation')
        await asyncio.sleep(0)
        release.set()
        await task
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write))
    monkeypatch.setattr(terminal, '_run_local_terminal', operation)
    monkeypatch.setattr(TerminalWriter, 'aclose', delayed_close)
    monkeypatch.setattr(os, 'set_blocking', set_blocking)
    try:
        with pytest.raises(asyncio.CancelledError, match='close cancellation') as raised:
            asyncio.run(run())
        if persistent:
            receipt = raised.value.cleanup_pending
            monkeypatch.setattr(os, 'set_blocking', original)
            asyncio.run(receipt.retry())
        assert os.get_blocking(write)
    finally:
        os.close(read)
        os.close(write)


def test_descriptor_receipt_does_not_retain_attempted_close(monkeypatch):
    read, write = os.pipe()
    original_close = os.close
    async def run():
        writer = TerminalWriter((write,))
        await writer.aclose()
        writer.owned[write] = os.dup(write)
        writer.pending_flags[write] = True
        def close(fd):
            original_close(fd)
            raise OSError('closed')
        monkeypatch.setattr(os, 'close', close)
        with pytest.raises(OSError) as raised:
            await writer.retry_descriptors()
        assert not writer.owned
        assert not hasattr(raised.value, 'cleanup_pending')
        monkeypatch.setattr(os, 'close', original_close)
    try:
        asyncio.run(run())
    finally:
        original_close(read)
        original_close(write)


def test_wrapper_constructor_retains_cross_loop_restoration(monkeypatch):
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read, write = os.pipe()
    original = os.set_blocking
    acquisition = OSError('acquire')
    attempts = []
    def set_blocking(fd, blocking):
        if blocking:
            attempts.append(fd)
            raise OSError('restore')
        original(fd, blocking)
        raise acquisition
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write))
    monkeypatch.setattr(os, 'set_blocking', set_blocking)
    try:
        with pytest.raises(OSError) as raised:
            asyncio.run(terminal.run_local_terminal(None, None, diagnostic=lambda text: None))
        assert raised.value is acquisition
        assert len(attempts) == 2
        assert not os.get_blocking(write)
        monkeypatch.setattr(os, 'set_blocking', original)
        asyncio.run(acquisition.cleanup_pending.retry())
        assert os.get_blocking(write)
        with pytest.raises(OSError):
            os.fstat(attempts[0])
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('cyclic', [False, True])
def test_descriptor_receipt_repeated_error_has_bounded_ownership(monkeypatch, cyclic):
    read, write = os.pipe()
    original = os.set_blocking
    fault = OSError('persistent restore')
    previous = RuntimeError('existing cleanup')
    if cyclic:
        previous.cleanup_pending = previous
        fault.cleanup_pending = previous
    async def acquire():
        writer = TerminalWriter((write,))
        def blocking(fd, enabled):
            if enabled:
                raise fault
            original(fd, enabled)
        monkeypatch.setattr(os, 'set_blocking', blocking)
        with pytest.raises(OSError) as raised:
            await writer.finish()
        assert raised.value is fault
        return writer
    try:
        writer = asyncio.run(acquire())
        receipt = fault.terminal_cleanup_pending
        for _ in range(3):
            with pytest.raises(OSError) as raised:
                asyncio.run(receipt.retry())
            assert raised.value is fault
            assert fault.retained_owners == (writer,)
            assert fault.terminal_cleanup_pending is receipt
            assert not hasattr(receipt, 'cleanup_pending')
        if cyclic:
            assert fault.cleanup_pending is previous
            assert previous.cleanup_pending is previous
        else:
            assert fault.cleanup_pending is receipt
        monkeypatch.setattr(os, 'set_blocking', original)
        asyncio.run(receipt.retry())
        assert os.get_blocking(write)
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('site', ['_run', '_close', 'finish'])
@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('failed_operation', [False, True])
def test_task_allocation_failure_rolls_back_real_wrapper(monkeypatch, site, persistent, failed_operation):
    import inspect
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read, write = os.pipe()
    original_task = asyncio.create_task
    original_blocking = os.set_blocking
    original_dup = os.dup
    rejected = []
    duplicates = []
    owners = []
    scheduling = RuntimeError('task allocation failed')
    operation_error = RuntimeError('original operation')
    def create_task(coroutine, *args, **kwargs):
        if coroutine.cr_code.co_name == site:
            rejected.append(coroutine)
            raise scheduling
        return original_task(coroutine, *args, **kwargs)
    def dup(fd):
        result = original_dup(fd)
        duplicates.append(result)
        return result
    def blocking(fd, enabled):
        if persistent and enabled and fd in duplicates:
            raise OSError('restoration pending')
        original_blocking(fd, enabled)
    real_writer = TerminalWriter
    def writer(*args, **kwargs):
        owner = real_writer(*args, **kwargs)
        owners.append(owner)
        return owner
    async def operation(*args, **kwargs):
        owners[0].submit(write, 'payload')
        await asyncio.sleep(0)
        if failed_operation:
            raise operation_error
        return 0
    monkeypatch.setattr(terminal, 'TerminalWriter', writer)
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write))
    monkeypatch.setattr(terminal, '_run_local_terminal', operation)
    monkeypatch.setattr(asyncio, 'create_task', create_task)
    monkeypatch.setattr(os, 'dup', dup)
    monkeypatch.setattr(os, 'set_blocking', blocking)
    try:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(terminal.run_local_terminal(None, None, diagnostic=lambda text: None))
        expected = operation_error if failed_operation and site != '_run' else scheduling
        assert raised.value is expected
        assert rejected
        assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in rejected)
        assert all(owner.task.done() and not owner.workers for owner in owners)
        if expected is operation_error:
            assert expected.terminal_cleanup_error is scheduling
        if persistent:
            assert not os.get_blocking(write)
            monkeypatch.setattr(os, 'set_blocking', original_blocking)
            asyncio.run(expected.cleanup_pending.retry())
        assert os.get_blocking(write)
        with pytest.raises(OSError):
            os.fstat(duplicates[0])
    finally:
        original_blocking(write, True)
        os.close(read)
        os.close(write)


def test_destination_allocation_failure_quiesces_partial_workers(monkeypatch):
    import inspect
    original_task = asyncio.create_task
    rejected = []
    read1, write1 = os.pipe()
    read2, write2 = os.pipe()
    failure = RuntimeError('second destination')
    async def run():
        writer = TerminalWriter((write1, write2))
        allocations = 0
        def create_task(coroutine, *args, **kwargs):
            nonlocal allocations
            if coroutine.cr_code.co_name == '_drain':
                allocations += 1
                if allocations == 2:
                    rejected.append(coroutine)
                    raise failure
            return original_task(coroutine, *args, **kwargs)
        monkeypatch.setattr(asyncio, 'create_task', create_task)
        writer.submit(write1, 'one')
        writer.submit(write2, 'two')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await writer.finish()
        assert writer.task.exception() is failure
        assert not writer.workers
        assert not writer.owned
        assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
    try:
        asyncio.run(run())
        assert os.get_blocking(write1)
        assert os.get_blocking(write2)
    finally:
        for fd in (read1, write1, read2, write2):
            os.close(fd)


def test_eager_delivery_worker_can_finish_before_registration():
    read, write = os.pipe()
    async def run():
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        writer = TerminalWriter((write,))
        writer.submit(write, 'first')
        await asyncio.sleep(0)
        writer.submit(write, 'second')
        await writer.finish()
        assert not writer.workers
    try:
        asyncio.run(run())
        assert os.read(read, 64) == b'firstsecond'
    finally:
        os.close(read)
        os.close(write)


def test_simultaneous_cross_loop_receipts_cannot_touch_reused_descriptor(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    read, write = os.pipe()
    original_blocking = os.set_blocking
    original_close = os.close
    fault = OSError('restore')
    async def acquire():
        writer = TerminalWriter((write,))
        def fail(fd, enabled):
            raise fault
        monkeypatch.setattr(os, 'set_blocking', fail)
        with pytest.raises(OSError):
            await writer.finish()
        return writer
    writer = asyncio.run(acquire())
    owned = writer.owned[write]
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    reused = threading.Event()
    replacements = []
    calls = []
    guard = threading.Lock()
    def blocking(fd, enabled):
        with guard:
            calls.append(fd)
            first = len(calls) == 1
        if first:
            first_entered.set()
            assert release_first.wait(2)
        else:
            assert reused.wait(2)
        original_blocking(fd, enabled)
    def close(fd):
        original_close(fd)
        replacement = os.pipe()
        replacements.extend(replacement)
        assert owned in replacement
        original_blocking(owned, False)
        reused.set()
    def retry_second():
        second_started.set()
        asyncio.run(fault.cleanup_pending.retry())
    monkeypatch.setattr(os, 'set_blocking', blocking)
    monkeypatch.setattr(os, 'close', close)
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(asyncio.run, fault.cleanup_pending.retry())
            assert first_entered.wait(2)
            second = pool.submit(retry_second)
            assert second_started.wait(2)
            time.sleep(.03)
            release_first.set()
            first.result(timeout=3)
            second.result(timeout=3)
        assert calls == [owned]
        assert not os.get_blocking(owned)
        assert not writer.owned
    finally:
        release_first.set()
        reused.set()
        monkeypatch.setattr(os, 'close', original_close)
        for fd in replacements:
            original_close(fd)
        original_close(read)
        original_close(write)


@pytest.mark.parametrize('failed_operation', [False, True])
def test_all_cleanup_allocation_fails_only_after_blocked_worker_quiesces(monkeypatch, failed_operation):
    import inspect
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read, write = os.pipe()
    original_close = os.close
    rejected = []
    owners = []
    scheduling = RuntimeError('all task allocation unavailable')
    operation_error = RuntimeError('operation')
    original_writer = TerminalWriter
    def writer(*args, **kwargs):
        owner = original_writer(*args, **kwargs)
        owners.append(owner)
        return owner
    def reject(coroutine, *args, **kwargs):
        rejected.append(coroutine)
        raise scheduling
    def close(fd):
        assert owners[0].task.done()
        assert not owners[0].workers
        original_close(fd)
    async def operation(*args, **kwargs):
        while True:
            try:
                os.write(write, b'x' * 4096)
            except BlockingIOError:
                break
        owners[0].submit(write, 'blocked')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert owners[0].workers
        assert owners[0].active
        monkeypatch.setattr(asyncio, 'create_task', reject)
        monkeypatch.setattr(os, 'close', close)
        if failed_operation:
            raise operation_error
        return 0
    monkeypatch.setattr(terminal, 'TerminalWriter', writer)
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write))
    monkeypatch.setattr(terminal, '_run_local_terminal', operation)
    try:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(terminal.run_local_terminal(None, None, diagnostic=lambda text: None))
        assert raised.value is (operation_error if failed_operation else scheduling)
        assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in rejected)
        assert os.get_blocking(write)
        assert not owners[0].owned
    finally:
        monkeypatch.setattr(os, 'close', original_close)
        original_close(read)
        original_close(write)


def test_partial_worker_allocation_and_wrapper_rejection_clear_settled_workers(monkeypatch):
    from types import SimpleNamespace
    from api_agent.runtime import local_terminal as terminal
    read1, write1 = os.pipe()
    read2, write2 = os.pipe()
    owners = []
    original_task = asyncio.create_task
    original_close = os.close
    original_writer = TerminalWriter
    fault = RuntimeError('partial worker and cleanup allocation')
    allocations = 0
    def writer(*args, **kwargs):
        owner = original_writer(*args, **kwargs)
        owners.append(owner)
        return owner
    def create_task(coroutine, *args, **kwargs):
        nonlocal allocations
        if coroutine.cr_code.co_name == '_drain':
            allocations += 1
            if allocations == 2:
                raise fault
        if coroutine.cr_code.co_name == 'finish':
            raise fault
        return original_task(coroutine, *args, **kwargs)
    def close(fd):
        assert owners[0].task.done()
        assert not owners[0].workers
        original_close(fd)
    async def operation(*args, output, diagnostic, **kwargs):
        output('first')
        diagnostic('second')
        await asyncio.sleep(0)
        return 0
    monkeypatch.setattr(terminal, 'TerminalWriter', writer)
    monkeypatch.setattr(terminal, '_run_local_terminal', operation)
    monkeypatch.setattr(terminal.sys, 'stdout', SimpleNamespace(fileno=lambda: write1))
    monkeypatch.setattr(terminal.sys, 'stderr', SimpleNamespace(fileno=lambda: write2))
    monkeypatch.setattr(asyncio, 'create_task', create_task)
    monkeypatch.setattr(os, 'close', close)
    try:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(terminal.run_local_terminal(None, None))
        assert raised.value is fault
        assert not owners[0].owned
        assert not owners[0].workers
        assert os.get_blocking(write1)
        assert os.get_blocking(write2)
    finally:
        monkeypatch.setattr(os, 'close', original_close)
        for fd in (read1, write1, read2, write2):
            original_close(fd)


@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('cancel_close', [False, True])
def test_direct_finish_cancellation_settles_close_and_delivery(monkeypatch, persistent, cancel_close):
    read, write = os.pipe()
    original_blocking = os.set_blocking
    original_release = TerminalWriter._release_descriptors
    original_aclose = TerminalWriter.aclose
    primary = []
    releases = []
    restoration = OSError('restore pending')
    owner = None
    def release(self):
        assert self.task.done()
        assert not self.workers
        assert not self.active
        releases.append(True)
        return original_release(self)
    def blocking(fd, enabled):
        if persistent and enabled and fd != write:
            raise restoration
        original_blocking(fd, enabled)
    async def aclose(self):
        try:
            await original_aclose(self)
        except asyncio.CancelledError as error:
            primary.append(error)
            raise
    async def run():
        nonlocal owner
        owner = TerminalWriter((write,), drain_timeout=.03)
        while True:
            try:
                os.write(write, b'x' * 4096)
            except BlockingIOError:
                break
        owner.submit(write, 'blocked')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert owner.active
        finish = asyncio.create_task(owner.finish())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        finish.cancel('first cancellation')
        await asyncio.sleep(0)
        finish.cancel('second cancellation')
        if cancel_close:
            owner.close_task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await finish
        assert raised.value is primary[0]
        assert str(raised.value) == 'first cancellation'
        assert owner.close_task.done()
        assert owner.task.done()
        assert not owner.workers
        assert not owner.active
        return raised.value
    monkeypatch.setattr(TerminalWriter, '_release_descriptors', release)
    monkeypatch.setattr(TerminalWriter, 'aclose', aclose)
    monkeypatch.setattr(os, 'set_blocking', blocking)
    try:
        error = asyncio.run(run())
        assert len(releases) == 2
        if persistent:
            assert error.terminal_cleanup_error is restoration
            assert not os.get_blocking(write)
            monkeypatch.setattr(os, 'set_blocking', original_blocking)
            asyncio.run(error.cleanup_pending.retry())
        assert os.get_blocking(write)
        assert not owner.owned
    finally:
        original_blocking(write, True)
        os.close(read)
        os.close(write)


def test_direct_finish_delivers_healthy_pipe_without_cancellation():
    read, write = os.pipe()
    async def run():
        owner = TerminalWriter((write,))
        owner.submit(write, 'complete output')
        await owner.finish()
        assert owner.close_task.done()
        assert owner.task.done()
        assert not owner.workers
        assert not owner.owned
    try:
        asyncio.run(run())
        assert os.read(read, 64) == b'complete output'
        assert os.get_blocking(write)
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('pending_owner', ['delivery', 'close', 'worker'])
def test_descriptor_retry_rejects_unsettled_delivery_without_releasing_pipe(pending_owner):
    read, write = os.pipe()
    async def run():
        owner = TerminalWriter((write,))
        if pending_owner != 'delivery':
            await owner._settle_delivery()
            pending = asyncio.create_task(asyncio.Event().wait())
            if pending_owner == 'close':
                owner.close_task = pending
            else:
                owner.workers['pending'] = pending
        owned = owner.owned[write]
        with pytest.raises(RuntimeError, match='has not settled'):
            await owner.retry_descriptors()
        assert owner.owned[write] == owned
        assert not os.get_blocking(write)
        os.fstat(owned)
        if pending_owner != 'delivery':
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await owner.finish()
        assert not owner.owned
    try:
        asyncio.run(run())
        assert os.get_blocking(write)
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize('persistent', [False, True])
def test_direct_finish_allocation_failure_preserves_error_and_receipt(monkeypatch, persistent):
    read, write = os.pipe()
    original_task = asyncio.create_task
    original_blocking = os.set_blocking
    scheduling = RuntimeError('close allocation')
    restoration = OSError('restoration')
    def create_task(coroutine, *args, **kwargs):
        if coroutine.cr_code.co_name == '_close':
            raise scheduling
        return original_task(coroutine, *args, **kwargs)
    def blocking(fd, enabled):
        if persistent and enabled and fd != write:
            raise restoration
        original_blocking(fd, enabled)
    async def run():
        owner = TerminalWriter((write,))
        monkeypatch.setattr(asyncio, 'create_task', create_task)
        monkeypatch.setattr(os, 'set_blocking', blocking)
        with pytest.raises(RuntimeError) as raised:
            await owner.finish()
        assert raised.value is scheduling
        assert owner.task.done()
        assert not owner.workers
        return owner
    try:
        owner = asyncio.run(run())
        if persistent:
            monkeypatch.setattr(os, 'set_blocking', original_blocking)
            asyncio.run(scheduling.cleanup_pending.retry())
        assert os.get_blocking(write)
        assert not owner.owned
    finally:
        os.close(read)
        os.close(write)
