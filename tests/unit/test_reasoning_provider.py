"""Deterministic native effort mapping and admission checks."""
import pytest
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent, Tool
from api_agent.providers.contracts import ProviderAccessError
from api_agent.llm_providers.gpt.reasoning import reasoning_efforts, reasoning_fields
from api_agent.llm_providers.gpt.inference import validate_text_request
from api_agent.llm_providers.gpt.discovery import _catalog
from api_agent.llm_providers.gpt.factory import GPTProvider

BASE = 'https://api.openai.com/v1'
MODELS = ('gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra', 'gpt-6.1-sol')


def request(model='gpt-6-luna', effort=None, *, tools=False, temperature=None):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('Hello'),)),),
        tools=(Tool('lookup', 'Lookup', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),) if tools else (),
        generation=GenerationOptions(reasoning_effort=effort, temperature=temperature)))


@pytest.mark.parametrize('model,effort', [(model, effort) for model in MODELS for effort in (None, '', 'off', 'low', 'medium', 'high') if effort != 'off' or model not in MODELS[2:]])
@pytest.mark.parametrize('mode', ('responses', 'completions'))
def test_verified_native_mapping(model, mode, effort):
    req = request(model, effort)
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False, api_mode=mode, request=req)
    expected = {} if not effort else ({'reasoning': {'effort': 'none' if effort == 'off' else effort}} if mode == 'responses' else {'reasoning_effort': 'none' if effort == 'off' else effort})
    assert {key: body[key] for key in ('reasoning', 'reasoning_effort') if key in body} == expected
    assert body['model'] == model


@pytest.mark.parametrize('base,model', [('https://compatible.example/v1','gpt-6-luna'), ('https://openrouter.ai/api/v1','openai/gpt-unknown'), (BASE,'gpt-6-luna-2026-10-15'), (BASE,'gpt-unknown'), (BASE,'gpt-6.1-terra')])
def test_unknown_contract_rejects_explicit_effort(base, model):
    assert reasoning_efforts(model, base_url=base) is None
    with pytest.raises(ProviderAccessError, match='not verified'):
        reasoning_fields(request(model, 'low'), base_url=base, api_mode='responses')
    assert reasoning_fields(request(model, ''), base_url=base, api_mode='responses') == {}


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('effort', (None, '', 'low', 'medium', 'high'))
def test_chat_reasoning_tools_rejected(model, effort):
    with pytest.raises(ProviderAccessError, match='requires Responses'):
        reasoning_fields(request(model, effort, tools=True), base_url=BASE, api_mode='completions')


@pytest.mark.parametrize('model', MODELS[:2])
def test_chat_disabled_reasoning_tools_allowed(model):
    assert reasoning_fields(request(model, 'off', tools=True), base_url=BASE, api_mode='completions') == {'reasoning_effort':'none'}


def test_responses_reasoning_tools_preserved():
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False, api_mode='responses', request=request(effort='high', tools=True))
    assert body['reasoning'] == {'effort':'high'}
    assert body['tools'][0]['name'] == 'lookup'
    assert body['parallel_tool_calls'] is False


def test_temperature_rejected_with_reasoning():
    with pytest.raises(ProviderAccessError, match='temperature'):
        reasoning_fields(request(effort='high', temperature=1), base_url=BASE, api_mode='responses')


def test_catalog_exposes_verified_capability_only():
    selections = _catalog({'data':[{'id':model} for model in (*MODELS, 'gpt-unknown')]}, False, 'synthetic-token', base_url=BASE)
    assert selections[0].reasoning_efforts == ('off','low','medium','high')
    assert selections[2].reasoning_efforts == ('low','medium','high')
    assert selections[0].features == ('reasoning_effort', 'cache_policy')
    assert selections[-1].reasoning_efforts is None


def test_provider_default_and_explicit_empty_preflight():
    provider = GPTProvider({'GPT_AUTH_MODE':'api_key', 'LLM_REASONING_EFFORT':'off'})
    with pytest.raises(ProviderAccessError, match='not verified'):
        provider.validate_request(request('gpt-6-astra'))
    assert provider.validate_request(request('gpt-6-astra', '')) is None
    assert 'reasoning_effort' in provider.capabilities.features


@pytest.mark.parametrize('mode', ('responses','completions'))
@pytest.mark.parametrize('model,effort', [(model, effort) for model in MODELS for effort in ('off','low','medium','high') if effort != 'off' or model not in MODELS[2:]])
def test_openrouter_verified_mapping_requires_parameter_support(mode, model, effort):
    req = request('openai/' + model, effort)
    fields = reasoning_fields(req, base_url='https://openrouter.ai/api/v1', api_mode=mode)
    assert fields == {'reasoning':{'effort':'none' if effort == 'off' else effort},
                      'provider':{'require_parameters':True, 'allow_fallbacks':False}}


