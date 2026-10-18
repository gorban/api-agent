"""Controlled local process and HTTP readiness ownership."""
import asyncio
import json
from pathlib import Path
import shutil
import socket
import ssl
from types import SimpleNamespace

import httpx
import pytest

from api_agent.providers.contracts import SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError
from api_agent.ui import process
from api_agent.ui.provider import app_directory


def launcher(**kwargs):
    return process.StudioProcesses(ssl_context=ssl.create_default_context(), **kwargs)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_actual_node_http_readiness_and_owned_disposal():
    async def run():
        selected_port = port()
        node = shutil.which('node')
        assert node is not None
        env = {'UI_LISTENER_URL': f'http://127.0.0.1:{selected_port}',
               'AGENT_DJANGO_URL': 'http://127.0.0.1:8443'}
        owned = await launcher().start((node, str(app_directory() / 'launch.mjs')),
            cwd=app_directory(), host='127.0.0.1', port=selected_port, scheme='http', env=env)
        try:
            assert owned.process.poll() is None
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(f'http://127.0.0.1:{selected_port}/config.json')
            assert json.loads(response.content) == {'apiOrigin': 'http://127.0.0.1:8443'}
        finally:
            await owned.aclose()
            await owned.aclose()
        assert owned.process.poll() is not None
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [dict(timeout=0), dict(timeout=float('inf')), dict(cleanup_timeout=0)])
def test_invalid_timeout(kwargs):
    with pytest.raises(ValueError, match='timeout'):
        launcher(**kwargs)


@pytest.mark.parametrize('host,port_value,scheme', [('remote.invalid', 8000, 'http'), ('192.0.2.1', 8000, 'http'), ('localhost', 0, 'http'), ('localhost', 8000, 'ftp')])
def test_listener_rejected_before_spawn(host, port_value, scheme, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('spawn before listener validation')
    monkeypatch.setattr(process.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError):
        asyncio.run(launcher().start(('node',), cwd='.', host=host, port=port_value, scheme=scheme, env={}))


class Child:
    def __init__(self, *, exit_on='terminate'):
        self.exit_on, self.code, self.terminations, self.kills = exit_on, None, 0, 0
    def poll(self):
        return self.code
    def terminate(self):
        self.terminations += 1
        if self.exit_on == 'terminate':
            self.code = 0
    def kill(self):
        self.kills += 1
        if self.exit_on == 'kill':
            self.code = -9


@pytest.mark.parametrize('exit_on', ['terminate', 'kill'])
def test_direct_child_disposal_escalation_is_idempotent(exit_on):
    async def run():
        child = Child(exit_on=exit_on)
        owned = process.OwnedStudioProcess(child, timeout=.08)
        await owned.aclose()
        await owned.aclose()
        assert child.terminations == 1
        assert child.kills == (1 if exit_on == 'kill' else 0)
    asyncio.run(run())


def test_failed_disposal_retains_retry_without_resignalling():
    async def run():
        child = Child(exit_on='never')
        owned = process.OwnedStudioProcess(child, timeout=.04)
        with pytest.raises(CleanupPendingError) as pending:
            await owned.aclose()
        assert pending.value.owners == (owned,)
        with pytest.raises(CleanupPendingError):
            await pending.value.retry()
        assert (child.terminations, child.kills) == (1, 1)
        child.code = 0
        await pending.value.retry()
        assert (child.terminations, child.kills) == (1, 1)
    asyncio.run(run())


def test_cancelling_disposal_waiter_does_not_cancel_owned_cleanup():
    async def run():
        child = Child(exit_on='kill')
        owned = process.OwnedStudioProcess(child, timeout=.08)
        task = asyncio.create_task(owned.aclose())
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owned.aclose()
        assert child.code == -9
    asyncio.run(run())


class Client:
    def __init__(self, response):
        self.response = response
        self.urls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def head(self, url):
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.parametrize('response', [httpx.Response(200, headers={'X-UI-Launch-Token': 'wrong'}), httpx.Response(503, headers={'X-UI-Launch-Token': 'a' * 43}), httpx.ConnectError('safe')])
def test_wrong_readiness_or_connection_failure_times_out_and_drains(response, monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    client = Client(response)
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        with pytest.raises(TimeoutError):
            await launcher(timeout=.04).start(('node',), cwd='.', host='::1', port=8000, scheme='https', env={})
        assert client.urls[0] == 'https://[::1]:8000/'
        assert child.code == 0
    asyncio.run(run())


def test_spawn_failure_is_safe(monkeypatch):
    def spawn(*args, **kwargs):
        raise OSError('secret')
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    with pytest.raises(SetupPrerequisiteError, match='Studio could not start Node.js'):
        asyncio.run(launcher().start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))


def test_early_process_exit_rejects_readiness(monkeypatch):
    child = Child()
    child.code = 1
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    with pytest.raises(SetupPrerequisiteError, match='exited'):
        asyncio.run(launcher().start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))


