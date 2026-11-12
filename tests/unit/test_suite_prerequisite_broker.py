"""Cross-process prerequisite leases preserve suite ownership."""
import hashlib
import multiprocessing
import time

import pytest

from api_agent.testing.suite_prerequisite_broker import SuitePrerequisiteBroker, borrow_prerequisite
from api_agent.testing.suite_prerequisite_pool import PoolPending


class Owner:
    def __init__(self, starts, stops):
        self.starts, self.stops = starts, stops

    def acquire(self, *, deadline):
        with self.starts.get_lock():
            self.starts.value += 1
        time.sleep(0.1)
        return {'endpoint': 'http://localhost:1234'}

    def release(self):
        with self.stops.get_lock():
            self.stops.value += 1


class Handler:
    def __init__(self, starts, stops, resolutions=None):
        self.starts, self.stops, self.resolutions = starts, stops, resolutions

    def prepare(self, request, *, deadline):
        if self.resolutions is not None:
            with self.resolutions.get_lock():
                self.resolutions.value += 1
        return hashlib.sha256(request['account_scope'].encode()).hexdigest(), lambda: Owner(self.starts, self.stops)


def worker(descriptor, ready, release, barrier):
    barrier.wait(10)
    lease = borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    ready.put(lease.value)
    assert release.wait(15)
    lease.release()


def test_eight_processes_share_one_start_and_release_independently(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops, resolutions = ctx.Value('i', 0), ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops, resolutions)}, owner_budget=5)
    descriptor = broker.start()
    ready, barrier = ctx.Queue(), ctx.Barrier(8)
    releases = [ctx.Event() for _ in range(8)]
    workers = [ctx.Process(target=worker, args=(descriptor, ready, release, barrier)) for release in releases]
    for process in workers:
        process.start()
    assert [ready.get(timeout=10) for _ in workers] == [{'endpoint': 'http://localhost:1234'}] * 8
    assert resolutions.value == 8
    assert starts.value == 1
    releases[0].set()
    workers[0].join(10)
    assert workers[0].exitcode == 0
    assert len(broker.pending_lease_ids()) == 7
    assert all(process.is_alive() for process in workers[1:])
    assert stops.value == 0
    with pytest.raises(PoolPending):
        broker.close(timeout=0)
    for release in releases[1:]:
        release.set()
    for process in workers[1:]:
        process.join(10)
        assert process.exitcode == 0
    broker.close(timeout=5)
    assert stops.value == 1


