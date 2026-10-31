"""Owned synchronous Bedrock SDK lifecycle; diagnostics contain no SDK text."""
import json
import os
import socket
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


RUNTIME_OPERATIONS = {'runtime_models', 'converse', 'converse_stream'}
OPERATIONS = {'models', 'completions', 'responses', 'completions_stream', 'responses_stream'}

ALL_OPERATIONS = OPERATIONS | RUNTIME_OPERATIONS


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



def qualified_schema(value, model, prefix, *, endpoint='mantle', response=False):
    """Validate the selected endpoint and native schema carrier before auth."""
    from api_agent.providers.rich_inference import schema
    if endpoint == 'runtime':
        from api_agent.llm_providers.bedrock.catalog import selection
        selected = selection(model, 'responses' if response else 'completions', endpoint=endpoint)
        if prefix != '/openai/v1' or 'structured' not in selected.features:
            raise ValueError()
        if response:
            if type(value) is not dict or set(value) != {'format'} or type(value['format']) is not dict:
                raise ValueError()
            format_spec = dict(value['format'])
            value = {'type': format_spec.pop('type', None), 'json_schema': format_spec}
    elif model != 'zai.glm-5' or prefix != '/v1':
        raise ValueError()
    if (type(value) is not dict
            or set(value) != {'type', 'json_schema'} or value['type'] != 'json_schema'):
        raise ValueError()
    definition = value['json_schema']
    if (type(definition) is not dict or set(definition) != {'name', 'schema', 'strict'}
            or definition['strict'] is not True or type(definition['name']) is not str
            or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', definition['name']) is None):
        raise ValueError()
    schema(json.dumps(definition['schema'], allow_nan=False), True)

def execute(request, emit, *, session_factory=sdk_session, client_factory=sdk_client, http_factory=sdk_http, join=None, runtime_client_factory=None, boto_client_factory=None):
    started = time.monotonic()
    client = body = http = None
    execution = 'not_started'
    received = {}
    secrets = None
    joined = False
    native = False
    endpoint = None
    decode_failure = None
    failed_observation = None
    try:
        json_value(request)
        if set(request) != {'operation', 'auth', 'payload', 'timeout'} or request['operation'] not in ALL_OPERATIONS:
            raise ValueError()
        timeout = inference_timeout(request['timeout'])
        deadline = started + timeout
        operation, payload = request['operation'], request['payload']
        endpoint = payload.get('endpoint', 'mantle') if type(payload) is dict else None
        if endpoint not in ('mantle', 'runtime'):
            raise ValueError()
        if operation == 'runtime_models':
            endpoint = 'runtime'
        native = operation in RUNTIME_OPERATIONS
        if operation in ('models', 'runtime_models'):
            if payload != {}:
                raise ValueError()
            prefix, arguments = '/v1', {}
        else:
            if (set(payload) not in ({'prefix', 'body'}, {'prefix', 'body', 'endpoint'})
                    or payload['prefix'] not in {'/v1', '/openai/v1', '/converse'}
                    or type(payload['body']) is not dict):
                raise ValueError()
            prefix, arguments = payload['prefix'], dict(payload['body'])
            if (endpoint == 'runtime' and prefix != ('/converse' if native else '/openai/v1')
                    or endpoint == 'mantle' and (native or prefix == '/converse')):
                raise ValueError()
            response = operation.startswith('responses')
            allowed = ({'modelId', 'messages', 'system', 'inferenceConfig', 'toolConfig', 'outputConfig'}
                       if native else {'model', 'input', 'tools', 'temperature', 'max_output_tokens', 'store', 'reasoning', 'text'}
                       if response else {'model', 'messages', 'tools', 'temperature', 'max_tokens', 'reasoning_effort', 'response_format'})
            if (not set(arguments) <= allowed
                    or 'store' in arguments and arguments['store'] is not False):
                raise ValueError()
            if endpoint == 'runtime':
                from api_agent.llm_providers.bedrock.catalog import selection
                selected = selection(arguments.get('modelId') if native else arguments.get('model'),
                                     'converse' if native else ('responses' if response else 'completions'),
                                     endpoint=endpoint)
            if endpoint == 'runtime' and not native and operation.endswith('_stream') and ('response_format' in arguments or 'text' in arguments):
                raise ValueError()
            if native and 'outputConfig' in arguments:
                from api_agent.llm_providers.bedrock.runtime_worker import converse_schema
                converse_schema(arguments['outputConfig'], selected)
            if 'response_format' in arguments:
                if endpoint == 'runtime' and 'structured' not in selected.features:
                    raise ValueError()
                qualified_schema(arguments['response_format'], arguments.get('model'), prefix,
                                 **({'endpoint': endpoint} if endpoint != 'mantle' else {}))
            if 'text' in arguments:
                if endpoint != 'runtime' or not response:
                    raise ValueError()
                qualified_schema(arguments['text'], arguments.get('model'), prefix, endpoint=endpoint, response=True)
            control = 'reasoning' if response else 'reasoning_effort'
            if control in arguments:
                effort = arguments[control]
                if response:
                    if type(effort) is not dict or set(effort) != {'effort'}:
                        raise ValueError()
                    effort = effort['effort']
                if (type(effort) is not str
                        or endpoint == 'runtime' and ('reasoning_effort' not in selected.features
                            or effort not in (selected.reasoning_efforts or ()))
                        or endpoint == 'mantle' and (prefix != '/v1' or arguments.get('model') not in
                            ('openai.gpt-oss-20b', 'openai.gpt-oss-120b')
                            or effort not in ('low', 'medium', 'high'))):
                    raise ValueError()
            if operation.startswith('responses'):
                arguments['store'] = False
            if operation.endswith('_stream') and not native:
                arguments['stream'] = True
                if not response:
                    arguments['stream_options'] = {'include_usage': True}
        region, options = selected_auth(request['auth'], session_factory)
        from api_agent.llm_providers.bedrock.normalized_stream import guard_semantic
        secrets = {value for value in options.values() if isinstance(value, str) and value}
        guard_semantic(arguments, secrets)
        if join is not None:
            secrets = set(join(sorted(secrets), arguments))
            joined = True
        timeout = inference_timeout(deadline - time.monotonic())
        if native:
            from api_agent.llm_providers.bedrock.runtime_worker import boto_client, catalog
            factory = boto_client_factory or boto_client
            client = factory(region, 'bedrock' if operation == 'runtime_models' else 'bedrock-runtime', options, timeout)
        else:
            http = http_factory(timeout=timeout)
        hooks = getattr(http, 'event_hooks', None)
        if isinstance(hooks, dict) and isinstance(hooks.get('response'), list):
            def capture_headers(response):
                received['status'] = response.status_code
                # Only a named request identifier is observed; never arbitrary
                # headers or URLs. Admission/redaction happens before IPC.
                received['request_id'] = response.headers.get('x-amzn-requestid') or response.headers.get('x-request-id')
                from api_agent.providers.inference_retry import retry_after
                received['retry_after'] = retry_after(response.headers.get('retry-after'))
            hooks['response'].append(capture_headers)
        if not native:
            if endpoint == 'runtime':
                from api_agent.llm_providers.bedrock.runtime_worker import openai_client
                client = (runtime_client_factory or openai_client)(region, prefix, options, http)
            else:
                client = client_factory(region, prefix, options, http)
        execution = 'possibly_executed'
        if operation == 'runtime_models':
            result = catalog(client, deadline)
        elif native:
            result = client.converse(**arguments)
        elif operation == 'models':
            result = client.models.list()
        elif operation.startswith('completions'):
            result = client.chat.completions.create(**arguments)
        else:
            result = client.responses.create(**arguments)
        if operation.endswith('_stream') and not native:
            body = result
            from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream
            decoder = WorkerStream('responses' if response else 'completions', arguments['model'], secrets, **({'endpoint': endpoint} if endpoint != 'mantle' else {}))
            for event in body:
                value = json_value(event.model_dump(exclude_unset=True))
                if type(value) is not dict or value.get('error') is not None or value.get('type') in {'error', 'response.failed'}:
                    if type(value) is dict:
                        from api_agent.llm_providers.bedrock.mantle import _reported_error
                        from api_agent.providers.contracts import ProviderAccessError
                        raw = value.get('response', value)
                        failure = _reported_error(ProviderAccessError('unknown', 'Invalid Bedrock inference response.'), raw, response)
                        failure.provider_response = value
                        raise failure
                    raise ValueError()
                for frame in decoder.feed(value):
                    emit({'event': frame})
            for frame in decoder.finish():
                emit({'event': frame})
        else:
            value = json_value(result if native else result.model_dump(exclude_unset=True))
            if type(value) is not dict or value.get('error') is not None:
                if type(value) is dict:
                    from api_agent.llm_providers.bedrock.mantle import _reported_error
                    from api_agent.providers.contracts import ProviderAccessError
                    failure = _reported_error(ProviderAccessError('unknown', 'Invalid Bedrock inference response.'), value, operation.startswith('responses'))
                    failure.provider_response = value
                    raise failure
                raise ValueError()
            try:
                guard_semantic(value, secrets)
            except Exception:
                decode_failure = 'response_safety_rejected'
                failed_observation = {'version':1, 'response_validation':'rejected'}
                raise
            if operation not in ('models', 'runtime_models'):
                if native:
                    from api_agent.llm_providers.bedrock.converse import decode
                else:
                    from api_agent.llm_providers.bedrock.mantle import decode
                from api_agent.llm_providers.bedrock.normalized_stream import guard_result
                try:
                    decoded = decode(value, 'converse' if native else ('responses' if response else 'completions'),
                                     selected_llm=arguments['modelId'] if native else arguments['model'],
                                     **({'endpoint': endpoint} if endpoint != 'mantle' else {}))
                except Exception as error:
                    from api_agent.providers.contracts import ProviderAccessError
                    from api_agent.llm_providers.bedrock.mantle import RESPONSE_CODES
                    if isinstance(error, ProviderAccessError):
                        decode_failure = error.code if error.code in RESPONSE_CODES else 'invalid_response'
                        from api_agent.telemetry.inference_diagnostics import sdk_observation, scrub_diagnostics
                        from api_agent.telemetry.privacy import EmissionBoundary
                        # Only bounded content-free shape, never rejected text.
                        observation = sdk_observation({'provider_response':value}, 'response')
                        observation.update(scrub_diagnostics(getattr(error, 'provider_diagnostics', {}),
                                                            EmissionBoundary(secrets=secrets)))
                        failed_observation = scrub_diagnostics(observation, EmissionBoundary(secrets=secrets))
                        if failed_observation:
                            failed_observation['response_validation'] = 'rejected'
                            status = received.get('status')
                            if type(status) is int and 100 <= status <= 599:
                                failed_observation['http_status'] = status
                        if joined:
                            from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
                            try:
                                error.provider_response = guard_response_evidence(value, secrets)
                            except Exception:
                                # A failed decoder has not run the normal result
                                # guard. Evidence needs its own safety admission.
                                pass
                    raise
                try:
                    guard_result(decoded, secrets)
                except Exception:
                    decode_failure = 'response_safety_rejected'
                    failed_observation = {'version':1, 'response_validation':'rejected'}
                    raise
            metadata = {}
            if operation not in ('models', 'runtime_models'):
                from api_agent.telemetry.inference_diagnostics import valid_diagnostics, scrub_diagnostics
                from api_agent.telemetry.privacy import EmissionBoundary
                from importlib.metadata import version
                metadata = {'version': 1, 'sdk_versions': {}}
                for package, label in (('openai', 'openai'), ('httpx2', 'httpx')):
                    try:
                        candidate = {'version': 1, 'sdk_versions': {label: version(package)}}
                    except Exception:
                        # Optional package metadata must never turn an already
                        # completed, validated inference into a provider error.
                        continue
                    if valid_diagnostics(candidate):
                        metadata['sdk_versions'].update(candidate['sdk_versions'])
                for source, target in (('status', 'http_status'), ('request_id', 'request_id')):
                    candidate = {'version': 1, target: received.get(source)}
                    if valid_diagnostics(candidate):
                        metadata[target] = candidate[target]
                metadata = scrub_diagnostics(metadata, EmissionBoundary(secrets=secrets))
            if native and operation.endswith('_stream'):
                from api_agent.providers.inference import result_to_wire
                text = ''.join(part.text for part in decoded.payload.content)
                if text:
                    emit({'event': {'kind': 'text', 'text': text}})
                emit({'event': {'kind': 'completed', 'result': result_to_wire(decoded)}})
            else:
                emit(dict(result=value, **({'diagnostics': metadata} if metadata else {})))
    except Exception as error:
        name = type(error).__name__
        status = getattr(error, 'status_code', None)
        if status is None:
            status = received.get('status')
        native_metadata = {}
        if native and type(getattr(error, 'response', None)) is dict:
            candidate = error.response.get('ResponseMetadata')
            if type(candidate) is dict:
                native_metadata = candidate
                if status is None:
                    status = candidate.get('HTTPStatusCode')
        kind = {401: 'authentication', 403: 'authorization', 429: 'rate_limited',
                400: 'invalid_request', 404: 'invalid_request', 422: 'invalid_request',
                500: 'unavailable', 502: 'unavailable', 503: 'unavailable', 504: 'unavailable'}.get(status)
        if kind is None:
            kind = {'NoCredentialsError': 'authentication', 'PartialCredentialsError': 'authentication',
                    'APIConnectionError': 'unavailable', 'APITimeoutError': 'unavailable',
                    'ModuleNotFoundError': 'setup', 'ImportError': 'setup',
                    'MissingDependencyException': 'setup', 'ProfileNotFound': 'setup', 'NoRegionError': 'setup'}.get(name, 'unknown')
        if status in (None, 200) and name in {'APIConnectionError', 'APITimeoutError'}:
            import httpx2
            from api_agent.providers.inference_retry import transient_network_failure
            if transient_network_failure(error, httpx2):
                kind = 'network'
        if native and kind == 'unknown' and status in (None, 200):
            from api_agent.llm_providers.bedrock.runtime_worker import transient_network_failure
            if transient_network_failure(error):
                kind = 'network'
        frame = {'error': kind, 'execution': execution}
        if native and joined and execution == 'possibly_executed' and decode_failure is None:
            aws_response = getattr(error, 'response', None)
            if type(aws_response) is dict and 'Error' in aws_response:
                from api_agent.llm_providers.bedrock.runtime_worker import aws_error
                try:
                    frame['aws_error'] = aws_error(aws_response, secrets)
                except Exception:
                    frame['aws_error_rejected'] = True
        if not native and joined and execution == 'possibly_executed' and decode_failure is None:
            sdk_body = getattr(error, 'body', None)
            if sdk_body is not None:
                try:
                    from openai import APIStatusError
                except ImportError:
                    pass
                else:
                    if isinstance(error, APIStatusError):
                        from api_agent.llm_providers.bedrock.runtime_worker import openai_error
                        try:
                            request_id = received.get('request_id') or getattr(error, 'request_id', None)
                            frame['aws_error'] = openai_error(sdk_body, request_id, secrets)
                        except Exception:
                            frame['aws_error_rejected'] = True
        if decode_failure is not None:
            frame['code'] = decode_failure
            if failed_observation:
                frame['diagnostics'] = failed_observation
        # Only an observed JSON response, checked against the same frozen
        # credentials already joined by this call, can cross the process boundary.
        if joined and execution == 'possibly_executed' and frame.get('aws_error_rejected') is not True:
            raw_response = getattr(error, 'provider_response', None)
            if raw_response is None:
                raw_response = getattr(error, 'body', None)
            if type(raw_response) is dict:
                try:
                    frame['provider_response'] = guard_semantic(json_value(raw_response), secrets)
                except Exception:
                    pass
        from api_agent.providers.inference import error_usage, usage_to_wire
        usage = error_usage(error)
        if usage is not None:
            frame['usage'] = usage_to_wire(usage)
        if type(status) is int and 100 <= status <= 599:
            frame['http_status'] = status
            response = getattr(error, 'response', None)
            headers = getattr(response, 'headers', None)
            if native and type(native_metadata.get('HTTPHeaders')) is dict:
                headers = native_metadata['HTTPHeaders']
            from api_agent.providers.inference_retry import retry_after
            hint = retry_after(headers.get('retry-after')) if headers is not None else received.get('retry_after')
            if hint is not None:
                frame['retry_after'] = hint
        emit(frame)
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
        descriptor = os.environ.pop("API_AGENT_BEDROCK_AUTHORITY_FD", None)
        def join(values, arguments):
            with socket.socket(fileno=int(descriptor)) as channel:
                channel.settimeout(30)
                encoded = json.dumps(values, ensure_ascii=False).encode() + b"\n"
                if len(encoded) > 32768:
                    raise ValueError()
                channel.sendall(encoded)
                incoming = bytearray()
                while not incoming.endswith(b"\n"):
                    chunk = channel.recv(32769 - len(incoming))
                    if not chunk:
                        raise ValueError()
                    incoming.extend(chunk)
                    if len(incoming) > 32768:
                        raise ValueError()
                union = json.loads(incoming)
                if (type(union) is not list or len(union) > 64
                        or any(type(value) is not str or not value for value in union)
                        or sum(len(value.encode()) for value in union) > 16384
                        or not set(values) <= set(union)):
                    raise ValueError()
                from api_agent.llm_providers.bedrock.normalized_stream import guard_semantic
                guard_semantic(arguments, union)
                channel.sendall(b"1")
                if channel.recv(1) != b"1":
                    raise ValueError()
                return union
        if descriptor is None:
            execute(json.loads(raw), emit)
        else:
            execute(json.loads(raw), emit, join=join)
    except Exception:
        emit({'error': 'protocol'})


if __name__ == '__main__':
    # Source launches do not install api-agent or export PYTHONPATH. Resolve
    # sibling provider/contract imports from this worker's own distribution.
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    main()
