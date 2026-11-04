"""Provider metadata cannot turn opaque replay material into evidence or diagnostics."""
import copy
import json

import httpx
import pytest

from api_agent.llm_providers.gpt.inference import complete_text
from api_agent.llm_providers.gpt.router_reasoning import metadata_projection, safe_error_text
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import collect_inference

BASE = 'https://openrouter.ai/api/v1'
OPAQUE = dict(type='reasoning.encrypted', data='opaque-metadata-sentinel', signature='signature-metadata-sentinel')
SERIALIZED = json.dumps(dict(reasoning_details=[OPAQUE]))


def response(mode):
    if mode == 'responses':
        return dict(status='completed', output=[
            dict(type='reasoning', id='r', summary=[], encrypted_content='opaque-replay-sentinel'),
            dict(type='message', id='m', role='assistant', content=[dict(type='output_text', text='Ok')])],
            usage=dict(input_tokens=5, output_tokens=7))
    return dict(choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='Ok',
        reasoning_details=[dict(type='reasoning.encrypted', data='opaque-replay-sentinel', id='r', index=0)]))],
        usage=dict(prompt_tokens=5, completion_tokens=7))


def request():
    return LLMRequest('openai/gpt-5.6-luna', CompletionRequest((Message('user', (TextContent('Question'),)),)))


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('status', [200, 400])
@pytest.mark.parametrize('metadata', [dict(reasoning_details=[OPAQUE]), {'nested': [{'payload': OPAQUE}]},
    {'reasoning': OPAQUE}, {'reasoning_content': SERIALIZED}, {'raw': SERIALIZED},
    {'raw': 'provider detail prefix: ' + SERIALIZED}, {'raw': json.dumps(SERIALIZED)},
    {'encrypted_content': 'opaque-metadata-sentinel', 'signature': 'signature-metadata-sentinel'}])
@pytest.mark.asyncio
async def test_complete_text_withholds_opaque_fields_in_unknown_wrappers(mode, status, metadata):
    raw = response(mode)
    raw['metadata'] = copy.deepcopy(metadata)
    if status == 400:
        raw['error'] = dict(message='Permission denied for requested model.', metadata=copy.deepcopy(metadata))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=raw))) as client:
            if status == 200:
                result = await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                    subscription=False, api_mode=mode, request=request())
                usage = result.usage
                assert result.payload.content[0].text == 'Ok'
            else:
                with pytest.raises(ProviderAccessError) as caught:
                    await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                        subscription=False, api_mode=mode, request=request())
                usage = caught.value.usage
                assert caught.value.status == 400
                assert caught.value.message == 'Permission denied for requested model.'
    assert usage.input_tokens == 5 and usage.output_tokens == 7
    recorded = json.dumps(evidence['rows'])
    assert 'opaque-metadata-sentinel' not in recorded
    assert 'signature-metadata-sentinel' not in recorded
    assert 'opaque-replay-sentinel' not in recorded
    assert any(row['phase'] == 'response' for row in evidence['rows'])


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('message', [SERIALIZED, 'upstream diagnostic: ' + SERIALIZED,
                                  json.dumps(SERIALIZED), 'upstream diagnostic: ' + json.dumps(SERIALIZED),
    r'upstream: {"\u0073ignature":"signature-metadata-sentinel"}',
    r'upstream: {"\u0072easoning\u005fdetails":[{"data":"opaque-metadata-sentinel"}]}'])
