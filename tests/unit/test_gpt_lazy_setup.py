"""Deterministic authentication-only factory checks without live transports."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.gpt import factory as module
from api_agent.llm_providers.gpt.connections import ConnectionStore
from api_agent.llm_providers.gpt.contracts import Connection
from api_agent.providers.contracts import ContractError, InteractionRequiredError


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
            return connection
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
    with pytest.raises(ContractError, match=message):
        run(setup(module.factory(config, None)))


def test_uncached_and_ambiguous(store, monkeypatch):
    monkeypatch.setattr(module, "connect_account", AsyncMock(side_effect=AssertionError("browser")))
    provider = module.factory({}, None)
    with pytest.raises(InteractionRequiredError, match="attended sign-in.*start attended"):
        run(setup(provider))
    store.entries = ("one", "two")
    with pytest.raises(InteractionRequiredError, match="GPT_PROFILE"):
        run(setup(provider, True))


@pytest.mark.parametrize("config,entries,interactive,expected", [({}, ("single",), False, "single"), ({"GPT_PROFILE": "explicit"}, ("other",), False, "explicit"), ({}, (), True, "new")])
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
    assert connect.await_count == int(not entries)


def test_explicit_profile_error_does_not_connect(store, monkeypatch):
    def fail(*_):
        raise ContractError("missing")
    monkeypatch.setattr(store, "load", fail)
    monkeypatch.setattr(module, "connect_account", AsyncMock(side_effect=AssertionError("browser")))
    with pytest.raises(ContractError, match="missing"):
        run(setup(module.factory({"GPT_PROFILE": "missing"}, None), True))


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
        provider = module.factory({"GPT_STATE_DIR": store.root, "GPT_PROFILE": profile}, None)
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
