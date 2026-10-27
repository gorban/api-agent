"""Grouping labels preserve independent turn and invocation identities."""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from test_workflow_stateful import fixture_graph

from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.workflows.run_context import (
    conversation, current_execution_context, current_turn_id, trial, turn,
)

CONVERSATION = '00000000-0000-0000-0000-000000000001'
OTHER = '00000000-0000-0000-0000-000000000002'
TURN = '00000000-0000-0000-0000-000000000003'


def definition():
    return SimpleNamespace(graph_id='Example', definition_id='b' * 64,
        entry_root='base', resources=[], replacements=[],
        layout=SimpleNamespace(steps=[SimpleNamespace(id='Make', kind='node', parent=None)],
                               tables={'Edges': []}))


def test_conversation_retains_separate_turns_and_invocations():
    with conversation(CONVERSATION):
        with turn(TURN):
            first = WorkflowJournal(definition()).finalize()
            second = WorkflowJournal(definition()).finalize()
        with turn(OTHER):
            followup = WorkflowJournal(definition()).finalize()
    assert first['execution_context'] == second['execution_context'] == followup['execution_context'] == {
        'kind': 'conversation', 'conversation_id': CONVERSATION}
    assert first['turn_id'] == second['turn_id'] == TURN
    assert followup['turn_id'] == OTHER
    assert len({row['invocation_id'] for row in (first, second, followup)}) == 3
    assert validate_journal(first) == first
    assert current_execution_context() is None
    assert current_turn_id() is None


def test_new_conversation_and_legacy_context_are_separate():
    with conversation(CONVERSATION):
        first = WorkflowJournal(definition()).finalize()
    with conversation(OTHER):
        second = WorkflowJournal(definition()).finalize()
    legacy = WorkflowJournal(definition()).finalize()
    assert first['execution_context'] != second['execution_context']
    assert 'execution_context' not in legacy
    assert validate_journal(legacy) == legacy


@pytest.mark.parametrize('value', [0, 61, True, '1', None])
def test_trial_bound_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        with trial(value):
            pass


def test_nested_grouping_restores_after_exception():
    with conversation(CONVERSATION):
        with pytest.raises(RuntimeError):
            with trial(60):
                assert current_execution_context() == {'kind': 'trial', 'trial_index': 60}
                raise RuntimeError
        assert current_execution_context() == {'kind': 'conversation', 'conversation_id': CONVERSATION}


@pytest.mark.parametrize('context', [None, {}, {'kind': 'trial', 'trial_index': True},
    {'kind': 'trial', 'trial_index': 61},
    {'kind': 'conversation', 'conversation_id': CONVERSATION, 'extra': 1},
    {'kind': 'conversation', 'conversation_id': 'invalid'}])
def test_valid_digest_does_not_admit_invalid_grouping(context):
    payload = WorkflowJournal(definition()).finalize()
    payload['execution_context'] = context
    unsigned = {key: value for key, value in payload.items() if key != 'sha256'}
    payload['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError, match='execution'):
        validate_journal(payload)


@pytest.mark.parametrize('inherited', [False, True])
def test_declarative_turn_conflict_rejects_before_execution(inherited):
    import asyncio
    from contextlib import nullcontext
    from pathlib import Path

    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.workflows.contrived import build

    calls = []

    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent(json.dumps(dict(
            original_input='x', route='skip', ready=True, final_output='x'))),)))

    workflow = build(Path(__file__).resolve().parents[2], llm=complete,
                     selection='example', trial='synthetic')
    journal = WorkflowJournal(workflow.definition, turn_id=TURN)
    workflow.services['journal'] = journal

    with turn(OTHER) if inherited else nullcontext():
        with pytest.raises(ValueError, match='turn identity disagrees with the active journal'):
            asyncio.run(workflow.arun({'user_input': 'x'},
                                     turn_id=None if inherited else OTHER))

    assert calls == []
    assert journal.turn_id == TURN
    assert journal.finalize()['events'] == []


