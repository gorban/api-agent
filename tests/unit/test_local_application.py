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


def test_observations_capture_shared_node_values_without_changing_provider_request(application):
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    resolved = ConfigResult({'LOG_CONTENT':True}, {'LOG_CONTENT':ResolvedValue(True,'cli',True)}, (), (), ())
    rows = []
    application.observations = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append)
    result = LLMResult(Message('assistant',(TextContent('visible answer'),)),Usage(2,3))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    request = LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('visible question'),)),)))
    async def run():
        context = OperationContext('request')
        assert await application.complete(request,llm=provider,context=context) is result
        provider.complete.assert_awaited_once_with(request,context=context)
    asyncio.run(run())
    captures = [row for row in rows if row['event']=='capture' and row.get('values')]
    assert len(captures) == 2
    assert captures[0]['values']['messages'][0]['content'] == 'visible question'
    assert captures[1]['values']['response_text'] == 'visible answer'
    assert captures[1]['values']['selected_model'] == 'synthetic'
    assert captures[1]['values']['outcome'] == 'completed'
    assert all(row['capture_class'] == 'schema_user_io' for row in captures)
    assert len({row['invocation_id'] for row in rows}) == 1


@pytest.mark.parametrize('log,trace',[(True,False),(False,True),(True,True)])
def test_privacy_unrepresentable_visible_response_preserves_actual_graph_result(application,log,trace):
    from api_agent.config.resolve import ConfigResult,ResolvedValue
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    from api_agent.workflows.observations import WorkflowObservationFactory
    from unittest.mock import Mock
    values = {'LOG_CONTENT':log,'TRACELOOP_TRACE_CONTENT':trace}
    resolved = ConfigResult(values,{key:ResolvedValue(value,'cli',True) for key,value in values.items()},(),(),())
    rows = []
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(trace)))
    application.observations = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append,trace=runtime)
    visible = '123456789012345678901234567890'
    result = LLMResult(Message('assistant',(TextContent(visible),)),Usage(2,3))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    request = LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('question'),)),)))
    async def run():
        context = OperationContext('request')
        assert await application.complete(request,llm=provider,context=context) is result
        provider.complete.assert_awaited_once_with(request,context=context)
    asyncio.run(run())
    capture = next(row for row in rows if row['event']=='capture' and row['direction']=='output')
    assert visible not in repr(capture)
    assert visible not in repr(rows)


from test_workflow_observations import recording_policy


def test_recorded_local_graph_publishes_only_resolved_log_permission(application,recording_policy,tmp_path,monkeypatch):
    import json
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.workflows.evidence import attachment
    resolved,allowed = recording_policy
    events = tmp_path/'recorded.jsonl'
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents',lambda *args,**kwargs:None)
    rows = []
    application.config['LOG_CONTENT'] = True
    application.observations = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append)
    result = LLMResult(Message('assistant',(TextContent('private-response-sentinel'),)),Usage(2,3))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    request = LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('private-prompt-sentinel'),)),)))
    assert asyncio.run(application.complete(request,llm=provider,context=OperationContext('request'))) is result
    packet = next(json.loads(line) for line in events.read_text().splitlines() if json.loads(line)['kind']=='workflow_evidence')
    payload = packet['data']
    _,attachment_bytes,_ = attachment(SimpleNamespace(text=json.dumps(payload)),None)
    assert ('private-prompt-sentinel' in attachment_bytes.decode()) is allowed
    assert ('private-response-sentinel' in attachment_bytes.decode()) is allowed
    assert payload['content']==('permitted_scrubbed' if allowed else 'withheld')
    assert [row['event_id'] for row in rows]==[row['event_id'] for row in payload['events']]


@pytest.mark.parametrize('size', [65536, 65537])
@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('full_content', [False, True])
def test_local_chat_capture_scalar_limit_preserves_reason_and_execution(
        application, size, log_content, full_content, monkeypatch):
    import json
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.runtime.local_diagnostics import LocalDiagnosticProjection
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    resolved = ConfigResult({'LOG_CONTENT': log_content},
        {'LOG_CONTENT': ResolvedValue(log_content, 'cli', True)}, (), (), ())
    rows = []
    application.observations = WorkflowObservationFactory(
        resolved, boundary=EmissionBoundary(), sink=rows.append)
    from api_agent.workflows.observations import WorkflowObservations
    capture_ids = []
    original_capture = WorkflowObservations.capture_json
    def observe_capture(adapter, **options):
        row = original_capture(adapter, **options)
        capture_ids.append(row['event_id'])
        return row
    monkeypatch.setattr(WorkflowObservations, 'capture_json', observe_capture)
    # Shared capture retains the structured messages and bounds total text.
    first = 'q' * 32760
    second = 'r' * (size - 13 - len(first))
    # The output also carries the completed outcome and selected model.
    answer = 'a' * (size - len('completed') - len('synthetic'))
    request = LLMRequest('synthetic', CompletionRequest((
        Message('user', (TextContent(first),)), Message('user', (TextContent(second),)))))
    result = LLMResult(Message('assistant', (TextContent(answer),)), Usage(2, 3))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    context = OperationContext('request')

    actual = asyncio.run(application.complete(request, llm=provider, context=context))

    assert actual is result
    provider.complete.assert_awaited_once_with(request, context=context)
    captures = [row for row in rows if row['event_id'] in capture_ids]
    assert [row['direction'] for row in captures] == ['input', 'output']
    displayed = [json.loads(LocalDiagnosticProjection(full_content=full_content).format(row))
                 for row in captures]
    if log_content:
        assert captures[0]['values']['messages'][0]['content'] == first
        assert captures[0]['values']['messages'][1]['content'] == second
        assert captures[0]['reason'] == 'captured'
        if size > 65536:
            assert 'response_text' not in captures[1]['values']
            assert captures[1]['reason'] == 'truncated'
        else:
            assert captures[1]['values']['response_text'] == answer
            limit = len(answer) if full_content else 512
            assert len(displayed[1]['values']['response_text']) == limit
        limit = len(first) if full_content else 512
        assert len(displayed[0]['values']['messages'][0]['content']) == limit
    else:
        assert [row['content'] for row in displayed] == ['withheld', 'withheld']
        assert all('values' not in row for row in captures + displayed)
