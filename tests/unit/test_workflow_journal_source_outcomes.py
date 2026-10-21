"""A following route requires successful completion of its source scope."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.workflows.application import build
from api_agent.workflows.contrived import build as build_contrived
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.journal import WorkflowJournal, validate_journal

ROOT = Path(__file__).resolve().parents[2]


def redigest(payload):
    unsigned = {key: value for key, value in payload.items() if key != 'sha256'}
    payload['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def completed_one_pass(monkeypatch):
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')

    async def complete(request):
        return LLMResult(Message('assistant', (TextContent('Ok'),)))

    workflow = build(ROOT, llm=complete, selection='controlled')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal
    asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Controlled journal'}]}))
    return workflow.definition, journal


@pytest.mark.parametrize('source_owner', ['llm_call', 'agent'])
def test_redigested_failed_source_cannot_claim_complete_one_pass(completed_one_pass, source_owner):
    definition, journal = completed_one_pass
    original = journal.finalize()
    assert original['completeness'] == 'complete'
    assert validate_journal(original, expected_definition=definition) == original
    changed = deepcopy(original)
    source_end = next(event for event in changed['events']
                      if event['event'] == 'scope_end' and event['owner'] == source_owner)
    source_end['outcome'] = 'failed'
    redigest(changed)

    with pytest.raises(ValueError, match='completeness'):
        validate_journal(changed, expected_definition=definition)

    assert sum(left != right for left, right in zip(original['events'], changed['events'])) == 1
    assert next(event for event in changed['events']
                if event['event'] == 'scope_end' and event['owner'] == 'GRAPH')['outcome'] == 'passed'


@pytest.mark.parametrize('source_owner', ['llm_call', 'agent'])
def test_finalize_marks_failed_source_followed_by_edge_partial(completed_one_pass, source_owner):
    definition, journal = completed_one_pass
    source_end = next(event for event in journal._events
                      if event['event'] == 'scope_end' and event['owner'] == source_owner)
    source_end['outcome'] = 'failed'

    payload = journal.finalize()

    assert payload['completeness'] == 'partial'
    assert validate_journal(payload, expected_definition=definition) == payload


def test_runtime_failed_child_and_enclosing_scopes_remain_valid_partial(monkeypatch):
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')

    async def fail(request):
        raise WorkflowError('Controlled local failure')

    workflow = build(ROOT, llm=fail, selection='controlled')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal

    with pytest.raises(WorkflowError, match='Controlled local failure'):
        asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Controlled journal'}]}))
    payload = journal.finalize()

    assert payload['completeness'] == 'partial'
    assert {event['owner'] for event in payload['events']
            if event['event'] == 'scope_end' and event['outcome'] == 'failed'} == {'llm_call', 'agent', 'GRAPH'}
    assert not any(event['event'] == 'edge' and event.get('source') in {'llm_call', 'agent'}
                   for event in payload['events'])
    assert validate_journal(payload, expected_definition=workflow.definition) == payload


def test_guard_rejection_retries_passed_children_and_completes(monkeypatch):
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')
    requests = []

    async def complete(request):
        requests.append(request)
        tail = json.loads(request.payload.messages[-1].content[0].text)
        draft = {'original_input': 'hello', 'route': 'skip',
                 'ready': tail['attempt'] > 1, 'final_output': 'hello'}
        return LLMResult(Message('assistant', (TextContent(json.dumps(draft)),)))

    workflow = build_contrived(ROOT, llm=complete, selection='controlled', trial='local')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal

    asyncio.run(workflow.arun({'user_input': 'hello'}))
    payload = journal.finalize()

    assert len(requests) == 2
    assert [event['accepted'] for event in payload['events'] if event['event'] == 'guard'] == [False, True]
    assert all(event['outcome'] == 'passed' for event in payload['events'] if event['event'] == 'scope_end')
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=workflow.definition) == payload


def test_guard_exhaustion_keeps_failed_component_and_graph_partial(monkeypatch):
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')

    async def complete(request):
        draft = {'original_input': 'hello', 'route': 'skip', 'ready': False, 'final_output': 'hello'}
        return LLMResult(Message('assistant', (TextContent(json.dumps(draft)),)))

    workflow = build_contrived(ROOT, llm=complete, selection='controlled', trial='local')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal

    with pytest.raises(WorkflowError, match='exhausted 2 passes'):
        asyncio.run(workflow.arun({'user_input': 'hello'}))
    payload = journal.finalize()

    assert [event['accepted'] for event in payload['events'] if event['event'] == 'guard'] == [False, False]
    assert {event['owner'] for event in payload['events']
            if event['event'] == 'scope_end' and event['outcome'] == 'failed'} == {'first', 'GRAPH'}
    assert payload['completeness'] == 'partial'
    assert validate_journal(payload, expected_definition=workflow.definition) == payload


def test_terminal_graph_outcome_does_not_redefine_captured_route_completeness(completed_one_pass):
    definition, journal = completed_one_pass
    graph_end = next(event for event in journal._events
                     if event['event'] == 'scope_end' and event['owner'] == 'GRAPH')
    graph_end['outcome'] = 'failed'

    payload = journal.finalize()

    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload
