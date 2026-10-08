"""Shared Stealth-enabled Chromium for provider, test and local UI sessions."""
import asyncio
import threading
from pathlib import Path

from api_agent.providers.sessions import BrowserOwner
from api_agent.runtime.browser import ensure_chromium_ready


async def _dispose(awaitable):
    """Finish owned cleanup even when its caller is cancelled."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


class ChromiumDriver:
    """Retain in-flight acquisitions until browser-owner cleanup confirms disposal."""
    def __init__(self, *, inspectable=False):
        self.cancelled = threading.Event()
        self.preparing = self.starting = self.launching = None
        self.handed_browser = False
        self.inspectable = inspectable
        self.inspection_endpoint = None

    async def launch(self, *, headless):
        self.preparing = asyncio.create_task(asyncio.to_thread(
            ensure_chromium_ready, headed=not headless, cancelled=self.cancelled.is_set))
        await asyncio.shield(self.preparing)
        from playwright.async_api import async_playwright
        self.starting = asyncio.create_task(async_playwright().start())
        playwright = await asyncio.shield(self.starting)
        from playwright_stealth import Stealth
        Stealth().hook_playwright_context(playwright)
        options = {'headless': headless}
        if self.inspectable:
            options['args'] = ['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--enable-automation']
        self.launching = asyncio.create_task(playwright.chromium.launch(**options))
        browser = await asyncio.shield(self.launching)
        if self.inspectable:
            session = await browser.new_browser_cdp_session()
            try:
                arguments = (await session.send('Browser.getBrowserCommandLine'))['arguments']
                directory = next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--user-data-dir='))
                port = int((Path(directory) / 'DevToolsActivePort').read_text().splitlines()[0])
                if not 0 < port < 65536:
                    raise ValueError('Invalid inspection port')
                self.inspection_endpoint = f'http://127.0.0.1:{port}'
            finally:
                await _dispose(session.detach())
        self.handed_browser = True
        return browser

    async def _result(self, task):
        if task is None:
            return None
        try:
            return await asyncio.shield(task)
        except Exception:
            # Acquisition failure allocated no returned resource. Close failures
            # below propagate and retain their task/resource for explicit retry.
            return None

    async def _close(self):
        self.cancelled.set()
        await self._result(self.preparing)
        browser = await self._result(self.launching)
        if browser is not None and not self.handed_browser:
            await browser.close()
            self.launching = None
        playwright = await self._result(self.starting)
        if playwright is not None:
            await playwright.stop()
            self.starting = None

    async def stop(self):
        await _dispose(self._close())


def managed_browser(*, inspection=None, headless=False):
    """Create a lazy shared browser owner; Stealth precedes every context/page."""
    async def driver():
        return ChromiumDriver(inspectable=inspection is not None)
    return BrowserOwner(driver, headless=headless, inspection=inspection)
