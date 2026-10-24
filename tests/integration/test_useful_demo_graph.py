"""The selected useful demo compiles and runs with controlled providers."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.tools import ToolResult, ToolSpec
from api_agent.workflows.definition import load_definition
from api_agent.workflows.demo_application import UsefulDemoApplication
from api_agent.workflows.evidence import publish
from api_agent.workflows.execution import WorkflowError

pytestmark = pytest.mark.feature('Useful API demo')
ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'demo/simple/flow.md.html'
PLAN = json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''})


class ControlledOwner:
    def __init__(self, events):
        self.events = events

    async def describe(self):
        self.events.append('describe')
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                  'required': ['query'], 'additionalProperties': False}
        return ToolSpec('lookup', 'Read data', schema, {'type': 'object'}, sha256(b'schema').hexdigest())

    async def validate(self, prepared):
        self.events.append('validate')
        if prepared.arguments != {'query': '{ item { name } }'}:
            raise ValueError('invalid query')

    async def invoke(self, prepared):
        self.events.append('invoke')
        return ToolResult('partial', {'item': {'name': 'Ada'}}, ({'message': 'optional field failed'},))

    async def aclose(self):
        pass


class ControlledLLM:
    def __init__(self, outputs, events):
        self.outputs = iter(outputs)
        self.events = events
        self.requests = []

    async def complete(self, request, *, context):
        output = next(self.outputs)
        self.requests.append(request)
        self.events.append('llm')
        return LLMResult(Message('assistant', (TextContent(output),)), Usage(2, 3))


def run_demo(verdicts, *, render=None):
    events = []
    definition = load_definition(ENTRY, base_root=ROOT, pack_root=ROOT / 'demo', entry_root='base')
    app = UsefulDemoApplication(definition, tool_owner=ControlledOwner(events))
    outputs = [PLAN]
    for index, verdict in enumerate(verdicts):
        outputs.append(json.dumps(verdict))
        if index + 1 < len(verdicts):
            outputs.append(PLAN)
    if render is not None:
        outputs.append(render)
    llm = ControlledLLM(outputs, events)
    request = LLMRequest('selected-model', CompletionRequest((Message('user', (TextContent('Who?'),)),)))
    return app, llm, events, request


def verdict(**changes):
    return dict(correct=True, grounded=True, relevant=True, preference=True) | changes


def test_selected_graph_relaxes_preference_only_after_retry_and_renders_partial_evidence():
    app, llm, events, request = run_demo([verdict(preference=False)] * 2, render='Ada; optional field failed')

    result = asyncio.run(app.complete(request, llm=llm, context=OperationContext('integration')))
    rendered_input = json.loads(llm.requests[-1].payload.messages[-1].content[0].text)
    publish({'graph_id': app.definition.graph_id, 'events': [
        {'event': event, 'attempt': index} for index, event in enumerate(events)]})

    assert app.definition.entry == ENTRY
    assert app.definition.graph_id == 'useful_api_demo'
    assert result.payload.content[0].text == 'Ada; optional field failed'
    assert events == ['describe', 'llm', 'validate', 'llm', 'llm', 'validate', 'llm', 'invoke', 'llm']
    assert [row.selected_llm for row in llm.requests] == ['selected-model'] * 5
    assert rendered_input['tool']['status'] == 'partial'
    assert rendered_input['tool']['data'] == {'item': {'name': 'Ada'}}
    assert rendered_input['tool']['errors'] == [{'message': 'optional field failed'}]


@pytest.mark.parametrize('failed', ['correct', 'grounded'])
def test_final_pass_still_blocks_correctness_and_grounding(failed):
    app, llm, events, request = run_demo([verdict(preference=False, **{failed: False})] * 2)

    with pytest.raises(WorkflowError, match='exhausted'):
        asyncio.run(app.complete(request, llm=llm, context=OperationContext('integration')))

    assert events == ['describe', 'llm', 'validate', 'llm', 'llm', 'validate', 'llm']
    assert len(llm.requests) == 4
    publish({'graph_id': app.definition.graph_id, 'events': [
        {'event': event, 'attempt': index} for index, event in enumerate(events)]})


def test_selected_graph_rejects_removed_review_guard_before_execution():
    app, _, _, _ = run_demo([verdict()])
    layout = app.definition.layout
    tables = dict(layout.tables)
    tables['Components'] = tuple(dict(row, Guard='-') for row in tables['Components'])
    altered = replace(app.definition, layout=replace(layout, tables=tables))

    with pytest.raises(ContractError, match='useful demo contract'):
        UsefulDemoApplication(altered, tool_owner=ControlledOwner([]))
