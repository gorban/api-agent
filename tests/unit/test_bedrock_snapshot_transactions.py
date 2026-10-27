"""Synthetic cross-process readiness transactions; no SDK or network calls."""
import asyncio
from contextlib import nullcontext
import hashlib
import json
import multiprocessing
from pathlib import Path
import threading
import time

import pytest

from api_agent.llm_providers.bedrock import login_worker as worker
from api_agent.llm_providers.bedrock.persistence import ProfileStore, parsed, read
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.providers.contracts import ContractError


IDENTITY = dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')
OLD = dict(AccessKeyId='old-key', SecretAccessKey='old-secret', SessionToken='old-token')
NEW = dict(AccessKeyId='new-key', SecretAccessKey='new-secret', SessionToken='new-token')


@pytest.fixture
def transaction_store(tmp_path):
    home = tmp_path / 'home'
    aws = home / '.aws'
    aws.mkdir(parents=True)
    paths = dict(home=str(home), config=str(aws / 'config'),
                 credentials=str(aws / 'credentials'), login=str(aws / 'login/cache'))
    native = tmp_path / 'native'
    (native / '.aws').mkdir(parents=True)
    (native / '.aws/config').write_text('[profile chosen]\nregion=us-east-1\nlogin_session=example\n')
    cache = native / '.aws/login/cache' / (hashlib.sha256(b'example').hexdigest() + '.json')
    cache.parent.mkdir(parents=True)
    cache.write_text('{"synthetic":"refresh-cache"}')
    ProfileStore(paths, 'chosen').publish(native, OLD, time.time() + 3600, IDENTITY)
    return paths, native


def _peer_publish(paths, native, pipe):
    try:
        store = ProfileStore(paths, 'chosen')
        pipe.send('attempting')
        store.publish(native, NEW, time.time() + 3600, IDENTITY)
        pipe.send('published')
    finally:
        pipe.close()


def _peer_ready(paths, pipe):
    try:
        worker.capture_paths = lambda: paths
        worker.verify_identity = lambda *args, **kwargs: dict(IDENTITY)
        worker._ambient = lambda *args: (_ for _ in ()).throw(AssertionError('redundant native refresh'))
        pipe.send('attempting')
        result = worker.ambient(dict(profile='chosen', region='us-east-1'))
        pipe.send(result['AccessKeyId'])
    finally:
        pipe.close()


def _spawn(target, *args):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    return process, parent


def _receive(pipe):
    assert pipe.poll(5), 'worker failed to report progress'
    return pipe.recv()


def _finish(process, pipe):
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    pipe.close()
    assert process.exitcode == 0


def test_ready_transaction_blocks_peer_publish_until_tuple_verified(transaction_store, monkeypatch):
    paths, native = transaction_store
    original = ProfileStore.snapshot
    peer = None
    def snapshot(store):
        nonlocal peer
        marker = original(store)
        peer = _spawn(_peer_publish, paths, native)
        assert _receive(peer[1]) == 'attempting'
        # The peer is live and attempting the same persistent transaction.
        assert not peer[1].poll(0.1)
        return marker
    monkeypatch.setattr(ProfileStore, 'snapshot', snapshot)
    monkeypatch.setattr(worker, 'capture_paths', lambda: paths)
    monkeypatch.setattr(worker, 'verify_identity', lambda *args, **kwargs: dict(IDENTITY))
    try:
        result = worker.ambient(dict(profile='chosen', region='us-east-1'))
        assert result['AccessKeyId'] == 'old-key'
        assert _receive(peer[1]) == 'published'
    finally:
        if peer is not None:
            _finish(*peer)
    assert parsed(read(paths['credentials']))['chosen']['aws_access_key_id'] == 'new-key'


def test_waiting_acquirer_rereads_new_snapshot_without_native_refresh(transaction_store):
    paths, native = transaction_store
    store = ProfileStore(paths, 'chosen')
    with store.locked():
        peer = _spawn(_peer_ready, paths)
        assert _receive(peer[1]) == 'attempting'
        assert not peer[1].poll(0.1)
        # Nested publication must reuse this same thread's held lock.
        store.publish(native, NEW, time.time() + 3600, IDENTITY)
    try:
        assert _receive(peer[1]) == 'new-key'
    finally:
        _finish(*peer)


def test_same_store_threads_serialize_and_nested_exception_restores_lock(transaction_store):
    paths, _ = transaction_store
    store = ProfileStore(paths, 'chosen')
    attempting = threading.Event()
    acquired = threading.Event()
    def contender():
        attempting.set()
        with store.locked():
            acquired.set()
    with store.locked():
        with pytest.raises(RuntimeError, match='synthetic'):
            with store.locked():
                raise RuntimeError('synthetic')
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempting.wait(5)
        assert not acquired.wait(0.1)
    thread.join(5)
    assert not thread.is_alive()
    assert acquired.is_set()
    assert store._lock_depth == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('termination', ['cancel', 'deadline'])
@pytest.mark.parametrize('phase', ['holding', 'waiting'])
async def test_terminated_login_worker_releases_persistent_transaction(transaction_store, tmp_path, monkeypatch, termination, phase):
    paths, _ = transaction_store
    marker = tmp_path / 'holding'
    script = ('import json,sys,time\nfrom pathlib import Path\n'
              'from api_agent.llm_providers.bedrock.persistence import ProfileStore\n'
              'config=json.loads(sys.stdin.readline())\n'
              'if config["phase"]=="waiting": Path(config["marker"]).touch()\n'
              'with ProfileStore(config["paths"],"chosen").locked():\n'
              ' if config["phase"]=="holding": Path(config["marker"]).touch()\n'
              ' time.sleep(60)\n')
    original = asyncio.create_subprocess_exec
    children = []
    async def launch(*args, **kwargs):
        process = await original(args[0], '-c', script, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    transport = LoginTransport(timeout=1 if termination == 'deadline' else 5)
    task = None
    try:
        with ProfileStore(paths, 'chosen').locked() if phase == 'waiting' else nullcontext():
            task = asyncio.create_task(transport.acquire(dict(login=False, paths=paths, marker=str(marker), phase=phase), dict(HOME=str(tmp_path))))
            async with asyncio.timeout(5):
                while not marker.exists():
                    assert not task.done(), 'worker terminated before acquiring lock'
                    await asyncio.sleep(0.01)
            if termination == 'cancel':
                task.cancel()
            with pytest.raises(asyncio.CancelledError if termination == 'cancel' else ContractError):
                await task
            assert len(children) == 1
            assert children[0].returncode is not None
            assert not transport._processes
        peer = _spawn(_peer_ready, paths)
        try:
            assert _receive(peer[1]) == 'attempting'
            assert _receive(peer[1]) == 'old-key'
        finally:
            _finish(*peer)
    finally:
        await transport.aclose()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
