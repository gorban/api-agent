"""Documented native Completions state stays separate from displayed text."""
from dataclasses import replace
import copy
import json
import httpx
import pytest
from api_agent.llm_providers.gpt.inference import complete_text, _result, validate_text_request
from api_agent.llm_providers.gpt.reasoning import native_reasoning
from api_agent.llm_providers.gpt.router_reasoning import chat_state, evidence_payload, carriers
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Tool, ToolResult
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import collect_inference

BASE = 'https://openrouter.ai/api/v1'
MODEL = 'openai/gpt-5.6-luna'


def native(*, tools=False, plaintext=True):
    message = dict(role='assistant', content=None if tools else 'Ok', reasoning_details=[
        dict(type='reasoning.encrypted', data='opaque-state', id=None, format='openai-responses-v1', index=0)])
    if plaintext:
        message['reasoning_details'].append(dict(type='reasoning.text', text='  Checked.  ', signature='opaque-signature', index=1))
    if tools:
        message['tool_calls'] = [dict(id='call-1', type='function', function=dict(name='lookup', arguments='{}'))]
    return dict(choices=[dict(finish_reason='tool_calls' if tools else 'stop', message=message)],
                usage=dict(prompt_tokens=5, completion_tokens=10, completion_tokens_details=dict(reasoning_tokens=7)))


@pytest.mark.parametrize('tools', [False, True])
@pytest.mark.parametrize('plaintext', [False, True])
@pytest.mark.asyncio
async def test_chat_native_transport_replays_exact_assistant_before_tool_result(tools, plaintext):
    raw = native(tools=tools, plaintext=plaintext)
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=raw)
    definitions = (Tool('lookup', 'Lookup', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),) if tools else ()
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),), tools=definitions))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await complete_text(client=client, base_url=BASE, access_token='credential-sentinel', subscription=False, api_mode='completions', request=request)
            messages = (*request.payload.messages, first.payload)
            if tools:
                messages += (Message('tool', results=(ToolResult('call-1', (TextContent('Found'),)),)),)
            messages += (Message('user', (TextContent('Continue'),)),)
            following = replace(request, payload=replace(request.payload, messages=messages))
            await complete_text(client=client, base_url=BASE, access_token='credential-sentinel', subscription=False, api_mode='completions', request=following)
    assert json.loads(first.payload.continuation)['state'] == [raw['choices'][0]['message']]
    assert sent[1]['messages'][1] == raw['choices'][0]['message']
    if tools:
        assert sent[1]['messages'][2] == dict(role='tool', tool_call_id='call-1', content='Found')
    assert first.reasoning_summary == ('  Checked.  ' if plaintext else None)
    assert first.usage.reasoning_tokens == 7
    assert 'opaque-state' not in json.dumps(evidence['rows'])
    assert 'opaque-signature' not in json.dumps(evidence['rows'])


