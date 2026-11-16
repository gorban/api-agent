"""Draft fixtures, to run only after the core contract is landed."""
import json
import pytest
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, StructuredOutput, StructuredFunctionProfile, TextContent
from api_agent.providers.contracts import ProviderAccessError
from api_agent.llm_providers.gpt import rich
from api_agent.llm_providers.bedrock import converse

SCHEMA = '{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"],"additionalProperties":false}'

def request(strict=None, *, profile=None):
    profile = profile or StructuredFunctionProfile(None, 'converter')
    return LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Describe a count'),)),), structured=StructuredOutput('Count', SCHEMA, False, encoding='function_calling', provider_tool_strict=strict, provider_tool_schema=SCHEMA, function_profile=profile)))

@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('strict', [None, False, True])
def test_forces_only_named_non_executable_schema(mode, strict):
    body = rich.payload(request(strict), mode, False)
    tool, = body['tools']
    definition = tool if mode == 'responses' else tool['function']
    assert definition['name'] == 'Count'
    assert definition['parameters'] == json.loads(SCHEMA)
    assert ('strict' in definition) == (strict is not None)
    if strict is not None:
        assert definition['strict'] is strict
    assert body['tool_choice'] == ({'type': 'function', 'name': 'Count'} if mode == 'responses' else {'type': 'function', 'function': {'name': 'Count'}})
    assert 'response_format' not in body and 'text' not in body
    assert not request(strict).payload.tools

def test_converse_forces_named_schema_for_qualified_gpt():
    req = LLMRequest('us.openai.gpt-6-luna', request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    body = converse.encode(req)
    assert body['toolConfig']['toolChoice'] == {'tool': {'name': 'Count'}}
    spec, = body['toolConfig']['tools']
    assert spec['toolSpec']['name'] == 'Count'
    assert spec['toolSpec']['inputSchema']['json'] == json.loads(SCHEMA)
    assert 'strict' not in spec['toolSpec']
    assert 'outputConfig' not in body


def test_converse_rejects_unqualified_forced_proposal_before_dispatch():
    req = LLMRequest('us.anthropic.claude-opus-5-5', request().payload)
    from api_agent.providers.control import OperationalError
    with pytest.raises(OperationalError) as error:
        converse.validate_request(req)
    assert error.value.kind == 'unsupported' and error.value.execution == 'not_started'


@pytest.mark.parametrize('responses', [False, True])
def test_real_call_and_argument_bytes_survive_provider_decoding(responses):
    argument_bytes = '{ "count" : 3 }'
    if responses:
        data = {'status': 'completed', 'output': [{'type': 'function_call', 'status': 'completed', 'id': 'item-1', 'call_id': 'actual-id', 'name': 'Count', 'arguments': argument_bytes}], 'usage': {'input_tokens': 10, 'output_tokens': 4}}
    else:
        data = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'actual-id', 'type': 'function', 'function': {'name': 'Count', 'arguments': argument_bytes}}]}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}
    decoded = rich.tool_result(data, responses, 'not-a-secret-in-fixture')
    proposal = rich._mark_proposal(request(), decoded)
    assert proposal.outcome == 'tool_calls'
    assert proposal.structured_proposal.call == proposal.payload.calls[0]
    assert proposal.structured_proposal.call.call_id == 'actual-id'
    assert proposal.structured_proposal.call.arguments == argument_bytes
    assert proposal.usage.input_tokens == 10
    assert proposal.usage.output_tokens == 4

@pytest.mark.parametrize('calls', [[], [('wrong-id', 'Other', '{}')], [('a', 'Count', '{}'), ('b', 'Count', '{}')]])
def test_non_exact_calls_are_never_marked_as_proposals(calls):
    from api_agent.providers.llm import LLMResult, ToolCall, Usage
    message = Message('assistant', calls=tuple(ToolCall(*call) for call in calls))
    result = LLMResult(message, Usage(1, 1), 'tool_calls')
    assert rich._mark_proposal(request(), result).structured_proposal is None


