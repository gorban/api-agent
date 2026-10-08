"""Deterministic authentication-only factory checks without live transports."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.gpt import factory as module
from api_agent.llm_providers.gpt.connections import ConnectionStore
from api_agent.llm_providers.gpt.contracts import Connection
from api_agent.providers.contracts import ContractError, InteractionRequiredError, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def store(monkeypatch):
    class Store:
        entries = ()
        loaded = []
        def __init__(self, path):
            self.path = path
        def list(self):
            return self.entries
        def load(self, profile):
            self.loaded.append(profile)
        def profile_id(self, connection):
            return connection if isinstance(connection, str) else connection.profile
    monkeypatch.setattr(module, "ConnectionStore", Store)
    return Store


def setup(provider, interactive=False):
    return provider.asetup(interactive=interactive, user_index=None, credentials=None)


def test_factory_no_io(monkeypatch):
    monkeypatch.setattr(module, "ConnectionStore", lambda *_: pytest.fail("storage at creation"))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: pytest.fail("transport at creation"))
    provider = module.factory({}, object())
    assert provider.version == 1
    assert provider.capabilities.operations == ()
    assert provider.capabilities.credential_roles == ()
    assert provider.state_dir == module.Path.home() / ".config/api-agent/gpt"
    assert module.factory({"GPT_AUTH_MODE": "api_key", "GPT_STATE_DIR": "/tmp/test"}, None).api_mode == "completions"


@pytest.mark.parametrize("config,message", [({"GPT_AUTH_MODE": "api_key"}, "pending"), ({"GPT_API_MODE": "completions"}, "requires responses")])
def test_route_rejected_before_io(config, message, monkeypatch):
    monkeypatch.setattr(module, "ConnectionStore", lambda *_: pytest.fail("storage"))
    with pytest.raises(SetupPrerequisiteError, match=message):
        run(setup(module.factory(config, None)))


@pytest.mark.parametrize('name', ['GPT_AUTH_MODE', 'GPT_API_MODE'])
@pytest.mark.parametrize('value', [None, False, 42, [], 'malformed'])
def test_malformed_modes_are_errors_not_missing_prerequisites(name, value, monkeypatch):
    monkeypatch.setattr(module, 'ConnectionStore', lambda *_: pytest.fail('unexpected storage'))
    with pytest.raises(ContractError, match=name) as error:
        run(setup(module.factory({name: value}, None)))
    assert not isinstance(error.value, SetupPrerequisiteError)


def test_uncached_and_ambiguous(store, monkeypatch):
    monkeypatch.setattr(module, "connect_account", AsyncMock(side_effect=AssertionError("browser")))
    provider = module.factory({}, None)
    with pytest.raises(InteractionRequiredError, match="attended sign-in.*start attended"):
        run(setup(provider))
    store.entries = ("one", "two")
    with pytest.raises(InteractionRequiredError, match="attended sign-in"):
        run(setup(provider))


@pytest.mark.parametrize("config,entries,interactive,expected", [({}, ("single",), False, "single"), ({}, ("one", "two"), True, "new"), ({}, (), True, "new")])
def test_select_and_close(config, entries, interactive, expected, store, monkeypatch):
    store.entries = entries
    connect = AsyncMock(return_value="new")
    monkeypatch.setattr(module, "connect_account", connect)
    async def scenario():
        provider = module.factory(config, None)
        session = await setup(provider, interactive)
        assert session.profile_id == expected
        assert session.client is None
        await provider.aclose()
        await provider.aclose()
        assert session._closed
        with pytest.raises(ContractError, match="closed"):
            await setup(provider)
    run(scenario())
    assert connect.await_count == int(len(entries) != 1)


def test_cached_setup_never_allocates_a_browser(store, monkeypatch):
    store.entries = ('single',)
    monkeypatch.setattr(module, 'subscription_browser', lambda: pytest.fail('browser allocated for cached setup'))
    async def scenario():
        provider = module.factory({}, None)
        session = await setup(provider)
        assert session.profile_id == 'single' and not provider.browsers
        await provider.aclose()
    run(scenario())


@pytest.mark.parametrize('run_ui', [False, True])
def test_failed_attended_setup_keeps_browser_until_cleanup_confirmed(store, monkeypatch, run_ui):
    browser = SimpleNamespace(aclose=AsyncMock())
    browser.aclose.side_effect = [CleanupPendingError((browser,), browser.aclose), None]
    monkeypatch.setattr(module, 'subscription_browser', lambda: browser)
    connect = AsyncMock(side_effect=RuntimeError('consent failed'))
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory({'RUN_UI': run_ui}, None)
        with pytest.raises(RuntimeError, match='consent failed'):
            await setup(provider, interactive=True)
        assert provider.browsers == [browser] and not provider.sessions
        assert connect.await_args.kwargs['browser_owner'] is browser
        with pytest.raises(CleanupPendingError):
            await provider.aclose()
        assert provider.browsers == [browser]
        await provider.aclose()
        assert not provider.browsers
        browser.aclose.assert_awaited()
        assert browser.aclose.await_count == 2
    run(scenario())


@pytest.mark.parametrize("username,entries,expected", [
    (" Person@example.test ", ("person@example.test", "other@example.test"), "0"),
    ("missing@example.test", ("other@example.test",), None),
    ("person@example.test", ("person@example.test", "person@example.test"), None),
    ("", (), "invalid"), (None, (), "invalid"),
])
def test_optional_username_selects_only_unique_matching_cache(username, entries, expected, store, monkeypatch):
    store.entries = tuple(SimpleNamespace(email=email, profile=str(index))
                          for index, email in enumerate(entries))
    reader = AsyncMock(return_value=username)
    connect = AsyncMock(side_effect=AssertionError("unexpected browser"))
    monkeypatch.setattr(module, "connect_account", connect)
    async def scenario():
        provider = module.factory({}, None)
        try:
            if expected == "invalid":
                with pytest.raises(ContractError, match="nonempty username"):
                    await provider.asetup(interactive=False, user_index=1, credentials=reader)
            elif expected is None:
                with pytest.raises(InteractionRequiredError, match="no unique cached"):
                    await provider.asetup(interactive=False, user_index=1, credentials=reader)
            else:
                auth = await provider.asetup(interactive=False, user_index=1, credentials=reader)
                assert auth.profile_id == expected
        finally:
            await provider.aclose()
    run(scenario())
    reader.assert_awaited_once_with(1, "username")
    connect.assert_not_awaited()


def test_refresh_transport_lifetime(store, monkeypatch):
    client = type("Client", (), {"aclose": AsyncMock()})()
    client_factory = lambda: client
    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    refresh = AsyncMock(return_value="updated")
    monkeypatch.setattr(module, "refresh_connection", refresh)
    async def scenario():
        session = module.OwnedSubscriptionSession(store("path"), "one")
        assert await session._refresh("connection", 1) == "updated"
        assert await session._refresh("connection", 2) == "updated"
        await session.aclose()
        await session.aclose()
        assert session.client is None
    run(scenario())
    client.aclose.assert_awaited_once()


def test_cancelled_dispose_retains_transport(store, monkeypatch):
    client = type("Client", (), {"aclose": AsyncMock()})()
    async def cancelled(self):
        raise asyncio.CancelledError
    monkeypatch.setattr(module.SubscriptionSession, "_dispose", cancelled)
    async def scenario():
        session = module.OwnedSubscriptionSession(store("path"), "one")
        session.client = client
        with pytest.raises(asyncio.CancelledError):
            await session.aclose()
        assert session.client is client
    run(scenario())
    client.aclose.assert_not_awaited()


def test_cancelled_cleanup_waits_for_refresh_before_transport_close(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path / "state")
    record = Connection("subject", "client", store.host_id(), "person@example.test",
                        "access", "refresh", "identity", ("scope",), 0)
    profile = store.save(record)
    client = type("Client", (), {"aclose": AsyncMock()})()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: client)

    async def scenario():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def refresh(connection, *, client, now):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            client.aclose.assert_not_awaited()
            return replace(connection, expires_at=200)

        monkeypatch.setattr(module, "refresh_connection", refresh)
        provider = module.factory({"GPT_STATE_DIR": store.root}, None)
        session = await setup(provider)
        session.clock = lambda: 100
        reader = asyncio.create_task(session())
        await entered.wait()
        closer = asyncio.create_task(session.aclose())
        await cancelled.wait()
        closer.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert not session._task.done()
            assert session.client is client
            client.aclose.assert_not_awaited()
        finally:
            release.set()
            await reader
            await provider.aclose()
        assert session._task.done()
        assert session.client is None
        client.aclose.assert_awaited_once()
        await provider.aclose()

    run(scenario())
