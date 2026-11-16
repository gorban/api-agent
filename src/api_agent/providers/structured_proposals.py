"""Owner-local authority for non-executable structured-proposal replay.

This draft deliberately contains no provider-specific mapping or prompt text.
The authority is held privately by one canonical owner. A replay receipt never
becomes a model message, diagnostic field, or an unverified wire exception.
"""
import json
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256

from .contracts import ContractError
from .llm import LLMRequest, LLMResult

_CONSTRUCTION = object()
_MAX_PENDING = 64
_MAX_AGE_SECONDS = 300
_MAX_FEEDBACK_BYTES = 16384
_VERIFIED_REPLAY = ContextVar('verified_structured_replay', default=None)
_SOURCE_REQUEST = ContextVar('structured_source_request', default=None)


@contextmanager
def bind_structured_source_request(request):
    """Bind exact pre-configuration source only for this owned LocalLLM call."""
    if _SOURCE_REQUEST.get() is not None:
        raise ContractError('Structured source request already bound')
    state = {'request': request, 'active': True, 'lock': threading.Lock()}
    token = _SOURCE_REQUEST.set(state)
    try:
        yield
    finally:
        with state['lock']:
            state['active'] = False
        _SOURCE_REQUEST.reset(token)


def current_structured_source_request():
    state = _SOURCE_REQUEST.get()
    if state is None:
        return None
    with state['lock']:
        if not state['active']:
            raise ContractError('Structured source request expired')
        return state['request']


class StructuredProposalReceipt:
    """Opaque, unpickleable, single-use authority held only by its issuer."""
    __slots__ = ('_issuer', '_nonce')

    def __init__(self, gate, issuer, nonce):
        if gate is not _CONSTRUCTION:
            raise ContractError('Invalid structured replay authority')
        self._issuer, self._nonce = issuer, nonce

    def __repr__(self):
        return '<StructuredProposalReceipt>'

    def __reduce__(self):
        raise TypeError('Structured replay authority cannot be serialized')

    def release(self):
        """Idempotently retire unused owner-issued replay authority."""
        self._issuer.release(self)


class VerifiedStructuredReplay:
    """Internal proof for one exact replay; cannot be made from caller data."""
    __slots__ = ('_digest', '_message_index', '_call', '_scope_lock', '_scope_used', '_owner_verified', '_issuer', '_current_row', '_ceiling', '_active')

    def __init__(self, gate, digest, message_index, call, *, owner_verified=True, issuer=None, row=None):
        if gate is not _CONSTRUCTION:
            raise ContractError('Invalid structured replay permit')
        object.__setattr__(self, '_digest', digest)
        object.__setattr__(self, '_message_index', message_index)
        object.__setattr__(self, '_call', call)
        object.__setattr__(self, '_owner_verified', owner_verified)
        object.__setattr__(self, '_issuer', issuer)
        object.__setattr__(self, '_current_row', None if row is None else json.loads(json.dumps(row, allow_nan=False)))
        object.__setattr__(self, '_ceiling', None)
        object.__setattr__(self, '_scope_lock', threading.Lock())
        object.__setattr__(self, '_scope_used', False)
        object.__setattr__(self, '_active', False)

    def __setattr__(self, name, value):
        raise AttributeError('Structured replay permit is immutable')

    def __repr__(self):
        return '<VerifiedStructuredReplay>'

    def __reduce__(self):
        raise TypeError('Structured replay permit cannot be serialized')

    def matches(self, row):
        digest = _wire_digest(row)
        with self._scope_lock:
            return self._active and digest == self._digest

    def allows(self, index, call):
        with self._scope_lock:
            return self._active and index == self._message_index and call == self._call

    def claim_scope(self):
        with self._scope_lock:
            if self._scope_used:
                raise ContractError('Structured replay permit already used')
            object.__setattr__(self, '_scope_used', True)
            object.__setattr__(self, '_active', True)

    def revoke(self):
        with self._scope_lock:
            object.__setattr__(self, '_active', False)


def _current_verified_replay():
    permit = _VERIFIED_REPLAY.get()
    if permit is not None:
        with permit._scope_lock:
            if not permit._active:
                raise ContractError('Structured replay permit expired')
    return permit


