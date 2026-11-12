"""Origin-local candidate repair shares component budgets without replaying work."""
import asyncio
import json
from pathlib import Path

import pytest

from api_agent.providers.llm import LLMRequest, LLMResult, CompletionRequest, Message, TextContent
from api_agent.workflows.contrived import build
from api_agent.workflows.execution import CandidateCorrection, NodeFunction, WorkflowError
from test_workflow_trial_decisions import trial_uuid

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    async def unused(request):
        pytest.fail('unexpected provider dispatch')
    return build(ROOT, llm=unused, selection='example', trial=trial_uuid('skip'))


def replace_node(flow, node, callback):
    row = flow.functions[node]
    original = flow.registry[row['Registry']]
    flow.registry[row['Registry']] = NodeFunction(callback, original.kind)
    flow.graph = flow._compile(None)
    return original.invoke


def draft():
    return dict(original_input='user', route='skip', ready=True, final_output='user')


def test_local_correction_keeps_successful_prefix_and_does_not_fabricate_previous():
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append('producer')
        context.mark_effect()
        return draft()
    replace_node(flow, 'first_draft', producer)
    original = flow.registry[flow.functions['first_format']['Registry']].invoke
    async def formatter(inputs, context):
        visits.append(('format', context.attempt, context.guard_attempt, context.correction_attempt))
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'native limit', 'maximum': 8192}, rejected={'answer': 'rejected'})
        assert context.correction == {'reason': 'native limit', 'maximum': 8192}
        assert context.rejected == {'answer': 'rejected'}
        return original(inputs, context)
    replace_node(flow, 'first_format', formatter)
    result = asyncio.run(flow.arun({'user_input': 'user'}))
    assert result['first_draft'] == draft()
    assert visits == ['producer', ('format', 1, 1, 0), ('format', 2, 1, 1)]


def test_feedback_reaches_actual_next_request_without_feedback_binding():
    flow = workflow()
    requests = []
    async def llm(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('rejected response' if len(requests) == 1 else 'fixed'),)))
    flow.services['llm'] = llm
    async def producer(inputs, context):
        assert context.guard_attempt == 1
        assert inputs['previous'] is None and inputs['feedback'] is None
        request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),)))
        await context.services['llm'](request)
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'limit', 'maximum': 8192})
        return draft()
    replace_node(flow, 'first_draft', producer)
    asyncio.run(flow.arun({'user_input': 'user'}))
    initial, retry = requests
    assert retry.payload.messages[:len(initial.payload.messages)] == initial.payload.messages
    assert retry.payload.messages[len(initial.payload.messages)] == Message('assistant', (TextContent('rejected response'),))
    assert json.loads(retry.payload.messages[-1].content[0].text) == {'correction': {'reason': 'limit', 'maximum': 8192}}


def test_explicit_output_contract_schema_rejection_repairs_locally():
    flow = workflow()
    flow.functions['first_draft'] = dict(flow.functions['first_draft'], **{'Output mode': 'structured'})
    seen = []
    async def producer(inputs, context):
        seen.append(context)
        return {} if not context.correction_attempt else draft()
    replace_node(flow, 'first_draft', producer)
    asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(seen) == 2
    assert seen[1].rejected == {}
    assert seen[1].correction['reason'] == 'output_schema'
    assert seen[1].correction['errors']
    assert all(set(error) <= {'path', 'keyword', 'constraint', 'actual'} for error in seen[1].correction['errors'])


@pytest.mark.parametrize('error', [RuntimeError('programming'), TimeoutError('deadline')])
def test_unknown_execution_failure_is_never_repaired(error):
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        raise error
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(type(error)):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(visits) == 1


def test_origin_effect_blocks_reexecution_before_second_dispatch():
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        context.mark_effect()
        raise CandidateCorrection({'reason': 'rejected after effect'})
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(WorkflowError, match='cannot replay dispatched effects'):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(visits) == 1


def test_deterministic_and_semantic_corrections_share_total_pass_limit():
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append((context.attempt, context.guard_attempt))
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'native rejection'})
        return dict(draft(), ready=False)
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(WorkflowError, match='exhausted 2 passes'):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert visits == [(1, 1), (2, 1)]


@pytest.mark.parametrize('feedback,rejected', [({}, None), ({'bad': float('nan')}, None), ({'reason': 'safe'}, object())])
def test_correction_requires_bounded_json_data(feedback, rejected):
    with pytest.raises(ValueError):
        CandidateCorrection(feedback, rejected=rejected)