@pytest.mark.asyncio
async def test_serialized_native_error_message_is_withheld_before_evidence_and_diagnostics(mode, message):
    raw = response(mode)
    raw['error'] = dict(message=message)
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400, json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                    subscription=False, api_mode=mode, request=request())
    assert caught.value.message == 'GPT inference was rejected.'
    assert caught.value.status == 400
    assert caught.value.usage.input_tokens == 5 and caught.value.usage.output_tokens == 7
    released = json.dumps(evidence['rows']) + str(caught.value) + repr(vars(caught.value))
    assert 'opaque-metadata-sentinel' not in released and 'signature-metadata-sentinel' not in released


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.asyncio
async def test_actual_assistant_json_plaintext_and_private_replay_are_unchanged(mode):
    answer = json.dumps(dict(reasoning_details=[dict(type='reasoning.encrypted', data='assistant-plaintext')]))
    raw = response(mode)
    if mode == 'responses':
        raw['output'][1]['content'][0]['text'] = answer
        replay = raw['output']
    else:
        raw['choices'][0]['message']['content'] = answer
        replay = [raw['choices'][0]['message']]
    before = copy.deepcopy(raw)
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=raw))) as client:
            result = await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                subscription=False, api_mode=mode, request=request())
    assert result.payload.content[0].text == answer
    assert json.loads(result.payload.continuation)['state'] == replay
    assert raw == before
    assert 'assistant-plaintext' in json.dumps(evidence['rows'])
    assert 'opaque-replay-sentinel' not in json.dumps(evidence['rows'])


@pytest.mark.parametrize('message', ['Permission denied.', 'Model example is unavailable.',
    'Invalid parameter max_tokens: expected integer.', 'Invalid signature.', 'Unsupported parameter reasoning_details.'])
def test_ordinary_diagnostic_remains_actionable(message):
    assert safe_error_text(message) == message


@pytest.mark.parametrize('value', [None, 7, 'x'*65537])
def test_nontext_or_oversized_diagnostic_is_withheld(value):
    assert safe_error_text(value) is None


def test_unknown_metadata_bounds_fail_closed_and_ordinary_structure_survives():
    ordinary = {'usage': {'input_tokens': 5}, 'metadata': [{'label': 'safe'}, None, True, 7]}
    assert metadata_projection(ordinary) == ordinary
    deep = {}
    for _ in range(34):
        deep = {'nested': deep}
    assert metadata_projection(deep) == {}
    assert metadata_projection([None]*100001) == {}
    assert metadata_projection(['x'*65536]*17) == {}


@pytest.mark.parametrize('identities,indices', [(['duplicate', 'duplicate'], [0, 1]),
    (['first', 'second'], [0, 0]), (['first', 'second'], [0, 2]),
    (['first', 'second'], [1, 0]), (['first', 'second'], [0, None])])
@pytest.mark.asyncio
async def test_detail_identity_or_original_index_conflict_rejects_native_replay_with_usage(identities, indices):
    raw = response('completions')
    raw['choices'][0]['message']['reasoning_details'] = [
        dict(type='reasoning.encrypted', data='opaque-replay-sentinel', id=identifier, index=index)
        for identifier, index in zip(identities, indices)]
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                    subscription=False, api_mode='completions', request=request())
    assert caught.value.usage.input_tokens == 5 and caught.value.usage.output_tokens == 7
    assert 'opaque-replay-sentinel' not in json.dumps(evidence['rows'])
    assert 'opaque-replay-sentinel' not in str(caught.value)


@pytest.mark.parametrize('details', [
    [dict(type='reasoning.encrypted', data='first', id=None), dict(type='reasoning.encrypted', data='second', id=None)],
    [dict(type='reasoning.encrypted', data='first', id='first', index=0),
     dict(type='reasoning.encrypted', data='second', id='second', index=1)]])
@pytest.mark.asyncio
async def test_optional_detail_identity_and_valid_sequential_indices_preserve_exact_replay(details):
    raw = response('completions')
    raw['choices'][0]['message']['reasoning_details'] = details
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=raw))) as client:
        result = await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
            subscription=False, api_mode='completions', request=request())
    assert json.loads(result.payload.continuation)['state'] == [raw['choices'][0]['message']]


@pytest.mark.parametrize('message', [r'{"\u0073ignature":"opaque"}',
    r'prefix {"\u005cu005cu005cu005cu005cu0073ignature":"opaque"}',
    r'{\\\"signature\\\":\\\"opaque\\\"}'])
def test_encoded_native_marker_is_withheld_only_in_metadata_diagnostics(message):
    assert safe_error_text(message) is None


