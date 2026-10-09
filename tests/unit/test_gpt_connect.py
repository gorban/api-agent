import asyncio
import sys
from types import SimpleNamespace
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
    owner.start = AsyncMock(return_value=SimpleNamespace(page=SimpleNamespace(goto=goto)))
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
        page = SimpleNamespace(goto=goto, is_closed=lambda: state['page'])
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
            await asyncio.wait_for(task, .5)
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


@pytest.mark.parametrize('outcome', ['success', 'interaction', 'page_error', 'cancel'])
def test_full_login_workflow_callback_and_safe_failure_cleanup(lane, monkeypatch, outcome):
    from unittest.mock import AsyncMock, Mock
    async def run():
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
        session = SimpleNamespace(page=SimpleNamespace(goto=goto, is_closed=lambda: False),
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
            else:
                raise RuntimeError('private page and credential content')
        workflow = SimpleNamespace(advance=AsyncMock(side_effect=advance))
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
            error = asyncio.CancelledError if outcome == 'cancel' else c.AuthError
            code = 'login_interaction_required' if outcome == 'interaction' else 'login_step_failed'
            with pytest.raises(error, match=None if outcome == 'cancel' else '^' + code + '$'):
                await c.connect_account(store, **options)
            assert not store.saved
            lane.finish_authorization.assert_not_awaited()
        args, kwargs = create.call_args
        assert args[:4] == (session, 'person@example.test', reader, 'named-app')
        assert isinstance(args[4], c.LoginPageGuard)
        assert kwargs == {'allow_interaction': False}
        workflow.advance.assert_awaited_once()
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
        field = SimpleNamespace(fill=AsyncMock())
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
        async def click(**kwargs):
            assert kwargs == {'timeout': 0}
            assert b'200 OK' in await request(urls[0])
        button = SimpleNamespace(click=AsyncMock(side_effect=click))
        blocked = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        blocked.first = blocked
        def locator(role, **kwargs):
            return blocked if role == 'heading' else field if role == 'textbox' else button
        page = SimpleNamespace(goto=goto, get_by_role=Mock(side_effect=locator), is_closed=lambda: False)
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
            code = 'account_mismatch' if outcome == 'wrong_account' else 'username_submission_failed'
            with pytest.raises(error, match=None if outcome == 'cancel' else '^' + code + '$'):
                await c.connect_account(store, **options)
            assert not store.saved
        field.fill.assert_awaited_once_with('person@example.test', timeout=0)
        textbox_call = next(call for call in page.get_by_role.call_args_list if call.args == ('textbox',))
        assert textbox_call.kwargs == {'name': 'Email address', 'exact': True}
        if outcome in {'success', 'wrong_account'}:
            button_call = next(call for call in page.get_by_role.call_args_list if call.args == ('button',))
            assert button_call.kwargs == {'name': 'Continue', 'exact': True}
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
        session = SimpleNamespace(page=SimpleNamespace(goto=goto, is_closed=lambda: state['closed']),
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
    page = SimpleNamespace(goto=AsyncMock(side_effect=error if stage == 'navigation' else None))
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
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=SimpleNamespace(goto=goto))),
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
    page = SimpleNamespace(get_by_role=Mock(return_value=blocked))
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
    page = SimpleNamespace(get_by_role=Mock(return_value=blocked), is_closed=lambda: closed)
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
        field = SimpleNamespace(fill=AsyncMock(side_effect=fill))
        page = SimpleNamespace(get_by_role=Mock(return_value=field), is_closed=lambda: state['page'])
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: not state['browser']))
        task = asyncio.create_task(c.submit_login_username(session, 'person@example.test',
                                  guard=SimpleNamespace(check=AsyncMock())))
        await started.wait()
        state[closed] = True
        with pytest.raises(c.AuthError, match='^browser_closed$'):
            await asyncio.wait_for(task, .5)
        assert stopped.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_headed_verification_resolution_resumes_owned_username_submission(capsys):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        resolved, started = asyncio.Event(), asyncio.Event()
        state = {'blocked': True}
        async def fill(value, **kwargs):
            started.set()
            await resolved.wait()
            assert value == 'person@example.test'
            assert kwargs == {'timeout': 0}
        field = SimpleNamespace(fill=AsyncMock(side_effect=fill))
        button = SimpleNamespace(click=AsyncMock())
        blocked = SimpleNamespace(is_visible=AsyncMock(side_effect=lambda: state['blocked']))
        blocked.first = blocked
        page = SimpleNamespace(get_by_role=Mock(side_effect=lambda role, **kwargs:
                               blocked if role == 'heading' else field if role == 'textbox' else button),
                               is_closed=lambda: False)
        session = SimpleNamespace(page=page, browser=SimpleNamespace(is_connected=lambda: True))
        guard = c.LoginPageGuard(page, allow_interaction=True)
        task = asyncio.create_task(c.submit_login_username(session, 'person@example.test', guard=guard))
        await started.wait()
        assert not task.done()
        state['blocked'] = False
        resolved.set()
        await asyncio.wait_for(task, .5)
        button.click.assert_awaited_once_with(timeout=0)
        assert guard.notified
    asyncio.run(scenario())
    assert capsys.readouterr().out.count('visible browser') == 1


@pytest.mark.parametrize('stage', ['before_email', 'after_continue'])
def test_headless_blocked_flow_fails_before_token_exchange_and_drains_all_work(lane, stage):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        state = {'blocked': stage == 'before_email'}
        field = SimpleNamespace(fill=AsyncMock())
        async def click(**kwargs):
            state['blocked'] = True
        button = SimpleNamespace(click=AsyncMock(side_effect=click))
        blocked = SimpleNamespace(is_visible=AsyncMock(side_effect=lambda: state['blocked']))
        blocked.first = blocked
        page = SimpleNamespace(goto=AsyncMock(), is_closed=lambda: False,
            get_by_role=Mock(side_effect=lambda role, **kwargs:
                blocked if role == 'heading' else field if role == 'textbox' else button))
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
            field.fill.assert_awaited_once_with('person@example.test', timeout=0)
            button.click.assert_awaited_once_with(timeout=0)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['completed', 'pending', 'cancelled'])
def test_browser_guard_cannot_overturn_completed_callback_but_preserves_cancellation(outcome):
    from api_agent.providers.sessions import wait_browser_result
    async def scenario():
        result = asyncio.get_running_loop().create_future()
        session = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False),
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
        assert await asyncio.wait_for(task, .5) == 'account'
        assert drained.is_set()
        assert len(store.saved) == 1
        lane.finish_authorization.assert_awaited_once()
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        owner.aclose.assert_awaited_once()
        assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task() and not other.done()]
    asyncio.run(scenario())