@pytest.mark.parametrize('cancel', [False, True])
def test_failed_start_retains_pending_disposal(cancel, monkeypatch):
    async def run():
        child = Child(exit_on='never')
        monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
        client = Client(httpx.Response(200))
        monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
        task = asyncio.create_task(launcher(timeout=.04, cleanup_timeout=.04).start(
            ('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))
        await asyncio.sleep(.01)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CleanupPendingError) as error:
            await task
        pending = error.value.cleanup_pending if cancel else error.value
        assert isinstance(pending, CleanupPendingError)
        assert child.code is None
        child.code = 0
        await pending.retry()
    asyncio.run(run())


def test_actual_http_wrong_identity_never_admits_foreign_server(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import sys
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('X-UI-Launch-Token', 'foreign-host-token')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    children = []
    popen, actual_client = process.subprocess.Popen, httpx.AsyncClient
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    async def run():
        async with actual_client(trust_env=False) as real:
            class VerifiedForeign(Client):
                async def head(self, url):
                    if self.urls:
                        raise TimeoutError('Controlled retry cutoff after the foreign response')
                    response = await real.head(url)
                    assert response.headers['X-UI-Launch-Token'] == 'foreign-host-token'
                    self.urls.append(url)
                    return response
            client = VerifiedForeign(None)
            monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
            # The response is observed before the controlled cutoff; this larger
            # outer budget is only a safety watchdog, not the identity proof.
            with pytest.raises(TimeoutError):
                await launcher(timeout=2).start((sys.executable, '-c', 'import time; time.sleep(30)'),
                    cwd='.', host='127.0.0.1', port=server.server_port, scheme='http', env={})
            assert len(client.urls) == 1 and children[0].poll() is not None
            response = await real.head(f'http://127.0.0.1:{server.server_port}/')
            assert response.headers['X-UI-Launch-Token'] == 'foreign-host-token'
    try:
        asyncio.run(run())
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join()


def test_readiness_cancellation_drains_real_owned_child(monkeypatch):
    import sys
    children = []
    popen = process.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    async def run():
        task = asyncio.create_task(launcher().start((sys.executable, '-c', 'import time; time.sleep(30)'),
            cwd='.', host='127.0.0.1', port=port(), scheme='http', env={}))
        await asyncio.sleep(.03)
        assert children[0].poll() is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].poll() is not None
    asyncio.run(run())


def test_composition_supplies_default_and_accepts_explicit_services(tmp_path):
    from api_agent.api.composition import compose_runtime
    context = SimpleNamespace(pack_id='demo', owner_root=tmp_path, identity_factory_path=None)
    runtime = compose_runtime(context, {'UI_PROVIDER': 'included'}, callback_origin='https://localhost:8000', required_kinds={'ui'})
    assert isinstance(runtime.services['ui_processes'], process.StudioProcesses)
    injected = object()
    backend = SimpleNamespace(aclose=None)
    explicit = compose_runtime(context, {}, callback_origin='https://localhost:8000', required_kinds=(),
                               ui_processes=injected, studio_backend=backend, ui_access_token='a' * 32)
    assert explicit.services['ui_processes'] is injected
    assert explicit.services['studio_backend'] is backend
    assert explicit.services['ui_access_token'] == 'a' * 32
    async def run():
        await runtime.aclose()
        await explicit.aclose()
    asyncio.run(run())


def test_composition_redacts_ephemeral_access_capability_with_content_enabled(tmp_path):
    from api_agent.api.composition import compose_runtime
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    token = 'ephemeral_local_ui_capability_value'
    config = ConfigResult({'LOG_CONTENT': True},
        {'LOG_CONTENT': ResolvedValue(True, 'cli', True)}, (), (), ())
    runtime = compose_runtime(SimpleNamespace(pack_id='demo', owner_root=tmp_path), config,
        callback_origin='https://localhost:8443', ui_access_token=token)
    assert runtime.emissions.policy.allow_content is True
    exported = runtime.emissions.export({'message': 'visible content ' + token})
    assert exported['message'] == 'visible content [REDACTED]'
    assert token not in json.dumps(exported)
    asyncio.run(runtime.aclose())


def test_owned_parent_writer_closes_once_even_after_child_already_exited():
    from unittest.mock import Mock
    async def run():
        child = Child()
        child.code = 0
        child.stdin = SimpleNamespace(close=Mock())
        owned = process.OwnedStudioProcess(child)
        await owned.aclose()
        await owned.aclose()
        child.stdin.close.assert_called_once()
        assert child.terminations == child.kills == 0
    asyncio.run(run())


def test_writer_close_failure_retains_owner_and_retries_before_signalling():
    from unittest.mock import Mock
    async def run():
        child = Child()
        child.stdin = SimpleNamespace(close=Mock(side_effect=[OSError('controlled pipe close'), None]))
        owned = process.OwnedStudioProcess(child)
        with pytest.raises(CleanupPendingError) as pending:
            await owned.aclose()
        assert child.terminations == child.kills == 0
        await pending.value.retry()
        assert child.stdin.close.call_count == 2
        assert child.terminations == 1
        await owned.aclose()
        assert child.stdin.close.call_count == 2
    asyncio.run(run())


def test_actual_node_parent_pipe_eof_stops_listener_without_signal():
    async def run():
        selected_port = port()
        owned = await launcher().start((shutil.which('node'), str(app_directory() / 'launch.mjs')),
            cwd=app_directory(), host='127.0.0.1', port=selected_port, scheme='http',
            env={'UI_LISTENER_URL': f'http://127.0.0.1:{selected_port}'})
        try:
            assert owned.process.poll() is None
            owned.process.stdin.close()
            await owned._wait(3)
            assert owned.process.returncode == 0
        finally:
            await owned.aclose()
        assert not owned._terminated and not owned._killed
    asyncio.run(run())


@pytest.mark.parametrize('matching', [False, True])
def test_consumed_probe_deadline_cannot_retry_or_admit_a_late_response(monkeypatch, matching):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    class Consumed(Client):
        def __init__(self):
            super().__init__(None)
            self.cancelled = False
        async def __aexit__(self, kind, error, traceback):
            pass
        async def head(self, url):
            self.urls.append(url)
            if len(self.urls) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
            else:
                child.code = 0  # A later natural exit cannot replace the timeout.
            return httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43 if matching else 'foreign'})
    client = Consumed()
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        owned = None
        try:
            with pytest.raises(TimeoutError):
                owned = await launcher(timeout=.03).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
            assert client.cancelled and len(client.urls) == 1
            assert child.code == 0 and child.terminations == 1
        finally:
            if owned is not None: await owned.aclose()
    asyncio.run(run())


