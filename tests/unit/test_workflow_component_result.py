"""``<component>.<field>`` on an outer edge reads the output of the child that exited the component."""
import pytest

from api_agent.workflows import LayoutError
from api_agent.workflows.predicates import PredicateError, component_exits

import scripted_support as scripted
from expiry_support import expiring, play
from loop_support import edges, limits

STR = {'type': 'string'}
VERDICT = {'verdict': STR}
NODES = {
    'check': ({}, VERDICT),
    'fix': ({}, VERDICT),
    'gate': ({}, {**VERDICT, 'accepted': {'type': 'boolean'}, 'feedback': {'type': 'object'}}),
    'inner': ({}, VERDICT),
    'done': ({}, {'ok': {'type': 'boolean'}}),
    'retry': ({}, {'ok': {'type': 'boolean'}}),
}
# check leaves the group when it passes; otherwise fix runs and ends the group.
CHECK_OR_FIX = edges(('check', 'END', 'check.verdict == "ok"'), ('check', 'fix', 'otherwise'))
ROUTE = edges(('group', 'done', 'group.verdict == "ok"'), ('group', 'retry', 'otherwise'), ('done', 'END', 'always'))
FLOW = [('component', 'group'), 'done', 'retry']


def graph(root, *, nodes=NODES, inner=CHECK_OR_FIX, route=ROUTE, children=('check', 'fix'), flow=FLOW,
          row=('1', '-'), components=None):
    return scripted.graph(root, flow, nodes, sections=route, components={
        'group': (list(children), row, inner), **(components or {})})


def calls_for(tmp_path, script, **kwargs):
    workflow, calls, _ = scripted.start(graph(tmp_path, **kwargs), script)
    scripted.run(workflow)
    return calls


def test_the_result_is_the_output_of_the_child_that_took_the_edge_to_end(tmp_path):
    assert calls_for(tmp_path, {'check': [{'verdict': 'ok'}]}) == ['check', 'done']


def test_a_later_child_that_ends_the_component_supplies_the_result(tmp_path):
    # check's own verdict is bad; fix ends the group implicitly, so its verdict is the result.
    script = {'check': [{'verdict': 'bad'}], 'fix': [{'verdict': 'ok'}]}

    assert calls_for(tmp_path, script) == ['check', 'fix', 'done']


def test_a_result_that_fails_the_predicate_takes_the_other_edge(tmp_path):
    script = {'check': [{'verdict': 'bad'}], 'fix': [{'verdict': 'bad'}]}

    assert calls_for(tmp_path, script) == ['check', 'fix', 'retry']


def test_the_latest_completed_run_supplies_the_result(tmp_path):
    route = limits('PLAN_PASSES') + edges(
        ('group', 'group', 'group.verdict == "again" and group.attempt < PLAN_PASSES'),
        ('group', 'done', 'otherwise'), ('done', 'END', 'always'))

    assert calls_for(tmp_path, {'check': [{'verdict': 'again'}, {'verdict': 'ok'}]}, inner='', route=route,
                     children=('check',), flow=[('component', 'group'), 'done']) == ['check', 'check', 'done']


def test_a_guarded_components_result_comes_from_the_accepted_pass(tmp_path):
    script = {'gate': [{'accepted': False, 'feedback': {'reason': 'again'}, 'verdict': 'bad'},
                       {'accepted': True, 'feedback': {}, 'verdict': 'ok'}]}

    assert calls_for(tmp_path, script, inner='', children=('check', 'gate'), row=('2', 'gate')) == [
        'check', 'gate', 'check', 'gate', 'done']


def test_a_nested_components_result_comes_from_the_node_that_exited_it(tmp_path):
    components = {'inner_group': (['inner'], ('1', '-'), '')}
    route = edges(('group', 'done', 'group.verdict == "ok" and inner_group.verdict == "ok"'),
                  ('group', 'retry', 'otherwise'), ('done', 'END', 'always'))

    assert calls_for(tmp_path, {'inner': [{'verdict': 'ok'}]}, inner='', route=route,
                     children=(('component', 'inner_group'),), components=components) == ['inner', 'done']


