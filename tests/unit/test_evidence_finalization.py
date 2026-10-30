import asyncio
from concurrent.futures import Future
import pytest

from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.evidence_finalization import enroll, finalize


def test_visit_finalization_waits_for_its_owned_publisher():
    async def scenario():
        future = Future()
        with bind_workflow_visit(object(), 'visit'):
            enroll(future)
            work = asyncio.create_task(finalize())
            await asyncio.sleep(0)
            assert not work.done()
            future.set_result(None)
            await work
    asyncio.run(scenario())


def test_cancellation_does_not_abandon_a_visit_publisher():
    async def scenario():
        future = Future()
        with bind_workflow_visit(object(), 'visit'):
            enroll(future)
            work = asyncio.create_task(finalize())
            await asyncio.sleep(0)
            work.cancel()
            await asyncio.sleep(0)
            assert not work.done()
            future.set_result(None)
            with pytest.raises(asyncio.CancelledError):
                await work
    asyncio.run(scenario())