def test_lost_worker_lease_requires_disposal_acknowledgement(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    lease = borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    lease._socket.close()
    with pytest.raises(PoolPending):
        broker.close(timeout=0.1)
    assert broker.pending_lease_ids() == (lease.lease_id,)
    broker.acknowledge_disposal(lease.lease_id)
    broker.close(timeout=5)
    assert stops.value == 1


def test_confirmation_gets_a_fresh_lease_but_reuses_owner(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    scope = {'account_scope': 'a' * 64}
    first = borrow_prerequisite(descriptor, 'service', scope, timeout=5)
    first.release()
    second = borrow_prerequisite(descriptor, 'service', scope, timeout=5)
    assert first.lease_id != second.lease_id
    assert starts.value == 1
    second.release()
    broker.close(timeout=5)
    assert stops.value == 1


def test_invalid_scope_never_reaches_handler(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    with pytest.raises(RuntimeError, match='acquisition failed'):
        borrow_prerequisite(descriptor, 'service', {'account_scope': 'raw-secret'}, timeout=5)
    broker.close(timeout=5)
    assert starts.value == 0


class FailingOwner(Owner):
    def acquire(self, *, deadline):
        with self.starts.get_lock():
            self.starts.value += 1
        time.sleep(0.1)
        raise RuntimeError('private startup detail')


class FailingHandler(Handler):
    def prepare(self, request, *, deadline):
        return hashlib.sha256(request['account_scope'].encode()).hexdigest(), lambda: FailingOwner(self.starts, self.stops)


class OddlyNamed(Exception):
    pass


OddlyNamed.__qualname__ = 'private detail: not a class name'


class OddlyNamedHandler(Handler):
    def prepare(self, request, *, deadline):
        raise OddlyNamed()


def test_borrower_drops_an_error_kind_that_is_not_a_plain_name(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    broker = SuitePrerequisiteBroker(tmp_path, {'service': OddlyNamedHandler(ctx.Value('i', 0), ctx.Value('i', 0))}, owner_budget=5)
    descriptor = broker.start()
    with pytest.raises(RuntimeError, match='^prerequisite acquisition failed$'):
        borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    broker.close(timeout=5)


def test_failed_start_is_shared_without_exposing_error_content(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    events = []
    broker = SuitePrerequisiteBroker(tmp_path, {'service': FailingHandler(starts, stops)}, owner_budget=5, event=events.append)
    descriptor = broker.start()
    def acquire():
        with pytest.raises(RuntimeError, match='^prerequisite acquisition failed: RuntimeError$'):
            borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: acquire(), range(8)))
    assert starts.value == 1
    assert 'private startup detail' not in str(events)
    broker.close(timeout=5)
    assert stops.value == 1


def test_descriptor_roundtrip_and_symlink_rejection(tmp_path):
    from api_agent.testing.suite_prerequisite_broker import read_descriptor

    ctx = multiprocessing.get_context('spawn')
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(ctx.Value('i', 0), ctx.Value('i', 0))}, owner_budget=5)
    descriptor = broker.start()
    assert read_descriptor(tmp_path) == descriptor
    path = tmp_path / 'suite-prerequisite-broker.json'
    path.unlink()
    path.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError):
        read_descriptor(tmp_path)
    path.unlink()
    broker.close(timeout=5)


class SlowOwner:
    def __init__(self, started, proceed, stops):
        self.started, self.proceed, self.stops = started, proceed, stops

    def acquire(self, *, deadline):
        self.started.set()
        assert self.proceed.wait(max(0, deadline - time.monotonic()))
        return {'endpoint': 'http://localhost:1234'}

    def release(self):
        self.stops.append(True)


class SlowHandler:
    def __init__(self, started, proceed, stops):
        self.started, self.proceed, self.stops = started, proceed, stops

    def prepare(self, request, *, deadline):
        return '1' * 64, lambda: SlowOwner(self.started, self.proceed, self.stops)


