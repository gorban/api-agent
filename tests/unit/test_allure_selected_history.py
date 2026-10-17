"""Selected evidence imports native launch history without replacing its attempt."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import threading
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import allure_viewer as viewer
from api_agent.testing.allure_source import PreviewSnapshot, RunSource, ViewerError
from test_allure_viewer import (run, cli, history_cli, port, Browser, async_test,
                               save, history_runs, generated_root)


@pytest.fixture(params=[(False, 'passed'), (True, 'passed'), (False, 'failed'), (True, 'failed')],
                ids=['final-pass', 'live-pass', 'final-fail', 'live-fail'])
def selected(run, request):
    manifest, original, name = run
    preview, status = request.param
    manifest = dict(manifest, run_id=str(uuid4()), started_at='2026-01-25T00:00:00Z',
                    finished_at='2026-01-25T00:00:00Z')
    files = dict(original)
    row = json.loads(files[name])
    row['status'] = status
    files[name] = json.dumps(row).encode()
    other = dict(row)
    other['uuid'] = str(uuid4())
    other['name'] = 'unselected_test'
    other['fullName'] = 'tests/test_other.py::unselected_test'
    other['historyId'] = 'other-history'
    files[other['uuid'] + '-result.json'] = json.dumps(other).encode()
    manifest['files'] = {key: hashlib.sha256(body).hexdigest() for key, body in files.items()}
    if preview:
        del manifest['finished_at']
        manifest['preview_version'] = 1
    return PreviewSnapshot(files, manifest, {'selected': name}, preview), name


@async_test
async def test_selected_report_retains_native_history_without_other_current_tests(tmp_path, run, selected, history_cli, port):
    snapshot, name = selected
    before = dict(snapshot.files)
    rows = history_runs(tmp_path, run, ['2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z',
                                      '2026-01-05T00:00:00Z', '2026-01-26T00:00:00Z'],
                        modes=['integration', 'e2e', 'integration', 'integration'])
    # A published copy of this same run must not become its own predecessor.
    save(tmp_path, dict(snapshot.manifest, finished_at=snapshot.manifest['started_at']), snapshot.files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_preview(snapshot, name, source=RunSource('sample', tmp_path))
        observed = json.loads((generated_root(owner, location) / 'observed.json').read_bytes())
        assert observed['chain'] == ['integration — ' + rows[i]['finished_at'] for i in (0, 2)] + [
            'integration — ' + snapshot.manifest['started_at']]
        assert observed['results'] == 1
        assert '#testresult/' in location.url and snapshot.manifest['run_id'] in location.url
        assert location.history_error is None
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(location.archive_url)).status_code == 200
        assert snapshot.files == before
    finally:
        await owner.aclose()


@async_test
async def test_selected_history_uses_same_launch_window(tmp_path, run, selected, history_cli, port, monkeypatch):
    monkeypatch.setattr(viewer, 'HISTORY_LAUNCHES', 3)
    rows = history_runs(tmp_path, run, [f'2026-01-{day:02d}T01:00:00Z' for day in range(1, 5)])
    snapshot, name = selected
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_preview(snapshot, name, source=RunSource('sample', tmp_path))
        chain = json.loads((generated_root(owner, location) / 'history/history.json').read_bytes())
        assert chain == ['integration — ' + row['finished_at'] for row in rows[-2:]] + [
            'integration — ' + snapshot.manifest['started_at']]
        assert len(owner._bindings) == 3
    finally:
        await owner.aclose()


@pytest.mark.parametrize('failure', ['discover', 'artifact', 'repository', 'deadline', 'construction'])
@async_test
async def test_unavailable_prior_history_keeps_current_evidence(tmp_path, run, selected, cli, port, monkeypatch, failure):
    snapshot, name = selected
    manifest, files, _ = run
    directory = save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    def reject(*args, **kwargs):
        raise ViewerError('Controlled prior-history failure')
    async def expired(*args):
        raise TimeoutError()
    if failure == 'construction':
        def source():
            from api_agent.testing.storage import RunStorage
            return RunSource('sample', storage=RunStorage('bucket', prefix='bad//prefix'))
    elif failure == 'discover':
        monkeypatch.setattr(source, 'history', reject)
    elif failure == 'artifact':
        (directory / 'image.png').write_bytes(b'changed')
    elif failure == 'repository':
        source = RunSource('different', tmp_path)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    if failure == 'deadline':
        monkeypatch.setattr(owner, '_selected_history', expired)
    try:
        location = await owner.show_preview(snapshot, name, source=source)
        assert location.history_error == 'Prior Allure history is unavailable; showing this attempt without history.'
        assert location.archive_url is None
        assert snapshot.manifest['run_id'] in location.url and '#testresult/' in location.url
        assert not owner._operations and not owner._processes
    finally:
        await owner.aclose()


@async_test
async def test_cancelled_history_does_not_open_an_incomplete_selected_report(run, selected, cli, port, monkeypatch):
    snapshot, name = selected
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Future()
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    monkeypatch.setattr(owner, '_selected_history', blocked)
    task = asyncio.create_task(owner.show_preview(snapshot, name, source=RunSource('sample')))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._bindings and not owner._operations and not owner._processes
        assert owner._browser is None
    finally:
        await owner.aclose()


@async_test
async def test_first_selected_run_has_no_history_warning(tmp_path, selected, history_cli, port):
    snapshot, name = selected
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_preview(snapshot, name, source=RunSource('sample', tmp_path))
        observed = json.loads((generated_root(owner, location) / 'observed.json').read_bytes())
        assert observed['chain'] == ['integration — ' + snapshot.manifest['started_at']]
        assert observed['results'] == 1
        assert location.history_error is None
        assert location.archive_url is None
        assert 'buildUrl' not in observed['executor']
    finally:
        await owner.aclose()


@async_test
async def test_other_mode_still_exposes_nonempty_archive(tmp_path, run, selected, history_cli, port):
    history_runs(tmp_path, run, ['2026-01-01T00:00:00Z'], modes=['e2e'])
    snapshot, name = selected
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_preview(snapshot, name, source=RunSource('sample', tmp_path))
        observed = json.loads((generated_root(owner, location) / 'observed.json').read_bytes())
        assert observed['chain'] == ['integration — ' + snapshot.manifest['started_at']]
        assert location.archive_url == observed['executor']['buildUrl']
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(location.archive_url)).status_code == 200
    finally:
        await owner.aclose()


def blocked_history(monkeypatch):
    """Expire after real synchronous discovery enters, retaining its read thread."""
    loop = asyncio.get_running_loop()
    release = threading.Event()
    entered = asyncio.Event()
    scopes = []
    reads = []
    original = viewer.component_timeout
    @asynccontextmanager
    async def timeout(name, seconds):
        async with original(name, seconds) as budget:
            if name == 'Allure prior test history':
                async with asyncio.timeout(None) as scope:
                    scopes.append(scope)
                    yield budget
            else:
                yield budget
    def start():
        entered.set()
        scopes[-1].reschedule(loop.time())
    def history(*, budget):
        reads.append(budget)
        loop.call_soon_threadsafe(start)
        release.wait()
        return []
    monkeypatch.setattr(viewer, 'component_timeout', timeout)
    return SimpleNamespace(source=SimpleNamespace(repo='sample', history=history),
                           release=release, entered=entered, reads=reads)


@async_test
async def test_slow_history_does_not_consume_current_report_budget(selected, cli, port, monkeypatch):
    # Discovery cannot finish until this test releases it after rendering.
    # Current evidence must open while cleanup still owns the blocked read.
    with monkeypatch.context() as patch:
        blocked = blocked_history(patch)
        snapshot, name = selected
        owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
        try:
            location = await asyncio.wait_for(owner.show_preview(snapshot, name, source=blocked.source), 10)
            assert blocked.entered.is_set() and not blocked.release.is_set()
            assert owner._history_tasks and all(not task.done() for task in owner._history_tasks)
            assert '#testresult/' in location.url and location.history_error
            assert owner._browser.session.page.urls == [location.url]
        finally:
            blocked.release.set()
            await owner.aclose()
        assert not owner._history_tasks and owner._temporary is None


@async_test
async def test_history_cleanup_retains_retry_until_read_finishes(selected, cli, port, monkeypatch):
    with monkeypatch.context() as patch:
        blocked = blocked_history(patch)
        snapshot, name = selected
        owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
        try:
            await owner.show_preview(snapshot, name, source=blocked.source)
            original = viewer.report_timeout
            @asynccontextmanager
            async def exhausted(name, seconds):
                async with original(name, seconds):
                    yield SimpleNamespace(remaining=0)
            patch.setattr(viewer, 'report_timeout', exhausted)
            with pytest.raises(CleanupPendingError) as pending:
                await owner.aclose()
            assert pending.value.owners == (owner,) and owner._temporary is not None
            # Repeated cleanup must not interrupt offload's cancellation drainage.
            with pytest.raises(CleanupPendingError):
                await owner._drain_history(SimpleNamespace(remaining=0))
            patch.setattr(viewer, 'report_timeout', original)
            blocked.release.set()
            await pending.value.retry()
            assert not owner._history_tasks and owner._temporary is None
        finally:
            blocked.release.set()
            await owner.aclose()


@async_test
async def test_cleanup_receipt_is_not_an_optional_history_failure(selected, cli, port, monkeypatch):
    snapshot, name = selected
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    receipt = CleanupPendingError((owner,), owner.aclose)
    async def pending(*args):
        raise receipt
    monkeypatch.setattr(owner, '_selected_history', pending)
    try:
        with pytest.raises(CleanupPendingError) as caught:
            await owner.show_preview(snapshot, name, source=RunSource('sample'))
        assert caught.value is receipt and owner._browser is None
    finally:
        await owner.aclose()


@async_test
async def test_source_construction_failure_from_results_shortcut_keeps_current_report(tmp_path, selected, cli, port, monkeypatch):
    from test_tui import fixture
    snapshot, name = selected
    app = fixture(tmp_path, fields={
        'ALLURE_S3_BUCKET': {'type': 'str', 'default': 'reports'},
        'ALLURE_S3_PREFIX': {'type': 'str', 'default': 'bad//prefix'}})
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    app.allure_viewer = owner
    opened = []
    monkeypatch.setattr(app, 'show_allure_location', opened.append)
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_rows = {'selected': ('PASSED', 'selected', '1s')}
            app.show('test-results', 'Results', rows=list(app.unit_rows.items()))
            app.receive_allure_snapshot(snapshot, app._inspection_generation)
            await pilot.pause()
            await app.open_selected_allure()
            assert len(opened) == 1 and '#testresult/' in opened[0].url
            assert opened[0].history_error and opened[0].archive_url is None
    finally:
        await owner.aclose()


@async_test
async def test_archive_refresh_preserves_the_first_selected_route(tmp_path, run, selected, history_cli, port):
    snapshot, name = selected
    source = RunSource('sample', tmp_path)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        first = await owner.show_preview(snapshot, name, source=source)
        original = (generated_root(owner, first) / 'observed.json').read_bytes()
        history_runs(tmp_path, run, ['2026-01-01T00:00:00Z'], modes=['e2e'])
        refreshed = await owner.show_preview(snapshot, name, source=source)
        assert refreshed.url != first.url
        assert first.archive_url is None and refreshed.archive_url
        assert (generated_root(owner, first) / 'observed.json').read_bytes() == original
        observed = json.loads((generated_root(owner, refreshed) / 'observed.json').read_bytes())
        assert observed['executor']['buildUrl'] == refreshed.archive_url
    finally:
        await owner.aclose()


@async_test
async def test_repeated_selections_do_not_accumulate_blocked_history_reads(tmp_path, selected, cli, port, monkeypatch):
    with monkeypatch.context() as patch:
        blocked = blocked_history(patch)
        snapshot, name = selected
        other = next(key for key in snapshot.files if key.endswith('-result.json') and key != name)
        owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
        try:
            first = await owner.show_preview(snapshot, name, source=blocked.source)
            second = await owner.show_preview(snapshot, other, source=blocked.source)
            third = await owner.show_preview(snapshot, name, source=blocked.source)
            assert len(blocked.reads) == len(owner._history_tasks) == 1
            assert all(location.history_error for location in (first, second, third))
            assert first.url != second.url and third.url == first.url
            assert owner._browser.urls == [first.url, second.url, third.url]
            blocked.release.set()
            await owner._drain_history(SimpleNamespace(remaining=10))
            assert not owner._history_tasks
            retried = await owner.show_preview(snapshot, name, source=RunSource('sample', tmp_path))
            assert retried.history_error is None
        finally:
            blocked.release.set()
            await owner.aclose()
