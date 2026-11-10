"""Session writes follow completion order, and every completed run of a looped node counts."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

import loop_support
from loop_support import NATIVE, build, edges, graph, limits, table

NATIVE_LINES = NATIVE.replace(
    "return {'n': CALLS.count('draft')}",
    "return {'n': CALLS.count('draft'), 'lines': ['draft#%d' % CALLS.count('draft')]}")
RETRY = ('verify', 'draft', 'not verify.accepted and draft.attempt < PLAN_PASSES')
FLOW = [('node', 'draft'), ('node', 'verify'), ('node', 'accept'), ('node', 'failed')]


@pytest.fixture
def array_outputs(monkeypatch):
    monkeypatch.setitem(loop_support.OUTPUTS, 'draft', {'n': {'type': 'integer'}, 'lines': {'type': 'array'}})
    monkeypatch.setitem(loop_support.OUTPUTS, 'plan', {'n': {'type': 'integer'}, 'items': {'type': 'array'}})


def stored(saver):
    """The newest saved session, whatever key the engine files it under."""
    return next(iter(saver.list(None))).checkpoint['channel_values']['session']


def play(tmp_path, sections, *, flow=FLOW, accept_at=2, native=NATIVE_LINES, **kwargs):
    entry = graph(tmp_path, flow, sections=sections, native=native, **kwargs)
    saver = InMemorySaver()
    workflow, native, _ = build(entry, checkpointer=saver)
    asyncio.run(workflow.arun({'accept_at': accept_at}, thread_id='t'))
    return stored(saver), native['CALLS']


def test_append_takes_the_field_from_every_completed_run_in_completion_order(tmp_path, array_outputs):
    sections = (limits('PLAN_PASSES')
                + edges(('verify', 'accept', 'verify.accepted'), RETRY, ('verify', 'failed', 'otherwise'),
                        ('accept', 'END', 'always'))
                + table('Session', ('Field', 'Source', 'Merge'), [('log', 'draft.lines', 'append')]))

    session, calls = play(tmp_path, sections)

    assert calls.count('draft') == 2
    assert session['log'] == ['draft#1', 'draft#2']


def test_replace_takes_the_value_of_the_last_completed_run_not_the_last_in_flow_order(tmp_path, array_outputs):
    # `failed` comes later in Flow but completes before the second draft.
    sections = (limits('PLAN_PASSES')
                + edges(('verify', 'accept', 'verify.accepted'), ('verify', 'failed', 'otherwise'),
                        ('failed', 'draft', 'draft.attempt < PLAN_PASSES'), ('failed', 'END', 'otherwise'),
                        ('accept', 'END', 'always'))
                + table('Session', ('Field', 'Source', 'Merge'),
                        [('last', 'failed.ok', 'replace'), ('last', 'draft.n', 'replace')]))

    session, calls = play(tmp_path, sections)

    assert calls == ['draft', 'verify', 'failed', 'draft', 'verify', 'accept']
    assert session['last'] == 2


def test_replace_of_a_looped_node_keeps_its_latest_run(tmp_path, array_outputs):
    sections = (limits('PLAN_PASSES')
                + edges(('verify', 'accept', 'verify.accepted'), RETRY, ('verify', 'failed', 'otherwise'),
                        ('accept', 'END', 'always'))
                + table('Session', ('Field', 'Source', 'Merge'), [('n', 'draft.n', 'replace')]))

    session, _ = play(tmp_path, sections, accept_at=3)

    assert session['n'] == 2


def test_each_visit_of_a_looped_component_appends_its_runs(tmp_path, array_outputs):
    sections = (limits('REPLAN_PASSES')
                + edges(('analysis', 'planning', 'check.route == "replan" and planning.attempt < REPLAN_PASSES'),
                        ('analysis', 'finish', 'otherwise'))
                + table('Session', ('Field', 'Source', 'Merge'), [('plans', 'plan.items', 'append')]))
    native = NATIVE.replace("return {'n': CALLS.count('plan')}",
                            "return {'n': CALLS.count('plan'), 'items': ['plan#%d' % CALLS.count('plan')]}")

    session, _ = play(tmp_path, sections, flow=[('component', 'planning'), ('component', 'analysis'),
                                                ('node', 'finish')],
                      native=native, components={'planning': (['plan'], ''), 'analysis': (['analyze', 'check'], '')})

    assert session['plans'] == ['plan#1', 'plan#2']


def test_a_run_whose_output_omits_the_field_writes_nothing(tmp_path, array_outputs):
    native = NATIVE.replace(
        "return {'n': CALLS.count('draft')}",
        "return {'n': CALLS.count('draft'), **({'lines': ['kept']} if CALLS.count('draft') == 1 else {})}")
    sections = (limits('PLAN_PASSES')
                + edges(('verify', 'accept', 'verify.accepted'), RETRY, ('verify', 'failed', 'otherwise'),
                        ('accept', 'END', 'always'))
                + table('Session', ('Field', 'Source', 'Merge'),
                        [('log', 'draft.lines', 'append'), ('first', 'draft.lines', 'replace')]))

    session, calls = play(tmp_path, sections, native=native)

    assert calls.count('draft') == 2 and session == {'log': ['kept'], 'first': ['kept']}
