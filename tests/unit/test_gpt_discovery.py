import asyncio

import httpx
import pytest

from api_agent.llm_providers.gpt.discovery import discover_models, ModelDiscoveryError
from api_agent.providers.progress import report_timeout

BASE = 'https://api.openai.com/v1'
TOKEN = 'synthetic-private-token'


def run_discovery(response=None, *, subscription=False, base_url=BASE, handler=None, token=TOKEN):
    requests = []
    def transport(request):
        requests.append(request)
        return handler(request) if handler else response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            result = await discover_models(client=client, base_url=base_url, access_token=token, subscription=subscription)
            assert not client.is_closed
            return result
    return asyncio.run(run()), requests


@pytest.mark.parametrize('subscription, payload, expected', [
    (False, {'data': [{'id': 'gpt-4.1'}, {'id': 'o3'}, {'id': 'whisper-1'}, {'id': 'gpt-4.1-mini'}]}, [('gpt-4.1', ''), ('gpt-4.1-mini', '')]),
    (True, {'models': [{'slug': 'gpt-6.1-sol', 'display_name': 'GPT 6.1', 'visibility': 'list'},
        {'slug': 'hidden', 'display_name': 'Hidden', 'visibility': 'hidden'},
        {'slug': 'o3', 'display_name': 'Other family', 'visibility': 'list'},
        {'slug': 'gpt-6-sol', 'display_name': 'GPT 6', 'visibility': 'list'}]},
        [('gpt-6.1-sol', 'GPT 6.1'), ('gpt-6-sol', 'GPT 6')]),
    (False, {'data': []}, []),
])
def test_discovery_single_authorized_get_preserves_order_and_client(subscription, payload, expected):
    results, requests = run_discovery(httpx.Response(200, json=payload), subscription=subscription)
    assert [(row.selected_llm, row.description) for row in results] == expected
    assert all(row.features == (('reasoning_effort',) if row.reasoning_efforts else ()) for row in results)
    assert len(requests) == 1
    assert requests[0].method == 'GET' and str(requests[0].url) == BASE + '/models'
    assert requests[0].headers['Authorization'] == 'Bearer ' + TOKEN


@pytest.mark.parametrize('identifier,family,version', [
    ('gpt-6-luna', 'gpt-luna', '6'),
    ('gpt-6.10-luna', 'gpt-luna', '6.10'),
    ('openai/gpt-6.1-luna-2026-10-01', 'gpt-luna', '6.1+2026.10.01'),
    ('gpt-6.1-sol', 'gpt-sol', '6.1'),
    ('gpt-4.1', 'gpt', '4.1'),
    ('gpt-unknown', None, None),
])
def test_public_identifier_metadata_is_provider_owned(identifier, family, version):
    rows, _ = run_discovery(httpx.Response(200, json={'data': [{'id': identifier}]}))
    assert rows[0].family == family and rows[0].version == version


def test_upstream_version_takes_precedence_over_identifier_version():
    rows, _ = run_discovery(httpx.Response(200, json={'data': [{'id': 'gpt-6-luna', 'version': '6.2'}]}))
    assert rows[0].family == 'gpt-luna' and rows[0].version == '6.2'


@pytest.mark.parametrize('status,kind', [(401, 'authentication'), (403, 'authorization'), (429, 'rate_limited'), (400, 'invalid_request'), (500, 'unavailable'), (302, 'unavailable')])
def test_errors_preserve_safe_detail_and_exclude_secret_response_fields(status, kind):
    payload = {'error': {'message': 'Models permission denied: ' + TOKEN + ' https://private.example/key',
        'code': 'denied', 'type': 'permission_error', 'request': {'Authorization': TOKEN}, 'secret': TOKEN}}
    response = httpx.Response(status, json=payload, headers={'x-request-id': 'req-123', 'location': 'https://other.example'})
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(response)
    error = caught.value
    assert error.kind == kind and error.status == status
    assert error.code == 'denied' and error.type == 'permission_error' and error.request_id == 'req-123'
    assert 'Models permission denied' in str(error)
    assert TOKEN not in repr(error) + repr(vars(error)) and 'private.example' not in str(error)
    assert set(vars(error)) == {'kind', 'execution', 'retry_after', 'status', 'code', 'type', 'request_id', 'message', 'usage'}
    assert error.usage is None


