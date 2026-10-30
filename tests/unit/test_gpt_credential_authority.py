import asyncio

import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.telemetry.privacy import EmissionBoundary


def test_registered_credential_rejected_before_dispatch_and_in_response():
    async def run():
        authority = EmissionBoundary()
        authority.register_secret('registered-password-example')
        calls = []
        async def handler(sent):
            calls.append(sent)
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'registered-password-example'}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            def request(text):
                return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent(text),)),)))
            async def complete(text):
                return await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request(text), authority=authority)
            with pytest.raises(ProviderAccessError):
                await complete('registered-password-example')
            assert not calls
            with pytest.raises(ProviderAccessError):
                await complete('safe input')
            assert len(calls) == 1
    asyncio.run(run())


def test_registration_during_guard_fails_closed(monkeypatch):
    from api_agent.providers import credential_guard
    original = credential_guard.async_guard_json
    async def register_during_guard(value, secrets):
        authority.register_secret('new-credential-example')
        return await original(value, secrets)
    authority = EmissionBoundary()
    monkeypatch.setattr(credential_guard, 'async_guard_json', register_during_guard)
    with pytest.raises(ProviderAccessError):
        asyncio.run(inference._guard_content({'text': 'safe'}, 'active-token-example', authority))


def test_registration_during_result_validation_is_checked_before_return(monkeypatch):
    from api_agent.llm_providers.gpt import rich
    original = rich.async_validate_result
    authority = EmissionBoundary()

    async def validate(request, result, token):
        validated = await original(request, result, token)
        authority.register_secret('late-result-credential')
        return validated

    monkeypatch.setattr(rich, 'async_validate_result', validate)

    async def run():
        async def handler(sent):
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'late-result-credential'}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('safe'),)),)))
            with pytest.raises(ProviderAccessError):
                await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=authority)
    asyncio.run(run())


def test_capture_time_registration_blocks_dispatch(monkeypatch):
    from api_agent.llm_providers import response_policy
    authority = EmissionBoundary()
    calls = []
    original = response_policy.capture_payload

    def capture(*args, **kwargs):
        original(*args, **kwargs)
        authority.register_secret('capture-time-credential')

    monkeypatch.setattr(response_policy, 'capture_payload', capture)

    async def run():
        async def handler(sent):
            calls.append(sent)
            return httpx.Response(500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('capture-time-credential'),)),)))
            with pytest.raises(ProviderAccessError):
                await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=authority)
        assert not calls
    asyncio.run(run())


def test_request_handoff_owns_snapshot_and_orders_registration(monkeypatch):
    from api_agent.providers import credential_guard
    authority = EmissionBoundary()
    original = credential_guard.async_guard_json

    async def run():
        body = {'text': 'original'}
        handed = await inference._handoff_body(body, 'active-token-example', authority)
        body['text'] = 'changed'
        assert handed == {'text': 'original'}
        # A later registration does not retroactively revoke an owned request.
        authority.register_secret('original')
        assert handed == {'text': 'original'}

        async def register_before_handoff(value, secrets):
            checked = await original(value, secrets)
            authority.register_secret('another-credential')
            return checked
        monkeypatch.setattr(credential_guard, 'async_guard_json', register_before_handoff)
        with pytest.raises(ProviderAccessError):
            await inference._handoff_body({'text': 'safe'}, 'active-token-example', authority)

    asyncio.run(run())


def test_cancelled_handoff_does_not_record_dispatch(monkeypatch):
    from api_agent.llm_providers.gpt import response_policy as gpt_policy
    from api_agent.llm_providers import response_policy
    from api_agent.workflows import inference_capture
    events = []
    entered = asyncio.Event()
    evidence = object()
    preparations = []
    def prepare(request, **kwargs):
        preparations.append(True)
        return request, evidence if len(preparations) > 1 else None
    monkeypatch.setattr(gpt_policy, 'prepare_response', prepare)
    monkeypatch.setattr(response_policy, 'capture_payload', lambda *args, **kwargs: None)
    monkeypatch.setattr(inference_capture, 'record_response_dispatch', lambda value: events.append(value))

    async def blocked_handoff(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(inference, '_handoff_body', blocked_handoff)

    async def run():
        async def handler(sent):
            pytest.fail('cancelled request reached transport')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('safe'),)),)))
            task = asyncio.create_task(inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=EmissionBoundary()))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert events == []
    asyncio.run(run())


def test_gpt_authority_binding_precedes_setup_and_cannot_discard_credentials():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    from api_agent.providers.contracts import ContractError
    from api_agent.telemetry.privacy import ContentPolicy

    authority = EmissionBoundary()
    provider = GPTProvider({'GPT_AUTH_MODE': 'api_key'})
    provider.bind_credential_authority(authority)
    provider.bind_credential_authority(authority.for_policy(ContentPolicy(True)))
    with pytest.raises(ContractError):
        provider.bind_credential_authority(EmissionBoundary())
    with pytest.raises(ContractError):
        provider.bind_credential_authority(object())

    late = GPTProvider({'GPT_AUTH_MODE': 'api_key'})
    with pytest.raises(ContractError):
        asyncio.run(late.asetup(interactive=False, user_index=None, credentials=None))
    with pytest.raises(ContractError):
        late.bind_credential_authority(EmissionBoundary())


