"""Controlled real HTTP fixture regressions; no browser or external provider."""
import asyncio
import importlib.util
from pathlib import Path
import threading

import httpx
import pytest

from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent


@pytest.fixture
def connection():
    source = Path(__file__).parents[1] / 'integration' / 'test_studio_request.py'
    spec = importlib.util.spec_from_file_location('studio_connection_test_helper', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Provider:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
    def describe(self):
        return (AvailableSelection('sample'),)
    def complete(self, request):
        self.entered.set()
        try:
            assert self.release.wait(10), 'Controlled provider was not released.'
            return LLMResult(Message('assistant', (TextContent('Ok'),)))
        finally:
            self.finished.set()


async def wait_event(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(.01)


async def send(client, config, origin):
    return await client.post(config['apiOrigin'] + '/app/agent/chat',
        headers={'Origin': origin, **config['apiHeaders']}, json={
            'selected_llm': 'sample', 'request_id': 'controlled', 'max_output_tokens': None,
            'cache_policy': None, 'messages': [{'role': 'user', 'text': 'synthetic'}]})


def test_disconnect_retains_native_completion_until_drained(connection):
    async def run():
        provider = Provider()
        active = []
        original = connection.StudioBackend
        def observe(**kwargs):
            backend = original(**kwargs)
            active.append(backend)
            return backend
        connection.StudioBackend = observe
        try:
            async with connection.studio_endpoint(provider, 'sample') as origin:
                async with httpx.AsyncClient(trust_env=False) as client:
                    config = (await client.get(origin + '/config.json')).json()
                    task = asyncio.create_task(send(client, config, origin))
                    await wait_event(provider.entered)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    await asyncio.sleep(.1)
                    assert not provider.finished.is_set()
                    assert active[0]._contexts
                    context = next(iter(active[0]._contexts.values()))
                    assert context.retained_tasks
                    provider.release.set()
                    await wait_event(provider.finished)
            assert not active[0]._contexts
            assert active[0].owner._closed
            assert provider.finished.is_set()
        finally:
            provider.release.set()
            await wait_event(provider.finished)
    asyncio.run(run())


def test_server_cleanup_failure_still_closes_node_and_socket(connection, monkeypatch):
    async def run():
        provider = Provider()
        provider.release.set()
        children = []
        listeners = []
        spawn = connection.asyncio.create_subprocess_exec
        create = connection.create_application
        async def record_spawn(*args, **kwargs):
            child = await spawn(*args, **kwargs)
            children.append(child)
            return child
        async def fail_server(server, task):
            # First settle the real server, then simulate its terminal failure.
            await original_close(server, task)
            raise RuntimeError('controlled server failure')
        def record_application(runtime):
            listeners.append(runtime)
            return create(runtime)
        original_close = connection._close_server
        monkeypatch.setattr(connection.asyncio, 'create_subprocess_exec', record_spawn)
        monkeypatch.setattr(connection, '_close_server', fail_server)
        monkeypatch.setattr(connection, 'create_application', record_application)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(RuntimeError, match='controlled server failure'):
                    async with connection.studio_endpoint(provider, 'sample') as origin:
                        config = (await client.get(origin + '/config.json')).json()
                        assert (await send(client, config, origin)).status_code == 200
                assert children[0].returncode is not None
                assert listeners[0]._closed
                assert not listeners[0].services['studio_backend']._contexts
                assert provider.finished.is_set()
                with pytest.raises(httpx.ConnectError):
                    await client.get(origin)
                with pytest.raises(httpx.ConnectError):
                    await client.get(config['apiOrigin'])
        finally:
            for child in children:
                if child.returncode is None:
                    child.terminate()
                    await child.communicate()
    asyncio.run(run())


def test_disconnect_retains_native_catalog_discovery_until_drained(connection, monkeypatch):
    class CatalogProvider(Provider):
        def describe(self):
            self.entered.set()
            try:
                assert self.release.wait(10), 'Controlled catalog was not released.'
                return (AvailableSelection('sample'),)
            finally:
                self.finished.set()
    async def run():
        provider = CatalogProvider()
        active = []
        original = connection.StudioBackend
        def observe(**kwargs):
            backend = original(**kwargs)
            active.append(backend)
            return backend
        monkeypatch.setattr(connection, 'StudioBackend', observe)
        try:
            async with connection.studio_endpoint(provider, 'sample') as origin:
                async with httpx.AsyncClient(trust_env=False) as client:
                    config = (await client.get(origin + '/config.json')).json()
                    task = asyncio.create_task(client.get(config['apiOrigin'] + '/app/agent/models',
                        headers={'Origin': origin, **config['apiHeaders']}))
                    await wait_event(provider.entered)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    await asyncio.sleep(.1)
                    assert not provider.finished.is_set()
                    assert active[0]._contexts
                    assert active[0].owner._calls
                    provider.release.set()
                    await wait_event(provider.finished)
            assert not active[0]._contexts
            assert active[0].owner._closed
        finally:
            provider.release.set()
            await wait_event(provider.finished)
    asyncio.run(run())


def test_server_forced_shutdown_cancels_and_drains_task(connection, monkeypatch):
    from types import SimpleNamespace
    async def run():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def serve():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(serve())
        await entered.wait()
        wait = connection.asyncio.wait
        calls = 0
        async def expired(tasks, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return set(), set(tasks)
            return await wait(tasks, timeout=timeout)
        monkeypatch.setattr(connection.asyncio, 'wait', expired)
        server = SimpleNamespace(should_exit=False)
        fallback_fired = asyncio.Event()
        async def fallback():
            await asyncio.sleep(.1)
            fallback_fired.set()
            task.cancel()
        watchdog = asyncio.create_task(fallback())
        try:
            await connection._close_server(server, task)
            assert not fallback_fired.is_set()
            assert server.should_exit
            assert task.cancelled()
            assert stopped.is_set()
        finally:
            watchdog.cancel()
            task.cancel()
            await asyncio.gather(task, watchdog, return_exceptions=True)
    asyncio.run(run())


def test_pending_server_shutdown_preserves_retry_and_avoids_second_cancel(connection, monkeypatch):
    from types import SimpleNamespace
    from api_agent.providers.control import CleanupPendingError
    async def run():
        entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def serve():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                stopped.set()
        task = asyncio.create_task(serve())
        await entered.wait()
        async def expired(tasks, timeout):
            await asyncio.sleep(0)
            return ({task}, set()) if task.done() else (set(), {task})
        monkeypatch.setattr(connection.asyncio, 'wait', expired)
        server = SimpleNamespace(should_exit=False)
        try:
            with pytest.raises(CleanupPendingError) as pending:
                await connection._close_server(server, task)
            assert pending.value.owners == (server, task)
            assert task.cancelling() == 1
            with pytest.raises(CleanupPendingError):
                await pending.value.retry()
            assert task.cancelling() == 1
            release.set()
            await pending.value.retry()
            assert stopped.is_set()
            assert task.done()
        finally:
            release.set()
            await task
    asyncio.run(run())


class ImmediateProvider:
    def __init__(self, *, failure=False):
        self.requests=[]
        self.failure=failure
    def describe(self):
        return (AvailableSelection('sample'),)
    def complete(self, request):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError('controlled provider failure')
        return LLMResult(Message('assistant', (TextContent('Ok'),)))


@pytest.mark.parametrize('failure,enabled',[(False,True),(True,True),(False,False)])
def test_prepared_studio_request_uses_one_http_prompt_and_shared_capture(connection,tmp_path,monkeypatch,failure,enabled):
    from api_agent.testing.ui_runtime import UIIntegrationOwner
    from api_agent.testing import browser_attachments
    from api_agent.ui import provider as included
    from api_agent.testing.browser_evidence import PNG_SIGNATURE
    attachments=[]
    monkeypatch.setattr(browser_attachments,'publish_attachment',attachments.append)
    provider=ImmediateProvider(failure=failure)
    backend=connection.ui_provider_backend.__wrapped__(None,provider,'sample')
    owner=UIIntegrationOwner({'config':{
        'UI_LISTENER_URL':'http://127.0.0.1:9443',
        'UI_PROVIDER':str(Path(included.__file__)) + ':create_provider','LOG_CONTENT':enabled},
        'owner_root':str(tmp_path)},None,backend=backend)
    async def exercise():
        prepared=await owner.start()
        original=prepared.session
        try:
            if failure:
                with pytest.raises(AssertionError):await connection.browser_prompt(prepared)
            else:
                assert await connection.browser_prompt(prepared)=='Ok'
            assert prepared.session is original
            assert original.browser.is_connected() and not original.page.is_closed()
            assert len(provider.requests)==1
            request=provider.requests[0]
            assert request.payload.messages[0].content==(TextContent('Answer with exactly: Ok'),)
            await owner.capture('failure' if failure else 'success')
            if enabled:
                assert len(attachments)==1
                title,data,mime=attachments[0]
                assert title==f'UI {"failure" if failure else "success"} context 1 page 1'
                assert mime=='image/png' and data.startswith(PNG_SIGNATURE)
            else:
                assert attachments==[]
        finally:
            await owner.aclose()
        assert not original.browser.is_connected()
        assert owner.backend is None
    asyncio.run(exercise())


def test_studio_request_declares_both_provider_intents(connection):
    assert {mark.name for mark in connection.test_studio_request.pytestmark}=={'requires_ui_session','requires_llm'}


@pytest.mark.parametrize('selected',[True,False])
def test_studio_precondition_checks_frontend_ownership_before_llm_setup(connection,selected):
    from types import SimpleNamespace
    from api_agent.testing.provider_setup import IntegrationRequirements
    from api_agent.ui import provider
    location=str(Path(provider.__file__).resolve())+':create_provider' if selected else str(Path(provider.__file__).resolve())+':other_factory'
    requirements=IntegrationRequirements({'UI_PROVIDER':location},Path.cwd())
    if selected:
        assert connection.studio_configuration.__wrapped__(requirements) is None
    else:
        with pytest.raises(pytest.skip.Exception,match='included Studio'):
            connection.studio_configuration.__wrapped__(requirements)
