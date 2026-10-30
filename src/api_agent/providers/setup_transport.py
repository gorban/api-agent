"""Capability-protected readiness/catalog IPC; authentication stays backend-local."""
import asyncio
import base64
import binascii
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
import hmac
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from uuid import uuid4

from .contracts import (ContractError, InteractionRequiredError, SessionClosedError,
                        SetupPrerequisiteError, UnattendedCredentialError, ProviderAccessError, SetupRegistrationLimitError)
from .progress import (ComponentProgress, component_timeout, independent_timeout_scope,
                       progress_sink, report_timeout, _sink, _scope, TimeoutBudget, _clock)
from .sessions import _Owner, _spawn, _drain, _consume
from .control import CleanupPendingError, OperationalError, KINDS

MAX_FRAME = 4194304
MAX_CONTENT_FRAME = 131072
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
EVIDENCE_CHUNK = 200 * 1024
_EVIDENCE_FRAGMENT = 48 * 1024
_EVIDENCE_ITEMS = 100000
_MAX_EVIDENCE_ROWS = 128
_MAX_SETUP_PEERS = 128
_MAX_EVIDENCE_TRANSFERS = 1
_PREPARE_CANCEL = threading.local()
_BINDING = {'version', 'operation_id', 'selected_identity', 'worker_id', 'attempt'}
_COMPONENT = {'id', 'name', 'timeout', 'elapsed', 'active', 'parent_id'}
_NAME = 'Shared provider setup'


def _timeout(value):
    if type(value) not in (int, float) or not 0 < value <= 3600 or not math.isfinite(value):
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
    if not isinstance(descriptor, Mapping) or set(descriptor) not in ({'version', 'host', 'port', 'capability'}, {'version', 'host', 'port', 'capability', 'authority_path'}):
        raise ContractError('Invalid setup endpoint')
    if (type(descriptor['version']) is not int or descriptor['version'] != 1
            or type(descriptor['host']) is not str or descriptor['host'] != '127.0.0.1' or type(descriptor['port']) is not int
            or not 0 < descriptor['port'] < 65536):
        raise ContractError('Invalid setup endpoint')
    _capability(descriptor['capability'])
    if 'authority_path' in descriptor:
        path = descriptor['authority_path']
        if type(path) is not str or not os.path.isabs(path) or len(path.encode()) > 100 or '\x00' in path:
            raise ContractError('Invalid authority endpoint')
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


def _request_digest(request):
    return sha256(json.dumps(request, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _request_auth(capability, request):
    body = {key: value for key, value in request.items() if key != 'capability'}
    return hmac.new(capability.encode(), _request_digest(body).encode(), sha256).hexdigest()


def _encode(value):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    except (ValueError, TypeError):
        raise ContractError('Invalid setup frame') from None
    if len(raw) > MAX_FRAME:
        raise ContractError('Invalid setup frame')
    return raw


def _valid_evidence(evidence):
    from api_agent.workflows.inference_capture import _valid_attempt_id
    from api_agent.telemetry.inference_diagnostics import valid_diagnostics
    return not (type(evidence) is not dict or not {'phase', 'content'} <= set(evidence)
            or set(evidence) - {'phase', 'content', 'reason', 'transport_attempt_id', 'capture_source', 'diagnostics'}
            or evidence['phase'] not in ('request', 'response') or type(evidence['content']) is not dict
            or ('transport_attempt_id' in evidence and (evidence['transport_attempt_id'] is None
                or not _valid_attempt_id(evidence['transport_attempt_id'])))
            or ('capture_source' in evidence and evidence['capture_source'] not in ('fallback', 'provider'))
            or ('diagnostics' in evidence and not valid_diagnostics(evidence['diagnostics']))
            or evidence.get('reason') not in (None, 'captured', 'partial', 'truncated', 'unavailable', 'withheld'))


def _evidence_key(row):
    return row.get('phase'), row.get('transport_attempt_id')


def _evidence_omission(row, reason):
    return dict(phase=row['phase'], content={}, reason=reason,
                **({'diagnostics': row['diagnostics']} if 'diagnostics' in row else {}),
                **({'transport_attempt_id': row['transport_attempt_id']} if 'transport_attempt_id' in row else {}),
                **({'capture_source': row['capture_source']} if 'capture_source' in row else {}))


def _bounded_evidence(value, maximum):
    """Measure canonical JSON bytes without materializing the encoded value."""
    budget = [maximum, _EVIDENCE_ITEMS]
    active = set()
    def charge(amount):
        budget[0] -= amount
        return budget[0] >= 0
    def string(item):
        if not charge(2):
            return False
        for index in range(0, len(item), 65536):
            if getattr(_PREPARE_CANCEL, 'event', None) is not None and _PREPARE_CANCEL.event.is_set():
                raise InterruptedError('Evidence preparation stopped')
            try:
                part = json.dumps(item[index:index + 65536], ensure_ascii=False).encode('utf-8')
            except UnicodeError:
                return False
            if not charge(len(part) - 2):
                return False
        return True
    def visit(item, depth):
        budget[1] -= 1
        if budget[1] < 0 or depth > 20:
            return False
        if type(item) is str:
            return string(item)
        if item is None:
            return charge(4)
        if type(item) is bool:
            return charge(4 if item else 5)
        if type(item) is int:
            return abs(item) <= (1 << 53) - 1 and charge(len(str(item)))
        if type(item) is float:
            return math.isfinite(item) and charge(len(json.dumps(item, allow_nan=False)))
        if type(item) not in (dict, list) or id(item) in active or len(item) > budget[1]:
            return False
        if not charge(2 + max(0, len(item) - 1)):
            return False
        active.add(id(item))
        try:
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        return False
                    if not string(key) or not charge(1) or not visit(child, depth + 1):
                        return False
                return True
            return all(visit(child, depth + 1) for child in item)
        finally:
            active.remove(id(item))
    return visit(value, 0)


def _content_frame(evidence):
    """Optional evidence has its own post-redaction bound, never a result gate."""
    if not _valid_evidence(evidence):
        return None
    if not _bounded_evidence(evidence['content'], MAX_EVIDENCE_BYTES):
        return _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'truncated')))
    try:
        frame = _encode(dict(version=1, status='evidence', **evidence))
        return frame if len(frame) <= MAX_CONTENT_FRAME else _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'truncated')))
    except ContractError:
        return _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'truncated')))


def _evidence_frames(evidence, remaining):
    """Frame already scrubbed evidence; logical chunks never cross 200 KiB."""
    if not _valid_evidence(evidence):
        return
    if not _bounded_evidence(evidence['content'], remaining):
        yield _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'truncated')))
        return
    small = _content_frame(evidence)
    try:
        body = json.dumps(evidence['content'], sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        yield _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'unavailable')))
        return
    if len(body) > remaining:
        yield _encode(dict(version=1, status='evidence', **_evidence_omission(evidence, 'truncated')))
        return
    if len(small) <= MAX_CONTENT_FRAME and json.loads(small)['content'] == evidence['content']:
        yield small
        return
    phase = evidence['phase']
    yield _encode(dict(version=1, status='evidence_start', phase=phase,
                       reason=evidence.get('reason'), length=len(body), sha256=sha256(body).hexdigest(),
                       **({'diagnostics': evidence['diagnostics']} if 'diagnostics' in evidence else {}),
                       **({'transport_attempt_id': evidence['transport_attempt_id']} if 'transport_attempt_id' in evidence else {}),
                       **({'capture_source': evidence['capture_source']} if 'capture_source' in evidence else {})))
    for start in range(0, len(body), EVIDENCE_CHUNK):
        chunk = body[start:start + EVIDENCE_CHUNK]
        for offset in range(0, len(chunk), _EVIDENCE_FRAGMENT):
            frame = _encode(dict(version=1, status='evidence_part', phase=phase,
                                 offset=start + offset,
                                 data=base64.b64encode(chunk[offset:offset + _EVIDENCE_FRAGMENT]).decode('ascii')))
            if len(frame) > MAX_CONTENT_FRAME:
                raise ContractError('Evidence frame exceeds limit')
            yield frame
    yield _encode(dict(version=1, status='evidence_end', phase=phase))


