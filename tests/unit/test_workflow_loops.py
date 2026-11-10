"""Bounded backward edges give Bindings-grammar graphs explicit retry and replan loops."""
import asyncio

import pytest

from api_agent.workflows import LayoutError
from loop_support import build, edges, graph, limits, load, node, run

FLOW = [('node', 'draft'), ('node', 'verify'), ('node', 'accept'), ('node', 'failed')]
RETRY = ('verify', 'draft', 'not verify.accepted and draft.attempt < PLAN_PASSES')


def retry(tmp_path, *rows, flow=FLOW, sections=None, **kwargs):
    """The draft -> verify retry pattern, with ``rows`` overriding the verify routes."""
    rows = rows or (('verify', 'accept', 'verify.accepted'), RETRY, ('verify', 'failed', 'otherwise'))
    return graph(tmp_path, flow, sections=limits('PLAN_PASSES') + edges(*rows, ('accept', 'END', 'always'))
                 if sections is None else sections, **kwargs)


def test_success_on_the_first_run_never_takes_the_retry_edge(tmp_path):
    values, calls, _ = run(retry(tmp_path), {'accept_at': 1})

    assert calls == ['draft', 'verify', 'accept']
    assert values['draft'] == {'n': 1}
    assert 'failed' not in values


def test_success_on_the_second_run_sees_the_latest_values(tmp_path):
    values, calls, _ = run(retry(tmp_path), {'accept_at': 2})

    assert calls == ['draft', 'verify', 'draft', 'verify', 'accept']
    # Each re-entered step replaces its earlier output and binds the latest upstream values.
    assert values['draft'] == {'n': 2}
    assert values['verify'] == {'accepted': True}


def test_exhausted_retries_take_the_failure_route_as_an_ordinary_edge(tmp_path):
    values, calls, _ = run(retry(tmp_path), {'accept_at': 9})

    assert calls == ['draft', 'verify', 'draft', 'verify', 'failed']
    assert values['verify'] == {'accepted': False}
    assert 'accept' not in values


@pytest.mark.parametrize('passes,expected', [(0, 1), (1, 1), (2, 2), (3, 3)])
def test_the_limit_value_bounds_the_number_of_runs(tmp_path, passes, expected):
    _, calls, _ = run(retry(tmp_path), {'accept_at': 99}, config={'PLAN_PASSES': passes})

    assert calls.count('draft') == expected


def test_inclusive_bounds_allow_one_more_run(tmp_path):
    rows = (('verify', 'accept', 'verify.accepted'),
            ('verify', 'draft', 'not verify.accepted and draft.attempt <= PLAN_PASSES'),
            ('verify', 'failed', 'otherwise'))

    _, calls, _ = run(retry(tmp_path, *rows), {'accept_at': 99})

    assert calls.count('draft') == 3


def test_counters_read_zero_before_the_first_start(tmp_path):
    sections = limits('PLAN_PASSES') + edges(
        ('draft', 'verify', 'verify.attempt == 0'), ('draft', 'failed', 'otherwise'),
        ('verify', 'accept', 'verify.accepted'), RETRY, ('verify', 'failed', 'otherwise'),
        ('accept', 'END', 'always'))

    _, calls, _ = run(retry(tmp_path, sections=sections), {'accept_at': 2})

    # The second draft finds verify already started once, so it leaves the loop.
    assert calls == ['draft', 'verify', 'draft', 'failed']


def test_counters_count_every_start_of_the_step(tmp_path):
    rows = (('verify', 'accept', 'verify.attempt == 2'),
            ('verify', 'draft', 'verify.attempt == 1 and draft.attempt < PLAN_PASSES'),
            ('verify', 'failed', 'otherwise'))

    _, calls, _ = run(retry(tmp_path, *rows), {'accept_at': 99})

    assert calls == ['draft', 'verify', 'draft', 'verify', 'accept']


def test_counters_reset_every_turn(tmp_path):
    workflow, native, _ = build(retry(tmp_path))

    first = asyncio.run(workflow.arun({'accept_at': 2}))
    second = asyncio.run(workflow.arun({'accept_at': 9}))

    assert first['verify'] == {'accepted': True}
    assert second['verify'] == {'accepted': False}
    assert native['CALLS'] == ['draft', 'verify', 'draft', 'verify', 'accept',
                               'draft', 'verify', 'draft', 'verify', 'failed']


def test_a_self_loop_is_a_bounded_backward_edge(tmp_path):
    flow = [('node', 'plan'), ('node', 'accept')]
    sections = limits('PLAN_PASSES') + edges(
        ('plan', 'plan', 'plan.attempt < PLAN_PASSES'), ('plan', 'accept', 'otherwise'), ('accept', 'END', 'always'))
    entry = graph(tmp_path, flow, sections=sections)

    values, calls, _ = run(entry, config={'PLAN_PASSES': 3})

    assert calls == ['plan', 'plan', 'plan', 'accept']
    assert values['plan'] == {'n': 3}


def test_an_unbounded_self_loop_is_rejected(tmp_path):
    flow = [('node', 'plan'), ('node', 'accept')]
    entry = graph(tmp_path, flow, sections=limits('PLAN_PASSES') + edges(
        ('plan', 'plan', 'plan.n < 3'), ('plan', 'accept', 'otherwise')))

    with pytest.raises(LayoutError, match='backward edge'):
        load(entry)


def nested(tmp_path):
    components = {'group': (['draft', 'verify'], edges(RETRY, ('verify', 'END', 'otherwise')))}
    return graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                 sections=limits('PLAN_PASSES'), components=components)


