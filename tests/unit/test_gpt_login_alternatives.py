"""Only choose an observed route backed by the selected login's material."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from api_agent.llm_providers.gpt.connect import LoginPageGuard
from api_agent.llm_providers.gpt.login import LoginWorkflow
from api_agent.providers.contracts import UnattendedCredentialError


@pytest.fixture
def alternative_page():
    async def use(test):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>Check your inbox</h1><a href="#passkey" onclick="window.chosen=\'passkey\';return false">Continue with passkey</a>'
                    '<a href="#password" onclick="window.chosen=\'password\';return false">Continue with password</a>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/email-verification')
                await test(page, browser)
            finally:
                await browser.close()
    return use


def make_workflow(page, browser, *, password=None, passkey=False, reader=None, known=True):
    return LoginWorkflow(SimpleNamespace(page=page, browser=browser), 'person@example.test',
        reader or AsyncMock(return_value='confirmed-password'), 'test-app',
        LoginPageGuard(page, allow_interaction=True), allow_interaction=True,
        material=SimpleNamespace(password=password) if known else None, software_passkey=passkey)


def test_passkey_is_preferred_when_both_routes_are_offered(alternative_page):
    async def check(page, browser):
        login = make_workflow(page, browser, password='confirmed-password', passkey=True)
        await login.advance()
        assert await page.evaluate('window.chosen') == 'passkey'
        login.credentials.assert_not_awaited()
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_password_is_used_only_when_present(alternative_page):
    async def check(page, browser):
        login = make_workflow(page, browser, password='confirmed-password')
        await login.advance()
        assert await page.evaluate('window.chosen') == 'password'
        login.credentials.assert_not_awaited()
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_absent_password_does_not_choose_password_route(alternative_page, capsys):
    async def check(page, browser):
        login = make_workflow(page, browser)
        await login.advance()
        await login.advance()
        assert login.documents == []
        assert await page.evaluate('window.chosen') is None
        login.credentials.assert_not_awaited()
        await login.aclose()
    asyncio.run(alternative_page(check))
    assert capsys.readouterr().out.count('no password') == 1


def test_legacy_adapter_password_is_confirmed_before_choosing(alternative_page):
    async def check(page, browser):
        async def retrieve(kind):
            assert kind == 'password'
            assert await page.evaluate('window.chosen') is None
            return 'confirmed-password'
        login = make_workflow(page, browser, reader=AsyncMock(side_effect=retrieve), known=False)
        await login.advance()
        assert await page.evaluate('window.chosen') == 'password'
        login.credentials.assert_awaited_once_with('password')
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_locked_manager_is_an_error_not_an_absent_credential(alternative_page):
    async def check(page, browser):
        login = make_workflow(page, browser, known=False,
            reader=AsyncMock(side_effect=UnattendedCredentialError('Sign in to the configured CLI.')))
        with pytest.raises(UnattendedCredentialError, match='configured CLI'):
            await login.advance()
        assert await page.evaluate('window.chosen') is None
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_navigation_while_reading_material_never_clicks_replacement(alternative_page):
    async def check(page, browser):
        async def retrieve(_):
            await page.goto('https://other.example/email-verification')
            return 'confirmed-password'
        login = make_workflow(page, browser, reader=AsyncMock(side_effect=retrieve), known=False)
        await login.advance()
        assert await page.evaluate('window.chosen') is None
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_software_passkey_page_is_not_mistaken_for_human_only_challenge(alternative_page):
    # The input-free challenge uses DOM text replacement, not credential entry.
    async def challenge(page, browser):
        await page.goto('https://auth.openai.com/auth_challenge/passkey')
        await page.locator('h1').evaluate('(node) => node.textContent = "Use your passkey"')
        login = make_workflow(page, browser, passkey=True)
        await login.advance()
        assert not login.guard.notified
        login.credentials.assert_not_awaited()
        await login.aclose()
    asyncio.run(alternative_page(challenge))


def test_missing_control_handle_cannot_choose_route(alternative_page, monkeypatch):
    async def check(page, browser):
        login = make_workflow(page, browser, password='confirmed-password')
        control = SimpleNamespace(element_handle=AsyncMock(return_value=None))
        heading = page.get_by_role('heading', name='Check your inbox', exact=True)
        await login._choose(control, heading, page.url, 'Continue with password')
        assert await page.evaluate('window.chosen') is None
        assert not login.documents
        await login.aclose()
    asyncio.run(alternative_page(check))


def test_unchanged_document_is_not_submitted_twice(alternative_page):
    async def check(page, browser):
        login = make_workflow(page, browser, password='confirmed-password')
        await login.advance()
        current = login.last_document
        await login.advance()
        assert login.last_document is current and login.documents == [current]
        await login.aclose()
    asyncio.run(alternative_page(check))
