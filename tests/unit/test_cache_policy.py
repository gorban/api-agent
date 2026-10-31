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
OPENROUTER = 'https://openrouter.ai/api/v1'


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
    if policy in (None, '', 'medium'):
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
    assert prompt_cache_fields('medium',selection,base_url=BASE,subscription=True)=={}


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


@pytest.mark.parametrize('base', [OPENROUTER, 'https://OPENROUTER.AI/api/v1/'])
@pytest.mark.parametrize('selection', ['openai/gpt-5.6-sol', 'openai/gpt-5.6-sol-pro', 'openai/gpt-6-astra', 'openai/gpt-6-luna', 'openai/gpt-6-luna-2026-10-01', 'openai/gpt-6.1-sol'])
@pytest.mark.parametrize('policy,expected', [(None, {}), ('', {}), ('off', {'prompt_cache_options': {'mode': 'explicit'}}), ('medium', {'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}}), ('high', {'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}})])
def test_openrouter_known_families_and_normalized_endpoint(base, selection, policy, expected):
    assert prompt_cache_fields(policy, selection, base_url=base, subscription=False) == expected


@pytest.mark.parametrize('selection', ['gpt-6-luna', 'openai/gpt-5.5', 'openai/gpt-4.1', 'openai/gpt-6-unknown', 'openai/gpt-7', 'anthropic/claude-sonnet-4', 'google/gemini-2.5-pro'])
@pytest.mark.parametrize('policy', ['off', 'medium', 'high'])
def test_openrouter_unknown_or_older_models_reject(selection, policy):
    with pytest.raises(ProviderAccessError, match='unknown.*OpenRouter'):
        prompt_cache_fields(policy, selection, base_url=OPENROUTER, subscription=False)
    assert prompt_cache_fields('', selection, base_url=OPENROUTER, subscription=False) == {}


