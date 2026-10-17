"""Passkey removal gates the real browser owner's DEBUG handoff."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.llm_providers.gpt import connect as c
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.sessions import BrowserOwner, InspectionOwner
from test_gpt_connect import lane, Store, request


@pytest.fixture
def passkey_owner(monkeypatch):
    monkeypatch.setattr(c, 'authenticate_first_party', AsyncMock())
    for name in ('DEBUG', 'DEBUGP', 'PWDEBUG', 'DEBUG_FILE'):
        monkeypatch.delenv(name, raising=False)
    async def create():
        connected = True
        async def close():
            nonlocal connected
            connected = False
        async def goto(url, **_):
            assert b'200 OK' in await request(url)
        page = SimpleNamespace(goto=goto, is_closed=lambda: False)
        channel = SimpleNamespace(send=AsyncMock(return_value={'authenticatorId': 'synthetic', 'frameTree': {'frame': {'id': 'main'}}}),
            on=Mock(), remove_listener=Mock(), detach=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(),
            new_cdp_session=AsyncMock(return_value=channel))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context),
            close=AsyncMock(side_effect=close), is_connected=lambda: connected)
        driver = SimpleNamespace(launch=AsyncMock(return_value=browser), stop=AsyncMock())
        inspection = InspectionOwner()
        owner = BrowserOwner(AsyncMock(return_value=driver), headless=True, inspection=inspection)
        key = SimpleNamespace(sign_count=0, credential_id='Y3JlZGVudGlhbA', user_handle='dXNlcg',
            relying_party_id='openai.com', username='person@example.test', pkcs8_base64=lambda: 'synthetic')
        material = SimpleNamespace(passkey_available=True, passkey=key, password=None)
        return owner, inspection, channel, browser, material
    return create


def test_success_removes_key_before_debug_retention(lane, passkey_owner):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        await c.connect_account(Store(), browser_owner=owner, client=object(),
            login_username='person@example.test', login_credentials=AsyncMock(),
            login_material=AsyncMock(return_value=material))
        assert channel.send.call_args_list[-3].args[0] == 'WebAuthn.removeVirtualAuthenticator'
        channel.detach.assert_awaited_once()
        assert len(inspection.sessions) == 1 and len(inspection._resources) == 3
        browser.close.assert_not_awaited()
        await inspection.aclose()
        browser.close.assert_awaited_once()
    asyncio.run(scenario())


def test_failed_key_removal_closes_original_browser_instead_of_retaining(lane, passkey_owner):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        async def send(method, _=None):
            if method == 'WebAuthn.removeVirtualAuthenticator':
                raise RuntimeError('synthetic disposal failure')
            return {'authenticatorId': 'synthetic', 'frameTree': {'frame': {'id': 'main'}}}
        channel.send.side_effect = send
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        with pytest.raises(CleanupPendingError) as error:
            await c.connect_account(Store(), browser_owner=owner, client=object(),
                login_username='person@example.test', login_credentials=AsyncMock(),
                login_material=AsyncMock(return_value=material))
        browser.close.assert_awaited_once()
        assert inspection._resources == []
        await error.value.retry()
        assert error.value.owners[0].closed
        await inspection.aclose()
    asyncio.run(scenario())


def test_protocol_logging_rejects_before_material_retrieval(lane, passkey_owner, monkeypatch):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        reader = AsyncMock(return_value=material)
        monkeypatch.setenv('DEBUG', 'pw:protocol')
        with pytest.raises(c.AuthError, match='passkey_protocol_logging'):
            await c.connect_account(Store(), browser_owner=owner, client=object(),
                login_username='person@example.test', login_credentials=AsyncMock(), login_material=reader)
        reader.assert_not_awaited()
        channel.send.assert_not_awaited()
        await inspection.aclose()
        browser.close.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('material', [None, SimpleNamespace(passkey_available=False, password='confirmed-password')])
def test_material_without_passkey_never_imports_one(lane, passkey_owner, material):
    async def scenario():
        owner, inspection, channel, _, _ = await passkey_owner()
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        await c.connect_account(Store(), browser_owner=owner, client=object(),
            login_username='person@example.test', login_credentials=AsyncMock(),
            login_material=AsyncMock(return_value=material))
        channel.send.assert_not_awaited()
        await inspection.aclose()
    asyncio.run(scenario())


def test_primary_error_keeps_failed_passkey_cleanup_receipt(lane, passkey_owner):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        async def send(method, _=None):
            if method == 'WebAuthn.removeVirtualAuthenticator':
                raise RuntimeError('synthetic removal failure')
            return {'authenticatorId': 'synthetic', 'frameTree': {'frame': {'id': 'main'}}}
        channel.send.side_effect = send
        lane.finish_authorization = AsyncMock(side_effect=c.AuthError('account_mismatch'))
        with pytest.raises(c.AuthError, match='account_mismatch') as error:
            await c.connect_account(Store(), browser_owner=owner, client=object(),
                login_username='person@example.test', login_credentials=AsyncMock(),
                login_material=AsyncMock(return_value=material))
        assert isinstance(error.value.cleanup_error, CleanupPendingError)
        browser.close.assert_awaited_once()
        await error.value.cleanup_error.retry()
        assert error.value.retained_owners[0].closed
        await inspection.aclose()
    asyncio.run(scenario())


def test_factory_preserves_scoped_material_reader(monkeypatch, tmp_path):
    from api_agent.llm_providers.gpt import factory
    from api_agent.providers.sessions import ScopedLLMCredentials, ScopedRoleCredentials
    async def scenario():
        selected = AsyncMock(return_value=None)
        credentials = ScopedLLMCredentials('llm', ScopedRoleCredentials(('llm',), 4, AsyncMock(), retrieve_login=selected))
        browser = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(factory, 'subscription_browser', lambda **_: browser)
        provider = factory.factory({'GPT_STATE_DIR': tmp_path}, None)
        async def connect(store, **options):
            assert await options['login_material']() is None
            return 'selected-profile'
        provider._connect_account = connect
        profile = await provider._login(object(), interactive=False, username='person@example.test',
            user_index=4, credentials=credentials)
        assert profile == 'selected-profile'
        selected.assert_awaited_once_with('llm', 4, expected_username='person@example.test', relying_party_id='openai.com')
        await provider.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,expected', [
    (c.AuthError('login_interaction_required'), 'login_interaction_required'),
    (RuntimeError('synthetic browser detail'), 'browser_unavailable'),
    (c.SessionClosedError('synthetic closure'), 'browser_closed'),
])
def test_firstparty_failure_cleans_workflow_and_key_before_oauth(lane, passkey_owner, monkeypatch, failure, expected):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        observed = []
        async def fail(workflow, session, **_):
            observed.append(workflow)
            raise failure
        monkeypatch.setattr(c, 'authenticate_first_party', fail)
        store = Store()
        lane.finish_authorization = AsyncMock()
        with pytest.raises(c.AuthError, match='^' + expected + '$'):
            await c.connect_account(store, browser_owner=owner, client=object(),
                login_username='person@example.test', login_credentials=AsyncMock(),
                login_material=AsyncMock(return_value=material))
        assert observed[0].ready.cancelled() and not observed[0].documents
        assert not store.saved
        lane.finish_authorization.assert_not_awaited()
        channel.detach.assert_awaited_once()
        await inspection.aclose()
        browser.close.assert_awaited_once()
    asyncio.run(scenario())


def test_firstparty_cancellation_retains_normal_browser_cleanup(lane, passkey_owner, monkeypatch):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        async def cancel(workflow, session, **_):
            raise asyncio.CancelledError()
        monkeypatch.setattr(c, 'authenticate_first_party', cancel)
        store = Store()
        with pytest.raises(asyncio.CancelledError):
            await c.connect_account(store, browser_owner=owner, client=object(),
                login_username='person@example.test', login_credentials=AsyncMock(),
                login_material=AsyncMock(return_value=material))
        assert not store.saved
        channel.detach.assert_awaited_once()
        await inspection.aclose()
        browser.close.assert_awaited_once()
    asyncio.run(scenario())


def test_accepted_callback_during_firstparty_skips_reopening_authorization(lane, passkey_owner, monkeypatch):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        session = await owner.start()
        session.page.goto = AsyncMock()
        async def accepted(workflow, browser_session, *, callback):
            callback.set_result({'state': 's', 'code': 'c'})
        monkeypatch.setattr(c, 'authenticate_first_party', accepted)
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        store = Store()
        result = await c.connect_account(store, browser_owner=owner, client=object(),
            login_username='person@example.test', login_credentials=AsyncMock(),
            login_material=AsyncMock(return_value=material))
        assert result == 'account' and len(store.saved) == 1
        session.page.goto.assert_not_awaited()
        assert lane.finish_authorization.await_args.args[1] == {'state': 's', 'code': 'c'}
        channel.detach.assert_awaited_once()
        await inspection.aclose()
        browser.close.assert_awaited_once()
    asyncio.run(scenario())
