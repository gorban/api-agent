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

    def get_by_role(self, role, *, name, exact=False):
        if role == 'heading':
            visible = bool(name.search(self.heading)) if isinstance(name, re.Pattern) else self.heading == name
            return SimpleNamespace(is_visible=AsyncMock(return_value=visible),
                                   text_content=AsyncMock(return_value=self.heading))
        if role == 'button':
            assert name == 'Continue' and exact
            return SimpleNamespace(is_visible=AsyncMock(return_value=self.button_visible), click=self.click)
        assert role == 'textbox' and name == 'Email address' and exact
        return self.locator('email')

    def locator(self, selector):
        assert selector in ('email', 'input[type=password]', 'input[name=code][autocomplete=one-time-code]')
        return SimpleNamespace(is_visible=AsyncMock(return_value=self.field_visible), fill=self.fill)


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
def test_unavailable_credentials_hand_off_once_or_fail_safely(value, interactive, capsys):
    async def run():
        page = Page('Enter your password')
        reader = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
        login = workflow(page, allow_interaction=interactive, credentials=reader)
        if interactive:
            await login.advance()
            await login.advance()
            assert login.disabled
        else:
            with pytest.raises(AuthError, match='^login_interaction_required$'):
                await login.advance()
        reader.assert_awaited_once_with('password')
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
    asyncio.run(run())
    output = capsys.readouterr().out
    assert 'private CLI output' not in output
    assert bool(output) is interactive
    assert len(output.splitlines()) == int(interactive)


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
