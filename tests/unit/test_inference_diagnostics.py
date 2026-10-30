"""Content permission never substitutes reasoning for an answer or exposes state."""
import asyncio
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api_agent.telemetry.inference_diagnostics import (diagnostic_attachment, has_optional_content,
    omission_notice, scrub_diagnostics, sdk_observation, valid_diagnostics)
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, OwnedLogFilter
from api_agent.workflows.inference_capture import (bind_boundary, bind_inference_call, capture_inference,
    capture_transport_diagnostics, collect_inference, select_capture_rows)


@pytest.fixture
def sdk_response():
    return {'id': 'resp-synthetic', 'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'A short summary.'}],
         'content': [{'type': 'reasoning_text', 'text': 'Check each step.'}], 'encrypted_content': 'never-export'},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Final answer.'}]}]}


@pytest.mark.parametrize('allow', [False, True])
@pytest.mark.parametrize('tag', [{'type': 'reasoning'}, {'type': 'thinking'}, {'type': 'analysis'},
                                {'channel': 'analysis'}, {'channel': 'reasoning'}])
@pytest.mark.parametrize('carrier', ['native', 'serialized', 'embedded', 'parts'])
def test_provider_plaintext_reasoning_is_opt_in_and_scrubbed(allow, tag, carrier):
    reasoning = dict(tag, text='Provider reasoning.')
    encoded = json.dumps(reasoning)
    value = {'native': reasoning, 'serialized': encoded, 'embedded': 'prefix ' + encoded,
             'parts': [encoded[:30], encoded[30:]]}[carrier]
    boundary = EmissionBoundary(ContentPolicy(allow), secrets=('selected-secret',))

    result = json.dumps(boundary.export({'response': value, 'reasoning_tokens': 7}))

    assert ('Provider reasoning.' in result) is allow
    assert 'selected-secret' not in result and 'never-export' not in result
    assert 'reasoning_tokens' in result


@pytest.mark.parametrize('serialized', [False, True])
def test_reasoning_secrets_and_encrypted_state_remain_excluded(serialized):
    data = {'type': 'reasoning', 'text': 'Provider reasoning with selected-secret.',
            'encrypted_content': 'never-export', 'internal_analysis': 'never-export'}
    value = json.dumps(data) if serialized else data
    result = json.dumps(EmissionBoundary(ContentPolicy(True), secrets=('selected-secret',)).export({'response': value}))
    assert 'selected-secret' not in result and 'never-export' not in result
    assert 'Provider reasoning' in result


@pytest.mark.parametrize('key', ['reasoning_content', 'thinking_content', 'chain_of_thought'])
def test_plaintext_fields_and_request_controls_remain_separate_from_internal_state(key):
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({key: 'provider text', 'reasoning': {'effort': 'low'},
                            'internal_reasoning': 'never-export'}) == {
        key: 'provider text', 'reasoning': {'effort': 'low'}}


@pytest.mark.parametrize('value', [None, [], {}, {'version': True}, {'version': 2},
    {'version': 1, 'extra': 'body'}, {'version': 1, 'http_status': True},
    {'version': 1, 'http_status': 600}, {'version': 1, 'request_id': 'x\nsecret'},
    {'version': 1, 'request_id': 'x' * 129}, {'version': 1, 'response_id': []},
    {'version': 1, 'output_items': -1}, {'version': 1, 'output_items': True},
    {'version': 1, 'reasoning_text_characters': None},
    {'version': 1, 'validation_rule': 'provider-supplied-text'},
    {'version': 1, 'validation_path': '$.provider-supplied-text'},
    {'version': 1, 'error_present': 'false'}, {'version': 1, 'response_status': []},
    {'version': 1, 'response_status': 'secret'}, {'version': 1, 'finish_reasons': ['secret']},
    {'version': 1, 'finish_reasons': ['stop'] * 9}, {'version': 1, 'finish_reasons': 'stop'},
    {'version': 1, 'sdk_versions': {'unknown': '1'}}, {'version': 1, 'sdk_versions': {'openai': 'private text'}},
    {'version': 1, 'sdk_versions': []}, dict(version=1, **{str(i): 1 for i in range(33)})])
