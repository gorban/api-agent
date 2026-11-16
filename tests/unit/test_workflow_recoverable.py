"""Declared node recovery counts only failed visits and own deadline expiries."""
import asyncio

import pytest

from api_agent.workflows.execution import RecoverableNodeFailure
from expiry_support import ENDS, NATIVE, calls_after, expiring, play

ROUTES = (('work', 'work', 'work.recoverable and work.recoveries < NODE_TIMEOUT_PASSES'),
          ('work', 'timed_out', 'work.recoverable and work.recoveries >= NODE_TIMEOUT_PASSES'),
          ('work', 'finish', 'otherwise'))


def recovery_graph(root, routes=ROUTES, **kwargs):
    entry = expiring(root, *routes, **kwargs)
    source = NATIVE.replace('CALLS = []', 'COUNTS = []\nEFFECTS = []\nCALLS = []')
    source = source.replace("    CALLS.append('work')", "    CALLS.append('work')\n    COUNTS.append(context.recoveries)")
    source = source.replace('from api_agent.providers.progress import component_timeout',
                            'from api_agent.providers.progress import component_timeout\n'
                            'from api_agent.workflows.execution import RecoverableNodeFailure')
    source = source.replace("    if step == 'effect':", "    if step == 'failure':\n        try:\n            raise TimeoutError('private provider error')\n        except TimeoutError:\n            raise RecoverableNodeFailure() from None\n    if step == 'effect_failure':\n        context.mark_effect()\n        raise RecoverableNodeFailure()\n    if step == 'effect':")
    source = source.replace("        context.mark_effect()\n        raise RecoverableNodeFailure()", "        EFFECTS.append(context.effect_dispatched)\n        context.mark_effect()\n        EFFECTS.append(context.effect_dispatched)\n        raise RecoverableNodeFailure()")
    source = source.replace("    if step == 'effect':", "    if step == 'cancel_swallow':\n        try:\n            await asyncio.sleep(30)\n        except asyncio.CancelledError:\n            raise RecoverableNodeFailure() from None\n    if step == 'cancelled_failure':\n        from api_agent.telemetry.outcomes import inherit_failure\n        raise inherit_failure(RecoverableNodeFailure(), asyncio.CancelledError())\n    if step == 'effect':")
    (root / 'native.py').write_text(source)
    return entry


def test_native_caught_failure_replays_without_committing_failed_output(tmp_path):
    values, calls, _ = play(recovery_graph(tmp_path), {'work': ['failure', 'ok']})
    assert calls == ['work', 'work', 'finish']
    assert values['work'] == {'n': 2}


def test_typed_failures_exhaust_recovery_budget(tmp_path):
    values, calls, _ = play(recovery_graph(tmp_path), {'work': ['failure', 'failure']})
    assert calls == ['work', 'work', 'timed_out']
    assert 'work' not in values


def test_real_expiry_and_typed_failure_share_budget(tmp_path):
    values, calls, _ = play(recovery_graph(tmp_path), {'work': ['hang', 'failure']})
    assert calls == ['work', 'work', 'timed_out']
    assert 'work' not in values


def test_successful_revisit_does_not_spend_recovery_budget(tmp_path):
    flow = [('node', 'work'), ('node', 'review'), ('node', 'finish'), ('node', 'timed_out')]
    routes = (*ROUTES[:2], ('work', 'review', 'otherwise'),
              ('review', 'work', 'review.ok and work.attempt < PLAN_PASSES'), ('review', 'finish', 'otherwise'))
    entry = recovery_graph(tmp_path, routes, flow=flow, keys=('NODE_TIMEOUT_PASSES', 'PLAN_PASSES'))
    _, calls, _ = play(entry, {'work': ['ok', 'failure', 'ok']}, config={'PLAN_PASSES': 3})
    assert calls == ['work', 'review', 'work', 'work', 'review', 'finish']


def test_no_declared_recovery_route_propagates_failure(tmp_path):
    calls, error = calls_after(recovery_graph(tmp_path, (('work', 'finish', 'always'),)), {'work': ['failure']})
    assert calls == ['work']
    assert isinstance(error, RecoverableNodeFailure)


