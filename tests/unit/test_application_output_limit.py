"""Application output limits reject before inference and preserve exact budgets."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.api.chat_contract import MAX_OUTPUT_TOKENS, completion
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
from api_agent.testing.studio_http import agent_endpoint


def body(maximum):
    return {'session_id':str(uuid4()), 'messages':[{'role':'user','content':'Hello'}],
        'max_output_tokens':maximum}


@pytest.mark.parametrize('maximum', [1, 4096, 4097, 16000, 16384, 200000, 1048576])
def test_parser_accepts_exact_application_output_budget(maximum):
    assert MAX_OUTPUT_TOKENS == 1048576
    _, request = completion(body(maximum), 'example', None)
    assert request.payload.generation.max_output_tokens == maximum


@pytest.mark.parametrize('maximum', [1048577, True, False, 0, -1, None, '16384', 16384.0])
def test_parser_rejects_invalid_output_budget(maximum):
    with pytest.raises(ContractError, match='Invalid output limit'):
        completion(body(maximum), 'example', None)


@pytest.mark.parametrize('maximum', [4096, 16000, 16384])
def test_real_http_preserves_accepted_budget_into_provider(maximum):
    calls=[]
    def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant',(TextContent('answer'),)))
    provider = SimpleNamespace(describe=lambda:(AvailableSelection('example',
        features=('max_output_tokens',), max_output_tokens=20000),), complete=complete)
    async def run():
        async with agent_endpoint(provider, 'example', 'http://127.0.0.1:8080', 'n'*32) as endpoint:
            async with httpx.AsyncClient(trust_env=False) as client:
                return await client.post(endpoint+'/app/agent/chat',json=body(maximum),
                    headers={'Origin':'http://127.0.0.1:8080','X-API-Agent-Session':'n'*32})
    response=asyncio.run(run())
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0].payload.generation.max_output_tokens == maximum


@pytest.mark.parametrize('maximum', [1048577, True, 0, None, '16384'])
def test_real_http_rejects_invalid_budget_before_provider(maximum):
    calls=[]
    provider=SimpleNamespace(describe=lambda:calls.append('describe'), complete=lambda request:calls.append('complete'))
    async def run():
        async with agent_endpoint(provider, 'example', 'http://127.0.0.1:8080', 'n'*32) as endpoint:
            async with httpx.AsyncClient(trust_env=False) as client:
                return await client.post(endpoint+'/app/agent/chat',json=body(maximum),
                    headers={'Origin':'http://127.0.0.1:8080','X-API-Agent-Session':'n'*32})
    response=asyncio.run(run())
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize('maximum', [16001, 16384])
def test_bedrock_encoder_rejects_budget_above_advertised_16000(maximum):
    from api_agent.llm_providers.bedrock.mantle import encode
    from api_agent.providers.control import OperationalError
    from api_agent.providers.llm import LLMRequest, CompletionRequest, GenerationOptions
    request=LLMRequest('openai.gpt-oss-20b', CompletionRequest((Message('user',(TextContent('Hello'),)),),
        generation=GenerationOptions(max_output_tokens=maximum, reasoning_effort='high')))
    with pytest.raises(OperationalError, match='unsupported'):
        encode(request,'responses')
