"""Offline named-proposal transport and authority coverage."""
import json
from types import SimpleNamespace

import pytest
from api_agent.llm_providers.bedrock import catalog, converse, mantle, worker
from api_agent.providers.llm import LLMRequest, StructuredFunctionProfile, LLMResult, Message, ToolCall, Usage
from api_agent.providers.structured_proposals import _new_admission_authority
from api_agent.providers.contracts import ContractError
from test_structured_function_providers import request as proposal_request
from test_bedrock_runtime_worker import request as worker_request, native_client

MODEL = 'us.openai.gpt-6-luna'
PROFILE = StructuredFunctionProfile(True, 'strict_all_properties')

def canonical():
    return LLMRequest(MODEL, proposal_request(profile=PROFILE).payload)

def native_request():
    request = canonical()
    body = converse.encode(request)
    return request, {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}

def call_result(name='Count', arguments='{"count":3}'):
    return LLMResult(Message('assistant', calls=(ToolCall('call-id', name, arguments),)), Usage(7, 4), 'tool_calls')

def test_worker_terminal_requires_authority_and_exact_named_call():
    with pytest.raises(ContractError, match='authority'):
        worker.validated_proposal_terminal(call_result())
    req = canonical()
    selection = catalog.selection(MODEL, 'converse', 'runtime')
    owner = object()
    with _new_admission_authority(owner).bind(owner, req, selection):
        result = worker.validated_proposal_terminal(call_result())
        assert result.structured_proposal.call == result.payload.calls[0]
        assert result.structured_proposal.call.arguments == '{"count":3}'
        with pytest.raises(ContractError) as bad:
            worker.validated_proposal_terminal(call_result(name='Other'))
        assert bad.value.usage == Usage(7, 4)

@pytest.mark.parametrize('operation', ['converse', 'converse_stream'])
def test_worker_admits_native_proposal_and_preserves_named_call(operation):
    from api_agent.providers.inference import result_from_wire
    req, payload = native_request()
    raw = {'output': {'message': {'role': 'assistant', 'content': [
        {'toolUse': {'toolUseId': 'call-id', 'name': 'Count', 'input': {'count': 3}}}]}},
        'stopReason': 'tool_use', 'usage': {'inputTokens': 7, 'outputTokens': 4, 'totalTokens': 11}}
    seen = []
    owner = object()
    selection = catalog.selection(MODEL, 'converse', 'runtime')
    with _new_admission_authority(owner).bind(owner, req, selection):
        worker.execute(worker_request(operation, payload), seen.append,
            boto_client_factory=lambda *args: native_client(raw), proposal_controls={})
    if operation == 'converse':
        assert seen[0]['result'] == raw
    else:
        assert len(seen) == 1 and seen[0]['event']['kind'] == 'completed'
        result = result_from_wire(seen[0]['event']['result'])
        assert result.structured_proposal.call.call_id == 'call-id'
        assert json.loads(result.structured_proposal.call.arguments) == {'count': 3}

@pytest.mark.parametrize('change', ['missing-choice', 'extra-config', 'no-tools', 'bad-tool', 'bad-spec', 'bad-name', 'bad-schema', 'bad-choice'])
def test_native_proposal_shape_tampering_fails_before_dispatch(change, monkeypatch):
    monkeypatch.setattr(worker, 'validate_proposal_transport', lambda *args, **kwargs: pytest.fail('shape guard bypassed'))
    req, payload = native_request()
    config = payload['body']['toolConfig']
    spec = config['tools'][0]['toolSpec']
    if change == 'missing-choice': del config['toolChoice']
    elif change == 'extra-config': config['extra'] = True
    elif change == 'no-tools': config['tools'] = []
    elif change == 'bad-tool': config['tools'] = [None]
    elif change == 'bad-spec': config['tools'] = [{'toolSpec': None}]
    elif change == 'bad-name': spec['name'] = ''
    elif change == 'bad-schema': spec['inputSchema'] = {'json': []}
    else: config['toolChoice'] = {'tool': {'name': 'Other'}}
    frames = []
    selection = catalog.selection(MODEL, 'converse', 'runtime')
    owner = object()
    with _new_admission_authority(owner).bind(owner, req, selection):
        worker.execute(worker_request('converse', payload), frames.append,
            boto_client_factory=lambda *args: pytest.fail('invalid request dispatched'), proposal_controls={})
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]

