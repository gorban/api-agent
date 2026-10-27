"""Capability protected, bounded login-only IPC; never uses report channels."""
import json
import secrets
import socket
import socketserver
import struct
import threading
import time

_LIMIT = 65536
_NOT_STARTED = object()


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
        self.closed_channels = set()
        self.close_guard = threading.RLock()
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
                if (type(label) is not str or len(label) > 200
                        or len(self.channels) + len(self.closed_channels) >= 4096):
                    raise ValueError
                identifier = row.get('channel', secrets.token_hex(16))
                if (type(identifier) is not str or len(identifier) != 32
                        or any(char not in '0123456789abcdef' for char in identifier)):
                    raise ValueError
                if identifier in self.closed_channels:
                    return {'action': 'cancel', 'value': ''}
                if identifier not in self.channels:
                    self.channels[identifier] = self.interaction.open(label)
                return {'channel': identifier}
            identifier = row.get('channel')
            if action == 'close' and identifier not in self.channels:
                if (type(identifier) is not str or len(identifier) != 32
                        or any(char not in '0123456789abcdef' for char in identifier)):
                    raise ValueError
                if identifier not in self.closed_channels:
                    if len(self.channels) + len(self.closed_channels) >= 4096:
                        raise ValueError
                    self.closed_channels.add(identifier)
                return {'closed': True}
            channel = self.channels.get(identifier)
            if channel is None:
                return {'action': 'cancel', 'value': ''}
            if action == 'check':
                event = getattr(self.interaction, 'cancel_event', None)
                if (getattr(channel, 'closed', False) or event is not None and event.is_set()
                        or not all(callable(getattr(channel, name, None)) for name in ('write', 'read', 'close'))):
                    return {'action': 'cancel', 'value': ''}
                check = getattr(channel, 'check_open', None)
                if check is not None:
                    check()
                return {'open': True}
            if action == 'close':
                channel.close()
                self.channels.pop(identifier)
                self.closed_channels.add(identifier)
                return {'closed': True}
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
        with self.close_guard:
            with self.lock:
                self.closed = True
                channels = tuple(self.channels.items())
                connections = tuple(self.connections)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            failed = False
            try:
                for identifier, channel in channels:
                    try:
                        channel.close()
                    except Exception:
                        failed = True
                    else:
                        with self.lock:
                            self.channels.pop(identifier)
                            self.closed_channels.add(identifier)
            finally:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join()
            if failed:
                raise RuntimeError('Interaction cleanup failed') from None


class InteractionClient:
    def __init__(self, descriptor):
        self.lock = threading.RLock()
        self.descriptor = validate_descriptor(descriptor)

    def _call(self, **row):
        from .interaction import Reply
        started = False
        try:
            with socket.create_connection(('127.0.0.1', self.descriptor['port']), timeout=2) as stream:
                started = True
                _send(stream, dict(row, token=self.descriptor['token']))
                return _receive(stream)
        except (OSError, EOFError, ValueError):
            if row.get('action') == 'open' and not started:
                return _NOT_STARTED
            return {'action': 'cancel', 'value': ''}

    def open(self, label):
        identifier = secrets.token_hex(16)
        result = self._call(action='open', label=label, channel=identifier)
        if result is _NOT_STARTED:
            return _CancelledChannel()
        admitted = type(result) is dict and result == {'channel': identifier}
        return _Channel(self, identifier, admitted=admitted)


class _Channel:
    def __init__(self, client, identifier, *, admitted=True):
        self.client, self.identifier = client, identifier
        self.admitted = admitted
    def check_open(self):
        if not self.admitted:
            raise RuntimeError('Interaction unavailable')
        result = self.client._call(action='check', channel=self.identifier)
        if type(result) is not dict or set(result) != {'open'} or result['open'] is not True:
            raise RuntimeError('Interaction unavailable')
    def write(self, text):
        if not self.admitted:
            return
        for offset in range(0, len(text), 12000):
            self.client._call(action='write', channel=self.identifier, text=text[offset:offset + 12000])
    def read(self, timeout):
        from .interaction import Reply
        if not self.admitted:
            return Reply('cancel')
        result = self.client._call(action='read', channel=self.identifier, timeout=min(1, max(0, timeout)))
        if result is None:
            return None
        if (type(result) is not dict or result.get('action') not in {'input', 'retry', 'cancel', 'eof'}
                or type(result.get('value')) is not str):
            return Reply('cancel')
        return Reply(result['action'], result['value'])
    def close(self):
        result = self.client._call(action='close', channel=self.identifier)
        if type(result) is not dict or set(result) != {'closed'} or result['closed'] is not True:
            raise RuntimeError('Interaction cleanup failed')


class OwnedInteraction:
    """Cancel only channels belonging to this provider operation."""
    def __init__(self, interaction, *, parent=None):
        self.parent = parent
        self.requests = set()
        self.interaction = interaction
        self.lock = getattr(interaction, 'lock', threading.RLock())
        self.guard = threading.RLock()
        self.close_guard = threading.RLock()
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
        with self.close_guard:
            self.cancel()
            with self.guard:
                channels = tuple(self.channels)
                requests = tuple(self.requests)
            failed = False
            for resource in (*requests, *channels):
                try:
                    resource.close()
                except Exception:
                    failed = True
            if failed:
                raise RuntimeError('Interaction cleanup failed') from None
            if self.parent is not None:
                with self.parent.guard:
                    self.parent.requests.discard(self)


class _CancelledChannel:
    def check_open(self):
        raise RuntimeError('Interaction unavailable')
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
    def check_open(self):
        if (self.closed or self.owner.cancel_event.is_set() or self.channel is None
                or getattr(self.channel, 'closed', False)
                or not all(callable(getattr(self.channel, name, None)) for name in ('write', 'read', 'close'))):
            raise RuntimeError('Interaction unavailable')
        check = getattr(self.channel, 'check_open', None)
        if check is not None:
            check()
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
            self.channel.close()
            self.closed = True
            self.owner.channels.discard(self)
