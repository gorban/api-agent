"""Synthetic transport proofs for optional schemas and ignored cache hints."""
import asyncio
import json

import httpx
import pytest

from api_agent.llm_providers.gpt import inference, rich
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import (CacheBoundary, CompletionRequest, GenerationOptions,
    LLMRequest, Message, StructuredOutput, TextContent, Tool, Usage)

TOKEN = 'synthetic-optional-authentication'
DEFINITION = {'type': 'object', '$defs': {'name': {'type': 'string'}},
    'properties': {'value': {'$ref': '#/$defs/name'},
                   'optional': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}},
    'required': ['value'], 'additionalProperties': False}
SCHEMA = json.dumps(DEFINITION)
STRICT = json.dumps({'type': 'object', 'properties': {'value': {'type': 'string'}},
                     'required': ['value'], 'additionalProperties': False})


def request(kind, *, strict=False, boundary=None, maximum=None):
    messages = (Message('user', (TextContent('Answer the question'),), cache_boundary=boundary),)
    options = {'tools': (Tool('lookup', 'Lookup', STRICT if strict else SCHEMA, strict=strict),)} if kind == 'tool' else {
        'structured': StructuredOutput('answer', STRICT if strict else SCHEMA, strict=strict)}
    return LLMRequest('gpt-synthetic', CompletionRequest(messages,
        generation=GenerationOptions(max_output_tokens=maximum), **options))


def native(mode, kind, text):
    usage = {'input_tokens': 9, 'output_tokens': 3, 'input_tokens_details': {'cached_tokens': 4}}
    if mode == 'responses':
        output = ([dict(type='function_call', status='completed', call_id='call-1', name='lookup', arguments=text)]
                  if kind == 'tool' else [dict(type='message', role='assistant', content=[dict(type='output_text', text=text)])])
        return dict(status='completed', output=output, usage=usage)
    message = (dict(role='assistant', tool_calls=[dict(id='call-1', type='function', function=dict(name='lookup', arguments=text))])
               if kind == 'tool' else dict(role='assistant', content=text))
    return dict(choices=[dict(finish_reason='tool_calls' if kind == 'tool' else 'stop', message=message)],
                usage=dict(prompt_tokens=9, completion_tokens=3, prompt_tokens_details=dict(cached_tokens=4)))


def complete(req, mode, kind, text, *, subscription=False):
    sent = []
    async def execute():
        data = native(mode, kind, text)
        def handler(outbound):
            sent.append(outbound)
            if subscription:
                return httpx.Response(200, text='data: '+json.dumps(dict(type='response.completed', response=data))+'\n\n',
                                      headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token=TOKEN, subscription=subscription, api_mode=mode, request=req)
            assert not client.is_closed
            return result
    return asyncio.run(execute()), sent


@pytest.mark.parametrize('mode,subscription', [('responses', False), ('completions', False), ('responses', True)])
@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_optional_schema_survives_transport_and_local_validation(mode, subscription, kind):
    result, sent = complete(request(kind), mode, kind, '{"value":"answer"}', subscription=subscription)
    body = json.loads(sent[0].content)
    if kind == 'tool':
        definition = body['tools'][0] if mode == 'responses' else body['tools'][0]['function']
        assert definition['parameters'] == DEFINITION
    else:
        definition = body['text']['format'] if mode == 'responses' else body['response_format']['json_schema']
        assert definition['schema'] == DEFINITION
    assert definition['strict'] is False
    assert result.usage == Usage(9, 3, 4)
    assert result.applied_cache_boundaries == ()
    assert sent[0].headers['authorization'] == 'Bearer '+TOKEN
    assert TOKEN.encode() not in sent[0].content


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('kind', ['tool', 'structured'])
@pytest.mark.parametrize('text', ['{"value":4}', '{"value":"ok","optional":"wrong"}', '{"optional":1}'])
def test_invalid_nonstrict_output_preserves_usage(mode, kind, text):
    with pytest.raises(ProviderAccessError) as caught:
        complete(request(kind), mode, kind, text)
    assert caught.value.usage == Usage(9, 3, 4)
    assert 'outside its declared contract' in str(caught.value)
    assert TOKEN not in str(vars(caught.value))


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_strict_default_mapping_remains_true(mode, kind):
    req = request(kind, strict=True)
    body = rich.payload(req, mode, False)
    definition = ((body['tools'][0] if mode == 'responses' else body['tools'][0]['function']) if kind == 'tool' else
                  (body['text']['format'] if mode == 'responses' else body['response_format']['json_schema']))
    assert definition['strict'] is True


@pytest.mark.parametrize('mode,subscription', [('responses', False), ('completions', False), ('responses', True)])
@pytest.mark.parametrize('lifetime', [300, 3600])
@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_unsupported_boundary_preserves_entire_prompt_and_usage(mode, subscription, lifetime, kind):
    plain = request(kind)
    hinted = request(kind, boundary=CacheBoundary(lifetime))
    assert rich.payload(hinted, mode, subscription) == rich.payload(plain, mode, subscription)
    result, sent = complete(hinted, mode, kind, '{"value":"ok"}', subscription=subscription)
    assert json.loads(sent[0].content) == rich.payload(plain, mode, subscription)
    assert result.applied_cache_boundaries == ()
    assert result.usage.cached_input_tokens == 4


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_large_output_budget_is_forwarded_without_clipping(mode):
    result, sent = complete(request('structured', maximum=131072), mode, 'structured', '{"value":"ok"}')
    body = json.loads(sent[0].content)
    assert body['max_output_tokens' if mode == 'responses' else 'max_completion_tokens'] == 131072
    assert result.usage.output_tokens == 3


