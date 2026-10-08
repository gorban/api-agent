"""Shared managed Chromium with fake drivers and no external requests."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.runtime import managed_browser as module
from api_agent.providers.control import CleanupPendingError


@pytest.fixture
def chromium(monkeypatch):
    page = SimpleNamespace()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
    ready = []
    stealth = SimpleNamespace(hook_playwright_context=Mock())
    monkeypatch.setattr('playwright_stealth.Stealth', lambda: stealth)
    monkeypatch.setattr(module, 'ensure_chromium_ready', lambda **kwargs: ready.append(kwargs))
    monkeypatch.setattr('playwright.async_api.async_playwright', lambda: manager)
    return SimpleNamespace(page=page, context=context, browser=browser, playwright=playwright,
                           manager=manager, ready=ready, stealth=stealth)


@pytest.mark.parametrize('headless', [False, True])
def test_owner_is_lazy_visible_and_closes_in_order(chromium, headless):
    async def scenario():
        owner = module.managed_browser(headless=headless)
        assert not chromium.ready and not chromium.manager.start.called
        session = await owner.start()
        assert session.page is chromium.page
        chromium.stealth.hook_playwright_context.assert_called_once_with(chromium.playwright)
        assert chromium.ready[0]['headed'] is not headless
        assert not chromium.ready[0]['cancelled']()
        chromium.playwright.chromium.launch.assert_awaited_once_with(headless=headless)
        await owner.aclose()
        await owner.aclose()
        chromium.context.close.assert_awaited_once()
        chromium.browser.close.assert_awaited_once()
        chromium.playwright.stop.assert_awaited_once()
    asyncio.run(scenario())


def test_stealth_precedes_launch_and_all_contexts_share_the_instrumented_browser(chromium):
    order = []
    chromium.stealth.hook_playwright_context.side_effect = lambda p: order.append('stealth')
    async def launch(**kwargs):
        order.append('launch')
        return chromium.browser
    async def context():
        order.append('context')
        return chromium.context
    async def page():
        order.append('page')
        return chromium.page
    chromium.playwright.chromium.launch.side_effect = launch
    chromium.browser.new_context.side_effect = context
    chromium.context.new_page.side_effect = page
    async def scenario():
        owner = module.managed_browser(headless=True)
        first = await owner.start()
        assert await owner.start() is first
        assert order == ['stealth', 'launch', 'context', 'page']
        assert first.browser is chromium.browser
        chromium.stealth.hook_playwright_context.assert_called_once_with(chromium.playwright)
        await owner.aclose()
    asyncio.run(scenario())


def test_gpt_uses_the_same_shared_browser_factory():
    from api_agent.llm_providers.gpt.browser import subscription_browser
    assert subscription_browser is module.managed_browser


@pytest.mark.parametrize('stage', ['prepare', 'driver', 'stealth', 'launch'])
def test_failed_acquisition_disposes_previous_resources(chromium, monkeypatch, stage):
    if stage == 'prepare':
        def fail(**kwargs):
            raise RuntimeError('private preparation details')
        monkeypatch.setattr(module, 'ensure_chromium_ready', fail)
    elif stage == 'driver':
        chromium.manager.start.side_effect = RuntimeError('private driver details')
    elif stage == 'stealth':
        chromium.stealth.hook_playwright_context.side_effect = RuntimeError('private stealth details')
    else:
        chromium.playwright.chromium.launch.side_effect = RuntimeError('private launch details')
    async def scenario():
        owner = module.managed_browser()
        with pytest.raises(Exception, match='Browser setup failed'):
            await owner.start()
        await owner.aclose()
        assert not owner._resources
        assert chromium.playwright.stop.await_count == int(stage in ('stealth', 'launch'))
        chromium.browser.close.assert_not_awaited()
        if stage != 'launch':
            chromium.playwright.chromium.launch.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['driver', 'launch'])
def test_canceled_start_retains_late_acquisition_until_disposed(chromium, stage):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        value = chromium.playwright if stage == 'driver' else chromium.browser
        async def late(*args, **kwargs):
            entered.set()
            await release.wait()
            return value
        target = chromium.manager.start if stage == 'driver' else chromium.playwright.chromium.launch
        target.side_effect = late
        owner = module.managed_browser()
        owner._timeout = .01
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        try:
            with pytest.raises(CleanupPendingError):
                await owner.aclose()
            assert owner._resources
            chromium.playwright.stop.assert_not_awaited()
            release.set()
            # A previously canceled cleanup wrapper may require a retry after
            # its confirmed late-resource disposal; retain the owner throughout.
            for _ in range(3):
                try:
                    await owner.aclose()
                    break
                except CleanupPendingError:
                    await asyncio.sleep(.01)
            assert not owner._resources
            chromium.playwright.stop.assert_awaited_once()
            assert chromium.browser.close.await_count == int(stage == 'launch')
            if stage == 'driver':
                chromium.playwright.chromium.launch.assert_not_awaited()
        finally:
            release.set()
            await owner.aclose()
    asyncio.run(scenario())


def test_close_failure_keeps_browser_for_retry(chromium):
    chromium.browser.close.side_effect = [RuntimeError('not disposed'), None]
    async def scenario():
        owner = module.managed_browser()
        await owner.start()
        with pytest.raises(CleanupPendingError):
            await owner.aclose()
        assert owner._resources and not chromium.playwright.stop.called
        await owner.aclose()
        assert not owner._resources
        assert chromium.browser.close.await_count == 2
        chromium.playwright.stop.assert_awaited_once()
    asyncio.run(scenario())


def test_preparation_cancellation_is_signaled_and_drained(chromium, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def prepare(**kwargs):
        observed.append(kwargs)
        entered.set()
        release.wait(2)
        finished.set()
    monkeypatch.setattr(module, 'ensure_chromium_ready', prepare)
    async def scenario():
        owner = module.managed_browser()
        owner._timeout = .01
        starting = asyncio.create_task(owner.start())
        assert await asyncio.to_thread(entered.wait, 2)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        try:
            with pytest.raises(CleanupPendingError):
                await owner.aclose()
            assert observed[0]['cancelled']()
            assert not finished.is_set() and owner._resources
            release.set()
            for _ in range(3):
                try:
                    await owner.aclose()
                    break
                except CleanupPendingError:
                    await asyncio.sleep(.01)
            assert finished.is_set() and not owner._resources
            chromium.manager.start.assert_not_awaited()
        finally:
            release.set()
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['browser', 'driver'])
def test_driver_retains_late_resources_after_close_failure(chromium, stage):
    async def scenario():
        driver = module.ChromiumDriver()
        # A launch that finishes after its awaiter was canceled never hands the
        # browser to BrowserOwner, so the driver must dispose it itself.
        driver.starting = asyncio.create_task(asyncio.sleep(0, result=chromium.playwright))
        driver.launching = asyncio.create_task(asyncio.sleep(0, result=chromium.browser))
        target = chromium.browser.close if stage == 'browser' else chromium.playwright.stop
        target.side_effect = [RuntimeError('disposal unconfirmed'), None]
        with pytest.raises(RuntimeError, match='disposal unconfirmed'):
            await driver.stop()
        assert driver.starting is not None
        assert (driver.launching is not None) == (stage == 'browser')
        await driver.stop()
        assert driver.starting is None and driver.launching is None
        assert target.await_count == 2
        assert chromium.browser.close.await_count == (2 if stage == 'browser' else 1)
        assert chromium.playwright.stop.await_count == (2 if stage == 'driver' else 1)
    asyncio.run(scenario())


@pytest.mark.parametrize('port', ['32123', '0', '65536', 'not-a-port'])
def test_debug_endpoint_is_loopback_and_cdp_session_is_disposed(chromium, tmp_path, port):
    (tmp_path / 'DevToolsActivePort').write_text(port + '\n/browser/path\n')
    cdp = SimpleNamespace(send=AsyncMock(return_value={'arguments': ['chromium', '--user-data-dir=' + str(tmp_path)]}), detach=AsyncMock())
    chromium.browser.new_browser_cdp_session = AsyncMock(return_value=cdp)
    async def scenario():
        driver = module.ChromiumDriver(inspectable=True)
        if port == '32123':
            assert await driver.launch(headless=False) is chromium.browser
            assert driver.inspection_endpoint == 'http://127.0.0.1:32123'
        else:
            with pytest.raises(ValueError):
                await driver.launch(headless=False)
            assert driver.inspection_endpoint is None
        chromium.playwright.chromium.launch.assert_awaited_once_with(headless=False,
            args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--enable-automation'])
        cdp.detach.assert_awaited_once()
        await driver.stop()
        assert chromium.browser.close.await_count == int(port != '32123')
    asyncio.run(scenario())


def test_debug_browser_hands_resources_to_common_inspector(chromium, monkeypatch):
    from api_agent.providers.sessions import InspectionOwner
    monkeypatch.setattr(module.ChromiumDriver, 'launch', AsyncMock(return_value=chromium.browser))
    async def scenario():
        inspection = InspectionOwner()
        owner = module.managed_browser(inspection=inspection)
        session = await owner.start()
        assert inspection.sessions == [session]
        await owner.aclose()
        chromium.browser.close.assert_not_awaited()
        await inspection.aclose()
        chromium.browser.close.assert_awaited_once()
        assert not inspection.sessions
    asyncio.run(scenario())
