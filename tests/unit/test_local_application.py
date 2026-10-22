"""The local bridge traverses its admitted graph with a controlled provider."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest
from unittest.mock import AsyncMock

from api_agent.providers.control import OperationContext
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage, GenerationOptions, CacheBoundary
from api_agent.workflows.definition import load_definition
from api_agent.workflows.local_application import LocalChatApplication
from api_agent.workflows.execution import WorkflowError

ROOT = Path(__file__).resolve().parents[2]


def test_selected_application_executes_native_graph_and_preserves_result(monkeypatch):
    from api_agent.workflows.execution import Workflow
    traversals = []
    original_run = Workflow.arun
    async def observed(graph, inputs):
        traversals.append(graph.definition)
        return await original_run(graph, inputs)
    monkeypatch.setattr(Workflow, 'arun', observed)
    definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    app = LocalChatApplication(definition)
    result = LLMResult(Message('assistant', (TextContent('hello'),)), Usage(2, 3))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
    async def run():
        context = OperationContext('request')
        assert await app.complete(request, llm=provider, context=context) is result
        provider.complete.assert_awaited_once_with(request, context=context)
        assert traversals == [definition]
    asyncio.run(run())


@pytest.fixture
def application():
    definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    return LocalChatApplication(definition)


@pytest.mark.parametrize('shape', ['node', 'schema'])
def test_unsupported_definition_rejected(application, shape):
    definition = application.definition
    if shape == 'node':
        layout = replace(definition.layout, steps=())
        definition = replace(definition, layout=layout)
    else:
        contents = dict(definition.contents)
        contents[definition.layout.tables['Functions'][0]['Input']] = b'{"type":"string"}'
        definition = replace(definition, contents=contents)
    with pytest.raises(ContractError): LocalChatApplication(definition)


@pytest.mark.parametrize('message', [
    Message('tool'), Message('assistant', continuation='id'),
    Message('user', (TextContent('cached'),), cache_boundary=CacheBoundary(300)),
])
def test_unsupported_messages_never_reach_provider(application, message):
    provider = SimpleNamespace(complete=AsyncMock())
    async def run():
        with pytest.raises(ContractError):
            await application.complete(LLMRequest('synthetic', CompletionRequest((message,))),
                llm=provider, context=OperationContext('turn'))
        provider.complete.assert_not_called()
    asyncio.run(run())


def test_unsupported_generation_never_reaches_provider(application):
    provider = SimpleNamespace(complete=AsyncMock())
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('hello'),)),),
        generation=GenerationOptions(temperature=.5)))
    async def run():
        with pytest.raises(ContractError): await application.complete(request, llm=provider, context=OperationContext('turn'))
        provider.complete.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('behavior', ['none', 'twice', 'output', 'selection'])
def test_graph_execution_cannot_bypass_or_replace_declared_call(application, monkeypatch, behavior):
    from api_agent.workflows import local_application
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    result = LLMResult(Message('assistant', (TextContent('hello'),)))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    class Graph:
        def __init__(self, *args, services, **kwargs): self.services = services
        async def arun(self, inputs):
            if behavior == 'none': return {}
            selected = replace(request, selected_llm='wrong') if behavior == 'selection' else request
            await self.services['llm'](selected)
            if behavior == 'twice': await self.services['llm'](selected)
            return {'llm_call': {'response_text': 'different'}}
    monkeypatch.setattr(local_application, 'Workflow', Graph)
    async def run():
        with pytest.raises(WorkflowError): await application.complete(request, llm=provider, context=OperationContext('turn'))
    asyncio.run(run())
