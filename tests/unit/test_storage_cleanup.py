"""Temporary-copy cleanup keeps interruption identity and real retry ownership."""
import asyncio
import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import integration, launched, suite_lock
from api_agent.testing.inspection_controls import ReceiptInspection
from api_agent.testing.storage import RunStorage, StorageError
from test_recording_storage import independent_publication, assert_complete_primary, assert_complete_secondary
from test_recorded_integration import fixture as recording_fixture
from test_launched_recording import owner


@pytest.fixture
def copy_faults(monkeypatch):
    state = SimpleNamespace(link_error=None, cleanup_error=None, retained=set(), names={})
    duplicate, close, link, unlink = os.dup, os.close, os.link, os.unlink
    close_range = os.closerange
    def owned_duplicate(fd):
        result = duplicate(fd)
        state.retained.add(result)
        return result
    def owned_close(fd):
        close(fd)
        state.retained.discard(fd)
    def acknowledged_close(low, high):
        close_range(low, high)
        state.retained.difference_update(range(low, high))
    def controlled_link(*args, **kwargs):
        if state.link_error is not None:
            raise state.link_error
        return link(*args, **kwargs)
    def controlled_unlink(name, *, dir_fd=None):
        if dir_fd in state.retained:
            state.names[dir_fd] = name
            if state.cleanup_error is not None:
                raise state.cleanup_error
        return unlink(name, dir_fd=dir_fd)
    monkeypatch.setattr(os, 'dup', owned_duplicate)
    monkeypatch.setattr(os, 'close', owned_close)
    monkeypatch.setattr(os, 'link', controlled_link)
    monkeypatch.setattr(os, 'closerange', acknowledged_close)
    monkeypatch.setattr(os, 'unlink', controlled_unlink)
    yield state
    # Test assertion failures must not leave the deliberately retained descriptor.
    for fd in tuple(state.retained):
        try:
            unlink(state.names[fd], dir_fd=fd)
        except FileNotFoundError:
            pass
        close(fd)
        state.retained.remove(fd)


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_combined_fault_preserves_interruption_and_pinned_retry(independent_publication, copy_faults, kind, tmp_path):
    publication, faults = independent_publication, copy_faults
    publication.primary_error = ConnectionRefusedError('synthetic primary outage')
    original = kind()
    faults.link_error = original
    faults.cleanup_error = OSError('synthetic cleanup failure')

    with pytest.raises(kind) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    receipt = original.cleanup_pending
    assert isinstance(receipt, CleanupPendingError)
    assert original.retained_owners == receipt.owners
    retained = receipt.owners[0]
    fd = retained.directory_fd
    assert faults.retained == {fd}
    temporary = retained.temporary
    assert os.stat(temporary, dir_fd=fd).st_size == 2
    assert publication.client.objects == {}
    assert not (publication.target / 'manifest.json').exists()
    relocated = tmp_path / 'relocated'
    publication.target.rename(relocated)
    publication.target.mkdir()
    (publication.target / temporary).write_bytes(b'unrelated replacement')
    (relocated / 'unrelated').write_bytes(b'preserve')
    faults.cleanup_error = None
    asyncio.run(receipt.retry())
    asyncio.run(receipt.retry())
    assert faults.retained == set()
    assert retained.directory_fd is None
    assert not (relocated / temporary).exists()
    assert (publication.target / temporary).read_bytes() == b'unrelated replacement'
    assert (relocated / 'unrelated').read_bytes() == b'preserve'
    with pytest.raises(OSError):
        os.fstat(fd)


