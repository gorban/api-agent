"""Controlled route admission and exact effective-response-policy payloads."""
import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.llm_providers.gpt.response_policy import prepare_response as gpt_prepare
from api_agent.llm_providers.bedrock.response_policy import prepare_response as bedrock_prepare
from api_agent.llm_providers.response_policy import native_schema_supported, resolve_response
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.llm import (CompletionRequest, GenerationOptions, LLMRequest,
    Message, TextContent, StructuredOutput, ResponsePolicy, Tool)

BASE = 'https://api.openai.com/v1'
SHAPE = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}'
OPEN = '{"type":"object","properties":{"arguments":{"type":"object"}},"required":["arguments"]}'


def request(mode='structured', fallback='allow', *, model='gpt-6-sol', shape=SHAPE, strict=True, tools=()):
    messages = (Message('system', (TextContent('Authored system'),)),
                Message('user', (TextContent('Authored question'),)))
    return LLMRequest(model, CompletionRequest(messages, tools, GenerationOptions(),
        response=ResponsePolicy(mode, fallback, None if mode == 'text' else StructuredOutput('answer', shape, strict))), 'call-one')


def prepare(req, *, base=BASE, mode='responses', subscription=False):
    return gpt_prepare(req, base_url=base, api_mode=mode, subscription=subscription)


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('model', ['gpt-6-sol', 'gpt-6.1-sol', 'gpt-5.6-luna', 'gpt-4o-mini', 'gpt-4o-mini-2024-07-18', 'gpt-4o-2024-08-06'])
def test_exact_official_models_admit_schema_without_projection(mode, model):
    req = request(model=model)
    adapted, evidence = prepare(req, mode=mode)
    assert adapted.payload.structured is req.payload.response.schema
    assert adapted.payload.messages == req.payload.messages
    assert adapted.selected_llm == req.selected_llm and adapted.request_id == req.request_id
    assert evidence.actual_mode == 'structured' and evidence.fallback_reason is None
    assert evidence.schema_sha256 == hashlib.sha256(SHAPE.encode()).hexdigest()


@pytest.mark.parametrize('kwargs', [dict(base='https://alternate.example/v1'), dict(subscription=True), dict(mode='unknown')])
def test_unknown_routes_downgrade_only_when_allowed(kwargs):
    req = request()
    adapted, evidence = prepare(req, **kwargs)
    assert evidence.actual_mode == 'prompted_json' and evidence.fallback_reason == 'unknown_capability'
    assert adapted.payload.messages is req.payload.messages
    assert adapted.payload.structured is None and adapted.payload.response is None
    with pytest.raises(OperationalError):
        prepare(request(fallback='reject'), **kwargs)


ROUTER = 'https://openrouter.ai/api/v1'


@pytest.mark.parametrize('api_mode', ['completions', 'responses'])
def test_openrouter_exact_structured_route_sends_required_routing_and_schema(api_mode):
    req = request(fallback='reject', model='openai/gpt-6-luna')
    result, calls = run_gpt(req, base=ROUTER, api_mode=api_mode)
    body = json.loads(calls[0].content)
    assert result.response.actual_mode == 'structured'
    assert body['provider'] == {'require_parameters': True}
    definition = {'name': 'answer', 'schema': json.loads(SHAPE), 'strict': True}
    if api_mode == 'responses':
        assert body['text']['format'] == {'type': 'json_schema', **definition}
    else:
        assert body['response_format'] == {'type': 'json_schema', 'json_schema': definition}


@pytest.mark.parametrize('api_mode', ['completions', 'responses'])
def test_openrouter_structured_and_reasoning_keep_both_routing_constraints(api_mode):
    req = request(fallback='reject', model='openai/gpt-6-luna')
    req = replace(req, payload=replace(req.payload, generation=GenerationOptions(reasoning_effort='low')))
    result, calls = run_gpt(req, base=ROUTER, api_mode=api_mode)
    body = json.loads(calls[0].content)
    assert result.response.actual_mode == 'structured'
    assert body['provider'] == {'require_parameters': True, 'allow_fallbacks': False}
    assert body['reasoning'] == {'effort': 'low'}
    assert (body['text']['format'] if api_mode == 'responses' else body['response_format'])['type'] == 'json_schema'


@pytest.mark.parametrize('api_mode', ['completions', 'responses'])
def test_openrouter_invalid_structured_response_keeps_evidence_and_usage(api_mode):
    req = request(fallback='reject', model='openai/gpt-6-luna')
    with pytest.raises(ProviderAccessError) as caught:
        run_gpt(req, base=ROUTER, api_mode=api_mode, output='{"answer":7}')
    assert caught.value.response.actual_mode == 'structured'
    assert caught.value.usage.output_tokens == 2


