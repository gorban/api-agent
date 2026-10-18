"""Progress observer allocation failures close exactly their owned coroutines."""
import asyncio
import inspect

import pytest
from api_agent.providers.progress import progress_sink,report_timeout


@pytest.mark.parametrize('name',['heartbeat','stop_heartbeat'])
def test_actual_progress_allocator_rejection_closes_native_work(monkeypatch,name):
    async def run():
        original=asyncio.create_task
        rejected=[]
        def create(coroutine,**kwargs):
            if coroutine.cr_code.co_name == name:
                rejected.append(coroutine)
                raise RuntimeError('controlled observer allocation failure')
            return original(coroutine,**kwargs)
        monkeypatch.setattr(asyncio,'create_task',create)
        with progress_sink(lambda event: None):
            with pytest.raises(RuntimeError,match='controlled observer allocation failure'):
                async with report_timeout('Controlled observer',1):
                    pass
        assert len(rejected) == 1
        assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
        assert len(asyncio.all_tasks()) == 1
        monkeypatch.setattr(asyncio,'create_task',original)
        async with report_timeout('After failure',1) as budget:
            assert budget.parent_id is None
    asyncio.run(run())