@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('change', ['no-tools', 'two-tools', 'bad-tool', 'bad-function', 'bad-type', 'bad-name', 'bad-schema', 'bad-choice'])
def test_mantle_proposal_shape_tampering_fails_before_dispatch(mode, change, monkeypatch):
    monkeypatch.setattr(worker, 'validate_proposal_transport', lambda *args, **kwargs: pytest.fail('shape guard bypassed'))
    req = canonical()
    body = mantle.encode(req, mode, endpoint='runtime')
    payload = {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': body}
    item = body['tools'][0]
    spec = item if mode == 'responses' else item['function']
    if change == 'no-tools': body['tools'] = []
    elif change == 'two-tools': body['tools'] = [item, dict(item)]
    elif change == 'bad-tool': body['tools'] = [None]
    elif change == 'bad-function':
        if mode == 'responses': item['extra'] = True
        else: item['function'] = None
    elif change == 'bad-type': item['type'] = 'other'
    elif change == 'bad-name': spec['name'] = ''
    elif change == 'bad-schema': spec['parameters'] = []
    else: body['tool_choice'] = 'auto'
    frames = []
    owner = object()
    selection = catalog.selection(MODEL, mode, 'runtime')
    with _new_admission_authority(owner).bind(owner, req, selection):
        worker.execute(worker_request(mode, payload), frames.append,
            http_factory=lambda **kwargs: pytest.fail('invalid request dispatched'), proposal_controls={})
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]

@pytest.mark.asyncio
async def test_gpt_capture_projection_failure_does_not_change_proposal(monkeypatch):
    import httpx
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.providers import structured_proposals
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    def reject_projection(value):
        raise ValueError('private capture failure')
    monkeypatch.setattr(structured_proposals, 'proposal_capture_projection', reject_projection)
    selected = LLMRequest('gpt-6-luna', proposal_request().payload)
    def send(request):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'private-id', 'type': 'function',
            'function': {'name': 'Count', 'arguments': '{"count":3}'}}]}}],
            'usage': {'prompt_tokens': 9, 'completion_tokens': 4}})
    with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as capture:
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            result = await complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token='synthetic-token', subscription=False, api_mode='completions', request=selected)
    assert result.structured_proposal.call.arguments == '{"count":3}'
    assert result.usage.input_tokens == 9 and result.usage.output_tokens == 4
    assert 'private-id' not in json.dumps(capture['rows'])
    assert 'private capture failure' not in json.dumps(capture['rows'])


def test_mantle_proposal_description_is_preserved():
    from dataclasses import replace
    req = canonical()
    definition = replace(req.payload.structured, provider_tool_description='Convert the count')
    req = replace(req, payload=replace(req.payload, structured=definition))
    body = mantle.encode(req, 'completions', endpoint='runtime')
    assert body['tools'][0]['function']['description'] == 'Convert the count'


@pytest.mark.parametrize('mode', ['completions', 'responses', 'converse'])
def test_proposal_transport_rejects_wrong_route_and_extra_controls(mode):
    req = canonical()
    endpoint = 'runtime'
    body = converse.encode(req) if mode == 'converse' else mantle.encode(req, mode, endpoint=endpoint)
    payload = {'endpoint': endpoint, 'prefix': '/converse' if mode == 'converse' else '/openai/v1', 'body': body}
    worker.validate_proposal_transport(req, payload, mode, {})
    with pytest.raises(ValueError):
        worker.validate_proposal_transport(None, payload, mode, {})
    wrong = dict(payload, prefix='/v1')
    with pytest.raises(ValueError):
        worker.validate_proposal_transport(req, wrong, mode, {})
    if mode != 'converse':
        with pytest.raises(ValueError):
            worker.validate_proposal_transport(req, payload, mode, {'LLM_REASONING_LOW_TOKENS': 1024})
    if mode != 'completions':
        with pytest.raises(ValueError):
            worker.validate_proposal_transport(req, payload, mode, {'reasoning_format': 'literal'})
        with pytest.raises(ValueError):
            worker.validate_proposal_transport(req, dict(payload, reasoning_format='literal'), mode, {})


def test_proposal_body_rejects_invalid_json_without_leaking_value():
    req = canonical()
    body = mantle.encode(req, 'completions', endpoint='runtime')
    body['messages'] = object()
    with pytest.raises(ValueError, match='Invalid structured proposal body'):
        worker.validate_proposal_body(req, body, native=False, response=False, endpoint='runtime', controls={})

