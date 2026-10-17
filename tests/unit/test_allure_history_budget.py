"""Archive window deadlines grow with actual work; individual commands do not."""
import asyncio
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

import httpx
import pytest

from api_agent.providers.progress import component_timeout, progress_sink
from api_agent.testing import allure_viewer as viewer
from test_allure_viewer import Browser, async_test, cli, port, run, save, history_runs
from api_agent.testing.allure_source import RunSource, ViewerError


@pytest.mark.parametrize(('prior', 'later', 'batches'), [
    (0, 0, 1), (19, 0, 1), (20, 0, 2), (0, 20, 2),
    (19, 19, 2), (99, 99, 10), (199, 0, 10), (200, 0, 11),
])
def test_budget_uses_actual_reports_including_current(prior, later, batches):
    owner = viewer.ManagedAllureViewer(history_count=10 ** 100)
    assert owner._history_timeout(range(prior), range(later)) == 120 * batches


@async_test
async def test_direct_history_separates_preparation_from_the_admitted_generation_budget(tmp_path, run, cli, port, monkeypatch):
    history_runs(tmp_path, run, [f'2026-01-01T00:{minute:02d}:00Z' for minute in range(41)])
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser, history_count=100)
    observed, calls = [], []
    async def report(source, manifest, previous, budget):
        calls.append(len(previous))
        await asyncio.sleep(1.1)
        return f'http://127.0.0.1:{port}/archive.html'
    try:
        await owner._ready()
        owner.timeout = 1
        monkeypatch.setattr(owner, '_historical_report', report)
        with progress_sink(observed.append):
            async with component_timeout('Outer operation', 2.5):
                location = await owner.show_history(RunSource('sample', tmp_path))
        assert location.url.endswith('/archive.html') and calls == [40]
        preparation = next(row for row in observed if row.name == 'Allure history preparation')
        generation = next(row for row in observed if row.name == 'Allure history viewer')
        assert preparation.timeout == 1 and 1 < generation.timeout <= 2.5
        assert not owner._http_tasks and not owner._operations
    finally:
        await owner.aclose()


@async_test
async def test_large_archive_can_run_past_one_budget_and_http_waits_for_the_same_limit(tmp_path, run, cli, port, monkeypatch):
    manifest, files, _ = run
    save(tmp_path, manifest, files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser, history_count=100)
    limits, waits = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def report(*args):
        entered.set()
        await release.wait()
        return '/archive.html'
    original_submit = asyncio.run_coroutine_threadsafe
    def submit(coroutine, loop):
        future = original_submit(coroutine, loop)
        def result(*, timeout):
            waits.append(timeout)
            return future.result(timeout=timeout)
        return SimpleNamespace(result=result, cancel=future.cancel)
    @asynccontextmanager
    async def observed(name, seconds):
        limits.append((name, seconds))
        async with component_timeout(name, seconds) as budget:
            yield budget
    request = None
    try:
        await owner.show_archive(RunSource('sample', tmp_path))
        owner.timeout = .5
        path, binding = next(iter(owner._history_routes.items()))
        owner._history_routes[path] = (*binding[:2], range(99), range(99))
        monkeypatch.setattr(owner, '_historical_report', report)
        monkeypatch.setattr(viewer, 'component_timeout', observed)
        monkeypatch.setattr(viewer.asyncio, 'run_coroutine_threadsafe', submit)
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.create_task(client.get(f'http://127.0.0.1:{port}' + path))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(.6)
            assert not request.done()
            release.set()
            response = await request
        assert response.status_code == 302 and response.headers['location'] == '/archive.html'
        assert ('Allure archived run', 5) in limits and waits == [10]
        assert not owner._http_tasks
    finally:
        release.set()
        await asyncio.gather(*(task for task in (request,) if task), return_exceptions=True)
        await owner.aclose()


@async_test
async def test_stalled_archive_still_expires_and_drains(port, monkeypatch):
    owner = viewer.ManagedAllureViewer(port, timeout=.02, history_count=100)
    disposed = asyncio.Event()
    async def stalled(*args):
        try:
            await asyncio.Future()
        finally:
            disposed.set()
    monkeypatch.setattr(owner, '_historical_report', stalled)
    try:
        with pytest.raises(TimeoutError):
            await owner._history_report(None, None, range(20))
        assert disposed.is_set() and not owner._http_tasks and not owner._operations
    finally:
        await owner.aclose()


@async_test
async def test_history_window_does_not_extend_a_shorter_parent_deadline(port, monkeypatch):
    owner = viewer.ManagedAllureViewer(port, timeout=10, history_count=100)
    observed = []
    async def stalled(*args):
        await asyncio.Future()
    monkeypatch.setattr(owner, '_historical_report', stalled)
    try:
        with progress_sink(observed.append):
            with pytest.raises(TimeoutError):
                async with component_timeout('Outer operation', .02):
                    await owner._history_report(None, None, range(99), range(99))
        child = next(row for row in observed if row.name == 'Allure archived run')
        assert 0 < child.timeout <= .02
        assert not owner._http_tasks and not owner._operations
    finally:
        await owner.aclose()


@async_test
async def test_large_window_keeps_a_single_command_deadline_and_process_disposal(port):
    owner = viewer.ManagedAllureViewer(port, timeout=.04, history_count=100)
    observed = []
    try:
        with progress_sink(observed.append):
            with pytest.raises((TimeoutError, ViewerError)):
                await owner._command([sys.executable, '-c', 'import time; time.sleep(5)'])
        assert observed[0].timeout == .04
        assert not owner._processes
    finally:
        await owner.aclose()
