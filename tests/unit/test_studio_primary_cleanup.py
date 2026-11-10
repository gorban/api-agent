"""Studio teardown retains every pre-existing body cleanup duty."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from test_workflow_observations import config,collector
from test_studio_retained_owner import Automated


@pytest.mark.parametrize('kind',['error','cancelled','receipt'])
def test_endpoint_composes_primary_cleanup_before_trace_without_losing_either_owner(kind):
    import httpx
    from api_agent.testing.studio_http import agent_endpoint,studio_trace_pending
    from api_agent.testing.studio_tracing import StudioTraceOwner
    from api_agent.testing.workflow_capture import capture_sources
    from api_agent.testing.suite_lock import _cleanup_receipts
    state=SimpleNamespace(blocked=True,effects=[])
    original_owner=object()
    following_owner=object()
    async def dispose_previous():
        if state.blocked:raise RuntimeError('provider cleanup pending')
        if original_owner not in state.effects:state.effects.append(original_owner)
    async def dispose_following():
        if following_owner not in state.effects:state.effects.append(following_owner)
    previous=CleanupPendingError((original_owner,),dispose_previous)
    following=CleanupPendingError((following_owner,),dispose_following)
    previous.cleanup_pending=following
    primary=previous if kind=='receipt' else asyncio.CancelledError('original') if kind=='cancelled' else ValueError('original')
    if primary is not previous:primary.cleanup_pending=previous
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        trace=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),Automated())
        async def run():
            runtime=shutdown=None
            try:
                with pytest.raises(type(primary)) as caught:
                    async with agent_endpoint(trace.provider,'sample','http://localhost:9443','x'*32,tracing=trace) as url:
                        async with httpx.AsyncClient(timeout=60) as client:
                            response=await client.get(url+'/app/agent/models',headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32})
                            assert response.status_code==200
                        runtime=trace._sessions['automated'].runtime
                        shutdown=runtime._provider.shutdown
                        runtime._provider.shutdown=Mock(side_effect=RuntimeError('exporter pending'))
                        raise primary
                assert caught.value is primary
                joined=primary.cleanup_pending
                assert type(joined) is CleanupPendingError and not studio_trace_pending(joined)
                assert previous in joined.owners and following in joined.owners
                assert all(receipt in tuple(_cleanup_receipts(primary)) or receipt in joined.owners for receipt in (previous,following))
                with pytest.raises(CleanupPendingError):await joined.retry()
                assert state.effects==[] and not runtime.disposal_confirmed
                state.blocked=False
                runtime._provider.shutdown=shutdown
                await joined.retry();await joined.retry()
                assert state.effects==[original_owner,following_owner]
                assert runtime.disposal_confirmed and trace._sessions['automated'].closed
            finally:
                state.blocked=False
                if shutdown is not None:
                    runtime._provider.shutdown=shutdown
                    await trace.aclose()
                await previous.retry();await following.retry()
        asyncio.run(run())


def test_composed_retries_acknowledge_late_suite_lock_wrappers(isolated_suite_lock):
    from api_agent.testing import suite_lock as locks
    from api_agent.testing.studio_http import _endpoint_cleanup
    from api_agent.testing.studio_retained import StudioCleanup
    state=SimpleNamespace(blocked=True,effects=[])
    async def previous_close():
        if 'previous' not in state.effects:state.effects.append('previous')
    async def following_close():
        if 'following' not in state.effects:state.effects.append('following')
    async def trace_close():
        if state.blocked:raise RuntimeError('trace cleanup pending')
        if 'trace' not in state.effects:state.effects.append('trace')
    previous=CleanupPendingError((object(),),previous_close)
    following=CleanupPendingError((object(),),following_close)
    previous.cleanup_pending=following
    trace=CleanupPendingError((object(),),trace_close)
    async def duty():
        try:await trace_close()
        except RuntimeError:raise trace
    duties=StudioCleanup((duty,))
    primary=ValueError('original')
    primary.cleanup_pending=previous
    @locks.suite_locked
    async def operation():
        await _endpoint_cleanup(duties,primary)
        raise primary
    def assert_contended():
        with pytest.raises(locks.SuiteBusyError):
            with locks.suite_lock():pytest.fail('Unacknowledged resources released suite ownership')
    async def run():
        with pytest.raises(ValueError) as caught:await operation()
        assert caught.value is primary
        assert_contended()
        with pytest.raises(CleanupPendingError):await primary.cleanup_pending.retry()
        assert_contended()
        state.blocked=False
        await primary.cleanup_pending.retry()
        await primary.cleanup_pending.retry()
        assert state.effects==['previous','following','trace']
        with locks.suite_lock():pass
    asyncio.run(run())
