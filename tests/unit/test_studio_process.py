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
    popen = process.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    async def run():
        with pytest.raises(TimeoutError):
            await launcher(timeout=.1).start((sys.executable, '-c', 'import time; time.sleep(30)'),
                cwd='.', host='127.0.0.1', port=server.server_port, scheme='http', env={})
        assert children[0].poll() is not None
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.head(f'http://127.0.0.1:{server.server_port}/')
        assert response.headers['X-UI-Launch-Token'] == 'foreign-host-token'
    try:
        asyncio.run(run())
    finally:
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