@pytest.mark.parametrize('model', ['openai/gpt-5.6-luna', 'openai/gpt-6-terra', 'openai/gpt-6.1-sol', 'openai/gpt-6-astra', 'openai/gpt-7-luna', 'openai/gpt-oss-120b'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_common_native_schema_is_independent_of_effort_version_qualification(model, mode):
    assert native_reasoning(model, base_url=BASE, api_mode=mode)


@pytest.mark.parametrize('model', [None, [], 'gpt-6-luna', 'openai/gpt-06-luna', 'openai/gpt-6-luna:free', 'openai/gpt-6-other', 'other/gpt-6-luna'])
def test_noncanonical_native_routes_remain_unqualified(model):
    assert not native_reasoning(model, base_url=BASE, api_mode='completions')


@pytest.mark.parametrize('change', [
    {'type':'other'}, {'data':42}, {'data':''}, {'data':'x'*32769}, {'data':'bad\nstate'},
    {'signature':'unallowed'}, {'extra':1}, {'id':3}, {'format':[]}, {'index':True}, {'index':-1}, {'index':4096},
])
def test_invalid_encrypted_detail_is_rejected(change):
    message = native(plaintext=False)['choices'][0]['message']
    message['reasoning_details'][0].update(change)
    with pytest.raises(ContractError):
        chat_state(message)


@pytest.mark.parametrize('change', [
    {'reasoning':3}, {'reasoning_details':{}}, {'reasoning_details':[None]},
    {'reasoning_details':[dict(type='reasoning.text',text='\ud800')]},
    {'reasoning_details':[dict(type='reasoning.text',text='x'*65537)]},
    {'refusal':'refused'}, {'extra':1}, {'content':3}, {'role':'user'},
])
def test_invalid_native_assistant_is_rejected(change):
    message = native()['choices'][0]['message']
    message.update(change)
    with pytest.raises(ContractError):
        chat_state(message)


def test_summary_precedence_and_plaintext_alias_preserve_original():
    message = native()['choices'][0]['message']
    message.update(reasoning='Alias.', reasoning_content='Other.')
    message['reasoning_details'].append(dict(type='reasoning.summary', summary='Summary.'))
    original = copy.deepcopy(message)
    assert chat_state(message) == 'Summary.'
    assert message == original
    assert 'Alias.' in carriers(dict(choices=[dict(message=message)]))
    assert 'opaque-state' not in json.dumps(evidence_payload(dict(choices=[dict(message=message)])))


def test_modified_assistant_cannot_replay_native_state():
    answer = _result(native(), False, 'credential-sentinel', openrouter=True)
    altered = replace(answer.payload, content=(TextContent('Changed'),))
    request = LLMRequest(MODEL, CompletionRequest((altered,)))
    with pytest.raises(ProviderAccessError, match='Invalid GPT provider continuation'):
        validate_text_request(base_url=BASE, access_token='credential-sentinel', subscription=False, api_mode='completions', request=request)


def test_invalid_native_output_retains_incurred_usage():
    raw = native()
    raw['choices'][0]['message']['reasoning_details'][0]['data'] = 3
    with pytest.raises(ProviderAccessError) as caught:
        _result(raw, False, 'credential-sentinel', openrouter=True)
    assert caught.value.usage.reasoning_tokens == 7


@pytest.mark.parametrize('calls', [{}, [], [None], [dict(id='call', type='other', function={})]])
def test_invalid_chat_tool_carriers_fail_closed(calls):
    message = native()['choices'][0]['message']
    message['tool_calls'] = calls
    with pytest.raises(ContractError):
        chat_state(message)


def test_plaintext_only_native_carrier_is_retained_without_details():
    message = dict(role='assistant', content='Ok', reasoning='  Checked.  ')
    assert chat_state(message) == '  Checked.  '


@pytest.mark.parametrize('base,mode', [(BASE,'responses'),('https://api.openai.com/v1','completions')])
def test_chat_replay_requires_its_original_route(base, mode):
    answer = _result(native(), False, 'credential-sentinel', openrouter=True)
    request = LLMRequest(MODEL, CompletionRequest((answer.payload,)))
    with pytest.raises(ProviderAccessError):
        validate_text_request(base_url=base, access_token='credential-sentinel', subscription=False, api_mode=mode, request=request)


def test_plaintext_alias_does_not_duplicate_structured_native_text():
    message = native()['choices'][0]['message']
    message['reasoning'] = '  Checked.  '
    assert chat_state(message) == '  Checked.  '


@pytest.mark.asyncio
async def test_chat_credential_fragments_are_guarded_across_reasoning_and_answer():
    from api_agent.llm_providers.gpt.inference import _guard_content
    raw = native()
    message = raw['choices'][0]['message']
    message['reasoning_details'] = [dict(type='reasoning.text', text='credential-')]
    message['content'] = 'sentinel'
    decoded = _result(raw, False, 'credential-sentinel', openrouter=True)
    with pytest.raises(ProviderAccessError, match='protected'):
        await _guard_content(raw, 'credential-sentinel', decoded=decoded, router_native=True)


@pytest.mark.asyncio
async def test_unknown_chat_family_native_output_is_rejected_with_usage():
    request = LLMRequest('openai/gpt-7-unknown', CompletionRequest((Message('user', (TextContent('Question'),)),)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=native()))) as client:
        with pytest.raises(ProviderAccessError, match='not qualified') as caught:
            await complete_text(client=client, base_url=BASE, access_token='credential-sentinel', subscription=False, api_mode='completions', request=request)
    assert caught.value.usage.reasoning_tokens == 7


@pytest.mark.parametrize('finish', ['stop', 'length'])
@pytest.mark.parametrize('details', [dict(data='opaque-state', signature='opaque-signature'), [dict(type='reasoning.encrypted', data=3)]])
def test_malformed_chat_native_state_fails_closed_even_after_output_limit(finish, details):
    raw = native()
    raw['choices'][0]['finish_reason'] = finish
    raw['choices'][0]['message']['reasoning_details'] = details
    with pytest.raises(ProviderAccessError) as caught:
        _result(raw, False, 'credential-sentinel', openrouter=True)
    assert caught.value.usage.reasoning_tokens == 7
    projected = json.dumps(evidence_payload(raw))
    assert 'opaque-state' not in projected and 'opaque-signature' not in projected


def test_incomplete_valid_chat_reasoning_displays_without_replay_state():
    raw = native()
    raw['choices'][0]['finish_reason'] = 'length'
    result = _result(raw, False, 'credential-sentinel', openrouter=True)
    assert result.outcome == 'length'
    assert result.reasoning_summary == '  Checked.  '
    assert result.payload.continuation is None


@pytest.mark.parametrize('parameters,expected', [(['tools','tool_choice'],True),
    (['tools'],False), (['tool_choice'],False), (None,False), ({},False),
    (['tools','tool_choice',3],False), (['tools','tool_choice','tools'],False),
    (['tools','tool_choice','x'*101],False), (['tools','tool_choice']+['x']*127,False)])
@pytest.mark.asyncio
async def test_native_tool_discovery_requires_valid_explicit_catalog_support(parameters, expected):
    from api_agent.llm_providers.gpt.discovery import discover_selections
    raw = dict(data=[dict(id=MODEL, supported_parameters=parameters)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,json=raw if request.url.path.endswith('/models') else dict(data=dict(label='Synthetic key'))))) as client:
        rows = await discover_selections(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False)
    assert ('tools' in rows[0].features) is expected


