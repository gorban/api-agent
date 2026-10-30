"""Offline maintained SDK qualification and worker ownership checks."""
import io
import json
import runpy
import sys
from types import SimpleNamespace

import httpx2
import pytest
from api_agent.llm_providers.bedrock import worker
from test_bedrock_mantle import MODEL, completion_stream, response_stream, completion, response, message


AUTH = {'mode': 'api_key', 'api_key': 'synthetic-selected', 'region': 'us-east-1'}


def request(operation='models', auth=None, prefix='/v1'):
    return {'timeout': 240, 'operation': operation, 'auth': AUTH if auth is None else auth,
            'payload': {} if operation == 'models' else {'prefix': prefix, 'body': {
                'model': MODEL, **({'messages': [{'role': 'user', 'content': 'hello'}]} if operation.startswith('completions') else {'input': 'hello'})}}}


@pytest.mark.parametrize('mode', ['api_key', 'access_key', 'temporary', 'session'])
@pytest.mark.parametrize('prefix', ['/v1', '/openai/v1'])
@pytest.mark.parametrize('operation', sorted(worker.OPERATIONS))
def test_actual_sdk_serializes_selected_auth_and_paths(monkeypatch, mode, prefix, operation):
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient')
    monkeypatch.setenv('AWS_BEDROCK_BASE_URL', 'https://wrong.invalid')
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'ambient')
    auth = {'mode': mode, 'region': 'us-east-1'}
    if mode == 'api_key':
        auth['api_key'] = 'synthetic-selected'
    elif mode != 'session':
        auth.update(access_key='synthetic-access', secret_key='synthetic-secret')
        if mode == 'temporary':
            auth['session_token'] = 'synthetic-token'
    seen, owners, emitted = [], [], []
    def handle(req):
        seen.append(req)
        if operation.endswith('_stream'):
            values = response_stream() if operation.startswith('responses') else completion_stream()
            data = ''.join('data: '+json.dumps(value)+'\n\n' for value in values) + 'data: [DONE]\n\n'
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'}, content=data)
        value = ({'object': 'list', 'data': []} if operation == 'models' else
                 dict(response([message()]), object='response', created_at=0) if operation == 'responses' else
                 dict(completion(), id='c', object='chat.completion', created=0))
        return httpx2.Response(200, json=value, headers={'x-request-id': 'public-request-id',
                                                       'x-private-header': 'synthetic-secret'})
    def http_factory(*, timeout):
        http = httpx2.Client(transport=httpx2.MockTransport(handle), verify=True, follow_redirects=False, trust_env=False, timeout=httpx2.Timeout(timeout, connect=min(10, timeout)))
        owners.append(http)
        return http
    def session_factory(**kw):
        assert mode == 'session'
        return SimpleNamespace(region_name='us-east-1', get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: SimpleNamespace(access_key='synthetic-access', secret_key='synthetic-secret', token='synthetic-token')))
    worker.execute(request(operation, auth, prefix), emitted.append, session_factory=session_factory, http_factory=http_factory)
    assert len(seen) == 1 and owners[0].is_closed
    expected_prefix = '/v1' if operation == 'models' else prefix
    suffix = '/models' if operation == 'models' else '/chat/completions' if operation.startswith('completions') else '/responses'
    assert str(seen[0].url) == 'https://bedrock-mantle.us-east-1.api.aws'+expected_prefix+suffix
    header = seen[0].headers['authorization']
    assert header == 'Bearer synthetic-selected' if mode == 'api_key' else '/us-east-1/bedrock-mantle/aws4_request' in header
    assert seen[0].headers.get('x-amz-security-token') == ('synthetic-token' if mode in {'temporary', 'session'} else None)
    assert set(emitted[0]) == ({'event'} if operation.endswith('_stream') else
                              {'result'} if operation == 'models' else {'result', 'diagnostics'})
    if operation in ('responses', 'completions'):
        assert emitted[0]['diagnostics']['http_status'] == 200
        assert emitted[0]['diagnostics']['request_id'] == 'public-request-id'
        assert emitted[0]['diagnostics']['sdk_versions']['openai']
        assert 'x-private-header' not in json.dumps(emitted)
    if operation.endswith('_stream'):
        assert emitted[-1]['event']['kind'] == 'completed'
    if operation.startswith('responses'):
        assert json.loads(seen[0].content)['store'] is False
    if operation == 'completions_stream':
        assert json.loads(seen[0].content)['stream_options'] == {'include_usage': True}


