"""Structural graph packets remain self-contained under an execution deadline."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from api_agent.providers import progress
from api_agent.testing.recorded import _WorkflowStepBridge
from api_agent.testing.workflow_steps import scope_steps
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import Workflow, NodeFunction
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition
from test_workflow_inline_declarations import inline_graph, stateful_graph


@pytest.mark.parametrize('stateful', [False, True])
@pytest.mark.parametrize('interrupt', [None, 'deadline', 'cancel'])
def test_parsed_graph_packet_is_complete_under_outer_deadline(inline_graph, tmp_path, monkeypatch, stateful, interrupt):
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    entry, _ = stateful_graph(tmp_path) if stateful else inline_graph
    loader = load_stateful_definition if stateful else load_definition
    definition = loader(entry, base_root=entry.parent, pack_root=entry.parent)
    journal = WorkflowJournal(definition)
    budgets = []
    def observe(row):
        if row.get('event') == 'scope_start' and row.get('kind') == 'graph':
            budgets.append(progress._scope.get())
            if interrupt == 'deadline':
                monkeypatch.setattr(progress, '_clock', lambda: budgets[-1].deadline + 1)
            elif interrupt == 'cancel':
                asyncio.current_task().cancel()
    services = {'journal': journal, 'observe': observe}
    if stateful:
        workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition, config={}),
                                    reducers={}, config={}, services=services)
        inputs, options = {'seen': False}, {'thread_id': 'synthetic', 'journal': journal}
    else:
        native = load_native_catalog(definition, tmp_path / 'native.py')
        workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'])}, services=services)
        inputs, options = {}, {}
    async def run():
        async with progress.component_timeout('Workflow case coverage', 30) as outer:
            if interrupt:
                from api_agent.workflows.execution import WorkflowError
                expected = asyncio.CancelledError if interrupt == 'cancel' else (TimeoutError, WorkflowError)
                with pytest.raises(expected):
                    await workflow.arun(inputs, **options)
            else:
                await workflow.arun(inputs, **options)
            from api_agent.workflows.run_context import current_structural_scope
            assert current_structural_scope() is None
            assert len(budgets) == 1
            assert budgets[0].parent_id == outer.id
            assert budgets[0].deadline == outer.deadline
            assert budgets[0].timeout <= outer.timeout
    asyncio.run(run())
    packet = journal.finalize()
    validate_journal(packet)
    if interrupt:
        assert not any(row.get('event') == 'scope_end' and row.get('kind') == 'graph'
                       and row.get('outcome') == 'passed' for row in packet['events'])
        return
    assert packet['completeness'] == 'complete'
    roots = [row for row in packet['events'] if row.get('kind') == 'graph' and row['event'].startswith('scope_')]
    assert len(roots) == 2
    assert all(row['parent_id'] is None for row in roots)
    assert scope_steps(packet['events'])
    bridge = _WorkflowStepBridge()
    bridge.add(packet)
    bridge.add({'events': packet['events']})
    assert len(bridge.steps) == 1
    malformed = deepcopy(packet['events'])
    child = next(row for row in malformed if row['event'] == 'scope_start' and row.get('kind') != 'graph')
    orphan = str(uuid4())
    for row in malformed:
        if row.get('scope_id') == child['scope_id'] and row['event'] in ('scope_start', 'scope_end'):
            row['parent_id'] = orphan
    with pytest.raises(ValueError, match='Workflow scope parent is absent'):
        scope_steps(malformed)


@pytest.mark.parametrize('outer_stateful,inner_stateful', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('journaling', [False, True])
@pytest.mark.parametrize('offloaded', [False, True])
def test_nested_raw_scopes_and_independent_journals(tmp_path, monkeypatch,
        outer_stateful, inner_stateful, journaling, offloaded):
    from api_agent.workflows.run_context import current_structural_scope
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    events, journals = [], []
    turn_id = str(uuid4())

    def create(name, stateful, nested=None):
        root = tmp_path / name
        root.mkdir()
        entry, _ = stateful_graph(root) if stateful else inline_graph.__wrapped__(root)
        if nested is not None:
            prefix = ('from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n' if stateful else '')
            declaration = 'def' if offloaded else 'async def'
            call = 'context.services["nested_sync"]()' if offloaded else 'await context.services["nested"]()'
            (root / 'native.py').write_text(prefix + declaration + ' inspect(inputs, context):\n    return ' + call + '\n')
        loader = load_stateful_definition if stateful else load_definition
        definition = loader(entry, base_root=root, pack_root=root)
        services = {'observe': events.append}
        if nested is not None:
            services.update(nested=nested, nested_sync=lambda: asyncio.run(nested()))
        journal = WorkflowJournal(definition, turn_id=turn_id) if journaling else None
        if journal is not None:
            services['journal'] = journal
            journals.append(journal)
        if stateful:
            workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition, config={}),
                                        reducers={}, services=services)
            async def run():
                return await workflow.arun({'seen': False}, thread_id=name, journal=journal)
        else:
            native = load_native_catalog(definition, root / 'native.py')
            workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'])}, services=services)
            async def run():
                return (await workflow.arun({}))['inspect']
        return run

    inner = create('inner', inner_stateful)
    async def nested():
        async with progress.component_timeout('Nonstructural nested wait', 30):
            return await inner()
    outer = create('outer', outer_stateful, nested)
    async def run():
        async with progress.component_timeout('Workflow case coverage', 30):
            if outer_stateful and not inner_stateful:
                # Existing checkpoint-context incompatibility is a separate
                # execution contract; failure must still restore scope ownership.
                with pytest.raises(TypeError, match='Dict key must a type serializable with OPT_NON_STR_KEYS'):
                    await outer()
                assert current_structural_scope() is None
                start = len(events)
                sibling = create('sibling', False)
                assert await sibling() == {'seen': True}
                sibling_events = events[start:]
                assert next(row for row in sibling_events if row['event'] == 'scope_start')['parent_id'] is None
                assert scope_steps(sibling_events)
            else:
                assert await outer() == {'seen': True}
            assert current_structural_scope() is None
    asyncio.run(run())
    if outer_stateful and not inner_stateful:
        return
    starts = [row for row in events if row['event'] == 'scope_start']
    graphs = [row for row in starts if row['kind'] == 'graph']
    node = next(row for row in starts if row['kind'] == 'node')
    assert len(graphs) == 2 and graphs[0]['parent_id'] is None
    assert graphs[1]['parent_id'] == node['scope_id']
    bridge = _WorkflowStepBridge()
    bridge.add({'events': events})
    assert len(bridge.steps) == 1
    assert current_structural_scope() is None
    for journal in journals:
        packet = journal.finalize()
        validate_journal(packet)
        assert packet['completeness'] == 'complete'
        root = next(row for row in packet['events'] if row.get('event') == 'scope_start' and row.get('kind') == 'graph')
        assert root['parent_id'] is None
        assert len(scope_steps(packet['events'])) == 1
    # Journal normalization must never mutate observer rows retained above.
    assert graphs[1]['parent_id'] == node['scope_id']
