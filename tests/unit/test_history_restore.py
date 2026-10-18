"""Controlled S3 and local-file checks for explicit history restoration."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
from types import SimpleNamespace
from contextlib import contextmanager
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError, EndpointConnectionError

from api_agent.testing.history_restore import (HistoryRestoreCancelled,
    HistoryRestoreError, restore_missing_history)
from api_agent.testing import history_restore
from api_agent.testing import suite_lock as suite_lock_module
from api_agent.testing.storage import RunStorage
from api_agent.testing.allure_source import RunSource, ViewerError, validate_manifest
from api_agent.tui.allure import AllureNavigation, _known_history_viewer_message
from test_allure_viewer import run, save
from test_recording_storage import FakeS3
from test_tui import fixture
from textual.widgets import Static


@pytest.fixture(autouse=True)
def private_restore_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(suite_lock_module, 'suite_lock_path', lambda: tmp_path / 'restore-suite.lock')


@pytest.fixture
def history(tmp_path, run):
    manifest, files, _ = run
    local = tmp_path / 'secondary'
    directory = save(local, manifest, files)
    s3 = FakeS3()
    storage = RunStorage('selected-bucket', client=s3, prefix='selected-prefix')
    return manifest, files, local, directory, s3, storage


def test_restores_verified_artifacts_before_original_manifest(history):
    manifest, files, local, directory, s3, storage = history
    progress = []

    count = restore_missing_history('sample', local, storage, progress=lambda done, total: progress.append((done, total)))

    assert count == 1 and progress == [(0, 1), (1, 1)]
    prefix = f'selected-prefix/sample/{manifest["run_id"]}/'
    assert list(s3.objects) == [prefix + name for name in sorted(files)] + [prefix + 'manifest.json']
    assert s3.objects[prefix + 'manifest.json'] == (directory / 'manifest.json').read_bytes()
    assert {name: s3.objects[prefix + name] for name in files} == files


def test_equal_existing_bytes_are_idempotent_and_conflicts_never_overwrite(history):
    manifest, files, local, _, s3, storage = history
    assert restore_missing_history('sample', local, storage) == 1
    original = dict(s3.objects)
    assert restore_missing_history('sample', local, storage) == 1
    assert s3.objects == original
    artifact = next(iter(files))
    key = storage._key('sample', manifest['run_id'], artifact)
    s3.objects[key] = b'conflicting destination bytes'

    with pytest.raises(HistoryRestoreError, match='Could not restore'):
        restore_missing_history('sample', local, storage)

    assert s3.objects[key] == b'conflicting destination bytes'
    assert s3.objects[storage._key('sample', manifest['run_id'], 'manifest.json')] == original[
        storage._key('sample', manifest['run_id'], 'manifest.json')]


def test_different_committed_manifest_rejects_before_missing_artifact_upload(history):
    manifest, files, local, _, s3, storage = history
    key = storage._key('sample', manifest['run_id'], 'manifest.json')
    s3.objects[key] = b'different committed manifest'

    with pytest.raises(HistoryRestoreError, match='different immutable manifest'):
        restore_missing_history('sample', local, storage)

    assert s3.objects == {key: b'different committed manifest'}


def test_competing_manifest_after_preflight_wins_without_merged_visible_run(history):
    manifest, files, local, _, s3, storage = history
    run_id = manifest['run_id']
    manifest_key = storage._key('sample', run_id, 'manifest.json')
    competitor_id = str(uuid4())
    competitor_name = competitor_id + '-result.json'
    competitor_body = json.dumps({'uuid': competitor_id, 'status': 'passed',
                                   'name': 'competitor', 'fullName': 'competitor'}).encode()
    competitor = dict(manifest, files={competitor_name: hashlib.sha256(competitor_body).hexdigest()})
    validate_manifest(competitor, repo='sample')
    competitor_manifest = json.dumps(competitor).encode()
    competitor_key = storage._key('sample', run_id, competitor_name)
    original_put = s3.put_object
    observed = []

    def concurrent_put(**kwargs):
        if not observed:
            # Preflight has already reported no manifest; an independent writer commits now.
            assert manifest_key not in s3.objects
            s3.objects[competitor_key] = competitor_body
            s3.objects[manifest_key] = competitor_manifest
        observed.append(kwargs['Key'])
        return original_put(**kwargs)

    s3.put_object = concurrent_put
    with pytest.raises(HistoryRestoreError):
        restore_missing_history('sample', local, storage)

    assert observed == [storage._key('sample', run_id, name) for name in sorted(files)] + [manifest_key]
    assert s3.objects[manifest_key] == competitor_manifest
    assert s3.objects[competitor_key] == competitor_body
    assert {name: s3.objects[storage._key('sample', run_id, name)] for name in files} == files
    source = RunSource('sample', storage=storage)
    assert source.history() == [competitor]
    assert source.load(competitor) == {competitor_name: competitor_body}
    retained = dict(s3.objects)
    observed.clear()
    with pytest.raises(HistoryRestoreError, match='different immutable manifest'):
        restore_missing_history('sample', local, storage)
    assert observed == [] and s3.objects == retained


def test_corrupt_artifact_blocks_all_writes_for_its_run(history):
    manifest, files, local, directory, s3, storage = history
    (directory / next(iter(files))).write_bytes(b'changed')

    with pytest.raises(HistoryRestoreError, match='Could not restore'):
        restore_missing_history('sample', local, storage)

    assert s3.objects == {}


def test_cancellation_leaves_uncommitted_partial_upload_retryable(history):
    manifest, files, local, _, s3, storage = history
    cancel = lambda: len(s3.objects) == 1

    with pytest.raises(HistoryRestoreCancelled):
        restore_missing_history('sample', local, storage, cancelled=cancel)

    assert len(s3.objects) == 1
    assert storage._key('sample', manifest['run_id'], 'manifest.json') not in s3.objects
    assert restore_missing_history('sample', local, storage) == 1
    assert len(s3.objects) == len(files) + 1


def test_run_limit_and_manifest_identity_fail_before_writes(history):
    manifest, files, local, directory, s3, storage = history
    another = dict(manifest, run_id='d8d991e8-5ca6-4740-8c5a-eabfeacb4681')
    save(local, another, files)
    with pytest.raises(HistoryRestoreError, match='run limit'):
        restore_missing_history('sample', local, storage, max_runs=1)
    assert s3.objects == {}
    (directory / 'manifest.json').write_text(json.dumps(dict(manifest, run_id=another['run_id'])))
    with pytest.raises(HistoryRestoreError, match='identity'):
        restore_missing_history('sample', local, storage)
    assert not any(f'/{manifest["run_id"]}/' in key for key in s3.objects)


@pytest.mark.parametrize('replacement', ['symlink', 'fifo'])
def test_nonregular_artifact_is_rejected_without_storage_write(history, tmp_path, replacement):
    manifest, files, local, directory, s3, storage = history
    artifact = directory / next(iter(files))
    artifact.unlink()
    if replacement == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(b'outside')
        artifact.symlink_to(outside)
    else:
        os.mkfifo(artifact)

    with pytest.raises(HistoryRestoreError, match='Could not restore'):
        restore_missing_history('sample', local, storage)

    assert s3.objects == {}


def test_uploaded_bytes_remain_frozen_if_local_file_changes_during_transfer(history, monkeypatch):
    manifest, files, local, directory, s3, storage = history
    original = storage._put_once
    first = True
    later_artifact = sorted(files)[-1]
    def changed_after_first_write(key, body):
        nonlocal first
        original(key, body)
        if first:
            first = False
            (directory / later_artifact).write_bytes(b'changed after validation')
    monkeypatch.setattr(storage, '_put_once', changed_after_first_write)

    assert restore_missing_history('sample', local, storage) == 1

    assert {name: s3.objects[storage._key('sample', manifest['run_id'], name)] for name in files} == files


def test_late_manifest_is_skipped_until_finalized(history):
    manifest, files, local, _, s3, storage = history
    pending = local / 'sample' / '9f16f1cc-6cc4-48e6-b469-78659c6e55e5'
    pending.mkdir()
    progress = []

    assert restore_missing_history('sample', local, storage,
        progress=lambda done, total: progress.append((done, total))) == 1

    assert progress == [(0, 2), (1, 2), (2, 2)]
    assert len(s3.objects) == len(files) + 1


def test_artifact_read_limit_blocks_upload(history, monkeypatch):
    manifest, files, local, _, s3, storage = history
    monkeypatch.setattr(history_restore, 'MAX_ARTIFACT', 1)

    with pytest.raises(HistoryRestoreError, match='Could not restore'):
        restore_missing_history('sample', local, storage)

    assert s3.objects == {}


def test_explicit_tui_action_reuses_reporting_session_and_refreshes_primary(history, monkeypatch):
    manifest, files, local, _, s3, storage = history
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    events = []
    class Viewer:
        async def show_archive(self, source):
            events.append(('archive', source.storage is storage))
            return SimpleNamespace(url='http://127.0.0.1:5252/archive.html')
    class Workbench(AllureNavigation):
        def __init__(self):
            self.initialize_allure()
            self.context = SimpleNamespace(owner_root=local)
            values = {'ALLURE_SECONDARY_DIR': str(local), 'ALLURE_S3_BUCKET': 'selected-bucket'}
            self.session = SimpleNamespace(result=lambda: SimpleNamespace(values=values))
            self.operation_status = None
        def refresh_allure_loading(self):
            events.append(('progress', self._allure_loading))
        async def prepare_allure_reporting(self, *, for_restore=False):
            events.append(('reporting', for_restore))
        def allure_source(self, *, primary=False):
            events.append(('source', primary))
            return SimpleNamespace(storage=storage)
        def _allure(self):
            return Viewer()
        def call_from_thread(self, callback, *args):
            callback(*args)
        def show_allure_location(self, location):
            events.append(('location', location.url))
        def notify(self, message, **kwargs):
            events.append(('notice', message))

    app = Workbench()
    asyncio.run(app.restore_allure_secondary_history())

    assert ('reporting', True) in events
    assert ('source', True) in events
    assert ('archive', True) in events
    assert ('location', 'http://127.0.0.1:5252/archive.html') in events
    assert app.operation_status == 0 and app._allure_opening is False
    assert len(s3.objects) == len(files) + 1


def test_active_suite_lease_refuses_restore_before_any_s3_write(history):
    manifest, files, local, _, s3, storage = history

    with suite_lock_module.suite_lock(), ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(restore_missing_history, 'sample', local, storage)
        with pytest.raises(suite_lock_module.SuiteBusyError):
            operation.result()

    assert s3.objects == {}


@pytest.mark.parametrize('code,expected', [('NoSuchKey', True), ('404', True), ('AccessDenied', False)])
def test_destination_lookup_classifies_only_missing_keys_as_safe(history, code, expected):
    manifest, files, local, _, s3, storage = history
    def denied(**kwargs):
        raise ClientError({'Error': {'Code': code}}, 'GetObject')
    s3.get_object = denied

    if expected:
        assert restore_missing_history('sample', local, storage) == 1
    else:
        with pytest.raises(HistoryRestoreError, match='inspect the destination'):
            restore_missing_history('sample', local, storage)
        assert s3.objects == {}


def test_missing_repo_has_no_history_and_configuration_is_required(history):
    manifest, files, local, _, s3, storage = history
    progress = []
    assert restore_missing_history('absent', local, storage,
        progress=lambda done, total: progress.append((done, total))) == 0
    assert progress == [(0, 0)]
    with pytest.raises(HistoryRestoreError, match='Select secondary'):
        restore_missing_history('sample', None, storage)
    with pytest.raises(HistoryRestoreError, match='Select secondary'):
        restore_missing_history('sample', local, object())
    with pytest.raises(ValueError, match='run limit'):
        restore_missing_history('sample', local, storage, max_runs=0)


def test_scan_refuses_symlink_and_excess_unfinalized_entries(history):
    manifest, files, local, _, s3, storage = history
    repo = local / 'sample'
    (repo / 'link').symlink_to(repo / manifest['run_id'])
    with pytest.raises(HistoryRestoreError, match='Symlinked'):
        restore_missing_history('sample', local, storage)
    (repo / 'link').unlink()
    for number in range(4):
        (repo / f'unfinalized-{number}').write_text('')
    with pytest.raises(HistoryRestoreError, match='entry limit'):
        restore_missing_history('sample', local, storage, max_runs=1)
    assert s3.objects == {}


def test_oversized_manifest_fails_before_s3(history, monkeypatch):
    manifest, files, local, _, s3, storage = history
    monkeypatch.setattr(history_restore, 'MAX_MANIFEST', 1)
    with pytest.raises(HistoryRestoreError, match='manifest exceeds'):
        restore_missing_history('sample', local, storage)
    assert s3.objects == {}


@pytest.mark.parametrize('phase', ['scan', 'run', 'manifest'])
def test_cancellation_at_each_boundary_preserves_uncommitted_state(history, phase):
    manifest, files, local, _, s3, storage = history
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        if phase == 'scan':
            return calls == 1
        if phase == 'run':
            return calls == 2
        return len(s3.objects) == len(files)
    with pytest.raises(HistoryRestoreCancelled):
        restore_missing_history('sample', local, storage, cancelled=cancelled)
    assert storage._key('sample', manifest['run_id'], 'manifest.json') not in s3.objects


@pytest.mark.parametrize('expiration', [1, 2, 3, 5])
def test_timeout_at_scan_run_artifact_or_manifest_boundary(history, monkeypatch, expiration):
    manifest, files, local, _, s3, storage = history
    class Budget:
        checks = 0
        @property
        def remaining(self):
            self.checks += 1
            return 0 if self.checks == expiration else 120
    @contextmanager
    def budget(name, seconds):
        yield Budget()
    monkeypatch.setattr(history_restore, 'report_timeout_sync', budget)
    with pytest.raises(HistoryRestoreError, match='timed out'):
        restore_missing_history('sample', local, storage)
    assert storage._key('sample', manifest['run_id'], 'manifest.json') not in s3.objects


@pytest.mark.parametrize('failure', [
    CredentialRetrievalError(provider='synthetic', error_msg='synthetic-secret'),
    EndpointConnectionError(endpoint_url='https://synthetic-secret.invalid'),
])
def test_sdk_destination_failures_never_expose_details(history, failure):
    manifest, files, local, _, s3, storage = history
    def broken(**kwargs):
        raise failure
    s3.get_object = broken

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage)

    assert 'synthetic-secret' not in str(caught.value)
    assert s3.objects == {}


def test_sdk_stream_failure_closes_body_and_sanitizes_error(history):
    manifest, files, local, _, s3, storage = history
    class Stream:
        closed = False
        def read(self, size):
            raise EndpointConnectionError(endpoint_url='https://synthetic-secret.invalid')
        def close(self):
            self.closed = True
    stream = Stream()
    s3.get_object = lambda **kwargs: {'Body': stream}

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage)

    assert stream.closed and 'synthetic-secret' not in str(caught.value)
    assert s3.objects == {}


@pytest.mark.parametrize('expiry', [False, True])
def test_cancel_or_expire_during_artifact_verification_prevents_destination_get(history, monkeypatch, expiry):
    manifest, files, local, _, s3, storage = history
    state = {'stopped': False, 'reads': 0, 'gets': 0}
    original = RunStorage._read_source
    def read(path, **kwargs):
        body = original(path, **kwargs)
        if path != 'manifest.json':
            state['reads'] += 1
            state['stopped'] = True
        return body
    monkeypatch.setattr(RunStorage, '_read_source', staticmethod(read))
    class Budget:
        @property
        def remaining(self):
            return 0 if expiry and state['stopped'] else 120
    @contextmanager
    def budget(name, seconds):
        yield Budget()
    if expiry:
        monkeypatch.setattr(history_restore, 'report_timeout_sync', budget)
    expected = HistoryRestoreError if expiry else HistoryRestoreCancelled

    with pytest.raises(expected):
        restore_missing_history('sample', local, storage,
            cancelled=lambda: state['stopped'] and not expiry)

    assert state['reads'] == 1 and state['gets'] == 0 and s3.objects == {}


@pytest.mark.parametrize('expiry', [False, True])
def test_cancel_or_expire_after_full_verification_prevents_destination_get(history, monkeypatch, expiry):
    manifest, files, local, _, s3, storage = history
    state = {'stopped': False, 'gets': 0}
    verified = history_restore.verified_files
    def stop_after_verification(manifest, read):
        files = verified(manifest, read)
        state['stopped'] = True
        return files
    monkeypatch.setattr(history_restore, 'verified_files', stop_after_verification)
    original_get = s3.get_object
    def observed_get(**kwargs):
        state['gets'] += 1
        return original_get(**kwargs)
    s3.get_object = observed_get
    class Budget:
        @property
        def remaining(self):
            return 0 if expiry and state['stopped'] else 120
    @contextmanager
    def budget(name, seconds):
        yield Budget()
    if expiry:
        monkeypatch.setattr(history_restore, 'report_timeout_sync', budget)
    expected = HistoryRestoreError if expiry else HistoryRestoreCancelled

    with pytest.raises(expected):
        restore_missing_history('sample', local, storage,
            cancelled=lambda: state['stopped'] and not expiry)

    assert state['gets'] == 0 and s3.objects == {}


def test_sdk_write_and_reconciliation_failure_is_sanitized(history):
    manifest, files, local, _, s3, storage = history
    calls = 0
    def missing_then_failed(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError(kwargs['Key'])
        raise CredentialRetrievalError(provider='synthetic', error_msg='synthetic-secret')
    s3.get_object = missing_then_failed
    def failed_put(**kwargs):
        raise EndpointConnectionError(endpoint_url='https://synthetic-secret.invalid')
    s3.put_object = failed_put

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage)

    assert 'synthetic-secret' not in str(caught.value)
    assert s3.objects == {}


@pytest.mark.parametrize('expiry', [False, True])
def test_cancel_or_expire_during_client_setup_prevents_destination_get(history, monkeypatch, expiry):
    manifest, files, local, _, s3, storage = history
    state = {'stopped': False, 'gets': 0}
    original_client = storage._client
    def setup():
        state['stopped'] = True
        return original_client()
    monkeypatch.setattr(storage, '_client', setup)
    original_get = s3.get_object
    def observed_get(**kwargs):
        state['gets'] += 1
        return original_get(**kwargs)
    s3.get_object = observed_get
    class Budget:
        @property
        def remaining(self):
            return 0 if expiry and state['stopped'] else 120
    @contextmanager
    def budget(name, seconds):
        yield Budget()
    if expiry:
        monkeypatch.setattr(history_restore, 'report_timeout_sync', budget)
    expected = HistoryRestoreError if expiry else HistoryRestoreCancelled

    with pytest.raises(expected):
        restore_missing_history('sample', local, storage,
            cancelled=lambda: state['stopped'] and not expiry)

    assert state['gets'] == 0 and s3.objects == {}


class RestoreHarness(AllureNavigation):
    def __init__(self, local, storage, *, configured=True):
        self.initialize_allure()
        self.context = SimpleNamespace(owner_root=local)
        values = {'ALLURE_SECONDARY_DIR': str(local), 'ALLURE_S3_BUCKET': 'selected-bucket'} if configured else {}
        self.session = SimpleNamespace(result=lambda: SimpleNamespace(values=values))
        self.storage = storage
        self.events = []
        self.operation_status = None
        self.page = None
        self.prepare = None

    def refresh_allure_loading(self):
        self.events.append(('progress', self._allure_loading))

    async def prepare_allure_reporting(self, *, for_restore=False):
        self.events.append(('prepare', for_restore))
        if self.prepare is not None:
            await self.prepare()

    def allure_source(self, *, primary=False):
        self.events.append(('source', primary))
        return SimpleNamespace(storage=self.storage)

    def _allure(self):
        async def show_archive(source):
            self.events.append(('archive', source.storage is self.storage))
            return SimpleNamespace(url='http://127.0.0.1:5252/archive.html')
        return SimpleNamespace(show_archive=show_archive)

    def call_from_thread(self, callback, *args):
        callback(*args)

    def show_allure_location(self, location):
        self.events.append(('location', location.url))

    def notify(self, message, **kwargs):
        self.events.append(('notice', message, kwargs))

    def allure_page(self):
        return self.page


def test_restore_tui_busy_and_missing_config_do_not_start_reporting(history):
    manifest, files, local, _, s3, storage = history
    busy = RestoreHarness(local, storage)
    busy._allure_opening = True
    asyncio.run(busy.restore_allure_secondary_history())
    assert busy.events == [] and s3.objects == {}

    absent = RestoreHarness(local, storage, configured=False)
    asyncio.run(absent.restore_allure_secondary_history())
    assert absent.events == [('notice', 'Configure secondary history and an S3 bucket before restoring.',
                              {'severity': 'error', 'timeout': 10})]
    assert s3.objects == {}


def test_restore_tui_stale_generation_during_reporting_skips_upload(history):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    async def supersede():
        app.stop_allure_admission()
    app.prepare = supersede

    asyncio.run(app.restore_allure_secondary_history())

    assert ('prepare', True) in app.events
    assert not any(kind == 'source' for kind, *_ in app.events)
    assert s3.objects == {}


@pytest.mark.parametrize('stale', [False, True])
def test_restore_tui_generation_after_worker_controls_archive_refresh(history, monkeypatch, stale):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    original = history_restore.restore_missing_history
    def run(*args, **kwargs):
        count = original(*args, **kwargs)
        if stale:
            app._allure_generation = object()
        return count
    monkeypatch.setattr(history_restore, 'restore_missing_history', run)

    asyncio.run(app.restore_allure_secondary_history())

    assert len(s3.objects) == len(files) + 1
    assert any(kind == 'archive' for kind, *_ in app.events) is not stale
    assert any(kind == 'location' for kind, *_ in app.events) is not stale


@pytest.mark.parametrize('page_visible', [False, True])
@pytest.mark.parametrize('stale', [False, True])
def test_restore_tui_safe_error_targets_current_page_or_notice(history, monkeypatch, page_visible, stale):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    displayed = []
    if page_visible:
        app.page = SimpleNamespace(query_one=lambda selector, widget: SimpleNamespace(update=displayed.append))
    def fail(*args, **kwargs):
        if stale:
            app._allure_generation = object()
        raise HistoryRestoreError('Allure history restore timed out.')
    monkeypatch.setattr(history_restore, 'restore_missing_history', fail)

    asyncio.run(app.restore_allure_secondary_history())

    if stale:
        assert app.operation_status is None and displayed == []
        assert not any(kind == 'notice' for kind, *_ in app.events)
    else:
        assert app.operation_status == 3
        if page_visible:
            assert displayed == ['Allure history restore timed out.']
        else:
            assert ('notice', 'Allure history restore timed out.',
                    {'severity': 'error', 'timeout': 10}) in app.events
    assert s3.objects == {}


def test_restore_tui_task_cancellation_waits_for_owned_worker(history, monkeypatch):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    started, finished = threading.Event(), threading.Event()
    def work(repo, directory, destination, *, progress, cancelled):
        started.set()
        assert cancelled() is False
        assert started.wait(2)
        while not cancelled():
            threading.Event().wait(.01)
        finished.set()
        raise HistoryRestoreCancelled('Allure history restore was cancelled.')
    monkeypatch.setattr(history_restore, 'restore_missing_history', work)

    async def scenario():
        task = asyncio.create_task(app.restore_allure_secondary_history())
        while not started.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set() and app._allure_tasks == set()
    asyncio.run(scenario())

    assert s3.objects == {}


def test_restore_progress_ignores_stale_generation(history):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    app._restore_allure_progress(object(), 1, 2)
    assert app.events == [] and app._allure_loading == ''
    app._restore_allure_progress(app._allure_generation, 1, 2)
    assert app._allure_loading == 'Restoring secondary history: 1/2 finalized runs'


@pytest.mark.parametrize('phase', ['get', 'read', 'put', 'progress'])
def test_unexpected_adapter_failures_are_safe_and_do_not_commit(history, monkeypatch, phase):
    manifest, files, local, _, s3, storage = history
    secret = 'synthetic-secret'
    if phase == 'get':
        s3.get_object = lambda **kwargs: (_ for _ in ()).throw(RuntimeError(secret))
    elif phase == 'read':
        original = RunStorage._read_source
        def fail_after_manifest(path, **kwargs):
            if path != 'manifest.json':
                raise RuntimeError(secret)
            return original(path, **kwargs)
        monkeypatch.setattr(RunStorage, '_read_source', staticmethod(fail_after_manifest))
    elif phase == 'put':
        monkeypatch.setattr(storage, '_put_once', lambda key, body: (_ for _ in ()).throw(RuntimeError(secret)))
    else:
        def progress(done, total):
            raise RuntimeError(secret)

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage,
            progress=progress if phase == 'progress' else lambda done, total: None)

    assert secret not in str(caught.value)
    assert s3.objects == {}


@pytest.mark.parametrize('phase', ['prepare', 'storage', 'viewer'])
def test_mounted_tui_masks_unexpected_failures(history, tmp_path, monkeypatch, phase):
    manifest, files, local, _, s3, storage = history
    secret = 'synthetic-secret'
    app = fixture(tmp_path, fields={
        'ALLURE_SECONDARY_DIR': {'type': 'str', 'default': str(local)},
        'ALLURE_S3_BUCKET': {'type': 'str', 'default': 'selected-bucket'},
    })
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    async def prepared(*, for_restore=False):
        if phase == 'prepare':
            raise RuntimeError(secret)
    monkeypatch.setattr(app, 'prepare_allure_reporting', prepared)
    monkeypatch.setattr(app, 'allure_source', lambda *, primary=False: SimpleNamespace(storage=storage))
    if phase == 'storage':
        s3.get_object = lambda **kwargs: (_ for _ in ()).throw(RuntimeError(secret))
    if phase == 'viewer':
        async def failed_view(source):
            raise RuntimeError(secret)
        monkeypatch.setattr(app, '_allure', lambda: SimpleNamespace(show_archive=failed_view))

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.show('allure', 'Allure history', 'Ready', (('refresh-allure', 'Refresh history'),))
            await pilot.pause()
            await app.restore_allure_secondary_history()
            await pilot.pause()
            shown = str(app.screen.query_one('#error', Static).render())
            expected = ('Could not restore finalized Allure history; check secondary files and S3 storage.'
                        if phase == 'storage' else 'Could not restore Allure history; check reporting setup and storage.')
            assert expected in shown
            assert secret not in shown and secret not in app._allure_loading
            assert app.operation_status == 3
    asyncio.run(scenario())

    if phase == 'viewer':
        assert len(s3.objects) == len(files) + 1
    else:
        assert s3.objects == {}


def test_restore_tui_masks_unexpected_configuration_read(history):
    manifest, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    def failed_result():
        raise RuntimeError('synthetic-secret')
    app.session = SimpleNamespace(result=failed_result)

    asyncio.run(app.restore_allure_secondary_history())

    assert ('notice', 'Could not restore Allure history; check reporting setup and storage.',
            {'severity': 'error', 'timeout': 10}) in app.events
    assert s3.objects == {}


class RestoreInterruption(BaseException):
    """Controlled interruption outside ordinary adapter failures."""


def test_restore_interruption_preserves_identity_and_releases_suite_lease(history):
    _, _, local, _, s3, storage = history
    interruption = RestoreInterruption('synthetic restore interruption')
    def interrupted_progress(done, total):
        raise interruption

    with pytest.raises(RestoreInterruption) as caught:
        restore_missing_history('sample', local, storage, progress=interrupted_progress)

    assert caught.value is interruption
    assert s3.objects == {}
    with suite_lock_module.suite_lock():
        pass


def test_restore_tui_prepare_interruption_preserves_owned_state(history):
    _, _, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    interruption = RestoreInterruption('synthetic reporting interruption')
    async def interrupted_prepare():
        raise interruption
    app.prepare = interrupted_prepare

    with pytest.raises(RestoreInterruption) as caught:
        asyncio.run(app.restore_allure_secondary_history())

    assert caught.value is interruption
    assert app._allure_tasks == set()
    assert not app._allure_opening and app._allure_loading == ''
    assert not any(event[0] == 'notice' for event in app.events)
    assert s3.objects == {}


def test_restore_tui_configuration_interruption_preserves_owned_state(history):
    _, _, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    interruption = RestoreInterruption('synthetic configuration interruption')
    def interrupted_result():
        raise interruption
    app.session = SimpleNamespace(result=interrupted_result)

    with pytest.raises(RestoreInterruption) as caught:
        asyncio.run(app.restore_allure_secondary_history())

    assert caught.value is interruption
    assert app._allure_tasks == set()
    assert not app._allure_opening and app._allure_loading == ''
    assert app.events == [] and s3.objects == {}


@pytest.mark.parametrize('error_type', [HistoryRestoreError, HistoryRestoreCancelled,
                                        suite_lock_module.SuiteBusyError])
def test_forged_typed_progress_failure_is_normalized(history, error_type):
    _, _, local, _, s3, storage = history
    def progress(done, total):
        raise error_type('synthetic-secret')

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage, progress=progress)

    assert type(caught.value) is HistoryRestoreError
    assert str(caught.value) == 'Could not restore finalized Allure history; check secondary files and S3 storage.'
    assert s3.objects == {}
    with suite_lock_module.suite_lock():
        pass


@pytest.mark.parametrize('error_type', [HistoryRestoreError, HistoryRestoreCancelled,
                                        suite_lock_module.SuiteBusyError])
def test_forged_typed_tui_prepare_failure_never_displays_callback_text(history, error_type):
    _, _, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    async def failed_prepare():
        raise error_type('synthetic-secret')
    app.prepare = failed_prepare

    asyncio.run(app.restore_allure_secondary_history())

    assert ('notice', 'Could not restore Allure history; check reporting setup and storage.',
            {'severity': 'error', 'timeout': 10}) in app.events
    assert app._allure_tasks == set() and not app._allure_opening
    assert app._allure_loading == '' and s3.objects == {}


def test_typed_error_with_untrusted_args_is_never_stringified(history):
    _, _, local, _, s3, storage = history
    class SecretArg:
        def __str__(self):
            raise AssertionError('untrusted __str__ called')
    with pytest.raises(HistoryRestoreError, match='Could not restore finalized'):
        restore_missing_history('sample', local, storage,
            progress=lambda done, total: (_ for _ in ()).throw(HistoryRestoreError(SecretArg())))
    assert s3.objects == {}


def test_typed_error_with_untrusted_cause_is_rebuilt(history):
    _, _, local, _, s3, storage = history
    def progress(done, total):
        try:
            raise RuntimeError('synthetic-secret')
        except RuntimeError as cause:
            raise HistoryRestoreError('Allure history restore timed out.') from cause

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage, progress=progress)

    assert str(caught.value) == 'Allure history restore timed out.'
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert s3.objects == {}


def test_typed_error_with_allowed_message_discards_diagnostic_notes(history):
    _, _, local, _, s3, storage = history
    def progress(done, total):
        error = HistoryRestoreError('Allure history restore timed out.')
        error.add_note('synthetic-secret')
        raise error

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage, progress=progress)

    assert str(caught.value) == 'Allure history restore timed out.'
    assert not getattr(caught.value, '__notes__', None)
    assert 'synthetic-secret' not in ''.join(__import__('traceback').format_exception(caught.value))
    assert s3.objects == {}


def test_forged_typed_error_discards_untrusted_cause_and_notes(history):
    _, _, local, _, s3, storage = history
    def progress(done, total):
        try:
            raise RuntimeError('synthetic-secret-cause')
        except RuntimeError as cause:
            error = suite_lock_module.SuiteBusyError('synthetic-secret-message')
            error.add_note('synthetic-secret-note')
            raise error from cause

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage, progress=progress)

    assert type(caught.value) is HistoryRestoreError
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert not getattr(caught.value, '__notes__', None)
    rendered = ''.join(__import__('traceback').format_exception(caught.value))
    assert 'synthetic-secret' not in rendered
    assert s3.objects == {}


def test_unknown_exception_metaclass_is_not_executed_by_restore_filter(history):
    _, _, local, _, s3, storage = history
    class HostileMeta(type):
        def __hash__(cls):
            raise RuntimeError('synthetic-secret-metaclass')
    class HostileError(HistoryRestoreError, metaclass=HostileMeta):
        pass
    secret = 'synthetic-secret'

    with pytest.raises(HistoryRestoreError) as caught:
        restore_missing_history('sample', local, storage,
            progress=lambda done, total: (_ for _ in ()).throw(HostileError(secret)))

    assert type(caught.value) is HistoryRestoreError
    assert 'synthetic-secret' not in ''.join(__import__('traceback').format_exception(caught.value))
    assert s3.objects == {}


@pytest.mark.parametrize('failure', [RuntimeError('synthetic-secret'),
                                     ViewerError('synthetic-secret'),
                                     ViewerError('No finalized integration or E2E Allure runs are available')])
def test_history_open_displays_only_known_viewer_messages(history, tmp_path, monkeypatch, failure):
    _, _, local, _, s3, storage = history
    app = fixture(tmp_path, fields={'ALLURE_RESULTS_DIR': {'type': 'str', 'default': str(local)}})
    async def failed_setup():
        raise failure
    monkeypatch.setattr(app, 'prepare_allure_reporting', failed_setup)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await app.show_allure_history()
            await pilot.pause()
            shown = str(app.screen.query_one('#error', Static).render())
            expected = ('No finalized integration or E2E Allure runs are available'
                        if failure.args[0].startswith('No finalized')
                        else 'Could not open Allure history; check reporting setup and storage.')
            assert expected in shown
            assert 'synthetic-secret' not in shown
            assert app.operation_status == 3 and not app._allure_opening
    asyncio.run(scenario())
    assert s3.objects == {}


@pytest.mark.parametrize(('message', 'expected'), [
    ('Allure 2 CLI command is missing from PATH; configure ALLURE_CLI or install with brew install allure; see https://allurereport.org/docs/v2/install/',
     'Allure 2 CLI command is missing from PATH; configure ALLURE_CLI or install with brew install allure; see https://allurereport.org/docs/v2/install/'),
    ('Allure viewer port 16686 is occupied; stop its owner or configure ALLURE_VIEWER_PORT',
     'Allure viewer port 16686 is occupied; stop its owner or configure ALLURE_VIEWER_PORT'),
    ('Allure command failed (exit 127); check ALLURE_CLI, its host PATH and Java installation',
     'Allure command failed; check ALLURE_CLI, its host PATH and Java installation'),
    ('Allure viewer port 16686 is occupied; stop its owner or configure ALLURE_VIEWER_PORT synthetic-secret', None),
    ('Allure command failed (exit 127); check ALLURE_CLI, its host PATH and Java installation synthetic-secret', None),
    ('Allure viewer port 00000 is occupied; stop its owner or configure ALLURE_VIEWER_PORT', None),
    ('Allure viewer port 65536 is occupied; stop its owner or configure ALLURE_VIEWER_PORT', None),
])
def test_history_viewer_guidance_is_structured_and_bounded(message, expected):
    assert _known_history_viewer_message(ViewerError(message)) == expected


@pytest.mark.parametrize('message', [
    'The exact selected Allure result is unavailable',
    'Allure did not preserve the exact selected attempt',
    'Allure generation produced no report',
    'Allure generated an invalid selected-result link',
])
def test_selected_viewer_retains_only_fixed_diagnostics(message):
    assert _known_history_viewer_message(ViewerError(message)) == message
    assert _known_history_viewer_message(ViewerError(message + ' synthetic-secret')) is None


@pytest.mark.parametrize(('failure', 'expected'), [
    (RuntimeError('synthetic-secret'), 'Could not open Allure history; check reporting setup and storage.'),
    (ViewerError('synthetic-secret'), 'Could not open Allure history; check reporting setup and storage.'),
    (ViewerError('Allure could not start; install Allure 2 and check Java on PATH'),
     'Allure could not start; install Allure 2 and check Java on PATH'),
])
def test_selected_report_displays_only_controlled_errors(history, tmp_path, monkeypatch, failure, expected):
    _, _, local, _, s3, storage = history
    app = fixture(tmp_path, fields={'ALLURE_RESULTS_DIR': {'type': 'str', 'default': str(local)}})
    monkeypatch.setattr(app, 'selected_allure_result', lambda screen: (object(), object()))
    async def failed_setup():
        raise failure
    monkeypatch.setattr(app, 'prepare_allure_reporting', failed_setup)
    notices = []
    monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append((message, kwargs)))

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await app.open_selected_allure()
            assert (expected, {'severity': 'error', 'timeout': 10}) in notices
            assert all('synthetic-secret' not in message for message, _ in notices)
            assert not app._allure_opening and app._allure_loading == ''
    asyncio.run(scenario())
    assert s3.objects == {}


def test_restore_refresh_preserves_known_viewer_setup_guidance(history, monkeypatch):
    _, files, local, _, s3, storage = history
    app = RestoreHarness(local, storage)
    monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda context, root: 'sample')
    async def failed_archive(source):
        raise ViewerError('Allure could not start; install Allure 2 and check Java on PATH')
    app._allure = lambda: SimpleNamespace(show_archive=failed_archive)

    asyncio.run(app.restore_allure_secondary_history())

    assert ('notice', 'Allure could not start; install Allure 2 and check Java on PATH',
            {'severity': 'error', 'timeout': 10}) in app.events
    assert len(s3.objects) == len(files) + 1
