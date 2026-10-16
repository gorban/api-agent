import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from api_agent.llm_providers.gpt import connect as c


async def request(uri, target=None, raw=None):
    parsed = urlsplit(uri)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    writer.write(raw or f"GET {target or '/auth/callback?state=s&code=c'} HTTP/1.1\r\nHost: {parsed.netloc}\r\n\r\n".encode())
    await writer.drain()
    writer.write_eof()
    result = await reader.read()
    writer.close()
    await writer.wait_closed()
    return result


@pytest.mark.parametrize('raw', [
    b'POST /auth/callback?state=s&code=c HTTP/1.1\r\n\r\n',
    b'GET /else?state=s&code=c HTTP/1.1\r\n\r\n',
    b'GET http://host/auth/callback?state=s&code=c HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&code=c#x HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&state=s&code=c HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=&code=c HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&code= HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&extra=x HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&code=%xx HTTP/1.1\r\n\r\n',
    b'GET /auth/callback?state=s&code=c HTTP/1.1\r\nInvalid\r\n\r\n',
    b'GET /auth/callback?state=s&code=c HTTP/1.1\r\nContent-Length: 2\r\n\r\n',
    b'GET /auth/callback?state=s&code=c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
    b'GET /auth/callback?state=s&code=\xff HTTP/1.1\r\n\r\n',
    b'GET /auth/callback HTTP/1.1\r\nX: ' + b'a'*5000 + b'\r\n\r\n',
    b'incomplete',
])
def test_rejected(raw):
    async def run():
        nonlocal raw
        async with c.CallbackListener() as listener:
            listener.expected_state = "s"
            parsed = urlsplit(listener.redirect_uri)
            if raw.endswith(b'\r\n\r\n'):
                raw = raw[:-2] + f'Host: {parsed.netloc}\r\n\r\n'.encode()
            response = await request(listener.redirect_uri, raw=raw)
            assert b'400 Bad Request' in response
            assert not listener.result.done()
    asyncio.run(run())


def test_success_and_replay():
    async def run():
        async with c.CallbackListener() as listener:
            listener.expected_state = "s"
            assert b'200 OK' in await request(listener.redirect_uri)
            assert await listener.result == {'state':'s', 'code':'c'}
            assert b'400 Bad Request' in await request(listener.redirect_uri)
    asyncio.run(run())


class Store:
    def __init__(self):
        self.saved = []
    def host_id(self): return 'host'
    def load(self, profile): return SimpleNamespace(email='person@example.org', scopes=('chatgpt.tokens.use.direct',))
    def profile_id(self, connection): return 'account'
    def save(self, connection):
        self.saved.append(connection)
        return 'account'
    async def save_serialized(self, connection): return self.save(connection)
    def list(self): return (self.load('account'),)


@pytest.fixture
def lane(monkeypatch):
    async def finish(*args, **kwargs): return object()
    oauth = SimpleNamespace(begin_authorization=lambda host, uri, **kw: SimpleNamespace(authorization_url=uri, state="s"), finish_authorization=finish)
    monkeypatch.setitem(sys.modules, 'api_agent.llm_providers.gpt.oauth', oauth)
    return oauth


@pytest.mark.parametrize('profile', [None, 'account', 'other'])
def test_account(lane, profile):
    async def run():
        tasks = []
        def browser(uri):
            tasks.append(asyncio.create_task(request(uri)))
            return True
        store = Store()
        if profile == 'other':
            with pytest.raises(c.AuthError, match='account_mismatch'):
                await c.connect_account(store, open_browser=browser, profile=profile)
            assert not store.saved
        else:
            assert await c.connect_account(store, open_browser=browser, profile=profile) == 'account'
            assert len(store.saved) == 1
        await asyncio.gather(*tasks)
    asyncio.run(run())


def test_invalid_timeout(lane):
    with pytest.raises(c.AuthError, match='invalid_timeout'):
        asyncio.run(c.connect_account(Store(), timeout=0))


def test_browser_failure(lane):
    with pytest.raises(c.AuthError, match='browser_unavailable'):
        asyncio.run(c.connect_account(Store(), open_browser=lambda _: False))


def test_timeout(lane, monkeypatch):
    original = c.asyncio.timeout
    monkeypatch.setattr(c.asyncio, 'timeout', lambda _: original(.001))
    with pytest.raises(c.AuthError, match='callback_timeout'):
        asyncio.run(c.connect_account(Store(), open_browser=lambda _: True))


def test_login_reports_its_actual_component_limit_until_timeout(lane, monkeypatch):
    from api_agent.providers.progress import progress_sink
    original = c.asyncio.timeout
    monkeypatch.setattr(c.asyncio, 'timeout', lambda _: original(.02))
    rows = []
    async def run():
        with progress_sink(rows.append):
            with pytest.raises(c.AuthError, match='callback_timeout'):
                await c.connect_account(Store(), open_browser=lambda _: True, timeout=180)
    asyncio.run(run())
    assert len(rows) == 2 and rows[0].active and not rows[-1].active
    assert rows[0].id == rows[-1].id and all(row.timeout == 180 and row.name == 'GPT login' for row in rows)


def test_login_enforces_remaining_parent_budget_without_changing_configured_limit(lane):
    from api_agent.providers.progress import progress_sink, report_timeout
    async def run():
        rows = []
        with progress_sink(rows.append):
            async with report_timeout('Parent operation', .2):
                with pytest.raises(c.AuthError, match='callback_timeout'):
                    # The independent guard prevents a broken clamp from hanging.
                    await asyncio.wait_for(c.connect_account(Store(), open_browser=lambda _: True), 1)
        login = [row for row in rows if row.name == 'GPT login']
        assert len(login) == 2 and 0 < login[0].timeout < .2
        assert 0 <= login[0].elapsed < login[0].timeout
        assert login[-1].elapsed >= login[0].timeout
        assert login[-1].timeout == login[0].timeout
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_cancel(lane):
    async def run():
        ready = asyncio.Event()
        def browser(_): ready.set(); return True
        task = asyncio.create_task(c.connect_account(Store(), open_browser=browser, client=object()))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())


