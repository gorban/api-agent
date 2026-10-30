import asyncio

import pytest

from api_agent.providers import evidence_watchdog as watchdog
from api_agent.providers.contracts import ContractError


async def _sleeping_child():
    return await asyncio.create_subprocess_exec(
        __import__('sys').executable, '-c', 'import time; time.sleep(30)',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)


def test_watchdog_deadline_kills_and_reaps_real_child(monkeypatch):
    async def scenario():
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .05)
        process = await _sleeping_child()
        with pytest.raises(ContractError, match='timed out'):
            await watchdog.communicate(process, b'')
        assert process.returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('measurement, error', [
    (lambda _pid: watchdog._RSS_LIMIT + 1, 'memory limit exceeded'),
    (lambda _pid: (_ for _ in ()).throw(OSError()), 'measurement unavailable'),
])
def test_darwin_watchdog_failure_kills_and_reaps_real_child(monkeypatch, measurement, error):
    async def scenario():
        process = await _sleeping_child()
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', measurement)
        with pytest.raises(ContractError, match=error):
            await watchdog.communicate(process, b'synthetic-secret')
        assert process.returncode is not None
    asyncio.run(scenario())


def test_cancellation_kills_and_reaps_real_child():
    async def scenario():
        process = await _sleeping_child()
        task = asyncio.create_task(watchdog.communicate(process, b''))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
    asyncio.run(scenario())


def test_darwin_worker_accepts_bounded_input_and_sets_cpu_limit(monkeypatch):
    import io
    import resource
    from types import SimpleNamespace
    from api_agent.providers import evidence_worker

    input_stream = io.BytesIO(b'{"rows":[],"secrets":[],"ids":[]}')
    output_stream = io.BytesIO()
    limits = []
    monkeypatch.setattr(evidence_worker.sys, 'stdin', SimpleNamespace(buffer=input_stream))
    monkeypatch.setattr(evidence_worker.sys, 'stdout', SimpleNamespace(buffer=output_stream))
    monkeypatch.setattr(evidence_worker.sys, 'platform', 'darwin')
    monkeypatch.setattr(resource, 'setrlimit', lambda kind, value: limits.append((kind, value)))

    assert evidence_worker.main() == 0
    assert resource.RLIMIT_CPU in [kind for kind, _ in limits]
    assert resource.RLIMIT_AS not in [kind for kind, _ in limits]
    assert output_stream.getvalue() == b'[]'


def test_completion_delivered_after_deadline_is_rejected(monkeypatch):
    from contextlib import asynccontextmanager

    async def scenario():
        class Budget:
            calls = 0
            @property
            def remaining(self):
                self.calls += 1
                return 1 if self.calls == 1 else 0
        @asynccontextmanager
        async def expired(*_args):
            yield Budget()
        class Process:
            returncode = 0
            async def communicate(self, _payload):
                return b'[]', None
        monkeypatch.setattr(watchdog, 'report_timeout', expired)
        with pytest.raises(ContractError, match='timed out'):
            await watchdog.communicate(Process(), b'')
    asyncio.run(scenario())


def test_darwin_measurement_race_with_child_exit_does_not_fail_capture(monkeypatch):
    async def scenario():
        process = await _sleeping_child()
        def exiting(_pid):
            import os
            process.kill()
            try:
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            except ChildProcessError:
                pass
            raise OSError()
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', exiting)
        output, _ = await watchdog.communicate(process, b'')
        assert output == b''
        assert process.returncode is not None
    asyncio.run(scenario())


def test_deadline_closes_pipe_held_by_descendant(monkeypatch):
    import sys
    async def scenario():
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .15)
        code = 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])'
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', code, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        with pytest.raises(ContractError, match='timed out'):
            await asyncio.wait_for(watchdog.communicate(process, b''), 2)
        assert process.returncode == 0
    asyncio.run(scenario())


def test_darwin_exit_before_delayed_asyncio_reap_stays_within_deadline(monkeypatch):
    async def scenario():
        class Process:
            pid = 42
            returncode = None
            async def communicate(self, _payload):
                await asyncio.sleep(.08)
                self.returncode = 0
                return b'[]', None
        checks = []
        def measurement(_pid):
            checks.append(True)
            raise OSError()
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', measurement)
        monkeypatch.setattr(watchdog, '_child_exited', lambda _pid: True)
        assert await watchdog.communicate(Process(), b'') == (b'[]', None)
        assert len(checks) > 1
    asyncio.run(scenario())


def test_child_exit_check_does_not_consume_status(monkeypatch):
    import os
    calls = []
    def waitid(kind, pid, flags):
        calls.append((kind, pid, flags))
        return object()
    monkeypatch.setattr(os, 'waitid', waitid)
    assert watchdog._child_exited(123)
    assert calls == [(os.P_PID, 123, os.WEXITED | os.WNOHANG | os.WNOWAIT)]


def test_cleanup_without_process_group_support_kills_child(monkeypatch):
    async def scenario():
        process = await _sleeping_child()
        monkeypatch.delattr(watchdog.os, 'killpg')
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .05)
        with pytest.raises(ContractError, match='timed out'):
            await watchdog.communicate(process, b'')
        assert process.returncode is not None
    asyncio.run(scenario())