def test_router_tools_require_parameter_support_without_output_override():
    request = LLMRequest(MODEL, CompletionRequest((Message('user',(TextContent('Lookup'),)),),tools=(Tool('lookup','Lookup','{"type":"object","properties":{},"required":[],"additionalProperties":false}'),)))
    _, body = validate_text_request(base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='responses',request=request)
    assert body['provider'] == dict(require_parameters=True,allow_fallbacks=False)


@pytest.mark.parametrize('index,accepted', [(0,True),(1,False),(-1,False),(True,False),('0',False),(None,False)])
def test_optional_tool_index_must_match_original_sequence(index, accepted):
    message = native(tools=True)['choices'][0]['message']
    message['tool_calls'][0]['index'] = index
    if accepted:
        assert chat_state(message) == '  Checked.  '
    else:
        with pytest.raises(ContractError, match='assistant tools'):
            chat_state(message)


@pytest.mark.parametrize('field', ['id','type','function'])
def test_missing_tool_field_is_a_contract_error(field):
    message = native(tools=True)['choices'][0]['message']
    del message['tool_calls'][0][field]
    with pytest.raises(ContractError):
        chat_state(message)


@pytest.mark.parametrize('model,effort,temperature,tools', [
    ('gpt-4.1','high',None,False), ('gpt-6-luna','high',0.5,False),
    ('gpt-6-astra','high',None,True)])
