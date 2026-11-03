"""Runtime discovery preserves IDs; native schema qualification remains explicit."""
import re
import tomllib
from pathlib import Path

import pytest

from api_agent.llm_providers.bedrock import auth, catalog, pricing
from api_agent.providers.contracts import ContractError


@pytest.mark.parametrize('identifier, family, version, title', [
    ('openai.gpt-5.6-luna', 'gpt-luna', '5.6', 'GPT 5.6 Luna'),
    ('openai.gpt-6-luna', 'gpt-luna', '6', 'GPT 6 Luna'),
    ('openai.gpt-6.1-terra', 'gpt-terra', '6.1', 'GPT 6.1 Terra'),
    ('openai.gpt-6-sol-2026-10-31', 'gpt-sol', '6', 'GPT 6 Sol'),
    ('eu.openai.gpt-5.6-luna', 'gpt-luna-eu', '5.6', 'GPT 5.6 Luna [EU]'),
    ('moon.openai.gpt-8-nova', 'gpt-nova-moon', '8', 'GPT 8 Nova [MOON]'),
])
@pytest.mark.parametrize('mode', ['responses', 'completions', 'converse'])
def test_discovered_gpt_display_metadata_does_not_grant_capabilities(identifier, family, version, title, mode):
    row = catalog.selection(identifier, mode, 'runtime')
    assert (row.family, row.version, row.display_name) == (family, version, title)
    assert row.selected_llm == identifier
    assert row.context_length is None and row.max_output_tokens is None
    assert 'structured' not in row.features
    assert catalog.route(identifier, mode, 'runtime') == ('/converse' if mode == 'converse' else '/openai/v1')


@pytest.mark.parametrize('identifier', ['other.future-model-v1', 'openai.gpt-oss-20b',
                                        'openai.gpt-', 'vendor.gpt-5.6-luna',
                                        'openai.gpt-6-luna-us', 'openai.gpt-8-nova-moon',
                                        'direct.openai.gpt-8-oss'])
def test_unrecognized_display_identity_stays_exact(identifier):
    row = catalog.selection(identifier, 'responses', 'runtime')
    assert (row.display_name, row.family, row.version) == (identifier, identifier, None)


def test_bedrock_regex_filter_and_latest_bare_gpt_version():
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.contracts import ProviderAccessError
    rows = tuple(catalog.selection(identifier, 'responses', 'runtime') for identifier in
                 ('openai.gpt-5.6-luna', 'openai.gpt-6-luna', 'openai.gpt-6.1-luna',
                  'openai.gpt-6.1-terra', 'us.openai.gpt-6-luna'))
    old = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': r'(?:openai/)?gpt-\d+(?:\.\d+)*-luna',
                                'LLM_DEFAULT_MODEL': r'(?:openai/)?gpt-6(?:\.\d+)*-luna'})
    with pytest.raises(ProviderAccessError, match='matches no allowed model'):
        old.select_default(rows)
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': r'openai\.gpt-\d+(?:\.\d+)*-(?:luna|terra|sol)',
                                   'LLM_DEFAULT_MODEL': r'openai\.gpt-6(?:\.\d+)*-luna'})
    assert policy.select_default(rows) == 'openai.gpt-6.1-luna'
    assert [row.selected_llm for row in policy.filter(rows)] == [row.selected_llm for row in rows[:-1]]


@pytest.mark.parametrize('scoped, compound', [
    ('us.openai.gpt-5.6-luna', 'openai.gpt-6-luna-us'),
    ('moon.openai.gpt-8-nova', 'openai.gpt-9-nova-moon'),
])
def test_display_parser_does_not_merge_scope_with_compound_flavor(scoped, compound):
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.contracts import ProviderAccessError
    rows = tuple(catalog.selection(identifier, 'responses', 'runtime') for identifier in (scoped, compound))
    assert rows[0].family != rows[1].family
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '.*', 'LLM_DEFAULT_MODEL': '.*'})
    with pytest.raises(ProviderAccessError, match='one model family'):
        policy.select_default(rows)