def test_cli(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'api_agent.llm_providers.gpt.connections', SimpleNamespace(ConnectionStore=lambda _: Store()))
    async def done(*args, **kwargs): return 'account'
    monkeypatch.setattr(c, 'connect_account', done)
    c.main(['list'])
    c.main(['connect'])
    output = capsys.readouterr().out
    assert 'person@example.org' in output and 'allowed' in output
    async def fail(*args, **kwargs): raise c.AuthError('secret')
    monkeypatch.setattr(c, 'connect_account', fail)
    with pytest.raises(SystemExit): c.main(['connect'])
    assert 'secret' not in capsys.readouterr().err
    with pytest.raises(SystemExit) as error: c.main(['--help'])
    assert error.value.code == 0


def test_error_and_documented_parameters():
    async def run():
        for query in ('state=s&error=denied&error_description=no&error_uri=https%3A%2F%2Fa', 'state=s&code=c&client_id=client&scope=openid'):
            async with c.CallbackListener() as listener:
                listener.expected_state = "s"
                assert b'200 OK' in await request(listener.redirect_uri, target='/auth/callback?' + query)
    asyncio.run(run())


def test_disconnect_cleanup():
    async def run():
        async with c.CallbackListener() as listener:
            listener.expected_state = "s"
            parsed = urlsplit(listener.redirect_uri)
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            await asyncio.sleep(.01)
        assert await reader.read() == b''
        writer.close()
        await writer.wait_closed()
    asyncio.run(run())


def test_exact_oversize(monkeypatch):
    class Reader:
        async def readuntil(self, _): return b'a' * 4097
    class Writer:
        def write(self, value): self.value = value
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass
    async def run():
        async with c.CallbackListener() as listener:
            listener.expected_state = "s"
            writer = Writer()
            await listener._accept(Reader(), writer)
            assert b'400 Bad Request' in writer.value
    asyncio.run(run())


def test_script_entry(monkeypatch):
    import runpy
    monkeypatch.setattr(sys, 'argv', ['connect', '--help'])
    monkeypatch.delitem(sys.modules, c.__name__)
    with pytest.raises(SystemExit) as error:
        runpy.run_module(c.__name__, run_name='__main__')
    assert error.value.code == 0


@pytest.mark.parametrize('timeout', [True, '180', float('nan'), float('inf'), -1, 601])
def test_timeout_bounds(lane, timeout):
    with pytest.raises(c.AuthError, match='invalid_timeout'):
        asyncio.run(c.connect_account(Store(), timeout=timeout))


def test_verification_failure_preserves_store(lane):
    async def fail(*args, **kwargs): raise c.AuthError('invalid_credentials')
    lane.finish_authorization = fail
    async def run():
        tasks = []
        def browser(uri):
            tasks.append(asyncio.create_task(request(uri)))
            return True
        store = Store()
        with pytest.raises(c.AuthError, match='invalid_credentials'):
            await c.connect_account(store, open_browser=browser)
        assert store.saved == []
        await asyncio.gather(*tasks)
    asyncio.run(run())


def test_reconnect_uses_saved_record(lane):
    observed = []
    def begin(host, uri, registration=None, app_name=None):
        observed.append(registration)
        assert app_name == 'api-agent'
        return SimpleNamespace(authorization_url=uri, state="s")
    lane.begin_authorization = begin
    with pytest.raises(c.AuthError, match='browser_unavailable'):
        asyncio.run(c.connect_account(Store(), profile='account', open_browser=lambda _: False))
    assert observed[0].email == 'person@example.org'


def test_connection_passes_application_name_to_authorization(lane):
    observed = []
    def begin(host, uri, **kwargs):
        observed.append(kwargs)
        return SimpleNamespace(authorization_url=uri, state='s')
    lane.begin_authorization = begin
    with pytest.raises(c.AuthError, match='browser_unavailable'):
        asyncio.run(c.connect_account(Store(), app_name='My Agent', open_browser=lambda _: False))
    assert observed == [{'registration': None, 'app_name': 'My Agent'}]


@pytest.mark.parametrize('host', [None, 'foreign', 'duplicate'])
def test_host_rejection(host):
    async def run():
        async with c.CallbackListener() as listener:
            listener.expected_state = 's'
            headers = '' if host is None else ('Host: foreign\r\n' if host == 'foreign' else f'Host: {listener.host}\r\nHost: {listener.host}\r\n')
            raw = ('GET /auth/callback?state=s&code=c HTTP/1.1\r\n' + headers + '\r\n').encode()
            assert b'400 Bad Request' in await request(listener.redirect_uri, raw=raw)
            assert not listener.result.done()
    asyncio.run(run())


def test_wrong_state_then_correct(lane):
    async def run():
        tasks = []
        async def callbacks(uri):
            assert b'400 Bad Request' in await request(uri, target='/auth/callback?state=wrong&code=c&client_id=client')
            assert b'200 OK' in await request(uri, target='/auth/callback?state=s&code=c&client_id=client')
        def browser(uri): tasks.append(asyncio.create_task(callbacks(uri))); return True
        store = Store()
        assert await c.connect_account(store, open_browser=browser) == 'account'
        await asyncio.gather(*tasks)
    asyncio.run(run())


def test_huge_timeout(lane):
    with pytest.raises(c.AuthError, match='invalid_timeout'):
        asyncio.run(c.connect_account(Store(), timeout=10**1000))


