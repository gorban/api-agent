"""Capability-protected loopback readiness IPC; authentication stays backend-local."""
import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import hmac
import json
import math
import os
import re
import threading
from uuid import uuid4

from .contracts import (ContractError, InteractionRequiredError, SessionClosedError,
                        SetupPrerequisiteError, UnattendedCredentialError)
from .progress import ComponentProgress, component_timeout, progress_sink, report_timeout, _sink
from .sessions import _Owner, _spawn, _drain, _consume
from .control import CleanupPendingError

MAX_FRAME = 16384
_BINDING = {'version', 'operation_id', 'selected_identity', 'worker_id', 'attempt'}
_COMPONENT = {'id', 'name', 'timeout', 'elapsed', 'active', 'parent_id'}
_NAME = 'Shared provider setup'


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ContractError('Invalid setup timeout')
    return value


def _identifier(value):
    if type(value) is not str or not value or len(value) > 512 or not value.isprintable():
        raise ContractError('Invalid setup identifier')
    return value


def _capability(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value) is None:
        raise ContractError('Invalid setup capability')
    return value


def validate_descriptor(descriptor):
    if not isinstance(descriptor, Mapping) or set(descriptor) != {'version', 'host', 'port', 'capability'}:
        raise ContractError('Invalid setup endpoint')
    if (type(descriptor['version']) is not int or descriptor['version'] != 1
            or type(descriptor['host']) is not str or descriptor['host'] != '127.0.0.1' or type(descriptor['port']) is not int
            or not 0 < descriptor['port'] < 65536):
        raise ContractError('Invalid setup endpoint')
    _capability(descriptor['capability'])
    return dict(descriptor)


def _binding(value):
    if not isinstance(value, dict) or set(value) != _BINDING:
        raise ContractError('Invalid setup binding')
    if (type(value['version']) is not int or value['version'] != 1
            or type(value['attempt']) is not int or value['attempt'] < 1):
        raise ContractError('Invalid setup binding')
    for name in ('operation_id', 'selected_identity', 'worker_id'):
        _identifier(value[name])
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError('Invalid setup frame')
        result[key] = value
    return result


def _constant(value):
    raise ValueError


def _decode(raw):
    if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
        raise ContractError('Invalid setup frame')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise ContractError('Invalid setup frame') from None


def _encode(value):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    except (ValueError, TypeError):
        raise ContractError('Invalid setup frame') from None
    if len(raw) > MAX_FRAME:
        raise ContractError('Invalid setup frame')
    return raw


async def _read(reader):
    try:
        return _decode(await reader.readline())
    except (ValueError, OSError):
        raise ContractError('Setup connection unavailable') from None


def _error(error, capability=None):
    category, source, message = 'failed', None, 'Shared authentication setup failed'
    if isinstance(error, UnattendedCredentialError):
        category = 'credential'
        original = getattr(error, 'index_source', None)
        source = 'dotenv' if original == 'dotenv' or isinstance(original, str) and original.endswith('/.env') else 'override'
        message = error.detail
    elif isinstance(error, SetupPrerequisiteError):
        category, message = 'prerequisite', str(error)
    elif isinstance(error, InteractionRequiredError):
        category, message = 'interaction', str(error)
    if type(message) is not str or not message or len(message) > 4096:
        message = 'Shared setup requires attention.'
    if capability is not None:
        message = message.replace(capability, '[redacted]')
    return dict(version=1, status='error', category=category, index_source=source, message=message)


def _raise_error(frame):
    if (set(frame) != {'version', 'status', 'category', 'index_source', 'message'}
            or type(frame['category']) is not str or frame['category'] not in ('failed', 'prerequisite', 'credential', 'interaction')
            or frame['index_source'] not in (None, 'dotenv', 'override')
            or type(frame['message']) is not str or not frame['message'] or len(frame['message']) > 4096):
        raise ContractError('Invalid setup response')
    category = frame['category']
    if category == 'credential':
        raise UnattendedCredentialError(frame['message'], index_source=frame['index_source'])
    if frame['index_source'] is not None:
        raise ContractError('Invalid setup response')
    if category == 'prerequisite':
        raise SetupPrerequisiteError(frame['message'])
    if category == 'interaction':
        raise InteractionRequiredError(frame['message'])
    raise ContractError('Shared authentication setup failed')