def test_normal_cleanup_failure_is_a_retryable_receipt_then_publication_can_retry(independent_publication, copy_faults):
    publication, faults = independent_publication, copy_faults
    faults.cleanup_error = OSError('synthetic unlink failure')

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert_complete_primary(publication)
    receipt = caught.value
    assert str(receipt) == 'cleanup pending'
    assert not (publication.target / 'manifest.json').exists()
    with pytest.raises(CleanupPendingError) as retried:
        asyncio.run(receipt.retry())
    assert retried.value.owners == receipt.owners
    assert len(faults.retained) == 1
    faults.cleanup_error = None
    asyncio.run(receipt.retry())
    assert faults.retained == set()
    # Temporary cleanup alone does not assert publication success.
    assert not (publication.target / 'manifest.json').exists()
    publication.storage.persist(publication.source, publication.manifest)
    assert_complete_secondary(publication)


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_clean_interruption_removes_temporary_and_closes_duplicate(independent_publication, copy_faults, kind):
    publication, faults = independent_publication, copy_faults
    original = kind()
    faults.link_error = original

    with pytest.raises(kind) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert not hasattr(original, 'cleanup_pending')
    assert faults.retained == set()
    assert list(publication.target.glob('.allure-*')) == []


def test_cleanup_interruption_has_its_own_receipt(independent_publication, copy_faults):
    publication, faults = independent_publication, copy_faults
    original = KeyboardInterrupt()
    faults.cleanup_error = original

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert original.retained_owners == original.cleanup_pending.owners
    assert len(faults.retained) == 1
    faults.cleanup_error = None
    asyncio.run(original.cleanup_pending.retry())
    assert faults.retained == set()


def test_temporary_creation_failure_closes_pin_when_no_name_was_created(independent_publication, copy_faults, monkeypatch):
    publication, faults = independent_publication, copy_faults
    open_file = os.open
    def refuse_temporary(name, flags, *args, **kwargs):
        if isinstance(name, str) and name.startswith('.allure-'):
            raise OSError('synthetic creation failure')
        return open_file(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', refuse_temporary)

    with pytest.raises(StorageError):
        publication.storage.persist(publication.source, publication.manifest)

    assert faults.retained == set()
    assert list(publication.target.iterdir()) == []


@pytest.mark.parametrize('kind', [KeyboardInterrupt, asyncio.CancelledError])
def test_launched_boundary_retains_storage_and_prerequisite_duties(
        tmp_path, monkeypatch, copy_faults, kind):
    context, selected, config = recording_fixture(tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: [selected])
    monkeypatch.setattr(integration.subprocess, 'Popen', Mock(side_effect=AssertionError('No worker after cancellation')))
    cancelled = Event()
    cancelled.set()
    faults = copy_faults
    original = kind()
    faults.link_error = original
    faults.cleanup_error = OSError('synthetic unlink failure')
    events = []
    async def release_prerequisite():
        events.append('prerequisite-released')
    pending = CleanupPendingError((), release_prerequisite)
    prerequisites = owner([], failure=pending)
    class PrimaryUnavailable:
        def put_object(self, **kwargs):
            raise ConnectionRefusedError('synthetic outage')
        def get_object(self, **kwargs):
            raise ConnectionRefusedError('synthetic outage')
    storage = RunStorage('synthetic', client=PrimaryUnavailable(), secondary_directory=tmp_path / 'secondary')

    with pytest.raises(kind) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites, storage=storage, cancel_event=cancelled)

    assert caught.value is original
    receipts = tuple(suite_lock._cleanup_receipts(original))
    assert len(receipts) == 2
    assert receipts[-1] is pending
    assert len(faults.retained) == 1
    with pytest.raises(suite_lock.SuiteBusyError):
        with suite_lock.suite_lock():
            pytest.fail('Owned cleanup must keep the suite lease')
    retained = next(handle for handle in original.inspections if isinstance(handle, ReceiptInspection))
    faults.cleanup_error = None
    asyncio.run(retained.aclose())
    assert faults.retained == set()
    assert events == ['prerequisite-released']
    with suite_lock.suite_lock():
        pass
    assert list((tmp_path / 'secondary').rglob('manifest.json')) == []


def test_interruption_keeps_preexisting_duties_and_owners(independent_publication, copy_faults):
    publication, faults = independent_publication, copy_faults
    events = []
    previous_owner = object()
    async def previous_cleanup():
        events.append('previous')
    previous = CleanupPendingError((previous_owner,), previous_cleanup)
    original = KeyboardInterrupt()
    original.cleanup_pending = previous
    original.retained_owners = (previous_owner,)
    faults.link_error = original
    faults.cleanup_error = OSError('synthetic unlink failure')

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    receipt = original.cleanup_pending
    assert receipt.cleanup_pending is previous
    assert original.retained_owners == (previous_owner, receipt.owners[0])
    faults.cleanup_error = None
    asyncio.run(ReceiptInspection(original).aclose())
    assert events == ['previous']
    assert faults.retained == set()