def test_metadata_rejects_unbounded_or_unrecognized_values(value):
    assert not valid_diagnostics(value)
    assert scrub_diagnostics(value, EmissionBoundary()) == {}


def test_safe_shape_is_measured_before_reasoning_is_filtered(sdk_response):
    data = sdk_observation({'provider_response': sdk_response}, 'response')
    assert valid_diagnostics(data)
    assert data == dict(version=1, observation='sdk_decoded', response_id='resp-synthetic', api='responses',
        response_status='completed', output_items=2, message_items=1, reasoning_items=1,
        tool_items=0, other_items=0, visible_text_characters=13, reasoning_text_characters=32,
        encrypted_reasoning_present=True, error_present=False)
    assert 'short summary' not in json.dumps(data)


def test_validation_diagnostics_are_fixed_and_still_credential_scrubbed():
    details = {'version':1, 'response_validation':'rejected', 'validation_rule':'allowed_fields',
               'validation_path':'$.choices[].message'}
    assert valid_diagnostics(details)
    assert scrub_diagnostics(details, EmissionBoundary()) == details
    assert scrub_diagnostics(details, EmissionBoundary(secrets=('allowed_fields',))) == {}


def test_raw_rejection_marker_survives_absent_diagnostics_until_final_projection():
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as capture:
        capture_inference('output', {'provider_response':{'content':'safe rejected answer'}},
                          'call', rejected=True)
    row, = select_capture_rows(capture['rows'])
    assert row['_rejected'] is True and row['reason'] == 'captured'


@pytest.mark.parametrize('change', [dict(_rejected=False), dict(_rejected=1),
                                  dict(phase='request', _rejected=True)])
def test_internal_rejection_marker_is_response_only_and_strictly_boolean(change):
    rows = select_capture_rows([dict(phase='response', content={}, reason='captured') | change])
    assert all(row['reason'] == 'truncated' and row['content'] == {} for row in rows)


@pytest.mark.parametrize('wire', [None, {}, {'provider_response': []}, {'provider_payload': None}])
def test_missing_sdk_payload_is_unknown_not_empty(wire):
    assert sdk_observation(wire, 'response') == {}


@pytest.mark.parametrize('body,expected', [
    ({'reasoning': {'effort': 'low'}, 'max_output_tokens': 16000}, {'reasoning_effort': 'low', 'max_output_tokens': 16000}),
    ({'reasoning_effort': 'high', 'max_tokens': 42}, {'reasoning_effort': 'high', 'max_output_tokens': 42}),
    ({'reasoning': [], 'max_tokens': -1}, {}),
    ({'reasoning_effort': {}, 'max_tokens': True}, {}),
])
def test_effective_request_controls_without_prompts(body, expected):
    body = dict(body, input='do not export this prompt')
    assert sdk_observation({'provider_payload': body}, 'request') == dict(version=1, observation='sdk_decoded', **expected)


@pytest.mark.parametrize('reason', ['max_output_tokens', 'unrecognized', []])
def test_response_categories_are_fixed_not_arbitrary_text(reason):
    raw = {'status': 'incomplete', 'incomplete_details': {'reason': reason}, 'output': [
        None, {'type': []}, {'type': 'function_call'}, {'type': 'message', 'channel': 'analysis', 'content': 'thinking'},
        {'type': 'reasoning', 'content': [None, {'type': 'reasoning_text', 'text': 'more'}]}], 'error': {}}
    data = sdk_observation({'provider_response': raw}, 'response')
    assert data['incomplete_reason'] == ('max_output_tokens' if reason == 'max_output_tokens' else 'other')
    assert data['other_items'] == 2 and data['tool_items'] == 1
    assert data['reasoning_text_characters'] == 12 and data['visible_text_characters'] == 0
    assert data['error_present'] is True