def test_effectful_failure_routes_only_explicit_effect_carrier(tmp_path):
    flow = [('node', 'work'), ('node', 'finish'), ('node', 'recover'), ('node', 'timed_out')]
    routes = (('work', 'recover', 'work.recoverable and work.effected'), *ROUTES)
    entry = recovery_graph(tmp_path, routes, flow=flow, ends=(*ENDS, ('recover', 'END', 'always')))
    _, calls, _ = play(entry, {'work': ['effect_failure']})
    assert calls == ['work', 'recover']


def test_typed_failure_cannot_route_through_negative_expiry_condition(tmp_path):
    routes = (('work', 'timed_out', 'work.recoverable and work.recoveries >= NODE_TIMEOUT_PASSES'),
              ('work', 'finish', 'not work.expired'), ('work', 'finish', 'otherwise'))
    calls, error = calls_after(recovery_graph(tmp_path, routes), {'work': ['failure']})
    assert calls == ['work']
    assert isinstance(error, RecoverableNodeFailure)


@pytest.mark.parametrize('guard', [
    'not work.recoverable and work.recoveries < NODE_TIMEOUT_PASSES',
    'work.recoverable or work.recoveries < NODE_TIMEOUT_PASSES',
    'work.recoverable and work.attempt < NODE_TIMEOUT_PASSES',
])
def test_invalid_recovery_self_edges_are_rejected(tmp_path, guard):
    from api_agent.workflows import LayoutError
    from loop_support import build
    routes = (('work', 'work', guard), ('work', 'finish', 'otherwise'))
    with pytest.raises(LayoutError):
        build(recovery_graph(tmp_path, routes))


def test_expiry_only_edge_cannot_bypass_shared_budget(tmp_path):
    from api_agent.workflows import LayoutError
    from loop_support import build
    routes = (('work', 'work', 'work.expired and work.expirations < NODE_TIMEOUT_PASSES'), *ROUTES)
    with pytest.raises(LayoutError, match='recovery replay requires'):
        build(recovery_graph(tmp_path, routes))


def test_recorded_replay_has_valid_evidence_and_recovered_presentation(tmp_path):
    from api_agent.testing.workflow_steps import scope_steps
    from api_agent.workflows.journal import validate_journal
    from test_workflow_expiry_journal import recorded

    payload, definition = recorded(recovery_graph(tmp_path), {'work': ['failure', 'ok']})
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload
    graph = scope_steps(payload['events'], projection=payload['projection'])[0]
    work = [step for step in graph['steps'] if step['name'] == 'node work']
    assert [step['status'] for step in work] == ['broken', 'passed']
    assert graph['status'] == 'broken'
    assert {'name': 'node_recovery_replays', 'value': '1'} in work[1]['parameters']


def test_exhausted_recoveries_are_not_presented_as_recovered(tmp_path):
    from api_agent.testing.workflow_steps import scope_steps
    from test_workflow_expiry_journal import recorded

    payload, _ = recorded(recovery_graph(tmp_path), {'work': ['failure', 'failure']})
    graph = scope_steps(payload['events'], projection=payload['projection'])[0]
    work = [step for step in graph['steps'] if step['name'] == 'node work']
    assert [step['status'] for step in work] == ['failed', 'failed']
    assert graph['status'] == 'failed'


def test_cancellation_receipt_never_replays(tmp_path):
    from api_agent.telemetry.outcomes import OutcomeState, failure_outcome

    calls, error = calls_after(recovery_graph(tmp_path), {'work': ['cancelled_failure', 'ok']})
    assert calls == ['work']
    assert failure_outcome(error).state == OutcomeState.CANCELLED


@pytest.mark.parametrize('guard', ['not work.recoverable', 'work.recoverable or work.recoveries < NODE_TIMEOUT_PASSES'])
def test_nonpositive_recovery_guard_is_rejected_on_forward_edge(tmp_path, guard):
    from api_agent.workflows import LayoutError
    from loop_support import build

    routes = (('work', 'timed_out', guard), ('work', 'finish', 'otherwise'))
    with pytest.raises(LayoutError, match='top-level positive conjunction'):
        build(recovery_graph(tmp_path, routes))


def test_typed_state_propagates_recoverable_signal(tmp_path):
    import test_workflow_typed_limits as typed

    entry = typed.build(tmp_path, [('node', 'raises_timeout')], '| raises_timeout | END | always |')
    source = (tmp_path / 'native.py').read_text().replace(
        "    raise TimeoutError('inner')",
        '    from api_agent.workflows import RecoverableNodeFailure\n    raise RecoverableNodeFailure()')
    (tmp_path / 'native.py').write_text(source)
    with pytest.raises(RecoverableNodeFailure):
        typed.run(tmp_path, entry)


