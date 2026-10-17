"""Protocol-only OAuth diagnostics never serialize provider response contents."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from api_agent.llm_providers.gpt import oauth
from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.llm_providers.gpt.factory import GPTProvider
from api_agent.providers.progress import progress_sink


@pytest.mark.parametrize('url,stage,label', [
    (oauth.TOKEN, 'token', 'token exchange'),
    (oauth.METADATA, 'metadata', 'identity metadata request'),
    (oauth.ISSUER + '/keys', 'keys', 'identity signing-key request'),
])
@pytest.mark.parametrize('error,expected', [
    ('invalid_grant', 'invalid_grant'), ({'code': 'invalid_client'}, 'invalid_client'),
])
def test_http_rejection_preserves_only_known_protocol_metadata(url, stage, label, error, expected):
    async def scenario():
        def reject(request):
            assert request.headers['accept-encoding'] == 'identity'
            return httpx.Response(400, json={'error': error, 'error_description': 'secondary-secret',
                                           'access_token': 'returned-token'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            with pytest.raises(AuthError, match='^authorization_failed$') as caught:
                await oauth._request(client, 'GET', url)
        failure = caught.value
        assert failure.stage == stage and failure.http_status == 400
        assert failure.oauth_error == expected
        rendered = str(GPTProvider._login_error(failure))
        assert rendered == f'GPT {label} failed (HTTP 400; {expected}). Retry sign-in through launch.py.'
        assert 'secondary-secret' not in repr(failure.__dict__) + rendered
        assert 'returned-token' not in repr(failure.__dict__) + rendered
    asyncio.run(scenario())


@pytest.mark.parametrize('data', [
    {'error': 'secondary-secret'}, {'error': {'code': 'secondary-secret'}},
    {'error': []}, {'error': {'message': 'secondary-secret'}}, {},
])
def test_unrecognized_error_fields_are_not_diagnostics(data):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json=data))) as client:
            with pytest.raises(AuthError) as caught:
                await oauth._request(client, 'POST', oauth.TOKEN)
        assert caught.value.oauth_error is None
        assert str(GPTProvider._login_error(caught.value)) == (
            'GPT token exchange failed (HTTP 403). Retry sign-in through launch.py.')
        assert 'secondary-secret' not in repr(caught.value.__dict__)
    asyncio.run(scenario())


@pytest.mark.parametrize('raw', [b'not json', b'[]', b'x' * (oauth.LIMIT + 1), b'[' * 2000])
def test_malformed_or_oversized_http_failure_keeps_status_without_content(raw):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(502, content=raw))) as client:
            with pytest.raises(AuthError) as caught:
                await oauth._request(client, 'POST', oauth.TOKEN)
        assert caught.value.http_status == 502 and caught.value.oauth_error is None
        assert str(GPTProvider._login_error(caught.value)) == (
            'GPT token exchange failed (HTTP 502). Retry sign-in through launch.py.')
    asyncio.run(scenario())


def test_transport_failure_has_no_invented_http_status_or_exception_text():
    async def scenario():
        def unavailable(request):
            raise httpx.ConnectError('secondary-secret', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            with pytest.raises(AuthError) as caught:
                await oauth._request(client, 'GET', oauth.METADATA)
        assert caught.value.http_status is None
        assert str(GPTProvider._login_error(caught.value)) == (
            'GPT identity metadata request failed. Retry sign-in through launch.py.')
    asyncio.run(scenario())


@pytest.mark.parametrize('stage,status,reason,details', [
    ('token', 400, 'invalid_grant', 'token exchange failed (HTTP 400; invalid_grant)'),
    ('unrecognized-secret', 401, [], 'authorization request failed (HTTP 401)'),
    ([], None, 'invalid_client', 'authorization request failed (invalid_client)'),
    (None, False, None, 'authorization request failed'),
    ('token', 99, 'secondary-secret', 'token exchange failed'),
    ('token', 600, None, 'token exchange failed'),
])
def test_failure_formatter_does_not_trust_attached_metadata(stage, status, reason, details):
    failure = AuthError('authorization_failed')
    failure.stage, failure.http_status, failure.oauth_error = stage, status, reason
    assert str(GPTProvider._login_error(failure)) == f'GPT {details}. Retry sign-in through launch.py.'


@pytest.mark.parametrize('status', [200, 400])
def test_compressed_authentication_responses_are_rejected_before_decoding(status):
    class UnreadStream(httpx.AsyncByteStream):
        consumed = False
        async def __aiter__(self):
            self.consumed = True
            yield b'unsupported compressed bytes'
    async def scenario():
        stream = UnreadStream()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(status, headers={'content-encoding': 'gzip'}, stream=stream))) as client:
            with pytest.raises(AuthError) as caught:
                await oauth._request(client, 'POST', oauth.TOKEN)
        assert not stream.consumed
        assert caught.value.http_status == status
    asyncio.run(scenario())


@pytest.mark.parametrize('code,phrase', [
    ('authorization_denied', 'declined'), ('incomplete_registration', 'issued app registration'),
    ('invalid_state', 'did not match this sign-in attempt'), ('client_mismatch', 'different app registration'),
    ('invalid_issuer', 'expected OpenAI issuer'), ('invalid_identity', 'signed account identity'),
    ('invalid_credentials', 'invalid token response'), ('identity_mismatch', 'saved registration'),
    ('plan_permission_required', 'plan or refresh permissions'),
])
def test_known_post_callback_failures_are_actionable_without_raw_error_details(code, phrase):
    failure = AuthError(code)
    failure.response = 'secondary-secret'
    rendered = str(GPTProvider._login_error(failure))
    assert phrase in rendered
    assert 'secondary-secret' not in rendered
    assert 'GPT sign-in failed;' not in rendered


def test_real_streamed_http_rejection_retains_only_protocol_classification():
    async def scenario():
        requests = []
        tasks = set()
        async def reject(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                body = b'{"error":"invalid_grant","error_description":"secondary-secret"}'
                writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)
        async with asyncio.timeout(5):
            server = await asyncio.start_server(reject, '127.0.0.1', 0)
            try:
                port = server.sockets[0].getsockname()[1]
                async with httpx.AsyncClient(trust_env=False) as client:
                    with pytest.raises(AuthError) as caught:
                        await oauth._request(client, 'POST', f'http://127.0.0.1:{port}/token')
                assert caught.value.http_status == 400 and caught.value.oauth_error == 'invalid_grant'
                assert 'secondary-secret' not in str(GPTProvider._login_error(caught.value))
                assert b'accept-encoding: identity' in requests[0].lower()
            finally:
                server.close()
                await server.wait_closed()
                await asyncio.gather(*tasks)
    asyncio.run(scenario())



def test_native_deadline_reports_the_oauth_component_and_drains_request(monkeypatch):
    original = oauth.component_timeout
    timers = []
    @asynccontextmanager
    async def controlled(name, timeout):
        async with original(name, timeout) as budget, asyncio.timeout(None) as timer:
            timers.append(timer)
            yield budget
    monkeypatch.setattr(oauth, 'component_timeout', controlled)
    async def scenario():
        rows = []
        async def blocked(request):
            timers[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()
        with progress_sink(rows.append):
            async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
                with pytest.raises(AuthError) as caught:
                    await oauth._request(client, 'POST', oauth.TOKEN)
        assert caught.value.stage == 'token' and caught.value.http_status is None
        assert len(rows) == 2 and rows[0].id == rows[1].id
        assert rows[0].name == 'GPT OAuth token exchange' and rows[0].timeout == 15
        assert rows[0].active and not rows[1].active
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(scenario())
