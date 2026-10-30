"""Failed local provider calls retain accounting without exporting model prose."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.control import OperationContext
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.definition import load_definition
from api_agent.workflows.observations import WorkflowObservationFactory
from test_local_llm import selected, request


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('model', ['customer_private_question', 'provider/model-v1', '模型@v1'])
def test_failed_local_request_preserves_selection_but_omits_model_prose_from_otlp(selected, mode, model):
    from api_agent.workflows.local_application import LocalChatApplication
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    llm, implementation, _, _, _ = selected
    llm.policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '.*'})
    implementation.adescribe = AsyncMock(return_value=(AvailableSelection('provider/model-v1'), AvailableSelection('模型@v1'),))
    failure = ProviderAccessError('unavailable', 'Synthetic provider failure')
    implementation.acomplete = AsyncMock(side_effect=failure)
    values = {'TRACE_MODE': mode, 'TRACELOOP_TRACE_CONTENT': False,
              'LANGFUSE_PUBLIC_KEY': 'synthetic-public-key',
              'LANGFUSE_SECRET_KEY': 'synthetic-secret-key'}
    resolved = ConfigResult(values,
        {'TRACELOOP_TRACE_CONTENT': ResolvedValue(False, 'cli', True)}, (), (), ())
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace = TraceRuntime(resolved, factory=lambda *args: (provider, provider.get_tracer('local-test')))
    observations = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=trace)
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root / 'flow.md.html', base_root=root, pack_root=root)
    application = LocalChatApplication(definition, observations=observations)
    value = request(model)

    async def run():
        llm.trace = trace
        await llm.describe()
        await trace.astart()
        try:
            with pytest.raises(ProviderAccessError) as caught:
                await application.complete(value, llm=llm, context=OperationContext('request'))
            assert caught.value is failure
            assert implementation.acomplete.call_args.args[0] == value
            assert len(llm.usage) == 1
            assert llm.usage[0]['selection'] == model
            assert llm.usage[0]['outcome'] == 'failed'
        finally:
            await llm.aclose()

    try:
        asyncio.run(run())
        spans = exporter.get_finished_spans()
        clients = [span for span in spans if span.kind.name == 'CLIENT']
        client, = [span for span in clients if 'api_agent.transport_id' not in span.attributes]
        physical, = [span for span in clients if 'api_agent.transport_id' in span.attributes]
        assert physical.parent.span_id == client.context.span_id
        assert physical.attributes['api_agent.transport_index'] == 1
        assert physical.attributes['api_agent.transport_outcome'] == 'failed'
        if model == 'customer_private_question':
            assert client.name == 'chat'
            assert 'gen_ai.request.model' not in client.attributes
        else:
            assert client.name == 'chat ' + model
            assert client.attributes['gen_ai.request.model'] == model
        assert client.attributes['status'] == 'error'
        encoded = encode_spans(spans).SerializeToString()
        if model == 'customer_private_question':
            assert model.encode() not in encoded
    finally:
        assert trace.shutdown()


@pytest.mark.parametrize('model, admitted', [
    ('customer_private_question', False), ('provider/model-v1', True), ('模型@v1', True)])
def test_failed_request_selection_is_catalog_admitted_before_live_and_journal_retention(selected, model, admitted):
    from api_agent.workflows.local_application import LocalChatApplication
    llm, implementation, _, _, _ = selected
    llm.policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '.*'})
    implementation.adescribe = AsyncMock(return_value=(
        AvailableSelection('provider/model-v1'), AvailableSelection('模型@v1')))
    failure = ProviderAccessError('unavailable', 'Synthetic provider failure')
    implementation.acomplete = AsyncMock(side_effect=failure)
    resolved = ConfigResult({'LOG_CONTENT': False, 'TRACE_MODE': ''},
        {'LOG_CONTENT': ResolvedValue(False, 'cli', True)}, (), (), ())
    trace = TraceRuntime(resolved)
    llm.trace = trace
    rows, journals = [], []
    class RetainedFactory(WorkflowObservationFactory):
        def create(self, *args, **kwargs):
            result = super().create(*args, **kwargs)
            journals.append(result.journal)
            return result
    factory = RetainedFactory(resolved, boundary=EmissionBoundary(), sink=rows.append, trace=trace)
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root / 'flow.md.html', base_root=root, pack_root=root)
    application = LocalChatApplication(definition, observations=factory)
    value = request(model)

    async def run():
        try:
            await llm.describe()
            with pytest.raises(ProviderAccessError) as caught:
                await application.complete(value, llm=llm, context=OperationContext('request'))
            assert caught.value is failure
            assert implementation.acomplete.call_args.args[0] == value
            assert len(llm.usage) == 1
            assert llm.usage[0]['selection'] == model
            assert llm.usage[0]['outcome'] == 'failed'
        finally:
            await llm.aclose()
    asyncio.run(run())

    live = next(row for row in rows if row['event'] == 'call')
    journal = journals[0].finalize()
    retained = next(row for row in journal['events'] if row['event'] == 'call')
    expected = model if admitted else None
    assert live['selection'] == retained['selection'] == expected
    assert live.get('reason') == retained.get('reason') == (None if admitted else 'selection_unavailable')
    assert admitted or model not in repr(rows) + repr(journal)


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('effort', [None, 'high'])
def test_authenticated_local_owner_preserves_call_settings_and_actual_usage(selected, failed, effort):
    from api_agent.workflows.local_application import LocalChatApplication
    from dataclasses import replace
    from api_agent.providers.llm import GenerationOptions, LLMResult, Message, TextContent, Usage
    llm, implementation, _, _, _ = selected
    from api_agent.runtime.local_llm import LocalLLM
    llm = LocalLLM({**llm.config, 'LLM_REASONING_EFFORT': 'low'}, llm.owner_root, loader=llm.loader)
    llm.policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': 'sample|other'})
    implementation.capabilities = replace(implementation.capabilities, features=('reasoning_effort',))
    implementation.adescribe = AsyncMock(return_value=(AvailableSelection('sample'), AvailableSelection('other')))
    result = LLMResult(Message('assistant', (TextContent('answer'),)), Usage(7, 3), actual_model='other')
    failure = ProviderAccessError('unavailable', 'Synthetic failure', usage=Usage(4, 1))
    implementation.acomplete = AsyncMock(side_effect=failure) if failed else AsyncMock(return_value=result)
    resolved = ConfigResult({'LOG_CONTENT': False, 'TRACE_MODE': ''}, {}, (), (), ())
    trace = TraceRuntime(resolved)
    llm.trace = trace
    rows = []
    factory = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), sink=rows.append, trace=trace)
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root / 'flow.md.html', base_root=root, pack_root=root)
    application = LocalChatApplication(definition, observations=factory)
    value = replace(request(), payload=replace(request().payload,
        generation=GenerationOptions(reasoning_effort=effort)))

    async def run():
        try:
            await llm.describe()
            if failed:
                with pytest.raises(ProviderAccessError):
                    await application.complete(value, llm=llm, context=OperationContext('request'))
            else:
                await application.complete(value, llm=llm, context=OperationContext('request'))
        finally:
            await llm.aclose()
    asyncio.run(run())
    call = next(row for row in rows if row['event'] == 'call')
    assert call['selection'] == 'sample'
    expected_effort = effort or 'low'
    assert implementation.acomplete.call_args.args[0].payload.generation.reasoning_effort == expected_effort
    assert call['reasoning_effort'] == expected_effort
    assert call.get('actual_model') == (None if failed else 'other')
    assert call['usage']['input_tokens'] == (4 if failed else 7)
    assert call['outcome'] == ('failed' if failed else 'completed')
    assert len(llm.usage) == 1
    assert llm.usage[0]['call_id'] == call['call_id']
