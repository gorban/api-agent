"""Prove overlap below the shared IPC connection, with synthetic inference only."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer
from api_agent.testing.provider_setup import RemoteIntegrationLLM
from api_agent.testing.reasoning_provider import reasoning_provider
from api_agent.testing.reasoning_contrast import MODEL, evaluate, run_pairs
from api_agent.testing.studio_http import agent_endpoint


def test_six_application_requests_overlap_one_backend_owner(monkeypatch):
    usage=[];content=[];owners=[];native=[];events=[]
    monkeypatch.setattr('api_agent.testing.inference_usage.publish',
        lambda request, result=None, **kwargs: usage.append((request,result,kwargs)) or str(uuid4()))
    monkeypatch.setattr('api_agent.testing.inference_content.publish_content',
        lambda evidence, identifier: content.append((evidence,identifier)))
    async def scenario():
        entered=asyncio.Event()
        class Owner:
            def __init__(self):owners.append(self)
            async def ensure_ready(self):pass
            async def describe(self):
                return (AvailableSelection(MODEL,features=('reasoning_effort',),reasoning_efforts=('low','medium')),)
            async def complete(self, request):
                from api_agent.llm_providers.gpt.inference import validate_text_request
                _,body=validate_text_request(base_url='https://api.openai.com/v1',access_token='synthetic-token',
                    subscription=True,api_mode='responses',request=request)
                native.append(body)
                if len(native)==6:entered.set()
                async with asyncio.timeout(3):await entered.wait()
                effort=body['reasoning']['effort']
                return LLMResult(Message('assistant',(TextContent(body['input'][0]['content']),)),
                    Usage(1,200,reasoning_tokens=100 if effort=='low' else 175))
            def quote_cost(self, request, result):
                return {'list_cost_usd':result.usage.reasoning_tokens/10000,'payment':None}
            def export_content(self, request, result=None):
                return ({'phase':'request','content':{'marker':request.payload.messages[0].content[0].text}},)
            async def aclose(self):pass
        registry=SetupRegistry('synthetic-parallel-operation','synthetic-identity',Owner)
        server=SetupServer(registry,'synthetic-parallel-capability-000000')
        remote=RemoteIntegrationLLM(await server.start())
        try:
            async with reasoning_provider(remote) as parallel, agent_endpoint(parallel,MODEL,'http://127.0.0.1:8080','x'*32) as endpoint:
                async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
                    records=await run_pairs(client,endpoint,'x'*32,emit=events.append)
            assert evaluate(records)==dict(passed=True,differences=[75]*3)
            assert len(parallel._calls)==0
            assert len(registry._leases)==7  # One catalog lease plus six independent inference leases.
            assert len({lease['binding']['selected_identity'] for lease in registry._leases.values()})==1
            assert sum(lease['revoked'] for lease in registry._leases.values())==6
        finally:
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
        assert all(lease['revoked'] for lease in registry._leases.values())
    asyncio.run(scenario())
    assert len(owners)==1 and len(native)==6
    finished=[row for row in usage if row[1] is not None]
    assert len(finished)==6
    assert all(row[0].payload.messages[0].content[0].text==row[1].payload.content[0].text for row in finished)
    assert len(content)==6
    by_id={row[2]['call_id']:row for row in finished}
    for evidence, identifier in content:
        row=by_id[identifier]
        assert evidence[0]['content']['marker']==row[0].payload.messages[0].content[0].text
        assert row[2]['quote']['list_cost_usd']==row[1].usage.reasoning_tokens/10000


def test_sender_cancellation_drains_every_started_call():
    async def scenario():
        entered=asyncio.Event();blocked=asyncio.Event();finished=[];events=[];count=0
        async def reply(sent):
            nonlocal count
            count+=1
            if count==6:entered.set()
            try:await blocked.wait()
            finally:finished.append(json.loads(sent.content)['session_id'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            task=asyncio.create_task(run_pairs(client,'http://127.0.0.1:1','nonce',emit=events.append))
            async with asyncio.timeout(3):await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        assert len(finished)==len(set(finished))==6
        calls=[row for row in events if row['event']=='call']
        assert len(calls)==6 and all(row['status']=='interrupted' for row in calls)
        assert not any(row['event']=='pair' for row in events)
    asyncio.run(scenario())


@pytest.mark.parametrize('interrupted',[False,True])
def test_failed_child_close_remains_owned_until_retry(monkeypatch,interrupted):
    from types import SimpleNamespace
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing import reasoning_provider as adapter
    children=[]
    class Remote:
        def __init__(self,*args):
            self.client=SimpleNamespace(_descriptor={});self.config={};self.closed=0
            children.append(self)
        def complete(self,request):
            if interrupted:raise asyncio.CancelledError()
            return 'result'
        def close(self):
            self.closed+=1
            if self.closed<=2:raise CleanupPendingError((self,),self.retry)
        async def retry(self):self.close()
    monkeypatch.setattr(adapter,'RemoteIntegrationLLM',Remote)
    parent=Remote();parallel=adapter._ReasoningProvider(parent)
    expected=asyncio.CancelledError if interrupted else CleanupPendingError
    with pytest.raises(expected) as caught:parallel.complete(object())
    child=children[-1]
    assert parent.closed==0 and child in parallel._calls
    pending=caught.value.cleanup_pending if interrupted else caught.value
    from api_agent.testing.suite_lock import _cleanup_receipts
    assert any(parallel in receipt.owners for receipt in _cleanup_receipts(pending))
    with pytest.raises(CleanupPendingError) as repeated:parallel.close()
    assert any(parallel in receipt.owners for receipt in _cleanup_receipts(repeated.value)) and child in parallel._calls
    asyncio.run(pending.retry())
    parallel._close_call(child)
    parallel._close_call(child)  # Already acknowledged disposal is idempotent.
    assert child.closed==4 and not parallel._calls and parent.closed==0
    parallel.close()
    with pytest.raises(Exception,match='closed'):parallel.complete(object())


def test_endpoint_drains_remote_calls_after_sender_cancellation():
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event();cancelled=asyncio.Event()
        started=[];finished=[]
        class Owner:
            async def ensure_ready(self):pass
            async def describe(self):return (AvailableSelection(MODEL,features=('reasoning_effort',),reasoning_efforts=('low','medium')),)
            async def complete(self,request):
                started.append(request)
                if len(started)==6:entered.set()
                try:await release.wait()
                finally:finished.append(request)
                return LLMResult(Message('assistant',(TextContent('synthetic'),)),Usage(1,200,reasoning_tokens=100))
            async def aclose(self):pass
        registry=SetupRegistry('synthetic-drain-operation','synthetic-identity',Owner)
        server=SetupServer(registry,'synthetic-drain-capability-000000000')
        remote=RemoteIntegrationLLM(await server.start())
        async def consume():
            async with reasoning_provider(remote) as parallel, agent_endpoint(parallel,MODEL,'http://127.0.0.1:8080','x'*32) as endpoint:
                async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
                    sender=asyncio.create_task(run_pairs(client,endpoint,'x'*32))
                    await entered.wait()
                    sender.cancel()
                    with pytest.raises(asyncio.CancelledError):await sender
                    cancelled.set()
            assert not parallel._calls
        consumer=asyncio.create_task(consume())
        try:
            async with asyncio.timeout(5):await cancelled.wait()
            assert len(started)==6 and not finished and not consumer.done()
            release.set()
            async with asyncio.timeout(5):await consumer
            assert len(finished)==6
        finally:
            release.set()
            if not consumer.done():consumer.cancel()
            await asyncio.gather(consumer,return_exceptions=True)
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('interrupted',[False,True])
def test_exit_receipts_preserve_backend_then_child_cleanup(monkeypatch,interrupted):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing import reasoning_provider as adapter
    from api_agent.testing.suite_lock import _cleanup_receipts
    events=[]
    class Borrowed:
        def complete(self,request):raise AssertionError('must not infer')
        def close(self):raise AssertionError('must not close borrowed parent')
    async def backend_retry():events.append('backend')
    backend=CleanupPendingError((object(),),backend_retry)
    primary=asyncio.CancelledError() if interrupted else backend
    if interrupted:primary.cleanup_pending=backend
    async def scenario():
        with pytest.raises(type(primary)) as caught:
            async with adapter.reasoning_provider(Borrowed()) as parallel:
                attempts=0
                async def child_cleanup():
                    nonlocal attempts
                    attempts+=1;events.append('children')
                    if attempts==1:raise CleanupPendingError((parallel,),child_cleanup)
                monkeypatch.setattr(parallel,'aclose',child_cleanup)
                raise primary
        assert caught.value is primary and not events
        with pytest.raises(Exception,match='closed'):parallel.complete(object())
        from api_agent.testing.inspection_controls import ReceiptInspection
        consumer=ReceiptInspection(primary)
        with pytest.raises(CleanupPendingError) as retry:await consumer.aclose()
        assert events==['backend','children'] and not consumer.closed
        await retry.value.retry()
        await consumer.aclose()
        assert consumer.closed
        assert events==['backend','children','children','backend','children']
    asyncio.run(scenario())


@pytest.mark.parametrize('cleanup_interrupted',[False,True])
def test_operation_error_retains_later_cleanup_failure(monkeypatch,cleanup_interrupted):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing import reasoning_provider as adapter
    from api_agent.testing.suite_lock import _cleanup_receipts
    primary=ValueError('synthetic operation failure')
    async def scenario():
        with pytest.raises(ValueError) as caught:
            async with adapter.reasoning_provider(object()) as parallel:
                async def failed_cleanup():
                    if cleanup_interrupted:raise asyncio.CancelledError()
                    raise CleanupPendingError((parallel,),parallel.aclose)
                monkeypatch.setattr(parallel,'aclose',failed_cleanup)
                raise primary
        assert caught.value is primary
        assert any(parallel in receipt.owners for receipt in _cleanup_receipts(primary))
        with pytest.raises(Exception,match='closed'):parallel.describe()
    asyncio.run(scenario())


def test_records_stay_ordered_when_calls_complete_in_reverse():
    from test_reasoning_contrast import response
    async def scenario():
        entered=asyncio.Event();requests=[];gates={};completed=[];ack={}
        async def reply(sent):
            body=json.loads(sent.content);identifier=body['session_id']
            requests.append(body);gates[identifier]=asyncio.Event();ack[identifier]=asyncio.Event()
            if len(requests)==6:entered.set()
            await gates[identifier].wait()
            completed.append(identifier);ack[identifier].set()
            return httpx.Response(200,json=response(body))
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            sender=asyncio.create_task(run_pairs(client,'http://127.0.0.1:1','nonce'))
            async with asyncio.timeout(3):await entered.wait()
            for request in reversed(requests):
                identifier=request['session_id'];gates[identifier].set();await ack[identifier].wait()
            records=await sender
        assert completed==[row['session_id'] for row in reversed(requests)]
        assert [record['pair'] for record in records]==[1,2,3]
        for record in records:
            assert [call['effort'] for call in record['calls']]==['low','medium']
            assert {call['marker'] for call in record['calls']}=={record['marker']}
    asyncio.run(scenario())
