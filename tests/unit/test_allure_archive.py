"""Complete archive navigation and verified, attachment-free count requests."""
import asyncio
from io import BytesIO
import json
import re
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.testing import allure_viewer as viewer
from api_agent.testing.allure_source import RunSource, ViewerError
from api_agent.testing.storage import RunStorage
from test_allure_viewer import (run, cli, port, Browser, async_test, save,
                               history_runs, generated_root)


@async_test
async def test_archive_is_the_only_main_entry_and_counts_do_not_read_images(tmp_path, run, cli, port):
    manifest, files, _ = run
    directory = save(tmp_path, manifest, files)
    (directory / 'image.png').unlink()
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_archive(RunSource('sample', tmp_path))
        assert location.url.endswith('/archive.html') and location.archive_url is None
        assert not owner._bindings
        async with httpx.AsyncClient(trust_env=False) as client:
            archive = await client.get(location.url)
            assert 'including the most recent 20' in archive.text and 'History links to earlier' in archive.text
            counts = re.search(r'data-summary="([^"]+)"', archive.text)[1]
            response = await client.get(f'http://127.0.0.1:{port}'+counts)
            assert response.json() == {'label': '1 / 1 passed'}
            path = next(iter(owner._history_routes))
            assert (await client.get(f'http://127.0.0.1:{port}'+path)).status_code == 503
            assert (await client.get(f'http://127.0.0.1:{port}'+counts, headers={'Host': 'foreign'})).status_code == 403
        assert not owner._http_tasks
    finally:
        await owner.aclose()


@async_test
async def test_empty_archive_is_explicitly_unavailable(cli, port):
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        with pytest.raises(ViewerError, match='No finalized'):
            await owner.show_archive(RunSource('sample'))
    finally:
        await owner.aclose()


@async_test
async def test_archive_refresh_rebinds_counts_to_secondary_source(tmp_path, run, cli, port):
    manifest, files, name = run
    save(tmp_path / 'primary', manifest, files)
    directory = save(tmp_path / 'secondary', manifest, files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(RunSource('sample', tmp_path / 'primary'))
        route = next(iter(owner._summary_routes))
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f'http://127.0.0.1:{port}'+route)).status_code == 200
            (directory / name).write_bytes(b'changed private source')
            await owner.show_archive(RunSource('sample', tmp_path / 'secondary'))
            response = await client.get(f'http://127.0.0.1:{port}'+route)
            assert response.status_code == 503 and 'changed private source' not in response.text
    finally:
        await owner.aclose()


def test_s3_count_loading_fetches_only_manifest_and_result_json(run):
    manifest, files, name = run
    reads, streams = [], []
    def get_object(**kwargs):
        key = kwargs['Key'].rsplit('/', 1)[-1]
        reads.append(key)
        stream = BytesIO(json.dumps(manifest).encode() if key == 'manifest.json' else files[key])
        streams.append(stream)
        return {'Body': stream}
    storage = RunStorage('reports')
    storage._client = lambda: SimpleNamespace(get_object=get_object)
    result = RunSource('sample', storage=storage).load(manifest, results_only=True)
    assert result == {name: files[name]}
    assert reads == ['manifest.json', name] and all(stream.closed for stream in streams)


