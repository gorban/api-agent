"""Provider-owned payloads survive a separate backend without credential leakage."""
import asyncio
import json
import multiprocessing
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import bind_boundary, capture_rows, collect_inference
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import config, definition

SECRET = 'synthetic-provider-owned-secret'
VISIBLE = 'visible nested payload ' + 'x' * 800
CAPABILITY = 'synthetic-payload-capture-capability-000000'


def backend_process(addresses, stop, log, fail, text=VISIBLE, auth_secret=SECRET):
    async def run():
        from api_agent.providers.shared_setup import SharedSetupOwner
        boundary = EmissionBoundary(ContentPolicy(log))
        async def authenticate(callback):
            return await callback(auth=auth_secret)
        async def provider(request, *, auth):
            assert auth == auth_secret
            if fail == 'cancel':
                raise asyncio.CancelledError()
            if fail:
                raise ValueError('synthetic failure')
            return LLMResult(Message('assistant', (TextContent(text + auth_secret),)), Usage(3, 4))
        shared = SharedSetupOwner('operation', 'selection',
            lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()), complete=provider)
        async def complete(request):
            bind_boundary(boundary)
            return await shared.complete(request)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=shared.aclose)
        registry = SetupRegistry('operation', 'selection', lambda: owner)
        server = SetupServer(registry, CAPABILITY)
        try:
            addresses.put(await server.start())
            await asyncio.to_thread(stop.wait, 10)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('fail', [False, True, 'cancel'])
def test_separate_backend_authority_full_values_flags_and_failure(tmp_path, log, trace, fail):
    status = 'cancelled' if fail == 'cancel' else 'failed' if fail else 'passed'
    exit_code = 130 if fail == 'cancel' else int(fail)
    context = multiprocessing.get_context('spawn')
    addresses, stop = context.Queue(), context.Event()
    process = context.Process(target=backend_process, args=(addresses, stop, log, fail))
    process.start()
    client = None
    try:
        client = SetupClient(addresses.get(timeout=10), timeout=5)
        request = LLMRequest('synthetic-model', CompletionRequest((Message('user', (TextContent(VISIBLE + SECRET),)),)))
        if fail:
            with pytest.raises(Exception):
                client.complete(request)
        else:
            result = client.complete(request)
            assert SECRET in result.payload.content[0].text
        rows = client.evidence
        assert [row['phase'] for row in rows] == (['request'] if fail else ['request', 'response'])
        serialized = json.dumps(rows)
        assert SECRET not in serialized
        assert (VISIBLE in serialized) is log
        assert all(row['reason'] == ('captured' if log else 'withheld') for row in rows)
        if log:
            assert rows[0]['content']['messages'][0]['text'] == VISIBLE + '[REDACTED]'
            assert rows[0]['content']['selected_llm'] == 'synthetic-model'
            if not fail:
                assert rows[1]['content']['usage']['input_tokens'] == 3
        from unittest.mock import Mock
        span = Mock()
        boundary = EmissionBoundary()
        runtime = SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace)),
            begin=Mock(return_value=span), is_model_identity=lambda value: False)
        adapter = WorkflowObservationFactory(config(log, trace), boundary=boundary, trace=runtime).create(definition())
        visit, call, graph = str(uuid4()), str(uuid4()), str(uuid4())
        adapter.observe(dict(event='scope_start', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999))
        adapter.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
        adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000))
        from api_agent.workflows.attribution import bind_workflow_visit
        with bind_workflow_visit(adapter, visit):
            capture_rows(rows, call)
        adapter.record_call(visit_id=visit, call_id=call, usage=None if fail else {'input_tokens': 3, 'output_tokens': 4}, outcome=status if fail else 'completed')
        adapter.observe(dict(event='scope_end', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000, stopped_ms=1001, outcome=status))
        adapter.observe(dict(event='edge', owner='GRAPH', source='Make', target='END', implicit=True, visit_id=visit))
        adapter.observe(dict(event='scope_end', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999, stopped_ms=1002, outcome=status))
        retained = adapter.finalize()
        captures = [row for row in retained['events'] if row['event'] == 'capture']
        assert all(row['cause_id'] == call and row['visit_id'] == visit for row in captures)
        assert ('llm_request' in json.dumps(retained)) is log
        # Continue the same provider phases and journal through publication,
        # then reopen the digest-bound files from the finalized manifest.
        from api_agent.testing.contracts import RunnerEvent, TestResult
        from api_agent.testing.integration import IntegrationRunResult
        from api_agent.testing.recorded import run_recorded_integration
        from api_agent.testing.payloads import PayloadResolver, hydrate_journal
        from test_recorded_integration import fixture, Storage
        recording_context, selected, recording_config = fixture(tmp_path)
        recording_config['LOG_CONTENT'] = log
        def runner(context, tests, **hooks):
            hooks['before_run']()
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(retained)))
            for row in rows:
                hooks['on_event'](RunnerEvent('inference_content', selected.repo, selected,
                    text=json.dumps(dict(call_id=call, **row))))
            outcome = TestResult(selected, status, .01)
            hooks['on_result'](outcome)
            execution = IntegrationRunResult(exit_code, (outcome,), '', fail == 'cancel')
            hooks['after_run'](execution)
            return execution
        published = run_recorded_integration(recording_context, (selected,), recording_config,
            accepted=True, storage=Storage(), runner=runner)
        assert published.persisted and published.exit_code == exit_code
        rendered = json.loads(next(published.directory.glob('*-result.json')).read_text())
        resolver = PayloadResolver.local(published.directory, published.manifest)
        index_name = next(row['source'] for row in rendered['attachments'] if row['name'] == 'Payload index')
        index = resolver.index(index_name)
        assert not any(row['name'] == 'Workflow execution evidence' for row in rendered['attachments'])
        graph = rendered['steps'][0]
        assert next(row['value'] for row in graph['parameters'] if row['name'] == 'invocation_id') == retained['invocation_id']
        journal_name = next(row['source'] for row in graph['attachments'] if row['name'] == 'Execution evidence')
        envelope = json.loads((published.directory / journal_name).read_text())
        assert hydrate_journal(envelope, resolver) == retained
        payload_bodies = [(published.directory / name).read_text() for name in published.manifest['files'] if name.startswith('payload-') and not name.startswith('payload-index-')]
        assert all(SECRET not in body for body in payload_bodies)
        assert sum(VISIBLE in body for body in payload_bodies) == (len(rows) if log else 0)
        assert all('values' not in row for row in envelope['events'] if row['event'] == 'capture')
    finally:
        if client is not None:
            client.close()
        stop.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
        addresses.close()