def test_effort_controls_remain_independent_rejections(model, effort, temperature, tools):
    from api_agent.llm_providers.gpt.reasoning import reasoning_fields
    from api_agent.providers.llm import GenerationOptions
    request = LLMRequest(model,CompletionRequest((Message('user',(TextContent('Question'),)),),
        tools=(Tool('lookup','Lookup','{"type":"object"}'),) if tools else (),
        generation=GenerationOptions(reasoning_effort=effort,temperature=temperature)))
    with pytest.raises(ProviderAccessError,match='support|requires'):
        reasoning_fields(request,base_url='https://api.openai.com/v1',api_mode='completions')


def test_omitted_effort_keeps_provider_defaults():
    from api_agent.llm_providers.gpt.reasoning import reasoning_fields
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    assert reasoning_fields(request,base_url=BASE,api_mode='completions') == {}


@pytest.mark.parametrize('mode,expected', [('responses',dict(reasoning=dict(effort='low'))),('completions',dict(reasoning_effort='low'))])
def test_official_effort_rendering_keeps_its_own_wire_contract(mode, expected):
    from api_agent.llm_providers.gpt.reasoning import reasoning_fields
    from api_agent.providers.llm import GenerationOptions
    request = LLMRequest('gpt-6-luna',CompletionRequest((Message('user',(TextContent('Question'),)),),generation=GenerationOptions(reasoning_effort='low')))
    assert reasoning_fields(request,base_url='https://api.openai.com/v1',api_mode=mode) == expected


@pytest.mark.parametrize('native_carrier', [
    dict(output=dict(type='reasoning',encrypted_content='opaque-sentinel',signature='signature-sentinel')),
    dict(output=[dict(type='reasoning',id='r',content='malformed',encrypted_content='opaque-sentinel',signature='signature-sentinel')]),
    dict(output=[dict(type='reasoning',id='r',status='failed',encrypted_content='opaque-sentinel',signature='signature-sentinel')]),
    dict(choices=dict(message=dict(reasoning_details=[dict(type='reasoning.encrypted',data='opaque-sentinel',signature='signature-sentinel')]))),
    dict(choices=[dict(message=dict(role='assistant',reasoning_details='{"data":"opaque-sentinel","signature":"signature-sentinel"}'))]),
    dict(choices=[dict(message=dict(role='assistant',reasoning_details=dict(data='opaque-sentinel',signature='signature-sentinel')))]),
    dict(choices=[dict(delta=dict(reasoning_details=[dict(type='reasoning.encrypted',data='opaque-sentinel',signature='signature-sentinel')]))]),
    dict(choices=['{"data":"opaque-sentinel","signature":"signature-sentinel"}']),
    dict(choices=[dict(message='{"data":"opaque-sentinel","signature":"signature-sentinel"}')]),
])
@pytest.mark.asyncio
async def test_rejected_malformed_native_carriers_never_reach_transport_evidence(native_carrier):
    raw = dict(error=dict(message='Safe failure'),usage=dict(prompt_tokens=5,completion_tokens=7),**native_carrier)
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(400,json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
    recorded = json.dumps(evidence['rows'])
    assert 'opaque-sentinel' not in recorded and 'signature-sentinel' not in recorded
    assert caught.value.usage.output_tokens == 7
    assert caught.value.http_status == 400


def incomplete_response():
    return dict(status='incomplete',incomplete_details=dict(reason='max_output_tokens'),output=[
        dict(type='reasoning',id='r',status='incomplete',summary=[],encrypted_content='opaque-sentinel'),
        dict(type='message',id='m',role='assistant',status='incomplete',content=[dict(type='output_text',text='Ok')])],
        usage=dict(input_tokens=5,output_tokens=7))


@pytest.mark.parametrize('change', ['reasoning_id','duplicate_id','message_opaque','reasoning_unknown'])
@pytest.mark.asyncio
async def test_truncated_native_responses_validate_full_shape_before_evidence(change):
    raw = incomplete_response()
    if change == 'reasoning_id':
        raw['output'][0]['id'] = []
    elif change == 'duplicate_id':
        raw['output'][1]['id'] = 'r'
    elif change == 'message_opaque':
        raw['output'][1].update(encrypted_content='opaque-sentinel',signature='signature-sentinel')
    else:
        raw['output'][0]['unknown'] = 'unqualified'
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='responses',request=request)
    recorded = json.dumps(evidence['rows'])
    assert 'opaque-sentinel' not in recorded and 'signature-sentinel' not in recorded
    assert caught.value.usage.output_tokens == 7


