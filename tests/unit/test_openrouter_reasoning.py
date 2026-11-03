"""Native plaintext is separate from answers and retained verbatim for replay."""
from dataclasses import replace
import copy
import json

import pytest

from api_agent.llm_providers.gpt.inference import _result, validate_text_request, _guard_content
from api_agent.llm_providers.gpt.rich import native_summary
from api_agent.llm_providers.gpt.router_reasoning import reasoning_text, carriers, evidence_payload
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent, Tool, ToolResult
from api_agent.api.chat_contract import result as browser_result

BASE = 'https://openrouter.ai/api/v1'
MODEL = 'openai/gpt-oss-20b'


def native_response(*, tools=False):
    answer = ({'type': 'function_call', 'id': 'fn-1', 'call_id': 'call-1',
               'name': 'lookup', 'arguments': '{}', 'status': 'completed'} if tools else
              {'type': 'message', 'id': 'msg-1', 'status': 'completed', 'role': 'assistant',
               'content': [{'type': 'output_text', 'text': 'Ok', 'annotations': []}]})
    return {'status': 'completed', 'output': [
        {'type': 'reasoning', 'id': 'rs-1', 'status': 'completed', 'format': 'unknown',
         'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'Check the product.'}],
         'signature': 'opaque-signature'}, answer]}


@pytest.mark.parametrize('tools', [False, True])
def test_native_text_displays_separately_and_replays_verbatim(tools):
    raw = native_response(tools=tools)
    answer = _result(raw, True, 'credential-sentinel', openrouter=True)
    assert answer.reasoning_summary == 'Check the product.'
    assert json.loads(answer.payload.continuation)['state'] == raw['output']
    messages = (Message('user', (TextContent('Question'),)), answer.payload)
    definitions = ()
    if tools:
        definitions = (Tool('lookup', 'Lookup', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),)
        messages += (Message('tool', results=(ToolResult('call-1', (TextContent('Found'),)),)),)
    request = LLMRequest(MODEL, CompletionRequest((*messages,
        Message('user', (TextContent('Continue'),))), tools=definitions))
    _, body = validate_text_request(base_url=BASE, access_token='credential-sentinel',
        subscription=False, api_mode='responses', request=request)
    assert body['input'][1:3] == raw['output']
    if not tools:
        displayed = browser_result('turn', answer)
        assert displayed['reasoning_summary'] == 'Check the product.'
        assert 'opaque-signature' not in json.dumps(displayed)
        assert 'continuation' not in displayed
        assert displayed['response_text'] == 'Ok'



@pytest.mark.parametrize('base,mode', [('https://api.openai.com/v1', 'responses'),
                                     (BASE, 'completions')])
def test_native_replay_rejects_a_different_route(base, mode):
    answer = _result(native_response(), True, 'credential-sentinel', openrouter=True)
    request = LLMRequest(MODEL, CompletionRequest((answer.payload,)))
    with pytest.raises(ProviderAccessError):
        validate_text_request(base_url=base, access_token='credential-sentinel',
                              subscription=False, api_mode=mode, request=request)


@pytest.mark.parametrize('change', [
    {'content': {}}, {'summary': None}, {'content': [{'type': 'reasoning_text', 'text': 3}]},
    {'content': [{'type': 'output_text', 'text': 'wrong'}]},
    {'content': [{'type': 'reasoning_text', 'text': 'x', 'extra': 1}]},
    {'signature': ''}, {'signature': 3}, {'format': 'x'*101}, {'encrypted_content': 'x'*32769},
    {'signature': 'bad\nmetadata'}, {'content': [{'type': 'reasoning_text', 'text': '\ud800'}]},
    {'content': [{'type': 'reasoning_text', 'text': 'x'*65537}]},
    {'unknown': 'unadmitted'},
])
@pytest.mark.parametrize('tools', [False, True])
def test_malformed_native_reasoning_is_rejected(change, tools):
    raw = native_response(tools=tools)
    raw['output'][0].update(change)
    with pytest.raises(ProviderAccessError):
        _result(raw, True, 'credential-sentinel', openrouter=True)


