"""Source, destination and transport cleanup retain all interrupted duties."""
import asyncio
from concurrent.futures import Future
import io
import json
import os
import stat
from threading import Event
from types import SimpleNamespace

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import integration, launched, storage as module, suite_lock
from api_agent.testing.inspection_controls import ReceiptInspection
from api_agent.testing.storage import RunStorage, StorageError
from test_recording_storage import independent_publication
from test_recorded_integration import fixture as recording_fixture
from test_launched_recording import owner


@pytest.fixture
def directory_close_faults(monkeypatch):
    state = SimpleNamespace(active=True, retained=[], starts=[])
    start = module._DescriptorCleanup._start_close
    def fail_directory(subject, ticket):
        if (state.active and not isinstance(subject, module._CopyCleanup) and
                stat.S_ISDIR(os.fstat(subject.descriptor).st_mode)):
            state.retained.append(subject)
            raise OSError('synthetic directory disposal launch failure')
        state.starts.append(subject)
        return start(subject, ticket)
    monkeypatch.setattr(module._DescriptorCleanup, '_start_close', fail_directory)
    yield state
    state.active = False
    for subject in state.retained:
        asyncio.run(subject.aclose())


def receipts(error):
    return tuple(suite_lock._cleanup_receipts(error))


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_secondary_interruption_survives_all_outer_directory_failures(
        independent_publication, directory_close_faults, monkeypatch, kind):
    publication = independent_publication
    original = kind()
    publication.primary_error = ConnectionRefusedError('synthetic outage')
    monkeypatch.setattr(os, 'link', lambda *a, **k: (_ for _ in ()).throw(original))

    with pytest.raises(kind) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 4
    assert len(directory_close_faults.retained) == 4
    assert not (publication.target / 'manifest.json').exists()
    for subject in directory_close_faults.retained:
        assert stat.S_ISDIR(os.fstat(subject.descriptor).st_mode)
    directory_close_faults.active = False
    asyncio.run(ReceiptInspection(original).aclose())
    assert all(subject.descriptor is None for subject in directory_close_faults.retained)


class ControlledBody:
    def __init__(self, content, *, read_error=None, close_error=None):
        self.content = content
        self.read_error = read_error
        self.close_error = close_error
        self.closed = False
        self.closes = 0
    def read(self, size=-1):
        if self.read_error is not None:
            raise self.read_error
        return self.content[:size]
    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


@pytest.mark.parametrize('kind', [KeyboardInterrupt, asyncio.CancelledError])
def test_s3_readback_interruption_survives_body_and_source_root_cleanup(
        independent_publication, directory_close_faults, kind):
    publication = independent_publication
    original = kind()
    body = ControlledBody(b'{}', read_error=original, close_error=OSError('synthetic body close failure'))
    class Client:
        def put_object(self, **request):
            raise OSError('synthetic immutable retry')
        def get_object(self, **request):
            return {'Body': body}
    publication.storage.client = Client()

    with pytest.raises(kind) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 2
    assert not publication.secondary.exists()
    body.close_error = None
    directory_close_faults.active = False
    asyncio.run(ReceiptInspection(original).aclose())
    assert body.closed and body.closes == 2
    assert directory_close_faults.retained[0].descriptor is None


