"""Capability-protected readiness/catalog IPC; authentication stays backend-local."""
import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
import hmac
import json
import math
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from .contracts import (ContractError, InteractionRequiredError, SessionClosedError,
                        SetupPrerequisiteError, UnattendedCredentialError, ProviderAccessError)
from .progress import (ComponentProgress, component_timeout, independent_timeout_scope,
                       progress_sink, report_timeout, _sink, _scope, TimeoutBudget, _clock)
from .sessions import _Owner, _spawn, _drain, _consume
from .control import CleanupPendingError, OperationalError, KINDS

MAX_FRAME = 262144
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


def _content_frame(evidence):
    """Optional evidence has its own post-redaction bound, never a result gate."""
    if (type(evidence) is not dict or set(evidence) != {'phase', 'content'}
            or evidence['phase'] not in ('request', 'response') or type(evidence['content']) is not dict):
        return None
    try:
        return _encode(dict(version=1, status='evidence', **evidence))
    except ContractError:
        return None


async def _read(reader):
    try:
        return _decode(await reader.readline())
    except (ValueError, OSError):
        raise ContractError('Setup connection unavailable') from None


def _diagnostic(error, boundary):
    """Locations and fixed classifications only: no messages, code or locals."""
    if isinstance(error, OperationalError):
        kind = error.kind if type(error.kind) is str and error.kind in KINDS else 'unknown'
    elif isinstance(error, TimeoutError):
        kind = 'deadline'
    elif isinstance(error, OSError):
        kind = 'unavailable'
    elif isinstance(error, ContractError):
        kind = 'contract'
    else:
        kind = 'unknown'
    rows = [f'{boundary} failure category: {kind}', 'Stack locations (innermost last):']
    root = Path(__file__).parent.parent
    locations = []
    trace = error.__traceback__
    while trace is not None:
        filename = Path(trace.tb_frame.f_code.co_filename)
        location = '<external code>'
        if filename.is_relative_to(root):
            relative = filename.relative_to(root)
            if all(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\.py)?', part) for part in relative.parts):
                location = 'api_agent/' + relative.as_posix()
        locations.append(f'  {location}:{trace.tb_lineno}')
        trace = trace.tb_next
    rows.extend(locations[-8:] or ['  <no captured stack>'])
    return '\n'.join(rows)[:2048]


def _error(error, capability=None):
    category, source, message = 'failed', None, 'Shared authentication setup failed'
    if isinstance(error, ProviderAccessError):
        details = {name: getattr(error, name) for name in ('status', 'code', 'type', 'request_id')}
        details['kind'] = error.kind
        default_execution = 'possibly_executed' if error.kind == 'unknown' else 'not_started'
        if error.execution != default_execution:
            details['execution'] = error.execution
        if error.usage is not None:
            from .inference import usage_to_wire
            details['usage'] = usage_to_wire(error.usage)
        message = error.message if capability is None else error.message.replace(capability, '[redacted]')
        if capability is not None:
            details = {name: value.replace(capability, '[redacted]') if isinstance(value, str) else value
                       for name, value in details.items()}
        return dict(version=1, status='error', category='provider', index_source=None,
                    message=message, details=details)
    if isinstance(error, UnattendedCredentialError):
        category = 'credential'
        original = getattr(error, 'index_source', None)
        source = 'dotenv' if original == 'dotenv' or isinstance(original, str) and original.endswith('/.env') else 'override'
        message = error.detail
    elif isinstance(error, SetupPrerequisiteError):
        category, message = 'prerequisite', str(error)
    elif isinstance(error, InteractionRequiredError):
        category, message = 'interaction', str(error)
    elif error is not None:
        message += '\n\n' + _diagnostic(error, 'Backend')
    if type(message) is not str or not message or len(message) > 4096:
        message = 'Shared setup requires attention.'
    if capability is not None:
        message = message.replace(capability, '[redacted]')
    return dict(version=1, status='error', category=category, index_source=source, message=message)


