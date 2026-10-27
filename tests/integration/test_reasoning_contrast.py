"""Three matched pairs through the ordinary provider and real application API."""
import asyncio
import json
import secrets

import httpx
import pytest

from api_agent.testing.reasoning_contrast import evaluate, run_pairs, scope, reasoning_timeout
from api_agent.testing.studio_http import agent_endpoint
from api_agent.api.chat_contract import MAX_OUTPUT_TOKENS
from api_agent.testing.reasoning_provider import reasoning_provider
from api_agent.providers.progress import report_timeout_sync

pytestmark = [pytest.mark.feature('Reasoning contrast'), pytest.mark.requires_llm]


@pytest.fixture
def reasoning_ready(integration_config):
    config = integration_config.values
    timeout = reasoning_timeout(config)
    model = scope(config, owner_root=integration_config.owner_root)
    efforts = tuple(config.get(key, '') for key in (
        'LLM_REASONING_TEST_LOW_EFFORT', 'LLM_REASONING_TEST_HIGH_EFFORT'))
    if not all(effort in ('off', 'low', 'medium', 'high') for effort in efforts) or ('off', 'low', 'medium', 'high').index(efforts[0]) >= ('off', 'low', 'medium', 'high').index(efforts[1]):
        raise ValueError('Reasoning contrast requires two distinct qualified explicit effort controls')
    limit = config.get('LLM_REASONING_TEST_MAX_OUTPUT_TOKENS')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_TOKENS):
        raise ValueError(f'Reasoning contrast output limit must be a positive integer at most {MAX_OUTPUT_TOKENS} or unset')
    return model, efforts, limit, timeout


def test_reasoning_contrast(reasoning_ready, request, record_property):
    provider = request.getfixturevalue('llm_provider')
    from api_agent.testing.model_choices import catalog_choice
    model, efforts, limit, timeout = reasoning_ready
    events = []
    async def scenario():
        nonce = secrets.token_urlsafe(32)
        async with reasoning_provider(provider) as parallel, agent_endpoint(parallel, model, 'http://127.0.0.1:8080', nonce, timeout=timeout) as endpoint:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                return await run_pairs(client, endpoint, nonce, model=model, efforts=efforts, max_output_tokens=limit, timeout_seconds=timeout, emit=events.append)
    try:
        try:
            with report_timeout_sync('Reasoning contrast catalog preparation', getattr(provider, 'config', {}).get('LLM_SETUP_REQUEST_TIMEOUT_SECONDS', 300)):
                catalog = provider.describe()
        except pytest.skip.Exception as error:
            raise AssertionError('Reasoning contrast catalog setup was unavailable.') from error
        row = catalog_choice(catalog, model, efforts, require_metadata=False)
        if limit is not None and 'max_output_tokens' not in row.features:
            raise ValueError('Reasoning contrast output limit requires advertised model support')
        if limit is not None and row.max_output_tokens is not None and limit > row.max_output_tokens:
            raise ValueError('Reasoning contrast output limit exceeds the advertised model limit')
        records = asyncio.run(scenario())
        score = evaluate(records, model=model, efforts=efforts)
        assert score['passed'], score
    finally:
        record_property('reasoning_comparison', json.dumps(events, sort_keys=True))
