"""A step whose own declared timeout expires is routed by its `<step>.expired` edges."""
import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from api_agent.workflows import LayoutError
from expiry_support import ENDS, REPLAY, ROUTE, calls_after, expiring, play
from loop_support import build, edges, load, node, table
from test_workflow_session_loop import stored


def test_an_expired_node_is_routed_to_the_step_its_expired_edge_names(tmp_path):
    values, calls, _ = play(expiring(tmp_path, *ROUTE), {'work': ['hang']})

    assert calls == ['work', 'timed_out']
    assert values['timed_out'] == {'ok': False}
    # The expired run produced nothing, so nothing of it is visible downstream.
    assert 'work' not in values and 'finish' not in values


def test_a_node_that_finishes_in_time_never_takes_the_expired_edge(tmp_path):
    values, calls, _ = play(expiring(tmp_path, *ROUTE))

    assert calls == ['work', 'finish']
    assert values['work'] == {'n': 1}


def test_a_bounded_backward_edge_replays_an_expired_node_once(tmp_path):
    values, calls, _ = play(expiring(tmp_path, *REPLAY), {'work': ['hang', 'ok']})

    assert calls == ['work', 'work', 'finish']
    assert values['work'] == {'n': 2}


def test_replays_stop_at_the_limit_and_take_the_error_route(tmp_path):
    _, calls, _ = play(expiring(tmp_path, *REPLAY), {'work': ['hang', 'hang']})

    assert calls == ['work', 'work', 'timed_out']


@pytest.mark.parametrize('limit,runs', [(1, 1), (3, 3)])
def test_the_limit_value_bounds_the_replays(tmp_path, limit, runs):
    _, calls, _ = play(expiring(tmp_path, *REPLAY), {'work': ['hang'] * 5}, config={'NODE_TIMEOUT_PASSES': limit})

    assert calls == ['work'] * runs + ['timed_out']


def test_the_flag_is_visible_to_later_edges_and_clears_when_the_step_runs_again(tmp_path):
    rows = (('work', 'finish', 'work.expired'), ('work', 'finish', 'otherwise'),
            ('finish', 'timed_out', 'work.expired'), ('finish', 'END', 'otherwise'))
    entry = expiring(tmp_path, *rows, ends=ENDS[1:])

    assert play(entry, {'work': ['hang']})[1] == ['work', 'finish', 'timed_out']
    assert play(entry)[1] == ['work', 'finish']


def test_the_flag_clears_when_a_replay_succeeds(tmp_path):
    rows = (*REPLAY[:2], ('work', 'finish', 'otherwise'))
    sections = (('finish', 'timed_out', 'work.expired'), ('finish', 'END', 'otherwise'))

    _, calls, _ = play(expiring(tmp_path, *rows, *sections, ends=ENDS[1:]), {'work': ['hang', 'ok']})

    assert calls == ['work', 'work', 'finish']


def test_an_expired_rerun_drops_the_output_of_the_run_before_it(tmp_path):
    flow = [('node', 'work'), ('node', 'review'), ('node', 'finish'), ('node', 'timed_out')]
    rows = (('work', 'timed_out', 'work.expired'), ('work', 'review', 'otherwise'),
            ('review', 'work', 'work.attempt < NODE_TIMEOUT_PASSES'), ('review', 'finish', 'otherwise'))

    values, calls, _ = play(expiring(tmp_path, *rows, flow=flow), {'work': ['ok', 'hang']})

    assert calls == ['work', 'review', 'work', 'timed_out']
    assert 'work' not in values and values['review'] == {'ok': True}


def test_bindings_of_the_next_step_fall_back_to_their_defaults(tmp_path):
    flow = [('node', 'work'), ('node', 'report'), ('node', 'finish'), ('node', 'timed_out')]
    rows = (('work', 'report', 'work.expired'), ('work', 'report', 'otherwise'), ('report', 'finish', 'always'))
    entry = expiring(tmp_path, *rows, flow=flow)
    document = tmp_path / 'report.md.html'
    document.write_text(document.read_text().replace('| report | n | work | n | - |', '| report | n | work | n | 0 |'))

    expired, _, _ = play(entry, {'work': ['hang']})
    finished, _, _ = play(entry)

    assert expired['report'] == {'n': 0} and finished['report'] == {'n': 1}


def test_a_node_without_an_expired_edge_still_fails_the_turn(tmp_path):
    calls, error = calls_after(expiring(tmp_path, ('work', 'finish', 'always')), {'work': ['hang']})

    assert isinstance(error, TimeoutError) and calls == ['work']


def test_an_expiry_that_no_expired_edge_accepts_fails_the_turn(tmp_path):
    rows = (('work', 'work', 'work.expired and work.attempt < NODE_TIMEOUT_PASSES'), ('work', 'finish', 'otherwise'))

    calls, error = calls_after(expiring(tmp_path, *rows), {'work': ['hang', 'hang']})

    # The fallback edge is for finished runs: an exhausted replay does not silently continue.
    assert isinstance(error, TimeoutError) and calls == ['work', 'work']