def _raise_error(frame):
    if frame.get('category') == 'provider':
        if (set(frame) != {'version', 'status', 'category', 'index_source', 'message', 'details'}
                or frame['index_source'] is not None or type(frame['details']) is not dict
                or set(frame['details']) - {'usage', 'execution'} != {'kind', 'status', 'code', 'type', 'request_id'}):
            raise ContractError('Invalid provider response')
        details = frame['details'].copy()
        kind = details.pop('kind')
        execution = details.pop('execution', None)
        try:
            if 'execution' in frame['details'] and (type(execution) is not str or execution not in ('not_started', 'possibly_executed', 'completed')):
                raise ValueError()
            if 'usage' in details:
                from .inference import result_from_wire
                usage = details['usage']
                details['usage'] = result_from_wire({'text': '', 'outcome': 'completed', 'usage': usage}).usage
            error = ProviderAccessError(kind, frame['message'], **details)
            if execution is not None:
                error.execution = execution
        except ValueError:
            raise ContractError('Invalid provider response') from None
        raise error
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
    raise ContractError(frame['message'])


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
        # The listener captures startup's context. Each accepted connection owns
        # its request deadlines after that startup budget has ended.
        with independent_timeout_scope():
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
        keys = {'version', 'capability', 'action', 'timeout'} | ({'worker_id', 'attempt'} if action == 'register' else {'binding'}) | ({'request'} if action == 'complete' else set())
        if action == 'complete' and 'parent_id' in frame:
            keys.add('parent_id')
            parent = frame['parent_id']
            if type(parent) is not str or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', parent) is None:
                raise ContractError('Invalid inference progress ancestry')
        if set(frame) != keys or type(frame['version']) is not int or frame['version'] != 1 or action not in ('register', 'ready', 'describe', 'complete', 'revoke'):
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
        if action == 'complete':
            from .inference import request_from_wire
            request_from_wire(frame['request'])
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

    async def _ready(self, peer, action, request=None):
        peer.waiting = True
        peer.count = 0
        peer.finals.clear()
        try:
            with progress_sink(self._progress):
                if action == 'complete':
                    return await self._registry.complete(peer.binding, request)
                if action == 'describe':
                    return await self._registry.describe(peer.binding)
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
        if action == 'complete':
            from .inference import request_from_wire
            request = request_from_wire(frame['request'])
            ready = _spawn(self._ready(peer, action, request))
        else:
            ready = _spawn(self._ready(peer, action))
        disconnected = _spawn(peer.reader.read(1))
        peer.jobs = (ready, disconnected)
        result = None
        try:
            done, _ = await asyncio.wait({ready, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ContractError('Setup connection disconnected')
            result = await ready
            self._check()
            if action == 'complete':
                from .inference import result_to_wire
                return dict(version=1, status='completed', result=result_to_wire(result))
            if action == 'describe':
                from .catalog import catalog_to_wire
                return dict(version=1, status='catalog', catalog=catalog_to_wire(result))
            return dict(version=1, status='ready')
        finally:
            try:
                if (action == 'complete' and not peer.writer.is_closing()
                        and not disconnected.done()):
                    quote = getattr(self._registry, 'quote_cost', lambda *args: None)(peer.binding, request, result) if result is not None else None
                    if quote is not None:
                        from .costs import validate_quote
                        peer.writer.write(_encode(dict(version=1, status='cost', quote=validate_quote(quote))))
                    exporter = getattr(self._registry, 'export_content', lambda *args: ())
                    try:
                        phases = set()
                        for evidence in exporter(peer.binding, request, result):
                            frame = _content_frame(evidence)
                            if frame is not None and evidence['phase'] not in phases:
                                phases.add(evidence['phase'])
                                peer.writer.write(frame)
                    except Exception:
                        # Content capture is optional; completed result/usage and
                        # mandatory cleanup retain their original behavior.
                        pass
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
                        parent = frame.get('parent_id')
                        scope = None
                        if parent is not None:
                            started = _clock()
                            scope = TimeoutBudget(parent, None, started, started + frame['timeout'], frame['timeout'])
                            self._ids[parent] = parent
                        token = _scope.set(scope) if scope is not None else None
                        try:
                            name = 'Shared inference call' if action == 'complete' else 'Shared setup readiness'
                            async with component_timeout(name, min(self._request_timeout, frame['timeout'])):
                                response = await self._dispatch(peer, frame, action)
                        finally:
                            if token is not None:
                                _scope.reset(token)
                                self._ids.pop(parent, None)
                    except (SetupPrerequisiteError, InteractionRequiredError) as error:
                        response = _error(error, self._capability)
                    except Exception as error:
                        response = _error(error, self._capability)
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
        self._progress_timeouts = {}
        self._progress_components = {}
        self._progress_parents = {}
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
        self.evidence = []
        self.cost_quote = None

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
        try:
            budget = _scope.get()
            if budget is not None:
                # Backend scopes are independent of any one waiter's cancellation.
                # Their display still belongs under this waiter's current scope.
                # Translate the first observed entry budget, not every heartbeat:
                # remote elapsed also covers a component begun before this waiter.
                timeout = event.timeout
                if timeout is not None and budget.remaining is not None:
                    timeouts = self._progress_timeouts
                    if event.id not in timeouts:
                        timeouts[event.id] = min(timeout, max(0, budget.remaining + event.elapsed))
                    timeout = timeouts[event.id]
                event = replace(event, timeout=timeout)
                if event.active:
                    self._progress_components[event.id] = event
                else:
                    self._progress_components.pop(event.id, None)
                # A late waiter can see a child before its parent's next
                # heartbeat. Anchor missing ancestors locally, then reconnect
                # existing children when their real parent arrives or departs.
                parents = {identifier: item.parent_id if item.parent_id in self._progress_components else budget.id
                           for identifier, item in self._progress_components.items()}
                repaired = [replace(item, parent_id=parents[identifier])
                            for identifier, item in self._progress_components.items()
                            if identifier != event.id and self._progress_parents.get(identifier) != parents[identifier]]
                self._progress_parents = parents
                parent = event.parent_id if event.parent_id in self._progress_components else budget.id
                event = replace(event, parent_id=parent)
            else:
                repaired = []
        except (ValueError, TypeError):
            raise ContractError('Invalid setup progress') from None
        callback = _sink.get()
        if callback is not None:
            try:
                callback(event)
                for item in repaired:
                    callback(item)
            except Exception:
                pass

    async def _exchange(self, action, request=None):
        if action == 'complete':
            self.evidence = []
            self.cost_quote = None
        frame = dict(version=1, action=action, timeout=self._timeout, capability=self._descriptor['capability'])
        if action == 'register':
            frame.update(worker_id=self._worker, attempt=1)
        else:
            frame['binding'] = self._binding
        if action == 'complete':
            frame['request'] = request
            budget = _scope.get()
            if budget is not None:
                frame['parent_id'] = budget.id
                remaining = budget.remaining
                if remaining is not None:
                    if remaining <= 0:
                        raise ProviderAccessError('deadline', 'Inference call deadline expired.')
                    frame['timeout'] = min(self._timeout, remaining)
        self._writer.write(_encode(frame))
        await self._writer.drain()
        while True:
            response = await _read(self._reader)
            if type(response.get('version')) is not int or response['version'] != 1:
                raise ContractError('Invalid setup response')
            status = response.get('status')
            if status == 'cost' and action == 'complete':
                from .costs import validate_quote
                if set(response) != {'version', 'status', 'quote'} or self.cost_quote is not None:
                    raise ContractError('Invalid cost response')
                self.cost_quote = validate_quote(response['quote'])
                continue
            if status == 'evidence' and action == 'complete':
                if (set(response) != {'version', 'status', 'phase', 'content'}
                        or response['phase'] not in ('request', 'response')
                        or type(response['content']) is not dict
                        or any(row['phase'] == response['phase'] for row in self.evidence)):
                    raise ContractError('Invalid inference evidence response')
                self.evidence.append({'phase': response['phase'], 'content': response['content']})
                continue
            if status == 'progress' and action in ('ready', 'describe', 'complete'):
                self._progress(response)
                continue
            if status == 'error':
                _raise_error(response)
            if action == 'complete' and status == 'completed':
                if set(response) != {'version', 'status', 'result'}:
                    raise ContractError('Invalid completion response')
                from .inference import result_from_wire
                return result_from_wire(response['result'])
            if action == 'describe' and status == 'catalog':
                if set(response) != {'version', 'status', 'catalog'}:
                    raise ContractError('Invalid catalog response')
                from .catalog import catalog_from_wire
                return catalog_from_wire(response['catalog'])
            expected = {'complete': 'completed', 'describe': 'catalog', 'register': 'registered', 'ready': 'ready', 'revoke': 'revoked'}[action]
            keys = {'version', 'status', 'binding'} if action == 'register' else {'version', 'status'}
            if status != expected or set(response) != keys:
                raise ContractError('Invalid setup response')
            if action == 'register':
                binding = _binding(response['binding'])
                if binding['worker_id'] != self._worker or binding['attempt'] != 1:
                    raise ContractError('Invalid setup registration')
                self._binding = binding
            return

    async def _ensure(self, action='ready', request=None):
        name = 'Worker inference call' if action == 'complete' else 'Worker shared setup'
        async with component_timeout(name, self._timeout):
            self._progress_timeouts.clear()
            self._progress_components.clear()
            self._progress_parents.clear()
            try:
                if self._writer is None:
                    self._reader, self._writer = await asyncio.open_connection(self._descriptor['host'], self._descriptor['port'], limit=MAX_FRAME)
                    await self._exchange('register')
                return await self._exchange(action, request)
            finally:
                self._progress_timeouts.clear()
                self._progress_components.clear()
                self._progress_parents.clear()


    async def _close(self):
        # An expired readiness request must not exhaust owned disposal.
        with independent_timeout_scope():
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
        # Disposal acknowledgement has its own bounded, non-request budget too.
        with independent_timeout_scope():
            async with component_timeout('Worker transport cleanup', self._cleanup_timeout):
                await asyncio.shield(self._closing_task)

    async def _retry_close(self):
        await asyncio.to_thread(self.close)

    def ensure_ready(self):
        return self._run_request('ready')

    def describe(self):
        return self._run_request('describe')

    def complete(self, request):
        from .inference import request_to_wire
        with self._lock:
            self.evidence = []
            self.cost_quote = None
            return self._run_request('complete', request_to_wire(request))

    def _run_request(self, action, request=None):
        with self._lock:
            self._context()
            if self._closed:
                raise SessionClosedError('Setup client is closed')
            if self._loop is None:
                self._thread = threading.get_ident()
                self._loop = asyncio.new_event_loop()
            self._request_task = self._loop.create_task(self._ensure(action, request))
            self._request_task.add_done_callback(_consume)
            try:
                return self._loop.run_until_complete(self._request_task)
            except (SetupPrerequisiteError, InteractionRequiredError, ProviderAccessError):
                raise
            except BaseException as original:
                self._failed = True
                interrupted = isinstance(original, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
                operation_error = ContractError('Shared setup connection failed')
                cause = original if isinstance(original, ContractError) else ContractError(_diagnostic(original, 'Worker transport'))
                operation_error.__cause__ = cause
                try:
                    self.close()
                except CleanupPendingError as pending:
                    pending.operation_error = operation_error
                    if interrupted:
                        original.retained_owners = (self,)
                        raise original from None
                    raise pending from operation_error
                if interrupted:
                    raise original from None
                raise operation_error from cause

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
