import asyncio
import httpx
from api_agent.testing import reasoning_contrast as contrast

def test_partial_task_creation_failure_does_not_start_abandoned_requests(monkeypatch):
    async def scenario():
        original=asyncio.create_task;owned=[];rejected=[];attempts=[];gate=asyncio.Event();created=0
        def create(coroutine,*args,**kwargs):
            nonlocal created
            if coroutine.__qualname__.endswith('run_pairs.<locals>.call'):
                created+=1
                if created==4:
                    rejected.append(coroutine)
                    raise RuntimeError('synthetic scheduler allocation failure')
            task=original(coroutine,*args,**kwargs);owned.append(task);return task
        async def reply(sent):
            attempts.append(sent)
            await gate.wait()
            return httpx.Response(503)
        monkeypatch.setattr(contrast.asyncio,'create_task',create)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            try:
                try:await contrast.run_pairs(client,'http://127.0.0.1:1','nonce')
                except RuntimeError as error:assert 'scheduler' in str(error)
                await asyncio.sleep(0)
                assert not attempts, 'previously created tasks started HTTP side effects after helper failure'
                assert all(task.done() for task in owned)
                assert rejected[0].cr_frame is None, 'unsubmitted coroutine must be closed'
            finally:
                gate.set()
                await asyncio.gather(*owned,return_exceptions=True)
    asyncio.run(scenario())