@pytest.mark.parametrize('base,model,mode,subscription', [
    ('https://alternate.example/v1', 'openai/gpt-6-luna', 'completions', False),
    (ROUTER, 'openai/gpt-6-sol', 'completions', False),
    (ROUTER, 'openai/gpt-6-sol', 'responses', False),
    ('https://alternate.example/v1', 'openai/gpt-6-luna', 'responses', False),
    (ROUTER, 'openai/gpt-6-luna', 'completions', True),
])
def test_openrouter_adjacent_routes_still_reject(base, model, mode, subscription):
    with pytest.raises(OperationalError):
        prepare(request(fallback='reject', model=model), base=base, mode=mode, subscription=subscription)


@pytest.mark.parametrize('kwargs', [dict(shape=OPEN), dict(tools=(Tool('lookup', '', SHAPE),))])
@pytest.mark.parametrize('api_mode', ['completions', 'responses'])
def test_openrouter_unqualified_schema_and_tools_still_reject(kwargs, api_mode):
    with pytest.raises(OperationalError):
        prepare(request(fallback='reject', model='openai/gpt-6-luna', **kwargs),
                base=ROUTER, mode=api_mode)


@pytest.mark.parametrize('shape', [OPEN, '{"type":"object","allOf":[{"type":"object"}]}',
    '{"type":"object","properties":{"value":false}}'])
def test_unsupported_schema_keeps_original_and_downgrades_without_closing_objects(shape):
    req = request(shape=shape)
    adapted, evidence = prepare(req)
    assert evidence.actual_mode == 'json' and evidence.fallback_reason == 'unsupported_schema'
    assert req.payload.response.schema.schema == shape
    assert adapted.payload.messages is req.payload.messages
    with pytest.raises(OperationalError):
        prepare(request(shape=shape, fallback='reject'))


def test_strict_false_remains_native_hint_and_is_never_rewritten():
    req = request(shape=OPEN, strict=False)
    adapted, evidence = prepare(req)
    assert adapted.payload.structured.strict is False and evidence.strict is False
    assert adapted.payload.structured.schema == OPEN
    assert native_schema_supported(StructuredOutput('shape', '{"type":"object","default":{}}', False)) is False


@pytest.mark.parametrize('mode', ['json', 'prompted_json', 'text'])
def test_explicit_weaker_modes_never_upgrade(mode):
    adapted, evidence = prepare(request(mode))
    assert evidence.actual_mode == mode and evidence.fallback_reason is None
    assert adapted.payload.structured is None
    assert (evidence.schema_sha256 is None) == (mode == 'text')


def test_tools_native_combination_is_unqualified_but_tool_text_route_preserved():
    tool = Tool('lookup', '', SHAPE)
    req = request(tools=(tool,))
    adapted, evidence = prepare(req)
    assert evidence.actual_mode == 'prompted_json' and evidence.fallback_reason == 'unsupported_combination'
    assert adapted.payload.tools == (tool,)
    with pytest.raises(OperationalError):
        prepare(request(tools=(tool,), fallback='reject'))


@pytest.mark.parametrize('model,mode,expected', [('zai.glm-5', 'completions', 'structured'),
    ('openai.gpt-oss-20b', 'responses', 'prompted_json'), ('zai.glm-4.7', 'completions', 'prompted_json')])
def test_bedrock_native_matrix_is_not_broadened(model, mode, expected):
    req = request(model=model)
    adapted, evidence = bedrock_prepare(req, mode)
    assert evidence.actual_mode == expected
    assert adapted.selected_llm == model
    if expected != 'structured':
        with pytest.raises(OperationalError):
            bedrock_prepare(request(model=model, fallback='reject'), mode)


@pytest.mark.parametrize('kwargs', [dict(strict=False), dict(shape=OPEN), dict(tools=(Tool('lookup', '', SHAPE),)),])
def test_bedrock_unqualified_schema_combination_uses_text(kwargs):
    _, evidence = bedrock_prepare(request(model='zai.glm-5', **kwargs), 'completions')
    assert evidence.actual_mode == 'prompted_json'


def test_bedrock_json_mode_rejects_or_honestly_downgrades():
    _, evidence = bedrock_prepare(request('json', model='zai.glm-5'), 'completions')
    assert evidence.actual_mode == 'prompted_json' and evidence.fallback_reason == 'unsupported_mode'
    with pytest.raises(OperationalError):
        bedrock_prepare(request('json', 'reject', model='zai.glm-5'), 'completions')


def test_legacy_requests_and_no_supported_mode():
    req = request()
    legacy = replace(req, payload=replace(req.payload, response=None))
    assert resolve_response(legacy, lambda mode: 'unknown_capability') == (legacy, None)
    with pytest.raises(OperationalError):
        resolve_response(req, lambda mode: 'unknown_capability')
    with pytest.raises(ContractError):
        prepare(request(shape='{"type":"object","properties":{"bad":{"type":"impossible"}}}'))


