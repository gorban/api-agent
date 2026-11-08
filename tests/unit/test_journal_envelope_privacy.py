"""Captured carriers remain safe inside their final immutable journal envelope."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.journal import validate_journal
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import config, definition


def schema_tool_history():
    # The retained shape is an ordinary introspection tool result: nested type
    # references inside field arguments, serialized in a canonical tool carrier.
    reference = {'kind': 'SCALAR', 'name': 'String', 'ofType': None}
    result = {'result': {'__schema': {'queryType': {'fields': [
        {'name': 'record', 'args': [], 'type': reference},
        {'name': 'records', 'args': [{'name': 'filter', 'type': {
            'kind': 'NON_NULL', 'name': None, 'ofType': reference}}], 'type': reference},
    ], 'filteredFromFieldCount': 12}}}}
    return {'llm_request': {'messages': [
        {'role': 'system', 'content': ['Review the schema']},
        {'role': 'user', 'content': ['Read matching records']},
        {'role': 'assistant', 'calls': [{'id': 'schema-call', 'name': 'inspect_schema',
                                         'arguments': '{}'}]},
        {'role': 'tool', 'results': [{'call_id': 'schema-call',
                                     'content': [json.dumps(result)]}]},
    ]}}


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_nested_tool_carrier_is_safe_at_final_journal_depth_without_reopening_withheld_log(log, trace):
    boundary = EmissionBoundary()
    span = Mock()
    runtime = SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace)),
                              begin=Mock(return_value=span), is_model_identity=lambda value: False)
    emitted = []
    adapter = WorkflowObservationFactory(config(log, trace), boundary=boundary,
        trace=runtime, sink=emitted.append).create(definition())
    graph, visit, cause = (str(uuid4()) for _ in range(3))
    adapter.observe(dict(event='scope_start', scope_id=graph, owner='GRAPH', kind='graph'))
    adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, owner='Make', kind='node'))
    values = schema_tool_history()
    # At the original source depth no credential transformation is necessary.
    assert EmissionBoundary(ContentPolicy(True)).content_export(values) == values
    adapter.capture_json(visit_id=visit, direction='input', cause_id=cause,
        capture_source='fallback', values=values, schema={'type': 'object'})
    adapter.record_call(call_id=cause, visit_id=visit, usage={'input_tokens': 7, 'output_tokens': 2})
    adapter.observe(dict(event='scope_end', scope_id=visit, owner='Make', kind='node', outcome='passed'))
    adapter.observe(dict(event='scope_end', scope_id=graph, owner='GRAPH', kind='graph', outcome='passed'))
    packet = adapter.finalize()
    validate_journal(packet)
    # Keep the consumer's independent whole-envelope admission unchanged.
    assert EmissionBoundary(ContentPolicy(True)).content_export(packet) == packet
    stored = next(row for row in packet['events'] if row['event'] == 'capture')
    live = next(row for row in emitted if row['event'] == 'capture')
    assert stored['reason'] == live['reason'] == 'withheld'
    assert 'values' not in stored and 'values' not in live
    call = next(row for row in packet['events'] if row['event'] == 'call')
    assert call['call_id'] == cause and call['usage'] == {'input_tokens': 7, 'output_tokens': 2}
    assert packet['turn_id'] == stored['turn_id'] and stored['visit_id'] == visit
    node_events = [call.args[0] for call in span.event.call_args_list
                   if 'values' in call.args[0]]
    assert node_events and (node_events[0]['values'] == {}) is (not trace)


def test_shallow_safe_capture_keeps_original_values_and_reason():
    from api_agent.workflows.journal import WorkflowJournal
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True))
    visit = str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    values = {'messages': [{'role': 'user', 'content': 'ordinary request'}],
              'result': json.dumps({'answer': 'ordinary result'})}
    journal.capture_json(visit_id=visit, direction='input', values=values, schema={'type': 'object'})
    packet = journal.finalize()
    captured = packet['events'][-1]
    assert captured['values'] == values and captured['reason'] == 'captured'
    assert EmissionBoundary(ContentPolicy(True)).content_export(packet) == packet


def test_registered_whole_row_is_not_fragmented_by_journal_envelope():
    from api_agent.workflows.journal import WorkflowJournal
    row = {'event': 'node', 'scope_id': str(uuid4()), 'node': 'Make'}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(row),))
    journal = WorkflowJournal(definition(), boundary=boundary, log_content=True,
                              content_policy=ContentPolicy(True))
    emitted = []
    journal.subscribe(emitted.append)
    journal.observe(row)
    packet = journal.finalize()
    assert packet['events'] == [] and emitted == []
    assert packet['gap'] == 'tool_evidence_unavailable'
    validate_journal(packet)


def test_plain_serialized_capture_cannot_reopen_withheld_journal_values_in_live_sink():
    emitted = []
    adapter = WorkflowObservationFactory(config(log=True), boundary=EmissionBoundary(),
                                         sink=emitted.append).create(definition())
    visit = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    nested = 'ordinary text'
    for _ in range(17):
        nested = {'nested': nested}
    adapter.capture(visit_id=visit, direction='input', values={'text': json.dumps(nested)},
        schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}, plain_fields=['text'])
    packet = adapter.finalize()
    stored = next(row for row in packet['events'] if row['event'] == 'capture')
    live = next(row for row in emitted if row['event'] == 'capture')
    assert stored['reason'] == live['reason'] == 'withheld'
    assert 'values' not in stored and 'values' not in live
    assert EmissionBoundary(ContentPolicy(True)).content_export(packet) == packet
