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


@pytest.mark.parametrize('outcome', ['success', 'wrong_account', 'field_error', 'cancel'])
def test_username_submission_and_verified_account_binding(lane, outcome):
    from unittest.mock import AsyncMock, Mock
    async def scenario():
        field = SimpleNamespace(fill=AsyncMock())
        urls = []
        async def goto(url, **kwargs):
            urls.append(url)
        async def click():
            assert b'200 OK' in await request(urls[0])
        button = SimpleNamespace(click=AsyncMock(side_effect=click))
        page = SimpleNamespace(goto=goto, get_by_role=Mock(side_effect=[field, button]))
        owner = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=page)), aclose=AsyncMock())
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
        field.fill.assert_awaited_once_with('person@example.test')
        assert page.get_by_role.call_args_list[0].args == ('textbox',)
        assert page.get_by_role.call_args_list[0].kwargs == {'name': 'Email address', 'exact': True}
        if outcome in {'success', 'wrong_account'}:
            assert page.get_by_role.call_args_list[1].kwargs == {'name': 'Continue', 'exact': True}
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