def test_ordinary_inner_receipt_survives_multiple_outer_cleanup_failures(
        independent_publication, directory_close_faults, monkeypatch):
    publication = independent_publication
    publication.primary_error = OSError('synthetic outage')
    unlink = os.unlink
    def fail_temp_unlink(name, *args, **kwargs):
        if isinstance(name, str) and name.startswith('.allure-'):
            raise OSError('synthetic temp close failure')
        return unlink(name, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', fail_temp_unlink)

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert len(receipts(caught.value)) == 5
    assert len(directory_close_faults.retained) == 4
    monkeypatch.setattr(os, 'unlink', unlink)
    directory_close_faults.active = False
    asyncio.run(ReceiptInspection(caught.value).aclose())
    assert all(subject.descriptor is None for subject in directory_close_faults.retained)
    assert list(publication.target.glob('.allure-*')) == []
    assert not (publication.target / 'manifest.json').exists()


def test_source_file_read_and_nonowning_stream_close_failures_keep_both_duties(
        independent_publication, monkeypatch):
    publication = independent_publication
    original = KeyboardInterrupt()
    actual_fdopen = os.fdopen
    wrappers = []
    fail = SimpleNamespace(active=True)
    class Wrapper:
        def __init__(self, stream):
            self.stream = stream
        def read(self, size=-1):
            raise original
        def close(self):
            if fail.active:
                raise OSError('synthetic nonowning close failure')
            self.stream.close()
    def read_wrapper(fd, *args, **kwargs):
        assert kwargs.get('closefd') is False
        wrapper = Wrapper(actual_fdopen(fd, *args, **kwargs))
        wrappers.append(wrapper)
        return wrapper
    start = module._DescriptorCleanup._start_close
    pending_files = []
    def fail_file(subject, ticket):
        if fail.active and stat.S_ISREG(os.fstat(subject.descriptor).st_mode):
            pending_files.append(subject)
            raise OSError('synthetic read descriptor failure')
        return start(subject, ticket)
    monkeypatch.setattr(os, 'fdopen', read_wrapper)
    monkeypatch.setattr(module._DescriptorCleanup, '_start_close', fail_file)

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 2
    assert pending_files[0].descriptor is not None
    fail.active = False
    asyncio.run(ReceiptInspection(original).aclose())
    assert wrappers[0].stream.closed
    assert pending_files[0].descriptor is None
    assert not publication.secondary.exists()


def test_completed_outer_close_acknowledgement_never_recloses_reused_number(
        independent_publication, monkeypatch, tmp_path):
    publication = independent_publication
    original = KeyboardInterrupt()
    source_inode = publication.source.stat().st_ino
    owners = []
    class InterruptedAcknowledgement(Future):
        interrupted = False
        def result(self, *args, **kwargs):
            result = super().result(*args, **kwargs)
            if not self.interrupted:
                self.interrupted = True
                raise OSError('synthetic completed-close acknowledgement failure')
            return result
    ticket = module._DescriptorCleanup._close_ticket
    def source_ticket(subject):
        if os.fstat(subject.descriptor).st_ino == source_inode:
            owners.append(subject)
            return InterruptedAcknowledgement()
        return ticket(subject)
    monkeypatch.setattr(module._DescriptorCleanup, '_close_ticket', source_ticket)
    monkeypatch.setattr(os, 'link', lambda *a, **k: (_ for _ in ()).throw(original))

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 1
    subject = owners[0]
    descriptor = subject.descriptor
    assert subject.closing.done()
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'unrelated open file')
    opened = os.open(replacement, os.O_RDONLY)
    try:
        os.dup2(opened, descriptor)
        asyncio.run(ReceiptInspection(original).aclose())
        assert os.pread(descriptor, 100, 0) == b'unrelated open file'
        assert subject.descriptor is None
    finally:
        os.close(descriptor)
        if opened != descriptor:
            os.close(opened)


@pytest.mark.parametrize('operation', ['put-readback', 'history', 'accounting'])
def test_sdk_cleanup_receipts_bypass_every_storage_sanitizer(
        independent_publication, monkeypatch, operation):
    publication = independent_publication
    body = ControlledBody(b'{}', close_error=OSError('synthetic SDK close failure'))
    key = publication.storage._key('example', publication.manifest['run_id'], 'manifest.json')
    class Client:
        def put_object(self, **request):
            raise OSError('synthetic immutable retry')
        def get_object(self, **request):
            return {'Body': body}
        def list_objects_v2(self, **request):
            return {'Contents': [{'Key': key}]}
    publication.storage.client = Client()
    manifest = {'run_id': publication.manifest['run_id'], 'files': {'accounting.json': 'digest'}}
    if operation == 'accounting':
        monkeypatch.setattr(publication.storage, 'history', lambda *a, **k: [manifest])
    calls = {'put-readback': lambda: publication.storage.persist(publication.source, publication.manifest),
             'history': lambda: publication.storage.history('example'),
             'accounting': lambda: list(publication.storage.accounting_history('example'))}

    with pytest.raises(CleanupPendingError) as caught:
        calls[operation]()

    assert len(receipts(caught.value)) == 1
    assert not publication.secondary.exists()
    body.close_error = None
    asyncio.run(ReceiptInspection(caught.value).aclose())
    assert body.closed
    asyncio.run(caught.value.retry())