def run_gpt(req, *, api_mode='responses', output='{"answer":"yes"}', status=200, base=BASE, captures=None):
    from api_agent.llm_providers.gpt.inference import complete_text
    calls = [] if captures is None else captures
    async def handler(sent):
        calls.append(sent)
        if status != 200:
            return httpx.Response(status, json={'error': {'message': 'synthetic rejection'}})
        body = ({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': output}]}], 'usage': {'input_tokens': 4, 'output_tokens': 2}}
            if api_mode == 'responses' else {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': output}}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2}})
        return httpx.Response(200, json=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await complete_text(client=client, base_url=base, access_token='synthetic-token', subscription=False,
                api_mode=api_mode, request=req)
    return asyncio.run(run()), calls


@pytest.mark.parametrize('api_mode', ['responses', 'completions'])
def test_gpt_prompted_envelope_is_decoded_but_captured_as_returned(monkeypatch, api_mode):
    from api_agent.providers.rich_inference import response_json
    output = '````json\n```json\n{"answer":"yes"}\n```\n````'
    captures = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference',
        lambda *args, **kw: captures.append(args))

    result, calls = run_gpt(request('prompted_json'), api_mode=api_mode, output=output)

    assert len(calls) == 1
    assert response_json(result) == {'answer': 'yes'}
    assert result.payload.content[0].text == output
    provider_output = captures[-1][1]
    observed = (provider_output['output'][0]['content'][0]['text'] if api_mode == 'responses'
                else provider_output['choices'][0]['message']['content'])
    assert observed == output


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('answer,accepted', [('yes', True), ('synthetic-token', False)])
def test_gpt_prompted_envelope_retains_credential_protection(asynchronous, answer, accepted):
    from api_agent.llm_providers.gpt import rich
    from api_agent.providers.llm import LLMResult, Usage
    req = request('prompted_json')
    _, evidence = prepare(req)
    output = '```json\n' + json.dumps({'answer': answer}) + '\n```'
    result = LLMResult(Message('assistant', (TextContent(output),)), Usage(3, 2), response=evidence)

    def validate():
        if asynchronous:
            return asyncio.run(rich.async_validate_result(req, result, 'synthetic-token'))
        return rich.validate_result(req, result, 'synthetic-token')

    if accepted:
        assert validate() is result
    else:
        with pytest.raises(ProviderAccessError):
            validate()


def test_bedrock_prompted_envelope_uses_shared_decoder_without_changing_capture(monkeypatch):
    from api_agent.llm_providers.bedrock import factory as module
    from api_agent.providers.rich_inference import response_json
    output = '```json\n{"answer":"yes"}\n```'
    body = {'model': 'zai.glm-5', 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': output}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}
    transport = SimpleNamespace(call=AsyncMock(return_value=body))
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: transport)
    captures = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference',
        lambda *args, **kw: captures.append(args))

    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        return await provider.acomplete(request('prompted_json', model='zai.glm-5'))
    result = asyncio.run(run())

    assert response_json(result) == {'answer': 'yes'}
    assert result.payload.content[0].text == output
    assert captures[-1][1]['provider_response']['choices'][0]['message']['content'] == output
    assert transport.call.await_count == 1


@pytest.mark.parametrize('api_mode', ['responses', 'completions'])
@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json', 'text'])
def test_gpt_dispatch_captures_actual_native_input_and_output(monkeypatch, api_mode, mode):
    captures = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *args, **kw: captures.append((args, kw)))
    req = request(mode)
    result, calls = run_gpt(req, api_mode=api_mode)
    assert result.response.actual_mode == mode and len(calls) == 1
    assert [capture[0][0] for capture in captures] == ['input', 'output']
    body = json.loads(calls[0].content)
    assert captures[0][0][1]['provider_payload'] == body
    output = captures[1][0][1]
    if api_mode == 'responses':
        assert output['output'][0]['content'][0]['text'] == '{"answer":"yes"}'
    else:
        assert output['choices'][0]['message']['content'] == '{"answer":"yes"}'
    metadata = captures[0][0][1]['adapter_instructions']
    if mode in ('json', 'prompted_json'):
        assert metadata == [{'role': 'user', 'path': '/input/2' if api_mode == 'responses' else '/messages/2', 'purpose': 'response_policy'}]
    else:
        assert metadata == []
    native = body.get('text', {}).get('format') if api_mode == 'responses' else body.get('response_format')
    if mode == 'structured':
        assert native['type'] == 'json_schema'
    elif mode == 'json':
        assert native == {'type': 'json_object'}
    else:
        assert native is None


@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json'])
def test_completed_json_is_checked_against_original_without_replay(mode):
    with pytest.raises(ProviderAccessError) as caught:
        run_gpt(request(mode), output='{"answer":7}')
    assert caught.value.response.actual_mode == mode and caught.value.usage.output_tokens == 2


def test_unknown_model_and_route_are_never_native_capability_claims():
    _, evidence = prepare(request(model='gpt-future-2099'))
    assert evidence.actual_mode == 'prompted_json'
    result, calls = run_gpt(request(), base='https://alternate.example/v1')
    assert len(calls) == 1 and str(calls[0].url) == 'https://alternate.example/v1/responses'
    assert result.response.actual_mode == 'prompted_json'


def test_postdispatch_failure_keeps_evidence_and_never_replays():
    with pytest.raises(ProviderAccessError) as caught:
        run_gpt(request(), status=400)
    assert caught.value.response.actual_mode == 'structured'


def test_qualified_luna_subscription_native_schema_is_preserved():
    req = request(model='gpt-6-luna')
    adapted, evidence = prepare(req, subscription=True)
    assert evidence.actual_mode == 'structured' and adapted.payload.structured is req.payload.response.schema
    _, weaker = prepare(request('json', model='gpt-6-luna'), subscription=True)
    assert weaker.actual_mode == 'prompted_json'


@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json'])
def test_original_schema_containing_credential_is_rejected_before_capture(monkeypatch, mode):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    monkeypatch.setenv('LOG_CONTENT', 'false')
    monkeypatch.setenv('TRACELOOP_TRACE_CONTENT', 'false')
    boundary = EmissionBoundary(ContentPolicy(True))
    req = request(mode, shape=SHAPE.replace('"answer":{"type":"string"}', '"answer":{"type":"string","description":"synthetic-token"}'))
    calls = []
    with collect_inference(boundary) as captured:
        with pytest.raises(ProviderAccessError, match='protected authentication content'):
            run_gpt(req, captures=calls)
    assert calls == []
    assert captured['rows'] == []


@pytest.mark.parametrize('generation', [GenerationOptions(temperature=0.5), GenerationOptions(reasoning_effort='high', cache_policy='high')])
def test_format_fallback_does_not_drop_generation_controls(generation):
    from api_agent.llm_providers.gpt.inference import validate_text_request
    req = request(model='gpt-future-2099')
    req = replace(req, payload=replace(req.payload, generation=generation))
    adapted, evidence = prepare(req)
    assert evidence.actual_mode == 'prompted_json' and adapted.payload.generation is generation
    with pytest.raises(ProviderAccessError):
        validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False,
            api_mode='responses', request=req)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json', 'text'])