@pytest.mark.parametrize('mode', ('responses', 'completions'))
@pytest.mark.parametrize('effort', ('off', 'low', 'medium', 'high'))
def test_openrouter_terra_61_catalog_control_reaches_the_native_request(mode, effort):
    model = 'openai/gpt-6.1-terra'
    selection, = _catalog({'data': [{'id': model}]}, False, 'synthetic-token',
                         base_url='https://openrouter.ai/api/v1')
    assert selection.reasoning_efforts == ('off', 'low', 'medium', 'high')
    selection.validate(request(model, effort))
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='synthetic-token', subscription=False, api_mode=mode,
        request=request(model, effort))
    assert body['reasoning']['effort'] == ('none' if effort == 'off' else effort)
    assert body['provider'] == {'require_parameters': True, 'allow_fallbacks': False}


@pytest.mark.parametrize('effort', (None, '', 'off', 'low', 'medium', 'high'))
def test_quote_records_requested_effort(effort):
    from api_agent.llm_providers.gpt.pricing import quote
    from api_agent.providers.llm import LLMResult
    result = LLMResult(Message('assistant',(TextContent('Hello'),)))
    accounting = quote(request(effort=effort), result, direct=False, subscription=False)['accounting']
    assert accounting['reasoning_effort'] == (effort or None)
    assert accounting['actual_model'] is None


def test_subscription_explicit_reasoning_preserves_transport_shape():
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=True,
        api_mode='responses', request=request(effort='high'))
    assert body['reasoning'] == {'effort':'high'}
    assert body['stream'] is True
    assert body['store'] is False
    assert 'max_output_tokens' not in body


def test_provider_rejects_before_http_client_creation(monkeypatch):
    import asyncio
    from api_agent.llm_providers.gpt import factory as factory_module
    def unexpected_client(**kwargs):
        pytest.fail('HTTP client created for unsupported reasoning')
    monkeypatch.setattr(factory_module.httpx, 'AsyncClient', unexpected_client)
    provider = GPTProvider({'GPT_AUTH_MODE':'api_key'})
    with pytest.raises(ProviderAccessError, match='not verified'):
        asyncio.run(provider.acomplete(request('gpt-6-astra','off'),auth='synthetic-token'))


@pytest.mark.parametrize('model', MODELS[2:])
@pytest.mark.parametrize('base,prefix', [(BASE,''), ('https://openrouter.ai/api/v1','openai/')])
def test_mandatory_reasoning_rejects_off(model, base, prefix):
    with pytest.raises(ProviderAccessError, match='not verified'):
        reasoning_fields(request(prefix + model, 'off'), base_url=base, api_mode='responses')


@pytest.mark.parametrize('mode', ('responses','completions'))
def test_openrouter_transport_retains_effort_and_routing_guard(mode):
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1', access_token='synthetic-token',
        subscription=False, api_mode=mode, request=request('openai/gpt-6-luna','off'))
    assert body['reasoning'] == {'effort':'none'}
    assert body['provider'] == {'require_parameters':True, 'allow_fallbacks':False}
    assert body['model'] == 'openai/gpt-6-luna'


def test_factory_quote_applies_default_and_explicit_empty():
    from api_agent.providers.llm import LLMResult
    provider = GPTProvider({'GPT_AUTH_MODE':'api_key', 'LLM_REASONING_EFFORT':'high'})
    result = LLMResult(Message('assistant',(TextContent('Hello'),)))
    assert provider.quote_cost(request(), result)['accounting']['reasoning_effort'] == 'high'
    assert provider.quote_cost(request(effort=''), result)['accounting']['reasoning_effort'] is None


def test_openrouter_catalog_advertises_only_verified_aliases():
    rows = [{'id':'openai/' + model} for model in MODELS]
    selections = _catalog({'data':rows}, False, 'synthetic-token', base_url='https://openrouter.ai/api/v1')
    assert [row.reasoning_efforts for row in selections] == [
        ('off','low','medium','high'), ('off','low','medium','high'),
        ('low','medium','high'), ('low','medium','high')]


def test_compatible_catalog_keeps_capability_unknown():
    selection, = _catalog({'data':[{'id':'gpt-6-luna'}]}, False, 'synthetic-token', base_url='https://compatible.example/v1')
    assert selection.reasoning_efforts is None
    assert selection.features == ()