@pytest.mark.parametrize('step', ['hang', 'swallow'])
def test_the_graph_deadline_is_not_the_nodes_expiry(tmp_path, step):
    # Routing straight to END would let the turn pass if the graph's deadline were taken for the node's.
    rows = (('work', 'END', 'work.expired'), ('work', 'finish', 'otherwise'))
    entry = expiring(tmp_path, *rows, owners=(('GRAPH', 'GRAPH_TIMEOUT'), ('work', 'NODE_TIMEOUT')))

    calls, error = calls_after(entry, {'work': [step]}, config={'GRAPH_TIMEOUT': 0.1, 'NODE_TIMEOUT': 30})

    # A node that turns the graph's cancellation into its own TimeoutError still did not expire itself.
    assert isinstance(error, TimeoutError) and calls == ['work']


@pytest.mark.parametrize('step', ['port', 'native'])
def test_a_timeout_raised_inside_the_node_is_not_the_nodes_expiry(tmp_path, step):
    calls, error = calls_after(expiring(tmp_path, *ROUTE), {'work': [step]}, config={'NODE_TIMEOUT': 30})

    assert isinstance(error, TimeoutError) and calls == ['work']


def test_a_node_that_dispatched_an_effect_fails_the_turn_instead_of_routing(tmp_path):
    for rows in (ROUTE, REPLAY):
        calls, error = calls_after(expiring(tmp_path, *rows), {'work': ['effect', 'ok']})

        assert isinstance(error, TimeoutError) and calls == ['work']


def group(tmp_path, *rows, inner=(), children=('prepare', 'work'), owners=(('group', 'GROUP_TIMEOUT'),), extra=''):
    flow = [('component', 'group'), ('node', 'finish'), ('node', 'timed_out')]
    components = {'group': (list(children), edges(*inner) if inner else '')}
    return expiring(tmp_path, *rows, flow=flow, owners=owners, components=components, extra=extra)


GROUP_ROUTE = (('group', 'timed_out', 'group.expired'), ('group', 'finish', 'otherwise'))


def test_an_expired_component_is_routed_by_its_own_edges(tmp_path):
    values, calls, _ = play(group(tmp_path, *GROUP_ROUTE), {'work': ['hang']})

    assert calls == ['prepare', 'work', 'timed_out']
    # Nothing the abandoned run produced survives, even from steps that had finished.
    assert set(values) == {'START', 'timed_out'}


def test_a_component_that_finishes_in_time_never_takes_the_expired_edge(tmp_path):
    values, calls, _ = play(group(tmp_path, *GROUP_ROUTE))

    assert calls == ['prepare', 'work', 'finish'] and values['work'] == {'n': 1}


def test_an_expired_component_can_be_replayed_by_a_bounded_backward_edge(tmp_path):
    rows = (('group', 'group', 'group.expired and group.attempt < NODE_TIMEOUT_PASSES'),
            ('group', 'timed_out', 'group.expired and group.attempt >= NODE_TIMEOUT_PASSES'),
            ('group', 'finish', 'otherwise'))

    assert play(group(tmp_path, *rows), {'work': ['hang', 'ok']})[1] == [
        'prepare', 'work', 'prepare', 'work', 'finish']
    (tmp_path / 'again').mkdir()
    assert play(group(tmp_path / 'again', *rows), {'work': ['hang', 'hang']})[1] == [
        'prepare', 'work', 'prepare', 'work', 'timed_out']


def test_a_component_without_an_expired_edge_still_fails_the_turn(tmp_path):
    calls, error = calls_after(group(tmp_path, ('group', 'finish', 'always')), {'work': ['hang']})

    assert isinstance(error, TimeoutError) and calls == ['prepare', 'work']


def test_an_effect_inside_a_component_fails_the_turn_instead_of_routing(tmp_path):
    calls, error = calls_after(group(tmp_path, *GROUP_ROUTE), {'work': ['effect']})

    assert isinstance(error, TimeoutError) and calls == ['prepare', 'work']


def test_a_node_inside_a_component_is_routed_within_it(tmp_path):
    inner = (('work', 'recover', 'work.expired'), ('work', 'after', 'otherwise'),
             ('after', 'END', 'always'), ('recover', 'END', 'always'))
    entry = group(tmp_path, ('group', 'finish', 'always'), inner=inner, children=('work', 'after', 'recover'),
                  owners=(('work', 'NODE_TIMEOUT'), ('group', 'GROUP_TIMEOUT')))

    values, calls, _ = play(entry, {'work': ['hang']}, config={'GROUP_TIMEOUT': 30})

    assert calls == ['work', 'recover', 'finish'] and values['recover'] == {'ok': False}


