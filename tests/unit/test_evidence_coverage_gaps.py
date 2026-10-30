"""Adversarial checks for evidence custody, attribution and worker bounds."""
import asyncio
import hashlib
import io
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def test_sidecar_overflow_retains_attempt_provenance(tmp_path):
    from api_agent.testing import inference_content as content
    path = str(tmp_path / 'evidence.db')
    node, call, attempt = 'test::case', str(uuid4()), str(uuid4())
    content.initialize_evidence(path, [node])
    row = dict(phase='request', content={'text': 'x' * (content.MAX_TEST_BYTES + 1)},
               transport_attempt_id=attempt, capture_source='provider')
    content._publish_sidecar(path, node, row, call)
    _, _, stored = content.read_evidence(path, 0)[0]
    assert stored == dict(call_id=call, phase='request', content={}, reason='truncated',
                          transport_attempt_id=attempt, capture_source='provider')


@pytest.mark.parametrize('corruption, message', [('length', 'framing'), ('digest', 'digest'), ('identity', 'identity')])
def test_sidecar_rejects_independently_corrupted_record(tmp_path, corruption, message):
    from api_agent.testing import inference_content as content
    path, node, call = str(tmp_path / 'evidence.db'), 'test::case', str(uuid4())
    content.initialize_evidence(path, [node])
    content._publish_sidecar(path, node, dict(phase='request', content={'text': 'safe'}), call)
    with closing(sqlite3.connect(path)) as db, db:
        if corruption == 'length':
            db.execute('UPDATE evidence SET length=0')
        elif corruption == 'digest':
            db.execute("UPDATE evidence SET digest=?", ('0' * 64,))
        else:
            db.execute("UPDATE evidence SET phase='response'")
    with pytest.raises(ValueError, match=message):
        content.read_evidence(path, 0)


@pytest.mark.parametrize('field, value, message', [
    ('capture_source', 'unknown', 'capture source'),
    ('transport_attempt_id', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA', 'attempt identity'),
])
def test_attachment_rejects_invalid_attribution(field, value, message):
    from api_agent.testing.inference_content import content_attachment
    row = dict(call_id=str(uuid4()), phase='request', content={'text': 'safe'}, **{field: value})
    with pytest.raises(ValueError, match=message):
        content_attachment(SimpleNamespace(text=json.dumps(row)), EmissionBoundary(ContentPolicy(True)))


def test_capture_rejects_malformed_attempt_and_unbounded_rows():
    from api_agent.workflows.inference_capture import select_capture_rows
    omitted = [dict(phase=phase, content={}, reason='truncated') for phase in ('request', 'response')]
    assert select_capture_rows([dict(phase='request', content={}, transport_attempt_id='bad')]) == omitted
    assert select_capture_rows([{}] * 129) == omitted


@pytest.mark.parametrize('wire', [ {'text': ['x'] * 100001}, {'text': {1: 'bad'}}, {'text': float('nan')} ])
def test_raw_snapshot_rejects_unbounded_or_non_json_content(wire):
    from api_agent.workflows.inference_capture import _snapshot_raw
    with pytest.raises(ValueError):
        _snapshot_raw(wire, 4 * 1024 * 1024)


def test_raw_snapshot_preserves_finite_float_and_rejects_cycle():
    from api_agent.workflows.inference_capture import _snapshot_raw
    assert _snapshot_raw({'value': 1.25}, 100)[0] == {'value': 1.25}
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match='Unsupported'):
        _snapshot_raw({'value': cyclic}, 100)


def test_restricting_shared_authority_withholds_prior_capture():
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference, collect_inference
    boundary = EmissionBoundary(ContentPolicy(True))
    with collect_inference(boundary) as collector:
        capture_inference('input', {'text': 'sensitive'}, str(uuid4()))
        bind_boundary(boundary.for_policy(ContentPolicy(False)))
    assert collector['rows'] == [dict(phase=phase, content={}, reason='withheld') for phase in ('request', 'response')]


@pytest.mark.parametrize('operation, kwargs, message', [
    ('put_wire', {'capture_source': 'invalid'}, 'capture source'),
    ('put_wire', {'transport_attempt_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'}, 'attempt identity'),
    ('omit_wire', {'capture_source': 'invalid'}, 'capture source'),
    ('omit_wire', {'transport_attempt_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'}, 'attempt identity'),
])
def test_payload_wire_rejects_invalid_attribution(tmp_path, operation, kwargs, message):
    from api_agent.testing.payloads import PayloadStore
    from test_payloads import recorder
    store = PayloadStore(recorder(tmp_path), 'test')
    args = (str(uuid4()), 'request', {}) if operation == 'put_wire' else (str(uuid4()), 'request')
    with pytest.raises(ValueError, match=message):
        getattr(store, operation)(*args, **kwargs)


@pytest.mark.parametrize('value, maximum', [([None] * 2049, 4 * 1024 * 1024), ({'text': 'é'}, 12)])
def test_payload_rejects_structure_or_encoded_byte_overflow(value, maximum):
    from api_agent.testing.payloads import _json_value
    with pytest.raises(ValueError):
        _json_value(value, max_bytes=maximum)