def test_exchange_deadline(lane, monkeypatch):
    async def slow(*args, **kwargs): await asyncio.Event().wait()
    lane.finish_authorization = slow
    original = c.asyncio.timeout
    monkeypatch.setattr(c.asyncio, 'timeout', lambda _: original(.02))
    async def run():
        tasks = []
        def browser(uri): tasks.append(asyncio.create_task(request(uri))); return True
        store = Store()
        with pytest.raises(c.AuthError, match='callback_timeout'):
            await c.connect_account(store, open_browser=browser)
        await asyncio.gather(*tasks)
        assert not store.saved
    asyncio.run(run())


@pytest.mark.parametrize('supplied', [False, True])
def test_managed_browser_is_default_and_handles_callback(lane, monkeypatch, supplied):
    from unittest.mock import AsyncMock
    owner = SimpleNamespace(aclose=AsyncMock())
    async def goto(url, **kwargs):
        assert kwargs == {'wait_until': 'domcontentloaded'}
        assert b'200 OK' in await request(url)
    owner.start = AsyncMock(return_value=SimpleNamespace(page=SimpleNamespace(url='https://auth.openai.com/log-in', goto=goto)))
    create = lambda: owner
    monkeypatch.setattr('api_agent.llm_providers.gpt.browser.subscription_browser', create)
    options = {'browser_owner': owner} if supplied else {}
    assert asyncio.run(c.connect_account(Store(), client=object(), **options)) == 'account'
    owner.start.assert_awaited_once()
    owner.aclose.assert_awaited_once()


@pytest.mark.parametrize('closed', ['page', 'browser'])
def test_closing_login_without_callback_ends_promptly_and_disposes(lane, closed):
    from unittest.mock import AsyncMock
    async def scenario():
        navigated = asyncio.Event()
        state = {'page': False, 'browser': False}
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
            navigated.set()
        blocked = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        blocked.first = blocked
        page = SimpleNamespace(url='https://auth.openai.com/log-in', goto=goto, is_closed=lambda: state['page'], get_by_role=lambda *a, **kw: blocked)
        browser = SimpleNamespace(is_connected=lambda: not state['browser'])
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page, browser=browser)),
                                aclose=AsyncMock())
        client = SimpleNamespace(aclose=AsyncMock())
        lane.finish_authorization = AsyncMock()
        store = Store()
        task = asyncio.create_task(c.connect_account(store, browser_owner=owner, client=client))
        await navigated.wait()
        await asyncio.sleep(0)
        state[closed] = True
        with pytest.raises(c.AuthError, match='^browser_closed$'):
            await asyncio.wait_for(task, 5)
        assert not store.saved
        lane.finish_authorization.assert_not_awaited()
        owner.aclose.assert_awaited_once()
        client.aclose.assert_not_awaited()
        parsed = urlsplit(urls[0])
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection(parsed.hostname, parsed.port)
        assert not [other for other in asyncio.all_tasks()
                    if other is not asyncio.current_task() and not other.done()]
    asyncio.run(scenario())


@pytest.mark.parametrize('username,external', [('', False), (' ', False), (42, False), ('person@example.test', True)])
def test_invalid_login_username_has_no_transport_or_browser(lane, monkeypatch, username, external):
    monkeypatch.setattr(c.httpx, 'AsyncClient', lambda: pytest.fail('transport allocated'))
    with pytest.raises(c.AuthError, match='^invalid_login_username$'):
        asyncio.run(c.connect_account(Store(), login_username=username,
                                      open_browser=(lambda _: True) if external else None))


@pytest.mark.parametrize('username,reader', [(None, lambda _: None), ('person@example.test', 1)])
def test_invalid_login_credentials_before_allocation(lane, monkeypatch, username, reader):
    monkeypatch.setattr(c.httpx, 'AsyncClient', lambda: pytest.fail('transport allocated'))
    with pytest.raises(c.AuthError, match='^invalid_login_credentials$'):
        asyncio.run(c.connect_account(Store(), login_username=username, login_credentials=reader))