@pytest.mark.parametrize('executor', ['declarative', 'stateful'])
@pytest.mark.parametrize('recorded', [False, True])
@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('binding', ['ambient', 'journal', 'conflict', 'started'])
def test_supplied_journal_context_admitted_before_execution(
        fixture_graph, monkeypatch, executor, recorded, binding, wrapped):
    import asyncio
    from contextlib import nullcontext
    from pathlib import Path

    from langgraph.graph.message import add_messages
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.workflows.contrived import build
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition

    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1' if recorded else '0')
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.workflows.stateful._source_manifest', lambda *a, **k: None)
    packets, calls = [], []
    monkeypatch.setattr('api_agent.workflows.evidence.publish', packets.append)

    def record():
        calls.append((current_execution_context(), current_turn_id()))

    if executor == 'declarative':
        async def complete(request):
            record()
            return LLMResult(Message('assistant', (TextContent(json.dumps(dict(
                original_input='x', route='skip', ready=True, final_output='x'))),)))
        from test_workflow_trial_decisions import trial_uuid
        workflow = build(Path(__file__).resolve().parents[2], llm=complete,
                         selection='example', trial=trial_uuid())
        admitted = workflow.definition
    else:
        root, entry = fixture_graph
        source = root / 'native.py'
        source.write_text(source.read_text().replace('def intake(state, context):',
            "def intake(state, context):\n    context.services['record']()"))
        admitted = load_stateful_definition(entry, base_root=root, pack_root=root)
        workflow = StatefulWorkflow(admitted, catalog=load_stateful_catalog(admitted),
            reducers={'messages': add_messages}, services={'record': record})

    with conversation(OTHER if binding == 'conflict' else CONVERSATION) if binding in ('journal', 'conflict') else nullcontext():
        journal = WorkflowJournal(admitted, turn_id=TURN)
    supplied = journal
    if wrapped:
        from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
        from api_agent.workflows.observations import WorkflowObservations
        supplied = WorkflowObservations(journal, boundary=EmissionBoundary(ContentPolicy(False)),
                                        log_policy=ContentPolicy(False))
    if binding == 'started':
        journal.observe({'event': 'scope_start', 'scope_id': 'existing', 'kind': 'graph',
                         'owner': 'GRAPH', 'started_ms': 1})
    retained = list(journal._events)
    if executor == 'declarative':
        workflow.services['journal'] = supplied
        operation = lambda: workflow.arun({'user_input': 'x'}, turn_id=TURN)
    else:
        workflow.journal = supplied
        workflow._journal.set(supplied)
        operation = lambda: workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                         thread_id='example', turn_id=TURN)

    with conversation(CONVERSATION) if binding != 'journal' else nullcontext():
        if binding in ('conflict', 'started'):
            with pytest.raises(ValueError, match='execution context disagrees'):
                asyncio.run(operation())
        else:
            asyncio.run(operation())

    if binding in ('conflict', 'started'):
        assert calls == []
        assert journal._events == retained
        assert journal.execution_context == ({'kind': 'conversation', 'conversation_id': OTHER}
                                             if binding == 'conflict' else None)
    else:
        assert calls == [({'kind': 'conversation', 'conversation_id': CONVERSATION}, TURN)]
        packet = packets[0] if recorded else journal.finalize()
        assert packet['execution_context'] == {'kind': 'conversation', 'conversation_id': CONVERSATION}
        assert packet['turn_id'] == TURN
    assert current_execution_context() is None


@pytest.mark.parametrize('executor', ['declarative', 'stateful'])
@pytest.mark.parametrize('recorded', [False, True])
def test_empty_explicit_turn_rejects_without_journal(fixture_graph, monkeypatch, executor, recorded):
    import asyncio
    from pathlib import Path
    from langgraph.graph.message import add_messages
    from api_agent.workflows.contrived import build
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition

    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')
    calls = []
    if executor == 'declarative':
        async def complete(request):
            calls.append(request)
        workflow = build(Path(__file__).resolve().parents[2], llm=complete,
                         selection='example', trial='synthetic')
        operation = lambda: workflow.arun({'user_input': 'x'}, turn_id='')
    else:
        root, entry = fixture_graph
        admitted = load_stateful_definition(entry, base_root=root, pack_root=root)
        workflow = StatefulWorkflow(admitted, catalog=load_stateful_catalog(admitted),
                                    reducers={'messages': add_messages})
        operation = lambda: workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                         thread_id='example', turn_id='')
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1' if recorded else '0')
    with pytest.raises(ValueError, match='execution identity'):
        asyncio.run(operation())
    assert calls == []