def test_provider_capture_oversized_has_explicit_omission():
    from api_agent.workflows.inference_capture import capture_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('input', {'messages': [{'text': 'é' * 33000}]}, str(uuid4()), auth=SECRET)
    assert collector['rows'] == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]


@pytest.mark.parametrize('failure', [ValueError('synthetic failure'), asyncio.CancelledError()])
def test_auth_callback_collector_keeps_request_on_failure_and_cancellation(failure):
    from api_agent.providers.shared_setup import SharedSetupOwner
    async def run():
        async def authenticate(callback):
            return await callback(auth=SECRET)
        async def provider(request, *, auth):
            raise failure
        shared = SharedSetupOwner('operation', 'selection',
            lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()), complete=provider)
        call_id = str(uuid4())
        with collect_inference(EmissionBoundary(ContentPolicy(True)), call_id) as collector:
            try:
                request = LLMRequest('synthetic-model', CompletionRequest((Message('user', (TextContent(VISIBLE + SECRET),)),)))
                with pytest.raises(type(failure)):
                    await shared.complete(request)
            finally:
                await shared.aclose()
        assert collector['call_id'] == call_id
        assert [row['phase'] for row in collector['rows']] == ['request']
        assert collector['rows'][0]['content']['messages'][0]['text'] == VISIBLE + '[REDACTED]'
    asyncio.run(run())


def test_auth_collector_preserves_request_settings_and_response_metadata():
    from api_agent.workflows.inference_capture import capture_inference
    wire = {'reasoning_effort': 'high', 'max_output_tokens': 42,
            'usage': {'input_tokens': 7, 'output_tokens': 8, 'reasoning_tokens': 2},
            'message': {'text': VISIBLE, 'continuation': {'state': 'hidden-sentinel'}},
            'hidden_reasoning': 'hidden-sentinel'}
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('output', wire, str(uuid4()), auth=SECRET)
    safe = collector['rows'][0]['content']
    assert safe['reasoning_effort'] == 'high'
    assert safe['max_output_tokens'] == 42
    assert safe['usage'] == wire['usage']
    assert safe['message'] == {'text': VISIBLE}
    assert 'hidden-sentinel' not in json.dumps(safe)


