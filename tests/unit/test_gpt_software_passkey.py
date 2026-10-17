"""Managed software credentials never survive operation disposal."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.llm_providers.gpt.passkeys import SoftwarePasskey, require_private_transport
from api_agent.providers.control import CleanupPendingError


@pytest.fixture
def software(monkeypatch):
    for name in ('DEBUG', 'DEBUGP', 'PWDEBUG', 'DEBUG_FILE'):
        monkeypatch.delenv(name, raising=False)
    channel = SimpleNamespace(send=AsyncMock(return_value={'authenticatorId': 'synthetic', 'frameTree': {'frame': {'id': 'main'}}}),
                              detach=AsyncMock(), on=Mock(), remove_listener=Mock())
    session = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=channel)),
                              browser=SimpleNamespace(is_connected=lambda: True),
                              page=SimpleNamespace(route=AsyncMock(), unroute=AsyncMock()))
    key = SimpleNamespace(sign_count=0, credential_id='Y3JlZGVudGlhbA', user_handle='dXNlcg',
                          relying_party_id='example.test', username='person@example.test',
                          pkcs8_base64=lambda: 'synthetic-key')
    return SoftwarePasskey(session, key), channel


@pytest.mark.parametrize('name,value', [('DEBUG', 'pw:protocol'), ('DEBUG', '*'),
    ('DEBUG', 'pw:channel'), ('DEBUGP', ''), ('PWDEBUG', '1'), ('DEBUG_FILE', 'diagnostics')])
def test_protocol_logging_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(AuthError, match='passkey_protocol_logging'):
        require_private_transport()


@pytest.mark.parametrize('value', ['', 'True', 'False', '0', '1'])
def test_application_debug_is_supported(monkeypatch, value):
    monkeypatch.setenv('DEBUG', value)
    require_private_transport()


def test_import_and_removal_are_owned(software):
    owner, channel = software
    async def scenario():
        await owner.prepare()
        assert owner.credential is None
        imported = next(call.args[1]['credential'] for call in channel.send.call_args_list if call.args[0] == 'WebAuthn.addCredential')
        assert imported['signCount'] == -1
        assert imported['backupEligibility'] is False
        assert imported['privateKey'] == 'synthetic-key'
        await owner.aclose()
        await owner.aclose()
        assert owner.closed
        channel.detach.assert_awaited_once()
        assert channel.send.call_args_list[-3].args[0] == 'WebAuthn.removeVirtualAuthenticator'
        assert channel.send.call_args_list[-2].args[0] == 'WebAuthn.disable'
    asyncio.run(scenario())


def test_positive_counter_is_never_reset(software):
    owner, channel = software
    owner.credential.sign_count = 1
    with pytest.raises(AuthError, match='passkey_counter_unsupported'):
        asyncio.run(owner.prepare())
    channel.send.assert_not_called()


def test_import_failure_uses_static_diagnostic_and_still_removes(software):
    owner, channel = software
    channel.send.side_effect = [{'frameTree': {'frame': {'id': 'main'}}}, None, None, {'authenticatorId': 'synthetic'}, RuntimeError('private detail'), None, None, None]
    async def scenario():
        with pytest.raises(AuthError, match='passkey_browser_unsupported') as error:
            await owner.prepare()
        assert 'private detail' not in str(error.value)
        await owner.aclose()
        assert owner.closed
    asyncio.run(scenario())


def test_failed_removal_keeps_retry_receipt(software):
    owner, channel = software
    async def scenario():
        await owner.prepare()
        channel.send.side_effect = RuntimeError('private detail')
        with pytest.raises(CleanupPendingError) as error:
            await owner.aclose()
        assert error.value.owners == (owner,)
        assert not owner.closed
        channel.send.side_effect = None
        await error.value.retry()
        assert owner.closed
    asyncio.run(scenario())


def test_disconnected_browser_revokes_material(software):
    owner, channel = software
    owner.session.browser.is_connected = lambda: False
    asyncio.run(owner.aclose())
    assert owner.closed and owner.credential is None
    channel.send.assert_not_called()


def test_cancelled_import_finishes_before_removal(software):
    owner, channel = software
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(method, _=None):
            if method == 'WebAuthn.addCredential':
                entered.set()
                await release.wait()
            return {'authenticatorId': 'synthetic', 'frameTree': {'frame': {'id': 'main'}}}
        channel.send.side_effect = send
        task = asyncio.create_task(owner.prepare())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert owner.closed
        assert channel.send.call_args_list[-3].args[0] == 'WebAuthn.removeVirtualAuthenticator'
    asyncio.run(scenario())


@pytest.mark.parametrize('url,allowed', [
    ('https://auth.openai.com/log-in', True),
    ('https://sibling.openai.com/log-in', False),
    ('https://auth.openai.com.evil.test/log-in', False),
    ('http://127.0.0.1:1234/auth/callback?code=synthetic', True),
    ('http://127.0.0.1:1234/other', False),
    ('http://127.0.0.1:1234/auth/callback#fragment', False)])
def test_every_intercepted_navigation_is_origin_bound(software, url, allowed):
    owner, channel = software
    owner.callback_url = 'http://127.0.0.1:1234/auth/callback'
    owner.channel = channel
    asyncio.run(owner._navigation({'requestId': 'synthetic', 'request': {'url': url}}))
    method, params = channel.send.call_args.args
    assert method == ('Fetch.continueRequest' if allowed else 'Fetch.failRequest')
    assert params['requestId'] == 'synthetic'


def test_failed_navigation_control_is_recorded_without_diagnostic_payload(software):
    owner, channel = software
    owner.channel = channel
    channel.send.side_effect = RuntimeError('private diagnostic')
    asyncio.run(owner._navigation({'requestId': 'synthetic', 'request': {'url': 'https://auth.openai.com/log-in'}}))
    assert owner.navigation_failed


def test_close_before_preparation_and_cancelled_task_consumption(software):
    owner, channel = software
    async def scenario():
        cancelled = asyncio.create_task(asyncio.sleep(10))
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        owner._consume(cancelled)
        await owner.aclose()
        assert owner.closed
        channel.send.assert_not_called()
    asyncio.run(scenario())


def test_expired_cleanup_budget_retains_work_until_retry(software, monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.llm_providers.gpt import passkeys
    owner, channel = software
    async def scenario():
        await owner.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = channel.send.side_effect
        async def send(method, _=None):
            if method == 'WebAuthn.removeVirtualAuthenticator':
                entered.set()
                await release.wait()
        channel.send.side_effect = send
        @asynccontextmanager
        async def no_budget(*_):
            yield SimpleNamespace(remaining=0)
        with monkeypatch.context() as patch:
            patch.setattr(passkeys, 'report_timeout', no_budget)
            with pytest.raises(CleanupPendingError) as error:
                await owner.aclose()
        await entered.wait()
        assert not owner.closed
        release.set()
        await error.value.retry()
        assert owner.closed
        channel.send.side_effect = original_send
    asyncio.run(scenario())


def test_cancellation_during_cleanup_drains_and_preserves_cancellation(software):
    owner, channel = software
    async def scenario():
        await owner.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(method, _=None):
            if method == 'WebAuthn.removeVirtualAuthenticator':
                entered.set()
                await release.wait()
        channel.send.side_effect = send
        closing = asyncio.create_task(owner.aclose())
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert owner.closed
        await owner.aclose()
    asyncio.run(scenario())


def test_cancelled_cleanup_task_keeps_retry_ownership(software):
    owner, channel = software
    async def scenario():
        await owner.prepare()
        owner.closing = asyncio.create_task(asyncio.sleep(10))
        owner.closing.cancel()
        await asyncio.gather(owner.closing, return_exceptions=True)
        # A failed prior task is replaced, so normal retry can still revoke it.
        await owner.aclose()
        assert owner.closed
    asyncio.run(scenario())


def test_cancelled_failed_removal_exports_recoverable_receipt(software):
    owner, channel = software
    async def scenario():
        await owner.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(method, _=None):
            if method == 'WebAuthn.removeVirtualAuthenticator':
                entered.set()
                await release.wait()
                raise RuntimeError('synthetic failure')
        channel.send.side_effect = send
        closing = asyncio.create_task(owner.aclose())
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await closing
        assert error.value.retained_owners == (owner,)
        assert not owner.closed
        channel.send.side_effect = None
        await error.value.cleanup_error.retry()
        assert owner.closed
    asyncio.run(scenario())


def test_failed_authenticator_creation_still_disables_interception(software):
    owner, channel = software
    channel.send.side_effect = [{'frameTree': {'frame': {'id': 'main'}}}, None, None, RuntimeError('creation failed'), None, None]
    async def scenario():
        with pytest.raises(AuthError, match='passkey_browser_unsupported'):
            await owner.prepare()
        await owner.aclose()
        assert owner.closed and not owner.intercepting
        channel.detach.assert_awaited_once()
    asyncio.run(scenario())


def test_detach_retry_preserves_already_removed_authenticator(software):
    owner, channel = software
    channel.detach.side_effect = [RuntimeError('detach failed'), None]
    async def scenario():
        await owner.prepare()
        with pytest.raises(CleanupPendingError) as error:
            await owner.aclose()
        assert owner.authenticator is None and not owner.intercepting
        await error.value.retry()
        assert owner.closed
        removals = [call for call in channel.send.call_args_list if call.args[0] == 'WebAuthn.removeVirtualAuthenticator']
        assert len(removals) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('frame', ['child', 'nested-child'])
def test_foreign_supporting_frame_document_can_load(software, frame):
    owner, channel = software
    owner.channel, owner.main_frame = channel, 'main'
    asyncio.run(owner._navigation({'requestId': 'frame', 'frameId': frame,
                                  'request': {'url': 'https://support.example.test/frame'}}))
    channel.send.assert_awaited_once_with('Fetch.continueRequest', {'requestId': 'frame'})


def test_document_response_restricts_delegation_without_losing_headers(software):
    owner, channel = software
    owner.channel = channel
    headers = [{'name': 'Set-Cookie', 'value': 'one=1'}, {'name': 'Set-Cookie', 'value': 'two=2'}]
    asyncio.run(owner._navigation({'requestId': 'response', 'responseStatusCode': 200,
                                  'responseStatusText': 'OK', 'responseHeaders': headers}))
    method, data = channel.send.call_args.args
    assert method == 'Fetch.continueResponse'
    assert data['responseCode'] == 200 and data['responsePhrase'] == 'OK'
    assert data['responseHeaders'][:2] == headers
    assert 'publickey-credentials-get=(self)' in data['responseHeaders'][-1]['value']


def test_ambiguous_response_policy_is_never_resumed(software):
    owner, channel = software
    owner.channel = channel
    asyncio.run(owner._navigation({'requestId': 'response', 'responseStatusCode': 200,
        'responseStatusText': 'OK', 'responseHeaders': [
            {'name': 'Permissions-Policy', 'value': 'publickey-credentials-get=*'}]}))
    assert owner.navigation_failed
    channel.send.assert_awaited_once_with('Fetch.failRequest',
        {'requestId': 'response', 'errorReason': 'BlockedByClient'})


def test_uncertain_response_resume_is_aborted(software):
    owner, channel = software
    owner.channel = channel
    channel.send.side_effect = [RuntimeError('private diagnostic'), None]
    asyncio.run(owner._navigation({'requestId': 'response', 'responseStatusCode': 200,
                                  'responseStatusText': 'OK', 'responseHeaders': []}))
    assert owner.navigation_failed
    assert channel.send.call_args_list[-1].args[0] == 'Fetch.failRequest'


@pytest.mark.parametrize('status,phrase,expected', [(200, '', 'OK'), (200, None, 'OK'),
    (599, '', 'Response'), (200, 'Original phrase', 'Original phrase')])
def test_empty_http2_reason_phrase_gets_a_valid_cdp_reason(software, status, phrase, expected):
    owner, channel = software
    owner.channel = channel
    event = {'requestId': 'response', 'responseStatusCode': status, 'responseHeaders': []}
    if phrase is not None:
        event['responseStatusText'] = phrase
    asyncio.run(owner._navigation(event))
    method, params = channel.send.call_args.args
    assert method == 'Fetch.continueResponse'
    assert params['responseCode'] == status and params['responsePhrase'] == expected
    assert not owner.navigation_failed


@pytest.mark.parametrize('url', [
    'https://chatgpt.com/',
    'https://chatgpt.com/auth/callback',
    'http://chatgpt.com/',
    'https://chatgpt.com.evil.test/',
    'https://chatgpt.com:8443/',
])
def test_non_authorization_main_documents_are_blocked(software, url):
    owner, channel = software
    owner.channel, owner.main_frame = channel, 'main'
    asyncio.run(owner._navigation({'requestId': 'document', 'frameId': 'main', 'request': {'url': url}}))
    channel.send.assert_awaited_once_with('Fetch.failRequest',
        {'requestId': 'document', 'errorReason': 'BlockedByClient'})
