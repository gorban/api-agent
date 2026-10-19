import asyncio
from types import SimpleNamespace
import pytest
from api_agent.providers.control import CleanupPendingError
from api_agent.testing import reasoning_provider as adapter
from api_agent.testing.inspection_controls import ReceiptInspection

@pytest.mark.parametrize('direct',[False,True])
def test_context_retry_preserves_nested_prior_duty(direct):
    events=[]
    async def first():events.append('first')
    async def second():events.append('second')
    old=CleanupPendingError((object(),),first)
    old.cleanup_pending=CleanupPendingError((object(),),second)
    primary=old if direct else RuntimeError('synthetic endpoint error')
    if not direct:primary.cleanup_pending=old
    async def scenario():
        try:
            async with adapter.reasoning_provider(object()):
                raise primary
        except BaseException as caught:
            assert caught is primary
            consumer=ReceiptInspection(caught)
            await consumer.aclose()
            assert consumer.closed
        assert events==['first','second'], ('cleanup duties were skipped or repeated',events)
    asyncio.run(scenario())

def test_child_failure_retains_prior_receipt_and_owner(monkeypatch):
    events=[];old_owner=object()
    async def old_retry():events.append('old')
    primary=asyncio.CancelledError()
    primary.cleanup_pending=CleanupPendingError((old_owner,),old_retry)
    primary.retained_owners=(old_owner,)
    class Remote:
        def __init__(self,*args):self.client=SimpleNamespace(_descriptor={});self.config={};self.closes=0
        def complete(self,request):raise primary
        def close(self):
            self.closes+=1
            if self.closes==1:raise OSError('synthetic temporary child disposal fault')
    monkeypatch.setattr(adapter,'RemoteIntegrationLLM',Remote)
    borrowed=Remote();parallel=adapter._ReasoningProvider(borrowed)
    try:parallel.complete(object())
    except BaseException as caught:assert caught is primary
    assert old_owner in primary.cleanup_pending.owners
    consumer=ReceiptInspection(primary);asyncio.run(consumer.aclose())
    assert consumer.closed and not parallel._calls
    assert 'old' in events, 'original cleanup duty was overwritten'
    assert any(owner is old_owner for owner in primary.retained_owners)

@pytest.mark.parametrize('boundary',['per-call','owner-close'])
def test_cleanup_interrupt_keeps_original_identity(monkeypatch,boundary):
    interruption=KeyboardInterrupt('synthetic cleanup interrupt')
    class Remote:
        def __init__(self,*args):self.client=SimpleNamespace(_descriptor={});self.config={};self.closes=0
        def complete(self,request):return 'result'
        def close(self):
            self.closes+=1
            if self.closes==1:raise interruption
    monkeypatch.setattr(adapter,'RemoteIntegrationLLM',Remote)
    borrowed=Remote();parallel=adapter._ReasoningProvider(borrowed)
    if boundary=='owner-close':parallel._calls[Remote()]=adapter.RLock()
    try:
        parallel.complete(object()) if boundary=='per-call' else parallel.close()
    except BaseException as caught:
        assert caught is interruption, ('cleanup replaced original interruption',type(caught))
        assert isinstance(caught.cleanup_pending,CleanupPendingError)
        consumer=ReceiptInspection(caught)
        asyncio.run(consumer.aclose())
        assert consumer.closed and not parallel._calls
    else:pytest.fail('cleanup interruption disappeared')
