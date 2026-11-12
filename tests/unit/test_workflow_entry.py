"""``START`` edge rows choose which child runs first when a component (or the graph) is entered."""
import pytest

from api_agent.workflows import LayoutError, WorkflowError
from api_agent.workflows.predicates import PredicateError

import scripted_support as scripted
from loop_support import edges, limits, table

STR = {'type': 'string'}
NODES = {
    'pre': ({}, {'mode': STR}),
    'skipped': ({}, {'x': STR}),
    'a': ({}, {'tag': STR}),
    'b': ({}, {'tag': STR}),
    'c': ({}, {'tag': STR}),
    'post': ({}, {'ok': {'type': 'boolean'}}),
    'judge': ({}, {'ok': {'type': 'boolean'}}),
}
ENTRY = edges(('START', 'b', 'pre.mode == "b"'), ('START', 'c', 'pre.mode == "c"'), ('START', 'a', 'otherwise'))
FLOW = ['pre', ('component', 'group'), 'post']


def graph(root, rows=ENTRY, *, flow=FLOW, sections='', nodes=NODES, extra=None, children=('a', 'b', 'c')):
    return scripted.graph(root, flow, nodes, sections=sections,
                          components={'group': (list(children), ('1', '-'), rows if extra is None else extra)})


def calls_for(tmp_path, mode, rows=ENTRY, **kwargs):
    workflow, calls, _ = scripted.start(graph(tmp_path, rows, **kwargs), {'pre': [{'mode': mode}]})
    scripted.run(workflow)
    return calls


@pytest.mark.parametrize(('mode', 'ran'), [('b', ['b', 'c']), ('c', ['c']), ('other', ['a', 'b', 'c'])])
def test_entering_a_component_runs_the_child_the_matching_row_names(tmp_path, mode, ran):
    assert calls_for(tmp_path, mode) == ['pre', *ran, 'post']


def test_a_component_without_start_rows_runs_its_first_child(tmp_path):
    assert calls_for(tmp_path, 'c', '') == ['pre', 'a', 'b', 'c', 'post']


def test_the_graph_entry_may_choose_its_first_step_from_the_turn_input(tmp_path):
    nodes = {**NODES, 'a': ({'mode': ('START', 'mode', '"none"', STR)}, {'tag': STR})}
    entry = graph(tmp_path, '', extra='', nodes=nodes, flow=['a', 'b', 'c'],
                  sections=edges(('START', 'c', 'START.mode == "c"'), ('START', 'a', 'otherwise')))
    workflow, calls, _ = scripted.start(entry)

    scripted.run(workflow, {'mode': 'c'})
    assert calls == ['c']
    calls.clear()
    scripted.run(workflow, {'mode': 'x'})
    assert calls == ['a', 'b', 'c']


@pytest.mark.parametrize(('rows', 'mode'), [
    (edges(('START', 'b', 'pre.mode != "z"'), ('START', 'c', 'pre.mode != "y"')), 'b'),
    (edges(('START', 'b', 'pre.mode == "b"'), ('START', 'c', 'pre.mode == "c"')), 'other'),
])
def test_the_entry_requires_exactly_one_matching_row(tmp_path, rows, mode):
    workflow, _, _ = scripted.start(graph(tmp_path, rows), {'pre': [{'mode': mode}]})

    with pytest.raises(WorkflowError, match='Routing requires exactly one matching condition or fallback'):
        scripted.run(workflow)


def test_several_matching_rows_that_name_one_child_enter_it(tmp_path):
    rows = edges(('START', 'b', 'pre.mode == "b"'), ('START', 'b', 'pre.mode != "c"'), ('START', 'a', 'otherwise'))

    assert calls_for(tmp_path, 'b', rows) == ['pre', 'b', 'c', 'post']


def test_a_sole_always_row_is_a_fixed_entry(tmp_path):
    assert calls_for(tmp_path, 'b', edges(('START', 'c', 'always'))) == ['pre', 'c', 'post']


def test_an_entry_predicate_reads_the_components_start_count_and_a_limit(tmp_path):
    rows = edges(('START', 'b', 'group.attempt >= PLAN_PASSES'), ('START', 'a', 'otherwise'))
    flow = [('component', 'group'), 'judge']
    sections = limits('PLAN_PASSES') + edges(('judge', 'group', 'group.attempt < PLAN_PASSES'),
                                             ('judge', 'END', 'otherwise'))
    workflow, calls, _ = scripted.start(graph(tmp_path, rows, flow=flow, sections=sections))

    scripted.run(workflow)

    assert calls == ['a', 'b', 'c', 'judge', 'b', 'c', 'judge']


def test_an_entry_predicate_reads_an_earlier_steps_expiry_flag(tmp_path):
    timeouts = table('Timeouts', ('Owner', 'Source', 'Key'), [('pre', '[config](config.toml)', 'NODE_TIMEOUT')])
    rows = edges(('START', 'c', 'pre.expired'), ('START', 'a', 'otherwise'))

    assert calls_for(tmp_path, 'b', rows, sections=timeouts) == ['pre', 'a', 'b', 'c', 'post']


