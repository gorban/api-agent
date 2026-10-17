"""Direct authorization uses the configured name and the original managed page."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.async_api import async_playwright

from api_agent.llm_providers.gpt import connect
from api_agent.llm_providers.gpt.login import LoginWorkflow
from test_gpt_connect import Store


@pytest.fixture
def authorization_page():
    async def use(check):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                navigations = []
                async def respond(route):
                    location = urlsplit(route.request.url)
                    navigations.append((location.netloc, location.path))
                    assert navigations == [('auth.openai.com', '/api/accounts/authorize')]
                    query = parse_qs(location.query)
                    assert query['agent_name_hint'] == ['chosen-app']
                    target = query['redirect_uri'][0] + '?' + urlencode(
                        dict(state=query['state'][0], code='synthetic-code'))
                    html = ('<h1>Connect chosen-app to ChatGPT</h1>'
                            '<button onclick="location.href=' + repr(target) + '">Continue</button>')
                    await route.fulfill(content_type='text/html', body=html)
                await context.route('https://**/*', respond)
                page = await context.new_page()
                session = SimpleNamespace(page=page, context=context, browser=browser)
                async with asyncio.timeout(10):
                    await check(session)
                assert navigations == [('auth.openai.com', '/api/accounts/authorize')]
            finally:
                await browser.close()
    return use


def test_direct_authorization_real_callback_preserves_configured_name_and_identity(authorization_page, monkeypatch):
    from api_agent.llm_providers.gpt import oauth, passkeys
    async def check(session):
        authenticator = SimpleNamespace(prepare=AsyncMock(), aclose=AsyncMock())
        create = Mock(return_value=authenticator)
        monkeypatch.setattr(passkeys, 'SoftwarePasskey', create)
        monkeypatch.setattr(passkeys, 'require_private_transport', lambda: None)
        finish = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        monkeypatch.setattr(oauth, 'finish_authorization', finish)
        store = Store()
        close = LoginWorkflow.aclose
        async def close_after_verified_save(login):
            assert len(store.saved) == 1
            await close(login)
        monkeypatch.setattr(LoginWorkflow, 'aclose', close_after_verified_save)
        store.host_id = lambda: 'urn:uuid:12345678-1234-1234-1234-123456789abc'
        owner = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
        material = SimpleNamespace(passkey_available=True, passkey=object(), password=None)
        result = await connect.connect_account(store, browser_owner=owner, client=object(),
            app_name='chosen-app', login_username='person@example.test', login_credentials=AsyncMock(),
            login_material=AsyncMock(return_value=material))
        assert result == 'account'
        assert len(store.saved) == 1 and session.context.pages == [session.page]
        assert create.call_args.kwargs['callback_url'].startswith('http://127.0.0.1:')
        assert finish.await_args.args[1]['code'] == 'synthetic-code'
        authenticator.prepare.assert_awaited_once()
        authenticator.aclose.assert_awaited_once()
        owner.aclose.assert_awaited_once()
    asyncio.run(authorization_page(check))