def test_direct_sdk_pending_put_is_not_mistaken_for_an_immutable_retry(independent_publication):
    publication = independent_publication
    events = []
    async def close_client():
        events.append('closed')
    pending = CleanupPendingError((), close_client)
    class Client:
        def put_object(self, **request):
            raise pending
        def get_object(self, **request):
            pytest.fail('A pending cleanup is not an ordinary put conflict')
    publication.storage.client = Client()

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is pending
    assert not publication.secondary.exists()
    asyncio.run(ReceiptInspection(pending).aclose())
    assert events == ['closed']


def test_copy_writer_close_cannot_mask_interrupted_write(independent_publication, monkeypatch):
    publication = independent_publication
    publication.primary_error = OSError('synthetic primary failure')
    original = KeyboardInterrupt()
    active = SimpleNamespace(failure=True)
    create = module._CopyCleanup.create
    streams = []
    class Writer:
        def __init__(self, stream):
            self.stream = stream
        def write(self, data):
            raise original
        def close(self):
            if active.failure:
                raise OSError('synthetic writer close failure')
            self.stream.close()
    def create_writer(subject):
        wrapper = Writer(create(subject))
        subject.stream = wrapper
        streams.append(wrapper)
        return wrapper
    monkeypatch.setattr(module._CopyCleanup, 'create', create_writer)

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 1
    assert not streams[0].stream.closed
    active.failure = False
    asyncio.run(ReceiptInspection(original).aclose())
    assert streams[0].stream.closed
    assert list(publication.target.glob('.allure-*')) == []
    assert not (publication.target / 'manifest.json').exists()


def test_existing_target_reader_close_and_descriptor_failure_keep_both_owners(independent_publication, monkeypatch):
    publication = independent_publication
    publication.primary_error = OSError('synthetic primary failure')
    publication.target.mkdir(parents=True)
    target = publication.target / 'a-result.json'
    target.write_bytes(b'{}')
    target_inode = target.stat().st_ino
    original = KeyboardInterrupt()
    active = SimpleNamespace(failure=True)
    raw_fdopen = os.fdopen
    wrappers, descriptors = [], []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            raise original
        def close(self):
            if active.failure:
                raise OSError('synthetic existing stream close failure')
            self.stream.close()
    def fdopen(fd, *args, **kwargs):
        stream = raw_fdopen(fd, *args, **kwargs)
        if os.fstat(fd).st_ino == target_inode:
            assert kwargs['closefd'] is False
            wrapper = Reader(stream)
            wrappers.append(wrapper)
            return wrapper
        return stream
    start = module._DescriptorCleanup._start_close
    def close_target(subject, ticket):
        if active.failure and os.fstat(subject.descriptor).st_ino == target_inode:
            descriptors.append(subject)
            raise OSError('synthetic existing descriptor failure')
        return start(subject, ticket)
    monkeypatch.setattr(os, 'fdopen', fdopen)
    monkeypatch.setattr(module._DescriptorCleanup, '_start_close', close_target)

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert len(receipts(original)) == 2
    active.failure = False
    asyncio.run(ReceiptInspection(original).aclose())
    assert wrappers[0].stream.closed
    assert descriptors[0].descriptor is None
    assert target.read_bytes() == b'{}'