@pytest.mark.parametrize('ipc', [False, True])
def test_provider_schema_rejection_retains_actual_opaque_response_and_exact_limit(ipc):
    from dataclasses import replace
    from api_agent.providers.llm import ResponsePolicy, StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.providers.rich_inference import async_validate_result
    from api_agent.providers.setup_transport import _error, _raise_error
    from api_agent.workflows.inference_history import InferenceHistory
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 8192}},
              'required': ['answer'], 'additionalProperties': False}
    payload = CompletionRequest((Message('user', (TextContent('original'),)),),
        response=ResponsePolicy('structured', 'reject', StructuredOutput('answer', json.dumps(schema))))
    request = LLMRequest('example', payload)
    from test_response_policy_contract import evidence
    response = evidence(request)
    opaque = json.dumps({'provider': 'synthetic', 'state': [{'opaque': 'retained-state'}]})
    rejected = LLMResult(Message('assistant', (TextContent(json.dumps({'answer': 'x' * 8666})),), continuation=opaque), response=response)
    accepted = LLMResult(Message('assistant', (TextContent('{"answer":"fixed"}'),)), response=response)
    calls = []
    async def provider(prepared):
        calls.append(prepared)
        result = rejected if len(calls) == 1 else accepted
        try:
            return await async_validate_result(prepared, result)
        except CandidateOutputError as error:
            assert 'retained-state' not in str(error) and 'retained-state' not in repr(error)
            if ipc:
                _raise_error(json.loads(json.dumps(_error(error))))
            raise
    history = InferenceHistory()
    async def run():
        with pytest.raises(CandidateCorrection) as rejected_error:
            await history.call('origin', 'llm', provider, request)
        facts = rejected_error.value.feedback
        assert facts == {'reason': 'response_schema', 'errors': [
            {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 8192, 'actual': 8666}]}
        history.correct('origin', facts)
        assert await history.call('origin', 'llm', provider, request) == accepted
    try:
        asyncio.run(run())
    finally:
        history.close()
    assert calls[1].payload.messages[1] == rejected.payload
    assert calls[1].payload.messages[1].continuation == opaque
    assert json.loads(calls[1].payload.messages[-1].content[0].text)['correction']['errors'][0]['constraint'] == 8192


def test_unavailable_schema_validation_is_not_candidate_repair(monkeypatch):
    from api_agent.providers import rich_inference
    from api_agent.providers.contracts import CandidateOutputError, ProviderAccessError
    from api_agent.providers.llm import StructuredOutput
    request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),),
        structured=StructuredOutput('answer', '{"type":"object"}')))
    result = LLMResult(Message('assistant', (TextContent('{}'),)))
    monkeypatch.setattr(rich_inference, 'schema_verdict', lambda *args: None)
    with pytest.raises(ProviderAccessError) as failure:
        rich_inference.validate_result(request, result)
    assert type(failure.value) is not CandidateOutputError


def test_correction_redacts_registered_secrets_without_capture_flags():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.execution import scrub_correction
    boundary = EmissionBoundary(ContentPolicy(False), secrets=['private-token'])
    feedback = scrub_correction({'reason': 'fix private-token', 'password': 'hidden'}, boundary=boundary)
    assert 'private-token' not in repr(feedback)
    # Only registered credentials are removed; authored fact names and values reach the model.
    assert feedback == {'reason': 'fix [REDACTED]', 'password': 'hidden'}
    assert scrub_correction({'reason': 'fix', 'password': 'hidden'}) == {'reason': 'fix', 'password': 'hidden'}


def test_pending_feedback_is_admitted_before_another_billable_request():
    from api_agent.workflows.inference_history import InferenceHistory
    from api_agent.providers.contracts import ContractError
    history = InferenceHistory()
    calls = []
    request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),)))
    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent('actual'),)))
    async def run():
        await history.call('origin', 'llm', complete, request)
        history.max_bytes = history._bytes + 10
        with pytest.raises(ContractError, match='budget exceeded before correction'):
            history.correct('origin', {'reason': 'x' * 100})
    try:
        asyncio.run(run())
    finally:
        history.close()
    assert len(calls) == 1


