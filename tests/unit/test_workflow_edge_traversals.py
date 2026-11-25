"""Edge budgets count selections, independently of destination starts."""
import asyncio
from copy import deepcopy
import pytest
from loop_support import build
from test_workflow_loops import retry
from test_workflow_loops_journal_bound import sign
from api_agent.workflows.journal import WorkflowJournal, validate_journal


def test_edge_budget_run_and_journal(tmp_path):
    edges = (('verify', 'accept', 'verify.accepted'),
             ('verify', 'draft', 'not verify.accepted and EDGE.traversals < PLAN_PASSES'),
             ('verify', 'failed', 'otherwise'))
    entry = retry(tmp_path, *edges)
    workflow, native, definition = build(entry)
    journal = WorkflowJournal(definition)
    asyncio.run(workflow.arun({'accept_at': 3}, journal=journal))
    result = journal.finalize()
    assert native['CALLS'].count('draft') == 3
    assert result['completeness'] == 'complete'
    assert validate_journal(result, expected_definition=definition) == result
    changed = deepcopy(result)
    changed['projection']['limits'][0]['value'] = 1
    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(changed))
    changed = deepcopy(result)
    next(edge for edge in changed['projection']['edges'] if 'bound' in edge)['bound']['counter'] = 'recoveries'
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(sign(changed), expected_definition=definition)
    changed = deepcopy(result)
    next(edge for edge in changed['projection']['edges'] if 'bound' in edge).pop('bound')
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(sign(changed), expected_definition=definition)


def test_distinct_edges_to_same_target_have_independent_budgets(tmp_path):
    edges = (('verify', 'accept', 'verify.accepted'),
             ('verify', 'draft', 'not verify.accepted and draft.n < 3 and EDGE.traversals < PLAN_PASSES'),
             ('verify', 'draft', 'not verify.accepted and draft.n >= 3 and EDGE.traversals < PLAN_PASSES'),
             ('verify', 'failed', 'otherwise'))
    entry = retry(tmp_path, *edges)
    workflow, native, definition = build(entry)
    journal = WorkflowJournal(definition)
    asyncio.run(workflow.arun({'accept_at': 5}, journal=journal))
    result = journal.finalize()
    assert native['CALLS'].count('draft') == 5
    assert validate_journal(result, expected_definition=definition) == result
    native['CALLS'].clear()
    asyncio.run(workflow.arun({'accept_at': 5}))
    assert native['CALLS'].count('draft') == 5


def test_disjunction_does_not_prove_a_cycle_bound(tmp_path):
    from api_agent.workflows import LayoutError
    from loop_support import load
    entry = retry(tmp_path, ('verify', 'draft', 'not verify.accepted or EDGE.traversals < PLAN_PASSES'),
                  ('verify', 'failed', 'otherwise'))
    with pytest.raises(LayoutError, match='top-level bound'):
        load(entry)


def test_recovery_does_not_reset_selected_edge_budget(tmp_path):
    from test_workflow_recoverable import ROUTES, recovery_graph
    from test_workflow_expiry_journal import recorded
    flow = [('node', 'work'), ('node', 'review'), ('node', 'finish'), ('node', 'timed_out')]
    routes = (*ROUTES[:2], ('work', 'review', 'otherwise'),
              ('review', 'work', 'review.ok and EDGE.traversals < PLAN_PASSES'),
              ('review', 'finish', 'otherwise'))
    entry = recovery_graph(tmp_path, routes, flow=flow, keys=('NODE_TIMEOUT_PASSES', 'PLAN_PASSES'))
    result, definition = recorded(entry, {'work': ['ok', 'failure', 'ok', 'ok']},
                                  limit=1, PLAN_PASSES=1)
    assert validate_journal(result, expected_definition=definition,
                            expected_limits={'NODE_TIMEOUT_PASSES': 1, 'PLAN_PASSES': 1}) == result
    taken = [e for e in result['events'] if e['event'] == 'edge'
             and e.get('source') == 'review' and e.get('target') == 'work']
    assert len(taken) == 1


def test_typed_loop_rejects_edge_budget(tmp_path):
    import test_workflow_typed_limits as typed
    entry = typed.build(tmp_path, [('node', 'loop'), ('node', 'finish')],
                        '| loop | loop | EDGE.traversals < PLAN_RETRIES |\n| loop | finish | otherwise |\n| finish | END | always |',
                        sections=typed.LIMIT_TABLE + '| PLAN_RETRIES | [limits](limits.toml) |\n')
    from api_agent.workflows import LayoutError
    with pytest.raises(LayoutError, match='only on Bindings'):
        typed.run(tmp_path, entry)


@pytest.mark.parametrize('condition', [
    'EDGE.traversals < PLAN_PASSES',
    "EDGE['traversals'] < PLAN_PASSES",
    'EDGE.traversals < PLAN_PASSES and EDGE.traversals < PLAN_PASSES',
])
def test_edge_counter_is_not_a_forward_or_end_condition(tmp_path, condition):
    from api_agent.workflows import LayoutError
    from loop_support import load
    entry = retry(tmp_path, ('verify', 'accept', condition), ('verify', 'failed', 'otherwise'))
    with pytest.raises(LayoutError, match='EDGE.traversals'):
        load(entry)


def test_edge_is_reserved_as_a_limit_name(tmp_path):
    from api_agent.workflows import LayoutError
    from loop_support import load
    entry = retry(tmp_path)
    entry.write_text(entry.read_text().replace('PLAN_PASSES', 'EDGE'))
    (tmp_path / 'config.toml').write_text((tmp_path / 'config.toml').read_text().replace('PLAN_PASSES', 'EDGE'))
    with pytest.raises(LayoutError, match='invalid limit key'):
        load(entry)
