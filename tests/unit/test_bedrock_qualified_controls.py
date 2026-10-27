"""Synthetic requests exercise qualified controls without inference or auth."""
import json
import pytest

from api_agent.llm_providers.bedrock import catalog, mantle
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent


def request(model, effort):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('hello'),)),),
        generation=GenerationOptions(reasoning_effort=effort)))


@pytest.mark.parametrize('model', ['openai.gpt-oss-20b', 'openai.gpt-oss-120b'])
@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_native_categorical_effort(model, mode, effort):
    selected = catalog.selection(model, mode)
    assert selected.reasoning_efforts == ('low', 'medium', 'high')
    assert 'reasoning_effort' in selected.features
    body = mantle.encode(request(model, effort), mode)
    expected = {'effort': effort} if mode == 'responses' else effort
    assert body['reasoning' if mode == 'responses' else 'reasoning_effort'] == expected
    assert ('reasoning_effort' if mode == 'responses' else 'reasoning') not in body


@pytest.mark.parametrize('model', sorted(catalog.MODELS))
@pytest.mark.parametrize('effort', [None, ''])
def test_default_leaves_native_setting_unspecified(model, effort):
    body = mantle.encode(request(model, effort), 'completions')
    assert 'reasoning' not in body and 'reasoning_effort' not in body


@pytest.mark.parametrize('model', ['zai.glm-5', 'zai.glm-4.7', 'zai.glm-4.7-flash'])
@pytest.mark.parametrize('effort', ['off', 'low', 'medium', 'high'])
def test_unqualified_binary_model_does_not_advertise_intensity(model, effort):
    selected = catalog.selection(model)
    assert selected.reasoning_efforts is None
    assert 'reasoning_effort' not in selected.features
    with pytest.raises(ProviderAccessError):
        mantle.validate_request(request(model, effort))


@pytest.mark.parametrize('model', ['openai.gpt-oss-20b', 'openai.gpt-oss-120b'])
def test_always_reasoning_model_refuses_off(model):
    with pytest.raises(ProviderAccessError):
        mantle.encode(request(model, 'off'), 'completions')


@pytest.mark.parametrize('model, mode', [('openai.unknown', 'completions'),
    ('zai.glm-5', 'responses'), ('openai.gpt-oss-20b', 'unknown')])
def test_unqualified_model_or_route_fails_before_encoding(model, mode):
    with pytest.raises(ContractError):
        mantle.encode(request(model, None), mode)


@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_worker_forwards_qualified_effort_without_sdk_ownership_changes(mode, effort):
    from types import SimpleNamespace
    from api_agent.llm_providers.bedrock import worker

    seen, closed, emitted = [], [], []
    def create(**arguments):
        seen.append(arguments)
        return SimpleNamespace(model_dump=lambda **kwargs: {})
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create), close=lambda: closed.append('client'))
    http = SimpleNamespace(close=lambda: closed.append('http'))
    body = mantle.encode(request('openai.gpt-oss-20b', effort), mode)
    worker.execute({'timeout': 240, 'operation': mode, 'payload': {'prefix': '/v1', 'body': body},
        'auth': {'mode': 'api_key', 'api_key': 'synthetic', 'region': 'us-east-1'}}, emitted.append,
        client_factory=lambda *args: client, http_factory=lambda **kw: http)
    assert seen == [body]
    assert emitted == [{'result': {}}]
    assert closed == ['client', 'http']


@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('model,prefix,value', [
    ('openai.gpt-oss-20b', '/v1', 'off'), ('openai.gpt-oss-20b', '/v1', []),
    ('zai.glm-5', '/v1', 'high'), ('unknown', '/v1', 'high'),
    ('openai.gpt-oss-20b', '/openai/v1', 'high'),
])
def test_worker_rejects_unqualified_effort_before_auth(mode, model, prefix, value):
    from api_agent.llm_providers.bedrock import worker

    body = {'model': model, 'reasoning' if mode == 'responses' else 'reasoning_effort':
        {'effort': value} if mode == 'responses' else value}
    emitted = []
    def forbidden(*args, **kwargs):
        pytest.fail('invalid control reached authentication or transport')
    worker.execute({'timeout': 240, 'operation': mode, 'payload': {'prefix': prefix, 'body': body},
        'auth': {'mode': 'api_key', 'api_key': 'synthetic', 'region': 'us-east-1'}},
        emitted.append, session_factory=forbidden, client_factory=forbidden, http_factory=forbidden)
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('value', [None, {}, {'effort': 'high', 'summary': 'auto'}])
def test_worker_rejects_malformed_reasoning_object(value):
    from api_agent.llm_providers.bedrock import worker

    emitted = []
    worker.execute({'timeout': 240, 'operation': 'responses', 'payload': {'prefix': '/v1', 'body': {
        'model': 'openai.gpt-oss-20b', 'reasoning': value}}, 'auth': {}}, emitted.append)
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


