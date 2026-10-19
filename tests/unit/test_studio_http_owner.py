"""Controlled bridge ownership and retry receipts; no external providers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.testing import studio_http as bridge
from api_agent.providers.control import CleanupPendingError


def test_cleanup_returns_original_failure_and_receipt():
    async def run():
        error = asyncio.CancelledError('original')
        action = AsyncMock(side_effect=error)
        with pytest.raises(asyncio.CancelledError) as raised:
            await bridge._cleanup(action)
        assert raised.value is error
        failure = RuntimeError('synthetic')
        with pytest.raises(RuntimeError) as raised:
            await bridge._cleanup(AsyncMock(side_effect=failure))
        assert raised.value is failure
        await bridge._cleanup(AsyncMock())
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, 'cancel', 'pending', 'error'])
def test_cleanup_interruption_keeps_child_duties(monkeypatch, failure):
    async def run():
        original = asyncio.CancelledError('outer')
        child = {'cancel': asyncio.CancelledError('child'),
                 'pending': CleanupPendingError(('owned',), AsyncMock()),
                 'error': RuntimeError('child')}.get(failure)
        async def finish(work):
            await work
            raise original
        monkeypatch.setattr(bridge, 'finish_cleanup', finish)
        action = AsyncMock(side_effect=child)
        with pytest.raises(asyncio.CancelledError) as raised:
            await bridge._cleanup(action)
        if failure == 'cancel':
            assert raised.value is child
        else:
            assert raised.value is original
            receipt = getattr(original, 'cleanup_pending', None)
            if failure == 'pending': assert receipt is child
            elif failure == 'error':
                assert receipt.owners == (action,)
                action.side_effect = None
                monkeypatch.setattr(bridge, 'finish_cleanup', lambda work: work)
                await receipt.retry()
                assert action.await_count == 2
            else: assert receipt is None
    asyncio.run(run())


@pytest.mark.parametrize('already_cancelled,still_pending', [(False,False),(True,False),(False,True)])
def test_server_disposal_retains_exact_task_until_acknowledged(monkeypatch, already_cancelled, still_pending):
    async def run():
        gate = asyncio.Event()
        async def held():
            try: await gate.wait()
            except asyncio.CancelledError: await gate.wait()
        task = asyncio.create_task(held())
        await asyncio.sleep(0)
        if already_cancelled:
            task.cancel()
            await asyncio.sleep(0)
        server = SimpleNamespace(should_exit=False)
        waits = 0
        async def wait(tasks, *, timeout):
            nonlocal waits
            waits += 1
            if waits == 1 or still_pending: return set(), tasks
            gate.set()
            await task
            return tasks, set()
        monkeypatch.setattr(bridge.asyncio, 'wait', wait)
        try:
            if still_pending:
                with pytest.raises(CleanupPendingError) as raised:
                    await bridge._close_server(server, task)
                assert raised.value.owners == (server, task)
                gate.set()
                await task
                still_wait = AsyncMock(return_value=({task}, set()))
                monkeypatch.setattr(bridge.asyncio, 'wait', still_wait)
                await raised.value.retry()
            else: await bridge._close_server(server, task)
            assert server.should_exit and task.done()
        finally:
            gate.set()
            await task
    asyncio.run(run())


def test_server_disposal_acknowledges_cancelled_task():
    async def run():
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        await bridge._close_server(SimpleNamespace(should_exit=False), task)
        assert task.cancelled()
    asyncio.run(run())


@pytest.mark.parametrize('retained,supplied_listener', [(False,False),(True,True)])
def test_actual_http_bridge_preserves_request_and_owned_lifetime(retained, supplied_listener):
    import httpx
    import socket
    from uuid import uuid4
    from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
    from api_agent.testing.studio_retained import RetainedStudio
    class Provider:
        def __init__(self): self.calls = []
        def describe(self): return (AvailableSelection('sample', features=('reasoning_effort',), reasoning_efforts=('off','medium')),)
        def complete(self, request):
            self.calls.append(request)
            return LLMResult(Message('assistant', (TextContent('Maybe'),)))
    async def run():
        provider = Provider()
        owner = RetainedStudio(provider, lambda _: None, lambda: False) if retained else provider
        listener = None
        if supplied_listener:
            listener = socket.socket()
            listener.bind(('127.0.0.1',0)); listener.listen(); listener.setblocking(False)
        try:
            async with bridge.agent_endpoint(owner, 'sample', 'http://localhost:9443', 'x'*32,
                    listener=listener, reasoning_default='medium') as url:
                async with httpx.AsyncClient() as client:
                    headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32}
                    models = await client.get(url+'/app/agent/models', headers=headers)
                    assert models.json()['reasoning_default'] == 'medium'
                    result = await client.post(url+'/app/agent/chat',headers=headers,json={
                        'model':'sample','reasoning_effort':'off','session_id':str(uuid4()),
                        'messages':[{'role':'user','content':'Maybe'}]})
                    assert result.status_code == 200 and result.json()['response_text'] == 'Maybe'
                    assert provider.calls[0].payload.generation.reasoning_effort == 'off'
                    denied = await client.get(url+'/app/agent/models',headers={})
                    assert denied.status_code == 403
            if retained: assert owner.phase == 'closed'
        finally:
            if listener is not None: listener.close()
    asyncio.run(run())


def test_http_catalog_and_send_keep_exact_known_family_default_selection():
    import httpx
    from uuid import uuid4
    from api_agent.llm_providers.gpt.discovery import _catalog
    from api_agent.providers.llm import LLMResult, Message, TextContent
    selections = _catalog({'data': [{'id': model} for model in ('gpt-5.6-terra', 'gpt-5.6-sol')]},
                          False, 'synthetic-token', base_url='https://api.openai.com/v1')

    class Provider:
        def __init__(self): self.calls = []
        def describe(self): return selections
        def complete(self, request):
            self.calls.append(request)
            return LLMResult(Message('assistant', (TextContent('Maybe'),)))

    async def run():
        provider = Provider()
        headers = {'Origin': 'http://localhost:9443', 'X-API-Agent-Session': 'x' * 32}
        async with bridge.agent_endpoint(provider, 'gpt-5.6-terra', 'http://localhost:9443', 'x' * 32,
                policy_config={'LLM_ALLOWED_MODELS': r'gpt-5\.6-(terra|sol)'}) as url:
            async with httpx.AsyncClient() as client:
                response = await client.get(url + '/app/agent/models', headers=headers)
                assert response.status_code == 200
                catalog = response.json()
                assert catalog['default'] == 'gpt-5.6-terra'
                assert catalog['family_defaults'] == {'gpt-terra': 'gpt-5.6-terra', 'gpt-sol': 'gpt-5.6-sol'}
                assert [(row['value'], row['reasoning_efforts']) for row in catalog['models']] == [
                    ('gpt-5.6-terra', ['off', 'low', 'medium', 'high']),
                    ('gpt-5.6-sol', ['off', 'low', 'medium', 'high'])]
                result = await client.post(url + '/app/agent/chat', headers=headers, json={
                    'model': catalog['default'], 'reasoning_effort': 'off', 'session_id': str(uuid4()),
                    'messages': [{'role': 'user', 'content': 'Maybe'}]})
                assert result.status_code == 200 and result.json()['response_text'] == 'Maybe'
        assert len(provider.calls) == 1
        assert provider.calls[0].selected_llm == 'gpt-5.6-terra'
        assert provider.calls[0].payload.generation.reasoning_effort == 'off'

    asyncio.run(run())


def test_stopped_server_before_readiness_fails_and_closes_listener(monkeypatch):
    import uvicorn
    seen=[]
    class Server:
        started=False
        def __init__(self,config): self.should_exit=False
        async def serve(self, *, sockets):
            seen.extend(sockets)
    monkeypatch.setattr(uvicorn,'Server',Server)
    async def run():
        with pytest.raises(AssertionError,match='stopped before readiness'):
            async with bridge.agent_endpoint(object(),'sample','http://localhost:9443','x'*32):
                pytest.fail('unready server admitted')
        assert seen[0].fileno() == -1
    asyncio.run(run())
