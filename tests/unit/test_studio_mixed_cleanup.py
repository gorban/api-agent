"""Interaction disposal must settle before a UI receipt can be trace-only."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_ui_runtime import operation
from test_workflow_observations import config,collector
from test_studio_retained_owner import Automated


@pytest.mark.parametrize('failure_kind',['none','error','cancelled'])
def test_real_interaction_channels_settle_before_optional_trace_classification(operation,monkeypatch,tmp_path,failure_kind):
    import httpx
    from api_agent.auth.interaction_transport import OwnedInteraction
    from api_agent.testing import provider_setup
    from api_agent.testing.studio_tracing import StudioTraceOwner
    from api_agent.testing.studio_http import agent_endpoint
    from api_agent.testing.workflow_capture import capture_sources
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(tmp_path/'events'))
    failure=RuntimeError('channel pending') if failure_kind=='error' else asyncio.CancelledError('channel pending') if failure_kind=='cancelled' else None
    state=SimpleNamespace(failure=failure,effects=0)
    def close_channel():
        if state.failure is not None:raise state.failure
        state.effects+=1
    interaction=OwnedInteraction(SimpleNamespace(open=lambda _:SimpleNamespace(close=close_channel)))
    interaction.open('independent login')
    ui=operation.create()
    ui.interaction=interaction
    authority=ui.emissions
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        trace=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),Automated())
        @asynccontextmanager
        async def backend(values):
            async with agent_endpoint(trace.provider,'sample','http://localhost:9443','x'*32,tracing=trace) as url:
                yield {'agent_url':url,'access_token':'x'*32}
        ui.backend_factory=backend
        loop=asyncio.new_event_loop()
        retained=None
        shutdown=None
        try:
            loop.run_until_complete(ui.start())
            url=operation.prepared_factory.call_args.args[0]['AGENT_DJANGO_URL']
            async def request():
                async with httpx.AsyncClient() as client:
                    response=await client.get(url+'/app/agent/models',headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32})
                    assert response.status_code==200
            loop.run_until_complete(request())
            runtime=trace._sessions['automated'].runtime
            shutdown=runtime._provider.shutdown
            runtime._provider.shutdown=Mock(side_effect=RuntimeError('exporter pending'))
            if failure is None:
                retained=provider_setup.finish_ui_browser_inspection(loop,None,owners=(ui,),close_loop=False)
                assert ui._nontrace_settled and state.effects==1 and not interaction.channels
            else:
                with pytest.raises(type(failure)) as caught:
                    provider_setup.finish_ui_browser_inspection(loop,None,owners=(ui,),close_loop=False)
                if failure_kind=='cancelled':assert caught.value is failure
                else:assert str(caught.value)=='Interaction cleanup failed'
                retained=next(owner for owner in provider_setup._inspection_providers if ui in owner._owners)
                assert not ui._nontrace_settled and state.effects==0 and interaction.channels
            assert retained.loop is loop and ui._backend_cleanup is not None
            assert ui.emissions is authority and not runtime.disposal_confirmed
            runtime._provider.shutdown=shutdown
            state.failure=None
            retained.close_loop=False
            retained.close()
            assert retained.closed and ui._nontrace_settled
            assert state.effects==1 and not interaction.channels and runtime.disposal_confirmed
        finally:
            state.failure=None
            if shutdown is not None:runtime._provider.shutdown=shutdown
            if retained is None:
                retained=next((owner for owner in provider_setup._inspection_providers if ui in owner._owners),None)
            if retained is not None:
                retained.close_loop=False
                retained.close()
                if retained in provider_setup._inspection_providers:provider_setup._inspection_providers.remove(retained)
            if not loop.is_closed():loop.close()
