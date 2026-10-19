"""Buffered application requests must maintain the existing chat lease."""
import asyncio
from types import SimpleNamespace
import json
from uuid import uuid4

import httpx
import pytest

from api_agent.api.chat_lifecycle import ChatLifecycle
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing import reasoning_heartbeat as h
from api_agent.testing.reasoning_contrast import MODEL, evaluate, run_pairs
from api_agent.testing.studio_http import agent_endpoint
from api_agent.testing.studio_retained import RetainedStudio


class BufferedProvider(RetainedStudio):
    def __init__(self):
        self.admitted=asyncio.Event();self.entered=asyncio.Event();self.release=asyncio.Event();self.calls=[];self.stopped=[];self.closed=False
    async def describe(self):
        return (AvailableSelection(MODEL,features=('reasoning_effort',),reasoning_efforts=('low','high')),)
    async def complete(self,request):
        self.calls.append(request)
        self.admitted.set()
        if len(self.calls)==6:self.entered.set()
        try:await self.release.wait()
        finally:self.stopped.append(request.request_id)
        count={'low':100,'high':175}[request.payload.generation.reasoning_effort]
        return LLMResult(Message('assistant',(TextContent('arbitrary'),)),Usage(1,200,reasoning_tokens=count))
    async def aclose(self):self.closed=True


def controlled_lifecycle(monkeypatch):
    clock=SimpleNamespace(now=0,lifecycles=[],acknowledged={},waiters={})
    monkeypatch.setattr('api_agent.api.chat_lifecycle.time',SimpleNamespace(monotonic=lambda:clock.now))
    def create():
        lifecycle=ChatLifecycle()  # The real ten-second interval and thirty-second expiry.
        original=lifecycle.heartbeat
        def heartbeat(owner,session):
            status=original(owner,session)
            if status=='running':
                acknowledged=clock.acknowledged.setdefault(clock.now,set())
                acknowledged.add(session)
                if len(acknowledged)==6:
                    clock.waiters.setdefault(clock.now,asyncio.Event()).set()
            return status
        lifecycle.heartbeat=heartbeat
        clock.lifecycles.append(lifecycle)
        return lifecycle
    monkeypatch.setattr('api_agent.api.studio.ChatLifecycle',create)
    monkeypatch.setattr(h,'_INTERVAL',.001)
    return clock


def advance(clock,seconds):
    clock.now+=seconds
    for lifecycle in clock.lifecycles:
        for turn in lifecycle._turns.values():turn._wake.set()


def test_unmaintained_buffered_request_expires_with_cancelled_409(monkeypatch,record_property):
    clock=controlled_lifecycle(monkeypatch)
    async def scenario():
        provider=BufferedProvider();identifier=str(uuid4())
        async with agent_endpoint(provider,MODEL,'http://127.0.0.1:8080','x'*32) as endpoint:
            async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
                task=asyncio.create_task(client.post(endpoint+'/app/agent/chat',
                    headers={'Origin':'http://127.0.0.1:8080','X-API-Agent-Session':'x'*32},
                    json={'session_id':identifier,'model':MODEL,'reasoning_effort':'low',
                          'messages':[{'role':'user','content':'synthetic lease witness'}]}))
                async with asyncio.timeout(5):
                    await provider.admitted.wait()
                    advance(clock,31)
                    result=await task
            assert result.status_code==409
            assert result.json()=={'session_id':identifier,'code':'cancelled'}
            record_property('baseline_response',json.dumps({'status':result.status_code,'body':result.json()}))
            assert provider.stopped==[identifier]
            assert all(turn.done.is_set() for turn in clock.lifecycles[0]._turns.values())
        assert provider.closed
    asyncio.run(scenario())


def test_six_buffered_calls_survive_multiple_expiry_windows(monkeypatch):
    clock=controlled_lifecycle(monkeypatch)
    async def scenario():
        provider=BufferedProvider()
        async with agent_endpoint(provider,MODEL,'http://127.0.0.1:8080','x'*32) as endpoint:
            async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
                sender=asyncio.create_task(run_pairs(client,endpoint,'x'*32))
                try:
                    async with asyncio.timeout(5):
                        await provider.entered.wait()
                        for phase in range(7):
                            await clock.waiters.setdefault(clock.now,asyncio.Event()).wait()
                            assert len(clock.acknowledged[clock.now])==6
                            if phase<6:advance(clock,20)
                        assert clock.now==120
                        assert len(provider.calls)==6 and not provider.stopped
                        assert all(turn.state=='running' for turn in clock.lifecycles[0]._turns.values())
                        provider.release.set()
                        records=await sender
                finally:
                    provider.release.set()
                    if not sender.done():sender.cancel()
                    await asyncio.gather(sender,return_exceptions=True)
            assert evaluate(records)=={'passed':True,'differences':[75,75,75]}
            assert len(provider.stopped)==6
        assert provider.closed
    asyncio.run(scenario())


