"""Rejected refresh renews the bound registration without inference or replay."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from api_agent.llm_providers.gpt import factory, oauth
from api_agent.llm_providers.gpt.connections import ConnectionStore
from api_agent.llm_providers.gpt.contracts import AuthError, Connection
from api_agent.providers.contracts import InteractionRequiredError


@pytest.fixture
def cached(tmp_path):
    store = ConnectionStore(tmp_path / 'state')
    connection = Connection('subject', 'client', store.host_id(), 'person@example.test',
                            'expired', 'refresh', 'identity', ('scope',), 0)
    profile = store.save(connection)
    return store, connection, profile


@pytest.fixture
def browser(monkeypatch):
    owner = SimpleNamespace(aclose=AsyncMock())
    create = Mock(return_value=owner)
    monkeypatch.setattr(factory, 'subscription_browser', create)
    return create


@pytest.mark.parametrize('status,refresh,code', [
    (400, True, 'refresh_rejected'), (401, True, 'refresh_rejected'),
    (403, True, 'authorization_failed'), (429, True, 'authorization_failed'),
    (503, True, 'authorization_failed'), (400, False, 'authorization_failed'),
])
def test_only_refresh_credential_rejection_requests_reconnection(status, refresh, code):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, text='private upstream body'))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(AuthError, match='^' + code + '$'):
                await oauth._request(client, 'POST', oauth.TOKEN, refresh=refresh)
    asyncio.run(scenario())


@pytest.mark.parametrize('unattended', [False, True])
def test_concurrent_waiters_renew_one_bound_profile(cached, browser, monkeypatch, unattended):
    store, connection, profile = cached
    reader = AsyncMock(return_value=connection.email)
    refresh = AsyncMock(side_effect=AuthError('refresh_rejected'))
    monkeypatch.setattr(factory, 'refresh_connection', refresh)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def reconnect(received, **options):
            assert options['profile'] == profile
            entered.set()
            await release.wait()
            return await received.save_serialized(replace(connection, access_token='renewed', expires_at=10**12))
        connect = AsyncMock(side_effect=reconnect)
        monkeypatch.setattr(factory, 'connect_account', connect)
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        session = await provider.asetup(interactive=not unattended,
            user_index=0 if unattended else None, credentials=reader if unattended else None)
        browser.assert_not_called()
        calls = [asyncio.create_task(session()) for _ in range(4)]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not any(task.done() for task in calls)
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*calls), 3) == ['renewed'] * 4
            connect.assert_awaited_once()
            refresh.assert_awaited_once()
            assert session.profile_id == profile and store.load(profile).subject == connection.subject
            assert connect.await_args.kwargs['browser_owner'] is browser.return_value
            assert browser.call_args.kwargs == ({'headless': True} if unattended else {})
        finally:
            release.set()
            await provider.aclose()
            await asyncio.gather(*calls, return_exceptions=True)
        assert session._task.done() and session.client is None
    asyncio.run(scenario())
    assert reader.await_count == int(unattended)


@pytest.mark.parametrize('code', ['authorization_failed', 'invalid_connection', 'private failure'])
def test_unavailable_or_invalid_refresh_never_opens_login(cached, browser, monkeypatch, code):
    store, _, profile = cached
    monkeypatch.setattr(factory, 'refresh_connection', AsyncMock(side_effect=AuthError(code)))
    async def scenario():
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        session = await provider.asetup(interactive=True, user_index=None, credentials=None)
        try:
            with pytest.raises(InteractionRequiredError, match='could not refresh') as error:
                await session()
            assert code not in str(error.value)
            assert session.profile_id == profile
            browser.assert_not_called()
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_manual_noninteractive_expired_cache_fails_without_browser(cached, browser, monkeypatch):
    store, _, _ = cached
    monkeypatch.setattr(factory, 'refresh_connection', AsyncMock(side_effect=AuthError('refresh_rejected')))
    async def scenario():
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        session = await provider.asetup(interactive=False, user_index=None, credentials=None)
        try:
            with pytest.raises(InteractionRequiredError, match='Start attended.*select an unattended'):
                await session()
            browser.assert_not_called()
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_failed_renewal_is_not_an_unbounded_login_loop(cached, browser, monkeypatch):
    store, _, _ = cached
    refresh = AsyncMock(side_effect=AuthError('refresh_rejected'))
    monkeypatch.setattr(factory, 'refresh_connection', refresh)
    connect = AsyncMock(return_value='unchanged')
    monkeypatch.setattr(factory, 'connect_account', connect)
    async def scenario():
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        session = await provider.asetup(interactive=True, user_index=None, credentials=None)
        try:
            with pytest.raises(InteractionRequiredError, match='could not refresh'):
                await session()
            assert refresh.await_count == 2
            connect.assert_awaited_once()
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_cancellation_drains_the_single_owned_reconnect(cached, browser, monkeypatch):
    store, _, _ = cached
    monkeypatch.setattr(factory, 'refresh_connection', AsyncMock(side_effect=AuthError('refresh_rejected')))
    async def scenario():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def reconnect(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        connect = AsyncMock(side_effect=reconnect)
        monkeypatch.setattr(factory, 'connect_account', connect)
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        session = await provider.asetup(interactive=True, user_index=None, credentials=None)
        waiter = asyncio.create_task(session())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert not stopped.is_set()
        finally:
            await provider.aclose()
            await asyncio.gather(waiter, return_exceptions=True)
        assert stopped.is_set() and session._task.done() and not provider.browsers
        browser.return_value.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_unconfigured_session_maps_rejection_without_reconnect(cached, browser, monkeypatch):
    store, _, profile = cached
    monkeypatch.setattr(factory, 'refresh_connection', AsyncMock(side_effect=AuthError('refresh_rejected')))
    async def scenario():
        session = factory.OwnedSubscriptionSession(store, profile)
        try:
            with pytest.raises(InteractionRequiredError, match='could not refresh'):
                await session()
            browser.assert_not_called()
        finally:
            await session.aclose()
    asyncio.run(scenario())



def test_real_refresh_rejection_preserves_named_browser_error_at_shared_owner(cached, browser, monkeypatch):
    from api_agent.providers.sessions import LLMSessionOwner
    store, _, _ = cached
    original_client = httpx.AsyncClient
    responses = []
    def reject(request):
        assert request.url == oauth.TOKEN
        responses.append(request.method)
        return httpx.Response(400, text='private upstream body')
    monkeypatch.setattr(factory.httpx, 'AsyncClient',
        lambda: original_client(transport=httpx.MockTransport(reject)))
    monkeypatch.setattr(factory, 'connect_account', AsyncMock(side_effect=AuthError('browser_closed')))
    async def scenario():
        provider = factory.GPTProvider({'GPT_STATE_DIR': store.root})
        owner = LLMSessionOwner(provider, interactive=True)
        ready = AsyncMock()
        try:
            with pytest.raises(InteractionRequiredError, match='browser closed') as error:
                await owner.call(ready)
            assert 'private upstream body' not in str(error.value)
            assert responses == ['POST']
            ready.assert_not_awaited()
        finally:
            await owner.aclose()
            await provider.aclose()
        browser.return_value.aclose.assert_awaited_once()
    asyncio.run(scenario())