def current_verified_replay():
    """Expose only whether a verified replay scope exists, never its permit."""
    permit = _current_verified_replay()
    return permit is not None and permit._owner_verified


@contextmanager
def bind_verified_replay(permit):
    prior = _current_verified_replay()
    if (type(permit) is not VerifiedStructuredReplay or not permit._owner_verified
            or (prior is not None and (prior._owner_verified or prior._digest != permit._digest))):
        raise ContractError('Invalid structured replay permit')
    permit.claim_scope()
    token = _VERIFIED_REPLAY.set(permit)
    try:
        yield
    finally:
        permit.revoke()
        _VERIFIED_REPLAY.reset(token)


@contextmanager
def _bind_history_replay(request):
    receipt = getattr(request, 'proposal_replay', None)
    if type(receipt) is not StructuredProposalReceipt or _current_verified_replay() is not None:
        raise ContractError('Invalid structured replay authority')
    request, permit = receipt._issuer.prepare_replay(request)
    permit.claim_scope()
    token = _VERIFIED_REPLAY.set(permit)
    try:
        yield request
    finally:
        permit.revoke()
        _VERIFIED_REPLAY.reset(token)


def _wire_digest(value):
    # Reject oversized objects before json.dumps or UTF-8 copies can allocate
    # proportionally to attacker-controlled data.
    import math
    pending, nodes, size = [(value, 0)], 0, 0
    try:
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if nodes > 65536 or depth > 64:
                raise ValueError()
            if type(item) is str:
                if len(item) > 2097152 - size:
                    raise ValueError()
                size += len(item.encode('utf-8'))
            elif type(item) is dict:
                if len(item) > 8192 or len(pending) + len(item) > 65536 - nodes:
                    raise ValueError()
                for key, child in item.items():
                    if type(key) is not str or len(key) > 2097152 - size:
                        raise ValueError()
                    size += len(key.encode('utf-8'))
                    pending.append((child, depth + 1))
            elif type(item) in (list, tuple):
                if len(item) > 8192 or len(pending) + len(item) > 65536 - nodes:
                    raise ValueError()
                pending.extend((child, depth + 1) for child in item)
            elif item is None or type(item) in (bool, int):
                pass
            elif type(item) is float and math.isfinite(item):
                pass
            else:
                raise ValueError()
            if size > 2097152:
                raise ValueError()
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > 2097152:
            raise ValueError()
        return sha256(encoded).digest()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ContractError('Invalid structured replay evidence') from None


def _original_request_digest(request):
    from .inference import request_to_wire
    token = _VERIFIED_REPLAY.set(None)
    try:
        return _wire_digest(request_to_wire(request))
    finally:
        _VERIFIED_REPLAY.reset(token)


def _feedback(message, call_id):
    from .rich_inference import object_json
    if message.role != 'tool' or message.calls or len(message.results) != 1:
        raise ContractError('Invalid structured replay result')
    item = message.results[0]
    if item.call_id != call_id or len(item.content) != 1:
        raise ContractError('Invalid structured replay result')
    text = item.content[0].text
    if len(text.encode('utf-8')) > _MAX_FEEDBACK_BYTES:
        raise ContractError('Structured replay feedback exceeds its bound')
    try:
        value = object_json(text)
    except ContractError:
        raise ContractError('Invalid structured replay feedback') from None
    if (set(value) != {'error', 'issues', 'truncated'} or value['error'] != 'structured_validation_failed'
            or type(value['issues']) is not list or len(value['issues']) > 16
            or type(value['truncated']) is not bool):
        raise ContractError('Invalid structured replay feedback')
    for issue in value['issues']:
        required = {'field_path', 'constraint', 'message'}
        if (type(issue) is not dict or not required <= issue.keys()
                or issue.keys() - required - {'rejected_value'}
                or any(type(issue[key]) is not str or len(issue[key]) > limit
                       for key, limit in (('field_path', 256), ('constraint', 128), ('message', 512)))):
            raise ContractError('Invalid structured replay feedback')


