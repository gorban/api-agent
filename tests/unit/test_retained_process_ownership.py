"""Group signals retain an owned child identity until the final signal."""
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing import processes

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX child reservation')


def test_exit_observation_retains_child_until_group_disposal():
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    try:
        assert processes.wait_owned(child, timeout=5) == 0
        assert child.returncode is None
        assert os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        processes.finalize(child)
        assert child.returncode == 0
        assert processes.disposal_complete(child)
    finally:
        processes.finalize(child)


def test_final_group_signal_precedes_leader_reap(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    signals = []
    def signal_reserved(pid, signum):
        assert pid == child.pid
        assert child.returncode is None
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        signals.append(signum)
    monkeypatch.setattr(processes.os, 'killpg', signal_reserved)
    try:
        processes.wait_owned(child, timeout=5)
        processes.finalize(child)
        processes.finalize(child)
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert child.returncode == 0
    finally:
        processes.finalize(child)


@pytest.mark.parametrize('live', [True, False])
def test_reaped_handle_never_authorizes_signal_to_reused_numeric_group(monkeypatch, live):
    child = SimpleNamespace(pid=123, returncode=0)
    signals = Mock(side_effect=AssertionError('released group must never be signalled'))
    monkeypatch.setattr(processes.os, 'killpg', signals)
    monkeypatch.setattr(processes, '_group_running', lambda _pid: live)
    if live:
        with pytest.raises(RuntimeError, match='unreserved'):
            processes.signal_owned_group(child, signal.SIGKILL)
    else:
        assert processes.signal_owned_group(child, signal.SIGKILL)
    signals.assert_not_called()


@pytest.mark.parametrize('text', [True, False])
def test_owned_exchange_writes_input_and_drains_both_streams(text):
    child = subprocess.Popen([sys.executable, '-c',
        'import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(b"diagnostic")'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=text, start_new_session=True)
    payload = 'owned input' if text else b'owned input'
    try:
        assert processes.communicate_owned(child, payload, timeout=5) == (
            payload, 'diagnostic' if text else b'diagnostic')
        assert child.returncode == 0
        assert processes.disposal_complete(child)
        assert all(getattr(child, name).closed for name in ('stdin', 'stdout', 'stderr'))
    finally:
        processes.finalize(child)


def test_lost_child_reservation_cannot_authorize_group_signal(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    _, status = os.waitpid(child.pid, 0)
    signals = Mock(side_effect=AssertionError('unreserved PID signalled'))
    monkeypatch.setattr(processes.os, 'killpg', signals)
    try:
        with pytest.raises(RuntimeError, match='identity is no longer reserved'):
            processes.signal_owned_group(child, signal.SIGKILL)
        signals.assert_not_called()
    finally:
        child.returncode = os.waitstatus_to_exitcode(status)


def test_exit_status_delivery_race_uses_confirmed_handle_status(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    def reap_then_deliver(*args):
        child.wait(timeout=5)
        raise ChildProcessError()
    monkeypatch.setattr(processes.os, 'waitid', reap_then_deliver)
    assert processes.observe_exit(child) == 0
    assert child.returncode == 0


def test_wait_observation_timeout_preserves_owned_child():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            processes.wait_owned(child, timeout=0)
        assert child.returncode is None
    finally:
        processes.finalize(child)
    assert processes.disposal_complete(child)


def test_inspection_uses_reserved_live_worker_and_skips_disposed_handle():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    inspector = Mock(return_value=42)
    try:
        assert processes.inspect_owned(child, inspector) == 42
        inspector.assert_called_once_with(child.pid)
    finally:
        processes.finalize(child)
    assert processes.inspect_owned(child, inspector) is None
    inspector.assert_called_once()


def test_reserved_zombie_permission_refusal_can_confirm_disposal(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    processes.wait_owned(child, timeout=5)
    monkeypatch.setattr(processes.os, 'killpg', Mock(side_effect=PermissionError('zombie only')))
    try:
        assert processes.signal_owned_group(child, signal.SIGKILL)
        processes.finalize(child)
        assert child.returncode == 0
    finally:
        processes.finalize(child)


def test_exiting_group_refusal_is_confirmed_once_it_becomes_terminal(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    processes.wait_owned(child, timeout=5)
    proof = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(processes.os, 'killpg', Mock(side_effect=PermissionError('exiting')))
    monkeypatch.setattr(processes, '_owned_group_terminal', proof)
    try:
        assert processes.signal_owned_group(child, signal.SIGKILL)
        assert proof.call_count == 3
    finally:
        monkeypatch.undo()
        processes.finalize(child)
    assert child.returncode == 0


def test_group_refusal_that_never_becomes_terminal_is_not_confirmed(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    refusal = PermissionError('live group')
    monkeypatch.setattr(processes, '_TERMINAL_SETTLE_SECONDS', .05)
    monkeypatch.setattr(processes.os, 'killpg', Mock(side_effect=refusal))
    monkeypatch.setattr(processes, '_owned_group_terminal', Mock(return_value=False))
    try:
        with pytest.raises(PermissionError) as caught:
            processes.signal_owned_group(child, signal.SIGKILL)
        assert caught.value is refusal
        assert not getattr(child, '_api_agent_group_disposal_complete', False)
    finally:
        monkeypatch.undo()
        processes.finalize(child)


@pytest.mark.parametrize('payload', [None, b'bytes'])
def test_external_process_adapter_retains_communicate_contract(payload):
    adapter = SimpleNamespace(communicate=Mock(return_value=(b'output', None)))
    assert processes.communicate_owned(adapter, payload, timeout=1) == (b'output', None)
    assert adapter.communicate.call_args.kwargs == {'timeout': 1}


def test_owned_exchange_closes_unused_input_and_accepts_uncaptured_streams():
    child = subprocess.Popen([sys.executable, '-c', 'import sys; assert sys.stdin.read() == ""'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    assert processes.communicate_owned(child, timeout=5) == (None, None)
    assert child.returncode == 0 and child.stdin.closed


@pytest.mark.parametrize('text,expected', [(True, 'a\nb\nc\n'), (False, b'a\r\nb\rc\n')])
def test_owned_exchange_preserves_universal_newlines_and_binary_bytes(text, expected):
    child = subprocess.Popen([sys.executable, '-c', 'import os; os.write(1,b"a\\r\\nb\\rc\\n")'],
        stdout=subprocess.PIPE, text=text, start_new_session=True)
    assert processes.communicate_owned(child, timeout=5) == (expected, None)


def test_owned_exchange_handles_partial_input_writes_and_closed_child_stdin():
    child = subprocess.Popen([sys.executable, '-c', 'import os,time; os.close(0); os.write(1,b"closed"); time.sleep(.1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    assert processes.communicate_owned(child, b'x' * (1024 * 1024), timeout=5) == (b'closed', None)
    assert child.returncode == 0 and child.stdin.closed


def test_owned_exchange_sweeps_buffered_output_when_selector_misses_readiness(monkeypatch):
    original = processes.selectors.DefaultSelector
    child = subprocess.Popen([sys.executable, '-c', 'import os; os.write(1,b"retained-output")'],
        stdout=subprocess.PIPE, start_new_session=True)
    class QuietAfterDisposal:
        def __init__(self): self.inner = original()
        def __getattr__(self, name): return getattr(self.inner, name)
        def select(self, timeout):
            return [] if processes.disposal_complete(child) else self.inner.select(timeout)
    monkeypatch.setattr(processes.selectors, 'DefaultSelector', QuietAfterDisposal)
    processes.wait_owned(child, timeout=5)
    assert processes.communicate_owned(child, timeout=5) == (b'retained-output', None)
    assert child.returncode == 0


@pytest.mark.parametrize('operation', ['signal', 'inspect'])
def test_async_child_watcher_cannot_grant_reserved_process_authority(monkeypatch, operation):
    import asyncio
    async def scenario():
        child = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(30)', start_new_session=True)
        signals = Mock(side_effect=AssertionError('watcher-owned PID signalled'))
        inspection = Mock(side_effect=AssertionError('watcher-owned PID inspected'))
        monkeypatch.setattr(processes.os, 'killpg', signals)
        try:
            with pytest.raises(RuntimeError, match='watcher cannot reserve'):
                if operation == 'signal':
                    processes.signal_owned_group(child, signal.SIGKILL)
                else:
                    processes.inspect_owned(child, inspection)
            signals.assert_not_called()
            inspection.assert_not_called()
            assert child.returncode is None
        finally:
            child.kill()
            await child.wait()
        assert child.returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('captured', [False, True])
def test_exchange_rejects_deadline_crossed_only_by_final_disposal(monkeypatch, captured):
    child = subprocess.Popen([sys.executable, '-c', 'import os; os.close(1); os.close(2)'],
        stdout=subprocess.PIPE if captured else subprocess.DEVNULL,
        stderr=subprocess.PIPE if captured else subprocess.DEVNULL, start_new_session=True)
    processes.wait_owned(child, timeout=5)
    assert child.returncode is None
    ticks = [0.0]
    original_finalize = processes.finalize
    def slow_disposal(process, **kwargs):
        result = original_finalize(process, **kwargs)
        ticks[0] = .08
        return result
    monkeypatch.setattr(processes, 'time', SimpleNamespace(monotonic=lambda: ticks[0], sleep=lambda _: None))
    monkeypatch.setattr(processes, 'finalize', slow_disposal)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            processes.communicate_owned(child, timeout=.03)
        assert child.returncode == 0
        assert processes.disposal_complete(child)
        assert all(pipe is None or pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
    finally:
        original_finalize(child)