def test_cancelled_waiter_does_not_cancel_owner_or_leak_borrower(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    started, proceed = threading.Event(), threading.Event()
    stops = []
    broker = SuitePrerequisiteBroker(tmp_path, {'service': SlowHandler(started, proceed, stops)}, owner_budget=5)
    descriptor = broker.start()
    scope = {'account_scope': 'a' * 64}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(borrow_prerequisite, descriptor, 'service', scope, timeout=0.2)
        assert started.wait(2)
        second = executor.submit(borrow_prerequisite, descriptor, 'service', scope, timeout=5)
        with pytest.raises(TimeoutError):
            first.result(timeout=2)
        proceed.set()
        lease = second.result(timeout=5)
        lease.release()
    broker.close(timeout=5)
    assert stops == [True]


def test_close_waits_for_parent_side_resolution(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, proceed = threading.Event(), threading.Event()
    class ResolvingHandler:
        def prepare(self, request, *, deadline):
            entered.set()
            assert proceed.wait(max(0, deadline - time.monotonic()))
            return '1' * 64, lambda: None

    broker = SuitePrerequisiteBroker(tmp_path, {'service': ResolvingHandler()}, owner_budget=5)
    descriptor = broker.start()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(borrow_prerequisite, descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
        assert entered.wait(2)
        with pytest.raises(PoolPending):
            broker.close(timeout=0.01)
        proceed.set()
        with pytest.raises(RuntimeError, match='acquisition failed'):
            result.result(timeout=5)
    broker.close(timeout=5)


def read_fifo(directory, result):
    from api_agent.testing.suite_prerequisite_broker import read_descriptor
    try:
        read_descriptor(directory)
    except ValueError:
        result.put('rejected')


def test_fifo_descriptor_is_rejected_without_blocking(tmp_path):
    import os

    os.mkfifo(tmp_path / 'suite-prerequisite-broker.json', 0o600)
    ctx = multiprocessing.get_context('spawn')
    result = ctx.Queue()
    process = ctx.Process(target=read_fifo, args=(tmp_path, result))
    process.start()
    process.join(3)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail('descriptor read blocked on a FIFO')
    assert process.exitcode == 0
    assert result.get(timeout=1) == 'rejected'


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity', '1e999'])
def test_nonfinite_wire_numbers_are_rejected(number):
    import socket
    from api_agent.testing.suite_prerequisite_broker import _read

    reader, writer = socket.socketpair()
    try:
        writer.sendall(('{"value":' + number + '}\n').encode())
        with pytest.raises(ValueError):
            _read(reader)
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize('version', [True, 1.0])
def test_descriptor_version_requires_exact_integer(tmp_path, version):
    import json
    from api_agent.testing.suite_prerequisite_broker import read_descriptor

    path = tmp_path / 'suite-prerequisite-broker.json'
    path.write_text(json.dumps({'version': version, 'path': '/tmp/socket', 'token': 'a' * 64}))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        read_descriptor(tmp_path)


@pytest.mark.parametrize('duration', [float('nan'), float('inf'), -float('inf'), True])
def test_nonfinite_and_boolean_durations_are_rejected(tmp_path, duration):
    with pytest.raises(ValueError):
        SuitePrerequisiteBroker(tmp_path, {}, owner_budget=duration)
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    with pytest.raises(ValueError):
        broker.close(timeout=duration)
    with pytest.raises(ValueError):
        borrow_prerequisite({'version': 1, 'path': '/tmp/socket', 'token': 'a' * 64}, 'service', {}, timeout=duration)


def test_late_resolution_cannot_start_owner(tmp_path):
    class LateHandler:
        def prepare(self, request, *, deadline):
            time.sleep(max(0, deadline - time.monotonic()) + 0.05)
            return '1' * 64, lambda: pytest.fail('late resolution started an owner')

    broker = SuitePrerequisiteBroker(tmp_path, {'service': LateHandler()}, owner_budget=0.1)
    descriptor = broker.start()
    with pytest.raises(RuntimeError, match='acquisition failed'):
        borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    broker.close(timeout=5)


def test_host_invalidation_disposes_before_reacquisition(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    scope = {'account_scope': 'a' * 64}
    digest = hashlib.sha256(scope['account_scope'].encode()).hexdigest()
    lease = borrow_prerequisite(descriptor, 'service', scope, timeout=5)
    with pytest.raises(PoolPending):
        broker.invalidate('service', digest)
    assert stops.value == 0
    lease.release()
    broker.invalidate('service', digest)
    assert stops.value == 1
    second = borrow_prerequisite(descriptor, 'service', scope, timeout=5)
    assert starts.value == 2
    second.release()
    broker.close(timeout=5)
    assert stops.value == 2


@pytest.mark.parametrize('version', [True, 1.0])
def test_request_version_requires_exact_integer(tmp_path, version):
    import json
    import socket

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(descriptor['path'])
        request = {'version': version, 'token': descriptor['token'], 'handler': 'service',
                   'scope': {'account_scope': 'a' * 64}}
        sock.sendall(json.dumps(request).encode() + b'\n')
        assert json.loads(sock.recv(100)) == {'status': 'error', 'error': 'ValueError'}
    broker.close(timeout=5)
    assert starts.value == 0


@pytest.mark.parametrize('failure', ['construct', 'start'])
def test_accept_thread_launch_failure_removes_descriptor_and_allows_retry(tmp_path, monkeypatch, failure):
    import threading

    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=5)
    original_thread = threading.Thread

    class StartFailure:
        def start(self):
            raise RuntimeError('thread launch failed')

    def failed_thread(*args, **kwargs):
        if failure == 'construct':
            raise RuntimeError('thread construction failed')
        return StartFailure()

    monkeypatch.setattr(threading, 'Thread', failed_thread)
    with pytest.raises(RuntimeError, match='thread'):
        broker.start()
    assert not (tmp_path / 'suite-prerequisite-broker.json').exists()
    monkeypatch.setattr(threading, 'Thread', original_thread)
    descriptor = broker.start()
    assert descriptor['version'] == 1
    broker.close(timeout=5)


@pytest.mark.parametrize('failure', ['construct', 'start'])
def test_worker_thread_launch_failure_closes_client_and_accept_loop_recovers(tmp_path, monkeypatch, failure):
    import socket
    import threading

    ctx = multiprocessing.get_context('spawn')
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(ctx.Value('i', 0), ctx.Value('i', 0))}, owner_budget=5)
    descriptor = broker.start()
    original_thread = threading.Thread
    attempted = threading.Event()

    class StartFailure:
        def start(self):
            attempted.set()
            raise RuntimeError('thread launch failed')

    def fail_worker(*args, **kwargs):
        if failure == 'construct':
            attempted.set()
            raise RuntimeError('thread construction failed')
        return StartFailure()

    monkeypatch.setattr(threading, 'Thread', fail_worker)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(descriptor['path'])
        assert attempted.wait(5)
        assert sock.recv(1) == b''
    # Exhausting the permits proves the failed launch returned its permit.
    deadline = time.monotonic() + 5
    while True:
        acquired = 0
        while acquired < 128 and broker._slots.acquire(blocking=False):
            acquired += 1
        for _ in range(acquired):
            broker._slots.release()
        if acquired == 128:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    monkeypatch.setattr(threading, 'Thread', original_thread)
    lease = borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    lease.release()
    broker.close(timeout=5)


@pytest.mark.parametrize('message', [b'{"x":1,"x":2}\n', b'[]\n', b'x' * 32768])
def test_invalid_or_oversized_wire_message_is_rejected(message):
    import contextlib
    import socket
    import threading
    from api_agent.testing.suite_prerequisite_broker import _read

    def send():
        with contextlib.suppress(OSError):
            writer.sendall(message)

    reader, writer = socket.socketpair()
    # Some platforms buffer less than the oversized message, so send while reading.
    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        with pytest.raises(ValueError):
            _read(reader, deadline=time.monotonic() + 5)
    finally:
        reader.close()
        sender.join(5)
        writer.close()
    assert not sender.is_alive()


def test_unserializable_capability_releases_borrower_without_leaking(tmp_path):
    class InvalidOwner:
        def acquire(self, *, deadline):
            return {'bad': object()}

        def release(self):
            disposed.append(True)

    class InvalidHandler:
        def prepare(self, request, *, deadline):
            return '1' * 64, InvalidOwner

    disposed = []
    broker = SuitePrerequisiteBroker(tmp_path, {'service': InvalidHandler()}, owner_budget=5)
    descriptor = broker.start()
    with pytest.raises(RuntimeError, match='acquisition failed'):
        borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    assert broker.pending_lease_ids() == ()
    broker.close(timeout=5)
    assert disposed == [True]


def test_diagnostics_failure_does_not_change_lease_lifecycle(tmp_path):
    def failing_event(event):
        raise RuntimeError('diagnostics unavailable')

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5, event=failing_event)
    descriptor = broker.start()
    with borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5) as capability:
        assert capability == {'endpoint': 'http://localhost:1234'}
    broker.close(timeout=5)
    assert starts.value == stops.value == 1


