"""Offline maintained SDK qualification and worker ownership checks."""
import io
import json
import runpy
import sys
from types import SimpleNamespace

import httpx2
import pytest
from api_agent.llm_providers.bedrock import worker


AUTH = {'mode': 'api_key', 'api_key': 'synthetic-selected', 'region': 'us-east-1'}


def request(operation='models', auth=None, prefix='/v1'):
    return {'timeout': 240, 'operation': operation, 'auth': AUTH if auth is None else auth,
            'payload': {} if operation == 'models' else {'prefix': prefix, 'body': {
                'model': 'synthetic', **({'messages': [{'role': 'user', 'content': 'hello'}]} if operation.startswith('completions') else {'input': 'hello'})}}}


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
            value = {'type': 'response.output_text.delta', 'delta': 'hello', 'output_index': 0, 'content_index': 0, 'item_id': 'i', 'sequence_number': 0} if operation.startswith('responses') else {'id': 'c', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'synthetic', 'choices': [{'index': 0, 'delta': {'content': 'hello'}}]}
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'}, content='data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n')
        value = {'object': 'list', 'data': []} if operation == 'models' else {'id': 'r', 'object': 'response', 'output': [], 'status': 'completed', 'created_at': 0} if operation == 'responses' else {'id': 'c', 'object': 'chat.completion', 'created': 0, 'model': 'synthetic', 'choices': []}
        return httpx2.Response(200, json=value)
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
    assert set(emitted[0]) == ({'event'} if operation.endswith('_stream') else {'result'})
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
    assert emitted == [{'error': kind, 'execution': 'possibly_executed'}]


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


def fake_execute(events, *, stream=True, fail_body=False, fail_client=False, fail_construct=False, observed=None):
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
    worker.execute(request('completions_stream' if stream else 'completions'),emitted.append,client_factory=factory,http_factory=lambda **kw:SimpleNamespace(close=lambda:close('http')))
    return emitted,closed


@pytest.mark.parametrize('event', [[], {'error':{'message':'synthetic-secret'}}, {'type':'error'}, {'type':'response.failed'}])
def test_stream_error_is_never_published_as_success(event):
    emitted,closed=fake_execute([event])
    assert emitted == [{'error':'unknown','execution':'possibly_executed'}]
    assert closed == ['body','client','http']


@pytest.mark.parametrize('event', [[], {'error':{}}])
def test_nonstream_error_is_safe(event):
    assert fake_execute([event],stream=False)[0] == [{'error':'unknown','execution':'possibly_executed'}]


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
    result = SimpleNamespace(model_dump=lambda **kw: {'status': 'completed'})
    def make_client(*args):
        def create(**kwargs):
            assert kwargs['store'] is False
            return result
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: closed.append('client'))
    worker.execute(value_request, emitted.append, client_factory=make_client,
        http_factory=lambda **kw: SimpleNamespace(close=lambda: closed.append('http')))
    assert emitted == [{'result': {'status': 'completed'}}]
    assert closed == ['client', 'http']


def test_actual_sdk_incomplete_terminal_reaches_codec_boundary():
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
    assert emitted == [{'event': terminal}]


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
    assert emitted == [{'error': 'unavailable', 'execution': 'possibly_executed'}]


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