@pytest.mark.parametrize('scheme', ['HTTPS', 'hTtPs', 'HTTP'])
def test_all_error_text_fields_remove_mixed_case_endpoint_urls(scheme):
    sensitive = scheme + '://private.example/account?secret=hidden'
    payload = {'error': {'message': 'Permission denied at ' + sensitive,
                         'code': sensitive, 'type': sensitive}}
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(httpx.Response(403, json=payload, headers={'x-request-id': sensitive}))
    error = caught.value
    assert error.kind == 'authorization' and error.status == 403
    assert str(error).splitlines()[0] == 'Permission denied at [endpoint]'
    assert 'HTTP status: 403' in str(error) and 'request ID: [endpoint]' in str(error)
    assert error.code == error.type == error.request_id == '[endpoint]'
    assert 'private.example' not in repr(error) + repr(vars(error))


@pytest.mark.parametrize('payload', [None, [], {}, {'data': {}}, {'data': [None]}, {'data': [{}]},
    {'data': [{'id': ''}]}, {'data': [{'id': ' spaced '}]}, {'data': [{'id': 1}]}, {'data': [{'id': 'x' * 201}]},
    {'data': [{'id': 'line\nbreak'}]}, {'data': [{'id': 'same'}, {'id': 'same'}]},
    {'data': [{'id': TOKEN}]}])
def test_rejects_malformed_catalogs_safely(payload):
    with pytest.raises(ModelDiscoveryError, match='invalid catalog'):
        run_discovery(httpx.Response(200, json=payload))


@pytest.mark.parametrize('row', [{'slug': 'gpt'}, {'slug': 'gpt', 'display_name': ''}, {'slug': 'gpt', 'display_name': 1}, {'slug': 'gpt', 'display_name': 'x' * 513},
    {'slug': 'gpt', 'display_name': '\n'}, {'slug': 'gpt', 'display_name': TOKEN}])
def test_subscription_catalog_rejects_invalid_description(row):
    with pytest.raises(ModelDiscoveryError, match='invalid catalog'):
        run_discovery(httpx.Response(200, json={'models': [dict(row, visibility='list')]}), subscription=True)


@pytest.mark.parametrize('content', [b'broken', b'\xff', b'[' * 2000 + b']' * 2000])
@pytest.mark.parametrize('status', [200, 401])
def test_malformed_json_has_generic_safe_failure(content, status):
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(httpx.Response(status, content=content))
    assert 'broken' not in str(caught.value)


@pytest.mark.parametrize('payload', [{'error': 'bad'}, {'error': {'message': 1, 'code': None, 'type': ''}}])
def test_malformed_upstream_error_fields_are_not_exported(payload):
    with pytest.raises(ModelDiscoveryError, match='GPT model discovery failed') as caught:
        run_discovery(httpx.Response(403, json=payload))
    assert caught.value.code is None and caught.value.type is None


@pytest.mark.parametrize('code,expected', [(401, '401'), (0, '0'), (-32603, '-32603'),
    (-2147483648, '-2147483648'), (2147483647, '2147483647'),
    (-2147483649, None), (2147483648, None), (True, None), (401.0, None), ([], None)])
def test_numeric_upstream_codes_remain_bounded_strings(code, expected):
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(httpx.Response(401, json={'error': {'message': 'API key expired.', 'code': code}}))
    assert caught.value.code == expected
    assert caught.value.status == 401 and caught.value.kind == 'authentication'
    if expected is not None:
        assert 'code: ' + expected in str(caught.value)


@pytest.mark.parametrize('challenge,expected', [
    (None, None), (1, None), ('Bearer ' + 'x'*4090, None), ('Bearer', None),
    ('Basic error="invalid_token"', None), ('Bearer realm="example"', None),
    ('Bearer error="invalid_token"', 'invalid_token'),
    ('bEaReR realm="example", ERROR="invalid_token", error_description="API key expired"', 'invalid_token'),
    ('Bearer error="first", error="second"', None), ('Bearer error="' + 'x'*101 + '"', None),
    ('Bearer error="https://private.example/token"', None),
])
def test_authentication_challenge_exports_only_one_safe_identifier(challenge, expected):
    from api_agent.llm_providers.gpt.discovery import _authentication_type
    assert _authentication_type(challenge) == expected