def test_close_acknowledgement_interruption_cannot_touch_reused_descriptor(
        independent_publication, copy_faults, monkeypatch, tmp_path):
    from concurrent.futures import Future
    from api_agent.testing import storage as module
    publication, faults = independent_publication, copy_faults
    original = KeyboardInterrupt()
    class InterruptedAcknowledgement(Future):
        interrupted = False
        def result(self, *args, **kwargs):
            value = super().result(*args, **kwargs)
            if not self.interrupted:
                self.interrupted = True
                raise original
            return value
    monkeypatch.setattr(module._CopyCleanup, '_close_ticket', lambda self: InterruptedAcknowledgement())

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    retained = original.cleanup_pending.owners[0]
    descriptor = retained.directory_fd
    assert retained.closing.done()
    assert faults.retained == set()
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'unrelated descriptor bytes')
    opened = os.open(replacement, os.O_RDONLY)
    try:
        os.dup2(opened, descriptor)
        asyncio.run(original.cleanup_pending.retry())
        assert os.pread(descriptor, 100, 0) == b'unrelated descriptor bytes'
        assert retained.directory_fd is None
    finally:
        os.close(descriptor)
        if opened != descriptor:
            os.close(opened)


@pytest.mark.parametrize('launch', ['not-started', 'started'])
def test_close_launch_interruption_retains_exact_acknowledgement(
        independent_publication, copy_faults, monkeypatch, launch):
    from api_agent.testing import storage as module
    publication, faults = independent_publication, copy_faults
    original = KeyboardInterrupt()
    delayed = []
    native = module._CopyCleanup._start_close
    def interrupted_start(callback, arguments):
        delayed.append((callback, arguments))
        if launch == 'started':
            callback(*arguments)
        raise original
    monkeypatch.setattr(module._CopyCleanup, '_start_close', lambda self, ticket: interrupted_start(module._dispose_descriptor, (self.descriptor, ticket)))

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    retained = original.cleanup_pending.owners[0]
    assert retained.directory_fd is not None
    monkeypatch.setattr(module._CopyCleanup, '_start_close', native)
    asyncio.run(original.cleanup_pending.retry())
    assert faults.retained == set()
    # A delayed launch whose ticket was cancelled must never dispose again.
    callback, arguments = delayed[0]
    if launch == 'not-started':
        callback(*arguments)


def attached_cleanup_error(receipt):
    error = ValueError('synthetic after_run failure')
    error.cleanup_pending = receipt
    return error


@pytest.mark.parametrize('wrap, expected', [(lambda pending: pending, CleanupPendingError),
                                           (attached_cleanup_error, ValueError)])
def test_real_after_run_preserves_direct_and_attached_cleanup_receipts(
        tmp_path, monkeypatch, copy_faults, wrap, expected):
    context, selected, config = recording_fixture(tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: [selected])
    cancelled = Event()
    cancelled.set()
    faults = copy_faults
    faults.cleanup_error = OSError('synthetic unlink failure')
    class SuccessfulPrimary:
        def put_object(self, **kwargs):
            pass
    destination = RunStorage('synthetic', client=SuccessfulPrimary(), secondary_directory=tmp_path / 'secondary')
    class Adapter:
        def persist(self, *args):
            try:
                destination.persist(*args)
            except CleanupPendingError as pending:
                raise wrap(pending) from None

    with pytest.raises(expected) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=owner([]), storage=Adapter(), cancel_event=cancelled)

    assert len(faults.retained) == 1
    receipt = next(handle for handle in caught.value.inspections if isinstance(handle, ReceiptInspection))
    with pytest.raises(suite_lock.SuiteBusyError):
        with suite_lock.suite_lock():
            pytest.fail('Temporary cleanup must retain suite ownership')
    faults.cleanup_error = None
    asyncio.run(receipt.aclose())
    assert faults.retained == set()
    with suite_lock.suite_lock():
        pass
    assert list((tmp_path / 'secondary').rglob('manifest.json')) == []