def worker_waiting_for_borrow(descriptor):
    borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=20)


def test_disposal_acknowledgement_covers_borrower_before_lease_registration(tmp_path):
    import threading

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    borrowed, resume = threading.Event(), threading.Event()
    original_borrow = broker._pool.borrow

    def gated_borrow(*args, **kwargs):
        borrower = original_borrow(*args, **kwargs)
        borrowed.set()
        assert resume.wait(10)
        return borrower

    broker._pool.borrow = gated_borrow
    worker = ctx.Process(target=worker_waiting_for_borrow, args=(descriptor,))
    worker.start()
    try:
        assert borrowed.wait(10)
        worker.terminate()
        worker.join(5)
        assert not worker.is_alive()
        assert broker.pending_lease_ids() == ()
        broker.acknowledge_all_disposed()
        resume.set()
        broker.close(timeout=5)
        assert broker.pending_lease_ids() == ()
        assert starts.value == stops.value == 1
    finally:
        resume.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def test_trusted_all_worker_acknowledgement_drains_registered_leases(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    lease = borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    lease._socket.close()
    assert broker.pending_lease_ids() == (lease.lease_id,)
    broker.acknowledge_all_disposed()
    broker.acknowledge_all_disposed()
    assert broker.pending_lease_ids() == ()
    broker.close(timeout=5)
    assert starts.value == stops.value == 1


def test_wrong_capability_token_cannot_invoke_parent_handler(tmp_path):
    import json
    import socket

    ctx = multiprocessing.get_context('spawn')
    starts, stops, resolutions = ctx.Value('i', 0), ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops, resolutions)}, owner_budget=5)
    descriptor = broker.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(descriptor['path'])
        request = {'version': 1, 'token': '0' * 64, 'handler': 'service', 'scope': {'account_scope': 'a' * 64}}
        sock.sendall(json.dumps(request).encode() + b'\n')
        assert json.loads(sock.recv(100)) == {'status': 'error', 'error': 'ValueError'}
    broker.close(timeout=5)
    assert resolutions.value == starts.value == stops.value == 0