class StructuredReplayAuthority:
    """One canonical owner issues and consumes its own bounded evidence."""
    def __init__(self, *, clock=time.monotonic):
        self._clock, self._lock, self._entries, self._closed = clock, threading.Lock(), {}, False

    async def issue(self, request, result, *, selection=None, source_request=None):
        from .inference import request_to_wire, result_to_wire
        from .rich_inference import async_validate_result
        if (type(request) is not LLMRequest or type(result) is not LLMResult or request.proposal_replay is not None
                or request.payload.structured is None or request.payload.structured.encoding != 'function_calling'
                or request.payload.tools or result.structured_proposal is None or result.structured_proposal.receipt is not None):
            raise ContractError('Invalid structured proposal origin')
        # Schema validity is advisory: the original Python parser owns acceptance.
        confirmed = await async_validate_result(request, result, selection=selection)
        if confirmed is not result:
            raise ContractError('Invalid structured proposal origin')
        from .cache_policy import configured_request
        source_request = request if source_request is None else source_request
        if type(source_request) is not LLMRequest or source_request.proposal_replay is not None:
            raise ContractError('Invalid structured proposal source')
        generation = request.payload.generation
        configured = configured_request(source_request, generation.cache_policy,
                                        reasoning_effort=generation.reasoning_effort)
        request_hash, result_hash = _original_request_digest(request), _wire_digest(result_to_wire(result))
        source_hash = _original_request_digest(source_request)
        if _original_request_digest(configured) != request_hash:
            raise ContractError('Invalid structured proposal source')
        with self._lock:
            now = self._clock()
            self._entries = {key: value for key, value in self._entries.items() if value[0] > now}
            if self._closed or len(self._entries) >= _MAX_PENDING:
                raise ContractError('Structured replay authority unavailable')
            nonce = secrets.token_bytes(32)
            self._entries[nonce] = (now + _MAX_AGE_SECONDS, request, result, request_hash, result_hash, source_request, source_hash)
        return StructuredProposalReceipt(_CONSTRUCTION, self, nonce)

    async def bind(self, request, result, *, selection=None, source_request=None):
        """Return immutable proposal metadata with authority attached after admission."""
        receipt = await self.issue(request, result, selection=selection, source_request=source_request)
        return replace(result, structured_proposal=replace(result.structured_proposal, receipt=receipt))

    def release(self, receipt):
        if type(receipt) is not StructuredProposalReceipt or receipt._issuer is not self:
            raise ContractError('Invalid structured replay authority')
        with self._lock:
            self._entries.pop(receipt._nonce, None)

    def inspect_origin(self, request, result, receipt):
        """Recheck attached evidence without consuming or disclosing authority."""
        from .inference import result_to_wire
        if type(receipt) is not StructuredProposalReceipt or receipt._issuer is not self:
            raise ContractError('Invalid structured replay authority')
        if type(result) is not LLMResult or result.structured_proposal is None:
            raise ContractError('Invalid structured proposal origin')
        unbound = replace(result, structured_proposal=replace(result.structured_proposal, receipt=None))
        with self._lock:
            entry = self._entries.get(receipt._nonce)
            if self._closed or entry is None or entry[0] <= self._clock():
                raise ContractError('Structured replay authority unavailable')
            _, original, expected, request_hash, result_hash, source, source_hash = entry
            if (_original_request_digest(request) != source_hash or _original_request_digest(source) != source_hash
                    or _original_request_digest(original) != request_hash
                    or _wire_digest(result_to_wire(expected)) != result_hash
                    or _wire_digest(result_to_wire(unbound)) != result_hash):
                raise ContractError('Structured proposal origin mismatch')
        return unbound

    def prepare_replay(self, request):
        """Verify the exact raw request, then derive its recorded configuration."""
        from .cache_policy import configured_request
        from .rich_inference import _request_row
        raw_permit = self._permit(request, consume=False, normalized=False)
        receipt = request.proposal_replay
        with self._lock:
            entry = self._entries.get(receipt._nonce)
            if self._closed or entry is None or entry[0] <= self._clock():
                raise ContractError('Structured replay authority unavailable')
            effective = entry[1]
            generation = effective.payload.generation
            normalized = configured_request(request, generation.cache_policy,
                                            reasoning_effort=generation.reasoning_effort)
            messages = normalized.payload.messages
            check = replace(normalized, request_id=effective.request_id, proposal_replay=None,
                            payload=replace(normalized.payload, messages=effective.payload.messages))
            if messages[:-2] != effective.payload.messages or _original_request_digest(check) != entry[3]:
                raise ContractError('Structured replay effective origin mismatch')
            row = _request_row(normalized)
            permit = VerifiedStructuredReplay(_CONSTRUCTION, _wire_digest(row), raw_permit._message_index,
                raw_permit._call, owner_verified=False, issuer=self, row=row)
        return normalized, permit

    def inspect(self, request):
        """Preflight history shape only; this never authorizes a provider call."""
        return self._permit(request, consume=False, normalized=False)

    def consume(self, request):
        from .rich_inference import _request_row
        prior = _current_verified_replay()
        normalized = (prior is not None and not prior._owner_verified and prior._issuer is self
                      and prior.matches(_request_row(request)))
        return self._permit(request, consume=True, normalized=normalized)

    def _permit(self, request, *, consume, normalized):
        from .inference import request_to_wire, result_to_wire
        if type(request) is not LLMRequest or type(request.proposal_replay) is not StructuredProposalReceipt:
            raise ContractError('Invalid structured replay authority')
        receipt = request.proposal_replay
        if receipt._issuer is not self:
            raise ContractError('Invalid structured replay authority')
        with self._lock:
            entry = self._entries.get(receipt._nonce)
            if self._closed or entry is None or entry[0] <= self._clock():
                self._entries.pop(receipt._nonce, None)
                raise ContractError('Structured replay authority unavailable')
            _, effective, result, request_hash, result_hash, source, source_hash = entry
            if consume and not normalized and source_hash != request_hash:
                raise ContractError('Structured replay requires configured owner preflight')
            original = effective if normalized else source
            expected_hash = request_hash if normalized else source_hash
            if (_original_request_digest(effective) != request_hash or
                    _original_request_digest(source) != source_hash or
                    _wire_digest(result_to_wire(result)) != result_hash):
                raise ContractError('Structured replay evidence changed')
            messages = request.payload.messages
            if len(messages) != len(original.payload.messages) + 2 or messages[:-2] != original.payload.messages or messages[-2] != result.payload:
                raise ContractError('Structured replay origin mismatch')
            check = replace(request, request_id=original.request_id, proposal_replay=None,
                            payload=replace(request.payload, messages=original.payload.messages))
            if _original_request_digest(check) != expected_hash:
                raise ContractError('Structured replay origin mismatch')
            _feedback(messages[-1], result.structured_proposal.call.call_id)
            # Produce proof only after exact owner/request/pair validation. The
            # receipt is consumed before dispatch, including cancellation/failure.
            from .rich_inference import _request_row
            row = _request_row(request)
            permit = VerifiedStructuredReplay(_CONSTRUCTION, _wire_digest(row),
                len(original.payload.messages), result.structured_proposal.call,
                owner_verified=consume, issuer=self, row=row)
            if consume:
                del self._entries[receipt._nonce]
            return permit

    def set_effective_ceiling(self, ceiling):
        """Set existing canonical policy limit for this admitted replay only."""
        permit = _current_verified_replay()
        if (permit is None or not permit._owner_verified or permit._issuer is not self
                or type(ceiling) is not int or ceiling <= 0):
            raise ContractError('Invalid structured replay ceiling')
        with permit._scope_lock:
            if not permit._active:
                raise ContractError('Structured replay permit expired')
            object.__setattr__(permit, '_ceiling', ceiling)

    def close(self):
        with self._lock:
            self._closed = True
            self._entries.clear()


