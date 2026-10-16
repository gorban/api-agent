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
from api_agent.providers.progress import component_timeout, report_timeout
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.runtime.tasks import offload, settle
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import InspectionOwner, LLMSessionOwner
from api_agent.runtime.managed_browser import managed_browser
from api_agent.ui.provider import app_directory


@asynccontextmanager
async def studio_endpoint(provider, selection):
    """Own real loopback listeners; provider selection/auth belongs to launch.py."""
    import uvicorn
    node = shutil.which('node')
    if node is None:
        pytest.skip('The included Studio browser test requires Node.js.')
    async with AsyncExitStack() as cleanup:
        listener = socket.socket()
        cleanup.callback(listener.close)
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
                cleanup.push_async_callback(_cleanup, _close_host, process)
                raise
            cleanup.push_async_callback(_cleanup, _close_host, process)
            origin = (await process.stdout.readline()).decode().strip()
            assert origin.startswith('http://127.0.0.1:'), 'Studio did not announce a loopback listener.'
        async def describe(*, auth):
            return await offload(provider.describe)
        async def complete(request, *, auth, context):
            return await context.run(offload(provider.complete, request))
        backend = StudioBackend(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
            policy=ModelSelectionPolicy({'LLM_ALLOWED_MODELS': re.escape(selection),
                                         'LLM_DEFAULT_MODEL': re.escape(selection)}),
            origin=origin, nonce=nonce, scrub=lambda _: 'Selected provider rejected the request.')
        cleanup.push_async_callback(_cleanup, backend.aclose)
        runtime = Runtime('demo', services={'studio_backend': backend})
        application = create_application(runtime)
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False, timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        cleanup.push_async_callback(_cleanup, _close_server, server, task)
        async with component_timeout('Studio agent readiness', 10):
            while not server.started:
                assert not task.done(), 'Agent listener stopped before readiness.'
                await asyncio.sleep(.01)
        yield origin


async def _cleanup(action, *args):
    """Each stack callback finishes independently despite caller cancellation."""
    await finish_cleanup(action(*args))


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


async def browser_prompt(provider, selection, *, screenshot=None, inspection=None):
    from playwright.async_api import expect
    async with studio_endpoint(provider, selection) as origin:
        config = getattr(provider, 'config', {})
        browser = managed_browser(inspection=inspection,
            headless=not (config.get('RUN_UI', False) or config.get('DEBUG', False)))
        try:
            async with component_timeout('Studio browser request', 150):
                session = await browser.start()
                page = session.page
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(origin)
                await expect(page.locator('#model option')).to_have_count(2)
                await page.locator('#prompt').fill('Answer with exactly: Ok')
                await page.locator('#send').click()
                status = page.locator('.assistant .metadata-grid dd').last
                await expect(status).to_have_text(re.compile(r'Complete|Incomplete'), timeout=120000)
                assert await status.text_content() == 'Complete', await page.locator('#status').inner_text()
                answer = await page.locator('.assistant .message-body').inner_text()
                assert answer.strip(), 'Agent returned an empty browser response.'
                assert not errors, errors
                assert await page.locator('#stop').is_hidden()
                if screenshot is not None:
                    await page.screenshot(path=str(screenshot), full_page=True)
                    assert screenshot.stat().st_size > 0
                return answer
        finally:
            await browser.aclose()


def _show_studio_browser(session):
    if session.inspection_endpoint:
        print('Studio browser inspection: ' + session.inspection_endpoint, flush=True)


def run_browser_prompt(provider, selection, *, screenshot=None):
    """Keep an allocated DEBUG browser on its creating loop for shared inspection."""
    from api_agent.testing.provider_setup import finish_ui_browser_inspection
    loop = asyncio.new_event_loop()
    inspection = (InspectionOwner(on_session=_show_studio_browser)
                  if getattr(provider, 'config', {}).get('DEBUG', False) else None)
    try:
        return loop.run_until_complete(browser_prompt(provider, selection,
            screenshot=screenshot, inspection=inspection))
    finally:
        finish_ui_browser_inspection(loop, inspection)


@pytest.fixture
def studio_screenshot(integration_config, tmp_path):
    """Browser content follows the same explicit opt-in as recorded evidence."""
    if integration_config.values.get('LOG_CONTENT') is not True:
        return None
    return tmp_path / 'studio-response.png'


def test_studio_request(llm_provider, llm_selection, studio_screenshot):
    screenshot = studio_screenshot
    answer = run_browser_prompt(llm_provider, llm_selection, screenshot=screenshot)
    assert answer.strip().lower().rstrip('.') == 'ok'