def test_worker_session_without_credentials_stops_before_transport():
    from types import SimpleNamespace
    from api_agent.llm_providers.bedrock import worker

    emitted = []
    worker.execute({'timeout': 240, 'operation': 'models', 'payload': {}, 'auth': {'mode': 'session'}}, emitted.append,
        session_factory=lambda **kwargs: SimpleNamespace(region_name='us-east-1', get_credentials=lambda: None))
    assert emitted == [{'error': 'authentication', 'execution': 'not_started'}]


SCHEMA = '{"type":"object","properties":{"answer":{"type":"integer"}},"required":["answer"],"additionalProperties":false}'

NATIVE_SCHEMA = {'type':'json_schema', 'json_schema':{'name':'answer', 'schema':json.loads(SCHEMA), 'strict':True}}


def structured(model='zai.glm-5', *, definition=SCHEMA, strict=True):
    from api_agent.providers.llm import StructuredOutput
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('BANANA'),)),),
        structured=StructuredOutput('answer', definition, strict)))


def test_glm5_qualified_schema_is_native_and_locally_validated():
    import json
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.providers.rich_inference import validate_result

    req = structured()
    assert 'structured' in catalog.selection(req.selected_llm).features
    body = mantle.encode(req, 'completions')
    assert body['response_format'] == {'type': 'json_schema', 'json_schema': {
        'name': 'answer', 'schema': json.loads(SCHEMA), 'strict': True}}
    result = LLMResult(Message('assistant', (TextContent('{"answer":7}'),)), Usage(2, 3), 'completed')
    assert validate_result(req, result) == result
    with pytest.raises(ProviderAccessError):
        validate_result(req, LLMResult(Message('assistant', (TextContent('BANANA'),)), Usage(2, 3), 'completed'))


@pytest.mark.parametrize('model,mode', [('zai.glm-4.7', 'completions'), ('zai.glm-4.7-flash', 'completions'),
    ('openai.gpt-oss-20b', 'completions'), ('openai.gpt-oss-120b', 'responses'), ('zai.glm-5', 'responses')])
def test_unqualified_schema_routes_are_refused(model, mode):
    with pytest.raises((ProviderAccessError, ContractError)):
        mantle.encode(structured(model), mode)


def test_non_strict_glm5_schema_is_refused():
    with pytest.raises(ProviderAccessError):
        mantle.validate_request(structured(strict=False))


@pytest.mark.parametrize('definition', ['{"type":"object","properties":{"a":{"$ref":"https://invalid.example/schema"}},"required":["a"],"additionalProperties":false}',
    '{"type":"object","properties":{},"required":[],"additionalProperties":true}'])
def test_glm5_invalid_schema_is_rejected_before_encoding(definition):
    with pytest.raises(ProviderAccessError):
        mantle.validate_request(structured(definition=definition))


@pytest.mark.parametrize('stream', [False, True])
def test_worker_qualified_schema_survives_actual_sdk_serialization(stream):
    import json
    import httpx2
    from api_agent.llm_providers.bedrock import worker

    seen, emitted = [], []
    body = mantle.encode(structured(), 'completions')
    def handle(req):
        seen.append(json.loads(req.content))
        if stream:
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'}, content='data: [DONE]\n\n')
        return httpx2.Response(200, json={'id':'c','object':'chat.completion','created':0,'model':'zai.glm-5','choices':[]})
    worker.execute({'timeout': 240, 'operation': 'completions_stream' if stream else 'completions',
        'payload': {'prefix': '/v1', 'body': body},
        'auth': {'mode': 'api_key', 'api_key': 'synthetic', 'region': 'us-east-1'}}, emitted.append,
        http_factory=lambda **kw: httpx2.Client(transport=httpx2.MockTransport(handle), timeout=30))
    assert len(seen) == 1
    assert seen[0]['response_format'] == body['response_format']
    assert not any('error' in row for row in emitted)