def test_bedrock_real_preflight_dispatch_and_terminal_evidence(monkeypatch, stream, mode):
    from api_agent.llm_providers.bedrock import factory as module
    req = request(mode, model='zai.glm-5')
    body = {'model': 'zai.glm-5', 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': '{"answer":"yes"}'}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}
    dispatched = []
    async def call(api_mode, payload, **kwargs):
        dispatched.append(payload)
        return body
    async def events(api_mode, payload, **kwargs):
        from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream
        dispatched.append(payload)
        decoder = WorkerStream('completions', 'zai.glm-5', set())
        source = [
            {'id': 'stream-one', 'object': 'chat.completion.chunk', 'model': 'zai.glm-5', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '{"answer":"yes"}'}, 'finish_reason': None}]},
            {'id': 'stream-one', 'object': 'chat.completion.chunk', 'model': 'zai.glm-5', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            {'id': 'stream-one', 'object': 'chat.completion.chunk', 'model': 'zai.glm-5', 'choices': [], 'usage': body['usage']},
        ]
        for event in source:
            for frame in decoder.feed(event):
                yield frame
        for frame in decoder.finish():
            yield frame
    transport = SimpleNamespace(call=call, stream=events)
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: transport)
    captures = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *args, **kw: captures.append(args))
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        if stream:
            terminal = [item async for item in provider.astream(req)][-1]
            return terminal.payload
        return await provider.acomplete(req)
    result = asyncio.run(run())
    assert result.response.actual_mode == ('prompted_json' if mode == 'json' else mode)
    assert len(captures) == 1
    assert captures[0][0] == 'output'
    assert len(dispatched) == 1
    payload = dispatched[0]['body']
    assert ('response_format' in payload) == (mode == 'structured')
    assert payload['model'] == 'zai.glm-5'


@pytest.mark.parametrize('stream', [False, True])
def test_bedrock_transport_error_retains_mode_without_replay(monkeypatch, stream):
    from api_agent.llm_providers.bedrock import factory as module
    req = request(model='zai.glm-5')
    error = ProviderAccessError('unavailable', 'synthetic transport failure')
    async def fail(*args, **kwargs):
        kwargs['on_dispatch']()
        raise error
    async def events(*args, **kwargs):
        kwargs['on_dispatch']()
        raise error
        yield
    transport = SimpleNamespace(call=AsyncMock(side_effect=fail), stream=events)
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: transport)
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        if stream:
            return [event async for event in provider.astream(req)]
        return await provider.acomplete(req)
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(run())
    assert caught.value is error and error.response.actual_mode == 'structured'
    assert transport.call.await_count == (0 if stream else 1)


