from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json

import pytest

from api_agent.testing.payloads import PayloadStore, PayloadResolver, hydrate_journal, encoded
from api_agent.testing.recording import RunRecorder, RunIdentity
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.telemetry.privacy import ContentPolicy
from api_agent.workflows.journal import WorkflowJournal


def recorder(tmp_path):
    return RunRecorder(tmp_path, RunIdentity(str(uuid4()), 'example', 'integration', datetime.now(timezone.utc)))


def journal(values=None, cause=None, test_id=None, call_owner=None, capture_reason=None):
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[node], tables={'Edges': []}), resources=[], replacements=[])
    owner = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True), test_id=test_id)
    owner.observe(dict(event='scope_start',scope_id='scope',kind='graph',owner='GRAPH',started_ms=1))
    owner.observe(dict(event='edge',source='START',target='Make',owner='GRAPH',implicit=True))
    owner.observe(dict(event='scope_start',scope_id='visit',parent_id='scope',kind='node',owner='Make',started_ms=1))
    if values is not None:
        owner.capture_json(visit_id='visit',direction='input', values=values, schema={'type':'object'}, cause_id=cause, capture_reason=capture_reason)
    if call_owner is not None:
        owner.observe(dict(event='scope_start',scope_id=call_owner,parent_id='scope',kind='node',owner='Make',started_ms=2))
        owner.record_call(visit_id=call_owner,call_id=cause)
    return owner.finalize()


def result():
    return TestResult(TestIdentity('example::test', 'example', 'test.py', 'test.py::test'), 'passed')