def test_nonreceipt_after_run_attribute_keeps_ordinary_diagnostic_behavior(tmp_path, monkeypatch):
    context, selected, config = recording_fixture(tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: [selected])
    cancelled = Event()
    cancelled.set()
    error = ValueError('synthetic callback failure')
    error.cleanup_pending = object()
    def fail_after(result):
        raise error

    result = integration.run_integration_tests(context, [selected], config=config,
        accepted=True, cancel_event=cancelled, after_run=fail_after)

    assert result.exit_code != 0
    assert 'after_run callback failed; details withheld.' in result.output
    assert result.inspections == ()


@pytest.mark.parametrize('duty', ['new', 'duplicate', 'none'])
def test_launched_preserves_cyclic_existing_receipts_without_duplicate_duties(tmp_path, monkeypatch, duty):
    from api_agent.testing import recorded
    context, selected, config = recording_fixture(tmp_path)
    events = []
    async def first_cleanup():
        events.append('first')
    async def second_cleanup():
        events.append('second')
    async def third_cleanup():
        events.append('third')
    first = CleanupPendingError((), first_cleanup)
    second = CleanupPendingError((), second_cleanup)
    third = CleanupPendingError((), third_cleanup)
    first.cleanup_pending = second
    second.cleanup_pending = first
    original = KeyboardInterrupt()
    original.cleanup_pending = first
    selected_duty = {'new': third, 'duplicate': second, 'none': None}[duty]
    prerequisites = owner([], failure=selected_duty)
    if duty == 'none':
        prerequisites.finish = Mock(side_effect=OSError('synthetic outer cleanup failure'))
    monkeypatch.setattr(recorded, 'run_recorded_integration', Mock(side_effect=original))

    with pytest.raises(KeyboardInterrupt) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites)

    assert caught.value is original
    receipts = tuple(suite_lock._cleanup_receipts(original))
    expected = (first, second, third) if duty == 'new' else (first, second)
    assert receipts == expected
    for pending in receipts:
        asyncio.run(pending.retry())
    assert events == (['first', 'second', 'third'] if duty == 'new' else ['first', 'second'])
    with suite_lock.suite_lock():
        pass


@pytest.mark.parametrize('cancel', [False, True])
def test_async_cleanup_keeps_loop_responsive_and_drains_close_acknowledgement(
        independent_publication, copy_faults, monkeypatch, cancel):
    from api_agent.testing import storage as module
    publication, faults = independent_publication, copy_faults
    faults.cleanup_error = OSError('synthetic cleanup failure')
    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)
    receipt = caught.value
    entered, release = Event(), Event()
    native = module.start_new_thread
    def delayed_start(callback, arguments):
        def dispose():
            entered.set()
            release.wait()
            callback(*arguments)
        return native(dispose, ())
    monkeypatch.setattr(module, 'start_new_thread', delayed_start)
    faults.cleanup_error = None
    async def exercise():
        cleanup = asyncio.create_task(receipt.retry())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await heartbeat.wait()
            assert not cleanup.done()
            if cancel:
                cleanup.cancel()
        finally:
            release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as interrupted:
                await cleanup
            assert interrupted.value.cleanup_pending.owners == receipt.owners
            await interrupted.value.cleanup_pending.retry()
        else:
            await cleanup
    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert faults.retained == set()
    assert receipt.owners[0].closing.done()
    assert not receipt.owners[0].closing.cancelled()


def test_existing_conflict_and_cleanup_failure_retains_safe_receipt(independent_publication, copy_faults):
    publication, faults = independent_publication, copy_faults
    publication.target.mkdir(parents=True)
    (publication.target / 'a-result.json').write_bytes(b'conflict')
    faults.cleanup_error = OSError('synthetic cleanup detail')

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert str(caught.value) == 'cleanup pending'
    assert not (publication.target / 'manifest.json').exists()
    faults.cleanup_error = None
    asyncio.run(caught.value.retry())
    assert (publication.target / 'a-result.json').read_bytes() == b'conflict'
    assert faults.retained == set()