def test_external_cancellation_cannot_be_converted_into_recovery(tmp_path):
    from loop_support import build

    workflow, native, _ = build(recovery_graph(tmp_path))
    native['SCRIPT']['work'] = ['cancel_swallow', 'ok']

    async def cancelled():
        task = asyncio.create_task(workflow.arun({'accept_at': 1}))
        while not native['CALLS']:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert native['CALLS'] == ['work']


def test_swallowed_own_deadline_remains_real_expiry_in_journal(tmp_path):
    from api_agent.workflows.journal import validate_journal
    from test_workflow_expiry_journal import ended, recorded

    payload, definition = recorded(recovery_graph(tmp_path), {'work': ['cancel_swallow', 'ok']})
    assert [event.get('reason') for event in ended(payload, 'work')] == ['declared_timeout_expired', None]
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload


def test_recovery_count_resets_each_turn(tmp_path):
    from loop_support import build

    workflow, native, _ = build(recovery_graph(tmp_path))

    async def twice():
        for _ in range(2):
            native['SCRIPT']['work'] = ['failure', 'ok']
            await workflow.arun({'accept_at': 1})

    asyncio.run(twice())
    assert native['COUNTS'] == [0, 1, 0, 1]


def test_recovery_self_edge_requires_shared_counter_even_when_attempt_is_bounded(tmp_path):
    from api_agent.workflows import LayoutError
    from loop_support import build

    routes = (('work', 'work', 'work.recoverable and work.attempt < NODE_TIMEOUT_PASSES'),
              ('work', 'finish', 'otherwise'))
    with pytest.raises(LayoutError, match='recovery replay requires'):
        build(recovery_graph(tmp_path, routes))


def test_typed_failure_does_not_change_expiry_truth(tmp_path):
    routes = (('work', 'timed_out', 'work.recoverable and not work.expired and work.expirations == 0'),
              ('work', 'finish', 'otherwise'))
    _, calls, _ = play(recovery_graph(tmp_path, routes), {'work': ['failure']})
    assert calls == ['work', 'timed_out']


def test_mixed_recovery_keeps_real_expiry_count_separate(tmp_path):
    routes = (('work', 'work', 'work.recoverable and work.recoveries < NODE_TIMEOUT_PASSES'),
              ('work', 'timed_out', 'work.recoverable and work.recoveries >= NODE_TIMEOUT_PASSES and work.expirations == 1'),
              ('work', 'finish', 'otherwise'))
    _, calls, _ = play(recovery_graph(tmp_path, routes), {'work': ['hang', 'failure']})
    assert calls == ['work', 'work', 'timed_out']


def test_concurrent_turns_keep_independent_recovery_counts(tmp_path):
    from loop_support import build

    entry = recovery_graph(tmp_path)
    path = tmp_path / 'native.py'
    source = path.read_text().replace('COUNTS.append(context.recoveries)',
                                     "COUNTS.append((context.services['tag'], context.recoveries))")
    source = source.replace("    step = SCRIPT['work'].pop(0) if SCRIPT.get('work') else 'ok'",
                            "    if context.recoveries == 0:\n        await asyncio.sleep(0.05)\n        raise RecoverableNodeFailure()\n    step = 'ok'")
    path.write_text(source)
    workflow, native, _ = build(entry)

    async def together():
        await asyncio.gather(*(workflow.arun({'accept_at': 1}, services={'tag': name})
                               for name in ('first', 'second')))

    asyncio.run(together())
    assert sorted(native['COUNTS']) == [('first', 0), ('first', 1), ('second', 0), ('second', 1)]