def test_occurrences_share_only_by_identity_and_conflicts_reject(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    call = str(uuid4())
    wire = {'messages':[{'content':'x' * 900}], 'data':{'nested':[None,True,3]}}
    first = store.put_wire(call,'request',wire)
    assert store.put_wire(call,'request',wire) == first
    second = store.put_wire(str(uuid4()),'request',wire)
    assert first['source'] != second['source']
    assert json.loads((tmp_path / first['source']).read_bytes()) == wire
    with pytest.raises(ValueError,match='Conflicting'):
        store.put_wire(call,'request',{})
    assert len(store.index()['entries']) == 2


def test_chunked_payload_roundtrip_and_detects_corruption(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    value = {'messages': [{'text': '🧪' * 130000}]}
    ref = store.put_wire(str(uuid4()), 'request', value)
    assert ref['kind'] == 'chunked'
    parts = store.native_parts(ref)
    assert len(parts) == 3
    assert all((tmp_path / name).stat().st_size <= 200 * 1024 for name in parts)
    run.record_test(result())
    manifest = run.finalize()
    with PayloadResolver.local(tmp_path, manifest) as resolver:
        assert resolver(ref) == value
    (tmp_path / parts[1]).write_bytes(b'corrupted')
    with PayloadResolver.local(tmp_path, manifest) as resolver:
        with pytest.raises(ValueError, match='Chunk'):
            resolver(ref)


def test_failed_chunk_validation_does_not_consume_resolver_budget(tmp_path):
    run = recorder(tmp_path)
    invalid = b'{broken'
    part = run.record_artifact('invalid.part', invalid)
    descriptor = encoded({'version': 1, 'length': len(invalid),
        'sha256': hashlib.sha256(invalid).hexdigest(),
        'chunks': [{'source': part, 'sha256': hashlib.sha256(invalid).hexdigest(), 'length': len(invalid)}]})
    name = run.record_artifact('invalid.json', descriptor)
    bad = {'kind': 'chunked', 'source': name, 'sha256': hashlib.sha256(descriptor).hexdigest(), 'length': len(invalid)}
    store = PayloadStore(run, 'test')
    value = {'text': 'x' * 70000}
    good = store.put_wire(str(uuid4()), 'request', value)
    run.record_test(result())
    with PayloadResolver.local(tmp_path, run.finalize()) as resolver:
        before = resolver.remaining
        with pytest.raises(ValueError):
            resolver(bad)
        assert resolver.remaining == before
        assert resolver(good) == value


def test_failed_chunk_write_removes_prior_fragments(tmp_path, monkeypatch):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    write = run.record_artifact
    calls = []
    def fail_second(name, content):
        calls.append(name)
        if len(calls) == 2:
            raise OSError('synthetic write failure')
        return write(name, content)
    monkeypatch.setattr(run, 'record_artifact', fail_second)
    with pytest.raises(OSError):
        store.put_wire(str(uuid4()), 'request', {'text': 'x' * 300000})
    assert not list(tmp_path.iterdir())
    assert store.index()['entries'] == []


def test_existing_attachment_reuses_one_native_source(tmp_path):
    run = recorder(tmp_path)
    ref = PayloadStore(run,'test').put({'kind':'node'}, {'value':None})
    attachment = run.existing_attachment('Input',ref['source'])
    filename = run.record_test(result(), attachments=(attachment, attachment))
    row = json.loads((tmp_path / filename).read_bytes())
    assert [item['source'] for item in row['attachments']] == [ref['source']] * 2
    manifest = run.finalize()
    assert set(manifest['files']) == {ref['source'], filename}
    assert PayloadResolver.local(tmp_path,manifest)(ref) == {'value':None}


@pytest.mark.parametrize('source',['../outside','/tmp/outside','missing.json','manifest.json'])
def test_existing_attachment_requires_owned_flat_file(tmp_path,source):
    with pytest.raises(ValueError):
        recorder(tmp_path).existing_attachment('Input',source)


def test_journal_materialization_retains_exact_digest_and_null(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    packet = journal({'nested':{'text':'x' * 900,'empty':None}})
    durable = store.materialize_journal(packet)
    captures = [item for item in durable['events'] if item['event'] == 'capture']
    assert len(captures) == 1 and 'values' not in captures[0]
    assert durable['original_sha256'] == packet['sha256']
    assert hydrate_journal(durable,lambda ref: store._values[ref['source']]) == packet
    assert hydrate_journal(packet,None) == packet
    durable['events'][0]['owner'] = 'Other'
    with pytest.raises(ValueError,match='digest'):
        hydrate_journal(durable,None)


def test_failed_hydration_restores_resolver_state(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    packet = journal({'text': 'visible'})
    durable = store.materialize_journal(packet)
    manifest = _finalized_with_indexes(run, {'index.json': store.index()})
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('index.json')
    broken = dict(durable, original_sha256='0' * 64)
    broken['sha256'] = hashlib.sha256(encoded({key: value for key, value in broken.items() if key != 'sha256'})).hexdigest()
    before = resolver.remaining
    with pytest.raises(ValueError):
        hydrate_journal(broken, resolver)
    assert resolver.remaining == before
    assert hydrate_journal(durable, resolver) == packet


def test_wire_capture_reuses_authoritative_source(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    wire = {'messages':[{'content':'hello'}]}
    ref = store.put_wire(call,'request',wire)
    packet = journal({'llm_request':wire},call)
    durable = store.materialize_journal(packet)
    capture = next(item for item in durable['events'] if item['event'] == 'capture')
    assert capture['payload']['source'] == ref['source']
    assert len(store.index()['entries']) == 1
    assert hydrate_journal(durable,lambda ref: wire) == packet


def test_missing_wire_and_withheld_do_not_create_payload(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    assert store.put_wire(call,'request',{},reason='withheld') is None
    assert store.materialize_journal(journal())['version'] == 2
    with pytest.raises(ValueError,match='authoritative'):
        store.materialize_journal(journal({'llm_request':{}},call))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('value',[{'x':'a' * (4 * 1024 * 1024)}, {'x':[0] * 2048}, {'x':float('inf')}, {'x':object()}])
def test_payload_bounds_reject_before_writing(tmp_path,value):
    store = PayloadStore(recorder(tmp_path),'test')
    with pytest.raises(ValueError):
        store.put({'kind':'node'},value)
    assert list(tmp_path.iterdir()) == []


def test_resolver_rejects_tamper_and_symlink(tmp_path):
    run = recorder(tmp_path)
    ref = PayloadStore(run,'test').put({'kind':'node'},{'value':'original'})
    run.record_test(result())
    manifest = run.finalize()
    path = tmp_path / ref['source']
    path.write_bytes(b'{}')
    with pytest.raises(ValueError,match='digest'):
        PayloadResolver.local(tmp_path,manifest)(ref)
    path.unlink()
    path.symlink_to(tmp_path / 'manifest.json')
    with pytest.raises(OSError):
        PayloadResolver.local(tmp_path,manifest)(ref)
    broken = dict(ref,source='../outside')
    with pytest.raises(ValueError,match='source'):
        PayloadResolver.local(tmp_path,manifest)(broken)


def test_index_reader_binds_occurrence_before_hydration(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    packet = journal({'nested': {'empty': None}}, test_id='test')
    durable = store.materialize_journal(packet)
    run.record_artifact('payload-index.json', encoded(store.index()))
    run.record_test(result())
    manifest = run.finalize()
    resolver = PayloadResolver.local(tmp_path, manifest)
    with pytest.raises(ValueError, match='test owner'):
        hydrate_journal(durable, resolver)
    assert resolver.index('payload-index.json') == store.index()
    assert hydrate_journal(durable, resolver) == packet
    capture = next(row for row in durable['events'] if row['event'] == 'capture')
    capture['visit_id'] = 'wrong'
    unsigned = {key:value for key,value in durable.items() if key != 'sha256'}
    durable['sha256'] = hashlib.sha256(encoded(unsigned)).hexdigest()
    with pytest.raises(ValueError, match='owner'):
        hydrate_journal(durable, resolver)


def test_semantic_conflict_rejects_even_when_bytes_match(tmp_path):
    store = PayloadStore(recorder(tmp_path), 'test')
    store.put({'kind':'node'}, None, owner='one', reason='captured')
    with pytest.raises(ValueError, match='Conflicting'):
        store.put({'kind':'node'}, None, owner='two', reason='captured')
    with pytest.raises(ValueError, match='Conflicting'):
        store.put({'kind':'node'}, None, owner='one', reason='partial')


def test_omitted_wire_reason_is_indexed_without_file(tmp_path):
    store = PayloadStore(recorder(tmp_path), 'test')
    call = str(uuid4())
    assert store.put_wire(call, 'response', {}, reason='unavailable') is None
    entry = store.index()['entries'][0]
    assert entry['reason'] == 'unavailable' and 'source' not in entry
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match='Conflicting'):
        store.put_wire(call, 'response', {}, reason='captured')


def test_payload_index_digest_tamper_rejects(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    store.put({'kind':'node'}, {'value': 'safe'})
    run.record_artifact('payload-index.json', encoded(store.index()))
    run.record_test(result())
    manifest = run.finalize()
    (tmp_path / 'payload-index.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='digest'):
        PayloadResolver.local(tmp_path, manifest).index('payload-index.json')


def _finalized_with_indexes(run, indexes):
    for name, index in indexes.items():
        run.record_artifact(name, encoded(index))
    run.record_test(result())
    return run.finalize()


def test_cross_index_occurrence_conflicts_are_transactional(tmp_path):
    from copy import deepcopy
    run = recorder(tmp_path)
    first, second = PayloadStore(run, 'test'), PayloadStore(run, 'test')
    first.put({'kind': 'node', 'event_id': 'shared'}, {'value': 'one'})
    packet = journal({'value': 'two'}, test_id='test')
    durable = second.materialize_journal(packet)
    rejected = second.index()
    conflict = deepcopy(first.index()['entries'][0])
    conflict['reason'] = 'partial'
    rejected['entries'].append(conflict)
    manifest = _finalized_with_indexes(run, {'first.json':first.index(), 'rejected.json':rejected})
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('first.json')
    with pytest.raises(ValueError, match='Conflicting'):
        resolver.index('rejected.json')
    with pytest.raises(ValueError, match='test owner'):
        hydrate_journal(durable, resolver)


def test_same_occurrence_in_separate_indexes_cannot_change_source(tmp_path):
    run = recorder(tmp_path)
    stores = [PayloadStore(run, 'test'), PayloadStore(run, 'test')]
    for number, store in enumerate(stores):
        store.put({'kind':'node','event_id':'same'}, {'value':number})
    manifest = _finalized_with_indexes(run, {'one.json':stores[0].index(), 'two.json':stores[1].index()})
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('one.json')
    with pytest.raises(ValueError,match='occurrence'):
        resolver.index('two.json')


def test_journal_owner_alias_is_explicit_and_preserves_original(tmp_path):
    packet = journal({'value':'original'}, test_id='test.py::test')
    store = PayloadStore(recorder(tmp_path), 'example::test.py::test')
    with pytest.raises(ValueError, match='test owner'):
        store.materialize_journal(packet)
    assert list(tmp_path.iterdir()) == []
    store = PayloadStore(store.recorder, store.test_id, journal_test_id='test.py::test')
    durable = store.materialize_journal(packet)
    assert hydrate_journal(durable, lambda ref:store._values[ref['source']]) == packet
    assert durable['recording_test_id'] == 'example::test.py::test'


def test_reference_must_agree_with_index_length(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    ref = store.put({'kind':'node'}, {'value':'nonempty'})
    index = store.index()
    index['entries'][0]['length'] = 0
    manifest = _finalized_with_indexes(run, {'index.json':index})
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('index.json')
    with pytest.raises(ValueError, match='index'):
        resolver(ref)


def test_full_payload_budget_can_reopen_after_index_admission(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    value = {'x':'a' * 65528}
    refs = [store.put({'kind':'node','event_id':str(number)}, value) for number in range(64)]
    with pytest.raises(ValueError, match='aggregate'):
        store.put({'kind':'node','event_id':'overflow'}, None)
    manifest = _finalized_with_indexes(run, {'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('index.json')
    assert [resolver(ref) for ref in refs] == [value] * 64
    assert resolver.remaining == 0
    assert resolver(refs[0]) == value
    (tmp_path / refs[0]['source']).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='digest'):
        resolver(refs[0])


@pytest.mark.parametrize('index', [None, {}, {'version':True,'test_id':'test','entries':[]},
    {'version':1,'test_id':'','entries':[]}, {'version':1,'test_id':'test','entries':[None]},
    {'version':1,'test_id':'test','entries':[{'occurrence':{}}]}])
def test_malformed_index_is_rejected(index):
    from api_agent.testing.payloads import validate_payload_index
    with pytest.raises(ValueError):
        validate_payload_index(index, {'files':{}})


def test_duplicate_occurrence_and_source_reject_within_index(tmp_path):
    from copy import deepcopy
    from api_agent.testing.payloads import validate_payload_index
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    ref = store.put({'kind':'node','event_id':'first'}, None)
    index = store.index()
    index['entries'].append(deepcopy(index['entries'][0]))
    manifest = {'files':{ref['source']:ref['sha256']}}
    with pytest.raises(ValueError, match='Duplicate'):
        validate_payload_index(index,manifest)
    index['entries'][1]['occurrence']['event_id'] = 'second'
    with pytest.raises(ValueError, match='multiple'):
        validate_payload_index(index,manifest)


@pytest.mark.parametrize('ref', [None, {}, {'kind':'payload','length':False},
    {'kind':'payload','length':65537}, {'kind':'payload','length':0,'source':'../bad'},
    {'kind':'payload','length':0,'source':'unbound','sha256':'a'*64}])
def test_malformed_and_unbound_reference_rejected(tmp_path,ref):
    run = recorder(tmp_path)
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path, run.finalize())
    with pytest.raises(ValueError):
        resolver(ref)


def test_parent_symlink_read_is_rejected(tmp_path):
    real = tmp_path / 'real'
    run = recorder(real)
    run.record_test(result())
    manifest = run.finalize()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlinked'):
        PayloadResolver.local(alias,manifest)


def test_unavailable_empty_journal_hydrates_without_payload_file(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    packet = journal({}, capture_reason='unavailable')
    durable = store.materialize_journal(packet)
    assert hydrate_journal(durable, None) == packet
    assert store.index()['entries'][0]['kind'] == 'omitted'
    assert list(tmp_path.iterdir()) == []


def test_legacy_wire_subset_is_reconstituted_without_copy(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    call = str(uuid4())
    wire = {'messages':[], 'tools':[]}
    store.put_wire(call,'request',wire)
    packet = journal({'llm_request':{'messages':[]}},call)
    durable = store.materialize_journal(packet)
    ref = next(row['payload'] for row in durable['events'] if row['event']=='capture')
    assert ref['fields'] == ['messages']
    assert hydrate_journal(durable,lambda _:wire) == packet
    assert len(store.index()['entries']) == 1


def _resign(packet):
    packet['sha256'] = hashlib.sha256(encoded({key:value for key,value in packet.items() if key != 'sha256'})).hexdigest()
    return packet


@pytest.mark.parametrize('change,expected', [
    ('recording_owner','recording owner'), ('duplicate','Duplicate journal'),
    ('projection','wire projection'), ('fields','wire fields'), ('omitted','omitted payload')])
def test_modified_envelope_rejects_before_legacy_validation(tmp_path,change,expected):
    store = PayloadStore(recorder(tmp_path),'test')
    durable = store.materialize_journal(journal({'value':'safe'}))
    capture = next(row for row in durable['events'] if row['event']=='capture')
    if change == 'recording_owner':
        durable['recording_test_id'] = ''
    elif change == 'duplicate':
        capture['values'] = {}
    elif change == 'projection':
        capture['payload']['projection'] = 'unknown'
    elif change == 'fields':
        capture['payload'].update(projection='llm_request',fields=['missing'])
    else:
        capture['payload'] = {'kind':'omitted','reason':capture['reason']}
    with pytest.raises(ValueError,match=expected):
        hydrate_journal(_resign(durable), lambda _: {'value':'safe'})


@pytest.mark.parametrize('packet',[None,{}, {'version':3}, {'version':2,'kind':'other'}])
def test_unsupported_durable_envelope_rejects(packet):
    with pytest.raises(ValueError,match='durable journal'):
        hydrate_journal(packet,None)


@pytest.mark.parametrize('values', [[[[[[[[[[0]]]]]]]]],
    {str(number):number for number in range(2048)}, {'x' * 129:0}])
def test_payload_depth_and_map_bounds_reject(tmp_path,values):
    with pytest.raises(ValueError):
        PayloadStore(recorder(tmp_path),'test').put({'kind':'node'},values)


@pytest.mark.parametrize('test_id,alias',[('',None),(None,None),('test',''),('test',1)])
def test_store_requires_explicit_valid_owner(tmp_path,test_id,alias):
    with pytest.raises(ValueError,match='owner'):
        PayloadStore(recorder(tmp_path),test_id,journal_test_id=alias)


@pytest.mark.parametrize('occurrence,owner,reason',[(None,None,'captured'),({},None,'captured'),
    ({'kind':'node'},1,'captured'),({'kind':'node'},None,'unknown')])
def test_invalid_occurrence_metadata_rejects(tmp_path,occurrence,owner,reason):
    with pytest.raises(ValueError):
        PayloadStore(recorder(tmp_path),'test').put(occurrence,None,owner=owner,reason=reason)


def test_entry_limit_applies_to_payload_and_omission(tmp_path,monkeypatch):
    import api_agent.testing.payloads as module
    monkeypatch.setattr(module,'MAX_ENTRIES',1)
    store = PayloadStore(recorder(tmp_path),'test')
    store.put({'kind':'node'},None)
    with pytest.raises(ValueError,match='occurrence limit'):
        store.put({'kind':'another'},None)
    with pytest.raises(ValueError,match='occurrence limit'):
        store.put_wire(str(uuid4()),'request',{},reason='withheld')
    call = str(uuid4())
    other = PayloadStore(store.recorder,'other')
    other.put_wire(call,'request',{},reason='withheld')
    with pytest.raises(ValueError,match='Conflicting'):
        other.put_wire(call,'request',{},reason='unavailable')


def test_resolver_rejects_noncanonical_bytes_and_missing_index(tmp_path):
    run = recorder(tmp_path)
    body = b'{"value": 1}'
    run.record_artifact('payload.json',body)
    run.record_test(result())
    resolver = PayloadResolver.local(tmp_path,run.finalize())
    ref = dict(kind='payload',source='payload.json',length=len(body),sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(ValueError,match='canonical'):
        resolver(ref)
    with pytest.raises(ValueError,match='not bound'):
        resolver.index('missing.json')


def test_omitted_index_is_admitted_without_value_read(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    store.put_wire(str(uuid4()),'request',{},reason='withheld')
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path,manifest)
    assert resolver.index('index.json') == store.index()
    assert resolver.remaining == 4 * 1024 * 1024


def test_wire_projection_index_direction_and_occurrence_binding(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    ref = store.put_wire(call,'request',{'messages':[]})
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path,manifest)
    resolver.index('index.json')
    event = dict(direction='input',cause_id=call)
    with pytest.raises(ValueError,match='direction'):
        resolver.verify_occurrence(dict(ref,projection='llm_response'),{},event,recording_test_id='test')
    event['cause_id'] = str(uuid4())
    with pytest.raises(ValueError,match='occurrence'):
        resolver.verify_occurrence(dict(ref,projection='llm_request'),{},event,recording_test_id='test')


def test_wire_subset_conflict_rejects(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    call = str(uuid4())
    store.put_wire(call,'request',{'messages':[]})
    with pytest.raises(ValueError,match='differs'):
        store.materialize_journal(journal({'llm_request':{'messages':['changed']}},call))


def test_envelope_size_limit_applies_to_reads_and_writes(tmp_path,monkeypatch):
    import api_agent.testing.payloads as module
    store = PayloadStore(recorder(tmp_path),'test')
    packet = journal()
    durable = store.materialize_journal(packet)
    monkeypatch.setattr(module,'MAX_JOURNAL',1)
    with pytest.raises(ValueError,match='exceeds limit'):
        hydrate_journal(durable,None)
    with pytest.raises(ValueError,match='exceeds limit'):
        store.materialize_journal(packet)


def test_call_correlation_owner_rejects_before_new_journal_payloads(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    call = str(uuid4())
    store.put_wire(call,'request',{'messages':[]})
    before = store.index()
    packet = journal({'llm_request':{'messages':[]}},call,call_owner='another')
    with pytest.raises(ValueError,match='correlation'):
        store.materialize_journal(packet)
    assert store.index() == before


def test_wire_occurrence_validation_and_missing_authority(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    with pytest.raises(ValueError):
        store.put_wire('invalid','request',{})
    with pytest.raises(ValueError,match='Invalid wire'):
        store.put_wire(str(uuid4()),'other',{})
    with pytest.raises(ValueError,match='authoritative'):
        store.materialize_journal(journal({'llm_request':{}},str(uuid4())))


def test_invalid_omission_and_unbound_index_entries_reject(tmp_path):
    from api_agent.testing.payloads import validate_payload_index
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    store.put_wire(str(uuid4()),'request',{},reason='withheld')
    index = store.index()
    index['entries'][0]['reason'] = 'captured'
    with pytest.raises(ValueError,match='omitted'):
        validate_payload_index(index,{'files':{}})
    stored = PayloadStore(run,'test')
    stored.put({'kind':'node'},None)
    with pytest.raises(ValueError,match='Unbound'):
        validate_payload_index(stored.index(),{'files':{}})


def test_index_aggregate_rejects_and_source_cannot_gain_another_owner(tmp_path,monkeypatch):
    from copy import deepcopy
    import api_agent.testing.payloads as module
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    ref = store.put({'kind':'node','event_id':'first'},None)
    changed = deepcopy(store.index())
    changed['entries'][0]['occurrence']['event_id'] = 'second'
    manifest = _finalized_with_indexes(run,{'one.json':store.index(),'two.json':changed})
    resolver = PayloadResolver.local(tmp_path,manifest)
    resolver.index('one.json')
    with pytest.raises(ValueError,match='source ownership'):
        resolver.index('two.json')
    monkeypatch.setattr(module,'MAX_TOTAL',0)
    with pytest.raises(ValueError,match='aggregate'):
        module.validate_payload_index(store.index(),manifest)


def test_evidence_refresh_preserves_result_identity_and_reuses_sources(tmp_path):
    run = recorder(tmp_path)
    filename = run.record_test(result())
    before = json.loads((tmp_path / filename).read_bytes())
    source = run.record_artifact('wire.json',b'{}')
    steps = [{'name':'GRAPH','attachments':[{'name':'Input','source':source,'type':'application/json'}]}]
    run.update_test_evidence(filename, attachments=[run.existing_attachment('Wire',source)],steps=steps)
    after = json.loads((tmp_path / filename).read_bytes())
    assert {key:value for key,value in before.items() if key!='attachments'} == {
        key:value for key,value in after.items() if key not in {'attachments','steps'}}
    assert after['steps'] == steps
    assert after['attachments'][0]['source'] == source
    assert set(run.finalize()['files']) == {source,filename}
    with pytest.raises(RuntimeError):
        run.update_test_evidence(filename,attachments=[],steps=[])


@pytest.mark.parametrize('method',['record','refresh','settle'])
@pytest.mark.parametrize('source',['../outside','missing.json'])
def test_all_native_step_writers_reject_unowned_sources(tmp_path,method,source):
    run = recorder(tmp_path)
    filename = run.record_test(result())
    before = (tmp_path / filename).read_bytes()
    steps = [{'steps':[{'attachments':[dict(name='Input',source=source,type='application/json')]}]}]
    with pytest.raises(ValueError):
        if method == 'record':
            run.record_test(result(),steps=steps)
        elif method == 'refresh':
            run.update_test_evidence(filename,attachments=[],steps=steps)
        else:
            run.update_workflow_steps({filename:steps})
    assert (tmp_path / filename).read_bytes() == before
    assert set(tmp_path.iterdir()) == {tmp_path / filename}


def test_failed_evidence_refresh_keeps_original_row_and_blocks_finalize(tmp_path,monkeypatch):
    from pathlib import Path
    run = recorder(tmp_path)
    filename = run.record_test(result())
    before = (tmp_path / filename).read_bytes()
    def reject_replace(*args):
        raise OSError('synthetic atomic replacement failure')
    monkeypatch.setattr(Path,'replace',reject_replace)
    with pytest.raises(OSError):
        run.update_test_evidence(filename,attachments=[('Legacy',b'{}','application/json')],steps=[])
    assert (tmp_path / filename).read_bytes() == before
    assert not list(tmp_path.glob('.*.tmp'))
    with pytest.raises(RuntimeError,match='incomplete'):
        run.finalize()


def test_evidence_refresh_rejects_unknown_and_changed_result_identity(tmp_path):
    run = recorder(tmp_path)
    with pytest.raises(ValueError,match='Unknown'):
        run.update_test_evidence('../outside',attachments=[],steps=[])
    filename = run.record_test(result())
    path = tmp_path / filename
    row = json.loads(path.read_bytes())
    row['uuid'] = str(uuid4())
    path.write_text(json.dumps(row))
    before = path.read_bytes()
    with pytest.raises(ValueError,match='identity'):
        run.update_test_evidence(filename,attachments=[],steps=[])
    assert path.read_bytes() == before


@pytest.mark.parametrize('steps',[[{'attachments':{}}],[{'attachments':[None]}]])
def test_invalid_native_attachment_container_rejects(tmp_path,steps):
    with pytest.raises(ValueError,match='native step attachment'):
        recorder(tmp_path).record_test(result(),steps=steps)


def test_native_tree_validator_rejects_excessive_depth(tmp_path):
    steps = []
    for _ in range(66):
        steps = [steps]
    with pytest.raises(ValueError,match='tree exceeds'):
        recorder(tmp_path)._validate_step_sources(steps)


def test_finalize_rejects_unregistered_shared_evidence(tmp_path):
    run = recorder(tmp_path)
    run.record_test(result())
    with pytest.raises(ValueError,match='Shared attachments'):
        run.finalize(shared_artifacts=[('Unknown','missing.json','application/json')])


def test_finalize_shared_attachment_failure_keeps_result_atomic(tmp_path,monkeypatch):
    from pathlib import Path
    run = recorder(tmp_path)
    filename = run.record_test(result())
    source = run.record_artifact('shared.json',b'{}')
    before = (tmp_path / filename).read_bytes()
    def reject_replace(*args):
        raise OSError('synthetic finalization replacement failure')
    monkeypatch.setattr(Path,'replace',reject_replace)
    with pytest.raises(OSError):
        run.finalize(shared_artifacts=[('Shared',source,'application/json')])
    assert (tmp_path / filename).read_bytes() == before
    assert not list(tmp_path.glob('.*.tmp'))
    assert not (tmp_path / 'manifest.json').exists()


@pytest.mark.parametrize('attachment_tree',[False,True])
def test_native_item_limit_precedes_source_reads_and_wide_expansion(tmp_path,monkeypatch,attachment_tree):
    run = recorder(tmp_path)
    source = run.record_artifact('canonical.json',b'{}')
    reads = []
    original = run.existing_attachment
    def observe(*args,**kwargs):
        reads.append(args)
        return original(*args,**kwargs)
    monkeypatch.setattr(run,'existing_attachment',observe)
    entry = dict(name='Input',source=source,type='application/json')
    steps = [{'attachments':[entry] * 65537}] if attachment_tree else [None] * 65536
    with pytest.raises(ValueError,match='tree exceeds'):
        run._validate_step_sources(steps)
    assert reads == []


def test_native_exact_item_limit_and_empty_tree_are_admitted(tmp_path):
    run = recorder(tmp_path)
    run._validate_step_sources([])
    run._validate_step_sources([None] * 65535)
    source = run.record_artifact('canonical.json',b'{}')
    # Root list + dict + attachments list + descriptor dict + three scalar
    # fields = seven items. Shared object references count for each occurrence.
    entry = dict(name='Input',source=source,type='application/json')
    run._validate_step_sources([{'attachments':[entry]}])


@pytest.mark.parametrize('reverse',[False,True])
def test_cross_invocation_call_claims_reject_before_materialization(tmp_path,reverse):
    store = PayloadStore(recorder(tmp_path),'test')
    call = str(uuid4())
    packets = [journal({'llm_request':{'messages':[]}},call),journal({'llm_request':{'messages':[]}},call)]
    if reverse:
        packets.reverse()
    with pytest.raises(ValueError,match='correlation'):
        store.admit_journals(packets)
    assert store._call_owners == {}
    assert list(tmp_path.iterdir()) == []
    # A rejected batch cannot poison later legitimate claims.
    store.admit_journals([packets[0],packets[0]])
    with pytest.raises(ValueError,match='correlation'):
        store.materialize_journal(packets[1])


def test_testwide_ownership_admission_is_bounded(tmp_path,monkeypatch):
    import api_agent.testing.payloads as module
    store = PayloadStore(recorder(tmp_path),'test')
    monkeypatch.setattr(module,'MAX_ENTRIES',1)
    with pytest.raises(ValueError,match='admission limit'):
        store.admit_journals([journal(),journal()])
    call = str(uuid4())
    store.admit_journals([journal({'llm_request':{}},call)])
    with pytest.raises(ValueError,match='ownership limit'):
        store.admit_journals([journal({'llm_request':{}},str(uuid4()))])


def test_persisted_wire_owner_cannot_be_reassigned(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    ref = store.put_wire(call,'request',{'messages':[]})
    packet = journal({'llm_request':{'messages':[]}},call,test_id='test')
    durable = store.materialize_journal(packet)
    assert store.put_wire(call,'request',{'messages':[]}) == ref
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path,manifest)
    resolver.index('index.json')
    assert hydrate_journal(durable,resolver) == packet
    event = next(row for row in packet['events'] if row['event']=='capture')
    ref = dict(ref,projection='llm_request')
    with pytest.raises(ValueError,match='invocation and visit owner'):
        resolver.verify_occurrence(ref,dict(packet,invocation_id=str(uuid4())),event,recording_test_id='test')


def test_orphan_wire_has_no_authority_to_claim_a_node(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    ref = store.put_wire(call,'request',{'messages':[]})
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path,manifest)
    resolver.index('index.json')
    assert resolver(ref) == {'messages':[]}
    with pytest.raises(ValueError,match='invocation and visit owner'):
        resolver.verify_occurrence(dict(ref,projection='llm_request'),{'invocation_id':'fake'},
            dict(direction='input',cause_id=call,visit_id='fake'),recording_test_id='test')


def test_payload_owner_and_occurrence_are_snapshotted_on_admission(tmp_path):
    store = PayloadStore(recorder(tmp_path),'test')
    occurrence = {'kind':'llm','call_id':str(uuid4()),'phase':'request'}
    owner = {'invocation_id':str(uuid4()),'visit_id':str(uuid4())}
    store.put(occurrence,{'value':'safe'},owner=owner)
    before = store.index()
    owner['visit_id'] = 'changed'
    occurrence['phase'] = 'response'
    assert store.index() == before
    # Returned index dictionaries cannot mutate internal ownership either.
    before['entries'][0]['owner']['visit_id'] = 'another'
    assert store.index()['entries'][0]['owner']['visit_id'] != 'another'


@pytest.mark.parametrize('method',['record','refresh','settle'])
def test_step_privacy_sees_complete_cyclic_ancestor_context(tmp_path,method):
    from api_agent.telemetry.privacy import EmissionBoundary
    protected = {'password':'synthetic-context-secret','opaque':'synthetic-fragment'}
    boundary = EmissionBoundary(ContentPolicy(True),secrets=(json.dumps(protected),))
    run = RunRecorder(tmp_path,RunIdentity.create('example','integration'),evidence_boundary=boundary)
    steps = [{'name':'ordinary'},{'name':'protected'}]
    steps[1]['steps'] = [dict(protected,ignored=steps)]
    filename = run.record_test(result())
    if method == 'record':
        filename = run.record_test(result(),steps=steps)
    elif method == 'refresh':
        run.update_test_evidence(filename,attachments=[],steps=steps)
    else:
        run.update_workflow_steps({filename:steps})
    saved = json.loads((tmp_path / filename).read_bytes())
    assert saved['steps'][0] == {'name':'ordinary'}
    assert saved['steps'][1]['name'] == 'protected'
    assert 'synthetic-fragment' not in json.dumps(saved)


def test_returned_resolver_index_cannot_change_admitted_authority(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run,'test')
    call = str(uuid4())
    store.put_wire(call,'request',{'messages':[]})
    packet = journal({'llm_request':{'messages':[]}},call,test_id='test')
    durable = store.materialize_journal(packet)
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    resolver = PayloadResolver.local(tmp_path,manifest)
    exposed = resolver.index('index.json')
    entry = exposed['entries'][0]
    entry['owner']['visit_id'] = 'forged'
    entry['occurrence']['call_id'] = str(uuid4())
    entry['length'] = 0
    assert hydrate_journal(durable,resolver) == packet
    capture = next(row for row in packet['events'] if row['event']=='capture')
    ref = next(row['payload'] for row in durable['events'] if row['event']=='capture')
    with pytest.raises(ValueError,match='invocation and visit owner'):
        resolver.verify_occurrence(ref,packet,dict(capture,visit_id='forged'),recording_test_id='test')


@pytest.mark.parametrize('phase,reason',[('request','truncated'),('request','unavailable'),('response','truncated'),('response','unavailable')])
def test_legacy_empty_wire_projection_preserves_exact_journal_without_file(tmp_path,phase,reason):
    from api_agent.workflows.journal import _encoded, validate_journal
    store = PayloadStore(recorder(tmp_path),'test')
    call = str(uuid4())
    projection = 'llm_request' if phase=='request' else 'llm_response'
    packet = journal({projection:{}},call)
    capture = next(row for row in packet['events'] if row['event']=='capture')
    capture.update(reason=reason,direction='input' if phase=='request' else 'output')
    packet['sha256'] = hashlib.sha256(_encoded({key:value for key,value in packet.items() if key!='sha256'})).hexdigest()
    validate_journal(packet)
    assert store.put_wire(call,phase,{},reason=reason) is None
    durable = store.materialize_journal(packet)
    omission = next(row['payload'] for row in durable['events'] if row['event']=='capture')
    assert omission == dict(kind='omitted',reason=reason,projection=projection)
    assert hydrate_journal(durable,None) == packet
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('swap',['directory','ancestor'])
def test_local_resolver_remains_anchored_after_path_replacement(tmp_path,swap):
    directory = tmp_path / 'store' / 'run'
    run = recorder(directory)
    store = PayloadStore(run,'test')
    ref = store.put({'kind':'node'}, {'value':'original'})
    manifest = _finalized_with_indexes(run,{'index.json':store.index()})
    with PayloadResolver.local(directory,manifest) as resolver:
        resolver.index('index.json')
        target = directory if swap=='directory' else directory.parent
        moved = tmp_path / 'moved'
        target.rename(moved)
        foreign = tmp_path / 'foreign'
        foreign.mkdir()
        target.symlink_to(foreign,target_is_directory=True)
        assert resolver(ref) == {'value':'original'}
    with pytest.raises(ValueError,match='closed'):
        resolver(ref)
    with pytest.raises(ValueError,match='closed'):
        resolver.index('index.json')
    with pytest.raises(ValueError,match='closed'):
        resolver.__enter__()
    with pytest.raises(ValueError,match='closed'):
        resolver.read(ref['source'])


def test_local_resolver_close_does_not_close_a_reused_descriptor(tmp_path,monkeypatch):
    import os
    run = recorder(tmp_path)
    run.record_test(result())
    manifest = run.finalize()
    opened = []
    actual_open = os.open
    def observe(*args,**kwargs):
        descriptor = actual_open(*args,**kwargs)
        opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(os,'open',observe)
    resolver = PayloadResolver.local(tmp_path,manifest)
    descriptor = opened[-1]
    resolver.close()
    replacement = actual_open('/dev/null',os.O_RDONLY)
    try:
        if replacement != descriptor:
            os.dup2(replacement,descriptor)
        resolver.close()
        os.fstat(descriptor)
    finally:
        os.close(descriptor)
        if replacement != descriptor:
            os.close(replacement)


def test_local_resolver_finalizer_releases_directory_descriptor(tmp_path,monkeypatch):
    import gc
    import os
    import weakref
    run = recorder(tmp_path)
    run.record_test(result())
    manifest = run.finalize()
    opened = []
    actual_open = os.open
    def observe(*args,**kwargs):
        descriptor = actual_open(*args,**kwargs)
        opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(os,'open',observe)
    resolver = PayloadResolver.local(tmp_path,manifest)
    descriptor = opened[-1]
    reference = weakref.ref(resolver)
    del resolver
    gc.collect()
    assert reference() is None
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_local_resolver_failed_manifest_admission_closes_descriptor(tmp_path,monkeypatch):
    import os
    opened = []
    actual_open = os.open
    def observe(*args,**kwargs):
        descriptor = actual_open(*args,**kwargs)
        opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(os,'open',observe)
    with pytest.raises(RuntimeError):
        PayloadResolver.local(tmp_path,{})
    with pytest.raises(OSError):
        os.fstat(opened[-1])


def test_supplied_reader_resolver_context_requires_no_reader_close(tmp_path):
    run = recorder(tmp_path)
    run.record_test(result())
    with PayloadResolver(run.finalize(), lambda _:b'{}') as resolver:
        assert resolver.read('unused') == b'{}'
    resolver.close()
    with pytest.raises(ValueError,match='closed'):
        resolver({})


def test_successful_empty_journal_has_canonical_payload_and_requires_resolver(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, 'test')
    packet = journal({}, test_id='test')
    capture = next(row for row in packet['events'] if row['event'] == 'capture')
    assert capture['reason'] == 'captured' and capture['values'] == {}
    durable = store.materialize_journal(packet)
    entry = store.index()['entries'][0]
    assert entry['kind'] != 'omitted'
    assert json.loads((tmp_path / entry['source']).read_bytes()) == {}
    with pytest.raises(TypeError):
        hydrate_journal(durable, None)
    manifest = _finalized_with_indexes(run, {'empty-index.json': store.index()})
    with PayloadResolver.local(tmp_path, manifest) as resolver:
        resolver.index('empty-index.json')
        assert hydrate_journal(durable, resolver) == packet