@pytest.mark.parametrize('model', ('gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'))
@pytest.mark.parametrize('effort', (None, '', 'off', 'low', 'medium', 'high'))
@pytest.mark.parametrize('mode', ('responses', 'completions'))
def test_56_official_wire_and_catalog(model, effort, mode):
    selection, = _catalog({'data': [{'id': model}]}, False,
        'synthetic-token', base_url=BASE)
    assert selection.reasoning_efforts == ('off', 'low', 'medium', 'high')
    assert selection.features == ('reasoning_effort', 'cache_policy')
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token',
        subscription=False, api_mode=mode, request=request(selection.selected_llm, effort))
    assert body['model'] == model
    expected = {} if not effort else ({'reasoning': {'effort': 'none' if effort == 'off' else effort}}
        if mode == 'responses' else {'reasoning_effort': 'none' if effort == 'off' else effort})
    assert {key: body[key] for key in ('reasoning', 'reasoning_effort') if key in body} == expected


@pytest.mark.parametrize('base,model', [
    (BASE, 'gpt-5.6-luna-2026-10-18'),
    (BASE, 'gpt-5.6-luna-latest'),
    (BASE, 'gpt-5.6-terra-2026-10-18'),
    (BASE, 'gpt-5.6-sol-latest'),
    ('https://openrouter.ai/api/v1', 'openai/gpt-5.6-luna'),
    ('https://openrouter.ai/api/v1', 'openai/gpt-5.6-terra'),
    ('https://openrouter.ai/api/v1', 'openai/gpt-5.6-sol'),
    ('https://compatible.example/v1', 'gpt-5.6-luna'),
    ('https://compatible.example/v1', 'gpt-5.6-terra'),
    ('https://compatible.example/v1', 'gpt-5.6-sol'),
])
def test_56_unverified_routes_and_versions(base, model):
    selection, = _catalog({'data': [{'id': model}]}, False, 'synthetic-token', base_url=base)
    assert selection.reasoning_efforts is None
    assert 'reasoning_effort' not in selection.features
    with pytest.raises(ProviderAccessError, match='not verified'):
        reasoning_fields(request(model, 'off'), base_url=base, api_mode='responses')
    assert reasoning_fields(request(model, ''), base_url=base, api_mode='responses') == {}


@pytest.mark.parametrize('model', ('gpt-5.6-terra', 'gpt-5.6-sol'))
@pytest.mark.parametrize('effort', (None, '', 'off', 'high'))
def test_56_new_effort_capability_does_not_change_chat_tool_route(model, effort):
    _, body = validate_text_request(base_url=BASE, access_token='synthetic-token',
        subscription=False, api_mode='completions', request=request(model, effort, tools=True))
    assert body['model'] == model
    assert body['tools'][0]['function']['name'] == 'lookup'
    if effort:
        assert body['reasoning_effort'] == ('none' if effort == 'off' else effort)
    else:
        assert 'reasoning_effort' not in body


@pytest.mark.parametrize('effort', (None, '', 'high'))
def test_56_luna_existing_chat_tool_restriction_is_unchanged(effort):
    with pytest.raises(ProviderAccessError, match='requires Responses'):
        reasoning_fields(request('gpt-5.6-luna', effort, tools=True),
            base_url=BASE, api_mode='completions')
    assert reasoning_fields(request('gpt-5.6-luna', 'off', tools=True),
        base_url=BASE, api_mode='completions') == {'reasoning_effort': 'none'}


def test_openrouter_catalog_exposes_output_limit_only_when_advertised():
    rows = [{'id': 'openai/gpt-6-luna', 'supported_parameters': ['max_tokens'],
             'top_provider': {'max_completion_tokens': 16384}},
            {'id': 'openai/gpt-6-sol', 'supported_parameters': ['max_tokens']}]
    luna, sol = _catalog({'data': rows}, False, 'synthetic-token',
                         base_url='https://openrouter.ai/api/v1')
    assert 'max_output_tokens' in luna.features
    assert luna.max_output_tokens == 16384
    assert 'max_output_tokens' not in sol.features


@pytest.mark.parametrize('model', ('openai/gpt-6.1-sol', 'openai/gpt-6.1-terra', 'openai/gpt-oss-20b'))
@pytest.mark.parametrize('parameter', ('max_tokens', 'max_completion_tokens'))
@pytest.mark.parametrize('mode,field', (('responses', 'max_output_tokens'), ('completions', 'max_completion_tokens')))
def test_advertised_openrouter_ceiling_replaces_small_text_fallback(model, parameter, mode, field):
    from api_agent.providers.output_limits import clamp_request
    selection, = _catalog({'data': [{'id': model, 'supported_parameters': [parameter],
        'top_provider': {'max_completion_tokens': 128000}}]}, False,
        'synthetic-token', base_url='https://openrouter.ai/api/v1')
    prepared = clamp_request(request(model), selection, 200000)
    assert prepared.payload.generation.max_output_tokens == 128000
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='synthetic-token', subscription=False, api_mode=mode, request=prepared)
    assert body[field] == 128000
    assert body['provider']['require_parameters'] is True
    bounded = LLMRequest(model, CompletionRequest((Message('user', (TextContent('Hello'),)),),
        generation=GenerationOptions(max_output_tokens=4096)))
    assert clamp_request(bounded, selection, 200000).payload.generation.max_output_tokens == 4096
    if model == 'openai/gpt-oss-20b':
        assert selection.reasoning_efforts == ('low', 'medium', 'high')