def test_an_entry_predicate_cannot_read_a_step_without_its_own_timeout(tmp_path):
    rows = edges(('START', 'c', 'pre.expired'), ('START', 'a', 'otherwise'))

    with pytest.raises(LayoutError, match='pre.expired requires pre to declare its own Timeouts row'):
        scripted.load(graph(tmp_path, rows))


def test_an_entry_predicate_reading_a_step_that_has_not_run_fails_the_turn(tmp_path):
    rows = edges(('START', 'c', 'skipped.x == "y"'), ('START', 'a', 'otherwise'))
    flow = ['pre', 'skipped', ('component', 'group'), 'post']
    sections = edges(('pre', 'group', 'always'))
    workflow, _, _ = scripted.start(graph(tmp_path, rows, flow=flow, sections=sections),
                                    {'pre': [{'mode': 'a'}]})

    with pytest.raises(PredicateError, match='condition field skipped.x was not produced'):
        scripted.run(workflow)


@pytest.mark.parametrize(('row', 'message'), [
    (('START', 'post', 'always'), 'START must name a direct child of the document that declares it'),
    (('START', 'END', 'always'), 'START must name a direct child of the document that declares it'),
    (('START', 'nowhere', 'always'), 'START must name a direct child of the document that declares it'),
    (('START', 'b', 'a.tag == "x"'), 'condition may read only declared scalar output fields'),
    (('START', 'b', 'post.ok'), 'condition may read only declared Boolean fields'),
    (('START', 'b', 'pre.mode == "b" and group.tag == "x"'), 'condition may read only declared scalar output fields'),
])
def test_load_rejects_an_invalid_entry_row_at_the_row(tmp_path, row, message):
    entry = graph(tmp_path, edges(row, ('START', 'a', 'otherwise')))

    with pytest.raises(LayoutError, match=message) as error:
        scripted.load(entry)

    assert 'group.md.html:' in str(error.value) and 'flow.md.html' not in str(error.value)


@pytest.mark.parametrize(('rows', 'message'), [
    (edges(('START', 'a', 'always'), ('START', 'b', 'otherwise')), 'always must stand alone'),
    (edges(('START', 'a', 'always'), ('START', 'b', 'always')), 'duplicate edge condition'),
    (edges(('START', 'a', 'pre.mode == "a"'), ('START', 'b', 'pre.mode == "a"')), 'duplicate edge condition'),
])
def test_entry_rows_follow_the_ordinary_group_rules(tmp_path, rows, message):
    with pytest.raises(LayoutError, match=message):
        scripted.load(graph(tmp_path, rows))


def test_an_entry_row_may_not_be_declared_in_a_node_document(tmp_path):
    entry = graph(tmp_path, '')
    path = tmp_path / 'a.md.html'
    path.write_text(path.read_text() + edges(('START', 'a', 'always')))

    with pytest.raises(LayoutError, match='START must name a direct child') as error:
        scripted.load(entry)

    assert 'a.md.html:' in str(error.value)


def test_the_same_condition_text_in_two_components_reads_each_entry_scope(tmp_path):
    inner = edges(('START', 'b', 'pre.mode == "b"'), ('START', 'a', 'otherwise'))
    entry = scripted.graph(tmp_path, ['pre', ('component', 'first'), ('component', 'second')], {
        **NODES, 'd': ({}, {'tag': STR}), 'e': ({}, {'tag': STR})}, components={
        'first': (['a', 'b'], ('1', '-'), inner), 'second': (['d', 'e'], ('1', '-'), edges(
            ('START', 'e', 'pre.mode == "b"'), ('START', 'd', 'otherwise')))})
    workflow, calls, _ = scripted.start(entry, {'pre': [{'mode': 'b'}]})

    scripted.run(workflow)

    assert calls == ['pre', 'b', 'e']


def test_the_entry_rows_change_the_workflow_identity(tmp_path):
    from api_agent.workflows.identity import declaration_identity
    for name in ('plain', 'entered', 'edited'):
        (tmp_path / name).mkdir()
    plain = declaration_identity(scripted.load(graph(tmp_path / 'plain', '')))
    entered = declaration_identity(scripted.load(graph(tmp_path / 'entered')))
    edited = declaration_identity(scripted.load(graph(
        tmp_path / 'edited', edges(('START', 'b', 'pre.mode == "q"'), ('START', 'c', 'pre.mode == "c"'),
                                  ('START', 'a', 'otherwise')))))

    assert len({plain, entered, edited}) == 3


def test_the_diagram_draws_the_entry_rows(tmp_path):
    workflow, _, _ = scripted.start(graph(tmp_path))

    diagram = workflow.diagram()

    assert 'group_START -->|pre.mode == "b"| b' in diagram
    assert 'group_START -->|otherwise| a' in diagram
    assert 'group_START --> a' not in diagram