@pytest.mark.parametrize('outcome', ['success', 'interaction', 'page_error', 'cancel', 'credentials'])
def test_full_login_workflow_callback_and_safe_failure_cleanup(lane, monkeypatch, outcome):
    from unittest.mock import AsyncMock, Mock
    async def run():
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
        session = SimpleNamespace(page=SimpleNamespace(url='https://auth.openai.com/log-in', goto=goto, is_closed=lambda: False),
                                  browser=SimpleNamespace(is_connected=lambda: True))
        owner = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
        reader = AsyncMock()
        async def advance():
            if outcome == 'success':
                assert b'200 OK' in await request(urls[0])
            elif outcome == 'interaction':
                raise c.AuthError('login_interaction_required')
            elif outcome == 'cancel':
                raise asyncio.CancelledError()
            elif outcome == 'credentials':
                raise c.InteractionRequiredError('Password-manager retrieval is unavailable.')
            else:
                raise RuntimeError('private page and credential content')
        workflow = SimpleNamespace(advance=AsyncMock(side_effect=advance), aclose=AsyncMock())
        create = Mock(return_value=workflow)
        monkeypatch.setattr(c, 'LoginWorkflow', create)
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='PERSON@example.test'))
        store = Store()
        options = dict(browser_owner=owner, client=object(), app_name='named-app',
                       login_username='person@example.test', login_credentials=reader, allow_interaction=False)
        if outcome == 'success':
            assert await c.connect_account(store, **options) == 'account'
            assert len(store.saved) == 1
            lane.finish_authorization.assert_awaited_once()
        else:
            error = (asyncio.CancelledError if outcome == 'cancel' else
                     c.InteractionRequiredError if outcome == 'credentials' else c.AuthError)
            code = ('login_interaction_required' if outcome == 'interaction' else
                    'Password-manager retrieval is unavailable.' if outcome == 'credentials' else 'login_step_failed')
            with pytest.raises(error, match=None if outcome == 'cancel' else '^' + code + '$'):
                await c.connect_account(store, **options)
            assert not store.saved
            lane.finish_authorization.assert_not_awaited()
        args, kwargs = create.call_args
        assert args[:4] == (session, 'person@example.test', reader, 'named-app')
        assert isinstance(args[4], c.LoginPageGuard)
        assert kwargs == {'allow_interaction': False}
        workflow.advance.assert_awaited_once()
        workflow.aclose.assert_awaited_once()
        owner.aclose.assert_awaited_once()
        parsed = urlsplit(urls[0])
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection(parsed.hostname, parsed.port)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['success', 'wrong_account', 'field_error', 'cancel'])
def test_username_submission_and_verified_account_binding(lane, outcome):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        from test_gpt_login import Page
        page = Page()
        field, button = page, page
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
        async def click(**kwargs):
            assert kwargs == {'timeout': 1000}
            assert b'200 OK' in await request(urls[0])
        page.goto = goto
        page.click.side_effect = click
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
                                browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
        email = 'different@example.test' if outcome == 'wrong_account' else 'PERSON@example.test'
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email=email))
        if outcome == 'field_error':
            field.fill.side_effect = RuntimeError('private username / upstream page text')
        elif outcome == 'cancel':
            field.fill.side_effect = asyncio.CancelledError()
        store = Store()
        options = dict(browser_owner=owner, client=object(), login_username=' person@example.test ')
        if outcome == 'success':
            assert await c.connect_account(store, **options) == 'account'
            assert len(store.saved) == 1
        else:
            error = asyncio.CancelledError if outcome == 'cancel' else c.AuthError
            code = 'account_mismatch' if outcome == 'wrong_account' else 'login_step_failed'
            with pytest.raises(error, match=None if outcome == 'cancel' else '^' + code + '$'):
                await c.connect_account(store, **options)
            assert not store.saved
        field.fill.assert_awaited_once_with('person@example.test', timeout=1000)
        if outcome in {'success', 'wrong_account'}:
            button.click.assert_awaited_once()
            lane.finish_authorization.assert_awaited_once()
        else:
            button.click.assert_not_awaited()
            lane.finish_authorization.assert_not_awaited()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('close_after_callback', [False, True])
def test_managed_browser_wait_accepts_callback_before_closure(lane, close_after_callback):
    from unittest.mock import AsyncMock
    async def scenario():
        state = {'closed': False}
        requests = []
        async def callback(url):
            await asyncio.sleep(.02)
            assert b'200 OK' in await request(url)
            state['closed'] = close_after_callback
        async def goto(url, **kwargs):
            requests.append(asyncio.create_task(callback(url)))
        blocked = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        blocked.first = blocked
        session = SimpleNamespace(page=SimpleNamespace(url='https://auth.openai.com/log-in', goto=goto, is_closed=lambda: state['closed'], get_by_role=lambda *a, **kw: blocked),
                                  browser=SimpleNamespace(is_connected=lambda: not state['closed']))
        owner = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
        store = Store()
        assert await c.connect_account(store, browser_owner=owner, client=object()) == 'account'
        await asyncio.gather(*requests)
        assert len(store.saved) == 1
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['start', 'navigation'])
def test_managed_browser_failure_is_safe_and_disposes_owner(lane, monkeypatch, stage):
    from unittest.mock import AsyncMock
    error = RuntimeError('private authorization URL')
    page = SimpleNamespace(url='https://auth.openai.com/log-in', goto=AsyncMock(side_effect=error if stage == 'navigation' else None))
    owner = SimpleNamespace(start=AsyncMock(side_effect=error if stage == 'start' else None,
                                          return_value=SimpleNamespace(page=page)), aclose=AsyncMock())
    client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(c.httpx, 'AsyncClient', lambda: client)
    with pytest.raises(c.AuthError, match='^browser_unavailable$'):
        asyncio.run(c.connect_account(Store(), browser_owner=owner))
    owner.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize('end', ['cancel', 'timeout'])
def test_blocked_managed_browser_is_disposed(lane, monkeypatch, end):
    from unittest.mock import AsyncMock
    async def scenario():
        entered = asyncio.Event()
        async def goto(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=SimpleNamespace(url='https://auth.openai.com/log-in', goto=goto))),
                                aclose=AsyncMock())
        original = c.asyncio.timeout
        monkeypatch.setattr(c.asyncio, 'timeout', lambda _: original(.05))
        task = asyncio.create_task(c.connect_account(Store(), client=object(), browser_owner=owner))
        await entered.wait()
        if end == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(c.AuthError, match='callback_timeout'):
                await task
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_repeated_cancel_disposal():
    async def run():
        release = asyncio.Event()
        async def cleanup(): await release.wait(); return 'done'
        task = asyncio.create_task(c._dispose(cleanup()))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert release.is_set()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['complete', 'cancel', 'timeout'])