def test_multiple_dialogues_are_not_guessed_or_broadcast():
    from api_agent.workflows.inference_history import InferenceHistory, inference_dialogue
    from api_agent.providers.contracts import ContractError
    history = InferenceHistory()
    calls = []
    request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),)))
    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent('actual'),)))
    async def run():
        for role in ('planner', 'reviewer'):
            with inference_dialogue(role):
                await history.call('origin', 'llm', complete, request)
        with pytest.raises(ContractError, match='ambiguous inference origin'):
            history.correct('origin', {'reason': 'explicit but unbound rejection'})
    try:
        asyncio.run(run())
    finally:
        history.close()
    assert len(calls) == 2


def test_late_correction_cannot_reset_expired_component_deadline(monkeypatch):
    from api_agent.providers import progress
    now = [100.0]
    monkeypatch.setattr(progress, '_clock', lambda: now[0])
    flow = workflow()
    flow.timeouts = {owner: 5 for owner in flow.timeouts}
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        now[0] += 10
        raise CandidateCorrection({'reason': 'late rejection'})
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(TimeoutError):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(visits) == 1


def test_semantic_guard_cannot_restart_effectful_prefix():
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        context.mark_effect()
        return dict(draft(), ready=False)
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(WorkflowError, match='cannot replay dispatched effects'):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(visits) == 1


@pytest.mark.asyncio
async def test_actual_tool_dispatch_receipt_is_early_monotonic_and_node_specific(monkeypatch):
    from api_agent.tooling import operations, runtime
    from api_agent.workflows.execution import NodeContext
    # The fixed operations are idempotent; this exercises the receipt for a vocabulary that is not.
    monkeypatch.setattr(operations, 'FIXED_IDEMPOTENT', frozenset())
    monkeypatch.setattr(runtime, 'FIXED_IDEMPOTENT', frozenset())
    from api_agent.tooling.runtime import OwnerTools, invocation_tools, node_tools
    from test_node_tools import definition
    from test_tool_gate import Owner
    selected = definition()
    seen = []
    class EffectOwner(Owner):
        async def invoke(self, prepared):
            seen.append(runner._dispatched())
            raise RuntimeError('uncertain dispatch')
    owner = EffectOwner('synthetic')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}):
        runner = NodeContext('run', (), (), {})
        unrelated = NodeContext('plan', (), (), {})
        planner = node_tools('plan')['reader']
        prepared = await planner.prepare({'query': 'one'})
        grant = await node_tools('review')['check'].verify(prepared,
            relevance=lambda _: {'relevant': True, 'grounded': True})
        with pytest.raises(RuntimeError, match='uncertain dispatch'):
            await runner.tools['primary'].execute(grant)
        assert runner._dispatched() and not unrelated._dispatched()
        await planner.prepare({'query': 'one'})
        assert runner._dispatched() and not unrelated._dispatched()
    assert seen == [True]


def test_guardless_inner_component_uses_nearest_guarded_budget_and_ancestry():
    from dataclasses import replace
    from api_agent.workflows.layout import Step
    flow = workflow()
    steps = list(flow.layout.steps)
    target = next(step for step in steps if step.id == 'first_format')
    parent = target.parent
    position = steps.index(target)
    steps[position:position + 1] = [Step('inner', 'component', parent, target.source, target.line),
                                  replace(target, parent='inner')]
    row = {'ID': 'inner', 'Max passes': '1', 'Guard': '-'}
    flow.layout = replace(flow.layout, steps=tuple(steps))
    flow.components['inner'] = row
    flow.component_passes['inner'] = 1
    flow.timeouts['inner'] = None
    visits = []
    async def producer(inputs, context):
        return draft()
    replace_node(flow, 'first_draft', producer)
    original = flow.registry[flow.functions['first_format']['Registry']].invoke
    async def formatter(inputs, context):
        visits.append((context.attempt, context.max_passes, context.ancestry))
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'inner candidate'})
        return original(inputs, context)
    replace_node(flow, 'first_format', formatter)
    asyncio.run(flow.arun({'user_input': 'user'}))
    assert [attempt for attempt, _, _ in visits] == [1, 2]
    assert all(maximum == 2 for _, maximum, _ in visits)
    assert visits[1][2][0] == (parent, 2, 2)
    assert visits[1][2][-1] == ('inner', 1, 1)