def test_manifest_link_cleanup_failure_keeps_committed_content(independent_publication, copy_faults, monkeypatch):
    publication, faults = independent_publication, copy_faults
    link = os.link
    def fail_cleanup_after_manifest(*args, **kwargs):
        result = link(*args, **kwargs)
        if args[1] == 'manifest.json':
            faults.cleanup_error = OSError('synthetic manifest temporary cleanup failure')
        return result
    monkeypatch.setattr(os, 'link', fail_cleanup_after_manifest)

    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    receipt_bytes = (publication.target / 'manifest.json').read_bytes()
    assert_complete_secondary(publication)
    faults.cleanup_error = None
    asyncio.run(caught.value.retry())
    assert (publication.target / 'manifest.json').read_bytes() == receipt_bytes
    assert faults.retained == set()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'directory', 'fifo'])
def test_exclusive_creation_collision_preserves_unowned_entry(tmp_path, monkeypatch, copy_faults, kind):
    from api_agent.testing import storage as module
    monkeypatch.setattr(module, 'uuid4', lambda: 'collision')
    temporary = tmp_path / '.allure-collision'
    outside = tmp_path / 'foreign'
    outside.write_bytes(b'foreign content')
    builders = {'file': lambda: temporary.write_bytes(b'foreign content'),
                'symlink': lambda: temporary.symlink_to(outside),
                'directory': temporary.mkdir, 'fifo': lambda: os.mkfifo(temporary)}
    builders[kind]()
    identity = temporary.lstat()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            RunStorage._copy_once(descriptor, 'artifact', b'new content')
        assert temporary.lstat().st_ino == identity.st_ino
        assert temporary.lstat().st_mode == identity.st_mode
        assert outside.read_bytes() == b'foreign content'
        assert not (tmp_path / 'artifact').exists()
        assert copy_faults.retained == set()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_cleanup_refuses_replacement_after_successful_link(tmp_path, monkeypatch, copy_faults, kind):
    actual_link = os.link
    moved = tmp_path / 'owned-moved'
    foreign = tmp_path / 'foreign'
    foreign.write_bytes(b'foreign bytes')
    def replace_after_link(source, destination, **kwargs):
        actual_link(source, destination, **kwargs)
        os.rename(source, moved, src_dir_fd=kwargs['src_dir_fd'])
        if kind == 'file':
            (tmp_path / source).write_bytes(b'foreign bytes')
        else:
            (tmp_path / source).symlink_to(foreign)
    monkeypatch.setattr(os, 'link', replace_after_link)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(CleanupPendingError) as caught:
            RunStorage._copy_once(descriptor, 'artifact', b'owned bytes')
        receipt = caught.value
        retained = receipt.owners[0]
        replacement = tmp_path / retained.temporary
        replacement_identity = replacement.lstat().st_ino
        assert moved.read_bytes() == b'owned bytes'
        assert (tmp_path / 'artifact').read_bytes() == b'owned bytes'
        with pytest.raises(CleanupPendingError):
            asyncio.run(receipt.retry())
        assert replacement.lstat().st_ino == replacement_identity
        assert foreign.read_bytes() == b'foreign bytes'
        replacement.unlink()
        moved.rename(replacement)
        asyncio.run(receipt.retry())
        assert not replacement.exists()
        assert (tmp_path / 'artifact').read_bytes() == b'owned bytes'
        assert copy_faults.retained == set()
    finally:
        os.close(descriptor)


def test_creation_ack_interruption_cleans_the_acknowledged_inode(independent_publication, copy_faults, monkeypatch):
    from concurrent.futures import Future
    from api_agent.testing import storage as module
    original = KeyboardInterrupt()
    class InterruptedCreation(Future):
        def result(self, *args, **kwargs):
            super().result(*args, **kwargs)
            raise original
    monkeypatch.setattr(module, '_CreationFuture', InterruptedCreation)
    publication = independent_publication

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert not hasattr(original, 'cleanup_pending')
    assert list(publication.target.glob('.allure-*')) == []
    assert copy_faults.retained == set()
    assert not (publication.target / 'manifest.json').exists()