def test_summary_takes_display_precedence_but_all_plaintext_is_guarded():
    raw = native_response()
    raw['output'][0]['summary'] = [{'type': 'summary_text', 'text': 'Summary.'}]
    result = _result(raw, True, 'credential-sentinel', openrouter=True)
    assert result.reasoning_summary == 'Summary.'
    assert 'Summary.' in carriers(None, result.payload.continuation)
    assert 'Check the product.' in carriers(None, result.payload.continuation)
    assert carriers(None) == ()
    assert carriers(None, json.dumps({'provider': 'openai.responses', 'state': []})) == ()


def test_separate_reasoning_items_keep_a_visible_boundary():
    raw = native_response()
    other = copy.deepcopy(raw['output'][0])
    other['id'] = 'rs-2'
    raw['output'].insert(1, other)
    result = _result(raw, True, 'credential-sentinel', openrouter=True)
    assert result.reasoning_summary == 'Check the product.\nCheck the product.'


def test_empty_or_encrypted_only_reasoning_is_not_displayed_as_text():
    raw = native_response()
    raw['output'][0].update(content=None, encrypted_content='opaque-state')
    result = _result(raw, True, 'credential-sentinel', openrouter=True)
    assert result.reasoning_summary is None
    assert json.loads(result.payload.continuation)['state'] == raw['output']
    assert 'opaque-state' not in json.dumps(browser_result('turn', result))
    assert reasoning_text({'type': 'reasoning', 'id': 'empty'}) == ('', ())


def test_official_decoder_still_rejects_plaintext_native_state():
    raw = native_response()
    raw['output'][0]['encrypted_content'] = 'opaque-state'
    with pytest.raises(ProviderAccessError):
        _result(raw, True, 'credential-sentinel')


@pytest.mark.asyncio
async def test_undisplayed_native_text_cannot_complete_an_answer_credential():
    raw = native_response()
    raw['output'][0].update(summary=[{'type': 'summary_text', 'text': 'Safe summary.'}],
        content=[{'type': 'reasoning_text', 'text': 'credential-tail'}])
    raw['output'][1]['content'][0]['text'] = 'credential-head-'
    result = _result(raw, True, 'credential-head-credential-tail', openrouter=True)
    with pytest.raises(ProviderAccessError, match='protected authentication'):
        await _guard_content(raw, 'credential-head-credential-tail', decoded=result, router_native=True)


@pytest.mark.parametrize('outcome', ['completed', 'length', 'refused'])
@pytest.mark.parametrize('field', ['content', 'signature', 'format', 'encrypted_content'])
@pytest.mark.asyncio
async def test_raw_native_fields_cannot_complete_a_credential(outcome, field):
    raw = native_response()
    item = raw['output'][0]
    item['summary'] = [{'type': 'summary_text', 'text': 'Safe summary.'}]
    if field == 'content':
        item[field] = [{'type': 'reasoning_text', 'text': 'credential-tail'}]
    else:
        item[field] = 'credential-tail'
    block = raw['output'][1]['content'][0]
    block['text'] = 'credential-head-'
    if outcome == 'length':
        raw.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    elif outcome == 'refused':
        block.clear()
        block.update(type='refusal', refusal='credential-head-')
    result = _result(raw, True, 'credential-head-credential-tail', openrouter=True)
    with pytest.raises(ProviderAccessError, match='protected authentication'):
        await _guard_content(raw, 'credential-head-credential-tail', decoded=result, router_native=True)


@pytest.mark.parametrize('outcome', ['length', 'refused'])
def test_nonreplay_reasoning_rejects_unrecognized_fields(outcome):
    raw = native_response()
    raw['output'][0]['unknown_native'] = 'unadmitted'
    if outcome == 'length':
        raw.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    else:
        raw['output'][1]['content'] = [{'type': 'refusal', 'refusal': 'No'}]
    with pytest.raises(ProviderAccessError):
        _result(raw, True, 'credential-sentinel', openrouter=True)