def test_real_connect_serializes_with_active_refresh(lane, tmp_path, outcome):
    from dataclasses import replace
    from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import Connection
    store = ConnectionStore(tmp_path / 'private')
    record = Connection('subject', 'client', store.host_id(), '', 'old', 'refresh', 'id', ('one',), 100)
    key = store.save(record)
    async def scenario():
        started, release, authorized = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests = []
        async def refresh(value, now):
            started.set()
            await release.wait()
            return replace(value, access_token='refreshed', expires_at=200)
        async def finish(*args, **kwargs):
            authorized.set()
            return replace(record, access_token='reconnected', expires_at=300)
        lane.finish_authorization = finish
        def browser(uri):
            requests.append(asyncio.create_task(request(uri)))
            return True
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50)
        refreshing = asyncio.create_task(session())
        await started.wait()
        reconnect = asyncio.create_task(c.connect_account(store, open_browser=browser,
                                                          profile=key, client=object(), timeout=1))
        try:
            await authorized.wait()
            await asyncio.sleep(.03)
            assert not reconnect.done()
            if outcome == 'cancel':
                reconnect.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reconnect
            elif outcome == 'timeout':
                with pytest.raises(c.AuthError, match='callback_timeout'):
                    await reconnect
            release.set()
            assert await refreshing == 'refreshed'
            if outcome == 'complete':
                assert await reconnect == key
                assert store.load(key).access_token == 'reconnected'
            else:
                assert store.load(key).access_token == 'refreshed'
            # A canceled/timed-out waiter cannot retain the profile lock.
            assert await store.save_serialized(replace(record, access_token='after')) == key
        finally:
            release.set()
            await asyncio.gather(refreshing, reconnect, *requests, return_exceptions=True)
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('interactive', [False, True])
def test_observed_login_blocker_requires_human_without_logging_page_contents(interactive, capsys):
    from unittest.mock import AsyncMock, Mock
    blocked = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    blocked.first = blocked
    page = SimpleNamespace(url='https://auth.openai.com/log-in', get_by_role=Mock(return_value=blocked))
    guard = c.LoginPageGuard(page, allow_interaction=interactive)
    if interactive:
        asyncio.run(guard.check())
        asyncio.run(guard.check())
        assert guard.notified
        assert capsys.readouterr().out.count('visible browser') == 1
    else:
        with pytest.raises(c.AuthError, match='^login_interaction_required$'):
            asyncio.run(guard.check())
        assert not capsys.readouterr().out
    call = page.get_by_role.call_args
    assert call.args == ('heading',)
    assert call.kwargs['name'].fullmatch('Oops, an error occurred!')
    assert call.kwargs['name'].fullmatch('Performing security verification')
    assert not call.kwargs['name'].fullmatch('Welcome back')


@pytest.mark.parametrize('closed', [False, True])
def test_login_guard_disconnect_is_safe(closed):
    from unittest.mock import AsyncMock, Mock
    blocked = SimpleNamespace(is_visible=AsyncMock(side_effect=RuntimeError('private page text')))
    blocked.first = blocked
    page = SimpleNamespace(url='https://auth.openai.com/log-in', get_by_role=Mock(return_value=blocked), is_closed=lambda: closed)
    guard = c.LoginPageGuard(page, allow_interaction=True)
    with pytest.raises(c.AuthError, match='^' + ('browser_closed' if closed else 'browser_unavailable') + '$'):
        asyncio.run(guard.check())


@pytest.mark.parametrize('value', [None, 1, 'True'])
def test_invalid_interaction_policy_allocates_nothing(value, monkeypatch):
    monkeypatch.setattr(c.httpx, 'AsyncClient', lambda: pytest.fail('transport allocated'))
    with pytest.raises(c.AuthError, match='^invalid_interaction_policy$'):
        asyncio.run(c.connect_account(Store(), allow_interaction=value))


@pytest.mark.parametrize('closed', ['page', 'browser'])
def test_username_pending_browser_closure_cancels_submission(closed):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        state = {'page': False, 'browser': False}
        async def fill(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        from test_gpt_login import Page
        page = Page()
        page.fill.side_effect = fill
        page.is_closed = lambda: state['page']
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: not state['browser']))
        task = asyncio.create_task(c.submit_login_username(session, 'person@example.test',
                                  guard=SimpleNamespace(check=AsyncMock(), allow_interaction=True)))
        await started.wait()
        state[closed] = True
        with pytest.raises(c.AuthError, match='^browser_closed$'):
            await asyncio.wait_for(task, 5)
        assert stopped.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_headed_verification_resolution_resumes_owned_username_submission(capsys):
    from test_gpt_login import Page
    async def scenario():
        page = Page('Use your security key')
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True))
        guard = c.LoginPageGuard(page, allow_interaction=True)
        task = asyncio.create_task(c.submit_login_username(session, 'person@example.test', guard=guard))
        while not guard.notified:
            await asyncio.sleep(.01)
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        page.heading = 'Welcome back'
        await asyncio.wait_for(task, 5)
        page.fill.assert_awaited_once_with('person@example.test', timeout=1000)
        page.click.assert_awaited_once_with(timeout=1000)
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('visible browser') == 1


@pytest.mark.parametrize('stage', ['before_email', 'after_continue'])
def test_headless_blocked_flow_fails_before_token_exchange_and_drains_all_work(lane, stage):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        from test_gpt_login import Page
        page = Page('Use your security key' if stage == 'before_email' else 'Welcome back')
        page.goto = AsyncMock()
        field, button = page, page
        async def click(**kwargs):
            page.heading = 'Use your security key'
        page.click.side_effect = click
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True))
        owner = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
        store = Store()
        lane.finish_authorization = AsyncMock()
        with pytest.raises(c.AuthError, match='^login_interaction_required$'):
            await asyncio.wait_for(c.connect_account(store, client=object(), browser_owner=owner,
                login_username='person@example.test', allow_interaction=False), 1)
        lane.finish_authorization.assert_not_awaited()
        assert not store.saved
        owner.aclose.assert_awaited_once()
        if stage == 'before_email':
            field.fill.assert_not_awaited()
            button.click.assert_not_awaited()
        else:
            field.fill.assert_awaited_once_with('person@example.test', timeout=1000)
            button.click.assert_awaited_once_with(timeout=1000)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['completed', 'pending', 'cancelled'])
