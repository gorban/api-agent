"""First-party hints advance navigation; only normal OAuth completes setup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.async_api import async_playwright

from api_agent.llm_providers.gpt import connect
from api_agent.llm_providers.gpt.firstparty import FirstPartyWorkflow, authenticate_first_party
from api_agent.llm_providers.gpt.login import LoginWorkflow
from test_gpt_connect import Store


def workflow(page, context, browser=None):
    return FirstPartyWorkflow(SimpleNamespace(page=page, context=context, browser=browser),
        'person@example.test', AsyncMock(), 'chosen-app',
        connect.LoginPageGuard(page, allow_interaction=True), allow_interaction=True,
        material=SimpleNamespace(password=None), software_passkey=True)


@pytest.mark.parametrize('name,value,expires,expected', [
    ('__Secure-next-auth.session-token', 'synthetic', -1, True),
    ('__Secure-next-auth.session-token.0', 'synthetic', 200, True),
    ('__Secure-next-auth.session-token', '', -1, False),
    ('__Secure-next-auth.session-token', 'synthetic', 100, False),
    ('__Secure-next-auth.session-token', 'synthetic', 99, False),
    ('__Secure-next-auth.session-token.evil', 'synthetic', -1, False),
    ('next-auth.csrf-token', 'synthetic', -1, False),
])
def test_only_eligible_session_cookies_advance_the_handoff(monkeypatch, name, value, expires, expected):
    from api_agent.llm_providers.gpt import firstparty
    monkeypatch.setattr(firstparty.time, 'time', lambda: 100)
    async def scenario():
        control = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        control.first = control
        page = SimpleNamespace(url='https://chatgpt.com/', get_by_role=Mock(return_value=control),
                               locator=Mock(return_value=control))
        context = SimpleNamespace(cookies=AsyncMock(return_value=[dict(name=name, value=value, expires=expires)]))
        login = workflow(page, context)
        await login.advance()
        assert login.ready.done() is expected
        assert login.submitted_email is False
        await login.aclose()
        assert login.ready.done()
    asyncio.run(scenario())


@pytest.mark.parametrize('submitted,login_visible,ready', [(False, False, False), (True, False, True)])
def test_missing_login_is_a_handoff_only_after_account_submission(submitted, login_visible, ready):
    async def scenario():
        control = SimpleNamespace(is_visible=AsyncMock(return_value=login_visible))
        control.first = control
        page = SimpleNamespace(url='https://chatgpt.com/', get_by_role=Mock(return_value=control),
                               locator=Mock(return_value=control))
        login = workflow(page, SimpleNamespace(cookies=AsyncMock(return_value=[])))
        login.submitted_email = submitted
        await login.advance()
        assert login.ready.done() is ready
        await login.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['https://auth.openai.com/log-in', 'https://chatgpt.com.evil.test/',
    'http://chatgpt.com/', 'https://chatgpt.com:8443/', 'https://other.example.test/'])
def test_other_pages_delegate_to_the_existing_guard(monkeypatch, url):
    async def scenario():
        control = SimpleNamespace(first=None)
        page = SimpleNamespace(url=url, get_by_role=Mock(return_value=control))
        context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        login = workflow(page, context)
        delegate = AsyncMock()
        monkeypatch.setattr(LoginWorkflow, 'advance', delegate)
        await login.advance()
        delegate.assert_awaited_once()
        assert not login.ready.done()
        await login.aclose()
    asyncio.run(scenario())


@pytest.fixture
def firstparty_pages():
    async def use(check):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                entered = []
                await context.expose_function('recordEmail', lambda value: entered.append(value))
                async def respond(route):
                    location = urlsplit(route.request.url)
                    if location.netloc == 'chatgpt.com' and location.path == '/':
                        html = '''<button onclick="document.querySelector('form').hidden=false">Log in</button>
                            <form hidden onsubmit="event.preventDefault();recordEmail(document.querySelector('input').value)
                            .then(()=>location.href='https://auth.openai.com/email-verification')">
                            <input type=email name=login_hint><button>Continue</button></form>'''
                    elif location.path == '/email-verification':
                        html = '<h1>Check your inbox</h1><a href="/log-in/passkey">Continue with passkey</a>'
                    elif location.path == '/log-in/passkey':
                        html = '<script>location.href="https://chatgpt.com/signed-in"</script>'
                    elif location.path == '/api/accounts/authorize':
                        query = parse_qs(location.query)
                        assert query['agent_name_hint'] == ['chosen-app']
                        target = query['redirect_uri'][0] + '?' + urlencode(dict(state=query['state'][0], code='synthetic-code'))
                        html = '<h1>Connect chosen-app to ChatGPT</h1><button onclick="location.href=' + repr(target) + '">Continue</button>'
                    else:
                        html = '<h1>Conversation</h1>'
                    await route.fulfill(content_type='text/html', body=html)
                await context.route('https://chatgpt.com/**', respond)
                await context.route('https://auth.openai.com/**', respond)
                page = await context.new_page()
                session = SimpleNamespace(page=page, context=context, browser=browser)
                async with asyncio.timeout(10):
                    await check(session, entered)
            finally:
                await browser.close()
    return use


def test_offered_firstparty_flow_reuses_the_original_page(firstparty_pages):
    async def check(session, entered):
        login = workflow(session.page, session.context, session.browser)
        try:
            await authenticate_first_party(login, session)
            assert entered == ['person@example.test']
            assert session.page.url == 'https://chatgpt.com/signed-in'
            assert session.context.pages == [session.page]
            login.credentials.assert_not_awaited()
        finally:
            await login.aclose()
        assert not login.documents
    asyncio.run(firstparty_pages(check))


def test_direct_authorization_real_callback_preserves_configured_name_and_identity(firstparty_pages, monkeypatch):
    from api_agent.llm_providers.gpt import oauth, passkeys
    async def check(session, entered):
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
        assert result == 'account' and entered == []
        assert len(store.saved) == 1 and session.context.pages == [session.page]
        assert 'first_party' not in create.call_args.kwargs
        assert finish.await_args.args[1]['code'] == 'synthetic-code'
        authenticator.prepare.assert_awaited_once()
        authenticator.aclose.assert_awaited_once()
        owner.aclose.assert_awaited_once()
    asyncio.run(firstparty_pages(check))


@pytest.mark.parametrize('where,expected_fills', [('button', 0), ('field', 0), ('before-fill', 0), ('after-fill', 1)])
def test_replaced_or_missing_pinned_controls_never_submit(where, expected_fills):
    async def scenario():
        document = SimpleNamespace(dispose=AsyncMock())
        handle = SimpleNamespace(evaluate_handle=AsyncMock(return_value=document),
            fill=AsyncMock(), click=AsyncMock(), dispose=AsyncMock())
        button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
            element_handle=AsyncMock(return_value=None if where == 'button' else handle))
        field = SimpleNamespace(element_handle=AsyncMock(return_value=None if where == 'field' else handle))
        page = SimpleNamespace(url='https://chatgpt.com/')
        login = workflow(page, object())
        login._bound = AsyncMock(side_effect=[True, False] if where == 'after-fill' else [False])
        await login._submit(button, field, expected_url=page.url)
        assert handle.fill.await_count == expected_fills
        handle.click.assert_not_awaited()
        assert login.last_action is None
        await login.aclose()
        assert not login.documents
    asyncio.run(scenario())


def test_hidden_button_and_repeated_action_do_not_allocate_handles():
    async def scenario():
        button = SimpleNamespace(is_visible=AsyncMock(side_effect=[False, True]), element_handle=AsyncMock())
        login = workflow(SimpleNamespace(url='https://chatgpt.com/'), object())
        login.last_action = ('https://chatgpt.com/', 'open')
        login._same_document = AsyncMock(return_value=True)
        await login._submit(button, expected_url=login.page.url)
        await login._submit(button, expected_url=login.page.url)
        button.element_handle.assert_not_awaited()
        await login.aclose()
    asyncio.run(scenario())


def test_navigation_before_control_resolution_preserves_original_origin_binding():
    async def scenario():
        button = SimpleNamespace(is_visible=AsyncMock(), element_handle=AsyncMock())
        login = workflow(SimpleNamespace(url='https://other.example.test/'), object())
        await login._submit(button, expected_url='https://chatgpt.com/')
        button.is_visible.assert_not_awaited()
        button.element_handle.assert_not_awaited()
        await login.aclose()
    asyncio.run(scenario())


def test_firstparty_cancellation_drains_assistance_and_documents(firstparty_pages):
    async def check(session, entered):
        login = workflow(session.page, session.context, session.browser)
        blocked = asyncio.Event()
        async def advance():
            blocked.set()
            await asyncio.Future()
        login.advance = advance
        task = asyncio.create_task(authenticate_first_party(login, session))
        await blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await login.aclose()
        assert login.ready.cancelled() and not login.documents
        assert not entered
    asyncio.run(firstparty_pages(check))


def test_accepted_callback_interrupts_blocked_firstparty_assistance(firstparty_pages):
    async def check(session, entered):
        login = workflow(session.page, session.context, session.browser)
        callback = asyncio.get_running_loop().create_future()
        blocked, drained = asyncio.Event(), asyncio.Event()
        async def advance():
            blocked.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()
        login.advance = advance
        task = asyncio.create_task(authenticate_first_party(login, session, callback=callback))
        await blocked.wait()
        callback.set_result({'code': 'synthetic'})
        await task
        assert drained.is_set() and not login.ready.done() and not entered
        await login.aclose()
    asyncio.run(firstparty_pages(check))


def test_already_accepted_callback_never_navigates_or_starts_assistance():
    async def scenario():
        callback = asyncio.get_running_loop().create_future()
        callback.set_result({'code': 'synthetic'})
        page = SimpleNamespace(goto=AsyncMock(), is_closed=lambda: True)
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: False))
        login = SimpleNamespace(ready=asyncio.get_running_loop().create_future(), advance=AsyncMock())
        await authenticate_first_party(login, session, callback=callback)
        page.goto.assert_not_awaited()
        login.advance.assert_not_awaited()
        assert not login.ready.done()
        login.ready.cancel()
    asyncio.run(scenario())


def test_callback_queued_during_navigation_prevents_the_next_credential_action():
    async def scenario():
        callback = asyncio.get_running_loop().create_future()
        async def goto(*_, **__):
            asyncio.get_running_loop().call_soon(callback.set_result, {'code': 'synthetic'})
        page = SimpleNamespace(goto=goto, is_closed=lambda: False)
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True))
        login = SimpleNamespace(ready=asyncio.get_running_loop().create_future(), advance=AsyncMock())
        await authenticate_first_party(login, session, callback=callback)
        login.advance.assert_not_awaited()
        assert callback.done() and not login.ready.done()
        login.ready.cancel()
    asyncio.run(scenario())


def test_headless_firstparty_challenge_requests_the_existing_visible_retry(firstparty_pages):
    async def check(session, entered):
        await session.page.goto('https://chatgpt.com/')
        await session.page.set_content('<h1>Performing security verification</h1>')
        login = workflow(session.page, session.context, session.browser)
        login.guard = connect.LoginPageGuard(session.page, allow_interaction=False)
        with pytest.raises(connect.AuthError, match='^login_interaction_required$'):
            await login.advance()
        assert not login.ready.done() and not entered
        await login.aclose()
    asyncio.run(firstparty_pages(check))


def test_visible_firstparty_challenge_pauses_without_typing(firstparty_pages):
    async def check(session, entered):
        await session.page.goto('https://chatgpt.com/')
        await session.page.set_content('<h1>Performing security verification</h1>')
        login = workflow(session.page, session.context, session.browser)
        await login.advance()
        assert login.disabled and login.guard.notified
        assert not login.ready.done() and not entered
        await login.aclose()
    asyncio.run(firstparty_pages(check))


def test_authorization_phase_keeps_the_workflow_and_uses_normal_login_actions(monkeypatch):
    async def scenario():
        context = SimpleNamespace(cookies=AsyncMock())
        login = workflow(SimpleNamespace(url='https://auth.openai.com/authorize'), context)
        login.ready.set_result(None)
        login.authorizing = True
        advance = AsyncMock()
        monkeypatch.setattr(LoginWorkflow, 'advance', advance)
        await login.advance()
        advance.assert_awaited_once()
        context.cookies.assert_not_awaited()
        assert login.ready.done() and login.password_value is None
        await login.aclose()
    asyncio.run(scenario())