@async_test
async def test_count_request_cancellation_drains_before_viewer_disposal(run, cli, port):
    manifest, files, _ = run
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    def load(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return {name: body for name, body in files.items() if name.endswith('-result.json')}
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    await owner._ready()
    request = asyncio.create_task(owner._counts_report(SimpleNamespace(load=load), manifest))
    try:
        await entered.wait()
        cleanup = asyncio.create_task(owner.aclose())
        done, _ = await asyncio.wait((cleanup,), timeout=0)
        assert not done and owner._temporary is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await cleanup
        assert owner._temporary is None and not owner._http_tasks
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await owner.aclose()


@pytest.fixture
def trend_cli(cli):
    cli.write_text(cli.read_text() + '''
 (output/'history').mkdir()
 (output/'widgets').mkdir()
 executor=json.loads((source/'executor.json').read_text())
 for name in ('history-trend.json','duration-trend.json','retry-trend.json','categories-trend.json'):
  path=source/'history'/name
  previous=json.loads(path.read_bytes()) if path.exists() else []
  entry={'buildOrder':executor['buildOrder'],'reportUrl':executor['reportUrl'],
    'reportName':executor['reportName'],'data':{'passed':1}}
  body=json.dumps([entry,*previous][:20])
  (output/'history'/name).write_text(body)
  (output/'widgets'/name).write_text(body)
''')
    return cli


@async_test
async def test_archive_surrounding_window_keeps_selected_results_and_history_exact(tmp_path, run, trend_cli, port):
    from datetime import datetime, timedelta, timezone
    dates = [(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)).isoformat() for n in range(42)]
    rows = history_runs(tmp_path, run, dates)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(RunSource('sample', tmp_path))
        binding = next(value for value in owner._history_routes.values() if value[1]['run_id'] == rows[20]['run_id'])
        assert [row['run_id'] for row in binding[2]] == [row['run_id'] for row in reversed(rows[1:20])]
        assert [row['run_id'] for row in binding[3]] == [row['run_id'] for row in rows[21:40]]
        url = await owner._history_report(*binding)
        root = generated_root(owner, SimpleNamespace(url=url))
        past = json.loads((root / 'history/history-trend.json').read_bytes())
        surrounding = json.loads((root / 'widgets/history-trend.json').read_bytes())
        assert len(past) == 20 and len(surrounding) == 39
        assert surrounding[19] == past[0]
        assert len(list((root / 'data/test-cases').glob('*.json'))) == 1
        original = (root / 'widgets/history-trend.json').read_bytes()
        assert await owner._history_report(*binding) == url
        assert (root / 'widgets/history-trend.json').read_bytes() == original
    finally:
        await owner.aclose()


@pytest.mark.parametrize('count', [1, 3, 25])
@async_test
async def test_configured_window_reaches_both_sides_beyond_native_cap(tmp_path, run, trend_cli, port, count):
    from datetime import datetime, timedelta, timezone
    dates = [(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)).isoformat() for n in range(2 * count + 1)]
    rows = history_runs(tmp_path, run, dates)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser, history_count=count)
    try:
        await owner.show_archive(RunSource('sample', tmp_path))
        binding = next(value for value in owner._history_routes.values() if value[1]['run_id'] == rows[count]['run_id'])
        assert len(binding[2]) == len(binding[3]) == count - 1
        url = await owner._history_report(*binding)
        root = generated_root(owner, SimpleNamespace(url=url))
        past = json.loads((root / 'history/history-trend.json').read_bytes())
        surrounding = json.loads((root / 'widgets/history-trend.json').read_bytes())
        assert len(past) == count and len(surrounding) == 2 * count - 1
        assert surrounding[count - 1] == past[0]
        assert len(list((root / 'data/test-cases').glob('*.json'))) == 1
        async with httpx.AsyncClient(trust_env=False) as client:
            archive = await client.get(f'http://127.0.0.1:{port}/archive.html')
            assert f'up to {count - 1} earlier and {count - 1} later' in archive.text
    finally:
        await owner.aclose()


@async_test
async def test_e2e_archive_reports_first_and_confirmed_final_fractions(tmp_path, run, cli, port):
    import hashlib
    manifest, _, _ = run
    files = {}
    for family, statuses in [('recovered', ['failed', 'passed', 'passed']), ('first_pass', ['passed'])]:
        for attempt, status in enumerate(statuses, 1):
            uid = str(uuid4())
            files[uid + '-result.json'] = json.dumps({'uuid': uid, 'status': status,
                'fullName': family, 'historyId': family, 'parameters': [
                    {'name': 'attempt', 'value': str(attempt), 'excluded': True}]}).encode()
    manifest = dict(manifest, mode='e2e', metadata={'e2e_reruns': 2},
                    files={name: hashlib.sha256(body).hexdigest() for name, body in files.items()})
    save(tmp_path, manifest, files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(RunSource('sample', tmp_path))
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f'http://127.0.0.1:{port}'+next(iter(owner._summary_routes)))
            assert response.json() == {'label': 'first 1 / 2 (50.0%) — final 2 / 2 (100.0%)'}
    finally:
        await owner.aclose()


