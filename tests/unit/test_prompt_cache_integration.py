"""Deterministic controls for the provider-owned two-call cache acceptance test."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.testing.provider_tests import load_test_declaration

_PATH = Path(__file__).parents[2] / 'src/api_agent/llm_providers/gpt/tests/integration/test_prompt_cache.py'
_spec = importlib.util.spec_from_file_location('gpt_prompt_cache_acceptance', _PATH)
_acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acceptance)


def result(cached=1024, write=0):
    return LLMResult(Message('assistant', (TextContent('Paris'),)), Usage(1027, 2, cached, None, write), billing={'usd': 0.01, 'request_id': None, 'generation_id': None})


class OwnedProvider:
    def __init__(self, results):
        self.results = iter(results)
        self.requests = []
        self.usages = []
        self.cost = 0

    def complete(self, supplied):
        self.requests.append(supplied)
        returned = next(self.results)
        if isinstance(returned, Exception):
            raise returned
        self.usages.append(returned.usage)
        self.cost += returned.billing['usd']
        return returned


class Fixtures:
    def __init__(self, provider, selection='openai/gpt-6-luna'):
        self.provider, self.selection = provider, selection
        self.lookups = []

    def getfixturevalue(self, name):
        self.lookups.append(name)
        return self.selection if name == 'llm_selection' else self.provider


def run(provider, values=None, selection='openai/gpt-6-luna'):
    fixtures = Fixtures(provider, selection)
    config = {'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': 'https://openrouter.ai/api/v1', 'LLM_CACHE_POLICY': ''} | (values or {})
    _acceptance.test_prompt_cache_1027_tokens(SimpleNamespace(values=config), fixtures)
    return fixtures


@pytest.mark.parametrize('values', [{'LLM_CACHE_POLICY': 'off'}, {'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'medium'}, {'GPT_BASE_URL': 'https://compatible.example/v1'}])
def test_integration_skip_happens_before_selection_discovery_or_auth(values):
    fixtures = Fixtures(OwnedProvider([]))
    config = {'GPT_AUTH_MODE': 'api_key'} | values
    with pytest.raises(pytest.skip.Exception):
        _acceptance.test_prompt_cache_1027_tokens(SimpleNamespace(values=config), fixtures)
    assert fixtures.lookups == []
    assert fixtures.provider.requests == []


@pytest.mark.parametrize('values,selection', [({}, 'openai/gpt-4.1'), ({'LLM_CACHE_POLICY': 'medium'}, 'gpt-6-luna')])
def test_unsupported_family_or_namespace_skips_before_inference(values, selection):
    provider = OwnedProvider([])
    with pytest.raises(pytest.skip.Exception):
        run(provider, values, selection)
    assert provider.requests == []


@pytest.mark.parametrize('values,selection', [({}, 'openai/gpt-6-luna'), ({'LLM_CACHE_POLICY': 'medium'}, 'openai/gpt-6-luna'), ({'LLM_CACHE_POLICY': 'high'}, 'openai/gpt-6-luna'), ({'GPT_AUTH_MODE': 'subscription'}, 'gpt-6-luna'), ({'GPT_BASE_URL': 'https://api.openai.com/v1'}, 'gpt-6.1-sol')])
def test_exact_1027_input_repeated_immutably_and_both_usage_costs_preserved(values, selection):
    provider = OwnedProvider([result(0, 1024), result(1024)])
    fixtures = run(provider, values, selection)
    assert fixtures.lookups == ['llm_selection', 'llm_provider']
    assert len(provider.requests) == 2
    first, second = provider.requests
    assert first is second
    assert first.selected_llm == selection
    assert first.payload.generation.cache_policy is None  # Owned provider applies configuration.
    text = first.payload.messages[0].content[0].text
    assert text == _acceptance._prompt_text(values.get('GPT_API_MODE', 'responses' if values.get('GPT_AUTH_MODE') == 'subscription' else 'completions'))
    assert 'What is the capital of France?' in text
    assert 'A small town library' in text
    assert provider.usages == [Usage(1027, 2, 0, None, 1024), Usage(1027, 2, 1024, None, 0)]
    assert provider.cost == 0.02


@pytest.mark.parametrize('cached', [None, 0, 1, 1023])
def test_missing_or_insufficient_repeat_cache_counter_fails(cached):
    provider = OwnedProvider([result(0, 1024), result(cached)])
    with pytest.raises(AssertionError, match='at least 1024'):
        run(provider)
    assert len(provider.requests) == 2
    assert provider.cost == 0.02


@pytest.mark.parametrize('failure_index', [0, 1])
def test_warm_or_repeat_error_bubbles_without_retry(failure_index):
    failure = ProviderAccessError('unavailable', 'Synthetic completion failed.')
    provider = OwnedProvider(([result(0, 1024)] if failure_index else []) + [failure])
    with pytest.raises(ProviderAccessError) as caught:
        run(provider)
    assert caught.value is failure
    assert len(provider.requests) == failure_index + 1
    assert provider.cost == 0.01 * failure_index


def test_provider_manifest_discovers_cache_acceptance_suite():
    assert load_test_declaration(_PATH.parents[2]) == {'integration': ['tests/integration']}


@pytest.mark.parametrize('failure_index', [0, 1])
@pytest.mark.parametrize('input_tokens', [None, 1026, 1028])
def test_exact_api_input_count_required_for_warm_and_repeat(failure_index, input_tokens):
    from dataclasses import replace
    wrong = replace(result(0), usage=Usage(input_tokens, 2, 0, None, 0))
    provider = OwnedProvider(([result(0, 1024)] if failure_index else []) + [wrong])
    with pytest.raises(AssertionError, match='exactly 1027 input tokens'):
        run(provider)
    assert len(provider.requests) == failure_index + 1
    assert provider.cost == 0.01 * (failure_index + 1)
