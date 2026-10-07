import asyncio
from types import SimpleNamespace
from threading import Event, Thread
import pytest
from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.telemetry.tracing import TraceRuntime
from test_tracing import config, fake
run = asyncio.run

def test_repeated_start_cancellation_and_concurrent_waiters():
    async def scenario():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        async def start():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                events.append('cleaned')
        async def close():
            events.append('closed')
        runtime = Runtime('pack', llm_provider=SimpleNamespace(astart=start, aclose=close))
        first = asyncio.create_task(runtime.astart())
        second = asyncio.create_task(runtime.astart())
        await entered.wait()
        first.cancel()
        await cleanup.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and not second.done() and events == []
        assert runtime._start_task.cancelling() == 1
        release.set()
        await closing
        with pytest.raises(asyncio.CancelledError):
            await second
        assert events == ['cleaned', 'closed']
        assert runtime.draining and not runtime.started
        with pytest.raises(RuntimeLifecycleError, match='runtime_closed'):
            await runtime.astart()
    run(scenario())

def test_concurrent_exporter_close_acquires_completion_atomically():
    provider_, _, _, factory = fake()
    runtime = TraceRuntime(config(), factory=factory)
    run(runtime.astart())
    paused, release = Event(), Event()
    bounded = runtime._bounded
    def gated(timeout):
        if not paused.is_set():
            paused.set()
            assert release.wait(3)
        return bounded(timeout)
    runtime._bounded = gated
    results = []
    thread = Thread(target=lambda: results.append(runtime.shutdown()))
    thread.start()
    assert paused.wait(3)
    assert runtime.shutdown()
    release.set()
    thread.join(3)
    assert not thread.is_alive() and results == [True]
    assert provider_.shutdown.call_count == provider_.force_flush.call_count == 1

def test_concurrent_start_waiters_share_single_owner():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def start():
            calls.append('start')
            entered.set()
            await release.wait()
        runtime = Runtime('pack', llm_provider=SimpleNamespace(astart=start))
        waiters = [asyncio.create_task(runtime.astart()) for _ in range(3)]
        await entered.wait()
        assert calls == ['start'] and not runtime.ready
        release.set()
        await asyncio.gather(*waiters)
        assert runtime.ready and calls == ['start']
        await runtime.aclose()
    run(scenario())
