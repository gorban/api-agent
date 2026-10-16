"""Controlled bridge requests never access external providers."""
import asyncio
import json

import pytest
from django.test import RequestFactory

from api_agent.api import Runtime, create_application
from api_agent.api.studio import StudioBackend
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import LLMSessionOwner

ORIGIN = 'http://127.0.0.1:8080'
NONCE = 'a' * 32


def setup_bridge(**changes):
    create_application(Runtime('demo'))
    calls = []
    async def describe(*, auth):
        calls.append('catalog')
        return (AvailableSelection('example'),)
    async def complete(request, *, auth, context):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent('hello'),)))
    values = dict(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
                  policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'example'}), origin=ORIGIN,
                  nonce=NONCE, scrub=lambda text: text)
    values.update(changes)
    return StudioBackend(**values), calls


def request(method='GET', body=None, **headers):
    headers = {'Origin': ORIGIN, 'X-API-Agent-Session': NONCE} | headers
    return RequestFactory().generic(method, '/app/agent/models',
        data=b'' if body is None else body, content_type='application/json', headers=headers,
        REMOTE_ADDR='127.0.0.1')


def payload(**changes):
    return json.dumps(dict(selected_llm='example', messages=[{'role': 'user', 'text': 'hi'}],
                          max_output_tokens=None, cache_policy=None, request_id='one') | changes).encode()


def test_catalog_and_completion_share_owner():
    async def run():
        backend, calls = setup_bridge()
        catalog = await backend.handle(request(), 'models')
        answer = await backend.handle(request('POST', payload()), 'chat')
        stream = await backend.handle(request('POST', payload()), 'chat_stream')
        assert json.loads(catalog.content)['default'] == 'example'
        assert json.loads(answer.content)['text'] == 'hello'
        assert [json.loads(frame[6:])['kind'] for frame in stream.content.decode().strip().split('\n\n')] == ['completed']
        assert [item for item in calls if isinstance(item, str)] == ['catalog'] * 3
        await backend.aclose()
        assert (await backend.handle(request(), 'models')).status_code == 503
    asyncio.run(run())


@pytest.mark.parametrize('headers', [{'Origin': 'http://evil.invalid'}, {'X-API-Agent-Session': 'wrong'}, {'X-API-Agent-Session': 'é'}])
def test_admission_precedes_provider_access(headers):
    async def run():
        backend, calls = setup_bridge()
        response = await backend.handle(request(**headers), 'models')
        assert response.status_code == 403
        assert not calls
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('body', [b'{', b'{"a":1,"a":2}', b'{"a":NaN}', payload(selected_llm='missing'), payload(request_id=''), payload(reasoning_effort='high')])
def test_invalid_requests_do_not_complete(body):
    async def run():
        backend, calls = setup_bridge()
        response = await backend.handle(request('POST', body), 'chat')
        assert response.status_code in (400, 502)
        assert calls == (['catalog'] if body in (payload(selected_llm='missing'), payload(reasoning_effort='high')) else [])
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('changes', [dict(origin='https://example.invalid'), dict(nonce='short'), dict(timeout=0), dict(body_limit=0)])
def test_invalid_bridge_configuration(changes):
    with pytest.raises(ValueError, match='configuration'):
        setup_bridge(**changes)


def test_preflight_is_exact_and_has_no_provider_access():
    async def run():
        backend, calls = setup_bridge()
        accepted = await backend.handle(request('OPTIONS', **{'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Content-Type, X-API-Agent-Session'}), 'chat')
        rejected = await backend.handle(request('OPTIONS'), 'chat')
        assert accepted.status_code == 204
        assert accepted['Access-Control-Allow-Origin'] == ORIGIN
        assert 'Access-Control-Allow-Credentials' not in accepted
        assert rejected.status_code == 403
        assert not calls
        await backend.aclose()
    asyncio.run(run())


def test_remote_peer_and_wrong_method_rejected():
    async def run():
        backend, calls = setup_bridge()
        remote = request()
        remote.META['REMOTE_ADDR'] = 'bad'
        assert (await backend.handle(remote, 'models')).status_code == 403
        assert (await backend.handle(request('DELETE'), 'models')).status_code == 405
        assert not calls
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('error,code,status', [
    (RuntimeError('secret'), 'unknown', 502),
    (ProviderAccessError('authorization', 'secret denied', status=403), 'authorization', 502),
])
def test_safe_provider_errors(error, code, status):
    async def run():
        async def complete(*args, **kwargs):
            raise error
        backend, calls = setup_bridge(complete=complete, scrub=lambda text: text.replace('secret', '[redacted]'))
        response = await backend.handle(request('POST', payload()), 'chat')
        assert response.status_code == status
        assert json.loads(response.content)['code'] == code
        assert b'secret' not in response.content
        await backend.aclose()
    asyncio.run(run())