def body():return {'session_id':str(uuid4()),'messages':[]}


@pytest.mark.parametrize('status',['unknown','running','completed','failed','cancelled','cleanup_pending'])
def test_heartbeat_status_does_not_replace_authoritative_chat_response(monkeypatch,status):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        request=body();beat=asyncio.Event();release=asyncio.Event();sent=[]
        async def reply(message):
            sent.append(message)
            if message.url.path.endswith('/heartbeat'):
                beat.set()
                return httpx.Response(200,json={'session_id':request['session_id'],'status':status})
            await release.wait()
            return httpx.Response(200,json={'authoritative':'chat'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            task=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',request))
            await beat.wait();release.set()
            result=await task
        assert result.json()=={'authoritative':'chat'}
        assert all(row.headers['X-API-Agent-Session']=='nonce' for row in sent)
        assert all(row.headers['Origin']=='http://127.0.0.1:8080' for row in sent)
        beats=[row for row in sent if row.url.path.endswith('/heartbeat')]
        assert all(json.loads(row.content)=={'session_id':request['session_id']} for row in beats)
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['http','identity','shape','status','status-type','extra','missing','json','exception'])
def test_failed_heartbeat_preserves_failure_and_drains_chat(monkeypatch,case):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        request=body();stopped=asyncio.Event();failure=RuntimeError('synthetic heartbeat failure')
        async def reply(message):
            if message.url.path.endswith('/heartbeat'):
                if case=='exception':raise failure
                if case=='http':return httpx.Response(503,text='untrusted response text')
                if case=='json':return httpx.Response(200,text='untrusted response text')
                value={'session_id':request['session_id'],'status':'running'}
                if case=='identity':value['session_id']=str(uuid4())
                if case=='shape':value=[]
                if case=='status':value['status']='invented'
                if case=='status-type':value['status']=True
                if case=='extra':value['extra']=1
                if case=='missing':del value['session_id']
                return httpx.Response(200,json=value)
            try:await asyncio.Event().wait()
            finally:stopped.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            expected=RuntimeError if case=='exception' else httpx.HTTPStatusError if case=='http' else ValueError
            with pytest.raises(expected) as caught:await h.post_chat(client,'http://127.0.0.1:1','nonce',request)
        assert stopped.is_set()
        if case=='exception':assert caught.value is failure
        assert 'untrusted response text' not in str(caught.value)
    asyncio.run(scenario())


def test_cancellation_drains_chat_and_inflight_heartbeat(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        entered=asyncio.Event();stopped=[]
        async def reply(message):
            if message.url.path.endswith('/heartbeat'):entered.set()
            try:await asyncio.Event().wait()
            finally:stopped.append(message.url.path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            task=asyncio.create_task(h.post_chat(client,'http://127.0.0.1:1','nonce',body()))
            await entered.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        assert sorted(stopped)==['/app/agent/chat','/app/agent/chat/heartbeat']
    asyncio.run(scenario())


@pytest.mark.parametrize('stage',['chat','heartbeat'])
def test_scheduling_failure_closes_work_and_drains_request(monkeypatch,stage):
    async def scenario():
        original=asyncio.create_task;rejected=[];owned=[];attempts=[]
        def schedule(work,*args,**kwargs):
            if ((stage=='heartbeat' and work.__qualname__.endswith('post_chat.<locals>.heartbeat'))
                    or (stage=='chat' and work.__qualname__.endswith('post_chat.<locals>.chat'))):
                rejected.append(work)
                raise RuntimeError('synthetic heartbeat scheduling failure')
            task=original(work,*args,**kwargs);owned.append(task);return task
        monkeypatch.setattr(h.asyncio,'create_task',schedule)
        async def reply(message):attempts.append(message);return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(RuntimeError,match='scheduling'):await h.post_chat(client,'http://127.0.0.1:1','nonce',body())
        assert not attempts and all(task.done() for task in owned)
        assert rejected[0].cr_frame is None
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome',['success','http-error','transport-error'])
def test_completed_chat_wins_over_stale_wait_snapshot(monkeypatch,outcome):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        release=asyncio.Event();original_wait=asyncio.wait;failure=RuntimeError('synthetic chat transport failure')
        async def reply(message):
            if message.url.path.endswith('/heartbeat'):return httpx.Response(503)
            await release.wait()
            if outcome=='transport-error':raise failure
            return httpx.Response(200 if outcome=='success' else 502,json={'from':'chat'})
        async def stale_wait(tasks,**kwargs):
            done,pending=await original_wait(tasks,**kwargs)
            assert tasks[1] in done and not tasks[0].done()
            release.set()
            await asyncio.gather(tasks[0],return_exceptions=True)
            return done,pending  # Preserve the older snapshot despite completed chat.
        monkeypatch.setattr(h.asyncio,'wait',stale_wait)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            if outcome=='transport-error':
                with pytest.raises(RuntimeError) as caught:await h.post_chat(client,'http://127.0.0.1:1','nonce',body())
                assert caught.value is failure
            else:
                result=await h.post_chat(client,'http://127.0.0.1:1','nonce',body())
                assert result.status_code==(200 if outcome=='success' else 502)
                assert result.json()=={'from':'chat'}
    asyncio.run(scenario())


def test_stalled_heartbeat_io_fails_and_drains_request(monkeypatch):
    from api_agent.providers.progress import component_timeout
    monkeypatch.setattr(h,'_INTERVAL',.001)
    monkeypatch.setattr(h,'component_timeout',lambda name,timeout:component_timeout(name,.01))
    async def scenario():
        stopped=[]
        async def reply(message):
            try:await asyncio.Event().wait()
            finally:stopped.append(message.url.path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(TimeoutError):await h.post_chat(client,'http://127.0.0.1:1','nonce',body())
        assert sorted(stopped)==['/app/agent/chat','/app/agent/chat/heartbeat']
    asyncio.run(scenario())


def test_pre_admission_unknown_heartbeat_is_valid_over_real_http(monkeypatch):
    controlled_lifecycle(monkeypatch)
    async def scenario():
        catalog=asyncio.Event();unknown=asyncio.Event()
        class PendingCatalog(BufferedProvider):
            async def describe(self):
                await catalog.wait()
                return await super().describe()
        provider=PendingCatalog();provider.release.set();identifier=str(uuid4())
        async def observe(response):
            if response.request.url.path.endswith('/heartbeat'):
                await response.aread()
                if response.json()['status']=='unknown':unknown.set()
        async with agent_endpoint(provider,MODEL,'http://127.0.0.1:8080','x'*32) as endpoint:
            async with httpx.AsyncClient(timeout=5,trust_env=False,event_hooks={'response':[observe]}) as client:
                sender=asyncio.create_task(h.post_chat(client,endpoint,'x'*32,
                    {'session_id':identifier,'model':MODEL,'reasoning_effort':'low',
                     'messages':[{'role':'user','content':'synthetic admission witness'}]}))
                try:
                    async with asyncio.timeout(5):
                        await unknown.wait()
                        assert not provider.calls
                        catalog.set()
                        response=await sender
                finally:
                    catalog.set()
                    if not sender.done():sender.cancel()
                    await asyncio.gather(sender,return_exceptions=True)
            assert response.status_code==200 and response.json()['session_id']==identifier
            assert len(provider.calls)==1
    asyncio.run(scenario())


def test_terminal_heartbeat_awaits_original_chat_accounting(monkeypatch):
    monkeypatch.setattr(h,'_INTERVAL',.001)
    async def scenario():
        request=body();release=asyncio.Event();original_wait=asyncio.wait
        async def reply(message):
            if message.url.path.endswith('/heartbeat'):
                return httpx.Response(200,json={'session_id':request['session_id'],'status':'completed'})
            await release.wait()
            return httpx.Response(200,json={'usage':{'reasoning_tokens':123}})
        async def terminal_first(tasks,**kwargs):
            result=await original_wait(tasks,**kwargs)
            assert tasks[1].done() and not tasks[0].done()
            asyncio.get_running_loop().call_soon(release.set)
            return result
        monkeypatch.setattr(h.asyncio,'wait',terminal_first)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            result=await h.post_chat(client,'http://127.0.0.1:1','nonce',request)
        assert result.json()=={'usage':{'reasoning_tokens':123}}
    asyncio.run(scenario())