def test_budget_expired_during_client_entry_prevents_any_probe(monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    class ConsumedEntry(Client):
        async def __aenter__(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return self
        async def head(self, url):
            self.urls.append(url)
            child.code = 0
            return httpx.Response(503)
    client = ConsumedEntry(None)
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        with pytest.raises(TimeoutError):
            await launcher(timeout=.03).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        assert client.urls == []
        assert child.terminations == 1
    asyncio.run(run())


def test_consumed_deadline_with_actual_foreign_http_and_owned_child(monkeypatch, record_property):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import sys
    from time import monotonic
    from api_agent.providers import progress
    clock = progress._clock
    offset = 0
    monkeypatch.setattr(progress, '_clock', lambda: clock() + offset)
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('X-UI-Launch-Token', 'foreign-host-token')
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    children = []
    popen, actual_client = process.subprocess.Popen, httpx.AsyncClient
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    async def run():
        async with actual_client(trust_env=False) as real:
            class Consumed(Client):
                async def head(self, url):
                    self.urls.append(url)
                    nonlocal offset
                    response = await real.head(url)
                    if len(self.urls) == 1:
                        # Expire the shared absolute budget only after a real
                        # foreign response was observed, then consume its cancel.
                        offset = 3
                        task = asyncio.current_task()
                        task.cancel()
                        try:
                            await asyncio.sleep(0)
                        except asyncio.CancelledError:
                            task.uncancel()
                    return response
            client = Consumed(None)
            monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
            started = monotonic()
            try:
                with pytest.raises(TimeoutError):
                    await launcher(timeout=2).start((sys.executable, '-c', 'import time; time.sleep(.4)'),
                        cwd='.', host='127.0.0.1', port=server.server_port, scheme='http', env={})
                assert len(client.urls) == 1
                assert children[0].poll() is not None
                response = await real.head(f'http://127.0.0.1:{server.server_port}/')
                assert response.headers['X-UI-Launch-Token'] == 'foreign-host-token'
            finally:
                record_property('elapsed_seconds', monotonic() - started)
                record_property('owned_child_returncode', children[0].poll() if children else None)
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=2)
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_probe_deadline_uses_clipped_parent_budget_and_retains_exact_cleanup(monkeypatch):
    from api_agent.providers.progress import component_timeout
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    class Consumed(Client):
        async def head(self, url):
            self.urls.append(url)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43})
    client = Consumed(None)
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        owned = None
        try:
            with pytest.raises(CleanupPendingError) as stopped:
                async with component_timeout('Tighter prerequisite deadline', .03):
                    owned = await launcher(timeout=2).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
            pending = stopped.value
            owner, = pending.owners
            assert isinstance(owner, process.OwnedStudioProcess) and owner.process is child
            assert len(client.urls) == 1 and child.terminations == 0
            # An expired parent can also bound immediate cleanup. Its exact
            # receipt must still finish outside that completed parent scope.
            await pending.retry()
            assert child.code == 0 and child.terminations == 1
        finally:
            if owned is not None: await owned.aclose()
    asyncio.run(run())