def admit_effective_replay(request):
    """Bind only owner-authorized output-limit normalization for this replay."""
    from contextlib import nullcontext
    from .rich_inference import _request_row
    permit = _current_verified_replay()
    if permit is None or not permit._owner_verified:
        raise ContractError('Invalid structured replay permit')
    proposed = _request_row(request)
    digest = _wire_digest(proposed)
    scope = _active_admitted_scope()
    with scope['lock'] if scope is not None else nullcontext():
        if scope is not None:
            if not scope['active']:
                raise ContractError('Structured proposal admission expired')
            scope['selection'].validate(request)
        with permit._scope_lock:
            if not permit._active or permit._current_row is None:
                raise ContractError('Invalid structured replay permit')
            old = permit._current_row
            if digest == _wire_digest(old):
                return
            old_rest, new_rest = dict(old), dict(proposed)
            old_max, new_max = old_rest.pop('max_output_tokens'), new_rest.pop('max_output_tokens')
            if (_wire_digest(old_rest) != _wire_digest(new_rest) or type(new_max) is not int or new_max <= 0
                    or type(permit._ceiling) is not int or new_max > permit._ceiling
                    or old_max is not None and new_max > old_max):
                raise ContractError('Structured replay effective request changed')
            object.__setattr__(permit, '_digest', digest)
            object.__setattr__(permit, '_current_row', json.loads(json.dumps(proposed, allow_nan=False)))
            if scope is not None:
                scope['request'] = replace(request, proposal_replay=None)


