import asyncio
import threading

import pytest

from api_agent.providers.authority_gate import acquire_authorities


@pytest.mark.asyncio
async def test_unrelated_authorities_do_not_wait():
    first, second = object(), object()
    async with acquire_authorities([first], 1):
        async with asyncio.timeout(0.5):
            async with acquire_authorities([second], 1):
                pass


@pytest.mark.asyncio
async def test_opposite_order_and_duplicates_do_not_deadlock():
    first, second = object(), object()
    seen = []

    async def run(authorities, value):
        async with acquire_authorities(authorities, 1):
            seen.append(value)
            await asyncio.sleep(0)

    await asyncio.gather(run([first, second, first], 1), run([second, first], 2))
    assert sorted(seen) == [1, 2]


@pytest.mark.asyncio
async def test_fifo_and_cancelled_waiter():
    authority = object()
    seen = []

    async def run(value):
        async with acquire_authorities([authority], 1):
            seen.append(value)

    async with acquire_authorities([authority], 1):
        first = asyncio.create_task(run(1))
        cancelled = asyncio.create_task(run(2))
        third = asyncio.create_task(run(3))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
    await asyncio.gather(first, third)
    assert seen == [1, 3]


@pytest.mark.asyncio
async def test_timeout_releases_partial_acquisition():
    first, second = sorted([object(), object()], key=id)
    async with acquire_authorities([second], 1):
        with pytest.raises(TimeoutError):
            async with acquire_authorities([first, second], 0.01):
                pytest.fail('unexpected acquisition')
        async with asyncio.timeout(0.5):
            async with acquire_authorities([first], 1):
                pass


@pytest.mark.asyncio
async def test_waiter_in_other_thread_is_woken_without_blocking_loop():
    authority = object()
    waiting = threading.Event()
    entered = threading.Event()

    def worker():
        async def run():
            waiting.set()
            async with acquire_authorities([authority], 1):
                entered.set()
        asyncio.run(run())

    async with acquire_authorities([authority], 1):
        thread = threading.Thread(target=worker)
        thread.start()
        await asyncio.to_thread(waiting.wait, 0.5)
        await asyncio.sleep(0.02)
        assert not entered.is_set()
    await asyncio.to_thread(thread.join, 1)
    assert entered.is_set()
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_cancellation_after_grant_wakes_next_waiter():
    authority = object()
    seen = []

    async def run(value):
        async with acquire_authorities([authority], 1):
            seen.append(value)

    async with acquire_authorities([authority], 1):
        first = asyncio.create_task(run(1))
        second = asyncio.create_task(run(2))
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert seen == [2]
