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
    def begin(host, uri, registration=None):
        observed.append(registration)
        return SimpleNamespace(authorization_url=uri, state="s")
    lane.begin_authorization = begin
    with pytest.raises(c.AuthError, match='browser_unavailable'):
        asyncio.run(c.connect_account(Store(), profile='account', open_browser=lambda _: False))
    assert observed[0].email == 'person@example.org'


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


def test_browser_helper_success_and_failure(tmp_path, monkeypatch):
    helper = tmp_path / 'browser'
    helper.write_text('#!/bin/sh\nexit 0\n')
    helper.chmod(0o700)
    monkeypatch.setenv('BROWSER', str(helper))
    assert asyncio.run(c._open_browser('http://127.0.0.1/'))
    class Process:
        returncode = 1
        async def communicate(self, value): pass
    async def spawn(*args, **kwargs): return Process()
    monkeypatch.setattr(c.asyncio, 'create_subprocess_exec', spawn)
    assert not asyncio.run(c._open_browser('http://127.0.0.1/'))


def test_blocked_browser_disposed(tmp_path, monkeypatch, lane):
    helper = tmp_path / 'browser'
    pidfile = tmp_path / 'pid'
    helper.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec sleep 30\n')
    helper.chmod(0o700)
    monkeypatch.setenv('BROWSER', str(helper))
    original = c.asyncio.timeout
    monkeypatch.setattr(c.asyncio, 'timeout', lambda _: original(.2))
    with pytest.raises(c.AuthError, match='callback_timeout'):
        asyncio.run(c.connect_account(Store()))
    pid = int(pidfile.read_text())
    # A killed descendant can remain a zombie until the host reaps it.
    import os
    path = f'/proc/{pid}/stat'
    assert not os.path.exists(path) or open(path).read().split()[2] == 'Z'


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