@pytest.mark.parametrize('started', [False, True])
def test_creation_launch_interruption_acknowledges_owned_or_cancelled_ticket(
        independent_publication, copy_faults, monkeypatch, started):
    from api_agent.testing import storage as module
    publication = independent_publication
    original = KeyboardInterrupt()
    delayed = []
    def interrupt_start(callback, arguments):
        delayed.append((callback, arguments))
        if started:
            callback(*arguments)
        raise original
    monkeypatch.setattr(module, '_start_creation', interrupt_start)

    with pytest.raises(KeyboardInterrupt) as caught:
        publication.storage.persist(publication.source, publication.manifest)

    assert caught.value is original
    assert copy_faults.retained == set()
    assert list(publication.target.iterdir()) == []
    if not started:
        callback, arguments = delayed[0]
        callback(*arguments)
        assert list(publication.target.iterdir()) == []


def test_owned_creation_fstat_failure_is_cleaned_after_retryable_observation(
        independent_publication, copy_faults, monkeypatch):
    from threading import current_thread, main_thread
    publication = independent_publication
    fstat = os.fstat
    def refuse_worker_stat(fd):
        if current_thread() is not main_thread():
            raise OSError('synthetic creation observation failure')
        return fstat(fd)
    monkeypatch.setattr(os, 'fstat', refuse_worker_stat)

    with pytest.raises(StorageError):
        publication.storage.persist(publication.source, publication.manifest)

    assert copy_faults.retained == set()
    assert list(publication.target.iterdir()) == []


@pytest.mark.parametrize('behavior', ['raise', 'equal'])
def test_launched_receipt_dedup_never_calls_foreign_equality(tmp_path, monkeypatch, behavior):
    from api_agent.testing import recorded
    context, selected, config = recording_fixture(tmp_path)
    calls, disposed = [], []
    async def previous_cleanup():
        disposed.append('previous')
    async def later_cleanup():
        disposed.append('later')
    class ForeignReceipt(CleanupPendingError):
        def __eq__(self, other):
            calls.append(other)
            if behavior == 'raise':
                raise RuntimeError('synthetic foreign equality')
            return True
    first = ForeignReceipt((), previous_cleanup)
    later = CleanupPendingError((), later_cleanup)
    original = KeyboardInterrupt()
    original.cleanup_pending = first
    prerequisites = owner([], failure=later)
    monkeypatch.setattr(recorded, 'run_recorded_integration', Mock(side_effect=original))

    with pytest.raises(KeyboardInterrupt) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites)

    assert caught.value is original
    receipts = tuple(suite_lock._cleanup_receipts(original))
    assert len(receipts) == 2
    assert receipts[0] is first and receipts[1] is later
    assert calls == []
    asyncio.run(ReceiptInspection(original).aclose())
    assert disposed == ['previous', 'later']
    with suite_lock.suite_lock():
        pass


def test_missing_owned_temporary_acknowledges_cleanup_without_touching_artifact(independent_publication, copy_faults):
    publication, faults = independent_publication, copy_faults
    faults.cleanup_error = OSError('synthetic unlink failure')
    with pytest.raises(CleanupPendingError) as caught:
        publication.storage.persist(publication.source, publication.manifest)
    retained = caught.value.owners[0]
    faults.cleanup_error = None
    os.unlink(retained.temporary, dir_fd=retained.directory_fd)

    asyncio.run(caught.value.retry())

    assert faults.retained == set()
    assert (publication.target / 'a-result.json').read_bytes() == b'{}'


def test_cancelled_creation_ticket_never_creates_or_unlinks_a_name(independent_publication, copy_faults, monkeypatch):
    from concurrent.futures import Future
    from api_agent.testing import storage as module
    ticket = Future()
    ticket.cancel()
    monkeypatch.setattr(module, '_CreationFuture', lambda: ticket)
    publication = independent_publication

    with pytest.raises(StorageError):
        publication.storage.persist(publication.source, publication.manifest)

    assert copy_faults.retained == set()
    assert list(publication.target.iterdir()) == []