def test_oauth_registers_received_tokens_before_async_identity_validation(monkeypatch):
    from api_agent.llm_providers.gpt import oauth
    authority = EmissionBoundary()
    async def identity(token, *args):
        _, snapshot = authority.snapshot_content()
        assert {'received-access', 'received-refresh', 'received-id'} <= snapshot._secrets
        raise RuntimeError('identity check stopped')
    monkeypatch.setattr(oauth, '_identity', identity)
    data = {'scope': 'resource.invoke chatgpt.tokens.use.direct offline_access',
            'token_type': 'bearer', 'expires_in': 100,
            'access_token': 'received-access', 'refresh_token': 'received-refresh',
            'id_token': 'received-id'}
    with pytest.raises(RuntimeError, match='identity check stopped'):
        asyncio.run(oauth._connection(data, 'client', 'host', None, None, None, 0,
                                      register_secret=authority.register_secret))


@pytest.mark.parametrize('status,refresh', [(400, False), (401, True)])
def test_oauth_error_response_registers_tokens_before_rejection(status, refresh):
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.contracts import AuthError
    authority = EmissionBoundary()
    async def run():
        async def handler(request):
            return httpx.Response(status, json={'access_token': 'error-access', 'refresh_token': 'error-refresh', 'id_token': 'error-id'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AuthError):
                await oauth._request(client, 'POST', oauth.TOKEN, refresh=refresh,
                                     register_secret=authority.register_secret)
        _, snapshot = authority.snapshot_content()
        assert {'error-access', 'error-refresh', 'error-id'} <= snapshot._secrets
    asyncio.run(run())


def test_login_material_is_registered_before_browser_uses_it(monkeypatch):
    from api_agent.auth.login_credentials import SelectedLoginCredentials
    from api_agent.llm_providers.gpt import factory
    authority = EmissionBoundary()
    provider = factory.GPTProvider({}, emissions=authority)
    material = SelectedLoginCredentials(True, username='user@example.test', password='login-password')
    class Reader:
        async def __call__(self, index, kind):
            raise AssertionError('unexpected credential request')
        async def login_credentials(self, index, **kwargs):
            return material
    monkeypatch.setattr(factory, 'subscription_browser', lambda **kwargs: object())
    async def connect(store, **options):
        assert await options['login_material']() is material
        _, snapshot = authority.snapshot_content()
        assert set(material.secret_values()) <= snapshot._secrets
        return 'profile'
    monkeypatch.setattr(provider, '_connect_account', connect)
    assert asyncio.run(provider._login(None, interactive=False, username=material.username,
                                       user_index=0, credentials=Reader())) == 'profile'


def test_json_credential_escaped_leaf_rejected_in_gpt_response():
    authority = EmissionBoundary(secrets=('[' + '"synthetic-json-auth"' + ']',))
    async def run():
        async def handler(sent):
            data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"payload":["\\\\u0073ynthetic-json-auth"]}'}]}]}
            return httpx.Response(200, json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('safe'),)),)))
            with pytest.raises(ProviderAccessError):
                await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=authority)
    asyncio.run(run())


def test_json_credential_escaped_native_leaf_and_nonmatch():
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    secret = '["synthetic-json-auth"]'
    with pytest.raises(CredentialProtectionError):
        guard_json({'payload': [r'\u0073ynthetic-json-auth']}, {secret})
    assert guard_json({'payload': [r'\u0073ynthetic-json-other']}, {secret}) == {'payload': [r'\u0073ynthetic-json-other']}


def test_json_escaped_quote_preserves_structure_during_credential_check():
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    import json
    secret = json.dumps({'a': 'b"c'})
    candidate = r'{"a":"\u0062\"c"}'
    assert json.loads(candidate) == json.loads(secret)
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': candidate}, {secret})
    safe = r'{"a":"\u0062\"d"}'
    assert guard_json({'text': safe}, {secret}) == {'text': safe}


def test_json_escaped_quote_credential_rejected_in_gpt_result():
    import json
    authority = EmissionBoundary(secrets=(json.dumps({'a': 'b"c'}),))
    candidate = r'{"a":"\u0062\"c"}'
    async def run():
        async def handler(sent):
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': candidate}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('safe'),)),)))
            with pytest.raises(ProviderAccessError):
                await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=authority)
    asyncio.run(run())


def test_nested_json_string_credential_guard_and_gpt_request_response():
    import json
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    secret = json.dumps({'a': 'b"c'}, separators=(',', ':'))
    inner = r'{"a":"\\u0062\"c"}'
    candidate = json.dumps({'outer': inner})
    safe = json.dumps({'outer': r'{"a":"\\u0062\"d"}'})
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': candidate}, {secret})
    assert guard_json({'text': safe}, {secret}) == {'text': safe}

    async def run():
        authority = EmissionBoundary(secrets=(secret,))
        calls = []
        async def handler(sent):
            calls.append(sent)
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': candidate}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            def request(text):
                return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent(text),)),)))
            async def complete(text):
                return await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request(text), authority=authority)
            with pytest.raises(ProviderAccessError):
                await complete(candidate)
            assert not calls
            with pytest.raises(ProviderAccessError):
                await complete(safe)
            assert len(calls) == 1
    asyncio.run(run())