@pytest.mark.parametrize('payload', [b'x' * (6 * 1024 * 1024 + 1), b'{}', b'not-json'])
def test_worker_rejects_oversized_or_invalid_input_without_output(monkeypatch, payload):
    import resource
    from api_agent.providers import evidence_worker
    output = io.BytesIO()
    limits = []
    monkeypatch.setattr(evidence_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(evidence_worker.sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(evidence_worker.sys, 'platform', 'linux')
    monkeypatch.setattr(resource, 'setrlimit', lambda kind, limit: limits.append((kind, limit)))
    assert evidence_worker.main() == 1
    assert output.getvalue() == b''
    assert resource.RLIMIT_AS in [kind for kind, _ in limits]


def test_worker_rejects_output_over_limit_without_emitting(monkeypatch):
    import resource
    from api_agent.providers import evidence_worker
    from api_agent.providers.setup_transport import SetupServer
    output = io.BytesIO()
    monkeypatch.setattr(evidence_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"rows":[],"secrets":[],"ids":[]}')))
    monkeypatch.setattr(evidence_worker.sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(evidence_worker.sys, 'platform', 'linux')
    monkeypatch.setattr(resource, 'setrlimit', lambda *_: None)
    monkeypatch.setattr(evidence_worker, '_OUTPUT_LIMIT', 1)
    monkeypatch.setattr(SetupServer, '_prepare_cancellable', lambda *_: [])
    assert evidence_worker.main() == 1
    assert output.getvalue() == b''


@pytest.mark.parametrize('valid', [False, True])
def test_darwin_resident_measurement_checks_kernel_result(monkeypatch, valid):
    import ctypes
    from api_agent.providers import evidence_watchdog as watchdog
    class Query:
        def __call__(self, _pid, _kind, _arg, info, size):
            ctypes.cast(info, ctypes.POINTER(ctypes.c_uint64))[1] = 1234
            return size if valid else 0
    monkeypatch.setattr(watchdog.ctypes, 'CDLL', lambda *_args, **_kwargs: SimpleNamespace(proc_pidinfo=Query()))
    if valid:
        assert watchdog._darwin_resident_bytes(42) == 1234
    else:
        with pytest.raises(OSError, match='measurement unavailable'):
            watchdog._darwin_resident_bytes(42)


def test_child_exited_when_already_reaped(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog
    monkeypatch.setattr(watchdog.os, 'waitid', lambda *_: (_ for _ in ()).throw(ChildProcessError()))
    assert watchdog._child_exited(42)


def test_capture_row_limit_replaces_unpublishable_evidence_with_omissions():
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    boundary = EmissionBoundary(ContentPolicy(True))
    with collect_inference(boundary) as collector:
        for number in range(129):
            capture_inference('input', {'text': str(number)}, str(uuid4()))
    assert collector['rows'] == [dict(phase=phase, content={}, reason='truncated') for phase in ('request', 'response')]


def test_evidence_listener_latch_fails_only_matching_teardown():
    from api_agent.testing import inference_content as content
    registered = {}
    manager = SimpleNamespace(register=lambda plugin, name: registered.update(plugin=plugin, name=name))
    content.pytest_configure(SimpleNamespace(pluginmanager=manager))
    plugin = registered['plugin']
    node = 'synthetic::failed'
    with content._unacknowledged_lock:
        content._unacknowledged.add(node)
    call = SimpleNamespace(nodeid=node, when='call', outcome='passed', longrepr=None)
    other = SimpleNamespace(nodeid='synthetic::other', when='teardown', outcome='passed', longrepr=None)
    failed = SimpleNamespace(nodeid=node, when='teardown', outcome='passed', longrepr=None)
    plugin.pytest_runtest_logreport(call)
    plugin.pytest_runtest_logreport(other)
    plugin.pytest_runtest_logreport(failed)
    assert call.outcome == other.outcome == 'passed'
    assert failed.outcome == 'failed'
    assert failed.longrepr == 'Inference evidence failure notification was not acknowledged.'
    assert node not in content._unacknowledged


def test_chunk_descriptor_rejects_incorrect_assembled_digest_with_valid_part_bindings(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, encoded
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    body = encoded({'text': 'safe content'})
    part_hash = hashlib.sha256(body).hexdigest()
    part = run.record_artifact('part.json', body)
    descriptor = encoded({'version': 1, 'length': len(body), 'sha256': '0' * 64,
                          'chunks': [{'source': part, 'sha256': part_hash, 'length': len(body)}]})
    source = run.record_artifact('descriptor.json', descriptor)
    ref = {'kind': 'chunked', 'source': source, 'sha256': hashlib.sha256(descriptor).hexdigest(),
           'length': len(body)}
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    with pytest.raises(ValueError, match='Chunk assembly mismatch'):
        resolver(ref)
    resolver.close()


def test_payload_index_rejects_unknown_capture_source():
    from api_agent.testing.payloads import validate_payload_index
    occurrence = dict(kind='llm', call_id=str(uuid4()), phase='request', capture_source='invalid')
    index = dict(version=1, test_id='test', entries=[dict(kind='omitted', test_id='test',
                 occurrence=occurrence, reason='truncated')])
    with pytest.raises(ValueError, match='capture source'):
        validate_payload_index(index, {'files': {}})


def test_omitted_wire_retains_valid_attempt_and_rejects_invalid_phase(tmp_path):
    from api_agent.testing.payloads import PayloadStore
    from test_payloads import recorder
    store = PayloadStore(recorder(tmp_path), 'test')
    call, attempt = str(uuid4()), str(uuid4())
    store.omit_wire(call, 'request', transport_attempt_id=attempt, capture_source='provider')
    entry, = store.index()['entries']
    assert entry['occurrence'] == dict(kind='llm', call_id=call, phase='request',
                                        transport_attempt_id=attempt, capture_source='provider')
    with pytest.raises(ValueError, match='wire occurrence'):
        store.omit_wire(call, 'invalid')
    with pytest.raises(ValueError, match='Unknown payload'):
        store.native_parts({'kind': 'payload', 'source': 'missing', 'sha256': '0' * 64, 'length': 0})


@pytest.mark.parametrize('scope', [
    {'invocation_id': 'invalid', 'visit_id': str(uuid4()), 'call_id': str(uuid4()), 'direction': 'input'},
    {'invocation_id': str(uuid4()), 'visit_id': str(uuid4()), 'call_id': str(uuid4()), 'direction': 'invalid'},
    {'invocation_id': str(uuid4()), 'visit_id': str(uuid4()), 'call_id': str(uuid4())},
])
def test_failure_channel_rejects_malformed_scope_then_accepts_valid(monkeypatch, scope):
    import socket
    from api_agent.testing.inference_content import EvidenceFailureChannel, notify_failure
    node = 'synthetic::node'
    with EvidenceFailureChannel([node]) as channel:
        descriptor = json.loads(channel.descriptor)
        message = (descriptor['token'] + ' ' + hashlib.sha256(node.encode()).hexdigest() + ' ' +
                   json.dumps(scope, separators=(',', ':')) + '\n').encode()
        with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=2) as connection:
            connection.settimeout(2)
            connection.sendall(message)
            assert connection.recv(1) == b''
        assert channel.reported() == frozenset()
        valid = dict(invocation_id=str(uuid4()), visit_id=str(uuid4()), call_id=str(uuid4()), direction='input')
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        notify_failure(node, valid)
        assert channel.reported() == frozenset({node})
        assert channel.reported_scopes() == frozenset({(node, valid['invocation_id'],
            valid['visit_id'], valid['call_id'], 'input')})


def test_direct_capture_budget_overflow_omits_both_phases(monkeypatch):
    from api_agent.workflows import inference_capture as capture
    boundary = EmissionBoundary(ContentPolicy(True))
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=boundary), 'visit', None))
    recorded = []
    monkeypatch.setattr(capture, 'capture_rows', lambda rows, _call: recorded.extend(rows))
    with capture.bind_inference_call(str(uuid4())):
        for number in range(129):
            capture.capture_inference('output', {'text': str(number)}, str(uuid4()))
    assert recorded == [dict(phase=phase, content={}, reason='truncated', capture_source='provider')
                        for phase in ('request', 'response')]


def test_two_manifest_bound_payloads_cannot_share_chunk_owner(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, encoded
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    body = encoded({'text': 'safe'})
    digest = hashlib.sha256(body).hexdigest()
    part = run.record_artifact('part.json', body)
    descriptor = encoded(dict(version=1, length=len(body), sha256=digest,
        chunks=[dict(source=part, sha256=digest, length=len(body))]))
    refs = []
    for name in ('first.json', 'second.json'):
        source = run.record_artifact(name, descriptor)
        refs.append(dict(kind='chunked', source=source, sha256=hashlib.sha256(descriptor).hexdigest(), length=len(body)))
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    assert resolver(refs[0]) == {'text': 'safe'}
    with pytest.raises(ValueError, match='multiple payload owners'):
        resolver(refs[1])
    resolver.close()


def test_watchdog_process_group_lookup_race_falls_back_to_child_kill(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog
    from api_agent.providers.contracts import ContractError
    async def scenario():
        entered = asyncio.Event()
        done = asyncio.Event()
        class Process:
            pid = 42
            returncode = None
            kills = 0
            async def communicate(self, _payload):
                entered.set()
                await done.wait()
                self.returncode = -9
                return b'', None
            def kill(self):
                self.kills += 1
                done.set()
        process = Process()
        def missing(*_args):
            raise ProcessLookupError()
        monkeypatch.setattr(watchdog.os, 'killpg', missing)
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .05)
        task = asyncio.create_task(watchdog.communicate(process, b''))
        await entered.wait()
        with pytest.raises(ContractError, match='timed out'):
            await task
        assert process.kills == 1 and process.returncode == -9
    asyncio.run(scenario())


def test_watchdog_handles_simulated_group_and_child_disappearance(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog
    from api_agent.providers.contracts import ContractError
    async def scenario():
        ready, exited = asyncio.Event(), asyncio.Event()
        class Process:
            pid = 43
            returncode = None
            async def communicate(self, _payload):
                ready.set()
                await exited.wait()
                raise BrokenPipeError('child exited')
            def kill(self):
                exited.set()
                raise ProcessLookupError()
        process = Process()
        monkeypatch.setattr(watchdog.os, 'killpg', lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
        monkeypatch.setattr(watchdog, '_WALL_SECONDS', .05)
        task = asyncio.create_task(watchdog.communicate(process, b''))
        await ready.wait()
        with pytest.raises(ContractError, match='timed out'):
            await task
        assert exited.is_set()
    asyncio.run(scenario())


def test_manifest_bound_chunk_descriptor_rejects_duplicate_part(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, encoded
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    part_bytes = b'{}'
    digest = hashlib.sha256(part_bytes).hexdigest()
    part = run.record_artifact('part.json', part_bytes)
    chunk = dict(source=part, sha256=digest, length=len(part_bytes))
    descriptor = encoded(dict(version=1, length=4, sha256=hashlib.sha256(part_bytes * 2).hexdigest(),
                              chunks=[chunk, chunk]))
    name = run.record_artifact('descriptor.json', descriptor)
    ref = dict(kind='chunked', source=name, sha256=hashlib.sha256(descriptor).hexdigest(), length=4)
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    with pytest.raises(ValueError, match='Invalid chunk binding'):
        resolver(ref)
    resolver.close()


def test_rich_response_rejects_non_result_before_policy_validation():
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.providers.rich_inference import validate_response_result
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    with pytest.raises(ContractError, match='Invalid inference result'):
        validate_response_result(request, None)


def test_sidecar_rejects_invalid_phase_before_database_mutation(tmp_path):
    from api_agent.testing import inference_content as content
    path, node, call = str(tmp_path / 'evidence.db'), 'test::case', str(uuid4())
    content.initialize_evidence(path, [node])
    with pytest.raises(ValueError, match='identity'):
        content._publish_sidecar(path, node, {'phase': 'wrong', 'content': {}}, call)
    assert content.read_evidence(path, 0) == []


def test_evidence_pending_slots_fail_closed_when_exhausted(tmp_path, monkeypatch):
    from api_agent.testing import inference_content as content
    path, node = str(tmp_path / 'evidence.db'), 'test::case'
    monkeypatch.setattr(content, 'MAX_TEST_ROWS', 2)
    first = content._start_marker(path, node)
    second = content._start_marker(path, node)
    try:
        with pytest.raises(OSError, match='slots exhausted'):
            content._start_marker(path, node)
        assert content.has_pending(path, node)
    finally:
        __import__('os').unlink(first)
        __import__('os').unlink(second)
    assert not content.has_pending(path, node)


def test_duplicate_failure_marker_is_idempotent(tmp_path):
    from api_agent.testing import inference_content as content
    path, node = str(tmp_path / 'evidence.db'), 'test::case'
    content.initialize_evidence(path, [node])
    content._signal_failure(path, node)
    content._signal_failure(path, node)
    assert content.failure_reported(path, node)


def test_watchdog_measurement_and_exit_check_failure_kills_process(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog
    from api_agent.providers.contracts import ContractError
    async def scenario():
        process = await asyncio.create_subprocess_exec(__import__('sys').executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        def fail(*args):
            raise OSError('synthetic measurement failure')
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', fail)
        monkeypatch.setattr(watchdog, '_child_exited', fail)
        with pytest.raises(ContractError, match='measurement unavailable'):
            await watchdog.communicate(process, b'')
        assert process.returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('index_first', [True, False])
def test_chunk_and_index_cannot_claim_same_manifest_source(tmp_path, index_first):
    from api_agent.testing.payloads import PayloadResolver, PayloadStore, encoded
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    part_ref = store.put({'kind': 'node', 'event_id': 'one'}, {'text': 'safe'})
    body = (tmp_path / part_ref['source']).read_bytes()
    descriptor = encoded(dict(version=1, length=len(body), sha256=hashlib.sha256(body).hexdigest(),
        chunks=[dict(source=part_ref['source'], sha256=part_ref['sha256'], length=len(body))]))
    source = run.record_artifact('descriptor.json', descriptor)
    ref = dict(kind='chunked', source=source, sha256=hashlib.sha256(descriptor).hexdigest(), length=len(body))
    index_name = run.record_artifact('index.json', encoded(store.index()))
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    try:
        if index_first:
            resolver.index(index_name)
            with pytest.raises(ValueError, match='indexed payload source'):
                resolver(ref)
        else:
            assert resolver(ref) == {'text': 'safe'}
            with pytest.raises(ValueError, match='already a chunk'):
                resolver.index(index_name)
    finally:
        resolver.close()


def test_resolver_rejects_conflicting_cached_reference(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, PayloadStore
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    ref = store.put({'kind': 'node', 'event_id': 'one'}, {'text': 'safe'})
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    try:
        assert resolver(ref) == {'text': 'safe'}
        with pytest.raises(ValueError, match='Conflicting cached payload reference'):
            resolver({**ref, 'length': ref['length'] + 1})
    finally:
        resolver.close()


def test_manifest_bound_malformed_chunk_descriptor_is_rejected(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, encoded
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    descriptor = encoded({'version': 1, 'length': 2, 'sha256': '0' * 64, 'chunks': []})
    source = run.record_artifact('descriptor.json', descriptor)
    ref = dict(kind='chunked', source=source, sha256=hashlib.sha256(descriptor).hexdigest(), length=2)
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    try:
        with pytest.raises(ValueError, match='Invalid chunk descriptor'):
            resolver(ref)
    finally:
        resolver.close()


def test_resolver_enforces_cumulative_chunk_retrieval_budget(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, PayloadStore
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    first = PayloadStore(run, 'one').put({'kind': 'node', 'event_id': 'one'}, {'text': 'a' * 2_200_000})
    second = PayloadStore(run, 'two').put({'kind': 'node', 'event_id': 'two'}, {'text': 'b' * 2_200_000})
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    try:
        assert resolver(first)['text'] == 'a' * 2_200_000
        remaining = resolver.remaining
        with pytest.raises(ValueError, match='Chunk retrieval limit'):
            resolver(second)
        assert resolver.remaining == remaining
    finally:
        resolver.close()


def test_active_workflow_boundary_is_available_without_local_collector(monkeypatch):
    from api_agent.workflows import inference_capture as capture
    boundary = EmissionBoundary(ContentPolicy(True))
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=boundary), 'visit', None))
    assert capture.active_inference_boundary() is boundary


def test_signed_incomplete_journal_rejects_invalid_declaration(tmp_path):
    from api_agent.testing.payloads import PayloadStore, hydrate_journal
    from test_payloads import journal, recorder, _resign
    durable = PayloadStore(recorder(tmp_path), 'test').materialize_journal(journal({'value': 'safe'}))
    digest = durable.pop('original_sha256')
    durable.update(version=3, unverified_original_sha256=digest, reduced_sha256=digest, evidence_status='complete')
    _resign(durable)
    with pytest.raises(ValueError, match='Invalid incomplete journal declaration'):
        hydrate_journal(durable, lambda ref: {})


def test_signed_omitted_node_requires_matching_truncated_capture(tmp_path):
    from api_agent.testing.payloads import PayloadStore, hydrate_journal
    from test_payloads import journal, recorder, _resign
    durable = PayloadStore(recorder(tmp_path), 'test').materialize_journal(journal({'value': 'safe'}))
    digest = durable.pop('original_sha256')
    durable.update(version=3, unverified_original_sha256=digest, reduced_sha256=digest, evidence_status='incomplete')
    event = next(row for row in durable['events'] if row['event'] == 'capture')
    event['payload'] = {'kind': 'omitted', 'reason': 'truncated', 'node': True}
    _resign(durable)
    with pytest.raises(ValueError, match='Invalid omitted node payload'):
        hydrate_journal(durable, lambda ref: {})


def test_signed_canonical_omission_requires_indexed_resolver(tmp_path):
    from api_agent.testing.payloads import PayloadStore, hydrate_journal
    from test_payloads import journal, recorder, _resign
    durable = PayloadStore(recorder(tmp_path), 'test').materialize_journal(journal({'value': 'safe'}))
    event = next(row for row in durable['events'] if row['event'] == 'capture')
    event['reason'] = 'truncated'
    event['payload'] = {'kind': 'omitted', 'reason': 'truncated', 'canonical': event['payload']}
    _resign(durable)
    with pytest.raises(ValueError, match='Canonical omission requires an indexed resolver'):
        hydrate_journal(durable, lambda ref: {})


def test_failure_notification_rejects_wrong_node_and_malformed_suffix(monkeypatch):
    import socket
    from api_agent.testing.inference_content import EvidenceFailureChannel, notify_failure
    node = 'synthetic::known'
    with EvidenceFailureChannel([node]) as channel:
        descriptor = json.loads(channel.descriptor)
        digest = hashlib.sha256(node.encode()).hexdigest()
        for payload in (descriptor['token'] + ' ' + '0' * 64 + '\n',
                        descriptor['token'] + ' ' + digest + '!invalid\n'):
            with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=2) as connection:
                connection.settimeout(2)
                connection.sendall(payload.encode())
                assert connection.recv(1) == b''
        assert not channel.reported() and not channel.reported_scopes()
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        notify_failure(node)
        assert channel.reported() == frozenset({node})


def test_invalid_failure_channel_descriptor_is_unacknowledged(monkeypatch):
    from api_agent.testing import inference_content as content
    node = 'synthetic::invalid-channel'
    monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', json.dumps({'port': 0, 'token': 'a' * 64}))
    try:
        with pytest.raises(content.EvidencePublicationError, match='not acknowledged'):
            content.notify_failure(node)
        assert node in content._unacknowledged
    finally:
        with content._unacknowledged_lock:
            content._unacknowledged.discard(node)


def test_capture_loss_with_invalid_workflow_identity_reports_unscoped(monkeypatch):
    from api_agent.testing import inference_content
    from api_agent.workflows import inference_capture as capture
    calls = []
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'synthetic::node (call)')
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (
        SimpleNamespace(invocation_id='invalid'), str(uuid4()), None))
    monkeypatch.setattr(inference_content, 'notify_failure', lambda *args: calls.append(args))
    capture._report_capture_loss(str(uuid4()), 'input')
    assert calls == [('synthetic::node', None)]


def test_raw_capture_duplicate_does_not_consume_budget_twice():
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    boundary = EmissionBoundary(ContentPolicy(True))
    row = {'text': 'safe content'}
    with collect_inference(boundary, retain_raw=True) as collector:
        capture_inference('input', row, 'cause')
        remaining = collector['remaining']
        capture_inference('input', row, 'cause')
        assert collector['remaining'] == remaining
        assert collector['rows'] == [dict(phase='request', content=row, reason='captured')]


def test_raw_capture_non_object_is_reported_as_truncated():
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as collector:
        capture_inference('input', ['unsupported root'], 'cause')
    assert collector['rows'] == [dict(phase='request', content={}, reason='truncated')]


def test_payload_indexes_enforce_aggregate_limit_across_independent_indexes(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, PayloadStore
    from test_payloads import recorder, _finalized_with_indexes
    run = recorder(tmp_path)
    stores = [PayloadStore(run, 'same-test'), PayloadStore(run, 'same-test')]
    for i, store in enumerate(stores):
        store.put({'kind': 'node', 'event_id': str(i)}, {'text': chr(97 + i) * 2_200_000})
    manifest = _finalized_with_indexes(run, {'first-index.json': stores[0].index(),
                                             'second-index.json': stores[1].index()})
    resolver = PayloadResolver.local(tmp_path, manifest)
    try:
        resolver.index('first-index.json')
        with pytest.raises(ValueError, match='aggregate limit'):
            resolver.index('second-index.json')
    finally:
        resolver.close()


def test_darwin_watchdog_accepts_bounded_resident_measurement(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog
    async def scenario():
        process = await asyncio.create_subprocess_exec(__import__('sys').executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        measured = asyncio.Event()
        def observe(pid):
            assert pid == process.pid
            measured.set()
            return 1024
        monkeypatch.setattr(watchdog.sys, 'platform', 'darwin')
        monkeypatch.setattr(watchdog, '_POLL_SECONDS', .01)
        monkeypatch.setattr(watchdog, '_darwin_resident_bytes', observe)
        task = asyncio.create_task(watchdog.communicate(process, b''))
        try:
            await asyncio.wait_for(measured.wait(), 3)
            assert not task.done()
            process.kill()
            assert await task == (b'', None)
            assert process.returncode is not None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    asyncio.run(scenario())


def test_evidence_storage_and_marker_failures_still_notify_runner(tmp_path, monkeypatch):
    from api_agent.testing import inference_content as content
    path, node = str(tmp_path / 'evidence.db'), 'synthetic::node'
    content.initialize_evidence(path, [node])
    real_open, real_unlink = content.os.open, content.os.unlink
    marker, status = content.failure_marker(path, node), content.status_path(path, node)
    def failed_open(name, *args, **kwargs):
        if name in (marker, status):
            raise OSError('synthetic storage failure')
        return real_open(name, *args, **kwargs)
    def failed_unlink(name, *args, **kwargs):
        if name == status:
            raise OSError('synthetic storage failure')
        return real_unlink(name, *args, **kwargs)
    def failed_write(*args):
        raise sqlite3.OperationalError('synthetic storage failure')
    with content.EvidenceFailureChannel([node]) as channel:
        monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
        monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', path)
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        monkeypatch.setenv('PYTEST_CURRENT_TEST', node + ' (call)')
        monkeypatch.setattr(content.os, 'open', failed_open)
        monkeypatch.setattr(content.os, 'unlink', failed_unlink)
        monkeypatch.setattr(content, '_publish_sidecar', failed_write)
        content.publish_content([dict(phase='request', content={'text': 'safe'})], str(uuid4()))
        assert content.has_pending(path, node)
        assert channel.reported() == frozenset({node})


def test_short_status_initialization_write_fails_before_database_schema_initialization(tmp_path, monkeypatch):
    from api_agent.testing import inference_content as content
    path, node = str(tmp_path / 'evidence.db'), 'synthetic::node'
    monkeypatch.setattr(content.os, 'write', lambda fd, data: 0)
    with pytest.raises(OSError, match='initialize evidence status'):
        content.initialize_evidence(path, [node])
    assert content.failure_reported(path, node)


@pytest.mark.parametrize('payload', [b'partial-without-newline', b'short\n'])
def test_failure_channel_rejects_incomplete_messages_and_remains_available(monkeypatch, payload):
    import socket
    from api_agent.testing.inference_content import EvidenceFailureChannel, notify_failure
    node = 'synthetic::node'
    with EvidenceFailureChannel([node]) as channel:
        descriptor = json.loads(channel.descriptor)
        with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=2) as connection:
            connection.settimeout(2)
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(1) == b''
        assert not channel.reported()
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        notify_failure(node)
        assert channel.reported() == frozenset({node})


def test_resolver_rejects_wrong_uncached_payload_length_even_with_valid_digest(tmp_path):
    from api_agent.testing.payloads import PayloadResolver, PayloadStore
    from test_payloads import recorder, result
    run = recorder(tmp_path)
    ref = PayloadStore(run, 'test').put({'kind': 'node', 'event_id': 'one'}, {'text': 'safe'})
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    try:
        with pytest.raises(ValueError, match='Payload digest or retrieval limit'):
            resolver({**ref, 'length': ref['length'] + 1})
    finally:
        resolver.close()


def test_collected_capture_budget_preserves_first_and_truncates_next():
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('input', {'text': 'a' * 2_200_000}, 'first')
        capture_inference('output', {'text': 'b' * 2_200_000}, 'second')
    assert collector['rows'][0]['content']['text'] == 'a' * 2_200_000
    assert collector['rows'][1] == dict(phase='response', content={}, reason='truncated')


def test_direct_capture_drops_subsequent_rows_after_overflow(monkeypatch):
    from api_agent.workflows import inference_capture as capture
    boundary = EmissionBoundary(ContentPolicy(True))
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=boundary), 'visit', None))
    recorded = []
    monkeypatch.setattr(capture, 'capture_rows', lambda rows, _call: recorded.extend(rows))
    with capture.bind_inference_call(str(uuid4())):
        for number in range(130):
            capture.capture_inference('output', {'text': str(number)}, str(uuid4()))
    assert recorded == [dict(phase=phase, content={}, reason='truncated', capture_source='provider')
                        for phase in ('request', 'response')]


def test_repeated_shared_authority_restriction_stays_withheld():
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference, collect_inference
    original = EmissionBoundary(ContentPolicy(True))
    restricted = original.for_policy(ContentPolicy(False))
    with collect_inference(original) as collector:
        capture_inference('input', {'text': 'sensitive'}, 'cause')
        bind_boundary(restricted)
        bind_boundary(restricted)
        capture_inference('output', {'text': 'also sensitive'}, 'cause')
    assert all(row['content'] == {} and row['reason'] == 'withheld' for row in collector['rows'])


def test_watchdog_reaps_child_if_deadline_expires_before_first_poll(monkeypatch):
    from api_agent.providers import evidence_watchdog as watchdog, progress
    from api_agent.providers.contracts import ContractError
    async def scenario():
        process = await asyncio.create_subprocess_exec(__import__('sys').executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        times = iter([0.0, 11.0])
        monkeypatch.setattr(progress, '_clock', lambda: next(times, 11.0))
        try:
            with pytest.raises(ContractError, match='timed out'):
                await watchdog.communicate(process, b'')
            assert process.returncode is not None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    asyncio.run(scenario())


def test_failure_channel_expired_partial_read_rejects_without_reporting(monkeypatch):
    import socket
    import threading
    import time
    from api_agent.testing.inference_content import EvidenceFailureChannel, notify_failure
    node = 'synthetic::node'
    with EvidenceFailureChannel([node]) as channel:
        descriptor = json.loads(channel.descriptor)
        original = time.monotonic
        calls = []
        def advancing():
            if threading.current_thread() is channel._thread:
                calls.append(True)
                return 100.0 if len(calls) == 1 else 101.0
            return original()
        with monkeypatch.context() as patch:
            patch.setattr(time, 'monotonic', advancing)
            with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=2) as connection:
                connection.settimeout(2)
                connection.sendall(b'partial')
                try:
                    received = connection.recv(1)
                except ConnectionResetError:
                    received = b''
                assert received == b''
        assert len(calls) >= 2 and not channel.reported()
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        notify_failure(node)
        assert channel.reported() == frozenset({node})


def test_watchdog_kills_and_reaps_real_child_after_communication_failure():
    from api_agent.providers import evidence_watchdog as watchdog
    async def scenario():
        child = await asyncio.create_subprocess_exec(__import__('sys').executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        class FailedCommunication:
            pid = child.pid
            @property
            def returncode(self):
                return child.returncode
            async def communicate(self, payload):
                raise BrokenPipeError('synthetic pipe failure')
            def kill(self):
                child.kill()
            async def wait(self):
                return await child.wait()
        try:
            with pytest.raises(BrokenPipeError, match='synthetic pipe failure'):
                await watchdog.communicate(FailedCommunication(), b'')
            assert child.returncode is not None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    asyncio.run(scenario())


def test_evidence_worker_module_entry_projects_and_redacts(monkeypatch):
    import resource
    import runpy
    import sys
    from api_agent.providers import evidence_worker
    payload = {'rows': [{'phase': 'request', 'content': {'text': 'visible synthetic-secret'}, 'reason': 'captured'}],
               'secrets': ['synthetic-secret'], 'ids': []}
    input_stream = io.BytesIO(json.dumps(payload).encode())
    output_stream = io.BytesIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', SimpleNamespace(buffer=input_stream))
        patch.setattr(sys, 'stdout', SimpleNamespace(buffer=output_stream))
        # Never lower the test runner's own limits while exercising the entry point.
        patch.setattr(resource, 'setrlimit', lambda *_args: None)
        with pytest.raises(SystemExit) as exit_status:
            runpy.run_path(evidence_worker.__file__, run_name='__main__')
    assert exit_status.value.code == 0
    frames = json.loads(output_stream.getvalue())
    assert len(frames) == 1
    assert json.loads(frames[0][2])['content'] == {'text': 'visible [REDACTED]'}
    assert b'synthetic-secret' not in output_stream.getvalue()


def test_reentrant_projection_discards_intervening_fallback_capture(monkeypatch):
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    boundary = EmissionBoundary(ContentPolicy(True))
    export = boundary.content_export
    entered = False
    def reentrant(value):
        nonlocal entered
        if not entered:
            entered = True
            capture_inference('input', {'text': 'fallback'}, 'cause', fallback=True)
        return export(value)
    monkeypatch.setattr(boundary, 'content_export', reentrant)
    with collect_inference(boundary) as collector:
        capture_inference('input', {'text': 'provider'}, 'cause')
    assert collector['rows'] == [dict(phase='request', content={'text': 'provider'}, reason='captured')]
    assert collector['remaining'] == 4 * 1024 * 1024 - len(b'{"text":"provider"}')


def test_duplicate_authority_join_is_idempotent_during_active_lease():
    from api_agent.testing.authority_scope import TestAuthorityScope
    scope = TestAuthorityScope(('ui',))
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
    try:
        scope.join('ui', boundary)
        generation, owners = scope.snapshot()
        lease = scope.freeze()
        try:
            scope.join('ui', boundary)
            assert scope.snapshot() == (generation, owners)
            assert 'synthetic-secret' in lease.secrets
        finally:
            lease.release()
    finally:
        scope.close()


@pytest.mark.parametrize('wakeup', ['release', 'close'])
def test_cancelled_authority_waiter_is_not_woken_after_cancellation(wakeup):
    from api_agent.testing.authority_scope import TestAuthorityScope
    async def scenario():
        scope = TestAuthorityScope(('ui', 'tools'))
        scope.join('ui', EmissionBoundary(ContentPolicy(True)))
        scope.join('tools', EmissionBoundary(ContentPolicy(True)))
        lease = scope.freeze()
        errors = []
        loop = asyncio.get_running_loop()
        prior = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        waiter = asyncio.create_task(scope.ajoin('tools', EmissionBoundary(ContentPolicy(True))))
        try:
            async with asyncio.timeout(3):
                while not scope._waiters:
                    await asyncio.sleep(0)
            if wakeup == 'release':
                lease.release()
            else:
                scope.close()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await asyncio.sleep(0)
            assert not errors
        finally:
            lease.release()
            scope.close()
            loop.set_exception_handler(prior)
    asyncio.run(scenario())


def test_join_current_outside_scope_does_not_establish_global_authority():
    from api_agent.testing.authority_scope import current_scope, join_current
    assert current_scope() is None
    join_current('ui', EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',)))
    assert current_scope() is None


def test_failure_channel_survives_failed_ack_then_acknowledges_next(monkeypatch):
    import socket
    import threading
    from api_agent.testing.inference_content import EvidenceFailureChannel, notify_failure
    node = 'synthetic::node'
    with EvidenceFailureChannel([node]) as channel:
        descriptor = json.loads(channel.descriptor)
        message = (descriptor['token'] + ' ' + hashlib.sha256(node.encode()).hexdigest() + '\n').encode()
        failed = threading.Event()
        sendall = socket.socket.sendall
        def fail_first_server_ack(connection, data, *args, **kwargs):
            if threading.current_thread() is channel._thread and data == b'1' and not failed.is_set():
                failed.set()
                raise ConnectionResetError('synthetic acknowledgement failure')
            return sendall(connection, data, *args, **kwargs)
        monkeypatch.setattr(socket.socket, 'sendall', fail_first_server_ack)
        with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=2) as connection:
            connection.settimeout(2)
            connection.sendall(message)
            assert connection.recv(1) == b''
        assert failed.is_set()
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        notify_failure(node)
        assert channel.reported() == frozenset({node})


def test_carrier_work_budget_allows_exact_limit_and_rejects_next_character():
    from api_agent.providers.credential_guard import _charge_carrier_work
    # The same accounting combines independently inspected carrier lengths.
    work = 0
    for length in (4 * 1024 * 1024, 7 * 1024 * 1024, 5 * 1024 * 1024):
        work = _charge_carrier_work(work, length)
    assert work == 16 * 1024 * 1024
    with pytest.raises(ValueError):
        _charge_carrier_work(work, 1)


def test_guard_nested_carrier_rejects_credential_and_preserves_safe_control():
    from api_agent.providers.credential_guard import CredentialProtectionError, guard_json
    secret = '{"account":"synthetic-secret"}'
    unsafe = json.dumps({'nested': secret})
    safe = json.dumps({'nested': '{"account":"ordinary"}'})
    assert guard_json({'text': safe}, {secret}) == {'text': safe}
    with pytest.raises(CredentialProtectionError, match='safely released'):
        guard_json({'text': unsafe}, {secret})


@pytest.mark.parametrize('disappeared', [False, True])
def test_watchdog_reaps_real_child_when_kill_races_with_disappearance(disappeared):
    """Inject the disappearance race while retaining actual child ownership."""
    from api_agent.providers import evidence_watchdog as watchdog
    async def scenario():
        child = await asyncio.create_subprocess_exec(__import__('sys').executable, '-c', 'import time; time.sleep(30)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        calls = []
        class CommunicationFailure:
            pid = child.pid
            @property
            def returncode(self):
                return child.returncode
            async def communicate(self, payload):
                raise BrokenPipeError('synthetic communication failure')
            def kill(self):
                calls.append('kill')
                child.kill()
                if disappeared:
                    # Model an OS lookup race after the preceding returncode
                    # check; the real child still must be waited and reaped.
                    raise ProcessLookupError('synthetic disappearance race')
            async def wait(self):
                calls.append('wait')
                return await child.wait()
        try:
            with pytest.raises(BrokenPipeError, match='synthetic communication failure'):
                await watchdog.communicate(CommunicationFailure(), b'')
            assert calls == ['kill', 'wait']
            assert child.returncode is not None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    asyncio.run(scenario())


@pytest.mark.parametrize('group_lookup', ['unavailable', 'missing'])
def test_watchdog_waits_for_descendant_pipe_after_exited_leader(monkeypatch, group_lookup):
    """A disappeared leader does not justify discarding a still-open pipe."""
    import os
    import signal
    import sys
    from api_agent.providers import evidence_watchdog as watchdog
    async def scenario():
        code = ('import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])')
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        group_kill = os.killpg
        branch_entered = asyncio.Event()
        kill_calls = []
        class ChildOwner:
            pid = child.pid
            @property
            def returncode(self):
                result = child.returncode
                if group_lookup == 'unavailable' and result is not None:
                    branch_entered.set()
                return result
            async def communicate(self, payload):
                return await child.communicate(payload)
            def kill(self):
                kill_calls.append('individual')
                child.kill()
            async def wait(self):
                return await child.wait()
        def missing_group(pid, sig):
            assert pid == child.pid and sig == signal.SIGKILL
            branch_entered.set()
            raise ProcessLookupError('synthetic vanished process group')
        simulated_os = (SimpleNamespace(name='nt') if group_lookup == 'unavailable' else
                        SimpleNamespace(name='posix', killpg=missing_group))
        monkeypatch.setattr(watchdog, 'os', simulated_os)
        task = asyncio.create_task(watchdog.communicate(ChildOwner(), b''))
        try:
            async with asyncio.timeout(5):
                while child.returncode is None:
                    await asyncio.sleep(.01)
            assert child.returncode == 0 and not task.done()
            task.cancel()
            await asyncio.wait_for(branch_entered.wait(), 3)
            assert not task.done() and kill_calls == []
            # The test still owns the real group, including its live descendant.
            group_kill(child.pid, signal.SIGKILL)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert child.returncode == 0
        finally:
            try:
                group_kill(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if child.returncode is None:
                child.kill()
                await child.wait()
    asyncio.run(scenario())
