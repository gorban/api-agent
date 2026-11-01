"""Qualified native response mechanisms preserve exact routes and schemas."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock.response_policy import prepare_response
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.llm import (CompletionRequest, GenerationOptions, LLMRequest,
    Message, ResponsePolicy, StructuredOutput, TextContent, Tool)

MODELS = ('us.openai.gpt-5.6-luna', 'us.openai.gpt-6-luna')
SCHEMA = '{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}'
ROOT = Path(__file__).resolve().parents[2]


def request(model=MODELS[0], *, effort='low', tools=(), shape=SCHEMA, fallback='reject'):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('Synthetic question'),)),),
        tools=tools, generation=GenerationOptions(max_output_tokens=4096, reasoning_effort=effort),
        response=ResponsePolicy('structured', fallback, StructuredOutput('answer', shape))), 'request-one')


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('effort', [None, 'low', 'high'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('with_tools', [False, True])
def test_qualified_native_combinations_preserve_all_request_fields(model, effort, stream, with_tools):
    tools = (Tool('lookup', 'Synthetic tool', SCHEMA, strict=False),) if with_tools else ()
    original = request(model, effort=effort, tools=tools)

    prepared, evidence = prepare_response(original, 'responses', endpoint='runtime', stream=stream)

    assert evidence.actual_mode == 'structured' and evidence.fallback_reason is None
    assert prepared.payload.structured is original.payload.response.schema
    assert prepared.payload.generation is original.payload.generation
    assert prepared.payload.messages is original.payload.messages
    assert prepared.payload.tools is tools
    assert (prepared.selected_llm, prepared.request_id) == (model, 'request-one')


@pytest.mark.parametrize('model,api,endpoint,effort,tools', [
    ('global.openai.gpt-6-luna', 'responses', 'runtime', 'low', ()),
    ('us.openai.gpt-6.1-luna', 'responses', 'runtime', 'low', ()),
    ('us.openai.gpt-6-sol', 'responses', 'runtime', 'low', ()),
    (MODELS[0], 'completions', 'runtime', 'low', ()),
    ('openai.gpt-oss-20b', 'responses', 'mantle', 'low', ()),
    (MODELS[0], 'responses', 'runtime', 'medium', ()),
    (MODELS[0], 'responses', 'runtime', 'low', (Tool('strict_lookup', '', SCHEMA),)),
])
def test_adjacent_unqualified_combinations_keep_declared_fallback(model, api, endpoint, effort, tools):
    original = request(model, effort=effort, tools=tools, fallback='allow')
    prepared, evidence = prepare_response(original, api, endpoint=endpoint, stream=True)
    assert evidence.actual_mode == 'prompted_json'
    assert prepared.payload.structured is None
    assert prepared.payload.tools == tools
    with pytest.raises(OperationalError):
        prepare_response(request(model, effort=effort, tools=tools), api, endpoint=endpoint, stream=True)


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('effort', ['low', 'high'])
def test_real_reviewer_is_native_and_planner_open_object_keeps_fallback(model, effort):
    reviewer = (ROOT / 'demo/simple/schemas/review_response.json').read_text()
    planner = (ROOT / 'demo/simple/schemas/plan_output.json').read_text()
    original = request(model, effort=effort, shape=planner, fallback='allow')

    planned, plan_policy = prepare_response(original, 'responses', endpoint='runtime')
    reviewed, review_policy = prepare_response(request(model, effort=effort, shape=reviewer), 'responses', endpoint='runtime')

    assert plan_policy.actual_mode == 'prompted_json'
    assert plan_policy.fallback_reason == 'unsupported_schema'
    assert planned.payload.structured is None
    assert json.loads(original.payload.response.schema.schema)['properties']['arguments'] == {'type': 'object'}
    assert review_policy.actual_mode == 'structured'
    assert json.loads(reviewed.payload.structured.schema) == json.loads(reviewer)


def controlled_provider(monkeypatch, responses, *, authenticated=False):
    from api_agent.llm_providers.bedrock import factory as module
    from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream
    from test_bedrock_responses_continuation import stream_events
    pending = iter(responses)
    dispatched = []
    async def call(mode, payload, **kwargs):
        dispatched.append(payload)
        if authenticated:
            await kwargs['auth']()
        kwargs['on_dispatch']()
        return next(pending)
    async def events(mode, payload, **kwargs):
        dispatched.append(payload)
        if authenticated:
            await kwargs['auth']()
        kwargs['on_dispatch']()
        raw = next(pending)
        decoder = WorkerStream('responses', raw['model'], set(), endpoint='runtime')
        source = stream_events(raw['output'])
        source[0]['response']['id'] = raw['id']
        source[-1]['response'] = raw
        for event in source:
            for frame in decoder.feed(event):
                yield frame
        for frame in decoder.finish():
            yield frame
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: SimpleNamespace(call=call, stream=events))
    provider = module.factory({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'responses'}, {})
    return provider, dispatched


def native_result(model, output, *, identity='response-one'):
    return {'id': identity, 'created_at': 0, 'object': 'response', 'model': model,
            'status': 'completed', 'output': output,
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}


def native_message(value):
    return {'id': 'message-one', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': json.dumps(value), 'annotations': []}]}


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('invalid', [False, True])
def test_factory_sends_native_reviewer_and_keeps_original_validation(monkeypatch, model, stream, invalid):
    schema_text = (ROOT / 'demo/simple/schemas/review_response.json').read_text()
    verdict = dict(correct=True, grounded=True, relevant=True, preference=True)
    if invalid:
        verdict['unexpected'] = True
    text = json.dumps(verdict)
    raw = {'id': 'response-one', 'created_at': 0, 'object': 'response', 'model': model,
        'status': 'completed', 'output': [{'id': 'message-one', 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
        'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}
    provider, dispatched = controlled_provider(monkeypatch, [raw])
    original = request(model, shape=schema_text)
    async def run():
        if stream:
            return [event async for event in provider.astream(original)][-1].payload
        return await provider.acomplete(original)

    if invalid:
        with pytest.raises(ProviderAccessError) as caught:
            asyncio.run(run())
        assert caught.value.usage.input_tokens == 3
        assert caught.value.response.actual_mode == 'structured'
    else:
        result = asyncio.run(run())
        assert result.response.actual_mode == 'structured'
        assert result.usage.input_tokens == 3
    assert len(dispatched) == 1
    body = dispatched[0]['body']
    assert body['text']['format']['schema'] == json.loads(schema_text)
    assert body['reasoning'] == {'effort': 'low'}
    assert len(body['input']) == 1


@pytest.mark.parametrize('model', MODELS)
def test_legacy_native_stream_preflight_matches_policy_admission(monkeypatch, model):
    from api_agent.llm_providers.bedrock import factory as module
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: object())
    provider = module.factory({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'responses'}, {})
    value = request(model)
    legacy = replace(value, payload=replace(value.payload, response=None, structured=value.payload.response.schema))
    provider.validate_request(legacy, stream=True)
    with pytest.raises(OperationalError):
        provider.validate_request(replace(legacy, selected_llm='global.openai.gpt-6-luna'), stream=True)


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('stream', [False, True])
def test_native_schema_tool_continuation_preserves_request_and_opaque_state(monkeypatch, model, stream):
    from api_agent.providers.llm import ToolResult
    native = [
        {'type': 'reasoning', 'id': 'reason-one', 'status': 'completed', 'summary': [],
         'content': [], 'encrypted_content': 'opaque-synthetic-state'},
        {'type': 'function_call', 'id': 'function-one', 'call_id': 'call-one', 'name': 'lookup',
         'arguments': '{"value":"query"}', 'status': 'completed'}]
    provider, dispatched = controlled_provider(monkeypatch, [native_result(model, native),
        native_result(model, [native_message({'value': 'found'})], identity='response-two')], authenticated=True)
    original = request(model, tools=(Tool('lookup', '', SCHEMA, strict=False),))
    async def frozen_auth():
        return SimpleNamespace(worker_config=lambda: {'mode': 'temporary', 'region': 'us-east-1',
            'access_key': 'synthetic-key', 'secret_key': 'synthetic-secret', 'session_token': 'synthetic-session'})
    async def complete(value):
        if stream:
            return [event async for event in provider.astream(value, auth=frozen_auth)][-1].payload
        return await provider.acomplete(value, auth=frozen_auth)
    async def run():
        first = await complete(original)
        assert first.outcome == 'tool_calls' and first.response.actual_mode == 'structured'
        followup = replace(original, request_id='request-two', payload=replace(original.payload,
            messages=original.payload.messages + (first.payload,
                Message('tool', results=(ToolResult('call-one', (TextContent('found'),)),)))))
        final = await complete(followup)
        assert final.outcome == 'completed' and final.response.actual_mode == 'structured'
        assert json.loads(final.payload.content[0].text) == {'value': 'found'}
    asyncio.run(run())
    assert len(dispatched) == 2
    body = dispatched[1]['body']
    assert body['input'][1:3] == native
    assert body['input'][3] == {'type': 'function_call_output', 'call_id': 'call-one', 'output': 'found'}
    assert body['text']['format']['schema'] == json.loads(SCHEMA)
    assert body['tools'][0]['strict'] is False


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('invalid', [False, True])
def test_existing_strict_function_arguments_are_locally_validated(monkeypatch, model, invalid):
    arguments = {'value': 'answer'}
    if invalid:
        arguments['unexpected'] = True
    native = [{'type': 'function_call', 'id': 'function-one', 'call_id': 'call-one', 'name': 'emit_result',
               'arguments': json.dumps(arguments), 'status': 'completed'}]
    provider, dispatched = controlled_provider(monkeypatch, [native_result(model, native)])
    original = request(model, tools=(Tool('emit_result', '', SCHEMA, strict=True),))
    original = replace(original, payload=replace(original.payload, response=None))
    if invalid:
        with pytest.raises(ProviderAccessError) as caught:
            asyncio.run(provider.acomplete(original))
        assert caught.value.code == 'tool_arguments_schema'
        assert caught.value.usage.input_tokens == 3
    else:
        result = asyncio.run(provider.acomplete(original))
        assert result.outcome == 'tool_calls' and result.response is None
        assert json.loads(result.payload.calls[0].arguments) == arguments
    assert len(dispatched) == 1
    assert dispatched[0]['body']['tools'][0]['strict'] is True
    assert dispatched[0]['body']['tools'][0]['parameters'] == json.loads(SCHEMA)
