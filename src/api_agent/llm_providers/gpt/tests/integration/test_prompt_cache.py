"""Two owned English requests prove both processing and prompt-cache reuse.

Live OpenRouter GPT-6 Luna calibration found three framing tokens outside the
explicit cache prefix: 1024 reported input tokens gave zero cached tokens,
whereas 1027 reported input tokens allowed a 1024-token cached prefix. This
explains the deliberate 1027 total instead of 1024. Mode-specific fixtures
cover Responses and Chat Completions; any different backend/model framing
fails the exact usage assertion explicitly. Subscription routing instead uses
a 4045-token prefix: 1027/1035/1227-token probes still missed with stable session
headers; 2033 cached 1792 (below 90%), while 4045 cached 3840 (94.9%). Its cache
reporting differs from the explicit API-key boundary. Assert greater than 90%
reuse for that route without relaxing the 1027-token API-key acceptance.
No tokenizer download,
calibration inference, or retry occurs in this test.
"""
from pathlib import Path

import pytest

from api_agent.llm_providers.gpt.cache_policy import _OPENROUTER_FAMILIES, prompt_cache_fields
from api_agent.llm_providers.gpt.endpoint import OFFICIAL_BASE_URL, validate_base_url
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


def _prompt_text(api_mode, subscription=False):
    text = Path(__file__).with_name('prompt_cache_' + api_mode + '.txt').read_text()
    if subscription:
        prefix, question = text.rsplit('What is the capital', 1)
        return prefix * 4 + 'What is the capital' + question.rstrip()
    return text


def test_prompt_cache(integration_config, request):
    values = integration_config.values
    policy = values.get('LLM_CACHE_POLICY') or None
    if policy == 'off':
        pytest.skip('Prompt caching is disabled by LLM_CACHE_POLICY=off.')
    subscription = values.get('GPT_AUTH_MODE', 'subscription') == 'subscription'
    base = OFFICIAL_BASE_URL if subscription else validate_base_url(values.get('GPT_BASE_URL', OFFICIAL_BASE_URL))
    if base not in (OFFICIAL_BASE_URL, 'https://openrouter.ai/api/v1'):
        pytest.skip('This cache-hit test requires an official OpenAI or canonical OpenRouter GPT route.')
    if subscription and policy == 'high':
        pytest.skip('GPT subscription cannot request extended cache retention.')
    selection = request.getfixturevalue('llm_selection')
    # Bound acceptance to known cache-capable families.
    model = selection.removeprefix('openai/')
    import re
    family = re.sub(r'-(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})$', '', model)
    if family not in _OPENROUTER_FAMILIES:
        pytest.skip('The selected GPT family has no cache fixture in this test.')
    try:
        prompt_cache_fields(policy, selection, base_url=base, subscription=subscription)
    except ProviderAccessError as error:
        pytest.skip(error.message)
    provider = request.getfixturevalue('llm_provider')
    supplied = LLMRequest(selection, CompletionRequest((Message('user', (TextContent(_prompt_text(values.get('GPT_API_MODE', 'responses' if subscription else 'completions'), subscription)),)),)))
    expected_input = 4045 if subscription else 1027
    warm = provider.complete(supplied)
    assert warm.outcome == 'completed'
    assert warm.usage.input_tokens == expected_input, f'Warm request did not report exactly {expected_input} input tokens.'
    assert ''.join(part.text for part in warm.payload.content).strip() == 'Paris'
    repeated = provider.complete(supplied)
    assert repeated.outcome == 'completed'
    assert repeated.usage.input_tokens == expected_input, f'Repeated request did not report exactly {expected_input} input tokens.'
    assert ''.join(part.text for part in repeated.payload.content).strip() == 'Paris'
    if subscription:
        assert (repeated.usage.cached_input_tokens or 0) > expected_input * 0.9, 'Repeated subscription request did not report greater than 90% cached input tokens.'
    else:
        assert (repeated.usage.cached_input_tokens or 0) >= 1024, 'Repeated request did not report at least 1024 cached input tokens.'
