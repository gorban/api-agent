"""Failure annotation preserves the original physical payload and its authority."""
from copy import deepcopy
from uuid import uuid4

import pytest

from api_agent.providers.contracts import CandidateOutputError, ProviderAccessError
from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows import inference_capture as capture


def failure():
    return ProviderAccessError('unknown', 'visible rejection registered-secret', status=503,
        code='private-provider-code', type='provider_failure', request_id='private-request')


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_independent_lanes_annotate_one_existing_native_response(log, trace, monkeypatch):
    token = str(uuid4())
    monkeypatch.setattr('api_agent.providers.generation_attempts.current_transport_attempt', lambda: token)
    boundary = EmissionBoundary(ContentPolicy(log), secrets=['registered-secret'])
    with capture.collect_inference(boundary) as report:
        with capture.collect_inference(boundary.for_policy(ContentPolicy(trace))) as traced:
            pass
        report['trace_collector'] = traced
        native = {'provider_response': {'choices': [{'message': {'role': 'assistant', 'content': 'native reply'}}]}}
        capture.capture_inference('output', native, 'call')
        before_report = deepcopy(report['rows'])
        before_trace = deepcopy(traced['rows'])
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=token)
        capture.capture_inference_failure(RuntimeError('second must not replace first'), 'call', transport_attempt_id=token)
    for state, allowed, before in ((report, log, before_report), (traced, trace, before_trace)):
        row, = state['rows']
        selected, = capture.select_capture_rows(state['rows'])
        assert selected['reason'] == before[0]['reason']
        assert row['transport_attempt_id'] == token
        assert row['diagnostics']['failure_code'] == 'provider'
        assert ('application_failure' in row['content']) is allowed
        if allowed:
            assert row['content']['provider_response'] == before[0]['content']['provider_response']
            assert row['content']['application_failure']['provider']['status'] == 503
        assert 'registered-secret' not in repr(row)


@pytest.mark.parametrize('collision,remaining', [(True, 100000), (False, 0)])
def test_collision_or_budget_pressure_keeps_native_response(collision, remaining):
    token = str(uuid4())
    native = {'provider_response': {'text': 'native body'}}
    if collision:
        native['application_failure'] = {'native': 'untrusted collision'}
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        row = {'phase': 'response', 'content': native, 'reason': 'partial',
               'transport_attempt_id': token, '_rejected': True}
        state['rows'].append(row)
        state['charged'][id(row)] = len(__import__('json').dumps(native, separators=(',', ':')).encode())
        state['remaining'] = remaining
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=token)
    assert row['content'] == native
    assert row['reason'] == 'partial' and row['_rejected'] is True
    assert len(state['rows']) == 1


def test_missing_response_gets_failure_only_fallback_and_no_unknown_exception_text():
    token = str(uuid4())
    class UnknownError(RuntimeError):
        def __str__(self):
            pytest.fail('unknown error text must not be inspected')
        def __repr__(self):
            pytest.fail('unknown error representation must not be inspected')
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        capture.capture_inference_failure(UnknownError(), 'call', transport_attempt_id=token)
    row, = state['rows']
    assert row['_source'] == 'fallback'
    assert row['content']['application_failure'] == {'version': 1, 'cause': 'execution', 'code': 'execution'}


def test_completed_token_targets_only_its_response_after_active_transport_ends(monkeypatch):
    first, second = str(uuid4()), str(uuid4())
    monkeypatch.setattr('api_agent.providers.generation_attempts.current_transport_attempt', lambda: None)
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        for token in (first, second):
            row = {'phase': 'response', 'content': {'provider_response': {'text': token}},
                   'reason': 'captured', 'transport_attempt_id': token}
            state['rows'].append(row)
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=second)
    assert 'application_failure' not in state['rows'][0]['content']
    assert 'application_failure' in state['rows'][1]['content']


def test_candidate_annotation_selects_visible_response_not_opaque_continuation():
    from api_agent.telemetry.failure_content import failure_content
    import json
    opaque = json.dumps({'provider': 'synthetic', 'state': [{'opaque': 'must-never-export'}]})
    result = LLMResult(Message('assistant', (TextContent('visible rejected response'),), continuation=opaque))
    error = CandidateOutputError(result, {'reason': 'response_schema', 'errors': [
        {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 8192, 'actual': 8666},
        {'path': ['pin'], 'keyword': 'minimum', 'constraint': 999999, 'actual': 123456}]})
    value = failure_content(error, candidate_feedback=error.feedback,
                                          candidate_text=''.join(part.text for part in error.result.payload.content))
    assert value['candidate']['visible_text'] == 'visible rejected response'
    assert value['candidate']['feedback']['errors'][0]['actual'] == 8666
    assert 'actual' not in value['candidate']['feedback']['errors'][1]
    assert 'must-never-export' not in repr(value)