def test_prose_embedded_nested_json_credential_guard_and_gpt():
    import json
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    secret = json.dumps({'a': 'b"c'}, separators=(',', ':'))
    inner = r'{"a":"\\u0062\"c"}'
    candidate = json.dumps({'outer': 'prefix ' + inner + ' suffix'})
    safe = json.dumps({'outer': 'prefix ' + r'{"a":"\\u0062\"d"}' + ' suffix'})
    for text in ('prefix ' + inner, candidate):
        with pytest.raises(CredentialProtectionError):
            guard_json({'text': text}, {secret})
    assert guard_json({'text': safe}, {secret}) == {'text': safe}
    assert guard_json({'text': 'ordinary prose with {unfinished'}, {secret}) == {'text': 'ordinary prose with {unfinished'}

    async def run():
        authority = EmissionBoundary(secrets=(secret,))
        calls = []
        async def handler(sent):
            calls.append(sent)
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': candidate}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            def request(text):
                return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent(text),)),)))
            async def complete(text):
                return await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request(text), authority=authority)
            with pytest.raises(ProviderAccessError):
                await complete(candidate)
            assert not calls
            with pytest.raises(ProviderAccessError):
                await complete(safe)
            assert len(calls) == 1
    asyncio.run(run())


def test_nested_json_carrier_depth_is_bounded():
    import json
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    value = 'ordinary'
    for _ in range(23):
        value = json.dumps({'outer': value})
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': value}, {'["registered"]'})


@pytest.mark.parametrize('prefix,suffix', [('broken {blah ', ''), ('broken [blah ', ']'), ('broken "blah ', '')])
def test_malformed_prose_cannot_hide_json_credential(prefix, suffix):
    import json
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    secret = json.dumps({'a': 'b"c'}, separators=(',', ':'))
    inner = r'{"a":"\\u0062\"c"}'
    text = prefix + inner + suffix
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': text}, {secret})
    assert guard_json({'text': prefix + 'ordinary' + suffix}, {secret}) == {'text': prefix + 'ordinary' + suffix}

    async def run():
        authority = EmissionBoundary(secrets=(secret,))
        calls = []
        async def handler(sent):
            calls.append(sent)
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            def request(content):
                return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent(content),)),)))
            async def complete(content):
                return await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request(content), authority=authority)
            with pytest.raises(ProviderAccessError):
                await complete(text)
            assert not calls
            with pytest.raises(ProviderAccessError):
                await complete('safe')
            assert len(calls) == 1
    asyncio.run(run())


def test_quote_heavy_safe_text_passes_guard_and_gpt_round_trip():
    from api_agent.providers.credential_guard import guard_json
    safe = 'He said "hello". ' * 2000
    secret = '{"token":"synthetic"}'
    assert guard_json({'text': safe}, {secret}) == {'text': safe}
    async def run():
        async def handler(sent):
            import json
            assert json.loads(sent.content)['input'][0]['content'] == safe
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': safe}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent(safe),)),)))
            result = await inference.complete_text(client=client, base_url='https://api.openai.com/v1', access_token='active-token-example', subscription=False, api_mode='responses', request=request, authority=EmissionBoundary(secrets=(secret,)))
            assert result.payload.content[0].text == safe
    asyncio.run(run())


def test_malformed_carrier_recovery_work_is_bounded():
    from api_agent.providers.credential_guard import guard_json, CredentialProtectionError
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': '{' * 10000}, {'{"token":"synthetic"}'})


def test_oauth_error_waits_for_async_credential_admission_before_rejection():
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.contracts import AuthError
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        admitted = []
        retained = []
        async def register(value):
            entered.set()
            await release.wait()
            admitted.append(value)
        async def handler(request):
            return httpx.Response(401, json={'access_token': 'error-access', 'refresh_token': 'error-refresh', 'id_token': 'error-id'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = asyncio.create_task(oauth._request(client, 'POST', oauth.TOKEN, refresh=True,
                register_secret=register, retain_result=retained.append))
            await entered.wait()
            assert not request.done()
            release.set()
            with pytest.raises(AuthError, match='refresh_rejected'):
                await request
        assert admitted == ['error-access', 'error-refresh', 'error-id']
        assert retained == []
    asyncio.run(run())


def test_direct_gpt_discovery_registers_supplied_auth_before_request(monkeypatch):
    from api_agent.llm_providers.gpt import discovery
    from api_agent.llm_providers.gpt.factory import GPTProvider
    authority = EmissionBoundary()
    token = 'synthetic-direct-discovery-token'
    async def discover(**kwargs):
        assert token in authority._secrets
        assert kwargs['authority'] is authority
        return ()
    monkeypatch.setattr(discovery, 'discover_selections', discover)
    provider = GPTProvider({'GPT_AUTH_MODE': 'api_key'}, emissions=authority)
    assert asyncio.run(provider.adescribe(auth=token)) == ()
