"""An acknowledged cleanup owner, not runner return, ends suite exclusion."""
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import threading

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import suite_lock as locks, integration, setup_backend
from api_agent.testing.collection import TestIdentity
from test_setup_backend import fake_host,checkpoint


def assert_competing_suite_blocked():
    def competitor():
        with pytest.raises(locks.SuiteBusyError):
            with locks.suite_lock():
                pytest.fail('Pending cleanup released the execution lease')
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(competitor).result(timeout=2)


def assert_competing_suite_allowed():
    with locks.suite_lock():
        pass


def test_actual_runner_transfers_lease_to_retained_backend_until_retry_acknowledges_close(fake_host,tmp_path,monkeypatch):
    backend=fake_host.backend
    fake_host.server_failure=True
    test=TestIdentity('base::test.py::test_ok','base','test.py','test.py::test_ok',root=tmp_path)
    monkeypatch.setattr(integration,'collect_tests',lambda *a,**k:(test,))
    monkeypatch.setattr(setup_backend,'SetupBackend',lambda *a,**k:backend)
    def process(args,**kwargs):
        Path(args[3]).write_text(''.join(json.dumps(dict(kind='report',node=test.node_id,
            phase=phase,status='passed',duration=0,traceback=''))+'\n' for phase in ('setup','call','teardown')))
        return SimpleNamespace(stdout=StringIO(),returncode=0,poll=lambda:0)
    monkeypatch.setattr(integration.subprocess,'Popen',process)
    monkeypatch.setattr(integration,'finalize',lambda *a,**k:None)
    context=SimpleNamespace(base_root=tmp_path,owner_root=tmp_path)
    try:
        with pytest.raises(CleanupPendingError) as caught:
            integration.run_integration_tests(context,(test,),accepted=True,config={})
        result = caught.value.execution
        assert result.exit_code==1 and caught.value.inspections[0] is backend
        assert backend.pending and not backend.closed and backend.thread.is_alive()
        assert_competing_suite_blocked()
    finally:
        fake_host.server_failure=False
        backend.close()
    assert backend.closed and not backend.thread.is_alive()
    assert_competing_suite_blocked()
    from api_agent.testing.inspection_controls import InspectionGroup
    group = InspectionGroup(caught.value.inspections)
    assert group.active and group.close(force=True)
    assert not group.active
    assert_competing_suite_allowed()


@pytest.mark.parametrize('initial_owner_budget', [.04, 2], ids=['expired-owner-budget', 'live-owner-budget'])
def test_cancellation_resistant_inference_keeps_kernel_lease_until_disposal_ack(tmp_path,monkeypatch,initial_owner_budget):
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.providers.llm import CompletionRequest,LLMRequest,LLMResult,Message,TextContent,Usage
    entered,stopped=threading.Event(),threading.Event()
    release=threading.Event()
    class Owner:
        def __init__(self,backend): pass
        async def ensure_ready(self): pass
        async def complete(self,request):
            entered.set()
            try:
                while not release.is_set():
                    try:
                        await asyncio.sleep(.005)
                    except asyncio.CancelledError:
                        pass
                return LLMResult(Message('assistant',(TextContent('Ok'),)),Usage(1,1,0,0,0))
            finally:
                stopped.set()
        async def aclose(self): pass
    monkeypatch.setattr(setup_backend,'_SetupOwner',Owner)
    backend=setup_backend.SetupBackend({},tmp_path)
    request=LLMRequest('synthetic-model',CompletionRequest((Message('user',(TextContent('Ok'),)),)))
    def inference(descriptor):
        client=SetupClient(descriptor,timeout=2)
        try:
            return client.complete(request)
        finally:
            client.close()
    executor=ThreadPoolExecutor(max_workers=1)
    future = first_cleanup = None
    try:
        with locks.suite_lock():
            locks.retain_suite_cleanup(backend)
            descriptor=backend.start()
            backend.cleanup_timeout=.04
            backend.server._timeout=backend.registry._timeout=initial_owner_budget
            future=executor.submit(inference,descriptor)
            assert entered.wait(timeout=2)
            with pytest.raises(CleanupPendingError):
                backend.close()
            first_cleanup = backend._close_future
        assert not stopped.is_set() and backend.pending and not backend.closed
        assert_competing_suite_blocked()
    finally:
        release.set()
        # The client must also acknowledge its disconnected lease before the
        # backend's next disposal attempt; finishing the body alone is earlier.
        try:
            if future is not None:
                future.exception(timeout=2)
            if first_cleanup is not None:
                assert isinstance(first_cleanup.exception(timeout=2), (type(None), CleanupPendingError))
        finally:
            backend.cleanup_timeout = 2
            for owner in (backend.server, backend.registry):
                if owner is not None:
                    owner._timeout = 2
            try:
                backend.close()
            finally:
                executor.shutdown(wait=True)
    assert future.done() and stopped.is_set() and backend.closed and not backend.thread.is_alive()
    assert_competing_suite_allowed()


