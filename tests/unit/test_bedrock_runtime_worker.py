"""Offline Runtime dispatch, exact selected identity and bounded discovery."""
from types import SimpleNamespace
import json
import time

import httpx2
import pytest
import pytest_asyncio

from api_agent.llm_providers.bedrock import runtime_worker, worker
from test_bedrock_mantle import MODEL, completion, response, message

RUNTIME_MODEL = 'us.openai.gpt-6-sol'
AUTH = {'mode': 'api_key', 'api_key': 'synthetic-runtime-key', 'region': 'us-east-1'}


def request(operation='runtime_models', payload=None, auth=None):
    return {'operation': operation, 'auth': auth or AUTH, 'payload': {} if payload is None else payload, 'timeout': 60}


def test_runtime_catalog_pages_exact_ids_after_authority_join_and_closes():
    calls, frames = [], []
    class Client:
        def list_foundation_models(self):
            calls.append('models')
            return {'modelSummaries': [{'modelId': 'public.model-v1', 'inferenceTypesSupported': ['ON_DEMAND']}]}
        def list_inference_profiles(self, **kwargs):
            calls.append(kwargs)
            if not kwargs:
                return {'inferenceProfileSummaries': [{'inferenceProfileId': 'us.public.model-v1'}], 'nextToken': 'page2'}
            return {'inferenceProfileSummaries': [{'inferenceProfileId': 'exact.profile:1'}]}
        def close(self):
            calls.append('close')
    def factory(region, service, options, timeout):
        assert calls == ['join']
        assert (region, service, options) == ('us-east-1', 'bedrock', {'api_key': AUTH['api_key']})
        assert 0 < timeout <= 60
        return Client()
    def join(values, arguments):
        assert values == [AUTH['api_key']] and arguments == {}
        calls.append('join')
        return values
    worker.execute(request(), frames.append, boto_client_factory=factory, join=join)
    assert calls == ['join', 'models', {}, {'nextToken': 'page2'}, 'close']
    assert frames == [{'result': {'data': [{'id': 'public.model-v1'}, {'id': 'us.public.model-v1'}, {'id': 'exact.profile:1'}]}}]


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('effort', [None, 'medium'])
def test_runtime_openai_uses_runtime_host_and_selected_key(mode, effort):
    seen, frames, owners = [], [], []
    def handle(req):
        seen.append(req)
        raw = dict(response([message()]), object='response', created_at=0) if mode == 'responses' else dict(completion(), id='c', object='chat.completion', created=0)
        raw['model'] = RUNTIME_MODEL
        return httpx2.Response(200, json=raw)
    def http_factory(**kwargs):
        client = httpx2.Client(transport=httpx2.MockTransport(handle), timeout=60)
        owners.append(client)
        return client
    body = {'model': RUNTIME_MODEL, **({'input': 'hello'} if mode == 'responses' else {'messages': [{'role': 'user', 'content': 'hello'}]})}
    if effort is not None:
        body.update({'reasoning': {'effort': effort}} if mode == 'responses' else {'reasoning_effort': effort})
    worker.execute(request(mode, {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': body}), frames.append, http_factory=http_factory)
    assert len(seen) == 1 and owners[0].is_closed
    assert str(seen[0].url) == 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/' + ('responses' if mode == 'responses' else 'chat/completions')
    assert seen[0].headers['authorization'] == 'Bearer synthetic-runtime-key'
    assert 'result' in frames[0]
    sent = json.loads(seen[0].content)
    if effort is not None:
        assert sent.get('reasoning', {}).get('effort') == effort if mode == 'responses' else sent['reasoning_effort'] == effort


@pytest.mark.parametrize('payload', [
    {'endpoint': 'runtime', 'prefix': '/v1', 'body': {}},
    {'endpoint': 'other', 'prefix': '/openai/v1', 'body': {}},
    {'endpoint': 'mantle', 'prefix': '/converse', 'body': {}},
])
def test_invalid_endpoint_routes_reject_before_credentials(payload):
    frames = []
    def forbidden(**kwargs):
        pytest.fail('invalid route selected credentials')
    worker.execute(request('completions', payload, auth={'mode': 'session'}), frames.append, session_factory=forbidden)
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('page', [None, {}, {'modelSummaries': [None]}, {'modelSummaries': [{'modelId': ''}]}])
def test_malformed_runtime_catalog_is_rejected(page):
    client = SimpleNamespace(list_foundation_models=lambda: page)
    with pytest.raises(ValueError, match='Invalid Bedrock'):
        runtime_worker.catalog(client, time.monotonic()+10)


def test_catalog_repeated_continuation_is_bounded():
    calls = []
    def page(**kwargs):
        calls.append(kwargs)
        return {'modelSummaries': [], 'nextToken': 'same'}
    with pytest.raises(ValueError, match='continuation'):
        runtime_worker.catalog(SimpleNamespace(list_foundation_models=page), time.monotonic()+10)
    assert calls == [{}, {'nextToken': 'same'}]


def test_expired_catalog_does_not_dispatch():
    client = SimpleNamespace(list_foundation_models=lambda: pytest.fail('expired dispatch'))
    with pytest.raises(TimeoutError):
        runtime_worker.catalog(client, time.monotonic()-1)


@pytest.mark.parametrize('mode', ['api_key', 'temporary'])
def test_actual_boto_request_uses_only_selected_identity_without_retries(monkeypatch, mode):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'ambient-access')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'ambient-secret')
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'ambient-bearer')
    options = {'api_key': 'selected-bearer'} if mode == 'api_key' else {'api_key': None, 'access_key_id': 'selected-access', 'secret_access_key': 'selected-secret', 'session_token': 'selected-session'}
    client = runtime_worker.boto_client('us-east-1', 'bedrock-runtime', options, 30)
    seen = []
    class Stopped(Exception):
        pass
    def stop(request, **kwargs):
        seen.append(request)
        raise Stopped()
    client.meta.events.register('before-send.bedrock-runtime.Converse', stop)
    try:
        with pytest.raises(Stopped):
            client.converse(modelId='public.model-v1', messages=[{'role': 'user', 'content': [{'text': 'hello'}]}])
        assert len(seen) == 1
        header = seen[0].headers['Authorization'].decode()
        if mode == 'api_key':
            assert header == 'Bearer selected-bearer'
        else:
            assert 'Credential=selected-access/' in header
            assert seen[0].headers['X-Amz-Security-Token'] == b'selected-session'
        assert 'ambient' not in header
        assert client.meta.config.retries['total_max_attempts'] == 1
        assert client.meta.config.connect_timeout == 10
        assert client.meta.config.read_timeout == 30
    finally:
        client.close()