def test_cleanup_interruption_retains_an_inner_direct_receipt(independent_publication, directory_close_faults):
    publication = independent_publication
    events = []
    async def earlier_cleanup():
        events.append('earlier')
    earlier = CleanupPendingError((), earlier_cleanup)
    interrupted = KeyboardInterrupt()
    body = ControlledBody(b'{}', read_error=earlier, close_error=interrupted)
    class Client:
        def put_object(self, **request):
            raise OSError('synthetic immutable conflict')
        def get_object(self, **request):
            return {'Body': body}
    publication.storage.client = Client()

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is interrupted
    assert any(pending is earlier for pending in receipts(interrupted))
    assert len(receipts(interrupted)) == 3
    body.close_error = None
    directory_close_faults.active = False
    asyncio.run(ReceiptInspection(interrupted).aclose())
    assert body.closed
    assert events == ['earlier']


def test_launched_tui_retry_keeps_all_outer_duties_and_suite_exclusion(
        tmp_path, monkeypatch, directory_close_faults):
    context, selected, config = recording_fixture(tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *a, **k: [selected])
    cancelled = Event()
    cancelled.set()
    original = KeyboardInterrupt()
    monkeypatch.setattr(os, 'link', lambda *a, **k: (_ for _ in ()).throw(original))
    class Client:
        def put_object(self, **request):
            raise ConnectionRefusedError('synthetic primary outage')
        def get_object(self, **request):
            raise ConnectionRefusedError('synthetic primary outage')
    storage = RunStorage('synthetic', client=Client(), secondary_directory=tmp_path / 'secondary')
    prerequisite = SimpleNamespace(ready=False, done=False)
    async def release():
        if not prerequisite.ready:
            raise pending
        prerequisite.done = True
    pending = CleanupPendingError((), release)
    prerequisites = owner([], failure=pending)

    with pytest.raises(KeyboardInterrupt) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites, storage=storage, cancel_event=cancelled)

    assert caught.value is original
    assert len(receipts(original)) == 5
    retained = next(handle for handle in original.inspections if isinstance(handle, ReceiptInspection))
    directory_close_faults.active = False
    with pytest.raises(CleanupPendingError):
        asyncio.run(retained.aclose())
    assert all(subject.descriptor is None for subject in directory_close_faults.retained)
    assert not prerequisite.done
    with pytest.raises(suite_lock.SuiteBusyError):
        with suite_lock.suite_lock():
            pytest.fail('The remaining prerequisite duty still owns exclusion')
    prerequisite.ready = True
    asyncio.run(retained.aclose())
    assert prerequisite.done
    with suite_lock.suite_lock():
        pass
    assert list((tmp_path / 'secondary').rglob('manifest.json')) == []


def test_source_artifact_cleanup_receipt_stops_before_transport_or_secondary(independent_publication, monkeypatch):
    publication = independent_publication
    inode = (publication.source / 'a-result.json').stat().st_ino
    active = SimpleNamespace(failure=True)
    retained = []
    start = module._DescriptorCleanup._start_close
    def refuse_source(subject, ticket):
        if active.failure and os.fstat(subject.descriptor).st_ino == inode:
            retained.append(subject)
            raise OSError('synthetic source artifact close failure')
        return start(subject, ticket)
    monkeypatch.setattr(module._DescriptorCleanup, '_start_close', refuse_source)
    try:
        with pytest.raises(CleanupPendingError) as caught:
            publication.storage.persist(publication.source, publication.manifest)
        assert len(receipts(caught.value)) == 1
        assert publication.attempted == []
        assert not publication.secondary.exists()
        active.failure = False
        asyncio.run(ReceiptInspection(caught.value).aclose())
        assert retained[0].descriptor is None
    finally:
        active.failure = False
        for subject in retained:
            asyncio.run(subject.aclose())


def test_verified_immutable_readback_still_requires_sdk_body_cleanup(independent_publication):
    publication = independent_publication
    body = ControlledBody(b'{}', close_error=OSError('synthetic SDK close failure'))
    class Client:
        def put_object(self, **request):
            raise OSError('synthetic existing object')
        def get_object(self, **request):
            return {'Body': body}
    publication.storage.client = Client()

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage._put_once('synthetic/object', b'{}')

    assert not body.closed
    assert len(receipts(caught.value)) == 1
    body.close_error = None
    asyncio.run(ReceiptInspection(caught.value).aclose())
    assert body.closed