@pytest.mark.parametrize('body_type,expected', [(None, 'invalid_token'), ('key_expired', 'key_expired'),
    (TOKEN, '[redacted]')])
def test_challenge_type_fallback_and_body_type_precedence(body_type, expected):
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(httpx.Response(401,
            json={'error': {'message': 'API key expired.', 'code': 401, 'type': body_type,
                            'metadata': {'headers': {'secret': TOKEN}}}},
            headers={'www-authenticate': 'Bearer error="invalid_token", error_description="' + TOKEN + '"'}))
    assert caught.value.type == expected and caught.value.code == '401'
    assert TOKEN not in str(caught.value) + repr(vars(caught.value))


def test_redirect_is_not_followed_even_when_injected_client_follows():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://attacker.example/'})
    with pytest.raises(ModelDiscoveryError):
        run_discovery(handler=handler)
    assert len(calls) == 1


@pytest.mark.parametrize('token', ['', None, 'secret\n', '秘密', pytest.param('x' * 16385, id='oversized-token')])
def test_invalid_auth_is_rejected_before_request(token):
    with pytest.raises(ModelDiscoveryError, match='valid authentication'):
        run_discovery(token=token)


def test_subscription_rejects_alternate_endpoint_before_request():
    with pytest.raises(ModelDiscoveryError, match='official endpoint'):
        run_discovery(base_url='https://other.example/v1', subscription=True)


def test_body_limit_is_bounded():
    with pytest.raises(ModelDiscoveryError, match='exceeded its limit'):
        run_discovery(httpx.Response(200, content=b' ' * 1048577))


@pytest.mark.parametrize('exception,kind', [(httpx.ReadTimeout('private ' + TOKEN), 'deadline'),
    (httpx.ConnectError('private ' + TOKEN), 'unavailable')])
def test_transport_errors_are_sanitized(exception, kind):
    def handler(request):
        raise exception
    with pytest.raises(ModelDiscoveryError) as caught:
        run_discovery(handler=handler)
    assert caught.value.kind == kind and TOKEN not in repr(caught.value)
    assert caught.value.__context__ is None


def test_enclosing_deadline_and_cancellation_close_response_without_retry():
    async def run():
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.sleep(10)
            return httpx.Response(200, json={'data': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelDiscoveryError) as caught:
                async with report_timeout('Enclosing discovery', .01):
                    await discover_models(client=client, base_url=BASE, access_token=TOKEN, subscription=False)
            assert caught.value.kind == 'deadline'
            entered.clear()
            task = asyncio.create_task(discover_models(client=client, base_url=BASE, access_token=TOKEN, subscription=False))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not client.is_closed
    asyncio.run(run())


def test_luna_56_catalog_api_keeps_exact_capability_boundary():
    rows, requests = run_discovery(httpx.Response(200, json={'data': [
        {'id': 'gpt-5.6-luna'}, {'id': 'gpt-5.6-luna-2026-10-18'}]}))
    assert rows[0].selected_llm == 'gpt-5.6-luna'
    assert rows[0].reasoning_efforts == ('off', 'low', 'medium', 'high')
    assert rows[1].reasoning_efforts is None
    assert [(sent.method, str(sent.url)) for sent in requests] == [('GET', BASE + '/models')]


def test_authenticated_discovery_rejects_registered_non_access_credential():
    from api_agent.llm_providers.gpt.discovery import discover_selections
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.telemetry.privacy import EmissionBoundary

    boundary = EmissionBoundary()
    boundary.register_secret('synthetic-refresh-credential')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'data': [
                    {'id': 'gpt-4.1', 'description': 'synthetic-refresh-credential'}]}))) as client:
            with pytest.raises(ProviderAccessError):
                await discover_selections(client=client, base_url=BASE, access_token=TOKEN,
                                          subscription=False, authority=boundary)
    asyncio.run(run())