def test_forged_release_keeps_real_borrower_until_trusted_ack(tmp_path):
    import socket
    from api_agent.testing.suite_prerequisite_broker import _encode, _read

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops)}, owner_budget=5)
    descriptor = broker.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(descriptor['path'])
        sock.sendall(_encode({'version': 1, 'token': descriptor['token'], 'handler': 'service',
                              'scope': {'account_scope': 'a' * 64}}))
        ready = _read(sock, deadline=time.monotonic() + 5)
        sock.sendall(_encode({'release': 'wrong-id'}))
        assert _read(sock, deadline=time.monotonic() + 5) == {'status': 'error', 'error': 'ValueError'}
    assert broker.pending_lease_ids() == (ready['lease_id'],)
    with pytest.raises(PoolPending):
        broker.close(timeout=0)
    broker.acknowledge_disposal(ready['lease_id'])
    broker.close(timeout=5)
    assert stops.value == 1


@pytest.mark.parametrize('mode', ['accepted', 'wrong_uid', 'unavailable'])
def test_macos_peer_identity_is_required(monkeypatch, mode):
    import ctypes
    import os
    import socket
    import sys
    from api_agent.testing.suite_prerequisite_broker import _peer

    class GetPeer:
        def __call__(self, fd, uid, gid):
            if mode == 'unavailable':
                return 1
            uid._obj.value = os.geteuid() + (mode == 'wrong_uid')
            return 0

    class Library:
        getpeereid = GetPeer()

    class PeerSocket:
        def fileno(self):
            return 7

    monkeypatch.delattr(socket, 'SO_PEERCRED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: Library())
    if mode == 'accepted':
        _peer(PeerSocket())
    else:
        with pytest.raises(PermissionError if mode == 'wrong_uid' else OSError):
            _peer(PeerSocket())


@pytest.mark.parametrize('mode', ['accepted', 'wrong_uid'])
def test_kernel_peer_credentials_are_required(monkeypatch, mode):
    import os
    import socket
    import struct
    from api_agent.testing.suite_prerequisite_broker import _peer

    class PeerSocket:
        def getsockopt(self, level, option, size):
            assert (level, option, size) == (socket.SOL_SOCKET, 17, struct.calcsize('3i'))
            return struct.pack('3i', 1, os.geteuid() + (mode == 'wrong_uid'), 0)

    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    if mode == 'accepted':
        _peer(PeerSocket())
    else:
        with pytest.raises(PermissionError):
            _peer(PeerSocket())


def test_accept_loop_ends_when_the_server_socket_fails(tmp_path):
    class Server:
        def accept(self):
            raise OSError('closed')

    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=5)
    broker._server = Server()
    broker._accept()


