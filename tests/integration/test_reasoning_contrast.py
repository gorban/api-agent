"""Three matched pairs through the ordinary provider and real application API."""
import asyncio
import json
import secrets

import httpx
import pytest

from api_agent.testing.reasoning_contrast import MODEL, evaluate, run_pairs, scope
from api_agent.testing.studio_http import agent_endpoint
from api_agent.testing.reasoning_provider import reasoning_provider

pytestmark = [pytest.mark.feature('Reasoning contrast'), pytest.mark.requires_llm]


@pytest.fixture
def reasoning_ready(integration_config):
    scope(integration_config.values, owner_root=integration_config.owner_root)


def test_reasoning_contrast(reasoning_ready, request, record_property):
    provider = request.getfixturevalue('llm_provider')
    events = []
    async def scenario():
        nonce = secrets.token_urlsafe(32)
        async with reasoning_provider(provider) as parallel, agent_endpoint(parallel, MODEL, 'http://127.0.0.1:8080', nonce) as endpoint:
            async with httpx.AsyncClient(timeout=155, trust_env=False, follow_redirects=False) as client:
                return await run_pairs(client, endpoint, nonce, emit=events.append)
    try:
        records = asyncio.run(scenario())
        score = evaluate(records)
        assert score['passed'], score
    finally:
        record_property('reasoning_comparison', json.dumps(events, sort_keys=True))
