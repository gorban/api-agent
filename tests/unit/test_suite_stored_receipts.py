"""Stored parent retries advance retained replacement children under a real lease."""
import asyncio
import os
import subprocess
import sys

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import suite_lock as locks
from api_agent.telemetry import history


@pytest.fixture
def real_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    monkeypatch.setattr(locks,'cache_path',history.cache_path)
    return locks.suite_lock_path()


def competing_process():
    script='from api_agent.testing.suite_lock import suite_lock,SuiteBusyError\ntry:\n with suite_lock():pass\nexcept SuiteBusyError:raise SystemExit(17)\n'
    result=subprocess.run([sys.executable,'-c',script],env=os.environ,timeout=5,capture_output=True)
    assert result.returncode in (0,17),result.stderr.decode()
    return result.returncode


def bind(receipt):
    lease=None
    @locks.suite_locked
    def operation():
        nonlocal lease
        lease=locks._CURRENT.get()
        raise receipt
    with pytest.raises(CleanupPendingError) as raised:operation()
    assert raised.value is receipt
    return lease


def retry(receipt):
    return asyncio.run(asyncio.wait_for(receipt.retry(),1))


@pytest.mark.parametrize('direct_child',[False,True])
def test_same_parent_advances_stable_child_until_kernel_lease_released(real_namespace,direct_child):
    calls=[]
    closed=False
    rescue=False
    async def child_close():
        nonlocal closed
        calls.append('child')
        if calls.count('child')<2 and not rescue:raise child
        closed=True
    child=CleanupPendingError(('child',),child_close)
    async def parent_close():
        calls.append('parent')
        if not closed:raise child
    parent=CleanupPendingError(('parent',),parent_close)
    lease=bind(parent)
    descriptor=lease.fd
    try:
        assert competing_process()==17
        with pytest.raises(CleanupPendingError) as raised:retry(parent)
        assert raised.value is child and calls==['parent']
        assert competing_process()==17
        handle=child if direct_child else parent
        with pytest.raises(CleanupPendingError) as raised:retry(handle)
        assert raised.value is child and calls==['parent','child']
        assert competing_process()==17
        retry(handle)
        assert calls==['parent','child','child'] and closed
        assert lease.fd is None and competing_process()==0
        with pytest.raises(OSError):os.fstat(descriptor)
        retry(parent);retry(child)
        assert calls==['parent','child','child']
    finally:
        rescue=True
        retry(child);retry(parent)
        assert lease.fd is None


def test_known_child_cancellation_identity_and_pending_lease_survive(real_namespace):
    cancelled=asyncio.CancelledError('original child interruption')
    attempts=0
    rescue=False
    async def child_close():
        nonlocal attempts
        attempts+=1
        if attempts==1 and not rescue:raise cancelled
    child=CleanupPendingError(('child',),child_close)
    async def parent_close():raise child
    parent=CleanupPendingError(('parent',),parent_close)
    lease=bind(parent)
    try:
        with pytest.raises(CleanupPendingError):retry(parent)
        with pytest.raises(asyncio.CancelledError) as raised:retry(parent)
        assert raised.value is cancelled and raised.value.args==('original child interruption',)
        assert attempts==1 and competing_process()==17
        retry(parent)
        assert attempts==2 and lease.fd is None and competing_process()==0
    finally:
        rescue=True
        retry(child);retry(parent)
        assert lease.fd is None


def test_self_receipt_attachment_remains_independent_of_parent_ack(real_namespace):
    attempts=0
    child_calls=[]
    rescue=False
    async def child_close():child_calls.append('closed')
    child=CleanupPendingError(('independent',),child_close)
    async def parent_close():
        nonlocal attempts
        attempts+=1
        if attempts==1 and not rescue:raise parent
    parent=CleanupPendingError(('parent',),parent_close)
    parent.cleanup_pending=child
    lease=bind(parent)
    try:
        with pytest.raises(CleanupPendingError):retry(parent)
        retry(parent)
        assert attempts==2 and child_calls==[] and competing_process()==17
        assert lease.fd is not None
        retry(child)
        assert child_calls==['closed'] and lease.fd is None and competing_process()==0
    finally:
        rescue=True
        retry(child);retry(parent)
        assert lease.fd is None


def test_replacement_cycle_advances_without_duplicate_disposal(real_namespace):
    calls={'parent':0,'child':0}
    rescue=False
    async def first():
        calls['parent']+=1
        if calls['parent']==1 and not rescue:raise child
    async def second():
        calls['child']+=1
        if calls['child']<=2 and not rescue:raise parent
    parent=CleanupPendingError(('parent',),first)
    child=CleanupPendingError(('child',),second)
    lease=bind(parent)
    try:
        with pytest.raises(CleanupPendingError):retry(parent)
        with pytest.raises(CleanupPendingError):retry(parent)
        assert competing_process()==17
        with pytest.raises(CleanupPendingError):retry(parent)
        assert len(lease.receipts[id(parent)].parents) == 1
        assert len(lease.receipts[id(child)].parents) == 1
        assert competing_process()==17
        retry(parent)
        assert calls=={'parent':1,'child':3} and lease.fd is None and competing_process()==0
        retry(parent);retry(child)
        assert calls=={'parent':1,'child':3}
    finally:
        rescue=True
        retry(child);retry(parent)
        assert lease.fd is None


def test_all_replacement_duties_acknowledge_before_parent_releases(real_namespace):
    calls=[]
    async def close_first():calls.append('first')
    async def close_second():calls.append('second')
    first=CleanupPendingError(('first',),close_first)
    second=CleanupPendingError(('second',),close_second)
    first.cleanup_pending=second
    async def close_parent():raise first
    parent=CleanupPendingError(('parent',),close_parent)
    lease=bind(parent)
    try:
        with pytest.raises(CleanupPendingError):retry(parent)
        retry(first)
        assert calls==['first'] and lease.fd is not None and competing_process()==17
        assert not lease.receipts[id(parent)].acknowledged
        assert not lease.receipts[id(parent)].disposed
        retry(parent)
        assert calls==['first','second'] and lease.fd is None and competing_process()==0
        retry(parent);retry(first);retry(second)
        assert calls==['first','second']
    finally:
        retry(first);retry(second);retry(parent)
        assert lease.fd is None


def test_reexported_acknowledged_child_notifies_new_parent_once(real_namespace):
    calls=[]
    async def close_child():calls.append('child')
    child=CleanupPendingError(('child',),close_child)
    async def close_parent():
        calls.append('parent')
        raise child
    parent=CleanupPendingError(('parent',),close_parent)
    with locks.suite_lock():
        lease=locks._CURRENT.get()
        locks.retain_cleanup_receipt(child)
        locks.retain_cleanup_receipt(parent)
    try:
        retry(child)
        assert calls==['child'] and competing_process()==17
        with pytest.raises(CleanupPendingError) as raised:retry(parent)
        assert raised.value is child
        assert calls==['child','parent'] and lease.fd is None and competing_process()==0
        retry(parent);retry(child)
        assert calls==['child','parent']
    finally:
        retry(child);retry(parent)
        assert lease.fd is None
