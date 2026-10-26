"""Controlled real HTTP fixture regressions; no browser or external provider."""
import asyncio
from api_agent.testing import studio_http
import importlib.util
from pathlib import Path
import threading
from uuid import uuid4

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


def pending_contexts(backend):
    """Observe discovery and transferred inference ownership without conflating them."""
    return (*backend._contexts.values(), *(turn.context for turn in backend.lifecycle._turns.values()
        if not turn.done.is_set()))


async def send(client, config, origin):
    return await client.post(config['apiOrigin'] + '/app/agent/chat',
        headers={'Origin': origin, **config['apiHeaders']}, json={
            'model': 'sample', 'session_id': str(uuid4()),
            'messages': [{'role': 'user', 'content': 'synthetic'}]})


def test_disconnect_retains_native_completion_until_drained(connection, monkeypatch):
    async def run():
        provider = Provider()
        active = []
        original = studio_http.StudioBackend
        def observe(**kwargs):
            backend = original(**kwargs)
            active.append(backend)
            return backend
        monkeypatch.setattr(studio_http, 'StudioBackend', observe)
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
                    assert pending_contexts(active[0])
                    context = pending_contexts(active[0])[0]
                    assert context.retained_tasks
                    provider.release.set()
                    await wait_event(provider.finished)
            assert not pending_contexts(active[0])
            assert active[0].owner._closed
            assert provider.finished.is_set()
        finally:
            provider.release.set()
            await wait_event(provider.finished)
    asyncio.run(run())


@pytest.mark.parametrize('host_failure', [False, True])
def test_server_cleanup_failure_retains_node_and_socket_until_retry(connection, monkeypatch, host_failure):
    from api_agent.providers.control import CleanupPendingError
    async def run():
        provider = Provider()
        provider.release.set()
        children = []
        listeners = []
        spawn = connection.asyncio.create_subprocess_exec
        create = studio_http.create_application
        async def record_spawn(*args, **kwargs):
            child = await spawn(*args, **kwargs)
            children.append(child)
            return child
        failed = False
        async def fail_server(server, task):
            nonlocal failed
            # A failed parent acknowledgement must keep every later duty.
            await original_close(server, task)
            if not failed:
                failed = True
                raise RuntimeError('controlled server failure')
        def record_application(runtime):
            listeners.append(runtime)
            return create(runtime)
        original_close = connection._close_server
        original_host_close = connection._close_host
        host_failed = False
        async def close_host(process):
            nonlocal host_failed
            if host_failure and not host_failed:
                host_failed = True
                raise CleanupPendingError((process,), lambda: original_host_close(process))
            await original_host_close(process)
        monkeypatch.setattr(connection, '_close_host', close_host)
        monkeypatch.setattr(connection.asyncio, 'create_subprocess_exec', record_spawn)
        monkeypatch.setattr(studio_http, '_close_server', fail_server)
        monkeypatch.setattr(studio_http, 'create_application', record_application)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(CleanupPendingError) as pending:
                    async with connection.studio_endpoint(provider, 'sample') as origin:
                        config = (await client.get(origin + '/config.json')).json()
                        assert (await send(client, config, origin)).status_code == 200
                assert children[0].returncode is None
                if host_failure:
                    with pytest.raises(CleanupPendingError) as host_pending:
                        await pending.value.retry()
                    assert children[0].returncode is None
                    await host_pending.value.retry()
                else:
                    await pending.value.retry()
                assert children[0].returncode is not None
                assert listeners[0]._closed
                assert not pending_contexts(listeners[0].services['studio_backend'])
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
        original = studio_http.StudioBackend
        def observe(**kwargs):
            backend = original(**kwargs)
            active.append(backend)
            return backend
        monkeypatch.setattr(studio_http, 'StudioBackend', observe)
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
                    assert pending_contexts(active[0])
                    assert active[0].owner._calls
                    provider.release.set()
                    await wait_event(provider.finished)
            assert not pending_contexts(active[0])
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
    monkeypatch.setattr(browser_attachments,'publish_attachment',lambda attachment, **kwargs: attachments.append(attachment))
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


@pytest.mark.parametrize('failure_kind', ['none', 'cancel', 'pending', 'error'])
def test_cleanup_preserves_child_receipt_when_its_caller_is_cancelled(connection, failure_kind):
    from api_agent.providers.control import CleanupPendingError
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        settled = []
        async def retry_child(): settled.append('retried')
        receipt = CleanupPendingError((settled,), retry_child)
        child_error = asyncio.CancelledError()
        child_error.cleanup_pending = receipt
        first = True
        async def action():
            nonlocal first
            entered.set()
            await release.wait()
            if first:
                first = False
                if failure_kind == 'cancel': raise child_error
                if failure_kind == 'pending': raise receipt
                if failure_kind == 'error': raise RuntimeError('controlled cleanup failure')
            settled.append('closed')
        task = asyncio.create_task(connection._cleanup(action))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as stopped:
            await task
        if failure_kind == 'none':
            assert settled == ['closed']
            assert not hasattr(stopped.value, 'cleanup_pending')
        else:
            pending = stopped.value.cleanup_pending
            assert isinstance(pending, CleanupPendingError)
            if failure_kind == 'cancel': assert stopped.value is child_error
            await pending.retry()
            assert settled == (['closed'] if failure_kind == 'error' else ['retried'])
    asyncio.run(run())


def test_turn_capture_precedes_next_prompt(connection,tmp_path,monkeypatch):
    from api_agent.testing.ui_runtime import UIIntegrationOwner
    from api_agent.testing import browser_attachments
    from api_agent.ui import provider as included
    attachments=[]
    events=tmp_path/'browser-events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    real_publish=browser_attachments.publish_attachment
    provider=ImmediateProvider()
    def publish(attachment, *, owner=None):
        assert owner is not None
        assert len(provider.requests)==len(attachments)+1
        real_publish(attachment,owner=owner)
        attachments.append((attachment,owner))
    monkeypatch.setattr(browser_attachments,'publish_attachment',publish)
    backend=connection.ui_provider_backend.__wrapped__(None,provider,'sample')
    owner=UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://127.0.0.1:9443',
        'UI_PROVIDER':str(Path(included.__file__))+':create_provider','LOG_CONTENT':True},
        'owner_root':str(tmp_path)},None,backend=backend)
    async def exercise():
        prepared=await owner.start()
        try:
            assert await connection.browser_prompt(prepared)=='Ok'
            assert len(attachments)==1
            assert await connection.browser_prompt(prepared)=='Ok'
            assert len(attachments)==2
            first,second=[binding for _,binding in attachments]
            assert first['conversation_id']==second['conversation_id']
            assert first['turn_id']!=second['turn_id']
            await prepared.session.page.locator('#new-chat').click()
            assert await connection.browser_prompt(prepared)=='Ok'
            assert len(attachments)==3
            third=attachments[2][1]
            assert third['conversation_id']!=second['conversation_id']
            assert third['turn_id'] not in (first['turn_id'],second['turn_id'])
            import json
            packets=[json.loads(line) for line in events.read_text().splitlines()]
            assert len(packets)==3
            for packet,(original,binding) in zip(packets,attachments):
                descriptor=packet['data']
                assert descriptor['owner']==binding
                assert browser_attachments.read_attachment(descriptor,tmp_path)==original
                assert original[2]=='image/png'
        finally:
            await owner.aclose()
    asyncio.run(exercise())