def test_ordinary_unicode_escape_diagnostic_preserves_original_spelling():
    message = r'Invalid model \u0061lias.'
    assert safe_error_text(message) == message


def test_unrecognized_wrapper_native_fields_are_withheld_without_a_native_type_tag():
    raw = {'metadata': {'signature': 'opaque-signature', 'encrypted_content': 'opaque-encrypted',
                        'reasoning_details': [{'unexpected': 'opaque-detail'}]}}
    assert metadata_projection(raw) == {'metadata': {}}


@pytest.mark.parametrize('field,item', [('input', dict(type='function_call_output', call_id='call', output=SERIALIZED)),
    ('messages', dict(role='tool', tool_call_id='call', content=SERIALIZED))])
def test_admitted_tool_result_text_is_not_reinterpreted_as_error_metadata(field, item):
    from api_agent.llm_providers.gpt.router_reasoning import evidence_payload
    assert evidence_payload({field: [item]}) == {field: [item]}


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('contract', ['tools', 'structured'])
@pytest.mark.asyncio
async def test_validated_outgoing_capture_equals_effective_wire_controls_and_authored_schema(mode, contract):
    from api_agent.providers.llm import GenerationOptions, StructuredOutput, Tool
    properties = {name: {'type': 'string'} for name in ('reasoning', 'signature', 'safe')}
    schema = json.dumps(dict(type='object', properties=properties, required=list(properties), additionalProperties=False))
    plaintext = json.dumps({'reasoning': 'ordinary authored text', 'signature': 'ordinary signature field'})
    payload = CompletionRequest((Message('user', (TextContent(plaintext),)),),
        generation=GenerationOptions(reasoning_effort='high', max_output_tokens=128),
        tools=(Tool('lookup', 'Lookup', schema),) if contract == 'tools' else (),
        structured=StructuredOutput('answer', schema) if contract == 'structured' else None)
    outgoing = LLMRequest('openai/gpt-5.6-luna', payload)
    raw = response(mode)
    answer = json.dumps({'reasoning': 'answer reason', 'signature': 'answer signature', 'safe': 'yes'})
    if mode == 'responses':
        raw['output'][1]['content'][0]['text'] = answer
    else:
        raw['choices'][0]['message']['content'] = answer
    sent = []
    def respond(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=raw)
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await complete_text(client=client, base_url=BASE, access_token='synthetic-credential',
                subscription=False, api_mode=mode, request=outgoing)
    captured, = [row for row in evidence['rows'] if row['phase'] == 'request']
    assert captured['content']['provider_payload'] == sent[0]
    assert sent[0]['reasoning']['effort'] == 'high'
    assert captured['diagnostics']['reasoning_effort'] == 'high'
    assert json.dumps(properties, sort_keys=True) in json.dumps(sent[0], sort_keys=True)
    assert plaintext in json.dumps(sent[0], ensure_ascii=False).replace('\\"', '"')
    assert result.payload.content[0].text == answer
    assert 'opaque-replay-sentinel' not in json.dumps(evidence['rows'])


def test_incoming_request_shaped_metadata_cannot_acquire_outgoing_source_authority():
    from api_agent.llm_providers.gpt.router_reasoning import evidence_payload
    incoming = {'reasoning': {'effort': 'high'}, 'tools': [{'parameters': {'properties': {
        'reasoning': {'signature': 'opaque-wrapper'}}}}], 'error': {'message': SERIALIZED}}
    projected = evidence_payload(incoming)
    assert 'reasoning' not in projected
    assert 'opaque-wrapper' not in json.dumps(projected)
    assert 'opaque-metadata-sentinel' not in json.dumps(projected)


def test_native_evidence_source_must_be_explicit_known_direction():
    from api_agent.llm_providers.gpt.router_reasoning import evidence_payload
    from api_agent.providers.contracts import ContractError
    with pytest.raises(ContractError, match='source'):
        evidence_payload({}, source='provider-supplied-request')