@pytest.mark.parametrize('base', ['https://openrouter.ai/api/V1', 'https://openrouter.ai:443/api/v1', 'https://openrouter.ai/api/v1/other', 'https://openrouter.ai.example/api/v1', 'https://other.example/api/v1'])
def test_openrouter_requires_canonical_endpoint(base):
    with pytest.raises(ProviderAccessError, match='compatible backend'):
        prompt_cache_fields('medium', 'openai/gpt-6-luna', base_url=base, subscription=False)
    assert prompt_cache_fields(None, 'openai/gpt-6-luna', base_url=base, subscription=False) == {}


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
    adapter.credential_protection_version = 1
    adapter.bind_credential_authority = lambda authority: None
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
    adapter.adescribe=AsyncMock(return_value=(AvailableSelection('gpt-4.1',features=('cache_policy','max_output_tokens')),))
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
    if auth == 'subscription' and effective in ('off', 'high'):
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


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('configured,override,effective', [('', None, None), ('off', None, 'off'), ('medium', None, 'medium'), ('high', None, 'high'), ('high', 'off', 'off'), ('off', 'medium', 'medium')])
def test_openrouter_actual_effective_payload(mode, configured, override, effective, monkeypatch):
    from api_agent.llm_providers.gpt import factory
    bodies = []
    original_client = httpx.AsyncClient
    def transport(sent):
        body = json.loads(sent.content)
        bodies.append(body)
        options = body.get('prompt_cache_options')
        if effective:
            assert options == ({'mode': 'explicit'} if effective == 'off' else {'mode': 'explicit', 'ttl': '30m'})
            assert options['mode'] == 'explicit'  # Published OpenRouter enum.
        else:
            assert options is None
        rows = body['input' if mode == 'responses' else 'messages']
        expected_tail = 'Task' if effective in (None, 'off') else [{'type': 'input_text' if mode == 'responses' else 'text', 'text': 'Task', 'prompt_cache_breakpoint': {'mode': 'explicit'}}]
        assert rows == [{'role': 'system', 'content': 'Prefix'}, {'role': 'assistant', 'content': 'Earlier'}, {'role': 'user', 'content': expected_tail}]
        data = ({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}], 'usage': {'input_tokens': 1200, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}}} if mode == 'responses' else {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Ok'}}], 'usage': {'prompt_tokens': 1200, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}}})
        return httpx.Response(200, json=data)
    monkeypatch.setattr(factory.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    req = LLMRequest('openai/gpt-6-luna', CompletionRequest((Message('system', (TextContent('Prefix'),)), Message('assistant', (TextContent('Earlier'),)), Message('user', (TextContent('Task'),))), generation=GenerationOptions(cache_policy=override)))
    original_wire = request_to_wire(req)
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': OPENROUTER, 'GPT_API_MODE': mode, 'LLM_CACHE_POLICY': configured})
    result = asyncio.run(adapter.acomplete(req, auth='synthetic-token'))
    assert result.usage.cached_input_tokens == 1000
    assert result.usage.cache_write_tokens == 200
    assert result.usage.input_tokens == 1200
    assert request_to_wire(req) == original_wire
    assert len(bodies) == 1


@pytest.mark.parametrize('selection,base,tail', [('openai/gpt-5.5', OPENROUTER, 'Task'), ('openai/gpt-6-unknown', OPENROUTER, 'Task'), ('openai/gpt-6-luna', 'https://other.example/api/v1', 'Task'), ('openai/gpt-6-luna', OPENROUTER, ''), ('openai/gpt-6-luna-2099-99-99', OPENROUTER, 'Task'), ('openai/gpt-6-luna-20999999', OPENROUTER, 'Task'), ('openai/gpt-6-luna-２０９９９９９９', OPENROUTER, 'Task')])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_openrouter_unsupported_effective_request_precedes_auth(tmp_path, monkeypatch, selection, base, tail, mode):
    from api_agent.testing import provider_setup
    adapter = GPTProvider({'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': base, 'GPT_API_MODE': mode})
    adapter.asetup = AsyncMock()
    adapter.acomplete = AsyncMock()
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=SimpleNamespace(implementation=adapter, aclose=AsyncMock())))
    local = IntegrationLLM({'LLM_PROVIDER': 'synthetic', 'LLM_CACHE_POLICY': 'medium'}, tmp_path)
    req = LLMRequest(selection, CompletionRequest((Message('user', (TextContent(tail),)),)))
    try:
        with pytest.raises(ProviderAccessError):
            local.complete(req)
        adapter.asetup.assert_not_awaited()
        adapter.acomplete.assert_not_awaited()
    finally:
        local.close()


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('role', ['system', 'user', 'assistant'])
def test_openrouter_final_block_preserves_multipart_unicode_long_text(mode, role):
    from api_agent.llm_providers.gpt.inference import validate_text_request
    text = 'Reusable λ🙂 prefix\n' * 200
    req = LLMRequest('openai/gpt-6-luna-20261001', CompletionRequest((Message('user', (TextContent('Earlier'),)), Message(role, (TextContent(text), TextContent('尾')))), generation=GenerationOptions(cache_policy='high')))
    before = req.payload
    _, body = validate_text_request(base_url=OPENROUTER, access_token='synthetic-token', subscription=False, api_mode=mode, request=req)
    rows = body['input' if mode == 'responses' else 'messages']
    assert rows == [{'role': 'user', 'content': 'Earlier'}, {'role': role, 'content': [{'type': 'input_text' if mode == 'responses' else 'text', 'text': text + '尾', 'prompt_cache_breakpoint': {'mode': 'explicit'}}]}]
    # Published EasyInputMessage allows assistant with InputText content too.
    assert rows[-1]['role'] in ('user', 'system', 'assistant', 'developer')
    assert req.payload == before
    assert req.payload.messages[-1].content == (TextContent(text), TextContent('尾'))


@pytest.mark.parametrize('suffix', ['2099-99-99', '20999999', '２０９９９９９９', '2026-02-29', '20260229', '0000-01-01', '2026-13-01'])
@pytest.mark.parametrize('policy', ['off', 'medium', 'high'])
def test_openrouter_invalid_calendar_or_nonascii_snapshot_rejects(suffix, policy):
    with pytest.raises(ProviderAccessError, match='unknown.*OpenRouter'):
        prompt_cache_fields(policy, 'openai/gpt-6-luna-' + suffix, base_url=OPENROUTER, subscription=False)


