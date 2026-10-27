"""Configured setup transport budgets are validated before allocating resources."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.progress import _scope, progress_sink, report_timeout_sync
from api_agent.testing import provider_setup, setup_backend


@pytest.mark.parametrize('value', [None, True, False, '', '960', 0, -1, 3601, 10**1000,
                                  float('nan'), float('inf'), float('-inf')])
@pytest.mark.parametrize('kind', ['backend', 'remote'])
def test_invalid_budget_allocates_no_resources(monkeypatch, tmp_path, value, kind):
    def allocate(*args, **kwargs):
        pytest.fail('Invalid setup budget allocated a resource')
    monkeypatch.setattr('api_agent.auth.managers.registry_for_owner', allocate)
    monkeypatch.setattr('api_agent.providers.setup_transport.SetupClient', allocate)
    monkeypatch.setattr(asyncio, 'new_event_loop', allocate)
    monkeypatch.setattr('threading.Thread', allocate)
    config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':value}
    with pytest.raises(ContractError, match='Invalid setup timeout'):
        if kind == 'backend':
            setup_backend.SetupBackend(config,tmp_path)
        else:
            provider_setup.RemoteIntegrationLLM({},config)


@pytest.mark.parametrize('config, expected', [({},300), ({'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':960},960),
                                            ({'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':3600},3600),
                                            ({'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':.5},.5)])
@pytest.mark.parametrize('parent', [None,.25])
def test_actual_setup_backend_and_remote_obey_configured_and_parent_budgets(monkeypatch,tmp_path,config,expected,parent):
    observed=[];closed=[];events=[]
    class Owner:
        def __init__(self, backend):pass
        async def ensure_ready(self):pass
        async def complete(self, request):
            observed.append(_scope.get().remaining)
            return LLMResult(Message('assistant',(TextContent('synthetic'),)),Usage(1,1))
        def quote_cost(self, request, result):return None
        def export_content(self, request, result=None):return ()
        async def aclose(self):closed.append(True)
    monkeypatch.setattr(setup_backend,'_SetupOwner',Owner)
    backend=setup_backend.SetupBackend(config,tmp_path,password_manager_registry=SimpleNamespace(definitions=()))
    remote=provider_setup.RemoteIntegrationLLM(backend.start(),config)
    try:
        assert backend.timeout == remote.timeout == remote.client._timeout == expected
        assert backend.server._request_timeout == expected
        with progress_sink(events.append), (nullcontext() if parent is None else report_timeout_sync('Synthetic parent',parent)):
            result=remote.complete(LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('synthetic'),)),))))
        assert result.usage.output_tokens == 1
        ceiling=expected if parent is None else parent
        assert len(observed)==1 and ceiling-.1 < observed[0] <= ceiling
        active=[event.timeout for event in events if event.active and event.name=='Shared provider setup']
        assert active and all(ceiling-.1 < timeout <= ceiling for timeout in active)
    finally:
        remote.close()
        backend.close()
    assert closed==[True] and remote.loop.is_closed() and not backend.thread.is_alive()
