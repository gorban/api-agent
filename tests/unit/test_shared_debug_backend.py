"""Offline DEBUG lifetime checks against the actual setup transport."""
import asyncio
from concurrent.futures import Future
import json
import multiprocessing
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
import threading
from types import SimpleNamespace

import pytest

from api_agent.providers.sessions import InspectionOwner
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend


_DISPLAY_START_TIMEOUT = 10


def _display_number(process):
    deadline = time.monotonic() + _DISPLAY_START_TIMEOUT
    data = b''
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            pytest.fail('Isolated Xvfb display did not start within 10 seconds', pytrace=False)
        chunk = os.read(process.stdout.fileno(), 32 - len(data))
        if not chunk:
            pytest.fail('Isolated Xvfb display failed to start', pytrace=False)
        data += chunk
        if len(data) == 32 and b'\n' not in data:
            pytest.fail('Isolated Xvfb display failed to start', pytrace=False)
    number = data[:-1]
    if not number.isdigit() or process.poll() is not None:
        pytest.fail('Isolated Xvfb display failed to start', pytrace=False)
    return number.decode('ascii')


@pytest.fixture
def isolated_headed_display(monkeypatch):
    """Give real headed browser proofs a display owned by this test."""
    if sys.platform != 'linux':
        pytest.skip('Headed browser isolation requires a verified platform-specific display')
    executable = shutil.which('Xvfb')
    if not executable:
        pytest.fail('Headed browser proofs require Xvfb for an isolated display')
    try:
        process = subprocess.Popen([executable, '-displayfd', '1', '-nolisten', 'tcp'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        pytest.fail('Could not start isolated Xvfb display', pytrace=False)
    try:
        number = _display_number(process)
        monkeypatch.setenv('DISPLAY', ':' + number)
        monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
        monkeypatch.delenv('XAUTHORITY', raising=False)
        yield process, ':' + number
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Xvfb isolation is Linux-specific')
@pytest.mark.parametrize('payload,expected', [
    (b'123', 'did not start within'),
    (b'bad\n', 'failed to start'),
    (b'1' * 32, 'failed to start'),
    (b'', 'failed to start'),
])
def test_isolated_display_rejects_incomplete_or_invalid_startup(tmp_path, monkeypatch, payload, expected):
    import test_shared_debug_backend as module

    executable = tmp_path / 'Xvfb'
    executable.write_text(f'#!{sys.executable}\nimport os, time\nos.write(1, {payload!r})\n'
                          + ('time.sleep(5)\n' if payload else ''))
    executable.chmod(0o755)
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(executable))
    monkeypatch.setattr(module, '_DISPLAY_START_TIMEOUT', .2)
    started = []
    popen = subprocess.Popen
    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', start)
    fixture = isolated_headed_display.__wrapped__(monkeypatch)
    began = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=expected):
        next(fixture)
    assert time.monotonic() - began < 3
    assert len(started) == 1 and started[0].poll() is not None


