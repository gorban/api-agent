"""The virtual clock moves only when the loop is idle on a timer, never while real work or a stall runs."""
import asyncio
import time

import pytest

from api_agent.providers import progress
from api_agent.workflows import execution
from virtual_time import run


def test_a_blocked_loop_does_not_spend_a_timeout():
    async def stalled():
        async with asyncio.timeout(0.01):
            time.sleep(0.05)
            await asyncio.sleep(0.001)
        return 'finished'

    assert run(stalled()) == 'finished'


def test_an_idle_wait_on_a_timer_jumps_to_it():
    async def waits():
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(10):
                await asyncio.sleep(3600)
        return asyncio.get_running_loop().time() - started

    assert run(waits()) == 10


def test_time_does_not_pass_while_a_worker_thread_runs():
    async def threaded():
        async with asyncio.timeout(0.01):
            await asyncio.to_thread(time.sleep, 0.05)
        return 'finished'

    assert run(threaded()) == 'finished'


def test_every_deadline_reader_follows_the_loop_clock_and_is_restored():
    async def readings():
        loop = asyncio.get_running_loop()
        await asyncio.sleep(5)
        return loop.time(), progress._clock(), execution.time.monotonic()

    loop_time, progress_time, engine_time = run(readings())
    assert loop_time == progress_time == engine_time
    assert progress._clock is time.monotonic and execution.time is time
