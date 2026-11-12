"""Actual SDK workers retain kernel identity through async pipe disposal."""
import asyncio
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import owned_worker
from api_agent.runtime import processes

pytestmark = [pytest.mark.unit_platform('linux', 'darwin'),
              pytest.mark.skipif(os.name != 'posix', reason='POSIX retained worker pipes')]


async def spawn(code):
    return await owned_worker.spawn_worker(sys.executable, '-c', code,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)


@pytest.mark.asyncio
async def test_async_pipes_preserve_output_and_reserve_exit_until_disposal():
    child = await spawn('import sys; print(sys.stdin.readline().strip(), flush=True)')
    try:
        child.stdin.write(b'private payload\n')
        await child.stdin.drain()
        assert await child.stdout.readline() == b'private payload\n'
        assert await asyncio.wait_for(child.wait(), 5) == 0
        assert child.returncode is None
        assert processes.observe_exit(child.process) == 0
    finally:
        await child.dispose()
    assert child.returncode == 0
    await child.stdin.wait_closed()
    assert not processes._group_running(child.pid)


@pytest.mark.asyncio
async def test_terminal_permission_refusal_is_proved_and_never_resignalled(monkeypatch):
    child = await spawn('pass')
    assert await asyncio.wait_for(child.wait(), 5) == 0
    calls = []
    def denied(pid, signum):
        calls.append((pid, signum))
        assert child.returncode is None
        assert processes.observe_exit(child.process) == 0
        raise PermissionError('controlled zombie-only refusal')
    monkeypatch.setattr(processes, 'os', SimpleNamespace(**(vars(os) | {'killpg': denied})))
    await child.dispose()
    assert len(calls) == 1
    assert child.returncode == 0
    await child.dispose()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_live_or_unknown_refusal_retains_owner_until_retry(monkeypatch, uncertain):
    child = await spawn('import sys; print("ready", flush=True); sys.stdin.read()')
    assert await asyncio.wait_for(child.stdout.readline(), 5) == b'ready\n'
    try:
        with monkeypatch.context() as failure:
            def denied(*args): raise PermissionError('controlled refusal')
            failure.setattr(processes, 'os', SimpleNamespace(**(vars(os) | {'killpg': denied})))
            if uncertain:
                def unknown(**kwargs): raise RuntimeError('inspection unavailable')
                failure.setattr(processes, '_process_snapshot', unknown)
            with pytest.raises((PermissionError, RuntimeError)):
                await child.dispose()
            assert child.returncode is None
            assert processes.observe_exit(child.process) is None
    finally:
        await child.dispose()
    assert child.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_reap_or_abandon_actual_child():
    child = await spawn('import sys; sys.stdin.read()')
    pending = asyncio.create_task(child.wait())
    await asyncio.sleep(.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError): await pending
    assert child.returncode is None
    await child.dispose()
    assert child.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_spawn_joins_blocking_launch_and_disposes_result(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    children = []
    def launch(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        entered.set()
        assert release.wait(5)
        return child
    monkeypatch.setattr(owned_worker, 'subprocess', SimpleNamespace(Popen=launch))
    pending = asyncio.create_task(spawn('import sys; sys.stdin.read()'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await pending
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_pipe_attachment_failure_disposes_the_reserved_worker(monkeypatch):
    children = []
    def launch(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child
    async def fail(*args, **kwargs): raise OSError('controlled pipe attachment failure')
    monkeypatch.setattr(owned_worker, 'subprocess', SimpleNamespace(Popen=launch))
    monkeypatch.setattr(asyncio.get_running_loop(), 'connect_write_pipe', fail)
    with pytest.raises(OSError, match='pipe attachment'):
        await spawn('import sys; sys.stdin.read()')
    assert children[0].returncode is not None
    assert children[0].stdin.closed and children[0].stdout.closed


@pytest.mark.asyncio
async def test_failed_spawn_preserves_launch_error(monkeypatch):
    def fail(*args, **kwargs): raise OSError('controlled spawn failure')
    monkeypatch.setattr(owned_worker, 'subprocess', SimpleNamespace(Popen=fail))
    with pytest.raises(OSError, match='spawn failure'):
        await spawn('pass')


@pytest.mark.asyncio
async def test_failed_attachment_cleanup_retains_explicit_retry_owner(monkeypatch):
    def denied(process): raise PermissionError('controlled disposal refusal')
    async def fail(*args, **kwargs): raise OSError('controlled attachment failure')
    with monkeypatch.context() as failure:
        failure.setattr(owned_worker, 'finalize', denied)
        failure.setattr(asyncio.get_running_loop(), 'connect_write_pipe', fail)
        with pytest.raises(OSError, match='attachment') as caught:
            await spawn('import sys; sys.stdin.read()')
    owner, = caught.value.retained_owners
    try:
        assert owner.returncode is None
        assert caught.value.cleanup_error.owners == (owner,)
    finally:
        await owner.dispose()
    assert owner.returncode is not None


@pytest.mark.asyncio
async def test_wait_does_not_block_event_loop_on_disposal_lock():
    child = await spawn('import sys; sys.stdin.read()')
    entered, release = threading.Event(), threading.Event()
    def hold():
        with processes._disposal_lock(child.process):
            entered.set()
            assert release.wait(5)
    holder = asyncio.get_running_loop().run_in_executor(None, hold)
    pending = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending = asyncio.create_task(child.wait())
        await asyncio.wait_for(asyncio.sleep(.02), .5)
        assert not pending.done()
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        release.set()
        await holder
        if pending is not None:
            with pytest.raises(asyncio.CancelledError): await pending
        await child.dispose()


@pytest.mark.asyncio
async def test_repeated_cancellation_finishes_finalization_before_return(monkeypatch):
    child = await spawn('import sys; sys.stdin.read()')
    entered, release = threading.Event(), threading.Event()
    original = owned_worker.finalize
    def gated(process):
        entered.set()
        assert release.wait(5)
        return original(process)
    monkeypatch.setattr(owned_worker, 'finalize', gated)
    pending = asyncio.create_task(child.dispose())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(.01)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await pending
    assert child.returncode is not None
    assert child.process.stdin.closed and child.process.stdout.closed
    assert all(protocol.closed.done() for _, protocol in child._connections)


@pytest.mark.asyncio
async def test_cancel_during_pipe_ack_wait_preserves_receipt_boundary():
    child = await spawn('import sys; sys.stdin.read()')
    gate = asyncio.get_running_loop().create_future()
    transport, _ = child._connections[0]
    entered = asyncio.Event()
    def close():
        transport.close()
        entered.set()
    child._connections[0] = (SimpleNamespace(close=close), SimpleNamespace(closed=gate))
    pending = asyncio.create_task(child.dispose())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(.02)
        assert not pending.done()
        pending.cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
    finally:
        gate.set_result(None)
    with pytest.raises(asyncio.CancelledError): await pending
    assert child.process.stdin.closed and child.process.stdout.closed


@pytest.mark.asyncio
async def test_cancelled_writer_close_waiter_cannot_revoke_disposal_ack():
    child = await spawn('import sys; sys.stdin.read()')
    waiter = asyncio.create_task(child.stdin.wait_closed())
    try:
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        writer_protocol = child._connections[1][1]
        assert not writer_protocol.closed.cancelled()
    finally:
        await child.dispose()
    assert child.returncode is not None
    assert writer_protocol.closed.done() and not writer_protocol.closed.cancelled()
    await child.stdin.wait_closed()