@pytest.mark.parametrize('status,kind', [(307, 'unknown'), (400,'invalid_request'), (401,'authentication'), (403,'authorization'), (429,'rate_limited'), (500,'unavailable')])
def test_actual_sdk_denies_redirects_and_retries(status, kind):
    seen, emitted = [], []
    def handle(req):
        seen.append(req)
        return httpx2.Response(status, headers={'location': 'https://wrong.invalid'}, json={'error': {'message': 'synthetic-secret'}})
    worker.execute(request(), emitted.append, http_factory=lambda **kw: httpx2.Client(transport=httpx2.MockTransport(handle), follow_redirects=False, trust_env=False))
    assert len(seen) == 1
    assert emitted == [{'error': kind, 'execution': 'possibly_executed', 'http_status': status}]


@pytest.mark.parametrize('value', [{1: 'bad'}, (1,), float('nan'), {'x': object()}, 'x' * (worker.MAX_FRAME+1)])
def test_json_rejects_coercion_and_limits(value):
    with pytest.raises(ValueError):
        worker.json_value(value)


@pytest.mark.parametrize('auth', [dict(AUTH, mode='bad'), dict(AUTH, api_key=''), dict(AUTH, region='https://wrong.invalid'), dict(AUTH, region=1), dict(AUTH, profile='other'), {'mode':'temporary','access_key':'a','secret_key':'b','region':'us-east-1'}, {'mode':'access_key','access_key':'','secret_key':'b','region':'us-east-1'}])
def test_invalid_auth_never_constructs_http(auth):
    emitted=[]
    worker.execute(request(auth=auth), emitted.append, http_factory=lambda **kw: pytest.fail('HTTP constructed'))
    assert emitted == [{'error':'unknown','execution':'not_started'}]


@pytest.mark.parametrize('auth', [dict(AUTH, region=None), {'mode':'access_key','access_key':'a','secret_key':'b'}])
def test_region_only_resolution_preserves_selected_auth(auth):
    region, options = worker.selected_auth(auth, lambda: SimpleNamespace(region_name='us-east-1'))
    assert region == 'us-east-1'
    assert options['api_key'] == ('synthetic-selected' if auth['mode']=='api_key' else None)


def test_profile_without_session_token_is_frozen():
    session = SimpleNamespace(region_name='us-east-1', get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: SimpleNamespace(access_key='a',secret_key='b',token=None)))
    assert worker.selected_auth({'mode':'session'}, lambda **kw: session) == ('us-east-1', {'api_key':None,'access_key_id':'a','secret_access_key':'b'})


@pytest.mark.parametrize('change', [lambda r:r.update(operation='bad'), lambda r:r.update(extra=1), lambda r:r.update(payload={'x':1}), lambda r:r.update(operation='completions',payload={}), lambda r:r.update(operation='completions',payload={'prefix':'https://wrong.invalid','body':{}}), lambda r:r.update(operation='completions',payload={'prefix':'/v1','body':[]}), lambda r:r.update(operation='completions',payload={'prefix':'/v1','body':{'stream':False}})])
def test_invalid_request_rejected_before_auth(change):
    value=request();change(value);emitted=[]
    worker.execute(value,emitted.append,http_factory=lambda **kw:pytest.fail('HTTP constructed'))
    assert emitted == [{'error':'unknown','execution':'not_started'}]


def fake_execute(events, *, stream=True, fail_body=False, fail_client=False, fail_construct=False, observed=None, joined=False):
    closed=[] if observed is None else observed; emitted=[]
    def close(name, fail=False):
        closed.append(name)
        if fail: raise ValueError('synthetic-secret')
    class Model:
        def __init__(self,value): self.value=value
        def model_dump(self, **kwargs):
            assert kwargs == {'exclude_unset':True}
            return self.value
    class Body:
        def __iter__(self): return iter(Model(x) for x in events)
        def close(self): close('body',fail_body)
    def create(**kwargs): return Body() if stream else Model(events[0])
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=lambda:close('client',fail_client))
    def factory(*args):
        if fail_construct: raise ValueError('synthetic-secret')
        return client
    worker.execute(request('completions_stream' if stream else 'completions'),emitted.append,client_factory=factory,http_factory=lambda **kw:SimpleNamespace(close=lambda:close('http')),
                   join=(lambda secrets, payload: secrets) if joined else None)
    return emitted,closed


