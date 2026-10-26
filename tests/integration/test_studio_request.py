"""One browser prompt through the included host and actual agent HTTP contracts."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import os
import secrets
import re
import shutil
import socket

import pytest

from api_agent.providers.progress import component_timeout
from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.tasks import settle
from api_agent.ui.provider import app_directory
from api_agent.testing.studio_http import agent_endpoint, _cleanup, _close_socket, _close_server
from api_agent.testing.studio_retained import StudioCleanup
from api_agent.testing.ui_turn import ui_turn
from urllib.parse import urlsplit


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


async def browser_prompt(prepared, *, prompt="Answer with exactly: Ok"):
    """Consume the selected provider's original prepared page for one request."""
    from playwright.async_api import expect
    async with ui_turn(prepared) as turn:
        page = prepared.session.page
        errors = []
        listener = lambda error: errors.append(str(error))
        page.on('pageerror', listener)
        try:
            async with component_timeout('Studio browser request', 150):
                try:
                    # Existing config and catalog fetches each allow five seconds.
                    await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled', timeout=15000)
                except AssertionError as error:
                    diagnostic = await page.locator('#model-status').inner_text()
                    raise AssertionError('Studio catalog did not settle: ' + diagnostic) from error
                try:
                    await expect(page.locator('#model')).to_have_value(re.compile(r'.+'))
                except AssertionError as error:
                    diagnostic = await page.locator('#model-status').inner_text()
                    raise AssertionError('Studio model choices are unavailable: ' + diagnostic) from error
                diagnostic = await page.locator('#model-status').inner_text()
                assert not diagnostic, 'Studio model selection is unavailable: ' + diagnostic
                await expect(page.locator('#send')).to_be_enabled()
                await page.locator('#prompt').fill(prompt)
                async with page.expect_request(lambda request: request.method == 'POST' and
                        urlsplit(request.url).path in ('/app/agent/chat', '/app/agent/chat/stream')) as submitted:
                    await page.locator('#send').click()
                request = await submitted.value
                payload = request.post_data_json
                turn.bind(conversation_id=payload['conversation_id'], turn_id=payload['session_id'])
                status = page.locator('.assistant .metadata-grid dd').last
                await expect(status).to_have_text(re.compile(r'Complete|Incomplete'), timeout=120000)
                assert await status.text_content() == 'Complete', await page.locator('#status').inner_text()
                answer = await page.locator('.assistant .message-body').last.inner_text()
                assert answer.strip(), 'Agent returned an empty browser response.'
                assert not errors, errors
                assert await page.locator('#stop').is_hidden()
                return answer
        finally:
            page.remove_listener('pageerror', listener)


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
    return studio_backend_factory(llm_provider, llm_selection)


def studio_backend_factory(llm_provider, llm_selection, *, backend_config=None):
    """Keep one backend owner, with optional test-local defaults."""
    retained = []
    @asynccontextmanager
    async def backend(values):
        values = {**values, **(backend_config or {})}
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
