"""Published pages and their retrieval bindings commit together."""
import asyncio
from pathlib import Path
import threading
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from api_agent.testing import allure_viewer as viewer
from api_agent.testing.allure_source import RunSource
from test_allure_viewer import Browser, async_test, cli, port, run, save


@pytest.fixture
def sources(tmp_path, run):
    manifest, files, _ = run
    left, right = tmp_path / 'left', tmp_path / 'right'
    save(left, manifest, files)
    save(right, manifest, files)
    return RunSource('sample', left), RunSource('sample', right)


@async_test
async def test_failed_index_refresh_keeps_the_old_source_and_page(tmp_path, sources, cli, port, monkeypatch):
    left, right = sources
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(left)
        index = Path(owner._temporary.name) / 'archive.html'
        before = index.read_bytes()
        routes, summaries = dict(owner._history_routes), dict(owner._summary_routes)
        original = Path.replace
        def reject(path, target):
            if Path(target) == index:
                raise OSError('Controlled replacement failure')
            return original(path, target)
        monkeypatch.setattr(Path, 'replace', reject)
        with pytest.raises(OSError, match='Controlled'):
            await owner.show_archive(right)
        assert index.read_bytes() == before
        assert owner._history_routes == routes and owner._summary_routes == summaries
        monkeypatch.setattr(right, 'load', Mock(side_effect=AssertionError('Failed source was used')))
        async with httpx.AsyncClient(trust_env=False, follow_redirects=True) as client:
            response = await client.get(f'http://127.0.0.1:{port}' + next(iter(routes)))
            assert response.status_code == 200
        right.load.assert_not_called()
    finally:
        await owner.aclose()


@pytest.mark.parametrize('previous', [False, True])
@async_test
async def test_missing_staging_without_a_committed_match_preserves_bindings(tmp_path, sources, run, cli, port, monkeypatch, previous):
    left, right = sources
    manifest, files, _ = run
    save(tmp_path / 'right', dict(manifest, run_id=str(uuid4())), files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner._ready()
        index = Path(owner._temporary.name) / 'archive.html'
        if previous:
            await owner.show_archive(left)
        before = index.read_bytes() if previous else None
        routes, summaries = dict(owner._history_routes), dict(owner._summary_routes)
        original = Path.replace
        def reject(path, target):
            if Path(target) == index:
                path.unlink()
                raise OSError('Staging disappeared without replacement')
            return original(path, target)
        monkeypatch.setattr(Path, 'replace', reject)
        with pytest.raises(OSError, match='Staging'):
            await owner.show_archive(right)
        assert (index.read_bytes() if index.exists() else None) == before
        assert owner._history_routes == routes and owner._summary_routes == summaries
    finally:
        await owner.aclose()


@pytest.mark.parametrize('interrupted', [False, True])
@async_test
async def test_completed_replacement_is_reconciled_before_reporting_interruption(tmp_path, sources, cli, port, monkeypatch, interrupted):
    left, right = sources
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(left)
        index = Path(owner._temporary.name) / 'archive.html'
        original = Path.replace
        def replaced(path, target):
            result = original(path, target)
            if Path(target) == index:
                if interrupted:
                    raise asyncio.CancelledError()
                raise OSError('Replacement completed before its error')
            return result
        monkeypatch.setattr(Path, 'replace', replaced)
        if interrupted:
            with pytest.raises(asyncio.CancelledError):
                await owner.show_archive(right)
        else:
            await owner.show_archive(right)
        assert all(binding[0] is right for binding in owner._history_routes.values())
        assert all(binding[0] is right for binding in owner._summary_routes.values())
        assert index.is_file()
    finally:
        await owner.aclose()


@async_test
async def test_cancellation_settles_commit_and_http_readers_observe_its_complete_binding(tmp_path, sources, cli, port, monkeypatch):
    left, right = sources
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    entered, reading = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    operation = request = None
    try:
        await owner.show_archive(left)
        index = Path(owner._temporary.name) / 'archive.html'
        original_replace, original_get = Path.replace, viewer._Handler.do_GET
        def blocked(path, target):
            if Path(target) == index:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(10)
            return original_replace(path, target)
        def observed_get(handler):
            if handler.path.startswith('/counts/'):
                loop.call_soon_threadsafe(reading.set)
            return original_get(handler)
        monkeypatch.setattr(Path, 'replace', blocked)
        monkeypatch.setattr(viewer._Handler, 'do_GET', observed_get)
        monkeypatch.setattr(left, 'load', Mock(side_effect=AssertionError('Reader crossed commit using the old source')))
        monkeypatch.setattr(right, 'load', Mock(wraps=right.load))
        operation = asyncio.create_task(owner.show_archive(right))
        await asyncio.wait_for(entered.wait(), 5)
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.create_task(client.get(f'http://127.0.0.1:{port}' + next(iter(owner._summary_routes))))
            await asyncio.wait_for(reading.wait(), 5)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done() and not request.done()
            assert all(binding[0] is left for binding in owner._history_routes.values())
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            response = await request
            assert response.status_code == 200 and response.json() == {'label': '1 / 1 passed'}
        left.load.assert_not_called()
        right.load.assert_called_once()
        assert all(binding[0] is right for binding in owner._history_routes.values())
    finally:
        release.set()
        await asyncio.gather(*(task for task in (operation, request) if task is not None), return_exceptions=True)
        await owner.aclose()