def test_completions_counts_reasoning_separately_and_caps_finish_categories():
    choices = [{'finish_reason': 'stop', 'message': {'content': 'Answer', 'reasoning_content': 'Steps',
                'reasoning': 'More', 'thinking_content': 'Check', 'chain_of_thought': 'Plan'}},
               None, {'finish_reason': 'unknown', 'message': []}, {'message': {}}]
    data = sdk_observation({'provider_response': {'choices': choices}}, 'response')
    assert data['finish_reasons'] == ['stop', 'other']
    assert data['visible_text_characters'] == 6 and data['reasoning_text_characters'] == 18
    assert valid_diagnostics(data)


def test_counts_have_a_depth_bound_and_ignore_unknown_fields():
    part = {'type': 'output_text', 'text': 'too deep'}
    for _ in range(10):
        part = {'content': [part]}
    data = sdk_observation({'provider_response': {'output': [part], 'status': [], 'id': []}}, 'response')
    assert data['visible_text_characters'] == 0
    assert 'response_id' not in data and 'response_status' not in data


def test_identifiers_remain_credential_scrubbed_without_discarding_safe_counts():
    data = {'version': 1, 'request_id': 'selected-secret', 'output_items': 2, 'http_status': 200}
    assert scrub_diagnostics(data, EmissionBoundary(secrets=('selected-secret',))) == {
        'version': 1, 'output_items': 2, 'http_status': 200}
    assert scrub_diagnostics(data, EmissionBoundary(secrets=('1',))) == {}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('reason', ['captured', 'withheld', 'partial', 'unavailable', 'truncated'])
def test_attachment_has_actionable_and_truthful_omission_notices(enabled, reason):
    call, attempt = str(uuid4()), str(uuid4())
    row = dict(call_id=call, transport_attempt_id=attempt, phase='response', content={}, reason=reason,
               diagnostics={'version': 1, 'reasoning_text_characters': 0})
    title, body, mime = diagnostic_attachment(row, EmissionBoundary(), enabled=enabled)
    data = json.loads(body)
    assert call in title and mime == 'application/json'
    assert data['call_id'] == call and data['transport_attempt_id'] == attempt
    assert data['capture_reason'] == reason
    assert 'reasoning_text' not in data
    assert data['reasoning_text_characters'] == 0
    assert ('LOG_CONTENT=True' in data.get('content_notice', '')) is (not enabled and reason != 'unavailable')
    if enabled and reason == 'withheld':
        assert 'credential authority' in data['content_notice']
    elif enabled:
        assert 'content_notice' not in data
    assert ('capture_notice' in data) is (reason in ('partial', 'unavailable', 'truncated'))


def test_content_guidance_only_names_real_content_flags():
    assert 'LOG_CONTENT=True' in omission_notice('LOG_CONTENT')
    assert not has_optional_content(None)
    assert not has_optional_content({'http_status': 200})
    assert has_optional_content({'reasoning_content': 'steps'})
    with pytest.raises(ValueError):
        omission_notice('DEBUG')


@pytest.mark.parametrize('diagnostics,characters', [({}, None), ({'version': 1}, None),
    ({'version': 1, 'reasoning_text_characters': 0}, 0),
    ({'version': 1, 'reasoning_text_characters': 1547}, 1547)])
