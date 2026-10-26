"""Explicit, bounded JSON attachments for recorded tests and execution owners."""
import json
import math
import os
from uuid import UUID

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

MAX_BYTES = 65_536
MAX_DEPTH = 16
MAX_ITEMS = 2_048
MAX_LABEL_BYTES = 1_024


def _json_bytes(value):
    count = 0
    active = set()

    def visit(item, depth):
        nonlocal count
        count += 1
        if count > MAX_ITEMS or depth > MAX_DEPTH:
            raise ValueError('Attachment exceeds JSON structure budget')
        kind = type(item)
        if item is None or kind in (str, bool):
            return
        if kind is int:
            if abs(item) > (1 << 53) - 1:
                raise ValueError('Attachment integer exceeds interoperable range')
            return
        if kind is float and math.isfinite(item):
            return
        if kind not in (dict, list):
            raise ValueError('Attachment requires finite JSON values')
        if id(item) in active:
            raise ValueError('Attachment contains a cycle')
        active.add(id(item))
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError('Attachment keys must be strings')
                visit(key, depth + 1)
                visit(child, depth + 1)
        else:
            for child in item:
                visit(child, depth + 1)
        active.remove(id(item))

    visit(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
    except UnicodeError as error:
        raise ValueError('Attachment requires valid Unicode') from error
    if len(encoded) > MAX_BYTES:
        raise ValueError('Attachment exceeds JSON byte budget')
    return encoded


def _uuid(value):
    if type(value) is not str:
        raise ValueError('Owner identifiers must be canonical UUID strings')
    try:
        canonical = str(UUID(value))
    except ValueError as error:
        raise ValueError('Owner identifiers must be canonical UUID strings') from error
    if canonical != value:
        raise ValueError('Owner identifiers must be canonical UUID strings')
    return value


def _owner(invocation_id=None, scope_id=None, call_id=None, trial_index=None):
    if invocation_id is not None:
        if call_id is not None or trial_index is not None:
            raise ValueError('Incompatible attachment owners')
        owner = {'kind': 'graph', 'invocation_id': _uuid(invocation_id)}
        if scope_id is not None:
            owner.update(kind='scope', scope_id=_uuid(scope_id))
        return owner
    if scope_id is not None:
        raise ValueError('Scope ownership requires an invocation')
    if call_id is not None:
        if trial_index is not None:
            raise ValueError('Incompatible attachment owners')
        return {'kind': 'call', 'call_id': _uuid(call_id)}
    if trial_index is not None:
        if type(trial_index) is not int or not 1 <= trial_index <= 60:
            raise ValueError('Trial index must be an integer from 1 through 60')
        return {'kind': 'trial', 'trial_index': trial_index}
    return {'kind': 'test'}


def _boundary(boundary):
    if not isinstance(boundary, EmissionBoundary):
        raise ValueError('Attachment requires its owning emission boundary')


def _safe_owner(owner, boundary):
    if boundary.for_policy(ContentPolicy(True)).export(owner) != owner:
        raise ValueError('Attachment owner violates credential boundary')


def _label(name, boundary):
    if type(name) is not str or not name.strip():
        raise ValueError('Attachment name must be a nonempty string')
    if len(_json_bytes(name)) > MAX_LABEL_BYTES:
        raise ValueError('Attachment name exceeds byte budget')
    safe = boundary.for_policy(ContentPolicy(True)).export({'label': name}).get('label')
    if type(safe) is not str or not safe.strip():
        safe = 'Attachment'
        if boundary.for_policy(ContentPolicy(True)).export({'label': safe}).get('label') != safe:
            raise ValueError('Attachment fallback name violates credential boundary')
    if len(_json_bytes(safe)) > MAX_LABEL_BYTES:
        raise ValueError('Attachment name exceeds byte budget')
    return safe


def publish(name, data, *, boundary, invocation_id=None, scope_id=None,
            call_id=None, trial_index=None):
    """Publish JSON using an explicit owner and the owner's live privacy boundary.

    Outside the integration event environment this validates inputs but writes
    nothing. No file attachment paths or arbitrary object conversion are supported.
    """
    _boundary(boundary)
    owner = _owner(invocation_id, scope_id, call_id, trial_index)
    _safe_owner(owner, boundary)
    label = _label(name, boundary)
    _json_bytes(data)
    packet = {'kind': 'scoped_attachment', 'version': 1, 'name': label,
              'owner': owner, 'content': 'withheld'}
    if boundary.policy.allow_content:
        packet.update(content='permitted_scrubbed',
                      value=boundary.export({'attachment': data}).get('attachment'))
    _json_bytes(packet)
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if path and node:
        envelope = {'kind': 'workflow_evidence', 'node': node.rsplit(' ', 1)[0], 'data': packet}
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False) + '\n')


def validate_attachment(packet, boundary, allow_content):
    """Validate an IPC packet again before returning its owner and attachment."""
    _boundary(boundary)
    _json_bytes(packet)
    base = {'kind', 'version', 'name', 'owner', 'content'}
    if type(packet) is not dict or set(packet) not in (base, base | {'value'}):
        raise ValueError('Invalid scoped attachment schema')
    if packet['kind'] != 'scoped_attachment' or type(packet['version']) is not int or packet['version'] != 1:
        raise ValueError('Invalid scoped attachment version')
    owner = packet['owner']
    if type(owner) is not dict or type(owner.get('kind')) is not str:
        raise ValueError('Invalid attachment owner')
    fields = {'graph': {'invocation_id'}, 'scope': {'invocation_id', 'scope_id'},
              'call': {'call_id'}, 'trial': {'trial_index'}, 'test': set()}
    expected = fields.get(owner['kind'])
    if expected is None or set(owner) != expected | {'kind'}:
        raise ValueError('Invalid attachment owner fields')
    normalized = _owner(**{key: owner[key] for key in expected})
    if owner != normalized:
        raise ValueError('Invalid attachment owner identity')
    _safe_owner(owner, boundary)
    label = _label(packet['name'], boundary)
    if label != packet['name']:
        raise ValueError('Attachment name violates credential boundary')
    if packet['content'] == 'withheld' and set(packet) == base:
        value = {'content': 'withheld'}
    elif packet['content'] == 'permitted_scrubbed' and set(packet) == base | {'value'}:
        if allow_content is not True or not boundary.policy.allow_content:
            raise ValueError('Attachment content is disabled')
        value = packet['value']
        safe = boundary.export({'attachment': value}).get('attachment')
        if _json_bytes(safe) != _json_bytes(value):
            raise ValueError('Attachment violates credential boundary')
    else:
        raise ValueError('Invalid attachment content policy')
    return dict(owner), (label, _json_bytes(value), 'application/json')
