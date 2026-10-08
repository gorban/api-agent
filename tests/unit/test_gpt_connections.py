import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import stat

import pytest

from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
from api_agent.llm_providers.gpt.contracts import AuthError, Connection


@pytest.fixture
def record(store):
    return Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)


@pytest.fixture
def store(tmp_path):
    return ConnectionStore(tmp_path / 'private')


def test_roundtrip(store, record):
    key = store.save(record)
    assert store.load(key) == record
    assert store.list() == (record,)
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / (key + '.json')).stat().st_mode) == 0o600
    host = store.host_id()
    assert store.host_id() == host
    assert host.startswith('urn:uuid:')
    assert store.list() == (record,)
    assert key == store.profile_id(replace(record, email='other'))
    assert key != store.profile_id(replace(record, subject='other'))
    assert key != store.profile_id(replace(record, client_id='other'))
    store.save(replace(record, access_token='next'))
    assert store.load(key).access_token == 'next'
    assert 'access' not in repr(record)


@pytest.mark.parametrize('changes', [
    {'subject': ''}, {'client_id': 1}, {'host_id': ''}, {'access_token': ''},
    {'refresh_token': ''}, {'id_token': ''}, {'email': None}, {'scopes': ()},
    {'scopes': ['one']}, {'scopes': ('',)}, {'scopes': (1,)},
    {'expires_at': float('nan')}, {'expires_at': True}, {'expires_at': '100'},
    {'earliest_refresh_at': float('inf')}, {'expires_at': 10 ** 400},
])
def test_invalid_record(store, record, changes):
    with pytest.raises(AuthError, match='^invalid_connection$'):
        store.save(replace(record, **changes))


def test_invalid_type(store):
    with pytest.raises(AuthError):
        store.save(None)


@pytest.mark.parametrize('key', ['../escape', '', 'G' * 64, None])
def test_invalid_key(store, key):
    with pytest.raises(AuthError):
        store.load(key)


@pytest.mark.parametrize('kind', ['permissions', 'symlink', 'file'])
def test_bad_root(tmp_path, kind):
    root = tmp_path / 'private'
    if kind == 'permissions':
        root.mkdir(mode=0o755)
    elif kind == 'symlink':
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        root.write_text('x')
    with pytest.raises(AuthError):
        ConnectionStore(root).list()


@pytest.mark.parametrize('kind', ['permissions', 'symlink', 'hardlink', 'directory', 'missing'])
def test_bad_file(store, record, tmp_path, kind):
    key = store.save(record)
    path = store.root / (key + '.json')
    if kind == 'permissions':
        path.chmod(0o644)
    elif kind == 'symlink':
        path.unlink()
        path.symlink_to(tmp_path / 'absent')
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'link')
    elif kind == 'directory':
        path.unlink()
        path.mkdir()
    else:
        path.unlink()
    with pytest.raises(AuthError):
        store.load(key)


@pytest.mark.parametrize('raw', [b'x', b'\xff', b'x' * 65537, b'[]', b'{}'])
def test_corrupt(store, record, raw):
    key = store.save(record)
    (store.root / (key + '.json')).write_bytes(raw)
    with pytest.raises(AuthError):
        store.load(key)


@pytest.mark.parametrize('change', [{'version': 2}, {'version': True}, {'scopes': 'one'}, {'extra': 1}, {'subject': 'else'}])
def test_fields(store, record, change):
    key = store.save(record)
    path = store.root / (key + '.json')
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(AuthError):
        store.load(key)


@pytest.mark.parametrize('data', [[], {}, {'version': 2, 'host_id': 'x'}, {'version': 1, 'host_id': 'x'}, {'version': 1, 'host_id': None}, {'version': 1, 'host_id': 'urn:uuid:00000000-0000-1000-8000-000000000000'}])
def test_bad_host(store, data):
    store.host_id()
    (store.root / 'host.json').write_text(json.dumps(data))
    with pytest.raises(AuthError):
        store.host_id()


def test_foreign_owner(store, record, monkeypatch):
    store.save(record)
    monkeypatch.setattr(os, 'getuid', lambda: -1)
    with pytest.raises(AuthError):
        store.list()


