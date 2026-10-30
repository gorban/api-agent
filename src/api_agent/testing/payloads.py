"""Occurrence-owned JSON payloads shared by recording and history readers."""
from copy import deepcopy
import hashlib
import json
import math
import os
from threading import RLock
import weakref
from uuid import uuid4

MAX_PAYLOAD = 64 * 1024
MAX_JOURNAL = 5 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_TOTAL = 4 * 1024 * 1024
MAX_DESCRIPTORS = 1024 * 1024
CHUNK_SIZE = 200 * 1024


class PayloadLimitError(ValueError):
    """A permitted payload exceeds the bounded storage budget."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _json_value(value, *, max_bytes=MAX_PAYLOAD):
    budget = [2048]
    text_budget = [max_bytes]
    def visit(item, depth):
        budget[0] -= 1
        if depth > 8 or budget[0] < 0:
            raise ValueError('Payload nesting or item limit exceeded')
        if type(item) is dict:
            if len(item) > 2048 or any(type(key) is not str or len(key) > 128 for key in item):
                raise ValueError('Invalid payload object')
            text_budget[0] -= sum(map(len, item))
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            if len(item) > 2048:
                raise ValueError('Payload list limit exceeded')
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            text_budget[0] -= len(item)
        elif not (item is None or type(item) is bool
                  or type(item) is int and abs(item) <= (1 << 53) - 1
                  or type(item) is float and math.isfinite(item)):
            raise ValueError('Payload requires finite JSON values')
        if text_budget[0] < 0:
            raise PayloadLimitError('Payload byte limit exceeded')
    visit(value, 0)
    body = encoded(value)
    if len(body) > max_bytes:
        raise PayloadLimitError('Payload byte limit exceeded')
    return body


def _source(name):
    if (type(name) is not str or not name or name in {'.', '..', 'manifest.json'}
            or any(char in name for char in '/\\\0')):
        raise ValueError('Invalid payload source')
    return name


def _valid_owner(owner):
    return (owner is None or type(owner) is str
            or type(owner) is dict and set(owner) == {'invocation_id', 'visit_id'}
            and all(type(value) is str and value for value in owner.values()))


def _capture_occurrence(packet, event):
    if event.get('tool_invocation_id') is not None:
        from api_agent.workflows.tool_observations import tool_occurrence
        return tool_occurrence(packet, event)
    return dict(kind='node', invocation_id=packet['invocation_id'],
                event_id=event.get('event_id', event.get('sequence')), direction=event['direction'])


def validate_payload_index(index, manifest):
    """Validate an occurrence index without fetching or exposing its values."""
    if (type(index) is not dict or set(index) != {'version', 'test_id', 'entries'}
            or type(index['version']) is not int or index['version'] != 1
            or type(index['test_id']) is not str or not index['test_id']
            or type(index['entries']) is not list or len(index['entries']) > MAX_ENTRIES):
        raise ValueError('Invalid payload index')
    seen, wire_occurrences, sources, total = set(), set(), set(), 0
    tool_phases, tool_owners = set(), {}
    for entry in index['entries']:
        if type(entry) is not dict or type(entry.get('occurrence')) is not dict:
            raise ValueError('Invalid payload index entry')
        _json_value(entry['occurrence'])
        key = encoded(entry['occurrence'])
        if key in seen or entry.get('test_id') != index['test_id']:
            raise ValueError('Duplicate or detached payload occurrence')
        seen.add(key)
        occurrence = entry['occurrence']
        if occurrence.get('kind') == 'tool':
            from uuid import UUID
            if (set(occurrence) != {'kind', 'invocation_id', 'event_id', 'visit_id',
                    'attempt_id', 'tool_invocation_id', 'phase'}
                    or occurrence['phase'] not in ('request', 'response')
                    or type(occurrence['visit_id']) is not str or not occurrence['visit_id']
                    or occurrence['attempt_id'] is not None and (type(occurrence['attempt_id']) is not str
                        or not 0 < len(occurrence['attempt_id']) <= 64)):
                raise ValueError('Invalid tool payload occurrence')
            try:
                for name in ('invocation_id', 'event_id', 'tool_invocation_id'):
                    if type(occurrence[name]) is not str or str(UUID(occurrence[name])) != occurrence[name]:
                        raise ValueError()
            except ValueError:
                raise ValueError('Invalid tool payload identity') from None
            owner = (occurrence['invocation_id'], occurrence['visit_id'], occurrence['attempt_id'])
            previous = tool_owners.setdefault(occurrence['tool_invocation_id'], owner)
            phase = (occurrence['tool_invocation_id'], occurrence['phase'])
            if previous != owner or phase in tool_phases:
                raise ValueError('Conflicting tool payload owner or phase')
            tool_phases.add(phase)
            if entry.get('kind') != 'omitted' and entry.get('owner') != occurrence['visit_id']:
                raise ValueError('Tool payload lacks its node owner')
        if occurrence.get('kind') == 'llm':
            if occurrence.get('capture_source') not in (None, 'fallback', 'provider'):
                raise ValueError('Invalid provider capture source')
            identity = encoded({name: value for name, value in occurrence.items() if name != 'capture_source'})
            if identity in wire_occurrences:
                raise ValueError('Conflicting wire capture source')
            wire_occurrences.add(identity)
        if entry.get('kind') == 'omitted':
            if (set(entry) != {'kind', 'test_id', 'occurrence', 'reason'}
                    or entry['reason'] not in {'withheld', 'unavailable', 'truncated'}):
                raise ValueError('Invalid omitted occurrence')
            continue
        if (set(entry) != {'kind', 'source', 'sha256', 'length', 'test_id', 'occurrence', 'owner', 'reason'}
                or entry.get('kind') not in {'payload', 'chunked'} or type(entry['length']) is not int
                or not 0 <= entry['length'] <= (MAX_PAYLOAD if entry['kind'] == 'payload' else MAX_TOTAL)
                or entry['reason'] not in {'captured', 'partial', 'truncated', 'redacted'}
                or not _valid_owner(entry['owner'])
                or manifest['files'].get(_source(entry['source'])) != entry['sha256']):
            raise ValueError('Unbound payload index entry')
        if entry['source'] in sources:
            raise ValueError('Payload source has multiple occurrence owners')
        sources.add(entry['source'])
        total += entry['length']
        if total > MAX_TOTAL:
            raise ValueError('Payload index aggregate limit exceeded')
    return deepcopy(index)


class PayloadResolver:
    """Resolve only digest-bound payload files from an admitted run manifest."""
    def __init__(self, manifest, read):
        from .allure_source import validate_manifest
        validate_manifest(manifest, preview='preview_version' in manifest)
        self.files = dict(manifest['files'])
        self.read = read
        self.remaining = 4 * 1024 * 1024
        self.descriptor_remaining = MAX_DESCRIPTORS
        self.index_remaining = 4 * 1024 * 1024
        self._indexed = {}
        self._occurrences = {}
        self._chunk_owners = {}
        self._resolved = {}
        self._closed = False
        self._finalizer = None

    def _check_open(self):
        if self._closed:
            raise ValueError('Payload resolver is closed')

    def close(self):
        self._closed = True
        if self._finalizer is not None:
            self._finalizer()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()

    def __call__(self, ref):
        self._check_open()
        if (type(ref) is not dict or set(ref) - {'kind', 'source', 'sha256', 'length', 'projection', 'fields'}
                or ref.get('kind') not in {'payload', 'chunked'} or type(ref.get('length')) is not int
                or not 0 <= ref['length'] <= (MAX_PAYLOAD if ref['kind'] == 'payload' else MAX_TOTAL)):
            raise ValueError('Invalid payload reference')
        name = _source(ref.get('source'))
        if self.files.get(name) != ref.get('sha256'):
            raise ValueError('Payload is not bound to this manifest')
        entry = self._indexed.get(name)
        if entry is not None and any(entry[key] != ref.get(key) for key in ('kind', 'source', 'sha256', 'length')):
            raise ValueError('Payload reference differs from its index')
        cached = self._resolved.get(name)
        if cached is not None:
            if cached != (ref['kind'], ref['sha256'], ref['length']):
                raise ValueError('Conflicting cached payload reference')
        available = MAX_TOTAL if cached is not None else self.remaining
        descriptor_available = MAX_DESCRIPTORS if cached is not None else self.descriptor_remaining
        body = self.read(name)
        if (type(body) is not bytes or len(body) > MAX_PAYLOAD
                or hashlib.sha256(body).hexdigest() != ref['sha256']):
            raise ValueError('Payload digest or retrieval limit exceeded')
        from api_agent.workflows.contrived import strict_json
        overhead = 0
        names = set()
        if ref['kind'] == 'chunked':
            descriptor = strict_json(body)
            if (type(descriptor) is not dict or set(descriptor) != {'version', 'length', 'sha256', 'chunks'}
                    or type(descriptor['version']) is not int or descriptor['version'] != 1
                    or type(descriptor['length']) is not int or descriptor['length'] != ref['length']
                    or type(descriptor['chunks']) is not list or not descriptor['chunks']
                    or len(descriptor['chunks']) > 22):
                raise ValueError('Invalid chunk descriptor')
            chunks, names, size = [], set(), 0
            overhead = len(body)
            for chunk in descriptor['chunks']:
                if (type(chunk) is not dict or set(chunk) != {'source', 'sha256', 'length'}
                        or type(chunk['length']) is not int or not 0 < chunk['length'] <= CHUNK_SIZE
                        or chunk['source'] in names or chunk['source'] == name
                        or self.files.get(_source(chunk['source'])) != chunk['sha256']):
                    raise ValueError('Invalid chunk binding')
                names.add(chunk['source'])
                if self._chunk_owners.get(chunk['source'], name) != name:
                    raise ValueError('Chunk has multiple payload owners')
                if chunk['source'] in self._indexed:
                    raise ValueError('Chunk is an indexed payload source')
                size += chunk['length']
                if size > available or size > ref['length'] or overhead > descriptor_available:
                    raise ValueError('Chunk retrieval limit exceeded')
                part = self.read(chunk['source'])
                if (type(part) is not bytes or len(part) != chunk['length']
                        or hashlib.sha256(part).hexdigest() != chunk['sha256']):
                    raise ValueError('Chunk digest or length mismatch')
                part.decode('utf-8')
                chunks.append(part)
            body = b''.join(chunks)
            if len(body) != ref['length'] or hashlib.sha256(body).hexdigest() != descriptor['sha256']:
                raise ValueError('Chunk assembly mismatch')
        if len(body) != ref['length'] or len(body) > available or overhead > descriptor_available:
            raise ValueError('Payload digest or retrieval limit exceeded')
        value = strict_json(body)
        _json_value(value, max_bytes=MAX_TOTAL if ref['kind'] == 'chunked' else MAX_PAYLOAD)
        if encoded(value) != body:
            raise ValueError('Payload encoding is not canonical')
        self._chunk_owners.update({chunk_name: name for chunk_name in names})
        if cached is None:
            self.remaining -= len(body)
            self.descriptor_remaining -= overhead
            self._resolved[name] = (ref['kind'], ref['sha256'], ref['length'])
        return value

    def index(self, source):
        """Read one bounded index through the same immutable manifest binding."""
        self._check_open()
        source = _source(source)
        if source not in self.files:
            raise ValueError('Payload index is not bound to this manifest')
        body = self.read(source)
        if (type(body) is not bytes or len(body) > 1024 * 1024 or len(body) > self.index_remaining
                or hashlib.sha256(body).hexdigest() != self.files[source]):
            raise ValueError('Payload index digest or retrieval limit exceeded')
        from api_agent.workflows.contrived import strict_json
        index = validate_payload_index(strict_json(body), {'files': self.files})
        indexed, occurrences = dict(self._indexed), dict(self._occurrences)
        for entry in index['entries']:
            key = encoded(dict(test_id=entry['test_id'], occurrence=entry['occurrence']))
            previous = occurrences.setdefault(key, entry)
            if previous != entry:
                raise ValueError('Conflicting payload occurrence ownership')
            if entry['kind'] in {'payload', 'chunked'}:
                if entry['source'] in self._chunk_owners:
                    raise ValueError('Payload source is already a chunk')
                previous = indexed.setdefault(entry['source'], entry)
                if previous != entry:
                    raise ValueError('Conflicting payload source ownership')
        totals = {}
        for entry in occurrences.values():
            if entry['kind'] in {'payload', 'chunked'}:
                totals[entry['test_id']] = totals.get(entry['test_id'], 0) + entry['length']
                if totals[entry['test_id']] > MAX_TOTAL:
                    raise ValueError('Payload index aggregate limit exceeded')
        self._indexed, self._occurrences = indexed, occurrences
        self.index_remaining -= len(body)
        return deepcopy(index)

    def verify_occurrence(self, ref, journal, event, *, recording_test_id):
        """Require the index to bind a journal projection to its original owner."""
        entry = self._indexed.get(ref.get('source'))
        if entry is None or entry['test_id'] != recording_test_id:
            raise ValueError('Payload lacks the admitted test owner')
        if 'projection' in ref:
            phase = 'request' if event['direction'] == 'input' else 'response'
            expected = dict(kind='llm', call_id=event.get('cause_id'), phase=phase)
            if event.get('transport_attempt_id') is not None:
                expected['transport_attempt_id'] = event['transport_attempt_id']
            if event.get('capture_source') is not None:
                expected['capture_source'] = event['capture_source']
            if ref['projection'] != ('llm_request' if phase == 'request' else 'llm_response'):
                raise ValueError('Payload projection direction mismatch')
        else:
            expected = _capture_occurrence(journal, event)
            if entry['owner'] != event.get('visit_id'):
                raise ValueError('Payload node owner mismatch')
        if entry['occurrence'] != expected:
            raise ValueError('Payload occurrence does not match journal')
        if 'projection' in ref and entry['owner'] != dict(invocation_id=journal['invocation_id'], visit_id=event['visit_id']):
            raise ValueError('Payload wire lacks the admitted invocation and visit owner')

    def verify_node_omission(self, journal, event, *, recording_test_id):
        occurrence = _capture_occurrence(journal, event)
        key = encoded(dict(test_id=recording_test_id, occurrence=occurrence))
        entry = self._occurrences.get(key)
        if entry is None or entry.get('kind') != 'omitted' or entry.get('reason') != 'truncated':
            raise ValueError('Node omission lacks its admitted occurrence')

    @classmethod
    def local(cls, directory, manifest):
        from pathlib import Path
        from .storage import RunStorage
        parts = Path(directory).absolute().parts
        descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except OSError:
            os.close(descriptor)
            raise ValueError('Symlinked or unavailable payload directory') from None
        holder, lock = [descriptor], RLock()
        def close_directory():
            with lock:
                descriptor, holder[0] = holder[0], None
                os.close(descriptor)
        def read(name):
            with lock:
                if holder[0] is None:
                    raise ValueError('Payload resolver is closed')
                return RunStorage._read_source(_source(name), directory_fd=holder[0], max_bytes=1024 * 1024)
        try:
            resolver = cls(manifest, read)
        except Exception:
            close_directory()
            raise
        resolver._finalizer = weakref.finalize(resolver, close_directory)
        return resolver


def hydrate_journal(packet, resolver, **validation):
    """Hydrate atomically, preserving resolver state if journal validation fails."""
    if not isinstance(resolver, PayloadResolver):
        return _hydrate_journal(packet, resolver, **validation)
    state = (resolver.remaining, resolver.descriptor_remaining, dict(resolver._chunk_owners), dict(resolver._resolved))
    try:
        return _hydrate_journal(packet, resolver, **validation)
    except BaseException:
        resolver.remaining, resolver.descriptor_remaining, resolver._chunk_owners, resolver._resolved = state
        raise


def _hydrate_journal(packet, resolver, **validation):
    """Restore the original digest-bound journal before applying its admission rules."""
    from api_agent.workflows.journal import validate_journal
    if type(packet) is dict and packet.get('version') == 1:
        return validate_journal(packet, **validation)
    if type(packet) is not dict or packet.get('version') not in (2, 3) or packet.get('kind') != 'workflow_journal':
        raise ValueError('Invalid durable journal')
    if len(encoded(packet)) > MAX_JOURNAL:
        raise ValueError('Durable journal exceeds limit')
    unsigned = {key: value for key, value in packet.items() if key != 'sha256'}
    if _digest(unsigned) != packet.get('sha256'):
        raise ValueError('Durable journal digest mismatch')
    journal = deepcopy(packet)
    journal['version'] = 1
    if packet['version'] == 3:
        original = journal.pop('unverified_original_sha256', None)
        if (type(original) is not str or len(original) != 64
                or any(c not in '0123456789abcdef' for c in original)
                or journal.pop('evidence_status', None) != 'incomplete'):
            raise ValueError('Invalid incomplete journal declaration')
        journal['sha256'] = journal.pop('reduced_sha256')
    else:
        journal['sha256'] = journal.pop('original_sha256')
    journal.pop('kind')
    recording_test_id = journal.pop('recording_test_id')
    if type(recording_test_id) is not str or not recording_test_id:
        raise ValueError('Invalid durable recording owner')
    for event in journal['events']:
        if 'payload' in event:
            if 'values' in event:
                raise ValueError('Duplicate journal values')
            ref = event.pop('payload')
            if ref == {'kind': 'omitted', 'reason': 'truncated', 'node': True}:
                if (packet['version'] != 3 or event.get('event') != 'capture'
                        or event.get('direction') not in {'input', 'output'}
                        or event.get('reason') != 'truncated'):
                    raise ValueError('Invalid omitted node payload')
                verify = getattr(resolver, 'verify_node_omission', None)
                if verify is not None:
                    verify(journal, event, recording_test_id=recording_test_id)
                continue
            if (type(ref) is dict and set(ref) == {'kind', 'reason', 'canonical'}
                    and ref['kind'] == 'omitted' and ref['reason'] == event.get('reason') == 'truncated'):
                canonical = ref['canonical']
                verify = getattr(resolver, 'verify_occurrence', None)
                if verify is None:
                    raise ValueError('Canonical omission requires an indexed resolver')
                verify(canonical, journal, event, recording_test_id=recording_test_id)
                resolver(canonical)
                event['values'] = {}
                continue
            if ref == {'kind': 'omitted', 'reason': event.get('reason')}:
                if event.get('reason') not in {'withheld', 'unavailable', 'truncated'}:
                    raise ValueError('Invalid omitted payload')
                event['values'] = {}
                continue
            if (type(ref) is dict and set(ref) == {'kind', 'reason', 'projection'}
                    and ref['kind'] == 'omitted' and ref['reason'] == event.get('reason')
                    and ref['reason'] in {'truncated', 'unavailable', 'withheld'}
                    and ref['projection'] in {'llm_request', 'llm_response'}):
                event['values'] = {ref['projection']: {}}
                continue
            verify = getattr(resolver, 'verify_occurrence', None)
            if verify is not None:
                verify(ref, journal, event, recording_test_id=recording_test_id)
            value = resolver(ref)
            if 'projection' in ref:
                if ref['projection'] not in {'llm_request', 'llm_response'} or type(value) is not dict:
                    raise ValueError('Invalid wire projection')
                if 'fields' in ref:
                    fields = ref['fields']
                    if (type(fields) is not list or len(fields) > 32
                            or any(type(key) is not str or key not in value for key in fields)
                            or len(set(fields)) != len(fields)):
                        raise ValueError('Invalid wire fields')
                    value = {key: value[key] for key in fields}
                value = {ref['projection']: value}
            event['values'] = value
    return validate_journal(journal, **validation)


class PayloadStore:
    def __init__(self, recorder, test_id, *, journal_test_id=None):
        if type(test_id) is not str or not test_id:
            raise ValueError('Payload store requires a test owner')
        self.recorder, self.test_id = recorder, test_id
        if journal_test_id is not None and (type(journal_test_id) is not str or not journal_test_id):
            raise ValueError('Invalid journal test owner alias')
        self._journal_test_id = journal_test_id
        self._entries, self._values = {}, {}
        self._digests, self._parts = {}, {}
        self._total = 0
        self._descriptor_total = 0
        self._call_owners = {}

    def put(self, occurrence, value, *, owner=None, reason='captured'):
        if type(occurrence) is not dict or not occurrence:
            raise ValueError('Payload occurrence is required')
        _json_value(occurrence)
        if (reason not in {'captured', 'partial', 'truncated', 'redacted'}
                or not _valid_owner(owner)):
            raise ValueError('Invalid payload ownership or reason')
        key = encoded(dict(test_id=self.test_id, occurrence=occurrence))
        body = _json_value(value, max_bytes=MAX_TOTAL)
        digest = hashlib.sha256(body).hexdigest()
        if key in self._entries:
            if (self._digests.get(key) != digest
                    or self._entries[key].get('owner') != owner
                    or self._entries[key].get('reason') != reason):
                raise ValueError('Conflicting payload occurrence')
            return {name: self._entries[key][name] for name in ('kind', 'source', 'sha256', 'length')}
        if len(self._entries) >= MAX_ENTRIES:
            raise ValueError('Payload occurrence limit exceeded')
        parts, overhead = [], 0
        if len(body) > MAX_PAYLOAD:
            offset = 0
            while offset < len(body):
                end = min(offset + CHUNK_SIZE, len(body))
                while end < len(body) and body[end] & 0xc0 == 0x80:
                    end -= 1
                part = body[offset:end]
                parts.append((f'payload-{uuid4()}.part', part))
                offset = end
            descriptor = encoded(dict(version=1, length=len(body), sha256=digest,
                chunks=[dict(source=name, sha256=hashlib.sha256(part).hexdigest(), length=len(part))
                        for name, part in parts]))
            overhead = len(descriptor)
        if self._total + len(body) > MAX_TOTAL or self._descriptor_total + overhead > MAX_DESCRIPTORS:
            raise PayloadLimitError('Payload aggregate limit exceeded')
        if parts:
            written = []
            try:
                for part_name, part in parts:
                    self.recorder.record_artifact(part_name, part)
                    written.append(part_name)
                source = self.recorder.record_artifact(f'payload-{uuid4()}.json', descriptor)
            except BaseException:
                self.recorder.discard_artifacts(written)
                raise
            ref = dict(kind='chunked', source=source, sha256=hashlib.sha256(descriptor).hexdigest(), length=len(body))
            self._parts[source] = tuple(name for name, _ in parts)
        else:
            source = self.recorder.record_artifact(f'payload-{uuid4()}.json', body)
            ref = dict(kind='payload', source=source, sha256=digest, length=len(body))
        self._entries[key] = dict(ref, test_id=self.test_id, occurrence=deepcopy(occurrence), owner=deepcopy(owner), reason=reason)
        self._values[source] = deepcopy(value)
        self._digests[key] = digest
        self._total += len(body)
        self._descriptor_total += overhead
        return ref

    def native_parts(self, ref):
        """Return the ordered, store-owned sources for a payload reference."""
        if not any(all(entry.get(key) == ref.get(key) for key in ('kind', 'source', 'sha256', 'length'))
                   for entry in self._entries.values()):
            raise ValueError('Unknown payload reference')
        return self._parts[ref['source']] if ref['kind'] == 'chunked' else (ref['source'],)

    def _omit(self, occurrence, reason):
        key = encoded(dict(test_id=self.test_id, occurrence=occurrence))
        entry = dict(kind='omitted', test_id=self.test_id, occurrence=deepcopy(occurrence), reason=reason)
        if key in self._entries and self._entries[key] != entry:
            raise ValueError('Conflicting payload occurrence')
        if key not in self._entries and len(self._entries) >= MAX_ENTRIES:
            raise ValueError('Payload occurrence limit exceeded')
        self._entries[key] = entry

    def _check_wire_source(self, occurrence):
        for entry in self._entries.values():
            prior = entry['occurrence']
            if (prior.get('kind') == 'llm' and all(prior.get(key) == occurrence.get(key)
                    for key in ('call_id', 'phase', 'transport_attempt_id'))
                    and prior.get('capture_source') != occurrence.get('capture_source')):
                raise ValueError('Conflicting wire capture source')

    def put_wire(self, call_id, phase, value, reason='captured', transport_attempt_id=None, capture_source=None):
        from uuid import UUID
        if type(call_id) is not str or str(UUID(call_id)) != call_id or phase not in {'request', 'response'}:
            raise ValueError('Invalid wire occurrence')
        occurrence = dict(kind='llm', call_id=call_id, phase=phase)
        if capture_source is not None:
            if capture_source not in ('fallback', 'provider'):
                raise ValueError('Invalid provider capture source')
            occurrence['capture_source'] = capture_source
        if transport_attempt_id is not None:
            if type(transport_attempt_id) is not str or str(UUID(transport_attempt_id)) != transport_attempt_id:
                raise ValueError('Invalid transport attempt identity')
            occurrence['transport_attempt_id'] = transport_attempt_id
        self._check_wire_source(occurrence)
        if reason in {'withheld', 'unavailable'} or reason == 'truncated' and value == {}:
            self._omit(occurrence, reason)
            return None
        claim = self._call_owners.get(call_id)
        owner = dict(invocation_id=claim[0], visit_id=claim[1]) if claim is not None else None
        return self.put(occurrence, value, reason=reason, owner=owner)

    def omit_wire(self, call_id, phase, reason='truncated', transport_attempt_id=None, capture_source=None):
        from uuid import UUID
        if type(call_id) is not str or str(UUID(call_id)) != call_id or phase not in {'request', 'response'}:
            raise ValueError('Invalid wire occurrence')
        occurrence = dict(kind='llm', call_id=call_id, phase=phase)
        if capture_source is not None:
            if capture_source not in ('fallback', 'provider'):
                raise ValueError('Invalid provider capture source')
            occurrence['capture_source'] = capture_source
        if transport_attempt_id is not None:
            if type(transport_attempt_id) is not str or str(UUID(transport_attempt_id)) != transport_attempt_id:
                raise ValueError('Invalid transport attempt identity')
            occurrence['transport_attempt_id'] = transport_attempt_id
        self._check_wire_source(occurrence)
        self._omit(occurrence, reason)

    def index(self):
        return dict(version=1, test_id=self.test_id, entries=deepcopy(list(self._entries.values())))

    def admit_journals(self, packets):
        """Preflight all test journals before any payload materialization."""
        from api_agent.workflows.journal import validate_journal
        claims = dict(self._call_owners)
        for number, packet in enumerate(packets):
            if number >= MAX_ENTRIES:
                raise ValueError('Journal ownership admission limit exceeded')
            validate_journal(packet)
            if packet['test_id'] not in {None, self.test_id, self._journal_test_id}:
                raise ValueError('Journal belongs to another test owner')
            for event in packet['events']:
                call_id = event.get('call_id') if event['event'] == 'call' else event.get('cause_id')
                if call_id is None:
                    continue
                owner = (packet['invocation_id'], event['visit_id'])
                previous = claims.setdefault(call_id, owner)
                if previous != owner:
                    raise ValueError('Journal call correlation has a different invocation or visit owner')
                if len(claims) > MAX_ENTRIES:
                    raise ValueError('Journal call ownership limit exceeded')
        entries = dict(self._entries)
        for key, entry in entries.items():
            occurrence = entry['occurrence']
            if entry['kind'] in {'payload', 'chunked'} and occurrence.get('kind') == 'llm' and occurrence['call_id'] in claims:
                claim = claims[occurrence['call_id']]
                entries[key] = dict(entry, owner=dict(invocation_id=claim[0], visit_id=claim[1]))
        self._call_owners, self._entries = claims, entries

    def materialize_journal(self, packet):
        self.admit_journals((packet,))
        result = deepcopy(packet)
        reduced = deepcopy(packet)
        incomplete = False
        for event, reduced_event in zip(result['events'], reduced['events']):
            if 'values' not in event:
                continue
            values = event.pop('values')
            if values == {} and event.get('reason') in {'withheld', 'unavailable', 'truncated'}:
                event['payload'] = dict(kind='omitted', reason=event['reason'])
                cause = event.get('cause_id')
                phase = 'request' if event.get('direction') == 'input' else 'response'
                occurrence = dict(kind='llm', call_id=cause, phase=phase)
                if event.get('transport_attempt_id') is not None:
                    occurrence['transport_attempt_id'] = event['transport_attempt_id']
                if event.get('capture_source') is not None:
                    occurrence['capture_source'] = event['capture_source']
                key = encoded(dict(test_id=self.test_id, occurrence=occurrence))
                entry = self._entries.get(key)
                if cause and event['reason'] == 'truncated' and entry and entry['kind'] in {'payload', 'chunked'}:
                    ref = {name: entry[name] for name in ('kind', 'source', 'sha256', 'length')}
                    ref['projection'] = 'llm_request' if phase == 'request' else 'llm_response'
                    event['payload']['canonical'] = ref
                self._omit(_capture_occurrence(packet, event), event['reason'])
                continue
            cause = event.get('cause_id')
            phase = 'request' if event.get('direction') == 'input' else 'response'
            projection = 'llm_request' if phase == 'request' else 'llm_response'
            if cause and type(values) is dict and set(values) == {projection}:
                occurrence = dict(kind='llm', call_id=cause, phase=phase)
                if event.get('transport_attempt_id') is not None:
                    occurrence['transport_attempt_id'] = event['transport_attempt_id']
                if event.get('capture_source') is not None:
                    occurrence['capture_source'] = event['capture_source']
                key = encoded(dict(test_id=self.test_id, occurrence=occurrence))
                if key not in self._entries:
                    raise ValueError('Journal wire has no authoritative payload')
                if self._entries[key]['kind'] not in {'payload', 'chunked'}:
                    if (values[projection] == {} and event.get('reason') in {'truncated', 'unavailable', 'withheld'}
                            and self._entries[key]['reason'] == event['reason']):
                        event['payload'] = dict(kind='omitted', reason=event['reason'], projection=projection)
                        continue
                    if self._entries[key]['reason'] == 'truncated':
                        event['reason'] = reduced_event['reason'] = 'truncated'
                        reduced_event['values'] = {projection: {}}
                        event['payload'] = dict(kind='omitted', reason='truncated', projection=projection)
                        incomplete = True
                        continue
                    raise ValueError('Journal wire has no authoritative payload')
                ref = {name: self._entries[key][name] for name in ('kind', 'source', 'sha256', 'length')}
                wire = self._values[ref['source']]
                subset = values[projection]
                if type(subset) is not dict or type(wire) is not dict or any(key not in wire or wire[key] != value for key, value in subset.items()):
                    raise ValueError('Journal wire differs from authoritative payload')
                ref['projection'] = projection
                if subset != wire:
                    ref['fields'] = sorted(subset)
            else:
                occurrence = _capture_occurrence(packet, event)
                try:
                    ref = self.put(occurrence, values, owner=event.get('visit_id'),
                                   reason=event.get('reason', 'captured'))
                except PayloadLimitError:
                    self._omit(occurrence, 'truncated')
                    event['reason'] = reduced_event['reason'] = 'truncated'
                    reduced_event.pop('values')
                    ref = dict(kind='omitted', reason='truncated', node=True)
                    incomplete = True
            event['payload'] = ref
        original = result.pop('sha256')
        if incomplete:
            reduced.pop('sha256')
            reduced['sha256'] = _digest(reduced)
            from api_agent.workflows.journal import validate_journal
            validate_journal(reduced)
            result.update(version=3, kind='workflow_journal', evidence_status='incomplete',
                          unverified_original_sha256=original, reduced_sha256=reduced['sha256'],
                          recording_test_id=self.test_id)
        else:
            result.update(version=2, kind='workflow_journal', original_sha256=original,
                          recording_test_id=self.test_id)
        result['sha256'] = _digest(result)
        if len(encoded(result)) > MAX_JOURNAL:
            raise ValueError('Durable journal exceeds limit')
        return result