def test_recovered_node_visits_form_a_valid_retained_journal():
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    flow = workflow()
    journal = WorkflowJournal(flow.definition, component_passes=flow.component_passes)
    flow.services['journal'] = journal
    async def producer(inputs, context):
        return draft()
    replace_node(flow, 'first_draft', producer)
    original = flow.registry[flow.functions['first_format']['Registry']].invoke
    async def formatter(inputs, context):
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'format candidate'})
        return original(inputs, context)
    replace_node(flow, 'first_format', formatter)
    asyncio.run(flow.arun({'user_input': 'user'}))
    payload = journal.finalize()
    assert validate_journal(payload, expected_definition=flow.definition,
                            expected_component_passes=flow.component_passes) == payload
    visits = [event for event in payload['events'] if event['event'] == 'scope_end'
              and event.get('node') == 'first_format']
    assert [visit['outcome'] for visit in visits] == ['failed', 'passed']
    assert len({visit['scope_id'] for visit in visits}) == 2


def test_candidate_wire_rejects_conflicting_accounting_and_subtype_flags():
    from api_agent.providers.contracts import CandidateOutputError, ContractError
    from api_agent.providers.setup_transport import _error, _raise_error
    from api_agent.providers.llm import Usage
    candidate = LLMResult(Message('assistant', (TextContent('{}'),)), usage=Usage(4, 2))
    error = CandidateOutputError(candidate, {'reason': 'schema'})
    original = _error(error)
    changes = [lambda row: row['details']['usage'].update(input_tokens=5),
        lambda row: row['details'].update(status=400),
        lambda row: row['details'].update(origin='provider'),
        lambda row: row['details']['candidate'].update(version=True),
        lambda row: row['details'].update(code='empty_final_answer')]
    for change in changes:
        frame = json.loads(json.dumps(original))
        change(frame)
        with pytest.raises(ContractError, match='Invalid provider response'):
            _raise_error(frame)


@pytest.mark.asyncio
async def test_native_owned_correction_preserves_exact_result_without_node_replay():
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.providers.rich_inference import async_validate_result
    from api_agent.providers.llm import StructuredOutput
    from api_agent.workflows.inference_history import InferenceHistory, own_candidate_corrections
    request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),),
        structured=StructuredOutput('answer', '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}')))
    rejected = LLMResult(Message('assistant', (TextContent('{}'),)))
    async def complete(prepared):
        return await async_validate_result(prepared, rejected)
    history = InferenceHistory()
    try:
        with own_candidate_corrections():
            with pytest.raises(CandidateOutputError) as caught:
                await history.call('reviewer', 'llm', complete, request)
        assert caught.value.result is rejected
        entry, = history._entries.values()
        assert entry.messages[-1] == rejected.payload
    finally:
        history.close()


def test_schema_correction_never_forwards_const_enum_pattern_or_dynamic_keys():
    from jsonschema import Draft202012Validator
    from api_agent.workflows.execution import _schema_facts, scrub_correction
    schema = {'type': 'object', 'properties': {
        'fixed': {'const': 'private-constant'},
        'choice': {'enum': ['private-enum']},
        'match': {'pattern': 'private-pattern'}},
        'patternProperties': {'.*': {'type': 'string', 'maxLength': 2}}}
    value = {'fixed': 'bad', 'choice': 'bad', 'match': 'bad', 'private-dynamic-key': 'long'}
    errors = tuple(Draft202012Validator(schema).iter_errors(value))
    facts = _schema_facts(schema, value, errors)
    feedback = scrub_correction({'reason': 'output_schema', 'errors': facts})
    assert not any(secret in repr(feedback) for secret in (
        'private-constant', 'private-enum', 'private-pattern', 'private-dynamic-key'))
    assert {'path': ['*'], 'keyword': 'maxLength', 'constraint': 2, 'actual': 4} in facts
    assert any(fact['keyword'] == 'const' and 'constraint' not in fact for fact in facts)


@pytest.mark.asyncio
async def test_task_local_authority_is_borrowed_without_any_journal():
    from contextlib import asynccontextmanager
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.execution import project_correction
    boundary = EmissionBoundary(ContentPolicy(False), secrets=['live-private-secret'])
    leases = []
    @asynccontextmanager
    async def authority():
        leases.append('open')
        try:
            yield boundary
        finally:
            leases.append('closed')
    with bind_workflow_visit(None, None, capture_services={'capture_authority': authority}):
        feedback = await project_correction({'reason': 'repair live-private-secret'})
    assert 'live-private-secret' not in repr(feedback)
    assert leases == ['open', 'closed']