def test_probe_http_error_after_consumed_deadline_remains_timeout(monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    class Consumed(Client):
        async def head(self, url):
            self.urls.append(url)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise httpx.ConnectError('Controlled transport failure after deadline')
    client = Consumed(None)
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        with pytest.raises(TimeoutError):
            await launcher(timeout=.03).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        assert len(client.urls) == 1 and child.terminations == 1 and child.code == 0
    asyncio.run(run())


def test_successful_probe_client_exit_cannot_consume_startup_deadline(monkeypatch):
    child=Child();monkeypatch.setattr(process.subprocess,'Popen',lambda *a,**kw:child)
    monkeypatch.setattr(process,'token_urlsafe',lambda _:'a'*43)
    class ConsumedExit(Client):
        async def __aexit__(self,*args):
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:pass
    client=ConsumedExit(httpx.Response(200,headers={'X-UI-Launch-Token':'a'*43}))
    monkeypatch.setattr(process.httpx,'AsyncClient',lambda **kw:client)
    async def run():
        owned=None
        try:
            with pytest.raises(TimeoutError):
                owned=await launcher(timeout=.02).start(('node',),cwd='.',host='localhost',port=8000,scheme='http',env={})
            assert child.terminations==1
        finally:
            if owned is not None:await owned.aclose()
    asyncio.run(run())


def test_consumed_exit_deadline_http_error_keeps_timeout_classification(monkeypatch):
    child=Child();monkeypatch.setattr(process.subprocess,'Popen',lambda *a,**kw:child)
    monkeypatch.setattr(process,'token_urlsafe',lambda _:'a'*43)
    class ConsumedExit(Client):
        async def __aexit__(self,*args):
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:raise httpx.ReadError('synthetic exit error after deadline')
    client=ConsumedExit(httpx.Response(200,headers={'X-UI-Launch-Token':'a'*43}))
    monkeypatch.setattr(process.httpx,'AsyncClient',lambda **kw:client)
    async def run():
        with pytest.raises(TimeoutError):
            await launcher(timeout=.02).start(('node',),cwd='.',host='localhost',port=8000,scheme='http',env={})
        assert child.terminations==1
    asyncio.run(run())


def test_pre_deadline_client_exit_error_preserves_failure_and_owned_disposal(monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    failure = httpx.ReadError('Controlled pre-deadline client exit failure')
    class FailedExit(Client):
        async def __aexit__(self, *args): raise failure
    client = FailedExit(httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43}))
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        with pytest.raises(httpx.ReadError) as stopped:
            await launcher(timeout=2).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        assert stopped.value is failure and child.terminations == 1
    asyncio.run(run())


def test_external_cancellation_during_client_exit_retains_cancellation(monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    async def run():
        entered = asyncio.Event()
        class WaitingExit(Client):
            async def __aexit__(self, *args):
                entered.set()
                await asyncio.Event().wait()
        client = WaitingExit(httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43}))
        monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
        task = asyncio.create_task(launcher(timeout=2).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))
        await entered.wait()
        task.cancel('controlled external cancellation')
        with pytest.raises(asyncio.CancelledError) as stopped: await task
        assert str(stopped.value) == 'controlled external cancellation'
        assert child.terminations == 1 and child.code == 0
    asyncio.run(run())


