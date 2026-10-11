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


def result(cached=1024, write=0, input_tokens=1027):
    return LLMResult(Message('assistant', (TextContent('Paris'),)), Usage(input_tokens, 2, cached, None, write), billing={'usd': 0.01, 'request_id': None, 'generation_id': None})


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
    _acceptance.test_prompt_cache(_acceptance.prepare_prompt_cache_case(config, fixtures))
    return fixtures


@pytest.mark.parametrize('values', [{'LLM_CACHE_POLICY': 'off'}, {'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'high'}, {'GPT_BASE_URL': 'https://compatible.example/v1'}])
def test_integration_skip_happens_before_selection_discovery_or_auth(values):
    fixtures = Fixtures(OwnedProvider([]))
    config = {'GPT_AUTH_MODE': 'api_key'} | values
    with pytest.raises(pytest.skip.Exception):
        _acceptance.test_prompt_cache(_acceptance.prepare_prompt_cache_case(config, fixtures))
    assert fixtures.lookups == []
    assert fixtures.provider.requests == []


@pytest.mark.parametrize('values,selection', [({}, 'openai/gpt-4.1'), ({'LLM_CACHE_POLICY': 'medium'}, 'gpt-6-luna')])
def test_unsupported_family_or_namespace_skips_before_inference(values, selection):
    provider = OwnedProvider([])
    with pytest.raises(pytest.skip.Exception):
        run(provider, values, selection)
    assert provider.requests == []


@pytest.mark.parametrize('values,selection,tokens,cached', [
    ({}, 'openai/gpt-6-luna', 1027, 1024),
    ({'LLM_CACHE_POLICY': 'medium'}, 'openai/gpt-6-luna', 1027, 1024),
    ({'LLM_CACHE_POLICY': 'high'}, 'openai/gpt-6-luna', 1027, 1024),
    ({'GPT_AUTH_MODE': 'subscription'}, 'gpt-6-luna', 4045, 3840),
    ({'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'medium'}, 'gpt-6-luna', 4045, 3840),
    ({'GPT_BASE_URL': 'https://api.openai.com/v1'}, 'gpt-6.1-sol', 1027, 1024),
])
def test_route_input_repeated_immutably_and_both_usage_costs_preserved(values, selection, tokens, cached):
    provider = OwnedProvider([result(0, cached, tokens), result(cached, 0, tokens)])
    fixtures = run(provider, values, selection)
    assert fixtures.lookups == ['llm_selection', 'llm_provider']
    assert len(provider.requests) == 2
    first, second = provider.requests
    assert first is second
    assert first.selected_llm == selection
    assert first.payload.generation.cache_policy is None  # Owned provider applies configuration.
    text = first.payload.messages[0].content[0].text
    assert text == _acceptance._prompt_text(_acceptance._EXPECTATIONS[values.get('GPT_AUTH_MODE', 'api_key')], text.splitlines()[0])
    assert 'What is the capital of France?' in text
    assert 'A small town library' in text
    assert provider.usages == [Usage(tokens, 2, 0, None, cached), Usage(tokens, 2, cached, None, 0)]
    assert provider.cost == 0.02


@pytest.mark.parametrize('cached', [None, 0, 1, 1023])
def test_missing_or_insufficient_repeat_cache_counter_fails(cached):
    provider = OwnedProvider([result(0, 1024), result(cached)])
    with pytest.raises(AssertionError, match='at least 1024'):
        run(provider)
    assert len(provider.requests) == 2
    assert provider.cost == 0.02


@pytest.mark.parametrize('cached', [None, 0, 1024, 3640])
def test_subscription_requires_over_ninety_percent_reuse(cached):
    provider = OwnedProvider([result(0, 0, 4045), result(cached, 0, 4045)])
    with pytest.raises(AssertionError, match='greater than 90%'):
        run(provider, {'GPT_AUTH_MODE': 'subscription'}, 'gpt-6-luna')
    assert len(provider.requests) == 2


@pytest.mark.parametrize('failure_index', [0, 1])
def test_subscription_input_count_is_exact_not_a_local_tokenizer_estimate(failure_index):
    replies = [result(0, 0, 4045), result(3840, 0, 4045)]
    from dataclasses import replace
    replies[failure_index] = replace(replies[failure_index], usage=replace(replies[failure_index].usage, input_tokens=4044))
    provider = OwnedProvider(replies)
    with pytest.raises(AssertionError, match='exactly 4045 input tokens'):
        run(provider, {'GPT_AUTH_MODE': 'subscription'}, 'gpt-6-luna')
    assert len(provider.requests) == failure_index + 1


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
    replies = [result(0), result()]
    replies[failure_index] = replace(replies[failure_index], usage=replace(replies[failure_index].usage, input_tokens=input_tokens))
    provider = OwnedProvider(replies)
    with pytest.raises(AssertionError, match='exactly 1027 input tokens'):
        run(provider)
    assert len(provider.requests) == failure_index + 1
    assert provider.cost == 0.01 * (failure_index + 1)


@pytest.mark.parametrize('failure_index', [0, 1])
@pytest.mark.parametrize('change,message', [
    ({'outcome': 'length'}, 'did not complete'),
    ({'payload': Message('assistant', (TextContent('Wrong'),))}, 'did not answer Paris'),
])
def test_processing_rejection_has_no_other_invalid_reply_fields(failure_index, change, message):
    from dataclasses import replace
    replies = [result(0), result()]
    replies[failure_index] = replace(replies[failure_index], **change)
    provider = OwnedProvider(replies)
    with pytest.raises(AssertionError, match=message):
        run(provider)
    assert len(provider.requests) == failure_index + 1


@pytest.mark.parametrize('mode,digest', [
    ('api_key', '914ff23fc104e6e60a136880eecac8009ef9375ef0ee4e9c55f50414485f95da'),
    ('subscription', '12a1306993cd84bee3291d0735f124dd0191b98987ab86d4e3fbd4fe2537d933'),
])
def test_calibrated_nonce_prompt_budget_preserved(mode, digest):
    from hashlib import sha256
    assert sha256(_acceptance._prompt_text(_acceptance._EXPECTATIONS[mode], ' '.join('A'*32)).encode()).hexdigest() == digest


def test_nonce_encodes_every_uuid_bit_at_start_without_a_shared_label(monkeypatch):
    from uuid import UUID
    monkeypatch.setattr(_acceptance, 'uuid4', lambda: UUID('01234567-89ab-4def-8123-456789abcdef'))
    nonce = _acceptance._new_prompt_nonce()
    assert len(nonce.split(' ')) == 32
    assert ''.join(format(ord(letter) - ord('A'), 'x') for letter in nonce.split(' ')) == '0123456789ab4def8123456789abcdef'
    text = _acceptance._prompt_text(_acceptance._EXPECTATIONS['api_key'], nonce)
    assert text.startswith(nonce + '\n\nPlease read')
    assert '{run_nonce}' not in text


@pytest.mark.parametrize('mode,occurrences', [('api_key', 1), ('subscription', 4)])
def test_new_cases_have_unique_initial_prefixes(mode, occurrences, monkeypatch):
    from uuid import UUID
    identities = iter([UUID('01234567-89ab-4def-8123-456789abcdef'), UUID('fedcba98-7654-4321-8123-456789abcdef')])
    monkeypatch.setattr(_acceptance, 'uuid4', lambda: next(identities))
    first = _acceptance.prepare_prompt_cache_case({'GPT_AUTH_MODE': mode}, Fixtures(OwnedProvider([]), 'gpt-6-luna'))
    second = _acceptance.prepare_prompt_cache_case({'GPT_AUTH_MODE': mode}, Fixtures(OwnedProvider([]), 'gpt-6-luna'))
    first_text = first.request.payload.messages[0].content[0].text
    second_text = second.request.payload.messages[0].content[0].text
    first_nonce = first_text.splitlines()[0]
    second_nonce = second_text.splitlines()[0]
    assert first_nonce != second_nonce
    assert first_text.count(first_nonce) == occurrences
    assert second_text.count(second_nonce) == occurrences


@pytest.mark.parametrize('cached', [None, 1, 1024])
def test_first_reply_must_prove_fresh_prefix_not_previous_run_cache(cached):
    provider = OwnedProvider([result(cached), result()])
    with pytest.raises(AssertionError, match='First request did not report zero'):
        run(provider)
    assert len(provider.requests) == 1


@pytest.mark.parametrize('values,status,lookups', [
    ({'GPT_AUTH_MODE': 'api_key', 'GPT_API_MODE': 'completions'}, 'passed', ['selection', 'provider']),
    ({'GPT_AUTH_MODE': 'api_key', 'GPT_API_MODE': 'responses'}, 'passed', ['selection', 'provider']),
    ({'GPT_AUTH_MODE': 'subscription', 'GPT_API_MODE': 'responses'}, 'passed', ['selection', 'provider']),
    ({'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'off'}, 'skipped', []),
    ({'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'high'}, 'skipped', []),
])
def test_native_fixture_prepares_case_without_eager_authentication(tmp_path, values, status, lookups):
    import json
    import os
    import subprocess
    import sys
    events = tmp_path / 'events.json'
    script = '''
import importlib.util, json, pathlib, pytest, sys
spec = importlib.util.spec_from_file_location('cache_controls', pathlib.Path('tests/unit/test_prompt_cache_integration.py'))
controls = importlib.util.module_from_spec(spec); spec.loader.exec_module(controls)
events = []
config = {'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': 'https://openrouter.ai/api/v1'} | json.loads(sys.argv[1])
expectation = controls._acceptance._EXPECTATIONS[config['GPT_AUTH_MODE']]
provider = controls.OwnedProvider([controls.result(0, 0, expectation.input_tokens),
    controls.result(expectation.minimum_cached_tokens, 0, expectation.input_tokens)])
class Fixtures:
    @pytest.fixture(scope='session')
    def integration_config(self):
        return controls.SimpleNamespace(values=config)
    @pytest.fixture(scope='session')
    def llm_selection(self):
        events.append('selection')
        return 'gpt-6-luna' if config['GPT_AUTH_MODE'] == 'subscription' else 'openai/gpt-6-luna'
    @pytest.fixture(scope='session')
    def llm_provider(self):
        events.append('provider')
        return provider
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.skipped:
            events.append(report.outcome)
code = pytest.main([str(controls._PATH), '-q'], plugins=[Fixtures()])
pathlib.Path(sys.argv[2]).write_text(json.dumps(events))
raise SystemExit(code)
'''
    run = subprocess.run([sys.executable, '-c', script, json.dumps(values), str(events)],
        cwd=_PATH.parents[6], env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'),
        capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(events.read_text()) == lookups + [status]
