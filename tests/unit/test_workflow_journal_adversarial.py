from api_agent.telemetry.privacy import ContentPolicy
"""Mutation probes for causality and retention at the recording boundary."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.testing.workflow_steps import scope_steps


def _definition():
    step = SimpleNamespace(id='Make', kind='node', parent=None)
    return SimpleNamespace(graph_id='Example', definition_id='a' * 64,
        entry_root='base', layout=SimpleNamespace(steps=[step], tables={'Edges': []}),
        resources=[], replacements=[])


def _complete():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    graph, visit, call = str(uuid4()), str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=graph, parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=visit, parent_id=graph,
                         kind='node', owner='Make', started_ms=1))
    journal.record_call(call_id=call, visit_id=visit)
    journal.observe(dict(event='node', scope_id=visit, node='Make'))
    journal.observe(dict(event='scope_end', scope_id=visit, parent_id=graph,
                         kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Make', target='END',
                         implicit=True, visit_id=visit))
    journal.observe(dict(event='scope_end', scope_id=graph, parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    return journal, graph, visit, call


def _rehash(payload):
    payload['sha256'] = sha256(json.dumps({key: value for key, value in payload.items()
        if key != 'sha256'}, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def test_orphan_scope_cannot_claim_complete_or_allure_root():
    journal, _, visit, _ = _complete()
    packet = journal.finalize()
    orphan = str(uuid4())
    for event in packet['events']:
        if event.get('scope_id') == visit:
            event['parent_id'] = orphan
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)
    with pytest.raises(ValueError, match='parent is absent'):
        scope_steps(packet['events'])


def test_late_call_and_wrong_route_visit_cannot_claim_complete():
    journal, _, visit, _ = _complete()
    packet = journal.finalize()
    call = next(row for row in packet['events'] if row['event'] == 'call')
    packet['events'].remove(call)
    packet['events'].insert(5, call)
    for sequence, event in enumerate(packet['events'], 1):
        event['sequence'] = sequence
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


def test_producer_rejects_call_after_node_visit_closes():
    journal, _, visit, _ = _complete()
    with pytest.raises(ValueError, match='open node visit'):
        journal.record_call(call_id=str(uuid4()), visit_id=visit)


def test_finalization_detaches_and_closes_even_partial_journal():
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    with pytest.raises(ValueError, match='finalized'):
        journal.observe(dict(event='node', node='Make'))
    validate_journal(packet)
    with pytest.raises(ValueError, match='finalized'):
        journal.finalize()
    partial = WorkflowJournal(_definition())
    assert partial.finalize()['completeness'] == 'partial'
    with pytest.raises(ValueError, match='finalized'):
        partial.finalize()


def test_analysis_alias_is_excluded_before_retention():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='input',
        values={'question': 'hello', 'analysis': 'hidden chain', 'auth_state': 'secret'},
        schema={'type': 'object', 'properties': {key: {'type': 'string'}
            for key in ('question', 'analysis', 'auth_state')}},
        plain_fields=('question',))
    capture = journal.finalize()['events'][0]
    assert capture['values'] == {'question': 'hello'}


def test_unclassified_native_state_is_withheld_even_when_content_enabled():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='output',
        values={'cot': 'hidden chain', 'answer': 'safe answer'},
        schema={'type': 'object', 'properties': {
            'cot': {'type': 'string'}, 'answer': {'type': 'string'}}})
    packet = journal.finalize()
    validate_journal(packet)
    event = packet['events'][0]
    assert event['reason'] == 'unclassified'
    assert 'values' not in event
    assert 'hidden chain' not in json.dumps(event)


def test_forged_capture_alias_rejected_at_runner_boundary():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='input', values={'question': 'hello'},
        schema={'type': 'object', 'properties': {'question': {'type': 'string'}}},
        plain_fields=('question',))
    packet = journal.finalize()
    packet['events'][0]['values']['analysis'] = 'hidden chain'
    _rehash(packet)
    with pytest.raises(ValueError, match='Unsafe workflow capture'):
        validate_journal(packet)


def test_projection_requires_independent_definition_binding():
    journal, _, _, _ = _complete()
    packet = deepcopy(journal.finalize())
    packet['projection']['edges'][0]['condition'] = 'false'
    _rehash(packet)
    with pytest.raises(ValueError, match='independently admitted'):
        validate_journal(packet, expected_definition=_definition())


@pytest.mark.parametrize('tamper', ('missing', 'malformed', 'duplicate'))
def test_event_identity_must_be_canonical_uuid_and_unique(tamper):
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    if tamper == 'missing':
        packet['events'][1].pop('event_id')
    elif tamper == 'malformed':
        packet['events'][1]['event_id'] = 'forged-event'
    else:
        packet['events'][1]['event_id'] = packet['events'][0]['event_id']
    _rehash(packet)
    with pytest.raises(ValueError, match='event identity'):
        validate_journal(packet)


@pytest.mark.parametrize(('tamper', 'message'), (
    (lambda p: p['projection'].__setitem__('graph_id', '-invalid'), 'projection'),
    (lambda p: p['projection']['steps'][0].__setitem__('id', '-invalid'), 'projection step'),
    (lambda p: p['projection']['edges'][0].__setitem__('condition', '-invalid'), 'projection edge'),
    (lambda p: p['projection']['resources'].append(dict(kind='declaration',
        logical_path='../escape', sha256='a' * 64, length=1)), 'projection resource'),
    (lambda p: p['projection']['replacements'].append(dict(target_id='Make',
        root_id='outside')), 'projection replacement'),
    (lambda p: p['projection']['component_passes'].append(dict(id='Loop',
        max_passes=0, guard='Make')), 'component pass'),
    (lambda p: p.__setitem__('content', 'raw'), 'capture policy'),
    (lambda p: p['events'][0].__setitem__('turn_id', str(uuid4())), 'owner mismatch'),
    (lambda p: p['events'][0].__setitem__('unexpected', 'raw'), 'sequence or field'),
))
def test_structural_artifact_contract_rejects_forged_fields(tamper, message):
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    tamper(packet)
    _rehash(packet)
    with pytest.raises(ValueError, match=message):
        validate_journal(packet)


def test_plain_capture_requires_owner_classification_and_scalar_schema():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    schema = {'type': 'object', 'properties': {
        'answer': {'type': 'string'}, 'count': {'type': 'integer'}}}
    with pytest.raises(ValueError, match='Unsafe plain'):
        journal.capture(visit_id='visit', direction='output',
            values={'answer': 'text'}, schema=schema, plain_fields=('analysis',))
    with pytest.raises(ValueError, match='not scalar'):
        journal.capture(visit_id='visit', direction='output',
            values={'count': []}, schema=schema, plain_fields=('count',))
    journal.capture(visit_id='visit', direction='output',
        values={'answer': 'plain answer', 'count': 2}, schema=schema,
        plain_fields=('answer', 'count'))
    retained = journal.finalize()['events'][-1]['values']
    assert retained == {'answer': 'plain answer', 'count': 2}


@pytest.mark.parametrize(('values', 'schema', 'fields'), (
    ([], {'type': 'object', 'properties': {}}, ('answer',)),
    ({'answer': 'text'}, {'type': 'array'}, ('answer',)),
    ({'answer': 'text'}, {'type': 'object'}, ('answer',)),
    ({'answer': 'text'}, {'type': 'object', 'properties': {'answer': {'type': 'string'}}},
     ('answer', 'answer')),
    ({'answer': 'text'}, {'type': 'object', 'properties': {'answer': {'type': 'string'}}},
     ('hidden_reasoning',)),
    ({'answer': float('inf')}, {'type': 'object', 'properties': {'answer': {'type': 'number'}}},
     ('answer',)),
    ({'answer': 1 << 60}, {'type': 'object', 'properties': {'answer': {'type': 'integer'}}},
     ('answer',)),
))
def test_plain_capture_rejects_undeclared_or_unbounded_values(values, schema, fields):
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    with pytest.raises(ValueError):
        journal.capture(visit_id='visit', direction='input', values=values,
                        schema=schema, plain_fields=fields)


@pytest.mark.parametrize('row', (
    {'event': 'node', 'node': '-unsafe'},
    {'event': 'node', 'scope_id': 'x' * 65},
    {'event': 'capture', 'values': {'answer': 'unclassified'}},
    {'event': 1},
))
def test_producer_refuses_unsafe_observations_before_queue(row):
    journal = WorkflowJournal(_definition())
    with pytest.raises(ValueError):
        journal.observe(row)
    assert journal.finalize()['events'] == []


def test_event_limit_marks_partial_without_retaining_overflow(monkeypatch):
    import api_agent.workflows.journal as module
    monkeypatch.setattr(module, '_MAX_EVENTS', 1)
    journal = WorkflowJournal(_definition())
    journal.observe({'event': 'scope_start', 'scope_id': 'graph',
                     'kind': 'graph', 'owner': 'GRAPH', 'started_ms': 1})
    journal.observe({'event': 'edge', 'owner': 'GRAPH', 'source': 'START',
                     'target': 'Make', 'implicit': True})
    result = journal.finalize()
    assert result['gap'] == 'event_limit' and result['completeness'] == 'partial'
    assert len(result['events']) == 1


def test_plain_capture_preserves_only_bounded_declared_scalar_types():
    schema = {'type': 'object', 'properties': {
        'answer': {'type': 'string'}, 'ready': {'type': 'boolean'},
        'ratio': {'type': 'number'}, 'count': {'type': 'integer'},
        'empty': {'type': 'null'}}}
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='output', schema=schema,
        values={'answer': 'x' * 700, 'ready': True, 'ratio': 0.5,
                'count': 3, 'empty': None, 'extra': 'excluded'},
        plain_fields=('answer', 'ready', 'ratio', 'count', 'empty'))
    values = journal.finalize()['events'][0]['values']
    assert values == {'answer': 'x' * 512, 'ready': True, 'ratio': 0.5,
                      'count': 3, 'empty': None}


def test_plain_string_capture_distinguishes_exact_limit_from_truncation():
    schema = {'type': 'object', 'properties': {
        'answer': {'type': 'string'}, 'note': {'type': 'string'}}}
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='output', schema=schema,
        values={'answer': 'x' * 512, 'note': 'y' * 513},
        plain_fields=('answer', 'note'))
    packet = journal.finalize()
    validate_journal(packet)
    event = packet['events'][0]
    assert event['reason'] == 'truncated'
    assert event['truncated_fields'] == ['note']
    assert event['values'] == {'answer': 'x' * 512, 'note': 'y' * 512}
    exact = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    exact.capture(visit_id='visit', direction='output', schema=schema,
        values={'answer': 'x' * 512}, plain_fields=('answer',))
    exact_event = exact.finalize()['events'][0]
    assert exact_event['reason'] == 'captured' and 'truncated_fields' not in exact_event


def test_forged_capture_truncation_status_is_rejected():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='output',
        values={'answer': 'plain'},
        schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
        plain_fields=('answer',))
    packet = journal.finalize()
    event = packet['events'][0]
    event['reason'] = 'truncated'
    _rehash(packet)
    with pytest.raises(ValueError, match='Unclassified workflow capture'):
        validate_journal(packet)
    truncated = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    truncated.capture(visit_id='visit', direction='output',
        values={'answer': 'x' * 513},
        schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
        plain_fields=('answer',))
    truncated_packet = truncated.finalize()
    validate_journal(truncated_packet)
    truncated_packet['events'][0]['reason'] = 'captured'
    _rehash(truncated_packet)
    with pytest.raises(ValueError, match='Unclassified workflow capture'):
        validate_journal(truncated_packet)
    truncated_packet['events'][0]['reason'] = 'truncated'
    truncated_packet['events'][0].pop('truncated_fields')
    _rehash(truncated_packet)
    with pytest.raises(ValueError, match='Unclassified workflow capture'):
        validate_journal(truncated_packet)
    event['truncated_fields'] = ['analysis']
    _rehash(packet)
    with pytest.raises(ValueError, match='Unclassified workflow capture'):
        validate_journal(packet)


def test_capture_bound_declares_gap_without_retaining_large_value(monkeypatch):
    import api_agent.workflows.journal as module
    monkeypatch.setattr(module, '_MAX_BYTES', 100)
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='input', values={'answer': 'plain'},
        schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
        plain_fields=('answer',))
    packet = journal.finalize()
    assert packet['events'] == []
    assert packet['gap'] == 'event_limit' and packet['completeness'] == 'partial'


@pytest.mark.parametrize('tamper', (
    lambda rows: rows[0].__setitem__('owner', 'Other'),
    lambda rows: rows[2].__setitem__('parent_id', str(uuid4())),
    lambda rows: rows[4].__setitem__('node', 'Other'),
    lambda rows: rows[5].__setitem__('started_ms', 2),
    lambda rows: rows[6].__setitem__('target', 'Make'),
    lambda rows: rows[6].__setitem__('visit_id', str(uuid4())),
    lambda rows: rows[7].__setitem__('stopped_ms', 0),
))
def test_causal_route_mutations_cannot_claim_complete(tamper):
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    tamper(packet['events'])
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


@pytest.mark.parametrize(('tamper', 'message'), (
    (lambda p: p.__setitem__('version', 2), 'version'),
    (lambda p: p.__setitem__('extra', 'forged'), 'Unexpected'),
    (lambda p: p.__setitem__('source_manifest', {'version': 2}), 'source manifest'),
    (lambda p: p.__setitem__('source_manifest', {'version': 1, 'sha256': '0' * 64}), 'manifest digest'),
    (lambda p: p.__setitem__('events', {}), 'event count'),
    (lambda p: p['events'][3].__setitem__('call_id', ''), 'completeness'),
))
def test_journal_envelope_rejects_rehashed_contract_damage(tamper, message):
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    tamper(packet)
    _rehash(packet)
    with pytest.raises(ValueError, match=message):
        validate_journal(packet)


def test_forged_capture_cannot_appear_under_withheld_policy():
    journal, _, visit, _ = _complete()
    packet = journal.finalize()
    capture = dict(event='capture', visit_id=visit, direction='input',
                   values={'answer': 'leak'}, plain_fields=['answer'],
                   capture_class='plain_user_io', reason='captured')
    capture.update(sequence=5, event_id=str(uuid4()),
                   invocation_id=packet['invocation_id'], turn_id=packet['turn_id'])
    packet['events'].insert(4, capture)
    packet['content'] = 'withheld'
    for sequence, event in enumerate(packet['events'], 1):
        event['sequence'] = sequence
    _rehash(packet)
    with pytest.raises(ValueError, match='Withheld workflow capture'):
        validate_journal(packet)


def test_journal_constructor_requires_admitted_definition_and_boundary():
    from api_agent.workflows.journal import projection
    with pytest.raises(ValueError, match='admitted workflow definition'):
        projection(None)
    with pytest.raises(ValueError, match='turn identity'):
        WorkflowJournal(_definition(), turn_id='invalid')
    with pytest.raises(TypeError, match='emission boundary'):
        WorkflowJournal(_definition(), boundary=object())


def test_leaf_call_rejects_bad_usage_and_identity_before_observation():
    journal = WorkflowJournal(_definition())
    with pytest.raises(ValueError, match='visit owner'):
        journal.record_call(visit_id='')
    with pytest.raises(ValueError, match='accounting'):
        journal.record_call(visit_id='visit', usage={'unknown': 1})
    with pytest.raises(ValueError, match='basis'):
        journal.record_call(visit_id='visit', usage={'basis': '-unsafe'})
    with pytest.raises(ValueError, match='amount'):
        journal.record_call(visit_id='visit', usage={'input_tokens': -1})
    with pytest.raises(ValueError, match='call identity'):
        journal.record_call(visit_id='visit', call_id='invalid')
    assert journal.finalize()['events'] == []


def test_leaf_call_retries_reuse_identity_only_for_same_open_visit():
    journal = WorkflowJournal(_definition())
    first, second, call = str(uuid4()), str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=first, kind='node',
                         owner='Make', started_ms=1))
    assert journal.record_call(visit_id=first, call_id=call) == call
    assert journal.record_call(visit_id=first, call_id=call) == call
    journal.observe(dict(event='scope_start', scope_id=second, kind='node',
                         owner='Make', started_ms=1))
    with pytest.raises(ValueError, match='conflicting owner'):
        journal.record_call(visit_id=second, call_id=call)
    assert len([row for row in journal.finalize()['events'] if row['event'] == 'call']) == 1


def test_capture_arguments_rejected_before_retention():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    with pytest.raises(ValueError, match='direction'):
        journal.capture(visit_id='visit', direction='internal', values={}, schema={})
    with pytest.raises(ValueError, match='node visit'):
        journal.capture(visit_id='', direction='input', values={}, schema={})
    assert journal.finalize()['events'] == []


def _component_complete():
    graph, component, first, guard, attempt = (str(uuid4()) for _ in range(5))
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64,
        entry_root='base', layout=SimpleNamespace(steps=[
            SimpleNamespace(id='Loop', kind='component', parent=None),
            SimpleNamespace(id='Work', kind='node', parent='Loop'),
            SimpleNamespace(id='Check', kind='node', parent='Loop')],
            tables={'Edges': [], 'Components': [
                {'ID': 'Loop', 'Max passes': 1, 'Guard': 'Check'}]}),
        resources=[], replacements=[])
    journal = WorkflowJournal(definition)
    journal.observe(dict(event='scope_start', scope_id=graph, parent_id=None,
                         owner='GRAPH', kind='graph', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Loop', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=component, parent_id=graph,
                         owner='Loop', kind='component', started_ms=1))
    journal.observe(dict(event='attempt', scope_id=component, component='Loop',
                         attempt=1, attempt_id=attempt, max_passes=1))
    journal.observe(dict(event='edge', owner='Loop', source='START', target='Work',
                         attempt_id=attempt, implicit=True))
    for owner, visit, source, target in (('Work', first, 'Work', 'Check'),
                                         ('Check', guard, 'Check', 'END')):
        journal.observe(dict(event='scope_start', scope_id=visit, parent_id=component,
                             owner=owner, kind='node', attempt_id=attempt, started_ms=1))
        journal.observe(dict(event='node', scope_id=visit, node=owner))
        journal.observe(dict(event='scope_end', scope_id=visit, parent_id=component,
                             owner=owner, kind='node', attempt_id=attempt, started_ms=1,
                             stopped_ms=2, outcome='passed'))
        journal.observe(dict(event='edge', owner='Loop', source=source, target=target,
                             visit_id=visit, attempt_id=attempt, implicit=True))
    journal.observe(dict(event='guard', scope_id=component, component='Loop',
                         attempt=1, accepted=True))
    journal.observe(dict(event='scope_end', scope_id=component, parent_id=graph,
                         owner='Loop', kind='component', started_ms=1,
                         stopped_ms=3, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Loop', target='END',
                         visit_id=component, implicit=True))
    journal.observe(dict(event='scope_end', scope_id=graph, parent_id=None,
                         owner='GRAPH', kind='graph', started_ms=1,
                         stopped_ms=4, outcome='passed'))
    return journal.finalize()


def test_component_attempt_and_guard_are_required_for_complete_route():
    packet = _component_complete()
    assert packet['completeness'] == 'complete'
    validate_journal(packet)
    for event_kind, field, value in (('attempt', 'attempt', 2),
                                     ('attempt', 'max_passes', 2),
                                     ('guard', 'accepted', 'yes'),
                                     ('guard', 'attempt', 2)):
        mutant = deepcopy(packet)
        next(row for row in mutant['events'] if row['event'] == event_kind)[field] = value
        _rehash(mutant)
        with pytest.raises(ValueError, match='completeness'):
            validate_journal(mutant)


def test_plain_capture_omits_absent_field_and_rejects_bad_rule():
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    journal.capture(visit_id='visit', direction='input', values={},
        schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
        plain_fields=('answer',))
    with pytest.raises(ValueError, match='capture schema'):
        journal.capture(visit_id='visit', direction='input', values={'answer': 'plain'},
            schema={'type': 'object', 'properties': {'answer': 'string'}},
            plain_fields=('answer',))
    assert journal.finalize()['events'][0]['values'] == {}


def test_canonical_turn_and_call_ids_are_required():
    turn = str(uuid4()).upper()
    with pytest.raises(ValueError, match='turn identity'):
        WorkflowJournal(_definition(), turn_id=turn)
    journal = WorkflowJournal(_definition())
    with pytest.raises(ValueError, match='call identity'):
        journal.record_call(visit_id='visit', call_id=str(uuid4()).upper())


def test_observation_after_event_gap_cannot_resume_retention(monkeypatch):
    import api_agent.workflows.journal as module
    monkeypatch.setattr(module, '_MAX_EVENTS', 0)
    journal = WorkflowJournal(_definition())
    for _ in range(2):
        journal.observe({'event': 'node', 'node': 'Make'})
    assert journal.finalize()['events'] == []


def test_validation_rejects_oversized_envelope_even_with_digest(monkeypatch):
    import api_agent.workflows.journal as module
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    packet['test_id'] = 'x' * (1024 * 1024)
    _rehash(packet)
    monkeypatch.setattr(module, '_MAX_BYTES', 1)
    with pytest.raises(ValueError, match='retention bound'):
        validate_journal(packet)


def test_duplicate_leaf_call_is_rejected_at_artifact_boundary():
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    duplicate = deepcopy(packet['events'][3])
    duplicate['event_id'] = str(uuid4())
    packet['events'].insert(4, duplicate)
    for sequence, event in enumerate(packet['events'], 1):
        event['sequence'] = sequence
    _rehash(packet)
    with pytest.raises(ValueError, match='unique node visit owner'):
        validate_journal(packet)


def test_capture_artifact_requires_plain_classification():
    journal, _, visit, _ = _complete()
    packet = journal.finalize()
    capture = dict(event='capture', visit_id=visit, direction='input',
                   values={'answer': 'plain'}, plain_fields=['answer'],
                   capture_class='plain_user_io', reason='captured')
    capture.update(sequence=5, event_id=str(uuid4()),
                   invocation_id=packet['invocation_id'], turn_id=packet['turn_id'])
    packet['events'].insert(4, capture)
    for sequence, event in enumerate(packet['events'], 1):
        event['sequence'] = sequence
    _rehash(packet)
    validate_journal(packet)
    packet['events'][4]['plain_fields'] = ['other']
    _rehash(packet)
    with pytest.raises(ValueError, match='Unclassified workflow capture'):
        validate_journal(packet)


def test_projection_handles_empty_component_and_bounds_total_metadata():
    from api_agent.workflows.journal import projection
    empty = _definition()
    empty.layout.steps.append(SimpleNamespace(id='Empty', kind='component', parent=None))
    result = projection(empty)
    assert all(row['owner'] != 'Empty' for row in result['edges'])
    huge = _definition()
    huge.resources = [SimpleNamespace(kind='declaration',
        logical_path=f'base/{index:05d}/' + 'x' * 100,
        sha256='a' * 64, content_bytes=b'x') for index in range(9000)]
    with pytest.raises(ValueError, match='projection exceeds'):
        projection(huge)


@pytest.mark.parametrize('tamper', (
    lambda p: p['events'].insert(2, dict(p['events'][0], event_id=str(uuid4()))),
    lambda p: p['projection']['steps'][0].__setitem__('parent', 'Other'),
    lambda p: p['projection']['steps'].append(dict(id='Other', kind='node', parent=None)) or
              p['events'][2].__setitem__('owner', 'Other'),
    lambda p: p['events'][5].__setitem__('scope_id', p['events'][0]['scope_id']),
    lambda p: p['events'].insert(1, dict(p['events'][6], event_id=str(uuid4()))),
    lambda p: p['events'].insert(2, dict(p['events'][1], event_id=str(uuid4()))),
    lambda p: p['events'][1].__setitem__('visit_id', str(uuid4())),
))
def test_scope_owner_and_route_order_reject_rehashed_forgeries(tamper):
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    tamper(packet)
    for sequence, event in enumerate(packet['events'], 1):
        event['sequence'] = sequence
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


def test_valid_source_manifest_and_replacement_are_admitted():
    from hashlib import sha256 as digest
    source = {'version': 1, 'owners': [], 'documents': []}
    source['sha256'] = digest(json.dumps(source, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    packet['source_manifest'] = source
    packet['projection']['replacements'] = [dict(target_id='Make', root_id='base')]
    _rehash(packet)
    assert validate_journal(packet) is packet


def test_capture_structure_limits_nested_and_list_values():
    from api_agent.workflows.journal import _capture_contract
    assert _capture_contract({'plain': ['one', 'two']}) is True
    assert _capture_contract({'plain': list(range(17))}) is False
    nested = {'plain': None}
    for _ in range(6):
        nested = {'plain': nested}
    assert _capture_contract(nested) is False


def test_component_start_edge_requires_active_owner_scope():
    packet = _component_complete()
    events = packet['events']
    entry = next(index for index, row in enumerate(events)
                 if row.get('event') == 'edge' and row.get('owner') == 'Loop'
                 and row.get('source') == 'START')
    misplaced = events.pop(entry)
    events.insert(2, misplaced)
    for sequence, event in enumerate(events, 1):
        event['sequence'] = sequence
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


def test_unknown_observation_cannot_preserve_complete_claim():
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    packet['events'][3]['event'] = 'raw_stream'
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


def test_component_terminal_edge_rejects_wrong_active_owner():
    packet = _component_complete()
    packet['projection']['component_passes'].append(dict(id='Other', max_passes=1, guard='Check'))
    terminal = next(row for row in packet['events']
                    if row.get('event') == 'edge' and row.get('owner') == 'Loop'
                    and row.get('target') == 'END')
    terminal['owner'] = 'Other'
    _rehash(packet)
    with pytest.raises(ValueError, match='completeness'):
        validate_journal(packet)


def test_invalid_upstream_capture_reason_is_rejected_before_retention():
    journal, _, visit, _ = _complete()
    before = len(journal._events)
    with pytest.raises(ValueError, match='Invalid provider capture reason'):
        journal.capture_json(visit_id=visit, direction='input', values={'text': 'safe'},
            schema={'type': 'object'}, capture_reason='fabricated_complete')
    assert len(journal._events) == before


def test_schema_capture_encoded_size_rejection_is_independent_of_string_length():
    journal, _, visit, _ = _complete()
    journal.capture_json(visit_id=visit, direction='input', values={'text': 'safe'}, schema={'type': 'object'})
    packet = journal.finalize()
    capture = next(event for event in packet['events'] if event['event'] == 'capture')
    capture['values'] = {'text': 'é' * 33000}
    _rehash(packet)
    with pytest.raises(ValueError, match='Workflow capture exceeds byte limit'):
        validate_journal(packet)


@pytest.mark.parametrize('value,diagnostic', [
    ({'text': chr(0xD800)}, 'Workflow capture contains invalid UTF-8'),
    ({chr(0xDFFF): 'ordinary'}, 'Unsafe workflow capture values'),
], ids=['text', 'key'])
def test_hostile_non_utf8_capture_has_fixed_validation_error(value, diagnostic):
    journal, _, visit, _ = _complete()
    journal.capture_json(visit_id=visit, direction='output', values={'text': 'ordinary'}, schema={'type': 'object'})
    packet = journal.finalize()
    event = next(row for row in packet['events'] if row['event'] == 'capture')
    event['values'] = value
    _rehash(packet)
    with pytest.raises(ValueError, match=diagnostic) as error:
        validate_journal(packet)
    assert not isinstance(error.value, UnicodeError)
