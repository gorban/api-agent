"""Declared routing and controller decisions remain data only."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from api_agent.workflows.predicates import (
    PredicateError, component_controller, component_decision, parse_predicate,
)
from api_agent.workflows.contrived import validate_format, guard
from api_agent.workflows.contrived import build
from api_agent.workflows.execution import NodeFunction, Workflow, diagram_layout
from api_agent.workflows.layout import Layout, Step
from api_agent.workflows.predicates import predicate_fields
from api_agent.workflows.predicates import edge_label


FIELDS = {'first_draft': {'route': {'type': 'string'}, 'ready': {'type': 'boolean'}}}


def test_bounded_predicate_evaluates_declared_route():
    predicate = parse_predicate('first_draft["route"] == "skip"', FIELDS)

    assert predicate.evaluate({'first_draft': {'route': 'skip'}}) is True
    assert predicate.evaluate({'first_draft': {'route': 'next'}}) is False


def test_missing_producer_cannot_satisfy_inequality():
    predicate = parse_predicate('first_draft["route"] != "skip"', FIELDS)

    with pytest.raises(PredicateError, match='was not produced'):
        predicate.evaluate({})
    assert parse_predicate('first_draft["route"] == "skip" or first_draft["ready"] == True', FIELDS).evaluate(
        {'first_draft': {'route': 'skip'}}) is True


def test_present_nullable_field_compares_null_but_absent_field_fails_closed():
    fields = {'draft': {'result': {'type': ('string', 'null')}}}
    predicate = parse_predicate('draft["result"] == None', fields)

    assert predicate.evaluate({'draft': {'result': None}}) is True
    assert predicate.evaluate({'draft': {'result': 'ready'}}) is False
    with pytest.raises(PredicateError, match='was not produced'):
        predicate.evaluate({'draft': {}})


@pytest.mark.parametrize('kinds', [[], ['string', 'object'], ('number', 'array'), ['unknown']])
def test_predicate_rejects_nonscalar_or_empty_type_unions(kinds):
    with pytest.raises(PredicateError, match='declared scalar'):
        parse_predicate('draft["result"] == None', {'draft': {'result': {'type': kinds}}})


def test_numeric_predicates_compare_ints_and_floats_without_bool_coercion():
    fields = {'draft': {'score': {'type': 'number'}, 'ready': {'type': 'boolean'}}}

    assert parse_predicate('draft["score"] >= 0.5', fields).evaluate({'draft': {'score': 1}}) is True
    assert parse_predicate('draft["score"] < 1', fields).evaluate({'draft': {'score': 0.5}}) is True
    assert parse_predicate('draft["score"] == 1.0', fields).evaluate({'draft': {'score': 1}}) is True
    assert parse_predicate('draft["score"] != 1.0', fields).evaluate({'draft': {'score': 1}}) is False
    assert parse_predicate('draft["ready"] == 1.0', fields).evaluate({'draft': {'ready': True}}) is False


def _route_start(inputs, context):
    return {'flag': inputs['route']}


def _route_optional(inputs, context):
    return {'flag': 'other'}


def _route_final(inputs, context):
    return {}


def _branch_workflow(conditions=None):
    source = Path(__file__).resolve()
    steps = tuple(Step(name, 'node', None, source, 1) for name in ('route_start', 'route_optional', 'route_final'))
    empty = {'type': 'object', 'properties': {}}
    output = {'type': 'object', 'properties': {'flag': {'type': 'string'}}, 'required': ['flag']}
    tables = {'Components': [], 'Functions': [
        {'ID': 'route_start', 'Registry': 'start', 'Input schema': {'type': 'object', 'properties': {'route': {'type': 'string'}}, 'required': ['route']}, 'Output schema': output, 'line': 1},
        {'ID': 'route_optional', 'Registry': 'optional', 'Input schema': empty, 'Output schema': output, 'line': 1},
        {'ID': 'route_final', 'Registry': 'final', 'Input schema': empty, 'Output schema': empty, 'line': 1},
    ], 'Bindings': [{'Node': 'route_start', 'Input': 'route', 'From': 'START', 'Field': 'route', 'Default': '-', 'line': 1}],
        'Edges': [{'From': 'route_start', 'To': 'route_final', 'When': 'route_start["flag"] == "skip"', 'line': 1},
                  {'From': 'route_start', 'To': 'route_optional', 'When': 'otherwise', 'line': 1},
                  {'From': 'route_final', 'To': 'END', 'When': 'route_optional["flag"] != "x"', 'line': 1}],
        'Prompts': [], 'Timeouts': []}
    layout = Layout(source, steps, tables, ())
    return Workflow(layout, {'start': NodeFunction(_route_start), 'optional': NodeFunction(_route_optional),
                             'final': NodeFunction(_route_final)}, conditions=conditions)


def test_compiled_branch_fails_if_predicate_producer_was_skipped():
    workflow = _branch_workflow()

    with pytest.raises(PredicateError, match='was not produced'):
        asyncio.run(workflow.arun({'route': 'skip'}))
    values = asyncio.run(workflow.arun({'route': 'include'}))
    assert values['route_optional'] == {'flag': 'other'}


def test_registered_callable_cannot_override_declared_expression():
    called = []
    expression = 'route_start["flag"] == "skip"'

    def conflicting(values):
        called.append(values)
        return False

    workflow = _branch_workflow({expression: conflicting})

    with pytest.raises(PredicateError, match='was not produced'):
        asyncio.run(workflow.arun({'route': 'skip'}))
    assert called == []


def _nullable_route(inputs, context):
    return {'flag': inputs['route']}


def _numeric_route(inputs, context):
    return {'score': inputs['score']}


def test_compiled_branch_distinguishes_present_null_from_other_scalar():
    source = Path(__file__).resolve()
    nullable = {'type': 'object', 'properties': {'flag': {'type': ['string', 'null']}}, 'required': ['flag']}
    route_input = {'type': 'object', 'properties': {'route': {'type': ['string', 'null']}}, 'required': ['route']}
    empty = {'type': 'object', 'properties': {}}
    expression = 'route_start["flag"] == None'
    layout = Layout(source, (Step('route_start', 'node', None, source, 1),
                             Step('route_final', 'node', None, source, 1)),
                    {'Components': [], 'Functions': [
                        {'ID': 'route_start', 'Registry': 'start', 'Input schema': route_input,
                         'Output schema': nullable, 'line': 1},
                        {'ID': 'route_final', 'Registry': 'final', 'Input schema': empty,
                         'Output schema': empty, 'line': 1}],
                     'Bindings': [{'Node': 'route_start', 'Input': 'route', 'From': 'START',
                                   'Field': 'route', 'Default': '-', 'line': 1}],
                     'Edges': [{'From': 'route_start', 'To': 'END', 'When': expression, 'line': 1},
                               {'From': 'route_start', 'To': 'route_final', 'When': 'otherwise', 'line': 1}],
                     'Prompts': [], 'Timeouts': []}, ())
    events = []
    workflow = Workflow(layout, {'start': NodeFunction(_nullable_route),
                                 'final': NodeFunction(_route_final)}, services={'observe': events.append})

    null_values = asyncio.run(workflow.arun({'route': None}))
    assert null_values['route_start'] == {'flag': None}
    assert 'route_final' not in null_values
    assert any(event['event'] == 'edge' and event.get('condition') == expression
               and event['target'] == 'END' for event in events)

    other_values = asyncio.run(workflow.arun({'route': 'other'}))
    assert other_values['route_final'] == {}


def test_compiled_numeric_route_uses_python_int_float_comparison():
    source = Path(__file__).resolve()
    score_schema = {'type': 'object', 'properties': {'score': {'type': 'number'}}, 'required': ['score']}
    empty = {'type': 'object', 'properties': {}}
    expression = 'route_start["score"] >= 0.5'
    layout = Layout(source, (Step('route_start', 'node', None, source, 1),
                             Step('route_final', 'node', None, source, 1)),
                    {'Components': [], 'Functions': [
                        {'ID': 'route_start', 'Registry': 'start', 'Input schema': score_schema,
                         'Output schema': score_schema, 'line': 1},
                        {'ID': 'route_final', 'Registry': 'final', 'Input schema': empty,
                         'Output schema': empty, 'line': 1}],
                     'Bindings': [{'Node': 'route_start', 'Input': 'score', 'From': 'START',
                                   'Field': 'score', 'Default': '-', 'line': 1}],
                     'Edges': [{'From': 'route_start', 'To': 'END', 'When': expression, 'line': 1},
                               {'From': 'route_start', 'To': 'route_final', 'When': 'otherwise', 'line': 1}],
                     'Prompts': [], 'Timeouts': []}, ())
    events = []
    workflow = Workflow(layout, {'start': NodeFunction(_numeric_route),
                                 'final': NodeFunction(_route_final)}, services={'observe': events.append})

    integer_values = asyncio.run(workflow.arun({'score': 1}))
    assert 'route_final' not in integer_values
    assert any(event['event'] == 'edge' and event.get('condition') == expression
               and event['target'] == 'END' for event in events)
    fractional_values = asyncio.run(workflow.arun({'score': 0.25}))
    assert fractional_values['route_final'] == {}


def test_component_route_can_read_its_executed_descendant_output():
    source = Path(__file__).resolve()
    layout = Layout(source, (Step('first', 'component', None, source, 1),
                             Step('first_draft', 'node', 'first', source, 1)),
                    {'Functions': [{'ID': 'first_draft', 'Output schema': {
                        'type': 'object', 'properties': {'route': {'type': 'string'}}}}], 'Bindings': []}, ())

    predicate = parse_predicate('first_draft["route"] == "skip"', predicate_fields(layout, 'first'))
    assert predicate.evaluate({'first_draft': {'route': 'skip'}}) is True


@pytest.mark.parametrize('expression,diagnostic', [
    ('x' * 257, '256 characters'),
    (' and '.join('x == 1' for _ in range(9)), '40 syntax nodes'),
    ('not ' * 9 + 'first_draft["ready"] == True', 'nesting exceeds 8'),
])
def test_predicate_bounding_rejects_excessive_declarations(expression, diagnostic):
    with pytest.raises(PredicateError, match=diagnostic):
        parse_predicate(expression, FIELDS)


@pytest.mark.parametrize('expression,values,expected', [
    ('not first_draft["ready"] == True', {'ready': False}, True),
    ('first_draft["route"] < "skip"', {'route': 'next'}, True),
    ('first_draft["route"] <= "next"', {'route': 'next'}, True),
    ('first_draft["route"] > "next"', {'route': 'skip'}, True),
    ('first_draft["route"] >= "skip"', {'route': 'skip'}, True),
    ('first_draft["route"] < 2', {'route': 'skip'}, False),
    ('first_draft["ready"] < True', {'ready': False}, False),
    ('first_draft["ready"] != 1', {'ready': True}, True),
    ('first_draft["route"] == "skip" and first_draft["ready"] == True', {'route': 'skip', 'ready': True}, True),
])
def test_admitted_comparison_and_boolean_decisions(expression, values, expected):
    predicate = parse_predicate(expression, FIELDS)
    assert predicate.evaluate({'first_draft': values}) is expected
    assert edge_label(expression) == predicate.text


@pytest.mark.parametrize('expression', [
    'first_draft.route == "skip"',
    '__import__("os") == "x"',
    'first_draft["missing"] == "skip"',
    'first_draft["route"] = "skip"',
    'first_draft["route"] == "skip" or True',
])
def test_predicate_rejects_unadmitted_expression(expression):
    with pytest.raises(PredicateError):
        parse_predicate(expression, FIELDS)


def test_controller_has_bounded_retry_and_terminal_exhaustion():
    controller = component_controller({'Guard': 'review', 'Max passes': '2'})

    assert component_decision(controller, True, 1) == 'accepted'
    assert component_decision(controller, False, 1) == 'retry'
    assert component_decision(controller, False, 2) == 'exhausted'


def test_guarded_single_pass_diagram_has_terminal_rejection_without_retry():
    source = Path(__file__).resolve()
    layout = Layout(source, (Step('review', 'component', None, source, 1),
                             Step('review_guard', 'node', 'review', source, 1)),
                    {'Components': [{'ID': 'review', 'Guard': 'review_guard', 'Max passes': '1'}],
                     'Edges': [], 'Prompts': []}, ())

    diagram = diagram_layout(layout)

    assert 'review_DECISION -->|rejected, attempt = 1| review_EXHAUSTED' in diagram
    assert 'review_DECISION -->|rejected, attempt <' not in diagram


def test_expected_format_mismatch_rejects_through_guard():
    draft = {'original_input': 'request', 'final_output': 'wrong', 'ready': True, 'route': 'skip'}
    formatted = validate_format({'draft': draft, 'user_input': 'request'}, None)

    assert formatted == {'valid': False}
    result = guard({'draft': draft, 'format': formatted}, None)
    assert result['accepted'] is False
    assert 'final_output' in result['feedback']['reason']


def test_valid_format_and_ready_draft_are_accepted():
    draft = {'original_input': 'request', 'final_output': 'echo request', 'ready': True, 'route': 'skip'}
    formatted = validate_format({'draft': draft, 'user_input': 'request'}, None)

    assert formatted == {'valid': True}
    assert guard({'draft': draft, 'format': formatted}, None)['accepted'] is True


def test_compiled_format_rejection_retries_with_feedback_then_accepts():
    requests, events = [], []

    async def llm(request):
        metadata = json.loads(request.payload.messages[-1].content[0].text)
        requests.append(metadata)
        original = request.payload.messages[1].content[0].text
        draft = {'original_input': original, 'route': 'skip', 'ready': True,
                 'final_output': 'wrong' if metadata['attempt'] == 1 else f'echo {original}'}
        return SimpleNamespace(outcome='completed', payload=SimpleNamespace(
            content=(SimpleNamespace(text=json.dumps(draft)),)))

    workflow = build(Path(__file__).resolve().parents[2], llm=llm,
                     selection='synthetic', trial='format-repair', observe=events.append)
    values = asyncio.run(workflow.arun({'user_input': 'original request'}))

    assert values['first_format'] == {'valid': True}
    assert [event['accepted'] for event in events if event['event'] == 'guard'] == [False, True]
    assert [request['attempt'] for request in requests] == [1, 2]
    assert requests[1]['previous']['final_output'] == 'wrong'
    assert 'final_output' in requests[1]['feedback']['reason']
    assert requests[1]['previous']['route'] == 'skip'
    assert [event['condition'] for event in events if event['event'] == 'edge' and event['source'] == 'first'] == [
        'first_draft["route"] == "skip"']
    assert 'first -->|first_draft["route"] == "skip"| GRAPH_END' in workflow.diagram()
    starts = [event for event in events if event['event'] == 'scope_start']
    graph_scope = next(event for event in starts if event['owner'] == 'GRAPH')
    component_scope = next(event for event in starts if event['owner'] == 'first')
    draft_scopes = [event for event in starts if event['owner'] == 'first_draft']
    assert (graph_scope['declared_timeout'], graph_scope['timeout_key']) == (600.0, 'WORKFLOW_GRAPH_TIMEOUT_SECONDS')
    assert (component_scope['declared_timeout'], component_scope['timeout_key']) == (300.0, 'WORKFLOW_COMPONENT_TIMEOUT_SECONDS')
    assert all(scope['declared_timeout'] is None and scope['timeout_key'] is None for scope in draft_scopes)
    assert all(0 < scope['effective_timeout'] <= component_scope['effective_timeout'] for scope in draft_scopes)


def test_compiled_format_rejection_exhausts_declared_passes():
    requests, events = [], []

    async def llm(request):
        metadata = json.loads(request.payload.messages[-1].content[0].text)
        requests.append(metadata)
        draft = {'original_input': 'original request', 'route': 'skip', 'ready': True,
                 'final_output': 'still wrong'}
        return SimpleNamespace(outcome='completed', payload=SimpleNamespace(
            content=(SimpleNamespace(text=json.dumps(draft)),)))

    workflow = build(Path(__file__).resolve().parents[2], llm=llm,
                     selection='synthetic', trial='format-exhaustion', observe=events.append)

    with pytest.raises(RuntimeError, match='exhausted 2 passes'):
        asyncio.run(workflow.arun({'user_input': 'original request'}))
    assert [request['attempt'] for request in requests] == [1, 2]
    assert [event['accepted'] for event in events if event['event'] == 'guard'] == [False, False]
    assert next(event for event in events if event['event'] == 'scope_end' and event['owner'] == 'first')['outcome'] == 'failed'