# The following private worker envelope is not an LLM message or public wire
# field. The owning provider bridge must pass its per-worker secret only over
# its trusted startup channel and must never retain the envelope in captures.
from contextlib import ExitStack
from contextvars import ContextVar
import hmac

_ADMITTED_PROPOSAL_REQUEST = ContextVar('admitted_structured_proposal_request', default=None)


def _active_admitted_scope():
    scope = _ADMITTED_PROPOSAL_REQUEST.get()
    if scope is not None:
        with scope['lock']:
            if not scope['active']:
                raise ContractError('Structured proposal admission expired')
    return scope


def is_structured_proposal():
    """Whether the trusted owner admitted a proposal request in this scope."""
    return _active_admitted_scope() is not None


def current_structured_request():
    """Return a receipt-free admitted request, or None outside owner scope."""
    scope = _active_admitted_scope()
    if scope is None:
        return None
    with scope['lock']:
        if not scope['active']:
            raise ContractError('Structured proposal admission expired')
        return scope['request']


def admit_effective_structured_request(request):
    """Admit only a narrower output cap within the selected provider scope.

    Provider-side defaults and model ceilings may narrow the admitted request;
    they may not alter the model, messages, schema, profile, or other controls.
    A missing prior maximum denotes an unbounded request, subject to the selected
    model maximum and the canonical wire limit when a default is inserted.
    """
    from .rich_inference import _request_row
    scope = _active_admitted_scope()
    if scope is None or type(request) is not LLMRequest:
        raise ContractError('Structured proposal is not admitted')
    with scope['lock']:
        if not scope['active']:
            raise ContractError('Structured proposal admission expired')
        before, after = _request_row(scope['request']), _request_row(request)
        before_rest, after_rest = dict(before), dict(after)
        old_max, new_max = before_rest.pop('max_output_tokens'), after_rest.pop('max_output_tokens')
        selected_max = scope['selection'].max_output_tokens
        if (_wire_digest(before_rest) != _wire_digest(after_rest) or (new_max != old_max and (
                type(new_max) is not int or new_max <= 0 or new_max > 1048576
                or old_max is not None and new_max > old_max
                or selected_max is not None and new_max > selected_max))):
            raise ContractError('Structured proposal effective request changed')
        scope['selection'].validate(request)
        if _current_verified_replay() is not None:
            if request.proposal_replay is None:
                raise ContractError('Structured replay authority missing')
            admit_effective_replay(request)
        elif request.proposal_replay is not None:
            raise ContractError('Invalid structured replay permit')
        else:
            scope['request'] = replace(request, proposal_replay=None)
        return scope['request']