def test_display_scope_does_not_merge_with_existing_direct_oss_family():
    direct = catalog.selection('openai.gpt-oss-20b-1:0', 'responses', 'runtime')
    scoped = catalog.selection('direct.openai.gpt-8-oss', 'responses', 'runtime')
    assert direct.family != scoped.family
    assert scoped.selected_llm == 'direct.openai.gpt-8-oss'


@pytest.mark.parametrize('identifier', [
    'us.openai.gpt-5.6-luna', 'global.openai.gpt-5.6-terra',
    'in.openai.gpt-5.6-luna', 'us.openai.gpt-5.6-sol',
    'global.openai.gpt-6-luna', 'us.openai.gpt-6-sol',
    'global.openai.gpt-6.1-terra', 'us.openai.gpt-6.1-sol',
])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_openai_profiles_are_exact_and_route_to_runtime(identifier, mode):
    row = catalog.selection(identifier, mode, 'runtime')
    assert row.selected_llm == identifier
    assert 'structured' in row.features
    assert row.family.rsplit('-', 1)[0] in {'gpt-luna', 'gpt-terra', 'gpt-sol'}
    assert row.family.endswith('-' + identifier.split('.', 1)[0])
    assert row.version in {'5.6', '6', '6.1'}
    assert catalog.route(identifier, mode, 'runtime') == '/openai/v1'
    assert catalog.catalog({'data': [{'id': identifier}]}, mode, 'runtime') == (row,)


@pytest.mark.parametrize('identifier', [
    'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    'global.anthropic.claude-sonnet-4-6',
    'anthropic.claude-opus-4-6-v1',
])
def test_runtime_claude_known_metadata_and_schema_qualification(identifier):
    assert catalog.route(identifier, 'converse', 'runtime') == '/converse'
    selected = catalog.selection(identifier, 'converse', 'runtime')
    assert selected.selected_llm == identifier and 'structured' in selected.features
    assert selected.family.rsplit('-', 1)[0] in {'claude-haiku', 'claude-sonnet', 'claude-opus'}
    assert selected.version in {'4.5', '4.6'}
    assert 'temperature' in selected.features
    assert catalog.selection(identifier, 'responses', 'runtime').selected_llm == identifier
    assert 'structured' not in catalog.selection(identifier, 'responses', 'runtime').features


def test_runtime_discovery_preserves_all_ids_without_inventing_schema_support():
    accepted = 'us.openai.gpt-5.6-luna'
    rows = [{'id': accepted}, {'id': 'openai.gpt-5.6-luna'}, {'id': 'eu.openai.gpt-5.6-luna'},
            {'id': 'us.openai.gpt-5.6-new'}, {'id': 'global.anthropic.claude-sonnet-4-6'}]
    result = catalog.catalog({'data': rows}, 'responses', 'runtime')
    assert [row.selected_llm for row in result] == [row['id'] for row in rows]
    assert all('structured' not in row.features for row in result[1:])
    assert result[1].context_length is None and result[1].max_output_tokens is None
    with pytest.raises(ContractError):
        catalog.catalog({'data': [{'id': accepted}, {'id': accepted}]}, 'responses', 'runtime')


