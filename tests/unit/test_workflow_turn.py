"""The TURN binding source reads the latest value any node wrote earlier in the same turn."""
from pathlib import Path

import pytest

from api_agent.workflows import LayoutError, WorkflowError
from api_agent.workflows.execution import _latest_turn_value
from api_agent.workflows.identity import declaration_identity
from api_agent.workflows.model import freeze

from session_workflow_support import load_session, session_entry, session_workflow, turn

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'turn_workflow'


def entry_for(root, *edits):
    return session_entry(root, *edits, fixture=FIXTURE, flow='turn_chat.md.html')


def run(tmp_path, message, *edits, thread='chat'):
    return turn(session_workflow(entry_for(tmp_path, *edits)), message, thread)


def test_single_producer_is_visible_to_a_later_node(tmp_path):
    values = run(tmp_path, 'b1')

    assert values['path_b']['topic'] == 'B(b1)'
    assert values['finish']['answer'] == 'B(b1)'


def test_latest_completed_writer_wins_on_the_branch_that_ran(tmp_path):
    taken_a = run(tmp_path / 'a', 'a1')
    taken_b = run(tmp_path / 'b', 'b1')

    # intake, then path_a or path_b, each overwrite topic; path_a also writes extra.
    assert taken_a['path_a']['topic'] == 'A(a1)' and 'path_b' not in taken_a
    assert taken_a['finish']['answer'] == 'A(a1)'
    assert taken_a['draft']['draft'] == 'A(a1)#2'
    assert taken_a['finish']['saw_extra'] == 'extra-from-a1'
    assert taken_b['finish']['saw_extra'] == 'none'


def test_latest_turn_value_follows_completion_order_not_flow_order():
    values = freeze({'first': {'x': 1}, 'second': {'x': 2}, 'third': {'y': 3}})

    assert _latest_turn_value({'x': ['second', 'first']}, values, 'x') == (True, 1)
    assert _latest_turn_value({'x': ['first', 'second', 'third']}, values, 'x') == (True, 2)
    assert _latest_turn_value({'x': ['first', 'third']}, values, 'x') == (True, 1)


def test_latest_turn_value_skips_cleared_nodes_and_reports_absence():
    values = {'first': {'x': 1}}

    assert _latest_turn_value({'x': ['first', 'cleared']}, values, 'x') == (True, 1)
    assert _latest_turn_value({'x': ['cleared']}, values, 'x') == (False, None)
    assert _latest_turn_value({}, values, 'x') == (False, None)


def test_default_is_used_when_no_node_has_written_the_field(tmp_path):
    values = run(tmp_path, 'b1')

    assert values['draft']['saw_scratch'] == 'none'
    assert values['finish']['saw_extra'] == 'none'


def test_required_turn_field_fails_loudly_when_nothing_wrote_it(tmp_path):
    edit = ('| finish | extra | TURN | extra | "none" |', '| finish | extra | TURN | extra | - |')

    with pytest.raises(WorkflowError, match='Missing required turn value extra for finish.extra'):
        run(tmp_path, 'b1', edit)
    assert run(tmp_path / 'ok', 'a1', edit)['finish']['saw_extra'] == 'extra-from-a1'


def test_rejected_pass_values_are_not_visible_to_the_next_pass(tmp_path):
    values = run(tmp_path, 'a1')

    # Pass 1 wrote scratch and hint; the engine clears the component's outputs for
    # pass 2, so scratch is absent and hint falls back to the earlier writer, intake.
    assert values['draft']['saw_scratch'] == 'none'
    assert values['draft']['saw_hint'] == 'seed'
    assert values['draft']['draft'] == 'A(a1)#2'


def test_rejected_pass_values_are_not_visible_after_the_component(tmp_path):
    values = run(tmp_path, 'a1')

    assert values['finish']['saw_scratch'] == 'none'
    assert values['finish']['saw_hint'] == 'seed'


def test_turn_never_sees_a_prior_turn_while_session_does(tmp_path):
    workflow = session_workflow(entry_for(tmp_path))

    first = turn(workflow, 'a1')
    second = turn(workflow, 'b2')

    assert first['finish']['saw_extra'] == 'extra-from-a1'
    assert second['finish']['saw_extra'] == 'none'
    assert first['intake']['previous'] == ''
    assert second['intake']['previous'] == 'extra-from-a1'


@pytest.mark.parametrize(('edit', 'message'), [
    (('| finish | extra | TURN | extra | "none" |', '| finish | extra | TURN | nothing | "none" |'),
     'no node declares turn field'),
    (('| finish | extra | TURN | extra | "none" |', '| finish | extra | TURN | * | "none" |'),
     'TURN requires a named field'),
    (('| draft | attempt | ATTEMPT | value | - |', '| draft | attempt | TURN | topic | - |'),
     'turn value type does not match the input schema'),
    (('| finish | extra | TURN | extra | "none" |', '| finish | extra | TURN | extra | 3 |'),
     'is not of type'),
])
def test_load_rejects_invalid_turn_bindings(tmp_path, edit, message):
    with pytest.raises(LayoutError, match=message):
        load_session(entry_for(tmp_path, edit))


def test_turn_binding_changes_workflow_identity(tmp_path):
    base = declaration_identity(load_session(entry_for(tmp_path / 'base')))

    assert declaration_identity(load_session(entry_for(tmp_path / 'same'))) == base
    edit = ('| finish | hint | TURN | hint | "none" |', '| finish | hint | TURN | topic | "none" |')
    assert declaration_identity(load_session(entry_for(tmp_path / 'edit', edit))) != base