def test_native_schema_arrays_are_kept_without_projection():
    shape = '{"type":"object","properties":{"rows":{"type":"array","items":{"type":"string"}}},"required":["rows"],"additionalProperties":false}'
    assert native_schema_supported(StructuredOutput('shape', shape))


def test_provider_payload_helpers_reject_invalid_request_objects():
    from api_agent.llm_providers.gpt.inference import _payload
    from api_agent.llm_providers.gpt.rich import payload
    with pytest.raises(ProviderAccessError):
        _payload(None, 'responses', False)
    req = request()
    req = replace(req, payload=replace(req.payload, response=None, generation=GenerationOptions(1048577)))
    with pytest.raises(ProviderAccessError):
        payload(req, 'responses', False)


def test_response_resolution_rejects_invalid_request_before_capability_checks():
    def supports(mode):
        pytest.fail('Invalid requests must not inspect provider support')

    with pytest.raises(ProviderAccessError, match='Invalid response-policy request') as caught:
        resolve_response(None, supports)

    assert caught.value.kind == 'invalid_request'


@pytest.mark.parametrize('output,token,accept', [('{"answer":"yes"}', None, True),
    ('{"answer":"yes"}', 'unrelated-token', True), ('{"answer":"synthetic-token"}', 'synthetic-token', False),
    ('{"answer":7}', None, False)])
def test_sync_provider_validation_uses_original_policy_and_protects_credentials(output, token, accept):
    from api_agent.llm_providers.gpt.rich import validate_result
    from api_agent.providers.llm import LLMResult, Usage
    req = request()
    _, evidence = prepare(req)
    result = LLMResult(Message('assistant', (TextContent(output),)), Usage(3, 2), response=evidence)
    if accept:
        assert validate_result(req, result, token) is result
    else:
        with pytest.raises(ProviderAccessError) as caught:
            validate_result(req, result, token)
        assert caught.value.response is evidence and caught.value.usage is result.usage


def test_sync_policy_tool_calls_keep_intermediate_outcome():
    from api_agent.llm_providers.gpt.rich import validate_result
    from api_agent.providers.llm import LLMResult, ToolCall
    req = request(tools=(Tool('lookup', '', SHAPE),))
    _, evidence = prepare(req)
    result = LLMResult(Message('assistant', calls=(ToolCall('one', 'lookup', '{"answer":"yes"}'),)),
        outcome='tool_calls', response=evidence)
    assert validate_result(req, result) is result


@pytest.mark.parametrize('outcome', ['refused', 'length'])
def test_sync_policy_noncompleted_outcomes_are_not_generated_json(outcome):
    from api_agent.llm_providers.gpt.rich import validate_result
    from api_agent.providers.llm import LLMResult
    req = request()
    _, evidence = prepare(req)
    result = LLMResult(Message('assistant', (TextContent('partial or refusal'),)), outcome=outcome, response=evidence)
    assert validate_result(req, result) is result


def test_native_non_strict_body_preserves_open_nested_object():
    result, calls = run_gpt(request(shape=OPEN, strict=False), output='{"arguments":{"extra":1}}')
    definition = json.loads(calls[0].content)['text']['format']
    assert definition['strict'] is False and definition['schema'] == json.loads(OPEN)
    assert result.response.strict is False and result.response.actual_mode == 'structured'


def test_encoded_protected_json_is_rejected_after_decoding_with_evidence():
    with pytest.raises(ProviderAccessError) as caught:
        run_gpt(request('prompted_json'), output='{"answer":"synthetic\\u002dtoken"}')
    assert caught.value.response.actual_mode == 'prompted_json'
    assert caught.value.usage.output_tokens == 2


