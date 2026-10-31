"""Maintained SDK serialization preserves stateless native tool state offline."""
import json

import httpx2
import pytest

from api_agent.llm_providers.bedrock import worker
from api_agent.providers.inference import result_from_wire
from test_bedrock_mantle import MODEL, response
from test_bedrock_responses_continuation import CIPHER, native_output, stream_events


@pytest.mark.parametrize('endpoint', ['mantle', 'runtime'])
@pytest.mark.parametrize('streaming', [False, True])
def test_actual_sdk_requests_encrypted_state_and_retains_native_tool_result(endpoint, streaming):
    selected = MODEL if endpoint == 'mantle' else 'us.openai.gpt-6-luna'
    sent, frames = [], []
    def handle(request):
        sent.append(json.loads(request.content))
        if streaming:
            events = stream_events()
            for event in events:
                if 'response' in event:
                    event['response']['model'] = selected
            body = ''.join('data: ' + json.dumps(row) + '\n\n' for row in events)
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'},
                                   content=body + 'data: [DONE]\n\n')
        raw = response(native_output())
        raw['model'] = selected
        return httpx2.Response(200, json=raw)
    payload = dict(prefix='/v1' if endpoint == 'mantle' else '/openai/v1',
        body=dict(model=selected, input='hello', include=['reasoning.encrypted_content']))
    if endpoint == 'runtime':
        payload['endpoint'] = endpoint
    worker.execute(dict(timeout=60, operation='responses_stream' if streaming else 'responses',
        auth=dict(mode='api_key', api_key='synthetic-selected-token', region='us-east-1'), payload=payload),
        frames.append, http_factory=lambda **kw: httpx2.Client(transport=httpx2.MockTransport(handle)))
    assert len(sent) == 1 and sent[0]['include'] == ['reasoning.encrypted_content']
    assert sent[0]['store'] is False
    if streaming:
        result = result_from_wire(frames[-1]['event']['result'])
        assert json.loads(result.payload.continuation)['state'][0]['encrypted_content'] == CIPHER
        assert result.payload.calls[0].name == 'lookup'
    else:
        assert frames[-1]['result']['output'][0]['encrypted_content'] == CIPHER


@pytest.mark.parametrize('include', [None, [], ['unknown'], ['reasoning.encrypted_content', 'unknown']])
def test_worker_rejects_unadmitted_include_before_auth_or_http(include):
    frames = []
    def forbidden(**kwargs):
        pytest.fail('invalid include reached credentials or HTTP')
    worker.execute(dict(timeout=60, operation='responses', auth=dict(mode='session'),
        payload=dict(prefix='/v1', body=dict(model=MODEL, input='hello', include=include))), frames.append,
        session_factory=forbidden, http_factory=forbidden)
    assert frames == [dict(error='unknown', execution='not_started')]
