"""Controlled selected Workflow proof for the useful demo."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage, Tool
from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import request_from_wire, request_to_wire
from api_agent.providers.tools import ToolResult, ToolSpec
from api_agent.workflows.definition import load_definition
from api_agent.workflows.demo_application import UsefulDemoApplication
from api_agent.workflows.execution import WorkflowError


ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'demo/simple/flow.md.html'
INPUT_SCHEMA = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                'required': ['query'], 'additionalProperties': False}


class Owner:
    def __init__(self):
        self.events = []
        self.spec = ToolSpec('lookup', 'Read data', INPUT_SCHEMA, {'type': 'object'},
                             sha256(b'schema').hexdigest())

    async def describe(self):
        self.events.append('describe')
        return self.spec

    async def validate(self, prepared):
        self.events.append('validate')
        if prepared.arguments['query'] != '{ item { name } }':
            raise ValueError('invalid query')

    async def invoke(self, prepared):
        self.events.append('invoke')
        return ToolResult('partial', {'item': {'name': 'Ada'}}, ({'message': 'optional field failed'},))

    async def aclose(self):
        self.events.append('close')


class LLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def complete(self, request, *, context):
        self.requests.append(request)
        return LLMResult(Message('assistant', (TextContent(self.outputs.pop(0)),)), Usage(2, 3))


def application(owner):
    definition = load_definition(ENTRY, base_root=ROOT, pack_root=ROOT / 'demo', entry_root='base')
    return UsefulDemoApplication(definition, tool_owner=owner)


def request():
    return LLMRequest('chosen-model', CompletionRequest((Message('user', (TextContent('Who?'),)),)))


def verdict(*, correct=True, grounded=True, relevant=True, preference=True, details=None):
    if details is None:
        details = '' if all((correct, grounded, relevant, preference)) else 'A synthetic review check failed.'
    return json.dumps(dict(correct=correct, grounded=grounded, relevant=relevant, preference=preference, details=details))


def test_verified_query_executes_after_review_and_renderer_gets_partial_result():
    owner = Owner()
    llm = LLM([json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''}),
               verdict(), 'Ada (one optional field failed)'])
    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert ''.join(part.text for part in result.payload.content) == 'Ada (one optional field failed)'
    assert owner.events == ['describe', 'validate', 'invoke']
    assert [row.selected_llm for row in llm.requests] == ['chosen-model'] * 3
    assert 'optional field failed' in ''.join(part.text for part in llm.requests[-1].payload.messages[-1].content)


def test_completed_node_reasoning_is_delivered_while_next_node_is_still_blocked():
    from api_agent.providers.presentation import summary_delivery

    async def run():
        reviewer_started, release = asyncio.Event(), asyncio.Event()
        observed = []

        class ReasoningLLM(LLM):
            async def complete(self, value, *, context):
                if len(self.requests) == 1:
                    reviewer_started.set()
                    await release.wait()
                completed = await super().complete(value, context=context)
                return replace(completed, reasoning_summary=f'Completed call {len(self.requests)}.')

        llm = ReasoningLLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}),
                            verdict(), 'Hello'])

        async def sink(text):
            observed.append(text)

        with summary_delivery(sink):
            task = asyncio.create_task(application(Owner()).complete(request(), llm=llm,
                context=OperationContext('request')))
            try:
                await asyncio.wait_for(reviewer_started.wait(), 5)
                assert observed == ['Completed call 1.']
                assert not task.done()
            finally:
                release.set()
                final = await task
        assert ''.join(observed) == 'Completed call 1.\n\nCompleted call 2.\n\nCompleted call 3.'
        assert final.payload.content[0].text == 'Hello'

    asyncio.run(run())


def test_fenced_prompted_planner_and_native_reviewer_share_validated_json_consumption():
    from api_agent.llm_providers.response_policy import resolve_response
    from api_agent.providers.rich_inference import async_validate_result

    class PolicyLLM(LLM):
        async def complete(self, request, *, context):
            result = await super().complete(request, context=context)
            if request.payload.response is None:
                return result
            _, evidence = resolve_response(request, lambda actual:
                None if actual == ('prompted_json' if len(self.requests) == 1 else 'structured')
                else 'unsupported_schema')
            return await async_validate_result(request, replace(result, response=evidence))

    owner = Owner()
    candidate = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'})
    llm = PolicyLLM(['`````json\n````\n```json\n' + candidate + '\n```\n````\n`````',
                    verdict(), 'Hello'])

    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))

    assert result.payload.content[0].text == 'Hello'
    assert len(llm.requests) == 3
    assert owner.events == ['describe']
    assert llm.requests[0].payload.response.fallback == 'allow'
    assert llm.requests[1].payload.response.fallback == 'allow'


def test_same_declared_consumer_uses_synthetic_rest_owner_unchanged():
    class RestOwner(Owner):
        def __init__(self):
            super().__init__()
            self.spec = ToolSpec('rest.read', 'Read a resource',
                {'type': 'object', 'properties': {'path': {'type': 'string'}},
                 'required': ['path'], 'additionalProperties': False},
                {'type': 'object'}, sha256(b'rest-schema').hexdigest())
        async def validate(self, prepared):
            self.events.append('validate')
            assert prepared.arguments == {'path': '/items'}
        async def invoke(self, prepared):
            self.events.append('invoke')
            return ToolResult('success', {'items': ['Ada']})
    owner = RestOwner()
    llm = LLM([json.dumps({'use_tool': True, 'arguments': {'path': '/items'}, 'answer': ''}),
               verdict(), 'Ada'])
    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert ''.join(part.text for part in result.payload.content) == 'Ada'
    assert owner.events == ['describe', 'validate', 'invoke']
    assert '"path"' in ''.join(part.text for part in llm.requests[0].payload.messages[1].content)


def test_expected_remote_failure_reaches_final_renderer_as_bounded_status():
    class FailingRemote(Owner):
        async def invoke(self, prepared):
            self.events.append('invoke')
            return ToolResult('failure', None, diagnostic='http_failure')
    owner = FailingRemote()
    llm = LLM([json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'},
                           'answer': ''}), verdict(), 'The data service failed.'])
    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    rendered_prompt = ''.join(part.text for part in llm.requests[-1].payload.messages[-1].content)
    assert result.payload.content[0].text == 'The data service failed.'
    assert '"status": "failure"' in rendered_prompt
    assert 'http_failure' in rendered_prompt
    assert 'untrusted upstream stack trace' not in rendered_prompt
    assert owner.events == ['describe', 'validate', 'invoke']


def test_each_turn_uses_fresh_schema_view_and_closes_only_that_view():
    class SchemaView(Owner):
        def __init__(self, field):
            super().__init__()
            self.spec = ToolSpec('graphql.query', 'Read data',
                {'type': 'object', 'properties': {'query': {'type': 'string',
                  'description': f'type Query {{ {field}: String }}'}},
                 'required': ['query'], 'additionalProperties': False},
                {'type': 'object'}, sha256(field.encode()).hexdigest())
            self.closed = False
        async def aclose(self):
            self.closed = True
    class SharedOwner:
        def __init__(self):
            self.fields = iter(['oldField', 'newField'])
            self.views = []
        def for_invocation(self):
            view = SchemaView(next(self.fields))
            self.views.append(view)
            return view
    owner = SharedOwner()
    app = application(owner)
    plan = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'})
    llm = LLM([plan, verdict(), 'Hello', plan, verdict(), 'Hello'])
    async def run():
        await app.complete(request(), llm=llm, context=OperationContext('first'))
        await app.complete(request(), llm=llm, context=OperationContext('second'))
    asyncio.run(run())
    first = ''.join(part.text for part in llm.requests[0].payload.messages[1].content)
    second = ''.join(part.text for part in llm.requests[3].payload.messages[1].content)
    assert 'oldField' in first and 'newField' not in first
    assert 'newField' in second and 'oldField' not in second
    assert [view.closed for view in owner.views] == [True, True]


def test_optional_chat_skips_tool_without_skipping_grounding_review():
    owner = Owner()
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}),
               verdict(), 'Hello'])
    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert ''.join(part.text for part in result.payload.content) == 'Hello'
    assert owner.events == ['describe']
    assert len(llm.requests) == 3


@pytest.mark.parametrize('length', [100, 101, 200])
def test_wire_valid_request_identity_completes_no_tool_turn(length):
    owner = Owner()
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}),
               verdict(), 'Hello'])
    selected = replace(request(), request_id='x' * length)
    admitted = request_from_wire(request_to_wire(selected))

    result = asyncio.run(application(owner).complete(admitted, llm=llm,
        context=OperationContext('request')))

    assert result.payload.content[0].text == 'Hello'
    assert admitted.request_id == 'x' * length
    assert owner.events == ['describe']


def test_grounding_failure_blocks_both_passes_and_execution():
    owner = Owner()
    plan = json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''})
    details = 'The proposed query invents a field.\nUse a field from the supplied schema.'
    llm = LLM([plan, verdict(grounded=False, details=details), plan, verdict(grounded=False, details=details)])
    with pytest.raises(WorkflowError, match='exhausted'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe', 'validate', 'validate']
    retry = json.loads(llm.requests[2].payload.messages[-1].content[0].text)
    assert retry['feedback']['previous_candidate'] == json.loads(plan)
    assert retry['feedback']['failed_checks'] == ['grounded']
    assert retry['feedback']['details'] == details


def test_preference_relaxes_only_final_pass():
    owner = Owner()
    plan = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'})
    llm = LLM([plan, verdict(preference=False), plan, verdict(preference=False), 'Hello'])
    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert ''.join(part.text for part in result.payload.content) == 'Hello'
    assert len(llm.requests) == 5
    assert owner.events == ['describe']


def test_invalid_query_retries_without_calling_relevance_or_execution():
    owner = Owner()
    bad = json.dumps({'use_tool': True, 'arguments': {'query': 'wrong'}, 'answer': ''})
    llm = LLM([bad, bad])
    with pytest.raises(WorkflowError, match='exhausted'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe', 'validate', 'validate']
    assert len(llm.requests) == 2
    retry = json.loads(llm.requests[1].payload.messages[-1].content[0].text)
    assert retry['feedback']['previous_candidate'] == json.loads(bad)
    assert retry['feedback']['failed_checks'] == []


def test_rejected_tool_cannot_rewrite_the_previous_planner_candidate():
    class MutatingOwner(Owner):
        async def validate(self, prepared):
            self.events.append('validate')
            prepared.arguments['query'] = 'owner-mutated'
            raise ValueError('reject changed arguments')

    owner = MutatingOwner()
    original = {'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''}
    corrected = {'use_tool': False, 'arguments': {}, 'answer': 'Hello'}
    llm = LLM([json.dumps(original), json.dumps(corrected), verdict(), 'Hello'])

    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))

    retry = json.loads(llm.requests[1].payload.messages[-1].content[0].text)
    assert retry['feedback']['previous_candidate'] == original
    assert retry['feedback']['failed_checks'] == []
    assert result.payload.content[0].text == 'Hello'
    assert owner.events == ['describe', 'validate']
    assert len(llm.requests) == 4


@pytest.mark.parametrize('bad', [
    'not json', '{}',
    json.dumps({'use_tool': False, 'arguments': {'query': 'wrong'}, 'answer': ''}),
    pytest.param(json.dumps({'use_tool': True, 'arguments': {'path': 'x' * 16384}, 'answer': ''}), id='oversized-path'),
])
def test_invalid_planner_output_stops_before_tool_and_renderer(bad):
    owner = Owner()
    llm = LLM([bad])
    with pytest.raises(WorkflowError, match='Planner returned invalid'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe']
    assert len(llm.requests) == 1


@pytest.mark.parametrize('bad', ['not json', '{}'])
def test_invalid_reviewer_output_stops_before_tool(bad):
    owner = Owner()
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}), bad])
    with pytest.raises(WorkflowError, match='Reviewer returned invalid'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe']


@pytest.mark.parametrize('details', [None, 0, False, [], {}, '', ' \n\t', 'x' * 4097])
def test_rejected_verdict_requires_bounded_specific_details(details):
    owner = Owner()
    invalid = json.loads(verdict(correct=False))
    invalid['details'] = details
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Candidate'}), json.dumps(invalid)])

    with pytest.raises(WorkflowError, match='invalid verdict details'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))

    assert len(llm.requests) == 2
    assert owner.events == ['describe']


def test_missing_review_details_reject_even_with_valid_boolean_controls():
    invalid = json.loads(verdict(correct=False))
    del invalid['details']
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Candidate'}), json.dumps(invalid)])

    with pytest.raises(WorkflowError, match='invalid verdict details'):
        asyncio.run(application(Owner()).complete(request(), llm=llm, context=OperationContext('request')))

    assert len(llm.requests) == 2


def test_maximum_review_details_reach_retry_without_truncation():
    details = 'λ' * 4096
    plan = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Candidate'})
    llm = LLM([plan, verdict(correct=False, details=details), plan, verdict(), 'Answer'])

    result = asyncio.run(application(Owner()).complete(request(), llm=llm, context=OperationContext('request')))

    assert result.payload.content[0].text == 'Answer'
    assert json.loads(llm.requests[2].payload.messages[-1].content[0].text)['feedback']['details'] == details
    assert len(llm.requests) == 5


def test_correctness_failure_stays_blocking_on_final_pass():
    owner = Owner()
    plan = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'})
    llm = LLM([plan, verdict(correct=False, preference=False), plan,
               verdict(correct=False, preference=False)])
    with pytest.raises(WorkflowError, match='exhausted'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe']


def test_renderer_empty_result_fails_after_valid_review():
    owner = Owner()
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}), verdict(), ''])
    with pytest.raises(WorkflowError, match='Renderer returned no text'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe']


def test_noncompleted_provider_result_fails_before_candidate_acceptance():
    owner = Owner()
    class Incomplete(LLM):
        async def complete(self, request, *, context):
            self.requests.append(request)
            return LLMResult(Message('assistant', (TextContent(''),)), outcome='length')
    llm = Incomplete([])
    with pytest.raises(WorkflowError, match='did not complete'):
        asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))
    assert owner.events == ['describe']


@pytest.mark.parametrize('invalid', [
    LLMRequest('chosen-model', CompletionRequest((Message('tool'),))),
    LLMRequest('chosen-model', CompletionRequest((Message('assistant', continuation='more'),))),
])
def test_unsupported_messages_are_rejected_before_provider(invalid):
    owner = Owner()
    llm = LLM([])
    with pytest.raises(ContractError):
        asyncio.run(application(owner).complete(invalid, llm=llm, context=OperationContext('request')))
    assert llm.requests == []


def test_missing_owner_is_rejected_before_provider():
    app = application(Owner())
    app.tool_owner = None
    llm = LLM([])
    with pytest.raises(ContractError, match='tool owner'):
        asyncio.run(app.complete(request(), llm=llm, context=OperationContext('request')))
    assert llm.requests == []


def test_selected_graph_requires_review_guard_and_optional_branch():
    app = application(Owner())
    layout = app.definition.layout
    changed = dict(layout.tables)
    changed['Edges'] = ()
    invalid = replace(app.definition, layout=replace(layout, tables=changed))
    with pytest.raises(ContractError, match='useful demo contract'):
        UsefulDemoApplication(invalid)


def test_selected_model_is_immutable_across_declared_llm_calls(monkeypatch):
    from api_agent.workflows import demo_application
    app = application(Owner())
    class Graph:
        def __init__(self, *args, services, **kwargs):
            self.services = services
        async def arun(self, inputs):
            altered = replace(request(), selected_llm='another-model')
            await self.services['llm'](altered)
    monkeypatch.setattr(demo_application, 'Workflow', Graph)
    llm = LLM([])
    with pytest.raises(WorkflowError, match='changed selected model'):
        asyncio.run(app.complete(request(), llm=llm, context=OperationContext('request')))
    assert llm.requests == []


def test_render_result_must_match_declared_output_and_observations_close(monkeypatch):
    from api_agent.workflows import demo_application
    app = application(Owner())
    closed = []
    class Observations:
        def create(self, definition):
            return SimpleNamespace(close=lambda: closed.append(True))
    app.observations = Observations()
    class Graph:
        def __init__(self, *args, services, **kwargs):
            self.services = services
        async def arun(self, inputs):
            self.services['rendered_result'] = LLMResult(Message('assistant', (TextContent('real'),)))
            return {'render': {'selected_model': 'chosen-model', 'response_text': 'different'}}
    monkeypatch.setattr(demo_application, 'Workflow', Graph)
    with pytest.raises(WorkflowError, match='did not render'):
        asyncio.run(app.complete(request(), llm=LLM([]), context=OperationContext('request')))
    assert closed == [True]


@pytest.mark.parametrize('invalid', [
    LLMRequest('chosen-model', CompletionRequest((Message('user', (TextContent('x'),)),),
               tools=(Tool('lookup', 'Read', '{"type":"object"}'),))),
    LLMRequest('chosen-model', CompletionRequest((Message('user', (TextContent('x'),)),),
               generation=GenerationOptions(temperature=0.5))),
])
def test_caller_cannot_inject_tools_or_generation_override(invalid):
    llm = LLM([])
    with pytest.raises(ContractError):
        asyncio.run(application(Owner()).complete(invalid, llm=llm, context=OperationContext('request')))
    assert llm.requests == []


def test_explicit_reasoning_effort_keeps_selected_model():
    owner = Owner()
    req = replace(request(), payload=replace(request().payload,
                  generation=GenerationOptions(reasoning_effort='low')))
    llm = LLM([json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}), verdict(), 'Hello'])
    asyncio.run(application(owner).complete(req, llm=llm, context=OperationContext('request')))
    assert all(row.payload.generation.reasoning_effort == 'low' for row in llm.requests)
    assert all(row.selected_llm == 'chosen-model' for row in llm.requests)


def test_tool_node_rejects_missing_grant_and_records_failed_actual_call():
    app = application(Owner())
    invoke = app.registry['execute_tool'].invoke
    observations = []
    context = SimpleNamespace(services={}, record_call=lambda **row: observations.append(row))
    with pytest.raises(WorkflowError, match='lacked accepted verification'):
        asyncio.run(invoke({}, context))
    assert observations == []
    class FailingGate:
        async def execute(self, grant):
            raise RuntimeError('transport failed')
    context.services.update(verified_call=object())
    context.tools = {'primary': FailingGate()}
    with pytest.raises(RuntimeError, match='transport failed'):
        asyncio.run(invoke({}, context))
    assert len(observations) == 1
    assert observations[0]['outcome'] == 'failed'


def test_plain_text_check_still_rejects_tool_role_if_wire_layer_changes(monkeypatch):
    from api_agent.workflows import demo_application
    async def admitted(request):
        return None
    monkeypatch.setattr(demo_application, 'async_request_to_wire', admitted)
    invalid = LLMRequest('chosen-model', CompletionRequest((Message('tool'),)))
    with pytest.raises(ContractError, match='plain text'):
        asyncio.run(application(Owner()).complete(invalid, llm=LLM([]), context=OperationContext('request')))


@pytest.mark.parametrize('output,reason', [
    (LLMResult(Message('assistant', (TextContent('partial'),)), outcome='length'), 'did not complete'),
    (LLMResult(Message('assistant', (TextContent(''),))), 'no text'),
])
def test_existing_one_call_node_rejects_incomplete_or_empty_model_output(output, reason):
    from api_agent.workflows.local_application import LocalChatApplication
    definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    native = LocalChatApplication(definition).registry['llm_call'].invoke
    async def llm(value):
        assert value.selected_llm == 'chosen-model'
        assert value.payload.generation.reasoning_effort == 'low'
        return output
    context = SimpleNamespace(services={'selection': 'chosen-model',
        'generation': GenerationOptions(reasoning_effort='low'), 'llm': llm})
    with pytest.raises(WorkflowError, match=reason):
        asyncio.run(native({'messages': [{'role': 'user', 'content': 'Hello'}]}, context))


def test_named_tool_binding_runs_selected_port_and_closes_borrowed_view():
    owner = Owner()
    graph = application(None)
    from api_agent.tooling.runtime import OwnerTools
    class OwnerWithView(Owner):
        def for_invocation(self):
            return owner
    parent = OwnerWithView()
    graph.tools = OwnerTools(graph.definition, parent)
    llm = LLM([json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''}),
               verdict(), 'Ada'])

    result = asyncio.run(graph.complete(request(), llm=llm, context=OperationContext('request')))

    assert ''.join(part.text for part in result.payload.content) == 'Ada'
    assert owner.events == ['describe', 'validate', 'invoke', 'close']


def test_compatibility_owner_tool_evidence_uses_captured_instance_id(monkeypatch):
    from api_agent.workflows.tool_observations import ToolObservation
    recorded = []
    original = ToolObservation.capture
    async def capture(self, direction, values, schema, **kwargs):
        if direction == 'input':
            recorded.append((self.alias, self.instance, values['tool_instance']))
        return await original(self, direction, values, schema, **kwargs)
    monkeypatch.setattr(ToolObservation, 'capture', capture)
    llm = LLM([json.dumps({'use_tool': True, 'arguments': {'query': '{ item { name } }'}, 'answer': ''}),
               verdict(), 'Ada'])
    asyncio.run(application(Owner()).complete(request(), llm=llm, context=OperationContext('request')))
    assert recorded == [('primary', 'demo/primary', 'demo/primary')]


@pytest.mark.parametrize('model', ['us.openai.gpt-5.6-luna', 'us.openai.gpt-6-luna'])
@pytest.mark.parametrize('effort', ['low', 'high'])
@pytest.mark.parametrize('invalid_verdict', [False, True])
def test_bedrock_reasoning_native_reviewer_preserves_schema_validation(model, effort, invalid_verdict):
    from api_agent.llm_providers.bedrock.response_policy import prepare_response
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.rich_inference import async_validate_result

    class BedrockPolicyLLM(LLM):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.policies = []

        async def complete(self, request, *, context):
            adapted, evidence = prepare_response(request, 'responses', endpoint='runtime')
            self.policies.append((adapted, evidence))
            result = await super().complete(request, context=context)
            return await async_validate_result(request, replace(result, response=evidence))

    reviewer = json.loads(verdict())
    if invalid_verdict:
        reviewer['unexpected'] = True
    llm = BedrockPolicyLLM([
        json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Hello'}),
        json.dumps(reviewer), 'Hello'])
    owner = Owner()
    original = request()
    selected = replace(original, selected_llm=model,
        payload=replace(original.payload, generation=GenerationOptions(reasoning_effort=effort)))

    if invalid_verdict:
        with pytest.raises(ProviderAccessError):
            asyncio.run(application(owner).complete(selected, llm=llm, context=OperationContext('request')))
        assert len(llm.requests) == 2
    else:
        result = asyncio.run(application(owner).complete(selected, llm=llm, context=OperationContext('request')))
        assert result.payload.content[0].text == 'Hello'
        assert len(llm.requests) == 3

    adapted, evidence = llm.policies[1]
    assert llm.policies[0][1].actual_mode == 'prompted_json'
    assert evidence.actual_mode == 'structured'
    assert evidence.requested_mode == 'structured'
    assert adapted.payload.generation.reasoning_effort == effort
    assert llm.requests[1].payload.response.fallback == 'allow'
    assert json.loads(llm.requests[1].payload.response.schema.schema)['additionalProperties'] is False
    assert owner.events == ['describe']


def test_rejected_candidate_and_failed_checks_reach_the_next_planner_request():
    owner = Owner()
    rejected = {'use_tool': False, 'arguments': {}, 'answer': 'First synthetic candidate'}
    corrected = {'use_tool': False, 'arguments': {}, 'answer': 'Corrected synthetic candidate'}
    details = 'The accumulated count is lost.\nPropagate current_count unchanged; preserve Σ.'
    llm = LLM([json.dumps(rejected), verdict(correct=False, preference=False, details=details),
               json.dumps(corrected), verdict(), 'Corrected answer'])

    result = asyncio.run(application(owner).complete(request(), llm=llm, context=OperationContext('request')))

    second_payload = json.loads(llm.requests[2].payload.messages[-1].content[0].text)
    first_payload = json.loads(llm.requests[0].payload.messages[1].content[0].text)
    assert json.loads(llm.requests[2].payload.messages[1].content[0].text)['messages'] == first_payload['messages']
    assert llm.requests[2].payload.messages[:len(llm.requests[0].payload.messages)] == llm.requests[0].payload.messages
    assert llm.requests[2].payload.messages[len(llm.requests[0].payload.messages)].content[0].text == json.dumps(rejected)
    assert llm.requests[3].payload.messages[:len(llm.requests[1].payload.messages)] == llm.requests[1].payload.messages
    assert second_payload['feedback']['previous_candidate'] == rejected
    assert second_payload['feedback']['failed_checks'] == ['correct', 'preference']
    assert second_payload['feedback']['details'] == details
    assert ''.join(part.text for part in result.payload.content) == 'Corrected answer'
    assert len(llm.requests) == 5
    assert owner.events == ['describe']


def test_accepted_reviewer_returns_no_revision_feedback():
    app = application(Owner())
    llm = LLM([verdict()])
    async def complete(value):
        return await llm.complete(value, context=OperationContext('request'))
    context = SimpleNamespace(tools={'primary': object()}, attempt=1, max_passes=2,
        services={'selection': 'chosen-model', 'generation': GenerationOptions(),
                  'messages': [{'role': 'user', 'content': 'Who?'}], 'llm': complete})

    output = asyncio.run(app.registry['review'].invoke(
        {'candidate': {'use_tool': False, 'arguments': {}, 'answer': 'Hello'}}, context))

    assert output == {'accepted': True, 'feedback': {}}
    assert len(llm.requests) == 1


def test_review_and_revision_instructions_keep_original_constraints_authoritative():
    constraint = 'Use bounded state aggregation. Do not store complete candidate sequences.'
    incoming = LLMRequest('chosen-model', CompletionRequest((Message('user', (TextContent(constraint),)),)))
    first = {'use_tool': False, 'arguments': {}, 'answer': 'A synthetic initial candidate'}
    improved = {'use_tool': False, 'arguments': {}, 'answer': 'A synthetic corrected candidate'}
    advice = 'Store every complete sequence.'
    llm = LLM([json.dumps(first), verdict(correct=False, details=advice),
               json.dumps(improved), verdict(), 'A final answer'])

    asyncio.run(application(Owner()).complete(incoming, llm=llm, context=OperationContext('request')))

    planner = llm.requests[2].payload.messages[0].content[0].text
    reviewer = llm.requests[1].payload.messages[0].content[0].text
    assert 'Reviewer feedback is advisory' in planner
    assert 'conflicts with those requirements' in planner
    assert 'original request is authoritative' in reviewer
    assert 'concrete failing case or a precise invalid step' in reviewer
    assert 'every proposed correction preserves all explicit requirements' in reviewer
    assert 'Reassess earlier review advice' in reviewer
    # Advice remains exact evidence; it cannot replace the original request.
    second = json.loads(llm.requests[2].payload.messages[-1].content[0].text)
    assert second['feedback']['details'] == advice
    assert constraint in llm.requests[2].payload.messages[1].content[0].text
    assert llm.requests[3].payload.messages[0] == llm.requests[1].payload.messages[0]
    assert len(llm.requests) == 5