def test_file_foreign_owner(store, record, monkeypatch):
    key = store.save(record)
    real = os.fstat
    def foreign(fd):
        data = list(real(fd))
        data[4] = -1
        return os.stat_result(data)
    monkeypatch.setattr(os, 'fstat', foreign)
    with pytest.raises(AuthError):
        store.load(key)


def test_atomic_failure(store, record, monkeypatch):
    key = store.save(record)
    def fail(*args, **kwargs):
        raise OSError('private detail')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(AuthError, match='^invalid_connection$'):
        store.save(replace(record, access_token='new'))
    assert store.load(key) == record
    assert not tuple(store.root.glob('.*'))


def run(coro):
    return asyncio.run(coro)


def test_session_fresh_and_disposal(store, record):
    key = store.save(record)
    async def refresh(value, now):
        raise AssertionError
    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 0)
        assert await session() == 'access'
        assert await session() == 'access'
        closing = session.aclose()
        with pytest.raises(AuthError, match='session_closed'):
            await session()
        await closing
        await SubscriptionSession(store, key, refresh).aclose()
    run(scenario())


@pytest.mark.parametrize('early,now,calls', [(90, 50, 0), (90, 100, 1), (0, 50, 1)])
def test_refresh_threshold(store, record, early, now, calls):
    key = store.save(replace(record, earliest_refresh_at=early))
    seen = []
    async def refresh(value, moment):
        seen.append(moment)
        return replace(value, access_token='next', expires_at=200)
    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: now)
        assert await session() == ('next' if calls else 'access')
        await session.aclose()
    run(scenario())
    assert len(seen) == calls


@pytest.mark.parametrize('changes', [{'subject': 'other'}, {'client_id': 'other'}, {'host_id': 'other'}, {'scopes': ('other',)}, {'expires_at': 50}])
def test_bad_rotation(store, record, changes):
    key = store.save(record)
    async def refresh(value, now):
        return replace(value, **changes)
    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        with pytest.raises(AuthError):
            await session()
        await session.aclose()
    run(scenario())
    assert store.load(key) == record


def test_failure_retry(store, record):
    key = store.save(record)
    seen = []
    async def refresh(value, now):
        seen.append(1)
        if len(seen) == 1:
            raise AuthError('refresh_failed')
        return replace(value, expires_at=200)
    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        with pytest.raises(AuthError):
            await session()
        assert store.load(key) == record
        assert await session() == 'access'
        await session.aclose()
    run(scenario())


def test_coalescing_and_cancelled_waiter(store, record):
    key = store.save(record)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def refresh(value, now):
            seen.append(value.refresh_token)
            started.set()
            await release.wait()
            return replace(value, access_token='next', refresh_token='rotated', expires_at=200)
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        first = asyncio.create_task(session())
        await started.wait()
        second = asyncio.create_task(session())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == 'next'
        assert seen == ['refresh']
        await session.aclose()
    run(scenario())


def test_two_sessions_reread(store, record):
    key = store.save(record)
    async def scenario():
        seen = []
        async def refresh(value, now):
            seen.append(1)
            await asyncio.sleep(0.01)
            return replace(value, expires_at=200, access_token='next')
        first = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        second = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        assert await asyncio.gather(first(), second()) == ['next', 'next']
        assert seen == [1]
        await first.aclose()
        await second.aclose()
    run(scenario())


def test_dispose_during_refresh(store, record):
    key = store.save(record)
    async def scenario():
        started = asyncio.Event()
        async def refresh(value, now):
            started.set()
            await asyncio.Event().wait()
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        waiter = asyncio.create_task(session())
        await started.wait()
        await session.aclose()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    run(scenario())
    assert store.load(key) == record


def test_dispose_during_acquisition(store, record):
    key = store.save(record)
    async def scenario():
        guard = store.lock(key)
        guard.__enter__()
        session = SubscriptionSession(store, key, None, clock=lambda: 0)
        waiter = asyncio.create_task(session())
        await asyncio.sleep(0.02)
        closing = session.aclose()
        guard.__exit__(None, None, None)
        await closing
        with pytest.raises(asyncio.CancelledError):
            await waiter
    run(scenario())


