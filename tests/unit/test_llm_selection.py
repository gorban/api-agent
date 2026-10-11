"""Shared admission and version selection work independently of providers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.config.declarations import schema_from_dict
from api_agent.config.resolve import resolve_config
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import AvailableSelection, CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.selection import ModelSelectionPolicy, compile_pattern


def selection(identifier, version=None, family=None):
    return AvailableSelection(identifier, version=version, family=family)


@pytest.mark.parametrize('pattern', [None, '', 'x'*2049, '[', 'a{4294967296}', '('*700+'a'+')'*700],
                         ids=['missing', 'empty', 'too-long', 'syntax', 'repeat-overflow', 'nesting-overflow'])
def test_bad_regex_is_actionable(pattern):
    with pytest.raises(ValueError, match='regular expression'):
        compile_pattern(pattern)
    with pytest.raises(ProviderAccessError, match='regular expression'):
        ModelSelectionPolicy({'LLM_ALLOWED_MODELS': pattern})


def test_runtime_regex_compiler_limits_are_actionable():
    pattern = '('*400+'a'+')'*400
    assert compile_pattern(pattern).pattern == pattern
    with pytest.raises(ProviderAccessError, match='regular expression'):
        ModelSelectionPolicy({'LLM_ALLOWED_MODELS': pattern})


def test_config_regex_validation_type_and_external_value():
    with pytest.raises(ValueError, match='regex validation requires str'):
        schema_from_dict({'version': 1, 'fields': {'FILTER': {'type': 'int', 'validation': 'regex'}}})
    schema = schema_from_dict({'version': 1, 'fields': {'FILTER': {'type': 'str', 'validation': 'regex', 'default': '.*'}}})
    assert resolve_config(schema, argv=['--filter=['], environ={}).errors == ('FILTER: expected a valid regular expression',)
    assert resolve_config(schema, argv=['--filter=a|b'], environ={}).values['FILTER'] == 'a|b'


def test_config_regex_validation_needs_no_runtime_dependencies():
    import subprocess
    import sys
    from pathlib import Path
    source = Path(__file__).parents[2]/'src'
    script = '''
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def without_dependencies(name, *args, **kwargs):
    if name == 'regex' or name.startswith('packaging'):
        raise AssertionError('Config validation imported a runtime dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = without_dependencies
from api_agent.config import schema_from_dict, resolve_config
schema = schema_from_dict({'version':1,'fields':{'FILTER':{'type':'str','validation':'regex','default':'a|b'}}})
assert resolve_config(schema, environ={}).values['FILTER'] == 'a|b'
'''
    result = subprocess.run([sys.executable, '-S', '-c', script, str(source)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_declared_defaults_allow_three_variants_but_choose_luna():
    from api_agent.config.declarations import load_declarations
    from pathlib import Path
    schema = load_declarations(Path(__file__).parents[2]/'cfg/config.toml')
    config = resolve_config(schema, environ={}).values
    policy = ModelSelectionPolicy(config)
    values = tuple(selection(name, version, family) for name, version, family in [
        ('gpt-6-luna', '6', 'gpt-luna'), ('gpt-6.9-luna', '6.9', 'gpt-luna'),
        ('gpt-6.10-luna', '6.10', 'gpt-luna'), ('gpt-6.1-sol', '6.1', 'gpt-sol'),
        ('gpt-6-haiku', '6', 'gpt-haiku'), ('gpt-6-astra', '6', 'gpt-astra'),
        ('gpt-5-luna', '5', 'gpt-luna'), ('openai/gpt-6-sol', '6', 'gpt-sol')])
    assert tuple(row.selected_llm for row in policy.filter(values)) == tuple(row.selected_llm for row in values if row.selected_llm not in ('gpt-6-astra', 'gpt-5-luna'))
    assert policy.select_default(values) == 'gpt-6.10-luna'
    assert schema.fields['LLM_ALLOWED_MODELS'].acknowledge_default
    assert schema.fields['LLM_DEFAULT_MODEL'].acknowledge_default


@pytest.mark.parametrize('identifier', [None, '', 'x'*201, '\n', 'prefix-a', 'a-suffix', 'c'])
def test_fullmatch_allowlist_rejects_explicit_model(identifier):
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '(?:a|b)'})
    with pytest.raises(ProviderAccessError, match='not permitted'):
        policy.require_allowed(identifier)
    policy.require_allowed('a')
    assert policy.filter((selection('c'), selection('b'), selection('a'))) == (selection('b'), selection('a'))


def test_regex_wildcard_and_literal_asterisk_are_distinct():
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': r'gpt-6\*'})
    policy.require_allowed('gpt-6*')
    with pytest.raises(ProviderAccessError):
        policy.require_allowed('gpt-6-luna')
    ModelSelectionPolicy({'LLM_ALLOWED_MODELS': 'gpt-6.*'}).require_allowed('gpt-6-luna')


def test_another_family_and_catalog_order_are_provider_neutral():
    values = (selection('other-2', '2', 'other'), selection('claude-haiku-4.9', '4.9', 'haiku'), selection('claude-haiku-4.10', '4.10', 'haiku'))
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '(?:other|claude)-.*', 'LLM_DEFAULT_MODEL': 'claude-haiku-.*'})
    assert policy.filter(values) == values
    assert policy.select_default(values) == 'claude-haiku-4.10'
    assert policy.select_default((values[1],)) == values[1].selected_llm


def test_single_default_match_needs_no_version_comparison():
    assert ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'unknown'}).select_default((selection('unknown'),)) == 'unknown'


@pytest.mark.parametrize('values,message', [
    ((), 'no allowed model'),
    ((selection('x-1', '1', 'x'), selection('y-2', '2', 'y')), 'one model family'),
    ((selection('x-1', '1'), selection('x-2', '2')), 'one model family'),
    ((selection('x-1', '1', 'x'), selection('x-2', None, 'x')), 'version metadata'),
    ((selection('x-1', '1', 'x'), selection('x-2', 'unknown', 'x')), 'comparable version'),
    ((selection('x-1', '2', 'x'), selection('x-2', '2.0', 'x')), 'highest version'),
])
def test_default_resolution_fails_instead_of_picking_wrong_model(values, message):
    with pytest.raises(ProviderAccessError, match=message):
        ModelSelectionPolicy({'LLM_DEFAULT_MODEL': '.*'}).select_default(values)


def test_missing_default_and_default_excluded_by_allowlist():
    with pytest.raises(ProviderAccessError, match='LLM_DEFAULT_MODEL is required'):
        ModelSelectionPolicy({}).select_default((selection('x'),))
    with pytest.raises(ProviderAccessError, match='no allowed model'):
        ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'x', 'LLM_ALLOWED_MODELS': 'y'}).select_default((selection('x'),))


def test_regex_matching_is_bounded():
    policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '(a|aa)+$'})
    with pytest.raises(ProviderAccessError, match='match budget'):
        policy.require_allowed('a'*199+'!')


@pytest.mark.parametrize('remote', [False, True])
def test_explicit_disallowed_request_never_loads_provider_or_authenticates(tmp_path, monkeypatch, remote):
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.testing.setup_backend import SetupBackend
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing import provider_setup
    loader = AsyncMock(side_effect=AssertionError('Disallowed model reached provider loading'))
    monkeypatch.setattr(provider_setup, 'aload_provider', loader)
    config = {'LLM_ALLOWED_MODELS': 'allowed', 'LLM_PROVIDER': 'synthetic'}
    owner = SetupBackend(config, tmp_path) if remote else IntegrationLLM(config, tmp_path)
    client = SetupClient(owner.start()) if remote else owner
    try:
        request = LLMRequest('forbidden', CompletionRequest((Message('user', (TextContent('Ok'),)),)))
        with pytest.raises(ProviderAccessError, match='not permitted'):
            client.complete(request)
        loader.assert_not_awaited()
    finally:
        client.close()
        if remote:
            owner.close()


def test_local_catalog_filter_and_direct_shared_validation(tmp_path, monkeypatch):
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.testing import provider_setup
    values = (selection('allowed'), selection('forbidden'))
    adapter = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic')), adescribe=AsyncMock(return_value=values))
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=SimpleNamespace(implementation=adapter, aclose=AsyncMock())))
    owner = IntegrationLLM({'LLM_PROVIDER': 'synthetic', 'LLM_ALLOWED_MODELS': 'allowed'}, tmp_path)
    try:
        assert owner.describe() == (values[0],)
        with pytest.raises(ProviderAccessError, match='not permitted'):
            owner._validate_request(adapter, LLMRequest('forbidden', CompletionRequest((Message('user', (TextContent('Ok'),)),))))
        adapter.adescribe.assert_awaited_once()
    finally:
        owner.close()
