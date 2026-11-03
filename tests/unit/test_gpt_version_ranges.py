"""Version floors carry eligibility forward; known exceptions and metadata narrow it."""
import pytest
from packaging.version import Version
from api_agent.llm_providers.gpt.model_versions import gpt_version
from api_agent.llm_providers.gpt.reasoning import reasoning_efforts
from api_agent.llm_providers.gpt.cache_policy import prompt_cache_fields

ROUTER = 'https://openrouter.ai/api/v1'
COMMON = ('off', 'low', 'medium', 'high')


@pytest.mark.parametrize('value', [None, 1, 'x' * 201, 'gpt-unknown', 'gpt-6-luna-2026-11-03',
    'gpt-6.1-luna-latest?', 'gpt-６.1-luna', 'openai/gpt-6.1-luna',
    'gpt-05.6-luna', 'gpt-6.01-sol', 'gpt-6.00-terra'])
def test_generation_parser_rejects_unclassified_identifiers(value):
    assert gpt_version(value) is None


@pytest.mark.parametrize('value,version,family', [('gpt-5.6-luna', '5.6', 'luna'),
    ('gpt-5.10.0-terra', '5.10', 'terra'), ('gpt-7', '7', None), ('gpt-6.1-sol-pro', '6.1', 'sol-pro')])
def test_generation_parser_preserves_numeric_order(value, version, family):
    assert gpt_version(value) == (Version(version), family)


@pytest.mark.parametrize('model', ['gpt-5.6-luna', 'gpt-5.6.0-terra', 'gpt-5.10-luna',
    'gpt-7.2-terra', 'gpt-5.6-sol', 'gpt-6-sol'])
@pytest.mark.parametrize('base,prefix', [('https://api.openai.com/v1', ''), (ROUTER, 'openai/')])
def test_reasoning_minimum_versions_carry_forward(model, base, prefix):
    assert reasoning_efforts(prefix + model, base_url=base) == COMMON


@pytest.mark.parametrize('model', ['gpt-6.1-sol', 'gpt-6.2-sol', 'gpt-9-sol', 'gpt-6-astra', 'gpt-8-astra'])
def test_later_known_effort_exception_is_inherited(model):
    assert reasoning_efforts('openai/' + model, base_url=ROUTER) == ('low', 'medium', 'high')


@pytest.mark.parametrize('model', ['gpt-5.5-luna', 'gpt-5.5.99-terra', 'gpt-5-astra', 'gpt-7-other'])
def test_older_or_other_families_do_not_inherit_reasoning(model):
    assert reasoning_efforts('openai/' + model, base_url=ROUTER) is None


def test_known_later_loss_of_reasoning_can_close_a_range(monkeypatch):
    from api_agent.llm_providers.gpt import reasoning
    monkeypatch.setitem(reasoning._FAMILY_RANGES, 'terra',
        ((Version('5.6'), COMMON), (Version('8.2'), ())))
    assert reasoning_efforts('openai/gpt-8.1-terra', base_url=ROUTER) == COMMON
    assert reasoning_efforts('openai/gpt-8.3-terra', base_url=ROUTER) == ()


@pytest.mark.parametrize('metadata,expected', [
    ({'supported_efforts': ['max', 'high', 'low'], 'mandatory': True}, ('low', 'high')),
    ({'supported_efforts': ['none', 'low', 'medium', 'high'], 'mandatory': True}, ('low', 'medium', 'high')),
    ({'supported_efforts': ['none', 'low'], 'mandatory': False}, ('off', 'low')),
    ({'supported_efforts': []}, ()),
])
def test_catalog_can_narrow_inherited_controls(metadata, expected):
    assert reasoning_efforts('openai/gpt-7-terra', base_url=ROUTER, metadata=metadata) == expected


@pytest.mark.parametrize('metadata', [None, [], {}, {'supported_efforts': 'low'},
    {'supported_efforts': ['low'], 'mandatory': 1}, {'supported_efforts': ['low', 'low']},
    {'supported_efforts': [1]}, {'supported_efforts': ['']},
    {'supported_efforts': ['x' * 33]}, {'supported_efforts': [str(x) for x in range(33)]}])
def test_malformed_catalog_reasoning_is_unknown(metadata):
    assert reasoning_efforts('openai/gpt-7-terra', base_url=ROUTER, metadata=metadata) is None


@pytest.mark.parametrize('model', ['openai/gpt-5.6-luna', 'openai/gpt-5.10-terra',
    'openai/gpt-7', 'openai/gpt-9-next-family', 'openai/gpt-6.2-sol-2026-11-03'])
