"""Candidate repair preserves the native conversation and its dispatch ledger."""
import json

import pytest

from api_agent.providers.contracts import CandidateOutputError, ContractError
from api_agent.providers.inference import request_to_wire, request_from_wire
from api_agent.providers.llm import ToolCall
from api_agent.providers.tools import ToolValidationError
from api_agent.workflows.execution import CandidateCorrection
from api_agent.workflows.tool_gate import ToolGate, ToolGateError, ToolCandidateRejected
from test_tool_conversation import (
    ALIASES, CONTINUATION, Script, reply, tool_call, verdict, run, scenario,
)


def failure_receipt(request, index=0):
    message = next(message for message in request.payload.messages if message.role == 'tool')
    return json.loads(message.results[index].content[0].text)


def test_schema_repair_omits_unadmitted_assistant_and_does_not_dispatch(scenario):
    bad = tool_call(ALIASES[0], 'bad', '{"query":"too long for this schema"}')
    script = Script([bad, tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    journal = run(scenario, script, max_rounds=3)
    request = script.requests[1]
    assert request_from_wire(request_to_wire(request)).payload.messages == request.payload.messages
    assert all(message.role == 'user' for message in request.payload.messages)
    assert not any(message.results for message in request.payload.messages)
    assert '"keyword":"maxLength"' in request.payload.messages[-1].content[0].text
    assert '"constraint":8' in request.payload.messages[-1].content[0].text
    assert journal['inference'][0]['tool_calls'][0]['arguments'] == bad.payload.calls[0].arguments
    assert scenario[0][ALIASES[0]].invocations == ['fixed']
    assert [entry['status'] for entry in journal['calls']] == ['rejected', 'success']


def test_partial_batch_repair_preserves_real_result_and_opaque_history(scenario):
    batch = reply(calls=(ToolCall('first', ALIASES[0], '{"query":"hello"}'),
                        ToolCall('rejected', ALIASES[1], '{"query":"status"}')),
                  continuation=CONTINUATION)
    script = Script([batch, verdict(), verdict(False, True),
        tool_call(ALIASES[1], 'fixed', '{"query":"hello"}'), verdict(), reply('done')])
    result = run(scenario, script, max_rounds=3)
    request = script.requests[3]
    assert request_from_wire(request_to_wire(request)).payload.messages == request.payload.messages
    assert request.payload.messages[1] is batch.payload
    message = request.payload.messages[2]
    assert [item.call_id for item in message.results] == ['first', 'rejected']
    assert json.loads(message.results[0].content[0].text) == {
        'status': 'success', 'data': {'hello': 'unknown-0'}}
    assert json.loads(message.results[1].content[0].text)['errors'][0]['executed'] is False
    assert [owner.invocations for owner in scenario[0].values()] == [['first'], ['fixed']]
    assert result['calls'][0]['result'] == {'hello': 'unknown-0'}


def test_final_constraint_repair_uses_original_history(scenario):
    def check(text, results):
        if len(text) > 5:
            raise CandidateCorrection({'check': 'maxLength', 'maximum': 5, 'actual': len(text)})
    script = Script([reply('too long'), reply('short')])
    assert run(scenario, script, max_rounds=2, verify_final=check)['final'] == 'short'
    messages = script.requests[1].payload.messages
    assert messages[1].content[0].text == 'too long'
    assert '"actual":8' in messages[2].content[0].text
    assert all(not owner.invocations for owner in scenario[0].values())


def test_required_tools_repair_does_not_replace_completed_result(scenario):
    script = Script([tool_call(ALIASES[0], 'first'), verdict(), reply('premature'),
                     tool_call(ALIASES[1], 'second'), verdict(), reply('done')])
    run(scenario, script, max_rounds=4, required_tools=ALIASES)
    assert 'missing_tools' in script.requests[3].payload.messages[-1].content[0].text
    assert [owner.invocations for owner in scenario[0].values()] == [['first'], ['second']]


@pytest.mark.parametrize('malformed', ['not JSON', '{"relevant":"yes","grounded":true}',
                                     '{"relevant":true,"grounded":true,"extra":1}'])
def test_reviewer_repairs_own_malformed_verdict_without_reproposing(scenario, malformed):
    bad = reply(malformed)
    script = Script([tool_call(ALIASES[0]), bad, verdict(), reply('done')])
    run(scenario, script, max_rounds=3)
    reviewer = script.requests[2].payload
    assert reviewer.structured.name == 'tool_relevance'
    assert reviewer.messages[1] is bad.payload
    assert 'relevance_verdict_schema' in reviewer.messages[2].content[0].text
    assert len([request for request in script.requests if not request.payload.structured]) == 2
    assert scenario[0][ALIASES[0]].invocations == ['call-1']


def test_reviewer_retry_consumes_shared_round_allowance(scenario):
    script = Script([tool_call(ALIASES[0]), reply('bad'), verdict()])
    with pytest.raises(ValueError, match='round limit'):
        run(scenario, script, max_rounds=2)
    assert len(script.requests) == 3
    assert scenario[0][ALIASES[0]].invocations == ['call-1']


def test_preflight_typed_details_are_scrubbed_without_active_boundary(scenario):
    owner = scenario[0][ALIASES[0]]
    count = 0
    async def validate(prepared):
        nonlocal count
        count += 1
        if count == 1:
            raise ToolValidationError('Only a literal query is supported; Bearer synthetic-secret')
    owner.validate = validate
    script = Script([tool_call(ALIASES[0], 'bad'), tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    journal = run(scenario, script, max_rounds=3)
    transcript = '\n'.join(part.text for message in script.requests[1].payload.messages for part in message.content)
    assert 'Only a literal query is supported' in transcript
    assert 'synthetic-secret' not in transcript and 'synthetic-secret' not in json.dumps(journal['corrections'])
    assert owner.invocations == ['fixed']


def test_unknown_validator_state_is_terminal_not_candidate_rejection(scenario, monkeypatch):
    import api_agent.workflows.tool_gate as module
    async def unavailable(*args):
        return None
    monkeypatch.setattr(module, 'async_isolated_schema_verdict', unavailable)
    script = Script([tool_call(ALIASES[0])])
    with pytest.raises(ToolGateError, match='unavailable'):
        run(scenario, script)
    assert len(script.requests) == 1 and not scenario[0][ALIASES[0]].invocations


@pytest.mark.parametrize('phase', ['preflight', 'execution', 'result'])
def test_unknown_or_post_effect_failures_never_correct(scenario, phase):
    owner = scenario[0][ALIASES[0]]
    error = ValueError('programming failure') if phase == 'preflight' else CandidateCorrection({'reason':'after effect'})
    async def validate(prepared):
        raise error
    def proof(*args):
        raise error
    if phase == 'preflight':
        owner.validate = validate
    if phase == 'execution':
        async def invoke(prepared):
            owner.invocations.append(prepared.invocation_id)
            raise error
        owner.invoke = invoke
    script = Script([tool_call(ALIASES[0]), verdict()])
    with pytest.raises(ValueError if phase == 'preflight' else ToolGateError) as caught:
        run(scenario, script, result_proof=proof if phase == 'result' else None)
    if phase == 'preflight':
        assert caught.value is error
    else:
        assert 'after dispatch' in str(caught.value)
    assert owner.invocations == ([] if phase == 'preflight' else ['call-1'])
    assert len(script.requests) == (1 if phase == 'preflight' else 2)


def test_corrected_proposal_repeating_completed_input_is_corrected_not_reexecuted(scenario):
    script = Script([tool_call(ALIASES[0], 'first'), verdict(), reply('premature'),
                     tool_call(ALIASES[0], 'new-id'), tool_call(ALIASES[1], 'second'), verdict(), reply('done')])
    journal = run(scenario, script, max_rounds=6, required_tools=ALIASES)
    assert [owner.invocations for owner in scenario[0].values()] == [['first'], ['second']]
    assert [row['reason'] for row in journal['rejections']] == ['repeated_completed_call']
    assert 'repeated_completed_call' in script.requests[4].payload.messages[-1].content[0].text


def test_identical_repeat_before_any_correction_is_corrected_not_dispatched(scenario):
    script = Script([tool_call(ALIASES[0], 'first'), verdict(), tool_call(ALIASES[0], 'again'),
                     reply('done')])
    journal = run(scenario, script, max_rounds=4)
    assert scenario[0][ALIASES[0]].invocations == ['first']
    assert scenario[0][ALIASES[0]].validations == ['first']
    assert [row['status'] for row in journal['calls']] == ['success', 'rejected']
    assert [row['reason'] for row in journal['rejections']] == ['repeated_completed_call']
    assert len([request for request in script.requests if request.payload.structured]) == 1


def test_repeated_completed_calls_are_bounded_by_the_correction_allowance(scenario):
    script = Script([tool_call(ALIASES[0], 'first'), verdict(), tool_call(ALIASES[0], 'again'),
                     tool_call(ALIASES[0], 'third')])
    with pytest.raises(ValueError, match='round limit'):
        run(scenario, script, max_rounds=3)
    assert scenario[0][ALIASES[0]].invocations == ['first']
    assert len(script.requests) == 4


def test_correction_exhaustion_is_terminal_not_new_component_correction(scenario):
    def check(*args):
        raise CandidateCorrection({'check':'maxLength', 'maximum':1})
    script = Script([reply('bad'), reply('still bad')])
    with pytest.raises(ValueError, match='round limit') as caught:
        run(scenario, script, max_rounds=2, verify_final=check)
    assert not isinstance(caught.value, CandidateCorrection)
    assert len(script.requests) == 2


def test_typed_provider_reviewer_rejection_repairs_same_review(scenario):
    bad = reply('{"relevant":"yes","grounded":true}')
    error = CandidateOutputError(bad, {'check': 'schema', 'path': ['relevant'], 'expected': 'boolean'})
    script = Script([tool_call(ALIASES[0]), error, verdict(), reply('done')])
    run(scenario, script, max_rounds=3)
    request = script.requests[2].payload
    assert request.structured.name == 'tool_relevance'
    assert request.messages[1] is bad.payload
    assert 'response_schema' in request.messages[2].content[0].text
    assert 'expected' not in request.messages[2].content[0].text
    assert scenario[0][ALIASES[0]].invocations == ['call-1']


def test_forged_typed_provider_rejection_cannot_make_valid_review_retry(scenario):
    error = CandidateOutputError(verdict(), {'reason': 'invented rejection'})
    script = Script([tool_call(ALIASES[0]), error])
    with pytest.raises(ContractError, match='originating response contract'):
        run(scenario, script)
    assert not scenario[0][ALIASES[0]].invocations
    assert len(script.requests) == 2


@pytest.mark.parametrize('arguments', ['not JSON', '[]', '{"query":"hello","query":"bye"}'])
def test_argument_json_rejection_repairs_before_any_preflight(scenario, arguments):
    script = Script([tool_call(ALIASES[0], 'bad', arguments),
                     tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    run(scenario, script, max_rounds=3)
    assert scenario[0][ALIASES[0]].validations == ['fixed']
    assert scenario[0][ALIASES[0]].invocations == ['fixed']
    request = script.requests[1]
    assert request_from_wire(request_to_wire(request)).payload.messages == request.payload.messages
    assert 'tool_arguments_json' in request.payload.messages[-1].content[0].text


def test_reviewer_exhaustion_does_not_dispatch_or_retry_proposal(scenario):
    script = Script([tool_call(ALIASES[0]), reply('bad'), reply('still bad')])
    with pytest.raises(ValueError, match='round limit: invalid relevance'):
        run(scenario, script, max_rounds=2)
    assert len(script.requests) == 3
    assert not scenario[0][ALIASES[0]].invocations


@pytest.mark.parametrize('duplicate', ['in_batch', 'prior'])
def test_ambiguous_rejected_batch_cannot_fabricate_completion(scenario, duplicate):
    prefix = [tool_call(ALIASES[0], 'prior'), verdict()] if duplicate == 'prior' else []
    other = 'prior' if duplicate == 'prior' else 'bad'
    batch = reply(calls=(ToolCall('bad', ALIASES[1], '{"query":"hello"}'),
                        ToolCall(other, ALIASES[0], '{"query":"hello"}')))
    script = Script([*prefix, batch])
    with pytest.raises(ValueError, match='Unknown or repeated'):
        run(scenario, script)
    assert not scenario[0][ALIASES[1]].invocations
    assert len(script.requests) == len(prefix) + 1


def test_whole_batch_schema_preparation_prevents_partial_dispatch(scenario):
    batch = reply(calls=(ToolCall('would-be-valid', ALIASES[0], '{"query":"hello"}'),
                        ToolCall('invalid', ALIASES[1], '{"query":"over the maximum"}')))
    script = Script([batch, tool_call(ALIASES[1], 'fixed'), verdict(), reply('done')])
    journal = run(scenario, script, max_rounds=3)
    request_from_wire(request_to_wire(script.requests[1]))
    assert scenario[0][ALIASES[0]].invocations == scenario[0][ALIASES[0]].validations == []
    assert scenario[0][ALIASES[1]].invocations == ['fixed']
    assert not any(message.results for message in script.requests[1].payload.messages)
    assert journal['inference'][0]['tool_calls'][0]['invocation'] == 'would-be-valid'


def test_completion_replay_protection_stays_active_after_successful_repair(scenario):
    script = Script([tool_call(ALIASES[0], 'first'), verdict(), reply('premature'),
        tool_call(ALIASES[1], 'second'), verdict(), tool_call(ALIASES[0], 'replayed'), reply('done')])
    run(scenario, script, max_rounds=6, required_tools=ALIASES)
    assert [owner.invocations for owner in scenario[0].values()] == [['first'], ['second']]


def test_schema_literal_never_enters_feedback_without_authority(scenario):
    from dataclasses import replace
    owner = scenario[0][ALIASES[0]]
    owner.spec = replace(owner.spec, input_schema={
        'type': 'object', 'properties': {'query': {'const': 'synthetic-private-constraint'}},
        'required': ['query']})
    script = Script([tool_call(ALIASES[0])])
    journal = {}
    with pytest.raises(ValueError, match='round limit'):
        run(scenario, script, journal, max_rounds=1)
    encoded = json.dumps(journal['corrections'])
    assert 'synthetic-private-constraint' not in encoded
    assert 'schema' not in journal['corrections'][0]['feedback']
    assert not owner.invocations


def test_validated_undeclared_tool_rejection_repairs_without_invalid_history(scenario):
    from test_tool_conversation import validated_script
    script, complete = validated_script([tool_call('unknown-tool', 'unadmitted'),
        tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    run(scenario, complete, max_rounds=3)
    request_from_wire(request_to_wire(script.requests[1]))
    assert all(message.role == 'user' for message in script.requests[1].payload.messages)
    assert 'only the declared tool names' in script.requests[1].payload.messages[-1].content[0].text
    assert scenario[0][ALIASES[0]].invocations == ['fixed']


def test_provider_rejection_subclass_cannot_claim_candidate_authority(scenario):
    class UntrustedRejection(CandidateOutputError):
        pass
    error = UntrustedRejection(verdict(), {'reason': 'invented'})
    script = Script([tool_call(ALIASES[0]), error])
    with pytest.raises(UntrustedRejection) as caught:
        run(scenario, script)
    assert caught.value is error
    assert len(script.requests) == 2 and not scenario[0][ALIASES[0]].invocations


def test_raw_undeclared_tool_repairs_without_replaying_invalid_protocol(scenario):
    script = Script([tool_call('unknown', 'bad'), tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    run(scenario, script, max_rounds=3)
    request_from_wire(request_to_wire(script.requests[1]))
    assert all(message.role == 'user' for message in script.requests[1].payload.messages)
    assert 'allowed_tools' in script.requests[1].payload.messages[-1].content[0].text
    assert scenario[0][ALIASES[0]].invocations == ['fixed']


@pytest.mark.parametrize('early', ['json', 'schema', 'unknown'])
@pytest.mark.parametrize('identity', ['batch', 'prior'])
def test_early_invalid_candidate_cannot_hide_later_reused_call_identity(scenario, early, identity):
    prefix = [tool_call(ALIASES[0], 'prior'), verdict()] if identity == 'prior' else []
    alias = 'unadmitted' if early == 'unknown' else ALIASES[1]
    arguments = 'invalid JSON' if early == 'json' else '{"query":"over the schema limit"}'
    duplicate = 'prior' if identity == 'prior' else 'first-invalid'
    batch = reply(calls=(ToolCall('first-invalid', alias, arguments),
                        ToolCall(duplicate, ALIASES[0], '{"query":"hello"}')))
    script = Script([*prefix, batch])
    with pytest.raises(ValueError, match='Unknown or repeated'):
        run(scenario, script)
    assert not scenario[0][ALIASES[1]].validations
    assert not scenario[0][ALIASES[1]].invocations
    assert len(script.requests) == len(prefix) + 1


def test_malformed_argument_can_be_corrected_after_an_earlier_rejection(scenario):
    script = Script([tool_call(ALIASES[0], 'bad-schema', '{}'),
        tool_call(ALIASES[0], 'bad-json', 'not JSON'),
        tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    run(scenario, script, max_rounds=4)
    assert scenario[0][ALIASES[0]].invocations == ['fixed']
    request_from_wire(request_to_wire(script.requests[2]))


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [None, True, {}, {'relevant': True, 'grounded': None},
                                      {'relevant': None, 'grounded': True}])
async def test_invalid_relevance_validator_value_is_not_candidate_rejection(scenario, decision):
    owner = scenario[0][ALIASES[0]]
    gate = ToolGate(owner, invocation_id='candidate')
    prepared = await gate.prepare({'query': 'hello'})
    with pytest.raises(ToolGateError, match='unavailable or invalid') as caught:
        await gate.verify(prepared, relevance=lambda _: decision)
    assert not isinstance(caught.value, ToolCandidateRejected)
    assert not owner.invocations


def test_native_feedback_borrows_task_local_authority_without_journal(scenario):
    from contextlib import asynccontextmanager
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.attribution import bind_workflow_visit
    owner = scenario[0][ALIASES[0]]
    boundary = EmissionBoundary(ContentPolicy(False), secrets=['synthetic-private-value'])
    leases = []
    @asynccontextmanager
    async def authority():
        leases.append('open')
        try:
            yield boundary
        finally:
            leases.append('closed')
    async def validate(prepared):
        if prepared.invocation_id == 'bad':
            raise ToolValidationError('Only supported reads: synthetic-private-value')
    owner.validate = validate
    script = Script([tool_call(ALIASES[0], 'bad'), tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    with bind_workflow_visit(None, None, capture_services={'capture_authority': authority}):
        run(scenario, script, max_rounds=3)
    feedback_text = script.requests[1].payload.messages[-1].content[0].text
    assert 'Only supported reads' in feedback_text and 'synthetic-private-value' not in feedback_text
    assert leases and leases == ['open', 'closed'] * (len(leases) // 2)
    assert owner.invocations == ['fixed']


def preflight_feedback(scenario, error):
    owner = scenario[0][ALIASES[0]]
    async def validate(prepared):
        if prepared.invocation_id == 'bad':
            raise error
    owner.validate = validate
    script = Script([tool_call(ALIASES[0], 'bad'), tool_call(ALIASES[0], 'fixed'), verdict(), reply('done')])
    journal = run(scenario, script, max_rounds=3)
    assert owner.invocations == ['fixed']
    return journal['corrections'][0]['feedback']


@pytest.mark.parametrize('error,facts', [
    (ToolValidationError('Only a literal query is supported'),
     {'category': 'rejected', 'detail': 'Only a literal query is supported'}),
    (ToolValidationError('Only a literal query is supported', category='query_shape'),
     {'category': 'query_shape', 'detail': 'Only a literal query is supported'}),
    (ToolValidationError('x' * 1000), {'category': 'rejected', 'detail': 'x' * 256}),
    (ToolValidationError(), {'category': 'rejected'}),
    (ToolValidationError(category='query_depth'), {'category': 'query_depth'}),
    (ToolValidationError({'raw': 'object'}), {'category': 'rejected'}),
])
def test_preflight_feedback_is_typed_and_bounded_not_the_raw_error_text(scenario, error, facts):
    assert preflight_feedback(scenario, error) == {
        'reason': 'Tool preflight rejected the candidate', 'check': 'owner_preflight', **facts}


def test_preflight_category_identifies_a_bundled_query_rejection(scenario):
    from api_agent.api_providers.graphql_mcp import GraphQLQueryError
    feedback = preflight_feedback(scenario, GraphQLQueryError('query_depth'))
    assert feedback['category'] == 'query_depth' and 'detail' not in feedback


@pytest.mark.parametrize('category', ['', 'Query', '1query', 'a b', 'a' * 65, None, 3])
def test_preflight_category_must_be_a_short_identifier(category):
    with pytest.raises(ValueError, match='Invalid tool rejection category'):
        ToolValidationError('detail', category=category)


def test_exhaustion_exception_never_exports_authored_feedback_content(scenario):
    sentinel = 'synthetic-private-rejected-content'
    def check(*args):
        raise CandidateCorrection({'reason': 'revise', 'detail': sentinel})
    journal = {}
    with pytest.raises(ValueError, match='^Tool conversation exhausted its round limit$') as caught:
        run(scenario, Script([reply('candidate')]), journal, max_rounds=1, verify_final=check)
    assert sentinel not in str(caught.value) and sentinel not in repr(caught.value)
    assert journal['corrections'][0]['feedback']['detail'] == sentinel


@pytest.mark.parametrize('later', ['typed', 'schema', 'semantic'])
def test_native_node_is_corrected_after_a_conversation_that_only_ran_read_only_tools(scenario, later):
    import asyncio
    from test_workflow_candidate_repair import workflow, replace_node, draft
    from api_agent.workflows.tool_conversation import run_tool_conversation
    flow = workflow()
    visits = []
    async def producer(inputs, context):
        visits.append(context.attempt)
        script = Script([tool_call(ALIASES[0], 'legacy-' + str(context.attempt)), verdict(), reply('done')])
        await run_tool_conversation(prompt='Read once', gates=scenario[1], complete=script,
                                    selection='synthetic', max_rounds=2)
        if context.attempt > 1:
            return draft()
        if later == 'typed':
            raise CandidateCorrection({'reason': 'native output needs correction'})
        if later == 'schema':
            return {}
        return dict(draft(), ready=False)
    replace_node(flow, 'first_draft', producer)
    asyncio.run(flow.arun({'user_input': 'user'}))
    assert visits == [1, 2]
    assert scenario[0][ALIASES[0]].invocations == ['legacy-1', 'legacy-2']
    assert not scenario[0][ALIASES[1]].invocations