def test_browser_guard_cannot_overturn_completed_callback_but_preserves_cancellation(outcome):
    from api_agent.providers.sessions import wait_browser_result
    async def scenario():
        result = asyncio.get_running_loop().create_future()
        session = SimpleNamespace(page=SimpleNamespace(url='https://auth.openai.com/log-in', is_closed=lambda: False),
                                  browser=SimpleNamespace(is_connected=lambda: True))
        async def guard():
            if outcome != 'pending':
                result.set_result('accepted')
            if outcome == 'cancelled':
                raise asyncio.CancelledError()
            raise c.AuthError('login_interaction_required')
        if outcome == 'completed':
            assert await wait_browser_result(result, session, on_wait=guard) == 'accepted'
        else:
            error = asyncio.CancelledError if outcome == 'cancelled' else c.AuthError
            with pytest.raises(error):
                await wait_browser_result(result, session, on_wait=guard)
        result.cancel()
    asyncio.run(scenario())


def test_accepted_real_callback_interrupts_workflow_credential_lookup(lane, monkeypatch):
    from test_gpt_login import Page
    from unittest.mock import AsyncMock
    async def scenario():
        entered, drained = asyncio.Event(), asyncio.Event()
        page = Page('Enter your password')
        async def goto(uri, **kwargs):
            page.callback_uri = uri
            page.url = 'https://auth.openai.com/password'
        page.goto = goto
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
            browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
        async def lookup(kind):
            assert kind == 'password'
            entered.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()
        monkeypatch.setattr(c, 'LoginPageGuard', lambda *a, **kw: SimpleNamespace(check=AsyncMock()))
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        store = Store()
        task = asyncio.create_task(c.connect_account(store, client=object(), browser_owner=owner,
            timeout=1, login_username='person@example.test', login_credentials=lookup))
        await entered.wait()
        assert b'200 OK' in await request(page.callback_uri)
        assert await asyncio.wait_for(task, 5) == 'account'
        assert drained.is_set()
        assert len(store.saved) == 1
        lane.finish_authorization.assert_awaited_once()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        owner.aclose.assert_awaited_once()
        assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task() and not other.done()]
    asyncio.run(scenario())


@pytest.mark.parametrize('heading', [
    'Use your passkey', 'Sign in with a passkey', 'Use your security key',
    'Insert your security key', 'Check your email', 'Verify your email',
    'Check your phone', 'Verify your phone number', 'Choose a verification method',
    'Choose how to verify', 'Verify your identity',
])
def test_explicit_human_challenge_headings_pause_without_scraping(heading, capsys):
    from unittest.mock import Mock
    blocked = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    blocked.first = blocked
    page = SimpleNamespace(url='https://auth.openai.com/log-in', get_by_role=Mock(return_value=blocked))
    guard = c.LoginPageGuard(page, allow_interaction=True)
    async def scenario():
        assert await guard.check() is True
        assert await guard.check() is True
        expression = page.get_by_role.call_args.kwargs['name']
        assert expression.fullmatch(heading)
        assert not expression.fullmatch('Check your authenticator app')
        assert not expression.fullmatch('Welcome back')
        blocked.is_visible.return_value = False
        assert await guard.check() is False
        assert not guard.notified
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('visible browser') == 1


def test_attended_without_username_monitors_human_challenge(lane):
    from unittest.mock import Mock
    lane.finish_authorization = AsyncMock()
    blocked = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    blocked.first = blocked
    page = SimpleNamespace(url='https://auth.openai.com/log-in', goto=AsyncMock(), is_closed=lambda: False,
                           get_by_role=Mock(return_value=blocked))
    owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
                            browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
    with pytest.raises(c.AuthError, match='^login_interaction_required$'):
        asyncio.run(c.connect_account(Store(), browser_owner=owner, client=object(), allow_interaction=False))
    blocked.is_visible.assert_awaited_once()
    owner.aclose.assert_awaited_once()
    lane.finish_authorization.assert_not_awaited()


@pytest.mark.parametrize('error', [c.AuthError('login_interaction_required'), asyncio.CancelledError(), RuntimeError('private details')])
def test_shared_username_helper_preserves_control_errors_and_bounds_unknown_errors(error):
    from test_gpt_login import Page
    page = Page()
    page.fill.side_effect = error
    session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True))
    guard = SimpleNamespace(check=AsyncMock(), allow_interaction=True)
    expected = asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else c.AuthError
    message = None if isinstance(error, asyncio.CancelledError) else ('login_interaction_required' if isinstance(error, c.AuthError) else 'username_submission_failed')
    with pytest.raises(expected, match=message):
        asyncio.run(c.submit_login_username(session, 'person@example.test', guard=guard))
    page.click.assert_not_awaited()


