"""Two owned English requests prove both processing and prompt-cache reuse.

Live OpenRouter GPT-6 Luna calibration found three framing tokens outside the
explicit cache prefix: 1024 reported input tokens gave zero cached tokens,
whereas 1027 reported input tokens allowed a 1024-token cached prefix. This
explains the deliberate 1027 total instead of 1024. One shared text fixture
covers Responses and Chat Completions; any different backend/model framing
fails the exact usage assertion explicitly. Subscription routing instead uses
a 4045-token prefix: 1027/1035/1227-token probes still missed with stable session
headers; 2033 cached 1792 (below 90%), while 4045 cached 3840 (94.9%). Its cache
reporting differs from the explicit API-key boundary. Assert greater than 90%
reuse for that route without relaxing the 1027-token API-key acceptance.
No tokenizer download,
calibration inference, or retry occurs in this test.
"""
from dataclasses import dataclass
from pathlib import Path
import re

import pytest

from api_agent.llm_providers.gpt.cache_policy import _OPENROUTER_FAMILIES, prompt_cache_fields
from api_agent.llm_providers.gpt.endpoint import OFFICIAL_BASE_URL, validate_base_url
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


@dataclass(frozen=True)
class CacheExpectation:
    input_tokens: int
    minimum_cached_tokens: int
    prefix_repetitions: int
    cache_requirement: str


_EXPECTATIONS = {
    'api_key': CacheExpectation(1027, 1024, 1, 'at least 1024 cached input tokens'),
    'subscription': CacheExpectation(4045, 3641, 4, 'greater than 90% cached input tokens'),
}


@dataclass(frozen=True)
class PromptCacheCase:
    provider: object
    request: LLMRequest
    expectation: CacheExpectation


def _prompt_text(expectation):
    text = Path(__file__).with_name('prompt_cache.txt').read_text()
    prefix, question = text.rsplit('What is the capital', 1)
    return prefix * expectation.prefix_repetitions + 'What is the capital' + question


def prepare_prompt_cache_case(values, fixtures):
    """Determine eligibility before asking for an authenticated model/provider."""
    policy = values.get('LLM_CACHE_POLICY') or None
    if policy == 'off':
        pytest.skip('Prompt caching is disabled by LLM_CACHE_POLICY=off.')
    auth_mode = values.get('GPT_AUTH_MODE', 'subscription')
    subscription = auth_mode == 'subscription'
    base = OFFICIAL_BASE_URL if subscription else validate_base_url(values.get('GPT_BASE_URL', OFFICIAL_BASE_URL))
    if base not in (OFFICIAL_BASE_URL, 'https://openrouter.ai/api/v1'):
        pytest.skip('This cache-hit test requires an official OpenAI or canonical OpenRouter GPT route.')
    if subscription and policy == 'high':
        pytest.skip('GPT subscription cannot request extended cache retention.')
    selection = fixtures.getfixturevalue('llm_selection')
    family = re.sub(r'-(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})$', '', selection.removeprefix('openai/'))
    if family not in _OPENROUTER_FAMILIES:
        pytest.skip('The selected GPT family has no cache fixture in this test.')
    try:
        prompt_cache_fields(policy, selection, base_url=base, subscription=subscription)
    except ProviderAccessError as error:
        pytest.skip(error.message)
    expectation = _EXPECTATIONS[auth_mode]
    supplied = LLMRequest(selection, CompletionRequest((Message('user', (TextContent(_prompt_text(expectation)),)),)))
    return PromptCacheCase(fixtures.getfixturevalue('llm_provider'), supplied, expectation)


@pytest.fixture
def prompt_cache_case(integration_config, request):
    # Keep provider lookup inside this fixture so pytest cannot initialize an
    # eager model-selection dependency before the eligibility checks above.
    return prepare_prompt_cache_case(integration_config.values, request)


def assert_prompt_processed(result, case):
    assert result.outcome == 'completed', 'The request did not complete.'
    expected_input = case.expectation.input_tokens
    assert result.usage.input_tokens == expected_input, f'Request did not report exactly {expected_input} input tokens.'
    assert ''.join(part.text for part in result.payload.content).strip() == 'Paris', 'The request did not answer Paris.'


def assert_cached_prefix_reused(result, case):
    cached = result.usage.cached_input_tokens or 0
    assert cached >= case.expectation.minimum_cached_tokens, 'Repeated request did not report ' + case.expectation.cache_requirement + '.'


def test_prompt_cache(prompt_cache_case):
    warm = prompt_cache_case.provider.complete(prompt_cache_case.request)
    assert_prompt_processed(warm, prompt_cache_case)
    repeated = prompt_cache_case.provider.complete(prompt_cache_case.request)
    assert_prompt_processed(repeated, prompt_cache_case)
    assert_cached_prefix_reused(repeated, prompt_cache_case)
