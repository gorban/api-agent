"""One browser prompt through the included host and actual agent HTTP contracts."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import os
import secrets
import re
import shutil
import socket

import pytest

from api_agent.api import Runtime, create_application
from api_agent.api.studio import StudioBackend
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.runtime.tasks import offload, settle
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.ui.provider import app_directory
from api_agent.testing.studio_retained import StudioCleanup


pytestmark = pytest.mark.feature('Studio chat')

@asynccontextmanager
async def studio_endpoint(provider, selection):
    """Own real loopback listeners; provider selection/auth belongs to launch.py."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('The included Studio browser test requires Node.js.')
    async with AsyncExitStack() as cleanup:
        duties = StudioCleanup()
        cleanup.push_async_callback(_cleanup, duties.aclose)
        listener = socket.socket()
        duties.push(_close_socket, listener)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.setblocking(False)
        endpoint = f'http://127.0.0.1:{listener.getsockname()[1]}'
        nonce = secrets.token_urlsafe(32)
        app = app_directory()
        script = """
          const {createStudioServer} = await import(process.env.STUDIO_HOST_MODULE);
          const {server,stop} = createStudioServer();
          process.once('SIGTERM',stop); process.once('SIGINT',stop);
          server.listen(0,'127.0.0.1',()=>console.log('http://127.0.0.1:'+server.address().port));
        """
        async with component_timeout('Studio host startup', 10):
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
                cwd=app, env={'PATH': os.defpath, 'STUDIO_HOST_MODULE': (app / 'server.mjs').as_uri(),
                    'UI_LISTENER_URL': 'http://127.0.0.1:9443', 'AGENT_DJANGO_URL': endpoint,
                    'API_AGENT_UI_LAUNCH_TOKEN': secrets.token_urlsafe(24),
                    'API_AGENT_UI_ACCESS_TOKEN': nonce}, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await settle(spawning)
                duties.push(_close_host, process)
                raise
            duties.push(_close_host, process)
            origin = (await process.stdout.readline()).decode().strip()
            assert origin.startswith('http://127.0.0.1:'), 'Studio did not announce a loopback listener.'
        bridge = agent_endpoint(provider, selection, origin, nonce, listener=listener)
        await bridge.__aenter__()
        duties.push(bridge.__aexit__, None, None, None)
        yield origin


@asynccontextmanager
async def agent_endpoint(provider, selection, origin, nonce, *, listener=None, reasoning_default=None, policy_config=None):
    """Own the real Agent HTTP bridge for the selected UI runtime."""
    import uvicorn
    async with AsyncExitStack() as cleanup:
        duties = StudioCleanup()
        cleanup.push_async_callback(_cleanup, duties.aclose)
        if listener is None:
            listener = socket.socket()
            duties.push(_close_socket, listener)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.setblocking(False)
        endpoint = f'http://127.0.0.1:{listener.getsockname()[1]}'
        from api_agent.testing.studio_retained import RetainedStudio
        retained = isinstance(provider, RetainedStudio)
        if retained:
            duties.push(provider.aclose)
        async def describe(*, auth):
            return await provider.describe() if retained else await offload(provider.describe)
        async def complete(request, *, auth, context):
            call = provider.complete(request) if retained else offload(provider.complete, request)
            return await context.run(call)
        backend = StudioBackend(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
            policy=ModelSelectionPolicy({'LLM_ALLOWED_MODELS': re.escape(selection),
                                         'LLM_DEFAULT_MODEL': re.escape(selection), **(policy_config or {})}),
            origin=origin, nonce=nonce, scrub=lambda _: 'Selected provider rejected the request.',
            reasoning_default=reasoning_default)
        duties.push(backend.aclose)
        runtime = Runtime('demo', services={'studio_backend': backend})
        application = create_application(runtime)
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False, timeout_graceful_shutdown=1))
        # A long-lived listener must not inherit completed UI startup budgets.
        # Each admitted HTTP request still creates its own bounded context.
        with independent_timeout_scope():
            task = asyncio.create_task(server.serve(sockets=[listener]))
        duties.push(_close_server, server, task)
        async with component_timeout('Studio agent readiness', 10):
            while not server.started:
                assert not task.done(), 'Agent listener stopped before readiness.'
                await asyncio.sleep(.01)
        yield endpoint


async def _close_socket(listener):
    listener.close()


