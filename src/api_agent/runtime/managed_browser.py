"""Shared Stealth-enabled Chromium for provider, test and local UI sessions."""
import asyncio
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from api_agent.providers.sessions import BrowserOwner
from api_agent.providers.control import finish_cleanup as _dispose
from api_agent.runtime.browser import ensure_chromium_ready


class ChromiumDriver:
    """Retain in-flight acquisitions until browser-owner cleanup confirms disposal."""
    def __init__(self, *, inspectable=False):
        self.cancelled = threading.Event()
        self.preparing = self.starting = self.launching = None
        self.handed_browser = False
        self.inspectable = inspectable
        self.inspection_endpoint = None
        self.browser_pid = None

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
                try:
                    processes = (await session.send('SystemInfo.getProcessInfo'))['processInfo']
                    browser_pids = [item['id'] for item in processes if item.get('type') == 'browser']
                    if len(browser_pids) == 1 and type(browser_pids[0]) is int and browser_pids[0] > 0:
                        self.browser_pid = browser_pids[0]
                except Exception:
                    # An unavailable process report never qualifies a later
                    # terminal-settlement decision.
                    self.browser_pid = None
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

    async def terminated_inspection(self, session):
        """Confirm that an interrupted inspectable browser has no live transport."""
        if (not self.inspectable or not self.handed_browser
                or self.starting is None or not self.starting.done()
                or self.launching is None or not self.launching.done()
                or self.inspection_endpoint != session.inspection_endpoint
                or not self.inspection_endpoint or self.browser_pid is None
                or session.browser.is_connected()):
            return False
        try:
            try:
                os.kill(self.browser_pid, 0)
            except ProcessLookupError:
                pass
            else:
                return False
            if self.launching.result() is not session.browser:
                return False
            playwright = self.starting.result()
            transport = playwright._impl_obj._connection._transport
            process = transport._proc
            if process is None:
                return False
            await asyncio.wait_for(process.wait(), timeout=.25)
            endpoint = urlsplit(self.inspection_endpoint)
            if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or not endpoint.port:
                return False
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', endpoint.port), timeout=.25)
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=.25)
                return False
            except ConnectionRefusedError:
                failure = transport.on_error_future
                if failure.done() and not failure.cancelled():
                    failure.exception()
                return True
        except (AttributeError, ValueError, asyncio.TimeoutError, OSError):
            return False
        return False


def managed_browser(*, inspection=None, headless=False):
    """Create a lazy shared browser owner; Stealth precedes every context/page."""
    async def driver():
        return ChromiumDriver(inspectable=inspection is not None)
    return BrowserOwner(driver, headless=headless, inspection=inspection)
