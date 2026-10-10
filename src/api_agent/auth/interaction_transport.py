"""Capability protected, bounded login-only IPC; never uses report channels."""
import json
import secrets
import socket
import socketserver
import struct
import threading
import time

_LIMIT = 65536


def validate_descriptor(value):
    if (type(value) is not dict or set(value) != {'port', 'token'}
            or type(value['port']) is not int or not 0 < value['port'] < 65536
            or type(value['token']) is not str or len(value['token']) != 64
            or any(c not in '0123456789abcdef' for c in value['token'])):
        raise ValueError('Invalid interaction endpoint')
    return dict(value)


def _receive(stream):
    deadline = time.monotonic() + 2
    def exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Interaction frame deadline expired')
            stream.settimeout(remaining)
            chunk = stream.recv(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)
    size = struct.unpack('!I', exact(4))[0]
    if size > _LIMIT:
        raise ValueError('Interaction frame too large')
    return json.loads(exact(size))


def _send(stream, value):
    raw = json.dumps(value, allow_nan=False).encode()
    if len(raw) > _LIMIT:
        raise ValueError('Interaction frame too large')
    stream.sendall(struct.pack('!I', len(raw)) + raw)


class InteractionServer:
    def __init__(self, interaction):
        self.interaction = interaction
        self.channels = {}
        self.connections = set()
        self.lock = threading.RLock()
        self.closed = False
        token = secrets.token_hex(32)
        owner = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                with owner.lock:
                    if owner.closed:
                        return
                    owner.connections.add(self.request)
                self.request.settimeout(2)
                try:
                    row = _receive(self.request)
                    if not isinstance(row, dict) or not secrets.compare_digest(row.get('token', ''), token):
                        return
                    result = owner._dispatch(row)
                    _send(self.request, result)
                except Exception:
                    # Protocol errors contain no transcript or entered value.
                    return
                finally:
                    with owner.lock:
                        owner.connections.discard(self.request)
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = False
            block_on_close = True
        self.server = Server(('127.0.0.1', 0), Handler)
        self.descriptor = {'port': self.server.server_address[1], 'token': token}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _dispatch(self, row):
        action = row.get('action')
        with self.lock:
            if self.closed:
                return {'action': 'cancel', 'value': ''}
            if action == 'open':
                label = row.get('label')
                if type(label) is not str or len(label) > 200:
                    raise ValueError
                identifier = secrets.token_hex(16)
                self.channels[identifier] = self.interaction.open(label)
                return {'channel': identifier}
            channel = self.channels.get(row.get('channel'))
            if channel is None:
                return {'action': 'cancel', 'value': ''}
            if action == 'close':
                self.channels.pop(row['channel']).close()
                return {}
        if action == 'write':
            value = row.get('text')
            if type(value) is not str:
                raise ValueError
            channel.write(value)
            return {}
        if action == 'read':
            timeout = row.get('timeout')
            if type(timeout) not in (int, float) or not 0 <= timeout <= 1:
                raise ValueError
            reply = channel.read(timeout)
            if reply is None:
                return None
            if reply.action not in {'input', 'retry', 'cancel', 'eof'} or type(reply.value) is not str:
                raise ValueError
            return {'action': reply.action, 'value': reply.value}
        raise ValueError

    def close(self):
        with self.lock:
            self.closed = True
            channels, self.channels = self.channels, {}
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            for channel in channels.values():
                channel.close()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()


class InteractionClient:
    def __init__(self, descriptor):
        self.lock = threading.RLock()
        self.descriptor = validate_descriptor(descriptor)

    def _call(self, **row):
        from .interaction import Reply
        try:
            with socket.create_connection(('127.0.0.1', self.descriptor['port']), timeout=2) as stream:
                _send(stream, dict(row, token=self.descriptor['token']))
                return _receive(stream)
        except (OSError, EOFError, ValueError):
            return {'action': 'cancel', 'value': ''}

    def open(self, label):
        result = self._call(action='open', label=label)
        return _Channel(self, result.get('channel'))


class _Channel:
    def __init__(self, client, identifier):
        self.client, self.identifier = client, identifier
    def write(self, text):
        for offset in range(0, len(text), 12000):
            self.client._call(action='write', channel=self.identifier, text=text[offset:offset + 12000])
    def read(self, timeout):
        from .interaction import Reply
        result = self.client._call(action='read', channel=self.identifier, timeout=min(1, max(0, timeout)))
        if result is None:
            return None
        if (type(result) is not dict or result.get('action') not in {'input', 'retry', 'cancel', 'eof'}
                or type(result.get('value')) is not str):
            return Reply('cancel')
        return Reply(result['action'], result['value'])
    def close(self):
        self.client._call(action='close', channel=self.identifier)


class OwnedInteraction:
    """Cancel only channels belonging to this provider operation."""
    def __init__(self, interaction, *, parent=None):
        self.parent = parent
        self.requests = set()
        self.interaction = interaction
        self.lock = getattr(interaction, 'lock', threading.RLock())
        self.guard = threading.RLock()
        self.channels = set()
        self.cancelled = False
        self.cancel_event = threading.Event()
    def fork(self):
        with self.guard:
            request = OwnedInteraction(self.interaction, parent=self)
            self.requests.add(request)
            if self.cancelled:
                request.cancel()
            return request
    def open(self, label):
        with self.guard:
            if self.cancelled:
                return _CancelledChannel()
            channel = _OwnedChannel(self, self.interaction.open(label))
            self.channels.add(channel)
            return channel
    def cancel(self):
        with self.guard:
            self.cancelled = True
            self.cancel_event.set()
            requests = tuple(self.requests)
        for request in requests:
            request.cancel()
    def close(self):
        self.cancel()
        with self.guard:
            channels, self.channels = self.channels, set()
            requests, self.requests = self.requests, set()
        for request in requests:
            request.close()
        for channel in channels:
            channel.close()
        if self.parent is not None:
            with self.parent.guard:
                self.parent.requests.discard(self)


class _CancelledChannel:
    def write(self, text):
        pass
    def read(self, timeout):
        from .interaction import Reply
        return Reply('cancel')
    def close(self):
        pass


class _OwnedChannel:
    def __init__(self, owner, channel):
        self.owner, self.channel = owner, channel
        self.closed = False
    def write(self, text):
        if not self.closed and not self.owner.cancel_event.is_set():
            self.channel.write(text)
    def read(self, timeout):
        if self.closed or self.owner.cancel_event.is_set():
            from .interaction import Reply
            return Reply('cancel')
        return self.channel.read(timeout)
    def close(self):
        with self.owner.guard:
            if self.closed:
                return
            self.closed = True
            self.owner.channels.discard(self)
        self.channel.close()
