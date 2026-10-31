"""Output rejection is a provider admission fact, never a context estimate."""
from types import SimpleNamespace

import httpx2
import pytest

from api_agent.llm_providers.bedrock import worker
from api_agent.llm_providers.bedrock.output_limits import rejected_maximum
from test_bedrock_runtime_worker import request, RUNTIME_MODEL


@pytest.mark.parametrize('message', [
    'max_output_tokens must be between 1 and 8192',
    'max_tokens must be between 1 and 8192.',
    'max_output_tokens: 200000 is greater than the maximum of 8192',
    "Invalid 'max_tokens': integer above maximum value. Expected a value <= 8192, but got 200000 instead.",
    "1 validation error detected: Value '200000' at 'inferenceConfig.maxTokens' failed to satisfy constraint: Member must have value less than or equal to 8192",
    "Value '200000' at 'maxOutputTokens' failed to satisfy constraint: Member must have value less than or equal to 8192",
])
def test_explicit_parameter_upper_bound(message):
    assert rejected_maximum(400, {'message': message}) == 8192


@pytest.mark.parametrize('message', [
    'Input is too long for requested model.',
    'maximum context length is 8192 tokens',
    'max_tokens must fit the context window of 8192',
    'max_tokens: 200000 is greater than the maximum of 0',
    'max_tokens: 200000 is greater than the maximum of 1048577',
    'max_tokens: 200000 is greater than the maximum of 8192; context overflow',
    'max_tokens: 200000 is greater than the maximum of -1',
    'max_tokens: 200000 is greater than the maximum of 8.5',
])
def test_ambiguous_or_invalid_bounds_are_not_corrections(message):
    assert rejected_maximum(400, {'message': message}) is None


@pytest.mark.parametrize('status', [None, True, '400', 200, 401, 403, 429, 500, 503])
def test_nonvalidation_status_cannot_prove_admission_rejection(status):
    assert rejected_maximum(status, {'message': 'max_tokens: 200000 is greater than the maximum of 8192'}) is None


@pytest.mark.parametrize('endpoint', ['mantle', 'runtime'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('stream', [False, True])
def test_http_sdk_rejection_projects_only_maximum(endpoint, mode, stream):
    frames, calls = [], []
    field = 'max_output_tokens' if mode == 'responses' else 'max_tokens'
    def handle(req):
        calls.append(req)
        return httpx2.Response(400, json={'error': {'type': 'invalid_request_error',
            'message': f'{field}: 200000 is greater than the maximum of 8192'}})
    def http_factory(**kwargs):
        return httpx2.Client(transport=httpx2.MockTransport(handle), timeout=60)
    body = {'model': RUNTIME_MODEL, field: 200000,
            **({'input': 'hello'} if mode == 'responses' else {'messages': [{'role': 'user', 'content': 'hello'}]})}
    payload = {'prefix': '/openai/v1', 'body': body}
    if endpoint == 'runtime':
        payload['endpoint'] = endpoint
    worker.execute(request(mode + ('_stream' if stream else ''), payload), frames.append,
                   http_factory=http_factory, join=lambda values, arguments: values)
    assert len(calls) == 1
    assert frames == [{'error': 'invalid_request', 'execution': 'not_started',
                       'http_status': 400, 'maximum_output_tokens': 8192}]


@pytest.mark.parametrize('operation', ['converse', 'converse_stream'])
def test_converse_rejection_projects_only_maximum(operation):
    from botocore.exceptions import ClientError
    frames = []
    def converse(**kwargs):
        raise ClientError({'Error': {'Code': 'ValidationException', 'Message':
            "Value '200000' at 'inferenceConfig.maxTokens' failed to satisfy constraint: Member must have value less than or equal to 8192"},
            'ResponseMetadata': {'HTTPStatusCode': 400}}, 'Converse')
    client = SimpleNamespace(converse=converse, close=lambda: None)
    body = {'modelId': 'operator.model-v2:1', 'messages': [], 'inferenceConfig': {'maxTokens': 200000}}
    worker.execute(request(operation, {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}), frames.append,
                   boto_client_factory=lambda *args: client, join=lambda values, arguments: values)
    assert frames == [{'error': 'invalid_request', 'execution': 'not_started',
                       'http_status': 400, 'maximum_output_tokens': 8192}]


@pytest.mark.parametrize('status,detail', [
    (500, {'message': 'max_output_tokens: 200000 is greater than the maximum of 8192'}),
    (400, {'message': 'maximum context length is 8192 tokens'}),
    (400, {'message': 'max_output_tokens: 200000 is greater than the maximum of 8192', 'usage': {}}),
])
def test_http_failure_does_not_invent_output_admission_fact(status, detail):
    frames = []
    def http_factory(**kwargs):
        return httpx2.Client(transport=httpx2.MockTransport(lambda req:
            httpx2.Response(status, json={'error': detail})), timeout=60)
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1',
        'body': {'model': RUNTIME_MODEL, 'input': 'hello', 'max_output_tokens': 200000}}),
        frames.append, http_factory=http_factory, join=lambda values, arguments: values)
    assert all('maximum_output_tokens' not in frame for frame in frames)
    assert frames[-1]['execution'] == 'possibly_executed'


@pytest.mark.parametrize('detail', [None, [], {}, {'message': None}, {'message': 8192},
    {'message': 'a' * 513}, {'message': 'max_tokens: 200000 is greater than the maximum of 8192', 'output': []}])
def test_unstructured_or_inference_evidence_is_not_a_limit(detail):
    assert rejected_maximum(400, detail) is None
