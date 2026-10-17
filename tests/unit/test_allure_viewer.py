"""Controlled CLI and HTTP proof for immutable viewer bindings."""
import asyncio
from functools import wraps
import hashlib
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.testing import allure_viewer as viewer
from api_agent.testing.allure_source import (MAX_ARTIFACT, MAX_MANIFEST, MAX_RUN,
    PreviewSnapshot, RunSource, ViewerError, component, validate_manifest, verified_files)


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapped


@pytest.fixture
def run():
    result_id = str(uuid4())
    name = result_id + '-result.json'
    full_name = 'tests/test_demo.py::test_demo'
    identity = hashlib.sha256(f'sample:integration:{full_name}'.encode()).hexdigest()
    files = {name: json.dumps({'uuid': result_id, 'status': 'passed',
        'name': 'test_demo', 'fullName': full_name, 'historyId': identity,
        'labels': [{'name': 'parentSuite', 'value': 'sample'},
                   {'name': 'suite', 'value': 'integration'},
                   {'name': 'subSuite', 'value': 'test_demo.py'}], 'attachments': [
        {'name': 'Picture', 'source': 'image.png'}]}).encode(), 'image.png': b'original picture'}
    manifest = {'schema': 1, 'repo': 'sample', 'run_id': str(uuid4()), 'mode': 'integration',
                'started_at': '2026-01-01T00:00:00Z', 'finished_at': '2026-01-01T00:01:00Z',
                'status': 'completed', 'files': {key: hashlib.sha256(body).hexdigest() for key, body in files.items()}}
    return manifest, files, name


@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = tmp_path / 'allure'
    path.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
if '--version' in sys.argv:
 print('2.32.0')
else:
 source=pathlib.Path(sys.argv[2]); output=pathlib.Path(sys.argv[4]); output.mkdir()
 (output/'index.html').write_text('Allure')
 cases=output/'data'/'test-cases'; cases.mkdir(parents=True)
 for n, result in enumerate(source.glob('*-result.json')):
  (cases/f'case{n}.json').write_text(json.dumps({'uid':f'case{n}'}))