@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('projection_failure', [False, True])
async def test_factory_admitted_proposal_preserves_terminal_and_skips_unsafe_capture(monkeypatch, stream, projection_failure):
    from unittest.mock import AsyncMock
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers import structured_proposals
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    req = canonical()
    raw = {'model': MODEL, 'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
        'content': None, 'tool_calls': [{'id': 'private-call', 'type': 'function',
        'function': {'name': 'Count', 'arguments': '{"count":3}'}}]}}],
        'usage': {'prompt_tokens': 7, 'completion_tokens': 4, 'total_tokens': 11}}
    decoded = mantle.decode(raw, 'completions', selected_llm=MODEL, endpoint='runtime')
    calls = []
    async def complete(operation, payload, **options):
        calls.append((operation, payload, options))
        options['on_dispatch']()
        return raw
    async def streaming(operation, payload, **options):
        calls.append((operation, payload, options))
        options['on_dispatch']()
        yield {'kind': 'completed', 'result': result_to_wire(decoded)}
    provider = BedrockProvider({'BEDROCK_ENDPOINT': 'runtime', 'BEDROCK_API_MODE': 'completions'})
    provider.transport = SimpleNamespace(call=complete, stream=streaming, aclose=AsyncMock())
    if projection_failure:
        from api_agent.llm_providers import response_policy
        monkeypatch.setattr(response_policy, 'capture_payload', lambda *args, **kwargs: pytest.fail('unsafe proposal capture'))
        def fail_projection(value):
            raise ValueError('private capture failure')
        monkeypatch.setattr(structured_proposals, 'proposal_capture_projection', fail_projection)
    owner = object()
    selection = catalog.selection(MODEL, 'completions', 'runtime')
    try:
        with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as captured:
            with _new_admission_authority(owner).bind(owner, req, selection):
                if stream:
                    events = [event async for event in provider.astream(req)]
                    assert len(events) == 1 and events[0].kind == 'completed'
                    result = events[0].payload
                else:
                    result = await provider.acomplete(req)
        assert result.structured_proposal.call.call_id == 'private-call'
        assert result.structured_proposal.call.arguments == '{"count":3}'
        assert result.usage == Usage(7, 4)
        assert calls[0][2]['proposal_request'] == req
        assert calls[0][2]['proposal_controls'] == {'reasoning_format': 'auto'}
        if projection_failure:
            assert 'private-call' not in json.dumps(captured['rows'])
    finally:
        await provider.aclose()


@pytest.mark.parametrize('api_mode,model,effort,config,expected', [
    ('completions', MODEL, None, {'BEDROCK_COMPLETIONS_REASONING_FORMAT': 'literal'}, {'reasoning_format': 'literal'}),
    ('responses', MODEL, None, {}, {}),
    ('converse', MODEL, None, {}, {}),
    ('converse', 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', 'medium',
     {'LLM_REASONING_LOW_TOKENS': 2048, 'LLM_REASONING_HIGH_TOKENS': 8192},
     {'LLM_REASONING_LOW_TOKENS': 2048, 'LLM_REASONING_HIGH_TOKENS': 8192}),
])
def test_factory_proposal_controls_follow_request_and_selected_mode(api_mode, model, effort, config, expected):
    from dataclasses import replace
    from api_agent.llm_providers.bedrock.factory import _proposal_controls
    req = canonical()
    req = replace(req, selected_llm=model, payload=replace(req.payload,
        generation=replace(req.payload.generation, reasoning_effort=effort)))
    assert _proposal_controls(config, req, api_mode) == expected


def test_converse_refuses_unchanged_replay_of_unsigned_reasoning():
    from api_agent.providers.contracts import ProviderAccessError
    native = {'output': {'message': {'role': 'assistant', 'content': [
        {'reasoningContent': {'reasoningText': {'text': 'reasoning'}}}, {'text': 'answer'}]}},
        'stopReason': 'end_turn', 'usage': {'inputTokens': 1, 'outputTokens': 1, 'totalTokens': 2}}
    message = converse.decode(native, selected_llm=MODEL).payload
    with pytest.raises(ProviderAccessError) as error:
        converse.continuation_message(message, MODEL, preserve=True)
    assert error.value.kind == 'unsupported'
    stripped = converse.continuation_message(message, MODEL, preserve=False)
    assert stripped['content'] == [{'text': 'answer'}]


