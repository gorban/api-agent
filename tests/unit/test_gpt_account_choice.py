"""Account choice uses offered exact email text and the existing pinned action."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import async_playwright

from api_agent.llm_providers.gpt.connect import LoginPageGuard
from api_agent.llm_providers.gpt.login import LoginWorkflow
from api_agent.llm_providers.gpt.contracts import AuthError


@pytest.fixture
def choice_pages():
    async def use(check, *, email='person@example.test', duplicate=False, heading='Use ChatGPT to sign in to chosen-app'):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                selected = []
                await context.expose_function('selected', lambda value: selected.append(value))
                async def respond(route):
                    button = f'<button onclick="selected(1)"><span>{email}</span></button>'
                    await route.fulfill(content_type='text/html', body=(f'<h1>{heading}</h1>' + button
                        + (button if duplicate else '')
                        + '<button onclick="selected(2)"><span>other@example.test</span></button>'))
                await context.route('https://auth.openai.com/**', respond)
                page = await context.new_page()
                await page.goto('https://auth.openai.com/choose-an-account')
                credentials = AsyncMock()
                login = LoginWorkflow(SimpleNamespace(page=page, context=context, browser=browser),
                    'person@example.test', credentials, 'chosen-app', LoginPageGuard(page, allow_interaction=True),
                    allow_interaction=True)
                try:
                    async with asyncio.timeout(10):
                        await check(login, page, selected)
                finally:
                    await login.aclose()
                credentials.assert_not_awaited()
                assert not login.documents
            finally:
                await browser.close()
    return use


def test_matching_offered_account_is_clicked_once_without_retrieving_credentials(choice_pages):
    async def check(login, page, selected):
        await login.advance()
        await login.advance()
        assert selected == [1] and not login.disabled
        assert login.last_action[1] == 'Use selected account'
    asyncio.run(choice_pages(check))


@pytest.mark.parametrize('email,duplicate', [('wrong-person@example.test', False), ('person@example.test', True)])
def test_missing_or_ambiguous_account_pauses_without_selecting_another(choice_pages, email, duplicate):
    async def check(login, page, selected):
        await login.advance()
        assert selected == [] and login.disabled
    asyncio.run(choice_pages(check, email=email, duplicate=duplicate))


def test_unknown_app_heading_never_selects_a_matching_email(choice_pages):
    async def check(login, page, selected):
        await login.advance()
        assert selected == [] and login.last_action is None
    asyncio.run(choice_pages(check, heading='Use ChatGPT to sign in to another-app'))


def test_headless_missing_account_requests_existing_visible_intervention(choice_pages):
    async def check(login, page, selected):
        login.allow_interaction = False
        with pytest.raises(AuthError, match='login_interaction_required'):
            await login.advance()
        assert selected == []
    asyncio.run(choice_pages(check, email='other-account@example.test'))


def test_hidden_matching_control_does_not_select_another_account():
    async def scenario():
        heading = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        control = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=False))
        controls = SimpleNamespace(filter=Mock(return_value=control))
        def role(kind, **options):
            return heading if kind == 'heading' else controls
        page = SimpleNamespace(url='https://auth.openai.com/choose-an-account',
            get_by_role=role, get_by_text=Mock())
        login = LoginWorkflow(SimpleNamespace(page=page), 'person@example.test', AsyncMock(),
            'chosen-app', SimpleNamespace(check=AsyncMock(return_value=False)), allow_interaction=True)
        login._choose = AsyncMock()
        await login.advance()
        login._choose.assert_not_awaited()
        assert login.disabled
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['http://auth.openai.com/choose-an-account',
    'https://auth.openai.com.evil.test/choose-an-account', 'https://auth.openai.com/other'])
def test_untrusted_or_unrecognized_chooser_never_runs_account_selection(url):
    async def scenario():
        page = SimpleNamespace(url=url)
        login = LoginWorkflow(SimpleNamespace(page=page), 'person@example.test', AsyncMock(),
            'chosen-app', SimpleNamespace(check=AsyncMock(return_value=True)), allow_interaction=True)
        login._account_choice = AsyncMock()
        login._alternative = AsyncMock(return_value=False)
        await login.advance()
        login._account_choice.assert_not_awaited()
    asyncio.run(scenario())