def test_each_node_has_its_own_recovery_allowance(tmp_path):
    from loop_support import build

    flow = [('node', 'work'), ('node', 'prepare'), ('node', 'finish'), ('node', 'timed_out')]
    routes = (*ROUTES[:2], ('work', 'prepare', 'otherwise'),
              ('prepare', 'prepare', 'prepare.recoverable and prepare.recoveries < NODE_TIMEOUT_PASSES'),
              ('prepare', 'timed_out', 'prepare.recoverable and prepare.recoveries >= NODE_TIMEOUT_PASSES'),
              ('prepare', 'finish', 'otherwise'))
    entry = recovery_graph(tmp_path, routes, flow=flow)
    path = tmp_path / 'native.py'
    source = path.read_text().replace("    CALLS.append('prepare')\n    return {'ok': True}",
                                      "    CALLS.append('prepare')\n    if context.recoveries == 0:\n        raise RecoverableNodeFailure()\n    return {'ok': True}")
    path.write_text(source)
    workflow, native, _ = build(entry)
    native['SCRIPT']['work'] = ['failure', 'ok']
    asyncio.run(workflow.arun({'accept_at': 1}))
    assert native['CALLS'] == ['work', 'work', 'prepare', 'prepare', 'finish']


def test_compiled_workflow_replays_a_recoverable_native_failure(tmp_path):
    from api_agent.workflows.artifact import plan_digest
    from api_agent.workflows.artifact_compile import compile_plan
    from artifact_acceptance_support import build_wheel, install
    from artifact_unit_support import Built, load_built

    root = tmp_path / 'tree'
    root.mkdir()
    edges = (('work', 'work', 'work.recoverable and work.recoveries <= NODE_TIMEOUT_PASSES'),
             ('work', 'timed_out', 'work.recoverable and work.recoveries > NODE_TIMEOUT_PASSES'),
             ('work', 'finish', 'otherwise'))
    entry = recovery_graph(root, edges)
    wheel = build_wheel(tmp_path, 'aq-recovery', {'aq_recovery/__init__.py': b'',
                                                  'aq_recovery/native.py': (root / 'native.py').read_bytes()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    built = Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)
    compiled = load_built(built)
    native = compiled.modules['aq_recovery.native']
    native.SCRIPT['work'] = ['failure', 'failure', 'ok']

    result = asyncio.run(compiled.workflow().arun({'accept_at': 1}))
    assert native.COUNTS == [0, 1, 2]
    assert native.CALLS == ['work', 'work', 'work', 'finish']
    assert result['work'] == {'n': 3}


def test_swallowed_ancestor_deadline_never_replays(tmp_path):
    entry = recovery_graph(tmp_path, owners=(('work', 'NODE_TIMEOUT'), ('GRAPH', 'GRAPH_TIMEOUT')))
    calls, error = calls_after(entry, {'work': ['cancel_swallow', 'ok']}, config={'GRAPH_TIMEOUT': 0.5, 'NODE_TIMEOUT': 30.0})
    assert calls == ['work']
    assert isinstance(error, TimeoutError)


def test_incomplete_route_evidence_never_claims_recovery(tmp_path):
    from copy import deepcopy
    from api_agent.testing.workflow_steps import scope_steps
    from test_workflow_expiry_journal import recorded

    payload, _ = recorded(recovery_graph(tmp_path), {'work': ['failure', 'ok']})
    events = deepcopy(payload['events'])
    edge = next(event for event in events if event['event'] == 'edge' and event['source'] == event['target'] == 'work')
    edge['sequence'] += 100
    graph = scope_steps(events, projection=payload['projection'])[0]
    assert graph['status'] == 'failed'
    assert [step['status'] for step in graph['steps'] if step['name'] == 'node work'] == ['failed', 'passed']


def test_recoverable_routing_is_not_admitted_for_components(tmp_path):
    from api_agent.workflows import LayoutError
    from loop_support import build
    from test_workflow_expiry import group

    entry = group(tmp_path, ('group', 'timed_out', 'group.recoverable'), ('group', 'finish', 'otherwise'))
    with pytest.raises(LayoutError, match='requires a native node'):
        build(entry)


@pytest.mark.parametrize('retries', [0, 1, 2])
def test_inclusive_recovery_limit_counts_replays_and_validates_journal(tmp_path, retries):
    from api_agent.workflows.journal import validate_journal
    from test_workflow_expiry_journal import recorded, routes

    edges = (('work', 'work', 'work.recoverable and work.recoveries <= NODE_TIMEOUT_PASSES'),
             ('work', 'timed_out', 'work.recoverable and work.recoveries > NODE_TIMEOUT_PASSES'),
             ('work', 'finish', 'otherwise'))
    payload, definition = recorded(recovery_graph(tmp_path, edges), {'work': ['failure'] * 4}, limit=retries)
    assert [target for _, target in routes(payload)].count('work') == retries + 1
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition,
                            expected_limits={'NODE_TIMEOUT_PASSES': retries}) == payload


