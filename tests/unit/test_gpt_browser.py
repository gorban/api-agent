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
    monkeypatch.setenv('TMPDIR', '/var/tmp')
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
def test_canceled_start_retains_late_acquisition_until_disposed(chromium, stage, monkeypatch, tmp_path):
    from pathlib import Path
    if stage == 'launch':
        monkeypatch.setenv('TMPDIR', str(tmp_path / ('long-' + 'x' * 120)))
        chromium.browser.is_connected = Mock(return_value=False)
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
        short = (Path(chromium.playwright.chromium.launch.await_args.kwargs['env']['TMPDIR'])
                 if stage == 'launch' else None)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        try:
            with pytest.raises(CleanupPendingError):
                await owner.aclose()
            assert owner._resources
            assert short is None or short.is_dir()
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
            assert short is None or not short.exists()
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
        import tempfile
        from pathlib import Path
        driver = module.ChromiumDriver()
        driver.process_temp = tempfile.TemporaryDirectory(prefix='browser-test-', dir='/var/tmp')
        owned_path = Path(driver.process_temp.name)
        # A launch that finishes after its awaiter was canceled never hands the
        # browser to BrowserOwner, so the driver must dispose it itself.
        driver.starting = asyncio.create_task(asyncio.sleep(0, result=chromium.playwright))
        driver.launching = asyncio.create_task(asyncio.sleep(0, result=chromium.browser))
        target = chromium.browser.close if stage == 'browser' else chromium.playwright.stop
        target.side_effect = [RuntimeError('disposal unconfirmed'), None]
        with pytest.raises(RuntimeError, match='disposal unconfirmed'):
            await driver.stop()
        assert driver.starting is not None and owned_path.is_dir()
        assert (driver.launching is not None) == (stage == 'browser')
        await driver.stop()
        assert driver.starting is None and driver.launching is None
        assert not owned_path.exists() and driver.process_temp is None
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


@pytest.mark.parametrize(('report', 'pid'), [
    ({'processInfo': [{'type': 'browser', 'id': 321}, {'type': 'gpu', 'id': 322}]}, 321),
    ({'processInfo': []}, None),
    ({'processInfo': [{'type': 'browser', 'id': 0}]}, None),
    ({'processInfo': [{'type': 'browser', 'id': True}]}, None),
    ({'processInfo': [{'type': 'browser', 'id': 321}, {'type': 'browser', 'id': 322}]}, None),
    ({'missing': True}, None),
])
def test_inspection_browser_pid_requires_unique_positive_process_report(chromium, tmp_path, report, pid):
    (tmp_path / 'DevToolsActivePort').write_text('32123\n/browser/path\n')

    async def send(command):
        if command == 'Browser.getBrowserCommandLine':
            return {'arguments': ['--user-data-dir=' + str(tmp_path)]}
        assert command == 'SystemInfo.getProcessInfo'
        return report

    cdp = SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    chromium.browser.new_browser_cdp_session = AsyncMock(return_value=cdp)

    async def scenario():
        driver = module.ChromiumDriver(inspectable=True)
        assert await driver.launch(headless=False) is chromium.browser
        assert driver.browser_pid == pid
        await driver.stop()
        cdp.detach.assert_awaited_once()

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


def test_terminated_inspection_requires_confirmed_browser_pid_exit(monkeypatch):
    async def scenario():
        browser = SimpleNamespace(is_connected=lambda: False)
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        transport = SimpleNamespace(_proc=process, on_error_future=asyncio.get_running_loop().create_future())
        transport.on_error_future.set_result(None)
        playwright = SimpleNamespace(_impl_obj=SimpleNamespace(
            _connection=SimpleNamespace(_transport=transport)))
        driver = module.ChromiumDriver(inspectable=True)
        driver.handed_browser = True
        driver.inspection_endpoint = 'http://127.0.0.1:32123'
        driver.starting = asyncio.create_task(asyncio.sleep(0, result=playwright))
        driver.launching = asyncio.create_task(asyncio.sleep(0, result=browser))
        await asyncio.gather(driver.starting, driver.launching)
        session = SimpleNamespace(browser=browser, inspection_endpoint=driver.inspection_endpoint)
        monkeypatch.setattr(module.asyncio, 'open_connection', AsyncMock(side_effect=ConnectionRefusedError))

        assert not await driver.terminated_inspection(session)
        process.wait.assert_not_awaited()
        driver.browser_pid = 12345
        monkeypatch.setattr(module.os, 'kill', lambda pid, signal: None)
        assert not await driver.terminated_inspection(session)
        process.wait.assert_not_awaited()

        def absent(pid, signal):
            assert (pid, signal) == (12345, 0)
            raise ProcessLookupError
        monkeypatch.setattr(module.os, 'kill', absent)

        original_launching = driver.launching
        driver.launching = asyncio.create_task(asyncio.sleep(0, result=object()))
        await driver.launching
        assert not await driver.terminated_inspection(session)
        driver.launching = original_launching

        transport._proc = None
        assert not await driver.terminated_inspection(session)
        transport._proc = process
        driver.inspection_endpoint = session.inspection_endpoint = 'http://localhost:32123'
        assert not await driver.terminated_inspection(session)
        driver.inspection_endpoint = session.inspection_endpoint = 'http://127.0.0.1:32123'

        class Writer:
            closed = False
            def close(self):
                self.closed = True
            async def wait_closed(self):
                await asyncio.sleep(1)

        writer = Writer()
        monkeypatch.setattr(module.asyncio, 'open_connection', AsyncMock(return_value=(object(), writer)))
        assert not await driver.terminated_inspection(session)
        assert writer.closed
        monkeypatch.setattr(module.asyncio, 'open_connection', AsyncMock(
            return_value=(object(), SimpleNamespace(close=Mock(), wait_closed=AsyncMock()))))
        assert not await driver.terminated_inspection(session)

        monkeypatch.setattr(module.asyncio, 'open_connection', AsyncMock(side_effect=ConnectionRefusedError))
        owned_temp = SimpleNamespace(cleanup=Mock(side_effect=[OSError('cleanup pending'), None]))
        driver.process_temp = owned_temp
        assert not await driver.terminated_inspection(session)
        assert driver.process_temp is owned_temp
        process.wait.reset_mock()
        module.asyncio.open_connection.reset_mock()
        assert await driver.terminated_inspection(session)
        assert driver.process_temp is None and owned_temp.cleanup.call_count == 2
        process.wait.assert_awaited_once()
        module.asyncio.open_connection.assert_awaited_once_with('127.0.0.1', 32123)
        transport.on_error_future = asyncio.get_running_loop().create_future()
        transport.on_error_future.cancel()
        assert await driver.terminated_inspection(session)

    asyncio.run(scenario())


def test_long_browser_process_temp_is_owned_until_browser_disposal(chromium, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    bulk = tmp_path / ('bulk-' + 'x' * 120)
    bulk.mkdir()
    monkeypatch.setenv('TMPDIR', str(bulk))
    chromium.browser.is_connected = Mock(side_effect=[True, False])
    async def scenario():
        driver = module.ChromiumDriver()
        await driver.launch(headless=False)
        child_env = chromium.playwright.chromium.launch.await_args.kwargs['env']
        short = Path(child_env['TMPDIR'])
        assert short != bulk and short.stat().st_mode & 0o777 == 0o700
        assert os.environ['TMPDIR'] == str(bulk)
        with pytest.raises(RuntimeError, match='disposal is not confirmed'):
            await driver.stop()
        assert short.is_dir() and not chromium.playwright.stop.await_count
        await driver.stop()
        assert not short.exists() and driver.process_temp is None
    asyncio.run(scenario())
