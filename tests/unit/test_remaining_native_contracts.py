"""Boundary regressions for typed, bounded native trace observations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceSpan, TraceRuntime, _structural
from api_agent.telemetry.trace_mapping import genai_messages
from test_workflow_observations import config, definition


def test_mapping_selects_tool_results_and_skips_malformed_calls():
    result = genai_messages('input', {'messages': [
        {'role': 'assistant', 'content': [{'toolResult': {'toolUseId': 'one', 'content': [
            {'text': 'visible'}, {'toolUse': {'name': 'nested'}}]}}],
         'tool_calls': [None, {'function': {}}, {'function': {'name': 'search'}}],
         'results': [None, {'id': 'two', 'content': 'done'}]},
        {'role': 'tool', 'tool_call_id': 'three', 'content': 'answer'}]})
    assert result == [
        {'role': 'assistant', 'parts': [
            {'type': 'tool_call_response', 'id': 'one', 'response': 'visible'},
            {'type': 'tool_call', 'name': 'search'},
            {'type': 'tool_call_response', 'id': 'two', 'response': 'done'}]},
        {'role': 'tool', 'parts': [{'type': 'tool_call_response', 'id': 'three', 'response': 'answer'}]}]


def test_mapping_handles_plain_responses_input_and_rejects_invalid_inner_payload():
    assert genai_messages('input', {'input': 'hello'}) == [
        {'role': 'user', 'parts': [{'type': 'text', 'content': 'hello'}]}]
    assert genai_messages('input', {'provider_payload': []}) is None


def test_converse_unknown_stop_reason_does_not_discard_visible_counts():
    from api_agent.telemetry.inference_diagnostics import sdk_observation
    result = sdk_observation({'output': {'message': {'content': [{'text': 'hello'}]}},
                              'stopReason': 'unrecognized'}, 'response')
    assert result['api'] == 'converse'
    assert result['output_items'] == 1
    assert 'finish_reasons' not in result


@pytest.mark.asyncio
async def test_application_continues_after_tracing_start_failure(caplog):
    from api_agent.api.application import Runtime
    trace = SimpleNamespace(astart=AsyncMock(side_effect=RuntimeError('opaque detail')))
    provider = SimpleNamespace(astart=AsyncMock())
    runtime = Runtime('example', tracing=trace, llm_provider=provider)
    await runtime._start_owned()
    provider.astart.assert_awaited_once()
    assert 'Tracing initialization failed' in caplog.text
    assert 'opaque detail' not in caplog.text


def test_infrastructure_combined_capture_budget_is_independent_of_file_limits(tmp_path, monkeypatch):
    from api_agent.runtime import infrastructure_contract as module
    from test_infrastructure_contract import _context
    context = _context(tmp_path)
    services = module.load_infrastructure_contributions(context)
    size = sum(len(s.declaration_source.body) + len(s.compose_source.body) +
               sum(len(a.body) for a in s.assets) for s in services)
    monkeypatch.setattr(module, 'MAX_CAPTURE_BYTES', size)
    assert module.load_infrastructure_contributions(context)[0].id == 'example/api'
    monkeypatch.setattr(module, 'MAX_CAPTURE_BYTES', size - 1)
    with pytest.raises(module.InfrastructureDeclarationError, match='capture exceeds budget'):
        module.load_infrastructure_contributions(context)


def test_ended_remote_call_rejects_late_provider_fields():
    from api_agent.telemetry.remote_inference import RemoteTraceCall
    runtime = Mock()
    call = RemoteTraceCall(runtime, 'call')
    call.end()
    call.provider_fields('cause', 'transport', object(), object())
    runtime.bind_model_identities.assert_not_called()
    runtime.admit_provider_model.assert_not_called()
    runtime.begin.return_value.end.assert_called_once()


def test_remote_binding_without_pipeline_never_enables_worker_export(monkeypatch):
    from api_agent.telemetry import tracing_providers
    from api_agent.telemetry.remote_inference import RemoteInferenceTrace
    binding = SimpleNamespace(pipeline_identity=None, mapping=__import__('api_agent.telemetry.trace_mapping',fromlist=['TraceMapping']).TraceMapping())
    monkeypatch.setattr(tracing_providers, 'admit_binding', lambda *a, **k: binding)
    trace = RemoteInferenceTrace(config(), EmissionBoundary())
    assert trace.enabled is False
    assert trace.begin('call') is None


@pytest.mark.parametrize('value', [1, None, 'true'])
def test_test_evidence_permission_requires_real_boolean(value):
    from api_agent.workflows.run_context import test_evidence
    with pytest.raises(TypeError, match='must be boolean'):
        with test_evidence(value):
            pytest.fail('invalid permission admitted')


def test_structural_numeric_and_reasoning_fields_keep_native_types_and_reject_secrets():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('response-id',))
    fields = {'gen_ai.request.reasoning.level': 'high', 'gen_ai.request.temperature': .75,
              'gen_ai.response.id': 'response-id', 'gen_ai.response.finish_reasons': ['invalid'],
              'gen_ai.tool.name': object()}
    assert _structural(boundary, fields, ()) == {
        'gen_ai.request.reasoning.level': 'high', 'gen_ai.request.temperature': .75}


def test_span_without_sdk_context_and_closed_span_operations_are_safe():
    boundary = EmissionBoundary(ContentPolicy(True))
    empty = TraceSpan(None, boundary)
    assert empty.export_context() is None
    invalid = TraceSpan(SimpleNamespace(get_span_context=lambda: SimpleNamespace(is_valid=False)), boundary)
    assert invalid.export_context() is None
    empty.structural({'gen_ai.request.model': 'unknown'})
    from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeState
    empty.outcome(ExecutionOutcome(OutcomeState.SUCCESS))
    empty.capture_loss(1)
    empty.native_io('input', {'input': 'visible'})
    with pytest.raises(ValueError, match='Invalid observation direction'):
        empty.native_io('sideways', {})
    with pytest.raises(ValueError, match='Invalid trace loss count'):
        empty.capture_loss(True)
    with pytest.raises(TypeError, match='versioned'):
        empty.outcome('success')


def test_runtime_response_identity_capacity_counts_only_new_aliases():
    from api_agent.providers.llm import LLMResult, Message, TextContent
    runtime = TraceRuntime(config())
    runtime._tracer = object()
    runtime._response_model_identities = frozenset(f'model-{i}' for i in range(256))
    runtime.admit_provider_model(LLMResult(Message('assistant', (TextContent('ok'),)), actual_model='model-0'))
    assert runtime.losses.count == 0
    runtime.admit_provider_model(LLMResult(Message('assistant', (TextContent('ok'),)), actual_model='new-model'))
    assert runtime.losses.count == 1
    assert not runtime.is_model_identity('new-model')


def test_record_call_sanitizes_unknown_outcome_and_invalid_duration():
    runtime = TraceRuntime(config())
    runtime._tracer = object()
    runtime._binding = Mock()
    runtime.record_call({}, float('nan'), 'invented')
    runtime._binding.record_call.assert_called_once_with({}, None, 'unknown', attempts=None)


def test_disabled_runtime_parent_import_is_inert():
    runtime = TraceRuntime(config(mode=''))
    parent = {'version': 1, 'trace_id': '1' * 32, 'span_id': '2' * 16, 'trace_flags': 1}
    assert runtime.pipeline_id is None
    assert runtime.import_parent(parent).export_context() is None


def adapter():
    from api_agent.workflows.observations import WorkflowObservationFactory
    trace = TraceRuntime(config())
    trace._tracer = Mock()
    return WorkflowObservationFactory(config(), boundary=EmissionBoundary(), trace=trace).create(definition())


def test_observation_fields_require_matching_live_visit_and_bound_capacity():
    from api_agent.workflows.attribution import bind_workflow_visit
    observed = adapter()
    from api_agent.providers.llm import LLMRequest, LLMResult, CompletionRequest, Message, TextContent
    from uuid import uuid4
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hi'),)),)), str(uuid4()))
    result = LLMResult(Message('assistant', (TextContent('ok'),)))
    observed.provider_fields('call', 'transport', request, result)
    assert observed._call_fields == {}
    observed._spans['visit'] = Mock()
    observed._call_fields = {(f'call-{i}', 'transport'): {} for i in range(256)}
    with bind_workflow_visit(observed, 'visit'):
        observed.provider_fields('new', 'transport', request, result)
    observed.provider_diagnostics('new', 'transport', {'version': 1})
    assert len(observed._call_fields) == 256
    assert observed.trace_losses.count == 2
    observed.close()


@pytest.mark.parametrize('pipeline,visit,local', [(None, 'visit', True), ('a' * 64, 'missing', False)])
def test_remote_offer_requires_pipeline_and_exportable_parent(monkeypatch, pipeline, visit, local):
    observed = adapter()
    monkeypatch.setattr(TraceRuntime, 'pipeline_id', property(lambda self: pipeline))
    offer = observed.remote_offer('call', visit)
    assert offer.kwargs == {}
    assert offer.state == ('inactive' if local else None)
    offer.close()
    assert observed._remote_pending == 0
    observed.close()


def test_closed_observation_finalizer_counts_loss_and_tool_cleanup_failures():
    observed = adapter()
    observed._tool_spans['tool'] = SimpleNamespace(end=Mock(side_effect=RuntimeError('detail')))
    observed.close()
    assert observed.trace_losses.count == 1
    observed._trace_row({}, _finalizing=True)
    assert observed.trace_losses.count == 2
    assert observed._tool_spans == {}
    with pytest.raises(TypeError, match='versioned'):
        observed.report_outcome('scope', 'success')


def test_tool_terminal_without_live_span_cannot_create_replacement():
    observed = adapter()
    observed._trace_row({'event': 'tool_end', 'tool_invocation_id': 'missing'})
    observed.trace._tracer.start_span.assert_not_called()
    observed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [False, True])
async def test_parsed_executor_automatic_observation_owner_closes_after_terminal_contract(valid, monkeypatch, tmp_path):
    from api_agent.workflows.contrived import build
    from api_agent.workflows.execution import WorkflowError
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeState
    from test_workflow_trial_decisions import trial_uuid
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    factory = WorkflowObservationFactory(config(), boundary=EmissionBoundary())
    create = factory.create
    owners = []
    def tracked_create(*args, **kwargs):
        owner = create(*args, **kwargs)
        owners.append(owner)
        return owner
    monkeypatch.setattr(factory, 'create', tracked_create)
    from pathlib import Path
    workflow = build(Path(__file__).resolve().parents[2], llm=AsyncMock(), selection='model', trial=trial_uuid('skip'))
    workflow.services['observation_factory'] = factory
    workflow.services['execution_outcome'] = lambda: ExecutionOutcome(OutcomeState.SUCCESS) if valid else 'success'
    workflow.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={'values': {'answer': 'ok'}}))
    if valid:
        assert await workflow.arun({}) == {'answer': 'ok'}
    else:
        with pytest.raises(WorkflowError, match='Invalid explicit execution outcome'):
            await workflow.arun({})
    assert len(owners) == 1
    assert owners[0]._closed


from test_workflow_stateful import fixture_graph


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [False, True])
async def test_stateful_executor_validates_terminal_contract_without_optional_observer(fixture_graph, valid):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_definition, load_stateful_catalog
    from langgraph.graph.message import add_messages
    from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeState
    root, entry = fixture_graph
    declared = load_stateful_definition(entry, base_root=root, pack_root=root)
    workflow = StatefulWorkflow(declared, catalog=load_stateful_catalog(declared),
        reducers={'messages': add_messages}, services={'execution_outcome': lambda:
            ExecutionOutcome(OutcomeState.SUCCESS) if valid else 'success'})
    if valid:
        result = await workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='conversation')
        assert result['visits'] == 1
    else:
        with pytest.raises(ValueError, match='Invalid explicit execution outcome'):
            await workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='conversation')


@pytest.mark.asyncio
async def test_binding_start_without_loss_hook_and_completed_close_task_are_admitted():
    binding = SimpleNamespace(mapping=__import__('api_agent.telemetry.trace_mapping', fromlist=['TraceMapping']).TraceMapping(),
        open=AsyncMock(return_value=(SimpleNamespace(), Mock())), aclose=AsyncMock(), handles=None)
    runtime = TraceRuntime(config(), binding=binding)
    await runtime.astart()
    assert runtime.active
    done = asyncio.create_task(binding.aclose())
    await done
    runtime._binding_close_task = done
    runtime._provider = None
    await runtime.aclose()
    binding.aclose.assert_awaited_once()
    assert runtime._binding_closed


def test_remote_owned_terminal_discards_only_matching_deferred_content():
    from api_agent.telemetry.worker_trace import WorkerTraceOffer
    from uuid import uuid4
    observed = adapter()
    call = str(uuid4())
    observed.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    observed._call_payloads = {(call, None): {'input': 'local'}, ('other', None): {'input': 'retain'}}
    offer = WorkerTraceOffer(observed, call, {})
    offer.receipt('granted')
    with offer.completion():
        observed.record_call(visit_id='visit', call_id=call, selection='model', outcome='completed')
    assert observed._call_payloads == {('other', None): {'input': 'retain'}}
    assert observed.trace._tracer.start_span.call_count == 1
    observed.close()



def test_tool_capture_survives_declined_tool_span_admission():
    from api_agent.workflows.observations import WorkflowObservationFactory
    from uuid import uuid4
    from api_agent.telemetry.operation import TraceLoss
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)), losses=TraceLoss())
    observed = WorkflowObservationFactory(config(trace=True), boundary=EmissionBoundary(), trace=trace).create(definition())
    observed.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make', started_ms=1))
    observed._tool_spans = {str(i): Mock() for i in range(256)}
    identifier = str(uuid4())
    observed.observe(dict(event='tool_start', tool_invocation_id=identifier, visit_id='visit',
                         attempt_id=None, started_ms=2, tool_name='lookup'))
    assert identifier not in observed._tool_spans
    observed.capture_json(visit_id='visit', direction='input', values={'text': 'visible'},
        schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}, tool_invocation_id=identifier)
    assert trace.begin.call_count == 1
    trace.begin.return_value.event.assert_called_once()
    trace.begin.return_value.native_io.assert_not_called()
    assert observed.trace_losses.count == 1
    observed.close()


@pytest.mark.asyncio
async def test_startup_failure_after_provider_assignment_retains_disposal_owner():
    from api_agent.telemetry.tracing import TraceError
    class Provider:
        force_flush = Mock(return_value=True)
        shutdown = Mock()
        @property
        def trace_loss_owner(self):
            raise RuntimeError('opaque initialization error')
    provider = Provider()
    runtime = TraceRuntime(config(), factory=lambda *args: (provider, Mock()))
    with pytest.raises(TraceError, match='Tracing initialization failed'):
        await runtime.astart()
    assert provider in runtime._provider.owners
    await runtime.aclose()
    provider.shutdown.assert_called_once()
    assert runtime.disposal_confirmed


@pytest.mark.parametrize('mode',['','OTEL'])
def test_unstarted_tracing_does_not_process_optional_call_measurements(mode):
    runtime=TraceRuntime(config(mode=mode))

    runtime.record_call({},0,'completed',attempts=object())

    assert not runtime.active
    assert runtime.losses.count==0
