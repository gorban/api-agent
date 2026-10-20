"""Actual adapter paths await cancellable local schema validation."""
import asyncio
import json
import httpx
import pytest
from api_agent.providers.llm import LLMRequest,CompletionRequest,StructuredOutput,Message,TextContent,Tool,ToolCall,ToolResult
from api_agent.llm_providers.gpt.inference import complete_text

SCHEMA=json.dumps({'type':'object','properties':{'x':{'type':'string','pattern':'(a+)+$'}}})


@pytest.mark.parametrize('mode,subscription',[('responses',False),('responses',True),('completions',False)])
def test_actual_adapter_validation_cancellation_reaps_worker(monkeypatch,mode,subscription):
    processes=[]
    real=asyncio.create_subprocess_exec
    async def launch(*args,**kwargs):
        process=await real(*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    text=json.dumps({'x':'a'*64+'!'})
    response={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}],'usage':{'input_tokens':9,'output_tokens':3}} if mode == 'responses' else {'choices':[{'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'usage':{'prompt_tokens':9,'completion_tokens':3}}
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),),structured=StructuredOutput('answer',SCHEMA,strict=False)))
    def transport(outbound):
        if subscription:
            data='data: '+json.dumps({'type':'response.completed','response':response})+'\n\n'
            return httpx.Response(200,content=data.encode(),headers={'content-type':'text/event-stream'})
        return httpx.Response(200,json=response)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            task=asyncio.create_task(complete_text(client=client,base_url='https://api.openai.com/v1',access_token='synthetic-secret',subscription=subscription,api_mode=mode,request=request))
            while not processes and not task.done():
                await asyncio.sleep(.001)
            assert processes
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert all(process.returncode is not None for process in processes)
    asyncio.run(scenario())


def test_actual_adapter_replay_admission_is_cancellable_before_transport(monkeypatch):
    processes=[];sent=[]
    real=asyncio.create_subprocess_exec
    async def launch(*args,**kwargs):
        process=await real(*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),Message('assistant',calls=(ToolCall('call','lookup',json.dumps({'x':'a'*64+'!'})),)),Message('tool',results=(ToolResult('call',(TextContent('Answer'),)),))),tools=(Tool('lookup','Lookup',SCHEMA,strict=False),)))
    def transport(outbound):
        sent.append(outbound)
        return httpx.Response(500)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            task=asyncio.create_task(complete_text(client=client,base_url='https://api.openai.com/v1',access_token='synthetic-secret',subscription=False,api_mode='responses',request=request))
            while not processes:
                await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert all(process.returncode is not None for process in processes)
            assert sent == []
    asyncio.run(scenario())


def test_async_replay_schema_rejection_is_safe_before_transport():
    from api_agent.providers.contracts import ProviderAccessError
    sent=[]
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),Message('assistant',calls=(ToolCall('call','lookup','{"x":"123"}'),)),Message('tool',results=(ToolResult('call',(TextContent('Answer'),)),))),tools=(Tool('lookup','Lookup',SCHEMA,strict=False),)))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda outbound: sent.append(outbound))) as client:
            with pytest.raises(ProviderAccessError,match='Invalid GPT inference request'):
                await complete_text(client=client,base_url='https://api.openai.com/v1',access_token='synthetic-secret',subscription=False,api_mode='responses',request=request)
        assert sent == []
    asyncio.run(scenario())


def test_async_structured_output_keeps_secret_rejection_and_usage():
    from api_agent.llm_providers.gpt.rich import async_validate_result
    from api_agent.providers.llm import LLMResult,Usage
    from api_agent.providers.contracts import ProviderAccessError
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),),structured=StructuredOutput('answer',SCHEMA,strict=False)))
    result=LLMResult(Message('assistant',(TextContent('{"x":"synthetic-secret"}'),)),Usage(9,3))
    with pytest.raises(ProviderAccessError) as raised:
        asyncio.run(async_validate_result(request,result,'synthetic-secret'))
    assert raised.value.usage == Usage(9,3)


def test_factory_sync_preflight_remains_structure_only_before_async_execution(monkeypatch):
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers import rich_inference
    provider=GPTProvider({'GPT_AUTH_MODE':'api_key'})
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),Message('assistant',calls=(ToolCall('call','lookup','{"x":"123"}'),)),Message('tool',results=(ToolResult('call',(TextContent('Answer'),)),))),tools=(Tool('lookup','Lookup',SCHEMA,strict=False),)))
    monkeypatch.setattr(rich_inference,'valid_schema',lambda *args: pytest.fail('sync preflight must not start a validation worker'))
    try:
        provider.validate_request(request)
    finally:
        asyncio.run(provider.aclose())


def test_factory_async_replay_cancellation_happens_before_client_allocation(monkeypatch):
    from api_agent.llm_providers.gpt import factory
    processes=[]
    real=asyncio.create_subprocess_exec
    async def launch(*args,**kwargs):
        process=await real(*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    monkeypatch.setattr(factory.httpx,'AsyncClient',lambda **kwargs: pytest.fail('validation must precede client allocation'))
    request=LLMRequest('gpt-synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),Message('assistant',calls=(ToolCall('call','lookup',json.dumps({'x':'a'*64+'!'})),)),Message('tool',results=(ToolResult('call',(TextContent('Answer'),)),))),tools=(Tool('lookup','Lookup',SCHEMA,strict=False),)))
    async def scenario():
        provider=factory.GPTProvider({'GPT_AUTH_MODE':'api_key'})
        task=asyncio.create_task(provider.acomplete(request,auth='synthetic-secret'))
        try:
            while not processes and not task.done():
                await asyncio.sleep(.001)
            assert processes
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert all(process.returncode is not None for process in processes)
        finally:
            await provider.aclose()
    asyncio.run(scenario())
