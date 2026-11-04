import asyncio

import pytest

from api_agent.providers import evidence_watchdog as watchdog
from api_agent.providers.contracts import ContractError


async def _sleeping_child():
    return await watchdog.spawn(
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
            async def dispose(self):
                pass
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
            import signal
            from api_agent.testing.processes import signal_owned_group
            signal_owned_group(process.process, signal.SIGKILL)
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


def test_exited_owned_leader_disposes_descendant_before_reaping(monkeypatch):
    import sys
    async def scenario():
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', 2)
        code = 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])'
        process = await watchdog.spawn(
            sys.executable, '-c', code, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        # Establish the intended case before starting the short watchdog: the
        # leader exited successfully while its child retains the output pipe.
        deadline = asyncio.get_running_loop().time() + 5
        from api_agent.testing.processes import observe_exit
        while observe_exit(process.process) is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        leader_exited = observe_exit(process.process) == 0
        import os
        signals = []
        original_signal = os.killpg
        def reserved_signal(pid, signum):
            assert pid == process.pid
            assert process.returncode is None
            assert os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            signals.append(signum)
            original_signal(pid, signum)
        monkeypatch.setattr(os, 'killpg', reserved_signal)
        assert await asyncio.wait_for(watchdog.communicate(process, b''), 2) == (b'', None)
        assert leader_exited
        assert signals
        assert process.returncode == 0
        assert process.process.stdout.closed and process.process.stdin.closed
    asyncio.run(scenario())


def test_darwin_exit_before_delayed_asyncio_reap_stays_within_deadline(monkeypatch):
    async def scenario():
        class Process:
            pid = 42
            returncode = None
            async def dispose(self):
                pass
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


def test_raw_async_handle_rejected_before_exchange_without_taking_ownership(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        import sys
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        original_kill = process.kill
        exchange = AsyncMock(side_effect=AssertionError('unowned exchange started'))
        kill = Mock(side_effect=AssertionError('unowned child signaled'))
        monkeypatch.setattr(process, 'communicate', exchange)
        monkeypatch.setattr(process, 'kill', kill)
        try:
            with pytest.raises(ContractError, match='ownership unavailable'):
                await watchdog.communicate(process, b'')
            exchange.assert_not_awaited()
            kill.assert_not_called()
            assert process.returncode is None
        finally:
            original_kill()
            await process.wait()
    asyncio.run(scenario())


def test_repeated_cancellation_finishes_group_signal_and_pipe_drain(monkeypatch):
    import threading
    from api_agent.testing import processes
    async def scenario():
        drain = asyncio.Event()
        entered = threading.Event()
        release = threading.Event()
        class Process:
            pid = 123
            returncode = None
            finished = False
            async def dispose(self):
                await asyncio.to_thread(signal_group, self)
            async def communicate(self, _payload):
                await drain.wait()
                self.returncode = 0
                self.finished = True
                return b'', None
        process = Process()
        def signal_group(*_args):
            entered.set()
            assert release.wait(5)
        task = asyncio.create_task(watchdog.communicate(process, b''))
        try:
            await asyncio.sleep(0)
            task.cancel()
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            release.set()
            # Group signaling and EOF are independent completion barriers.
            # Cancellation cannot return while the pipe reader is still owned.
            await asyncio.sleep(.05)
            assert not task.done()
            assert not process.finished
            drain.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert process.finished and process.returncode == 0
        finally:
            release.set()
            drain.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())



@pytest.mark.parametrize('invalid_dispose', [None, 42, lambda: None])
def test_watchdog_rejects_raw_reaped_async_handle_without_exchange_or_signal(monkeypatch, invalid_dispose):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        process = type('Raw', (), {})()
        process.pid, process.returncode = 123, 0
        process.dispose = invalid_dispose
        process.communicate = AsyncMock()
        signal = Mock(side_effect=AssertionError('reaped group was signaled'))
        monkeypatch.setattr(watchdog.os, 'killpg', signal)
        with pytest.raises(ContractError, match='ownership unavailable'):
            await watchdog.communicate(process, b'')
        process.communicate.assert_not_awaited()
        signal.assert_not_called()
    asyncio.run(scenario())


def test_darwin_watchdog_samples_actual_projector_pid(monkeypatch):
    async def scenario():
        process = await _sleeping_child()
        sampled = []
        def measurement(pid):
            sampled.append(pid)
            return watchdog._RSS_LIMIT + 1
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', measurement)
        with pytest.raises(ContractError, match='memory limit exceeded'):
            await watchdog.communicate(process, b'')
        assert sampled == [process.process.pid]
        assert process.process.returncode is not None
    asyncio.run(scenario())



def test_direct_owned_spawn_cancellation_retains_and_disposes_created_child(monkeypatch):
    import threading
    import sys
    import subprocess
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        created = []
        original = subprocess.Popen
        def delayed(argv, **options):
            process = original(argv, **options)
            created.append(process)
            entered.set()
            assert release.wait(5)
            return process
        monkeypatch.setattr(watchdog.subprocess, 'Popen', delayed)
        task = asyncio.create_task(watchdog.spawn(
            sys.executable, '-c', 'import time; time.sleep(30)',
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert created[0].returncode is None
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert created[0].returncode is not None
            assert created[0].stdin.closed and created[0].stdout.closed
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())



def test_watchdog_rejects_success_after_disposal_crosses_deadline(monkeypatch):
    async def scenario():
        class Process:
            returncode = 0
            disposed = False
            async def communicate(self, payload):
                return b'[]', None
            async def dispose(self):
                await asyncio.sleep(.08)
                self.disposed = True
        process = Process()
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .03)
        with pytest.raises(ContractError, match='timed out'):
            await watchdog.communicate(process, b'')
        assert process.disposed
    asyncio.run(scenario())


def test_exit_winning_resident_inspection_does_not_reject_owned_exchange(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        class Process:
            returncode = None
            measurements = 0
            disposed = False
            async def communicate(self, payload):
                await release.wait()
                self.returncode = 0
                return b'[]', None
            async def resident_bytes(self):
                self.measurements += 1
                if self.measurements == 2:
                    release.set()
                return None
            async def dispose(self):
                self.disposed = True
                release.set()
        process = Process()
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)

        assert await watchdog.communicate(process, b'') == (b'[]', None)

        assert process.measurements == 2 and process.disposed
    asyncio.run(scenario())


def test_resident_inspection_crossing_deadline_rejects_before_result_release(monkeypatch):
    from api_agent.providers import progress
    async def scenario():
        clock, release = [0.0], asyncio.Event()
        class Process:
            returncode = None
            disposed = False
            async def communicate(self, payload):
                await release.wait()
                self.returncode = 0
                return b'[]', None
            async def resident_bytes(self):
                clock[0] = 2.0
                return watchdog._RSS_LIMIT + 1
            async def dispose(self):
                self.disposed = True
                release.set()
        process = Process()
        monkeypatch.setattr(progress, '_clock', lambda: clock[0])
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', 1)

        with pytest.raises(ContractError, match='timed out'):
            await watchdog.communicate(process, b'')

        assert process.disposed and process.returncode == 0
    asyncio.run(scenario())


def test_shutdown_cancelling_all_tasks_retains_spawn_result_and_disposes_child(monkeypatch):
    import subprocess
    import sys
    import threading
    from api_agent.testing.processes import disposal_complete, finalize
    async def scenario():
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        children = []
        original = subprocess.Popen
        def gated(argv, **options):
            child = original(argv, **options)
            children.append(child)
            entered.set()
            assert release.wait(5)
            returned.set()
            return child
        monkeypatch.setattr(watchdog.subprocess, 'Popen', gated)
        spawning = asyncio.create_task(watchdog.spawn(
            sys.executable, '-c', 'import time; time.sleep(30)',
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True))
        cancelled = []
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            coordinator = asyncio.current_task()
            cancelled = [task for task in asyncio.all_tasks() if task is not coordinator]
            assert spawning in cancelled
            for task in cancelled:
                task.cancel()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*cancelled, return_exceptions=True), 10)
            assert await asyncio.to_thread(returned.wait, 5)

            assert isinstance(results[cancelled.index(spawning)], asyncio.CancelledError)
            assert children[0].returncode is not None
            assert disposal_complete(children[0])
            assert all(pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr))
        finally:
            release.set()
            await asyncio.gather(*cancelled, return_exceptions=True)
            await asyncio.gather(spawning, return_exceptions=True)
            for child in children:
                await asyncio.to_thread(finalize, child)
                for pipe in (child.stdin, child.stdout, child.stderr):
                    pipe.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failed', [False, True])