@pytest.mark.parametrize('base', [BASE, 'https://compatible.example/v1'])
def test_official_native_state_cannot_cross_to_openrouter(base):
    raw = native_response()
    raw['output'][0] = {'type': 'reasoning', 'id': 'rs-1', 'summary': [],
                        'encrypted_content': 'opaque-official-state'}
    answer = _result(raw, True, 'credential-sentinel')
    request = LLMRequest(MODEL, CompletionRequest((answer.payload,)))
    with pytest.raises(ProviderAccessError, match='original route'):
        validate_text_request(base_url=base, access_token='credential-sentinel',
            subscription=False, api_mode='responses', request=request)


@pytest.mark.asyncio
async def test_other_openrouter_models_do_not_gain_native_admission():
    from api_agent.llm_providers.gpt.inference import complete_text
    import httpx
    raw = native_response()
    raw['usage'] = {'input_tokens': 2, 'output_tokens': 3}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=raw))) as client:
        request = LLMRequest('openai/gpt-6.1-terra', CompletionRequest((Message('user', (TextContent('Question'),)),)))
        with pytest.raises(ProviderAccessError, match='not qualified') as caught:
            await complete_text(client=client, base_url=BASE, access_token='credential-sentinel',
                subscription=False, api_mode='responses', request=request)
    assert caught.value.usage.input_tokens == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['openai/gpt-oss-20b', 'openai/gpt-oss-120b'])
async def test_qualified_http_result_keeps_native_reasoning_and_replay(model):
    from api_agent.llm_providers.gpt.inference import complete_text
    import httpx
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        raw = native_response()
        raw['usage'] = {'input_tokens': 2, 'output_tokens': 3}
        return httpx.Response(200, json=raw)
    request = LLMRequest(model, CompletionRequest((Message('user', (TextContent('Question'),)),),
        generation=GenerationOptions(reasoning_effort='low')))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await complete_text(client=client, base_url=BASE, access_token='credential-sentinel',
                subscription=False, api_mode='responses', request=request)
            assert first.reasoning_summary == 'Check the product.'
            following = replace(request, payload=replace(request.payload,
                messages=(*request.payload.messages, first.payload, Message('user', (TextContent('Continue'),)))))
            await complete_text(client=client, base_url=BASE, access_token='credential-sentinel',
                subscription=False, api_mode='responses', request=following)
    assert sent[1]['input'][1:3] == native_response()['output']
    assert evidence['rows']
    recorded = json.dumps(evidence['rows'])
    assert 'opaque-signature' not in recorded
    assert 'Check the product.' in recorded
    assert 'Ok' in recorded
    assert any(row['phase'] == 'request' for row in evidence['rows'])
    assert any(row['phase'] == 'response' for row in evidence['rows'])


def test_native_carrier_guard_ignores_other_items_but_keeps_nested_strings():
    assert carriers({'output': [None, {'type': 'message'}]}) == ()
    raw = native_response()
    raw['output'][0]['encrypted_content'] = None
    assert 'Check the product.' in carriers(raw)


def test_evidence_projection_is_contextual_and_leaves_native_input_unchanged():
    raw = native_response()
    raw['output'][0]['encrypted_content'] = 'opaque-encrypted'
    raw['metadata'] = {'signature': 'ordinary-tool-data', 'format': 'ordinary-format'}
    raw['input'] = [None, {'type': 'message', 'signature': 'ordinary-message-data'},
                    copy.deepcopy(raw['output'][0])]
    before = copy.deepcopy(raw)
    projected = evidence_payload(raw)
    for name in ('input', 'output'):
        item = next(item for item in projected[name] if type(item) is dict and item['type'] == 'reasoning')
        assert not {'signature', 'format', 'encrypted_content'} & item.keys()
        assert item['content'] == before['output'][0]['content']
    assert projected['metadata'] == before['metadata']
    assert projected['input'][1]['signature'] == 'ordinary-message-data'
    assert raw == before
    assert evidence_payload({'input': 'plain-input'}) == {'input': 'plain-input'}


