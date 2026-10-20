"""Real loopback policy checks for the immutable workflow view host."""
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from hashlib import sha256
from urllib.parse import unquote

import pytest

from api_agent.runtime.viewer_host import start_viewer_host, _files, _close_writer
from api_agent.runtime import viewer_host
from api_agent.providers.progress import component_timeout as original_timeout
from api_agent.workflows.viewer_render import ViewBundle, ViewFile, ViewSource, ViewRoute
from api_agent.workflows.viewer_render import RenderError


@pytest.fixture(autouse=True)
def host_policy_fixture(monkeypatch, request):
    # HTTP policy cases use a one-route synthetic bundle. The two proof tests
    # below exercise the real admission verifier at the host boundary.
    if request.node.name not in {'test_host_rejects_changed_route_bytes_before_binding',
                                 'test_host_rejects_different_admitted_identity_before_binding'}:
        from api_agent.workflows import viewer_render
        monkeypatch.setattr(viewer_render, 'verify_bundle_proof', lambda bundle, projection: None)


def admitted(file):
    logical = unquote(file.path.removeprefix('r/'))
    projection = SimpleNamespace(graph_id='graph', definition_id='definition',
                                 projection_sha256='projection', entry_path=logical,
                                 documents=(SimpleNamespace(logical_path=logical, raw_bytes=file.body),),
                                 resources=())
    content_type = (file.content_type if file.content_type.startswith('text/html;')
                    else 'text/html; charset=utf-8')
    bundled = ViewFile(file.path, content_type, file.body)
    bundle = ViewBundle(file.path, (bundled,), 'graph', 'definition', 'projection',
                        (ViewSource(logical, 'declaration', sha256(file.body).hexdigest(), len(file.body), True),),
                        (ViewRoute(file.path, content_type, sha256(file.body).hexdigest(), len(file.body)),))
    return bundle, projection


async def request(port, request_bytes):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(request_bytes)
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data


def wire(method, target, port, host=None):
    return f'{method} {target} HTTP/1.1\r\nHost: {host or f"127.0.0.1:{port}"}\r\n\r\n'.encode()


def test_serves_only_captured_routes_and_ordinary_query():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=b'<p>ok</p>')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            response = await request(host.port, wire('GET', '/r/base/flow.md.html?view=help', host.port))
            assert response.startswith(b'HTTP/1.1 200 OK')
            assert response.endswith(file.body)
            head = await request(host.port, wire('HEAD', '/r/base/flow.md.html', host.port))
            assert b'Content-Length: 9' in head
            assert not head.endswith(file.body)
            assert (await request(host.port, wire('GET', '/r/base/other.md.html', host.port))).startswith(b'HTTP/1.1 404')
        finally:
            await host.aclose()
            await host.aclose()


    asyncio.run(exercise())
@pytest.mark.parametrize('target', ['/r/base/%2e%2e/x', '/r//base/flow.md.html', '/r/base/%2fflow.md.html', '/r/base/%00x', '/r/base/%ZZ', '/r/base/%FF'])
def test_rejects_noncanonical_paths(target):
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            response = await request(host.port, wire('GET', target, host.port))
            assert response.startswith(b'HTTP/1.1 400')
        finally:
            await host.aclose()


    asyncio.run(exercise())
def test_requires_exact_host_and_limits_methods():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            wrong = await request(host.port, wire('GET', '/' + file.path, host.port, host='evil.example'))
            assert wrong.startswith(b'HTTP/1.1 400')
            denied = await request(host.port, wire('POST', '/' + file.path, host.port))
            assert denied.startswith(b'HTTP/1.1 405')
        finally:
            await host.aclose()
    asyncio.run(exercise())


def test_shutdown_drains_stalled_partial_client():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        reader, writer = await asyncio.open_connection('127.0.0.1', host.port)
        writer.write(b'GET /r/base/flow.md.html HTTP/1.1\r\nHost:')
        await writer.drain()
        await asyncio.wait_for(host.aclose(), timeout=2)
        assert not host.clients
        try:
            assert await reader.read() == b''
        except ConnectionResetError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
    asyncio.run(exercise())


def test_rejects_unadmitted_bundle_routes_before_binding():
    async def exercise():
        outside = SimpleNamespace(path='cfg/secrets.toml', content_type='text/plain', body=b'x')
        with pytest.raises(ValueError, match='outside viewer resources'):
            _files(SimpleNamespace(entry_path=outside.path, files=(outside,)))
    asyncio.run(exercise())


def test_rejects_malformed_bundle_and_missing_entry():
    with pytest.raises(ValueError, match='Invalid bundle path'):
        _files(SimpleNamespace(entry_path='r/base/flow.md.html',
                               files=(SimpleNamespace(path=123, content_type='text/html', body=b'x'),)))
    with pytest.raises(ValueError, match='Noncanonical path'):
        _files(SimpleNamespace(entry_path='r/base/flow.md.html',
                               files=(SimpleNamespace(path='r/base/../flow.md.html', content_type='text/html', body=b'x'),)))
    with pytest.raises(ValueError, match='Invalid bundle file'):
        _files(SimpleNamespace(entry_path='r/base/flow.md.html',
                               files=(SimpleNamespace(path='r/base/flow.md.html', content_type='text/html\r\nInjected: x', body=b'x'),)))
    with pytest.raises(ValueError, match='Bundle entry is absent'):
        _files(SimpleNamespace(entry_path='r/base/missing.md.html', files=()))