@pytest.mark.parametrize('suffix', ['2024-02-29', '20240229', '2099-12-31', '20991231'])
def test_openrouter_valid_calendar_snapshot_not_frozen_to_catalog(suffix):
    assert prompt_cache_fields('medium', 'openai/gpt-6-luna-' + suffix, base_url=OPENROUTER, subscription=False) == {'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}}


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('invalid_billing', [{'id': 42}, {'id': 'invalid generation id'}, {'request_id': 'invalid request id'}, {'usage_cost': -1}])
def test_openrouter_billing_failure_preserves_valid_cache_usage(mode, invalid_billing):
    usage = {'input_tokens': 1200, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}} if mode == 'responses' else {'prompt_tokens': 1200, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}}
    data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}]} if mode == 'responses' else {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Ok'}}]}
    data['usage'] = usage
    if 'usage_cost' in invalid_billing:
        usage['cost'] = invalid_billing['usage_cost']
    else:
        data.update(invalid_billing)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent: httpx.Response(200, json=data))) as client:
            with pytest.raises(ProviderAccessError, match='invalid billing metadata') as caught:
                await complete_text(client=client, base_url=OPENROUTER, access_token='synthetic-token', subscription=False, api_mode=mode, request=request('medium', selection='openai/gpt-6-luna'))
            assert caught.value.usage.input_tokens == 1200
            assert caught.value.usage.output_tokens == 2
            assert caught.value.usage.cached_input_tokens == 1000
            assert caught.value.usage.cache_write_tokens == 200
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_openrouter_billing_credential_is_rejected_without_disclosure(mode):
    usage = {'input_tokens': 1200, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}} if mode == 'responses' else {'prompt_tokens': 1200, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 1000, 'cache_write_tokens': 200}}
    data = {'id': 'synthetic-token', 'usage': usage}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent: httpx.Response(200, json=data))) as client:
            with pytest.raises(ProviderAccessError, match='protected authentication content') as caught:
                await complete_text(client=client, base_url=OPENROUTER, access_token='synthetic-token', subscription=False, api_mode=mode, request=request('medium', selection='openai/gpt-6-luna'))
            assert 'synthetic-token' not in str(caught.value)
            assert caught.value.usage.input_tokens == 1200
            assert caught.value.usage.output_tokens == 2
            assert caught.value.usage.cached_input_tokens == 1000
            assert caught.value.usage.cache_write_tokens == 200
    asyncio.run(run())


@pytest.mark.parametrize('policy', [None, '', 'medium', 'off', 'high'])
def test_subscription_cache_payload_and_rejection_before_http(policy):
    sent = []
    def transport(outbound):
        sent.append(json.loads(outbound.content))
        data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}], 'usage': {'input_tokens': 1027, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 1024}}}
        return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n', headers={'content-type': 'text/event-stream'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await complete_text(client=client, base_url=BASE, access_token='synthetic-token', subscription=True, api_mode='responses', request=request(policy))
    if policy in ('off', 'high'):
        with pytest.raises(ProviderAccessError, match='subscription route cannot'):
            asyncio.run(run())
        assert sent == []
    else:
        returned = asyncio.run(run())
        assert returned.payload.content[0].text == 'Ok'
        assert returned.usage.cached_input_tokens == 1024
        assert len(sent) == 1
        assert sent[0] == {'model': 'gpt-6.1-sol', 'input': [{'role': 'user', 'content': 'Ok'}], 'store': False, 'stream': True}


@pytest.mark.parametrize('explicit', [None, 'medium', 'off', 'high'])
def test_subscription_medium_through_backend_and_explicit_override(tmp_path, monkeypatch, explicit):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import SetupBackend
    from api_agent.providers.setup_transport import SetupClient
    adapter = GPTProvider({'GPT_AUTH_MODE': 'subscription', 'LLM_CACHE_POLICY': 'medium'})
    adapter.asetup = AsyncMock(return_value=AsyncMock(return_value='secret'))
    adapter.acomplete = AsyncMock(return_value=LLMResult(Message('assistant', (TextContent('Ok'),))))
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=SimpleNamespace(implementation=adapter, aclose=AsyncMock())))
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'LLM_CACHE_POLICY': 'medium'}, tmp_path)
    client = SetupClient(backend.start())
    try:
        if explicit in ('off', 'high'):
            with pytest.raises(ProviderAccessError, match='subscription route cannot'):
                client.complete(request(explicit))
            adapter.asetup.assert_not_awaited()
            adapter.acomplete.assert_not_awaited()
        else:
            assert client.complete(request(explicit)).payload.content[0].text == 'Ok'
            assert adapter.acomplete.call_args.args[0].payload.generation.cache_policy == 'medium'
            adapter.asetup.assert_awaited_once()
    finally:
        client.close()
        backend.close()