@pytest.mark.parametrize('model,prefix,value', [
    ('zai.glm-5', '/v1', None), ('zai.glm-5', '/v1', {}),
    ('openai.gpt-oss-20b', '/v1', NATIVE_SCHEMA),
    ('zai.glm-5', '/openai/v1', NATIVE_SCHEMA),
    ('zai.glm-5', '/v1', dict(NATIVE_SCHEMA, type='json_object')),
    ('zai.glm-5', '/v1', {'type':'json_schema', 'json_schema':None}),
    ('zai.glm-5', '/v1', {'type':'json_schema', 'json_schema':{'name':'answer','schema':json.loads(SCHEMA),'strict':False}}),
    ('zai.glm-5', '/v1', {'type':'json_schema', 'json_schema':{'name':42,'schema':json.loads(SCHEMA),'strict':True}}),
    ('zai.glm-5', '/v1', {'type':'json_schema', 'json_schema':{'name':'bad name','schema':json.loads(SCHEMA),'strict':True}}),
    ('zai.glm-5', '/v1', {'type':'json_schema', 'json_schema':{'name':'answer','schema':{'type':'invalid'},'strict':True}}),
])
def test_worker_invalid_native_schema_stops_before_effects(model, prefix, value):
    from api_agent.llm_providers.bedrock import worker

    emitted = []
    def forbidden(*args, **kwargs):
        pytest.fail('invalid schema reached auth or transport')
    worker.execute({'timeout': 240, 'operation':'completions', 'payload':{'prefix':prefix, 'body':{'model':model,'response_format':value}},
        'auth':{'mode':'api_key','api_key':'synthetic','region':'us-east-1'}}, emitted.append,
        session_factory=forbidden, http_factory=forbidden, client_factory=forbidden)
    assert emitted == [{'error':'unknown','execution':'not_started'}]


@pytest.mark.parametrize('kind', ['effort', 'structured'])
@pytest.mark.parametrize('stream', [False, True])
def test_ordinary_provider_view_admits_qualified_controls(monkeypatch, kind, stream):
    import asyncio
    from types import SimpleNamespace
    from api_agent.llm_providers.bedrock import factory
    from api_agent.providers.loading import _StreamView
    from api_agent.providers.cache_policy import validate_provider_request

    req = structured() if kind == 'structured' else request('openai.gpt-oss-20b', 'low')
    answer = '{"answer":7}' if kind == 'structured' else 'hello'
    usage = {'prompt_tokens':2, 'completion_tokens':3, 'total_tokens':5}
    raw = {'model':req.selected_llm,'choices':[{'index':0,'message':{'role':'assistant','content':answer},
        'finish_reason':'stop'}], 'usage':usage}
    calls = []
    async def call(operation, payload, **kwargs):
        calls.append((operation, payload))
        return raw
    async def chunks(operation, payload, **kwargs):
        calls.append((operation, payload))
        yield {'id':'c','object':'chat.completion.chunk','model':req.selected_llm,'choices':[
            {'index':0,'delta':{'role':'assistant','content':answer},'finish_reason':'stop'}]}
        yield {'id':'c','object':'chat.completion.chunk','model':req.selected_llm,'choices':[], 'usage':usage}
    transport = SimpleNamespace(call=call, stream=chunks)
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kwargs: transport)
    provider = factory.factory({'BEDROCK_API_MODE':'completions'}, {})
    validate_provider_request(provider, req)
    view = _StreamView(provider)
    async def run():
        if stream:
            events = [event async for event in view.astream(req)]
            result = events[-1].payload
        else:
            result = await view.acomplete(req)
        assert result.payload.content == (TextContent(answer),)
    asyncio.run(run())
    assert calls == [('completions_stream' if stream else 'completions',
        {'prefix':'/v1', 'body':mantle.encode(req,'completions')})]