def test_runtime_oss_can_be_selected_and_has_no_mantle_prices():
    identifier = 'openai.gpt-oss-20b-1:0'
    assert catalog.selection(identifier, 'completions', 'runtime').selected_llm == identifier
    assert catalog.selection(identifier, 'responses', 'runtime').selected_llm == identifier
    assert 'structured' not in catalog.selection(identifier, 'responses', 'runtime').features
    assert pricing.bundled_rate_card({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_REGION': 'us-east-1'}) is None


def test_endpoint_defaults_and_mode_rejection_are_lazy():
    assert auth.validate_config({}) == 'responses'
    assert auth.validate_config({'BEDROCK_API_MODE': 'converse'}) == 'converse'
    assert auth.validate_config({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'converse'}) == 'converse'
    for config in ({'BEDROCK_ENDPOINT': 'other'},
                   {'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'messages'}):
        with pytest.raises(ContractError):
            auth.validate_config(config)


def test_mantle_converse_rejected_before_credential_acquisition():
    with pytest.raises(ContractError, match='BEDROCK_API_MODE=converse requires BEDROCK_ENDPOINT=runtime'):
        auth.validate_config({'BEDROCK_ENDPOINT': 'mantle', 'BEDROCK_API_MODE': 'converse'})


@pytest.mark.asyncio
async def test_invalid_endpoint_api_never_loads_acquirer_or_reads_credentials(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    loader = Mock(side_effect=AssertionError('Acquisition must remain inert'))
    credentials = AsyncMock()
    monkeypatch.setattr('api_agent.llm_providers.bedrock.session_providers.load_acquirer', loader)

    with pytest.raises(ContractError, match='BEDROCK_API_MODE=converse requires BEDROCK_ENDPOINT=runtime'):
        await auth.create_session({'BEDROCK_ENDPOINT': 'mantle', 'BEDROCK_API_MODE': 'converse'},
            interactive=True, user_index=None, credentials=credentials)

    loader.assert_not_called()
    credentials.assert_not_awaited()


def test_missing_endpoint_selects_runtime_discovery_and_cost_scope():
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    provider = BedrockProvider({})
    assert provider.endpoint == 'runtime'
    assert provider.api_mode == 'responses'
    assert provider.rate_card is None
    assert catalog.route('us.openai.gpt-5.6-luna', provider.api_mode, provider.endpoint) == '/openai/v1'


@pytest.mark.asyncio
async def test_missing_endpoint_dispatches_runtime_catalog(monkeypatch):
    from unittest.mock import AsyncMock
    from api_agent.llm_providers.bedrock import factory as factory_module
    call = AsyncMock(return_value={'data': []})
    monkeypatch.setattr(factory_module.SDKTransport, 'call', call)
    provider = factory_module.BedrockProvider({})

    assert await provider.adescribe() == ()
    assert call.await_args.args == ('runtime_models', {})


def test_bundled_default_selects_exact_oss_20b_id_from_either_catalog():
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.contracts import ProviderAccessError
    path = Path(__file__).resolve().parents[2] / 'src/api_agent/llm_providers/bedrock/cfg/config.toml'
    fields = tomllib.loads(path.read_text())['fields']
    assert fields['BEDROCK_ENDPOINT']['default'] == 'runtime'
    pattern = fields['LLM_DEFAULT_MODEL']['default']
    assert pattern == r'openai\.gpt-oss-20b(?:-1:0)?'
    for endpoint, mode, identifier in (
        ('mantle', 'responses', 'openai.gpt-oss-20b'),
        ('runtime', 'responses', 'openai.gpt-oss-20b-1:0'),
        ('runtime', 'completions', 'openai.gpt-oss-20b-1:0'),
    ):
        other_ids = (('openai.gpt-oss-120b',) if endpoint == 'mantle' else
                     ('openai.gpt-oss-120b', 'openai.gpt-oss-20b-1:0-extra',
                      'us.openai.gpt-oss-20b-1:0'))
        rows = tuple(catalog.selection(value, mode, endpoint) for value in (identifier, *other_ids))
        assert ModelSelectionPolicy({'LLM_ALLOWED_MODELS': fields['LLM_ALLOWED_MODELS']['default'],
                                     'LLM_DEFAULT_MODEL': pattern}).select_default(rows) == identifier
        with pytest.raises(ProviderAccessError, match='matches no allowed model'):
            ModelSelectionPolicy({'LLM_DEFAULT_MODEL': pattern}).select_default(rows[1:])
        assert ModelSelectionPolicy({'LLM_DEFAULT_MODEL': r'openai\.gpt-oss-120b'}).select_default(rows) == 'openai.gpt-oss-120b'
    assert re.fullmatch(fields['LLM_ALLOWED_MODELS']['default'], 'us.openai.gpt-5.6-luna')
    assert re.fullmatch(fields['LLM_ALLOWED_MODELS']['default'], 'global.anthropic.claude-sonnet-4-6')


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['us.openai.gpt-5.6-luna', 'other.future-model-v1'])
async def test_runtime_factory_uses_control_plane_catalog_and_exact_profile_dispatch(monkeypatch, profile):
    from api_agent.llm_providers.bedrock import factory as factory_module
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent

    reported = 'openai.gpt-5.6-luna' if profile.startswith('us.openai.') else profile
    calls = []
    class Transport:
        async def call(self, operation, payload, *, auth, join, on_dispatch=None):
            calls.append((operation, payload))
            _, lease = await join(())
            lease.release()
            if operation == 'runtime_models':
                return {'data': [{'id': profile}]}
            if on_dispatch is not None:
                on_dispatch()
            return {'id': 'response', 'model': reported, 'status': 'completed',
                    'output': [{'id': 'msg', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                                'content': [{'type': 'output_text', 'text': 'ok', 'annotations': []}]}],
                    'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}}
        async def aclose(self): pass
    monkeypatch.setattr(factory_module, 'SDKTransport', lambda **kw: Transport())
    provider = factory_module.BedrockProvider({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'responses'})
    assert [row.selected_llm for row in await provider.adescribe()] == [profile]
    request = LLMRequest(profile, CompletionRequest((Message('user', (TextContent('hello'),)),)))
    result = await provider.acomplete(request)
    assert result.actual_model == reported
    assert calls[0] == ('runtime_models', {})
    operation, payload = calls[1]
    assert operation == 'responses' and payload['endpoint'] == 'runtime'
    assert payload['prefix'] == '/openai/v1' and payload['body']['model'] == profile
    assert provider.rate_card is None
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('policy_mode', ['structured', 'json'])
async def test_runtime_converse_factory_dispatch_and_policy_capture(monkeypatch, streaming, policy_mode):
    from api_agent.llm_providers.bedrock import factory as factory_module
    from api_agent.llm_providers.bedrock.converse import decode
    from api_agent.llm_providers import response_policy as policy_module
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, ResponsePolicy, StructuredOutput

    profile = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
    schema = StructuredOutput('answer', '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}')
    request = LLMRequest(profile, CompletionRequest((Message('user', (TextContent('hello'),)),),
                         response=ResponsePolicy(policy_mode, 'allow', schema)))
    raw = {'output': {'message': {'role': 'assistant', 'content': [{'text': '{"ok":true}'}]}},
           'stopReason': 'end_turn', 'usage': {'inputTokens': 2, 'outputTokens': 1, 'totalTokens': 3}}
    captured, calls = [], []
    monkeypatch.setattr(policy_module, 'capture_payload', lambda request, body, evidence, **kw: captured.append(body))
    class Transport:
        async def call(self, operation, payload, *, auth, join, on_dispatch):
            calls.append((operation, payload))
            on_dispatch()
            return raw
        async def stream(self, operation, payload, *, auth, join, on_dispatch):
            calls.append((operation, payload))
            on_dispatch()
            yield {'kind': 'text', 'text': '{"ok":true}'}
            yield {'kind': 'completed', 'result': result_to_wire(decode(raw, selected_llm=profile))}
        async def aclose(self): pass
    monkeypatch.setattr(factory_module, 'SDKTransport', lambda **kw: Transport())
    provider = factory_module.BedrockProvider({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'converse'})
    try:
        if streaming:
            events = [event async for event in provider.astream(request)]
            result = events[-1].payload
        else:
            result = await provider.acomplete(request)
        assert result.payload.content[0].text == '{"ok":true}'
        operation, payload = calls[0]
        assert operation == ('converse_stream' if streaming else 'converse')
        assert payload['endpoint'] == 'runtime' and payload['prefix'] == '/converse'
        assert payload['body']['modelId'] == profile
        assert captured == [payload['body']]
        assert 'store' not in captured[0] and 'stream' not in captured[0]
        if policy_mode == 'structured':
            assert 'outputConfig' in payload['body']
        else:
            assert 'outputConfig' not in payload['body']
            assert all(type(block) is dict and 'text' in block
                       for message in payload['body']['messages'] for block in message['content'])
            assert 'Return a complete final answer' in payload['body']['messages'][-1]['content'][-1]['text']
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback', ['allow', 'reject'])
async def test_unqualified_runtime_openai_stream_schema_policy_before_dispatch(monkeypatch, fallback):
    from api_agent.llm_providers.bedrock import factory as factory_module
    from api_agent.llm_providers import response_policy as policy_module
    from api_agent.providers.control import OperationalError
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import (CompletionRequest, LLMRequest, LLMResult, Message,
                                         TextContent, ResponsePolicy, StructuredOutput)

    profile = 'global.openai.gpt-5.6-luna'
    definition = StructuredOutput('answer', '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}')
    request = LLMRequest(profile, CompletionRequest((Message('user', (TextContent('hello'),)),),
                         response=ResponsePolicy('structured', fallback, definition)))
    result = LLMResult(Message('assistant', (TextContent('{"ok":true}'),)))
    calls, captured = [], []
    monkeypatch.setattr(policy_module, 'capture_payload', lambda request, body, evidence, **kw: captured.append((body, evidence)))
    class Transport:
        async def stream(self, operation, payload, *, auth, join, on_dispatch):
            calls.append((operation, payload))
            on_dispatch()
            yield {'kind': 'text', 'text': '{"ok":true}'}
            yield {'kind': 'completed', 'result': result_to_wire(result)}
        async def aclose(self): pass
    monkeypatch.setattr(factory_module, 'SDKTransport', lambda **kw: Transport())
    provider = factory_module.BedrockProvider({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'responses'})
    try:
        if fallback == 'reject':
            with pytest.raises(OperationalError):
                _ = [event async for event in provider.astream(request)]
            assert calls == [] and captured == []
        else:
            events = [event async for event in provider.astream(request)]
            assert events[-1].payload.payload.content[0].text == '{"ok":true}'
            operation, payload = calls[0]
            assert operation == 'responses_stream' and payload['endpoint'] == 'runtime'
            assert 'text' not in payload['body']
            assert 'Return a complete final answer' in payload['body']['input'][-1]['content']
            assert captured[0][0]['store'] is False and captured[0][0]['stream'] is True
            assert captured[0][1].actual_mode == 'prompted_json'
    finally:
        await provider.aclose()


def test_runtime_ui_families_preserve_geographic_route_and_exact_defaults():
    from api_agent.api.chat_contract import catalog as application_catalog
    us = 'us.openai.gpt-5.6-luna'
    global_target = 'global.openai.gpt-5.6-luna'
    rows = tuple(catalog.selection(identifier, 'responses', 'runtime') for identifier in (us, global_target))
    result = application_catalog(rows, us, None)
    assert result['family_defaults'] == {'gpt-luna-us': us, 'gpt-luna-global': global_target}
    assert [row['label'] for row in result['models']] == ['GPT 5.6 Luna [US]', 'GPT 5.6 Luna [GLOBAL]']
    assert all(row['version'] == '5.6' for row in result['models'])


@pytest.mark.parametrize(('base', 'prefixes', 'context', 'output'), [
    ('anthropic.claude-opus-5-5', ('us', 'eu', 'global'), 1000000, 128000),
    ('anthropic.claude-sonnet-5-5', ('us', 'eu', 'global'), 1000000, 128000),
    ('anthropic.claude-haiku-5-5', ('us', 'eu', 'global'), 1000000, 128000),
    ('anthropic.claude-opus-4-8', ('us', 'eu', 'au', 'jp', 'us-gov', 'global'), 1000000, 128000),
    ('anthropic.claude-opus-4-7', ('us', 'eu', 'au', 'jp', 'global'), 1000000, 128000),
    ('anthropic.claude-opus-4-1-20250805-v1:0', ('us',), 200000, 32000),
    ('anthropic.claude-opus-4-5-20251101-v1:0', ('us', 'eu', 'global'), 200000, 64000),
])
def test_additional_documented_claude_profiles(base, prefixes, context, output):
    fields = tomllib.loads((Path(__file__).resolve().parents[2] / 'src/api_agent/llm_providers/bedrock/cfg/config.toml').read_text())['fields']
    pattern = fields['LLM_ALLOWED_MODELS']['default']
    for prefix in prefixes:
        identifier = (prefix + '.' if prefix else '') + base
        row = catalog.selection(identifier, 'converse', 'runtime')
        assert row.selected_llm == identifier
        assert row.context_length == context and row.max_output_tokens == output
        assert re.fullmatch(pattern, identifier)
        assert 'temperature' in row.features and 'reasoning_effort' in row.features
        assert set(('low', 'medium', 'high')) <= set(row.reasoning_efforts)
        assert ('structured' in row.features) == (identifier not in {
            'us.anthropic.claude-opus-4-1-20250805-v1:0'})
        assert catalog.selection(identifier, 'responses', 'runtime').selected_llm == identifier


@pytest.mark.parametrize('identifier', ['us.openai.gpt-6-astra', 'global.openai.gpt-6-astra'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_astra_and_generic_gpt_54(identifier, mode):
    row = catalog.selection(identifier, mode, 'runtime')
    assert row.selected_llm == identifier and row.family.startswith('gpt-astra-')
    assert row.version == '6' and 'structured' in row.features
    assert row.context_length == 1050000 and row.max_output_tokens == 128000
    generic = catalog.selection('us.openai.gpt-5.4', mode, 'runtime')
    assert generic.selected_llm == 'us.openai.gpt-5.4' and 'structured' not in generic.features


@pytest.mark.parametrize('identifier', [
    'global.anthropic.claude-opus-5-7', 'us.anthropic.claude-fable-5',
    'global.anthropic.claude-fable-5-1', 'us.anthropic.claude-haiku-5-7',
    'us.anthropic.claude-opus-5', 'us.anthropic.claude-sonnet-5-7',
    'us.anthropic.claude-sonnet-5',
])
def test_unqualified_profiles_are_discoverable_without_native_schema_claims(identifier):
    row = catalog.selection(identifier, 'converse', 'runtime')
    assert row.selected_llm == identifier and 'structured' not in row.features
    assert catalog.catalog({'data': [{'id': identifier}]}, 'converse', 'runtime') == (row,)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['responses', 'completions'])
async def test_legacy_structured_runtime_stream_rejects_before_authentication(monkeypatch, mode):
    from api_agent.llm_providers.bedrock import factory as module
    from api_agent.providers.control import OperationalError
    from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent, StructuredOutput
    class Transport:
        async def stream(self, *args, **kwargs):
            raise AssertionError('Unsupported stream reached authentication transport')
            yield
        async def aclose(self): pass
    monkeypatch.setattr(module, 'SDKTransport', lambda **kwargs: Transport())
    provider = module.BedrockProvider({'BEDROCK_ENDPOINT':'runtime', 'BEDROCK_API_MODE':mode})
    req = LLMRequest('global.openai.gpt-5.6-luna', CompletionRequest((Message('user', (TextContent('question'),)),),
        structured=StructuredOutput('answer', '{"type":"object","properties":{},"required":[],"additionalProperties":false}')))
    try:
        with pytest.raises(OperationalError, match='unsupported'):
            _ = [item async for item in provider.astream(req)]
    finally:
        await provider.aclose()


@pytest.mark.parametrize('identifier', [None, '', ' leading', 'trailing ', 'line\nbreak', 12, 'x' * 257])
def test_runtime_discovery_rejects_malformed_identity_without_model_availability_rules(identifier):
    with pytest.raises(ContractError):
        catalog.selection(identifier, 'responses', 'runtime')


@pytest.mark.parametrize('identifier,family,version,title', [
    ('anthropic.claude-haiku-5-7', 'claude-haiku-direct', '5.7', 'Claude Haiku 5.7'),
    ('us.anthropic.claude-haiku-5-7', 'claude-haiku-us', '5.7', 'Claude Haiku 5.7 [US]'),
    ('global.anthropic.claude-haiku-5-7', 'claude-haiku-global', '5.7', 'Claude Haiku 5.7 [GLOBAL]'),
    ('anthropic.claude-haiku-4-5-20251001-v1:0', 'claude-haiku-direct', '4.5', 'Claude Haiku 4.5'),
    ('us.anthropic.claude-3-haiku-20240307-v1:0', 'claude-haiku-us', '3', 'Claude Haiku 3 [US]'),
    ('us.anthropic.claude-3-5-sonnet-20241022-v2:0', 'claude-sonnet-us', '3.5', 'Claude Sonnet 3.5 [US]'),
    ('anthropic.claude-fable-5-1', 'claude-fable-direct', '5.1', 'Claude Fable 5.1'),
    ('anthropic.claude-opus-5', 'claude-opus-direct', '5', 'Claude Opus 5'),
    ('openai.gpt-5.4', 'gpt', '5.4', 'GPT 5.4'),
    ('us.openai.gpt-5.5', 'gpt--us', '5.5', 'GPT 5.5 [US]'),
])
@pytest.mark.parametrize('mode', ['responses', 'completions', 'converse'])
def test_discovered_claude_and_numeric_gpt_display_never_grants_support(identifier, family, version, title, mode):
    row = catalog.selection(identifier, mode, 'runtime')
    assert row.selected_llm == identifier
    assert (row.family, row.version, row.display_name) == (family, version, title)
    assert row.max_output_tokens is row.context_length is None
    assert 'structured' not in row.features
    assert catalog.route(identifier, mode, 'runtime') == ('/converse' if mode == 'converse' else '/openai/v1')


@pytest.mark.parametrize('identifier', ['direct.anthropic.claude-haiku-5-5',
    'anthropic.claude-haiku-5-5-us', 'anthropic.claude-haiku', 'anthropic.claude-5-5',
    'anthropic.claude-haiku-5-5-vx', 'vendor.claude-haiku-5-5'])
def test_unrecognized_claude_display_remains_opaque(identifier):
    row = catalog.selection(identifier, 'converse', 'runtime')
    assert (row.display_name, row.family, row.version) == (identifier, identifier, None)


def test_claude_family_defaults_choose_numeric_versions_without_merging_routes():
    from api_agent.api.chat_contract import catalog as application_catalog
    old = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
    new = 'us.anthropic.claude-haiku-5-5'
    global_id = 'global.anthropic.claude-haiku-5-5'
    rows = tuple(catalog.selection(identifier, 'converse', 'runtime') for identifier in (old, new, global_id))
    result = application_catalog(rows, global_id, None)
    assert result['family_defaults'] == {'claude-haiku-us': new, 'claude-haiku-global': global_id}
    assert [row['value'] for row in result['models']] == [old, new, global_id]
    assert [row['version'] for row in result['models']] == ['4.5', '5.5', '5.5']


@pytest.mark.parametrize('scope', ['us', 'eu', 'au', 'jp', 'in', 'apac', 'global', 'direct'])
def test_numeric_gpt_route_cannot_merge_with_a_named_variant(scope):
    from api_agent.api.chat_contract import catalog as application_catalog
    named = 'openai.gpt-5-' + scope
    scoped = scope + '.openai.gpt-6'
    rows = tuple(catalog.selection(identifier, 'responses', 'runtime') for identifier in (named, scoped))
    assert rows[0].family != rows[1].family
    result = application_catalog(rows, named, None)
    assert set(result['family_defaults'].values()) == {named, scoped}
    assert [row['value'] for row in result['models']] == [named, scoped]