def test_response_reasoning_uses_one_consistent_nullable_count(diagnostics, characters):
    row = dict(call_id=str(uuid4()), phase='response', content={}, diagnostics=diagnostics)
    _, body, _ = diagnostic_attachment(row, EmissionBoundary(), enabled=True)
    data = json.loads(body)
    assert 'reasoning_text' not in data
    assert data['reasoning_text_characters'] == characters
    assert 'content_notice' not in data
    assert 'capture_notice' not in data


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('response_fields', [{}, {'reasoning_text_characters':1547}])
def test_request_diagnostics_do_not_invent_response_reasoning(enabled, response_fields):
    diagnostics = {'version': 1, 'reasoning_effort': 'low', 'max_output_tokens': 1024, **response_fields}
    row = dict(call_id=str(uuid4()), phase='request', content={}, diagnostics=diagnostics)
    _, body, _ = diagnostic_attachment(row, EmissionBoundary(), enabled=enabled)
    data = json.loads(body)
    assert data['reasoning_effort'] == 'low' and data['max_output_tokens'] == 1024
    assert 'reasoning_text' not in data and 'reasoning_text_characters' not in data
    assert ('content_notice' in data) is (not enabled)


@pytest.mark.parametrize('phase,field', [('request','provider_payload'), ('response','provider_response')])
def test_content_attachments_never_substitute_application_notices_for_provider_fields(phase, field):
    from api_agent.testing.inference_content import content_attachment
    content = {field:{'content_notice':'provider-owned notice', 'reasoning_text':'provider-owned text',
                      'content':'provider-owned content'}}
    event = SimpleNamespace(text=json.dumps(dict(call_id=str(uuid4()), phase=phase, content=content)))
    _, body, _ = content_attachment(event, EmissionBoundary(ContentPolicy(True)))
    assert json.loads(body) == content


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_log_and_trace_flags_are_independent_and_name_their_own_flag(log, trace):
    from api_agent.telemetry.tracing import _attributes
    payload = {'reasoning_content': 'Provider reasoning with selected-secret'}
    authority = EmissionBoundary(secrets=('selected-secret',))
    record = logging.LogRecord('owned', 20, '', 0, payload, (), None)
    OwnedLogFilter(authority.for_policy(ContentPolicy(log))).filter(record)
    logged = json.loads(record.msg)
    traced = _attributes(authority.for_policy(ContentPolicy(trace)), payload)
    assert ('Provider reasoning' in json.dumps(logged)) is log
    assert ('Provider reasoning' in json.dumps(traced)) is trace
    assert 'selected-secret' not in json.dumps([logged, traced])
    assert ('LOG_CONTENT=True' in json.dumps(logged)) is (not log)
    assert ('TRACELOOP_TRACE_CONTENT=True' in json.dumps(traced)) is (not trace)


@pytest.mark.parametrize('enabled', [False, True])
def test_shared_setup_ipc_retains_sdk_diagnostics_when_content_is_off(enabled, sdk_response):
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from test_setup_call_state import CAP, REQUEST

    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('secret-request-id',))
        async def complete(request):
            bind_boundary(boundary)
            capture_transport_diagnostics({'version': 1, 'http_status': 200, 'request_id': 'secret-request-id'})
            capture_inference('output', {'provider_response': sdk_response}, request.request_id,
                              decoded_visible_text_characters=13)
            return LLMResult(Message('assistant', (TextContent('Final answer.'),)))
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: SimpleNamespace(
            ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock()))
        server = SetupServer(registry, CAP)
        client = SetupClient(await server.start(), timeout=3)
        try:
            result = await asyncio.to_thread(client.complete, REQUEST)
            assert result.payload.content[0].text == 'Final answer.'
            row = client.evidence[0]
            assert row['diagnostics']['reasoning_items'] == 1
            assert row['diagnostics']['reasoning_text_characters'] == 32
            assert row['diagnostics']['decoded_visible_text_characters'] == 13
            assert row['diagnostics']['http_status'] == 200
            assert 'request_id' not in row['diagnostics']
            assert ('A short summary.' in json.dumps(row)) is enabled
            assert 'never-export' not in json.dumps(row)
            assert row['reason'] == ('captured' if enabled else 'withheld')
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('large', [False, True])
def test_diagnostics_survive_bounded_evidence_framing(large):
    from api_agent.providers.setup_transport import _evidence_frames, _EvidenceReceiver, MAX_EVIDENCE_BYTES, _evidence_omission
    data = {'version': 1, 'output_items': 2}
    row = dict(phase='response', content={'text': 'x' * (150000 if large else 1)}, reason='captured', diagnostics=data)
    receiver = _EvidenceReceiver()
    received = None
    for frame in _evidence_frames(row, MAX_EVIDENCE_BYTES):
        value = json.loads(frame)
        received = receiver.accept(value) if value['status'] != 'evidence' else {k: v for k, v in value.items() if k not in ('status', 'version')}
    assert received == row
    assert _evidence_omission(row, 'truncated')['diagnostics'] == data


