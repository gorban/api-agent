"""GPT calls inside a flow take their budget from the enclosing declaration; catalog discovery declares its own."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.llm_providers.gpt import budgets, discovery, inference
from api_agent.llm_providers.gpt.factory import GPTProvider
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
from api_agent.providers.progress import component_timeout, progress_sink

TOKEN = 'synthetic-selected-token'
BASE = 'https://api.openai.com/v1'
CATALOG = {'data': [{'id': 'gpt-example'}]}
COMPLETION = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [
    {'type': 'output_text', 'text': 'Ok'}]}], 'usage': {'input_tokens': 3, 'output_tokens': 1}}


def request():
    return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Say Ok'),)),),
                                                      generation=GenerationOptions(64)))


def reported(rows, name):
    return [row.timeout for row in rows if row.name == name]


def infer(handler, *, enclosing=None):
    async def scenario():
        rows = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with progress_sink(rows.append):
                async def call():
                    return await inference.complete_text(
                        client=client, base_url=BASE, access_token=TOKEN, subscription=False,
                        api_mode='responses', request=request())
                if enclosing is None:
                    await call()
                else:
                    async with component_timeout('Declared node', enclosing):
                        await call()
        return rows
    return asyncio.run(scenario())


def test_inference_declares_no_budget_of_its_own():
    rows = infer(lambda sent: httpx.Response(200, json=COMPLETION))

    assert reported(rows, 'GPT text inference') == [None, None]


def test_an_enclosing_budget_longer_than_the_old_hidden_limit_is_not_clipped():
    seen = []
    def handler(sent):
        seen.append(sent.extensions['timeout']['read'])
        return httpx.Response(200, json=COMPLETION)

    infer(handler, enclosing=500)

    assert seen[0] > 120


def test_inference_is_bounded_by_the_enclosing_declared_budget():
    async def handler(sent):
        await asyncio.sleep(30)

    async def scenario():
        started = asyncio.get_running_loop().time()
        with pytest.raises(Exception) as caught:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with component_timeout('Declared node', 0.1):
                    await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN, subscription=False,
                                                  api_mode='responses', request=request())
        return caught.value, asyncio.get_running_loop().time() - started
    error, elapsed = asyncio.run(scenario())

    assert (isinstance(error, TimeoutError) or getattr(error, 'kind', None) == 'deadline') and elapsed < 5


def discover_rows(timeout=None, *, models=True):
    async def scenario():
        rows = []
        handler = (lambda sent: httpx.Response(200, json=CATALOG if sent.url.path.endswith('/models')
                                               else {'data': {'label': 'selected'}}))
        kwargs = {} if timeout is None else {'timeout': timeout}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with progress_sink(rows.append):
                if models:
                    await discovery.discover_models(client=client, base_url=BASE, access_token=TOKEN,
                                                    subscription=False, **kwargs)
                else:
                    await discovery.discover_selections(client=client, base_url='https://other.example/v1',
                                                        access_token=TOKEN, subscription=False, **kwargs)
        return rows
    return asyncio.run(scenario())


@pytest.mark.parametrize('models,name', [(True, 'GPT model discovery'), (False, 'GPT authenticated discovery')])
def test_discovery_uses_its_declared_default_or_the_passed_budget(models, name):
    assert set(reported(discover_rows(models=models), name)) == {budgets.DEFAULT_DISCOVERY_TIMEOUT}
    assert set(reported(discover_rows(7, models=models), name)) == {7}


def test_the_declared_default_is_the_documented_fifteen_seconds():
    from api_agent.config.declarations import schema_from_dict
    import tomllib
    path = Path(discovery.__file__).parent / 'cfg' / 'config.toml'
    field = schema_from_dict(tomllib.loads(path.read_text())).fields['GPT_DISCOVERY_TIMEOUT_SECONDS']

    assert field.default == budgets.DEFAULT_DISCOVERY_TIMEOUT == 15
    assert field.type == 'float' and field.help


@pytest.mark.parametrize('config,expected', [({}, 15), ({'GPT_DISCOVERY_TIMEOUT_SECONDS': 7}, 7),
                                              ({'GPT_DISCOVERY_TIMEOUT_SECONDS': 2.5}, 2.5)])
def test_the_provider_passes_its_declared_discovery_budget(monkeypatch, config, expected):
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client)
    read = AsyncMock(return_value=())
    monkeypatch.setattr(discovery, 'discover_selections', read)

    asyncio.run(GPTProvider({'GPT_AUTH_MODE': 'api_key', **config}).adescribe(auth=TOKEN))

    assert read.await_args.kwargs['timeout'] == expected


@pytest.mark.parametrize('value', [True, False, 0, -1, float('inf'), float('nan'), '7', None, [], {}])
def test_an_invalid_discovery_budget_is_rejected_when_the_provider_is_built(value):
    with pytest.raises(ContractError, match='GPT_DISCOVERY_TIMEOUT_SECONDS'):
        GPTProvider({'GPT_DISCOVERY_TIMEOUT_SECONDS': value})
