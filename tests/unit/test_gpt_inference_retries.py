"""Admission recovery never replays potentially executed inference."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from api_agent.llm_providers.gpt import inference as module
from api_agent.providers import progress
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, StructuredOutput, TextContent
from api_agent.providers.progress import report_timeout, progress_sink
from test_gpt_inference import BASE, TOKEN, result

CODE = 'subscription_sharing_user_unavailable'


def rejection(**extra):
    return {'error': {'code': CODE, 'message': 'Temporarily unavailable'}, **extra}


def completed(data=None):
    return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data or result()}) + '\n\n',
                          headers={'content-type': 'text/event-stream'})


@pytest.fixture
def retry_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, waits=[])
    monkeypatch.setattr(progress, '_clock', lambda: clock.now)

    async def wait(delay):
        clock.waits.append(delay)
        clock.now += delay
    monkeypatch.setattr(module, 'sleep', wait)
    return clock


async def complete(handler, *, payload=None, timeout=None, cache_session_id=None, subscription=True, token=TOKEN, base=BASE):
    req = LLMRequest('gpt-example', payload or CompletionRequest((Message('user', (TextContent('Say Ok'),)),),
                                                               generation=GenerationOptions()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with report_timeout('test enclosing request', timeout):
            return await module.complete_text(client=client, base_url=base, access_token=token,
                subscription=subscription, api_mode='responses', request=req, cache_session_id=cache_session_id)


@pytest.mark.parametrize('failures', [1, 4])
def test_exact_backoff_recovers_with_same_selected_request(retry_clock, failures):
    calls, responses = [], []
    session = '00000000-0000-4000-8000-000000000001'
    async def handler(sent):
        calls.append(sent)
        response = httpx.Response(503, json=rejection()) if len(calls) <= failures else completed()
        responses.append(response)
        return response

    value = asyncio.run(complete(handler, cache_session_id=session))

    assert value.payload.content[0].text == 'Ok'
    assert value.usage.input_tokens == 3
    assert retry_clock.waits == [1, 5, 15, 30][:failures]
    assert len(calls) == failures + 1
    assert all(response.is_closed for response in responses)
    assert len({sent.content for sent in calls}) == 1
    assert all(sent.headers['Authorization'] == 'Bearer ' + TOKEN for sent in calls)
    assert all(sent.headers['session-id'] == session for sent in calls)
    assert all(str(sent.url) == BASE + '/responses' for sent in calls)
    assert [sent.extensions['timeout']['read'] for sent in calls] == [120, 119, 114, 99, 69][:failures + 1]


def test_exhaustion_preserves_last_safe_admission_rejection(retry_clock):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json={'error': {'code': CODE, 'message': 'Unavailable ' + TOKEN}},
                              headers={'x-request-id': f'request-{len(calls)}'})

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))

    assert len(calls) == 5
    assert retry_clock.waits == [1, 5, 15, 30]
    assert caught.value.code == CODE and caught.value.status == 503
    assert caught.value.request_id == 'request-5'
    assert caught.value.execution == 'not_started'
    assert caught.value.usage is None
    assert TOKEN not in str(vars(caught.value))


@pytest.mark.parametrize('timeout,elapsed,waits,calls_expected', [
    (60, 0, [1, 5, 15], 4), (20.9, 0, [], 1), (21, 0, [1], 2), (60, 40, [], 1),
])
def test_parent_budget_and_request_time_bound_retry_prefix(retry_clock, timeout, elapsed, waits, calls_expected):
    calls = []
    async def handler(sent):
        calls.append(sent)
        retry_clock.now += elapsed
        return httpx.Response(503, json=rejection())

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler, timeout=timeout))

    assert calls_expected == len(calls)
    assert retry_clock.waits == waits
    assert caught.value.execution == 'not_started'
    assert caught.value.code == CODE
    assert all(sent.extensions['timeout']['read'] <= timeout for sent in calls)


def test_oversleep_consumes_reserve_without_another_request(retry_clock, monkeypatch):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=rejection())
    async def wait(delay):
        retry_clock.waits.append(delay)
        retry_clock.now += delay + 1
    monkeypatch.setattr(module, 'sleep', wait)

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler, timeout=21))

    assert retry_clock.waits == [1]
    assert len(calls) == 1
    assert caught.value.code == CODE and caught.value.execution == 'not_started'


@pytest.mark.parametrize('status,data,subscription', [
    (503, {'error': {'code': 'subscription_sharing_usage_unavailable'}}, True),
    (401, rejection(), True), (403, rejection(), True), (429, rejection(), True),
    (500, rejection(), True), (503, rejection(), False), (503, None, True),
    (503, {'error': CODE}, True), (503, {}, True),
    (503, rejection(usage={'input_tokens': 1}), True),
    (503, rejection(output=[]), True), (503, rejection(id='response-1'), True),
    (503, {'error': {'code': CODE, 'usage': {'input_tokens': 1}}}, True),
    (503, {'error': {'code': CODE.upper()}}, True),
    (503, {'error': {'code': CODE + ' '}}, True),
])
def test_other_rejections_never_retry(retry_clock, status, data, subscription):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(status, json=data)

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler, subscription=subscription))

    assert len(calls) == 1
    assert retry_clock.waits == []
    assert caught.value.execution == 'possibly_executed'


def test_stream_admission_code_never_replays_started_response(retry_clock):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(200, text='data: ' + json.dumps({'type': 'error', **rejection()}) + '\n\n',
                              headers={'content-type': 'text/event-stream'})

    with pytest.raises(ProviderAccessError):
        asyncio.run(complete(handler))

    assert len(calls) == 1
    assert retry_clock.waits == []


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.ReadError])
def test_ambiguous_transport_never_retries(retry_clock, failure):
    calls = []
    async def handler(sent):
        calls.append(sent)
        raise failure('synthetic transport interruption', request=sent)

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))

    assert len(calls) == 1
    assert retry_clock.waits == []
    assert caught.value.execution == 'possibly_executed'


def test_cancellation_during_wait_closes_rejection_without_retry(monkeypatch):
    calls, responses = [], []
    async def scenario():
        waiting = asyncio.Event()
        async def handler(sent):
            calls.append(sent)
            response = httpx.Response(503, json=rejection())
            responses.append(response)
            return response
        async def wait(delay):
            assert delay == 1
            waiting.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(module, 'sleep', wait)
        task = asyncio.create_task(complete(handler))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())

    assert len(calls) == 1
    assert all(response.is_closed for response in responses)


def test_progress_keeps_one_component_across_backoff(retry_clock):
    events, calls = [], []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=rejection()) if len(calls) == 1 else completed()

    with progress_sink(events.append):
        asyncio.run(complete(handler))

    inference = [event for event in events if event.name == 'GPT text inference']
    assert len({event.id for event in inference}) == 1
    assert inference[0].active and not inference[-1].active
    assert inference[-1].elapsed == 1


def test_redacted_admission_code_cannot_trigger_retry(retry_clock):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=rejection())

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler, token=CODE))

    assert len(calls) == 1
    assert retry_clock.waits == []
    assert caught.value.code == '[redacted]'
    assert CODE not in str(vars(caught.value))


def test_structured_response_after_admission_preserves_schema_and_output(retry_clock):
    from test_rich_inference import SCHEMA
    payload = CompletionRequest((Message('user', (TextContent('Question'),)),),
                                structured=StructuredOutput('answer', SCHEMA))
    data = result()
    data['output'][0]['content'][0]['text'] = '{"value":"x"}'
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=rejection()) if len(calls) == 1 else completed(data)

    value = asyncio.run(complete(handler, payload=payload))

    assert value.payload.content == (TextContent('{"value":"x"}'),)
    assert value.usage.input_tokens == 3
    assert retry_clock.waits == [1]
    assert len({sent.content for sent in calls}) == 1
    assert json.loads(calls[0].content)['text']['format']['type'] == 'json_schema'


def test_tool_response_after_admission_returns_one_call_without_tool_execution(retry_clock):
    from test_rich_inference import native, request
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=rejection()) if len(calls) == 1 else completed(native(True))

    value = asyncio.run(complete(handler, payload=request().payload))

    assert value.outcome == 'tool_calls'
    assert len(value.payload.calls) == 1
    assert value.payload.calls[0].name == 'lookup'
    assert value.payload.calls[0].arguments == '{"value":"x"}'
    assert value.usage.input_tokens == 5
    assert retry_clock.waits == [1]
    assert len({sent.content for sent in calls}) == 1


def test_billing_failure_keeps_usage_without_replaying_completed_inference(retry_clock):
    data = result()
    data['usage']['cost'] = -1
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(200, json=data)

    with pytest.raises(ProviderAccessError, match='invalid billing metadata') as caught:
        asyncio.run(complete(handler, subscription=False, base='https://openrouter.ai/api/v1'))

    assert len(calls) == 1
    assert retry_clock.waits == []
    assert caught.value.usage.input_tokens == 3
    assert caught.value.usage.output_tokens == 1
    assert caught.value.execution == 'possibly_executed'


@pytest.mark.parametrize('field,value', [
    ('message', {'usage': {'input_tokens': 1}}),
    ('param', {'id': 'response-1', 'output': []}),
    ('type', ['response.completed']),
    ('message', 1), ('param', True), ('type', []),
    ('message', 'x' * 513), ('type', 'x' * 101), ('param', 'x' * 201),
])
def test_malformed_optional_admission_fields_never_replay(retry_clock, field, value):
    calls = []
    data = rejection()
    data['error'][field] = value
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json=data)

    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))

    assert len(calls) == 1
    assert retry_clock.waits == []
    assert caught.value.execution == 'possibly_executed'
    assert caught.value.code == CODE


@pytest.mark.parametrize('fields', [
    {'message': None, 'type': None, 'param': None},
    {'message': 'x' * 512, 'type': 'x' * 100, 'param': 'x' * 200},
    {'message': '', 'type': '', 'param': ''},
])
def test_valid_optional_admission_fields_recover(retry_clock, fields):
    calls = []
    async def handler(sent):
        calls.append(sent)
        return httpx.Response(503, json={'error': {'code': CODE, **fields}}) if len(calls) == 1 else completed()

    value = asyncio.run(complete(handler))

    assert value.payload.content[0].text == 'Ok'
    assert len(calls) == 2
    assert retry_clock.waits == [1]