def test_no_authority_never_creates_failure_content():
    with capture.collect_inference() as state:
        capture.capture_inference_failure(failure(), 'call')
    row, = state['rows']
    assert row['content'] == {} and row['reason'] == 'withheld'
    assert row['diagnostics']['failure_code'] == 'provider'


@pytest.mark.parametrize('log', [False, True])
def test_direct_journal_boundary_preserves_transient_annotation_for_independent_sinks(log, monkeypatch):
    from api_agent.workflows.journal import _JournalBoundary
    from types import SimpleNamespace
    token = str(uuid4())
    boundary = _JournalBoundary(ContentPolicy(log), EmissionBoundary(ContentPolicy(True),
        secrets=['registered-secret']))
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=boundary), 'visit'))
    row = {'phase': 'response', 'content': {'native': 'actual body'}, 'reason': 'captured',
           'transport_attempt_id': token}
    state = {'rows': [row], 'remaining': 100000, 'overflow': False}
    binding = capture._DIRECT_ROWS.set(state)
    try:
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=token)
    finally:
        capture._DIRECT_ROWS.reset(binding)
    assert row['content']['native'] == 'actual body'
    assert row['content']['application_failure']['provider']['status'] == 503
    assert 'registered-secret' not in repr(row)
    assert row['diagnostics']['failure_code'] == 'provider'


def test_malformed_optional_row_does_not_replace_primary_failure():
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        state['rows'].append(None)
        capture.capture_inference_failure(failure(), 'call')
    assert state['rows'] == [None]


def test_raw_candidate_annotation_preserves_rejection_admission_fact():
    token = str(uuid4())
    result = LLMResult(Message('assistant', (TextContent('rejected visible'),)))
    error = CandidateOutputError(result, {'reason': 'response_json'})
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as state:
        capture.capture_inference_failure(error, 'call', transport_attempt_id=token,
                                          candidate_feedback=error.feedback,
                                          candidate_text=''.join(part.text for part in error.result.payload.content))
    row, = state['rows']
    assert row['_rejected'] is True
    assert row['content']['application_failure']['candidate']['visible_text'] == 'rejected visible'


