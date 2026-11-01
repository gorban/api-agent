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


pytestmark = [pytest.mark.feature('Studio chat'), pytest.mark.credential_authorities('ui')]

@asynccontextmanager
async def studio_endpoint(provider, selection):
    """Own real loopback listeners; provider selection/auth belongs to launch.sh."""
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


from api_agent.testing.studio_browser import browser_prompt


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


from api_agent.testing.studio_http import studio_backend_factory


@pytest.mark.requires_ui_session
@pytest.mark.requires_llm
def test_studio_request(ui_provider_session, ui_provider_loop):
    answer = ui_provider_loop.run_until_complete(browser_prompt(ui_provider_session))
    assert answer.strip().lower().rstrip('.') == 'ok'
