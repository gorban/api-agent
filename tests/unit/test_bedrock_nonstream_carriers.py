"""Canonical response carriers are guarded before the SDK worker publishes IPC."""
import json
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import worker
from test_bedrock_mantle import MODEL, call, completion, message, response


@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('text,argument,blocked', [('secret-', 'canary', True), ('canary', 'secret-', True), ('safe text', 'safe argument', False)])
def test_nonstream_text_and_nested_tool_value_cannot_reassemble_credential(mode, text, argument, blocked):
    tool = call(mode == 'responses')
    arguments = json.dumps({'nested': [{'value': argument}]})
    if mode == 'responses':
        tool['arguments'] = arguments
        raw = response([message(text), tool])
    else:
        tool['function']['arguments'] = arguments
        raw = completion()
        raw['choices'][0].update(message={'role': 'assistant', 'content': text, 'tool_calls': [tool]}, finish_reason='tool_calls')
    raw.update(id='observed', service_tier='default', system_fingerprint='observed-revision')
    result = SimpleNamespace(model_dump=lambda **kw: raw)
    create = lambda **kw: result
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create), close=lambda: None)
    http = SimpleNamespace(close=lambda: None)
    request = {'timeout': 30, 'operation': mode,
        'auth': {'mode': 'api_key', 'api_key': 'secret-canary', 'region': 'us-east-1'},
        'payload': {'prefix': '/v1', 'body': {'model': MODEL,
            **({'messages': []} if mode == 'completions' else {'input': 'hello'})}}}
    frames = []
    worker.execute(request, frames.append, client_factory=lambda *args: client, http_factory=lambda **kw: http)
    if not blocked:
        assert len(frames) == 1 and frames[0]['result'] == raw
        assert set(frames[0]) == {'result', 'diagnostics'}
        return
    assert len(frames) == 1 and 'error' in frames[0]
    assert 'result' not in frames[0] and 'provider_response' not in frames[0]
    assert frames[0]['usage']['input_tokens'] == 2
    assert 'secret-' not in json.dumps(frames) and 'canary' not in json.dumps(frames)