@pytest.mark.asyncio
@pytest.mark.parametrize('parts', [('credential-', 'tail'), ('tail', 'credential-')])
async def test_three_native_and_answer_fragments_are_checked_before_capture(parts):
    raw = native_response()
    raw['output'][1]['content'][0]['text'] = 'head-'
    raw['output'][0]['content'] = [{'type': 'reasoning_text', 'text': text} for text in parts]
    result = _result(raw, True, 'head-credential-tail', openrouter=True)
    captured = []
    with pytest.raises(ProviderAccessError, match='protected authentication'):
        await _guard_content(raw, 'head-credential-tail', decoded=result, commit=lambda: captured.append(raw))
    assert not captured


@pytest.mark.asyncio
async def test_failure_response_evidence_also_excludes_opaque_native_state():
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    raw = native_response()
    raw['output'][0]['encrypted_content'] = 'opaque-encrypted'
    raw['error'] = {'message': 'Rejected request', 'type': 'invalid_request_error'}
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json=raw))) as client:
            with pytest.raises(ProviderAccessError):
                await complete_text(client=client, base_url=BASE, access_token='credential-sentinel',
                    subscription=False, api_mode='responses', request=request)
    responses = [row for row in evidence['rows'] if row['phase'] == 'response']
    assert responses
    text = json.dumps(responses)
    assert 'opaque-signature' not in text
    assert 'opaque-encrypted' not in text
    assert 'Check the product.' in text
    assert raw['output'][0]['signature'] == 'opaque-signature'


@pytest.mark.asyncio
async def test_failure_response_fragment_check_precedes_evidence_commit():
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    raw = native_response()
    raw['output'][1]['content'][0]['text'] = 'head-'
    raw['output'][0]['content'] = [{'type': 'reasoning_text', 'text': text}
                                 for text in ('credential-', 'tail')]
    raw['error'] = {'message': 'Rejected request', 'type': 'invalid_request_error'}
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json=raw))) as client:
            with pytest.raises(ProviderAccessError, match='protected authentication'):
                await complete_text(client=client, base_url=BASE, access_token='head-credential-tail',
                    subscription=False, api_mode='responses', request=request)
    assert not [row for row in evidence['rows'] if row['phase'] == 'response']


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'x': 'tail'}, {'tail': 'ordinary'}, {'x': [{'tail': 3}]}])
async def test_native_answer_and_parsed_tool_fragments_are_guarded_before_http_capture(arguments):
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    raw = native_response(tools=True)
    raw['output'][0]['content'] = [{'type': 'reasoning_text', 'text': 'credential-'}]
    raw['output'][1]['arguments'] = json.dumps(arguments)
    raw['output'].append(native_response()['output'][1])
    raw['output'][-1]['content'][0]['text'] = 'head-'
    tool = Tool('lookup', 'Lookup', '{"type":"object"}', strict=False)
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),), tools=(tool,)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=raw))) as client:
            with pytest.raises(ProviderAccessError, match='protected authentication'):
                await complete_text(client=client, base_url=BASE, access_token='head-credential-tail',
                    subscription=False, api_mode='responses', request=request)
    assert not [row for row in evidence['rows'] if row['phase'] == 'response']


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [64, 128, 2040])
@pytest.mark.parametrize('credential', ['credential-sentinel', '{"password":"hunter2"}'])
async def test_large_benign_tool_arguments_preserve_success_and_evidence(count, credential):
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    arguments = {f'k{index}': f'v{index}' for index in range(count)}
    raw = native_response(tools=True)
    raw['output'][1]['arguments'] = json.dumps(arguments)
    tool = Tool('lookup', 'Lookup', '{"type":"object"}', strict=False)
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),), tools=(tool,)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=raw))) as client:
            result = await complete_text(client=client, base_url=BASE, access_token=credential,
                subscription=False, api_mode='responses', request=request)
    assert result.outcome == 'tool_calls'
    assert json.loads(result.payload.calls[0].arguments) == arguments
    assert result.reasoning_summary == 'Check the product.'
    assert any(row['phase'] == 'response' and row['reason'] == 'captured' for row in evidence['rows'])