''')
    path.chmod(0o700)
    monkeypatch.setattr(viewer.shutil, 'which', lambda _: str(path))
    return path


@pytest.fixture
def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Browser:
    def __init__(self):
        self.urls = []
        self.closed = False
        self.session = SimpleNamespace(page=self, inspection_endpoint='http://127.0.0.1:9000')

    async def start(self):
        return self.session

    async def goto(self, url):
        self.urls.append(url)

    async def aclose(self):
        self.closed = True


def save(root, manifest, files):
    directory = root / manifest['repo'] / manifest['run_id']
    directory.mkdir(parents=True)
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    for name, body in files.items():
        (directory / name).write_bytes(body)
    return directory


def test_local_history_identity_and_bytes(tmp_path, run):
    manifest, files, _ = run
    save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    assert source.history() == [manifest]
    assert source.load(manifest) == files


def test_s3_staging_without_local_results(run):
    manifest, files, _ = run
    objects = dict(files, **{'manifest.json': json.dumps(manifest).encode()})
    streams = []
    def get_object(**kwargs):
        stream = io.BytesIO(objects[kwargs['Key'].rsplit('/', 1)[-1]])
        streams.append(stream)
        return {'Body': stream}
    storage = SimpleNamespace(history=lambda repo: [manifest], bucket='reports',
        _client=lambda: SimpleNamespace(get_object=get_object), _key=lambda repo, run_id, name: name)
    source = RunSource('sample', storage=storage)
    assert source.history() == [manifest]
    assert source.load(manifest) == files
    assert all(stream.closed for stream in streams)


@async_test
async def test_history_separates_runs_selected_attempt_and_cleanup(tmp_path, run, cli, port):
    manifest, files, name = run
    save(tmp_path, manifest, files)
    second = dict(manifest, run_id=str(uuid4()), finished_at='2026-01-02T00:01:00Z', status='failed')
    save(tmp_path, second, files)
    source = RunSource('sample', tmp_path)
    browser = Browser()
    owner = viewer.ManagedAllureViewer(port, browser_factory=lambda: browser)
    try:
        location = await owner.show_history(source)
        async with httpx.AsyncClient() as client:
            assert second['run_id'] in location.url
            response = await client.get(location.archive_url)
            assert response.text.index(second['run_id']) < response.text.index(manifest['run_id'])
            selected = await owner.show_run(source, manifest, name)
            assert '#testresult/case0' in selected.url
            assert manifest['run_id'] in selected.url
            assert (await client.get(selected.url.split('#')[0])).text == 'Allure'
        assert selected.inspection_endpoint == 'http://127.0.0.1:9000'
        assert (await owner.show_run(source, manifest, name)).url == selected.url
        root = Path(owner._temporary.name)
    finally:
        await owner.aclose()
    assert browser.closed
    assert not root.exists()
    await owner.aclose()


@async_test
async def test_preview_versions_preserve_existing_routes(run, cli, port):
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    first = dict(manifest, preview_version=1)
    second = dict(manifest, preview_version=2)
    try:
        one = await owner.show_preview(PreviewSnapshot(files, first, {'test': name}), name)
        two = await owner.show_preview(PreviewSnapshot(files, second, {'test': name}), name)
        final = await owner.show_preview(PreviewSnapshot(files, manifest, {'test': name}, False), name)
        assert len({one.url, two.url, final.url}) == 3
        async with httpx.AsyncClient() as client:
            assert (await client.get(one.url.split('#')[0])).status_code == 200
    finally:
        await owner.aclose()


@pytest.mark.parametrize('value', ['', '.', '..', 'a/b', 'a\\b', 'a\0b', 1])
def test_invalid_component(value):
    with pytest.raises(ViewerError):
        component(value)


@pytest.mark.parametrize('change', [ {'schema': 2}, {'schema': True}, {'repo': '..'}, {'run_id': 'bad'},
    {'started_at': '2026-01-01'}, {'mode': 'unit'}, {'finished_at': '2025-01-01T00:00:00Z'},
    {'finished_at': '2026-01-01'}, {'status': 'running'}, {'files': {}}, {'files': []},
    {'files': {'manifest.json': 'a'*64}}, {'files': {'ok': 'bad'}}, {'files': {'ok': 'a'*64}},
    {'run_id': None}, {'started_at': None}])
def test_reject_invalid_manifest(run, change):
    manifest, _, _ = run
    with pytest.raises(ViewerError):
        validate_manifest(dict(manifest, **change))


@pytest.mark.parametrize('change', [{}, {'preview_version': 0}, {'preview_version': True}])
def test_preview_requires_explicit_version(run, change):
    manifest, _, _ = run
    with pytest.raises(ViewerError):
        validate_manifest(dict(manifest, **change), preview=True)


def test_reject_other_repo(run):
    with pytest.raises(ViewerError):
        validate_manifest(run[0], repo='other')


def test_corrupt_artifact(run):
    manifest, files, name = run
    files[name] = b'corrupt'
    with pytest.raises(ViewerError, match='digest'):
        verified_files(manifest, files.__getitem__)


@pytest.mark.parametrize('row', [[], {'uuid':'wrong','status':'passed'}, {'uuid':'original','status':'running'},
    {'uuid':'original','status':'passed','attachments':[{}]},
    {'uuid':'original','status':'passed','steps':[{'attachments':[{'source':'missing'}]}]},
    {'uuid':'original','status':'passed','attachments':[None]}, {'uuid':'original','status':'passed','attachments':None}])
def test_result_validation(row):
    files = {'original-result.json': json.dumps(row).encode()}
    manifest = {'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    with pytest.raises(ViewerError):
        verified_files(manifest, files.__getitem__)


@pytest.mark.parametrize('port', [0, 65536, True])
def test_invalid_port(port):
    with pytest.raises(ValueError):
        viewer.ManagedAllureViewer(port)


@async_test
async def test_foreign_listener_is_preserved(cli, port):
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', port))
        foreign.listen()
        owner = viewer.ManagedAllureViewer(port)
        try:
            with pytest.raises(ViewerError, match='occupied'):
                await owner._ready()
            assert foreign.getsockname()[1] == port
        finally:
            await owner.aclose()


def read_server_response_to_eof(port):
    # Keep the client open until the server sends FIN, making the server the
    # active closer and exercising its TIME_WAIT socket rather than a race.
    with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
        client.sendall(f'GET /.ready HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
        return b''.join(chunks)


@async_test
async def test_viewer_reopens_same_port_after_confirmed_http_shutdown(cli, port):
    first = viewer.ManagedAllureViewer(port)
    second = viewer.ManagedAllureViewer(port)
    try:
        await first._ready()
        response = await asyncio.to_thread(read_server_response_to_eof, port)
        assert response.startswith(b'HTTP/1.0 200 OK')
        server, thread = first._server, first._thread
        await first.aclose()
        assert server.socket.fileno() == -1
        assert not thread.is_alive()

        await second._ready()

        assert second._server.server_address == ('127.0.0.1', port)
        assert second._thread.is_alive()
    finally:
        await first.aclose()
        await second.aclose()


@async_test
async def test_active_managed_viewer_is_not_replaced(cli, port):
    first = viewer.ManagedAllureViewer(port)
    second = viewer.ManagedAllureViewer(port)
    try:
        await first._ready()
        with pytest.raises(ViewerError, match='occupied'):
            await second._ready()
        await second.aclose()

        await first._ready()

        assert first._thread.is_alive()
        assert first._server.server_address == ('127.0.0.1', port)
    finally:
        await second.aclose()
        await first.aclose()


@async_test
async def test_missing_cli(monkeypatch, port):
    monkeypatch.setattr(viewer.shutil, 'which', lambda _: None)
    owner = viewer.ManagedAllureViewer(port)
    with pytest.raises(ViewerError, match='missing'):
        await owner._ready()
    await owner.aclose()


@async_test
async def test_incompatible_cli(cli, port):
    cli.write_text('#!/bin/sh\necho 3.0.0\n')
    owner = viewer.ManagedAllureViewer(port)
    with pytest.raises(ViewerError, match='Unsupported'):
        await owner._ready()
    await owner.aclose()


@async_test
async def test_browser_failure_keeps_report(run, cli, port):
    def fail():
        raise RuntimeError('browser unavailable')
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=fail)
    try:
        location = await owner.show_preview(PreviewSnapshot(files, dict(manifest, preview_version=1), {}), name)
        assert location.browser_error
        async with httpx.AsyncClient() as client:
            assert (await client.get(location.url.split('#')[0])).status_code == 200
    finally:
        await owner.aclose()

@pytest.mark.parametrize('mutation', ['symlink', 'mismatch', 'oversized', 'invalid', 'conflict'])
def test_history_rejects_invalid_commits(tmp_path, run, mutation, monkeypatch):
    manifest, files, _ = run
    directory = save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    if mutation == 'symlink':
        (tmp_path/'sample').rename(tmp_path/'real')
        (tmp_path/'sample').symlink_to(tmp_path/'real')
    elif mutation == 'mismatch':
        directory.rename(directory.with_name(str(uuid4())))
    elif mutation == 'oversized':
        monkeypatch.setattr('api_agent.testing.allure_source.MAX_MANIFEST', 1)
    elif mutation == 'invalid':
        (directory/'manifest.json').write_text('bad')
    else:
        source.storage = SimpleNamespace(history=lambda _: [dict(manifest, status='failed')])
    with pytest.raises(ViewerError):
        source.history()


def test_history_ignores_uncommitted_directories(tmp_path, run):
    manifest, files, _ = run
    repo = tmp_path/'sample'
    repo.mkdir()
    (repo/'uncommitted').mkdir()
    (repo/'file').write_text('other')
    assert RunSource('sample', tmp_path).history() == []
    assert RunSource('sample').history() == []
    assert RunSource('sample', tmp_path/'absent').history() == []


def test_history_rejects_symlinked_run_child(tmp_path, run):
    manifest, files, _ = run
    directory = save(tmp_path, manifest, files)
    target = directory.with_name('stored-run')
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(ViewerError, match='Symlinked Allure history'):
        RunSource('sample', tmp_path).history()


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'symlink', 'unreadable'])
def test_load_refuses_replacement(tmp_path, run, mutation):
    manifest, files, _ = run
    directory = save(tmp_path, manifest, files)
    if mutation == 'changed':
        (directory/'manifest.json').write_text(json.dumps(dict(manifest,status='failed')))
    elif mutation == 'missing':
        (directory/'manifest.json').unlink()
    elif mutation == 'symlink':
        directory.rename(directory.with_name('real'))
        directory.symlink_to(directory.with_name('real'))
    else:
        (directory/'manifest.json').write_text('bad')
    with pytest.raises(ViewerError):
        RunSource('sample', tmp_path).load(manifest)


def test_remote_replacement_refused(run):
    manifest, _, _ = run
    storage = SimpleNamespace(bucket='x', _key=lambda *args: 'manifest.json', _client=lambda: SimpleNamespace(
        get_object=lambda **kwargs: {'Body':io.BytesIO(json.dumps(dict(manifest,status='failed')).encode())}))
    with pytest.raises(ViewerError, match='binding changed'):
        RunSource('sample', storage=storage).load(manifest)


@pytest.mark.parametrize('limit', ['MAX_RUN','MAX_ARTIFACT'])
def test_artifact_limits(run, monkeypatch, limit):
    manifest, files, _ = run
    monkeypatch.setattr('api_agent.testing.allure_source.'+limit, 1)
    with pytest.raises(ViewerError):
        verified_files(manifest, files.__getitem__)


def test_manifest_size_limit(run, monkeypatch):
    monkeypatch.setattr('api_agent.testing.allure_source.MAX_MANIFEST', 1)
    with pytest.raises(ViewerError):
        validate_manifest(run[0])


@async_test
async def test_server_directory_listing_disabled(cli, port):
    owner = viewer.ManagedAllureViewer(port)
    try:
        await owner._ready()
        async with httpx.AsyncClient() as client:
            assert (await client.get(f'http://127.0.0.1:{port}/')).status_code == 404
    finally:
        await owner.aclose()
    with pytest.raises(ViewerError, match='closed'):
        await owner._ready()


@pytest.mark.parametrize('mode', ['missing-index','missing-case','invalid-uid','invalid-json'])
@async_test
async def test_generation_rejects_invalid_output(run, cli, port, mode):
    suffix = {'missing-index':"(output/'index.html').unlink()", 'missing-case':"[p.unlink() for p in cases.glob('*.json')]",
        'invalid-uid':"(cases/'case0.json').write_text('{\"uid\":\"../bad\"}')", 'invalid-json':"(cases/'case0.json').write_text('bad')"}[mode]
    cli.write_text(cli.read_text()+'\n '+suffix+'\n')
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        with pytest.raises(ViewerError):
            await owner.show_preview(PreviewSnapshot(files, dict(manifest,preview_version=1), {}), name)
        assert not owner._bindings
    finally:
        await owner.aclose()


@async_test
async def test_invalid_selected_result_and_empty_history(run, cli, port):
    manifest, files, _ = run
    owner = viewer.ManagedAllureViewer(port)
    with pytest.raises(ViewerError, match='selected'):
        await owner._report(manifest, files, result_file='image.png')
    with pytest.raises(ViewerError, match='No finalized'):
        await owner.show_history(RunSource('sample'))
    await owner.aclose()


@pytest.mark.parametrize('mode', ['startup','exit','timeout'])
@async_test
async def test_cli_failures_are_bounded(cli, port, mode):
    owner = viewer.ManagedAllureViewer(port, timeout=.02)
    if mode == 'startup':
        arguments = ['/missing-allure-executable']
    elif mode == 'exit':
        arguments = ['/bin/sh','-c','exit 1']
    else:
        arguments = ['/bin/sh','-c','sleep 30']
    with pytest.raises(ViewerError):
        await owner._command(arguments)
    assert owner._processes == []
    await owner.aclose()


@async_test
async def test_cancel_generation_reaps_owned_process(cli, port):
    owner = viewer.ManagedAllureViewer(port)
    task = asyncio.create_task(owner._command(['/bin/sh','-c','sleep 30']))
    while not owner._processes:
        await asyncio.sleep(0)
    process = owner._processes[0]
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.poll() is not None
        assert owner._processes == []
    finally:
        from api_agent.testing.processes import finalize
        await asyncio.to_thread(finalize, process)
        await owner.aclose()


@async_test
async def test_close_during_generation_cancels_and_reaps_owned_process(run, cli, port):
    cli.write_text(cli.read_text().replace(" source=pathlib.Path(sys.argv[2])", " import time; time.sleep(30)\n source=pathlib.Path(sys.argv[2])"))
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    task = asyncio.create_task(owner.show_preview(PreviewSnapshot(files, dict(manifest, preview_version=1), {}), name))
    try:
        while not any(len(process.args) > 1 and process.args[1] == 'generate' for process in owner._processes):
            await asyncio.sleep(.01)
        process = owner._processes[0]

        await asyncio.wait_for(owner.aclose(), timeout=3)

        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.poll() is not None
        assert owner._processes == []
        assert owner._operations == set()
        assert owner._temporary is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await owner.aclose()


@async_test
async def test_close_during_browser_start_cancels_operation_and_closes_browser(run, cli, port):
    entered = asyncio.Event()
    browser = Browser()

    async def waiting_start():
        entered.set()
        await asyncio.Event().wait()

    browser.start = waiting_start
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=lambda: browser)
    task = asyncio.create_task(owner.show_preview(PreviewSnapshot(files, dict(manifest, preview_version=1), {}), name))
    try:
        await entered.wait()
        await asyncio.wait_for(owner.aclose(), timeout=3)

        with pytest.raises(asyncio.CancelledError):
            await task
        assert browser.closed
        assert owner._operations == set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await owner.aclose()


@async_test
async def test_close_cancels_queued_viewer_work_before_source_retrieval(run, port):
    entered = asyncio.Event()
    owner = viewer.ManagedAllureViewer(port)
    source = SimpleNamespace(load=lambda *args, **kwargs: pytest.fail('queued source was loaded'))
    manifest, _, _ = run

    async def holding_lock():
        async with owner._lock:
            entered.set()
            await asyncio.Event().wait()

    blocker = asyncio.create_task(holding_lock())
    await entered.wait()
    queued = asyncio.create_task(owner.show_run(source, manifest))
    await asyncio.sleep(0)
    assert queued in owner._operations

    closing = asyncio.create_task(owner.aclose())
    await asyncio.sleep(0)
    assert queued.cancelling()
    await asyncio.gather(queued, return_exceptions=True)
    assert queued.cancelled()
    blocker.cancel()
    await asyncio.gather(blocker, return_exceptions=True)
    await asyncio.wait_for(closing, timeout=3)
    assert owner._operations == set()


@async_test
async def test_browser_cleanup_failure_retains_retry(port):
    browser = Browser()
    async def fail():
        raise RuntimeError('retry cleanup')
    browser.aclose = fail
    owner = viewer.ManagedAllureViewer(port, browser_factory=lambda:browser)
    owner._browser = browser
    with pytest.raises(RuntimeError):
        await owner.aclose()
    assert owner._browser is browser
    del browser.aclose
    await owner.aclose()
    assert browser.closed


def test_noncanonical_run_id(run):
    manifest = dict(run[0], run_id=run[0]['run_id'].upper())
    with pytest.raises(ViewerError):
        validate_manifest(manifest)


def test_nested_steps_validation_succeeds():
    row = {'uuid':'original','status':'passed','steps':[{'name':'first','steps':[]}]}
    files = {'original-result.json':json.dumps(row).encode()}
    assert verified_files({'files':{key:hashlib.sha256(data).hexdigest() for key,data in files.items()}}, files.__getitem__) == files


@async_test
async def test_readiness_mismatch(cli, port, monkeypatch):
    async def wrong(*args, **kwargs):
        return SimpleNamespace(status_code=200,text='foreign')
    monkeypatch.setattr(httpx.AsyncClient,'get',wrong)
    owner = viewer.ManagedAllureViewer(port)
    try:
        with pytest.raises(ViewerError, match='ready'):
            await owner._ready()
    finally:
        await owner.aclose()


@async_test
async def test_single_result_report_excludes_other_attempt(run, cli, port):
    manifest, files, name = run
    second = str(uuid4())
    files[second+'-result.json'] = json.dumps({'uuid':second,'status':'failed'}).encode()
    manifest['files'] = {key:hashlib.sha256(data).hexdigest() for key,data in files.items()}
    owner = viewer.ManagedAllureViewer(port,browser_factory=Browser)
    try:
        location = await owner.show_preview(PreviewSnapshot(files,dict(manifest,preview_version=1),{}),name)
        assert location.url.endswith('#testresult/case0')
    finally:
        await owner.aclose()


@async_test
async def test_pending_process_cleanup_is_retryable(port, monkeypatch):
    owner = viewer.ManagedAllureViewer(port)
    process = object()
    owner._processes.append(process)
    seen = []
    monkeypatch.setattr(viewer,'finalize',seen.append)
    await owner.aclose()
    assert seen == [process]
    assert owner._processes == []


@async_test
async def test_server_thread_cleanup_retry(cli, port):
    owner = viewer.ManagedAllureViewer(port)
    await owner._ready()
    thread = owner._thread
    class Pending:
        def join(self, **kwargs):
            pass
        def is_alive(self):
            return True
    owner._thread = Pending()
    with pytest.raises(ViewerError,match='pending'):
        await owner.aclose()
    owner._thread = thread
    await owner.aclose()


@async_test
async def test_default_browser_is_inspectable(port,monkeypatch):
    browser = Browser()
    seen = []
    def managed(**kwargs):
        seen.append(kwargs)
        return browser
    monkeypatch.setattr(viewer,'managed_browser',managed)
    owner = viewer.ManagedAllureViewer(port)
    await owner._open('http://127.0.0.1/example')
    assert seen == [{'headless':False,'inspection':owner._inspection}]
    await owner.aclose()


@async_test
async def test_history_http_generates_latest_and_preserves_host_boundary(tmp_path,run,cli,port):
    manifest,files,_=run
    save(tmp_path,manifest,files)
    source=RunSource('sample',tmp_path)
    owner=viewer.ManagedAllureViewer(port,browser_factory=Browser)
    try:
        location=await owner.show_history(source)
        assert len(owner._bindings)==1
        async with httpx.AsyncClient() as client:
            index=await client.get(location.archive_url)
            import re
            path=re.search(r'href="([^"]+)"',index.text)[1]
            response=await client.get(f'http://127.0.0.1:{port}'+path)
            assert response.status_code==302
            assert manifest['run_id'] in response.headers['Location']
            assert (await client.get(response.headers['Location'])).text=='Allure'
            assert len(owner._bindings)==1
            assert (await client.get(location.url,headers={'Host':'untrusted.example'})).status_code==403
            assert (await client.head(location.url,headers={'Host':'untrusted.example'})).status_code==403
            assert (await client.head(location.url)).status_code==200
            assert (await client.get(f'http://127.0.0.1:{port}/open/missing')).status_code==404
        assert owner._http_tasks==set()
    finally:
        await owner.aclose()


@async_test
async def test_late_artifact_corruption_returns_http_failure_without_serving_partial_report(tmp_path,run,cli,port):
    manifest,files,name=run
    directory=save(tmp_path,manifest,files)
    owner=viewer.ManagedAllureViewer(port,browser_factory=Browser)
    try:
        await owner.show_history(RunSource('sample',tmp_path))
        path=next(iter(owner._history_routes))
        (directory/name).write_text('corrupt')
        async with httpx.AsyncClient() as client:
            response=await client.get(f'http://127.0.0.1:{port}'+path)
            assert response.status_code==503
            assert 'corrupt' not in response.text
        assert len(owner._bindings)==1
    finally:
        await owner.aclose()


@async_test
async def test_http_wait_timeout_cancels_the_owned_generation_future(tmp_path,run,cli,port,monkeypatch):
    manifest,files,_=run
    save(tmp_path,manifest,files)
    owner=viewer.ManagedAllureViewer(port,browser_factory=Browser)
    cancelled=[]
    def submit(coroutine,loop):
        coroutine.close()
        def timeout(**kwargs):
            raise viewer.FutureTimeout()
        return SimpleNamespace(result=timeout,cancel=lambda:cancelled.append(True))
    try:
        await owner.show_history(RunSource('sample',tmp_path))
        monkeypatch.setattr(viewer.asyncio,'run_coroutine_threadsafe',submit)
        path=next(iter(owner._history_routes))
        async with httpx.AsyncClient() as client:
            assert (await client.get(f'http://127.0.0.1:{port}'+path)).status_code==504
        assert cancelled==[True]
    finally:
        await owner.aclose()


@async_test
async def test_cleanup_cancels_and_drains_queued_http_work(port):
    owner=viewer.ManagedAllureViewer(port)
    entered=asyncio.Event()
    stopped=asyncio.Event()
    async def waiting():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    task=asyncio.create_task(waiting())
    owner._http_tasks.add(task)
    await entered.wait()
    await owner.aclose()
    assert task.done() and stopped.is_set()


@async_test
async def test_closed_viewer_never_retrieves_or_generates_new_history(run,port):
    owner=viewer.ManagedAllureViewer(port)
    await owner.aclose()
    manifest,files,name=run
    source=object()
    with pytest.raises(ViewerError,match='closed'):
        await owner.show_history(source)
    with pytest.raises(ViewerError,match='closed'):
        await owner.show_run(source,manifest)
    with pytest.raises(ViewerError,match='closed'):
        await owner.show_preview(PreviewSnapshot(files,dict(manifest,preview_version=1),{}),name)
    with pytest.raises(ViewerError,match='closed'):
        await owner._history_report(source,manifest)
    assert owner._http_tasks==set()


def test_source_checks_budget_before_discovery_and_fetch(run):
    source=RunSource('sample')
    expired=SimpleNamespace(remaining=0)
    with pytest.raises(ViewerError,match='timed out'):
        source.history(budget=expired)
    with pytest.raises(ViewerError,match='timed out'):
        source.load(run[0],budget=expired)


def test_well_formed_replacement_still_requires_its_immutable_digest(run):
    manifest,files,name=run
    original=json.loads(files[name])
    files[name]=json.dumps(dict(original,status='failed')).encode()
    with pytest.raises(ViewerError,match='digest mismatch'):
        verified_files(manifest,files.__getitem__)


@pytest.fixture(params=[(True, True), (False, True), (True, False), (False, False)],
                ids=['preview-existing', 'final-existing', 'preview-annotation', 'final-annotation'])
def metadata_snapshot(run, request):
    manifest, files, name = run
    preview, existing = request.param
    original = b'Original=authoritative\nSuite=recorded\n'
    if existing:
        files['environment.properties'] = original
    manifest['files'] = {key: hashlib.sha256(body).hexdigest() for key, body in files.items()}
    if preview:
        manifest['preview_version'] = 1
    publication = 'Live preview; suite accounting pending' if preview else 'Finalized immutable run'
    expected = original if existing else f'Run={manifest["run_id"]}\nPublication={publication}\n'.encode()
    return PreviewSnapshot(files, manifest, {}, preview), name, expected


@async_test
async def test_generation_preserves_recorded_environment_before_adding_annotations(metadata_snapshot, cli, port):
    snapshot, name, expected = metadata_snapshot
    cli.write_text(cli.read_text() + "\n (output/'observed-environment.bin').write_bytes((source/'environment.properties').read_bytes())\n")
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    original_files = dict(snapshot.files)
    try:
        await owner.show_preview(snapshot, name)
        route, = owner._bindings
        observed = Path(owner._temporary.name) / route / 'observed-environment.bin'
        assert observed.read_bytes() == expected
        assert snapshot.files == original_files
    finally:
        await owner.aclose()


@pytest.mark.parametrize('command', ['', '  ', '"unfinished', None, 3, [], 'bad\0command'])
@async_test
async def test_invalid_configured_command_is_actionable_before_launch(port, command):
    owner = viewer.ManagedAllureViewer(port, cli=command)
    try:
        with pytest.raises(ViewerError, match='Invalid ALLURE_CLI'):
            await owner._ready()
        assert owner._processes == []
    finally:
        await owner.aclose()


@async_test
async def test_quoted_command_prefix_reaches_version_and_generation_literally(run, cli, port, tmp_path, monkeypatch):
    import shlex
    import sys
    wrapper = tmp_path / 'host wrapper.py'
    recorded = tmp_path / 'arguments.jsonl'
    literal = '$not_expanded; literal argument'
    wrapper.write_text('import json,os,sys\n'
        + f'with open({str(recorded)!r},"a") as stream: stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
        + 'os.execv(sys.executable,[sys.executable,sys.argv[2],*sys.argv[3:]])\n')
    monkeypatch.setattr(viewer.shutil, 'which', lambda name: name)
    command = shlex.join([sys.executable, str(wrapper), literal, str(cli)])
    manifest, files, name = run
    owner = viewer.ManagedAllureViewer(port, cli=command, browser_factory=Browser)
    try:
        await owner.show_preview(PreviewSnapshot(files, dict(manifest, preview_version=1), {}), name)
        calls = [json.loads(line) for line in recorded.read_text().splitlines()]
        assert [row[:3] for row in calls] == [[literal, str(cli), '--version'], [literal, str(cli), 'generate']]
        assert owner._cli == (sys.executable, str(wrapper), literal, str(cli))
    finally:
        await owner.aclose()


@async_test
async def test_host_command_exit_reports_status_without_echoing_output(cli, port):
    cli.write_text('#!/bin/sh\necho synthetic-private-diagnostic >&2\nexit 127\n')
    owner = viewer.ManagedAllureViewer(port)
    try:
        with pytest.raises(ViewerError, match='exit 127') as error:
            await owner._ready()
        assert 'synthetic-private-diagnostic' not in str(error.value)
        assert 'ALLURE_CLI' in str(error.value)
        assert owner._processes == []
    finally:
        await owner.aclose()


def test_shared_allure_command_is_review_visible_with_default():
    from api_agent.config import load_shared_schema
    field = load_shared_schema().fields['ALLURE_CLI']
    assert field.default == 'allure'
    assert field.acknowledge_default is True


@pytest.fixture
def history_cli(cli):
    cli.write_text(cli.read_text() + '''
 (output/'history').mkdir()
 prior=source/'history'/'history.json'
 chain=json.loads(prior.read_text()) if prior.exists() else []
 executor=json.loads((source/'executor.json').read_text())
 chain.append(executor.get('reportName','recorded executor'))
 (output/'history'/'history.json').write_text(json.dumps(chain))
 (output/'observed.json').write_text(json.dumps({'chain':chain,'executor':executor,
   'results':len(list(source.glob('*-result.json')))}))
