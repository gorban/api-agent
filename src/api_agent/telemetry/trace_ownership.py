"""Bounded, authenticated ownership receipts for a single setup inference call."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import hmac
import json
import re

from api_agent.providers.contracts import ContractError

_HANDOFF = ContextVar('remote_trace_handoff', default=None)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _hex(value, size):
    return type(value) is str and re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is not None


def validate_parent(value):
    if (type(value) is not dict or set(value) != {'version', 'trace_id', 'span_id', 'trace_flags'}
            or type(value['version']) is not int or value['version'] != 1
            or not _hex(value['trace_id'], 32) or int(value['trace_id'], 16) == 0
            or not _hex(value['span_id'], 16) or int(value['span_id'], 16) == 0
            or type(value['trace_flags']) is not int or value['trace_flags'] not in (0, 1)):
        raise ContractError('Invalid trace parent')
    return dict(value)


def validate_offer(value):
    if (type(value) is not dict or set(value) != {'parent', 'pipeline', 'nonce'}
            or not _hex(value['pipeline'], 64) or not _hex(value['nonce'], 64)):
        raise ContractError('Invalid trace ownership offer')
    return dict(parent=validate_parent(value['parent']), pipeline=value['pipeline'], nonce=value['nonce'])


def receipt(capability, offer, sequence, request, binding, state):
    if state not in ('granted', 'inactive') or type(sequence) is not int or sequence < 1:
        raise ContractError('Invalid trace ownership receipt')
    offer = validate_offer(offer)
    row = dict(version=1, status='trace_ownership', state=state, call_sequence=sequence,
               request_sha256=digest(request), binding_sha256=digest(binding), offer_sha256=digest(offer))
    row['proof'] = hmac.new(capability.encode(), ('trace-ownership:' + digest(row)).encode(), sha256).hexdigest()
    return row


def accept_receipt(row, capability, offer, sequence, request, binding):
    if type(row) is not dict or row.get('state') not in ('granted', 'inactive'):
        raise ContractError('Invalid trace ownership receipt')
    expected = receipt(capability, offer, sequence, request, binding, row['state'])
    if (set(row) != set(expected) or type(row.get('proof')) is not str
            or type(row.get('version')) is not int or type(row.get('call_sequence')) is not int
            or not hmac.compare_digest(row['proof'], expected['proof'])
            or any(row[key] != value for key, value in expected.items() if key != 'proof')):
        raise ContractError('Invalid trace ownership receipt')
    return row['state']


class RemoteHandoff:
    """One-shot grant: the host creates its SDK span before acknowledging ownership."""
    def __init__(self, offer, acknowledge):
        self.offer = validate_offer(offer)
        self.acknowledge = acknowledge
        self.state = None

    def begin(self, owner, call_id):
        if self.state is not None:
            return None
        if not owner.enabled or owner.runtime.pipeline_id != self.offer['pipeline']:
            self.settle()
            return None
        call = owner.begin(call_id, parent=self.offer['parent'])
        if call is None or call.span.export_context() is None:
            if call is not None:
                call.end(failed=True)
            self.settle()
            return None
        # Set ownership before the write: a failed/lost grant remains ambiguous,
        # and must never authorize another SDK generation on either participant.
        self.state = 'granted'
        try:
            self.acknowledge(self.state)
        except BaseException:
            call.end(failed=True)
            raise
        return call

    def settle(self):
        if self.state is None:
            self.state = 'inactive'
            self.acknowledge(self.state)


@contextmanager
def bind_handoff(handoff):
    token = _HANDOFF.set(handoff)
    try:
        yield
    finally:
        _HANDOFF.reset(token)


def begin_remote(owner, call_id):
    handoff = _HANDOFF.get()
    return owner.begin(call_id) if handoff is None else handoff.begin(owner, call_id)


def settle_trace_ownership():
    handoff = _HANDOFF.get()
    if handoff is not None:
        handoff.settle()
