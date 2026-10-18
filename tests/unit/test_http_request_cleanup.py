"""Request disposal survives response wrappers, interruptions and pending duties."""
import asyncio
import inspect

import pytest

from api_agent.api.application import Application, Runtime, close_http_callbacks
from api_agent.api.studio import ResponseCleanup
from api_agent.providers.control import CleanupPendingError


def test_http_scope_callbacks_are_awaited_and_scope_is_not_mutated():
    async def run():
        closed = []
        async def dispose():
            closed.append(True)
        async def handle(scope, receive, send):
            scope['api_agent_http_cleanup'].append(dispose)
        scope = {'type':'http'}
        await Application(Runtime('example'),handle)(scope,None,None)
        assert closed == [True] and scope == {'type':'http'}
    asyncio.run(run())


def test_callback_failures_do_not_skip_siblings_and_retry_is_typed():
    async def run():
        closed = []
        pending = True
        async def first():
            closed.append('first')
            if pending:
                raise RuntimeError('disposal unavailable')
        async def second():
            closed.append('second')
        callbacks = [first,second]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks)
        assert closed == ['first','second'] and len(callbacks) == 1
        pending = False
        await error.value.retry()
        assert callbacks == [] and closed == ['first','second','first']
    asyncio.run(run())


def test_callback_scheduler_failure_closes_unsubmitted_coroutine(monkeypatch):
    from api_agent.api import application
    async def run():
        created = []
        async def dispose():
            pass
        def callback():
            coroutine = dispose()
            created.append(coroutine)
            return coroutine
        submitted = []
        def reject(coroutine):
            submitted.append(coroutine)
            raise RuntimeError('scheduler unavailable')
        monkeypatch.setattr(asyncio,'create_task',reject)
        callbacks = [callback]
        with pytest.raises(CleanupPendingError):
            await close_http_callbacks(callbacks)
        assert created == []
        assert inspect.getcoroutinestate(submitted[0]) == inspect.CORO_CLOSED
        assert len(callbacks) == 1
    asyncio.run(run())


def test_interrupted_callback_preserves_retry_and_finishes_siblings():
    async def run():
        entered, release = asyncio.Event(),asyncio.Event()
        closed=[]
        async def first():
            entered.set()
            await release.wait()
            closed.append('first')
        async def second():
            closed.append('second')
        callbacks=[first,second]
        task=asyncio.create_task(close_http_callbacks(callbacks))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert sorted(closed) == ['first','second']
        assert isinstance(error.value.cleanup_pending,CleanupPendingError)
        await error.value.cleanup_pending.retry()
        assert not callbacks and sorted(closed) == ['first','second']
    asyncio.run(run())


@pytest.mark.parametrize('engine_error,source_error', [(RuntimeError,RuntimeError),(asyncio.CancelledError,None),(None,asyncio.CancelledError)])
def test_response_owner_preserves_both_disposal_duties(engine_error,source_error):
    async def run():
        calls=[]
        class Engine:
            async def disconnect_turn(self,*args):
                calls.append('engine')
                if engine_error:
                    raise engine_error()
        class Source:
            async def aclose(self):
                calls.append('source')
                if source_error:
                    raise source_error()
        class Backend:
            lifecycle=Engine()
            _owner_id='owner'
            _responses={}
        backend=Backend()
        owner=ResponseCleanup(backend,'session')
        owner.turn=object()
        owner.source=Source()
        backend._responses['session']=owner
        expected=asyncio.CancelledError if asyncio.CancelledError in (engine_error,source_error) else CleanupPendingError
        with pytest.raises(expected) as error:
            await owner.aclose()
        receipt=error.value.cleanup_pending if isinstance(error.value,asyncio.CancelledError) else error.value
        assert calls == ['engine','source'] and backend._responses['session'] is owner
        assert receipt.owners == (owner,)
    asyncio.run(run())


def test_pending_response_owner_blocks_backend_auth_disposal():
    from test_application_chat_contract import backend
    async def run():
        bridge=backend()
        class Source:
            pending=True
            async def aclose(self):
                if self.pending:
                    raise RuntimeError('pending')
        source=Source()
        owner=ResponseCleanup(bridge,'unknown')
        owner.source=source
        bridge._responses['unknown']=owner
        with pytest.raises(CleanupPendingError) as error:
            await bridge.aclose()
        assert error.value.owners == (bridge,)
        assert bridge._responses['unknown'] is owner
        source.pending=False
        await error.value.retry()
        assert not bridge._responses
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['source','response'])
def test_response_creation_failure_disposes_admitted_turn(monkeypatch,failure):
    from api_agent.api import studio
    from test_application_chat_contract import backend,payload,request
    async def run():
        bridge=backend()
        def reject(*args,**kwargs):
            raise RuntimeError('allocation unavailable')
        if failure == 'source':
            monkeypatch.setattr(bridge,'_events',reject)
        else:
            monkeypatch.setattr(studio,'StreamingHttpResponse',reject)
        response=await bridge.handle(request(payload()),'chat_stream')
        assert response.status_code == 502
        assert not bridge._responses
        assert all(turn.done.is_set() for turn in bridge.lifecycle._turns.values())
        await bridge.aclose()
    asyncio.run(run())


def test_unconsumed_response_capacity_is_bounded():
    from test_application_chat_contract import backend,payload,request
    async def run():
        bridge=backend()
        bridge.lifecycle.max_active=0
        bridge.lifecycle.max_terminal=0
        response=await bridge.handle(request(payload()),'chat_stream')
        assert response.status_code == 409 and not bridge.lifecycle._turns
        await bridge.aclose()
    asyncio.run(run())


