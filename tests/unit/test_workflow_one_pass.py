"""A one-pass component preserves execution while keeping guarded retries strict."""
import asyncio
from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.workflows.application import build
from api_agent.workflows.definition import load_definition
from api_agent.workflows.execution import diagram_layout
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.workflows.layout import LayoutError

ROOT = Path(__file__).resolve().parents[2]


def test_one_pass_executes_once_and_journal_requires_the_complete_child_route():
    calls = []
    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent('Ok'),)))
    workflow = build(ROOT, llm=complete, selection='example')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal

    values = asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]}))
    payload = journal.finalize()

    assert len(calls) == 1
    assert values['llm_call']['response_text'] == 'Ok'
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload) == payload
    assert [row['component'] for row in payload['events'] if row['event'] == 'attempt'] == ['agent']
    assert not any(row['event'] == 'guard' for row in payload['events'])
    assert 'reject, at most' not in diagram_layout(workflow.layout)
    assert [(step.id, step.parent) for step in workflow.layout.steps] == [('agent', None), ('llm_call', 'agent')]


@pytest.mark.parametrize('maximum', ['0', '2', '01', 'invalid'])
def test_unguarded_component_rejects_any_budget_except_exactly_one(tmp_path, maximum):
    shutil.copytree(ROOT / 'workflows', tmp_path / 'workflows')
    shutil.copytree(ROOT / 'demo/simple', tmp_path / 'demo/simple')
    shutil.copyfile(ROOT / 'flow.md.html', tmp_path / 'flow.md.html')
    component = tmp_path / 'workflows/components/agent.md.html'
    component.write_text(component.read_text().replace('| agent | 1 | - |', '| agent | ' + maximum + ' | - |'))

    with pytest.raises(LayoutError, match='positive integer|exactly one pass'):
        load_definition(tmp_path / 'flow.md.html', base_root=tmp_path, pack_root=tmp_path)


def test_one_pass_propagates_child_failure_without_retry_or_success():
    calls, events = [], []
    async def complete(request):
        calls.append(request)
        raise RuntimeError('controlled failure')
    workflow = build(ROOT, llm=complete, selection='example', observe=events.append)

    with pytest.raises(RuntimeError, match='controlled failure'):
        asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]}))

    assert len(calls) == 1
    assert not any(event['event'] == 'guard' for event in events)
    assert any(event['event'] == 'scope_end' and event.get('owner') == 'agent'
               and event['outcome'] == 'failed' for event in events)


def test_one_pass_restores_outer_context_without_changing_child_values(monkeypatch):
    workflow = build(ROOT, llm=None, selection='example')
    outer = dict(values={'START': {}, 'before': {'answer': 1}}, ancestry=(('outer', 2, 3),),
                 feedback={'outer': True}, previous={'before': 1}, attempt_id='outer-attempt', visit_id='outer-visit')
    observed = []
    class Child:
        async def ainvoke(self, state, config):
            observed.append(deepcopy(state))
            return dict(state, values={**state['values'], 'llm_call': {'answer': 'kept'}})
    monkeypatch.setattr(workflow, '_compile', lambda parent: Child())
    execute = workflow._component(workflow.layout.steps[0])

    result = asyncio.run(execute(deepcopy(outer)))

    assert len(observed) == 1
    assert observed[0]['ancestry'] == (*outer['ancestry'], ('agent', 1, 1))
    assert observed[0]['previous'] == {} and observed[0]['feedback'] == {}
    for key in ('ancestry', 'feedback', 'previous', 'attempt_id'):
        assert result[key] == outer[key]
    assert result['values']['llm_call'] == {'answer': 'kept'}
    assert result['values']['before'] == {'answer': 1}


def test_one_pass_component_deadline_cancels_owned_call_without_retry():
    calls, settled, events = [], [], []
    async def complete(request):
        calls.append(request)
        try:
            await asyncio.sleep(10)
        finally:
            settled.append(True)
    workflow = build(ROOT, llm=complete, selection='example', observe=events.append)
    workflow.timeouts['agent'] = 0.01

    with pytest.raises(TimeoutError):
        asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]}))

    assert len(calls) == 1 and settled == [True]
    assert any(event['event'] == 'scope_end' and event.get('owner') == 'agent'
               and event['outcome'] == 'failed' for event in events)
    assert not any(event['event'] == 'guard' for event in events)