@pytest.mark.parametrize('policy', ['off', 'medium', 'high'])
def test_documented_cache_floor_does_not_need_a_new_alias(model, policy):
    fields = prompt_cache_fields(policy, model, base_url=ROUTER, subscription=False)
    assert fields == ({'prompt_cache_options': {'mode': 'explicit'}} if policy == 'off'
                      else {'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}})


def test_future_catalog_reports_mandatory_reasoning_without_off():
    from api_agent.llm_providers.gpt.discovery import _catalog
    row, = _catalog({'data': [{'id': 'openai/gpt-7-terra', 'reasoning': {
        'mandatory': True, 'supported_efforts': ['low', 'high']}}]}, False, 'synthetic-token', base_url=ROUTER)
    assert row.reasoning_efforts == ('low', 'high')
    assert 'reasoning_effort' in row.features


@pytest.mark.parametrize('base', [ROUTER, 'https://api.openai.com/v1'])
def test_nonstring_reasoning_selection_stays_unknown(base):
    assert reasoning_efforts(None, base_url=base) is None


@pytest.mark.parametrize('model', ['gpt-5.6-luna', 'gpt-6.2-terra', 'gpt-8-sol'])
def test_official_cache_uses_its_own_later_generation_contract(model):
    assert prompt_cache_fields('medium', model, base_url='https://api.openai.com/v1',
        subscription=False) == {'prompt_cache_options': {'mode': 'implicit', 'ttl': '30m'}}


def test_catalog_metadata_does_not_restore_a_known_later_off_exception():
    assert reasoning_efforts('openai/gpt-7-sol', base_url=ROUTER,
        metadata={'supported_efforts': ['none', 'low', 'medium', 'high'], 'mandatory': False}) == ('low', 'medium', 'high')


@pytest.mark.parametrize('metadata,effort', [
    ({'mandatory': True, 'supported_efforts': ['low', 'high']}, 'off'),
    ({'mandatory': True, 'supported_efforts': ['high']}, 'low'),
    ({'supported_efforts': 'malformed'}, 'low'),
    ({'supported_efforts': []}, 'high'),
])
def test_shared_owner_without_ceiling_honors_observed_reasoning_catalog(monkeypatch, metadata, effort):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.llm_providers.gpt import discovery, factory
    from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent

    rows = discovery._catalog({'data': [{'id': 'openai/gpt-7-terra', 'reasoning': metadata}]},
        False, 'synthetic-token', base_url=ROUTER)
    monkeypatch.setattr(discovery, 'discover_selections', AsyncMock(return_value=rows))
    adapter = factory.GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': ROUTER, 'GPT_API_MODE': 'responses'})
    dispatched = []
    original_client = httpx.AsyncClient
    def transport(request):
        dispatched.append(request)
        raise AssertionError('Rejected effort reached inference transport')
    monkeypatch.setattr(factory.httpx, 'AsyncClient',
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    supplied = LLMRequest('openai/gpt-7-terra', CompletionRequest(
        (Message('user', (TextContent('Say Ok'),)),), generation=GenerationOptions(reasoning_effort=effort)))
    async def scenario():
        async def call(callback):
            return await callback(auth='synthetic-token')
        session = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('operation', 'identity', lambda credentials: session,
            describe=adapter.adescribe, complete=adapter.acomplete, validate_request=adapter.validate_request,
            supports_output_limit=True, max_attempts=1)
        try:
            with pytest.raises(ProviderAccessError, match='catalog does not admit'):
                await owner.complete(supplied)
        finally:
            await owner.aclose()
            await adapter.aclose()
    asyncio.run(scenario())
    assert dispatched == []


def test_catalog_restriction_is_scoped_to_the_described_model():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': ROUTER})
    adapter._reasoning_catalog = {'openai/gpt-7-sol': ('high',)}
    supplied = LLMRequest('openai/gpt-7-terra', CompletionRequest(
        (Message('user', (TextContent('Say Ok'),)),), generation=GenerationOptions(reasoning_effort='low')))
    adapter.validate_request(supplied)


@pytest.mark.parametrize('effort', [None, '', 'low', 'high'])
def test_observed_catalog_allows_supported_efforts_and_provider_default(effort):
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': ROUTER})
    adapter._reasoning_catalog = {'openai/gpt-7-terra': ('low', 'high')}
    supplied = LLMRequest('openai/gpt-7-terra', CompletionRequest(
        (Message('user', (TextContent('Say Ok'),)),), generation=GenerationOptions(reasoning_effort=effort)))
    adapter.validate_request(supplied)


def test_catalog_guard_preserves_controlled_sync_malformed_request_error():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers.contracts import ProviderAccessError
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': ROUTER})
    with pytest.raises(ProviderAccessError, match='Invalid response-policy request'):
        adapter.validate_request(object())


def test_catalog_guard_preserves_controlled_async_malformed_request_error():
    import asyncio
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers.contracts import ProviderAccessError
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': ROUTER})
    with pytest.raises(ProviderAccessError, match='Invalid response-policy request'):
        asyncio.run(adapter.acomplete(object(), auth='synthetic-token'))