@pytest.mark.skipif(sys.platform != 'linux', reason='Xvfb isolation is Linux-specific')
def test_isolated_display_reaps_process_after_startup_interrupt(tmp_path, monkeypatch):
    import test_shared_debug_backend as module

    executable = tmp_path / 'Xvfb'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(5)\n')
    executable.chmod(0o755)
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(executable))
    popen = subprocess.Popen
    started = []
    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', start)
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.select, 'select', interrupt)
    fixture = isolated_headed_display.__wrapped__(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        next(fixture)
    assert len(started) == 1 and started[0].poll() is not None


def test_pending_publication_future_is_drained_before_provider_disposal(tmp_path):
    backend = SetupBackend({}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    future = Future()
    backend._publication_future = future
    closed = []
    backend.registry = SimpleNamespace(aclose=lambda: _record_close(closed))

    async def drive():
        task = asyncio.create_task(backend._dispose())
        await asyncio.sleep(0)
        assert not task.done() and not closed
        future.set_exception(RuntimeError('publication interrupted'))
        await task

    async def _record_close(closed):
        closed.append('provider')

    asyncio.run(drive())
    assert closed == ['provider']


def test_publication_without_started_server_and_empty_inspection_waiters(tmp_path):
    backend = SetupBackend({}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    calls = []

    async def finish_execution():
        calls.append('quota')

    backend.registry = SimpleNamespace(finish_execution=finish_execution)

    async def drive():
        assert await backend._prepare_publication() is None
        await backend._dispose_inspection()
        await backend._cancel_inspection_wait()

    asyncio.run(drive())
    assert calls == ['quota'] and not backend._inspection_wait_tasks


def test_retained_finish_retries_after_inspection_lookup_fault(tmp_path, monkeypatch):
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    original = backend._inspector
    attempts = []

    def inspector():
        attempts.append('lookup')
        if len(attempts) == 1:
            raise RuntimeError('inspection lookup interrupted')
        return original()

    monkeypatch.setattr(backend, '_inspector', inspector)
    try:
        with pytest.raises(RuntimeError, match='lookup interrupted'):
            backend.finish_run()
        assert backend._disposed and not backend.run_complete
        assert backend.finish_run() == ()
        assert backend.closed and backend.run_complete
    finally:
        if not backend.closed:
            backend.close()


def test_monitor_suppresses_shutdown_race_and_tracks_partially_started_thread(tmp_path, monkeypatch):
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.inspection_open = True
    backend.closed = backend._stopping = True
    called = []
    monkeypatch.setattr(backend, 'wait_closed', lambda: (_ for _ in ()).throw(RuntimeError('closing')))
    backend.watch_inspection(called.append)
    backend._inspection_monitor.join(timeout=1)
    assert not backend._inspection_monitor.is_alive() and called == []

    partial = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    partial.inspection_open = True
    release = threading.Event()
    monkeypatch.setattr(partial, 'wait_closed', lambda: release.wait(timeout=2))
    original_start = threading.Thread.start

    def interrupted_start(thread):
        original_start(thread)
        if thread.name == 'api-agent-inspection-monitor':
            raise RuntimeError('start interrupted after thread creation')

    monkeypatch.setattr(threading.Thread, 'start', interrupted_start)
    try:
        with pytest.raises(RuntimeError, match='start interrupted'):
            partial.watch_inspection(lambda kind: None)
        assert partial._inspection_monitor.ident is not None
    finally:
        release.set()
        partial._inspection_monitor.join(timeout=2)
        assert not partial._inspection_monitor.is_alive()
        partial.close()


def test_closed_loop_receipts_do_not_claim_disposal(tmp_path):
    from api_agent.providers.control import CleanupPendingError
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.loop = asyncio.new_event_loop()
    backend.loop.close()
    backend.thread = threading.Thread(target=lambda: None)
    backend.thread.start()
    backend.thread.join(timeout=1)
    backend.inspection_open = True
    with pytest.raises(CleanupPendingError):
        backend.wait_closed(timeout=.01)
    with pytest.raises(CleanupPendingError):
        backend.close()
    assert not backend.closed and backend.pending
    backend._disposed = backend._stopping = True
    backend.close()
    assert backend.closed and not backend.pending


def test_retained_wait_without_inspector_still_closes_owner(tmp_path):
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend._start_thread()
    backend.inspection_open = True
    assert backend.wait_closed(timeout=1)
    assert backend.closed and backend.loop.is_closed()


def test_loop_closing_during_retained_wait_keeps_retry_receipt(tmp_path, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.cleanup_timeout = .05
    inspection = InspectionOwner()

    async def observe():
        inspection.observe(SimpleNamespace(
            inspection_endpoint='http://127.0.0.1:8133',
            browser=SimpleNamespace(is_connected=lambda: True),
            context=SimpleNamespace(pages=[object()])))

    async def release_registry():
        pass

    backend.registry = SimpleNamespace(
        _owner=SimpleNamespace(provider=SimpleNamespace(inspection=inspection)),
        aclose=release_registry)
    backend.inspection_open = True
    backend._start_thread()
    asyncio.run_coroutine_threadsafe(observe(), backend.loop).result(timeout=1)
    failures = []
    waiter = threading.Thread(target=lambda: _capture_wait_failure(backend, failures))

    def _capture_wait_failure(owner, output):
        try:
            owner.wait_closed(timeout=2)
        except BaseException as error:
            output.append(error)

    try:
        waiter.start()
        deadline = time.monotonic() + 2
        while not backend._inspection_wait_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        assert backend._inspection_wait_tasks
        with monkeypatch.context() as faults:
            faults.setattr(backend.loop, 'is_closed', lambda: True)
            waiter.join(timeout=1)
            assert not waiter.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], CleanupPendingError)
        assert backend.pending and not backend.closed
        backend.close()
        assert backend.closed and not backend._inspection_wait_tasks
    finally:
        if waiter.is_alive():
            waiter.join(timeout=2)
        if not backend.closed:
            backend.close()


def _worker(endpoint, barrier, output):
    from api_agent.providers.setup_transport import SetupClient
    client = SetupClient(endpoint, timeout=10)
    try:
        barrier.wait(timeout=10)
        client.ensure_ready()
        output.put('ready')
    finally:
        client.close()


def test_parallel_debug_workers_share_backend_and_retain_original_inspection(monkeypatch, tmp_path):
    created = []
    closed = []

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()
            created.append(self)

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8123',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=[object()])))
            await asyncio.sleep(.05)

        async def _close(self):
            closed.append('auth')

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(2)
    output = context.Queue()
    try:
        endpoint = backend.start()
        workers = [context.Process(target=_worker, args=(endpoint, barrier, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        assert [output.get(timeout=2) for _ in workers] == ['ready', 'ready']
        assert len(created) == 1
        assert backend.finish_run() == ('http://127.0.0.1:8123',)
        assert closed == ['auth'] and backend.inspection_open and not backend.closed
        assert backend.end_inspection()
        assert backend.closed and not backend.thread.is_alive()
    finally:
        backend.close()
        output.close()
        output.join_thread()


def test_retained_backend_cleanup_retry_keeps_original_owner(monkeypatch, tmp_path):
    resource = SimpleNamespace(attempts=0)

    async def release():
        resource.attempts += 1
        if resource.attempts == 1:
            raise RuntimeError('synthetic disposal failure')

    resource.aclose = release

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.retain([(resource, 'aclose')])
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8124',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=[object()])))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    from api_agent.providers.setup_transport import SetupClient
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        assert backend.inspection_open and not backend.closed
        assert backend.end_inspection() is False
        assert backend.pending and backend.thread.is_alive()
        assert resource.attempts == 1
        assert backend.end_inspection() is True
        assert resource.attempts == 2 and backend.closed
    finally:
        if not backend.closed:
            backend.close()


def test_wait_closed_restarts_stopped_backend_loop_and_drains_timeout(monkeypatch, tmp_path):
    window = SimpleNamespace(open=True)

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8125',
                    browser=SimpleNamespace(is_connected=lambda: window.open),
                    context=SimpleNamespace(pages=[object()])))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    from api_agent.providers.setup_transport import SetupClient
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        backend.loop.call_soon_threadsafe(backend.loop.stop)
        backend.thread.join(timeout=5)
        assert not backend.thread.is_alive()
        assert backend.wait_closed(timeout=.02) is False
        assert not backend._inspection_wait_tasks and backend.thread.is_alive()
        window.open = False
        assert backend.wait_closed(timeout=2) is True
        assert backend.closed and not backend._inspection_wait_tasks
    finally:
        if not backend.closed:
            backend.close()


def test_wait_closed_recovers_loop_stopped_during_the_wait(monkeypatch, tmp_path):
    pages = [object()]

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8131',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=pages)))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.cleanup_timeout = .05
    from api_agent.providers.setup_transport import SetupClient
    answers = []
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        waiter = threading.Thread(target=lambda: answers.append(backend.wait_closed(timeout=2)))
        waiter.start()
        deadline = time.monotonic() + 2
        while not backend._inspection_wait_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        assert backend._inspection_wait_tasks
        backend.loop.call_soon_threadsafe(backend.loop.stop)
        backend.thread.join(timeout=2)
        pages.clear()
        waiter.join(timeout=2)
        assert not waiter.is_alive() and answers == [True]
        assert backend.closed and not backend._inspection_wait_tasks
    finally:
        if not backend.closed:
            backend.close()


def test_unconfirmed_timed_wait_retains_cleanup_receipt(monkeypatch, tmp_path):
    from api_agent.providers.control import CleanupPendingError
    release = threading.Event()

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8132',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=[object()])))
                async def slow_wait():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await asyncio.to_thread(release.wait)
                self.inspection.wait_closed = slow_wait

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.cleanup_timeout = .05
    from api_agent.providers.setup_transport import SetupClient
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        with pytest.raises(CleanupPendingError) as caught:
            backend.wait_closed(timeout=.01)
        assert caught.value.inspections == (backend,)
        assert backend.pending and not backend.closed
        release.set()
        deadline = time.monotonic() + 2
        while backend._inspection_wait_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        assert not backend._inspection_wait_tasks
        backend.close()
        assert backend.closed
    finally:
        release.set()
        if not backend.closed:
            backend.close()


def test_natural_window_close_disposes_backend_and_reports_completion(monkeypatch, tmp_path):
    pages = [object()]

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8126',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=pages)))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    from api_agent.providers.setup_transport import SetupClient
    events = []
    def fail_callback(kind):
        events.append(kind)
        raise RuntimeError('synthetic event sink failure')
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        backend.watch_inspection(fail_callback)
        assert backend.inspection_open and not backend.closed
        pages.clear()
        backend._inspection_monitor.join(timeout=3)
        assert not backend._inspection_monitor.is_alive()
        assert events == ['inspection_closed'] and backend.closed
    finally:
        if not backend.closed:
            backend.close()


def test_parallel_timed_waiters_cannot_hide_monitor_wait_from_shutdown(monkeypatch, tmp_path):
    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8128',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=[object()])))
                async def slow_wait():
                    await asyncio.sleep(10)
                self.inspection.wait_closed = slow_wait

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    from api_agent.providers.setup_transport import SetupClient
    events = []
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        backend.watch_inspection(events.append)
        deadline = time.monotonic() + 2
        while not backend._inspection_wait_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(backend._inspection_wait_tasks) == 1
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(backend.wait_closed(timeout=.03)))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert answers == [False, False]
        assert len(backend._inspection_wait_tasks) == 1
        assert backend.end_inspection()
        backend._inspection_monitor.join(timeout=2)
        assert backend.closed and not backend._inspection_monitor.is_alive()
        assert not backend._inspection_wait_tasks and backend.loop.is_closed()
    finally:
        if not backend.closed:
            backend.close()


def test_monitor_start_failure_keeps_cleanup_retryable(monkeypatch, tmp_path):
    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8129',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=[object()])))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    from api_agent.providers.setup_transport import SetupClient
    original_start = threading.Thread.start

    def fail_monitor(thread):
        if thread.name == 'api-agent-inspection-monitor':
            raise RuntimeError('synthetic monitor startup failure')
        return original_start(thread)

    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        monkeypatch.setattr(threading.Thread, 'start', fail_monitor)
        with pytest.raises(RuntimeError, match='monitor startup'):
            backend.watch_inspection(lambda kind: None)
        assert backend._inspection_monitor is None and backend.inspection_open
        assert backend.end_inspection() and backend.closed
        assert backend.end_inspection()
    finally:
        if not backend.closed:
            backend.close()


def test_monitor_callback_timeout_retains_ack_until_retry(monkeypatch, tmp_path):
    pages = [object()]
    entered = threading.Event()
    release = threading.Event()

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8130',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=pages)))

        async def _close(self):
            pass

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.cleanup_timeout = .05
    from api_agent.providers.setup_transport import SetupClient
    try:
        client = SetupClient(backend.start())
        client.ensure_ready()
        client.close()
        backend.finish_run()
        def callback(kind):
            entered.set()
            release.wait(timeout=2)
        backend.watch_inspection(callback)
        pages.clear()
        assert entered.wait(timeout=2)
        assert backend.closed and backend._inspection_monitor.is_alive()
        assert not backend.end_inspection() and backend.pending
        release.set()
        backend._inspection_monitor.join(timeout=2)
        assert backend.end_inspection()
        assert backend.closed and not backend.pending
    finally:
        release.set()
        if not backend.closed or backend.pending:
            backend.close()


def test_recorded_remote_fixture_publishes_before_backend_inspection_opens(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from api_agent.telemetry.usage import QuotaSnapshot
    from api_agent.testing.collection import collect_tests
    from api_agent.testing import recorded, setup_backend

    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_remote.py').write_text(
        'def test_remote(llm_provider):\n    llm_provider.ensure_ready()\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,))
    config = {'DEBUG': True, 'TUI_MODE': 'integration', 'TEST_PARALLEL_JOBS': 2,
        'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': tmp_path / 'allure'}
    order = []
    pages = [object()]
    events = []

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()
            self._quota_observer = options['quota_observer']
            self._quota_before = self._quota_after = False

        async def _capture_quota(self, phase):
            if getattr(self, '_quota_' + phase):
                return
            setattr(self, '_quota_' + phase, True)
            instant = datetime(2028, 1, 1, tzinfo=timezone.utc)
            snapshot = QuotaSnapshot(10 if phase == 'before' else 12,
                'window', instant + timedelta(hours=1), instant)
            self._quota_observer(phase, snapshot)
            order.append('quota-' + phase)

        async def _ensure(self):
            if not self.inspection.sessions:
                self.inspection.observe(SimpleNamespace(
                    inspection_endpoint='http://127.0.0.1:8127',
                    browser=SimpleNamespace(is_connected=lambda: True),
                    context=SimpleNamespace(pages=pages)))
            await self._capture_quota('before')

        async def _finish_execution(self):
            await self._capture_quota('after')

        async def _close(self):
            pass

    class Backend(setup_backend.SetupBackend):
        def finish_run(self):
            order.append('finish_run')
            return super().finish_run()

    class Storage:
        def persist(self, directory, manifest):
            assert manifest['status'] == 'completed'
            summary = json.loads((directory / 'usage-summary.json').read_text())
            accounting = json.loads((directory / 'accounting.json').read_text())
            assert summary['before']['pct_used'] == 10
            assert summary['after']['pct_used'] == 12
            assert summary['pct_point_delta'] == 2
            assert accounting['quota']['after']['pct_used'] == 12
            order.append('published')

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    monkeypatch.setattr(setup_backend, 'SetupBackend', Backend)
    selected = collect_tests(context, 'integration', config=config)
    result = recorded.run_recorded_integration(context, selected, config,
        accepted=True, storage=Storage(), on_event=events.append)
    try:
        assert result.persisted and result.execution.tests[0].status == 'passed'
        assert order == ['quota-before', 'quota-after', 'published', 'finish_run']
        assert len(result.execution.inspections) == 1
        owner = result.execution.inspections[0]
        assert owner.inspection_open
        pages.clear()
        owner._inspection_monitor.join(timeout=3)
        assert owner.closed and any(event.kind == 'inspection_closed' for event in events)
    finally:
        for inspection in result.execution.inspections:
            inspection.end_inspection()


def test_original_controlled_browser_survives_parallel_workers_and_client_disconnect(monkeypatch, tmp_path, isolated_headed_display):
    display_process, display = isolated_headed_display
    assert display_process.poll() is None and os.environ['DISPLAY'] == display
    assert 'WAYLAND_DISPLAY' not in os.environ
    from api_agent.runtime.browser import _default_paths
    if not all(path.exists() for path in _default_paths(True)):
        pytest.fail('Headed browser proofs require installed Playwright Chromium')
    from playwright.async_api import async_playwright
    from api_agent.runtime.managed_browser import managed_browser

    created = []
    marker = 'shared-debug-original-page'

    class Provider:
        def __init__(self, config, root, **options):
            self.inspection = InspectionOwner()
            self.browser = managed_browser(inspection=self.inspection, headless=False)
            created.append(self)

        async def _ensure(self):
            session = await self.browser.start()
            await session.page.set_content(f'<main id="marker">{marker}</main>')

        async def _close(self):
            await self.browser.aclose()

    async def inspect(endpoint):
        playwright = await async_playwright().start()
        client = await playwright.chromium.connect_over_cdp(endpoint)
        try:
            pages = [page for context in client.contexts for page in context.pages]
            matches = [page for page in pages if await page.locator('#marker').text_content() == marker]
            assert len(matches) == 1
            return matches[0].url
        finally:
            await client.close()
            await playwright.stop()

    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(2)
    output = context.Queue()
    try:
        endpoint = backend.start()
        workers = [context.Process(target=_worker, args=(endpoint, barrier, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        assert [output.get(timeout=2) for _ in workers] == ['ready', 'ready']
        assert len(created) == 1
        retained = backend.finish_run()
        assert len(retained) == 1 and backend.inspection_open
        assert asyncio.run(inspect(retained[0])) == 'about:blank'
        assert asyncio.run(inspect(retained[0])) == 'about:blank'
        backend.loop.call_soon_threadsafe(backend.loop.stop)
        backend.thread.join(timeout=5)
        assert not backend.thread.is_alive() and not backend.closed
        assert backend.end_inspection()
        assert not backend._inspection_wait_tasks
    finally:
        if not backend.closed:
            backend.close()
        output.close()
        output.join_thread()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux process ancestry proof')
def test_abrupt_launcher_exit_reaps_its_controlled_backend_browser(tmp_path, isolated_headed_display):
    display_process, display = isolated_headed_display
    assert display_process.poll() is None and os.environ['DISPLAY'] == display
    assert 'WAYLAND_DISPLAY' not in os.environ
    from api_agent.runtime.browser import _default_paths
    if not all(path.exists() for path in _default_paths(True)):
        pytest.fail('Headed browser proofs require installed Playwright Chromium')
    script = '''
import asyncio, json, os, time
from pathlib import Path
from types import SimpleNamespace
from api_agent.providers.sessions import InspectionOwner
from api_agent.providers.setup_transport import SetupClient
from api_agent.runtime.managed_browser import managed_browser
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend

def descendants(pid):
    try:
        children = sorted({int(value) for task in Path(f'/proc/{pid}/task').iterdir()
            for value in (task / 'children').read_text().split()})
    except OSError:
        return []
    return children + [child for ident in children for child in descendants(ident)]

class Provider:
    def __init__(self, config, root, **options):
        self.inspection = InspectionOwner()
        self.browser = managed_browser(inspection=self.inspection, headless=False)
    async def _ensure(self):
        await (await self.browser.start()).page.set_content('<main>offline launcher proof</main>')
    async def _close(self):
        await self.browser.aclose()

provider_setup.IntegrationLLM = Provider
backend = SetupBackend({'DEBUG': True}, '.', password_manager_registry=SimpleNamespace(definitions=()))
client = SetupClient(backend.start())
client.ensure_ready()
client.close()
assert backend.finish_run() and backend.inspection_open
print(json.dumps({'pid': os.getpid(), 'children': descendants(os.getpid())}), flush=True)
time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-c', script], cwd=tmp_path,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    children = []
    try:
        readable, _, _ = select.select([process.stdout], [], [], 20)
        assert readable, process.stderr.read() if process.poll() is not None else 'Backend startup timed out'
        row = json.loads(process.stdout.readline())
        assert row['pid'] == process.pid and row['children']
        children = row['children']
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = {}
            for pid in children:
                try:
                    states[pid] = Path(f'/proc/{pid}/stat').read_text().split()[2]
                except OSError:
                    states[pid] = 'gone'
            if all(state in {'gone', 'Z'} for state in states.values()):
                break
            time.sleep(.05)
        assert all(state in {'gone', 'Z'} for state in states.values()), states
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_managed_browser_long_temp_keeps_profile_in_bulk_and_cleans_child(tmp_path, monkeypatch, isolated_headed_display):
    from pathlib import Path
    from api_agent.runtime.managed_browser import ChromiumDriver
    display_process, display = isolated_headed_display
    assert display_process.poll() is None and os.environ['DISPLAY'] == display
    bulk = tmp_path / ('browser-bulk-' + 'x' * 100)
    bulk.mkdir()
    monkeypatch.setenv('TMPDIR', str(bulk))
    async def run():
        driver = ChromiumDriver(inspectable=True)
        browser = await driver.launch(headless=False)
        process_temp = Path(driver.process_temp.name)
        try:
            session = await browser.new_browser_cdp_session()
            try:
                arguments = (await session.send('Browser.getBrowserCommandLine'))['arguments']
                profile = next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--user-data-dir='))
                assert Path(profile).is_relative_to(bulk)
                assert process_temp.is_dir() and process_temp.stat().st_mode & 0o777 == 0o700
                assert os.environ['TMPDIR'] == str(bulk)
            finally:
                await session.detach()
        finally:
            await browser.close()
            await driver.stop()
        assert not process_temp.exists() and driver.process_temp is None
    asyncio.run(run())


@pytest.mark.parametrize('closed', [False, True])
@pytest.mark.parametrize('observer_raises', [False, True])
def test_inspection_monitor_error_reports_disposition_and_acknowledges_closed(
        tmp_path, monkeypatch, closed, observer_raises):
    backend = SetupBackend({'DEBUG': True}, tmp_path,
        password_manager_registry=SimpleNamespace(definitions=()))
    backend.inspection_open = True
    entered, release = threading.Event(), threading.Event()
    events, acknowledgements = [], []
    backend._when_closed(lambda: acknowledgements.append('released'))

    def wait_closed():
        entered.set()
        if not release.wait(timeout=3):
            raise AssertionError('monitor barrier timed out')
        raise RuntimeError('synthetic inspection wait failure')

    def observe(kind):
        events.append(kind)
        if observer_raises:
            raise RuntimeError('synthetic observer failure')

    monkeypatch.setattr(backend, 'wait_closed', wait_closed)
    backend.watch_inspection(observe)
    try:
        assert entered.wait(timeout=3)
        with backend._lock:
            backend.closed = closed
        release.set()
        backend._inspection_monitor.join(timeout=3)
        assert not backend._inspection_monitor.is_alive()
        assert events == ['inspection_closed' if closed else 'inspection_cleanup_pending']
        assert acknowledgements == (['released'] if closed else [])
        if not closed:
            assert len(backend._cleanup_callbacks) == 1
            with backend._lock:
                backend.closed = True
                backend._acknowledge_closed()
            assert acknowledgements == ['released']
    finally:
        release.set()
        backend._inspection_monitor.join(timeout=3)
        assert not backend._inspection_monitor.is_alive()