def test_creation_failure_ticket_is_observed_before_pin_release(independent_publication, copy_faults, monkeypatch):
    from api_agent.testing import storage as module
    publication = independent_publication
    created = []
    def inline_creation(callback, arguments):
        created.append(arguments[0])
        callback(*arguments)
    def refuse_create(*args, **kwargs):
        raise PermissionError('synthetic create failure')
    monkeypatch.setattr(module, '_start_creation', inline_creation)
    monkeypatch.setattr(module, 'open', refuse_create, raising=False)

    with pytest.raises(StorageError):
        publication.storage.persist(publication.source, publication.manifest)

    assert len(created) == 1
    assert isinstance(created[0].exception(), PermissionError)
    assert copy_faults.retained == set()
    assert list(publication.target.iterdir()) == []


def test_interrupted_creation_waits_for_inflight_ownership_before_cleanup(
        independent_publication, copy_faults, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from api_agent.testing import storage as module
    publication, faults = independent_publication, copy_faults
    entered, release = Event(), Event()
    original = KeyboardInterrupt()
    open_file, native_start = os.open, module._start_creation
    def pause_after_exclusive_open(name, flags, *args, **kwargs):
        descriptor = open_file(name, flags, *args, **kwargs)
        if isinstance(name, str) and name.startswith('.allure-'):
            entered.set()
            release.wait()
        return descriptor
    def interrupt_caller_after_worker_open(callback, arguments):
        native_start(callback, arguments)
        assert entered.wait(2)
        raise original
    monkeypatch.setattr(os, 'open', pause_after_exclusive_open)
    monkeypatch.setattr(module, '_start_creation', interrupt_caller_after_worker_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(publication.storage.persist, publication.source, publication.manifest)
        try:
            assert entered.wait(2)
            assert not result.done()
            assert len(faults.retained) == 1
        finally:
            release.set()
        with pytest.raises(KeyboardInterrupt) as caught:
            result.result(timeout=2)
    assert caught.value is original
    assert faults.retained == set()
    assert list(publication.target.iterdir()) == []


def test_direct_prerequisite_receipt_keeps_legacy_cleanup_pending_alias(tmp_path):
    from test_recorded_integration import Storage, fake_runner
    context, selected, config = recording_fixture(tmp_path)
    disposed = []
    async def release():
        disposed.append('released')
    pending = CleanupPendingError((), release)
    prerequisites = owner([], failure=pending)

    with pytest.raises(CleanupPendingError) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites, storage=Storage(), runner=fake_runner())

    assert caught.value is pending
    assert caught.value.cleanup_pending is pending
    assert caught.value.recorded_result.persisted
    assert tuple(suite_lock._cleanup_receipts(pending)) == (pending,)
    asyncio.run(ReceiptInspection(pending).aclose())
    assert disposed == ['released']
    with suite_lock.suite_lock():
        pass


def test_direct_receipt_alias_never_overwrites_existing_child_duty(tmp_path):
    from test_recorded_integration import Storage, fake_runner
    context, selected, config = recording_fixture(tmp_path)
    disposed = []
    async def release_parent():
        disposed.append('parent')
    async def release_child():
        disposed.append('child')
    parent = CleanupPendingError((), release_parent)
    child = CleanupPendingError((), release_child)
    parent.cleanup_pending = child
    prerequisites = owner([], failure=parent)

    with pytest.raises(CleanupPendingError) as caught:
        launched.run_launched_integration(context, [selected], config, accepted=True,
            prerequisites=prerequisites, storage=Storage(), runner=fake_runner())

    assert caught.value is parent
    assert parent.cleanup_pending is child
    assert tuple(suite_lock._cleanup_receipts(parent)) == (parent, child)
    asyncio.run(ReceiptInspection(parent).aclose())
    assert disposed == ['parent', 'child']
    with suite_lock.suite_lock():
        pass
