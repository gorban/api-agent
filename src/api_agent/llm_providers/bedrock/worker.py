"""Owned synchronous Mantle SDK lifecycle; diagnostics contain no SDK text."""
import json
import re
import sys
import time

MAX_FRAME = 8 * 1024 * 1024
DEFAULT_INFERENCE_TIMEOUT = 900
MAX_INFERENCE_TIMEOUT = 3600


def inference_timeout(value):
    if (type(value) not in (int, float)
            or not 0 < value <= MAX_INFERENCE_TIMEOUT):
        raise ValueError('Invalid Bedrock inference timeout')
    return value


OPERATIONS = {'models', 'completions', 'responses', 'completions_stream', 'responses_stream'}


def sdk_session(**kwargs):
    import boto3
    return boto3.Session(**kwargs)


def json_value(value):
    """Admit JSON values only, without coercing keys, tuples or nonfinite floats."""
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError()
        for item in value.values():
            json_value(item)
    elif type(value) is list:
        for item in value:
            json_value(item)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError()
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_FRAME:
        raise ValueError()
    return value


def selected_auth(auth, session_factory):
    mode = auth['mode']
    region = auth.get('region')
    if mode == 'api_key':
        options = {'api_key': auth['api_key']}
        if any(auth.get(key) is not None for key in ('access_key', 'secret_key', 'session_token', 'profile')):
            raise ValueError()
        if region is None:
            region = session_factory().region_name
    elif mode in {'access_key', 'temporary'}:
        options = {'api_key': None, 'access_key_id': auth['access_key'], 'secret_access_key': auth['secret_key']}
        if mode == 'temporary':
            options['session_token'] = auth['session_token']
        if region is None:
            # Region discovery only: selected credentials never use the chain.
            region = session_factory().region_name
    elif mode == 'session':
        session = session_factory(region_name=region, profile_name=auth.get('profile'))
        region = session.region_name
        credentials = session.get_credentials()
        if credentials is None:
            from botocore.exceptions import NoCredentialsError
            raise NoCredentialsError()
        frozen = credentials.get_frozen_credentials()
        options = {'api_key': None, 'access_key_id': frozen.access_key,
                   'secret_access_key': frozen.secret_key}
        if frozen.token is not None:
            options['session_token'] = frozen.token
    else:
        raise ValueError()
    if any(not isinstance(value, str) or not value.strip() for key, value in options.items() if key != 'api_key' or mode == 'api_key'):
        raise ValueError()
    if not isinstance(region, str) or re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region) is None:
        raise ValueError()
    return region, options


def sdk_client(region, prefix, options, http):
    from openai import OpenAI
    from openai.providers.bedrock import bedrock
    return OpenAI(provider=bedrock(endpoint='mantle', region=region,
                  base_url=f'https://bedrock-mantle.{region}.api.aws{prefix}', **options),
                  http_client=http, max_retries=0, timeout=http.timeout)


def sdk_http(timeout=DEFAULT_INFERENCE_TIMEOUT):
    import httpx2
    return httpx2.Client(verify=True, follow_redirects=False, trust_env=False,
                         timeout=httpx2.Timeout(inference_timeout(timeout), connect=min(10, timeout)))