def test_capture_metadata_is_attempt_scoped_and_never_grants_content_permission(sdk_response):
    from api_agent.providers.generation_attempts import (begin_transport_attempt, finish_transport_attempt,
                                                       TransportAttemptLedger, bind_transport_attempt_ledger)
    from api_agent.providers.control import OperationalError
    from api_agent.providers.llm import LLMResult, Message, TextContent
    with bind_transport_attempt_ledger(TransportAttemptLedger(5)), collect_inference(EmissionBoundary()) as collection:
        with bind_inference_call(str(uuid4())):
            token = begin_transport_attempt(1)
            capture_transport_diagnostics({'version': 1, 'http_status': 200})
            capture_inference('output', {'provider_response': sdk_response}, 'call')
            finish_transport_attempt(token, error=OperationalError('network', 'possibly_executed'), retry_delay_seconds=.2)
            token = begin_transport_attempt(2)
            capture_inference('output', {'provider_response': sdk_response}, 'call')
            finish_transport_attempt(token, result=LLMResult(Message('assistant', (TextContent('answer'),))))
    rows = select_capture_rows(collection['rows'])
    assert len(rows) == 2 and rows[0]['transport_attempt_id'] != rows[1]['transport_attempt_id']
    assert rows[0]['diagnostics']['http_status'] == 200
    assert 'http_status' not in rows[1]['diagnostics']
    assert all(row['content'] == {} for row in rows)


def test_invalid_metadata_cannot_smuggle_content_through_capture():
    assert select_capture_rows([dict(phase='response', content={}, diagnostics={'version': 1, 'text': 'secret'})]) == [
        dict(phase=phase, content={}, reason='truncated') for phase in ('request', 'response')]
    capture_transport_diagnostics({'version': 1})  # No active call: inert.
    with bind_inference_call(str(uuid4())):
        capture_transport_diagnostics({'version': 1, 'text': 'secret'})


@pytest.mark.parametrize('reason', ['truncated', 'unavailable', 'partial'])
@pytest.mark.parametrize('observation', [None, 0, 30])
def test_lost_capture_never_claims_reasoning_was_retained_or_absent(reason, observation):
    diagnostics = {'version': 1}
    if observation is not None:
        diagnostics['reasoning_text_characters'] = observation
    _, body, _ = diagnostic_attachment(dict(call_id=str(uuid4()), phase='response',
        content={}, reason=reason, diagnostics=diagnostics), EmissionBoundary(), enabled=True)
    row = json.loads(body)
    assert 'capture_notice' in row
    assert 'is captured' not in row.get('content_notice', '')
    assert row['reasoning_text_characters'] == observation
    assert 'reasoning_text' not in row
    assert 'content_notice' not in row


