"""Publication settles real detached authentication without live accounts."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import SetupClient
from api_agent.telemetry.usage import QuotaReadError
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend


@pytest.fixture
def pending_backend(tmp_path, monkeypatch):
    state = SimpleNamespace(started=threading.Event(), stopped=threading.Event(),
                            release=None, resist=False, observations=[])
    auth = SimpleNamespace(aclose=AsyncMock())
    class Authentication:
        async def __call__(self):
            return object()
        aclose = auth.aclose
    async def setup(**kwargs):
        state.release = asyncio.Event()
        state.started.set()
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            if state.resist:
                await state.release.wait()
            else:
                raise
        finally:
            state.stopped.set()
        return Authentication()
    adapter = SimpleNamespace(asetup=setup, asnapshot=AsyncMock(return_value=QuotaReadError('request')))
    loaded = SimpleNamespace(implementation=adapter, aclose=AsyncMock())
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=loaded))
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'DEBUG': True}, tmp_path,
                           quota_observer=lambda phase, value: state.observations.append(phase))
    client = SetupClient(backend.start(), timeout=.1)
    state.backend, state.client = backend, client
    yield state
    if state.release is not None and not backend.loop.is_closed():
        backend.loop.call_soon_threadsafe(state.release.set)
    client.close()
    backend.cleanup_timeout = 2
    backend.close()
    assert backend.closed and backend.loop.is_closed() and not backend.thread.is_alive()


def expire_waiter(state):
    with pytest.raises(ContractError, match='Shared setup connection failed') as caught:
        state.client.ensure_ready()
    assert 'category: deadline' in str(caught.value.__cause__)
    assert state.started.is_set() and not state.stopped.is_set()


def test_publication_stops_shielded_initialization_before_freezing_accounting(pending_backend):
    state = pending_backend
    expire_waiter(state)

    state.backend.prepare_publication()
    frozen = tuple(state.observations)

    assert state.stopped.is_set()
    owner = state.backend.registry._owner.provider
    assert owner.owner._initializing.done()
    assert owner._shared_setup._ready.done()
    assert state.backend.registry._ready.done()
    assert frozen == ()
    asyncio.run_coroutine_threadsafe(owner._capture_quota('before'), state.backend.loop).result(timeout=1)
    assert tuple(state.observations) == frozen
    state.backend.finish_run()
    assert tuple(state.observations) == frozen


def test_unsettled_authentication_retains_publication_cleanup_receipt(pending_backend):
    state = pending_backend
    state.resist = True
    expire_waiter(state)
    state.backend.cleanup_timeout = .05

    with pytest.raises(CleanupPendingError) as caught:
        state.backend.prepare_publication()

    assert caught.value.owners == (state.backend,)
    assert state.backend.pending and not state.backend._publication_prepared
    assert not state.stopped.is_set()
    state.backend.loop.call_soon_threadsafe(state.release.set)
    assert state.stopped.wait(1)
    state.backend.cleanup_timeout = 2
    state.backend.close()
    assert state.observations == []


def test_ready_accounting_is_finalized_before_publication(pending_backend):
    state = pending_backend
    expire_waiter(state)
    state.backend.loop.call_soon_threadsafe(state.release.set)
    assert state.stopped.wait(1)
    # Readiness is still alive despite its departed worker; wait for its quota.
    async def completed():
        await state.backend.registry._ready
    asyncio.run_coroutine_threadsafe(completed(), state.backend.loop).result(timeout=1)
    assert state.observations == ['before']

    state.backend.prepare_publication()

    assert state.observations == ['before', 'after']
    state.backend.finish_run()
    assert state.observations == ['before', 'after']


def test_unstarted_backend_has_no_publication_side_effects(tmp_path):
    backend = SetupBackend({}, tmp_path)
    assert asyncio.run(backend._prepare_publication()) is None
    assert backend.registry is backend.server is backend.thread is None
    backend.close()
