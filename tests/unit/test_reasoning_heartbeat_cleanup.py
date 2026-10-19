import asyncio
import httpx
import pytest
from api_agent.providers.control import CleanupPendingError
from api_agent.testing import reasoning_heartbeat as h
from api_agent.testing.inspection_controls import ReceiptInspection


def test_cancel_arriving_after_chat_completion_is_preserved(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();cleaning=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    finished.set()
            await entered.wait()
            return httpx.Response(200,json={'actual':'chat'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            owner=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'}))
            try:
                await asyncio.wait_for(cleaning.wait(),2)
                owner.cancel()
                await asyncio.sleep(0)
                release.set()
                cancelled=False
                try:result=await owner
                except asyncio.CancelledError:cancelled=True
                assert finished.is_set()
                assert cancelled, 'fresh caller cancellation during heartbeat disposal was swallowed'
            finally:
                release.set()
                await asyncio.gather(owner,return_exceptions=True)
    asyncio.run(scenario())


def test_pending_heartbeat_disposal_is_not_acknowledged_by_success(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();retry_calls=[]
        async def retry():retry_calls.append('ack')
        pending=CleanupPendingError((object(),),retry)
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:raise pending
            await entered.wait()
            return httpx.Response(200,json={'actual':'chat'})
        error=None
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            try:result=await h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'})
            except BaseException as caught:error=caught
        assert error is not None, 'success returned while heartbeat disposal retained an unacknowledged owner'
        from api_agent.testing.inspection_controls import ReceiptInspection
        consumer=ReceiptInspection(error)
        await consumer.aclose()
        assert retry_calls==['ack'] and consumer.closed
    asyncio.run(scenario())


@pytest.mark.parametrize('caller_cancel',[False,True])
def test_cancelled_heartbeat_receipt_survives_task_collection(monkeypatch,caller_cancel):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();closing=asyncio.Event();release=asyncio.Event();ack=[]
        async def retry():ack.append('owned')
        pending=CleanupPendingError((object(),),retry)
        interrupted=asyncio.CancelledError('heartbeat disposal')
        interrupted.cleanup_pending=pending
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:
                    closing.set()
                    await release.wait()
                    raise interrupted
            await entered.wait()
            return httpx.Response(200,json={'actual':'chat'})
        error=None
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            owner=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'}))
            try:
                await asyncio.wait_for(closing.wait(),2)
                if caller_cancel:
                    owner.cancel('outer caller')
                    await asyncio.sleep(0)
                release.set()
                try:await owner
                except BaseException as caught:error=caught
            finally:
                release.set()
                await asyncio.gather(owner,return_exceptions=True)
        assert error is not None, 'task collection erased pending ownership from CancelledError'
        if caller_cancel:
            assert isinstance(error,asyncio.CancelledError) and error.args==('outer caller',)
        consumer=ReceiptInspection(error)
        await consumer.aclose()
        assert ack==['owned'] and consumer.closed
    asyncio.run(scenario())


def test_fresh_cleanup_cancel_preserves_http_failure_and_all_receipts(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();closing=asyncio.Event();release=asyncio.Event();ack=[]
        async def old_retry():ack.append('prior')
        async def heartbeat_retry():ack.append('heartbeat')
        prior_owner=object();heartbeat_owner=object()
        failure=httpx.HTTPStatusError('synthetic chat error',request=httpx.Request('POST','http://127.0.0.1:1'),response=httpx.Response(502))
        failure.cleanup_pending=CleanupPendingError((prior_owner,),old_retry)
        pending=CleanupPendingError((heartbeat_owner,),heartbeat_retry)
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:
                    closing.set();await release.wait();raise pending
            await entered.wait();raise failure
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            owner=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'}))
            try:
                await closing.wait();owner.cancel('first interruption');await asyncio.sleep(0)
                owner.cancel('repeated interruption');await asyncio.sleep(0);release.set()
                with pytest.raises(asyncio.CancelledError) as caught:await owner
            finally:
                release.set();await asyncio.gather(owner,return_exceptions=True)
        assert caught.value.args==('first interruption',)
        assert caught.value.operation_error is failure
        assert prior_owner in caught.value.retained_owners and heartbeat_owner in caught.value.retained_owners
        consumer=ReceiptInspection(caught.value);await consumer.aclose();await consumer.aclose()
        assert consumer.closed and ack==['prior','heartbeat']
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['deadline','cancel','outer-failure'])
def test_six_failed_calls_retain_each_heartbeat_disposal_once(monkeypatch,mode):
    from contextlib import asynccontextmanager
    from api_agent.testing import reasoning_contrast as contrast
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();closing=asyncio.Event();release=asyncio.Event()
        active=[];disposing=[];ack=[];scopes=[];chats=[]
        orchestration_error=RuntimeError('synthetic outer orchestration failure')
        if mode=='outer-failure':
            original_shield=asyncio.shield;first=True
            def shield(work):
                nonlocal first
                if first:
                    first=False
                    async def fail_after_admission():
                        await entered.wait()
                        raise orchestration_error
                    return original_shield(fail_after_admission())
                return original_shield(work)
            monkeypatch.setattr(contrast.asyncio,'shield',shield)
        @asynccontextmanager
        async def controlled_deadline(name,timeout):
            assert timeout==150
            async with asyncio.timeout(None) as scope:
                scopes.append(scope)
                yield scope
        if mode=='deadline':monkeypatch.setattr(contrast,'component_timeout',controlled_deadline)
        async def reply(request):
            import json
            identifier=json.loads(request.content)['session_id']
            if request.url.path.endswith('/heartbeat'):
                active.append(identifier)
                if len(active)==6:entered.set()
                async def retry():ack.append(identifier)
                pending=CleanupPendingError((object(),),retry)
                try:await asyncio.Event().wait()
                finally:
                    disposing.append(identifier)
                    if len(disposing)==6:closing.set()
                    await release.wait()
                    raise pending
            chats.append(identifier)
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            sender=asyncio.create_task(contrast.run_pairs(client,'http://127.0.0.1:1','nonce'))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                    if mode=='deadline':
                        for scope in scopes:scope.reschedule(asyncio.get_running_loop().time())
                    elif mode=='cancel':sender.cancel('initial suite interruption')
                    await closing.wait()
                    if mode=='cancel':
                        sender.cancel('repeated suite interruption');await asyncio.sleep(0)
                    elif mode=='outer-failure':
                        sender.cancel('fresh suite interruption');await asyncio.sleep(0)
                    release.set()
                    with pytest.raises(TimeoutError if mode=='deadline' else asyncio.CancelledError) as caught:await sender
            finally:
                release.set();await asyncio.gather(sender,return_exceptions=True)
        assert len(chats)==len(set(chats))==6
        if mode=='cancel':assert caught.value.args==('initial suite interruption',)
        if mode=='outer-failure':
            assert caught.value.args==('fresh suite interruption',)
            assert caught.value.operation_error is orchestration_error
        consumer=ReceiptInspection(caught.value);await consumer.aclose();await consumer.aclose()
        assert consumer.closed and len(ack)==len(set(ack))==6 and set(ack)==set(chats)
    asyncio.run(scenario())


def test_primary_receipt_identity_and_distinct_heartbeat_duty_survive(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();ack=[]
        async def first_retry():ack.append('chat')
        async def second_retry():ack.append('heartbeat')
        first=CleanupPendingError((object(),),first_retry)
        second=CleanupPendingError((object(),),second_retry)
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:raise second
            await entered.wait();raise first
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(CleanupPendingError) as caught:await h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'})
        assert caught.value is first
        consumer=ReceiptInspection(caught.value);await consumer.aclose();await consumer.aclose()
        assert consumer.closed and ack==['chat','heartbeat']
    asyncio.run(scenario())


def test_chat_completion_does_not_cancel_heartbeat_timeout_disposal_twice(monkeypatch):
    from contextlib import asynccontextmanager
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();closing=asyncio.Event();draining=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
        scopes=[];second_cancel=[];original=h._drain
        @asynccontextmanager
        async def controlled_timeout(name,timeout):
            assert timeout==10
            async with asyncio.timeout(None) as scope:
                scopes.append(scope);yield scope
        monkeypatch.setattr(h,'component_timeout',controlled_timeout)
        async def drain(tasks):
            draining.set()
            return await original(tasks)
        monkeypatch.setattr(h,'_drain',drain)
        async def reply(request):
            if request.url.path.endswith('/heartbeat'):
                entered.set()
                try:await asyncio.Event().wait()
                finally:
                    closing.set()
                    try:await release.wait()
                    except asyncio.CancelledError:second_cancel.append(True);raise
                    finished.set()
            await closing.wait();return httpx.Response(200,json={'actual':'chat'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            owner=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',{'session_id':'session'}))
            try:
                async with asyncio.timeout(5):
                    await entered.wait();scopes[0].reschedule(asyncio.get_running_loop().time())
                    await draining.wait();release.set();result=await owner
            finally:
                release.set();await asyncio.gather(owner,return_exceptions=True)
        assert result.status_code==200 and scopes[0].expired()
        assert finished.is_set() and not second_cancel
    asyncio.run(scenario())
