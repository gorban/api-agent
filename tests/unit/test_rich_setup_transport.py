"""Controlled rich HTTP behind real loopback IPC, with caller budgets."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from api_agent.providers.llm import CompletionRequest,LLMRequest,Message,TextContent,Tool
from api_agent.providers.contracts import ProviderAccessError, ContractError
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient,SetupServer,_encode,_decode
from api_agent.providers.progress import component_timeout, report_timeout_sync, progress_sink, _scope
from api_agent.llm_providers.gpt.inference import complete_text

CAP='synthetic-rich-inference-capability-000000'
SECRET='synthetic-private-token'
SCHEMA=json.dumps(dict(type='object',properties={},required=[],additionalProperties=False))
REQUEST=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),),(Tool('lookup','Lookup',SCHEMA),)))


@pytest.mark.parametrize('parallel',[False,True])
def test_real_ipc_http_join_preserves_tools_usage_and_parent_budget(parallel):
    async def scenario():
        sent=[]
        scopes=[]
        def respond(request):
            sent.append(request)
            return httpx.Response(200,json=dict(status='completed',output=[dict(type='function_call',status='completed',call_id='call-1',name='lookup',arguments='{}')],usage=dict(input_tokens=7,output_tokens=2)))
        async def complete(request):
            scopes.append(_scope.get())
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await complete_text(client=client,base_url='https://api.openai.com/v1',access_token=SECRET,subscription=False,api_mode='responses',request=request)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('operation','synthetic-selection',lambda:owner)
        server=SetupServer(registry,CAP)
        endpoint=await server.start()
        clients=[SetupClient(endpoint,worker_id=f'worker-{i}',timeout=10) for i in range(2)]
        events=[]
        parents=[]
        def invoke(client):
            with progress_sink(events.append), report_timeout_sync('Synthetic graph node',2) as budget:
                parents.append(budget.id)
                return client.complete(REQUEST)
        try:
            if parallel:
                results=await asyncio.gather(*(asyncio.to_thread(invoke,c) for c in clients))
            else:
                results=[await asyncio.to_thread(invoke,c) for c in clients]
            assert [r.outcome for r in results] == ['tool_calls']*2
            assert [r.usage.input_tokens for r in results] == [7]*2
            assert len(sent)==2
            assert all(SECRET not in req.content.decode() for req in sent)
            assert all(scope.remaining <= 2 for scope in scopes)
            assert set(parents) <= {e.parent_id for e in events}
            assert all(SECRET not in repr(e) for e in events)
            assert not set(parents) & server._ids.keys()
        finally:
            for client in clients:
                await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_ipc_call_budget_cancels_only_inference_and_preserves_shared_owner():
    async def scenario():
        entered=asyncio.Event()
        stopped=asyncio.Event()
        async def complete(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('operation','synthetic-selection',lambda:owner)
        server=SetupServer(registry,CAP)
        endpoint=await server.start()
        client=SetupClient(endpoint,timeout=10)
        def invoke():
            with report_timeout_sync('Synthetic graph node',.1):
                return client.complete(REQUEST)
        try:
            with pytest.raises((ContractError,ProviderAccessError)):
                await asyncio.to_thread(invoke)
            await asyncio.wait_for(stopped.wait(),1)
            assert entered.is_set()
            owner.aclose.assert_not_awaited()
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_transport_rejects_invalid_call_ancestry_before_dispatch():
    server=SetupServer(SimpleNamespace(),CAP)
    frame=dict(version=1,capability=CAP,action='complete',timeout=1,parent_id='private/path',binding={},request={})
    with pytest.raises(ContractError,match='ancestry'):
        server._request(frame)


def test_provider_error_wire_preserves_execution_usage_and_rejects_bad_execution():
    from api_agent.providers.setup_transport import _error,_raise_error
    from api_agent.providers.llm import Usage
    error=ProviderAccessError('authorization','Rejected',usage=Usage(4,2))
    error.execution='possibly_executed'
    frame=_error(error)
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert caught.value.execution == 'possibly_executed'
    assert caught.value.usage == Usage(4,2)
    frame['details']['execution']='unsafe'
    with pytest.raises(ContractError):
        _raise_error(frame)


def test_short_ipc_waiter_does_not_shorten_sibling_shared_authentication():
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    from api_agent.providers.llm import LLMResult,ToolCall
    async def scenario():
        auth_started=asyncio.Event()
        auth_complete=asyncio.Event()
        session=AsyncMock(return_value=SECRET)
        async def setup(**kwargs):
            auth_started.set()
            await asyncio.sleep(.15)
            auth_complete.set()
            return session
        adapter=SimpleNamespace(asetup=AsyncMock(side_effect=setup))
        result=LLMResult(Message('assistant',calls=(ToolCall('call-1','lookup','{}'),)),outcome='tool_calls')
        owner=SharedSetupOwner('operation','selection',lambda credentials:LLMSessionOwner(adapter),complete=AsyncMock(return_value=result))
        registry=SetupRegistry('operation','selection',lambda:owner)
        server=SetupServer(registry,CAP)
        endpoint=await server.start()
        clients=[SetupClient(endpoint,worker_id=f'worker-{i}',timeout=2) for i in range(2)]
        def short_call():
            with report_timeout_sync('Short graph node',.05):
                with pytest.raises((ContractError,ProviderAccessError)):
                    clients[0].complete(REQUEST)
        try:
            short=asyncio.create_task(asyncio.to_thread(short_call))
            await asyncio.wait_for(auth_started.wait(),1)
            sibling=asyncio.create_task(asyncio.to_thread(clients[1].complete,REQUEST))
            await short
            assert await sibling == result
            assert auth_complete.is_set()
            adapter.asetup.assert_awaited_once()
        finally:
            for client in clients:
                await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
        session.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('expired',[False,True])
def test_exchange_respects_untimed_ancestry_and_exhausted_budget_before_writing(expired):
    from api_agent.providers.progress import TimeoutBudget,_clock
    from api_agent.providers.inference import request_to_wire,result_to_wire
    from api_agent.providers.llm import LLMResult,ToolCall
    from uuid import uuid4
    async def scenario():
        descriptor=dict(version=1,host='127.0.0.1',port=12345,capability=CAP)
        client=SetupClient(descriptor)
        writer=SimpleNamespace(write=Mock(),drain=AsyncMock())
        client._writer=writer
        client._reader=asyncio.StreamReader()
        result=LLMResult(Message('assistant',calls=(ToolCall('call-1','lookup','{}'),)),outcome='tool_calls')
        client._reader.feed_data(_encode(dict(version=1,status='completed',result=result_to_wire(result))))
        now=_clock()
        budget=TimeoutBudget(str(uuid4()),None,now,now-1 if expired else None,1 if expired else None)
        token=_scope.set(budget)
        try:
            if expired:
                with pytest.raises(ProviderAccessError,match='deadline expired'):
                    await client._exchange('complete',request_to_wire(REQUEST))
                writer.write.assert_not_called()
            else:
                assert await client._exchange('complete',request_to_wire(REQUEST)) == result
                frame=_decode(writer.write.call_args.args[0])
                assert frame['timeout']==client._timeout
                assert frame['parent_id']==budget.id
        finally:
            _scope.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize('subscription',[False,True])
def test_reasoning_continuation_crosses_real_ipc_and_http_without_content_evidence(subscription):
    from api_agent.providers.llm import ToolResult
    from api_agent.testing.inference_content import content_boundary,export_content
    async def scenario():
        sent=[]
        encrypted='opaque-synthetic-encrypted-continuation'
        output=[dict(type='reasoning',id='reason-1',summary=[],encrypted_content=encrypted),dict(type='function_call',id='call-item-1',call_id='call-1',name='lookup',arguments='{}',status='completed')]
        def respond(req):
            body=json.loads(req.content)
            sent.append(body)
            if len(sent)==1:
                assert body['include']==['reasoning.encrypted_content'] and body['store'] is False
                data=dict(status='completed',output=output,usage=dict(input_tokens=7,output_tokens=2))
            else:
                assert body['input'][1:3]==output
                assert body['input'][3]==dict(type='function_call_output',call_id='call-1',output='Visible tool result')
                data=dict(status='completed',output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='Visible final answer')])],usage=dict(input_tokens=12,output_tokens=3))
            if subscription:
                return httpx.Response(200,text='data: '+json.dumps(dict(type='response.completed',response=data))+'\n\n',headers={'content-type':'text/event-stream'})
            return httpx.Response(200,json=data)
        async def complete(request):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await complete_text(client=client,base_url='https://api.openai.com/v1',access_token=SECRET,subscription=subscription,api_mode='responses',request=request)
        boundary=content_boundary({'LOG_CONTENT':True})
        from api_agent.providers.shared_setup import SharedSetupOwner
        from api_agent.workflows.inference_capture import bind_boundary
        async def authorized(request, *, auth):
            assert auth == SECRET
            return await complete(request)
        async def authenticate(callback):
            return await callback(auth=SECRET)
        shared=SharedSetupOwner('operation', 'selected', lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()), complete=authorized)
        async def captured(request):
            bind_boundary(boundary)
            return await shared.complete(request)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=captured,aclose=shared.aclose)
        registry=SetupRegistry('operation','selection',lambda:owner)
        server=SetupServer(registry,CAP)
        endpoint=await server.start()
        client=SetupClient(endpoint,timeout=2)
        try:
            first=await asyncio.to_thread(client.complete,REQUEST)
            assert first.payload.continuation is not None
            first_evidence=json.dumps(client.evidence)
            second_req=LLMRequest('gpt-synthetic',CompletionRequest(REQUEST.payload.messages+(first.payload,Message('tool',results=(ToolResult('call-1',(TextContent('Visible tool result'),)),))),REQUEST.payload.tools))
            final=await asyncio.to_thread(client.complete,second_req)
            second_evidence=json.dumps(client.evidence)
            assert final.payload.content[0].text=='Visible final answer'
            assert final.usage.input_tokens==12
            assert encrypted not in first_evidence+second_evidence
            # The request may name the include option; no encrypted payload field is retained.
            assert '"encrypted_content":' not in first_evidence+second_evidence
            assert 'Visible tool result' in second_evidence and 'Visible final answer' in second_evidence
            assert SECRET not in first_evidence+second_evidence
            assert len(sent)==2
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_ipc_declines_retry_wait_that_cannot_fit_parent_deadline():
    import time
    from api_agent.providers.llm import Usage
    from api_agent.providers.shared_setup import SharedSetupOwner

    async def scenario():
        calls = []
        async def invoke(callback):
            return await callback(auth='selected')
        async def complete(request, *, auth):
            calls.append(request)
            error = ProviderAccessError('rate_limited', 'Synthetic upstream failure',
                                        status=429, usage=Usage(7, 1))
            error.http_status = 429
            error.retry_after = 1728
            error.billing = {'usd': 0.1, 'request_id': None, 'generation_id': None}
            raise error
        owner = SharedSetupOwner('operation', 'synthetic-selection',
            lambda _: SimpleNamespace(call=invoke, aclose=AsyncMock()), complete=complete)
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        client = SetupClient(await server.start(), timeout=3)

        def call():
            with report_timeout_sync('Synthetic graph node', .5):
                return client.complete(REQUEST)
        try:
            started = time.monotonic()
            with pytest.raises(ProviderAccessError) as caught:
                await asyncio.to_thread(call)
            elapsed = time.monotonic() - started
            assert elapsed < .5
            assert caught.value.status == 429
            assert caught.value.message == 'Synthetic upstream failure'
            assert caught.value.usage == Usage(7, 1)
            assert calls == [REQUEST]
            ledger = client.transport_attempts
            assert ledger['complete'] is True
            assert len(ledger['attempts']) == 1
            attempt = ledger['attempts'][0]
            assert attempt['status'] == 429 and attempt['outcome'] == 'failed'
            assert attempt['retry_delay_seconds'] is None
            assert attempt['usage']['input_tokens'] == 7
            assert attempt['billing']['usd'] == .1
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())