class _StructuredAdmissionAuthority:
    """One loader/worker owner binds only selected, verified proposal calls."""
    __slots__ = ('_owner',)

    def __init__(self, gate, owner):
        if gate is not _CONSTRUCTION or owner is None:
            raise ContractError('Invalid structured proposal admission')
        self._owner = owner

    @contextmanager
    def bind(self, owner, request, selection):
        from .llm import AvailableSelection
        if (owner is not self._owner or type(request) is not LLMRequest
                or type(selection) is not AvailableSelection
                or request.payload.structured is None
                or request.payload.structured.encoding != 'function_calling'
                or _active_admitted_scope() is not None
                or request.proposal_replay is not None and not current_verified_replay()):
            raise ContractError('Invalid structured proposal admission')
        selection.validate(request)
        sanitized = replace(request, proposal_replay=None)
        scope = {'request': sanitized, 'selection': selection,
                 'worker_issuers': [], 'worker_controls': [], 'active': True, 'lock': threading.RLock()}
        token = _ADMITTED_PROPOSAL_REQUEST.set(scope)
        try:
            yield
        finally:
            with scope['lock']:
                scope['active'] = False
                controls, issuers = tuple(scope['worker_controls']), tuple(scope['worker_issuers'])
            for control in controls:
                control.close()
            for issuer in issuers:
                issuer.close()
            _ADMITTED_PROPOSAL_REQUEST.reset(token)


def _new_admission_authority(owner):
    return _StructuredAdmissionAuthority(_CONSTRUCTION, owner)


class StructuredWorkerControl:
    """One-use transfer of owner-issued evidence over a private worker socket."""
    __slots__ = ('_secret', '_row', '_proof', '_controls', '_lock', '_used')

    def __init__(self, gate, secret, row, proof, controls):
        if gate is not _CONSTRUCTION:
            raise ContractError('Invalid structured worker authority')
        self._secret, self._row, self._proof, self._controls = bytearray(secret), row, proof, controls
        self._lock, self._used = threading.Lock(), False

    def __repr__(self):
        return '<StructuredWorkerControl>'

    def __reduce__(self):
        raise TypeError('Structured worker authority cannot be serialized')

    def close(self):
        with self._lock:
            self._used = True
            for index in range(len(self._secret)):
                self._secret[index] = 0
            self._row, self._proof, self._controls = None, None, None

    async def send_private(self, channel):
        """Send one bounded control frame, never returning authority material."""
        import asyncio
        with self._lock:
            if self._used:
                raise ContractError('Structured worker authority already transferred')
            self._used = True
            try:
                data = json.dumps({'key': bytes(self._secret).hex(), 'row': self._row,
                                   'proof': self._proof, 'controls': self._controls}, allow_nan=False,
                                  separators=(',', ':')).encode() + b'\n'
                if len(data) > 8 * 1024 * 1024:
                    raise ContractError('Structured worker authority exceeds its bound')
            finally:
                for index in range(len(self._secret)):
                    self._secret[index] = 0
                self._row, self._proof, self._controls = None, None, None
        await asyncio.get_running_loop().sock_sendall(channel, data)


def _checked_provider_controls(value):
    if value is None:
        return {}
    if type(value) is not dict or len(value) > 8:
        raise ContractError('Invalid structured provider controls')
    for key, item in value.items():
        if type(key) is not str or len(key) > 128:
            raise ContractError('Invalid structured provider controls')
        if key == 'reasoning_format':
            if type(item) is not str or item not in ('auto', 'tagged', 'literal'):
                raise ContractError('Invalid structured provider controls')
        elif type(item) is not int or not 0 <= item <= 1048576:
            raise ContractError('Invalid structured provider controls')
    return dict(value)


def issue_worker_control(provider_body, operation, *, provider_controls=None):
    """Issue only from an already admitted canonical provider-dispatch scope."""
    from .inference import request_to_wire
    scope = _active_admitted_scope()
    if scope is None:
        raise ContractError('Structured proposal is not admitted')
    with scope['lock']:
        if not scope['active']:
            raise ContractError('Structured proposal admission expired')
        request = scope['request']
        provider_controls = _checked_provider_controls(provider_controls)
        row = request_to_wire(request)
        secret = secrets.token_bytes(32)
        issuer = StructuredWorkerAdmission(secret)
        try:
            proof = issuer.proof(row, provider_body, operation, provider_controls=provider_controls)
        except BaseException:
            issuer.close()
            raise
        scope['worker_issuers'].append(issuer)
        control = StructuredWorkerControl(_CONSTRUCTION, secret, row, proof, provider_controls)
        scope['worker_controls'].append(control)
        return control