def test_write_limit(store, record):
    with pytest.raises(AuthError):
        store.save(replace(record, access_token='x' * 65537))


def test_boolean_host_version(store):
    store.host_id()
    path = store.root / 'host.json'
    data = json.loads(path.read_text())
    data['version'] = True
    path.write_text(json.dumps(data))
    with pytest.raises(AuthError):
        store.host_id()


def _lock_child(root, key, ready, acquired):
    ready.set()
    with ConnectionStore(Path(root)).lock(key):
        acquired.set()


def test_process_lock(store, record):
    import multiprocessing
    context = multiprocessing.get_context('fork')
    ready, acquired = context.Event(), context.Event()
    key = store.save(record)
    # Fork before holding the lock so the child cannot inherit its descriptor.
    gate = context.Event()
    def child():
        gate.wait()
        _lock_child(store.root, key, ready, acquired)
    process = context.Process(target=child)
    process.start()
    try:
        with store.lock(key):
            gate.set()
            assert ready.wait(2)
            assert not acquired.wait(0.05)
        assert acquired.wait(2)
        process.join(2)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_duplicate_fields(store, record):
    key = store.save(record)
    path = store.root / (key + '.json')
    raw = path.read_text()
    path.write_text(raw[:-1] + ',"version":1}')
    with pytest.raises(AuthError):
        store.load(key)


def test_foreign_host_session(store, record):
    key = store.save(replace(record, host_id='else'))
    async def scenario():
        session = SubscriptionSession(store, key, None, clock=lambda: 0)
        with pytest.raises(AuthError):
            await session()
        await session.aclose()
    run(scenario())


@pytest.mark.parametrize('skew', [-1, float('nan'), float('inf'), True, 'x'])
def test_bad_skew(store, skew):
    with pytest.raises(AuthError):
        SubscriptionSession(store, '0' * 64, None, skew=skew)


@pytest.mark.parametrize('now', [float('nan'), float('inf'), True, 'x'])
def test_bad_clock(store, record, now):
    key = store.save(record)
    async def scenario():
        session = SubscriptionSession(store, key, None, clock=lambda: now)
        with pytest.raises(AuthError):
            await session()
        await session.aclose()
    run(scenario())


def test_nonfinite_threshold(store, record):
    key = store.save(replace(record, expires_at=-1e308))
    async def scenario():
        session = SubscriptionSession(store, key, None, clock=lambda: 0, skew=1e308)
        with pytest.raises(AuthError):
            await session()
        await session.aclose()
    run(scenario())


@pytest.mark.parametrize('operation', ['read', 'write', 'list'])
def test_root_swap(store, record, tmp_path, monkeypatch, operation):
    key = store.save(record)
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o755)
    path = outside / (key + '.json')
    data = json.loads((store.root / (key + '.json')).read_text())
    data['access_token'] = 'outside-secret'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    old = tmp_path / 'old'
    original = store._directory
    swapped = False
    def swap():
        nonlocal swapped
        fd = original()
        if not swapped:
            swapped = True
            store.root.rename(old)
            store.root.symlink_to(outside, target_is_directory=True)
        return fd
    monkeypatch.setattr(store, '_directory', swap)
    if operation == 'read':
        assert store.load(key) == record
    elif operation == 'write':
        store.save(replace(record, access_token='next'))
        assert json.loads((old / (key + '.json')).read_text())['access_token'] == 'next'
    else:
        with pytest.raises(AuthError):
            store.list()
    assert json.loads(path.read_text())['access_token'] == 'outside-secret'
    assert not tuple(outside.glob('.*'))


def test_ancestor_symlink(tmp_path, record):
    actual = tmp_path / 'actual'
    actual.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    saved = ConnectionStore(link / 'private')
    key = saved.save(record)
    assert saved.load(key) == record


