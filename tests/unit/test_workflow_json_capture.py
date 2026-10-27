"""Shared capture uses exact JSON data and independent destination permissions."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.capture import project_values
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import config, definition


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', [None, 0, 128, (1 << 53) - 1])
def test_native_generation_limit_survives_authorized_capture_projection(key, value):
    payload = {'provider_payload': {key: value}}
    boundary = EmissionBoundary(ContentPolicy(True))
    assert project_values(payload, {'type': 'object'}, boundary) == payload


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', [True, False, 'synthetic-sensitive-value', 128.0, -1])
def test_native_generation_limit_projection_keeps_typed_admission(key, value):
    payload = {'provider_payload': {key: value, 'visible': 'ordinary text'}}
    boundary = EmissionBoundary(ContentPolicy(True))
    assert project_values(payload, {'type': 'object'}, boundary) == {
        'provider_payload': {'visible': 'ordinary text'}}


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
def test_native_generation_limit_projection_preserves_json_integer_bound(key):
    payload = {'provider_payload': {key: 1 << 53, 'visible': 'ordinary text'}}
    assert project_values(payload, {'type': 'object'}, EmissionBoundary(ContentPolicy(True))) == {}


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('secret_kind', ['numeric', 'container'])
def test_native_generation_limit_projection_preserves_registered_secret_guards(key, secret_kind):
    native = {key: 128}
    payload = {'provider_payload': native}
    boundary = EmissionBoundary(ContentPolicy(True))
    assert project_values(payload, {'type': 'object'}, boundary) == payload
    boundary.register_secret('128' if secret_kind == 'numeric' else json.dumps(native))
    exported = project_values(payload, {'type': 'object'}, boundary)
    assert exported != payload and '128' not in json.dumps(exported)


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
def test_native_generation_limit_capture_keeps_content_disabled(key):
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    with collect_inference(EmissionBoundary(ContentPolicy(False))) as collector:
        capture_inference('input', {'provider_payload': {key: 128}}, str(uuid4()))
    assert collector['rows'] == [{'phase': 'request', 'content': {}, 'reason': 'withheld'}]


def test_json_capture_excludes_policy_admission_before_projection(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from api_agent.workflows import capture
    from api_agent.workflows.journal import WorkflowJournal

    entered, resume = Event(), Event()
    live = []
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True))
    original = capture.project_values
    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(capture, 'project_values', paused)
    rejection = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(journal.capture_json, visit_id='visit', direction='input',
            values={'text': 'original-authority-visible'},
            schema={'type': 'object', 'properties': {'text': {'type': 'string'}}})
        try:
            assert entered.wait(5)
            WorkflowObservationFactory(config(False), boundary=EmissionBoundary(),
                sink=live.append).create(definition(), journal=journal)
        except ValueError as error:
            rejection = str(error)
        finally:
            resume.set()
        operation.result(timeout=5)

    retained = journal.finalize()
    assert live == [], 'A denied subscriber must not attach to an active capture'
    assert retained['content'] == 'permitted_scrubbed'
    assert 'original-authority-visible' in json.dumps(retained)
    assert rejection == 'Journal policy must be admitted once before retention'


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}, 'parts': {
    'type': 'array', 'items': {'type': 'object', 'properties': {
        'text': {'type': 'string'}, 'hidden_reasoning': {'type': 'string'}}}}}}


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_json_capture_fans_out_independent_scrubbed_values(log, trace):
    boundary = EmissionBoundary(secrets=('synthetic-credential',))
    span = Mock()
    runtime = SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace)),
                              begin=Mock(return_value=span), is_model_identity=lambda value: False)
    live = []
    adapter = WorkflowObservationFactory(config(log, trace), boundary=boundary,
                                         sink=live.append, trace=runtime).create(definition())
    visit = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    pair = str(uuid4())
    adapter.capture_json(visit_id=visit, cause_id=pair, direction='input', schema=SCHEMA,
                         values={'text': 'visible synthetic-credential ' + 'x' * 600,
                                 'parts': [{'text': 'nested visible', 'hidden_reasoning': 'hidden-sentinel'}],
                                 'undeclared': 'excluded-sentinel'})
    stored = adapter.finalize()['events'][-1]
    emitted = live[-1]
    assert stored['cause_id'] == emitted['cause_id'] == pair
    assert ('values' in stored) is log
    assert ('values' in emitted) is log
    assert span.event.call_count == int(trace)
    if log:
        assert stored['values']['text'] == 'visible [REDACTED] ' + 'x' * 600
        assert len(emitted['values']['text']) > 512
        assert emitted['values']['parts'][0]['text'] == 'nested visible'
    if trace:
        traced = span.event.call_args.args[0]
        assert traced['cause_id'] == pair
        assert traced['values']['parts'] == [{'text': 'nested visible'}]
    serialized = json.dumps(stored) + str(emitted) + str(span.event.call_args)
    assert 'synthetic-credential' not in serialized
    assert 'hidden-sentinel' not in serialized
    assert 'excluded-sentinel' not in serialized


class Hostile:
    def __str__(self):
        raise AssertionError('capture called str')

    def __repr__(self):
        raise AssertionError('capture called repr')


def test_json_capture_drops_runtime_objects_and_cycles_without_losing_visible_siblings():
    cycle = {}
    cycle['loop'] = cycle
    values = {'visible': 'answer', 'object': Hostile(), 'cycle': {},
              'hidden_reasoning': 'excluded', 'parts': [Hostile(), {'type': 'reasoning', 'text': 'hidden'},
                                                       {'type': 'text', 'text': 'visible part'}]}
    captured = project_values(values, {'type': 'object'}, EmissionBoundary())
    assert captured['visible'] == 'answer'
    assert 'object' not in captured
    assert 'hidden_reasoning' not in captured
    assert captured['parts'] == [{'type': 'text', 'text': 'visible part'}]
    assert len(json.dumps(captured)) < 512


@pytest.mark.parametrize('value,rule,expected', [
    ('wrong', {'type': 'integer'}, {}),
    (1, {'type': 'integer'}, {'value': 1}),
    (True, {'type': 'boolean'}, {'value': True}),
    (1.5, {'type': 'number'}, {'value': 1.5}),
    (None, {'type': 'null'}, {'value': None}),
    (float('inf'), {'type': 'number'}, {}),
    (1 << 60, {'type': 'integer'}, {}),
    ('x' * 65537, {'type': 'string'}, {}),
    ('value', False, {}),
])
def test_json_capture_checks_declared_types_and_scalar_limits(value, rule, expected):
    result = project_values({'value': value}, {'type': 'object', 'properties': {'value': rule}},
                            EmissionBoundary())
    assert result == expected


def test_json_capture_limits_fields_items_depth_and_scrubs_before_truncation():
    secret = 'prefix-' + 'z' * 520
    boundary = EmissionBoundary(secrets=(secret,))
    values = {f'field{index}': 'visible' for index in range(40)}
    values['field0'] = secret
    values['field1'] = list(range(40))
    deep = {'value': 'too deep'}
    for _ in range(10):
        deep = {'nested': deep}
    values['field2'] = deep
    result = project_values(values, {'type': 'object'}, boundary)
    assert result == {}
    assert project_values({'text': secret}, {'type': 'object'}, boundary) == {'text': '[REDACTED]'}


@pytest.mark.parametrize('direction,visit', [('bad', 'visit'), ('input', ''), ('input', None)])
def test_json_capture_rejects_invalid_identity_or_direction(direction, visit):
    adapter = WorkflowObservationFactory(config(True), boundary=EmissionBoundary()).create(definition())
    with pytest.raises(ValueError):
        adapter.capture_json(visit_id=visit, direction=direction, values={}, schema={})


@pytest.mark.parametrize('mutation', ['invalid', 'non_string', 'duplicate_direction', 'different_visit',
                                     'unknown_visit', 'non_string_visit', 'bad_direction', 'bad_class', 'bad_reason', 'non_object'])
def test_schema_capture_artifact_rejects_invalid_provenance(mutation):
    from copy import deepcopy
    from hashlib import sha256
    from api_agent.workflows.journal import validate_journal
    adapter = WorkflowObservationFactory(config(True), boundary=EmissionBoundary()).create(definition())
    visits = [str(uuid4()), str(uuid4())]
    for visit in visits:
        adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    cause = str(uuid4())
    for direction in ('input', 'output'):
        adapter.capture_json(visit_id=visits[0], direction=direction, cause_id=cause,
                             values={'text': 'visible'}, schema=SCHEMA)
    payload = adapter.finalize()
    validate_journal(payload)
    changed = deepcopy(payload)
    row = changed['events'][-1]
    changes = {'invalid': {'cause_id': 'invalid'},
               'non_string': {'cause_id': []},
               'duplicate_direction': {'direction': 'input'},
               'different_visit': {'visit_id': visits[1]},
               'unknown_visit': {'visit_id': str(uuid4())},
               'non_string_visit': {'visit_id': []},
               'bad_direction': {'direction': 'sideways'},
               'bad_class': {'capture_class': 'unknown'},
               'bad_reason': {'reason': 'unknown'},
               'non_object': {'values': []}}
    row.update(changes[mutation])
    # Preserve envelope integrity so the specific admission check is exercised.
    unsigned = {key: value for key, value in changed.items() if key != 'sha256'}
    changed['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':'),
                                         allow_nan=False).encode()).hexdigest()
    expected = ('Unclassified workflow capture values' if mutation == 'bad_class' else
                'Invalid schema capture reason' if mutation in ('bad_reason', 'non_object') else
                'Invalid workflow capture correlation')
    with pytest.raises(ValueError, match=expected):
        validate_journal(changed)


def test_partial_capture_reason_preserves_visible_siblings():
    adapter = WorkflowObservationFactory(config(True), boundary=EmissionBoundary()).create(definition())
    adapter.capture_json(visit_id='visit', direction='input', values={'visible': 'answer', 'runtime': Hostile()},
                         schema={'type': 'object'})
    row = adapter.finalize()['events'][-1]
    assert row['reason'] == 'partial'
    assert row['values'] == {'visible': 'answer'}


def test_json_capture_rejects_non_json_root_and_marks_empty_capture_unavailable():
    assert project_values(Hostile(), {'type': 'object'}, EmissionBoundary()) == {}
    adapter = WorkflowObservationFactory(config(True), boundary=EmissionBoundary()).create(definition())
    adapter.capture_json(visit_id='visit', direction='output', values={'runtime': Hostile()},
                         schema={'type': 'object'})
    row = adapter.finalize()['events'][-1]
    assert row['reason'] == 'unavailable'
    assert row['values'] == {}


@pytest.mark.parametrize('values, schema, secrets', [
    ({'value': {'type': 'reasoning', 'text': 'hidden-sentinel'}},
     {'type': 'object', 'properties': {'value': {'type': 'object', 'properties': {'text': {}}}}}, ()),
    ({'value': {'public': 'sensitive-sentinel', 'auth': 'secretpart'}},
     {'type': 'object', 'properties': {'value': {'type': 'object', 'properties': {'public': {}}}}},
     ('{"public": "sensitive-sentinel", "auth": "secretpart"}',)),
])
def test_original_json_privacy_precedes_schema_projection(values, schema, secrets):
    result = project_values(values, schema, EmissionBoundary(secrets=secrets))
    assert 'sentinel' not in json.dumps(result)


@pytest.mark.parametrize('rule,value,expected', [
    ({'type': ['string', 'null']}, 'visible', {'value': 'visible'}),
    ({'type': ['string', 'null']}, None, {'value': None}),
    ({'type': ['string', 'null']}, 1, {}),
    ({'allOf': [{'type': 'object', 'properties': {'allowed': {}}}, {'type': 'object'}]},
     {'allowed': 'visible', 'extra': 'excluded'}, {'value': {'allowed': 'visible'}}),
    ({'anyOf': [{'type': 'string'}, {'type': 'null'}]}, 'visible', {'value': 'visible'}),
    ({'oneOf': [{'type': 'string'}, {'type': 'null'}]}, None, {'value': None}),
    ({'oneOf': [{}, {}]}, 'visible', {}),
    ({'anyOf': [{'type': 'integer'}]}, 'visible', {}),
    ({'$ref': 'https://example.test/schema'}, 'visible', {}),
    ({'$ref': '#/$defs/missing'}, 'visible', {}),
    ({'if': {}}, 'visible', {}),
])
def test_schema_compositions_are_conservative(rule, value, expected):
    schema = {'type': 'object', 'properties': {'value': rule}}
    assert project_values({'value': value}, schema, EmissionBoundary()) == expected


def test_local_schema_reference_retains_its_restrictions():
    schema = {'$defs': {'visible': {'type': 'object', 'properties': {'text': {}}}},
              'type': 'object', 'properties': {'value': {'$ref': '#/$defs/visible'}}}
    assert project_values({'value': {'text': 'visible', 'extra': 'excluded'}}, schema,
                          EmissionBoundary()) == {'value': {'text': 'visible'}}


@pytest.mark.parametrize('value', [float('inf'), 1 << 60, {1: 'non-string key'}, 'x' * 65537])
def test_incomplete_original_json_fails_closed_for_entire_capture(value):
    secret = json.dumps({'public': 'sensitive-sentinel', 'other': value})
    result = project_values({'public': 'sensitive-sentinel', 'other': value},
                            {'type': 'object', 'properties': {'public': {}}},
                            EmissionBoundary(secrets=(secret,)))
    assert result == {}


def test_schema_capture_preserves_safe_token_counts_without_credential_fields():
    result = project_values({'usage': {'input_tokens': 2, 'reasoning_tokens': 3},
                             'access_token': 'excluded'}, {'type': 'object'}, EmissionBoundary())
    assert result == {'usage': {'input_tokens': 2, 'reasoning_tokens': 3}}


def test_cyclic_original_json_fails_closed():
    cycle = {}
    cycle['loop'] = cycle
    assert project_values({'visible': 'answer', 'cycle': cycle}, {'type': 'object'}, EmissionBoundary()) == {}


@pytest.mark.parametrize('log,trace', [(True, False), (False, True), (True, True)])
def test_combined_live_authorities_recognize_original_composite(log, trace):
    original = {'public': 'composite-sensitive-sentinel', 'other': 'piece-two'}
    source = EmissionBoundary(secrets=('piece-two',))
    trace_boundary = EmissionBoundary(ContentPolicy(trace), secrets=(json.dumps(original),))
    span = Mock()
    runtime = SimpleNamespace(boundary=trace_boundary, begin=Mock(return_value=span),
                              is_model_identity=lambda value: False)
    rows = []
    adapter = WorkflowObservationFactory(config(log, trace), boundary=source,
                                         sink=rows.append, trace=runtime).create(definition())
    adapter.boundary.register_secret('refresh-sentinel')
    assert 'refresh-sentinel' in source._secrets
    assert 'refresh-sentinel' in trace_boundary._secrets
    adapter.observe(dict(event='scope_start', scope_id='visit', owner='Make', kind='node'))
    adapter.capture_json(visit_id='visit', direction='input', values={'value': original}, schema={'type': 'object'})
    assert 'composite-sensitive-sentinel' not in repr(rows)
    assert 'composite-sensitive-sentinel' not in repr(span.event.call_args)
    if trace:
        assert span.event.call_args.args[0]['capture_class'] == 'schema_user_io'
        assert 'reason' in span.event.call_args.args[0]


def test_state_annotation_schema_restricts_nested_typeddict_and_accepts_json_containers():
    from typing import Annotated, Any, TypedDict
    from api_agent.workflows.capture import annotation_schema
    class Nested(TypedDict):
        allowed: str
    class State(TypedDict):
        payload: Nested
        parts: list[dict[str, Any]]
        optional: str | None
        coordinates: tuple[int, str]
        numbers: tuple[int, ...]
        tagged: Annotated[str, 'metadata']
        runtime: object
    schema = annotation_schema(State)
    result = project_values({'payload': {'allowed': 'visible', 'extra': 'excluded'},
                             'parts': [{'text': 'visible'}], 'optional': None,
                             'coordinates': (1, 'label'), 'numbers': (1, 2),
                             'tagged': 'visible', 'runtime': Hostile()}, schema, EmissionBoundary())
    assert result == {'payload': {'allowed': 'visible'}, 'parts': [{'text': 'visible'}],
                      'optional': None, 'coordinates': [1, 'label'], 'numbers': [1, 2], 'tagged': 'visible'}


@pytest.mark.parametrize('composition', ['anyOf', 'oneOf'])
def test_schema_composition_chooses_valid_discriminated_object(composition):
    branches = [{'type': 'object', 'properties': {'kind': {'const': 'first'}, 'first': {}},
                 'required': ['kind', 'first']},
                {'type': 'object', 'properties': {'kind': {'const': 'second'}, 'text': {}},
                 'required': ['kind', 'text']}]
    schema = {'type': 'object', 'properties': {'value': {composition: branches}}}
    result = project_values({'value': {'kind': 'second', 'text': 'visible', 'extra': 'excluded'}},
                            schema, EmissionBoundary())
    assert result == {'value': {'kind': 'second', 'text': 'visible'}}


def test_allof_combines_sibling_property_declarations():
    schema = {'type': 'object', 'properties': {'value': {'allOf': [
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {'type': 'object', 'properties': {'b': {'type': 'string'}}}]}}}
    report = set()
    result = project_values({'value': {'a': 'first', 'b': 'second', 'extra': 'excluded'}},
                            schema, EmissionBoundary(), report=report)
    assert result == {'value': {'a': 'first', 'b': 'second'}}
    assert not report


@pytest.mark.parametrize('values,schema,expected', [
    ({'value': 'visible'}, False, {}),
    ({'value': 'visible'}, {'type': 'object', 'properties': {'value': True}}, {'value': 'visible'}),
    ({'bad key': 'excluded'}, {'type': 'object'}, {}),
    ({'value': 'visible'}, {'type': 'object', 'properties': {'value': {'const': 'different'}}}, {}),
    ({'value': 'visible'}, {'type': 'object', 'properties': {'value': {'enum': ['different']}}}, {}),
    ({'value': []}, {'type': 'object', 'properties': {'value': {'allOf': [False]}}}, {}),
    ({'value': {}}, {'type': 'object', 'properties': {'value': {'allOf': [True]}}}, {}),
    ({'value': 'visible'}, {'type': 'object', 'properties': {'value': {'anyOf': [{'$ref': 'https://example.test/never-fetch'}]}}}, {}),
    ({'value': 'visible'}, {'type': 'object', 'properties': {'value': {'anyOf': [{'if': {}}]}}}, {}),
    ({'value': [1, 'wrong']}, {'type': 'object', 'properties': {'value': {'type': 'array', 'items': {'type': 'integer'}}}}, {'value': [1]}),
    ({'value': {}}, {'type': 'object', 'properties': {'value': {'allOf': [{'type': 'object'}]}}}, {'value': {}}),
])
def test_capture_conservative_schema_omissions(values, schema, expected):
    assert project_values(values, schema, EmissionBoundary()) == expected


@pytest.mark.parametrize('case', ['depth', 'nodes', 'key', 'items'])
def test_original_json_limits_fail_closed_without_partial_credential_context(case):
    deep = {'visible': 'answer'}
    for _ in range(10):
        deep = {'nested': deep}
    cases = {'depth': deep,
             'nodes': {'outer': {str(i): {str(j): [None, None] for j in range(32)} for i in range(32)}},
             'key': {'x' * 129: 'answer'}, 'items': {'parts': [None] * 17}}
    report = set()
    assert project_values(cases[case], {'type': 'object'}, EmissionBoundary(), report=report) == {}
    assert report == {'truncated'}


def test_recursive_schema_reference_is_bounded_and_reported():
    schema = {'type': 'object', 'properties': {'value': {'$ref': '#/$defs/recursive'}},
              '$defs': {'recursive': {'$ref': '#/$defs/recursive'}}}
    report = set()
    assert project_values({'value': 'visible'}, schema, EmissionBoundary(), report=report) == {}
    assert report == {'truncated'}


@pytest.mark.parametrize('annotation,expected', [
    (list, {'type': 'array', 'items': {}}), (tuple, {'type': 'array'}),
    (dict, {'type': 'object', 'additionalProperties': {}}), (dict[int, str], False),
])
def test_annotation_schema_covers_bare_containers_and_rejects_non_json_keys(annotation, expected):
    from api_agent.workflows.capture import annotation_schema
    assert annotation_schema(annotation) == expected


def test_annotation_schema_handles_optional_and_literal_fields_and_bounds_recursion():
    from typing import Literal, NotRequired, Required
    from api_agent.workflows.capture import annotation_schema
    assert annotation_schema(Required[str]) == {'type': 'string'}
    assert annotation_schema(NotRequired[str]) == {'type': 'string'}
    assert annotation_schema(Literal['first', 'second']) == {'enum': ['first', 'second']}
    assert annotation_schema(str, depth=9) is False


def test_allof_merges_declared_fields_with_additional_property_constraints():
    schema = {'type': 'object', 'properties': {'value': {'allOf': [
        {'type': 'object', 'properties': {'a': {'type': 'string'}},
         'additionalProperties': {'type': 'string'}},
        {'type': 'object', 'properties': {'b': {'type': 'string'}}}]}}}
    assert project_values({'value': {'a': 'first', 'b': 'second'}}, schema,
                          EmissionBoundary()) == {'value': {'a': 'first', 'b': 'second'}}


def test_nested_boundary_authorities_scrub_original_container_once():
    from api_agent.workflows.journal import _JournalBoundary
    original = {'public': 'nested-sensitive-sentinel', 'other': 'piece-two'}
    first = EmissionBoundary(secrets=('piece-two',))
    second = EmissionBoundary(secrets=(json.dumps(original),))
    nested = _JournalBoundary(ContentPolicy(True), first)
    combined = _JournalBoundary(ContentPolicy(True), nested, second)
    combined.register_secret('new-refresh-sentinel')
    assert combined.content_export({'value': original}) == {}
    assert 'new-refresh-sentinel' in first._secrets
    assert 'new-refresh-sentinel' in second._secrets


def test_trace_json_capture_without_visit_does_not_create_span():
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    adapter = WorkflowObservationFactory(config(trace=True), boundary=EmissionBoundary(), trace=trace).create(definition())
    adapter.capture_json(visit_id='outside', direction='input', values={'text': 'visible'}, schema=SCHEMA)
    trace.begin.assert_not_called()


@pytest.mark.parametrize('failure', [RuntimeError(), asyncio.CancelledError()])
def test_trace_json_capture_failure_does_not_break_admission(failure):
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    trace.begin.return_value.event.side_effect = failure
    adapter = WorkflowObservationFactory(config(trace=True), boundary=EmissionBoundary(), trace=trace).create(definition())
    adapter.observe(dict(event='scope_start', scope_id='visit', owner='Make', kind='node'))
    event = adapter.capture_json(visit_id='visit', direction='input', values={'text': 'visible'}, schema=SCHEMA)
    assert event['reason'] == 'withheld'


@pytest.mark.parametrize('item_contract', [
    {'items': {'type': 'object', 'properties': {'allowed': {'type': 'string'}}}},
    {'prefixItems': [{'type': 'object', 'properties': {'allowed': {'type': 'string'}}}], 'items': False},
])
def test_allof_array_projects_item_fields_before_capture(item_contract):
    schema = {'type': 'object', 'properties': {'value': {'allOf': [
        {'type': 'array', **item_contract}]}}}
    report = set()
    result = project_values({'value': [{'allowed': 'yes', 'undeclared': 'leak-sentinel'}]},
                            schema, EmissionBoundary(), report=report)
    assert result == {'value': [{'allowed': 'yes'}]}
    assert not report


def test_allof_array_combines_sibling_item_declarations():
    schema = {'type': 'object', 'properties': {'value': {'allOf': [
        {'type': 'array', 'items': {'type': 'object', 'properties': {'a': {'type': 'string'}}}},
        {'type': 'array', 'items': {'type': 'object', 'properties': {'b': {'type': 'string'}}}}]}}}
    assert project_values({'value': [{'a': 'first', 'b': 'second', 'undeclared': 'leak-sentinel'}]},
                          schema, EmissionBoundary()) == {'value': [{'a': 'first', 'b': 'second'}]}


def test_allof_array_with_unsupported_compound_clause_reports_omission():
    schema = {'type': 'object', 'properties': {'value': {'allOf': [
        {'anyOf': [{'type': 'array'}]}]}}}
    report = set()
    assert project_values({'value': []}, schema, EmissionBoundary(), report=report) == {}
    assert report == {'unsupported'}


def test_overlapping_anyof_branch_subset_is_marked_partial():
    from api_agent.workflows.capture import capture_reason
    schema = {'type': 'object', 'properties': {'value': {'anyOf': [
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {'type': 'object', 'properties': {'b': {'type': 'string'}}}]}}}
    report = set()
    result = project_values({'value': {'a': 'first', 'b': 'second'}}, schema,
                            EmissionBoundary(), report=report)
    assert result == {'value': {'a': 'first'}}
    assert capture_reason(result, report) == 'partial'


def test_redaction_expansion_is_omitted_under_encoded_capture_bound():
    boundary = EmissionBoundary(secrets=('qx',))
    report = set()
    value = {'text': 'qx|' * 20000}
    assert len(json.dumps(value).encode()) < 65536
    assert project_values(value, {'type': 'object'}, boundary, report=report) == {}
    assert report == {'truncated'}


def test_optional_preview_reports_clipping_after_credential_projection():
    boundary = EmissionBoundary(secrets=('synthetic-secret',))
    report = set()
    values = {'parts': [{'text': 'synthetic-secret ' + 'x' * 600}], 'count': 2}
    result = project_values(values, {'type': 'object'}, boundary, limit=32, report=report)
    assert result == {'parts': [{'text': '[REDACTED] ' + 'x' * 21}], 'count': 2}
    assert report == {'truncated'}


def test_canonical_utf8_capture_admission_at_exact_boundary_and_escaping():
    from api_agent.testing.payloads import encoded
    boundary = EmissionBoundary(secrets=('synthetic-sensitive',))
    value = {'text': '🧪' * 6000 + '\n"\\ synthetic-sensitive'}
    report = set()
    safe = project_values(value, {'type': 'object'}, boundary, report=report)
    assert safe == {'text': '🧪' * 6000 + '\n"\\ [REDACTED]'}
    assert report == set()
    overhead = len(encoded({'text': ''}))
    capacity = 65536 - overhead
    text = '🧪' * (capacity // 4) + 'x' * (capacity % 4)
    value = {'text': text}
    assert len(encoded(value)) == 65536
    assert project_values(value, {'type': 'object'}, boundary) == value
    report = set()
    assert project_values({'text': text + '\n'}, {'type': 'object'}, boundary, report=report) == {}
    assert report == {'truncated'}


@pytest.mark.parametrize('value', [{'text': chr(0xD800)}, {chr(0xDFFF): 'ordinary'}], ids=['text', 'key'])
def test_non_utf8_capture_is_explicitly_unavailable(value):
    from api_agent.workflows.capture import capture_reason
    report = set()
    safe = project_values(value, {'type': 'object'}, EmissionBoundary(), report=report)
    assert safe == {}
    assert report == {'unsupported'}
    assert capture_reason(safe, report) == 'unavailable'


@pytest.mark.parametrize('value', [{'text': chr(0xD800)}, {chr(0xDFFF): 'ordinary'}], ids=['text', 'key'])
def test_non_utf8_final_projection_is_explicitly_unavailable(value):
    from api_agent.workflows.capture import capture_reason
    class CorruptProjection:
        def __init__(self):
            self.count = 0
        def content_export(self, payload):
            self.count += 1
            return payload if self.count == 1 else {'values': value}
    boundary = CorruptProjection()
    report = set()
    safe = project_values({'text': 'ordinary'}, {'type': 'object'}, boundary, report=report)
    assert boundary.count == 2
    assert safe == {}
    assert report == {'unsupported'}
    assert capture_reason(safe, report) == 'unavailable'


def test_parsed_deterministic_result_survives_non_utf8_optional_capture(tmp_path):
    from api_agent.workflows.definition import load_definition, load_native_catalog
    from api_agent.workflows.execution import Workflow, NodeFunction
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    from test_workflow_definition import make_graph
    entry = make_graph(tmp_path)
    (tmp_path / 'native.py').write_text('def inspect(inputs, context):\n    return {"text": chr(0xD800)}\n')
    (tmp_path / 'schemas/output.json').write_text(json.dumps({'type': 'object', 'properties': {'text': {'type': 'string'}}}))
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    native = load_native_catalog(definition, tmp_path / 'native.py')
    journal = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True))
    workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'])}, services={'journal': journal})
    result = asyncio.run(workflow.arun({}))
    assert result['inspect'] == {'text': chr(0xD800)}
    retained = validate_journal(journal.finalize())
    output = next(row for row in retained['events'] if row['event'] == 'capture' and row['direction'] == 'output')
    assert output['reason'] == 'unavailable' and output['values'] == {}
    assert chr(0xD800) not in json.dumps(retained, ensure_ascii=False)


from test_workflow_stateful import fixture_graph


def test_parsed_stateful_result_survives_non_utf8_optional_capture(fixture_graph):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace('    visits: int', '    visits: int\n    text: str').replace(
        "return {'pending': True}", "return {'pending': True, 'text': chr(0xD800)}"))
    definition = load_stateful_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True))
    from langgraph.graph.message import add_messages
    workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition), reducers={'messages': add_messages}, journal=journal)
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0, 'text': 'ordinary'},
                                      thread_id='invalid-encoding'))
    assert result['text'] == chr(0xD800)
    assert result['pending'] is True and result['visits'] == 1
    retained = validate_journal(journal.finalize())
    finish = next(row['scope_id'] for row in retained['events'] if row['event'] == 'scope_start' and row['owner'] == 'finish')
    output = next(row for row in retained['events'] if row['event'] == 'capture' and row['direction'] == 'output' and row['visit_id'] == finish)
    assert output['reason'] == 'unavailable' and output['values'] == {}
    assert chr(0xD800) not in json.dumps(retained, ensure_ascii=False)