@pytest.mark.asyncio
@pytest.mark.parametrize('calls', [[], [('wrong', 'Other', '{}')], [('a', 'Count', '{}'), ('b', 'Count', '{}')], [('a', 'Count', '{"count":1,"count":2}')]])
async def test_ambiguous_calls_fail_closed(calls):
    from api_agent.providers.llm import LLMResult, ToolCall, Usage
    result = LLMResult(Message('assistant', calls=tuple(ToolCall(*call) for call in calls)), Usage(9, 4), 'tool_calls')
    with pytest.raises((ProviderAccessError, ValueError)):
        await rich.async_validate_result(request(), result, 'synthetic-token')


def test_gpt_native_reasoning_and_call_continuation_remains_exact():
    arguments = '{ "count" : 3 }'
    native = [
        {'type': 'reasoning', 'id': 'r1', 'summary': [], 'encrypted_content': 'opaque'},
        {'type': 'function_call', 'id': 'i1', 'status': 'completed', 'call_id': 'real-id', 'name': 'Count', 'arguments': arguments},
    ]
    result = rich.tool_result({'status': 'completed', 'output': native, 'usage': {'input_tokens': 8, 'output_tokens': 5}}, True, 'synthetic-token')
    assert json.loads(result.payload.continuation)['state'] == native
    assert result.payload.calls[0].arguments == arguments


def test_converse_canonicalizes_sdk_object_once_and_keeps_opaque_continuation():
    native = {'role': 'assistant', 'content': [
        {'reasoningContent': {'reasoningText': {'text': 'summary', 'signature': 'opaque-signature'}}},
        {'toolUse': {'toolUseId': 'real-id', 'name': 'Count', 'input': {'count': 3}}},
    ]}
    result = converse.decode({'output': {'message': native}, 'stopReason': 'tool_use',
                              'usage': {'inputTokens': 8, 'outputTokens': 5, 'totalTokens': 13}}, selected_llm='us.openai.gpt-6-luna')
    assert result.payload.calls[0].call_id == 'real-id'
    assert result.payload.calls[0].arguments == json.dumps({'count': 3})
    assert json.loads(result.payload.continuation)['state'] == [native]

@pytest.mark.asyncio
@pytest.mark.parametrize('content_enabled', [False, True])
async def test_gpt_function_capture_preserves_policy_without_auth_context(content_enabled):
    import httpx
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    sent = []
    argument_bytes = '{"count":731}'
    def send(outbound):
        assert outbound.headers['authorization'] == 'Bearer synthetic-token'
        sent.append(json.loads(outbound.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'private-id', 'type': 'function', 'function': {'name': 'Count', 'arguments': argument_bytes}}]}}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 4}})
    original = request()
    selected = LLMRequest('gpt-6-luna', original.payload)
    boundary = EmissionBoundary(ContentPolicy(content_enabled))
    with collect_inference(boundary) as collected:
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            result = await complete_text(client=client, base_url='https://api.openai.com/v1', access_token='synthetic-token', subscription=False, api_mode='completions', request=selected)
    assert result.structured_proposal.call.arguments == argument_bytes
    assert result.usage.input_tokens == 9 and result.usage.output_tokens == 4
    assert sent[0]['tool_choice']['function']['name'] == 'Count'
    captured = json.dumps(collected['rows'])
    assert collected['rows']
    if content_enabled:
        assert '731' in captured and 'private-id' in captured
    else:
        assert all(row['content'] == {} for row in collected['rows'])
        assert 'private-id' not in captured
    assert 'synthetic-token' not in captured
    assert 'StructuredProposalReceipt' not in captured


def test_original_converter_parameters_and_description_are_transported():
    definition = StructuredOutput('Count', SCHEMA, strict=False, encoding='function_calling',
        provider_tool_schema='{"type":"object","properties":{"value":{"type":"string"}}}',
        provider_tool_description='Converted function description', function_profile=StructuredFunctionProfile(None, 'converter'))
    req = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Count'),)),), structured=definition))
    function = rich.payload(req, 'completions', False)['tools'][0]['function']
    assert function['description'] == 'Converted function description'
    assert function['parameters'] == {'type': 'object', 'properties': {'value': {'type': 'string'}}}
    assert json.loads(req.payload.structured.schema) == json.loads(SCHEMA)