@pytest.mark.parametrize('path,value,code', [
    (('choices',0,'message','unsupported'), 'private-response-content', 'unsupported_response_field'),
    (('choices',0,'finish_reason'), 'stop', 'tool_finish_mismatch'),
    (('choices',0,'message','tool_calls',0,'function','arguments'), '[]', 'invalid_tool_arguments'),
    (('usage','total_tokens'), 6, 'invalid_usage'),
    (('choices',0,'finish_reason'), 'private-response-content', 'unsupported_finish_reason'),
    (('model',), 'other-selection', 'invalid_response')])
def test_rejected_sdk_response_reports_fixed_contract_code_without_payload(path, value, code):
    from test_bedrock_mantle import call
    raw = completion()
    raw['choices'][0].update(finish_reason='tool_calls')
    raw['choices'][0]['message']['tool_calls'] = [call()]
    owner = raw
    for key in path[:-1]: owner = owner[key]
    owner[path[-1]] = value
    frames, closed = fake_execute([raw], stream=False)
    frame, = frames
    assert frame['code'] == code and frame['execution'] == 'possibly_executed'
    assert frame['diagnostics']['response_validation'] == 'rejected'
    assert 'provider_response' not in frame and 'result' not in frame
    assert 'private-response-content' not in json.dumps(frame)
    assert closed == ['client', 'http']


@pytest.mark.parametrize('joined', [False, True])
def test_contract_rejection_retains_safe_body_only_after_authority_join(joined):
    from test_bedrock_mantle import call
    raw = completion()
    raw['choices'][0]['message'].update(content='rejected-response-canary', tool_calls=[call()])

    frames, closed = fake_execute([raw], stream=False, joined=joined)

    frame, = frames
    assert frame['code'] == 'tool_finish_mismatch'
    assert ('provider_response' in frame) is joined
    assert frame.get('provider_response') == (raw if joined else None)
    assert 'rejected-response-canary' not in json.dumps(frame['diagnostics'])
    assert 'result' not in frame and closed == ['client', 'http']


def test_rejected_body_combined_carrier_is_withheld_without_hiding_contract_error():
    from test_bedrock_mantle import call
    raw = completion()
    tool = call()
    tool['function']['arguments'] = json.dumps({'suffix':'selected'})
    raw['choices'][0]['message'].update(content='synthetic-', tool_calls=[tool])

    frames, _ = fake_execute([raw], stream=False, joined=True)

    frame, = frames
    assert frame['code'] == 'tool_finish_mismatch'
    assert 'provider_response' not in frame and 'result' not in frame
    assert frame['diagnostics']['response_validation'] == 'rejected'


@pytest.mark.parametrize('field', ['thinking_content', 'chain_of_thought', 'analysis'])
def test_rejected_body_alternative_reasoning_carrier_cannot_split_a_credential(field):
    raw = completion()
    raw['choices'][0]['message'].update(content='synthetic-', **{field:'selected'})
    frames, _ = fake_execute([raw], stream=False, joined=True)
    frame, = frames
    assert frame['code'] == 'unsupported_response_field'
    assert frame['diagnostics']['validation_path'] == '$.choices[].message'
    assert 'provider_response' not in frame and 'result' not in frame


def test_safety_rejection_does_not_report_body_or_imply_a_login_failure():
    raw = completion()
    raw['choices'][0]['message']['content'] = AUTH['api_key']
    frames, _ = fake_execute([raw], stream=False, joined=True)
    frame, = frames
    assert frame['code'] == 'response_safety_rejected'
    assert frame['error'] == 'unknown'
    assert frame['diagnostics'] == {'version':1, 'response_validation':'rejected'}
    assert AUTH['api_key'] not in json.dumps(frame)
    assert 'provider_response' not in frame


def test_actual_sdk_http_200_contract_failure_has_safe_shape_and_usage():
    from test_bedrock_mantle import call
    raw = dict(completion(), id='observed-response', object='chat.completion', created=0)
    raw['choices'][0]['message']['tool_calls'] = [call()]
    raw['usage'] = {'prompt_tokens':375, 'completion_tokens':25, 'total_tokens':400}
    requests, frames = [], []
    def handle(request):
        requests.append(request)
        return httpx2.Response(200, json=raw)
    worker.execute(request('completions'), frames.append,
        http_factory=lambda **kw: httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False),
        join=lambda secrets, payload: secrets)
    frame, = frames
    assert len(requests) == 1
    assert frame['code'] == 'tool_finish_mismatch' and frame['http_status'] == 200
    assert frame['diagnostics']['http_status'] == 200
    assert frame['diagnostics']['finish_reasons'] == ['stop']
    assert frame['diagnostics']['response_validation'] == 'rejected'
    assert frame['diagnostics']['validation_rule'] == 'tool_finish_consistency'
    assert frame['diagnostics']['validation_path'] == '$.choices[].finish_reason'
    assert frame['usage']['input_tokens'] == 375 and frame['usage']['output_tokens'] == 25
    assert 'result' not in frame and frame['provider_response']['usage'] == raw['usage']