@pytest.mark.asyncio
async def test_correction_feedback_for_the_model_keeps_ordinary_keys_and_redacts_secrets():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.execution import project_correction
    boundary = EmissionBoundary(ContentPolicy(False), secrets=['live-private-secret'])
    facts = {'reason': 'repair live-private-secret', 'token_limit': 3, 'next_token': 'cursor',
             'live-private-secret': 'leaked key'}
    feedback = await project_correction(facts, services={'emissions': boundary})
    assert feedback['token_limit'] == 3 and feedback['next_token'] == 'cursor'
    assert 'live-private-secret' not in repr(feedback)


def test_nested_child_effect_blocks_reexecution_of_its_native_parent():
    parent, child = workflow(), workflow()
    visits = []
    async def child_producer(inputs, context):
        visits.append('child effect')
        context.mark_effect()
        return draft()
    replace_node(child, 'first_draft', child_producer)
    async def parent_producer(inputs, context):
        visits.append('parent')
        await child.arun({'user_input': 'user'})
        raise CandidateCorrection({'reason': 'parent candidate after child effect'})
    replace_node(parent, 'first_draft', parent_producer)
    with pytest.raises(WorkflowError, match='cannot replay dispatched effects'):
        asyncio.run(parent.arun({'user_input': 'user'}))
    assert visits == ['parent', 'child effect']


def test_numeric_candidate_value_is_not_duplicated_in_schema_feedback():
    from jsonschema import Draft202012Validator
    from api_agent.workflows.execution import _schema_facts, scrub_correction
    schema = {'type': 'object', 'properties': {'code': {'type': 'integer', 'minimum': 999999}}}
    candidate = {'code': 123456}
    errors = tuple(Draft202012Validator(schema).iter_errors(candidate))
    facts = _schema_facts(schema, candidate, errors)
    feedback = scrub_correction({'reason': 'output_schema', 'errors': facts})
    assert facts == [{'path': ['code'], 'keyword': 'minimum', 'constraint': 999999}]
    assert '123456' not in repr(feedback)
    assert feedback['errors'][0]['constraint'] == 999999


@pytest.mark.parametrize('semantic', [False, True])
def test_exhaustion_diagnostics_do_not_export_authorized_repair_content(semantic):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    flow = workflow()
    flow.services['emissions'] = EmissionBoundary(ContentPolicy(False))
    sentinel = 'authored-repair-content-sentinel'
    requests = []
    async def complete(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('actual rejected response'),)))
    flow.services['llm'] = complete
    async def producer(inputs, context):
        payload = CompletionRequest((Message('user', (TextContent(json.dumps(inputs['feedback'])),)),))
        await context.services['llm'](LLMRequest('example', payload))
        if not semantic:
            raise CandidateCorrection({'reason': sentinel, 'previous_candidate': 'private candidate'})
        return dict(draft(), ready=False)
    replace_node(flow, 'first_draft', producer)
    if semantic:
        async def guard(inputs, context):
            return {'accepted': False, 'feedback': {'reason': sentinel + ' private candidate'}}
        replace_node(flow, 'first_guard', guard)
    with pytest.raises(WorkflowError, match='exhausted 2 passes') as caught:
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert len(requests) == 2
    assert any(sentinel in part.text for message in requests[1].payload.messages for part in message.content)
    assert sentinel not in str(caught.value) and sentinel not in repr(caught.value)
    assert 'private candidate' not in str(caught.value) and 'private candidate' not in repr(caught.value)


@pytest.mark.parametrize('feedback', [{'reason': ('tuple',)}, {'reason': 'x' * (1024 * 1024)}])
def test_correction_rejects_noncanonical_or_oversized_json_before_retention(feedback):
    with pytest.raises(ValueError, match='^Invalid candidate correction data$'):
        CandidateCorrection(feedback)


def test_schema_fact_counts_use_real_nested_array_error_instances():
    from jsonschema import Draft202012Validator
    from api_agent.workflows.execution import _schema_facts
    schema = {'type': 'object', 'properties': {'rows': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 2}}}}}}
    candidate = {'rows': [{'text': 'private'}]}
    errors = tuple(Draft202012Validator(schema).iter_errors(candidate))
    assert _schema_facts(schema, candidate, errors) == [
        {'path': ['rows', 0, 'text'], 'keyword': 'maxLength', 'constraint': 2, 'actual': 7}]
    boolean_schema = {'type': 'array', 'items': False}
    errors = tuple(Draft202012Validator(boolean_schema).iter_errors(['private']))
    facts = _schema_facts(boolean_schema, ['private'], errors)
    assert facts[0]['path'] == list(errors[0].path) == []
    assert 'private' not in repr(facts)