def test_shutdown_cancelling_all_tasks_waits_for_owned_pipe_thread(monkeypatch, failed):
    import threading
    from api_agent.testing import processes
    async def scenario():
        child = await _sleeping_child()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = processes.communicate_owned
        def gated(*args, **options):
            entered.set()
            assert release.wait(5)
            try:
                if failed:
                    raise BrokenPipeError('controlled late IO failure')
                return original(*args, **options)
            finally:
                finished.set()
        monkeypatch.setattr(processes, 'communicate_owned', gated)
        exchange = asyncio.create_task(watchdog.communicate(child, b''))
        cancelled = []
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            coordinator = asyncio.current_task()
            cancelled = [task for task in asyncio.all_tasks() if task is not coordinator]
            assert exchange in cancelled
            for task in cancelled:
                task.cancel()
            await asyncio.sleep(.05)

            assert not exchange.done() and not finished.is_set()
            assert not child.process.stdout.closed
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*cancelled, return_exceptions=True), 10)
            assert isinstance(results[cancelled.index(exchange)], asyncio.CancelledError)
            assert finished.is_set() and processes.disposal_complete(child.process)
            assert all(pipe.closed for pipe in (child.process.stdin, child.process.stdout))
        finally:
            release.set()
            await asyncio.gather(*cancelled, return_exceptions=True)
            await asyncio.gather(exchange, return_exceptions=True)
            await child.dispose()
    asyncio.run(scenario())
