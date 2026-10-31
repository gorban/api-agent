"""Observed dispatch, authored conversation bounds and logical payload capture."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import Message, TextContent
from api_agent.providers.progress import report_timeout
from api_agent.workflows.inference_capture import bind_response_dispatch
from test_provider_response_policy import BASE, SHAPE, request, run_gpt


@pytest.mark.parametrize('provider', ['gpt', 'bedrock', 'bedrock_stream'])
def test_expired_inherited_budget_has_no_unobserved_actual_response_mode(monkeypatch, provider):
    from api_agent.llm_providers.gpt import inference
    from api_agent.llm_providers.bedrock import factory
    from api_agent.providers import progress
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    calls = []
    async def http(sent):
        calls.append(sent)
        raise AssertionError('expired operation dispatched HTTP')
    async def stream(*args, **kwargs):
        calls.append(args)
        yield {}
    transport = SimpleNamespace(call=AsyncMock(side_effect=AssertionError('expired operation dispatched')), stream=stream)
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: transport)
    async def run():
        with bind_response_dispatch() as observed:
            async with report_timeout('expired parent', 1):
                clock[0] = 2.0
                if provider == 'gpt':
                    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                        with pytest.raises(ProviderAccessError) as caught:
                            await inference.complete_text(client=client, base_url=BASE, access_token='synthetic-token',
                                subscription=False, api_mode='responses', request=request('json'))
                    assert caught.value.kind == 'deadline'
                else:
                    implementation = factory.factory({'BEDROCK_API_MODE': 'completions'}, {})
                    with pytest.raises(TimeoutError) as caught:
                        if provider == 'bedrock_stream':
                            _ = [part async for part in implementation.astream(request('json', model='zai.glm-5'))]
                        else:
                            await implementation.acomplete(request('json', model='zai.glm-5'))
                assert getattr(caught.value, 'response', None) is None
                assert observed['response'] is None and observed['usage'] is None
    asyncio.run(run())
    assert calls == [] and transport.call.await_count == 0


@pytest.mark.parametrize('api_mode', ['responses', 'completions'])
@pytest.mark.parametrize('mode', ['json', 'prompted_json', 'structured'])
def test_maximum_authored_conversation_keeps_every_row_and_one_encoded_instruction(api_mode, mode):
    req = request(mode, shape='{"type":"object"}' if mode == 'structured' else SHAPE)
    authored = tuple(Message('user' if index % 2 == 0 else 'assistant', (TextContent(f'authored-{index}'),)) for index in range(64))
    req = replace(req, payload=replace(req.payload, messages=authored))
    assert len(request_to_wire(req)['messages']) == 64
    result, calls = run_gpt(req, api_mode=api_mode)
    body = json.loads(calls[0].content)
    rows = body['input' if api_mode == 'responses' else 'messages']
    assert len(rows) == 65 and rows[:64] == [{'role': row.role, 'content': row.content[0].text} for row in authored]
    assert rows[-1]['role'] == 'user' and rows[-1]['content'].endswith(req.payload.response.schema.schema)
    assert result.response.actual_mode == ('json' if mode == 'structured' else mode)
    assert req.payload.messages is authored
    with pytest.raises(ContractError):
        request_to_wire(replace(req, payload=replace(req.payload, messages=(*authored, authored[-1]))))


def test_retryable_subscription_admission_captures_one_logical_effective_body(monkeypatch):
    from api_agent.llm_providers.gpt import inference
    from api_agent.providers import inference_retry
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    calls = []
    async def handler(sent):
        calls.append(json.loads(sent.content))
        if len(calls) in (1, 3):
            return httpx.Response(503, json={'error': {'code': 'subscription_sharing_user_unavailable', 'message': 'synthetic admission'}})
        data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': '{"answer":"yes"}'}]}],
            'usage': {'input_tokens': 3, 'output_tokens': 2}}
        return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n',
            headers={'content-type': 'text/event-stream'})
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda number, hint: 0)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def physical(req, *, auth):
                return await inference.complete_text(client=client, base_url=BASE, access_token=auth,
                    subscription=True, api_mode='responses', request=req)
            async def call(callback):
                return await callback(auth='synthetic-token')
            owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=call, aclose=AsyncMock()),
                                     complete=physical, captures_effective_payload=True)
            try:
                with collect_inference(EmissionBoundary(ContentPolicy(True))) as captured:
                    result = await owner.complete(request(model='gpt-6-luna'))
                    await owner.complete(request(model='gpt-6-luna'))
            finally:
                await owner.aclose()
            return result, captured
    result, captured = asyncio.run(run())
    assert len(calls) == 4 and all(body == calls[0] for body in calls)
    assert result.response.actual_mode == 'structured'
    inputs = [row for row in captured['rows'] if row['phase'] == 'request']
    assert len(inputs) == 2
    assert all(row['content']['provider_payload'] == calls[0] for row in inputs)


def test_logical_capture_keeps_changed_effective_input():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_inference_call, capture_inference, collect_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as captured:
        with bind_inference_call('logical-call'):
            capture_inference('input', {'value': 'first'}, 'physical')
            capture_inference('input', {'value': 'second'}, 'physical')
    assert [row['content']['value'] for row in captured['rows']] == ['first', 'second']


@pytest.mark.parametrize('api_mode', ['responses', 'completions'])
@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json', 'text'])
def test_bedrock_maximum_authored_rows_fit_bounded_effective_payload(monkeypatch, api_mode, mode):
    from api_agent.llm_providers.bedrock import factory
    model = 'openai.gpt-oss-20b' if api_mode == 'responses' else 'zai.glm-5'
    req = request(mode, model=model)
    authored = tuple(Message('user', (TextContent(f'row-{index}'),)) for index in range(64))
    req = replace(req, payload=replace(req.payload, messages=authored))
    request_to_wire(req)
    content = '{"answer":"yes"}'
    response = ({'model': model, 'id': 'one', 'status': 'completed',
        'output': [{'id': 'message', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': content, 'annotations': []}]}],
        'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}
        if api_mode == 'responses' else {'model': model,
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: SimpleNamespace(call=call))
    result = asyncio.run(factory.factory({'BEDROCK_API_MODE': api_mode}, {}).acomplete(req))
    body = call.call_args.args[1]['body']
    rows = body['input' if api_mode == 'responses' else 'messages']
    inserted = result.response.actual_mode == 'prompted_json'
    assert len(rows) == 64 + int(inserted)
    assert rows[:64] == [{'role': 'user', 'content': row.content[0].text} for row in authored]
    if inserted:
        assert rows[-1]['content'].endswith(SHAPE)
    assert req.payload.messages is authored and call.await_count == 1


def test_encoded_instruction_is_single_bounded_extra_and_public_author_limit_stays_64():
    from api_agent.llm_providers.response_policy import format_payload
    from test_provider_response_policy import prepare
    req = request('json')
    prepared, evidence = prepare(req)
    assert prepared.payload.messages is req.payload.messages
    body = {'input': [{'role': 'user', 'content': 'authored'}]}
    formatted = format_payload(req, body, evidence)
    assert len(formatted['input']) == 2 and len(body['input']) == 1
    assert formatted['input'][0] is body['input'][0]
    assert formatted['input'][1]['content'].endswith(SHAPE)
    with pytest.raises(ContractError, match='byte limit'):
        format_payload(req, {'input': [{'role': 'user', 'content': 'x' * 2097152}]}, evidence)


@pytest.mark.parametrize('provider', ['gpt', 'bedrock', 'bedrock_stream'])
def test_budget_expiring_during_optional_capture_does_not_admit_transport(monkeypatch, provider):
    from api_agent.providers import progress
    from api_agent.llm_providers.gpt import inference
    from api_agent.llm_providers.bedrock import factory
    clock = [0.0]
    calls = []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    def capture(*args, **kwargs):
        clock[0] = 2.0
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', capture)
    async def http(sent):
        calls.append(sent)
        raise AssertionError('expired capture dispatched HTTP')
    async def call(*args, **kwargs):
        kwargs['on_dispatch']()
        calls.append(args)
        raise AssertionError('expired capture dispatched transport')
    async def stream(*args, **kwargs):
        kwargs['on_dispatch']()
        calls.append(args)
        yield {}
    transport = SimpleNamespace(call=AsyncMock(side_effect=call), stream=stream)
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: transport)
    async def run():
        with bind_response_dispatch() as observed:
            async with report_timeout('positive parent', 1):
                if provider == 'gpt':
                    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                        with pytest.raises(ProviderAccessError) as caught:
                            await inference.complete_text(client=client, base_url=BASE, access_token='synthetic-token',
                                subscription=False, api_mode='responses', request=request('json'))
                else:
                    implementation = factory.factory({'BEDROCK_API_MODE': 'completions'}, {})
                    with pytest.raises(TimeoutError) as caught:
                        if provider == 'bedrock_stream':
                            _ = [part async for part in implementation.astream(request('json', model='zai.glm-5'))]
                        else:
                            await implementation.acomplete(request('json', model='zai.glm-5'))
                assert getattr(caught.value, 'response', None) is None and observed['response'] is None
    asyncio.run(run())
    assert calls == []
    assert transport.call.await_count == (1 if provider == 'bedrock' else 0)


def test_private_http_attempt_refuses_an_already_expired_budget_without_capture(monkeypatch):
    from api_agent.llm_providers.gpt.inference import _complete_attempt
    calls = []
    captures = []
    async def handler(sent):
        calls.append(sent)
        raise AssertionError('expired HTTP attempt dispatched')
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *a, **kw: captures.append(a))
    async def run():
        dispatch = {'response': None, 'captured': False}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TimeoutError):
                await _complete_attempt(client=client, base_url=BASE, headers={}, body={}, subscription=False,
                    api_mode='responses', request=request('json'), access_token='synthetic-token',
                    budget=SimpleNamespace(remaining=0), dispatch=dispatch)
        assert dispatch == {'response': None, 'captured': False}
    asyncio.run(run())
    assert calls == [] and captures == []


@pytest.mark.parametrize('provider,api_mode', [('gpt', 'responses'), ('gpt', 'completions'),
    ('bedrock', 'responses'), ('bedrock', 'completions')])
def test_encoded_instruction_preserves_prior_provider_serialization(provider, api_mode):
    from api_agent.llm_providers.response_policy import format_payload
    from test_provider_response_policy import prepare
    req = request('prompted_json', model='gpt-6-sol' if provider == 'gpt' else 'openai.gpt-oss-20b')
    instruction = Message('user', (TextContent(
        'Return a complete final answer containing only JSON satisfying this response schema. '
        'Include the final JSON answer even when reasoning is enabled; reasoning is not the answer.\n'
        + SHAPE),))
    legacy = replace(req, payload=replace(req.payload, response=None, messages=(*req.payload.messages, instruction)))
    if provider == 'gpt':
        from api_agent.llm_providers.gpt.inference import validate_text_request
        _, expected = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False,
            api_mode=api_mode, request=legacy)
        _, actual = validate_text_request(base_url=BASE, access_token='synthetic-token', subscription=False,
            api_mode=api_mode, request=req)
    else:
        from api_agent.llm_providers.bedrock.response_policy import prepare_response
        from api_agent.llm_providers.bedrock.mantle import encode
        prepared, evidence = prepare_response(req, api_mode)
        expected = encode(legacy, api_mode)
        actual = format_payload(req, encode(prepared, api_mode), evidence)
    assert actual == expected


def test_bedrock_encode_expiry_stops_before_optional_capture_and_transport(monkeypatch):
    from api_agent.llm_providers.bedrock import factory, mantle
    from api_agent.providers import progress
    from api_agent.providers.inference import error_usage
    clock = [0.0]
    captures = []
    encoded = []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    original_encode = mantle.encode
    def encode(value, api_mode, **kwargs):
        body = original_encode(value, api_mode, **kwargs)
        encoded.append(True)
        clock[0] = 2.0
        return body
    monkeypatch.setattr(mantle, 'encode', encode)
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *a, **kw: captures.append(a))
    transport = SimpleNamespace(call=AsyncMock(side_effect=AssertionError('expired encoding dispatched')))
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: transport)
    async def run():
        with bind_response_dispatch() as observed:
            async with report_timeout('positive parent', 1):
                implementation = factory.factory({'BEDROCK_API_MODE': 'completions'}, {})
                with pytest.raises(TimeoutError) as caught:
                    await implementation.acomplete(request('json', model='zai.glm-5'))
            assert getattr(caught.value, 'response', None) is None and error_usage(caught.value) is None
            assert observed['response'] is None and observed['usage'] is None
            assert asyncio.current_task().cancelling() == 0
    asyncio.run(run())
    assert encoded == [True] and captures == []
    transport.call.assert_not_awaited()


def test_bedrock_stream_read_entry_expiry_closes_owner_without_capture_or_read(monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.llm_providers.bedrock import factory
    from api_agent.providers import progress
    from api_agent.providers.inference import error_usage
    clock = [0.0]
    captures = []
    reads = []
    closed = []
    entered = []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    original_timeout = factory.component_timeout
    @asynccontextmanager
    async def timeout(name, duration=None):
        async with original_timeout(name, duration) as budget:
            if name == 'Bedrock stream read':
                assert budget.remaining > 0
                entered.append(True)
                clock[0] = 2.0
            yield budget
    monkeypatch.setattr(factory, 'component_timeout', timeout)
    class ControlledStream:
        def __aiter__(self):
            return self
        async def __anext__(self):
            reads.append(True)
            raise AssertionError('expired stream admitted a read')
        async def aclose(self):
            closed.append(True)
    stream = ControlledStream()
    transport = SimpleNamespace(stream=lambda *a, **kw: stream)
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: transport)
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *a, **kw: captures.append(a))
    async def run():
        with bind_response_dispatch() as observed:
            async with report_timeout('positive parent', 1):
                implementation = factory.factory({'BEDROCK_API_MODE': 'completions'}, {})
                with pytest.raises(TimeoutError) as caught:
                    _ = [part async for part in implementation.astream(request('json', model='zai.glm-5'))]
            assert getattr(caught.value, 'response', None) is None and error_usage(caught.value) is None
            assert observed['response'] is None and observed['usage'] is None
            assert asyncio.current_task().cancelling() == 0
    asyncio.run(run())
    assert entered == [True] and captures == [] and reads == [] and closed == [True]