CONVERSE_MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'


def converse_response(text='hello'):
    return {'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}},
            'stopReason': 'end_turn', 'usage': {'inputTokens': 2, 'outputTokens': 3, 'totalTokens': 5}}


@pytest.mark.parametrize('operation', ['converse', 'converse_stream'])
@pytest.mark.parametrize('structured', [False, True])
def test_converse_validates_before_buffered_publication_and_closes(operation, structured):
    seen, frames = [], []
    raw = converse_response()
    body = {'modelId': CONVERSE_MODEL, 'messages': [{'role': 'user', 'content': [{'text': 'hello'}]}],
            'inferenceConfig': {'maxTokens': 100}}
    if structured:
        body['outputConfig'] = {'textFormat': {'type': 'json_schema', 'structure': {'jsonSchema': {'name': 'answer', 'schema': json.dumps(SCHEMA)}}}}
        raw['output']['message']['content'][0]['text'] = '{}'
    class Client:
        def converse(self, **kwargs):
            seen.append(kwargs)
            return raw
        def close(self):
            seen.append('closed')
    worker.execute(request(operation, {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}), frames.append,
                   boto_client_factory=lambda *args: Client())
    assert seen == [body, 'closed']
    if operation == 'converse':
        assert frames[0]['result'] == raw
    else:
        assert frames[0] == {'event': {'kind': 'text', 'text': '{}' if structured else 'hello'}}
        assert frames[1]['event']['kind'] == 'completed'
        assert frames[1]['event']['result']['usage']['input_tokens'] == 2


@pytest.mark.parametrize('operation', ['converse', 'converse_stream'])
def test_converse_joined_credential_in_split_caller_carriers_never_publishes(operation):
    frames, closed = [], []
    raw = converse_response('synthetic-runtime-')
    raw['output']['message']['content'].append({'toolUse': {'toolUseId': 'call1', 'name': 'lookup', 'input': {'value': 'key'}}})
    raw['stopReason'] = 'tool_use'
    client = SimpleNamespace(converse=lambda **kwargs: raw, close=lambda: closed.append(True))
    worker.execute(request(operation, {'endpoint': 'runtime', 'prefix': '/converse', 'body': {'modelId': CONVERSE_MODEL, 'messages': []}}),
                   frames.append, boto_client_factory=lambda *args: client, join=lambda values, arguments: values)
    assert closed == [True]
    assert len(frames) == 1 and frames[0]['code'] == 'response_safety_rejected'
    assert frames[0]['usage']['output_tokens'] == 3
    assert 'result' not in frames[0] and 'event' not in frames[0] and 'provider_response' not in frames[0]


@pytest.mark.parametrize('body', [
    {'modelId': CONVERSE_MODEL, 'messages': [], 'additionalModelRequestFields': {'unadmitted': True}},
    {'modelId': None, 'messages': []},
])
def test_unadmitted_converse_body_rejects_before_credentials(body):
    frames = []
    worker.execute(request('converse', {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}, auth={'mode': 'session'}),
                   frames.append, session_factory=lambda **kwargs: pytest.fail('unadmitted body used credentials'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


def test_explicit_runtime_key_control_plane_denial_never_falls_back():
    from botocore.exceptions import ClientError
    calls, frames = [], []
    def denied():
        calls.append('selected')
        raise ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'not exported'},
                           'ResponseMetadata': {'HTTPStatusCode': 403}}, 'ListFoundationModels')
    client = SimpleNamespace(list_foundation_models=denied, close=lambda: calls.append('closed'))
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: client)
    assert calls == ['selected', 'closed']
    assert frames == [{'error': 'authorization', 'execution': 'possibly_executed', 'http_status': 403}]


def test_boto_ignores_configured_service_endpoint(monkeypatch):
    monkeypatch.setenv('AWS_ENDPOINT_URL_BEDROCK_RUNTIME', 'https://wrong.invalid')
    client = runtime_worker.boto_client('us-east-1', 'bedrock-runtime', {'api_key': 'selected'}, 10)
    try:
        assert client.meta.endpoint_url == 'https://bedrock-runtime.us-east-1.amazonaws.com'
    finally:
        client.close()