def test_original_external_cancel_survives_later_expiry_during_client_disposal(monkeypatch):
    child=Child();monkeypatch.setattr(process.subprocess,'Popen',lambda *a,**kw:child)
    async def run():
        entered=asyncio.Event()
        class SlowExit(Client):
            async def head(self,url):
                entered.set();await asyncio.Event().wait()
            async def __aexit__(self,*args):
                assert isinstance(args[1],asyncio.CancelledError)
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:pass
        monkeypatch.setattr(process.httpx,'AsyncClient',lambda **kw:SlowExit(None))
        task=asyncio.create_task(launcher(timeout=.04).start(('node',),cwd='.',host='localhost',port=8000,scheme='http',env={}))
        await entered.wait();task.cancel('original-external-cancel')
        with pytest.raises(asyncio.CancelledError) as interrupted:await task
        assert interrupted.value.args==('original-external-cancel',)
        assert child.code==0 and child.terminations==1
    asyncio.run(run())


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_expired_client_disposal_preserves_process_interruption(monkeypatch, kind):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    failure = kind('controlled process interruption')
    class InterruptedExit(Client):
        async def __aexit__(self, *args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure
    client = InterruptedExit(httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43}))
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
    async def run():
        with pytest.raises(kind) as stopped:
            await launcher(timeout=.02).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        assert stopped.value is failure
        assert child.terminations == 1 and child.code == 0
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['entry', 'head', 'exit'])
@pytest.mark.parametrize('translate_error', [False, True])
def test_consumed_external_cancel_never_admits_readiness(monkeypatch, stage, translate_error):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    async def run():
        entered = asyncio.Event()
        async def consume():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if translate_error:
                    raise httpx.ReadError('controlled consumed interruption')
        class Consumed(Client):
            async def __aenter__(self):
                if stage == 'entry': await consume()
                return self
            async def head(self, url):
                self.urls.append(url)
                if stage == 'head' and len(self.urls) == 1: await consume()
                return httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43})
            async def __aexit__(self, *args):
                if stage == 'exit': await consume()
        client = Consumed(None)
        monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: client)
        task = asyncio.create_task(launcher(timeout=2).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))
        await entered.wait()
        task.cancel('external message consumed by transport')
        with pytest.raises(asyncio.CancelledError) as stopped:
            await task
        assert stopped.value.args == ()  # The transport lost the original message.
        assert task.cancelling() == 1  # Ownership code never clears caller state.
        assert len(client.urls) == (0 if stage == 'entry' else 1)
        assert child.terminations == 1 and child.code == 0
    asyncio.run(run())


def test_consumed_external_cancel_retains_exact_failed_cleanup(monkeypatch):
    child = Child(exit_on='never')
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    async def run():
        entered = asyncio.Event()
        class Consumed(Client):
            async def head(self, url):
                entered.set()
                try: await asyncio.Event().wait()
                except asyncio.CancelledError: raise httpx.ReadError('controlled consumed cancellation')
        monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: Consumed(None))
        task = asyncio.create_task(launcher(timeout=2, cleanup_timeout=.03).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={}))
        await entered.wait()
        task.cancel('lost message')
        with pytest.raises(asyncio.CancelledError) as stopped: await task
        pending = stopped.value.cleanup_pending
        assert isinstance(pending, CleanupPendingError)
        owner, = pending.owners
        assert owner.process is child and task.cancelling() == 1
        child.code = 0
        await pending.retry()
        assert owner._closing.done() and owner._closing.exception() is None
    asyncio.run(run())


def test_preexisting_cancellation_count_does_not_reject_new_start(monkeypatch):
    child = Child()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: 'a' * 43)
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: Client(httpx.Response(200, headers={'X-UI-Launch-Token': 'a' * 43})))
    async def run():
        task = asyncio.current_task()
        task.cancel('preexisting consumed cancellation')
        try: await asyncio.sleep(0)
        except asyncio.CancelledError: pass
        owner = await launcher(timeout=2).start(('node',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        assert task.cancelling() == 1
        await owner.aclose()
        assert child.terminations == 1
    asyncio.run(run())