@pytest.mark.parametrize('value', ['synthetic-secret-value', -1, True, 2 ** 54])
def test_request_token_limit_exception_never_permits_arbitrary_credentials(value):
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.content_export({'max_output_tokens': value}) == {}


@pytest.mark.parametrize('authority', [True, False])
def test_empty_authoritative_collector_never_invokes_secondary_exporter(authority):
    from test_setup_transport import Registry, CAPABILITY
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('Ok'),)),)))
    expected = LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(1, 1))
    exported = []
    class Owner(Registry):
        async def complete(self, binding, request):
            if authority:
                bind_boundary(EmissionBoundary(ContentPolicy(True)))
            else:
                from api_agent.workflows.inference_capture import _COLLECTOR
                _COLLECTOR.get()['rows'].append({'phase': 'request', 'content': {'text': 'already-admitted'}})
            return expected
        def export_content(self, *args):
            exported.append(True)
            return [{'phase': 'request', 'content': {'text': 'unsafe-secondary'}}]
    async def run():
        server = SetupServer(Owner(), CAPABILITY, timeout=2)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                assert client.complete(request) == expected
                assert client.evidence == ([] if authority else [{'phase': 'request', 'content': {'text': 'already-admitted'}}])
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
            assert exported == []
        finally:
            await server.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [ValueError('synthetic observer'), asyncio.CancelledError()])
def test_optional_capture_observer_failure_does_not_escape(failure):
    from api_agent.workflows.attribution import bind_workflow_visit
    def broken(**kwargs):
        raise failure
    owner = SimpleNamespace(capture_json=broken)
    rows = [{'phase': 'request', 'content': {'text': VISIBLE}}]
    with bind_workflow_visit(owner, 'synthetic-visit'):
        capture_rows(rows, str(uuid4()))
    assert rows == [{'phase': 'request', 'content': {'text': VISIBLE}}]


@pytest.mark.parametrize('failure', [ValueError('synthetic boundary'), asyncio.CancelledError()])
def test_optional_provider_projection_failure_never_retains_raw_payload(failure):
    from api_agent.workflows.inference_capture import capture_inference
    bind_boundary(EmissionBoundary())  # No active collector is inert.
    class BrokenBoundary:
        def register_secret(self, value):
            raise failure
    with collect_inference(BrokenBoundary()) as collector:
        capture_inference('input', {'text': VISIBLE + SECRET}, str(uuid4()), auth=SECRET)
    assert collector['rows'] == []