def test_a_loop_inside_a_component_runs_within_the_nested_graph(tmp_path):
    _, calls, _ = run(nested(tmp_path), {'accept_at': 99})

    assert calls == ['draft', 'verify', 'draft', 'verify', 'accept']


@pytest.mark.parametrize('shape', [nested, retry])
def test_the_recursion_bound_covers_the_largest_declared_loops(tmp_path, shape):
    _, calls, _ = run(shape(tmp_path), {'accept_at': 99}, config={'PLAN_PASSES': 99})

    assert calls.count('draft') == 99
    assert calls.count('verify') == 99


def test_a_replan_edge_can_loop_between_sibling_components(tmp_path):
    flow = [('component', 'planning'), ('component', 'analysis'), ('node', 'finish')]
    sections = limits('REPLAN_PASSES') + edges(
        ('analysis', 'planning', 'check.route == "replan" and planning.attempt < REPLAN_PASSES'),
        ('analysis', 'finish', 'otherwise'))
    entry = graph(tmp_path, flow, sections=sections, components={
        'planning': (['plan'], ''), 'analysis': (['analyze', 'check'], '')})

    values, calls, _ = run(entry, {'accept_at': 2})
    assert calls == ['plan', 'analyze', 'check', 'plan', 'analyze', 'check', 'finish']
    assert values['plan'] == {'n': 2} and values['check'] == {'route': 'done'}

    _, calls, _ = run(entry, {'accept_at': 9})
    assert calls == ['plan', 'analyze', 'check', 'plan', 'analyze', 'check', 'finish']
    _, calls, _ = run(entry, {'accept_at': 9}, config={'REPLAN_PASSES': 3})
    assert calls.count('plan') == 3


@pytest.mark.parametrize('when', [
    'not verify.accepted',
    'otherwise',
    'always',
    'retry_allowed',
    'not verify.accepted and draft.attempt < 3',
    'not verify.accepted and verify.attempt < PLAN_PASSES',
    'not verify.accepted or draft.attempt < PLAN_PASSES',
    'not verify.accepted and draft.attempt == PLAN_PASSES',
    'not verify.accepted and draft.attempt > PLAN_PASSES',
    'not verify.accepted and not (draft.attempt < PLAN_PASSES)',
    'not verify.accepted and PLAN_PASSES > draft.attempt',
    'not verify.accepted and draft.attempt < draft.attempt',
])
def test_a_backward_edge_without_a_limits_bound_is_rejected_with_a_located_error(tmp_path, when):
    fallback = ('verify', 'failed', 'otherwise') if when != 'otherwise' else ('verify', 'failed', 'verify.accepted')
    entry = retry(tmp_path, ('verify', 'draft', when), fallback)

    with pytest.raises(LayoutError, match='backward edge to draft requires a bounded When') as caught:
        load(entry)

    assert f'{entry}:' in str(caught.value)


def test_a_bound_on_an_undeclared_name_is_not_a_limits_bound(tmp_path):
    entry = retry(tmp_path, ('verify', 'draft', 'not verify.accepted and draft.attempt < OTHER_PASSES'),
                  ('verify', 'failed', 'otherwise'))

    with pytest.raises(LayoutError, match='condition'):
        load(entry)


def test_bounds_may_be_nested_conjunctions(tmp_path):
    rows = (('verify', 'accept', 'verify.accepted'),
            ('verify', 'draft', '(not verify.accepted and (PLAN_PASSES > 0 and draft.attempt < PLAN_PASSES))'),
            ('verify', 'failed', 'otherwise'))

    _, calls, _ = run(retry(tmp_path, *rows), {'accept_at': 99})

    assert calls.count('draft') == 2


def test_backward_edges_still_connect_siblings_only(tmp_path):
    flow = [('component', 'group'), ('node', 'accept'), ('node', 'failed')]
    entry = graph(tmp_path, flow, sections=limits('PLAN_PASSES') + edges(
        ('accept', 'draft', 'draft.attempt < PLAN_PASSES'), ('accept', 'failed', 'otherwise')),
        components={'group': (['draft', 'verify'], '')})

    with pytest.raises(LayoutError, match='siblings'):
        load(entry)


def test_an_output_field_may_not_shadow_the_attempt_counter(tmp_path):
    entry = retry(tmp_path)
    node(tmp_path, 'draft', outputs={'n': {'type': 'integer'}, 'attempt': {'type': 'integer'}})

    with pytest.raises(LayoutError, match='reserved field attempt') as caught:
        load(entry)

    assert str(tmp_path / 'draft.md.html') + ':' in str(caught.value)


def test_nested_output_fields_named_attempt_are_allowed(tmp_path):
    entry = retry(tmp_path)
    node(tmp_path, 'draft', outputs={'n': {'type': 'integer'},
                                     'meta': {'type': 'object', 'properties': {'attempt': {'type': 'integer'}}}})

    assert load(entry)


def test_the_recursion_bound_is_derived_from_the_declared_limits(tmp_path):
    flat = {'PLAN_PASSES': 2}
    workflow, _, _ = build(retry(tmp_path), config=flat)
    inclusive = tmp_path / 'inclusive'
    inclusive.mkdir()
    rows = (('verify', 'accept', 'verify.accepted'),
            ('verify', 'draft', 'not verify.accepted and draft.attempt <= PLAN_PASSES'),
            ('verify', 'failed', 'otherwise'))
    wider, _, _ = build(retry(inclusive, *rows), config=flat)
    forward = tmp_path / 'forward'
    forward.mkdir()
    plain, _, _ = build(graph(forward, FLOW[:1] + FLOW[2:3]))

    assert workflow._recursion_limit == 4 * (1 + 2) + 1
    assert wider._recursion_limit == 4 * (1 + 3) + 1
    assert plain._recursion_limit == 2 + 1