def test_sdk_close_attached_child_remains_pending_through_consumer_retry(independent_publication):
    publication = independent_publication
    events = []
    state = SimpleNamespace(ready=False, closed=False)
    async def finish_child():
        events.append('child')
        if not state.ready:
            raise child
        state.closed = True
    child = CleanupPendingError((state,), finish_child)
    failure = RuntimeError('synthetic SDK close retains another duty')
    failure.cleanup_pending = child
    body = ControlledBody(b'{}', close_error=failure)
    class Client:
        def put_object(self, **request):
            raise OSError('synthetic immutable conflict')
        def get_object(self, **request):
            return {'Body': body}
    publication.storage.client = Client()

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert any(item is child for item in receipts(caught.value))
    consumer = ReceiptInspection(caught.value)
    body.close_error = None
    with pytest.raises(CleanupPendingError) as pending:
        asyncio.run(consumer.aclose())
    assert pending.value is child
    assert body.closed
    assert consumer.pending
    assert not state.closed
    state.ready = True
    asyncio.run(consumer.aclose())
    assert state.closed
    assert not consumer.pending
    assert events == ['child', 'child']
    assert not publication.target.exists()


@pytest.mark.parametrize('operation', ['put', 'readback', 'history', 'accounting', 'source', 'artifact-source', 'secondary', 'history-invalid', 'history-type', 'history-attribute'])
def test_attached_operation_receipts_are_sanitized_and_retained(independent_publication, monkeypatch, operation):
    publication = independent_publication
    state = SimpleNamespace(ready=False, done=False)
    async def finish():
        if not state.ready:
            raise child
        state.done = True
    child = CleanupPendingError((state,), finish)
    failure_type = {'history-invalid': ValueError, 'history-type': TypeError, 'history-attribute': AttributeError}.get(operation, RuntimeError)
    failure = failure_type('synthetic secret provider detail')
    failure.cleanup_pending = child
    def fail(*args, **kwargs):
        raise failure
    body = ControlledBody(b'{}', read_error=failure)
    key = publication.storage._key('example', publication.manifest['run_id'], 'manifest.json')
    class Client:
        def put_object(self, **request):
            if operation == 'put':
                raise failure
            raise OSError('synthetic conflict')
        def get_object(self, **request):
            if operation == 'put':
                pytest.fail('An owned transport failure cannot become an immutable retry')
            return {'Body': body}
        def list_objects_v2(self, **request):
            return {'Contents': [{'Key': key}]}
    publication.storage.client = Client()
    if operation == 'accounting':
        monkeypatch.setattr(publication.storage, 'history', lambda *a, **k: [{'run_id': publication.manifest['run_id'], 'files': {'accounting.json': 'digest'}}])
    if operation == 'source':
        monkeypatch.setattr(publication.storage, '_read_source', fail)
    if operation == 'artifact-source':
        read_source = publication.storage._read_source
        def read_artifact(name, **kwargs):
            return fail() if name == 'a-result.json' else read_source(name, **kwargs)
        monkeypatch.setattr(publication.storage, '_read_source', read_artifact)
    if operation == 'secondary':
        publication.storage.client = SimpleNamespace(put_object=lambda **k: None)
        monkeypatch.setattr(publication.storage, '_copy_once', fail)
    call = (lambda: publication.storage.history('example')) if operation in {'history', 'history-invalid', 'history-type', 'history-attribute'} else (
        (lambda: list(publication.storage.accounting_history('example'))) if operation == 'accounting' else
        (lambda: publication.storage.persist(publication.source, publication.manifest)))

    with pytest.raises(StorageError) as caught:
        call()

    assert 'secret' not in str(caught.value)
    assert any(item is child for item in receipts(caught.value))
    consumer = ReceiptInspection(caught.value)
    with pytest.raises(CleanupPendingError) as pending:
        asyncio.run(consumer.aclose())
    assert pending.value is child
    assert consumer.pending
    assert not state.done
    state.ready = True
    asyncio.run(consumer.aclose())
    assert state.done
    assert not consumer.pending
    assert not list(publication.secondary.rglob('manifest.json'))