@async_test
async def test_later_upload_creates_new_surrounding_route_without_changing_old_report(tmp_path, run, trend_cli, port):
    rows = history_runs(tmp_path, run, [f'2026-01-{day:02d}T01:00:00Z' for day in range(1, 6)])
    source = RunSource('sample', tmp_path)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        await owner.show_archive(source)
        old_binding = next(value for value in owner._history_routes.values() if value[1]['run_id'] == rows[2]['run_id'])
        old_url = await owner._history_report(*old_binding)
        old_root = generated_root(owner, SimpleNamespace(url=old_url))
        before = (old_root / 'widgets/history-trend.json').read_bytes()
        history_runs(tmp_path, run, ['2026-01-06T01:00:00Z'])
        await owner.show_archive(source)
        new_binding = next(value for value in reversed(list(owner._history_routes.values())) if value[1]['run_id'] == rows[2]['run_id'])
        new_url = await owner._history_report(*new_binding)
        assert new_url != old_url
        assert (old_root / 'widgets/history-trend.json').read_bytes() == before
        assert await owner._history_report(*old_binding) == old_url
    finally:
        await owner.aclose()


@async_test
async def test_interrupted_completed_publish_can_be_recovered_without_overwriting(tmp_path, run, trend_cli, port, monkeypatch):
    rows = history_runs(tmp_path, run, ['2026-01-01T01:00:00Z', '2026-01-02T01:00:00Z'])
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    original = viewer.shutil.move
    published = []
    def interrupt(source, destination):
        value = original(source, destination)
        if '/surrounding-' in str(destination):
            published.append(destination)
            raise asyncio.CancelledError()
        return value
    try:
        await owner.show_archive(RunSource('sample', tmp_path))
        binding = next(value for value in owner._history_routes.values() if value[1]['run_id'] == rows[0]['run_id'])
        monkeypatch.setattr(viewer.shutil, 'move', interrupt)
        with pytest.raises(asyncio.CancelledError):
            await owner._history_report(*binding)
        assert published and (published[0] / 'index.html').is_file()
        before = {str(path.relative_to(published[0])): path.read_bytes() for path in published[0].rglob('*') if path.is_file()}
        monkeypatch.setattr(viewer.shutil, 'move', original)
        url = await owner._history_report(*binding)
        after = {str(path.relative_to(published[0])): path.read_bytes() for path in published[0].rglob('*') if path.is_file()}
        assert before == after and 'surrounding-' in url
    finally:
        await owner.aclose()


@pytest.fixture(params=['retry', 'suite'], ids=['failed-launch-passing-retry', 'suite-failure'])
def failed_recorded_launch(tmp_path, request):
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.recording import RunIdentity, RunRecorder
    identity = RunIdentity.create('sample', 'integration')
    recorder = RunRecorder(tmp_path / 'sample' / identity.run_id, identity)
    test = TestIdentity('sample::test.py::one', 'sample', 'test.py', 'test.py::one')
    if request.param == 'retry':
        recorder.record_test(TestResult(test, 'failed'), attempt=1)
        recorder.record_test(TestResult(test, 'passed'), attempt=2)
        label = '1 / 1 passed'
    else:
        recorder.record_test(TestResult(test, 'passed'))
        setup = TestIdentity('sample::__suite__::setup', 'sample', '__suite__', '__suite__::setup')
        recorder.record_test(TestResult(setup, 'error'))
        label = '1 / 2 passed'
    manifest = recorder.finalize()
    assert manifest['status'] == 'failed'
    return manifest, label


@async_test
async def test_archive_keeps_run_failure_visible_alongside_requested_counts(tmp_path, failed_recorded_launch, cli, port):
    manifest, label = failed_recorded_launch
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_archive(RunSource('sample', tmp_path))
        async with httpx.AsyncClient(trust_env=False) as client:
            archive = await client.get(location.url)
            assert ' — run error — ' in archive.text and manifest['run_id'] in archive.text
            response = await client.get(f'http://127.0.0.1:{port}'+next(iter(owner._summary_routes)))
            assert response.json() == {'label': label}
        assert not owner._bindings
    finally:
        await owner.aclose()
