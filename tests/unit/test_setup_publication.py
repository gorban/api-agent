"""Publication settles real detached authentication without live accounts."""
import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import SetupClient
from api_agent.telemetry.usage import QuotaReadError
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend


async def expire_after_start(exchange, startup, *, watchdog=5, tasks=None, entered=None):
    """Own the real request and arm its native deadline after setup blocks."""
    request = asyncio.create_task(exchange())
    startup_wait = asyncio.ensure_future(asyncio.shield(asyncio.wrap_future(startup)))
    owned = (request, startup_wait)
    if tasks is not None:
        tasks.extend(owned)
    if entered is not None:
        entered.set()
    try:
        async with asyncio.timeout(None) as deadline:
            done, _ = await asyncio.wait(owned, timeout=watchdog,
                                         return_when=asyncio.FIRST_COMPLETED)
            if request in done:
                await request  # Preserve an early transport exception.
                raise AssertionError('Readiness unexpectedly succeeded before deadline')
            if startup_wait not in done:
                raise AssertionError('Setup startup signal missing before watchdog')
            await startup_wait
            if request.done():
                await request
                raise AssertionError('Readiness unexpectedly succeeded before deadline')
            deadline.reschedule(asyncio.get_running_loop().time())
            await request
            raise AssertionError('Readiness unexpectedly succeeded before deadline')
    finally:
        for task in owned:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.fixture
def pending_backend(tmp_path, monkeypatch):
    state = SimpleNamespace(started=threading.Event(), stopped=threading.Event(),
                            release=None, resist=False, observations=[],
                            startup=Future(), loader_gate=Future(), tasks=[],
                            request_entered=threading.Event())
    auth = SimpleNamespace(aclose=AsyncMock())
    class Authentication:
        async def __call__(self):
            return object()
        aclose = auth.aclose
    async def setup(**kwargs):
        state.release = asyncio.Event()
        state.started.set()
        state.startup.set_result(None)
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
    state.loader_gate.set_result(None)
    async def load(*args, **kwargs):
        await asyncio.shield(asyncio.wrap_future(state.loader_gate))
        return loaded
    monkeypatch.setattr(provider_setup, 'aload_provider', load)
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'DEBUG': True}, tmp_path,
                           quota_observer=lambda phase, value: state.observations.append(phase))
    client = SetupClient(backend.start(), timeout=10)
    exchange = client._exchange
    async def controlled_exchange(action, request=None):
        if action != 'ready':
            return await exchange(action, request)
        return await expire_after_start(lambda: exchange(action, request), state.startup,
                                        tasks=state.tasks, entered=state.request_entered)
    client._exchange = controlled_exchange
    state.backend, state.client = backend, client
    yield state
    if state.release is not None and not backend.loop.is_closed():
        backend.loop.call_soon_threadsafe(state.release.set)
    if not state.loader_gate.done():
        state.loader_gate.set_result(None)
    try:
        client.close()
    finally:
        backend.cleanup_timeout = 2
        try:
            backend.close()
        finally:
            # Only disposal may settle an absent entry signal; it must never
            # manufacture setup entry while authentication can still start.
            if backend.closed and backend.loop.is_closed() and not backend.thread.is_alive():
                if not state.startup.done():
                    state.startup.set_result(None)
    assert backend.closed and backend.loop.is_closed() and not backend.thread.is_alive()
    assert client._disposed and (client._loop is None or client._loop.is_closed())
    assert all(task.done() for task in state.tasks)
    assert not state.startup.cancelled()


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


def test_delayed_loader_does_not_expire_before_setup(pending_backend):
    state = pending_backend
    state.loader_gate = Future()
    outcome = Future()
    def request():
        try:
            expire_waiter(state)
            outcome.set_result(None)
        except BaseException as error:
            outcome.set_exception(error)
    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert state.request_entered.wait(5)
        assert not state.started.wait(.2)
        assert not outcome.done()
        state.loader_gate.set_result(None)
        outcome.result(timeout=5)
        assert not state.stopped.is_set()
    finally:
        if not state.loader_gate.done():
            state.loader_gate.set_result(None)
        worker.join(10)
        assert not worker.is_alive()


@pytest.mark.parametrize('error, expected, message', [
    (OSError('synthetic transport failure'), OSError, 'synthetic transport failure'),
    (None, AssertionError, 'unexpectedly succeeded'),
])
def test_harness_preserves_early_transport_outcome(error, expected, message):
    startup = Future()
    tasks = []
    exchange = AsyncMock(side_effect=error)

    async def exercise():
        with pytest.raises(expected, match=message):
            await expire_after_start(exchange, startup, tasks=tasks)
        assert all(task.done() for task in tasks)
        assert not startup.done()
        startup.set_result(None)

    asyncio.run(exercise())


def test_harness_missing_startup_drains_request():
    startup = Future()
    tasks = []
    drained = []
    async def exchange():
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(True)

    async def exercise():
        with pytest.raises(AssertionError, match='startup signal missing'):
            await expire_after_start(exchange, startup, watchdog=.01, tasks=tasks)
        assert drained == [True]
        assert all(task.done() for task in tasks)
        assert not startup.done()
        startup.set_result(None)

    asyncio.run(exercise())


def test_harness_cancellation_drains_request_without_cancelling_signal():
    startup = Future()
    tasks = []
    drained = []
    async def exercise():
        request_started = asyncio.Event()
        async def exchange():
            try:
                request_started.set()
                await asyncio.Event().wait()
            finally:
                drained.append(True)
        harness = asyncio.create_task(expire_after_start(exchange, startup, tasks=tasks))
        await request_started.wait()
        harness.cancel()
        with pytest.raises(asyncio.CancelledError):
            await harness
        assert drained == [True]
        assert all(task.done() for task in tasks)
        assert not startup.done()
        startup.set_result(None)

    asyncio.run(exercise())
