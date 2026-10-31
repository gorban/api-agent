"""Converse codecs preserve tool history and reject unsupported semantics."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from api_agent.llm_providers.bedrock import converse as c
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.llm import (LLMRequest, CompletionRequest, Message, TextContent,
    Tool, ToolCall, ToolResult, GenerationOptions, StructuredOutput, ResponsePolicy, CacheBoundary)
from api_agent.llm_providers.bedrock.response_policy import prepare_response

MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
SCHEMA = '{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}'


def request(**changes):
    return LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),), **changes))


def response():
    return {'output': {'message': {'role': 'assistant', 'content': [{'text': 'hello'}]}},
            'stopReason': 'end_turn', 'usage': {'inputTokens': 2, 'outputTokens': 3, 'totalTokens': 5}}


def test_encode_system_tools_results_and_role_merging():
    req = LLMRequest(MODEL, CompletionRequest((Message('system', (TextContent('rules'),)),
        Message('user', (TextContent('question'),)), Message('user', (TextContent('details'),)),
        Message('assistant', calls=(ToolCall('c', 'lookup', '{"value":"x"}'),)),
        Message('tool', results=(ToolResult('c', (TextContent('answer'),)),))),
        tools=(Tool('lookup', 'Find value', SCHEMA),),
        generation=GenerationOptions(max_output_tokens=50, temperature=.5)))
    body = c.encode(req)
    assert body['modelId'] == MODEL
    assert body['system'] == [{'text': 'rules'}]
    assert [row['role'] for row in body['messages']] == ['user', 'assistant', 'user']
    assert body['messages'][0]['content'] == [{'text': 'question'}, {'text': 'details'}]
    assert body['messages'][1]['content'][0]['toolUse']['input'] == {'value': 'x'}
    assert body['messages'][2]['content'][0]['toolResult']['toolUseId'] == 'c'
    assert body['toolConfig']['tools'][0]['toolSpec']['inputSchema']['json'] == json.loads(SCHEMA)
    assert body['inferenceConfig'] == {'maxTokens': 50, 'temperature': .5}


def test_encode_minimal_omits_optional_fields_and_empty_blocks():
    req = replace(request(), payload=CompletionRequest((Message('user', (TextContent('hi'),)),)))
    body = c.encode(req)
    assert set(body) == {'modelId', 'messages', 'inferenceConfig'}
    assert body['messages'] == [{'role': 'user', 'content': [{'text': 'hi'}]}]
    assert body['inferenceConfig']['maxTokens'] == 1024


@pytest.mark.parametrize('req', [None, request(generation=GenerationOptions(temperature=1.1)),
    request(generation=GenerationOptions(cache_policy='off')),
    LLMRequest(MODEL, CompletionRequest((Message('assistant', continuation='opaque'),))),
    LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('a'),), cache_boundary=CacheBoundary()),)))])
def test_unsupported_request_rejects_before_encoding(req):
    with pytest.raises((ProviderAccessError, OperationalError)):
        c.encode(req)


def test_invalid_authored_wire_has_safe_failure(monkeypatch):
    def reject(*args, **kwargs):
        raise ContractError('unsafe detail')
    monkeypatch.setattr(c, 'request_to_wire', reject)
    with pytest.raises(ProviderAccessError, match='Invalid Bedrock Converse request'):
        c.encode(request())


def test_converse_output_fallback_preserves_instruction_in_native_content():
    schema_text = SCHEMA.replace('"type":"string"', '"type":"string","minLength":1')
    req = request(response=ResponsePolicy('structured', 'allow', StructuredOutput('answer', schema_text)))
    prepared, evidence = prepare_response(req, 'converse', endpoint='runtime')
    body = c.encode(prepared)
    formatted = c.format_payload(req, body, evidence)
    assert len(body['messages']) == 1
    assert formatted['messages'][-1]['content'][0]['text'].endswith(schema_text)
    assert evidence.actual_mode == 'prompted_json'
    assert c.format_payload(request(), body, None) is body


@pytest.mark.parametrize('reason,outcome', [('end_turn','completed'), ('stop_sequence','completed'),
    ('max_tokens','length'), ('model_context_window_exceeded','length'), ('content_filtered','refused'), ('guardrail_intervened','refused')])
def test_decode_outcomes_and_unknown_metadata(reason, outcome):
    raw = response()
    raw['stopReason'] = reason
    result = c.decode(raw, selected_llm=MODEL)
    assert result.outcome == outcome
    assert result.payload.content[0].text == 'hello'
    assert result.actual_model is None
    assert result.usage.input_tokens == 2
    assert result.usage.cached_input_tokens is None


def test_decode_tool_and_cache_categories():
    raw = response()
    raw['output']['message']['content'] = [{'toolUse': {'toolUseId': 'c', 'name': 'lookup', 'input': {'q': 'x'}}}]
    raw['stopReason'] = 'tool_use'
    raw['usage'].update(cacheReadInputTokens=10, cacheWriteInputTokens=20)
    raw['serviceTier'] = {'type': 'default'}
    result = c.decode(raw, selected_llm=MODEL)
    assert result.outcome == 'tool_calls'
    assert result.payload.calls[0].arguments == '{"q": "x"}'
    assert result.usage.input_tokens == 32
    assert result.usage.cached_input_tokens == 10 and result.usage.cache_write_tokens == 20
    assert result.usage.reasoning_tokens is None
    assert result.service_tier == 'default'


@pytest.mark.parametrize('change', [
    lambda r: r.update(extra='unknown'),
    lambda r: r.update(output={}),
    lambda r: r['output']['message'].update(role='user'),
    lambda r: r['output']['message'].update(content=[{'reasoningContent': {}}]),
    lambda r: r['output']['message'].update(content=[{'text':'x','toolUse':{}}]),
    lambda r: r.update(stopReason='unknown'),
    lambda r: r.update(stopReason='tool_use'),
    lambda r: r.update(serviceTier={'wrong':'value'}),
    lambda r: r['usage'].update(inputTokens=True),
    lambda r: r['usage'].update(totalTokens='9'),
    lambda r: r['usage'].update(cacheReadInputTokens=-1),
    lambda r: r.update(usage=None),
])
def test_decode_malformed_response_retains_valid_usage(change):
    raw = response()
    change(raw)
    with pytest.raises(ProviderAccessError) as caught:
        c.decode(raw, selected_llm=MODEL)
    assert caught.value.execution == 'possibly_executed'
    assert 'hello' not in str(caught.value)


@pytest.mark.parametrize('tool', [
    {'toolUseId':'c','name':'lookup','input':[]},
    {'toolUseId':'c','name':'lookup','input':{},'type':'other'},
])
def test_invalid_tool_response_rejects(tool):
    raw = response()
    raw['stopReason'] = 'tool_use'
    raw['output']['message']['content'] = [{'toolUse':tool}]
    with pytest.raises(ProviderAccessError):
        c.decode(raw, selected_llm=MODEL)


def test_duplicate_call_identity_rejects():
    raw = response()
    raw['stopReason'] = 'tool_use'
    tool = {'toolUse': {'toolUseId':'c','name':'lookup','input':{}}}
    raw['output']['message']['content'] = [tool, deepcopy(tool)]
    with pytest.raises(ProviderAccessError):
        c.decode(raw, selected_llm=MODEL)


def test_native_schema_uses_converse_output_config():
    req = request(structured=StructuredOutput('answer', SCHEMA))
    body = c.encode(req)
    assert body['outputConfig'] == {'textFormat': {'type': 'json_schema', 'structure': {
        'jsonSchema': {'name': 'answer', 'schema': SCHEMA}}}}


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_native_schema_carrier_and_stream_policy(mode):
    from api_agent.llm_providers.bedrock import mantle
    model = 'us.openai.gpt-5.6-luna'
    req = replace(request(response=ResponsePolicy('structured', 'allow', StructuredOutput('answer', SCHEMA))), selected_llm=model)
    prepared, evidence = prepare_response(req, mode, endpoint='runtime')
    assert evidence.actual_mode == 'structured'
    body = mantle.encode(prepared, mode, endpoint='runtime')
    definition = body['text']['format'] if mode == 'responses' else body['response_format']['json_schema']
    assert definition['schema'] == json.loads(SCHEMA) and definition['strict'] is True
    prepared_stream, stream_evidence = prepare_response(req, mode, endpoint='runtime', stream=True)
    assert prepared_stream.payload.structured is None
    assert stream_evidence.actual_mode == 'prompted_json'
    assert stream_evidence.fallback_reason == 'unsupported_combination'


def test_runtime_native_stream_reject_policy_fails_before_dispatch():
    req = replace(request(response=ResponsePolicy('structured', 'reject', StructuredOutput('answer', SCHEMA))),
                  selected_llm='us.openai.gpt-5.6-luna')
    with pytest.raises(OperationalError, match='unsupported'):
        prepare_response(req, 'responses', endpoint='runtime', stream=True)


def test_converse_stream_uses_validated_nonstream_native_schema():
    req = request(response=ResponsePolicy('structured', 'reject', StructuredOutput('answer', SCHEMA)))
    prepared, evidence = prepare_response(req, 'converse', endpoint='runtime', stream=True)
    assert evidence.actual_mode == 'structured'
    assert c.encode(prepared)['outputConfig']['textFormat']['type'] == 'json_schema'


@pytest.mark.parametrize('reported,accepted', [('openai.gpt-5.6-luna', True),
    ('us.openai.gpt-5.6-luna', True), ('openai.gpt-6-luna', False),
    ('global.openai.gpt-5.6-luna', False)])
def test_reported_runtime_model_must_identify_same_selected_profile(reported, accepted):
    from api_agent.llm_providers.bedrock import mantle
    model = 'us.openai.gpt-5.6-luna'
    raw = {'model':reported, 'status':'completed','output':[],
           'usage':{'input_tokens':1,'output_tokens':2,'total_tokens':3}}
    if accepted:
        assert mantle.decode(raw, 'responses', selected_llm=model, endpoint='runtime').actual_model == reported
    else:
        with pytest.raises(ProviderAccessError):
            mantle.decode(raw, 'responses', selected_llm=model, endpoint='runtime')


def test_empty_blocks_do_not_create_empty_converse_messages():
    messages = []
    c._append(messages, 'assistant', [])
    assert messages == []


def test_usage_aggregates_disjoint_counts_without_assuming_total_cache_convention():
    raw = response()
    raw['usage'].update(cacheReadInputTokens=10, cacheWriteInputTokens=20, totalTokens=35)
    result = c.decode(raw, selected_llm=MODEL)
    assert result.usage.input_tokens == 32 and result.usage.output_tokens == 3


@pytest.mark.parametrize('reason', ['malformed_model_output', 'malformed_tool_use'])
def test_provider_malformed_stop_keeps_usage_and_never_reports_success(reason):
    raw = response()
    raw['stopReason'] = reason
    with pytest.raises(ProviderAccessError) as caught:
        c.decode(raw, selected_llm=MODEL)
    assert caught.value.usage.input_tokens == 2 and caught.value.usage.output_tokens == 3


@pytest.mark.parametrize('strict', [True, False])
def test_older_claude_strict_tools_are_not_silently_weakened(strict):
    req = replace(request(tools=(Tool('lookup', 'Find value', SCHEMA, strict=strict),)),
                  selected_llm='us.anthropic.claude-opus-4-1-20250805-v1:0')
    if strict:
        with pytest.raises(ProviderAccessError, match='strict tools'):
            c.encode(req)
    else:
        assert 'strict' not in c.encode(req)['toolConfig']['tools'][0]['toolSpec']