def test_disconnect_cancellation_drains_inference():
    async def run():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        backend, calls = setup_bridge(complete=complete)
        task = asyncio.create_task(backend.handle(request('POST', payload()), 'chat'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('body,content_type', [(b'{}', 'text/plain'), (b'x' * 131073, 'application/json'), (b'{"version":1}', 'application/json'), (b'[]', 'application/json')])
def test_body_boundary(body, content_type):
    async def run():
        backend, calls = setup_bridge()
        req = request('POST', body)
        req.content_type = content_type
        assert (await backend.handle(req, 'chat')).status_code == 400
        assert calls == []
        await backend.aclose()
    asyncio.run(run())


def test_deadline_and_active_disposal():
    async def run():
        entered = asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        backend, calls = setup_bridge(complete=complete, timeout=.01)
        response = await backend.handle(request('POST', payload()), 'chat')
        assert response.status_code == 504
        assert json.loads(response.content)['code'] == 'deadline'
        await backend.aclose()
        backend, calls = setup_bridge(complete=complete)
        task = asyncio.create_task(backend.handle(request('POST', payload()), 'chat'))
        await asyncio.sleep(.01)
        await backend.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_invalid_scrub_output_is_bounded():
    async def run():
        async def describe(*, auth):
            raise ProviderAccessError('unavailable', 'safe', status=503)
        backend, calls = setup_bridge(describe=describe, scrub=lambda text: '\nsecret')
        response = await backend.handle(request(), 'models')
        assert json.loads(response.content) == {'code': 'unavailable', 'message': 'Provider rejected the request.', 'status': 503}
        await backend.aclose()
    asyncio.run(run())


def test_safe_error_without_status_and_unclassified_boundary_error():
    class BrokenPolicy:
        def filter(self, selections):
            raise RuntimeError('secret')
    async def run():
        backend, calls = setup_bridge(policy=BrokenPolicy())
        response = await backend.handle(request(), 'models')
        assert json.loads(response.content) == {'code': 'unknown'}
        response = backend._error(ProviderAccessError('invalid_request', 'safe'))
        assert json.loads(response.content) == {'code': 'invalid_request', 'message': 'safe'}
        assert response.status_code == 400
        await backend.aclose()
    asyncio.run(run())


def test_real_asgi_routing_to_backend():
    async def run():
        backend, calls = setup_bridge()
        app = create_application(Runtime('demo', services={'studio_backend': backend}))
        sent = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': payload(), 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'POST', 'path': '/app/agent/chat', 'raw_path': b'/app/agent/chat',
            'query_string': b'', 'root_path': '', 'server': ('testserver', 80), 'client': ('127.0.0.1', 1),
            'headers': [(b'host', b'testserver'), (b'origin', ORIGIN.encode()),
                        (b'x-api-agent-session', NONCE.encode()), (b'content-type', b'application/json')]}, receive, send)
        assert sent[0]['status'] == 200
        assert json.loads(b''.join(item.get('body', b'') for item in sent[1:]))['text'] == 'hello'
        assert len(calls) == 2
        await backend.aclose()
    asyncio.run(run())


def test_duplicate_valid_field_is_rejected_before_discovery():
    async def run():
        backend, calls = setup_bridge()
        duplicate = payload()[:-1] + b', "selected_llm":"example"}'
        response = await backend.handle(request('POST', duplicate), 'chat')
        assert response.status_code == 400
        assert not calls
        await backend.aclose()
    asyncio.run(run())


def test_valid_json_over_bridge_limit_is_rejected():
    async def run():
        backend, calls = setup_bridge(body_limit=8)
        response = await backend.handle(request('POST', payload()), 'chat')
        assert response.status_code == 400
        assert not calls
        await backend.aclose()
    asyncio.run(run())


def test_authentication_owner_is_reused_across_requests():
    class Auth:
        closed = False
        async def __call__(self):
            return 'opaque'
        async def aclose(self):
            self.closed = True
    class Provider:
        setups = 0
        auth = Auth()
        async def asetup(self, **kwargs):
            self.setups += 1
            return self.auth
    async def run():
        provider = Provider()
        backend, calls = setup_bridge(owner=LLMSessionOwner(provider))
        assert (await backend.handle(request(), 'models')).status_code == 200
        assert (await backend.handle(request('POST', payload()), 'chat')).status_code == 200
        assert provider.setups == 1
        await backend.aclose()
        assert provider.auth.closed
    asyncio.run(run())