class StructuredWorkerAdmission:
    """Per-worker authenticated control plane; never a provider payload."""
    def __init__(self, secret):
        if type(secret) is not bytes or len(secret) != 32:
            raise ContractError('Invalid structured worker authority')
        self._secret, self._lock, self._seen, self._closed = bytearray(secret), threading.Lock(), set(), False

    def __repr__(self):
        return '<StructuredWorkerAdmission>'

    def __reduce__(self):
        raise TypeError('Structured worker authority cannot be serialized')

    def proof(self, row, provider_body, operation, permit=None, *, provider_controls=None):
        if permit is None:
            permit = _current_verified_replay()
        if (type(operation) is not str or not operation or len(operation) > 128 or not operation.isprintable()):
            raise ContractError('Invalid structured worker admission')
        if self._closed or (permit is not None and (type(permit) is not VerifiedStructuredReplay
                or not permit._owner_verified or _current_verified_replay() is not permit or not permit.matches(row))):
            raise ContractError('Invalid structured worker admission')
        if permit is None:
            if _current_verified_replay() is not None:
                raise ContractError('Invalid structured worker admission')
            from .rich_inference import request_from_wire
            request_from_wire(row)
        if (type(row) is not dict or row.get('version') != 4 or type(row.get('structured')) is not dict
                or row['structured'].get('encoding') != 'function_calling'):
            raise ContractError('Invalid structured worker admission')
        provider_controls = _checked_provider_controls(provider_controls)
        nonce, digest = secrets.token_bytes(32).hex(), _wire_digest(row).hex()
        index = None if permit is None else permit._message_index
        body = {'nonce': nonce, 'digest': digest, 'provider_digest': _wire_digest(provider_body).hex(),
                'operation': operation, 'index': index, 'controls_digest': _wire_digest(provider_controls).hex()}
        encoded = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
        body['mac'] = hmac.new(bytes(self._secret), encoded, 'sha256').hexdigest()
        return body

    @contextmanager
    def admit(self, row, provider_body, operation, proof, *, selection, provider_controls=None):
        from .rich_inference import message_from_wire, request_from_wire
        provider_controls = _checked_provider_controls(provider_controls)
        if (type(proof) is not dict or set(proof) != {'nonce', 'digest', 'provider_digest', 'operation', 'index', 'controls_digest', 'mac'}
                or any(type(proof[k]) is not str or len(proof[k]) != 64 or any(c not in '0123456789abcdef' for c in proof[k])
                       for k in ('nonce', 'digest', 'provider_digest', 'controls_digest', 'mac'))
                or type(operation) is not str or proof['operation'] != operation or not operation or len(operation) > 128 or not operation.isprintable()
                or (proof['index'] is not None and (type(proof['index']) is not int or not 0 <= proof['index'] < 64))):
            raise ContractError('Invalid structured worker admission')
        unsigned = {key: proof[key] for key in ('nonce', 'digest', 'provider_digest', 'operation', 'index', 'controls_digest')}
        encoded = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()
        with self._lock:
            if (self._closed or len(self._seen) >= 1024 or proof['nonce'] in self._seen
                    or not hmac.compare_digest(proof['mac'], hmac.new(bytes(self._secret), encoded, 'sha256').hexdigest())
                    or not hmac.compare_digest(proof['digest'], _wire_digest(row).hex())
                    or not hmac.compare_digest(proof['provider_digest'], _wire_digest(provider_body).hex())
                    or not hmac.compare_digest(proof['controls_digest'], _wire_digest(provider_controls).hex())):
                raise ContractError('Invalid structured worker admission')
            self._seen.add(proof['nonce'])
        with ExitStack() as scopes:
            if proof['index'] is not None:
                try:
                    raw_message = row['messages'][proof['index']]
                    message = message_from_wire(raw_message)
                    if len(message.calls) != 1:
                        raise ValueError()
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ContractError('Invalid structured worker admission') from None
                permit = VerifiedStructuredReplay(_CONSTRUCTION, bytes.fromhex(proof['digest']), proof['index'], message.calls[0])
                scopes.enter_context(bind_verified_replay(permit))
            request = request_from_wire(row)
            if request.payload.structured is None or request.payload.structured.encoding != 'function_calling':
                raise ContractError('Invalid structured worker admission')
            scopes.enter_context(_new_admission_authority(self).bind(self, request, selection))
            yield request

    def close(self):
        with self._lock:
            self._closed = True
            self._seen.clear()
            for index in range(len(self._secret)):
                self._secret[index] = 0