def test_failure_details_do_not_manufacture_provider_assistant_messages():
    from api_agent.telemetry.trace_mapping import genai_messages
    annotation = {'application_failure': {'version': 1, 'cause': 'provider', 'code': 'provider'}}
    assert genai_messages('output', {'llm_response': annotation}) is None
    native = {'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'actual provider reply'}]}}
    before = genai_messages('output', {'llm_response': native})
    assert before
    assert genai_messages('output', {'llm_response': {**native, **annotation}}) == before


def test_untrusted_feedback_literals_and_malformed_facts_are_not_failure_content():
    from api_agent.telemetry.failure_content import failure_content
    result = LLMResult(Message('assistant', (TextContent('visible'),)))
    error = CandidateOutputError(result, {'reason': 'private-reason', 'errors': [
        'private-fact', {'keyword': 'const', 'constraint': 'private-schema'},
        {'keyword': 'maxLength', 'path': 'private-path', 'constraint': 10}]})
    value = failure_content(error, candidate_feedback=error.feedback,
                                          candidate_text=''.join(part.text for part in error.result.payload.content))
    assert value['candidate']['feedback'] == {'errors': []}
    assert 'private-' not in repr(value)
    provider = ProviderAccessError('unknown', 'original public detail')
    # Exception attributes are mutable: optional capture must re-admit them.
    provider.message = {'untrusted': 'private-mutated-message'}
    assert failure_content(provider) == {'version': 1, 'cause': 'provider', 'code': 'provider'}


def test_excessive_candidate_annotation_cannot_displace_native_body():
    token = str(uuid4())
    result = LLMResult(Message('assistant', (TextContent('x' * 70000),)))
    error = CandidateOutputError(result, {'reason': 'response_json'})
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        row = {'phase': 'response', 'content': {'native': 'must survive'}, 'reason': 'captured',
               'transport_attempt_id': token}
        state['rows'].append(row)
        capture.capture_inference_failure(error, 'call', transport_attempt_id=token,
                                          candidate_feedback=error.feedback,
                                          candidate_text=''.join(part.text for part in error.result.payload.content))
    assert row['content'] == {'native': 'must survive'}
    assert row['diagnostics']['failure_code'] == 'schema_validation'


@pytest.mark.parametrize('collision', [False, True])
def test_raw_rejected_native_row_keeps_admission_guard_when_annotation_is_refused(collision):
    token = str(uuid4())
    error = CandidateOutputError(LLMResult(Message('assistant', (TextContent('rejected'),))),
                                 {'reason': 'response_json'})
    native = {'native': 'rejected actual body'}
    if collision:
        native['application_failure'] = {'native': 'collision'}
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as state:
        row = {'phase': 'response', 'content': native, 'reason': 'captured',
               'transport_attempt_id': token}
        state['rows'].append(row)
        state['remaining'] = 0 if not collision else 100000
        capture.capture_inference_failure(error, 'call', transport_attempt_id=token,
                                          candidate_feedback=error.feedback,
                                          candidate_text=''.join(part.text for part in error.result.payload.content))
    assert row['_rejected'] is True
    assert row['content'] == native


@pytest.mark.parametrize('post_validation', [False, True])
def test_shared_setup_failure_capture_precedes_credential_callback_retirement(post_validation, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.telemetry.outcomes import failure_diagnostic
    active, seen = [], []
    result = LLMResult(Message('assistant', (TextContent('actual rejected response'),)))
    error = CandidateOutputError(result, {'reason': 'response_json'}) if post_validation else failure()
    async def call(callback):
        active.append(True)
        try:
            return await callback(auth='callback-secret')
        finally:
            active.pop()
    async def complete(request, *, auth):
        if not post_validation:
            raise error
        return result
    async def validate(*args, **kwargs):
        raise error
    if post_validation:
        monkeypatch.setattr('api_agent.providers.rich_inference.async_validate_result', validate)
    def record(caught, cause_id, *, auth, transport_attempt_id, candidate_feedback=None, candidate_consistent=True, candidate_text=None):
        assert active and caught is error and auth == 'callback-secret'
        assert transport_attempt_id is not None
        seen.append(failure_diagnostic(caught))
    monkeypatch.setattr(capture, 'capture_inference_failure', record)
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        ledger = TransportAttemptLedger(1)
        try:
            with bind_transport_attempt_ledger(ledger):
                with pytest.raises(type(error)) as caught:
                    await owner.complete(LLMRequest('selected', CompletionRequest((
                        Message('user', (TextContent('request'),)),))))
                assert caught.value is error
            assert ledger.snapshot()['attempts'][0]['outcome'] == ('completed' if post_validation else 'failed')
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    assert not active and len(seen) == 1
    assert seen[0]['cause'] == ('validation' if post_validation else 'provider')


def test_shared_setup_annotation_describes_primary_response_contract_conflict(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from test_response_policy_contract import request, evidence
    req = request('json')
    original = ProviderAccessError('unknown', 'provider rejection', response=evidence(req))
    seen = []
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(value, *, auth):
        capture.record_response_dispatch(evidence(value, 'prompted_json', 'unsupported_mode'))
        raise original
    record = capture.capture_inference_failure
    def observe(error, *args, **kwargs):
        seen.append(error)
        return record(error, *args, **kwargs)
    monkeypatch.setattr(capture, 'capture_inference_failure', observe)
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with bind_transport_attempt_ledger(TransportAttemptLedger(1)):
                with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
                    with pytest.raises(ContractError, match='Conflicting') as caught:
                        await owner.complete(req)
                    assert seen == [caught.value]
            row, = [row for row in state['rows'] if row['phase'] == 'response']
            assert row['content']['application_failure']['code'] == 'contract_validation'
            assert 'provider' not in row['content']['application_failure']
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('refusal', ['bad-token', 'authority-conflict', 'overflow', 'no-owner'])
def test_failure_annotation_requires_admitted_active_capture_state(refusal):
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        if refusal == 'authority-conflict':
            state['authority_conflict'] = True
        elif refusal == 'overflow':
            state['overflow'] = True
        binding = capture._COLLECTOR.set(None) if refusal == 'no-owner' else None
        try:
            capture.capture_inference_failure(failure(), 'call', transport_attempt_id=
                'untrusted-token' if refusal == 'bad-token' else str(uuid4()))
        finally:
            if binding is not None:
                capture._COLLECTOR.reset(binding)
    assert state['rows'] == []


@pytest.mark.parametrize('disposition', ['ambiguous', 'withheld', 'malformed-diagnostics', 'capacity'])
def test_failure_annotation_cannot_repair_unavailable_or_conflicting_capture(disposition):
    token = str(uuid4())
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        row = {'phase': 'response', 'content': {}, 'reason': 'withheld' if disposition == 'withheld' else 'captured',
               'transport_attempt_id': token}
        if disposition == 'malformed-diagnostics':
            row['diagnostics'] = {'untrusted': 'private'}
        if disposition == 'capacity':
            state['rows'] = [{'phase': 'request', 'content': {}, 'reason': 'captured'} for _ in range(128)]
        else:
            state['rows'].append(row)
        if disposition == 'ambiguous':
            state['rows'].append({**row, 'content': {'native': 'conflicting'}})
        before = deepcopy(state['rows'])
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=token)
    if disposition == 'malformed-diagnostics':
        assert row['diagnostics'] == {'untrusted': 'private'}
    else:
        assert [r['content'] for r in state['rows']] == [r['content'] for r in before]
    assert len(state['rows']) == len(before)


def test_authentication_secret_is_registered_before_failure_projection():
    with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
        capture.capture_inference_failure(ProviderAccessError('unknown', 'active-auth-value'), 'call',
                                          auth='active-auth-value')
    row, = state['rows']
    assert 'active-auth-value' not in repr(row)


@pytest.mark.parametrize('allowed', [False, True])
def test_fixed_failure_metadata_remains_structural_without_content_authority(allowed):
    from api_agent.telemetry.genai_fields import diagnostic_fields
    from api_agent.telemetry.tracing import _structural
    boundary = EmissionBoundary(ContentPolicy(allowed))
    fields = diagnostic_fields({'version': 1, 'failure_cause': 'validation',
                                'failure_code': 'schema_validation'}, boundary)
    assert fields == {'api_agent.failure.cause': 'validation',
                      'api_agent.failure.code': 'schema_validation'}
    assert _structural(boundary, fields) == fields
    assert diagnostic_fields({'version': 1, 'failure_code': 'private-provider-code'}, boundary) == {}


def test_direct_capture_budget_never_erases_native_body_to_make_room_for_diagnostics(monkeypatch):
    from types import SimpleNamespace
    token = str(uuid4())
    boundary = EmissionBoundary(ContentPolicy(True))
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=boundary), 'visit'))
    row = {'phase': 'response', 'content': {'native': 'already retained'}, 'reason': 'captured',
           'transport_attempt_id': token}
    state = {'rows': [row], 'remaining': 0, 'overflow': False}
    binding = capture._DIRECT_ROWS.set(state)
    try:
        capture.capture_inference_failure(failure(), 'call', transport_attempt_id=token)
    finally:
        capture._DIRECT_ROWS.reset(binding)
    assert row == {'phase': 'response', 'content': {'native': 'already retained'},
                   'reason': 'captured', 'transport_attempt_id': token}
    assert state['remaining'] == 0