@pytest.mark.parametrize('event', [[], {'error':{'message':'synthetic-secret'}}, {'type':'error'}, {'type':'response.failed'}])
def test_stream_error_is_never_published_as_success(event):
    emitted,closed=fake_execute([event])
    assert emitted == [{'error':'unknown','execution':'possibly_executed'}]
    assert closed == ['body','client','http']


@pytest.mark.parametrize('event', [[], {'error':{}}])
def test_nonstream_error_is_safe(event):
    assert fake_execute([event],stream=False)[0] == [{'error':'unknown','execution':'possibly_executed'}]


@pytest.mark.parametrize('stream', [False, True])
def test_error_result_keeps_validated_usage_without_error_details(stream):
    raw = {'error': {'message': 'synthetic-secret'}, 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    emitted, _ = fake_execute([raw], stream=stream)
    assert emitted == [{'error': 'unknown', 'execution': 'possibly_executed',
        'usage': {'input_tokens': 2, 'output_tokens': 3, 'cached_input_tokens': None, 'reasoning_tokens': None}}]


def test_failed_response_event_keeps_usage():
    event = {'type': 'response.failed', 'response': {'error': {'message': 'synthetic-secret'},
        'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}}}
    class Body:
        def __iter__(self):
            yield SimpleNamespace(model_dump=lambda **kw: event)
        def close(self): pass
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: Body()), close=lambda: None)
    emitted = []
    worker.execute(request('responses_stream'), emitted.append, client_factory=lambda *args: client,
        http_factory=lambda **kw: SimpleNamespace(close=lambda: None))
    assert emitted == [{'error': 'unknown', 'execution': 'possibly_executed',
        'usage': {'input_tokens': 2, 'output_tokens': 3, 'cached_input_tokens': None, 'reasoning_tokens': None}}]


@pytest.mark.parametrize('body_fails,client_fails', [(True,False),(False,True),(True,True)])
def test_cleanup_attempts_every_owner(body_fails,client_fails):
    closed = []
    with pytest.raises(ValueError, match='synthetic-secret'):
        fake_execute([{}],fail_body=body_fails,fail_client=client_fails,observed=closed)
    assert closed == ['body', 'client', 'http']


def test_constructor_failure_closes_http():
    assert fake_execute([],fail_construct=True) == ([{'error':'unknown','execution':'not_started'}],['http'])


@pytest.mark.parametrize('name,kind', [('MissingDependencyException','setup'),('ProfileNotFound','setup'),('NoRegionError','setup'),('ModuleNotFoundError','setup'),('ImportError','setup'),('NoCredentialsError','authentication'),('PartialCredentialsError','authentication'),('APIConnectionError','unavailable'),('APITimeoutError','unavailable')])
def test_error_classification(name,kind):
    def fail(**kwargs): raise type(name,(Exception,),{})('synthetic-secret')
    emitted=[]
    worker.execute(request(),emitted.append,http_factory=fail)
    assert emitted==[{'error':kind,'execution':'not_started'}]


def test_lazy_session_factory(monkeypatch):
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(Session=lambda **kw:kw))
    assert worker.sdk_session(profile_name='synthetic')=={'profile_name':'synthetic'}


def test_http_policy():
    client=worker.sdk_http()
    try:
        assert client.follow_redirects is False and client.trust_env is False
        assert client.timeout.connect==10 and client.timeout.read==900
    finally: client.close()


@pytest.mark.parametrize('raw',['{','x'*(worker.MAX_FRAME+1)])
def test_main_bounded_input(raw):
    output=io.StringIO();worker.main(io.StringIO(raw),output)
    assert json.loads(output.getvalue())=={'error':'protocol'}


def test_main_output(monkeypatch):
    monkeypatch.setattr(worker,'execute',lambda req,emit:emit({'result':req}))
    output=io.StringIO();worker.main(io.StringIO('{}'),output)
    assert output.getvalue()=='{"result": {}}\n'