@pytest.mark.parametrize('outcome', ['refused', 'length'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_gpt_noncompleted_native_outcomes_keep_evidence_without_json_admission(outcome, mode):
    from api_agent.llm_providers.gpt.inference import complete_text
    sent = []
    req = request()
    async def handler(wire):
        sent.append(wire)
        if mode == 'responses':
            part = {'type': 'refusal', 'refusal': 'declined'} if outcome == 'refused' else {'type': 'output_text', 'text': 'partial'}
            body = {'status': 'completed' if outcome == 'refused' else 'incomplete',
                'output': [{'type': 'message', 'role': 'assistant', 'content': [part]}],
                'usage': {'input_tokens': 4, 'output_tokens': 2}}
            if outcome == 'length':
                body['incomplete_details'] = {'reason': 'max_output_tokens'}
        else:
            body = {'choices': [{'finish_reason': 'stop' if outcome == 'refused' else 'length',
                'message': {'role': 'assistant', 'content': None, 'refusal': 'declined'} if outcome == 'refused'
                    else {'role': 'assistant', 'content': 'partial'}}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}
        return httpx.Response(200, json=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await complete_text(client=client, base_url=BASE, access_token='synthetic-token',
                subscription=False, api_mode=mode, request=req)
    result = asyncio.run(run())
    assert result.outcome == outcome and result.response.actual_mode == 'structured' and len(sent) == 1


def test_structured_native_mapping_does_not_admit_temperature():
    from api_agent.llm_providers.gpt.rich import payload
    req = request()
    req = replace(req, payload=replace(req.payload, response=None, structured=StructuredOutput('shape', SHAPE),
        generation=GenerationOptions(temperature=0.5)))
    with pytest.raises(ProviderAccessError) as caught:
        payload(req, 'responses', False)
    assert caught.value.kind == 'unsupported'


@pytest.mark.parametrize('shape,output', [('{"type":"array","items":{"type":"string"}}', '["yes"]'),
    ('{"type":"string"}', '"yes"'), ('{"type":"integer"}', '7')])
@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json'])
def test_nonobject_roots_use_text_fallback_and_original_validation(shape, output, mode):
    req = request(mode, shape=shape)
    adapted, evidence = prepare(req)
    assert evidence.actual_mode == 'prompted_json' and adapted.payload.structured is None
    result, calls = run_gpt(req, output=output)
    assert result.response.actual_mode == 'prompted_json' and len(calls) == 1
    assert 'text' not in json.loads(calls[0].content)
    if mode != 'prompted_json':
        with pytest.raises(OperationalError):
            prepare(request(mode, 'reject', shape=shape))


@pytest.mark.parametrize('provider,api_mode', [('gpt', 'responses'), ('gpt', 'completions'),
    ('bedrock', 'responses'), ('bedrock', 'completions')])
def test_instruction_marker_points_to_effective_wire_item_after_tool_expansion(monkeypatch, provider, api_mode):
    from api_agent.providers.llm import ToolCall, ToolResult
    from api_agent.llm_providers.response_policy import capture_payload
    req = request('prompted_json', model='gpt-4o-mini' if provider == 'gpt' else 'openai.gpt-oss-20b',
        tools=(Tool('lookup', '', SHAPE),))
    req = replace(req, payload=replace(req.payload, messages=(*req.payload.messages,
        Message('assistant', (TextContent('Checking'),), (ToolCall('one', 'lookup', '{"answer":"yes"}'),)),
        Message('tool', results=(ToolResult('one', (TextContent('Found'),)),)))))
    if provider == 'gpt':
        from api_agent.llm_providers.gpt.inference import validate_text_request
        _, evidence = prepare(req, mode=api_mode)
        _, body = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False,
            api_mode=api_mode, request=req)
    else:
        from api_agent.llm_providers.bedrock.mantle import encode
        adapted, evidence = bedrock_prepare(req, api_mode)
        from api_agent.llm_providers.response_policy import format_payload
        body = format_payload(req, encode(adapted, api_mode), evidence)
    captured = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *a, **kw: captured.append(a[1]))
    capture_payload(req, body, evidence, auth='synthetic-token')
    container = 'input' if api_mode == 'responses' else 'messages'
    marker, = captured[0]['adapter_instructions']
    assert marker == {'role': 'user', 'path': f'/{container}/{len(body[container]) - 1}', 'purpose': 'response_policy'}
    assert body[container][-1]['content'].endswith(SHAPE)
    assert body[container][-1]['role'] == 'user'
    assert captured[0]['provider_payload'] is body


@pytest.mark.parametrize('api_mode', ['responses', 'completions'])
@pytest.mark.parametrize('mode', ['json', 'prompted_json'])
def test_json_instruction_requires_a_final_answer_without_mutating_authored_messages(api_mode, mode):
    from api_agent.llm_providers.response_policy import format_payload
    req = request(mode, model='openai.gpt-oss-20b')
    _, evidence = bedrock_prepare(req, api_mode)
    container = 'input' if api_mode == 'responses' else 'messages'
    authored = [{'role': 'user', 'content': 'Independently authored question'}]
    original = {container: authored}

    actual = format_payload(req, original, evidence)

    assert original == {container: authored}
    assert len(authored) == 1
    assert actual[container][:-1] == authored
    instruction = actual[container][-1]['content']
    assert 'complete final answer containing only JSON' in instruction
    assert 'code fences' not in instruction
    assert 'Include the final JSON answer even when reasoning is enabled' in instruction
    assert 'reasoning is not the answer' in instruction
    assert instruction.endswith(req.payload.response.schema.schema)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_completed_reasoning_only_response_has_an_explicit_terminal_error(asynchronous):
    from api_agent.llm_providers.bedrock.mantle import decode
    from api_agent.providers import rich_inference
    from api_agent.providers.inference_retry import retryable
    from api_agent.providers.setup_transport import _decode, _encode, _error, _raise_error
    req = request('prompted_json', model='openai.gpt-oss-20b')
    _, evidence = bedrock_prepare(req, 'responses')
    raw = {'model': req.selected_llm, 'status': 'completed', 'output': [
        {'id': 'synthetic-reasoning', 'type': 'reasoning', 'status': 'completed', 'summary': [],
         'content': [{'type': 'reasoning_text', 'text': 'Consider the question before answering.'}]}],
        'usage': {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10,
                  'output_tokens_details': {'reasoning_tokens': 2}}}
    result = replace(decode(raw, 'responses', selected_llm=req.selected_llm), response=evidence)
    assert ''.join(part.text for part in result.payload.content) == ''

    with pytest.raises(ProviderAccessError, match='no final-answer JSON') as caught:
        if asynchronous:
            asyncio.run(rich_inference.async_validate_result(req, result))
        else:
            rich_inference.validate_result(req, result)

    assert caught.value.code == 'empty_final_answer'
    assert caught.value.origin == 'local_output_validation'
    assert caught.value.usage == result.usage
    assert caught.value.response == evidence
    assert not retryable(caught.value)
    assert 'Consider the question' not in str(caught.value)
    with pytest.raises(ProviderAccessError) as restored:
        _raise_error(_decode(_encode(_error(caught.value))))
    assert restored.value.message == caught.value.message
    assert restored.value.code == caught.value.code
    assert restored.value.usage == result.usage
    assert restored.value.response == evidence


@pytest.mark.parametrize('nested', [{'not': {'type': 'string'}},
    {'type': 'object', 'additionalProperties': {'not': {'type': 'string'}}},
    {'type': 'array', 'items': {'type': 'object', 'additionalProperties': {'not': {'type': 'string'}}}}])
def test_schema_valued_additional_properties_cannot_hide_unsupported_native_constraints(nested):
    shape = json.dumps({'type': 'object', 'additionalProperties': nested})
    req = request(shape=shape, strict=False)
    adapted, evidence = prepare(req)
    assert evidence.actual_mode == 'json' and evidence.fallback_reason == 'unsupported_schema'
    assert adapted.payload.messages is req.payload.messages
    assert req.payload.response.schema.schema == shape and evidence.strict is False
    with pytest.raises(OperationalError):
        prepare(request(fallback='reject', shape=shape, strict=False))


def test_native_schema_walk_distinguishes_schema_locations_from_enum_data():
    shape = '{"type":"object","enum":[{"not":{"type":"string"}}],"additionalProperties":{"type":"string"}}'
    assert native_schema_supported(StructuredOutput('enum_values', shape, False))


@pytest.mark.parametrize('kind', ['deadline', 'cancelled', 'operational'])
def test_bedrock_postdispatch_control_failures_keep_choice_usage_and_cleanup(monkeypatch, kind):
    from api_agent.llm_providers.bedrock import factory as module
    from api_agent.providers.llm import Usage
    from api_agent.workflows import inference_capture
    entered = asyncio.Event()
    released = []
    chosen = []
    captured = []
    usage = Usage(3, 2)
    async def dispatch(*args, **kwargs):
        kwargs['on_dispatch']()
        entered.set()
        try:
            if kind == 'cancelled':
                await asyncio.Event().wait()
            error = TimeoutError('controlled deadline') if kind == 'deadline' else OperationalError('unavailable')
            error.usage = usage
            raise error
        except asyncio.CancelledError as error:
            error.usage = usage
            raise
        finally:
            released.append(True)
    transport = SimpleNamespace(call=dispatch)
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: transport)
    monkeypatch.setattr(inference_capture, 'capture_inference', lambda *args, **kwargs: captured.append(args[1]))
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        task = asyncio.create_task(provider.acomplete(request('json', model='zai.glm-5')))
        await entered.wait()
        if kind == 'cancelled':
            task.cancel()
        error_type = {'deadline': TimeoutError, 'cancelled': asyncio.CancelledError, 'operational': OperationalError}[kind]
        with pytest.raises(error_type) as caught:
            await task
        assert task.cancelled() == (kind == 'cancelled')
        assert caught.value.usage is usage
        assert caught.value.response.actual_mode == 'prompted_json'
    with inference_capture.bind_response_dispatch(chosen.append) as dispatch_state:
        asyncio.run(run())
    assert len(chosen) == 1 and chosen[0].actual_mode == 'prompted_json'
    assert dispatch_state['response'] is chosen[0]
    assert len(captured) == 1 and captured[0]['provider_payload']['model'] == 'zai.glm-5'
    assert released == [True]


def test_instruction_marker_follows_actual_responses_native_continuation_expansion(monkeypatch):
    from api_agent.llm_providers.gpt.inference import validate_text_request
    from api_agent.llm_providers.response_policy import capture_payload
    from api_agent.providers.llm import ToolCall, ToolResult
    from test_responses_native_metadata import native, TOOLS
    req = request('prompted_json', model='gpt-4o-mini', tools=TOOLS)
    continuation = json.dumps({'provider': 'openai.responses', 'state': native()['output']})
    req = replace(req, payload=replace(req.payload, messages=(*req.payload.messages,
        Message('assistant', (TextContent('Working'),), (ToolCall('call-1', 'lookup', '{}'),), continuation=continuation),
        Message('tool', results=(ToolResult('call-1', (TextContent('Found'),)),)))))
    _, evidence = prepare(req)
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False,
        api_mode='responses', request=req)
    captured = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *a, **kw: captured.append(a[1]))
    capture_payload(req, body, evidence, auth='synthetic-token')
    assert captured[0]['adapter_instructions'] == [{'role': 'user', 'path': '/input/6', 'purpose': 'response_policy'}]
    assert body['input'][6]['content'].endswith(SHAPE)
    assert body['input'][4]['type'] == 'function_call' and body['input'][5]['type'] == 'function_call_output'