@pytest.mark.asyncio
async def test_valid_partial_native_response_keeps_answer_usage_without_replay():
    raw = incomplete_response()
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=raw))) as client:
            result = await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='responses',request=request)
    assert result.outcome == 'length'
    assert result.payload.content[0].text == 'Ok'
    assert result.payload.continuation is None
    assert result.usage.output_tokens == 7
    assert 'opaque-sentinel' not in json.dumps(evidence['rows'])


@pytest.mark.parametrize('mode', ['responses','completions'])
@pytest.mark.asyncio
async def test_rejected_fragmented_credential_keeps_incurred_usage_without_capture(mode):
    raw = (dict(output=[dict(type='reasoning',id='r',summary=[],content=[dict(type='reasoning_text',text='credential-')]),
        dict(type='message',id='m',role='assistant',content=[dict(type='output_text',text='sentinel')])],usage=dict(input_tokens=5,output_tokens=7))
        if mode == 'responses' else dict(choices=[dict(message=dict(role='assistant',content='sentinel',reasoning_details=[dict(type='reasoning.text',text='credential-')]))],usage=dict(prompt_tokens=5,completion_tokens=7)))
    raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(400,json=raw))) as client:
            with pytest.raises(ProviderAccessError,match='protected') as caught:
                await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode=mode,request=request)
    assert caught.value.usage.input_tokens == 5
    assert caught.value.usage.output_tokens == 7
    assert caught.value.http_status == 400
    assert all(row['phase'] != 'response' for row in evidence['rows'])


@pytest.mark.parametrize('parameter', [' ', '\n', ' tool_choice', 'tool-choice', 'Tool_choice', 'tools\t'])
@pytest.mark.asyncio
async def test_malformed_catalog_parameter_names_withhold_tool_capability(parameter):
    from api_agent.llm_providers.gpt.discovery import discover_selections
    raw = dict(data=[dict(id=MODEL,supported_parameters=['tools','tool_choice',parameter])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=raw if request.url.path.endswith('/models') else dict(data=dict(label='Synthetic key'))))) as client:
        rows = await discover_selections(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False)
    assert 'tools' not in rows[0].features


@pytest.mark.asyncio
async def test_native_refusal_validates_without_retaining_replay_state():
    raw = incomplete_response()
    raw.update(status='completed')
    raw.pop('incomplete_details')
    for item in raw['output']:
        item['status'] = 'completed'
    raw['output'][1]['content'] = [dict(type='refusal',refusal='Declined')]
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=raw))) as client:
            result = await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='responses',request=request)
    assert result.outcome == 'refused'
    assert result.payload.content[0].text == 'Declined'
    assert result.payload.continuation is None
    assert result.usage.output_tokens == 7
    assert 'opaque-sentinel' not in json.dumps(evidence['rows'])


@pytest.mark.parametrize('status', [200,400])
@pytest.mark.asyncio
async def test_unhashable_native_detail_type_is_a_safe_accounted_rejection(status):
    raw = native()
    raw['choices'][0]['message']['reasoning_details'][0]['type'] = []
    if status == 400:
        raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
    assert caught.value.usage.output_tokens == 10
    assert 'opaque-state' not in json.dumps(evidence['rows'])