def test_all_worker_disposal_prevents_start_after_delayed_resolution(tmp_path):
    import threading

    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    entered, resume, rejected = threading.Event(), threading.Event(), threading.Event()

    class GatedHandler:
        def prepare(self, request, *, deadline):
            entered.set()
            assert resume.wait(max(0, deadline - time.monotonic()))
            return '1' * 64, lambda: Owner(starts, stops)

    def event(record):
        if record['phase'] == 'error':
            rejected.set()

    broker = SuitePrerequisiteBroker(tmp_path, {'service': GatedHandler()}, owner_budget=10, event=event)
    descriptor = broker.start()
    worker = ctx.Process(target=worker_waiting_for_borrow, args=(descriptor,))
    worker.start()
    try:
        assert entered.wait(10)
        worker.terminate()
        worker.join(5)
        assert not worker.is_alive()
        assert broker.pending_lease_ids() == ()
        broker.acknowledge_all_disposed()
        resume.set()
        assert rejected.wait(5)
        assert starts.value == 0
        broker.close(timeout=5)
        assert stops.value == 0
    finally:
        resume.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def test_oversized_outgoing_request_is_rejected_before_send():
    from api_agent.testing.suite_prerequisite_broker import _encode

    with pytest.raises(ValueError, match='too large'):
        _encode({'value': 'x' * 32768})


def test_expired_wire_deadline_and_failed_socket_probe(monkeypatch):
    import select
    import socket
    from api_agent.testing.suite_prerequisite_broker import _read, _gone

    reader, writer = socket.socketpair()
    try:
        with pytest.raises(TimeoutError):
            _read(reader, deadline=time.monotonic() - 1)
        def unavailable(*args):
            raise OSError('socket unavailable')
        monkeypatch.setattr(select, 'select', unavailable)
        assert _gone(reader)
    finally:
        reader.close()
        writer.close()