def test_scrubber_failure_cannot_expose_exception_text():
    def scrub(text):
        raise RuntimeError('secret')
    async def run():
        backend, calls = setup_bridge(scrub=scrub)
        response = backend._error(ProviderAccessError('unavailable', 'secret'))
        assert json.loads(response.content)['message'] == 'Provider rejected the request.'
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('cancel_request', [False, True])
def test_pending_nested_cleanup_retains_auth_and_supports_disposal_retry(cancel_request):
    from api_agent.providers.control import CleanupPendingError
    class Auth:
        closed = False
        async def __call__(self):
            return self
        async def aclose(self):
            self.closed = True
    class Provider:
        auth = Auth()
        async def asetup(self, **kwargs):
            return self.auth
    async def run():
        release = asyncio.Event()
        cancelled = asyncio.Event()
        entered = asyncio.Event()
        contexts = []
        async def child():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        async def complete(value, *, auth, context):
            contexts.append(context)
            return await context.run(child())
        provider = Provider()
        backend, calls = setup_bridge(owner=LLMSessionOwner(provider), complete=complete, timeout=120 if cancel_request else .01)
        try:
            task = asyncio.create_task(backend.handle(request('POST', payload()), 'chat'))
            await entered.wait()
            if cancel_request:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await task
                assert response.status_code == 502
            assert cancelled.is_set()
            assert contexts[0].retained_tasks
            assert tuple(backend._contexts.values()) == (contexts[0],)
            assert backend.closed
            assert (await backend.handle(request(), 'models')).status_code == 503
            with pytest.raises(CleanupPendingError) as pending:
                await backend.aclose()
            assert pending.value.owners == (backend,)
            assert not provider.auth.closed
            release.set()
            await pending.value.retry()
            assert not backend._contexts
            assert not contexts[0].retained_tasks
            assert provider.auth.closed
        finally:
            release.set()
            await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('capability,method,headers,allowed', [
    ('models', 'GET', 'X-API-Agent-Session', 'X-API-Agent-Session'),
    ('chat', 'POST', 'Content-Type, X-API-Agent-Session', 'Content-Type, X-API-Agent-Session'),
    ('chat_stream', 'POST', 'Content-Type, X-API-Agent-Session', 'Content-Type, X-API-Agent-Session'),
])
def test_browser_route_specific_preflight(capability, method, headers, allowed):
    async def run():
        backend, calls = setup_bridge()
        response = await backend.handle(request('OPTIONS', **{'Access-Control-Request-Method': method,
            'Access-Control-Request-Headers': headers}), capability)
        assert response.status_code == 204
        assert response['Access-Control-Allow-Headers'] == allowed
        assert not calls
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('capability,method,headers', [
    ('models', 'GET', 'Content-Type, X-API-Agent-Session'),
    ('chat', 'POST', 'X-API-Agent-Session'),
])
def test_preflight_rejects_other_route_header_set(capability, method, headers):
    async def run():
        backend, calls = setup_bridge()
        response = await backend.handle(request('OPTIONS', **{'Access-Control-Request-Method': method,
            'Access-Control-Request-Headers': headers}), capability)
        assert response.status_code == 403
        assert not calls
        await backend.aclose()
    asyncio.run(run())


def test_buffered_sse_does_not_duplicate_large_unicode_result():
    async def run():
        text = '😀' * 10000
        async def complete(*args, **kwargs):
            return LLMResult(Message('assistant', (TextContent(text),)))
        backend, calls = setup_bridge(complete=complete)
        response = await backend.handle(request('POST', payload()), 'chat_stream')
        assert response.status_code == 200
        assert len(response.content) <= 131072
        frames = response.content.decode().strip().split('\n\n')
        assert len(frames) == 1
        frame = json.loads(frames[0][6:])
        assert frame['kind'] == 'completed'
        assert frame['payload']['text'] == text
        await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('text', ['\n' * 65450, '😀' * 10910])
def test_buffered_sse_rejects_framing_that_exceeds_consumer_bound(text):
    async def run():
        async def complete(*args, **kwargs):
            return LLMResult(Message('assistant', (TextContent(text),)))
        backend, calls = setup_bridge(complete=complete)
        response = await backend.handle(request('POST', payload()), 'chat_stream')
        assert response.status_code == 400
        assert json.loads(response.content) == {'code': 'invalid_request'}
        assert len(response.content) <= 131072
        await backend.aclose()
    asyncio.run(run())