@pytest.mark.parametrize('contract', ['absent', 'rejecting', 'unavailable'])
def test_provider_candidate_claims_require_originating_request_confirmation(contract, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, StructuredOutput
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.telemetry.failure_content import failure_content
    result = LLMResult(Message('assistant', (TextContent('{"answer":"xxx"}'),)))
    forged = CandidateOutputError(result, {'reason': 'response_schema', 'errors': [
        {'path': ['private-invented-key'], 'keyword': 'maxLength', 'constraint': 123456, 'actual': 999999}]})
    assert failure_content(forged)['candidate'] == {'visible_text': '{"answer":"xxx"}'}
    if contract == 'unavailable':
        async def unavailable(*args, **kwargs):
            return None
        monkeypatch.setattr('api_agent.providers.rich_inference.async_schema_verdict', unavailable)
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(request, *, auth):
        raise forged
    schema = json.dumps({'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 2}}, 'required': ['answer'], 'additionalProperties': False})
    req = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=None if contract == 'absent' else StructuredOutput('answer', schema, False)))
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with bind_transport_attempt_ledger(TransportAttemptLedger(1)):
                with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
                    with pytest.raises(CandidateOutputError) as caught:
                        await owner.complete(req)
                    assert caught.value is forged
            row, = [row for row in state['rows'] if row['phase'] == 'response']
            assert 'private-invented-key' not in repr(row)
            detail = row['content']['application_failure']
            assert detail['candidate'] == {'visible_text': '{"answer":"xxx"}'}
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('rejection', ['schema', 'undeclared-tool', 'adapter-candidate', 'adapter-validated-schema', 'adapter-validated-tool'])
@pytest.mark.parametrize('credential_collision', [False, True])
def test_rejected_generation_retains_admitted_result_metadata_on_serialized_wire(tmp_path, monkeypatch, mode, credential_collision, rejection):
    import asyncio
    import json
    from dataclasses import replace
    from types import SimpleNamespace
    from test_demo_application import LLM, Owner, application, request
    from test_operation_traces import attrs, shared_model, spans_from
    from test_workflow_observations import collector, config
    from api_agent.api.application import Application
    from api_agent.providers.control import OperationContext
    from api_agent.telemetry.tracing import TraceRuntime
    from api_agent.workflows.execution import WorkflowError
    from api_agent.workflows.observations import WorkflowObservationFactory
    original = LLM.complete
    async def modelled(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if rejection in ('undeclared-tool', 'adapter-validated-tool'):
            from api_agent.providers.llm import ToolCall
            result = replace(result, payload=Message('assistant', (), calls=(
                ToolCall('actual-call', 'undeclared', '{}'),)), outcome='tool_calls')
        result = replace(result, actual_model='actual-model',
                         service_tier='standard', serving_revision='revision1')
        if rejection.startswith('adapter-validated'):
            from api_agent.llm_providers.response_policy import resolve_response
            from api_agent.providers.rich_inference import async_validate_result
            _, response = resolve_response(args[0], lambda _: None)
            result = replace(result, response=response)
            try:
                return await async_validate_result(args[0], result)
            except CandidateOutputError as error:
                # Provider code regains control only after authoritative facts were observed.
                error.feedback = {'reason': 'response_schema', 'errors': [
                    {'path': ['provider-invented-key'], 'keyword': 'maxLength', 'constraint': 1}]}
                raise
        if rejection == 'adapter-candidate':
            from api_agent.llm_providers.response_policy import resolve_response
            _, response = resolve_response(args[0], lambda _: None)
            result = replace(result, response=response)
            raise CandidateOutputError(result, {'reason': 'response_schema', 'errors': [
                {'path': ['unverified-private-key'], 'keyword': 'maxLength', 'constraint': 1}]})
        return result
    monkeypatch.setattr(LLM, 'complete', modelled)
    trace_content = rejection == 'adapter-validated-schema'
    with collector() as (endpoint, received):
        settings = config(False, trace_content, mode, TRACE_EXPORT_ENDPOINT=endpoint,
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(False), secrets=['revision1'] if credential_collision else [])
        runtime = TraceRuntime(settings, boundary=boundary, model_identities=('chosen-model', 'actual-model'))
        app = application(Owner())
        app.observations = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
        bad = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'x' * 8666})
        model = shared_model(tmp_path, boundary, runtime, [bad, bad])
        async def handler(scope, receive, send):
            await app.complete(request(), llm=model, context=OperationContext('request'))
        asgi = Application(SimpleNamespace(tracing=runtime, services={}), handler)
        async def scenario():
            await runtime.astart()
            try:
                if rejection == 'schema':
                    with pytest.raises(WorkflowError):
                        await asgi({'type': 'http'}, lambda: None, lambda _: asyncio.sleep(0))
                elif rejection in ('undeclared-tool', 'adapter-validated-tool'):
                    with pytest.raises(ProviderAccessError) as caught:
                        await asgi({'type': 'http'}, lambda: None, lambda _: asyncio.sleep(0))
                    assert caught.value.code == 'tool_name_undeclared'
                else:
                    with pytest.raises(WorkflowError):
                        await asgi({'type': 'http'}, lambda: None, lambda _: asyncio.sleep(0))
            finally:
                await model.aclose()
                await runtime.aclose()
        asyncio.run(scenario())
    rejected = [attrs(span) for span in spans_from(received)
                if attrs(span).get('gen_ai.operation.name') == {'stringValue': 'chat'} and span.status.code == 2]
    assert len(rejected) == (1 if rejection in ('undeclared-tool', 'adapter-validated-tool') else 2)
    for fields in rejected:
        assert fields['gen_ai.response.model'] == {'stringValue': 'actual-model'}
        assert fields['api_agent.response.service_tier'] == {'stringValue': 'standard'}
        assert ('api_agent.response.serving_revision' in fields) is not credential_collision
        if not credential_collision:
            assert fields['api_agent.response.serving_revision'] == {'stringValue': 'revision1'}
        assert ('langfuse.observation.output' in fields) is trace_content
        if trace_content:
            failure = json.loads(fields['langfuse.observation.output']['stringValue'])['llm_response']['application_failure']
            assert failure['candidate']['feedback']['errors'] == [
                {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 8192, 'actual': 8666}]
            assert 'provider-invented-key' not in repr(fields)
        assert 'gen_ai.output.messages' not in fields
    if credential_collision:
        assert 'revision1' not in repr(received)