@pytest.mark.parametrize('challenge', ['heading', 'route'])
@pytest.mark.parametrize('username', [None, 'person@example.test'])
def test_offline_challenge_wait_keeps_real_callback_priority_without_credentials(lane, monkeypatch, username, challenge):
    from playwright.async_api import async_playwright
    async def scenario():
        callbacks, guards = [], []
        def begin(host, uri, **kwargs):
            callbacks.append(uri)
            return SimpleNamespace(authorization_url='https://auth.openai.com/' + ('auth_challenge/passkey' if challenge == 'route' else 'log-in'), state='s')
        lane.begin_authorization = begin
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        original_guard = c.LoginPageGuard
        def guard(*args, **kwargs):
            instance = original_guard(*args, **kwargs)
            guards.append(instance)
            return instance
        monkeypatch.setattr(c, 'LoginPageGuard', guard)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>' + ('Connect your api-agent to ChatGPT' if challenge == 'route' else 'Use your security key') + '</h1><label>Email address<input></label>'
                    '<button onclick="window.clicked=true">Continue</button>')))
                page = await context.new_page()
                owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page, browser=browser)), aclose=AsyncMock())
                store = Store()
                task = asyncio.create_task(c.connect_account(store, client=object(), browser_owner=owner,
                    login_username=username, timeout=10))
                async with asyncio.timeout(5):
                    while not guards or not guards[0].notified:
                        await asyncio.sleep(.01)
                assert await page.locator('input').input_value() == ''
                assert b'200 OK' in await request(callbacks[0])
                assert await asyncio.wait_for(task, 5) == 'account'
                assert len(store.saved) == 1
                assert guards[0].notified
                assert await page.get_by_role('heading').is_visible()
                assert not await page.evaluate('Boolean(window.clicked)')
                owner.aclose.assert_awaited_once()
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['callback', 'cancel', 'mismatch', 'exchange', 'browser_closed', 'timeout'])
def test_final_document_cleanup_receipt_preserves_verified_save_or_primary_error(lane, monkeypatch, outcome):
    from test_gpt_login import Page
    from api_agent.providers.control import CleanupPendingError
    async def scenario():
        submitted = asyncio.Event()
        captured, callbacks, deadlines = [], [], []
        if outcome == 'timeout':
            original_timeout = c.asyncio.timeout
            def controlled_timeout(_):
                scope = original_timeout(None)
                deadlines.append(scope)
                return scope
            monkeypatch.setattr(c.asyncio, 'timeout', controlled_timeout)
        page = Page('Enter your password')
        async def goto(uri, **kwargs):
            callbacks.append(uri)
        page.goto = goto
        page.click.side_effect = lambda **kwargs: submitted.set()
        original = c.LoginWorkflow
        def create(*args, **kwargs):
            login = original(*args, **kwargs)
            captured.append(login)
            return login
        monkeypatch.setattr(c, 'LoginWorkflow', create)
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
            browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
        client = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(c.httpx, 'AsyncClient', lambda: client)
        email = 'other@example.test' if outcome == 'mismatch' else 'person@example.test'
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email=email))
        if outcome == 'exchange':
            lane.finish_authorization.side_effect = c.AuthError('callback_denied')
        store = Store()
        task = asyncio.create_task(c.connect_account(store, browser_owner=owner, timeout=10,
            login_username='person@example.test', login_credentials=AsyncMock(return_value='private-credential')))
        await asyncio.wait_for(submitted.wait(), 5)
        login = captured[0]
        document = login.last_document
        document.dispose.side_effect = RuntimeError('private document disposal diagnostic')
        if outcome == 'cancel':
            task.cancel()
        elif outcome == 'browser_closed':
            page.closed = True
        elif outcome == 'timeout':
            deadlines[0].reschedule(asyncio.get_running_loop().time())
        else:
            assert b'200 OK' in await request(callbacks[0])
        expected = CleanupPendingError if outcome == 'callback' else asyncio.CancelledError if outcome == 'cancel' else c.AuthError
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, 5)
        pending = caught.value if outcome == 'callback' else caught.value.cleanup_error
        assert isinstance(pending, CleanupPendingError)
        assert str(pending) == 'cleanup pending'
        assert pending.owners == (login,)
        assert pending.retry == login.aclose
        assert login.documents == [document]
        assert login.last_document is document
        if outcome != 'callback':
            assert caught.value.retained_owners == (login,)
        if outcome == 'callback':
            assert len(store.saved) == 1
            lane.finish_authorization.assert_awaited_once()
        else:
            assert not store.saved
        owner.aclose.assert_awaited_once()
        client.aclose.assert_awaited_once()
        document.dispose.side_effect = None
        await pending.retry()
        await pending.retry()
        assert login.documents == []
        assert login.last_document is None
        assert document.dispose.await_count == 2
    asyncio.run(scenario())


def test_replacement_document_disposal_receipt_survives_assistance_and_retains_both_handles(lane, monkeypatch):
    from test_gpt_login import Page
    from api_agent.providers.control import CleanupPendingError
    async def scenario():
        submitted = asyncio.Event()
        captured = []
        page = Page('Enter your password')
        page.goto = AsyncMock()
        def click(**kwargs):
            page.heading = 'Check your authenticator app'
            submitted.set()
        page.click.side_effect = click
        original = c.LoginWorkflow
        def create(*args, **kwargs):
            login = original(*args, **kwargs)
            captured.append(login)
            return login
        monkeypatch.setattr(c, 'LoginWorkflow', create)
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
            browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
        task = asyncio.create_task(c.connect_account(Store(), client=object(), browser_owner=owner,
            login_username='person@example.test', login_credentials=AsyncMock(return_value='private-credential')))
        await asyncio.wait_for(submitted.wait(), 5)
        login = captured[0]
        previous = login.last_document
        previous.dispose.side_effect = RuntimeError('private release diagnostic')
        with pytest.raises(CleanupPendingError, match='^cleanup pending$') as caught:
            await asyncio.wait_for(task, 5)
        assert caught.value.owners == (login,)
        assert len(login.documents) == 2
        current = login.last_document
        assert current is not previous
        assert login.documents == [previous, current]
        current.dispose.assert_not_awaited()
        owner.aclose.assert_awaited_once()
        previous.dispose.side_effect = None
        await caught.value.retry()
        assert login.documents == []
        assert login.last_document is None
        current.dispose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('url,expected', [
    ('https://auth.openai.com/auth_challenge/passkey', True),
    ('https://auth.openai.com/auth_challenge/passkey?flow=synthetic#step', True),
    ('http://auth.openai.com/auth_challenge/passkey', False),
    ('https://other.example/auth_challenge/passkey', False),
    ('https://auth.openai.com.evil.test/auth_challenge/passkey', False),
    ('https://auth.openai.com@other.example/auth_challenge/passkey', False),
    ('https://auth.openai.com/auth_challenge/passkey/extra', False),
    ('https://auth.openai.com/auth_challenge/unknown', False),
    ('https://auth.openai.com/consent', False),
])
def test_passkey_route_requires_exact_trusted_authority_and_path(url, expected, capsys):
    from test_gpt_login import Page
    async def scenario():
        page = Page('Connect your api-agent to ChatGPT')
        page.url = url
        guard = c.LoginPageGuard(page, allow_interaction=True)
        assert await guard.check() is expected
        assert await guard.check() is expected
        assert guard.notified is expected
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('human attention') == int(expected)