@pytest.mark.parametrize('error',[None,KeyboardInterrupt()])
def test_two_retained_owners_release_only_after_last_acknowledgement(tmp_path,error):
    class Owner(locks.CleanupLeaseOwner):
        def __init__(self):
            self.closed=False
            self._lock=threading.RLock()
        def close(self):
            self.closed=True
            self._acknowledge_closed()
    first,second=Owner(),Owner()
    def operation():
        with locks.suite_lock():
            locks.retain_suite_cleanup(first)
            locks.retain_suite_cleanup(first)
            locks.retain_suite_cleanup(second)
            if error is not None:
                raise error
    try:
        if error is not None:
            with pytest.raises(type(error)):
                operation()
        else:
            operation()
        assert_competing_suite_blocked()
        first.close()
        assert_competing_suite_blocked()
        second.close()
        assert_competing_suite_allowed()
    finally:
        first.close()
        second.close()


def test_closed_and_failed_registration_cannot_leak_a_descriptor():
    class Owner(locks.CleanupLeaseOwner):
        closed=True
        _lock=threading.RLock()
    with locks.suite_lock():
        owner=Owner()
        locks.retain_suite_cleanup(owner)
        assert not locks._CURRENT.get().owners
        owner.closed=False
        def invalid(callback):
            raise ValueError('Synthetic registration failure')
        owner._when_closed=invalid
        with pytest.raises(ValueError,match='registration failure'):
            locks.retain_suite_cleanup(owner)
        assert not locks._CURRENT.get().owners
    assert_competing_suite_allowed()


def test_stale_or_absent_context_cannot_attach_a_cleanup_owner():
    from contextvars import copy_context
    owner=SimpleNamespace(_when_closed=lambda callback:pytest.fail('Stale registration'))
    locks.retain_suite_cleanup(owner)
    with locks.suite_lock():
        context=copy_context()
    context.run(locks.retain_suite_cleanup,owner)
    assert_competing_suite_allowed()


def test_process_death_releases_a_transferred_pending_lease(isolated_suite_lock):
    import subprocess
    import sys
    import time
    from api_agent.providers.progress import report_timeout_sync
    ready=isolated_suite_lock.with_suffix('.retained-ready')
    script='''from pathlib import Path
from threading import RLock
import sys
from api_agent.testing import suite_lock as locks
locks.suite_lock_path=lambda:Path(sys.argv[1])
class Owner(locks.CleanupLeaseOwner):
    closed=False
    _lock=RLock()
owner=Owner()
with locks.suite_lock():
    locks.retain_suite_cleanup(owner)
Path(sys.argv[2]).touch()
sys.stdin.read()
'''
    process=subprocess.Popen([sys.executable,'-c',script,str(isolated_suite_lock),str(ready)],
        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    try:
        with report_timeout_sync('Pending lease holder startup',5) as budget:
            while not ready.exists():
                assert process.poll() is None
                if budget.remaining==0:
                    raise TimeoutError('Pending holder did not start')
                time.sleep(.005)
        assert_competing_suite_blocked()
        process.kill()
        process.wait(timeout=2)
        assert_competing_suite_allowed()
        assert isolated_suite_lock.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdin.close()
        process.stderr.close()