''')
    return cli


def history_runs(root, run, dates, *, modes=None):
    manifest, files, _ = run
    rows = []
    for index, date in enumerate(dates):
        row = dict(manifest, run_id=str(uuid4()), finished_at=date,
                   mode=modes[index] if modes else 'integration')
        save(root, row, files)
        rows.append(row)
    return rows


def generated_root(owner, location):
    return Path(owner._temporary.name) / location.url.split(f':{owner.port}/', 1)[1].split('/index.html')[0]


@async_test
async def test_native_history_transfers_launches_without_pooling_results(tmp_path, run, history_cli, port):
    rows = history_runs(tmp_path, run, ['2026-01-01T01:00:00Z', '2026-01-02T01:00:00Z',
                                      '2026-01-03T01:00:00Z', '2026-01-04T01:00:00Z'],
                        modes=['integration', 'e2e', 'integration', 'integration'])
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(RunSource('sample', tmp_path))
        observed = json.loads((generated_root(owner, location) / 'observed.json').read_text())
        assert observed['chain'] == ['integration — ' + rows[i]['finished_at'] for i in (0, 2, 3)]
        assert observed['results'] == 1
        assert observed['executor']['reportUrl'] == location.url
        assert observed['executor']['buildUrl'] == location.archive_url
        assert observed['executor']['buildOrder'] == 1767488400000
        assert len(owner._bindings) == 3
        assert rows[-1]['run_id'] in location.url
        assert run[1] == RunSource('sample', tmp_path).load(rows[0])
    finally:
        await owner.aclose()


@async_test
async def test_native_window_is_bounded_and_archive_still_lists_older_launches(tmp_path, run, history_cli, port):
    rows = history_runs(tmp_path, run, [f'2026-01-{day:02d}T01:00:00Z' for day in range(1, 23)])
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(RunSource('sample', tmp_path))
        chain = json.loads((generated_root(owner, location) / 'history/history.json').read_text())
        assert len(chain) == viewer.HISTORY_LAUNCHES == 20
        assert chain[0] == 'integration — ' + rows[2]['finished_at']
        assert len(owner._bindings) == 20
        async with httpx.AsyncClient() as client:
            archive = await client.get(location.archive_url)
            assert all(row['run_id'] in archive.text for row in rows)
            route = next(route for route, binding in owner._history_routes.items()
                         if binding[1]['run_id'] == rows[0]['run_id'])
            previous = await client.get(f'http://127.0.0.1:{port}' + route)
            assert previous.status_code == 302
            assert rows[0]['run_id'] in previous.headers['Location']
        assert len(owner._bindings) == 21
    finally:
        await owner.aclose()


@async_test
async def test_late_launch_refresh_freezes_old_history_context(tmp_path, run, history_cli, port):
    rows = history_runs(tmp_path, run, ['2026-01-01T01:00:00Z', '2026-01-03T01:00:00Z'])
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    source = RunSource('sample', tmp_path)
    try:
        first = await owner.show_history(source)
        original = (generated_root(owner, first) / 'observed.json').read_bytes()
        routes = dict(owner._history_routes)
        history_runs(tmp_path, run, ['2026-01-02T01:00:00Z'])
        second = await owner.show_history(source)
        assert first.url != second.url
        assert (generated_root(owner, first) / 'observed.json').read_bytes() == original
        updated = json.loads((generated_root(owner, second) / 'observed.json').read_bytes())
        assert len(updated['chain']) == 3
        assert all(owner._history_routes[path] == binding for path, binding in routes.items())
        assert rows[-1]['run_id'] in first.url and rows[-1]['run_id'] in second.url
    finally:
        await owner.aclose()


@async_test
async def test_history_preserves_manifest_executor_and_rejects_staging_collision(tmp_path, run, history_cli, port):
    manifest, files, _ = run
    original = b'{"name":"Original recorder","reportUrl":"https://example.org/report"}'
    files['executor.json'] = original
    manifest['files'] = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    save(tmp_path, manifest, files)
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(RunSource('sample', tmp_path))
        observed = json.loads((generated_root(owner, location) / 'observed.json').read_bytes())
        assert observed['executor'] == json.loads(original)
        files['history'] = b'recorded artifact'
        changed = dict(manifest, run_id=str(uuid4()), files={
            name: hashlib.sha256(body).hexdigest() for name, body in files.items()})
        save(tmp_path, changed, files)
        with pytest.raises(ViewerError, match='conflicts with derived history'):
            await owner.show_history(RunSource('sample', tmp_path))
    finally:
        await owner.aclose()


@pytest.mark.parametrize('damage', ['directory-link', 'file-link', 'fifo', 'artifact-limit', 'aggregate-limit'])
@async_test
async def test_generated_history_transfer_refuses_unsafe_or_oversized_files(run, cli, port, tmp_path, monkeypatch, damage):
    manifest, files, _ = run
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        url = await owner._report(manifest, files)
        directory = generated_root(owner, SimpleNamespace(url=url)) / 'history'
        directory.mkdir()
        (directory / 'history.json').write_text('{}')
        if damage == 'directory-link':
            directory.rename(directory.with_name('real-history'))
            directory.symlink_to(directory.with_name('real-history'))
        elif damage == 'file-link':
            (directory / 'history.json').unlink()
            (directory / 'history.json').symlink_to(tmp_path / 'missing')
        elif damage == 'fifo':
            import os
            (directory / 'history.json').unlink()
            os.mkfifo(directory / 'history.json')
        elif damage == 'artifact-limit':
            monkeypatch.setattr('api_agent.testing.allure_source.MAX_ARTIFACT', 1)
        else:
            monkeypatch.setattr('api_agent.testing.allure_source.MAX_RUN', 1)
        with pytest.raises(ViewerError, match='generated Allure history|transfer limit'):
            owner._generated_history(url)
    finally:
        await owner.aclose()


@pytest.mark.parametrize('parameters', [None, {}, [], [None], [None, {'name':'other'}],
    [{'name':'attempt'}], [{'name':'other','value':'2'}],
    [{'name':'attempt','value':'2'},{'name':'other','value':'3'}],
    [{'name':'attempt','value':'2','excluded':False}],
    [{'name':'attempt','value':'2','excluded':True}]])
def test_history_parameter_upgrade_preserves_unaffected_bytes(run, parameters):
    manifest = run[0]
    full_name = 'tests/test_demo.py::test_demo'
    history_id = hashlib.sha256(f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest()
    row = json.loads(run[1][run[2]])
    row.update(fullName=full_name, historyId=history_id, parameters=parameters)
    body = json.dumps(row, indent=2).encode()
    assert viewer._history_result(body, manifest) == body


@pytest.mark.parametrize('owned', [True, False])
def test_legacy_retry_counter_is_excluded_only_for_recorder_owned_identity(run, owned):
    manifest = run[0]
    full_name = 'tests/test_demo.py::test_demo'
    history_id = hashlib.sha256(f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest()
    row = json.loads(run[1][run[2]])
    row.update(fullName=full_name, historyId=history_id if owned else 'foreign',
               parameters=[{'name':'attempt','value':'2'}])
    body = json.dumps(row, indent=2).encode()
    converted = viewer._history_result(body, manifest)
    if owned:
        assert json.loads(converted)['parameters'] == [{'name':'attempt','value':'2','excluded':True}]
        assert json.loads(body) == row
    else:
        assert converted == body


@async_test
async def test_close_during_second_history_generation_drains_and_removes_partial_chain(tmp_path, run, cli, port):
    marker = tmp_path / 'generation-count'
    startup = (f' marker=pathlib.Path({str(marker)!r})\n'
               ' count=int(marker.read_text())+1 if marker.exists() else 1\n'
               ' marker.write_text(str(count))\n'
               ' if count==2:\n  import time; time.sleep(30)\n')
    cli.write_text(cli.read_text().replace(' source=pathlib.Path(sys.argv[2])',
                                         startup + ' source=pathlib.Path(sys.argv[2])'))
    history_runs(tmp_path, run, ['2026-01-01T01:00:00Z', '2026-01-02T01:00:00Z'])
    browser = Browser()
    owner = viewer.ManagedAllureViewer(port, browser_factory=lambda: browser)
    operation = asyncio.create_task(owner.show_history(RunSource('sample', tmp_path)))
    try:
        async with asyncio.timeout(10):
            while not marker.exists() or marker.read_text() != '2':
                await asyncio.sleep(.01)
        assert len(owner._bindings) == 1
        process, = owner._processes
        await asyncio.wait_for(owner.aclose(), timeout=10)
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert process.poll() is not None
        assert owner._processes == [] and owner._operations == set()
        assert owner._temporary is None and browser.urls == []
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await owner.aclose()


@pytest.mark.parametrize('value', ['semantic-blue', 'semantic-green', '', '0', '01', '+1', '-1',
    '1.0', '2e1', ' 2', '2\n', '２', 2, True, None])
def test_history_preserves_noncanonical_or_semantic_attempt_values(run, value):
    manifest = run[0]
    full_name = 'tests/test_demo.py::test_demo'
    history_id = hashlib.sha256(f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest()
    row = json.loads(run[1][run[2]])
    row.update(fullName=full_name, historyId=history_id,
               parameters=[{'name':'attempt','value':value}])
    body = json.dumps(row, indent=2).encode()
    assert viewer._history_result(body, manifest) == body


@pytest.mark.parametrize('parameters', [
    [{'name':'attempt','value':'semantic-blue'}],
    [{'name':'attempt','value':'0'}],
    [{'name':'attempt','value':'2'},{'name':'attempt','value':'semantic-blue'}],
    [{'name':'attempt','value':'2'},{'name':'attempt','value':'3'}],
    [{'name':'attempt','value':'2','excluded':False}],
    [{'name':'attempt','value':'2'},{'name':'other','value':'3'}],
])
@async_test
async def test_verified_foreign_parameters_reach_generation_unchanged(tmp_path, run, cli, port, parameters):
    manifest, files, name = run
    full_name = 'tests/test_demo.py::test_demo'
    row = json.loads(files[name])
    row.update(fullName=full_name, parameters=parameters, historyId=hashlib.sha256(
        f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest())
    files[name] = json.dumps(row, indent=2).encode()
    manifest['files'] = {key: hashlib.sha256(body).hexdigest() for key, body in files.items()}
    save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    assert validate_manifest(manifest, repo='sample')
    assert source.load(manifest) == files
    cli.write_text(cli.read_text() + "\n (output/'observed-result.bin').write_bytes(next(source.glob('*-result.json')).read_bytes())\n")
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(source)
        assert (generated_root(owner, location) / 'observed-result.bin').read_bytes() == files[name]
        assert source.load(manifest) == files
    finally:
        await owner.aclose()


@pytest.fixture(params=[
    ('"name":"attempt"', '"name":"other","name":"attempt"'),
    ('"name":"attempt"', '"na\\u006de":"other","name":"attempt"'),
    ('"value":"2"', '"value":"semantic-blue","value":"2"'),
    ('"value":"2"', '"\\u0076alue":"semantic-blue","value":"2"'),
    ('"fullName":', '"fullName":"foreign","fullName":'),
    ('"historyId":', '"historyId":"foreign","historyId":'),
    ('"parameters":', '"parameters":[],"parameters":'),
    ('"parameters":', '"metadata":{"mode":1,"mode":2},"parameters":'),
    ('"parameters":', '"metadata":{"name":1,"name":2,"deep":{"value":1,"value":2}},"parameters":'),
], ids=['name','escaped-name','value','escaped-value','full-name','history-id',
        'parameters','nested-metadata','multiple-depths'])
def duplicate_history_result(run, request):
    manifest, files, name = run
    full_name = 'tests/test_demo.py::test_demo'
    row = json.loads(files[name])
    row.update(fullName=full_name, parameters=[{'name':'attempt','value':'2'}],
        historyId=hashlib.sha256(f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest())
    body = json.dumps(row, separators=(',', ':'))
    old, new = request.param
    assert old in body
    files[name] = body.replace(old, new, 1).encode()
    manifest['files'] = {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}
    return manifest, files, name


@async_test
async def test_verified_duplicate_members_are_never_rewritten(tmp_path, duplicate_history_result, cli, port):
    manifest, files, name = duplicate_history_result
    save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    assert validate_manifest(manifest, repo='sample')
    assert source.load(manifest) == files
    cli.write_text(cli.read_text() + "\n (output/'observed-result.bin').write_bytes(next(source.glob('*-result.json')).read_bytes())\n")
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(source)
        assert (generated_root(owner, location) / 'observed-result.bin').read_bytes() == files[name]
        assert source.load(manifest) == files
    finally:
        await owner.aclose()


@pytest.fixture(params=[
    ({}, ('fullName',)),
    ({'fullName': None}, ()),
    ({'fullName': ''}, ()),
    ({'fullName': 0}, ()),
    ({'fullName': False}, ()),
    ({'fullName': []}, ()),
    ({'fullName': {}}, ()),
    ({'fullName': 'test_demo'}, ()),
    ({'fullName': '::test_demo'}, ()),
    ({'fullName': 'tests/test_demo.py::', 'name': ''}, ()),
    ({}, ('name',)),
    ({'name': 'foreign'}, ()),
    ({'name': None}, ()),
    ({}, ('labels',)),
    ({'labels': []}, ()),
    ({'labels': None}, ()),
    ({'labels': [{'name': 'parentSuite', 'value': 'foreign'},
                {'name': 'suite', 'value': 'integration'},
                {'name': 'subSuite', 'value': 'test_demo.py'}]}, ()),
    ({'labels': [{'name': 'parentSuite', 'value': 'sample'},
                {'name': 'suite', 'value': 'e2e'},
                {'name': 'subSuite', 'value': 'test_demo.py'}]}, ()),
    ({'labels': [{'name': 'parentSuite', 'value': 'sample'},
                {'name': 'suite', 'value': 'integration'},
                {'name': 'subSuite', 'value': 'other.py'}]}, ()),
    ({'labels': [{'name': 'parentSuite', 'value': 'sample'},
                {'name': 'suite', 'value': 'integration'},
                {'name': 'subSuite', 'value': 'test_demo.py'},
                {'name': 'owner', 'value': 'foreign'}]}, ()),
], ids=['missing-full-name', 'null-full-name', 'empty-full-name', 'numeric-full-name',
        'boolean-full-name', 'list-full-name', 'object-full-name', 'unqualified-full-name',
        'empty-file', 'empty-node', 'missing-name', 'foreign-name', 'null-name',
        'missing-labels', 'empty-labels', 'null-labels', 'foreign-repo', 'foreign-mode',
        'foreign-file', 'extra-label'])
def foreign_history_identity(run, request):
    manifest, files, name = run
    row = json.loads(files[name])
    changes, removed = request.param
    row.update(changes)
    for key in removed:
        del row[key]
    row['historyId'] = hashlib.sha256(
        f'{manifest["repo"]}:{manifest["mode"]}:{row.get("fullName")}'.encode()).hexdigest()
    row['parameters'] = [{'name': 'attempt', 'value': '2'}]
    files[name] = json.dumps(row, indent=2).encode()
    manifest['files'] = {key: hashlib.sha256(body).hexdigest() for key, body in files.items()}
    return manifest, files, name


@async_test
async def test_verified_foreign_identity_reaches_generation_byte_exact(tmp_path, foreign_history_identity, cli, port):
    manifest, files, name = foreign_history_identity
    save(tmp_path, manifest, files)
    source = RunSource('sample', tmp_path)
    assert validate_manifest(manifest, repo='sample')
    assert source.load(manifest) == files
    cli.write_text(cli.read_text() + "\n (output/'observed-result.bin').write_bytes(next(source.glob('*-result.json')).read_bytes())\n")
    owner = viewer.ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_history(source)
        assert (generated_root(owner, location) / 'observed-result.bin').read_bytes() == files[name]
        assert source.load(manifest) == files
    finally:
        await owner.aclose()


@pytest.mark.parametrize('node', ['test_demo', 'TestExample::test_demo',
                                 'TestExample::test_demo[first::second]'])
def test_genuine_recorder_identity_upgrades_legacy_counter(tmp_path, node):
    from datetime import datetime, timezone
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.recording import RunIdentity, RunRecorder
    full_name = 'tests/test_demo.py::' + node
    identity = RunIdentity(str(uuid4()), 'sample', 'integration', datetime(2026, 1, 1, tzinfo=timezone.utc))
    recorder = RunRecorder(tmp_path, identity)
    test = TestIdentity('sample::' + full_name, 'sample', 'tests/test_demo.py', full_name)
    name = recorder.record_test(TestResult(test, 'passed', 1), attempt=2)
    manifest = recorder.finalize()
    row = json.loads((tmp_path / name).read_bytes())
    del row['parameters'][0]['excluded']
    body = json.dumps(row, indent=2).encode()
    expected = json.loads(body)
    expected['parameters'][0]['excluded'] = True
    assert json.loads(viewer._history_result(body, manifest)) == expected