def qualified_schema(value, model, prefix):
    """Independently validate the sole qualified native schema route before auth."""
    from api_agent.providers.rich_inference import schema
    if (model != 'zai.glm-5' or prefix != '/v1' or type(value) is not dict
            or set(value) != {'type', 'json_schema'} or value['type'] != 'json_schema'):
        raise ValueError()
    definition = value['json_schema']
    if (type(definition) is not dict or set(definition) != {'name', 'schema', 'strict'}
            or definition['strict'] is not True or type(definition['name']) is not str
            or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', definition['name']) is None):
        raise ValueError()
    schema(json.dumps(definition['schema'], allow_nan=False), True)

def execute(request, emit, *, session_factory=sdk_session, client_factory=sdk_client, http_factory=sdk_http):
    started = time.monotonic()
    client = body = http = None
    execution = 'not_started'
    try:
        json_value(request)
        if set(request) != {'operation', 'auth', 'payload', 'timeout'} or request['operation'] not in OPERATIONS:
            raise ValueError()
        timeout = inference_timeout(request['timeout'])
        deadline = started + timeout
        operation, payload = request['operation'], request['payload']
        if operation == 'models':
            if payload != {}:
                raise ValueError()
            prefix, arguments = '/v1', {}
        else:
            if set(payload) != {'prefix', 'body'} or payload['prefix'] not in {'/v1', '/openai/v1'} or type(payload['body']) is not dict:
                raise ValueError()
            prefix, arguments = payload['prefix'], dict(payload['body'])
            response = operation.startswith('responses')
            allowed = ({'model', 'input', 'tools', 'temperature', 'max_output_tokens', 'store', 'reasoning'}
                       if response else {'model', 'messages', 'tools', 'temperature', 'max_tokens', 'reasoning_effort', 'response_format'})
            if (not set(arguments) <= allowed
                    or 'store' in arguments and arguments['store'] is not False):
                raise ValueError()
            if 'response_format' in arguments:
                qualified_schema(arguments['response_format'], arguments.get('model'), prefix)
            control = 'reasoning' if response else 'reasoning_effort'
            if control in arguments:
                effort = arguments[control]
                if response:
                    if type(effort) is not dict or set(effort) != {'effort'}:
                        raise ValueError()
                    effort = effort['effort']
                if (prefix != '/v1' or arguments.get('model') not in
                        ('openai.gpt-oss-20b', 'openai.gpt-oss-120b')
                        or type(effort) is not str or effort not in ('low', 'medium', 'high')):
                    raise ValueError()
            if operation.startswith('responses'):
                arguments['store'] = False
            if operation.endswith('_stream'):
                arguments['stream'] = True
                if not response:
                    arguments['stream_options'] = {'include_usage': True}
        region, options = selected_auth(request['auth'], session_factory)
        timeout = inference_timeout(deadline - time.monotonic())
        http = http_factory(timeout=timeout)
        client = client_factory(region, prefix, options, http)
        execution = 'possibly_executed'
        if operation == 'models':
            result = client.models.list()
        elif operation.startswith('completions'):
            result = client.chat.completions.create(**arguments)
        else:
            result = client.responses.create(**arguments)
        if operation.endswith('_stream'):
            body = result
            for event in body:
                value = json_value(event.model_dump(exclude_unset=True))
                if type(value) is not dict or value.get('error') is not None or value.get('type') in {'error', 'response.failed'}:
                    raise ValueError()
                emit({'event': value})
        else:
            value = json_value(result.model_dump(exclude_unset=True))
            if type(value) is not dict or value.get('error') is not None:
                raise ValueError()
            emit({'result': value})
    except Exception as error:
        name = type(error).__name__
        status = getattr(error, 'status_code', None)
        kind = {401: 'authentication', 403: 'authorization', 429: 'rate_limited',
                400: 'invalid_request', 404: 'invalid_request', 422: 'invalid_request',
                500: 'unavailable', 502: 'unavailable', 503: 'unavailable', 504: 'unavailable'}.get(status)
        if kind is None:
            kind = {'NoCredentialsError': 'authentication', 'PartialCredentialsError': 'authentication',
                    'APIConnectionError': 'unavailable', 'APITimeoutError': 'unavailable',
                    'ModuleNotFoundError': 'setup', 'ImportError': 'setup',
                    'MissingDependencyException': 'setup', 'ProfileNotFound': 'setup', 'NoRegionError': 'setup'}.get(name, 'unknown')
        emit({'error': kind, 'execution': execution})
    finally:
        try:
            if body is not None:
                body.close()
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                if http is not None:
                    http.close()


def main(input_stream=None, output_stream=None):
    source = sys.stdin if input_stream is None else input_stream
    target = sys.stdout if output_stream is None else output_stream

    def emit(value):
        encoded = json.dumps(json_value(value), allow_nan=False)
        target.write(encoded + '\n')
        target.flush()

    try:
        raw = source.readline(MAX_FRAME + 1)
        if len(raw.encode()) > MAX_FRAME:
            raise ValueError()
        execute(json.loads(raw), emit)
    except Exception:
        emit({'error': 'protocol'})


if __name__ == '__main__':
    main()
