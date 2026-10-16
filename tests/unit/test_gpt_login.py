"""Page-driven login, including omitted stages and credential-safe handoffs."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.gpt.login import LoginWorkflow
from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.providers.contracts import InteractionRequiredError
from api_agent.providers.contracts import SessionClosedError


class Page:
    url = 'https://auth.openai.com/log-in'

    def __init__(self, heading='Welcome back'):
        self.heading = heading
        self.field_visible = True
        self.button_visible = True
        self.fill = AsyncMock()
        self.click = AsyncMock()
        self.closed = False

    def is_closed(self):
        return self.closed

    def handle(self, action):
        async def bound(script, binding):
            return self.url == binding['url']
        handle = SimpleNamespace(fill=self.fill, click=self.click,
                                 evaluate=AsyncMock(side_effect=bound), dispose=AsyncMock(),
                                 evaluate_handle=AsyncMock(return_value=SimpleNamespace(
                                     evaluate=AsyncMock(return_value=True), dispose=AsyncMock())))
        return SimpleNamespace(is_visible=AsyncMock(return_value=action),
                               element_handle=AsyncMock(return_value=handle))

    def get_by_role(self, role, *, name, exact=False):
        if role == 'heading':
            def visible():
                return bool(name.search(self.heading)) if isinstance(name, re.Pattern) else self.heading == name
            item = SimpleNamespace(is_visible=AsyncMock(side_effect=visible),
                                   text_content=AsyncMock(side_effect=lambda: self.heading))
            item.first = item
            return item
        if role == 'button':
            assert name == 'Continue' and exact
            return self.handle(self.button_visible)
        assert role == 'textbox' and name == 'Email address' and exact
        return self.locator('email')

    def locator(self, selector):
        assert selector in ('email', 'input[type=password]', 'input[name=code][autocomplete=one-time-code]')
        return self.handle(self.field_visible)


def workflow(page, *, allow_interaction=True, credentials=None, app_name='test-app'):
    return LoginWorkflow(SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True)), 'person@example.test',
                         credentials or AsyncMock(return_value='private-credential'), app_name,
                         SimpleNamespace(check=AsyncMock()), allow_interaction=allow_interaction)


@pytest.mark.parametrize('headings', [
    ['Welcome back', 'Enter your password', 'Check your authenticator app',
     'Connect your test-app to ChatGPT', 'Connect test-app to ChatGPT'],
    ['Enter your password', 'Connect test-app to ChatGPT'],
    ['Check your authenticator app', 'Connect test-app to ChatGPT'],
    ['Connect test-app to ChatGPT'],
])
def test_observed_stages_skip_absent_steps_and_do_not_repeat(headings, capsys):
    async def run():
        page = Page()
        login = workflow(page)
        for heading in headings:
            page.heading = heading
            before = page.click.await_count
            await login.advance()
            await login.advance()
            assert page.click.await_count == before + 1
        expected = [kind for heading, kind in [('Enter your password', 'password'),
                    ('Check your authenticator app', 'totp')] if heading in headings]
        assert [call.args[0] for call in login.credentials.await_args_list] == expected
        values = [call.args[0] for call in page.fill.await_args_list]
        assert values == (['person@example.test'] if 'Welcome back' in headings else []) + ['private-credential'] * len(expected)
        assert login.guard.check.await_count >= len(headings) * 2
        assert login.disabled is False
    asyncio.run(run())
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('heading,field,button', [
    ('Unknown page', True, True), ('Welcome back', False, True),
    ('Enter your password', True, False), ('Connect test-app to ChatGPT', True, False),
])
def test_waits_for_recognized_visible_controls_without_retrieval(heading, field, button):
    async def run():
        page = Page(heading)
        page.field_visible, page.button_visible = field, button
        login = workflow(page)
        await login.advance()
        login.credentials.assert_not_awaited()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        assert login.last_action is None
    asyncio.run(run())


@pytest.mark.parametrize('url', ['about:blank', 'https://other.example/log-in',
                               'http://auth.openai.com/log-in', 'https://auth.openai.com.evil.test/log-in'])
@pytest.mark.parametrize('interactive', [True, False])
def test_never_sends_credentials_to_an_untrusted_origin(url, interactive, capsys):
    async def run():
        page = Page('Enter your password')
        page.url = url
        login = workflow(page, allow_interaction=interactive)
        if url != 'about:blank' and not interactive:
            with pytest.raises(AuthError, match='^login_interaction_required$'):
                await login.advance()
        else:
            await login.advance()
            assert login.disabled is (url != 'about:blank')
        login.credentials.assert_not_awaited()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
    asyncio.run(run())
    assert 'private-credential' not in capsys.readouterr().out


@pytest.mark.parametrize('value', [None, '', ' ', 1, InteractionRequiredError('private CLI output')])
@pytest.mark.parametrize('interactive', [True, False])
@pytest.mark.parametrize('heading,kind', [('Enter your password', 'password'), ('Check your authenticator app', 'totp')])
def test_unavailable_credentials_fail_without_manual_handoff(value, interactive, capsys, heading, kind):
    async def run():
        page = Page(heading)
        reader = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
        login = workflow(page, allow_interaction=interactive, credentials=reader)
        with pytest.raises(InteractionRequiredError) as caught:
            await login.advance()
        if isinstance(value, Exception):
            assert caught.value is value
        else:
            assert 'does not provide the credential requested' in str(caught.value)
        assert not login.disabled and login.last_action is None
        reader.assert_awaited_once_with(kind)
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
    asyncio.run(run())
    output = capsys.readouterr().out
    assert 'private CLI output' not in output
    assert not output


@pytest.mark.parametrize('where', ['guard', 'credentials', 'fill', 'click'])
def test_cancellation_propagates_and_does_not_mark_submission(where):
    async def run():
        page = Page('Enter your password')
        login = workflow(page)
        action = {'guard': login.guard.check, 'credentials': login.credentials,
                  'fill': page.fill, 'click': page.click}[where]
        action.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await login.advance()
        assert login.last_action is None
    asyncio.run(run())


def test_consent_uses_literal_app_name_and_never_requests_credentials():
    async def run():
        page = Page('Connect your app[2] to ChatGPT')
        login = workflow(page, app_name='app[2]')
        await login.advance()
        page.click.assert_awaited_once()
        login.credentials.assert_not_awaited()
        page.heading = 'Connect your app2 to ChatGPT'
        await login.advance()
        assert page.click.await_count == 1
    asyncio.run(run())


def test_browser_closure_cancels_and_drains_pending_credential_retrieval():
    async def run():
        page = Page('Enter your password')
        started, drained = asyncio.Event(), asyncio.Event()
        async def credentials(kind):
            assert kind == 'password'
            started.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()
        login = workflow(page, credentials=credentials)
        task = asyncio.create_task(login.advance())
        await started.wait()
        page.closed = True
        with pytest.raises(SessionClosedError):
            await asyncio.wait_for(task, 1)
        assert drained.is_set()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task() and not other.done()]
    asyncio.run(run())


def test_navigation_during_lookup_prevents_filling_a_different_page():
    async def run():
        page = Page('Enter your password')
        async def credentials(kind):
            page.url = 'https://other.example/log-in'
            return 'private-credential'
        login = workflow(page, credentials=credentials)
        await login.advance()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        assert login.last_action is None
    asyncio.run(run())


@pytest.mark.parametrize('replacement', ['https://untrusted.example/replacement',
                                       'https://auth.openai.com/replacement'])
def test_real_browser_navigation_during_fill_never_targets_replacement(replacement):
    """Locators retry across documents; credential actions must remain pinned."""
    from playwright.async_api import async_playwright, Locator, ElementHandle, Error
    from unittest.mock import patch

    async def run():
        entered = asyncio.Event()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                async def route(request):
                    disabled = ' disabled' if request.request.url.endswith('/original') else ''
                    await request.fulfill(content_type='text/html', body=(
                        '<h1>Enter your password</h1><input type=password' + disabled +
                        '><button onclick="window.clicked=true">Continue</button>'))
                await context.route('**/*', route)
                page = await context.new_page()
                await page.goto('https://auth.openai.com/original')
                login = workflow(page)
                login.session.browser = browser
                async def intercepted(original, instance, *args, **kwargs):
                    entered.set()
                    return await original(instance, *args, **kwargs)
                locator_fill, handle_fill = Locator.fill, ElementHandle.fill
                with patch.object(Locator, 'fill', lambda obj, *a, **kw: intercepted(locator_fill, obj, *a, **kw)), \
                     patch.object(ElementHandle, 'fill', lambda obj, *a, **kw: intercepted(handle_fill, obj, *a, **kw)):
                    action = asyncio.create_task(login.advance())
                    await asyncio.wait_for(entered.wait(), 3)
                    await page.goto(replacement)
                    try:
                        await action
                    except Error:
                        pass  # Detached original elements safely abort the action.
                assert await page.locator('input[type=password]').input_value() == ''
                assert not await page.evaluate('Boolean(window.clicked)')
                assert login.last_action is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing', ['field', 'button'])
def test_missing_pinned_element_aborts_and_disposes_other_handles(missing):
    async def run():
        page = Page('Enter your password')
        login = workflow(page)
        original = page.handle
        handles = []
        def acquired(visible):
            item = original(visible)
            handles.append(item.element_handle.return_value)
            if (len(handles) == 2 and missing == 'button') or (len(handles) == 1 and missing == 'field'):
                item.element_handle.return_value = None
            return item
        page.handle = acquired
        await login.advance()
        login.credentials.assert_not_awaited()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        for index, handle in enumerate(handles):
            if (index == 0 and missing != "field") or (index == 1 and missing != "button"):
                handle.dispose.assert_awaited_once()
        assert login.last_action is None
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['navigation', 'context_destroyed'])
def test_pinned_action_revalidates_before_submit_and_disposes_handles(changed):
    async def run():
        page = Page('Enter your password')
        handles = []
        original = page.handle
        def acquired(visible):
            item = original(visible)
            handles.append(item.element_handle.return_value)
            return item
        page.handle = acquired
        async def filled(*args, **kwargs):
            if changed == 'navigation':
                page.url = 'https://untrusted.example/replacement'
            else:
                for handle in handles:
                    handle.evaluate.side_effect = RuntimeError('destroyed original context')
        page.fill.side_effect = filled
        login = workflow(page)
        await login.advance()
        page.fill.assert_awaited_once()
        page.click.assert_not_awaited()
        assert login.last_action is None
        for handle in handles:
            handle.dispose.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('pause', ['foreign', 'challenge'])
def test_human_pause_resumes_only_recognized_trusted_page(pause, capsys):
    async def run():
        page = Page('Enter your password')
        login = workflow(page)
        if pause == 'foreign':
            page.url = 'https://other.example/log-in'
        else:
            login.guard.check.return_value = True
        await login.advance()
        await login.advance()
        login.credentials.assert_not_awaited()
        assert login.disabled
        page.url = 'https://auth.openai.com/log-in'
        page.heading = 'Unknown page'
        login.guard.check.return_value = False
        await login.advance()
        login.credentials.assert_not_awaited()
        page.heading = 'Enter your password'
        await login.advance()
        await login.advance()
        login.credentials.assert_awaited_once_with('password')
        page.click.assert_awaited_once()
        assert not login.disabled
    asyncio.run(run())
    assert capsys.readouterr().out.count('automatic credential entry is paused') <= 1


@pytest.mark.parametrize('heading', ['Use your security key', 'Check your email', 'Choose a verification method'])
def test_offline_dom_challenge_resumes_password_only_after_recognized_heading(heading):
    from playwright.async_api import async_playwright
    from api_agent.llm_providers.gpt.connect import LoginPageGuard
    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<h1>' + heading + '</h1>'))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/log-in')
                login = workflow(page)
                login.session.browser = browser
                login.guard = LoginPageGuard(page, allow_interaction=True)
                await login.advance()
                await login.advance()
                login.credentials.assert_not_awaited()
                await page.set_content('<h1>Unknown page</h1><input type=password><button>Continue</button>')
                await login.advance()
                login.credentials.assert_not_awaited()
                await page.set_content('<h1>Enter your password</h1><input type=password><button onclick="window.clicked=true">Continue</button>')
                await login.advance()
                await login.advance()
                login.credentials.assert_awaited_once_with('password')
                assert await page.locator('input').input_value() == 'private-credential'
                assert await page.evaluate('window.clicked')
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage,replacement,overlay', [
    ('Enter your password', 'Unknown page', False),
    ('Enter your password', 'Use your security key', False),
    ('Check your authenticator app', 'Check your email', False),
    ('Enter your password', 'Use your security key', True),
    ('Check your authenticator app', 'Check your email', True),
])
@pytest.mark.parametrize('transition', ['lookup', 'after_fill'])
def test_same_document_stage_change_invalidates_pending_action(stage, replacement, overlay, transition):
    from playwright.async_api import async_playwright, ElementHandle
    from unittest.mock import patch
    from api_agent.llm_providers.gpt.connect import LoginPageGuard
    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>' + stage + '</h1><input type=password name=code autocomplete=one-time-code>'
                    '<button onclick="window.clicks=(window.clicks||0)+1">Continue</button>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/log-in')
                async def replace_heading():
                    if overlay:
                        await page.evaluate('(text)=>{const el=document.createElement("h2");el.textContent=text;document.body.append(el)}', replacement)
                    else:
                        await page.locator('h1').evaluate('(el,text)=>el.textContent=text', replacement)
                async def credential(kind):
                    if transition == 'lookup':
                        await replace_heading()
                    return 'private-credential'
                login = workflow(page, credentials=AsyncMock(side_effect=credential))
                login.session.browser = browser
                login.guard = LoginPageGuard(page, allow_interaction=True)
                original_fill = ElementHandle.fill
                async def fill(handle, *args, **kwargs):
                    await original_fill(handle, *args, **kwargs)
                    if transition == 'after_fill':
                        await replace_heading()
                with patch.object(ElementHandle, 'fill', fill):
                    await login.advance()
                assert not await page.evaluate('Boolean(window.clicks)')
                if transition == 'lookup':
                    assert await page.locator('input').input_value() == ''
                assert login.last_action is None
                await page.locator('h2').evaluate_all('(els)=>els.forEach(el=>el.remove())')
                await page.locator('h1').evaluate('(el,text)=>el.textContent=text', stage)
                login.credentials.side_effect = None
                login.credentials.return_value = 'private-credential'
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                await replace_heading()
                await login.advance()
                await page.locator('h2').evaluate_all('(els)=>els.forEach(el=>el.remove())')
                await page.locator('h1').evaluate('(el,text)=>el.textContent=text', stage)
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
            finally:
                await browser.close()
    asyncio.run(run())


def test_username_only_workflow_waits_on_password_without_requesting_credentials():
    async def run():
        page = Page('Enter your password')
        login = workflow(page)
        login.credentials = None
        await login.advance()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
    asyncio.run(run())


def test_empty_username_is_rejected_before_entry():
    from api_agent.providers.contracts import UnattendedCredentialError
    async def run():
        page = Page()
        login = workflow(page)
        login.username = ''
        with pytest.raises(UnattendedCredentialError):
            await login.advance()
        page.fill.assert_not_awaited()
    asyncio.run(run())


def test_destroyed_bound_context_after_lookup_prevents_entry_on_matching_stage():
    async def run():
        page = Page('Enter your password')
        acquired = []
        original = page.handle
        def handle(visible):
            result = original(visible)
            acquired.append(result.element_handle.return_value)
            return result
        page.handle = handle
        async def credentials(kind):
            for item in acquired:
                item.evaluate.side_effect = RuntimeError('original context destroyed')
            return 'private-credential'
        login = workflow(page, credentials=credentials)
        await login.advance()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        assert login.last_action is None
        assert all(item.dispose.await_count == 1 for item in acquired)
    asyncio.run(run())


def test_full_document_reload_allows_same_stage_while_overlay_does_not():
    from playwright.async_api import async_playwright, Error
    from api_agent.llm_providers.gpt.connect import LoginPageGuard
    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>Enter your password</h1><input type=password>'
                    '<button onclick="window.clicks=(window.clicks||0)+1">Continue</button>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/log-in')
                login = workflow(page)
                login.session.browser = browser
                login.guard = LoginPageGuard(page, allow_interaction=True)
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                await page.evaluate('document.body.insertAdjacentHTML("beforeend","<h2>Use your security key</h2>")')
                await login.advance()
                await page.locator('h2').evaluate('(el)=>el.remove()')
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                login.credentials.assert_awaited_once_with('password')
                await page.reload()
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                assert login.credentials.await_count == 2
                document = login.last_document
                await login.aclose()
                assert login.last_document is None
                with pytest.raises(Error):
                    await document.evaluate('document => document === globalThis.document')
            finally:
                await browser.close()
    asyncio.run(run())


def test_click_navigation_retains_submitted_document_identity():
    from playwright.async_api import async_playwright
    from api_agent.llm_providers.gpt.connect import LoginPageGuard
    async def run():
        clicks = []
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.expose_function('countClick', lambda: clicks.append(True))
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>Enter your password</h1><input type=password>'
                    '<button onclick="countClick().then(()=>location.reload())">Continue</button>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/log-in')
                login = workflow(page)
                login.session.browser = browser
                login.guard = LoginPageGuard(page, allow_interaction=True)
                async with page.expect_event('domcontentloaded', timeout=3000):
                    await login.advance()
                async with page.expect_event('domcontentloaded', timeout=3000):
                    await login.advance()
                assert len(clicks) == 2
                assert login.credentials.await_count == 2
                await login.aclose()
            finally:
                await browser.close()
    asyncio.run(run())


def test_failed_document_disposal_retains_owned_handle_for_retry():
    from api_agent.providers.control import CleanupPendingError
    async def run():
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=RuntimeError('private cleanup diagnostic')))
        login.last_document = document
        login.documents.append(document)
        with pytest.raises(CleanupPendingError, match='^cleanup pending$') as caught:
            await login.aclose()
        assert caught.value.owners == (login,)
        assert caught.value.retry == login.aclose
        assert login.last_document is document
        assert login.documents == [document]
        document.dispose.side_effect = None
        await caught.value.retry()
        await caught.value.retry()
        assert login.last_document is None
        assert login.documents == []
        assert document.dispose.await_count == 2
    asyncio.run(run())


@pytest.mark.parametrize('failure', [False, True])
def test_repeated_cancellation_drains_document_release_once_with_safe_receipt(failure):
    from api_agent.providers.control import CleanupPendingError
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
            if failure:
                raise RuntimeError('private cleanup diagnostic')
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=dispose))
        login.last_document = document
        login.documents.append(document)
        task = asyncio.create_task(login.aclose())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert login.closing.done()
        document.dispose.assert_awaited_once()
        if failure:
            assert caught.value.retained_owners == (login,)
            pending = caught.value.cleanup_error
            assert isinstance(pending, CleanupPendingError)
            assert str(pending) == 'cleanup pending'
            assert login.documents == [document]
            document.dispose.side_effect = None
            await pending.retry()
            assert document.dispose.await_count == 2
        else:
            assert not hasattr(caught.value, 'cleanup_error')
            await login.aclose()
            document.dispose.assert_awaited_once()
        assert login.last_document is None
        assert login.documents == []
    asyncio.run(run())


def test_concurrent_document_release_joins_one_owned_disposal():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=dispose))
        login.documents.append(document)
        tasks = [asyncio.create_task(login.aclose()) for _ in range(2)]
        await entered.wait()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        document.dispose.assert_awaited_once()
        assert not login.documents
    asyncio.run(run())


def test_internally_cancelled_document_release_is_safe_pending_cleanup():
    from api_agent.providers.control import CleanupPendingError
    async def run():
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=asyncio.CancelledError()))
        login.documents.append(document)
        with pytest.raises(CleanupPendingError) as caught:
            await login.aclose()
        document.dispose.side_effect = None
        await caught.value.retry()
        assert not login.documents
    asyncio.run(run())


def test_finalizer_preserves_primary_when_release_is_cancelled_without_pending_documents():
    from api_agent.llm_providers.gpt.login import finish_login
    async def run():
        primary = AuthError('account_mismatch')
        owner = SimpleNamespace(aclose=AsyncMock(side_effect=asyncio.CancelledError()))
        await finish_login(owner, primary)
        assert isinstance(primary.cleanup_error, asyncio.CancelledError)
        assert not hasattr(primary, 'retained_owners')
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_blocked_document_release_returns_bounded_receipt_and_retry_joins_same_task(cancel, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.llm_providers.gpt import login as login_module
    report = login_module.report_timeout
    monkeypatch.setattr(login_module, 'report_timeout', lambda name, timeout: report(name, .05))
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=dispose))
        login.documents.append(document)
        task = asyncio.create_task(login.aclose())
        await entered.wait()
        if cancel:
            task.cancel()
        expected = asyncio.CancelledError if cancel else CleanupPendingError
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, 5)
        pending = caught.value.cleanup_error if cancel else caught.value
        assert pending.owners == (login,)
        original = login.closing
        assert not original.done()
        with pytest.raises(CleanupPendingError):
            await asyncio.wait_for(pending.retry(), 5)
        assert login.closing is original
        document.dispose.assert_awaited_once()
        release.set()
        await pending.retry()
        assert original.done()
        assert login.documents == []
        document.dispose.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('blocked', [False, True])
def test_cancellation_during_progress_finalization_classifies_actual_owned_document_state(monkeypatch, blocked):
    from contextlib import asynccontextmanager
    from api_agent.llm_providers.gpt import login as login_module
    async def run():
        progress_exit, release_progress, release_document = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_report = login_module.report_timeout
        @asynccontextmanager
        async def reporting(name, timeout):
            async with original_report(name, .05) as budget:
                try:
                    yield budget
                finally:
                    progress_exit.set()
                    await release_progress.wait()
        monkeypatch.setattr(login_module, 'report_timeout', reporting)
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=release_document.wait if blocked else None))
        login.documents.append(document)
        task = asyncio.create_task(login.aclose())
        await asyncio.wait_for(progress_exit.wait(), 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            if blocked:
                assert caught.value.retained_owners == (login,)
                assert caught.value.cleanup_error.retry == login.aclose
                assert not login.closing.done()
            else:
                assert not hasattr(caught.value, 'retained_owners')
                assert not hasattr(caught.value, 'cleanup_error')
                assert login.documents == []
                assert login.closing.done()
        finally:
            release_document.set()
            release_progress.set()
            await login.aclose()
        assert login.documents == []
        document.dispose.assert_awaited_once()
    asyncio.run(run())