def test_rejects_bad_request_syntax_and_proxy_headers():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            malformed = [b'GET /r/base/flow.md.html HTTP/1.0\r\nHost: x\r\n\r\n',
                         b'GET /r/base/flow.md.html HTTP/1.1\n',
                         b'GET /r/base/flow.md.html HTTP/1.1\r\nHost: x\r\nBad\r\n\r\n',
                         b'GET /' + b'x' * 5000 + b' HTTP/1.1\r\nHost: x\r\n\r\n',
                         b'GET /r/base/flow.md.html HTTP/1.1\r\nX-Large: ' + b'x' * 5000 + b'\r\n\r\n',
                         (f'GET /{file.path} HTTP/1.1\r\nHost: 127.0.0.1:{host.port}\r\n'
                          'Proxy-Connection: keep-alive\r\n\r\n').encode()]
            for message in malformed:
                assert (await request(host.port, message)).startswith(b'HTTP/1.1 400')
        finally:
            await host.aclose()
    asyncio.run(exercise())


def test_incomplete_request_has_bounded_deadline(monkeypatch):
    @asynccontextmanager
    async def short_timeout(name, seconds):
        async with original_timeout(name, .05 if name == 'Workflow viewer request' else seconds):
            yield

    monkeypatch.setattr(viewer_host, 'component_timeout', short_timeout)

    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', host.port)
            writer.write(b'GET /r/base/flow.md.html HTTP/1.1\r\nHost:')
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=1) == b''
            writer.close()
            await writer.wait_closed()
        finally:
            await host.aclose()
    asyncio.run(exercise())


def test_shutdown_cancels_nonreading_large_response():
    async def exercise():
        body = b'x' * (8 * 1024 * 1024)
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=body)
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        reader, writer = await asyncio.open_connection('127.0.0.1', host.port)
        writer.write(wire('GET', '/' + file.path, host.port))
        await writer.drain()
        await asyncio.wait_for(host.aclose(), timeout=3)
        assert not host.clients
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
    asyncio.run(exercise())


def test_client_reset_during_response_cleanup_still_closes_writer():
    class ResetWriter:
        closed = False
        async def drain(self):
            raise ConnectionResetError
        def close(self):
            self.closed = True
        async def wait_closed(self):
            raise ConnectionResetError

    writer = ResetWriter()
    asyncio.run(_close_writer(writer))
    assert writer.closed


def test_rejects_duplicate_host_and_absolute_target():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html', body=b'ok')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            duplicate = (f'GET /{file.path} HTTP/1.1\r\nHost: 127.0.0.1:{host.port}\r\n'
                         f'Host: 127.0.0.1:{host.port}\r\n\r\n').encode()
            assert (await request(host.port, duplicate)).startswith(b'HTTP/1.1 400')
            assert (await request(host.port, wire('GET', f'http://127.0.0.1:{host.port}/{file.path}', host.port))).startswith(b'HTTP/1.1 400')
        finally:
            await host.aclose()
    asyncio.run(exercise())


def test_host_rejects_changed_route_bytes_before_binding():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=b'ok')
        bundle, projection = admitted(file)
        forged = replace(bundle, files=(replace(bundle.files[0], body=b'changed'),))
        with pytest.raises(RenderError, match='route digest mismatch'):
            await start_viewer_host(forged, projection=projection)
    asyncio.run(exercise())


def test_host_rejects_different_admitted_identity_before_binding():
    async def exercise():
        file = SimpleNamespace(path='r/base/flow.md.html', content_type='text/html; charset=utf-8', body=b'ok')
        bundle, projection = admitted(file)
        forged = replace(bundle, definition_id='other-definition')
        with pytest.raises(RenderError, match='identity differs'):
            await start_viewer_host(forged, projection=projection)
    asyncio.run(exercise())


def test_host_uses_exact_encoded_declared_route():
    async def exercise():
        file = SimpleNamespace(path='r/base/caf%C3%A9.md.html', content_type='text/html; charset=utf-8', body=b'view')
        bundle, projection = admitted(file)
        host = await start_viewer_host(bundle, projection=projection)
        try:
            good = await request(host.port, wire('GET', '/' + file.path, host.port))
            assert good.startswith(b'HTTP/1.1 200') and good.endswith(b'view')
            bad = await request(host.port, wire('GET', '/r/base/caf%c3%a9.md.html', host.port))
            assert bad.startswith(b'HTTP/1.1 400')
        finally:
            await host.aclose()
    asyncio.run(exercise())