@pytest.mark.parametrize('which', ['directory', 'file'])
def test_fstat_failure_closes(store, record, monkeypatch, which):
    key = store.save(record)
    count = len(os.listdir('/proc/self/fd'))
    original = os.fstat
    def fail(fd):
        info = original(fd)
        if (which == 'directory') == stat.S_ISDIR(info.st_mode):
            raise OSError('failure')
        return info
    monkeypatch.setattr(os, 'fstat', fail)
    for _ in range(5):
        with pytest.raises(AuthError):
            store.load(key)
    assert len(os.listdir('/proc/self/fd')) == count


def test_cancel_disposal_retains_acquisition(store, record):
    key = store.save(record)
    async def scenario():
        guard = store.lock(key)
        guard.__enter__()
        session = SubscriptionSession(store, key, None, clock=lambda: 0)
        waiter = asyncio.create_task(session())
        await asyncio.sleep(0.02)
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert session._task is not None and not session._task.done()
        guard.__exit__(None, None, None)
        await session.aclose()
        assert session._task.done()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with store.lock(key):
            assert store.load(key) == record
    run(scenario())


@pytest.mark.parametrize('rotation_fails', [False, True])
def test_cleanup_fault_closes_directory(store, record, monkeypatch, rotation_fails):
    key = store.save(record)
    before = len(os.listdir('/proc/self/fd'))
    def unlink_fault(*args, **kwargs):
        raise OSError('private cleanup failure')
    monkeypatch.setattr(os, 'unlink', unlink_fault)
    if rotation_fails:
        def replace_fault(*args, **kwargs):
            raise OSError('private replacement failure')
        monkeypatch.setattr(os, 'replace', replace_fault)
    with pytest.raises(AuthError, match='^invalid_connection$'):
        store.save(replace(record, access_token='next'))
    assert len(os.listdir('/proc/self/fd')) == before
    assert store.load(key) == (record if rotation_fails else replace(record, access_token='next'))
    if rotation_fails:
        assert len(tuple(store.root.glob('.*'))) == 1
    else:
        assert not tuple(store.root.glob('.*'))


def test_reordered_scopes_preserve_valid_rotation(store, record):
    original = replace(record, scopes=('one', 'two'))
    key = store.save(original)
    rotated = replace(original, scopes=('two', 'one'), access_token='next',
                      refresh_token='rotated', expires_at=200)
    async def refresh(value, now):
        assert value == original and now == 50
        return rotated
    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        try:
            assert await session() == 'next'
            assert store.load(key) == rotated
        finally:
            await session.aclose()
    run(scenario())


def test_serialized_reconnect_waits_for_refresh_and_wins(store, record):
    key = store.save(record)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def refresh(value, now):
            started.set()
            await release.wait()
            return replace(value, access_token='refreshed', expires_at=200)
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        refreshing = asyncio.create_task(session())
        await started.wait()
        reconnect = asyncio.create_task(store.save_serialized(replace(record, access_token='reconnected')))
        try:
            # The refresh lock remains held, but unrelated event-loop work runs.
            await asyncio.sleep(.03)
            assert not reconnect.done()
            assert store.load(key) == record
            release.set()
            assert await refreshing == 'refreshed'
            assert await reconnect == key
            assert store.load(key).access_token == 'reconnected'
        finally:
            release.set()
            await asyncio.gather(refreshing, reconnect, return_exceptions=True)
            await session.aclose()
    run(scenario())


@pytest.mark.parametrize('end', ['cancel', 'timeout'])
def test_serialized_reconnect_wait_is_bounded_and_releases_lock(store, record, end):
    key = store.save(record)
    async def scenario():
        with store.lock(key):
            waiting = asyncio.create_task(store.save_serialized(replace(record, access_token='new')))
            await asyncio.sleep(.02)
            assert not waiting.done()
            if end == 'cancel':
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(.02):
                        await waiting
            assert store.load(key) == record
        assert await store.save_serialized(replace(record, access_token='after')) == key
        assert store.load(key).access_token == 'after'
    run(scenario())


def test_serialized_initial_save_and_host_mismatch(store, record):
    assert run(store.save_serialized(record)) == store.profile_id(record)
    with pytest.raises(AuthError, match='invalid_connection'):
        run(store.save_serialized(replace(record, host_id='different-host')))
    assert store.load(store.profile_id(record)) == record
