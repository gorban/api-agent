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
    assert all(row.features == () for row in results)
    assert len(requests) == 1
    assert requests[0].method == 'GET' and str(requests[0].url) == BASE + '/models'
    assert requests[0].headers['Authorization'] == 'Bearer ' + TOKEN


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
    assert set(vars(error)) == {'kind', 'execution', 'retry_after', 'status', 'code', 'type', 'request_id', 'message'}


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


def test_redirect_is_not_followed_even_when_injected_client_follows():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://attacker.example/'})
    with pytest.raises(ModelDiscoveryError):
        run_discovery(handler=handler)
    assert len(calls) == 1


@pytest.mark.parametrize('token', ['', None, 'secret\n', '秘密', 'x' * 16385])
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