@pytest.mark.asyncio
@pytest.mark.parametrize('allow_content', [False, True])
async def test_gpt_capture_respects_content_policy_and_never_exports_opaque_state(allow_content):
    import httpx
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    opaque = 'sealed-reasoning-value'
    argument_bytes = '{"count":731}'
    output = [
        {'type': 'reasoning', 'id': 'r1', 'summary': [], 'encrypted_content': opaque},
        {'type': 'function_call', 'id': 'i1', 'status': 'completed', 'call_id': 'real-id', 'name': 'Count', 'arguments': argument_bytes},
    ]
    def send(outbound):
        return httpx.Response(200, json={'status': 'completed', 'output': output, 'usage': {'input_tokens': 9, 'output_tokens': 4}})
    selected = LLMRequest('gpt-6-luna', request().payload)
    boundary = EmissionBoundary(ContentPolicy(allow_content))
    with collect_inference(boundary, retain_raw=True) as collected:
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            result = await complete_text(client=client, base_url='https://api.openai.com/v1', access_token='synthetic-token', subscription=False, api_mode='responses', request=selected)
    assert result.payload.continuation is not None
    retained = repr(collected['rows'])
    assert opaque not in retained and 'synthetic-token' not in retained
    assert ('731' in retained) is allow_content


def test_private_projection_keeps_function_data_not_opaque_reasoning():
    from api_agent.providers.structured_proposals import proposal_capture_projection
    payload = {'output': [
        {'type': 'reasoning', 'encrypted_content': 'never-export', 'summary': [{'type': 'summary_text', 'text': 'public reasoning'}]},
        {'type': 'function_call', 'call_id': 'real-id', 'name': 'Count', 'arguments': '{"signature":"normal-user-value"}'},
    ]}
    projected = proposal_capture_projection(payload)
    assert 'never-export' not in repr(projected)
    assert 'normal-user-value' in repr(projected)
    assert 'real-id' in repr(projected)

@pytest.mark.parametrize('strict', [None, False, True])
def test_converse_named_tool_strict_wire_and_converted_schema(strict):
    converted = '{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"],"additionalProperties":false}'
    definition = StructuredOutput('Count', SCHEMA, strict=False, encoding='function_calling',
                                  provider_tool_strict=strict, provider_tool_schema=converted,
                                  provider_tool_description='Count response',
                                  function_profile=StructuredFunctionProfile(True, 'strict_all_properties'))
    req = LLMRequest('us.openai.gpt-6-luna', CompletionRequest((Message('user', (TextContent('Count'),)),), structured=definition))
    body = converse.encode(req)
    spec, = body['toolConfig']['tools']
    spec = spec['toolSpec']
    assert spec['name'] == 'Count' and spec['description'] == 'Count response'
    assert spec['inputSchema']['json'] == json.loads(converted)
    assert ('strict' in spec) == (strict is not None)
    if strict is not None:
        assert spec['strict'] is strict
    assert body['toolConfig']['toolChoice'] == {'tool': {'name': 'Count'}}
    assert 'outputConfig' not in body