def test_converse_rejects_forced_choice_when_thinking_is_active():
    from dataclasses import replace
    from api_agent.providers.contracts import ProviderAccessError
    profile = StructuredFunctionProfile(None, 'converter')
    req = proposal_request(profile=profile)
    req = replace(req, selected_llm='us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        payload=replace(req.payload, generation=replace(req.payload.generation, reasoning_effort='medium')))
    with pytest.raises(ProviderAccessError) as error:
        converse.validate_request(req)
    assert error.value.kind == 'unsupported'


@pytest.mark.parametrize('frame', [b'', b'x' * 129])
def test_worker_closes_truncated_or_oversized_proposal_channel(monkeypatch, frame):
    import io
    closed = []
    class Channel:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def settimeout(self, timeout): pass
        def recv(self, count): return frame
    monkeypatch.setattr(worker.socket, 'socket', Channel)
    monkeypatch.setattr(worker, 'MAX_FRAME', 128)
    monkeypatch.setenv('API_AGENT_BEDROCK_PROPOSAL_FD', '12')
    monkeypatch.delenv('API_AGENT_BEDROCK_AUTHORITY_FD', raising=False)
    output = io.StringIO()
    worker.main(io.StringIO('{}\n'), output)
    assert json.loads(output.getvalue()) == {'error': 'protocol'}
    assert closed == [True]


def test_mantle_worker_stream_emits_only_authenticated_proposal_terminal():
    from api_agent.providers.inference import result_from_wire
    from test_bedrock_mantle import chunk, usage
    req = canonical()
    payload = {'endpoint': 'runtime', 'prefix': '/openai/v1',
        'body': mantle.encode(req, 'completions', endpoint='runtime')}
    values = [chunk({'role': 'assistant'}, model=MODEL),
        chunk({'content': 'private progress ' * 6}, model=MODEL),
        chunk({'tool_calls': [{'index': 0, 'id': 'call-id', 'type': 'function',
            'function': {'name': 'Count', 'arguments': '{"count":3}'}}]}, model=MODEL),
        chunk(finish='tool_calls', model=MODEL), chunk(choices=[], usage=usage(), model=MODEL)]
    class Body:
        def __iter__(self):
            return iter(SimpleNamespace(model_dump=lambda **kwargs: value) for value in values)
        def close(self): pass
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: Body())), close=lambda: None)
    frames = []
    owner = object()
    selection = catalog.selection(MODEL, 'completions', 'runtime')
    with _new_admission_authority(owner).bind(owner, req, selection):
        worker.execute(worker_request('completions_stream', payload), frames.append,
            runtime_client_factory=lambda *args: client, http_factory=lambda **kwargs: SimpleNamespace(close=lambda: None),
            proposal_controls={})
    assert len(frames) == 1 and frames[0]['event']['kind'] == 'completed'
    result = result_from_wire(frames[0]['event']['result'])
    assert result.structured_proposal.call.call_id == 'call-id'
    assert json.loads(result.structured_proposal.call.arguments) == {'count': 3}
    assert result.payload.content[0].text == 'private progress ' * 6


def test_mantle_proposal_explicit_strict_is_preserved():
    from dataclasses import replace
    req = canonical()
    definition = replace(req.payload.structured, provider_tool_strict=True)
    req = replace(req, payload=replace(req.payload, structured=definition))
    assert mantle.encode(req, 'completions', endpoint='runtime')['tools'][0]['function']['strict'] is True


def test_worker_rejects_proposal_control_for_discovery_operation(monkeypatch):
    import io
    import socket
    parent, child = socket.socketpair()
    control = {'key': (b'k' * 32).hex(), 'row': {}, 'proof': {}, 'controls': {}}
    parent.sendall(json.dumps(control).encode() + b'\n')
    parent.close()
    monkeypatch.setenv('API_AGENT_BEDROCK_PROPOSAL_FD', str(child.detach()))
    monkeypatch.delenv('API_AGENT_BEDROCK_AUTHORITY_FD', raising=False)
    monkeypatch.setattr(worker, 'execute', lambda *args, **kwargs: pytest.fail('discovery dispatched'))
    output = io.StringIO()
    worker.main(io.StringIO(json.dumps(worker_request('runtime_models')) + '\n'), output)
    assert json.loads(output.getvalue()) == {'error': 'protocol'}


def test_worker_rejects_well_shaped_proposal_with_changed_message():
    req, payload = native_request()
    payload['body']['messages'][0]['content'][0]['text'] = 'Changed after admission'
    frames = []
    owner = object()
    selection = catalog.selection(MODEL, 'converse', 'runtime')
    with _new_admission_authority(owner).bind(owner, req, selection):
        worker.execute(worker_request('converse', payload), frames.append,
            boto_client_factory=lambda *args: pytest.fail('changed proposal dispatched'), proposal_controls={})
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]