async def _cleanup(action, *args):
    """Settle owned work without losing its original interruption receipt."""
    failures = []
    async def dispose():
        try:
            await action(*args)
        except BaseException as error:
            # Return failures as data through finish_cleanup: a task ending in
            # CancelledError otherwise loses the error's ownership attributes.
            failures.append(error)
    try:
        await finish_cleanup(dispose())
    except asyncio.CancelledError as cancelled:
        if failures:
            failure = failures[0]
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            cancelled.cleanup_pending = (failure if isinstance(failure, CleanupPendingError)
                else CleanupPendingError((action,), lambda: _cleanup(action, *args)))
        raise
    if failures:
        raise failures[0]


async def _close_server(server, task):
    async with report_timeout('Studio agent cleanup', 3) as budget:
        server.should_exit = True
        done, _ = await asyncio.wait({task}, timeout=budget.remaining)
        if not done:
            if not task.cancelling():
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            if not done:
                raise CleanupPendingError((server, task), lambda: _close_server(server, task))
        try:
            await settle(task)
        except asyncio.CancelledError:
            pass


async def _close_host(process):
    try:
        async with component_timeout('Studio host cleanup', 5):
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 1)
            except TimeoutError:
                process.kill()
                await process.communicate()
    except Exception:
        raise CleanupPendingError((process,), lambda: _close_host(process)) from None


async def browser_prompt(prepared):
    """Consume the selected provider's original prepared page for one request."""
    from playwright.async_api import expect
    page = prepared.session.page
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    async with component_timeout('Studio browser request', 150):
        await expect(page.locator('#model')).to_have_value(re.compile(r'.+'))
        await page.locator('#prompt').fill('Answer with exactly: Ok')
        await page.locator('#send').click()
        status = page.locator('.assistant .metadata-grid dd').last
        await expect(status).to_have_text(re.compile(r'Complete|Incomplete'), timeout=120000)
        assert await status.text_content() == 'Complete', await page.locator('#status').inner_text()
        answer = await page.locator('.assistant .message-body').inner_text()
        assert answer.strip(), 'Agent returned an empty browser response.'
        assert not errors, errors
        assert await page.locator('#stop').is_hidden()
        return answer


@pytest.fixture
def studio_configuration(integration_config):
    """Studio selectors belong to the included frontend, before paid setup."""
    from pathlib import Path
    from api_agent.providers.loading import provider_location
    from api_agent.ui import provider
    selected, factory = provider_location(integration_config.values['UI_PROVIDER'],
                                          owner_root=integration_config.owner_root)
    integration_config.require_combination(
        selected.resolve() == Path(provider.__file__).resolve() and factory == 'create_provider',
        'This request test requires the included Studio UI provider.')


@pytest.fixture
def ui_provider_backend(studio_configuration, llm_provider, llm_selection):
    """Bind the chosen provider to the real HTTP contract before UI launch."""
    retained = []
    @asynccontextmanager
    async def backend(values):
        nonce = secrets.token_urlsafe(32)
        provider = llm_provider
        if values.get('DEBUG') is True:
            from api_agent.testing.provider_setup import _read_snapshot, _worker_manager_registry
            from api_agent.testing.studio_retained import RetainedStudio, retained_provider_factory
            from api_agent.testing.inspection_gate import manual_accounting_ready
            data = _read_snapshot()
            provider = RetainedStudio(llm_provider,
                retained_provider_factory(data, _worker_manager_registry(data)), manual_accounting_ready)
            retained.append(provider)
        async with agent_endpoint(provider, llm_selection,
                                  values['UI_LISTENER_URL'], nonce,
                                  reasoning_default=values.get('LLM_REASONING_EFFORT') or None,
                                  policy_config={key: values[key] for key in
                                    ('LLM_ALLOWED_MODELS', 'LLM_DEFAULT_MODEL') if key in values}) as endpoint:
            try:
                yield {'agent_url': endpoint, 'access_token': nonce}
            finally:
                if provider is not llm_provider:
                    provider.retire_test()
    def retire_test():
        for provider in retained:
            provider.retire_test()
    backend.retire_test = retire_test
    return backend


@pytest.mark.requires_ui_session
@pytest.mark.requires_llm
def test_studio_request(ui_provider_session, ui_provider_loop):
    answer = ui_provider_loop.run_until_complete(browser_prompt(ui_provider_session))
    assert answer.strip().lower().rstrip('.') == 'ok'