@pytest.mark.parametrize('endpoint,mode,model', [
    ('runtime', 'responses', 'us.openai.gpt-6-luna'),
])
def test_qualified_bedrock_openai_route_forces_named_function(endpoint, mode, model):
    from api_agent.llm_providers.bedrock import mantle
    req = LLMRequest(model, request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    body = mantle.encode(req, mode, endpoint=endpoint)
    expected = ({'type': 'function', 'name': 'Count'} if mode == 'responses'
                else {'type': 'function', 'function': {'name': 'Count'}})
    assert body['tool_choice'] == expected
    assert 'response_format' not in body and 'text' not in body


def test_unsupported_bedrock_route_rejects_before_dispatch():
    from api_agent.llm_providers.bedrock import mantle
    req = LLMRequest('zai.glm-5', request().payload)
    with pytest.raises(ProviderAccessError):
        mantle.validate_request(req, endpoint='mantle', api_mode='completions')

@pytest.mark.parametrize('mutation', ['model', 'history', 'name', 'schema', 'description', 'strict', 'choice', 'max_tokens', 'temperature', 'reasoning'])
def test_bedrock_worker_rejects_changed_provider_controls(mutation):
    from copy import deepcopy
    from api_agent.llm_providers.bedrock import mantle, worker
    definition = StructuredOutput('Count', SCHEMA, strict=False, encoding='function_calling',
                                  provider_tool_description='Count response', provider_tool_schema=SCHEMA,
                                  function_profile=StructuredFunctionProfile(True, 'strict_all_properties'))
    canonical = LLMRequest('us.openai.gpt-6-luna', CompletionRequest((Message('user', (TextContent('Count'),)),), structured=definition))
    valid = mantle.encode(canonical, 'completions', endpoint='runtime')
    worker.validate_proposal_body(canonical, valid, native=False, response=False, endpoint='runtime', controls={})
    altered = deepcopy(valid)
    if mutation == 'model':
        altered['model'] = 'us.openai.gpt-6-astra'
    elif mutation == 'history':
        altered['messages'][0]['content'] = 'Other'
    elif mutation == 'name':
        altered['tools'][0]['function']['name'] = 'Other'
    elif mutation == 'schema':
        altered['tools'][0]['function']['parameters']['properties']['count']['type'] = 'string'
    elif mutation == 'description':
        altered['tools'][0]['function']['description'] = 'Other'
    elif mutation == 'strict':
        altered['tools'][0]['function']['strict'] = False
    elif mutation == 'choice':
        altered['tool_choice']['function']['name'] = 'Other'
    elif mutation == 'max_tokens':
        altered['max_tokens'] = 500
    elif mutation == 'temperature':
        altered['temperature'] = 0.7
    else:
        altered['reasoning_effort'] = 'high'
    with pytest.raises(ValueError):
        worker.validate_proposal_body(canonical, altered, native=False, response=False, endpoint='runtime', controls={})


@pytest.mark.parametrize('endpoint,mode,model', [
    ('mantle', 'responses', 'openai.gpt-oss-20b'),
    ('mantle', 'completions', 'openai.gpt-oss-120b'),
    ('runtime', 'completions', 'openai.gpt-oss-20b-1:0'),
])
def test_oss_legacy_strict_profile_fails_before_dispatch_until_qualified(endpoint, mode, model):
    from api_agent.llm_providers.bedrock import mantle
    profile = StructuredFunctionProfile(True, 'strict_all_properties')
    req = LLMRequest(model, request(profile=profile).payload)
    with pytest.raises(ProviderAccessError):
        mantle.validate_request(req, endpoint=endpoint, api_mode=mode)


@pytest.mark.parametrize('tamper', [False, True])
def test_worker_startup_authenticates_request_controls_before_execution(monkeypatch, tamper):
    import io
    import socket
    from api_agent.llm_providers.bedrock import catalog, mantle, worker
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.structured_proposals import StructuredWorkerAdmission, current_structured_request
    profile = StructuredFunctionProfile(True, 'strict_all_properties')
    canonical = LLMRequest('us.openai.gpt-6-luna', request(profile=profile).payload)
    selection = catalog.selection(canonical.selected_llm, 'completions', endpoint='runtime')
    row = request_to_wire(canonical)
    body = mantle.encode(canonical, 'completions', endpoint='runtime')
    payload = {'prefix': '/openai/v1', 'body': body, 'endpoint': 'runtime'}
    key = b'k' * 32
    issuer = StructuredWorkerAdmission(key)
    controls = {'reasoning_format': 'auto'}
    proof = issuer.proof(row, payload, 'completions', provider_controls=controls)
    if tamper:
        payload['body'] = {**body, 'temperature': 0.7}
    parent, child = socket.socketpair()
    control = {'key': key.hex(), 'row': row, 'proof': proof, 'controls': controls}
    parent.sendall(json.dumps(control).encode() + b'\n')
    parent.close()
    monkeypatch.setenv('API_AGENT_BEDROCK_PROPOSAL_FD', str(child.detach()))
    monkeypatch.delenv('API_AGENT_BEDROCK_AUTHORITY_FD', raising=False)
    executed = []
    def fake_execute(decoded, emit, **kwargs):
        executed.append((decoded, current_structured_request(), kwargs['proposal_controls']))
    monkeypatch.setattr(worker, 'execute', fake_execute)
    source = io.StringIO(json.dumps({'operation': 'completions', 'payload': payload, 'auth': {}, 'timeout': 1}) + '\n')
    output = io.StringIO()
    worker.main(source, output)
    if tamper:
        assert executed == []
        assert json.loads(output.getvalue())['error'] == 'protocol'
    else:
        assert len(executed) == 1
        assert executed[0][1] == canonical and executed[0][2] == controls
        assert selection.structured_function_profile == canonical.payload.structured.function_profile

@pytest.mark.parametrize('endpoint,mode,model', [
    ('runtime', 'completions', 'openai.gpt-oss-20b-1:0'),
    ('runtime', 'converse', 'us.anthropic.claude-opus-4-1-20250805-v1:0'),
    ('mantle', 'completions', 'openai.gpt-oss-20b'),
])
def test_opt_in_capability_does_not_add_legacy_structured_feature(endpoint, mode, model):
    from api_agent.llm_providers.bedrock.catalog import selection
    assert 'structured' not in selection(model, mode, endpoint=endpoint).features


def test_bedrock_worker_rejects_changed_reasoning_format():
    from api_agent.llm_providers.bedrock import mantle, worker
    canonical = LLMRequest('us.openai.gpt-6-luna', request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    body = mantle.encode(canonical, 'completions', endpoint='runtime')
    payload = {'prefix': '/openai/v1', 'body': body, 'endpoint': 'runtime'}
    controls = {'reasoning_format': 'auto'}
    worker.validate_proposal_transport(canonical, payload, 'completions', controls)
    payload['reasoning_format'] = 'literal'
    with pytest.raises(ValueError):
        worker.validate_proposal_transport(canonical, payload, 'completions', controls)


@pytest.mark.parametrize('replacement', [True, 1.0])
def test_worker_rejects_numeric_enum_type_mutation(replacement):
    from copy import deepcopy
    from api_agent.llm_providers.bedrock import mantle, worker
    schema = json.dumps({'type': 'object', 'properties': {'count': {'type': 'integer', 'enum': [1]}},
                         'required': ['count'], 'additionalProperties': False})
    definition = StructuredOutput('Count', schema, strict=False, encoding='function_calling',
                                  provider_tool_schema=schema,
                                  function_profile=StructuredFunctionProfile(True, 'strict_all_properties'))
    canonical = LLMRequest('us.openai.gpt-6-luna', CompletionRequest((Message('user', (TextContent('Count'),)),), structured=definition))
    expected = mantle.encode(canonical, 'completions', endpoint='runtime')
    worker.validate_proposal_body(canonical, expected, native=False, response=False, endpoint='runtime', controls={})
    changed = deepcopy(expected)
    changed['tools'][0]['function']['parameters']['properties']['count']['enum'][0] = replacement
    with pytest.raises(ValueError):
        worker.validate_proposal_body(canonical, changed, native=False, response=False, endpoint='runtime', controls={})

@pytest.mark.parametrize('replacement', [True, 1])
def test_worker_rejects_generation_numeric_type_mutation(replacement):
    from copy import deepcopy
    from api_agent.llm_providers.bedrock import mantle, worker
    from api_agent.providers.llm import GenerationOptions
    profile = StructuredFunctionProfile(True, 'strict_all_properties')
    payload = request(profile=profile).payload
    canonical = LLMRequest('us.openai.gpt-6-luna', CompletionRequest(payload.messages,
        generation=GenerationOptions(temperature=1.0), structured=payload.structured))
    expected = mantle.encode(canonical, 'completions', endpoint='runtime')
    worker.validate_proposal_body(canonical, expected, native=False, response=False, endpoint='runtime', controls={})
    changed = deepcopy(expected)
    changed['temperature'] = replacement
    with pytest.raises(ValueError):
        worker.validate_proposal_body(canonical, changed, native=False, response=False, endpoint='runtime', controls={})

@pytest.mark.parametrize('bad_value', [float('nan'), float('inf'), {1: 'not-a-json-key'}, (1, 2)])
def test_worker_rejects_invalid_json_wire_values(bad_value):
    from copy import deepcopy
    from api_agent.llm_providers.bedrock import mantle, worker
    canonical = LLMRequest('us.openai.gpt-6-luna', request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    body = deepcopy(mantle.encode(canonical, 'completions', endpoint='runtime'))
    body['tools'][0]['function']['parameters']['properties']['count']['enum'] = [bad_value]
    with pytest.raises(ValueError):
        worker.validate_proposal_body(canonical, body, native=False, response=False, endpoint='runtime', controls={})


def test_worker_accepts_json_object_key_reordering():
    from copy import deepcopy
    from api_agent.llm_providers.bedrock import mantle, worker
    canonical = LLMRequest('us.openai.gpt-6-luna', request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    body = deepcopy(mantle.encode(canonical, 'completions', endpoint='runtime'))
    function = body['tools'][0]['function']
    body['tools'][0]['function'] = dict(reversed(list(function.items())))
    worker.validate_proposal_body(canonical, body, native=False, response=False, endpoint='runtime', controls={})







@pytest.mark.asyncio
async def test_named_schema_proposal_is_admitted_when_pydantic_may_coerce_it():
    from api_agent.providers.llm import LLMResult, StructuredProposal, ToolCall, Usage
    from api_agent.providers.rich_inference import async_validate_result
    call = ToolCall('native-id', 'Count', '{"count":"3"}')
    result = LLMResult(Message('assistant', calls=(call,)), Usage(9, 4), 'tool_calls', structured_proposal=StructuredProposal(call))
    admitted = await async_validate_result(request(), result)
    assert admitted is result
    assert admitted.payload.calls[0].arguments == '{"count":"3"}'
    assert admitted.usage == Usage(9, 4)

@pytest.mark.asyncio
async def test_public_structured_output_defaults_to_json_and_retains_candidate_error_identity():
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.providers.rich_inference import async_validate_result
    definition = StructuredOutput('Count', SCHEMA, strict=False)
    original = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Count'),)),), structured=definition))
    result = LLMResult(Message('assistant', (TextContent('{"other":1}'),)), Usage(1, 1))
    assert definition.encoding == 'json_schema'
    with pytest.raises(CandidateOutputError) as caught:
        await async_validate_result(original, result)
    assert type(caught.value) is CandidateOutputError
    assert caught.value.result is result

@pytest.mark.parametrize('reason,outcome', [('content_filtered', 'refused'), ('max_tokens', 'length')])
def test_bedrock_native_and_internal_stream_preserve_nonproposal_terminal(monkeypatch, reason, outcome):
    from api_agent.llm_providers.bedrock import converse, worker
    from api_agent.llm_providers.bedrock.normalized_stream import ParentStream
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers import structured_proposals
    selected_model = 'us.openai.gpt-6-luna'
    canonical = LLMRequest(selected_model, request(profile=StructuredFunctionProfile(True, 'strict_all_properties')).payload)
    native = {'output': {'message': {'role': 'assistant', 'content': [{'text': 'partial'}]}},
              'stopReason': reason,
              'usage': {'inputTokens': 7, 'outputTokens': 4, 'totalTokens': 11}}
    decoded = converse.decode(native, selected_llm=selected_model)
    monkeypatch.setattr(structured_proposals, 'current_structured_request', lambda: canonical)
    preserved = worker.validated_proposal_terminal(decoded)
    assert preserved is decoded
    assert preserved.outcome == outcome and preserved.structured_proposal is None
    assert preserved.usage.input_tokens == 7 and preserved.usage.output_tokens == 4
    # Internal worker stream still releases only the terminal; public proposal
    # astream is independently rejected by the core owner.
    parent = ParentStream('converse', selected_llm=selected_model, proposal=True)
    assert parent.feed({'kind': 'completed', 'result': result_to_wire(preserved)}) == ()
    assert parent.finish().outcome == outcome
    assert parent.finish().usage == preserved.usage
