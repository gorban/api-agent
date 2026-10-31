"""GPT endpoint and effective configuration checks use no external services."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable

from api_agent.config import resolve_config
from api_agent.llm_providers.gpt.endpoint import OFFICIAL_BASE_URL, validate_base_url
from api_agent.providers.contracts import ContractError
from api_agent.providers.loading import bootstrap_schema
from test_provider_configuration import selected_base
from api_agent.tui.app import ApiAgentApp


@pytest.mark.parametrize('value,expected', [
    ('https://api.openai.com/v1', OFFICIAL_BASE_URL),
    ('https://OPENROUTER.ai/api/v1/', 'https://openrouter.ai/api/v1'),
    ('https://example.com/', 'https://example.com'),
    ('https://example.com:8443/custom/v1', 'https://example.com:8443/custom/v1'),
    ('https://[::1]:8443/v1', 'https://[::1]:8443/v1'),
    ('https://127.0.0.1/v1', 'https://127.0.0.1/v1'),
    ('https://example.com/custom%7E/v1', 'https://example.com/custom%7E/v1'),
])
def test_base_url_normalizes_valid_origins(value, expected):
    assert validate_base_url(value) == expected


@pytest.mark.parametrize('value', [
    None, 1, '', ' https://example.com/v1', 'https://example.com/\n',
    'http://example.com/v1', '/v1', 'https:///v1', 'https://user@example.com/v1',
    'https://user:secret@example.com/v1', 'https://example.com/v1?secret',
    'https://example.com/v1#secret', 'https://example.com/v1?', 'https://example.com/v1#',
    'https://example.com:/v1', 'https://example.com:0/v1', 'https://example.com:65536/v1',
    'https://example.com:bad/v1', 'https://[invalid]/v1', 'https://-example.com/v1',
    'https://example..com/v1', 'https://999.1.1.1/v1', 'https://example.com/a\\b',
    'https://example.com/../v1', 'https://example.com/./v1', 'https://example.com//v1',
    'https://example.com/%2e%2e/v1', 'https://example.com/%0a',
    'https://example.com/%5c', 'https://example.com/a%2fb', 'https://example.com/a%20b',
    'https://example.com/%zz', 'https://example.com/{bad}', 'https://éxample.com/v1',
    'https://example.com/\x7f', 'https://example.com/%ff',
    'https://[::1]evil/v1', 'https://[fe80::1%25eth0]/v1',
    'https://example.com/%C2%85', 'https://example.com/%E2%80%8B',
])
def test_base_url_rejection_is_safe(value):
    with pytest.raises(ContractError) as caught:
        validate_base_url(value)
    assert str(caught.value) == 'GPT_BASE_URL must be an absolute HTTPS base without credentials, query, or fragment.'
    assert caught.value.__cause__ is None


def provider_schema(tmp_path):
    from api_agent.llm_providers.gpt import factory
    location = str(Path(factory.__file__).resolve()) + ':factory'
    return bootstrap_schema(selected_base(location), [], {}, {}, owner_root=tmp_path)


@pytest.mark.parametrize('mode,api_mode', [('subscription', 'responses'), ('api_key', 'completions')])
def test_provider_conditional_defaults_and_overrides(tmp_path, mode, api_mode):
    schema = provider_schema(tmp_path)
    result = resolve_config(schema, argv=[], environ={}, dotenv={},
                            anchors={'owner_root': tmp_path}, interactive=True,
                            overrides={'GPT_AUTH_MODE': mode})
    assert result.values['GPT_API_MODE'] == api_mode
    assert ('GPT_BASE_URL' in result.values) == (mode == 'api_key')
    assert ('GPT_STATE_DIR' in result.values) == (mode == 'subscription')
    assert ('GPT_APP_NAME' in result.values) == (mode == 'subscription')
    if mode == 'api_key':
        assert result.values['GPT_BASE_URL'] == OFFICIAL_BASE_URL
    alternate = resolve_config(schema, argv=[], environ={}, dotenv={},
                               anchors={'owner_root': tmp_path}, interactive=True,
                               overrides={'GPT_AUTH_MODE': mode, 'GPT_BASE_URL': 'https://openrouter.ai/api/v1',
                                          'GPT_API_MODE': 'responses'})
    assert alternate.values['GPT_API_MODE'] == 'responses'
    assert alternate.values.get('GPT_BASE_URL') == ('https://openrouter.ai/api/v1' if mode == 'api_key' else None)


@pytest.mark.parametrize('dotenv,environ,argv,source,expected', [
    ({'GPT_APP_NAME': 'api-agent'}, {}, [], 'dotenv', 'api-agent'),
    ({'GPT_APP_NAME': 'chosen-app'}, {}, [], 'dotenv', 'chosen-app'),
    ({'GPT_APP_NAME': 'older-app'}, {'GPT_APP_NAME': 'api-agent'}, [], 'environment', 'api-agent'),
    ({'GPT_APP_NAME': 'older-app'}, {'GPT_APP_NAME': 'another-app'}, ['--gpt-app-name=api-agent'], 'cli', 'api-agent'),
])
def test_effective_app_name_reaches_the_actual_registration_hint(tmp_path, monkeypatch, dotenv, environ, argv, source, expected):
    from unittest.mock import AsyncMock
    from urllib.parse import parse_qs, urlsplit
    from api_agent.llm_providers.gpt import factory
    from api_agent.llm_providers.gpt.oauth import begin_authorization
    schema = provider_schema(tmp_path)
    result = resolve_config(schema, argv=argv, environ=environ, dotenv=dotenv,
                            anchors={'owner_root': tmp_path}, interactive=True)
    assert result.fields['GPT_APP_NAME'].source == source
    async def scenario():
        browser = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(factory, 'subscription_browser', lambda **_: browser)
        provider = factory.factory(result.values, None)
        async def connect(store, **options):
            attempt = begin_authorization('urn:uuid:12345678-1234-1234-1234-123456789abc',
                'http://127.0.0.1:12345/auth/callback', app_name=options['app_name'])
            assert parse_qs(urlsplit(attempt.authorization_url).query)['agent_name_hint'] == [expected]
            return 'synthetic-profile'
        provider._connect_account = connect
        assert await provider._login(object(), interactive=False, username='person@example.test',
                                     user_index=4, credentials=AsyncMock()) == 'synthetic-profile'
        await provider.aclose()
    asyncio.run(scenario())


def test_tui_effective_provider_settings_and_default_review(tmp_path):
    async def scenario():
        context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
                                  external_pack=False, dotenv_path=tmp_path / '.env',
                                  anchors={'owner_root': tmp_path})
        from api_agent.llm_providers.gpt import factory
        base = selected_base(str(Path(factory.__file__).resolve()) + ':factory')
        app = ApiAgentApp(context, base, environ={},
                          container_runtime=SimpleNamespace(inventory=lambda: ()))
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.1)
            app.session.set_value('GPT_AUTH_MODE', 'api_key')
            app.show_configuration()
            await pilot.pause(.1)
            table = app.screen.query_one(DataTable)
            names = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
            assert 'GPT_BASE_URL' in names
            assert not {'GPT_STATE_DIR', 'GPT_APP_NAME'} & names
            assert not {'GPT_STATE_DIR', 'GPT_APP_NAME'} & {c.name for c in app.session.default_candidates()}
            assert not {'GPT_STATE_DIR', 'GPT_APP_NAME'} & app.session.result().values.keys()
            app.session.set_value('GPT_AUTH_MODE', 'subscription')
            app.show_configuration()
            await pilot.pause(.1)
            table = app.screen.query_one(DataTable)
            names = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
            assert {'GPT_STATE_DIR', 'GPT_APP_NAME'} <= names
            assert {'GPT_STATE_DIR', 'GPT_APP_NAME'} <= {c.name for c in app.session.default_candidates()}
            assert 'GPT_BASE_URL' not in names
            assert 'GPT_BASE_URL' not in app.session.result().values
            app.session.set_value('LLM_PROVIDER', '')
            assert 'GPT_BASE_URL' not in app.session.schema.fields
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['LLM_STUDIO_TEST_DEFAULT_MODEL', 'LLM_STUDIO_TEST_SELECTED_MODEL'])
def test_real_provider_default_admits_its_studio_selections(tmp_path, key):
    from api_agent.testing.model_choices import exact_model
    result = resolve_config(provider_schema(tmp_path), environ={}, dotenv={},
                            anchors={'owner_root': tmp_path}, interactive=True)
    assert exact_model(result.values, key) == result.values[key]


@pytest.mark.parametrize('model', [
    'gpt-5.6-luna', 'gpt-6-terra', 'gpt-6.1-sol',
    'openai/gpt-5.6-terra', 'openai/gpt-6-sol-2026-10-27', 'gpt-6.1-luna-2026-10-27',
])
def test_real_provider_default_admits_general_version_families(tmp_path, model):
    from api_agent.providers.selection import ModelSelectionPolicy
    result = resolve_config(provider_schema(tmp_path), environ={}, dotenv={},
                            anchors={'owner_root': tmp_path}, interactive=True)
    ModelSelectionPolicy(result.values).require_allowed(model)


@pytest.mark.parametrize('model', ['gpt-5.6-astra', 'gpt-6-astra', 'gpt-6.1-haiku', 'claude-haiku'])
def test_real_provider_default_rejects_other_families(tmp_path, model):
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.contracts import ProviderAccessError
    result = resolve_config(provider_schema(tmp_path), environ={}, dotenv={},
                            anchors={'owner_root': tmp_path}, interactive=True)
    with pytest.raises(ProviderAccessError):
        ModelSelectionPolicy(result.values).require_allowed(model)


@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli'])
def test_explicit_model_restriction_remains_authoritative(tmp_path, source):
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.contracts import ProviderAccessError
    restricted = r'gpt-6-sol'
    options = {'dotenv': {}, 'environ': {}, 'argv': []}
    if source == 'cli':
        options['argv'] = ['--llm-allowed-models=' + restricted]
    else:
        options['environ' if source == 'environment' else 'dotenv'] = {'LLM_ALLOWED_MODELS': restricted}
    result = resolve_config(provider_schema(tmp_path), **options,
                            anchors={'owner_root': tmp_path}, interactive=True)
    assert result.fields['LLM_ALLOWED_MODELS'].source == source
    policy = ModelSelectionPolicy(result.values)
    policy.require_allowed('gpt-6-sol')
    with pytest.raises(ProviderAccessError):
        policy.require_allowed('gpt-5.6-luna')
