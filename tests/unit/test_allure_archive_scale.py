"""Large windows keep a linear shared snapshot and cooperative cancellation."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading
import tracemalloc
from uuid import uuid4

import pytest

from api_agent.testing import allure_archive as archive
from api_agent.testing.allure_source import RunSource, ViewerError
from api_agent.testing.allure_viewer import ManagedAllureViewer
from test_allure_viewer import Browser, async_test, cli, port, run, save


@pytest.fixture
def manifests(run):
    manifest, _, _ = run
    return [dict(manifest, run_id=str(uuid4())) for _ in range(2000)]


def build(tmp_path, manifests, count, budget=None, stop=None):
    return archive.prepare(SimpleNamespace(repo='sample'), manifests, count,
        budget or SimpleNamespace(remaining=None), stop or threading.Event(), tmp_path / 'index.html')


def test_large_windows_share_linear_storage_and_keep_order(tmp_path, manifests):
    windows, routes, summaries = build(tmp_path, manifests, 2000)
    assert len(windows) == len(routes) == len(summaries) == 2000
    assert len({id(row.rows) for row in windows.values()}) == 1
    newest = windows[manifests[0]['run_id']]
    assert len(newest) == 1999
    assert list(newest) == manifests[1:]
    assert newest[-1] == manifests[-1]
    assert list(newest[:2]) == manifests[1:3]
    with pytest.raises(IndexError):
        newest[1999]
    following = next(binding[3] for binding in routes.values() if binding[1] == manifests[-1])
    assert list(following) == list(reversed(manifests[:-1]))


def test_large_count_does_not_allocate_quadratic_windows(tmp_path, manifests):
    def peak(count):
        tracemalloc.start()
        try:
            result = build(tmp_path, manifests, count)
            measured = tracemalloc.get_traced_memory()[1]
            assert len(result[0]) == len(manifests)
            return measured
        finally:
            tracemalloc.stop()
    small, large = peak(1), peak(10 ** 100)
    assert large < small * 2 + 1024 * 1024


@pytest.mark.parametrize('remaining', [0, -1])
def test_expired_archive_does_not_write_an_index(tmp_path, manifests, remaining):
    with pytest.raises(TimeoutError, match='timed out'):
        build(tmp_path, manifests, 2000, SimpleNamespace(remaining=remaining))
    assert not (tmp_path / 'index.html').exists()


def test_stopped_archive_does_not_write_an_index(tmp_path, manifests):
    stop = threading.Event()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        build(tmp_path, manifests, 2000, stop=stop)
    assert not (tmp_path / 'index.html').exists()


def test_expiry_during_construction_stops_before_traversing_the_archive(tmp_path, manifests, monkeypatch):
    validated = []
    original = archive.validate_manifest
    def observe(row, **kwargs):
        validated.append(row['run_id'])
        return original(row, **kwargs)
    class Budget:
        @property
        def remaining(self):
            return 0 if len(validated) > 10 else None
    monkeypatch.setattr(archive, 'validate_manifest', observe)
    with pytest.raises(TimeoutError):
        build(tmp_path, manifests, 2000, Budget())
    assert len(validated) < 20
    assert not (tmp_path / 'index.html').exists()


@pytest.mark.parametrize('limit', [1, 800])
def test_archive_html_is_byte_bounded_before_publication(tmp_path, run, monkeypatch, limit):
    monkeypatch.setattr(archive, 'MAX_ARTIFACT', limit)
    with pytest.raises(ViewerError, match='size limit'):
        build(tmp_path, [run[0]], 2000)
    assert not (tmp_path / 'index.html').exists()


def test_context_identity_includes_snapshot_and_count(tmp_path, run):
    first = [run[0]]
    _, routes, _ = build(tmp_path, first, 20)
    _, changed_count, _ = build(tmp_path, first, 25)
    _, added_run, _ = build(tmp_path, [dict(run[0], run_id=str(uuid4())), *first], 20)
    assert set(routes).isdisjoint(changed_count)
    assert set(routes).isdisjoint(added_run)


@async_test
async def test_archive_processing_is_off_loop_and_cancellation_keeps_published_state(tmp_path, run, cli, port, monkeypatch):
    manifest, files, _ = run
    save(tmp_path, manifest, files)
    owner = ManagedAllureViewer(port, browser_factory=Browser, history_count=2000)
    source = RunSource('sample', tmp_path)
    entered = asyncio.Event()
    release = threading.Event()
    stop_signal = []
    loop = asyncio.get_running_loop()
    original = archive.prepare
    def blocked(*args):
        assert threading.get_ident() != loop_thread
        stop_signal.append(args[4])
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(*args)
    loop_thread = threading.get_ident()
    try:
        await owner.show_archive(source)
        index = Path(owner._temporary.name) / 'archive.html'
        before = index.read_bytes()
        routes = dict(owner._history_routes)
        monkeypatch.setattr(archive, 'prepare', blocked)
        operation = asyncio.create_task(owner.show_archive(source))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
            assert stop_signal[0].is_set()
            assert index.read_bytes() == before and owner._history_routes == routes
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert index.read_bytes() == before and owner._history_routes == routes
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)
    finally:
        await owner.aclose()