@pytest.mark.parametrize('base, supported', [
    ('https://compatible.example/v1', ['max_tokens']),
    ('https://openrouter.ai/api/v1', None),
    ('https://openrouter.ai/api/v1', 'max_tokens'),
])
def test_output_limit_is_not_inferred_without_openrouter_parameter_list(base, supported):
    selection, = _catalog({'data': [{'id': 'openai/gpt-6-luna', 'supported_parameters': supported}]},
                          False, 'synthetic-token', base_url=base)
    assert 'max_output_tokens' not in selection.features


def test_subscription_catalog_does_not_advertise_output_limit():
    selection, = _catalog({'models': [{'slug': 'gpt-6-luna', 'display_name': 'Luna',
                                       'visibility': 'list', 'supported_parameters': ['max_tokens']}]},
                          True, 'synthetic-token', base_url='https://openrouter.ai/api/v1')
    assert 'max_output_tokens' not in selection.features


@pytest.mark.parametrize('mode,key', [('responses', 'max_output_tokens'),
                                      ('completions', 'max_completion_tokens')])
def test_openrouter_explicit_output_cap_requires_parameter_support_without_effort(mode, key):
    req = LLMRequest('openai/gpt-6-luna', CompletionRequest(
        (Message('user', (TextContent('Hello'),)),),
        generation=GenerationOptions(max_output_tokens=4096)))
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='synthetic-token', subscription=False, api_mode=mode, request=req)
    assert body[key] == 4096
    assert body['provider'] == {'require_parameters': True, 'allow_fallbacks': False}
    assert 'reasoning' not in body


def test_openrouter_capped_effort_retains_reasoning_and_strict_routing():
    req = LLMRequest('openai/gpt-6-luna', CompletionRequest(
        (Message('user', (TextContent('Hello'),)),),
        generation=GenerationOptions(max_output_tokens=4096, reasoning_effort='high')))
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='synthetic-token', subscription=False, api_mode='responses', request=req)
    assert body['provider'] == {'require_parameters': True, 'allow_fallbacks': False}
    assert body['reasoning'] == {'effort': 'high'}


@pytest.mark.parametrize('mode', ('responses', 'completions'))
@pytest.mark.parametrize('model', ('openai/gpt-oss-20b', 'openai/gpt-oss-120b'))
@pytest.mark.parametrize('effort', ('low', 'medium', 'high'))
def test_openrouter_oss_catalog_effort_reaches_native_request(mode, model, effort):
    selection, = _catalog({'data': [{'id': model, 'reasoning': {
        'mandatory': True, 'supported_efforts': ['high', 'medium', 'low']}}]},
        False, 'synthetic-token', base_url='https://openrouter.ai/api/v1')
    assert selection.reasoning_efforts == ('low', 'medium', 'high')
    selection.validate(request(model, effort))
    _, body = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='synthetic-token', subscription=False, api_mode=mode,
        request=request(model, effort))
    assert body['reasoning'] == {'effort': effort}
    assert body['provider'] == {'require_parameters': True, 'allow_fallbacks': False}


@pytest.mark.parametrize('model', ('openai/gpt-oss-20b', 'openai/gpt-oss-120b'))
@pytest.mark.parametrize('effort', ('off',))
def test_openrouter_oss_rejects_unqualified_effort(model, effort):
    with pytest.raises(ProviderAccessError, match='not verified'):
        reasoning_fields(request(model, effort), base_url='https://openrouter.ai/api/v1',
                         api_mode='responses')


@pytest.mark.parametrize('model', ('gpt-oss-20b', 'openai/gpt-oss-20b:free',
                                 'openai/gpt-oss-120b-unknown'))
def test_oss_qualification_does_not_expand_to_other_routes_or_aliases(model):
    assert reasoning_efforts(model, base_url='https://openrouter.ai/api/v1') is None
    assert reasoning_efforts(model, base_url=BASE) is None


@pytest.mark.parametrize('model', ('openai/gpt-oss-20b', 'openai/gpt-oss-120b'))
def test_openrouter_oss_display_qualification_is_responses_only(model):
    from api_agent.llm_providers.gpt.reasoning import display_reasoning
    assert reasoning_efforts(model, base_url=BASE) is None
    assert display_reasoning(model, base_url='https://openrouter.ai/api/v1', api_mode='responses')
    assert not display_reasoning(model, base_url='https://openrouter.ai/api/v1', api_mode='completions')