def test_entrypoint(monkeypatch):
    monkeypatch.setattr(sys,'stdin',io.StringIO('{'));output=io.StringIO();monkeypatch.setattr(sys,'stdout',output)
    with pytest.warns(RuntimeWarning): runpy.run_module(worker.__name__,run_name='__main__')
    assert json.loads(output.getvalue())=={'error':'protocol'}


@pytest.mark.parametrize('operation', ['completions', 'responses', 'completions_stream', 'responses_stream'])
@pytest.mark.parametrize('control,value', [
    ('extra_body', {'store': True, 'stream': False, 'model': 'overridden'}),
    ('extra_headers', {'Authorization': 'Bearer overridden'}),
    ('extra_query', {'stream': False}), ('timeout', 9999),
    ('max_retries', 3), ('base_url', 'https://wrong.invalid'),
    ('stream', False), ('stream_options', {'include_usage': False}),
    ('store', True), ('unexpected', None),
])
def test_sdk_escape_controls_rejected_before_profile_or_http(operation, control, value):
    value_request = request(operation, {'mode': 'session', 'profile': 'synthetic'})
    value_request['payload']['body'][control] = value
    emitted = []
    worker.execute(value_request, emitted.append,
        session_factory=lambda **kw: pytest.fail('Profile lookup before admission'),
        http_factory=lambda **kw: pytest.fail('HTTP before admission'))
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('operation', ['responses', 'responses_stream'])
def test_actual_sdk_extra_body_cannot_override_worker_store_or_stream(operation):
    value_request = request(operation)
    value_request['payload']['body']['extra_body'] = {'store': True, 'stream': False}
    seen, emitted, owners = [], [], []
    def handle(req):
        seen.append(req)
        return httpx2.Response(200, json={'id': 'r', 'object': 'response', 'status': 'completed', 'created_at': 0, 'output': []})
    def make_http(*, timeout):
        http = httpx2.Client(transport=httpx2.MockTransport(handle))
        owners.append(http)
        return http
    worker.execute(value_request, emitted.append, http_factory=make_http)
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]
    assert seen == [] and owners == []


def test_encoded_response_store_false_is_admitted():
    emitted, closed = [], []
    value_request = request('responses')
    value_request['payload']['body']['store'] = False
    result = SimpleNamespace(model_dump=lambda **kw: response([message()]))
    def make_client(*args):
        def create(**kwargs):
            assert kwargs['store'] is False
            return result
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: closed.append('client'))
    worker.execute(value_request, emitted.append, client_factory=make_client,
        http_factory=lambda **kw: SimpleNamespace(close=lambda: closed.append('http')))
    assert len(emitted) == 1 and emitted[0]['result'] == response([message()])
    assert emitted[0]['diagnostics']['sdk_versions']['openai']
    assert closed == ['client', 'http']


@pytest.mark.parametrize('failure', ['missing', 'io', 'malformed'])
def test_optional_sdk_version_metadata_cannot_fail_completed_inference(monkeypatch, failure):
    import importlib.metadata
    def version(package):
        if failure == 'missing':
            raise importlib.metadata.PackageNotFoundError(package)
        if failure == 'io':
            raise OSError('synthetic metadata read failure')
        return 'not a version\nprivate text'
    monkeypatch.setattr(importlib.metadata, 'version', version)
    emitted, closed = [], []
    result = SimpleNamespace(model_dump=lambda **kw: response([message()]))
    worker.execute(request('responses'), emitted.append,
        client_factory=lambda *args: SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: result),
                                                     close=lambda: closed.append('client')),
        http_factory=lambda **kw: SimpleNamespace(close=lambda: closed.append('http')))
    assert emitted == [{'result': response([message()]), 'diagnostics': {'version': 1, 'sdk_versions': {}}}]
    assert closed == ['client', 'http']


