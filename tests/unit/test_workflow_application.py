"""Bundled chat graph preserves the public completion request and result contract."""
import asyncio
from pathlib import Path

import pytest

from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import GenerationOptions, LLMResult, Message, TextContent, Usage
from api_agent.workflows.application import build
from api_agent.workflows.execution import WorkflowError

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = [
    {'role': 'system', 'content': 'Keep the answer short.'},
    {'role': 'user', 'content': 'Earlier question'},
    {'role': 'assistant', 'content': 'Earlier answer'},
    {'role': 'user', 'content': 'Respond with exactly: Ok'},
]


def test_single_renderer_preserves_history_selection_options_identity_and_metadata():
    requests = []

    async def complete(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('Ok'),)),
                         Usage(input_tokens=7, output_tokens=2, cache_write_tokens=1),
                         actual_model='gpt-test-revision', service_tier='priority',
                         serving_revision='revision-1')

    workflow = build(ROOT, llm=complete, selection='gpt-test', request_id='session-1',
                     generation=GenerationOptions(reasoning_effort='low', cache_policy='medium',
                                                  max_output_tokens=128))
    result = asyncio.run(workflow.arun({'messages': MESSAGES}))['renderer']

    assert workflow.definition.graph_id == 'simple_chat'
    assert [step.id for step in workflow.layout.steps] == ['renderer']
    assert len(requests) == 1
    assert request_to_wire(requests[0]) == {
        'selected_llm': 'gpt-test', 'request_id': 'session-1',
        'messages': [{'role': item['role'], 'text': item['content']} for item in MESSAGES],
        'max_output_tokens': 128, 'cache_policy': 'medium', 'reasoning_effort': 'low',
    }
    assert result == {
        'response_text': 'Ok', 'outcome': 'completed', 'selected_model': 'gpt-test',
        'actual_model': 'gpt-test-revision', 'service_tier': 'priority',
        'serving_revision': 'revision-1',
        'usage': {'input_tokens': 7, 'output_tokens': 2, 'cached_input_tokens': None,
                  'reasoning_tokens': None, 'cache_write_tokens': 1},
    }


def test_renderer_preserves_optional_billing_metadata():
    requests = []
    billing = {'usd': 0.0025, 'request_id': 'request-1', 'generation_id': 'generation-1'}

    async def complete(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(), billing=billing)

    result = asyncio.run(build(ROOT, llm=complete, selection='gpt-test').arun(
        {'messages': [{'role': 'user', 'content': 'Hello'}]}))['renderer']
    assert result['response_text'] == 'Ok'
    assert result['billing'] == billing
    assert requests[0].request_id


@pytest.mark.parametrize('outcome,text', [('refused', 'No'), ('completed', '')])
def test_renderer_rejects_incomplete_or_empty_result(outcome, text):
    async def complete(request):
        return LLMResult(Message('assistant', (TextContent(text),)), outcome=outcome)

    workflow = build(ROOT, llm=complete, selection='gpt-test')
    with pytest.raises(WorkflowError, match='did not complete|no text'):
        asyncio.run(workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]}))


def test_builder_uses_executing_source_checkout_when_root_is_omitted(monkeypatch):
    from api_agent.workflows import application

    monkeypatch.setattr(application, 'bundled_root', lambda: ROOT)
    workflow = application.build(llm=None, selection='gpt-test')
    assert workflow.definition.entry.samefile(ROOT / 'flow.md.html')

    with pytest.raises(FileNotFoundError, match='Selected application flow is missing'):
        application.build(ROOT / 'missing', llm=None, selection='gpt-test')


def test_builder_uses_selected_relocated_package_root(tmp_path, monkeypatch):
    from shutil import copytree
    from api_agent.workflows import application

    package = tmp_path / 'foreign/api_agent'
    package.mkdir(parents=True)
    (package / 'flow.md.html').write_bytes((ROOT / 'flow.md.html').read_bytes())
    copytree(ROOT / 'workflows', package / 'workflows')
    copytree(ROOT / 'demo/simple', package / 'demo/simple')
    monkeypatch.setattr(application, 'bundled_root', lambda: package)

    workflow = application.build(llm=None, selection='gpt-test')

    assert workflow.definition.entry == package / 'flow.md.html'


@pytest.mark.parametrize('generation', [{'reasoning_effort': 'low'}, GenerationOptions(temperature=0.5)])
def test_builder_rejects_generation_not_supported_by_chat_contract(generation):
    with pytest.raises(ValueError, match='accepted chat GenerationOptions'):
        build(ROOT, llm=None, selection='gpt-test', generation=generation)