SCHEMA = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_strict_schema_carrier_is_admitted(mode):
    definition = {'name': 'answer', 'strict': True, 'schema': SCHEMA}
    value = {'format': {'type': 'json_schema', **definition}} if mode == 'responses' else {'type': 'json_schema', 'json_schema': definition}
    worker.qualified_schema(value, RUNTIME_MODEL, '/openai/v1', endpoint='runtime', response=mode == 'responses')


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_native_schema_stream_rejected_before_credentials(mode):
    definition = {'name': 'answer', 'strict': True, 'schema': SCHEMA}
    body = {'model': RUNTIME_MODEL, **({'text': {'format': {'type': 'json_schema', **definition}}} if mode == 'responses' else {'response_format': {'type': 'json_schema', 'json_schema': definition}})}
    frames = []
    worker.execute(request(mode+'_stream', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': body}, auth={'mode': 'session'}), frames.append,
                   session_factory=lambda **kwargs: pytest.fail('stream schema acquired credentials'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('value', [None, {}, {'textFormat': {}}, {'textFormat': {'type': 'other', 'structure': {}}},
    {'textFormat': {'type': 'json_schema', 'structure': {'jsonSchema': {'name': 'bad name', 'schema': '{}'}}}}])
def test_converse_output_configuration_rejects_unknown_union_members(value):
    from api_agent.llm_providers.bedrock.catalog import selection
    with pytest.raises(ValueError):
        runtime_worker.converse_schema(value, selection(CONVERSE_MODEL, 'converse', endpoint='runtime'))


def test_converse_output_configuration_accepts_only_strict_advertised_schema():
    from api_agent.llm_providers.bedrock.catalog import selection
    value = {'textFormat': {'type': 'json_schema', 'structure': {'jsonSchema': {'name': 'answer', 'schema': json.dumps(SCHEMA)}}}}
    runtime_worker.converse_schema(value, selection(CONVERSE_MODEL, 'converse', endpoint='runtime'))


def test_catalog_deduplicates_exact_ids_across_discovery_operations():
    client = SimpleNamespace(list_foundation_models=lambda: {'modelSummaries': [{'modelId': 'same', 'inferenceTypesSupported': ['ON_DEMAND']}]},
        list_inference_profiles=lambda: {'inferenceProfileSummaries': [{'inferenceProfileId': 'same'}, {'inferenceProfileId': 'other'}]})
    assert runtime_worker.catalog(client, time.monotonic()+10) == {'data': [{'id': 'same'}, {'id': 'other'}]}


def test_catalog_rejects_more_than_supported_record_limit():
    client = SimpleNamespace(list_foundation_models=lambda: {'modelSummaries': [{'modelId': f'model{index}', 'inferenceTypesSupported': ['ON_DEMAND']} for index in range(1025)]})
    with pytest.raises(ValueError, match='exceeds limit'):
        runtime_worker.catalog(client, time.monotonic()+10)


def test_catalog_rejects_endless_distinct_pagination_tokens():
    calls = []
    def page(**kwargs):
        calls.append(kwargs)
        return {'modelSummaries': [], 'nextToken': str(len(calls))}
    with pytest.raises(ValueError, match='pagination exceeds limit'):
        runtime_worker.catalog(SimpleNamespace(list_foundation_models=page), time.monotonic()+10)
    assert len(calls) == 1024


@pytest.mark.parametrize('value', [None, {}, {'format': None}, {'format': {'type': 'other'}},
    {'format': {'type': 'json_schema', 'name': 'answer', 'strict': False, 'schema': SCHEMA}}])
def test_runtime_response_schema_rejects_unadmitted_format(value):
    with pytest.raises(ValueError):
        worker.qualified_schema(value, RUNTIME_MODEL, '/openai/v1', endpoint='runtime', response=True)


def test_runtime_schema_rejects_unqualified_route():
    value = {'type': 'json_schema', 'json_schema': {'name': 'answer', 'strict': True, 'schema': SCHEMA}}
    with pytest.raises(ValueError):
        worker.qualified_schema(value, RUNTIME_MODEL, '/v1', endpoint='runtime')


@pytest.mark.parametrize('control', [{'reasoning': {'effort': 'unknown'}}, {'reasoning': 'medium'}, {'reasoning': {'effort': 'medium', 'other': True}}])
def test_runtime_response_reasoning_controls_reject_before_credentials(control):
    frames = []
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': RUNTIME_MODEL, **control}}, auth={'mode': 'session'}), frames.append,
                   session_factory=lambda **kwargs: pytest.fail('invalid control acquired credentials'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_runtime_worker_dispatches_native_schema_without_dropping_it(mode):
    frames, seen = [], []
    definition = {'name': 'answer', 'strict': True, 'schema': SCHEMA}
    carrier = {'text': {'format': {'type': 'json_schema', **definition}}} if mode == 'responses' else {'response_format': {'type': 'json_schema', 'json_schema': definition}}
    body = {'model': RUNTIME_MODEL, **carrier}
    raw = response([message('{}')]) if mode == 'responses' else completion()
    raw['model'] = RUNTIME_MODEL
    if mode == 'completions':
        raw['choices'][0]['message']['content'] = '{}'
    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(model_dump=lambda **kwargs: raw)
    creator = SimpleNamespace(create=create)
    client = SimpleNamespace(responses=creator, chat=SimpleNamespace(completions=creator), close=lambda: None)
    http = SimpleNamespace(close=lambda: None)
    worker.execute(request(mode, {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': body}), frames.append,
        http_factory=lambda **kwargs: http, runtime_client_factory=lambda *args: client)
    assert len(seen) == 1 and all(seen[0][key] == value for key, value in carrier.items())
    assert frames[0]['result'] == raw


@pytest.mark.parametrize('mode,endpoint,body', [
    ('responses', 'mantle', {'model': MODEL, 'text': {}}),
    ('completions', 'runtime', {'model': 'openai.gpt-oss-20b-1:0', 'response_format': {}}),
])
def test_unadvertised_schema_carriers_reject_before_credentials(mode, endpoint, body):
    frames = []
    worker.execute(request(mode, {'endpoint': endpoint, 'prefix': '/openai/v1' if endpoint == 'runtime' else '/v1', 'body': body}, auth={'mode': 'session'}), frames.append,
        session_factory=lambda **kwargs: pytest.fail('unadvertised schema used credentials'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


def test_buffered_converse_tool_only_result_has_no_text_frame():
    frames = []
    raw = converse_response()
    raw['output']['message']['content'] = [{'toolUse': {'toolUseId': 'call1', 'name': 'lookup', 'input': {}}}]
    raw['stopReason'] = 'tool_use'
    client = SimpleNamespace(converse=lambda **kwargs: raw, close=lambda: None)
    worker.execute(request('converse_stream', {'endpoint': 'runtime', 'prefix': '/converse', 'body': {'modelId': CONVERSE_MODEL, 'messages': []}}),
        frames.append, boto_client_factory=lambda *args: client)
    assert len(frames) == 1 and frames[0]['event']['kind'] == 'completed'
    from api_agent.providers.inference import result_from_wire
    decoded = result_from_wire(frames[0]['event']['result'])
    assert decoded.payload.calls[0].call_id == 'call1'


@pytest.mark.parametrize('flags', [None, [], ['INFERENCE_PROFILE'], ['PROVISIONED']])
def test_foundation_catalog_preserves_exact_ids_for_operator_selection(flags):
    row = {'modelId': 'public.profile-required'}
    if flags is not None:
        row['inferenceTypesSupported'] = flags
    client = SimpleNamespace(list_foundation_models=lambda: {'modelSummaries': [row]},
        list_inference_profiles=lambda: {'inferenceProfileSummaries': [{'inferenceProfileId': 'us.public.profile-required'}]})
    assert runtime_worker.catalog(client, time.monotonic()+10) == {'data': [{'id': 'public.profile-required'}, {'id': 'us.public.profile-required'}]}


@pytest.mark.parametrize('flags', ['ON_DEMAND', [None], [1], ['UNKNOWN']])
def test_foundation_catalog_does_not_treat_unprojected_metadata_as_a_model_filter(flags):
    client = SimpleNamespace(list_foundation_models=lambda: {'modelSummaries': [
        {'modelId': 'public.model', 'inferenceTypesSupported': flags}]})
    client.list_inference_profiles = lambda: {'inferenceProfileSummaries': []}
    assert runtime_worker.catalog(client, time.monotonic()+10) == {'data': [{'id': 'public.model'}]}


@pytest.mark.parametrize('name', ['EndpointConnectionError', 'ReadTimeoutError', 'ConnectTimeoutError', 'ConnectionClosedError'])
def test_native_transient_transport_failure_is_classified_without_sdk_retry(name):
    from botocore import exceptions
    error = getattr(exceptions, name)(endpoint_url='https://unit.invalid', error=OSError('closed'))
    assert runtime_worker.transient_network_failure(error)
    calls, frames = [], []
    def fail():
        calls.append('dispatch')
        raise error
    client = SimpleNamespace(list_foundation_models=fail, close=lambda: calls.append('close'))
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: client)
    assert calls == ['dispatch', 'close']
    assert frames == [{'error': 'network', 'execution': 'possibly_executed'}]


@pytest.mark.parametrize('name', ['SSLError', 'NoCredentialsError', 'InvalidConfigError', 'PartialCredentialsError'])
def test_native_tls_and_configuration_failures_are_not_network(name):
    from botocore import exceptions
    error = getattr(exceptions, name)(endpoint_url='https://unit.invalid', error='certificate',
        error_msg='bad configuration', provider='explicit', cred_var='access_key')
    assert not runtime_worker.transient_network_failure(error)


@pytest.mark.parametrize('carrier', ['cause', 'context', 'kwargs'])
def test_nested_tls_failure_prevents_native_network_retry(carrier):
    import ssl
    from botocore.exceptions import EndpointConnectionError
    error = EndpointConnectionError(endpoint_url='https://unit.invalid')
    tls = ssl.SSLCertVerificationError('synthetic certificate failure')
    if carrier == 'cause':
        error.__cause__ = tls
    elif carrier == 'context':
        error.__context__ = tls
    else:
        error.kwargs['error'] = tls
    assert not runtime_worker.transient_network_failure(error)


def test_native_transport_chain_is_cycle_safe_and_bounded():
    from botocore.exceptions import EndpointConnectionError
    first = EndpointConnectionError(endpoint_url='https://unit.invalid')
    second = RuntimeError('wrapper')
    first.__cause__ = second
    second.__cause__ = first
    assert runtime_worker.transient_network_failure(first)
    chain = first
    for _ in range(17):
        wrapper = RuntimeError('wrapper')
        wrapper.__cause__ = chain
        chain = wrapper
    assert not runtime_worker.transient_network_failure(chain)
    assert not runtime_worker.transient_network_failure(ValueError('local configuration'))


def test_native_error_propagates_only_safe_retry_after_hint():
    from botocore.exceptions import ClientError
    frames = []
    def fail():
        raise ClientError({'Error': {'Code': 'ThrottlingException'}, 'ResponseMetadata': {
            'HTTPStatusCode': 429, 'HTTPHeaders': {'retry-after': '3', 'arbitrary': 'not exported'}}}, 'ListFoundationModels')
    client = SimpleNamespace(list_foundation_models=fail, close=lambda: None)
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: client)
    assert frames == [{'error': 'rate_limited', 'execution': 'possibly_executed', 'http_status': 429, 'retry_after': 3}]


@pytest.mark.parametrize('metadata,observed_status,expected_kind', [
    (None, None, 'unknown'), ([], None, 'unknown'),
    ({'HTTPStatusCode': 503}, 403, 'authorization'),
])
def test_native_error_metadata_cannot_mask_failure_or_replace_observed_status(metadata, observed_status, expected_kind):
    from botocore.exceptions import ClientError
    error = ClientError({'Error': {'Code': 'Synthetic'}, 'ResponseMetadata': {}}, 'ListFoundationModels')
    error.response['ResponseMetadata'] = metadata
    if observed_status is not None:
        error.status_code = observed_status
    def fail():
        raise error
    frames = []
    client = SimpleNamespace(list_foundation_models=fail, close=lambda: None)
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: client)
    expected = {'error': expected_kind, 'execution': 'possibly_executed'}
    if observed_status is not None:
        expected['http_status'] = observed_status
    assert frames == [expected]


def test_runtime_openai_transient_connection_failure_reports_network_without_sdk_retry():
    calls, frames = [], []
    def disconnected(request):
        calls.append(request)
        raise httpx2.ConnectTimeout('synthetic disconnection')
    client = httpx2.Client(transport=httpx2.MockTransport(disconnected), timeout=30)
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': RUNTIME_MODEL, 'input': 'hello'}}),
        frames.append, http_factory=lambda **kwargs: client)
    assert len(calls) == 1 and client.is_closed
    assert frames == [{'error': 'network', 'execution': 'possibly_executed'}]


def test_installed_boto_serializer_accepts_strict_tool_and_native_converse_schema():
    client = runtime_worker.boto_client('us-east-1', 'bedrock-runtime', {'api_key': 'selected'}, 30)
    body = {'modelId': CONVERSE_MODEL, 'messages': [{'role': 'user', 'content': [{'text': 'hello'}]}],
        'toolConfig': {'tools': [{'toolSpec': {'name': 'lookup', 'description': 'Public lookup',
            'strict': True, 'inputSchema': {'json': SCHEMA}}}]},
        'outputConfig': {'textFormat': {'type': 'json_schema', 'structure': {
            'jsonSchema': {'name': 'answer', 'schema': json.dumps(SCHEMA)}}}}}
    seen = []
    class NoSend(Exception):
        pass
    def stop(request, **kwargs):
        seen.append(json.loads(request.body))
        raise NoSend()
    client.meta.events.register('before-send.bedrock-runtime.Converse', stop)
    try:
        with pytest.raises(NoSend):
            client.converse(**body)
        assert len(seen) == 1
        assert seen[0]['toolConfig'] == body['toolConfig']
        assert seen[0]['outputConfig'] == body['outputConfig']
        assert seen[0]['outputConfig']['textFormat']['structure']['jsonSchema']['schema'] == json.dumps(SCHEMA)
    finally:
        client.close()


@pytest.mark.parametrize('missing', ['boto3', 'botocore'])
@pytest.mark.parametrize('malformed', [False, True])
def test_missing_native_dependency_preserves_safe_error_frame_in_fresh_process(missing, malformed):
    import subprocess
    import sys
    program = """
import builtins, json, sys
from api_agent.llm_providers.bedrock.worker import execute
missing = sys.argv[1]
original_import = builtins.__import__
def selected_import(name, *args, **kwargs):
    if name == missing or name.startswith(missing + '.'):
        raise ModuleNotFoundError('Synthetic unavailable dependency', name=missing)
    return original_import(name, *args, **kwargs)
builtins.__import__ = selected_import
frames = []
operation = 'converse' if sys.argv[2] == 'malformed' else 'runtime_models'
execute({'operation': operation, 'auth': {'mode': 'api_key', 'api_key': 'synthetic-selected', 'region': 'us-east-1'},
         'payload': {}, 'timeout': 10}, frames.append)
print(json.dumps(frames))
"""
    completed = subprocess.run([sys.executable, '-B', '-c', program, missing, 'malformed' if malformed else 'valid'],
        capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(completed.stdout) == [{'error': 'unknown' if malformed else 'setup', 'execution': 'not_started'}]
    assert completed.stderr == ''


@pytest.mark.parametrize('operation', ['converse', 'converse_stream'])
def test_unknown_operator_selected_converse_model_is_dispatched_without_a_family_blacklist(operation):
    frames, seen = [], []
    model = 'operator.model-v2:1'
    body = {'modelId': model, 'messages': [{'role': 'user', 'content': [{'text': 'hello'}]}]}
    raw = converse_response()
    def converse(**kwargs):
        seen.append(kwargs)
        return raw
    client = SimpleNamespace(converse=converse, close=lambda: None)
    worker.execute(request(operation, {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}),
        frames.append, boto_client_factory=lambda *args: client)
    assert seen == [body]
    assert 'result' in frames[0] if operation == 'converse' else frames[-1]['event']['kind'] == 'completed'


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_unknown_operator_selected_runtime_openai_model_is_dispatched(mode):
    model = 'operator.model-v2:1'
    raw = response([message()]) if mode == 'responses' else completion()
    raw['model'] = model
    seen, frames = [], []
    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(model_dump=lambda **kwargs: raw)
    creator = SimpleNamespace(create=create)
    client = SimpleNamespace(responses=creator, chat=SimpleNamespace(completions=creator), close=lambda: None)
    worker.execute(request(mode, {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': model}}), frames.append,
        runtime_client_factory=lambda *args: client, http_factory=lambda **kwargs: SimpleNamespace(close=lambda: None))
    assert seen[0]['model'] == model
    assert frames[0]['result'] == raw


def test_native_network_classification_remains_inert_without_optional_botocore(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'botocore':
            raise ImportError('Synthetic missing optional SDK')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    assert not runtime_worker.transient_network_failure(ValueError('invalid pre-dispatch request'))


def emit_aws_failure(detail, *, joined=True):
    from botocore.exceptions import ClientError
    error = ClientError({'Error': {'Code': detail.get('code'), 'Message': detail.get('message')},
        'ResponseMetadata': {'HTTPStatusCode': 400, **({'RequestId': detail['request_id']} if 'request_id' in detail else {})}}, 'ListFoundationModels')
    frames = []
    def fail():
        raise error
    client = SimpleNamespace(list_foundation_models=fail, close=lambda: None)
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: client,
        join=(lambda values, arguments: [*values, 'caller-only-secret']) if joined else None)
    return frames[0]


@pytest.mark.parametrize('log_content', ['true', 'false'])
@pytest.mark.parametrize('with_request_id', [True, False])
def test_joined_aws_diagnostic_is_bounded_and_independent_of_content_logging(monkeypatch, log_content, with_request_id):
    monkeypatch.setenv('LOG_CONTENT', log_content)
    detail = {'code': 'ValidationException', 'message': 'The selected model does not support this API route.', 'request_id': 'request-123'}
    if not with_request_id:
        detail.pop('request_id')
    frame = emit_aws_failure(detail)
    assert frame == {'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400, 'aws_error': detail}


@pytest.mark.parametrize('detail', [
    {'code': 'ValidationException', 'message': AUTH['api_key']},
    {'code': AUTH['api_key'], 'message': 'Rejected'},
    {'code': 'ValidationException', 'message': json.dumps({'value': 'caller-only-secret'})},
    {'code': 'secret', 'message': 'caller-', 'request_id': 'only-'},
    {'code': 'caller-', 'message': 'only-', 'request_id': 'secret'},
    {'code': 'c'*101, 'message': 'Rejected'},
    {'code': 'ValidationException', 'message': 'x'*513},
    {'code': 'ValidationException', 'message': 'Rejected', 'request_id': 'x'*101},
    {'code': 'ValidationException', 'message': 'line\nbreak'},
    {'code': 42, 'message': 'Rejected'},
])
def test_unsafe_aws_diagnostic_is_withheld_without_replacing_failure_identity(detail):
    frame = emit_aws_failure(detail)
    assert frame == {'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400, 'aws_error_rejected': True}


def test_unjoined_aws_diagnostic_never_crosses_worker_boundary():
    frame = emit_aws_failure({'code': 'ValidationException', 'message': 'Upstream body'}, joined=False)
    assert frame == {'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400}


# Reuse the SDK transport's maintained fake-process/selected-auth fixtures.
from test_bedrock_sdk import auth, fake_process


@pytest_asyncio.fixture
async def joined_aws_sdk_channel(fake_process, monkeypatch):
    import asyncio
    import os
    import socket
    from api_agent.llm_providers.bedrock import sdk
    original_spawn = sdk.asyncio.create_subprocess_exec
    tasks = []
    async def spawn(*args, **kwargs):
        descriptor = os.dup(kwargs['pass_fds'][0])
        async def exchange():
            channel = socket.socket(fileno=descriptor)
            channel.setblocking(False)
            loop = asyncio.get_running_loop()
            try:
                await loop.sock_sendall(channel, b'["synthetic-key"]\n')
                union = bytearray()
                while not union.endswith(b'\n'):
                    union.extend(await loop.sock_recv(channel, 1024))
                await loop.sock_sendall(channel, b'1')
                assert await loop.sock_recv(channel, 1) == b'1'
            finally:
                channel.close()
        tasks.append(asyncio.create_task(exchange()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    async def join(values):
        return [*values, 'caller-only-secret'], None
    try:
        yield join
    finally:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_admitted_aws_diagnostic_reaches_parent_exception_and_traceback(auth, fake_process, joined_aws_sdk_channel):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ProviderAccessError
    detail = {'code': 'ValidationException', 'message': 'The selected model does not support this API route.', 'request_id': 'request-123'}
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400, 'aws_error': detail}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('runtime_models', {}, auth=auth, join=joined_aws_sdk_channel)
        assert caught.value.message == detail['message']
        assert caught.value.code == detail['code'] and caught.value.request_id == detail['request_id']
        assert caught.value.kind == 'invalid_request' and caught.value.execution == 'possibly_executed'
        import traceback
        trace = ''.join(traceback.format_exception(caught.value))
        assert detail['message'] in trace and detail['code'] in trace
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('detail', [
    {'code': 'ValidationException', 'message': 'caller-only-secret'},
    {'code': 'ValidationException', 'message': 'synthetic-key'},
    {'code': 'secret', 'message': 'caller-', 'request_id': 'only-'},
    {'code': 'ValidationException', 'message': 'x'*513},
    {'code': 'ValidationException', 'message': 'Rejected', 'extra': 'not admitted'},
])
async def test_parent_independently_rejects_unsafe_or_malformed_aws_carrier(auth, fake_process, joined_aws_sdk_channel, detail):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ContractError
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400, 'aws_error': detail}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError):
            await transport.call('runtime_models', {}, auth=auth, join=joined_aws_sdk_channel)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_parent_rejects_aws_error_without_completed_authority_handshake(auth, fake_process):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ContractError
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'possibly_executed', 'aws_error': {'code': 'ValidationException', 'message': 'Unsafe unjoined detail'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='Unadmitted'):
            await transport.call('runtime_models', {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_withheld_aws_diagnostic_has_fixed_parent_message(auth, fake_process):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ProviderAccessError
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'possibly_executed', 'http_status': 400, 'aws_error_rejected': True}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError, match='additional AWS diagnostic withheld') as caught:
            await transport.call('runtime_models', {}, auth=auth)
        assert caught.value.kind == 'invalid_request' and caught.value.code is None
    finally:
        await transport.aclose()


@pytest.mark.parametrize('response', [None, {}, {'Error': []}])
def test_aws_diagnostic_projection_rejects_missing_service_error(response):
    with pytest.raises(ValueError, match='Missing Bedrock AWS error'):
        runtime_worker.aws_error(response, ['synthetic-selected'])


@pytest.mark.asyncio
async def test_parent_rejects_nonboolean_aws_withheld_marker(auth, fake_process):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ContractError
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'possibly_executed', 'aws_error_rejected': False}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='diagnostic marker'):
            await transport.call('runtime_models', {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_aws_diagnostic_cannot_override_worker_decode_failure(auth, fake_process, joined_aws_sdk_channel):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ContractError
    fake_process[0]['frames'] = [{'error': 'unknown', 'execution': 'possibly_executed', 'code': 'invalid_response',
        'aws_error': {'code': 'ValidationException', 'message': 'Should not replace original validation failure'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='Unadmitted'):
            await transport.call('runtime_models', {}, auth=auth, join=joined_aws_sdk_channel)
    finally:
        await transport.aclose()


@pytest.mark.parametrize('endpoint', ['mantle', 'runtime'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('status,kind', [(400, 'invalid_request'), (401, 'authentication'), (403, 'authorization')])
@pytest.mark.parametrize('wrapped', [True, False])
@pytest.mark.parametrize('with_code', [True, False])
def test_actual_openai_service_error_preserves_bounded_diagnostic(endpoint, mode, status, kind, wrapped, with_code):
    detail = {'message': 'The selected model does not support this API route.'}
    if with_code:
        detail['code'] = 'ValidationException'
    body = {'error': detail} if wrapped else detail
    seen, frames = [], []
    def rejected(req):
        seen.append(req)
        return httpx2.Response(status, json=body, headers={'x-request-id': 'route-request'})
    http = httpx2.Client(transport=httpx2.MockTransport(rejected), timeout=30)
    arguments = {'model': RUNTIME_MODEL if endpoint == 'runtime' else MODEL,
        **({'input': 'hello'} if mode == 'responses' else {'messages': [{'role': 'user', 'content': 'hello'}]})}
    payload = {'endpoint': endpoint, 'prefix': '/openai/v1' if endpoint == 'runtime' else '/v1', 'body': arguments}
    worker.execute(request(mode, payload), frames.append, http_factory=lambda **kwargs: http,
        join=lambda values, arguments: [*values, 'caller-only-secret'])
    assert len(seen) == 1 and http.is_closed
    assert seen[0].headers['authorization'] == 'Bearer ' + AUTH['api_key']
    expected_host = 'bedrock-runtime.us-east-1.amazonaws.com' if endpoint == 'runtime' else 'bedrock-mantle.us-east-1.api.aws'
    assert seen[0].url.host == expected_host
    assert frames[0]['error'] == kind and frames[0]['http_status'] == status
    assert frames[0]['execution'] == 'possibly_executed'
    assert frames[0]['aws_error'] == {**detail, 'request_id': 'route-request'}


@pytest.mark.parametrize('log_content', ['true', 'false'])
@pytest.mark.parametrize('detail', [
    {'message': AUTH['api_key']}, {'message': 'caller-only-secret'},
    {'message': json.dumps({'credential': 'caller-only-secret'})},
    {'message': 'caller-only-', 'code': 'secret'},
    {'message': 'x'*513}, {'message': 'Rejected', 'code': 'x'*101},
])
def test_openai_error_safety_uses_joined_union_independently_of_logging(monkeypatch, log_content, detail):
    monkeypatch.setenv('LOG_CONTENT', log_content)
    http = httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(403, json={'error': detail})), timeout=30)
    frames = []
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': RUNTIME_MODEL, 'input': 'hello'}}),
        frames.append, http_factory=lambda **kwargs: http, join=lambda values, arguments: [*values, 'caller-only-secret'])
    assert frames[0]['error'] == 'authorization' and frames[0]['http_status'] == 403
    assert frames[0]['execution'] == 'possibly_executed' and frames[0]['aws_error_rejected'] is True
    assert 'aws_error' not in frames[0] and 'provider_response' not in frames[0]
    assert 'caller-only-secret' not in json.dumps(frames)
    assert AUTH['api_key'] not in json.dumps(frames)


def test_unjoined_openai_error_has_no_service_diagnostic_carrier():
    http = httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(403, json={'error': {'message': 'Original permission details'}})), timeout=30)
    frames = []
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': RUNTIME_MODEL, 'input': 'hello'}}),
        frames.append, http_factory=lambda **kwargs: http)
    assert frames == [{'error': 'authorization', 'execution': 'possibly_executed', 'http_status': 403}]


@pytest.mark.parametrize('wrapped', [True, False])
def test_structured_sdk_error_projection_does_not_invent_missing_code(wrapped):
    detail = {'message': 'Model access is not enabled.'}
    body = {'error': detail} if wrapped else detail
    assert runtime_worker.openai_error(body, None, ['synthetic-selected']) == detail


@pytest.mark.asyncio
async def test_parent_sdk_preserves_message_when_service_code_is_absent(auth, fake_process, joined_aws_sdk_channel):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ProviderAccessError
    fake_process[0]['frames'] = [{'error': 'authorization', 'execution': 'possibly_executed', 'http_status': 403,
        'aws_error': {'message': 'This identity lacks model invocation permission.'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('responses', {}, auth=auth, join=joined_aws_sdk_channel)
        assert caught.value.message == 'This identity lacks model invocation permission.'
        assert caught.value.code is None and caught.value.kind == 'authorization'
        assert caught.value.status == 403
    finally:
        await transport.aclose()


@pytest.mark.parametrize('body', [None, {'error': 'not a structured error'}])
def test_openai_diagnostic_projection_rejects_unstructured_bodies(body):
    with pytest.raises(ValueError):
        runtime_worker.openai_error(body, None, ['synthetic-selected'])


def test_native_service_diagnostic_preserves_absent_code_without_inventing_one():
    frame = emit_aws_failure({'message': 'The account has no invocation permission.'})
    assert frame['aws_error'] == {'message': 'The account has no invocation permission.'}


def test_native_nonservice_error_does_not_project_arbitrary_response_fields():
    class NonServiceError(Exception):
        response = {'message': 'caller-only-secret'}
    def fail():
        raise NonServiceError('caller-only-secret')
    frames = []
    worker.execute(request(), frames.append, boto_client_factory=lambda *args: SimpleNamespace(list_foundation_models=fail, close=lambda: None),
        join=lambda values, arguments: [*values, 'caller-only-secret'])
    assert frames == [{'error': 'unknown', 'execution': 'possibly_executed'}]


def test_unsafe_openai_request_id_withholds_diagnostic_and_raw_evidence():
    http = httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(403,
        json={'error': {'message': 'Permission denied'}}, headers={'x-request-id': 'caller-only-secret'})), timeout=30)
    frames = []
    worker.execute(request('responses', {'endpoint': 'runtime', 'prefix': '/openai/v1', 'body': {'model': RUNTIME_MODEL, 'input': 'hello'}}),
        frames.append, http_factory=lambda **kwargs: http, join=lambda values, arguments: [*values, 'caller-only-secret'])
    assert frames == [{'error': 'authorization', 'execution': 'possibly_executed', 'aws_error_rejected': True, 'http_status': 403}]


def test_optional_sdk_error_export_cannot_replace_original_retry_failure_in_fresh_process():
    import subprocess
    import sys
    program = """
import json, sys
from types import SimpleNamespace
from api_agent.llm_providers.bedrock.worker import execute
sys.modules['openai'] = SimpleNamespace()
class RetryError(Exception):
    status_code = 503
    body = {'error': {'message': 'Temporary service failure'}}
def retry():
    raise RetryError()
frames = []
execute({'operation': 'models', 'auth': {'mode': 'api_key', 'api_key': 'synthetic-selected', 'region': 'us-east-1'},
         'payload': {}, 'timeout': 10}, frames.append,
    client_factory=lambda *args: SimpleNamespace(models=SimpleNamespace(list=retry), close=lambda: None),
    http_factory=lambda **kwargs: SimpleNamespace(close=lambda: None), join=lambda values, arguments: values)
print(json.dumps(frames))
"""
    completed = subprocess.run([sys.executable, '-B', '-c', program],
        capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(completed.stdout) == [{'error': 'unavailable', 'execution': 'possibly_executed',
        'http_status': 503, 'provider_response': {'error': {'message': 'Temporary service failure'}}}]
    assert completed.stderr == ''


@pytest.mark.asyncio
async def test_upstream_code_matching_internal_validation_name_preserves_safe_raw_evidence(auth, fake_process, joined_aws_sdk_channel):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ProviderAccessError
    raw = {'error': {'code': 'response_safety_rejected', 'message': 'Upstream policy denied the operation.'}}
    fake_process[0]['frames'] = [{'error': 'authorization', 'execution': 'possibly_executed', 'http_status': 403,
        'aws_error': raw['error'], 'provider_response': raw}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('responses', {}, auth=auth, join=joined_aws_sdk_channel)
        assert caught.value.code == 'response_safety_rejected'
        assert caught.value.provider_response == raw
        assert caught.value.kind == 'authorization' and caught.value.status == 403
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_forged_withheld_diagnostic_cannot_republish_raw_body(auth, fake_process, joined_aws_sdk_channel):
    from api_agent.llm_providers.bedrock import sdk
    from api_agent.providers.contracts import ContractError
    fake_process[0]['frames'] = [{'error': 'authorization', 'execution': 'possibly_executed', 'http_status': 403,
        'aws_error_rejected': True, 'provider_response': {'error': {'message': 'Should be withheld'}}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='worker response'):
            await transport.call('responses', {}, auth=auth, join=joined_aws_sdk_channel)
    finally:
        await transport.aclose()


def test_partial_sdk_without_optional_error_export_preserves_original_failure_in_process(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace())
    class RetryError(Exception):
        status_code = 503
        body = {'error': {'message': 'Temporary service failure'}}
    def retry():
        raise RetryError()
    frames = []
    worker.execute(request('models'), frames.append,
        client_factory=lambda *args: SimpleNamespace(models=SimpleNamespace(list=retry), close=lambda: None),
        http_factory=lambda **kwargs: SimpleNamespace(close=lambda: None), join=lambda values, arguments: values)
    assert frames == [{'error': 'unavailable', 'execution': 'possibly_executed', 'http_status': 503,
        'provider_response': {'error': {'message': 'Temporary service failure'}}}]