_CAPTURE_OMIT = object()
_CAPTURE_INTERNAL_KEYS = frozenset({
    'proposal_replay', 'structured_proposal_receipt', 'structured_worker_admission',
    'structured_replay_proof', 'replay_authority', 'owner_receipt',
    'continuation', 'continuation_state', 'api_agent_continuation',
    'encrypted_content', 'encrypted_reasoning', 'redacted_thinking',
    'hidden_reasoning', 'internal_reasoning', 'conversation_state',
    'resume_state', 'checkpoint_state', 'redacted_content',
})
_CAPTURE_REASONING_TYPES = frozenset({'thinking', 'reasoning', 'reasoning_text', 'reasoning_summary'})


def proposal_capture_projection(value):
    """Bounded provider content copy, excluding always-secret control state.

    This does not decide whether content capture is enabled; the existing
    telemetry boundary owns that decision. Tool input data is not interpreted
    as framework control fields, and opaque reasoning signatures are never
    projected. Failure must make optional capture unavailable, not inference.
    """
    import math
    import re
    count, size = 0, 0

    def normalized(key):
        return re.sub(r'(?<=[a-z])(?=[A-Z])', '_', key).lower().replace('-', '_')

    def walk(item, depth, *, payload=False, reasoning=False, native_tool=False):
        nonlocal count, size
        count += 1
        if count > 16384 or depth > 32:
            raise ContractError('Structured capture projection exceeds its bound')
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ContractError('Invalid structured capture projection')
            return item
        if type(item) is str:
            if len(item) > 4194304 - size:
                raise ContractError('Structured capture projection exceeds its bound')
            size += len(item.encode('utf-8'))
            if size > 4194304:
                raise ContractError('Structured capture projection exceeds its bound')
            return item
        if type(item) in (list, tuple):
            output = []
            for value in item:
                child = walk(value, depth + 1, payload=payload, reasoning=reasoning, native_tool=native_tool)
                if child is not _CAPTURE_OMIT:
                    output.append(child)
            return output
        if type(item) is not dict or any(type(key) is not str for key in item):
            raise ContractError('Invalid structured capture projection')
        kind = item.get('type') if not payload else None
        if kind in ('redacted_thinking', 'continuation', 'compaction'):
            return _CAPTURE_OMIT
        current_reasoning = reasoning or kind in _CAPTURE_REASONING_TYPES
        output = {}
        for key, value in item.items():
            if len(key) > 4194304 - size:
                raise ContractError('Structured capture projection exceeds its bound')
            size += len(key.encode('utf-8'))
            if size > 4194304:
                raise ContractError('Structured capture projection exceeds its bound')
            name = normalized(key)
            if not payload and (name in _CAPTURE_INTERNAL_KEYS or (current_reasoning and name in ('signature', 'encrypted_content', 'data'))):
                continue
            is_payload = payload or ((kind in ('tool_use', 'function_call') or native_tool) and name in ('input', 'arguments'))
            child_reasoning = current_reasoning or (not payload and name in ('reasoning_content', 'reasoning_text', 'reasoning'))
            output[key] = walk(value, depth + 1, payload=is_payload, reasoning=child_reasoning,
                               native_tool=not payload and name == 'tool_use')
            if output[key] is _CAPTURE_OMIT:
                del output[key]
        return output

    try:
        projected = walk(value, 0)
    except (UnicodeError, RecursionError):
        raise ContractError('Invalid structured capture projection') from None
    if projected is _CAPTURE_OMIT:
        raise ContractError('Invalid structured capture projection')
    return projected