@dataclass(eq=False, repr=False)
class _Peer:
    reader: object
    writer: object
    task: object = None
    binding: object = None
    waiting: bool = False
    count: int = 0
    dropping: object = None
    jobs: tuple = ()
    finals: set = field(default_factory=set)


class SetupServer(_Owner):
    def __init__(self, registry, capability, *, timeout=300, cleanup_timeout=5):
        self._request_timeout = _timeout(timeout)
        super().__init__(min(_timeout(cleanup_timeout), self._request_timeout))
        self._registry = registry
        self._capability = _capability(capability)
        self._server = None
        self._starting = None
        self._peers = set()
        self._ids = {}
        self._parents = {}

    def _accept(self, reader, writer):
        peer = _Peer(reader, writer)
        self._peers.add(peer)
        peer.task = _spawn(self._serve(peer))

    async def _start(self):
        self._server = await asyncio.start_server(self._accept, '127.0.0.1', 0, limit=MAX_FRAME)
        if self._closed:
            self._server.close()
            raise SessionClosedError('Setup server is closed')
        return dict(version=1, host='127.0.0.1', port=self._server.sockets[0].getsockname()[1], capability=self._capability)

    async def start(self):
        self._check()
        if self._starting is None:
            self._starting = _spawn(self._start())
        async with component_timeout('Setup transport start', self._timeout):
            descriptor = await asyncio.shield(self._starting)
        self._check()
        return descriptor.copy()

    def _request(self, frame):
        action = frame.get('action')
        keys = {'version', 'capability', 'action', 'timeout'} | ({'worker_id', 'attempt'} if action == 'register' else {'binding'})
        if set(frame) != keys or type(frame['version']) is not int or frame['version'] != 1 or action not in ('register', 'ready', 'revoke'):
            raise ContractError('Invalid setup request')
        capability = _capability(frame['capability'])
        if not hmac.compare_digest(capability, self._capability):
            raise ContractError('Setup capability rejected')
        _timeout(frame['timeout'])
        if action == 'register':
            _identifier(frame['worker_id'])
            if type(frame['attempt']) is not int or frame['attempt'] < 1:
                raise ContractError('Invalid setup attempt')
        else:
            _binding(frame['binding'])
        return action

    def _progress(self, event):
        if self._closed:
            return
        try:
            if not isinstance(event, ComponentProgress):
                raise ValueError
            requested = {event.id} | ({event.parent_id} if event.parent_id is not None else set())
            added = requested - self._ids.keys()
            if len(self._ids) + len(added) > 1024:
                if event.active or event.id not in self._ids:
                    return
                identifier = self._ids[event.id]
                parent = self._parents.get(event.id)
            else:
                for original in added:
                    self._ids[original] = str(uuid4())
                identifier = self._ids[event.id]
                parent = self._ids[event.parent_id] if event.parent_id is not None else None
                self._parents[event.id] = parent
            component = ComponentProgress(identifier, _NAME, event.timeout, event.elapsed, event.active, parent)
            raw = _encode(dict(version=1, status='progress', component=asdict(component)))
            for peer in self._peers:
                final = not event.active and identifier not in peer.finals
                if peer.waiting and (peer.count < 1024 or final) and not peer.writer.is_closing():
                    peer.writer.write(raw)
                    peer.count += 1
                    if not event.active:
                        peer.finals.add(identifier)
        except Exception:
            pass

    async def _ready(self, peer):
        peer.waiting = True
        peer.count = 0
        peer.finals.clear()
        try:
            with progress_sink(self._progress):
                await self._registry.ensure_ready(peer.binding)
        finally:
            peer.waiting = False

    async def _dispatch(self, peer, frame, action):
        if action == 'register':
            if peer.binding is not None:
                raise ContractError('Setup connection already registered')
            peer.binding = _binding(self._registry.register(frame['worker_id'], frame['attempt']))
            return dict(version=1, status='registered', binding=peer.binding)
        if peer.binding is None or frame['binding'] != peer.binding:
            raise ContractError('Setup connection binding rejected')
        if action == 'revoke':
            await self._registry.revoke(peer.binding)
            return dict(version=1, status='revoked')
        ready = _spawn(self._ready(peer))
        disconnected = _spawn(peer.reader.read(1))
        peer.jobs = (ready, disconnected)
        try:
            done, _ = await asyncio.wait({ready, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ContractError('Setup connection disconnected')
            await ready
            self._check()
            return dict(version=1, status='ready')
        finally:
            async with report_timeout('Setup request cancellation', self._timeout) as budget:
                await _drain(peer.jobs, budget.remaining, self._cancel_requested)

    async def _drop(self, peer):
        peer.writer.close()
        if peer.binding is not None:
            await self._registry.revoke(peer.binding)
        async with report_timeout('Setup peer cleanup', self._timeout) as budget:
            await _drain(peer.jobs, budget.remaining, self._cancel_requested)
            try:
                await peer.writer.wait_closed()
            except OSError:
                pass
        self._peers.discard(peer)

    async def _serve(self, peer):
        try:
            while not self._closed:
                async with component_timeout('Setup transport request', self._request_timeout):
                    frame = await _read(peer.reader)
                    action = self._request(frame)
                    try:
                        async with component_timeout('Shared setup readiness', min(self._request_timeout, frame['timeout'])):
                            response = await self._dispatch(peer, frame, action)
                    except (SetupPrerequisiteError, InteractionRequiredError) as error:
                        response = _error(error, self._capability)
                    except Exception:
                        response = _error(None)
                    peer.writer.write(_encode(response))
                    await peer.writer.drain()
                    if action == 'revoke':
                        return
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            peer.waiting = False
            peer.dropping = _spawn(self._drop(peer))
            try:
                async with component_timeout('Setup transport disconnect', self._timeout):
                    await asyncio.shield(peer.dropping)
            except (Exception, asyncio.CancelledError):
                pass

    async def _dispose(self):
        async with component_timeout('Setup transport shutdown', self._timeout):
            if self._starting is not None:
                try:
                    await asyncio.shield(self._starting)
                except Exception:
                    pass
            if self._server is not None:
                self._server.close()
            for peer in tuple(self._peers):
                peer.writer.close()
            if self._server is not None:
                await self._server.wait_closed()
            await _drain([p.task for p in self._peers], self._timeout, self._cancel_requested)
            for peer in tuple(self._peers):
                if peer.dropping is None or peer.dropping.done() and (peer.dropping.cancelled() or peer.dropping.exception() is not None):
                    peer.dropping = _spawn(self._drop(peer))
                await asyncio.shield(peer.dropping)


class SetupClient:
    """Inert synchronous worker facade with one persistent process-local connection."""
    def __init__(self, descriptor, *, worker_id=None, timeout=300, cleanup_timeout=5):
        self._descriptor = validate_descriptor(descriptor)
        self._worker = _identifier(str(uuid4()) if worker_id is None else worker_id)
        self._timeout = _timeout(timeout)
        self._cleanup_timeout = min(_timeout(cleanup_timeout), self._timeout)
        self._pid = os.getpid()
        self._thread = None
        self._loop = self._reader = self._writer = self._binding = None
        self._closed = False
        self._disposed = False
        self._failed = False
        self._closing_task = None
        self._request_task = None
        self._cancel_requested = set()
        self._lock = threading.RLock()

    def _context(self):
        if self._pid != os.getpid():
            raise ContractError('Setup client belongs to another execution context')
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise ContractError('Synchronous setup client requires a non-running caller loop')

    def _progress(self, frame):
        value = frame.get('component')
        if set(frame) != {'version', 'status', 'component'} or not isinstance(value, dict) or set(value) != _COMPONENT or value['name'] != _NAME:
            raise ContractError('Invalid setup progress')
        try:
            event = ComponentProgress(**value)
        except (ValueError, TypeError):
            raise ContractError('Invalid setup progress') from None
        callback = _sink.get()
        if callback is not None:
            try:
                callback(event)
            except Exception:
                pass

    async def _exchange(self, action):
        frame = dict(version=1, action=action, timeout=self._timeout, capability=self._descriptor['capability'])
        if action == 'register':
            frame.update(worker_id=self._worker, attempt=1)
        else:
            frame['binding'] = self._binding
        self._writer.write(_encode(frame))
        await self._writer.drain()
        while True:
            response = await _read(self._reader)
            if type(response.get('version')) is not int or response['version'] != 1:
                raise ContractError('Invalid setup response')
            status = response.get('status')
            if status == 'progress' and action == 'ready':
                self._progress(response)
                continue
            if status == 'error':
                _raise_error(response)
            expected = {'register': 'registered', 'ready': 'ready', 'revoke': 'revoked'}[action]
            keys = {'version', 'status', 'binding'} if action == 'register' else {'version', 'status'}
            if status != expected or set(response) != keys:
                raise ContractError('Invalid setup response')
            if action == 'register':
                binding = _binding(response['binding'])
                if binding['worker_id'] != self._worker or binding['attempt'] != 1:
                    raise ContractError('Invalid setup registration')
                self._binding = binding
            return

    async def _ensure(self):
        async with component_timeout('Worker shared setup', self._timeout):
            if self._writer is None:
                self._reader, self._writer = await asyncio.open_connection(self._descriptor['host'], self._descriptor['port'], limit=MAX_FRAME)
                await self._exchange('register')
            await self._exchange('ready')

    async def _close(self):
        async with component_timeout('Worker setup disconnect', self._cleanup_timeout) as budget:
            if self._request_task is not None:
                await _drain([self._request_task], budget.remaining, self._cancel_requested)
            if self._writer is not None:
                try:
                    if self._binding is not None and not self._failed:
                        await self._exchange('revoke')
                except Exception:
                    pass
                finally:
                    self._writer.close()
                    try:
                        await self._writer.wait_closed()
                    except OSError:
                        pass

    async def _await_close(self):
        async with component_timeout('Worker transport cleanup', self._cleanup_timeout):
            await asyncio.shield(self._closing_task)

    async def _retry_close(self):
        await asyncio.to_thread(self.close)

    def ensure_ready(self):
        with self._lock:
            self._context()
            if self._closed:
                raise SessionClosedError('Setup client is closed')
            if self._loop is None:
                self._thread = threading.get_ident()
                self._loop = asyncio.new_event_loop()
            self._request_task = self._loop.create_task(self._ensure())
            self._request_task.add_done_callback(_consume)
            try:
                self._loop.run_until_complete(self._request_task)
            except (SetupPrerequisiteError, InteractionRequiredError):
                raise
            except BaseException as original:
                self._failed = True
                interrupted = isinstance(original, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
                operation_error = ContractError('Shared setup connection failed')
                try:
                    self.close()
                except CleanupPendingError as pending:
                    pending.operation_error = operation_error
                    if interrupted:
                        original.retained_owners = (self,)
                        raise original from None
                    raise pending from None
                if interrupted:
                    raise original from None
                raise operation_error from None

    def close(self):
        with self._lock:
            self._context()
            self._closed = True
            if self._disposed:
                return
            if self._loop is None:
                self._disposed = True
                return
            if self._closing_task is None or self._closing_task.done() and (self._closing_task.cancelled() or self._closing_task.exception() is not None):
                self._closing_task = self._loop.create_task(self._close())
                self._closing_task.add_done_callback(_consume)
            try:
                self._loop.run_until_complete(self._await_close())
            except Exception:
                raise CleanupPendingError((self,), self._retry_close) from None
            self._disposed = True
            self._loop.close()
