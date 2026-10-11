"""Shared policies remain distinct from provider-specific retention controls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from api_agent.config import load_shared_schema, resolve_config, ConfigError, ConfigSchema
from api_agent.providers.cache_policy import configured_request, validate_provider_request
from api_agent.providers.contracts import ContractError, ProviderAccessError, ProviderCapabilities
from api_agent.providers.control import OperationalError
from api_agent.providers.inference import request_from_wire, request_to_wire
from api_agent.providers.llm import AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent
from api_agent.llm_providers.gpt.cache_policy import prompt_cache_fields
from api_agent.llm_providers.gpt.factory import GPTProvider
from api_agent.llm_providers.gpt.inference import complete_text
from api_agent.testing.provider_setup import IntegrationLLM

BASE = 'https://api.openai.com/v1'


def request(policy=None, selection='gpt-6.1-sol'):
    return LLMRequest(selection, CompletionRequest((Message('user', (TextContent('Ok'),)),), generation=GenerationOptions(cache_policy=policy)))


@pytest.mark.parametrize('value', [None, '', 'off', 'medium', 'high'])
def test_shared_normalization_wire_and_supported_selection(value):
    req = request(value)
    expected = value or None
    assert req.payload.generation.cache_policy == expected
    wire = request_to_wire(req)
    assert wire['cache_policy'] == expected
    assert request_from_wire(wire) == req
    AvailableSelection(req.selected_llm, features=('cache_policy',)).validate(req)
    if expected:
        with pytest.raises(OperationalError, match='unsupported'):
            AvailableSelection(req.selected_llm).validate(req)


@pytest.mark.parametrize('value', ['0', 'low', '5m', '60m', 'HIGH', False, 0, [], {}])
def test_invalid_shared_policy(value):
    with pytest.raises(ValueError):
        GenerationOptions(cache_policy=value)
    with pytest.raises(ContractError):
        request_from_wire(request_to_wire(request()) | {'cache_policy': value})
    with pytest.raises(ProviderAccessError, match='must be empty'):
        prompt_cache_fields(value, 'gpt-6.1-sol', base_url=BASE, subscription=False)


def test_configuration_is_default_empty_and_obeys_override_precedence():
    schema = load_shared_schema()
    def resolve(**kwargs):
        return resolve_config(ConfigSchema({'LLM_CACHE_POLICY': schema.fields['LLM_CACHE_POLICY']}), **kwargs).raise_for_errors().values['LLM_CACHE_POLICY']
    assert resolve(environ={}) == ''
    assert resolve(dotenv={'LLM_CACHE_POLICY':'off'}, environ={}) == 'off'
    assert resolve(dotenv={'LLM_CACHE_POLICY':'off'}, environ={'LLM_CACHE_POLICY':'medium'}) == 'medium'
    assert resolve(dotenv={'LLM_CACHE_POLICY':'off'}, environ={'LLM_CACHE_POLICY':'medium'}, argv=['--llm-cache-policy=high']) == 'high'
    assert resolve(dotenv={'LLM_CACHE_POLICY':'off'}, environ={}, argv=['--llm-cache-policy=']) == ''
    with pytest.raises(ConfigError):
        resolve(environ={}, argv=['--llm-cache-policy=low'])
    assert schema.fields['LLM_CACHE_POLICY'].acknowledge_default is True


@pytest.mark.parametrize('policy', [None, '', 'off', 'medium', 'high'])
@pytest.mark.parametrize('selection', ['gpt-5.6', 'gpt-5.6-2026-10-01', 'gpt-6.1-sol', 'gpt-6-astra'])
def test_new_openai_selection_mapping(policy, selection):
    result = prompt_cache_fields(policy, selection, base_url=BASE+'/', subscription=False)
    expected = {} if not policy else {'prompt_cache_options': {'mode':'explicit'} if policy=='off' else {'mode':'implicit', 'ttl':'30m'}}
    assert result == expected
    if not policy:
        assert prompt_cache_fields(policy,selection,base_url=BASE,subscription=True)=={}
    else:
        with pytest.raises(ProviderAccessError, match='subscription route'):
            prompt_cache_fields(policy,selection,base_url=BASE,subscription=True)


@pytest.mark.parametrize('selection,medium', [('gpt-4.1','in_memory'), ('gpt-4.1-2025-04-14','in_memory'), ('gpt-5.4','in_memory'), ('gpt-5.5','24h'), ('gpt-5.5-pro','24h')])
def test_documented_older_retention(selection, medium):
    assert prompt_cache_fields('medium',selection,base_url=BASE,subscription=False)=={'prompt_cache_retention':medium}
    assert prompt_cache_fields('high',selection,base_url=BASE,subscription=False)=={'prompt_cache_retention':'24h'}
    with pytest.raises(ProviderAccessError, match='cannot disable'):
        prompt_cache_fields('off',selection,base_url=BASE,subscription=False)
    with pytest.raises(ProviderAccessError, match='subscription route'):
        prompt_cache_fields('medium',selection,base_url=BASE,subscription=True)


@pytest.mark.parametrize('selection', ['gpt-example','gpt-4.1-nano','gpt-4.10','gpt-5.50example','openai/gpt-6.1-sol'])
def test_unknown_family_not_guessed(selection):
    with pytest.raises(ProviderAccessError, match='unknown'):
        prompt_cache_fields('high',selection,base_url=BASE,subscription=False)
    assert prompt_cache_fields(None,selection,base_url=BASE,subscription=False)=={}


@pytest.mark.parametrize('policy', ['off','medium','high'])
def test_alternate_route_rejects_explicit_policy_before_request(policy):
    with pytest.raises(ProviderAccessError,match='compatible backend'):
        prompt_cache_fields(policy,'openai/gpt-6.1-sol',base_url='https://compatible.example/v1',subscription=False)
    assert prompt_cache_fields('', 'openai/gpt-6.1-sol',base_url='https://compatible.example/v1',subscription=False)=={}


def test_configured_policy_preserves_immutable_call_overrides():
    req=request()
    assert configured_request(req,None) is req
    assert configured_request(req,'') is req
    updated=configured_request(req,'medium')
    assert updated.payload.generation.cache_policy=='medium' and req.payload.generation.cache_policy is None
    assert configured_request(updated,'high') is updated
    assert configured_request(None,'off') is None


def test_generic_provider_rejection_and_optional_validator():
    adapter=SimpleNamespace(capabilities=ProviderCapabilities('llm'),validate_request=Mock())
    validate_provider_request(adapter,request())
    adapter.validate_request.assert_called_once()
    with pytest.raises(ProviderAccessError, match='selected LLM provider'):
        validate_provider_request(adapter,request('off'))
    adapter.capabilities=ProviderCapabilities('llm',features=('cache_policy',))
    for policy in ('off','medium','high'):
        validate_provider_request(adapter,request(policy))
    assert adapter.validate_request.call_count==4
    del adapter.validate_request
    validate_provider_request(adapter,request('off'))
    adapter.validate_request=False
    with pytest.raises(ContractError,match='callable'):
        validate_provider_request(adapter,request())


@pytest.mark.parametrize('mode', ['responses','completions'])
@pytest.mark.parametrize('policy', [None, 'off','medium','high'])
def test_actual_outbound_payload_preserves_policy(mode,policy):
    bodies=[]
    def transport(sent):
        bodies.append(json.loads(sent.content))
        data=({'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Ok'}]}]} if mode=='responses' else {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Ok'}}]})
        return httpx.Response(200,json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result=await complete_text(client=client,base_url=BASE,access_token='synthetic-token',subscription=False,api_mode=mode,request=request(policy))
            assert result.payload.content[0].text=='Ok'
    asyncio.run(run())
    assert len(bodies)==1
    expected=prompt_cache_fields(policy,'gpt-6.1-sol',base_url=BASE,subscription=False)
    assert {k:v for k,v in bodies[0].items() if k.startswith('prompt_cache')}==expected
    assert all('prompt_cache_breakpoint' not in row for row in bodies[0].get('input',bodies[0].get('messages',[])))


def test_loaded_gpt_preflight_fails_before_credential_retrieval(tmp_path,monkeypatch):
    from api_agent.testing import provider_setup
    provider=GPTProvider({'GPT_AUTH_MODE':'api_key','LLM_CACHE_POLICY':'off'})
    provider.asetup=AsyncMock()
    loaded=SimpleNamespace(implementation=provider,aclose=AsyncMock())
    monkeypatch.setattr(provider_setup,'aload_provider',AsyncMock(return_value=loaded))
    local=IntegrationLLM({'LLM_PROVIDER':'synthetic','LLM_CACHE_POLICY':'off'},tmp_path)
    try:
        with pytest.raises(ProviderAccessError,match='cannot disable'):
            local.complete(request(selection='gpt-4.1'))
        provider.asetup.assert_not_awaited()
    finally:
        local.close()


@pytest.mark.parametrize('configured', [None,'','off','medium','high'])
def test_local_generic_adapter_receives_effective_policy(tmp_path,monkeypatch,configured):
    from api_agent.testing import provider_setup
    result=LLMResult(Message('assistant',(TextContent('Ok'),)))
    adapter=SimpleNamespace(capabilities=ProviderCapabilities('llm',features=('cache_policy',)),asetup=AsyncMock(return_value=AsyncMock(return_value='secret')),acomplete=AsyncMock(return_value=result),validate_request=Mock())
    monkeypatch.setattr(provider_setup,'aload_provider',AsyncMock(return_value=SimpleNamespace(implementation=adapter,aclose=AsyncMock())))
    local=IntegrationLLM({'LLM_PROVIDER':'synthetic','LLM_CACHE_POLICY':configured},tmp_path)
    try:
        assert local.complete(request())==result
        supplied=adapter.acomplete.call_args.args[0]
        assert supplied.payload.generation.cache_policy==(configured or None)
        assert adapter.validate_request.call_count == 2
        assert all(call.args == (supplied,) for call in adapter.validate_request.call_args_list)
    finally:
        local.close()


def test_backend_transport_rejects_cache_before_login_and_preserves_precedence(tmp_path,monkeypatch):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import SetupBackend
    from api_agent.providers.setup_transport import SetupClient
    adapter=GPTProvider({'GPT_AUTH_MODE':'api_key'})
    adapter.asetup=AsyncMock(return_value=AsyncMock(return_value='secret'))
    adapter.acomplete=AsyncMock(return_value=LLMResult(Message('assistant',(TextContent('Ok'),))))
    monkeypatch.setattr(provider_setup,'aload_provider',AsyncMock(return_value=SimpleNamespace(implementation=adapter,aclose=AsyncMock())))
    backend=SetupBackend({'LLM_PROVIDER':'synthetic','LLM_CACHE_POLICY':'off'},tmp_path)
    client=SetupClient(backend.start())
    try:
        with pytest.raises(ProviderAccessError,match='cannot disable'):
            client.complete(request(selection='gpt-4.1'))
        adapter.asetup.assert_not_awaited()
        result=client.complete(request('medium',selection='gpt-4.1'))
        assert result.payload.content[0].text=='Ok'
        assert adapter.acomplete.call_args.args[0].payload.generation.cache_policy=='medium'
        adapter.asetup.assert_awaited_once()
    finally:
        client.close()
        backend.close()


@pytest.mark.parametrize('auth', ['api_key','subscription'])
@pytest.mark.parametrize('configured,explicit,effective', [('',None,None), ('medium',None,'medium'), ('high','off','off')])
def test_direct_factory_uses_config_and_explicit_request_override(monkeypatch,auth,configured,explicit,effective):
    from api_agent.llm_providers.gpt import inference, factory
    sent=[]
    result=LLMResult(Message('assistant',(TextContent('Ok'),)))
    async def complete(**kwargs):
        sent.append(kwargs['request'])
        return result
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(factory.httpx,'AsyncClient',lambda **kwargs: Client())
    monkeypatch.setattr(inference,'complete_text',complete)
    adapter=GPTProvider({'GPT_AUTH_MODE':auth,'GPT_API_MODE':'responses','LLM_CACHE_POLICY':configured})
    req=request(explicit)
    if auth == 'subscription' and effective:
        with pytest.raises(ProviderAccessError, match='subscription route'):
            asyncio.run(adapter.acomplete(req,auth='secret'))
        assert not sent
        return
    assert asyncio.run(adapter.acomplete(req,auth='secret'))==result
    assert sent[0].payload.generation.cache_policy==effective
    assert req.payload.generation.cache_policy==explicit


def test_revoked_completion_cannot_return_a_cooperative_final_reply():
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.contracts import SessionClosedError
    async def run():
        entered=asyncio.Event()
        result=LLMResult(Message('assistant',(TextContent('Ok'),)))
        async def complete(req):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return result
        adapter=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('operation','identity',lambda:adapter)
        binding=registry.register('worker')
        task=asyncio.create_task(registry.complete(binding,request()))
        try:
            await entered.wait()
            await registry.revoke(binding)
            with pytest.raises(SessionClosedError,match='revoked'):
                await task
            assert not registry._lease(binding)['waiters']
        finally:
            await registry.aclose()
        adapter.aclose.assert_awaited_once()
    asyncio.run(run())