def test_unavailable_peer_identity_is_rejected(monkeypatch):
    import socket
    import sys
    from api_agent.testing.suite_prerequisite_broker import _peer

    monkeypatch.delattr(socket, 'SO_PEERCRED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'unsupported')
    with pytest.raises(OSError, match='identity unavailable'):
        _peer(None)


def test_extreme_owner_budget_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        SuitePrerequisiteBroker(tmp_path, {}, owner_budget=10 ** 1000)


def test_start_rejects_insecure_directory_and_duplicate_start(tmp_path):
    insecure = tmp_path / 'insecure'
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(ValueError, match='private'):
        SuitePrerequisiteBroker(insecure, {}, owner_budget=1).start()
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    broker.start()
    with pytest.raises(RuntimeError, match='already started'):
        broker.start()
    broker.close(timeout=5)


def test_socket_creation_failure_keeps_existing_descriptor_intact(tmp_path, monkeypatch):
    import socket

    descriptor = tmp_path / 'suite-prerequisite-broker.json'
    descriptor.write_text('existing')
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    def fail_socket(*args, **kwargs):
        raise OSError('unavailable')
    monkeypatch.setattr(socket, 'socket', fail_socket)
    with pytest.raises(OSError, match='unavailable'):
        broker.start()
    assert descriptor.read_text() == 'existing'


def test_exhausted_broker_rejects_new_connection_and_recovers(tmp_path):
    import socket

    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    descriptor = broker.start()
    for _ in range(128):
        assert broker._slots.acquire(blocking=False)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(descriptor['path'])
            assert sock.recv(1) == b''
    finally:
        for _ in range(128):
            broker._slots.release()
    broker.close(timeout=5)


def test_non_object_capability_does_not_leave_a_borrower(tmp_path):
    class InvalidOwner:
        def acquire(self, *, deadline):
            return ['not', 'an', 'object']
        def release(self):
            stopped.append(True)
    class InvalidHandler:
        def prepare(self, request, *, deadline):
            return '1' * 64, InvalidOwner

    stopped = []
    broker = SuitePrerequisiteBroker(tmp_path, {'service': InvalidHandler()}, owner_budget=5)
    descriptor = broker.start()
    with pytest.raises(RuntimeError, match='acquisition failed'):
        borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    assert broker.pending_lease_ids() == ()
    broker.close(timeout=5)
    assert stopped == [True]


def test_host_rejects_unknown_handler_invalidation(tmp_path):
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    with pytest.raises(ValueError, match='unknown handler'):
        broker.invalidate('unregistered', '1' * 64)
    broker.acknowledge_disposal('unknown')
    broker.close(timeout=0)


@pytest.mark.parametrize('mode', ['directory', 'permissions', 'oversize'])
def test_descriptor_requires_private_bounded_regular_file(tmp_path, mode):
    from api_agent.testing.suite_prerequisite_broker import read_descriptor

    path = tmp_path / 'suite-prerequisite-broker.json'
    path.write_text('x' * (32769 if mode == 'oversize' else 1))
    path.chmod(0o644 if mode == 'permissions' else 0o600)
    if mode == 'directory':
        tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        read_descriptor(tmp_path)


def test_release_requires_server_acknowledgement_and_is_idempotent():
    import socket
    from api_agent.testing.suite_prerequisite_broker import PrerequisiteLease

    client, server = socket.socketpair()
    lease = PrerequisiteLease(client, '1' * 64, {})
    try:
        server.sendall(b'{"status":"error"}\n')
        with pytest.raises(RuntimeError, match='not acknowledged'):
            lease.release()
        lease.release()
    finally:
        server.close()


def test_request_already_connected_is_rejected_after_admission_stops(tmp_path, monkeypatch):
    import json
    import socket
    import threading
    import api_agent.testing.suite_prerequisite_broker as module

    entered, resume = threading.Event(), threading.Event()
    original_peer = module._peer
    def gated_peer(sock):
        original_peer(sock)
        entered.set()
        assert resume.wait(5)
    monkeypatch.setattr(module, '_peer', gated_peer)
    ctx = multiprocessing.get_context('spawn')
    starts, stops, resolutions = ctx.Value('i', 0), ctx.Value('i', 0), ctx.Value('i', 0)
    broker = SuitePrerequisiteBroker(tmp_path, {'service': Handler(starts, stops, resolutions)}, owner_budget=5)
    descriptor = broker.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(descriptor['path'])
            assert entered.wait(5)
            broker.acknowledge_all_disposed()
            request = {'version': 1, 'token': descriptor['token'], 'handler': 'service',
                       'scope': {'account_scope': 'a' * 64}}
            sock.sendall(json.dumps(request).encode() + b'\n')
            resume.set()
            assert json.loads(sock.recv(100)) == {'status': 'error', 'error': 'RuntimeError'}
    finally:
        resume.set()
        broker.close(timeout=5)
    assert resolutions.value == starts.value == 0


def test_failure_callback_is_deduplicated_and_cannot_change_acquisition(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    ctx = multiprocessing.get_context('spawn')
    starts, stops = ctx.Value('i', 0), ctx.Value('i', 0)
    failures, events = [], []
    def report(error):
        failures.append(error)
        raise SystemExit('callback failure')
    broker = SuitePrerequisiteBroker(tmp_path, {'service': FailingHandler(starts, stops)},
        owner_budget=5, event=events.append, on_failure=report)
    descriptor = broker.start()
    def acquire(_):
        with pytest.raises(RuntimeError, match='^prerequisite acquisition failed: RuntimeError$'):
            borrow_prerequisite(descriptor, 'service', {'account_scope': 'a' * 64}, timeout=5)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(acquire, range(8)))
    assert len(failures) == 1
    assert 'private startup detail' in str(failures[0])
    assert 'private startup detail' not in str(events)
    broker.close(timeout=5)
    assert stops.value == 1


def test_failure_reporting_is_optional_and_bounded(tmp_path):
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1)
    broker._report_failure(ValueError('ignored'))
    failures = []
    broker._on_failure = failures.append
    errors = [ValueError(str(index)) for index in range(129)]
    for error in errors:
        broker._report_failure(error)
    broker._report_failure(errors[0])
    assert failures == errors[:128]
