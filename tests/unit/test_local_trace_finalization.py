"""Local completion uses its existing collector independently of remote offers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import LLMResult,Message,TextContent,Usage
from api_agent.testing.provider_setup import IntegrationLLM
from test_inference_shared_join import REQUEST,CATALOG


@pytest.mark.parametrize('outcome',['success','failure','cancelled'])
def test_local_completion_finalizes_shared_capture_once_without_replacing_outcome(tmp_path,monkeypatch,outcome):
    from api_agent.testing import provider_setup,inference_content
    from api_agent.workflows import inference_capture
    result=LLMResult(Message('assistant',(TextContent('safe answer'),)),Usage(2,1))
    failure=ProviderAccessError('authorization','synthetic denial') if outcome=='failure' else asyncio.CancelledError() if outcome=='cancelled' else None
    async def complete(request,*,auth):
        inference_capture.capture_inference('output',{'text':'safe answer'},'synthetic-source')
        if failure is not None:raise failure
        return result
    implementation=SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic-secret')),
        acomplete=complete,adescribe=AsyncMock(return_value=CATALOG),credential_protection_version=1)
    implementation.bind_credential_authority=lambda boundary:setattr(implementation,'authority',boundary)
    monkeypatch.setattr(provider_setup,'aload_provider',AsyncMock(return_value=SimpleNamespace(implementation=implementation,aclose=AsyncMock())))
    capture=Mock(wraps=inference_capture.capture_rows)
    content=Mock()
    monkeypatch.setattr(inference_capture,'capture_rows',capture)
    monkeypatch.setattr(inference_content,'publish_content',content)
    owner=IntegrationLLM({'LLM_PROVIDER':'synthetic','LLM_MAX_ATTEMPTS':1,'TRACELOOP_TRACE_CONTENT':True,'LOG_CONTENT':True},tmp_path)
    terminal=[]
    execute=owner._run_private
    def remember(awaitable):
        try:
            value=execute(awaitable)
        except BaseException as error:
            terminal.append(error)
            raise
        terminal.append(value)
        return value
    monkeypatch.setattr(owner,'_run_private',remember)
    try:
        if failure is None:
            observed=owner.complete(REQUEST)
            assert observed.payload is terminal[0].payload and observed.usage is terminal[0].usage
        else:
            with pytest.raises(type(failure)) as caught:owner.complete(REQUEST)
            assert caught.value is terminal[0]
        capture.assert_called_once()
        content.assert_called_once_with(*capture.call_args.args)
        rows,call_id=capture.call_args.args
        assert any(row['phase']=='response' and row['content'].get('text')=='safe answer' for row in rows)
        assert type(call_id) is str and call_id
    finally:owner.close()
