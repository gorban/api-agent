"""Finite, snapshot-backed loopback host for admitted workflow views."""
import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import quote, unquote_to_bytes, urlsplit

from api_agent.providers.progress import component_timeout


class ViewerRequestError(ValueError):
    pass


async def _close_writer(writer):
    try:
        async with asyncio.timeout(1):
            await writer.drain()
    except (ConnectionError, TimeoutError, asyncio.CancelledError):
        pass
    writer.close()
    try:
        async with asyncio.timeout(1):
            await writer.wait_closed()
    except (ConnectionError, TimeoutError, asyncio.CancelledError):
        pass


def _route(raw):
    if not raw.startswith('/') or raw.startswith('//') or any(c in raw for c in '\\#\r\n'):
        raise ViewerRequestError('Invalid request target')
    parts = urlsplit(raw)
    path = parts.path
    try:
        decoded = unquote_to_bytes(path).decode('utf-8', 'strict')
    except UnicodeError as error:
        raise ViewerRequestError('Invalid path encoding') from error
    if ('//' in decoded or '\\' in decoded
            or any(part in ('', '.', '..') for part in decoded[1:].split('/'))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or quote(decoded, safe='/-._~') != path):
        raise ViewerRequestError('Noncanonical path')
    return path[1:]


def _files(bundle):
    routes = {}
    for item in bundle.files:
        if not isinstance(item.path, str) or _route('/' + item.path) != item.path:
            raise ValueError('Invalid bundle path')
        if (item.path in routes or type(item.body) is not bytes or not isinstance(item.content_type, str)
                or not item.content_type.isascii() or any(ord(char) < 32 or ord(char) == 127
                                                          for char in item.content_type)):
            raise ValueError('Invalid bundle file')
        if not (item.path.startswith('r/') or item.path.startswith('.private_resources/')):
            raise ValueError('Bundle path is outside viewer resources')
        routes[item.path] = (item.content_type, item.body)
    if bundle.entry_path not in routes:
        raise ValueError('Bundle entry is absent')
    return routes


@dataclass
class OwnedViewerHost:
    server: asyncio.AbstractServer
    routes: object
    port: int
    entry_path: str
    clients: set
    _closed: bool = False

    @property
    def entry_url(self):
        return f'http://127.0.0.1:{self.port}/{self.entry_path}'

    async def aclose(self):
        if self._closed:
            return
        async with component_timeout('Workflow viewer host cleanup', 10):
            self.server.close()
            for task in tuple(self.clients):
                task.cancel()
            if self.clients:
                async with asyncio.timeout(5):
                    await asyncio.gather(*tuple(self.clients), return_exceptions=True)
            await self.server.wait_closed()
            self._closed = True


async def start_viewer_host(bundle, *, projection):
    """Bind an ephemeral loopback listener and serve immutable captured bytes only."""
    from api_agent.workflows.viewer_render import verify_bundle_proof
    verify_bundle_proof(bundle, projection)
    routes = _files(bundle)
    async with component_timeout('Workflow viewer host startup', 10):
        host = None

        async def handle(reader, writer):
            task = asyncio.current_task()
            host.clients.add(task)
            try:
                async with component_timeout('Workflow viewer request', 5):
                    line = await reader.readline()
                    if len(line) > 4096 or not line.endswith(b'\r\n'):
                        raise ViewerRequestError('Invalid request line')
                    fields = line[:-2].decode('ascii', 'strict').split(' ')
                    if len(fields) != 3 or fields[2] != 'HTTP/1.1':
                        raise ViewerRequestError('Invalid request line')
                    method, target, _ = fields
                    headers = []
                    while True:
                        row = await reader.readline()
                        if len(row) > 4096 or sum(map(len, headers)) + len(row) > 16384:
                            raise ViewerRequestError('Headers too large')
                        if row == b'\r\n':
                            break
                        if not row.endswith(b'\r\n') or b':' not in row:
                            raise ViewerRequestError('Invalid header')
                        headers.append(row[:-2].decode('ascii', 'strict'))
                    hosts = [value.strip() for row in headers if row.lower().startswith('host:')
                             for value in [row.partition(':')[2]]]
                    if hosts != [f'127.0.0.1:{host.port}']:
                        raise ViewerRequestError('Invalid host')
                    if any(row.lower().startswith(('proxy-', 'forwarded:', 'transfer-encoding:')) for row in headers):
                        raise ViewerRequestError('Disallowed header')
                    path = _route(target)
                    if method not in ('GET', 'HEAD'):
                        status, content_type, body = '405 Method Not Allowed', 'text/plain; charset=utf-8', b'Method not allowed'
                    elif path not in routes:
                        status, content_type, body = '404 Not Found', 'text/plain; charset=utf-8', b'Not found'
                    else:
                        status = '200 OK'
                        content_type, body = routes[path]
                    payload = (f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n'
                               "X-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\n"
                               "Content-Security-Policy: default-src 'self'; object-src 'none'; base-uri 'none'\r\n"
                               'Connection: close\r\n\r\n').encode('ascii')
                    writer.write(payload + (body if method == 'GET' else b''))
            except (ViewerRequestError, UnicodeError, ValueError):
                writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except (ConnectionError, TimeoutError):
                pass
            finally:
                try:
                    await _close_writer(writer)
                finally:
                    host.clients.discard(task)

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        host = OwnedViewerHost(server, MappingProxyType(routes), server.sockets[0].getsockname()[1], bundle.entry_path, set())
        return host
