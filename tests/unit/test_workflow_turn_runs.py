"""TURN resolves against the turn's recorded completed runs, the same record Session merges use."""
import asyncio

from api_agent.workflows.execution import _latest_turn_value

import scripted_support as scripted
from loop_support import edges, limits, table

ANY = {}
NODES = {
    'seed': ({}, {'v': {'type': 'integer'}}),
    'x': ({}, {'v': {'type': 'integer'}}),
    'y': ({}, {'v': {'type': 'integer'}}),
    'w': ({'saw': ('TURN', 'v', '"none"')}, {'v': {'type': 'integer'}, 'saw': ANY}),
    'gate': ({}, {'accepted': {'type': 'boolean'}, 'feedback': {'type': 'object'}}),
    'judge': ({}, {'ok': {'type': 'boolean'}}),
    'reader': ({'seen': ('TURN', 'v', '"none"')}, {'seen': ANY}),
}


def graph(root, flow, *, components=None, sections=''):
    return scripted.graph(root, flow, NODES, sections=sections,
                          components={name: (*value, '') for name, value in (components or {}).items()})


def start(entry, script):
    workflow, calls, _ = scripted.start(entry, script)
    return workflow, calls


def run(workflow, thread=None):
    return scripted.run(workflow, thread=thread)


REPEAT_X = limits('PLAN_PASSES') + edges(('x', 'x', 'x.attempt < PLAN_PASSES'), ('x', 'reader', 'otherwise'))
REENTER = limits('PLAN_PASSES') + edges(('judge', 'group', 'group.attempt < PLAN_PASSES'),
                                        ('judge', 'END', 'otherwise'))
SESSION = table('Session', ('Field', 'Source', 'Merge'), [
    ('last_v', 'y.v', 'replace'), ('last_v', 'x.v', 'replace'), ('seen_v', 'reader.seen', 'replace')])


def test_a_rerun_that_omits_a_field_does_not_hide_the_nodes_own_earlier_write(tmp_path):
    entry = graph(tmp_path, ['y', 'x', 'reader'], sections=REPEAT_X)
    workflow, calls = start(entry, {'y': [{'v': 2}], 'x': [{'v': 1}, {}]})

    values = run(workflow)

    assert calls == ['y', 'x', 'x', 'reader']
    assert values['x'] == {} and values['reader']['seen'] == 1


def test_reentering_a_component_keeps_its_earlier_values_visible_to_turn(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), 'judge'], sections=REENTER,
                  components={'group': (['w'], ('1', '-'))})
    workflow, calls = start(entry, {'w': [{'v': 1}, {'v': 2}], 'judge': [{'ok': False}]})

    values = run(workflow)

    assert calls == ['w', 'judge', 'w', 'judge']
    assert values['w'] == {'v': 2, 'saw': 1}


def test_a_value_written_only_in_a_rejected_pass_is_not_counted(tmp_path):
    entry = graph(tmp_path, ['seed', ('component', 'group'), 'reader'],
                  components={'group': (['w', 'gate'], ('2', 'gate'))})
    workflow, calls = start(entry, {
        'seed': [{'v': 3}], 'w': [{'v': 7}, {}],
        'gate': [{'accepted': False, 'feedback': {'reason': 'again'}}, {'accepted': True, 'feedback': {}}]})

    values = run(workflow)

    assert calls == ['seed', 'w', 'gate', 'w', 'gate', 'reader']
    assert values['w']['saw'] == 3 and values['reader']['seen'] == 3


def test_a_rejected_pass_with_no_earlier_writer_leaves_the_default(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), 'reader'],
                  components={'group': (['w', 'gate'], ('2', 'gate'))})
    workflow, _ = start(entry, {
        'w': [{'v': 7}, {}],
        'gate': [{'accepted': False, 'feedback': {'reason': 'again'}}, {'accepted': True, 'feedback': {}}]})

    values = run(workflow)

    assert values['w']['saw'] == 'none' and values['reader']['seen'] == 'none'


def test_turn_and_session_agree_on_the_latest_completed_write(tmp_path):
    entry = graph(tmp_path, ['y', 'x', 'reader'], sections=REPEAT_X + SESSION)
    workflow, _ = start(entry, {'y': [{'v': 2}], 'x': [{'v': 1}, {}]})

    values = run(workflow, 'thread')
    committed = asyncio.run(workflow.session('thread'))

    assert values['reader']['seen'] == 1
    assert committed == {'last_v': 1, 'seen_v': 1}


def test_turn_and_session_agree_across_component_reentry(tmp_path):
    rows = table('Session', ('Field', 'Source', 'Merge'), [('last_v', 'w.v', 'replace')])
    entry = graph(tmp_path, [('component', 'group'), 'judge'], sections=REENTER + rows,
                  components={'group': (['w'], ('1', '-'))})
    workflow, _ = start(entry, {'w': [{'v': 1}, {}], 'judge': [{'ok': False}]})

    values = run(workflow, 'thread')

    assert values['w'] == {'saw': 1}
    assert asyncio.run(workflow.session('thread')) == {'last_v': 1}


def test_latest_turn_value_follows_completion_order_not_flow_order():
    runs = [('second', {'x': 2}), ('first', {'x': 1}), ('third', {'y': 3})]

    assert _latest_turn_value(runs, 'x') == (True, 1)
    assert _latest_turn_value(runs[:1], 'x') == (True, 2)
    assert _latest_turn_value(runs, 'y') == (True, 3)


def test_latest_turn_value_counts_every_recorded_run_and_reports_absence():
    runs = [('first', {'x': 1}), ('first', {})]

    assert _latest_turn_value(runs, 'x') == (True, 1)
    assert _latest_turn_value(runs, 'z') == (False, None)
    assert _latest_turn_value([], 'x') == (False, None)