def test_physical_candidate_failure_does_not_start_optional_validation_or_delay_original_error(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Usage
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    result = LLMResult(Message('assistant', (TextContent('rejected'),)), Usage(7, 3))
    original = CandidateOutputError(result, {'reason': 'response_json'})
    calls = []
    async def scenario():
        async def validate(*args, **kwargs):
            pytest.fail('optional capture must not perform schema validation')
        monkeypatch.setattr('api_agent.providers.rich_inference.async_validate_result', validate)
        async def call(callback):
            return await callback(auth='synthetic-auth')
        async def complete(request, *, auth):
            calls.append(request)
            raise original
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=2)
        ledger = TransportAttemptLedger(2)
        try:
            with bind_transport_attempt_ledger(ledger):
                task = asyncio.create_task(owner.complete(LLMRequest('selected', CompletionRequest((
                    Message('user', (TextContent('ask'),)),)))))
                with pytest.raises(CandidateOutputError) as caught:
                    await asyncio.wait_for(task, 2)
                assert caught.value is original
            row, = ledger.snapshot()['attempts']
            assert row['outcome'] == 'failed'
            assert row['usage']['input_tokens'] == 7 and row['usage']['output_tokens'] == 3
            assert len(calls) == 1
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_unavailable_post_result_validation_never_claims_confirmed_candidate_facts(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, StructuredOutput
    from api_agent.providers.shared_setup import SharedSetupOwner
    async def unavailable(*args, **kwargs):
        return None
    monkeypatch.setattr('api_agent.providers.rich_inference.async_schema_verdict', unavailable)
    async def call(callback):
        return await callback(auth='synthetic-auth')
    result = LLMResult(Message('assistant', (TextContent('{"answer":"xxx"}'),)))
    async def complete(request, *, auth):
        return result
    req = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=StructuredOutput('answer', json.dumps({'type': 'object',
            'properties': {'answer': {'type': 'string', 'maxLength': 2}},
            'required': ['answer'], 'additionalProperties': False}), False)))
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
                with pytest.raises(ProviderAccessError) as caught:
                    await owner.complete(req)
                assert type(caught.value) is ProviderAccessError
            row, = [row for row in state['rows'] if row['phase'] == 'response']
            assert 'candidate' not in row['content']['application_failure']
            assert row['content']['application_failure']['code'] == 'schema_validation'
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_adapter_candidate_mutated_result_cannot_supply_unadmitted_identity(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.shared_setup import SharedSetupOwner
    original = CandidateOutputError(LLMResult(Message('assistant', (TextContent('rejected'),))),
                                    {'reason': 'response_json'})
    original.result = 'unadmitted-mutated-result'
    fields = Mock()
    monkeypatch.setattr(capture, 'record_provider_fields', fields)
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(request, *, auth):
        raise original
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with pytest.raises(CandidateOutputError) as caught:
                await owner.complete(LLMRequest('selected', CompletionRequest((
                    Message('user', (TextContent('ask'),)),))))
            assert caught.value is original
            fields.assert_not_called()
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_confirmed_candidate_requires_immutable_text_and_never_rereads_mutable_carrier():
    from api_agent.telemetry.failure_content import failure_content
    error = CandidateOutputError(LLMResult(Message('assistant', (TextContent('original'),))),
                                 {'reason': 'response_json'})
    assert 'candidate' not in failure_content(error, candidate_feedback=error.feedback)
    error.result = object()
    assert failure_content(error, candidate_feedback=error.feedback, candidate_text='')['candidate'] == {
        'visible_text': '', 'feedback': {'reason': 'response_json', 'errors': []}}
