"""Observable tool carriers are protected before response evidence is emitted."""
import asyncio
import json

import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import Tool
from api_agent.telemetry.privacy import EmissionBoundary
from test_gpt_inference import BASE, TOKEN, request


@pytest.mark.parametrize('mode,stream', [('responses', False), ('completions', False), ('responses', True)])
@pytest.mark.parametrize('escaped', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('protected', [False, True])
@pytest.mark.parametrize('selected', [False, True])
def test_split_tool_carriers_before_capture(monkeypatch, mode, stream, escaped, reverse, protected, selected):
    authority = EmissionBoundary()
    secret = TOKEN if selected else 'synthetic-carrier-secret'
    authority.register_secret(secret if not selected else 'synthetic-unrelated-secret')
    prefix, suffix = secret.rsplit('-', 1)
    prefix += '-'
    text, leaf = (suffix, prefix) if reverse else (prefix, suffix)
    if not protected:
        leaf = 'ordinary'
        text = 'safe-' + text if reverse else text
    arguments = json.dumps({'nested': [{'value': leaf}]})
    if escaped:
        arguments = arguments.replace('s', '\\u0073')
    assert json.loads(arguments)['nested'][0]['value'] == leaf
    usage = {'input_tokens': 7, 'output_tokens': 3} if mode == 'responses' else {'prompt_tokens': 7, 'completion_tokens': 3}
    data = ({'status': 'completed', 'output': [
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]},
        {'type': 'function_call', 'status': 'completed', 'call_id': 'call_1', 'name': 'tool', 'arguments': arguments}], 'usage': usage}
        if mode == 'responses' else {'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': text, 'tool_calls': [{'id': 'call_1', 'type': 'function',
            'function': {'name': 'tool', 'arguments': arguments}}]}}], 'usage': usage})
    captured = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda direction, payload, *a, **k:
                        captured.append(payload) if direction == 'output' else None)
    calls = []
    def respond(sent):
        calls.append(sent)
        if stream:
            return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n',
                                  headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=data)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN,
                subscription=stream, api_mode=mode, authority=authority,
                request=request(maximum=None if stream else 64, tools=(Tool('tool', 'Test tool', '{"type":"object","properties":{"nested":{"type":"array","items":{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}}},"required":["nested"],"additionalProperties":false}'),)))
    if protected:
        with pytest.raises(ProviderAccessError) as raised:
            asyncio.run(execute())
        assert raised.value.usage.input_tokens == 7
        assert raised.value.usage.output_tokens == 3
        assert raised.value.http_status == 200
        assert captured == []
    else:
        result = asyncio.run(execute())
        assert result.payload.calls[0].arguments == arguments
        assert result.payload.content[0].text == text
        assert captured == [data]
    assert len(calls) == 1


def test_raw_extra_registered_after_initial_guard_is_not_captured(monkeypatch):
    authority = EmissionBoundary()
    secret = 'synthetic-late-extra-secret'
    data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'Safe'}]}],
            'usage': {'input_tokens': 7, 'output_tokens': 3}, 'provider_extra': secret}
    original = inference._guard_content
    registered = False
    async def guard(value, *args, **kwargs):
        nonlocal registered
        await original(value, *args, **kwargs)
        if type(value) is dict and 'provider_extra' in value and not registered:
            registered = True
            authority.register_secret(secret)
    monkeypatch.setattr(inference, '_guard_content', guard)
    captured = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference',
                        lambda direction, payload, *a, **k: captured.append(payload) if direction == 'output' else None)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
            return await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN,
                subscription=False, api_mode='responses', authority=authority, request=request())
    with pytest.raises(ProviderAccessError) as raised:
        asyncio.run(execute())
    assert registered and captured == []
    assert raised.value.usage.input_tokens == 7 and raised.value.usage.output_tokens == 3
    assert raised.value.http_status == 200


def test_invalid_subscription_terminal_preserves_original_failure_without_capture(monkeypatch):
    authority = EmissionBoundary()
    authority.register_secret('synthetic-carrier-secret')
    terminal = {'status': 'incomplete', 'incomplete_details': {'reason': 'other'}, 'output': [
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'synthetic-carrier-'}]},
        {'type': 'function_call', 'status': 'completed', 'call_id': 'call_1', 'name': 'tool',
         'arguments': '{"value":"secret"}'}], 'usage': {'input_tokens': 7, 'output_tokens': 3}}
    captured = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference',
                        lambda direction, payload, *a, **k: captured.append(payload) if direction == 'output' else None)
    async def execute():
        response = httpx.Response(200, text='data: ' + json.dumps({'type': 'response.incomplete', 'response': terminal}) + '\n\n',
                                  headers={'content-type': 'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            return await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN,
                subscription=True, api_mode='responses', authority=authority, request=request(maximum=None))
    with pytest.raises(ProviderAccessError, match='GPT inference returned an incomplete response.') as raised:
        asyncio.run(execute())
    assert raised.value.kind == 'unknown'
    assert raised.value.usage.input_tokens == 7 and raised.value.usage.output_tokens == 3
    assert captured == []


def test_observable_carriers_exclude_keys_and_independent_values():
    from api_agent.providers.response_carriers import observable_response_carriers
    arguments = [{'ignored-key': [None, 1, True, {'other-key': 'value'}], 'second': 'other'}]
    assert list(observable_response_carriers('text', arguments)) == [
        'textvalue', 'valuetext', 'textother', 'othertext']


def test_observable_carrier_work_limit_rejects_next_pair_before_release():
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.response_carriers import observable_response_carriers
    text = 'x' * (2 * 1024 * 1024)
    value = 'y' * (2 * 1024 * 1024)
    carriers = observable_response_carriers(text, [value, ''])
    assert next(carriers) == text + value
    assert next(carriers) == value + text
    with pytest.raises(ContractError, match='Credential protection limit exceeded'):
        next(carriers)
