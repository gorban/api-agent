"""Selected evidence imports native launch history without replacing its attempt."""
import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

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


@pytest.mark.parametrize('failure', ['discover', 'artifact', 'repository', 'deadline'])
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
    if failure == 'discover':
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
    finally:
        await owner.aclose()