@pytest.mark.parametrize('enabled', [False, True])
def test_allure_retains_diagnostics_and_gates_reasoning_content(tmp_path, enabled, sdk_response):
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.recorded import run_recorded_integration
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    call, attempt = str(uuid4()), str(uuid4())
    config['LOG_CONTENT'] = enabled
    diagnostics = sdk_observation({'provider_response': sdk_response}, 'response')
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(dict(
            call_id=call, transport_attempt_id=attempt, phase='response', capture_source='provider',
            reason='captured' if enabled else 'withheld', diagnostics=diagnostics,
            content={'provider_response': sdk_response} if enabled else {}))))
        outcome = TestResult(test, 'failed', .1)
        hooks['on_result'](outcome)
        result = IntegrationRunResult(1, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, (test,), config, accepted=True, storage=Storage(), runner=runner)
    outcome = json.loads(next(result.directory.glob('*-result.json')).read_text())
    attachments = outcome['attachments']
    diagnostic = next(row for row in attachments if 'LLM response diagnostics' in row['name'])
    data = json.loads((result.directory / diagnostic['source']).read_text())
    assert data['call_id'] == call and data['transport_attempt_id'] == attempt
    assert data['output_items'] == 2 and data['reasoning_items'] == 1
    bodies = '\n'.join(path.read_text() for path in result.directory.iterdir() if path.is_file())
    assert ('A short summary.' in bodies) is enabled
    assert 'never-export' not in bodies
    assert ('LOG_CONTENT=True' in data.get('content_notice', '')) is (not enabled)


def test_allure_response_loss_is_visible_even_when_metadata_was_scrubbed(tmp_path):
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.recorded import run_recorded_integration
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    call = str(uuid4())
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(dict(
            call_id=call, phase='response', content={}, reason='unavailable'))))
        outcome = TestResult(test, 'failed', .1)
        hooks['on_result'](outcome)
        result = IntegrationRunResult(1, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, (test,), config, accepted=True, storage=Storage(), runner=runner)
    report = json.loads(next(result.directory.glob('*-result.json')).read_text())
    def attachments(node):
        yield from node.get('attachments', [])
        for step in node.get('steps', []):
            yield from attachments(step)
    retained = list(attachments(report))
    attachment = next(row for row in retained if 'LLM response diagnostics' in row['name'])
    data = json.loads((result.directory / attachment['source']).read_text())
    assert data['call_id'] == call and data['capture_reason'] == 'unavailable'
    assert data['reasoning_text_characters'] is None
    assert 'capture_notice' in data and 'content_notice' not in data
    assert not any('LLM response (' in row['name'] for row in retained)


@pytest.mark.parametrize('enabled', [False, True])
def test_unavailable_rejected_response_never_promises_policy_only_recovery(enabled):
    row = dict(call_id=str(uuid4()), phase='response', content={}, reason='unavailable',
        diagnostics={'version':1, 'response_validation':'rejected', 'http_status':200,
                     'reasoning_text_characters':1547})
    _, body, _ = diagnostic_attachment(row, EmissionBoundary(), enabled=enabled)
    data = json.loads(body)
    assert data['reasoning_text_characters'] == 1547 and data['response_validation'] == 'rejected'
    assert 'content_notice' not in data
    assert 'cannot recover' in data['capture_notice']
    assert 'Rerun with' not in body.decode()


@pytest.mark.parametrize('unavailable', [False, True])
def test_unavailable_capture_never_retains_response_text_in_either_lane(unavailable):
    from api_agent.workflows.inference_capture import bind_inference_trace
    boundary = EmissionBoundary(ContentPolicy(True))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=boundary))
    with collect_inference(boundary, retain_raw=True) as captured:
        bind_inference_trace(owner)
        capture_inference('output', {'provider_response':{'choices':[{'message':{'content':'response-canary'}}]}},
                          'call', unavailable=unavailable)
    for state in (captured, captured['trace_collector']):
        row, = state['rows']
        assert ('response-canary' in json.dumps(row)) is (not unavailable)
        assert row['reason'] == ('unavailable' if unavailable else 'captured')


def test_rejected_scrubbed_diagnostics_are_not_added_to_capture(monkeypatch, sdk_response):
    import api_agent.telemetry.inference_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, 'scrub_diagnostics', lambda value, boundary: {})
    with collect_inference(EmissionBoundary()) as collection:
        with bind_inference_call(str(uuid4())):
            capture_inference('output', {'provider_response': sdk_response}, 'call')
    rows = select_capture_rows(collection['rows'])
    assert len(rows) == 1
    assert 'diagnostics' not in rows[0]
    assert rows[0]['content'] == {}