def test_client_omits_oversized_evidence_without_losing_result(monkeypatch):
    from api_agent.providers import setup_transport
    from test_setup_transport import Registry, CAPABILITY
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('Ok'),)),)))
    expected = LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(1, 1))
    class Owner(Registry):
        async def complete(self, binding, request):
            return expected
        def export_content(self, *args):
            return [{'phase': 'request', 'content': {'text': 'x' * 200000}}]
    # A faulty sender bypasses its optional-frame limiter. The independent
    # receiver bound must still omit content while preserving completion.
    monkeypatch.setattr(setup_transport, '_content_frame', lambda row:
        setup_transport._encode(dict(version=1, status='evidence', **row)))
    async def run():
        server = SetupServer(Owner(), CAPABILITY, timeout=2)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [dict(phase='request', content={}, reason='truncated')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('auth', [None, ''])
def test_public_provider_capture_without_auth_and_without_workflow_is_valid(auth):
    from api_agent.workflows.inference_capture import capture_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('input', {'messages': [{'text': VISIBLE}]}, str(uuid4()), auth=auth)
    assert collector['rows'][0]['content'] == {'messages': [{'text': VISIBLE}]}
    # Calls outside a parsed workflow remain available as provider phases and
    # do not fabricate an active node visit during worker rebind.
    capture_rows(collector['rows'], collector['call_id'])


@pytest.mark.parametrize('text,auth_secret,reason', [
    ('é' * 33000, SECRET, 'truncated'),
    ('ordinary visible', 'values', 'unavailable'),
    ('🧪' * 6000 + '\n"\\', SECRET, 'captured'),
    ('🧪' * 11000, SECRET, 'truncated'),
], ids=['utf8_overflow', 'authority_unavailable', 'unicode_roundtrip', 'ipc_unicode_overflow'])
def test_separate_backend_unicode_and_omitted_phases_finalize_exactly(tmp_path, text, auth_secret, reason):
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.testing.payloads import PayloadResolver, hydrate_journal
    from test_recorded_integration import fixture, Storage
    context = multiprocessing.get_context('spawn')
    addresses, stop = context.Queue(), context.Event()
    process = context.Process(target=backend_process, args=(addresses, stop, True, False, text, auth_secret))
    process.start()
    client = None
    try:
        client = SetupClient(addresses.get(timeout=10), timeout=5)
        request = LLMRequest('synthetic-model', CompletionRequest((Message('user', (TextContent(text + auth_secret),)),)))
        result = client.complete(request)
        assert result.payload.content[0].text == text + auth_secret
        assert result.usage == Usage(3, 4)
        rows = client.evidence
        assert [row['phase'] for row in rows] == ['request', 'response']
        assert [row['reason'] for row in rows] == [reason, reason]
        assert all(auth_secret not in json.dumps(row, ensure_ascii=False) for row in rows)
        adapter = WorkflowObservationFactory(config(True, False), boundary=EmissionBoundary()).create(definition())
        visit, call, graph = str(uuid4()), str(uuid4()), str(uuid4())
        adapter.observe(dict(event='scope_start', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999))
        adapter.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
        adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000))
        with bind_workflow_visit(adapter, visit):
            capture_rows(rows, call)
        adapter.record_call(visit_id=visit, call_id=call, usage={'input_tokens': 3, 'output_tokens': 4}, outcome='completed')
        adapter.observe(dict(event='scope_end', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000, stopped_ms=1001, outcome='passed'))
        adapter.observe(dict(event='edge', owner='GRAPH', source='Make', target='END', implicit=True, visit_id=visit))
        adapter.observe(dict(event='scope_end', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999, stopped_ms=1002, outcome='passed'))
        retained = adapter.finalize()
        captures = [row for row in retained['events'] if row['event'] == 'capture']
        assert [row['reason'] for row in captures] == [reason, reason]
        expected_values = ([{'llm_request': rows[0]['content']}, {'llm_response': rows[1]['content']}]
                           if reason == 'captured' else [{}, {}])
        assert [row['values'] for row in captures] == expected_values
        recording_context, selected, recording_config = fixture(tmp_path)
        recording_config['LOG_CONTENT'] = True
        def runner(context, tests, **hooks):
            hooks['before_run']()
            for row in rows:
                hooks['on_event'](RunnerEvent('inference_content', selected.repo, selected,
                    text=json.dumps(dict(call_id=call, **row), ensure_ascii=False)))
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(retained)))
            outcome = TestResult(selected, 'passed', .01)
            hooks['on_result'](outcome)
            execution = IntegrationRunResult(0, (outcome,), '')
            hooks['after_run'](execution)
            return execution
        published = run_recorded_integration(recording_context, (selected,), recording_config,
            accepted=True, storage=Storage(), runner=runner)
        assert published.persisted and published.exit_code == 0
        rendered = json.loads(next(published.directory.glob('*-result.json')).read_text())
        resolver = PayloadResolver.local(published.directory, published.manifest)
        resolver.index(next(row['source'] for row in rendered['attachments'] if row['name'] == 'Payload index'))
        assert not any(row['name'] == 'Workflow execution evidence' for row in rendered['attachments'])
        graph = rendered['steps'][0]
        assert next(row['value'] for row in graph['parameters'] if row['name'] == 'invocation_id') == retained['invocation_id']
        name = next(row['source'] for row in graph['attachments'] if row['name'] == 'Execution evidence')
        envelope = json.loads((published.directory / name).read_text())
        assert hydrate_journal(envelope, resolver) == retained
        payload_names = [name for name in published.manifest['files'] if name.startswith('payload-') and not name.startswith('payload-index-')]
        assert len(payload_names) == (2 if reason == 'captured' else 0)
        if reason == 'captured':
            assert rows[0]['content']['messages'][0]['text'] == text + '[REDACTED]'
            assert rows[1]['content']['text'] == text + '[REDACTED]'
    finally:
        if client is not None:
            client.close()
        stop.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
        addresses.close()


@pytest.mark.parametrize('reason', ['captured', 'partial'])
def test_available_empty_wire_remains_distinct_from_omitted_phase(reason):
    from api_agent.workflows.attribution import bind_workflow_visit
    calls = []
    journal = SimpleNamespace(capture_json=lambda **kwargs: calls.append(kwargs))
    with bind_workflow_visit(journal, 'synthetic-visit'):
        capture_rows([dict(phase='response', content={}, reason=reason)], str(uuid4()))
    assert calls[0]['values'] == {'llm_response': {}}
    assert calls[0]['capture_reason'] == reason