@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_remote_schema_rejected_before_transport(kind):
    req = request(kind)
    bad = json.dumps({'type': 'object', 'properties': {'value': {'$ref': 'https://schema.example/value'}}})
    payload = CompletionRequest(req.payload.messages, tools=(Tool('lookup', 'Lookup', bad, strict=False),)) if kind == 'tool' else CompletionRequest(req.payload.messages, structured=StructuredOutput('answer', bad, strict=False))
    with pytest.raises(ProviderAccessError) as caught:
        complete(LLMRequest('gpt-synthetic', payload), 'responses', kind, '{"value":"ok"}')
    assert caught.value.kind == 'invalid_request'


def test_schema_default_is_strict():
    assert Tool('lookup', 'Lookup', STRICT).strict is True
    assert StructuredOutput('answer', STRICT).strict is True


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('lifetime', [300, 3600])
def test_plain_text_ignores_boundaries_and_accepts_native_budget(mode, lifetime):
    options = GenerationOptions(max_output_tokens=131072)
    plain = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Answer the question'),)),), generation=options))
    hinted = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Answer the question'),), cache_boundary=CacheBoundary(lifetime)),), generation=options))
    assert inference._payload(hinted, mode, False) == inference._payload(plain, mode, False)
    result, sent = complete(hinted, mode, 'structured', 'Answer')
    body = json.loads(sent[0].content)
    assert body == inference._payload(plain, mode, False)
    assert body['max_output_tokens' if mode == 'responses' else 'max_completion_tokens'] == 131072
    assert result.applied_cache_boundaries == ()
    assert result.usage == Usage(9, 3, 4)


def test_stream_owned_response_closes_when_local_validation_fails():
    class TrackedStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            data = native('responses', 'structured', '{"value":4}')
            yield ('data: '+json.dumps(dict(type='response.completed', response=data))+'\n\n').encode()
        async def aclose(self):
            self.closed = True
    stream = TrackedStream()
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            stream=stream, headers={'content-type': 'text/event-stream'}))) as client:
            return await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token=TOKEN, subscription=True, api_mode='responses', request=request('structured'))
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(execute())
    assert caught.value.usage == Usage(9, 3, 4)
    assert stream.closed is True


@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_subscription_output_budget_rejected_before_transport(kind):
    with pytest.raises(ProviderAccessError) as caught:
        complete(request(kind, maximum=131072), 'responses', kind, '{"value":"ok"}', subscription=True)
    assert caught.value.kind == 'unsupported'
    assert 'max_output_tokens' in str(caught.value)


@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_wire_output_budget_rejected_before_transport(kind):
    with pytest.raises(ProviderAccessError) as caught:
        complete(request(kind, maximum=1048577), 'responses', kind, '{"value":"ok"}')
    assert caught.value.kind == 'invalid_request'


@pytest.mark.parametrize('identity', [42, 'bad', '12345678-1234-1234-1234-123456789ABC'])
def test_invalid_cache_session_identity_fails_before_transport(identity):
    async def execute():
        def forbidden(req):
            pytest.fail('Invalid identity reached HTTP transport')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            return await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token=TOKEN, subscription=True, api_mode='responses', request=request('structured'),
                cache_session_id=identity)
    with pytest.raises(ProviderAccessError, match='canonical UUID') as caught:
        asyncio.run(execute())
    assert caught.value.kind == 'invalid_request'


def test_tool_output_rejects_authentication_split_between_text_parts():
    data = native('responses', 'tool', '{"value":"ok"}')
    midpoint = len(TOKEN)//2
    data['output'].append(dict(type='message', role='assistant', content=[
        dict(type='output_text', text=TOKEN[:midpoint]), dict(type='output_text', text=TOKEN[midpoint:])]))
    with pytest.raises(ProviderAccessError) as caught:
        rich.tool_result(data, True, TOKEN)
    assert TOKEN not in str(vars(caught.value))


@pytest.mark.parametrize('composition', ['allOf', 'anyOf', 'oneOf'])
@pytest.mark.parametrize('kind', ['tool', 'structured'])
def test_root_composition_is_preserved_and_validated(composition, kind):
    member = {key: value for key, value in DEFINITION.items() if key != '$defs'}
    definition = {composition: [member], '$defs': DEFINITION['$defs']}
    source = json.dumps(definition)
    messages = (Message('user', (TextContent('Answer'),)),)
    payload = (CompletionRequest(messages, tools=(Tool('lookup', 'Lookup', source, strict=False),)) if kind == 'tool' else
               CompletionRequest(messages, structured=StructuredOutput('answer', source, strict=False)))
    req = LLMRequest('gpt-synthetic', payload)
    body = rich.payload(req, 'responses', False)
    mapped = body['tools'][0]['parameters'] if kind == 'tool' else body['text']['format']['schema']
    assert mapped == definition
    result, _ = complete(req, 'responses', kind, '{"value":"ok"}')
    assert result.usage == Usage(9, 3, 4)