def test_wrong_emission_authority_cannot_fall_back_to_empty_scrubbing():
    from api_agent.workflows.execution import scrub_correction
    with pytest.raises(WorkflowError, match='^Correction credential authority unavailable$'):
        scrub_correction({'reason': 'sensitive feedback'}, boundary=object())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['wrong-boundary', 'exception', 'timeout'])
async def test_failed_declared_authority_is_terminal_without_plaintext_fallback(failure):
    from contextlib import asynccontextmanager
    from api_agent.workflows.execution import project_correction
    errors = {'exception': RuntimeError('private authority internals'), 'timeout': TimeoutError('authority timeout')}
    closed = []
    @asynccontextmanager
    async def authority():
        try:
            if failure in errors:
                raise errors[failure]
            yield object()
        finally:
            closed.append(True)
    expected = TimeoutError if failure == 'timeout' else WorkflowError
    with pytest.raises(expected) as caught:
        await project_correction({'reason': 'sensitive feedback'}, services={'capture_authority': authority})
    assert 'private authority internals' not in str(caught.value)
    assert 'sensitive feedback' not in str(caught.value)
    assert closed == [True]


@pytest.mark.parametrize('value,mode', [({'not-json'}, 'structured'), ((1, 2), 'structured'), ({}, '-')])
def test_unsupported_native_or_undeclared_schema_failure_is_not_repaired(value, mode):
    flow = workflow()
    flow.functions['first_draft'] = dict(flow.functions['first_draft'], **{'Output mode': mode})
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        return value
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(WorkflowError) as caught:
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert not isinstance(caught.value, CandidateCorrection)
    assert len(visits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('subclass', [False, True])
async def test_candidate_error_requires_exact_subtype_and_actual_request_rejection(subclass):
    from api_agent.providers.contracts import CandidateOutputError, ContractError
    from api_agent.workflows.inference_history import InferenceHistory
    class ForeignCandidate(CandidateOutputError):
        pass
    result = LLMResult(Message('assistant', (TextContent('{}'),)))
    error_type = ForeignCandidate if subclass else CandidateOutputError
    forged = error_type(result, {'reason': 'untrusted remote assertion'})
    calls = []
    async def complete(request):
        calls.append(request)
        raise forged
    request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('original'),)),)))
    history = InferenceHistory()
    try:
        with pytest.raises(ForeignCandidate if subclass else ContractError) as caught:
            await history.call('origin', 'llm', complete, request)
        assert not isinstance(caught.value, CandidateCorrection)
        entry, = history._entries.values()
        assert entry.messages == ()
    finally:
        history.close()
    assert len(calls) == 1


@pytest.mark.parametrize('dynamic', [False, True])
def test_terminal_schema_error_masks_generated_keys_but_keeps_declared_path(dynamic):
    flow = workflow()
    private_key = 'private-generated-key-sentinel'
    if dynamic:
        schema = {'type': 'object', 'patternProperties': {'.*': {'type': 'string'}}}
        candidate = {private_key: 42}
        expected_path = "['*']"
    else:
        schema = {'type': 'object', 'properties': {'declared': {'type': 'object',
            'properties': {'answer': {'type': 'string'}}}}}
        candidate = {'declared': {'answer': 42}}
        expected_path = "['declared', 'answer']"
    flow.functions['first_draft'] = dict(flow.functions['first_draft'], **{
        'Output schema': schema, 'Output mode': '-'})
    visits = []
    async def producer(inputs, context):
        visits.append(context)
        return candidate
    replace_node(flow, 'first_draft', producer)
    with pytest.raises(WorkflowError) as caught:
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert not isinstance(caught.value, CandidateCorrection)
    assert expected_path in str(caught.value) and expected_path in repr(caught.value)
    assert private_key not in str(caught.value) and private_key not in repr(caught.value)
    assert len(visits) == 1
    if dynamic:
        assert list(caught.value.errors[0].path) == [private_key]