class _EvidenceReceiver:
    """Bound and verify a single ordered evidence transfer before admitting it."""
    def __init__(self):
        self.pending = None
        self.total = 0

    def accept(self, frame):
        status = frame['status']
        if len(_encode(frame)) > MAX_CONTENT_FRAME:
            raise ContractError('Invalid inference evidence response')
        if status == 'evidence_start':
            from api_agent.workflows.inference_capture import _valid_attempt_id
            from api_agent.telemetry.inference_diagnostics import valid_diagnostics
            base = {'version', 'status', 'phase', 'reason', 'length', 'sha256'}
            if (self.pending is not None or not base <= set(frame) or set(frame) - base - {'transport_attempt_id', 'capture_source', 'diagnostics'}
                    or ('diagnostics' in frame and not valid_diagnostics(frame['diagnostics']))
                    or ('transport_attempt_id' in frame and (frame['transport_attempt_id'] is None
                        or not _valid_attempt_id(frame['transport_attempt_id'])))
                    or ('capture_source' in frame and frame['capture_source'] not in ('fallback', 'provider'))
                    or frame['phase'] not in ('request', 'response')
                    or frame['reason'] not in (None, 'captured', 'partial', 'truncated', 'unavailable', 'withheld')
                    or type(frame['length']) is not int or not 0 < frame['length'] <= MAX_EVIDENCE_BYTES - self.total
                    or type(frame['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}', frame['sha256']) is None):
                raise ContractError('Invalid inference evidence response')
            self.pending = (frame, bytearray())
            return None
        if self.pending is None:
            raise ContractError('Invalid inference evidence response')
        start, body = self.pending
        if frame.get('phase') != start['phase']:
            raise ContractError('Invalid inference evidence response')
        if status == 'evidence_part':
            if (set(frame) != {'version', 'status', 'phase', 'offset', 'data'}
                    or type(frame['offset']) is not int or frame['offset'] != len(body)
                    or type(frame['data']) is not str):
                raise ContractError('Invalid inference evidence response')
            try:
                part = base64.b64decode(frame['data'], validate=True)
            except (ValueError, binascii.Error):
                raise ContractError('Invalid inference evidence response') from None
            expected = min(_EVIDENCE_FRAGMENT, start['length'] - len(body),
                           EVIDENCE_CHUNK - len(body) % EVIDENCE_CHUNK)
            if len(part) != expected or expected <= 0:
                raise ContractError('Invalid inference evidence response')
            body.extend(part)
            return None
        if status != 'evidence_end' or set(frame) != {'version', 'status', 'phase'}:
            raise ContractError('Invalid inference evidence response')
        if len(body) != start['length'] or sha256(body).hexdigest() != start['sha256']:
            raise ContractError('Invalid inference evidence response')
        try:
            content = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            if type(content) is not dict or json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8') != body:
                raise ValueError
        except (ValueError, UnicodeError):
            raise ContractError('Invalid inference evidence response') from None
        self.total += len(body)
        self.pending = None
        return dict(phase=start['phase'], content=content, reason=start['reason'],
                    **({'diagnostics': start['diagnostics']} if 'diagnostics' in start else {}),
                    **({'transport_attempt_id': start['transport_attempt_id']} if 'transport_attempt_id' in start else {}),
                    **({'capture_source': start['capture_source']} if 'capture_source' in start else {}))


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
    if isinstance(error, SetupRegistrationLimitError):
        return dict(version=1, status='error', category='registration_limit', index_source=None,
                    message='Setup registration limit reached for this operation')
    category, source, message = 'failed', None, 'Shared authentication setup failed'
    if isinstance(error, OperationalError):
        # Exception attributes remain mutable after construction, including on
        # subclasses. Validate before either wire representation is selected.
        try:
            kind, execution, retry_after = error.kind, error.execution, error.retry_after
            classification = OperationalError(kind, execution, retry_after)
            if isinstance(error, ProviderAccessError):
                # Provider acquisition can establish that even an unknown
                # failure occurred before inference. Preserve that valid stage.
                classification.execution = execution
        except (ValueError, OverflowError, AttributeError):
            classification = OperationalError('unknown')
    if isinstance(error, ProviderAccessError):
        # Reuse the provider contract to validate each bounded diagnostic. Keep
        # valid details even when another mutable field has become invalid.
        message = getattr(error, 'message', None)
        try:
            ProviderAccessError(classification.kind, message)
        except ValueError:
            message = 'Shared provider operation failed'
        details = {}
        for name in ('status', 'code', 'type', 'request_id'):
            value = getattr(error, name, None)
            try:
                ProviderAccessError(classification.kind, message, **{name: value})
            except ValueError:
                value = None
            details[name] = value
        details['kind'] = classification.kind
        origin = getattr(error, 'origin', 'provider')
        if origin.__class__ is str and origin == 'local_output_validation':
            details['origin'] = origin
        default_execution = 'possibly_executed' if classification.kind == 'unknown' else 'not_started'
        if classification.execution != default_execution:
            details['execution'] = classification.execution
        if getattr(error, 'usage', None) is not None:
            from .inference import error_usage, usage_to_wire
            details['usage'] = usage_to_wire(error_usage(error))
        if getattr(error, 'response', None) is not None:
            from .rich_inference import response_evidence_to_wire
            details['response'] = dict(version=3, response=response_evidence_to_wire(error.response))
        if capability is not None:
            message = message.replace(capability, '[redacted]')
            details = {name: value.replace(capability, '[redacted]') if isinstance(value, str) else value
                       for name, value in details.items()}
        return dict(version=1, status='error', category='provider', index_source=None,
                    message=message, details=details)
    if isinstance(error, OperationalError):
        from .inference import error_usage, usage_to_wire
        from .rich_inference import response_evidence_to_wire
        usage = error_usage(error)
        response = getattr(error, 'response', None)
        return dict(version=1, status='error', category='operational', index_source=None,
            message='Shared operation failed', details=dict(version=3, kind=classification.kind,
                execution=classification.execution, retry_after=classification.retry_after,
                response=None if response is None else response_evidence_to_wire(response),
                usage=None if usage is None else usage_to_wire(usage)))
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
    frame = dict(version=1, status='error', category=category, index_source=source, message=message)
    response = getattr(error, 'response', None)
    if category == 'failed' and response is not None:
        from .rich_inference import response_evidence_to_wire
        from .inference import error_usage, usage_to_wire
        usage = error_usage(error)
        frame['details'] = dict(version=3, response=response_evidence_to_wire(response),
            usage=None if usage is None else usage_to_wire(usage))
    return frame


def _raise_error(frame):
    if frame.get('category') == 'registration_limit':
        if type(frame.get('version')) is not int or frame != dict(version=1, status='error', category='registration_limit', index_source=None,
                         message='Setup registration limit reached for this operation'):
            raise ContractError('Invalid setup response')
        raise SetupRegistrationLimitError(frame['message'])
    if frame.get('category') == 'operational':
        from .inference import result_from_wire
        from .rich_inference import response_evidence_from_wire
        if (set(frame) != {'version', 'status', 'category', 'index_source', 'message', 'details'}
                or type(frame['version']) is not int or frame['version'] != 1 or frame['status'] != 'error'
                or frame['index_source'] is not None or frame['message'] != 'Shared operation failed'
                or type(frame['details']) is not dict
                or set(frame['details']) != {'version', 'kind', 'execution', 'retry_after', 'response', 'usage'}
                or type(frame['details']['version']) is not int or frame['details']['version'] != 3):
            raise ContractError('Invalid operational response')
        details = frame['details']
        try:
            error = OperationalError(details['kind'], details['execution'], details['retry_after'])
            if error.execution != details['execution']:
                raise ValueError()
            response = None if details['response'] is None else response_evidence_from_wire(details['response'])
            usage = None if details['usage'] is None else result_from_wire(dict(text='', outcome='completed', usage=details['usage'])).usage
        except (ValueError, OverflowError):
            raise ContractError('Invalid operational response') from None
        if response is not None:
            error.response = response
        if usage is not None:
            error.usage = usage
        raise error
    if frame.get('category') == 'provider':
        if (set(frame) != {'version', 'status', 'category', 'index_source', 'message', 'details'}
                or frame['index_source'] is not None or type(frame['details']) is not dict
                or set(frame['details']) - {'usage', 'execution', 'response', 'origin'} != {'kind', 'status', 'code', 'type', 'request_id'}):
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
            if 'response' in details:
                from .rich_inference import response_evidence_from_wire
                nested = details['response']
                if type(nested) is not dict or set(nested) != {'version', 'response'} or type(nested['version']) is not int or nested['version'] != 3:
                    raise ValueError()
                details['response'] = response_evidence_from_wire(nested['response'])
            error = ProviderAccessError(kind, frame['message'], **details)
            if execution is not None:
                error.execution = execution
        except ValueError:
            raise ContractError('Invalid provider response') from None
        raise error
    if (set(frame) - {'details'} != {'version', 'status', 'category', 'index_source', 'message'}
            or type(frame['category']) is not str or frame['category'] not in ('failed', 'prerequisite', 'credential', 'interaction')
            or frame['index_source'] not in (None, 'dotenv', 'override')
            or type(frame['message']) is not str or not frame['message'] or len(frame['message']) > 4096):
        raise ContractError('Invalid setup response')
    if 'details' in frame:
        from .rich_inference import response_evidence_from_wire
        from .inference import result_from_wire
        details = frame['details']
        if (frame['category'] != 'failed' or frame['index_source'] is not None
                or type(details) is not dict or set(details) != {'version', 'response', 'usage'}
                or type(details['version']) is not int or details['version'] != 3):
            raise ContractError('Invalid dispatched failure evidence')
        response = response_evidence_from_wire(details['response'])
        usage = None if details['usage'] is None else result_from_wire(dict(text='', outcome='completed', usage=details['usage'])).usage
        error = ContractError(frame['message'])
        error.response, error.usage = response, usage
        raise error
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
    complete_count: int = 0
    evidence_phase: object = None
    evidence_sent: set = field(default_factory=set)
    dropping: object = None
    jobs: tuple = ()
    finals: set = field(default_factory=set)
    authority_ticket: object = None
    revoked: bool = False


class SetupServer(_Owner):
    def __init__(self, registry, capability, *, timeout=300, cleanup_timeout=5, authority=None):
        self._request_timeout = _timeout(timeout)
        super().__init__(min(_timeout(cleanup_timeout), self._request_timeout))
        self._registry = registry
        from api_agent.telemetry.privacy import EmissionBoundary
        if authority is not None and type(authority) is not EmissionBoundary:
            raise ContractError('Invalid operation authority')
        self._operation_authority = authority
        self._authority_channel = None
        self._authority_pending = {}
        self._capability = _capability(capability)
        self._server = None
        self._starting = None
        self._peers = set()
        self._evidence_slots = asyncio.Semaphore(_MAX_EVIDENCE_TRANSFERS)
        self._evidence_tasks = set()
        self._ids = {}
        self._parents = {}

    def _accept(self, reader, writer):
        if len(self._peers) >= _MAX_SETUP_PEERS:
            writer.close()
            return
        peer = _Peer(reader, writer)
        self._peers.add(peer)
        # The listener captures startup's context. Each accepted connection owns
        # its request deadlines after that startup budget has ended.
        with independent_timeout_scope():
            peer.task = _spawn(self._serve(peer))

    async def _start(self):
        self._server = await asyncio.start_server(self._accept, '127.0.0.1', 0, limit=MAX_FRAME)
        if self._operation_authority is not None:
            from .authority_channel import AuthorityChannel
            self._authority_channel = AuthorityChannel(self._capability.encode(), self._admit_authority,
                snapshot_source=self._graph_authority_source)
            authority_path = await self._authority_channel.start()
        if self._closed:
            self._server.close()
            raise SessionClosedError('Setup server is closed')
        descriptor = dict(version=1, host='127.0.0.1', port=self._server.sockets[0].getsockname()[1], capability=self._capability)
        if self._authority_channel is not None:
            descriptor['authority_path'] = authority_path
        return descriptor

    def _graph_authority_source(self, binding):
        """Resolve only the operation authority of a live registered worker."""
        self._check()
        binding = _binding(binding)
        lease = self._registry._lease(binding)
        if lease['revoked'] or not any(peer.binding == binding and not peer.revoked
                                      for peer in self._peers):
            raise ContractError('Graph authority binding unavailable')
        if self._operation_authority is None:
            raise ContractError('Graph credential authority unavailable')
        return self._operation_authority

    def _admit_authority(self, binding, ticket, sequence):
        pending = self._authority_pending.get(ticket)
        if (pending is None or pending['binding'] != binding or pending['sequence'] != sequence
                or pending['reserved'] or pending['future'].done()):
            return None
        pending['reserved'] = True
        def accept(values):
            if not pending['future'].done():
                pending['future'].set_result(values)
        return accept

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
        if action == 'complete' and self._authority_channel is not None:
            keys.add('authority_nonce')
            if type(frame.get('authority_nonce')) is not str or re.fullmatch(r'[0-9a-f]{64}', frame['authority_nonce']) is None:
                raise ContractError('Invalid authority challenge')
        if action == 'complete' and 'authority_required' in frame:
            keys.add('authority_required')
            if frame['authority_required'] is not True or self._authority_channel is None:
                raise ContractError('Invalid authority requirement')
        if action == 'complete' and 'background' in frame:
            keys.add('background')
            if frame['background'] is not True:
                raise ContractError('Invalid background evidence request')
        if action == 'complete' and 'parent_id' in frame:
            keys.add('parent_id')
            parent = frame['parent_id']
            if type(parent) is not str or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', parent) is None:
                raise ContractError('Invalid inference progress ancestry')
        if action == 'complete' and 'observation_call_id' in frame:
            from api_agent.workflows.inference_capture import _valid_attempt_id
            keys.add('observation_call_id')
            if not _valid_attempt_id(frame['observation_call_id']):
                raise ContractError('Invalid inference observation identity')
        if set(frame) != keys or type(frame['version']) is not int or frame['version'] != 1 or action not in ('register', 'ready', 'describe', 'complete', 'revoke'):
            raise ContractError('Invalid setup request')
        capability = _capability(frame['capability'])
        expected = _request_auth(self._capability, frame) if self._authority_channel is not None else self._capability
        if not hmac.compare_digest(capability, expected):
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
            request_from_wire(frame['request'], _validate_instances=False)
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
        from api_agent.workflows.inference_capture import collect_inference, bind_response_dispatch
        if action == 'complete':
            peer.complete_count += 1
        def dispatched(evidence):
            from .rich_inference import response_evidence_to_wire, validate_response_evidence
            from .inference import request_from_wire
            from types import SimpleNamespace
            if action != 'complete':
                raise ContractError('Response dispatch requires a completion')
            validate_response_evidence(request_from_wire(frame['request']), SimpleNamespace(response=evidence, usage=None))
            peer.writer.write(_encode(dict(version=1, status='response_policy',
                call_sequence=peer.complete_count, request_sha256=_request_digest(frame['request']),
                binding_sha256=_request_digest(peer.binding),
                response=dict(version=3, response=response_evidence_to_wire(evidence)))))
        from .generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
        def attempt_event(phase, row):
            peer.writer.write(_encode(dict(version=1, status='transport_attempt', phase=phase, row=row,
                call_sequence=peer.complete_count, request_sha256=_request_digest(frame['request']),
                binding_sha256=_request_digest(peer.binding))))
        def attempt_quote(result):
            from .inference import request_from_wire
            return self._registry.quote_cost(peer.binding, request_from_wire(frame['request']), result)
        ledger = TransportAttemptLedger(20, observer=attempt_event, quote=attempt_quote) if action == 'complete' else None
        early_sent = False
        def early_result(result, request):
            nonlocal early_sent
            if frame.get('background') is not True:
                return
            from .inference import result_to_wire
            try:
                quote = getattr(self._registry, 'quote_cost', lambda *args: None)(peer.binding, request, result)
            except Exception:
                quote = None
            if quote is not None:
                from .costs import validate_quote
                peer.writer.write(_encode(dict(version=1, status='cost', quote=validate_quote(quote))))
            peer.writer.write(_encode(dict(version=1, status='transport_terminal', ledger=ledger.snapshot(),
                call_sequence=peer.complete_count, request_sha256=_request_digest(frame['request']),
                binding_sha256=_request_digest(peer.binding))))
            peer.writer.write(_encode(dict(version=1, status='result_ready', result=result_to_wire(result))))
            early_sent = True

        def early_error(error):
            nonlocal early_sent
            from api_agent.workflows.inference_capture import retain_response_failure
            retain_response_failure(error, dispatch['response'], dispatch['usage'])
            peer.writer.write(_encode(dict(version=1, status='transport_terminal', ledger=ledger.snapshot(),
                call_sequence=peer.complete_count, request_sha256=_request_digest(frame['request']),
                binding_sha256=_request_digest(peer.binding))))
            peer.writer.write(_encode(dict(version=1, status='error_ready', error=_error(error, self._capability))))
            early_sent = True

        from contextlib import AsyncExitStack
        local_identity = None
        gated_entry = None
        async with AsyncExitStack() as stack:
            if action == 'complete' and self._authority_channel is not None:
                from .local_call_registry import key, lookup
                local_identity = key(self._capability, _request_digest(peer.binding), frame['authority_nonce'])
                gated_entry = lookup(local_identity)
                if gated_entry is not None:
                    from .authority_gate import acquire_authorities
                    boundaries = gated_entry['scope'].authorities()
                    gated_entry['gated_authorities'] = frozenset(id(boundary._authority) for boundary in boundaries)
                    await stack.enter_async_context(acquire_authorities(
                        [boundary._authority for boundary in boundaries], frame['timeout']))
            with bind_transport_attempt_ledger(ledger), bind_response_dispatch(dispatched) as dispatch, collect_inference(call_id=frame.get('observation_call_id'), retain_raw=getattr(self, '_authority_channel', None) is not None) as collector:
                collector['trace_ledger'] = ledger
                try:
                    return await self._dispatch_captured(peer, frame, action, collector, early_result, early_error,
                                                         local_identity, gated_entry)
                except BaseException as error:
                    from api_agent.workflows.inference_capture import retain_response_failure
                    retain_response_failure(error, dispatch['response'], dispatch['usage'])
                    raise
                finally:
                    if ledger is not None and not early_sent and not peer.writer.is_closing():
                        peer.writer.write(_encode(dict(version=1, status='transport_terminal', ledger=ledger.snapshot(),
                            call_sequence=peer.complete_count, request_sha256=_request_digest(frame['request']),
                            binding_sha256=_request_digest(peer.binding))))

    @staticmethod
    def _prepare_evidence(rows):
        """Prepare bounded, scrubbed frames without running on the event loop."""
        from api_agent.workflows.inference_capture import select_capture_rows
        if type(rows) not in (list, tuple) or len(rows) > _MAX_EVIDENCE_ROWS:
            rows = None
        else:
            authoritative = {_evidence_key(row) for row in rows if type(row) is dict
                             and row.get('phase') in ('request', 'response')
                             and row.get('_source') is None}
            rows = [row for row in rows if not (type(row) is dict and row.get('_source') == 'fallback'
                    and _evidence_key(row) in authoritative)]
            bounded, remaining = [], MAX_EVIDENCE_BYTES
            for evidence in rows:
                if type(evidence) is dict and evidence.get('_rejected'):
                    # A rejected body without final-snapshot admission must not
                    # bypass projection through this framing-only entry point.
                    evidence = _evidence_omission(evidence, 'unavailable')
                if type(evidence) is dict and type(evidence.get('content')) is dict:
                    content = evidence['content']
                    if not _bounded_evidence(content, remaining):
                        evidence = dict(_evidence_omission(evidence, 'truncated'),
                                        **({'_source': evidence['_source']} if '_source' in evidence else {}))
                    else:
                        remaining -= len(json.dumps(content, sort_keys=True, separators=(',', ':'),
                                                    ensure_ascii=False, allow_nan=False).encode('utf-8'))
                bounded.append(evidence)
            rows = bounded
        phases = select_capture_rows(rows)
        remaining, prepared = MAX_EVIDENCE_BYTES, []
        for evidence in phases:
            for frame in _evidence_frames(evidence, remaining):
                prepared.append((_evidence_key(evidence), json.loads(frame)['status'], frame))
            if _bounded_evidence(evidence['content'], remaining):
                remaining -= len(json.dumps(evidence['content'], sort_keys=True, separators=(',', ':'),
                                            ensure_ascii=False, allow_nan=False).encode('utf-8'))
        return prepared

    @staticmethod
    def _prepare_cancellable(rows, stopped, projection=None, raw=False, limit=None):
        _PREPARE_CANCEL.event = stopped
        try:
            if projection is not None and type(rows) in (list, tuple) and len(rows) <= _MAX_EVIDENCE_ROWS:
                scrubbed = []
                for row in rows:
                    if type(row) is not dict or type(row.get('content')) is not dict:
                        scrubbed.append(row)
                        continue
                    if '_rejected' in row and (row['_rejected'] is not True or row.get('phase') != 'response'):
                        scrubbed.append(_evidence_omission(row, 'unavailable'))
                        continue
                    if not _bounded_evidence(row['content'], MAX_EVIDENCE_BYTES):
                        scrubbed.append(dict(_evidence_omission(row, 'truncated'),
                                             **({'_source': row['_source']} if '_source' in row else {})))
                        continue
                    source_content = row['content']
                    if row.get('_rejected') is True and source_content:
                        from .rejected_evidence import guard_rejected_evidence
                        try:
                            guard_rejected_evidence(source_content, projection._secrets)
                        except Exception:
                            source_content = {}
                    if raw:
                        from api_agent.workflows.capture import project_values, capture_reason
                        report = set()
                        # The capture budget applies to the content, not the
                        # internal projection wrapper around it.
                        content = project_values({'values': source_content}, {'type': 'object'}, projection,
                                                 report=report, max_bytes=MAX_EVIDENCE_BYTES + 64,
                                                 _content_wrapper=True, _provider_content=True, limit=limit).get('values', {})
                    else:
                        content = projection.content_export({'values': source_content}).get('values', {})
                    clean = dict(row, content=content)
                    clean.pop('_rejected', None)
                    if 'diagnostics' in row:
                        from api_agent.telemetry.inference_diagnostics import scrub_diagnostics
                        diagnostics = scrub_diagnostics(row['diagnostics'], projection)
                        clean.pop('diagnostics')
                        if diagnostics:
                            clean['diagnostics'] = diagnostics
                    if raw:
                        clean['reason'] = (row.get('reason') if not row['content'] and row.get('reason') in ('withheld', 'truncated', 'unavailable')
                                           else capture_reason(content, report))
                        if row.get('reason') == 'partial' and clean['reason'] == 'captured':
                            clean['reason'] = 'partial'
                    if row['content'] and not content and not (raw and 'truncated' in report):
                        clean['reason'] = 'unavailable'
                    scrubbed.append(clean)
                rows = scrubbed
            return SetupServer._prepare_evidence(rows)
        finally:
            del _PREPARE_CANCEL.event

    @staticmethod
    async def _prepare_isolated(rows, projection, *, limit=None):
        """Project in a disposable bounded process; credentials travel only by pipe."""
        stopped = threading.Event()
        def serialize():
            _PREPARE_CANCEL.event = stopped
            try:
                # Metadata is bounded separately from the admitted content.
                # A full 4 MiB content value must not lose space to wrappers.
                if not _bounded_evidence(rows, MAX_EVIDENCE_BYTES + 1024 * 1024):
                    raise ContractError('Evidence projection exceeds input limit')
                encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                payload = bytearray()
                value = dict(rows=rows, secrets=list(projection._secrets), ids=list(projection._assigned_ids))
                if limit is not None:
                    value['limit'] = limit
                for fragment in encoder.iterencode(value):
                    if stopped.is_set():
                        raise ContractError('Evidence projection cancelled')
                    chunk = fragment.encode('utf-8')
                    if len(payload) + len(chunk) > 6 * 1024 * 1024:
                        raise ContractError('Evidence projection exceeds input limit')
                    payload.extend(chunk)
                return bytes(payload)
            finally:
                del _PREPARE_CANCEL.event
        serialization = asyncio.create_task(asyncio.to_thread(serialize))
        try:
            payload = await asyncio.shield(serialization)
        except BaseException:
            stopped.set()
            from api_agent.runtime.tasks import settle
            try:
                await settle(serialization)
            except BaseException:
                pass
            raise
        if os.name != 'posix' or sys.platform not in ('linux', 'darwin'):
            raise ContractError('Evidence projection platform unavailable')
        starting = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, '-m', 'api_agent.providers.evidence_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name == 'posix'),
            env={'PYTHONPATH': os.pathsep.join(os.path.abspath(path) for path in sys.path if type(path) is str),
                 'PYTHONDONTWRITEBYTECODE': '1'}))
        try:
            process = await asyncio.shield(starting)
        except BaseException:
            # Spawn owns a child even if its waiter is cancelled mid-creation.
            from api_agent.runtime.tasks import settle
            process = await settle(starting)
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await settle(asyncio.create_task(process.wait()))
            raise
        try:
            from . import evidence_watchdog
            output, _ = await evidence_watchdog.communicate(process, payload)
            if process.returncode != 0 or len(output) > 9 * 1024 * 1024:
                raise ContractError('Evidence projection unavailable')
            frames = json.loads(output)
            if type(frames) is not list or len(frames) > 4096:
                raise ContractError('Invalid evidence projection')
            result = []
            for item in frames:
                if (type(item) is not list or len(item) != 3 or type(item[0]) is not list
                        or len(item[0]) != 2 or type(item[1]) is not str or type(item[2]) is not str):
                    raise ContractError('Invalid evidence projection')
                result.append((tuple(item[0]), item[1], item[2].encode('utf-8')))
            return result
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            from api_agent.runtime.tasks import settle
            await settle(asyncio.create_task(process.wait()))

    async def _publish_evidence(self, peer, rows, boundary=None, *, raw=False, destination=None):
        # A cancelled waiter cannot release a slot while its thread still runs.
        await self._evidence_slots.acquire()
        task = None
        try:
            for attempt in range(2):
                generation, projection = ((boundary.snapshot() if raw else boundary.snapshot_content())
                                          if boundary is not None else (None, None))
                stopped = threading.Event()
                trace_options = {'limit': 65536} if destination is not None else {}
                task = asyncio.create_task(self._prepare_isolated(rows, projection, **trace_options) if raw else
                                           asyncio.to_thread(self._prepare_cancellable, rows, stopped, projection, raw, **trace_options))
                self._evidence_tasks.add(task)
                def finished(done):
                    self._evidence_tasks.discard(done)
                    _consume(done)
                task.add_done_callback(finished)
                try:
                    frames = await asyncio.shield(task)
                except BaseException:
                    # The server retains the task and its slot until a delayed
                    # serializer/spawn has settled and its child is reaped.
                    # No publication can occur after this waiter leaves.
                    if raw and not task.done():
                        task.cancel()
                    raise
                def commit():
                    if destination is not None:
                        destination(frames)
                        return
                    for phase, status, content_frame in frames:
                        peer.writer.write(content_frame)
                        if status == 'evidence_start':
                            peer.evidence_phase = phase
                        elif status == 'evidence_end':
                            peer.evidence_phase = None
                            peer.evidence_sent.add(phase)
                        elif status == 'evidence':
                            peer.evidence_sent.add(phase)
                committed = boundary.commit_if_current(generation, commit) if boundary is not None else (commit() is None)
                if committed:
                    break
            else:
                raise ContractError('Evidence credential authority changed')
            if frames and destination is None:
                await peer.writer.drain()
        finally:
            if task is None or task.done():
                self._evidence_slots.release()
            else:
                stopped.set()
                task.add_done_callback(lambda _: self._evidence_slots.release())

    async def _caller_authority(self, peer, frame):
        if len(self._authority_pending) >= _MAX_SETUP_PEERS:
            return None
        ticket = secrets.token_hex(32)
        peer.authority_ticket = ticket
        binding = _request_digest(peer.binding)
        sequence = peer.complete_count
        future = asyncio.get_running_loop().create_future()
        self._authority_pending[ticket] = dict(binding=binding, sequence=sequence, future=future, reserved=False)
        payload = [binding, ticket, sequence, frame['authority_nonce']]
        proof = hmac.new(self._capability.encode(),
                         ('authority-request:' + _request_digest(payload)).encode(), sha256).hexdigest()
        declined = None
        try:
            peer.writer.write(_encode(dict(version=1, status='authority_request', ticket=ticket,
                                           sequence=sequence, proof=proof)))
            await peer.writer.drain()
            declined = _spawn(_read(peer.reader))
            done, _ = await asyncio.wait({future, declined}, return_when=asyncio.FIRST_COMPLETED)
            if declined in done:
                value = await declined
                if value != dict(version=1, status='authority_declined', ticket=ticket):
                    raise ContractError('Invalid authority response')
                return None
            return future.result()
        finally:
            self._authority_pending.pop(ticket, None)
            if not future.done():
                future.cancel()
            if declined is not None:
                declined.cancel()
                await asyncio.gather(declined, return_exceptions=True)

    async def _dispatch_captured(self, peer, frame, action, collector, early_result, early_error,
                                 local_identity, gated_entry):
        if action == 'register':
            if peer.binding is not None:
                raise ContractError('Setup connection already registered')
            peer.binding = _binding(self._registry.register(frame['worker_id'], frame['attempt']))
            return dict(version=1, status='registered', binding=peer.binding)
        if peer.binding is None or frame['binding'] != peer.binding:
            raise ContractError('Setup connection binding rejected')
        if action == 'revoke':
            await self._registry.revoke(peer.binding)
            peer.revoked = True
            return dict(version=1, status='revoked')
        caller_values = None
        local_entry = None
        if action == 'complete':
            from .inference import async_request_from_wire
            request = await async_request_from_wire(frame['request'])
            if self._authority_channel is not None:
                from .local_call_registry import lookup
                local_entry = gated_entry
                if lookup(local_identity) is not gated_entry:
                    raise ContractError('Caller authority changed during admission')
                caller_values = await self._caller_authority(peer, frame)
                changed = lookup(local_identity) is not gated_entry
                if caller_values is not None and local_entry is not None:
                    local_lease = local_entry['lease']
                    gated = local_entry.get('gated_authorities', frozenset())
                    changed = changed or local_lease is None or any(
                        id(lease._authority) not in gated for lease in local_lease._leases)
                if changed:
                    ticket = peer.authority_ticket
                    if ticket is not None:
                        payload = [_request_digest(peer.binding), ticket, peer.complete_count, frame['authority_nonce']]
                        proof = hmac.new(self._capability.encode(),
                                         ('authority-release:' + _request_digest(payload)).encode(), sha256).hexdigest()
                        peer.writer.write(_encode(dict(version=1, status='authority_release', ticket=ticket, proof=proof)))
                        peer.authority_ticket = None
                    raise ContractError('Caller authority changed during admission')
                if caller_values is None and frame.get('authority_required') is True:
                    raise ContractError('Caller authority unavailable')
            async def invoke():
                from .call_authority import bind_call_authority
                local_lease = None if local_entry is None else local_entry['lease']
                with bind_call_authority(self._operation_authority, caller_values, local_lease):
                    return await self._ready(peer, action, request)
            ready = _spawn(invoke())
        else:
            ready = _spawn(self._ready(peer, action))
        disconnected = _spawn(peer.reader.read(1))
        peer.jobs = (ready, disconnected)
        result = None
        failed = cancelled = False
        try:
            done, _ = await asyncio.wait({ready, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ContractError('Setup connection disconnected')
            result = await ready
            self._check()
            if action == 'complete':
                from .inference import result_to_wire
                early_result(result, request)
                if frame.get('background') is True:
                    await peer.writer.drain()
                return dict(version=1, status='completed', result=result_to_wire(result))
            if action == 'describe':
                from .catalog import catalog_to_wire
                return dict(version=1, status='catalog', catalog=catalog_to_wire(result))
            return dict(version=1, status='ready')
        except BaseException as error:
            failed = True
            cancelled = isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            if isinstance(error, Exception) and action == 'complete' and frame.get('background') is True:
                early_error(error)
                await peer.writer.drain()
            raise
        finally:
            try:
                if action == 'complete':
                    await self._finish_trace(collector, caller_values, result, failed=failed,
                                             cancelled=cancelled, settled=ready.done())
                if (action == 'complete' and not peer.writer.is_closing()
                        and not disconnected.done()):
                    quote = (getattr(self._registry, 'quote_cost', lambda *args: None)(peer.binding, request, result)
                             if result is not None and frame.get('background') is not True else None)
                    if quote is not None:
                        from .costs import validate_quote
                        peer.writer.write(_encode(dict(version=1, status='cost', quote=validate_quote(quote))))
                    try:
                        rows = collector['rows']
                        from api_agent.telemetry.privacy import EmissionBoundary
                        boundary = collector['boundary']
                        if type(boundary) is EmissionBoundary and boundary.policy.allow_content is not True:
                            # Safe diagnostics survive content opt-out, but still
                            # cross the complete credential-authority projection.
                            rows = [_evidence_omission(row, 'withheld') for row in rows
                                    if type(row) is dict
                                    and ('_rejected' not in row or row['_rejected'] is True and row.get('phase') == 'response')
                                    and _valid_evidence({key: value for key, value in row.items()
                                                        if key not in ('_source', '_rejected')})]
                            phases = {row['phase'] for row in rows}
                            for phase in (('request', 'response') if result is not None else ('request',)):
                                if phase not in phases:
                                    rows.append(dict(phase=phase, content={}, reason='withheld'))
                        elif type(boundary) is not EmissionBoundary:
                            # Neither untrusted rows nor a legacy exporter may
                            # grant permission or establish credential ownership.
                            phases = {'request'}
                            if result is not None:
                                phases.add('response')
                            if type(rows) in (list, tuple):
                                for row in rows[:_MAX_EVIDENCE_ROWS]:
                                    if type(row) is dict and row.get('phase') in ('request', 'response'):
                                        phases.add(row['phase'])
                            rows = [dict(phase=phase, content={}, reason='withheld') for phase in sorted(phases)]
                            boundary = None
                        budget = _scope.get()
                        remaining = None if budget is None else budget.remaining
                        peer.evidence_sent.clear()
                        pending_phases = set()
                        if type(rows) in (list, tuple) and len(rows) <= _MAX_EVIDENCE_ROWS:
                            for row in rows:
                                if type(row) is dict and row.get('phase') in ('request', 'response'):
                                    pending_phases.add(_evidence_key(row))
                        else:
                            pending_phases.update((('request', None), ('response', None)))
                        allowance = (min(30.0, self._request_timeout) if frame.get('background') is True else
                                     min(5.0 if collector.get('retain_raw') is True else 1.0, self._request_timeout / 4,
                                         remaining / 4 if remaining is not None else self._request_timeout / 4))
                        try:
                            async with asyncio.timeout(max(0.001, allowance)):
                                raw = collector.get('retain_raw') is True
                                if raw and boundary is not None:
                                    if caller_values is not None:
                                        from .evidence_authority import EvidenceAuthority
                                        boundary = EvidenceAuthority((boundary, self._operation_authority), caller_values)
                                    else:
                                        boundary = None
                                if raw and boundary is None:
                                    rows = [_evidence_omission(row, 'withheld') for row in rows
                                            if type(row) is dict and row.get('phase') in ('request', 'response')]
                                    if not rows:
                                        rows = [dict(phase=phase, content={}, reason='withheld')
                                                for phase in (('request', 'response') if result is not None else ('request',))]
                                await self._publish_evidence(peer, rows, boundary, raw=raw and boundary is not None)
                        except Exception as failure:
                            if peer.evidence_phase is not None:
                                phase, attempt = peer.evidence_phase
                                peer.writer.write(_encode(dict(version=1, status='evidence_abort', phase=phase,
                                                               **({'transport_attempt_id': attempt} if attempt is not None else {}))))
                                peer.evidence_sent.add(peer.evidence_phase)
                                peer.evidence_phase = None
                            for phase, attempt in sorted(pending_phases - peer.evidence_sent, key=lambda item: (item[0], item[1] or '')):
                                peer.writer.write(_encode(dict(version=1, status='evidence', phase=phase,
                                                               content={}, reason='unavailable' if collector.get('retain_raw') is True and isinstance(failure, ContractError) else 'truncated',
                                                               **({'transport_attempt_id': attempt} if attempt is not None else {}))))
                    except Exception:
                        # Content capture is optional; completed result/usage and
                        # mandatory cleanup retain their original behavior.
                        pass
                    finally:
                        if peer.authority_ticket is not None and not peer.writer.is_closing():
                            ticket = peer.authority_ticket
                            payload = [_request_digest(peer.binding), ticket, peer.complete_count, frame['authority_nonce']]
                            proof = hmac.new(self._capability.encode(),
                                             ('authority-release:' + _request_digest(payload)).encode(), sha256).hexdigest()
                            peer.writer.write(_encode(dict(version=1, status='authority_release', ticket=ticket, proof=proof)))
                            peer.authority_ticket = None
            finally:
                async with report_timeout('Setup request cancellation', self._timeout) as budget:
                    await _drain(peer.jobs, budget.remaining, self._cancel_requested)

    async def _finish_trace(self, collector, caller_values, result, *, failed, cancelled, settled):
        call = collector.get('trace_owner')
        if call is None:
            return
        try:
            trace = collector.get('trace_collector')
            boundary = trace.get('boundary') if trace is not None else None
            from api_agent.telemetry.privacy import EmissionBoundary
            if not settled:
                # Content may still be changing. Retain only the ledger's
                # content-free physical-attempt identities, never active rows.
                call.publish([], collector['trace_ledger'].snapshot())
                call.loss('unavailable')
                return
            if trace is None or type(boundary) is not EmissionBoundary:
                call.loss('unavailable')
                return
            raw = collector['retain_raw']
            if raw:
                if caller_values is None:
                    call.loss('withheld')
                    return
                from .evidence_authority import EvidenceAuthority
                boundary = EvidenceAuthority((boundary, self._operation_authority), caller_values)
            async with component_timeout('Remote trace payload projection', min(5.0, self._request_timeout / 4)):
                await self._publish_evidence(None, trace['rows'], boundary, raw=raw,
                    destination=lambda frames: call.publish(frames, collector['trace_ledger'].snapshot()))
        except asyncio.CancelledError:
            original_cancelled, cancelled = cancelled, True
            if not original_cancelled:
                raise
        except Exception:
            # Projection is optional; preserve the original result/interruption
            # while reporting loss with fixed, content-free metadata.
            try:
                call.loss('unavailable')
            except Exception:
                pass
        finally:
            try:
                call.end(failed=failed, cancelled=cancelled)
            except Exception:
                pass

    async def _drop(self, peer):
        peer.writer.close()
        if peer.binding is not None and not peer.revoked:
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
            if self._authority_channel is not None:
                await self._authority_channel.close()
            await _drain([p.task for p in self._peers], self._timeout, self._cancel_requested)
            if self._evidence_tasks:
                await asyncio.shield(asyncio.gather(*tuple(self._evidence_tasks), return_exceptions=True))
            for peer in tuple(self._peers):
                if peer.dropping is None or peer.dropping.done() and (peer.dropping.cancelled() or peer.dropping.exception() is not None):
                    peer.dropping = _spawn(self._drop(peer))
                await asyncio.shield(peer.dropping)


class SetupClient:
    """Inert synchronous worker facade with one persistent process-local connection."""
    def __init__(self, descriptor, *, worker_id=None, attempt=1, timeout=300, cleanup_timeout=5):
        self._descriptor = validate_descriptor(descriptor)
        self._worker = _identifier(str(uuid4()) if worker_id is None else worker_id)
        if type(attempt) is not int or attempt < 1:
            raise ContractError('Invalid setup attempt')
        self._attempt = attempt
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
        self._revoke_confirmed = False
        self._closing_task = None
        self._request_task = None
        self._cancel_requested = set()
        self._lock = threading.RLock()
        self.evidence = []
        self.cost_quote = None
        self.response_evidence = None
        self.transport_attempts = None
        self._complete_count = 0
        self._authority_scope = None
        self._authority_lease = None
        self._authority_ticket = None
        self._authority_nonce = None
        self._local_call = None
        self._result_ready = None
        self._error_ready = None
        self._observation_call_id = None

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

    def _accept_authority_release(self, frame):
        ticket = self._authority_ticket
        if (set(frame) != {'version', 'status', 'ticket', 'proof'}
                or frame.get('version') != 1 or frame.get('status') != 'authority_release'
                or ticket is None or frame.get('ticket') != ticket
                or type(frame.get('proof')) is not str or self._authority_nonce is None):
            raise ContractError('Invalid authority release')
        payload = [_request_digest(self._binding), ticket, self._complete_count, self._authority_nonce]
        expected = hmac.new(self._descriptor['capability'].encode(),
                            ('authority-release:' + _request_digest(payload)).encode(), sha256).hexdigest()
        if not hmac.compare_digest(frame['proof'], expected):
            raise ContractError('Invalid authority release')
        self._release_authority()

    def _release_authority(self):
        if self._local_call is not None:
            from .local_call_registry import remove
            remove(*self._local_call)
            self._local_call = None
        if self._authority_lease is not None:
            self._authority_lease.release()
            self._authority_lease = None
        self._authority_ticket = None
        self._authority_nonce = None

    async def _settle_authority(self):
        # Retain both connection and lease until the authenticated publication
        # barrier or EOF proves that no further frames can reach this caller.
        while self._authority_lease is not None:
            try:
                frame = await _read(self._reader)
            except ContractError:
                if self._reader.at_eof():
                    self._release_authority()
                    return
                raise
            if frame.get('status') == 'authority_release':
                self._accept_authority_release(frame)

    async def _exchange(self, action, request=None):
        evidence_receiver = _EvidenceReceiver()
        incomplete_phases = set()
        authority_requested = False
        if action == 'complete':
            self.evidence = []
            self.cost_quote = None
            self.transport_attempts = {'version': 1, 'complete': False, 'attempts': []}
        frame = dict(version=1, action=action, timeout=self._timeout, capability=self._descriptor['capability'])
        if action == 'register':
            frame.update(worker_id=self._worker, attempt=self._attempt)
        else:
            frame['binding'] = self._binding
        if action == 'complete':
            frame['request'] = request
            if self._observation_call_id is not None:
                frame['observation_call_id'] = self._observation_call_id
            if self._result_ready is not None:
                frame['background'] = True
            if 'authority_path' in self._descriptor:
                frame['authority_nonce'] = secrets.token_hex(32)
                self._authority_nonce = frame['authority_nonce']
                if self._authority_scope is not None:
                    frame['authority_required'] = True
                    from .local_call_registry import key, register
                    identity = key(self._descriptor['capability'], _request_digest(self._binding), self._authority_nonce)
                    self._local_call = (identity, register(identity, self._authority_scope))
            budget = _scope.get()
            if budget is not None:
                frame['parent_id'] = budget.id
                remaining = budget.remaining
                if remaining is not None:
                    if remaining <= 0:
                        raise ProviderAccessError('deadline', 'Inference call deadline expired.')
                    frame['timeout'] = min(self._timeout, remaining)
        if action == 'complete':
            self._complete_count += 1
        if 'authority_path' in self._descriptor:
            frame['capability'] = _request_auth(self._descriptor['capability'], frame)
        self._writer.write(_encode(frame))
        await self._writer.drain()
        while True:
            try:
                response = await _read(self._reader)
            except BaseException:
                if evidence_receiver.pending is not None:
                    self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                    evidence_receiver.pending = None
                raise
            if type(response.get('version')) is not int or response['version'] != 1:
                if evidence_receiver.pending is not None:
                    self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                    evidence_receiver.pending = None
                raise ContractError('Invalid setup response')
            status = response.get('status')
            if status == 'authority_request' and action == 'complete' and 'authority_path' in self._descriptor:
                if (set(response) != {'version', 'status', 'ticket', 'sequence', 'proof'}
                        or type(response['ticket']) is not str or re.fullmatch(r'[0-9a-f]{64}', response['ticket']) is None
                        or type(response['sequence']) is not int or response['sequence'] != self._complete_count
                        or type(response['proof']) is not str):
                    raise ContractError('Invalid authority request')
                binding = _request_digest(self._binding)
                payload = [binding, response['ticket'], response['sequence'], frame['authority_nonce']]
                proof = hmac.new(self._descriptor['capability'].encode(),
                                 ('authority-request:' + _request_digest(payload)).encode(), sha256).hexdigest()
                if not hmac.compare_digest(response['proof'], proof):
                    raise ContractError('Authority server authentication failed')
                if authority_requested:
                    raise ContractError('Duplicate authority request')
                authority_requested = True
                self._authority_ticket = response['ticket']
                try:
                    if self._authority_scope is None:
                        raise ContractError('Caller authority unavailable')
                    from api_agent.testing.authority_scope import TestAuthorityScope
                    lease = (self._authority_scope.freeze(credential_only=True)
                             if type(self._authority_scope) is TestAuthorityScope
                             else self._authority_scope.freeze())
                    self._authority_lease = lease
                    from .local_call_registry import assign_lease
                    if self._local_call is None or not assign_lease(*self._local_call, lease):
                        raise ContractError('Caller authority handoff unavailable')
                    from .authority_channel import transfer
                    await transfer(self._descriptor['authority_path'], self._descriptor['capability'].encode(),
                                   binding, response['ticket'], response['sequence'], list(lease.secrets))
                except Exception:
                    self._writer.write(_encode(dict(version=1, status='authority_declined', ticket=response['ticket'])))
                    await self._writer.drain()
                continue
            if status == 'authority_release' and action == 'complete' and 'authority_path' in self._descriptor:
                self._accept_authority_release(response)
                continue
            if status == 'evidence_abort' and action == 'complete':
                if (evidence_receiver.pending is None or set(response) not in ({'version', 'status', 'phase'}, {'version', 'status', 'phase', 'transport_attempt_id'})
                        or _evidence_key(response) != _evidence_key(evidence_receiver.pending[0])):
                    if evidence_receiver.pending is not None:
                        self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                        evidence_receiver.pending = None
                    raise ContractError('Invalid inference evidence response')
                self.evidence.append(_evidence_omission(response, 'truncated'))
                incomplete_phases.add(_evidence_key(response))
                evidence_receiver.pending = None
                continue
            if status in ('evidence_start', 'evidence_part', 'evidence_end') and action == 'complete':
                if status == 'evidence_start' and response.get('transport_attempt_id') is not None:
                    if not any(row.get('id') == response['transport_attempt_id']
                               for row in self.transport_attempts['attempts']):
                        raise ContractError('Inference evidence has no transport attempt owner')
                if status == 'evidence_start' and type(response.get('phase')) is not str:
                    if evidence_receiver.pending is not None:
                        self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                        evidence_receiver.pending = None
                    raise ContractError('Invalid inference evidence response')
                if status == 'evidence_start' and (_evidence_key(response) in incomplete_phases
                        or any(_evidence_key(prior) == _evidence_key(response) for prior in self.evidence)):
                    if evidence_receiver.pending is not None:
                        self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                        evidence_receiver.pending = None
                    raise ContractError('Conflicting inference evidence response')
                try:
                    row = evidence_receiver.accept(response)
                except BaseException:
                    if evidence_receiver.pending is not None:
                        start = evidence_receiver.pending[0]
                        self.evidence.append(_evidence_omission(start, 'truncated'))
                        evidence_receiver.pending = None
                    raise
                if row is not None:
                    if any(_evidence_key(prior) == _evidence_key(row) for prior in self.evidence):
                        raise ContractError('Invalid inference evidence response')
                    self.evidence.append(row)
                continue
            if evidence_receiver.pending is not None:
                if status not in ('completed', 'error', 'transport_terminal'):
                    self.evidence.append(_evidence_omission(evidence_receiver.pending[0], 'truncated'))
                    evidence_receiver.pending = None
                    raise ContractError('Interleaved inference evidence response')
                start = evidence_receiver.pending[0]
                self.evidence.append(_evidence_omission(start, 'truncated'))
                incomplete_phases.add(_evidence_key(start))
                evidence_receiver.pending = None
            if status in ('transport_attempt', 'transport_terminal') and action == 'complete':
                from .generation_attempts import validate_transport_attempts
                expected = {'version', 'status', 'call_sequence', 'request_sha256', 'binding_sha256'}
                expected |= {'phase', 'row'} if status == 'transport_attempt' else {'ledger'}
                if (set(response) != expected or response['call_sequence'] != self._complete_count
                        or response['request_sha256'] != _request_digest(request)
                        or response['binding_sha256'] != _request_digest(self._binding)
                        or self.transport_attempts['complete']):
                    raise ContractError('Invalid transport attempt response')
                previous = self.transport_attempts['attempts']
                if status == 'transport_attempt':
                    phase, row = response['phase'], response['row']
                    if phase == 'start' and (not previous or previous[-1]['outcome'] == 'failed'):
                        rows = previous + [row]
                        if row.get('outcome') != 'started':
                            raise ContractError('Invalid transport attempt start')
                    elif phase in ('evidence', 'finish') and previous and previous[-1]['outcome'] in ('started', 'observed'):
                        prior = previous[-1]
                        outcome = 'observed' if phase == 'evidence' else ('completed', 'failed')
                        valid_outcome = row.get('outcome') == outcome if phase == 'evidence' else row.get('outcome') in outcome
                        if (any(row.get(key) != prior[key] for key in ('id', 'index', 'started_ms')) or not valid_outcome
                                or (prior['outcome'] == 'observed' and any(prior[key] is not None and row.get(key) != prior[key] for key in ('usage', 'billing', 'quote', 'response')))):
                            raise ContractError('Invalid transport attempt update')
                        rows = previous[:-1] + [row]
                    else:
                        raise ContractError('Invalid transport attempt sequence')
                    self.transport_attempts = validate_transport_attempts(dict(version=1, complete=False, attempts=rows))
                else:
                    terminal = validate_transport_attempts(response['ledger'])
                    if terminal['attempts'] != previous:
                        raise ContractError('Conflicting transport attempt ledger')
                    self.transport_attempts = terminal
                continue
            if status == 'response_policy' and action == 'complete':
                from .rich_inference import response_evidence_from_wire, validate_response_evidence
                from .inference import request_from_wire
                from types import SimpleNamespace
                nested = response.get('response')
                if (set(response) != {'version', 'status', 'call_sequence', 'request_sha256', 'binding_sha256', 'response'} or self.response_evidence is not None
                        or type(response['call_sequence']) is not int or response['call_sequence'] != self._complete_count
                        or response['request_sha256'] != _request_digest(request)
                        or response['binding_sha256'] != _request_digest(self._binding)
                        or type(nested) is not dict or set(nested) != {'version', 'response'}
                        or type(nested['version']) is not int or nested['version'] != 3):
                    raise ContractError('Invalid dispatched response policy')
                evidence = response_evidence_from_wire(nested['response'])
                validate_response_evidence(request_from_wire(request), SimpleNamespace(response=evidence, usage=None))
                self.response_evidence = evidence
                continue
            if status == 'cost' and action == 'complete':
                from .costs import validate_quote
                if set(response) != {'version', 'status', 'quote'} or self.cost_quote is not None:
                    raise ContractError('Invalid cost response')
                self.cost_quote = validate_quote(response['quote'])
                continue
            if status == 'evidence' and action == 'complete':
                if (not {'version', 'status', 'phase', 'content'} <= set(response)
                        or set(response) - {'version', 'status', 'phase', 'content', 'reason', 'transport_attempt_id', 'capture_source', 'diagnostics'}
                        or response['phase'] not in ('request', 'response')
                        or type(response['content']) is not dict):
                    raise ContractError('Invalid inference evidence response')
                row = {key: response[key] for key in ('phase', 'content', 'reason', 'transport_attempt_id', 'capture_source', 'diagnostics') if key in response}
                if row.get('transport_attempt_id') is not None:
                    if not any(attempt.get('id') == row['transport_attempt_id']
                               for attempt in self.transport_attempts['attempts']):
                        raise ContractError('Inference evidence has no transport attempt owner')
                if _evidence_key(row) in incomplete_phases:
                    raise ContractError('Conflicting inference evidence response')
                if _content_frame(row) is None:
                    raise ContractError('Invalid inference evidence response')
                if len(_encode(response)) > MAX_CONTENT_FRAME:
                    row = _evidence_omission(row, 'truncated')
                size = len(json.dumps(row['content'], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8'))
                if size > MAX_EVIDENCE_BYTES - evidence_receiver.total:
                    row = _evidence_omission(row, 'truncated')
                else:
                    evidence_receiver.total += size
                previous = next((index for index, prior in enumerate(self.evidence)
                                 if _evidence_key(prior) == _evidence_key(row)), None)
                if previous is not None:
                    raise ContractError('Invalid inference evidence response')
                else:
                    self.evidence.append(row)
                continue
            if status == 'error_ready' and action == 'complete' and self._result_ready is not None:
                if set(response) != {'version', 'status', 'error'} or type(response['error']) is not dict:
                    raise ContractError('Invalid early failure response')
                callback, self._error_ready = self._error_ready, None
                self._result_ready = None
                try:
                    _raise_error(response['error'])
                except Exception as error:
                    if callback is not None:
                        callback(error)
                continue
            if status == 'result_ready' and action == 'complete' and self._result_ready is not None:
                if set(response) != {'version', 'status', 'result'}:
                    raise ContractError('Invalid early completion response')
                from .inference import result_from_wire
                callback, self._result_ready = self._result_ready, None
                callback(result_from_wire(response['result']))
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
                if self._authority_lease is not None and self._authority_ticket is None:
                    self._release_authority()
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
                if binding['worker_id'] != self._worker or binding['attempt'] != self._attempt:
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
                if action == 'register_only':
                    return dict(self._binding)
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
                    if self._authority_lease is not None and self._authority_ticket is None:
                        # The pre-acquired lease has not been handed off. Close
                        # the receiver before releasing its authority snapshot.
                        self._writer.close()
                        try:
                            await self._writer.wait_closed()
                        finally:
                            self._release_authority()
                        return
                    if self._authority_lease is not None:
                        await self._settle_authority()
                    try:
                        if self._binding is not None and not self._failed:
                            await self._exchange('revoke')
                            self._revoke_confirmed = True
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

    async def prepare_graph_capture(self):
        """Register before taking caller leases; do not activate authentication."""
        from api_agent.runtime.tasks import offload
        return await offload(self._run_request, 'register_only')

    def graph_capture_authority(self):
        """Borrow credentials without taking inference locks or completion slots."""
        from .authority_channel import leased_snapshot
        if (self._closed or self._failed or self._binding is None
                or 'authority_path' not in self._descriptor):
            raise ContractError('Graph credential authority unavailable')
        return leased_snapshot(self._descriptor['authority_path'],
            self._descriptor['capability'].encode(), dict(self._binding))

    def ensure_ready(self):
        return self._run_request('ready')

    def describe(self):
        return self._run_request('describe')

    def complete(self, request, *, authority_scope=None, result_ready=None, error_ready=None, observation_call_id=None):
        from .inference import request_to_wire
        with self._lock:
            from api_agent.workflows.inference_capture import _valid_attempt_id
            if observation_call_id is not None and not _valid_attempt_id(observation_call_id):
                raise ContractError('Invalid inference observation identity')
            self._observation_call_id = observation_call_id
            self._authority_scope = authority_scope
            self._result_ready = result_ready
            self._error_ready = error_ready
            self.evidence = []
            self.cost_quote = None
            self.response_evidence = None
            self.transport_attempts = {'version': 1, 'complete': False, 'attempts': []}
            from .rich_inference import validate_response_evidence, validate_response_result
            try:
                result = self._run_request('complete', request_to_wire(request))
            except BaseException as error:
                from .generation_attempts import attach_transport_attempts
                attach_transport_attempts(error, self.transport_attempts)
                from api_agent.workflows.inference_capture import retain_response_failure
                if self.response_evidence is None and getattr(error, 'response', None) is not None:
                    from .inference import error_usage
                    rejected = ContractError('Unobserved dispatched response policy')
                    rejected.usage = error_usage(error)
                    attach_transport_attempts(rejected, self.transport_attempts)
                    raise rejected from error
                try:
                    retain_response_failure(error, self.response_evidence)
                except ContractError as conflict:
                    attach_transport_attempts(conflict, self.transport_attempts)
                    raise
                if getattr(error, 'response', None) is not None:
                    validate_response_evidence(request, error)
                raise
            finally:
                if self._local_call is not None and self._authority_lease is None:
                    from .local_call_registry import remove
                    remove(*self._local_call)
                    self._local_call = None
                self._authority_scope = None
                self._result_ready = None
                self._error_ready = None
                self._observation_call_id = None
            if self.response_evidence is None and result.response is not None:
                rejected = ContractError('Unobserved dispatched response policy')
                rejected.usage = result.usage
                from .generation_attempts import attach_transport_attempts
                attach_transport_attempts(rejected, self.transport_attempts)
                raise rejected
            if self.response_evidence is not None and self.response_evidence != result.response:
                conflict = ContractError('Conflicting dispatched response policy')
                conflict.response = self.response_evidence
                conflict.usage = result.usage
                from .generation_attempts import attach_transport_attempts
                attach_transport_attempts(conflict, self.transport_attempts)
                raise conflict
            from .generation_attempts import attach_transport_attempts
            result = attach_transport_attempts(result, self.transport_attempts)
            try:
                return validate_response_result(request, result)
            except BaseException as error:
                attach_transport_attempts(error, self.transport_attempts)
                raise

    def _run_request(self, action, request=None):
        with self._lock:
            self._context()
            if self._closed:
                raise SessionClosedError('Setup client is closed')
            if self._authority_lease is not None:
                raise ContractError('Previous authority handoff remains pending')
            if self._loop is None:
                self._thread = threading.get_ident()
                self._loop = asyncio.new_event_loop()
            self._request_task = self._loop.create_task(self._ensure(action, request))
            self._request_task.add_done_callback(_consume)
            try:
                return self._loop.run_until_complete(self._request_task)
            except (SetupPrerequisiteError, InteractionRequiredError, ProviderAccessError, OperationalError, SetupRegistrationLimitError):
                raise
            except BaseException as original:
                self._failed = True
                interrupted = isinstance(original, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
                operation_error = ContractError('Shared setup connection failed')
                if getattr(original, 'response', None) is not None:
                    from .inference import error_usage
                    operation_error.response = original.response
                    operation_error.usage = error_usage(original)
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
