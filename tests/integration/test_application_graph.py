"""The bundled selected flow compiles and executes its real llm_call node."""
import asyncio
from pathlib import Path

import pytest

from api_agent.providers.llm import GenerationOptions, LLMResult, Message, TextContent, Usage
from api_agent.workflows.application import build
from api_agent.workflows.evidence import publish

pytestmark = [pytest.mark.feature('Application workflow'), pytest.mark.credential_authorities('workflow')]
ROOT = Path(__file__).resolve().parents[2]


def test_selected_application_flow_executes_llm_call():
    requests = []
    events = []

    async def complete(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('Ok'),)),
                         Usage(input_tokens=8, output_tokens=2), actual_model='selected-revision')

    workflow = build(ROOT, llm=complete, selection='selected-model',
                     generation=GenerationOptions(reasoning_effort='low', cache_policy='medium'),
                     observe=events.append)
    result = asyncio.run(workflow.arun({'messages': [
        {'role': 'user', 'content': 'Earlier question'},
        {'role': 'assistant', 'content': 'Earlier answer'},
        {'role': 'user', 'content': 'Respond with exactly: Ok'},
    ]}))
    publish({'graph_id': workflow.definition.graph_id,
             'definition_id': workflow.definition.definition_id, 'events': events})

    assert workflow.definition.entry == ROOT / 'flow.md.html'
    assert workflow.definition.graph_id == 'simple_chat'
    assert [event['node'] for event in events if event['event'] == 'node'] == ['llm_call']
    assert len(requests) == 1
    assert [(message.role, message.content[0].text) for message in requests[0].payload.messages] == [
        ('user', 'Earlier question'), ('assistant', 'Earlier answer'),
        ('user', 'Respond with exactly: Ok')]
    assert result['llm_call']['response_text'] == 'Ok'
    assert result['llm_call']['actual_model'] == 'selected-revision'
    assert result['llm_call']['usage']['input_tokens'] == 8