def test_carrier_collection_stops_at_limit_before_consuming_remaining_arguments():
    def arguments():
        yield {str(index): str(index) for index in range(2050)}
        pytest.fail('Collection continued after the bound')
    with pytest.raises(ContractError, match='carrier limit'):
        carriers(native_response(), arguments=arguments())


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', ['answer', 'argument_value', 'argument_key', 'tool_to_tool'])
async def test_semantic_json_credential_fragments_are_rejected_before_capture(destination):
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    tail = 'word": "hunter2"}'
    raw = native_response(tools=destination != 'answer')
    raw['output'][0]['content'] = [{'type': 'reasoning_text',
                                  'text': 'ordinary' if destination == 'tool_to_tool' else '{"pass'}]
    if destination == 'answer':
        raw['output'][1]['content'][0]['text'] = tail
    else:
        arguments = ({'x': tail} if destination == 'argument_value' else
                     {'x': '{"pass', 'y': tail} if destination == 'tool_to_tool' else {tail: 'ordinary'})
        raw['output'][1]['arguments'] = json.dumps(arguments)
    tool = Tool('lookup', 'Lookup', '{"type":"object"}', strict=False)
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),), tools=(tool,)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=raw))) as client:
            with pytest.raises(ProviderAccessError, match='protected authentication'):
                await complete_text(client=client, base_url=BASE, access_token='{"password":"hunter2"}',
                    subscription=False, api_mode='responses', request=request)
    assert not [row for row in evidence['rows'] if row['phase'] == 'response']


def test_semantic_carrier_work_is_bounded():
    from api_agent.llm_providers.gpt.router_reasoning import guard_semantic_carriers
    with pytest.raises(ContractError, match='limit exceeded'):
        guard_semantic_carriers(['x' * (8 * 1024 * 1024 + 1)],
                                {'{"password":"hunter2"}'})


@pytest.mark.asyncio
@pytest.mark.parametrize('credential, fragments', [
    ('{"password":"hunter2"}', ['{"pass', 'word": ', '"hunter2"}']),
    ('{"a":1,"b":2}', ['{"b":', '2,"a":', '1}']),
    ('100', ['1', 'e', '2']),
])
@pytest.mark.parametrize('destination', ['tool', 'answer'])
async def test_unresolved_multi_part_json_is_withheld_before_capture(credential, fragments, destination):
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    import httpx
    raw = native_response(tools=True)
    if destination == 'tool':
        raw['output'][1]['arguments'] = json.dumps(dict(zip(('left', 'middle', 'right'), fragments)))
    else:
        raw['output'].append(native_response()['output'][1])
        raw['output'][-1]['content'] = [{'type': 'output_text', 'text': text, 'annotations': []} for text in fragments]
    tool = Tool('lookup', 'Lookup', '{"type":"object"}', strict=False)
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),), tools=(tool,)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=raw))) as client:
            with pytest.raises(ProviderAccessError, match='protected authentication'):
                await complete_text(client=client, base_url=BASE, access_token=credential,
                    subscription=False, api_mode='responses', request=request)
    assert not [row for row in evidence['rows'] if row['phase'] == 'response']


def test_completed_semantic_carriers_still_use_common_guard():
    from api_agent.llm_providers.gpt.router_reasoning import guard_semantic_carriers
    from api_agent.providers.credential_guard import CredentialProtectionError
    with pytest.raises(CredentialProtectionError):
        guard_semantic_carriers(['{"b":2, "a":1}', 'ordinary'], {'{"a":1,"b":2}'})
    guard_semantic_carriers(['{"safe":"value"}', 'ordinary'], {'{"a":1,"b":2}'})


def test_semantic_pair_budget_includes_normalization_work():
    from api_agent.llm_providers.gpt.router_reasoning import guard_semantic_carriers
    with pytest.raises(ContractError, match='limit exceeded'):
        guard_semantic_carriers([json.dumps({'safe': 'x' * (3 * 1024 * 1024)}), 'ordinary'],
                                {'{"password":"hunter2"}'})
    guard_semantic_carriers(['{"safe":"value"}', '{"also":"safe"}'], {'{"a":1,"b":2}'})
    guard_semantic_carriers(['ordinary', 'safe'], {'100'})