@pytest.mark.parametrize('kind', ['deadline', 'cancelled', 'operational', 'transport'])
def test_gpt_postdispatch_control_failure_keeps_choice_reported_usage_and_cancellation(kind):
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.providers.llm import Usage
    from api_agent.workflows.inference_capture import bind_response_dispatch
    entered = asyncio.Event()
    released = []
    chosen = []
    usage = Usage(3, 2)
    async def handler(sent):
        entered.set()
        try:
            if kind == 'cancelled':
                await asyncio.Event().wait()
            error = {'deadline': lambda: TimeoutError('controlled deadline'),
                'operational': lambda: OperationalError('unavailable'),
                'transport': lambda: httpx.ReadError('controlled transport failure')}[kind]()
            error.usage = usage
            raise error
        except asyncio.CancelledError as error:
            error.usage = usage
            raise
        finally:
            released.append(True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(complete_text(client=client, base_url=BASE, access_token='synthetic-token',
                subscription=False, api_mode='responses', request=request('json')))
            await entered.wait()
            if kind == 'cancelled':
                task.cancel()
            with pytest.raises(asyncio.CancelledError if kind == 'cancelled' else OperationalError) as caught:
                await task
            assert task.cancelled() == (kind == 'cancelled')
            assert caught.value.usage == usage and caught.value.response.actual_mode == 'json'
    with bind_response_dispatch(chosen.append) as dispatch_state:
        asyncio.run(run())
    assert len(chosen) == 1 and chosen[0].actual_mode == 'json'
    assert dispatch_state['response'] is chosen[0]
    assert released == [True]


@pytest.mark.parametrize('provider', ['gpt', 'bedrock', 'bedrock_stream'])
@pytest.mark.parametrize('reported', [True, False])
@pytest.mark.parametrize('bound', [True, False])
def test_real_component_deadline_keeps_explicit_usage_before_timeout_rewrites_cancellation(monkeypatch, provider, reported, bound):
    from api_agent.providers.inference import error_usage
    from api_agent.providers.llm import Usage
    from api_agent.workflows.inference_capture import bind_response_dispatch
    usage = Usage(3, 2) if reported else None
    closed = []
    started = []
    async def stall(*args, **kwargs):
        if 'on_dispatch' in kwargs:
            kwargs['on_dispatch']()
        started.append(True)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            if usage is not None:
                error.usage = usage
            raise
        finally:
            closed.append(True)
    async def stream(*args, **kwargs):
        await stall(*args, **kwargs)
        yield {}
    async def run():
        from contextlib import nullcontext
        with (bind_response_dispatch() if bound else nullcontext(None)) as dispatch:
            if provider == 'gpt':
                from api_agent.llm_providers.gpt import inference
                monkeypatch.setattr(inference, '_TIMEOUT', 0.02)
                async with httpx.AsyncClient(transport=httpx.MockTransport(stall)) as client:
                    with pytest.raises(ProviderAccessError) as caught:
                        await inference.complete_text(client=client, base_url=BASE, access_token='synthetic-token',
                            subscription=False, api_mode='responses', request=request('json'))
                assert caught.value.kind == 'deadline'
            else:
                from api_agent.llm_providers.bedrock import factory as module
                monkeypatch.setattr(module, 'SDKTransport', lambda **kw: SimpleNamespace(call=stall, stream=stream))
                implementation = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
                implementation.inference_timeout = 0.02
                with pytest.raises(TimeoutError) as caught:
                    req = request('json', model='zai.glm-5')
                    if provider == 'bedrock_stream':
                        _ = [event async for event in implementation.astream(req)]
                    else:
                        await implementation.acomplete(req)
            assert error_usage(caught.value) == usage
            assert caught.value.response.actual_mode == ('json' if provider == 'gpt' else 'prompted_json')
            if dispatch is not None:
                assert dispatch['usage'] == usage
                assert dispatch['response'].actual_mode == ('json' if provider == 'gpt' else 'prompted_json')
    asyncio.run(run())
    assert started == [True] and closed == [True]
