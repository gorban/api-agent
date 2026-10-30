"""Trace ownership, destination separation and failure receipts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.remote_inference import RemoteInferenceTrace, RemoteTraceCall
from api_agent.telemetry.tracing import TraceDeliveryError, TraceError
from api_agent.workflows import inference_capture as capture
from test_setup_transport import Registry, CAPABILITY
from test_workflow_observations import config


@pytest.fixture
def trace_call():
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    runtime.spans = []
    def begin(*args, **kwargs):
        span = Mock()
        runtime.spans.append(span)
        return span
    runtime.begin.side_effect = begin
    return RemoteTraceCall(runtime, str(uuid4()))


def ledger():
    return {'attempts': [dict(id=str(uuid4()), index=1, started_ms=100, stopped_ms=120, status=200, outcome='completed')]}


def test_disabled_remote_trace_does_not_allocate_sdk():
    factory = Mock()
    owner = RemoteInferenceTrace(config(mode=''), EmissionBoundary(), factory=factory)
    asyncio.run(owner.astart())
    assert owner.begin(str(uuid4())) is None
    asyncio.run(owner.aclose())
    factory.assert_not_called()


def test_remote_trace_retains_unacknowledged_cleanup_and_reports_confirmed_loss_once(monkeypatch):
    owner = RemoteInferenceTrace(config(), EmissionBoundary())
    owner.runtime.aclose = AsyncMock(side_effect=[TraceError('pending'), TraceDeliveryError('lost'), TraceDeliveryError('lost')])
    with pytest.raises(TraceError, match='pending'):
        asyncio.run(owner.aclose())
    with pytest.warns(RuntimeWarning, match='Trace delivery failed') as warnings:
        asyncio.run(owner.aclose())
        asyncio.run(owner.aclose())
    assert len(warnings) == 1


def test_enabled_owner_binds_its_existing_runtime():
    owner = RemoteInferenceTrace(config(), EmissionBoundary())
    call = owner.begin(str(uuid4()))
    assert call.runtime is owner.runtime
    assert call.boundary is owner.runtime.boundary
    call.end()
    asyncio.run(owner.aclose())


@pytest.mark.parametrize('large', [False, True])
def test_trace_frames_keep_attempt_ownership_and_payload_reason(trace_call, large):
    attempts = ledger()
    text = 'visible' * (20000 if large else 1)
    rows = [dict(phase='request', content={'prompt': 'input'}, reason='captured'),
            dict(phase='response', content={'reasoning_content': text}, reason='partial',
                 transport_attempt_id=attempts['attempts'][0]['id'])]
    trace_call.publish(SetupServer._prepare_evidence(rows), attempts)
    child = trace_call.runtime.spans[1]
    root = trace_call.span
    root.event.assert_called_once_with(dict(phase='request', reason='captured', capture_source='provider', values={'prompt':'input'}),
        structural={'api_agent.capture.phase':'request', 'api_agent.capture.reason':'captured', 'api_agent.capture.source':'provider'})
    # The actual child is the receiver of the second event, not a root duplicate.
    assert len(trace_call.runtime.begin.call_args_list) == 2
    args = trace_call.runtime.begin.call_args
    assert args.kwargs['parent'] is root
    assert args.kwargs['start_time'] == 100_000_000
    assert args.args[1]['api_agent.transport_id'] == attempts['attempts'][0]['id']
    assert root.event.call_count == 1
    child.event.assert_called_once_with(dict(phase='response', reason='partial', capture_source='provider', values={'reasoning_content':text}),
        structural={'api_agent.capture.phase':'response', 'api_agent.capture.reason':'partial', 'api_agent.capture.source':'provider'},
        timestamp=120_000_000)
    child.end.assert_called_once_with(end_time=120_000_000)


@pytest.mark.parametrize('row', [dict(phase='bad', content={}), dict(phase='request', content={}, transport_attempt_id=str(uuid4()))])
def test_trace_rejects_invalid_or_ownerless_projection(trace_call, row):
    raw = json.dumps(dict(version=1, status='evidence', **row)).encode()
    with pytest.raises(ContractError):
        trace_call.publish([((), 'evidence', raw)], ledger())


def test_trace_rejects_incomplete_chunk_sequence(trace_call):
    frames = SetupServer._prepare_evidence([dict(phase='response', content={'text': 'x' * 200000}, reason='captured')])
    assert frames[0][1] == 'evidence_start'
    with pytest.raises(ContractError, match='Incomplete trace'):
        trace_call.publish(frames[:1], None)


@pytest.mark.parametrize('failed,cancelled,status', [(False,False,'ok'), (True,False,'error'), (True,True,'cancelled')])
def test_trace_span_ends_once_even_after_loss(trace_call, failed, cancelled, status):
    trace_call.loss('unavailable')
    trace_call.end(failed=failed, cancelled=cancelled)
    trace_call.end()
    trace_call.span.attributes.assert_called_once_with({'status':status})
    trace_call.span.end.assert_called_once()
    trace_call.span.event.assert_called_once_with({'values':{}, 'reason':'unavailable'}, structural={'api_agent.capture.reason':'unavailable'})


@pytest.mark.parametrize('log,trace', [(False,False), (False,True), (True,False), (True,True)])
@pytest.mark.parametrize('raw', [False, True])
def test_trace_and_report_collectors_are_independent(log, trace, raw):
    boundary = EmissionBoundary(ContentPolicy(log))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace))))
    with capture.bind_inference_call(str(uuid4())), capture.collect_inference(boundary, retain_raw=raw) as state:
        capture.bind_inference_trace(owner)
        capture.capture_inference('input', {'prompt':'visible-input'}, 'request')
        capture.capture_inference('output', {'reasoning_content':'visible-output'}, 'request')
    assert ('visible-input' in repr(state['rows'])) is log
    assert ('visible-output' in repr(state['rows'])) is log
    assert ('visible-input' in repr(state['trace_collector']['rows'])) is trace
    assert ('visible-output' in repr(state['trace_collector']['rows'])) is trace


def test_trace_preserves_provider_precedence_and_partial_response():
    boundary = EmissionBoundary(ContentPolicy(False))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(True))))
    with capture.collect_inference(boundary, retain_raw=True) as state:
        capture.bind_inference_trace(owner)
        capture.capture_inference('input', {'prompt':'fallback'}, 'request', fallback=True)
        capture.capture_inference('input', {'prompt':'effective-provider-input'}, 'request')
        capture.capture_inference('input', {'prompt':'late-fallback'}, 'request', fallback=True)
        capture.capture_inference('output', {'reasoning_content':'partial-provider-response'}, 'request', partial=True)
    rows = capture.select_capture_rows(state['trace_collector']['rows'])
    assert rows[0]['content'] == {'prompt':'effective-provider-input'}
    assert rows[1]['content'] == {'reasoning_content':'partial-provider-response'}
    assert rows[1]['reason'] == 'partial'
    assert 'effective-provider-input' not in repr(state['rows'])


def test_same_authority_log_downgrade_keeps_trace_but_foreign_authority_revokes_it():
    boundary = EmissionBoundary(ContentPolicy(True))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=boundary))
    with capture.collect_inference(boundary, retain_raw=True) as state:
        capture.bind_inference_trace(owner)
        capture.bind_boundary(boundary.for_policy(ContentPolicy(False)))
        capture.capture_inference('input', {'prompt':'trace-only'}, 'request')
        assert 'trace-only' not in repr(state['rows'])
        assert 'trace-only' in repr(state['trace_collector']['rows'])
        capture.bind_boundary(EmissionBoundary(ContentPolicy(True)))
        capture.bind_inference_trace(owner)
        assert state['trace_collector'] is None


def test_withholding_veto_cannot_be_elevated_by_trace_permission():
    boundary = EmissionBoundary(ContentPolicy(True))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=boundary))
    capture.bind_inference_trace(owner)  # outside a call is inert
    with capture.collect_inference(boundary, retain_raw=True) as state:
        capture.bind_inference_trace(SimpleNamespace(begin=lambda call_id: None))
        assert state['trace_owner'] is None
        capture.bind_inference_trace(owner)
        with capture.withholding_inference_content():
            capture.capture_inference('input', {'prompt':'untrusted'}, 'request')
        assert 'untrusted' not in repr(state['rows'])
        assert 'untrusted' not in repr(state['trace_collector']['rows'])


@pytest.mark.parametrize('settled,raw,caller,reason', [(False,True,(),'unavailable'), (True,True,None,'withheld')])
def test_unsettled_or_unadmitted_call_cannot_export_payload(settled, raw, caller, reason):
    async def run():
        server = SetupServer(Registry(), CAPABILITY)
        call = Mock()
        boundary = EmissionBoundary(ContentPolicy(True))
        state = dict(trace_owner=call, trace_collector={'boundary':boundary, 'rows':[]}, retain_raw=raw,
                     trace_ledger=SimpleNamespace(snapshot=lambda: {'attempts':[]}))
        server._publish_evidence = AsyncMock()
        await server._finish_trace(state, caller, None, failed=True, cancelled=False, settled=settled)
        server._publish_evidence.assert_not_called()
        call.loss.assert_called_once_with(reason)
        assert call.publish.call_count == int(not settled)
        call.end.assert_called_once_with(failed=True, cancelled=False)
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError('secret'), asyncio.CancelledError()])
def test_trace_projection_failure_preserves_original_outcome(failure):
    async def run():
        server = SetupServer(Registry(), CAPABILITY)
        boundary, call = EmissionBoundary(ContentPolicy(True)), Mock()
        state = dict(trace_owner=call, trace_collector={'boundary':boundary, 'rows':[]}, retain_raw=False)
        server._publish_evidence = AsyncMock(side_effect=failure)
        await server._finish_trace(state, (), None, failed=True, cancelled=True, settled=True)
        call.end.assert_called_once_with(failed=True, cancelled=True)
    asyncio.run(run())


def test_new_cancellation_during_trace_projection_is_not_swallowed():
    async def run():
        server = SetupServer(Registry(), CAPABILITY)
        call = Mock()
        state = dict(trace_owner=call, trace_collector={'boundary':EmissionBoundary(), 'rows':[]}, retain_raw=False)
        server._publish_evidence = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await server._finish_trace(state, (), None, failed=False, cancelled=False, settled=True)
        call.end.assert_called_once_with(failed=False, cancelled=True)
    asyncio.run(run())


@pytest.mark.parametrize('provider', [False, True])
def test_provider_plaintext_permission_does_not_relax_workflow_field_declarations(provider):
    from api_agent.workflows.capture import project_values
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('selected-secret',))
    value = {'reasoning_content':'visible selected-secret', 'thinking':'visible',
             'analysis':'visible', 'encrypted_content':'never-export', 'hidden_reasoning':'never-export',
             'continuation_state':'never-export', 'internal_analysis':'never-export'}
    result = project_values(value, {'type':'object'}, boundary, _provider_content=provider)
    assert ('reasoning_content' in result) is provider
    assert ('analysis' in result) is provider
    assert result['thinking'] == 'visible'  # Existing generic field behavior is unchanged.
    assert 'never-export' not in repr(result)
    assert 'selected-secret' not in repr(result)


def test_invalid_provider_projection_flag_is_not_coerced():
    from api_agent.workflows.capture import project_values
    with pytest.raises(ValueError, match='provider content'):
        project_values({}, {'type':'object'}, EmissionBoundary(), _provider_content=1)


def test_trace_projection_uses_its_own_policy_and_complete_credential_union():
    async def run():
        server = SetupServer(Registry(), CAPABILITY,
                             authority=EmissionBoundary(secrets=('operation-secret',)))
        log_boundary = EmissionBoundary(ContentPolicy(False), secrets=('provider-secret',))
        trace_boundary = log_boundary.for_policy(ContentPolicy(True))
        call = Mock()
        state = dict(trace_owner=call, boundary=log_boundary,
                     trace_collector={'boundary':trace_boundary, 'rows':[]},
                     retain_raw=True, trace_ledger=SimpleNamespace(snapshot=lambda: {'attempts':[]}))
        async def publish(peer, rows, authority, **kwargs):
            generation, projection = authority.snapshot()
            assert projection.policy.allow_content
            assert projection._secrets == {'provider-secret','operation-secret','caller-secret'}
            assert authority.commit_if_current(generation, lambda: kwargs['destination']([]))
        server._publish_evidence = publish
        await server._finish_trace(state, ('caller-secret',), None, failed=False, cancelled=False, settled=True)
        call.publish.assert_called_once_with([], {'attempts':[]})
        call.end.assert_called_once_with(failed=False, cancelled=False)
    asyncio.run(run())


def test_profile_pool_freezes_explicit_content_provenance(monkeypatch, tmp_path):
    from api_agent.config.resolve import ResolvedValue
    from api_agent.testing import setup_backend
    from api_agent.testing.setup_pool import SetupPool
    fields = {'TRACELOOP_TRACE_CONTENT':ResolvedValue(True, 'environment', True)}
    configs = {'test':{'config':{'TRACELOOP_TRACE_CONTENT':True}, 'provenance':{}}}
    made = []
    def backend(config, root, **kwargs):
        made.append(kwargs)
        return SimpleNamespace(start=lambda: {}, close=lambda: None)
    monkeypatch.setattr(setup_backend, 'SetupBackend', backend)
    pool = SetupPool(configs, tmp_path, config_fields=fields)
    fields.clear()
    pool.start()
    assert made[0]['capture_sources'] == {'TRACELOOP_TRACE_CONTENT':{'value':True, 'source':'environment', 'explicit':True}}
    assert 'config_fields' not in made[0]
    pool.close()


@pytest.mark.parametrize('sources', [[], {'TRACELOOP_TRACE_CONTENT':{'value':False, 'source':'cli', 'explicit':True}}])
def test_backend_rejects_invalid_or_conflicting_provenance_before_allocating(sources, tmp_path):
    from api_agent.testing.setup_backend import SetupBackend
    with pytest.raises(ContractError, match='provenance'):
        SetupBackend({'TRACELOOP_TRACE_CONTENT':True}, tmp_path, capture_sources=sources)


@pytest.mark.parametrize('enabled', [False, True])
def test_backend_owns_trace_start_and_both_publication_and_close(monkeypatch, tmp_path, enabled):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import _SetupOwner
    from api_agent.telemetry import remote_inference
    async def run():
        provider = SimpleNamespace(emissions=EmissionBoundary(), _complete=AsyncMock(return_value='answer'),
                                   _close=AsyncMock(), _finish_execution=AsyncMock())
        trace = SimpleNamespace(astart=AsyncMock(), begin=Mock(return_value=None), aclose=AsyncMock())
        factory = Mock(return_value=trace)
        monkeypatch.setattr(provider_setup, 'IntegrationLLM', lambda *args, **kwargs: provider)
        monkeypatch.setattr(remote_inference, 'RemoteInferenceTrace', factory)
        backend = SimpleNamespace(config={'TRACE_MODE':'OTEL' if enabled else ''}, owner_root=tmp_path,
            unattended_index_source=None, password_manager_registry=None, interaction=None,
            quota_observer=None, authority=None, capture_sources={})
        owner = _SetupOwner(backend)
        with capture.collect_inference():
            assert await owner.complete('request') == 'answer'
        await owner.finish_execution()
        await owner.aclose()
        assert provider.closed
        assert factory.call_count == int(enabled)
        assert trace.astart.await_count == int(enabled)
        assert trace.aclose.await_count == 2 * int(enabled)
    asyncio.run(run())


def test_isolated_trace_projection_marks_clipping_after_redaction():
    async def run():
        boundary = EmissionBoundary(ContentPolicy(True), secrets=('selected-secret',))
        frames = await SetupServer._prepare_isolated([dict(phase='response', reason='captured',
            content={'reasoning_content':'selected-secret ' + 'x' * 70000,
                     'encrypted_content':'never-export'})], boundary, limit=65536)
        from api_agent.providers.setup_transport import _EvidenceReceiver
        receiver = _EvidenceReceiver()
        row = None
        for _, _, raw in frames:
            frame = json.loads(raw)
            row = frame if frame['status'] == 'evidence' else receiver.accept(frame)
        assert row['reason'] == 'truncated'
        assert row['content']['reasoning_content'].startswith('[REDACTED] ')
        assert len(row['content']['reasoning_content']) == 65536
        assert 'never-export' not in repr(row)
    asyncio.run(run())


@pytest.mark.parametrize('log,trace', [(False,False), (False,True), (True,False), (True,True)])
def test_provider_reasoning_survives_each_direct_workflow_destination(log, trace):
    from api_agent.telemetry.tracing import TraceRuntime
    from api_agent.workflows.observations import WorkflowObservationFactory
    from test_workflow_observations import collector, definition
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    with collector() as (endpoint, received):
        resolved = config(log, trace, TRACE_TARGET=endpoint)
        boundary = EmissionBoundary(secrets=('selected-secret',))
        runtime = TraceRuntime(resolved, boundary=boundary)
        asyncio.run(runtime.astart())
        rows = []
        adapter = WorkflowObservationFactory(resolved, boundary=boundary, sink=rows.append, trace=runtime).create(definition())
        graph, visit, call_id = str(uuid4()), str(uuid4()), str(uuid4())
        adapter.observe(dict(event='scope_start', scope_id=graph, owner='GRAPH', kind='graph'))
        adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, owner='Make', kind='node'))
        adapter.capture_json(visit_id=visit, direction='output', cause_id=call_id, capture_source='provider',
            values={'llm_response':{'provider_response':{'reasoning_content':'direct-visible-reasoning selected-secret',
                    'encrypted_content':'never-export', 'hidden_reasoning':'never-export'}}}, schema={'type':'object'})
        adapter.observe(dict(event='scope_end', scope_id=visit, owner='Make', kind='node', outcome='passed'))
        adapter.observe(dict(event='scope_end', scope_id=graph, owner='GRAPH', kind='graph', outcome='passed'))
        adapter.close()
        journal = adapter.finalize()
        asyncio.run(runtime.aclose())
    wire = str([ExportTraceServiceRequest.FromString(body) for _, body in received])
    assert ('direct-visible-reasoning' in repr(rows)) is log
    assert ('direct-visible-reasoning' in repr(journal)) is log
    assert ('direct-visible-reasoning' in wire) is trace
    assert all(secret not in repr(rows) + repr(journal) + wire for secret in ('selected-secret', 'never-export'))


@pytest.mark.parametrize('limit', [True, False, 65535, 65537, -1, '65536'])
def test_projection_process_rejects_noncanonical_trace_limit(limit):
    async def run():
        with pytest.raises(ContractError, match='projection unavailable'):
            await SetupServer._prepare_isolated([], EmissionBoundary(ContentPolicy(True)), limit=limit)
    asyncio.run(run())


def test_observation_identity_is_optional_and_authenticated_without_changing_inference():
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.setup_transport import _request_auth
    from test_setup_call_state import REQUEST
    registry = Registry()
    server = SetupServer(registry, CAPABILITY)
    binding = registry.register('worker')
    request = request_to_wire(REQUEST)
    frame = dict(version=1, action='complete', capability=CAPABILITY, timeout=3, binding=binding, request=request)
    assert server._request(frame) == 'complete'
    frame['observation_call_id'] = str(uuid4())
    assert server._request(frame) == 'complete'
    frame['observation_call_id'] = 'invalid'
    with pytest.raises(ContractError, match='observation identity'):
        server._request(frame)
    frame['observation_call_id'] = str(uuid4())
    server._authority_channel = object()
    frame['authority_nonce'] = 'a' * 64
    frame['capability'] = _request_auth(CAPABILITY, frame)
    assert server._request(frame) == 'complete'
    frame['observation_call_id'] = str(uuid4())
    with pytest.raises(ContractError, match='capability rejected'):
        server._request(frame)
    assert frame['request'] == request_to_wire(REQUEST)


@pytest.mark.parametrize('invalid', ['', 'call', 1, True, 'AAAAAAAA-0000-4000-8000-000000000001'])
def test_noncanonical_observation_identity_is_rejected_before_connect(invalid):
    client = SetupClient(dict(version=1, host='127.0.0.1', port=1, capability=CAPABILITY))
    from test_setup_call_state import REQUEST
    with pytest.raises(ContractError, match='observation identity'):
        client.complete(REQUEST, observation_call_id=invalid)
    assert client._observation_call_id is None


@pytest.mark.parametrize('trace', [None, {'boundary': object(), 'rows': []}])
def test_missing_trace_authority_cannot_publish_content(trace):
    async def run():
        server = SetupServer(Registry(), CAPABILITY)
        call = Mock()
        server._publish_evidence = AsyncMock()
        await server._finish_trace({'trace_owner': call, 'trace_collector': trace}, (), None,
                                   failed=False, cancelled=False, settled=True)
        call.loss.assert_called_once_with('unavailable')
        call.end.assert_called_once_with(failed=False, cancelled=False)
        server._publish_evidence.assert_not_called()
    asyncio.run(run())


def test_broken_trace_loss_and_end_callbacks_do_not_replace_the_result():
    async def run():
        server = SetupServer(Registry(), CAPABILITY)
        call = Mock()
        call.loss.side_effect = RuntimeError('unavailable sink')
        call.end.side_effect = RuntimeError('unavailable sink')
        server._publish_evidence = AsyncMock(side_effect=RuntimeError('projection unavailable'))
        state = dict(trace_owner=call, trace_collector={'boundary': EmissionBoundary(), 'rows': []}, retain_raw=False)
        await server._finish_trace(state, (), None, failed=False, cancelled=False, settled=True)
        call.loss.assert_called_once_with('unavailable')
        call.end.assert_called_once_with(failed=False, cancelled=False)
    asyncio.run(run())
