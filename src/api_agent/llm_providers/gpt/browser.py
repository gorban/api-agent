"""Owned Chromium for attended subscription consent and loopback callbacks."""
import asyncio
import threading

from api_agent.providers.sessions import BrowserOwner
from api_agent.runtime.browser import ensure_chromium_ready
from .connect import _dispose


class _Driver:
    """Retain in-flight acquisitions until browser-owner cleanup confirms disposal."""
    def __init__(self):
        self.cancelled = threading.Event()
        self.preparing = self.starting = self.launching = None
        self.handed_browser = False

    async def launch(self, *, headless):
        self.preparing = asyncio.create_task(asyncio.to_thread(
            ensure_chromium_ready, headed=not headless, cancelled=self.cancelled.is_set))
        await asyncio.shield(self.preparing)
        from playwright.async_api import async_playwright
        self.starting = asyncio.create_task(async_playwright().start())
        playwright = await asyncio.shield(self.starting)
        self.launching = asyncio.create_task(playwright.chromium.launch(headless=headless))
        browser = await asyncio.shield(self.launching)
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


def subscription_browser():
    """Attended consent is always visible, independently of app UI visibility."""
    async def driver():
        return _Driver()
    return BrowserOwner(driver, headless=False)