def test_the_enclosing_component_deadline_is_not_the_nodes_expiry(tmp_path):
    inner = (('work', 'recover', 'work.expired'), ('work', 'after', 'otherwise'),
             ('after', 'END', 'always'), ('recover', 'END', 'always'))
    owners = (('work', 'NODE_TIMEOUT'), ('group', 'GROUP_TIMEOUT'))
    config = {'NODE_TIMEOUT': 30, 'GROUP_TIMEOUT': 0.1}

    unrouted = group(tmp_path, ('group', 'finish', 'always'), inner=inner, children=('work', 'after', 'recover'),
                     owners=owners)
    calls, error = calls_after(unrouted, {'work': ['swallow']}, config=config)
    assert isinstance(error, TimeoutError) and calls == ['work']

    (tmp_path / 'routed').mkdir()
    routed = group(tmp_path / 'routed', *GROUP_ROUTE, inner=inner, children=('work', 'after', 'recover'),
                   owners=owners)
    calls, error = calls_after(routed, {'work': ['hang']}, config=config)
    assert error is None and calls == ['work', 'timed_out']


def test_an_expired_and_routed_turn_persists_the_session_of_the_runs_that_completed(tmp_path):
    flow = [('node', 'prepare'), ('node', 'work'), ('node', 'finish'), ('node', 'timed_out')]
    rows = (('work', 'timed_out', 'work.expired'), ('work', 'finish', 'otherwise'))
    fields = [('prepared', 'prepare.ok', 'replace'), ('worked', 'work.n', 'replace'),
              ('note', 'timed_out.ok', 'replace')]
    entry = expiring(tmp_path, *rows, flow=flow, extra=table('Session', ('Field', 'Source', 'Merge'), fields))
    saver = InMemorySaver()
    workflow, native, _ = build(entry, checkpointer=saver)
    native['SCRIPT']['work'] = ['hang']

    asyncio.run(workflow.arun({'accept_at': 1}, thread_id='t'))

    assert native['CALLS'] == ['prepare', 'work', 'timed_out']
    assert stored(saver) == {'prepared': True, 'note': False}


def test_an_expired_component_contributes_nothing_to_the_session(tmp_path):
    fields = [('prepared', 'prepare.ok', 'replace'), ('note', 'timed_out.ok', 'replace')]
    entry = group(tmp_path, *GROUP_ROUTE, extra=table('Session', ('Field', 'Source', 'Merge'), fields))
    saver = InMemorySaver()
    workflow, native, _ = build(entry, checkpointer=saver)
    native['SCRIPT']['work'] = ['hang']

    asyncio.run(workflow.arun({'accept_at': 1}, thread_id='t'))

    assert native['CALLS'] == ['prepare', 'work', 'timed_out']
    assert stored(saver) == {'note': False}


def test_a_failed_expiry_leaves_the_session_untouched(tmp_path):
    flow = [('node', 'prepare'), ('node', 'work'), ('node', 'finish'), ('node', 'timed_out')]
    fields = [('prepared', 'prepare.ok', 'replace')]
    entry = expiring(tmp_path, ('work', 'finish', 'always'), flow=flow,
                     extra=table('Session', ('Field', 'Source', 'Merge'), fields))
    saver = InMemorySaver()
    workflow, native, _ = build(entry, checkpointer=saver)
    native['SCRIPT']['work'] = ['hang']

    with pytest.raises(TimeoutError):
        asyncio.run(workflow.arun({'accept_at': 1}, thread_id='t'))

    assert not list(saver.list(None))


def test_the_output_schema_may_not_declare_expired(tmp_path):
    entry = expiring(tmp_path, *ROUTE)
    node(tmp_path, 'work', outputs={'n': {'type': 'integer'}, 'expired': {'type': 'boolean'}})

    with pytest.raises(LayoutError, match=r'work\.md\.html:\d+: Output schema declares reserved field expired'):
        load(entry)


def test_an_expired_condition_needs_the_step_to_declare_its_own_timeout_row(tmp_path):
    with pytest.raises(LayoutError, match=r'work\.expired requires work to declare its own Timeouts row'):
        load(expiring(tmp_path, *ROUTE, owners=(('finish', 'NODE_TIMEOUT'),)))


def test_a_port_row_is_not_the_nodes_own_timeout_row(tmp_path):
    with pytest.raises(LayoutError, match='requires work to declare its own Timeouts row'):
        load(expiring(tmp_path, *ROUTE, owners=(('GRAPH', 'GRAPH_TIMEOUT'),)))


def test_a_backward_expired_edge_needs_the_usual_bound(tmp_path):
    rows = (('work', 'work', 'work.expired'), ('work', 'finish', 'otherwise'))

    with pytest.raises(LayoutError, match='backward edge to work requires a bounded When'):
        load(expiring(tmp_path, *rows))


def test_expired_conditions_compose_with_other_conditions(tmp_path):
    rows = (('work', 'timed_out', 'work.expired or not work.attempt < 1'), ('work', 'finish', 'otherwise'))

    assert load(expiring(tmp_path, *rows)).layout.tables['Edges'][0]['When'] == rows[0][2]