def test_inclusive_limit_records_and_presents_two_successful_replays(tmp_path):
    from api_agent.testing.workflow_steps import scope_steps
    from test_workflow_expiry_journal import recorded

    edges = (('work', 'work', 'work.recoverable and work.recoveries <= NODE_TIMEOUT_PASSES'),
             ('work', 'timed_out', 'work.recoverable and work.recoveries > NODE_TIMEOUT_PASSES'),
             ('work', 'finish', 'otherwise'))
    payload, _ = recorded(recovery_graph(tmp_path, edges), {'work': ['failure', 'failure', 'ok']}, limit=2)
    [bounded] = [edge for edge in payload['projection']['edges'] if 'bound' in edge]
    assert bounded['bound']['inclusive'] is True
    graph = scope_steps(payload['events'], projection=payload['projection'])[0]
    visits = [step for step in graph['steps'] if step['name'] == 'node work']
    assert [step['status'] for step in visits] == ['broken', 'broken', 'passed']
    assert {'name': 'node_recovery_replays', 'value': '2'} in visits[2]['parameters']


def test_public_recovery_signal_is_content_free():
    from api_agent.workflows import RecoverableNodeFailure as PublicFailure

    assert PublicFailure is RecoverableNodeFailure
    with pytest.raises(TypeError):
        PublicFailure('private diagnostic')


@pytest.mark.parametrize('field', ['recoverable', 'recoveries'])
def test_recovery_fields_are_reserved_for_engine_state(tmp_path, field):
    import json
    from api_agent.workflows import LayoutError
    from loop_support import build

    entry = recovery_graph(tmp_path)
    path = tmp_path / 'work_out.json'
    schema = json.loads(path.read_text())
    schema['properties'][field] = {'type': 'boolean' if field == 'recoverable' else 'integer'}
    path.write_text(json.dumps(schema))
    with pytest.raises(LayoutError, match=f'reserved field {field}'):
        build(entry)


def test_context_reports_effect_dispatch_for_the_current_node(tmp_path):
    from loop_support import build

    flow = [('node', 'work'), ('node', 'finish'), ('node', 'recover'), ('node', 'timed_out')]
    routes = (('work', 'recover', 'work.recoverable and work.effected'), *ROUTES)
    entry = recovery_graph(tmp_path, routes, flow=flow, ends=(*ENDS, ('recover', 'END', 'always')))
    workflow, native, _ = build(entry)
    native['SCRIPT']['work'] = ['effect_failure']
    asyncio.run(workflow.arun({'accept_at': 1}))
    assert native['EFFECTS'] == [False, True]


def test_recovery_signal_outside_native_handler_propagates(tmp_path, monkeypatch):
    from api_agent.workflows import execution

    async def rejected_progress(*args):
        raise RecoverableNodeFailure()

    monkeypatch.setattr(execution, 'publish_step_progress', rejected_progress)
    calls, error = calls_after(recovery_graph(tmp_path), {'work': ['ok']})
    assert calls == []
    assert isinstance(error, RecoverableNodeFailure)


def test_expired_clipped_budget_does_not_route_as_own_expiry(tmp_path, monkeypatch):
    from api_agent.workflows.execution import Workflow

    monkeypatch.setattr(Workflow, '_own_expiry', lambda self, owner, budget: False)
    calls, error = calls_after(recovery_graph(tmp_path), {'work': ['cancel_swallow', 'ok']})
    assert calls == ['work']
    assert isinstance(error, TimeoutError)


def test_recovery_validator_reports_component_recovery_route(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.predicates import recovery_problems
    from loop_support import load
    from test_workflow_expiry import GROUP_ROUTE, group

    layout = load(group(tmp_path, *GROUP_ROUTE)).layout
    edges = tuple({**row, 'When': 'group.recoverable'} if row['When'] == 'group.expired' else row
                  for row in layout.tables['Edges'])
    problems = list(recovery_problems(replace(layout, tables={**layout.tables, 'Edges': edges})))
    assert [message for _, message in problems] == ['recoverable routing requires a native node']