def test_component_exits_follow_end_edges_implicit_ends_and_nested_components(tmp_path):
    entry = scripted.graph(tmp_path, [('component', 'group'), ('component', 'outer')], NODES, components={
        'group': (['check', 'fix'], ('1', '-'), CHECK_OR_FIX),
        'outer': ([('component', 'solo')], ('1', '-'), ''),
        'solo': (['inner'], ('1', '-'), '')})

    assert component_exits(scripted.load(entry).layout) == {
        'group': ('check', 'fix'), 'outer': ('inner',), 'solo': ('inner',)}


def test_a_result_field_must_be_declared_by_every_node_that_can_exit(tmp_path):
    nodes = {**NODES, 'fix': ({}, {'score': {'type': 'integer'}})}

    with pytest.raises(LayoutError, match=r'group\.verdict must be declared with one type .* check, fix') as error:
        scripted.load(graph(tmp_path, nodes=nodes))

    assert 'flow.md.html:' in str(error.value)


def test_a_result_field_must_have_one_type_across_the_exiting_nodes(tmp_path):
    nodes = {**NODES, 'fix': ({}, {'verdict': {'type': 'integer'}})}

    with pytest.raises(LayoutError, match=r'group\.verdict must be declared with one type'):
        scripted.load(graph(tmp_path, nodes=nodes))


def test_a_field_only_a_node_that_cannot_exit_declares_is_not_a_result(tmp_path):
    nodes = {**NODES, 'check': ({}, {**VERDICT, 'extra': STR})}
    route = edges(('group', 'done', 'group.extra == "x"'), ('group', 'retry', 'otherwise'), ('done', 'END', 'always'))

    with pytest.raises(LayoutError, match=r'group\.extra must be declared'):
        scripted.load(graph(tmp_path, nodes=nodes, route=route))


def test_a_component_with_no_exiting_node_has_no_result(tmp_path):
    loop = edges(('check', 'fix', 'always'), ('fix', 'fix', 'fix.attempt < PLAN_PASSES'))
    route = limits('PLAN_PASSES') + ROUTE

    with pytest.raises(LayoutError, match=r'group\.verdict must be declared .*: none'):
        scripted.load(graph(tmp_path, inner=loop, route=route))


def test_a_component_result_is_not_readable_inside_the_component(tmp_path):
    inner = edges(('check', 'fix', 'group.verdict == "ok"'), ('check', 'END', 'otherwise'))

    with pytest.raises(LayoutError, match='condition may read only declared'):
        scripted.load(graph(tmp_path, inner=inner))


def test_a_result_missing_from_the_exiting_nodes_output_fails_the_turn(tmp_path):
    workflow, _, _ = scripted.start(graph(tmp_path), {'check': [{'verdict': 'bad'}], 'fix': [{}]})

    with pytest.raises(PredicateError, match=r'condition field group\.verdict was not produced'):
        scripted.run(workflow)


def test_a_component_that_never_ran_has_no_result(tmp_path):
    flow = ['retry', ('component', 'group'), 'done']
    route = edges(('retry', 'done', 'always'), ('done', 'END', 'group.verdict == "ok"'),
                  ('done', 'END', 'otherwise'))
    workflow, _, _ = scripted.start(graph(tmp_path, flow=flow, route=route))

    with pytest.raises(PredicateError, match=r'condition field group\.verdict was not produced'):
        scripted.run(workflow)


def test_an_exit_without_output_leaves_the_component_without_a_result(tmp_path):
    inner = edges(('work', 'END', 'work.expired'), ('work', 'END', 'otherwise'))
    flow = [('component', 'group'), ('node', 'finish'), ('node', 'timed_out')]
    rows = (('group', 'finish', 'group.n == 1'), ('group', 'timed_out', 'otherwise'))
    for name in ('finished', 'expired'):
        (tmp_path / name).mkdir()
    components = {'group': (['work'], inner)}

    assert play(expiring(tmp_path / 'finished', *rows, flow=flow, components=components))[1] == ['work', 'finish']
    with pytest.raises(PredicateError, match=r'condition field group\.n was not produced'):
        play(expiring(tmp_path / 'expired', *rows, flow=flow, components=components), {'work': ['hang']})
