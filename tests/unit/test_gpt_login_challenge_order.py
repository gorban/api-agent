"""Human challenges take precedence over every offered automatic login route."""
import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.gpt.contracts import AuthError
from test_gpt_login_alternatives import alternative_page, make_workflow


@pytest.mark.parametrize('interactive', [False, True])
@pytest.mark.parametrize('path,html', [
    ('/auth_challenge/passkey', '<h1>Check your inbox</h1><button onclick="window.chosen=true">Continue with password</button>'),
    ('/choose-an-account', '<h1>Performing security verification</h1><h2>Use ChatGPT to sign in to test-app</h2>'
     '<button onclick="window.chosen=true"><span>person@example.test</span></button>'),
])
def test_initial_challenge_prevents_alternative_and_account_actions(alternative_page, interactive, path, html):
    async def check(page, browser):
        await page.evaluate('(path) => history.replaceState({}, "", path)', path)
        await page.set_content(html)
        login = make_workflow(page, browser, password='synthetic-password')
        login.allow_interaction = login.guard.allow_interaction = interactive
        expected = nullcontext() if interactive else pytest.raises(AuthError, match='^login_interaction_required$')
        try:
            with expected:
                await login.advance()
            assert await page.evaluate('window.chosen') is None
            login.credentials.assert_not_awaited()
            assert not login.documents
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))


@pytest.mark.parametrize('interactive', [False, True])
def test_challenge_during_password_lookup_prevents_alternative_click(alternative_page, interactive):
    async def check(page, browser):
        async def retrieve(_):
            await page.locator('body').evaluate(
                '(body) => body.insertAdjacentHTML("beforeend", "<h2>Performing security verification</h2>")')
            return 'synthetic-password'
        login = make_workflow(page, browser, reader=AsyncMock(side_effect=retrieve), known=False)
        login.allow_interaction = login.guard.allow_interaction = interactive
        expected = nullcontext() if interactive else pytest.raises(AuthError, match='^login_interaction_required$')
        try:
            with expected:
                await login.advance()
            assert await page.evaluate('window.chosen') is None
            login.credentials.assert_awaited_once_with('password')
            assert not login.documents
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))


def test_challenge_appearing_during_element_validation_prevents_click(alternative_page):
    async def check(page, browser):
        login = make_workflow(page, browser, password='synthetic-password')
        original = login._bound
        async def overlay(*args):
            bound = await original(*args)
            await page.locator('body').evaluate(
                '(body) => body.insertAdjacentHTML("beforeend", "<h2>Performing security verification</h2>")')
            return bound
        login._bound = overlay
        try:
            await login.advance()
            assert await page.evaluate('window.chosen') is None
            assert login.guard.notified and login.disabled
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))


@pytest.mark.parametrize('heading', ['Check your email', 'Verify your email',
    'Choose a verification method', 'Choose how to verify', 'Verify your identity'])
def test_offered_credential_alternative_is_allowed_without_a_blocking_challenge(alternative_page, heading):
    async def check(page, browser):
        await page.locator('h1').evaluate('(element, text) => element.textContent=text', heading)
        login = make_workflow(page, browser, password='synthetic-password')
        try:
            await login.advance()
            assert await page.evaluate('window.chosen') == 'password'
            assert not login.guard.notified
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))


@pytest.mark.parametrize('heading', ['Performing security verification', 'Use your security key', 'Check your email'])
def test_software_authenticator_does_not_suppress_other_challenges(alternative_page, heading):
    async def check(page, browser):
        await page.evaluate('history.replaceState({}, "", "/auth_challenge/passkey")')
        await page.locator('h1').evaluate('(element, text) => element.textContent=text', heading)
        login = make_workflow(page, browser, passkey=True)
        try:
            await login.advance()
            assert login.guard.notified and login.disabled
            assert await page.evaluate('window.chosen') is None
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))


@pytest.mark.parametrize('interactive', [False, True])
def test_missing_alternatives_request_manual_help_without_reading_credentials(alternative_page, interactive):
    async def check(page, browser):
        await page.locator('a').evaluate_all('(links) => links.forEach(link => link.remove())')
        login = make_workflow(page, browser, password='synthetic-password', passkey=True)
        login.allow_interaction = login.guard.allow_interaction = interactive
        expected = nullcontext() if interactive else pytest.raises(AuthError, match='^login_interaction_required$')
        try:
            with expected:
                await login.advance()
            assert login.disabled is interactive
            assert await page.evaluate('window.chosen') is None
            login.credentials.assert_not_awaited()
            assert not login.documents
        finally:
            await login.aclose()
    asyncio.run(alternative_page(check))
