"""No-inference verification rejects wrong credentials without fallback."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.llm_providers.gpt.discovery import discover_selections, ModelDiscoveryError
from api_agent.llm_providers.gpt.factory import GPTProvider
from api_agent.providers.progress import component_timeout, report_timeout
from api_agent.providers.contracts import ContractError

BASE = 'https://api.openai.com/v1'
SECRET = 'synthetic-selected-password'

def catalog(subscription=False):
    return {'models': [{'slug': 'gpt-example', 'display_name': 'Example', 'visibility': 'list'}]} if subscription else {'data': [{'id': 'gpt-example'}]}


def invoke(handler, *, subscription=False, token=SECRET, base=BASE):
    requests = []
    def receive(request):
        requests.append(request)
        return handler(request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            result = await discover_selections(client=client, base_url=base, access_token=token, subscription=subscription)
            assert not client.is_closed
            return result
    return asyncio.run(run()), requests


@pytest.mark.parametrize('subscription', [False, True])
def test_authenticated_catalog_reuses_exact_selected_credential(subscription):
    def handler(request):
        if 'Authorization' not in request.headers:
            return httpx.Response(401, json={})
        assert request.headers['Authorization'] == 'Bearer ' + SECRET
        return httpx.Response(200, json=catalog(subscription))
    rows, requests = invoke(handler, subscription=subscription)
    assert rows[0].selected_llm == 'gpt-example'
    assert len(requests) == 2 and all(request.method == 'GET' for request in requests)
    assert 'Authorization' not in requests[0].headers
    assert all(str(request.url) == BASE + '/models' for request in requests)


@pytest.mark.parametrize('status,kind', [(401, 'authentication'), (403, 'authorization'), (429, 'rate_limited'), (500, 'unavailable')])
def test_remote_rejection_never_passes_or_tries_another_credential(status, kind):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(status, json={'error': {'message': 'Access denied: ' + SECRET, 'code': 'denied'}})
    with pytest.raises(ModelDiscoveryError) as caught:
        invoke(handler)
    assert len(calls) == 2
    assert caught.value.kind == kind and caught.value.status == status and caught.value.code is None
    assert SECRET not in str(caught.value) and caught.value.__suppress_context__


def test_safe_remote_rejection_retains_code_and_classification():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={'error': {'message': 'Access denied', 'code': 'denied'}})
    with pytest.raises(ModelDiscoveryError) as caught:
        invoke(handler)
    assert len(calls) == 2
    assert (caught.value.kind, caught.value.status, caught.value.code) == ('authentication', 401, 'denied')
    assert caught.value.message == 'Access denied'


def test_registered_other_credential_in_error_is_not_released():
    from api_agent.telemetry.privacy import EmissionBoundary
    other = 'synthetic-other-registered-credential'
    boundary = EmissionBoundary()
    boundary.register_secret(other)
    async def run():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, json={'error': {'message': 'Access denied ' + other, 'code': other}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelDiscoveryError) as caught:
                await discover_selections(client=client, base_url=BASE, access_token=SECRET, subscription=False, authority=boundary)
        assert len(calls) == 1
        assert (caught.value.kind, caught.value.status, caught.value.code) == ('authentication', 401, None)
        assert other not in str(caught.value) and caught.value.__suppress_context__
    asyncio.run(run())


def test_credential_registered_during_key_verification_rejects_prior_catalog():
    from api_agent.telemetry.privacy import EmissionBoundary
    boundary = EmissionBoundary()
    other = 'synthetic-late-catalog-secret'
    async def handler(request):
        if request.url.path.endswith('/key'):
            boundary.register_secret(other)
            return httpx.Response(200, json={'data': {'label': 'selected'}})
        return httpx.Response(200, json={'data': [{'id': 'gpt-example', 'name': other}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelDiscoveryError) as caught:
                await discover_selections(client=client, base_url=BASE, access_token=SECRET, subscription=False, authority=boundary)
        assert caught.value.kind == 'unknown' and other not in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize('public', [False, True])
def test_catalog_handoff_rejects_registration_during_validation(monkeypatch, public):
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.providers import credential_guard
    boundary = EmissionBoundary()
    original = credential_guard.async_guard_json
    async def racing_guard(value, secrets):
        checked = await original(value, secrets)
        if type(value) is list and value and type(value[0]) is dict and 'selected_llm' in value[0]:
            boundary.register_secret('synthetic-concurrently-registered-secret')
        return checked
    monkeypatch.setattr(credential_guard, 'async_guard_json', racing_guard)
    def handler(request):
        if request.url.path.endswith('/key'):
            return httpx.Response(200, json={'data': {'label': 'selected'}})
        if not public and 'Authorization' not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json=catalog())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelDiscoveryError) as caught:
                await discover_selections(client=client, base_url=BASE, access_token=SECRET, subscription=False, authority=boundary)
        assert caught.value.kind == 'unknown' and caught.value.__suppress_context__
    asyncio.run(run())


@pytest.mark.parametrize('status', [403, 404, 500, 302])
def test_anonymous_other_errors_do_not_trigger_credentialed_protocol_guess(status):
    calls = []
    def handler(request):
        calls.append(request)
        assert 'Authorization' not in request.headers
        return httpx.Response(status)
    with pytest.raises(ModelDiscoveryError):
        invoke(handler)
    assert len(calls) == 1


def test_public_catalog_requires_authenticated_key_check_and_retains_metadata():
    def handler(request):
        if request.url.path.endswith('/models'):
            assert 'Authorization' not in request.headers
            return httpx.Response(200, json={'data': [{'id': 'openai/gpt-example', 'name': 'GPT Example', 'version': 'v1',
                'context_length': 8192, 'top_provider': {'max_completion_tokens': 2048}}]})
        assert request.url.path.endswith('/key') and request.headers['Authorization'] == 'Bearer ' + SECRET
        return httpx.Response(200, json={'data': {'label': 'selected key', 'usage': 0}})
    rows, requests = invoke(handler, base='https://compatible.example/v1')
    assert len(requests) == 2
    assert (rows[0].selected_llm, rows[0].display_name, rows[0].version, rows[0].max_output_tokens, rows[0].context_length) == ('openai/gpt-example', 'GPT Example', 'v1', 2048, 8192)


@pytest.mark.parametrize('payload', [None, [], {}, {'data': []}, {'data': {}}, {'data': None},
    {'data': {'error': 'Invalid API key'}}, {'data': {'label': 1}}, {'data': {'label': ''}},
    {'data': {'label': 'x'*513}}, {'data': {'label': '\n'}}])
def test_public_catalog_with_unverified_key_shape_fails(payload):
    def handler(request):
        return httpx.Response(200, json=catalog() if request.url.path.endswith('/models') else payload)
    with pytest.raises(ModelDiscoveryError, match='did not verify'):
        invoke(handler)


def test_public_catalog_rejected_for_subscription_before_token_transmission():
    calls = []
    def handler(request):
        calls.append(request)
        assert 'Authorization' not in request.headers
        return httpx.Response(200, json=catalog())
    with pytest.raises(ModelDiscoveryError, match='public catalog'):
        invoke(handler, subscription=True)
    assert len(calls) == 1


def test_public_catalog_wrong_key_fails_authentication():
    def handler(request):
        return httpx.Response(200, json=catalog()) if request.url.path.endswith('/models') else httpx.Response(401, json={'error': {'message': 'Invalid API key'}})
    with pytest.raises(ModelDiscoveryError, match='Invalid API key') as caught:
        invoke(handler)
    assert caught.value.kind == 'authentication'


@pytest.mark.parametrize('kwargs', [{'subscription': 1}, {'token': ''}, {'token': None}, {'token': '\n'}, {'token': '秘密'}, {'token': 'x'*16385}, {'subscription': True, 'base': 'https://other.example/v1'}])
def test_invalid_mode_token_and_subscription_origin_fail_before_request(kwargs):
    def forbidden(request):
        pytest.fail('must fail before request')
    with pytest.raises(ModelDiscoveryError):
        invoke(forbidden, **kwargs)


@pytest.mark.parametrize('exception,kind', [(httpx.ReadTimeout('secret'), 'deadline'), (httpx.ConnectError('secret'), 'unavailable')])
def test_transport_failure_is_safe(exception, kind):
    def handler(request):
        raise exception
    with pytest.raises(ModelDiscoveryError) as caught:
        invoke(handler)
    assert caught.value.kind == kind and 'secret' not in str(caught.value)


@pytest.mark.parametrize('content', [b'not-json', b'\xff', b'[' * 2000 + b']' * 2000, b' ' * 1048577], ids=['text', 'encoding', 'nested', 'oversized'])
def test_bad_or_oversized_catalog_does_not_send_key(content):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=content)
    with pytest.raises(ModelDiscoveryError):
        invoke(handler)
    assert len(calls) == 1 and 'Authorization' not in calls[0].headers


def test_deadline_and_cancellation_close_owned_request():
    async def scenario():
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.sleep(10)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelDiscoveryError) as caught:
                async with report_timeout('Outer', .01):
                    await discover_selections(client=client, base_url=BASE, access_token=SECRET, subscription=False)
            assert caught.value.kind == 'deadline'
            entered.clear()
            task = asyncio.create_task(discover_selections(client=client, base_url=BASE, access_token=SECRET, subscription=False))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['api_key', 'subscription'])
def test_factory_catalog_owns_client_and_fixed_subscription_origin(monkeypatch, mode):
    from api_agent.llm_providers.gpt import discovery
    client = AsyncMock()
    client.__aenter__.return_value = client
    calls = []
    def make_client(**kwargs):
        assert kwargs == {'follow_redirects': False, 'trust_env': False}
        calls.append(True)
        return client
    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    read = AsyncMock(return_value=())
    monkeypatch.setattr(discovery, 'discover_selections', read)
    async def scenario():
        provider = GPTProvider({'GPT_AUTH_MODE': mode, 'GPT_BASE_URL': 'https://other.example/v1'})
        assert not calls
        assert await provider.adescribe(auth=SECRET) == ()
        assert read.await_args.kwargs['base_url'] == (BASE if mode == 'subscription' else 'https://other.example/v1')
        assert read.await_args.kwargs['access_token'] == SECRET
        client.__aexit__.assert_awaited_once()
        await provider.aclose()
        with pytest.raises(ContractError, match='closed'):
            await provider.adescribe(auth=SECRET)
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('name', 1), ('name', 'x'*201), ('name', '\n'), ('name', SECRET),
    ('version', 1), ('version', 'x'*101), ('version', '\n'), ('version', SECRET),
    ('max_output_tokens', True), ('max_output_tokens', -1), ('context_length', 0), ('context_length', '8192')])
def test_bad_metadata_rejected_without_key_submission(field, value):
    payload = {'data': [{'id': 'gpt-example', field: value}]}
    with pytest.raises(ModelDiscoveryError, match='invalid metadata'):
        invoke(lambda request: httpx.Response(200, json=payload))


def test_unknown_limits_and_version_are_preserved_without_guessing():
    def handler(request):
        if request.url.path.endswith('/key'):
            return httpx.Response(200, json={'data': {'label': 'Selected'}})
        return httpx.Response(200, json={'data': [{'id': 'gpt-example-v1', 'top_provider': None}]})
    values, _ = invoke(handler)
    assert values[0].version is values[0].max_output_tokens is values[0].context_length is None


def test_catalog_selection_count_is_bounded():
    payload = {'data': [{'id': f'gpt-example-{i}'} for i in range(257)]}
    with pytest.raises(ModelDiscoveryError, match='too many selections'):
        invoke(lambda request: httpx.Response(200, json=payload))