def test_backend_cleanup_scheduler_rejection_closes_coroutine(monkeypatch):
    from api_agent.api import studio
    async def run():
        async def dispose():
            pass
        coroutine=dispose()
        def reject(value):
            raise RuntimeError('scheduler unavailable')
        monkeypatch.setattr(asyncio,'create_task',reject)
        with pytest.raises(RuntimeError):
            await studio._finish(coroutine)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    asyncio.run(run())


def test_stale_response_cleanup_cannot_remove_later_owner():
    from test_application_chat_contract import backend
    async def run():
        bridge=backend()
        stale=ResponseCleanup(bridge,'session')
        newer=ResponseCleanup(bridge,'session')
        bridge._responses['session']=newer
        await stale.aclose()
        assert bridge._responses['session'] is newer
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('replacement', [False,True])
def test_finished_source_does_not_remove_different_registry_entry(replacement):
    from test_application_chat_contract import backend,payload,request
    async def run():
        bridge=backend()
        body=payload()
        response=await bridge.handle(request(body),'chat_stream')
        bridge._responses.pop(body['session_id'])
        newer=ResponseCleanup(bridge,body['session_id'])
        if replacement:
            bridge._responses[body['session_id']]=newer
        frames=[part async for part in response.streaming_content]
        assert frames[-1].startswith(b'event: done')
        assert bridge._responses.get(body['session_id']) is (newer if replacement else None)
        await bridge.aclose()
    asyncio.run(run())


def test_shutdown_pending_response_does_not_skip_cooperative_sibling():
    from test_application_chat_contract import backend,payload,request
    async def run():
        release=asyncio.Event()
        entered=[asyncio.Event(),asyncio.Event()]
        stopped=[asyncio.Event(),asyncio.Event()]
        async def complete(value,**kwargs):
            index=int(value.payload.messages[0].content[0].text)
            entered[index].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if index == 0:
                    await release.wait()
                raise
            finally:
                stopped[index].set()
        bridge=backend(complete=complete)
        bridge.lifecycle.cleanup_timeout=.02
        for index in range(2):
            await bridge.handle(request(payload(messages=[{'role':'user','content':str(index)}])),'chat_stream')
            await entered[index].wait()
        with pytest.raises(CleanupPendingError) as error:
            await bridge.aclose()
        assert not stopped[0].is_set() and stopped[1].is_set()
        assert bridge.lifecycle.closed and not bridge.owner._closed
        release.set()
        await error.value.retry()
        assert all(event.is_set() for event in stopped) and bridge.owner._closed
        assert not bridge._responses
    asyncio.run(run())


def test_response_source_transfer_preserves_child_retry_and_acknowledged_engine():
    from test_application_chat_contract import backend
    async def run():
        bridge=backend()
        calls=[]
        async def child():
            calls.append('child')
        receipt=CleanupPendingError(('resource',),child)
        class Source:
            async def aclose(self):
                calls.append('source')
                raise receipt
        owner=ResponseCleanup(bridge,'absent')
        owner.source=Source()
        bridge._responses['absent']=owner
        with pytest.raises(CleanupPendingError) as error:
            await owner.aclose()
        await error.value.retry()
        assert calls == ['source','child'] and not bridge._responses
        await bridge.aclose()
    asyncio.run(run())


def test_interrupted_backend_shutdown_retries_only_unacknowledged_auth_phase(monkeypatch):
    from test_application_chat_contract import backend
    async def run():
        bridge=backend()
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def close_engine():
            calls.append('engine')
            entered.set()
            await release.wait()
        monkeypatch.setattr(bridge.lifecycle,'aclose',close_engine)
        operation=asyncio.create_task(bridge.aclose())
        await entered.wait()
        operation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await operation
        assert calls == ['engine'] and not bridge.owner._closed
        await error.value.cleanup_pending.retry()
        assert calls == ['engine'] and bridge.owner._closed
    asyncio.run(run())


@pytest.mark.parametrize('path',[None,'/custom','/app/agent/chat'])
def test_configured_injected_handler_keeps_every_path_and_cleanup(path):
    from api_agent.api import create_application
    async def run():
        create_application(Runtime('example'))
        calls=[]
        scope={'type':'http'}
        if path is not None:
            scope['path']=path
        original=dict(scope)
        async def dispose():
            calls.append('closed')
        async def injected(request_scope,receive,send):
            assert request_scope['api_agent_runtime'].pack_id == 'example'
            assert receive is None and send is None
            calls.append('handled')
            request_scope['api_agent_http_cleanup'].append(dispose)
        await Application(Runtime('example'),injected)(scope,None,None)
        assert calls == ['handled','closed'] and scope == original
    asyncio.run(run())


@pytest.mark.parametrize('path',[None,'/custom','/app/agent/chat'])
def test_unconfigured_injected_handler_is_independent_in_fresh_process(path):
    import os
    import subprocess
    import sys
    script='''
import asyncio
from django.conf import settings
from api_agent.api.application import Application, Runtime
assert not settings.configured
scope={'type':'http'}
path=PATH_VALUE
if path is not None:
    scope['path']=path
original=dict(scope)
calls=[]
async def dispose():
    calls.append('closed')
async def injected(request_scope,receive,send):
    assert request_scope['api_agent_runtime'].pack_id == 'example'
    assert receive is None and send is None
    calls.append('handled')
    request_scope['api_agent_http_cleanup'].append(dispose)
asyncio.run(Application(Runtime('example'),injected)(scope,None,None))
assert calls == ['handled','closed'] and scope == original
assert not settings.configured
'''.replace('PATH_VALUE',repr(path))
    result=subprocess.run([sys.executable,'-c',script],env=dict(os.environ),capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stdout+result.stderr