def test_actual_sdk_rejects_incomplete_terminal_without_lifecycle():
    terminal = {
        'type': 'response.incomplete', 'sequence_number': 1,
        'response': {'id': 'r', 'object': 'response', 'created_at': 0,
            'model': 'synthetic', 'status': 'incomplete', 'error': None,
            'incomplete_details': {'reason': 'max_output_tokens'},
            'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}},
    }
    emitted, owners, seen = [], [], []
    def handle(req):
        seen.append(req)
        return httpx2.Response(200, headers={'content-type': 'text/event-stream'},
            content='data: '+json.dumps(terminal)+'\n\ndata: [DONE]\n\n')
    def make_http(*, timeout):
        http = httpx2.Client(transport=httpx2.MockTransport(handle), follow_redirects=False, trust_env=False)
        owners.append(http)
        return http
    worker.execute(request('responses_stream'), emitted.append, http_factory=make_http)
    assert len(seen) == 1 and owners[0].is_closed
    assert emitted == [{'error': 'unknown', 'execution': 'possibly_executed', 'http_status': 200,
                        'usage': {'input_tokens': 1, 'output_tokens': 1, 'cached_input_tokens': None, 'reasoning_tokens': None}}]


def test_actual_sdk_incomplete_response_preserves_text_usage_and_length():
    from test_bedrock_mantle import response, message
    from api_agent.llm_providers.bedrock.normalized_stream import ParentStream
    events = response_stream()
    item = message() | {'status': 'incomplete'}
    events[-2]['item'] = item
    events[-1].update(type='response.incomplete', response=response([item]) | {
        'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}})
    emitted, owners, seen = [], [], []
    def handle(req):
        seen.append(req)
        body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        return httpx2.Response(200, headers={'content-type': 'text/event-stream'}, content=body + 'data: [DONE]\n\n')
    def make_http(*, timeout):
        http = httpx2.Client(transport=httpx2.MockTransport(handle), follow_redirects=False, trust_env=False)
        owners.append(http)
        return http
    worker.execute(request('responses_stream'), emitted.append, http_factory=make_http)
    assert len(seen) == 1 and owners[0].is_closed
    parent = ParentStream('responses', selected_llm=MODEL)
    text = []
    for frame in emitted:
        assert set(frame) == {'event'}
        text.extend(event.payload.text for event in parent.feed(frame['event']))
    result = parent.finish()
    assert ''.join(text) == 'hi' and result.payload.content[0].text == 'hi'
    assert result.outcome == 'length'
    assert (result.usage.input_tokens, result.usage.output_tokens) == (2, 3)


def test_incomplete_event_with_top_level_error_remains_rejected():
    emitted, closed = fake_execute([{'type': 'response.incomplete', 'error': {'message': 'synthetic-secret'}}])
    assert emitted == [{'error': 'unknown', 'execution': 'possibly_executed'}]
    assert closed == ['body', 'client', 'http']


@pytest.mark.parametrize('value', [True, False, 0, -1, float('inf'), float('nan'), '240', None, 3601, 10**1000, [], {}])
def test_invalid_wire_deadline_precedes_auth(value):
    value_request = request(auth={'mode': 'session'})
    value_request['timeout'] = value
    emitted = []
    worker.execute(value_request, emitted.append,
                   session_factory=lambda **kw: pytest.fail('auth started'),
                   http_factory=lambda **kw: pytest.fail('HTTP started'))
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('timeout', [900, 240, .25])
def test_real_sdk_receives_wire_timeout_without_retry(timeout, monkeypatch):
    monkeypatch.setattr(worker.time, "monotonic", lambda: 10)
    seen, owners, emitted = [], [], []
    def handle(req):
        seen.append(req)
        return httpx2.Response(503, json={'error': {'message': 'synthetic'}})
    def http_factory(**kwargs):
        assert kwargs == {'timeout': timeout}
        client = worker.sdk_http(**kwargs)
        client._transport.close()
        client._transport = httpx2.MockTransport(handle)
        owners.append(client)
        return client
    value_request = request()
    value_request['timeout'] = timeout
    worker.execute(value_request, emitted.append, http_factory=http_factory)
    assert len(seen) == 1
    assert seen[0].extensions['timeout'] == {
        'connect': min(10, timeout), 'read': timeout, 'write': timeout, 'pool': timeout}
    assert owners[0].is_closed
    assert emitted == [{'error': 'unavailable', 'execution': 'possibly_executed', 'http_status': 503}]


def test_worker_deducts_authentication_elapsed_time(monkeypatch):
    received = []
    clock = iter([10, 12])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(clock))
    emitted = []
    def http_factory(*, timeout):
        received.append(timeout)
        raise RuntimeError('synthetic stop')
    value_request = request()
    value_request['timeout'] = 240
    worker.execute(value_request, emitted.append, http_factory=http_factory)
    assert received == [238]
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


def test_worker_exhausted_deadline_never_constructs_http(monkeypatch):
    clock = iter([10, 250])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(clock))
    emitted = []
    value_request = request()
    value_request['timeout'] = 240
    worker.execute(value_request, emitted.append,
                   http_factory=lambda **kw: pytest.fail('HTTP constructed'))
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


def test_missing_wire_timeout_is_rejected_before_auth():
    value_request = request(auth={'mode': 'session'})
    value_request.pop('timeout')
    emitted = []
    worker.execute(value_request, emitted.append,
                   session_factory=lambda **kw: pytest.fail('auth started'),
                   http_factory=lambda **kw: pytest.fail('HTTP started'))
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


def test_worker_deducts_json_validation_elapsed_time(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    value_request = request()
    original = worker.json_value
    def validate(value):
        result = original(value)
        if value is value_request:
            clock[0] += 2
        return result
    monkeypatch.setattr(worker, 'json_value', validate)
    received, emitted = [], []
    def http_factory(*, timeout):
        received.append(timeout)
        raise RuntimeError('synthetic stop')
    worker.execute(value_request, emitted.append, http_factory=http_factory)
    assert received == [238]
    assert emitted == [{'error': 'unknown', 'execution': 'not_started'}]


def test_worker_retains_safe_retry_after_with_http_status():
    emitted = []
    def handle(request):
        return httpx2.Response(429, headers={'retry-after': '9', 'x-private': 'sensitive'},
                               json={'error': {'message': 'sensitive'}})
    worker.execute(request(), emitted.append, http_factory=lambda **kw: httpx2.Client(
        transport=httpx2.MockTransport(handle), follow_redirects=False, trust_env=False))
    assert emitted == [{'error': 'rate_limited', 'execution': 'possibly_executed',
                        'http_status': 429, 'retry_after': 9}]


def test_body_timeout_after_headers_retains_status_and_closes_worker_http():
    emitted, owners = [], []
    class Stream(httpx2.SyncByteStream):
        def __iter__(self):
            raise httpx2.ReadTimeout('synthetic timeout')
            yield b''
    def handle(request):
        return httpx2.Response(503, headers={'retry-after': '3'}, stream=Stream())
    def http_factory(**kwargs):
        client = httpx2.Client(transport=httpx2.MockTransport(handle), follow_redirects=False, trust_env=False)
        owners.append(client)
        return client
    worker.execute(request(), emitted.append, http_factory=http_factory)
    assert owners[0].is_closed
    assert emitted == [{'error': 'unavailable', 'execution': 'possibly_executed',
                        'http_status': 503, 'retry_after': 3}]


@pytest.mark.parametrize('reply, ready, expected', [
    (['synthetic-credential', 'other-secret'], b'1', 'joined'),
    (['other-secret'], b'1', 'protocol'),
    ('invalid', b'1', 'protocol'),
    (['synthetic-credential'], b'0', 'protocol'),
])
def test_worker_authority_handoff_validates_union_and_acknowledgement(monkeypatch, reply, ready, expected):
    import socket
    import threading
    parent, child = socket.socketpair()
    monkeypatch.setenv('API_AGENT_BEDROCK_AUTHORITY_FD', str(child.detach()))
    observed = []
    def coordinator():
        with parent:
            line = bytearray()
            while not line.endswith(b'\n'):
                line.extend(parent.recv(1))
            observed.append(json.loads(line))
            parent.sendall(json.dumps(reply).encode() + b'\n')
            if expected == 'joined' or ready == b'0':
                assert parent.recv(1) == b'1'
                parent.sendall(ready)
    thread = threading.Thread(target=coordinator)
    thread.start()
    def execute(value, emit, *, join):
        union = join(['synthetic-credential'], {'prompt': 'ordinary'})
        emit({'result': {'joined': len(union)}})
    monkeypatch.setattr(worker, 'execute', execute)
    output = io.StringIO()
    try:
        assert worker.main(io.StringIO('{}\n'), output) is None
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert observed == [['synthetic-credential']]
        frame = json.loads(output.getvalue())
        assert frame == ({'result': {'joined': 2}} if expected == 'joined' else {'error': 'protocol'})
        assert 'synthetic-credential' not in output.getvalue()
    finally:
        parent.close()


@pytest.mark.parametrize('body, retained', [
    ({'message': 'ordinary failure'}, True),
    ({'message': 'synthetic-selected'}, False),
    (None, False),
])
def test_joined_observed_error_only_retains_safe_provider_response(body, retained):
    class Failure(Exception):
        status_code = 503
        def __init__(self): self.body = body
    class Client:
        models = SimpleNamespace(list=lambda: (_ for _ in ()).throw(Failure()))
        def close(self): pass
    class Http:
        def close(self): pass
    frames = []
    worker.execute(request(), frames.append, join=lambda values, arguments: values,
        client_factory=lambda *args: Client(), http_factory=lambda **kwargs: Http())
    assert len(frames) == 1 and frames[0]['error'] == 'unavailable'
    assert frames[0]['execution'] == 'possibly_executed'
    assert ('provider_response' in frames[0]) is retained
    if retained: assert frames[0]['provider_response'] == body
    assert 'synthetic-selected' not in json.dumps(frames)


def test_long_safe_worker_stream_emits_before_terminal_and_closes_owners():
    from test_bedrock_mantle import chunk, usage
    text = 'ordinary streamed response with enough characters to cross the retained credential lookbehind'
    events = [chunk({'role': 'assistant'}), chunk({'content': text}),
              chunk(finish='stop'), chunk(choices=[], usage=usage())]
    frames, closed = fake_execute(events)
    assert closed == ['body', 'client', 'http']
    assert frames[0]['event']['kind'] == 'text'
    assert ''.join(frame['event'].get('text', '') for frame in frames) == text
    assert frames[-1]['event']['kind'] == 'completed'


@pytest.mark.parametrize('case', ['oversized-outgoing', 'closed-peer', 'oversized-incoming'])
def test_worker_authority_rejects_oversized_or_disconnected_handoff(monkeypatch, case):
    sent, closed = [], []
    class Channel:
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
        def settimeout(self, value): assert value == 30
        def sendall(self, value): sent.append(value)
        def recv(self, size): return b'' if case == 'closed-peer' else b'x' * size
    monkeypatch.setattr(worker.socket, 'socket', lambda *, fileno: Channel())
    monkeypatch.setenv('API_AGENT_BEDROCK_AUTHORITY_FD', '9')
    def execute(value, emit, *, join):
        join(['x' * 33000] if case == 'oversized-outgoing' else ['secret'], {'prompt': 'safe'})
    monkeypatch.setattr(worker, 'execute', execute)
    output = io.StringIO()
    worker.main(io.StringIO('{}\n'), output)
    assert json.loads(output.getvalue()) == {'error': 'protocol'}
    assert closed == [True]
    assert len(sent) == (0 if case == 'oversized-outgoing' else 1)


def test_joined_error_uses_safe_provider_response_without_fallback_body():
    class Failure(Exception):
        status_code = 503
        provider_response = {'message': 'safe response'}
        body = {'message': 'synthetic-selected'}
    class Client:
        models = SimpleNamespace(list=lambda: (_ for _ in ()).throw(Failure()))
        def close(self): pass
    frames = []
    worker.execute(request(), frames.append, join=lambda values, arguments: values,
        client_factory=lambda *args: Client(), http_factory=lambda **kwargs: SimpleNamespace(close=lambda: None))
    assert frames == [{'error': 'unavailable', 'execution': 'possibly_executed',
                       'provider_response': {'message': 'safe response'}, 'http_status': 503}]


def test_unclassified_sdk_decode_failure_closes_owned_clients(monkeypatch):
    from api_agent.llm_providers.bedrock import mantle
    def broken(*args, **kwargs):
        raise ValueError('unclassified decoder failure')
    monkeypatch.setattr(mantle, 'decode', broken)
    frames, closed = fake_execute([completion()], stream=False)
    assert frames == [{'error':'unknown', 'execution':'possibly_executed'}]
    assert closed == ['client', 'http']


def test_rejected_response_retains_code_when_diagnostics_are_unavailable(monkeypatch):
    from api_agent.llm_providers.bedrock import mantle
    from api_agent.telemetry import inference_diagnostics
    from api_agent.providers.contracts import ProviderAccessError
    def broken(*args, **kwargs):
        raise ProviderAccessError('unknown', 'Rejected response', code='invalid_response')
    monkeypatch.setattr(mantle, 'decode', broken)
    monkeypatch.setattr(inference_diagnostics, 'scrub_diagnostics', lambda value, boundary: {})
    frames, closed = fake_execute([completion()], stream=False)
    assert frames == [{'error':'unknown', 'execution':'possibly_executed', 'code':'invalid_response'}]
    assert closed == ['client', 'http']
