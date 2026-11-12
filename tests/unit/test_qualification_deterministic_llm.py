"""The qualification-only provider answers the bundled demo flow's own model calls."""
import asyncio
from hashlib import sha256
from pathlib import Path
import runpy

import pytest

from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.tools import ToolResult, ToolSpec
from api_agent.workflows.definition import load_definition
from api_agent.workflows.demo_application import UsefulDemoApplication

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / 'qualification/deterministic_llm.py'))


class Owner:
    def __init__(self):
        self.queries = []

    async def describe(self):
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                  'required': ['query'], 'additionalProperties': False}
        return ToolSpec('lookup', 'Read data', schema, {'type': 'object'}, sha256(b'schema').hexdigest())

    async def validate(self, prepared):
        pass

    async def invoke(self, prepared):
        self.queries.append(prepared.arguments['query'])
        return ToolResult('success', {'greeting': 'synthetic-greeting'})

    async def aclose(self):
        pass


class Provider:
    """Adapts the deterministic provider to the application's selected-model interface."""
    def __init__(self):
        self.provider = MODULE['create']({}, {})
        self.results = []

    async def complete(self, request, *, context):
        result = await self.provider.acomplete(request)
        self.results.append(result)
        return result


def _run(request):
    from api_agent.tooling.runtime import OwnerTools, bind_workflow_tools
    owner, llm = Owner(), Provider()
    definition = load_definition(ROOT / 'demo/simple/flow.md.html', base_root=ROOT,
                                 pack_root=ROOT / 'demo', entry_root='base')
    application = UsefulDemoApplication(definition)
    with bind_workflow_tools(OwnerTools(definition, owner)):
        result = asyncio.run(application.complete(request, llm=llm, context=OperationContext('unit')))
    return result, owner, llm


def test_the_demo_flow_plans_reviews_queries_and_renders_the_tool_result_without_inference():
    request = LLMRequest('deterministic', CompletionRequest((Message('user', (TextContent('Greet?'),)),)))

    result, owner, llm = _run(request)

    assert owner.queries == ['{ greeting }']
    assert result.payload.content[0].text == 'deterministic demo answer: {"greeting": "synthetic-greeting"}'
    # Plan and review are structured requests answered as prompted JSON; render is plain text.
    assert [item.response and item.response.actual_mode for item in llm.results] == [
        'prompted_json', 'prompted_json', None]


def _complete(text):
    provider = MODULE['create']({}, {})
    request = LLMRequest('deterministic', CompletionRequest((Message('user', (TextContent(text),)),)))
    return asyncio.run(provider.acomplete(request))


def test_other_requests_keep_the_fixed_chat_reply():
    result = _complete('hello')

    assert result.payload.content[0].text == 'deterministic response' and result.response is None


def test_the_controlled_failure_request_still_fails():
    with pytest.raises(Exception, match='controlled deterministic failure'):
        _complete('controlled-error')