@pytest.mark.parametrize('interactive', [False, True])
def test_passkey_route_with_consent_controls_cannot_submit_then_resumes_offline(interactive, capsys):
    from playwright.async_api import async_playwright
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>Connect your api-agent to ChatGPT</h1><input type=password>'
                    '<button onclick="window.clicks=(window.clicks||0)+1">Continue</button>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/auth_challenge/passkey')
                credentials = AsyncMock(return_value='private-credential')
                guard = c.LoginPageGuard(page, allow_interaction=interactive)
                login = c.LoginWorkflow(SimpleNamespace(page=page, browser=browser), 'person@example.test',
                                        credentials, 'api-agent', guard, allow_interaction=interactive)
                if interactive:
                    await login.advance()
                    await login.advance()
                else:
                    with pytest.raises(c.AuthError, match='^login_interaction_required$'):
                        await login.advance()
                credentials.assert_not_awaited()
                assert not await page.evaluate('Boolean(window.clicks)')
                assert await page.locator('input').input_value() == ''
                await page.goto('https://auth.openai.com/consent')
                await login.advance()
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                credentials.assert_not_awaited()
                await login.aclose()
            finally:
                await browser.close()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('human attention') == int(interactive)


def test_passkey_route_uses_real_connect_guard_for_single_headless_visible_retry(lane, monkeypatch, capsys):
    from test_gpt_login import Page
    from api_agent.llm_providers.gpt import factory as factory_module
    async def scenario():
        store = Store()
        store.list = lambda: ()
        monkeypatch.setattr(factory_module, 'ConnectionStore', lambda _: store)
        created, pages, guards, callbacks = [], [], [], []
        original_guard = c.LoginPageGuard
        def guard(*args, **kwargs):
            instance = original_guard(*args, **kwargs)
            guards.append(instance)
            return instance
        monkeypatch.setattr(c, 'LoginPageGuard', guard)
        def browser(**options):
            page = Page('Connect your api-agent to ChatGPT')
            async def goto(uri, **kwargs):
                callbacks.append(uri)
                page.url = 'https://auth.openai.com/auth_challenge/passkey'
            page.goto = goto
            pages.append(page)
            owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page,
                browser=SimpleNamespace(is_connected=lambda: True))), aclose=AsyncMock())
            created.append((options, owner))
            return owner
        monkeypatch.setattr(factory_module, 'subscription_browser', browser)
        lane.finish_authorization = AsyncMock(return_value=SimpleNamespace(email='person@example.test'))
        provider = factory_module.factory({'NO_STD_IN': True, 'RUN_UI': False}, None)
        reader = AsyncMock(return_value='person@example.test')
        task = asyncio.create_task(provider.asetup(interactive=False, user_index=0, credentials=reader))
        try:
            async with asyncio.timeout(5):
                while len(guards) != 2 or not guards[-1].notified:
                    await asyncio.sleep(.01)
            assert [options for options, _ in created] == [{'headless': True}, {}]
            for page in pages:
                page.fill.assert_not_awaited()
                page.click.assert_not_awaited()
            reader.assert_awaited_once_with(0, 'username')
            assert b'200 OK' in await request(callbacks[-1])
            session = await asyncio.wait_for(task, 5)
            assert session.profile_id == 'account'
            assert len(store.saved) == 1
            assert pages[-1].url == 'https://auth.openai.com/auth_challenge/passkey'
        finally:
            await provider.aclose()
        assert len(created) == 2
        created[0][1].aclose.assert_awaited()
        created[1][1].aclose.assert_awaited()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('Opening visible Chromium') == 1


def test_passkey_route_arriving_after_consent_controls_are_pinned_pauses_same_document(capsys):
    from playwright.async_api import async_playwright, ElementHandle
    from unittest.mock import patch
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.set_offline(True)
                await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=(
                    '<h1>Connect your api-agent to ChatGPT</h1>'
                    '<button onclick="window.clicks=(window.clicks||0)+1">Continue</button>')))
                page = await context.new_page()
                await page.goto('https://auth.openai.com/consent')
                guard = c.LoginPageGuard(page, allow_interaction=True)
                reader = AsyncMock()
                login = c.LoginWorkflow(SimpleNamespace(page=page, browser=browser), 'person@example.test',
                                        reader, 'api-agent', guard, allow_interaction=True)
                original = ElementHandle.evaluate_handle
                async def capture_then_change_route(handle, *args, **kwargs):
                    document = await original(handle, *args, **kwargs)
                    await page.evaluate('history.pushState({}, "", "/auth_challenge/passkey")')
                    return document
                with patch.object(ElementHandle, 'evaluate_handle', capture_then_change_route):
                    await login.advance()
                assert guard.notified
                assert not await page.evaluate('Boolean(window.clicks)')
                assert login.last_action is None
                reader.assert_not_awaited()
                await page.evaluate('history.pushState({}, "", "/consent")')
                await login.advance()
                await login.advance()
                assert await page.evaluate('window.clicks') == 1
                await login.aclose()
            finally:
                await browser.close()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('human attention') == 1
