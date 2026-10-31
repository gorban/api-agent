"""Selected-identity AWS Runtime clients and bounded catalog projection."""
import time


def openai_client(region, prefix, options, http):
    from openai import OpenAI
    from openai.providers.bedrock import bedrock
    return OpenAI(provider=bedrock(endpoint='runtime', region=region,
                  base_url=f'https://bedrock-runtime.{region}.amazonaws.com{prefix}', **options),
                  http_client=http, max_retries=0, timeout=http.timeout)


def boto_client(region, service, options, timeout):
    import boto3
    import botocore.session
    from botocore.config import Config
    from botocore.tokens import FrozenAuthToken
    session = botocore.session.Session()
    key = options.get('api_key')
    # These per-client resolvers cannot consult environment/profile credentials
    # or select an ambient bearer token in preference to the admitted identity.
    session.get_auth_token = lambda **kwargs: FrozenAuthToken(key, None) if key else None
    session.get_credentials = lambda: None
    config = Config(signature_version='bearer' if key else 'v4',
                    connect_timeout=min(10, timeout), read_timeout=timeout,
                    retries={'total_max_attempts': 1}, proxies={},
                    ignore_configured_endpoint_urls=True)
    arguments = {} if key else {
        'aws_access_key_id': options['access_key_id'],
        'aws_secret_access_key': options['secret_access_key'],
        'aws_session_token': options.get('session_token')}
    return boto3.Session(botocore_session=session).client(service, region_name=region,
        config=config, **arguments)


def catalog(client, deadline):
    """Return identifiers only; retain exact profile IDs and bound pagination."""
    from .worker import MAX_FRAME, json_value
    data, seen = [], set()
    for method, field, identifier in (
            ('list_foundation_models', 'modelSummaries', 'modelId'),
            ('list_inference_profiles', 'inferenceProfileSummaries', 'inferenceProfileId')):
        token = None
        tokens = set()
        for _ in range(1024):
            if time.monotonic() >= deadline:
                raise TimeoutError('Bedrock catalog deadline expired')
            page = getattr(client, method)(**({'nextToken': token} if token else {}))
            if type(page) is not dict or type(page.get(field)) is not list:
                raise ValueError('Invalid Bedrock catalog')
            for row in page[field]:
                value = row.get(identifier) if type(row) is dict else None
                if type(value) is not str or not value.strip() or len(value) > 2048:
                    raise ValueError('Invalid Bedrock model identifier')
                if value not in seen:
                    seen.add(value)
                    data.append({'id': value})
                    if len(data) > 1024:
                        raise ValueError('Bedrock catalog exceeds limit')
            # Enforce the process frame bound while pages are accumulated.
            json_value({'data': data})
            token = page.get('nextToken')
            if token is None:
                break
            if type(token) is not str or not token or token in tokens or len(token) > MAX_FRAME:
                raise ValueError('Invalid Bedrock catalog continuation')
            tokens.add(token)
        else:
            raise ValueError('Bedrock catalog pagination exceeds limit')
    return {'data': data}


def converse_schema(value, selected):
    """Admit only the documented strict JSON text-format request union."""
    import re
    from api_agent.providers.rich_inference import schema
    if ('structured' not in selected.features or type(value) is not dict
            or set(value) != {'textFormat'}):
        raise ValueError('Invalid Converse output configuration')
    text = value['textFormat']
    if (type(text) is not dict or set(text) != {'type', 'structure'}
            or text['type'] != 'json_schema' or type(text['structure']) is not dict
            or set(text['structure']) != {'jsonSchema'}):
        raise ValueError('Invalid Converse text format')
    definition = text['structure']['jsonSchema']
    if (type(definition) is not dict or set(definition) != {'name', 'schema'}
            or type(definition['name']) is not str
            or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', definition['name']) is None
            or type(definition['schema']) is not str):
        raise ValueError('Invalid Converse JSON schema')
    schema(definition['schema'], True)


def transient_network_failure(error):
    """Recognize native transport failures while excluding TLS and local setup."""
    import ssl
    try:
        from botocore import exceptions as errors
    except ImportError:
        return False
    transient = (errors.EndpointConnectionError, errors.ReadTimeoutError,
                 errors.ConnectTimeoutError, errors.ConnectionClosedError)
    excluded = (ssl.SSLError, errors.SSLError, errors.NoCredentialsError,
                errors.PartialCredentialsError, errors.NoRegionError,
                errors.InvalidConfigError, errors.ConfigNotFound,
                errors.ProfileNotFound, errors.CredentialRetrievalError,
                errors.TokenRetrievalError, errors.NoAuthTokenError)
    pending, seen, found = [error], set(), False
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        if len(seen) == 16:
            return False
        seen.add(id(current))
        if isinstance(current, excluded):
            return False
        found |= isinstance(current, transient)
        pending.append(current.__cause__)
        if not current.__suppress_context__:
            pending.append(current.__context__)
        kwargs = getattr(current, 'kwargs', None)
        if type(kwargs) is dict:
            pending.append(kwargs.get('error'))
    return found


def validate_aws_error(value, secrets):
    """Admit a bounded diagnostic carrier against the call's frozen authority."""
    import re
    from api_agent.llm_providers.bedrock.normalized_stream import guard_semantic
    from api_agent.providers.contracts import ContractError
    if (type(value) is not dict or set(value) not in (
            {'message'}, {'message', 'request_id'},
            {'code', 'message'}, {'code', 'message', 'request_id'})
            or 'code' in value and (type(value['code']) is not str
                or re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,99}', value['code']) is None)):
        raise ContractError('Invalid Bedrock AWS diagnostic')
    for key, limit in (('message', 512), ('request_id', 100)):
        if key in value and (type(value[key]) is not str or not value[key]
                or len(value[key]) > limit or not value[key].isprintable()):
            raise ContractError('Invalid Bedrock AWS diagnostic')
    guard_semantic(value, secrets)
    fields = [value['message']]
    if 'code' in value:
        fields.append(value['code'])
    if 'request_id' in value:
        fields.append(value['request_id'])
    import itertools
    import json
    guard_semantic(json.dumps(value, ensure_ascii=False, allow_nan=False), secrets)
    for ordering in itertools.permutations(fields):
        guard_semantic(list(ordering), secrets)
    return value


def aws_error(response, secrets):
    """Project only named service diagnostics; never SDK exception text."""
    if type(response) is not dict or type(response.get('Error')) is not dict:
        raise ValueError('Missing Bedrock AWS error')
    error = response['Error']
    value = {'message': error.get('Message')}
    if error.get('Code') is not None:
        value['code'] = error['Code']
    metadata = response.get('ResponseMetadata')
    if type(metadata) is dict and 'RequestId' in metadata:
        value['request_id'] = metadata['RequestId']
    return validate_aws_error(value, secrets)


def openai_error(body, request_id, secrets):
    """Project wrapped or SDK-unwrapped OpenAI-compatible service errors."""
    if type(body) is not dict:
        raise ValueError('Missing Bedrock structured SDK error')
    detail = body.get('error', body)
    if type(detail) is not dict:
        raise ValueError('Invalid Bedrock structured SDK error')
    value = {'message': detail.get('message')}
    if detail.get('code') is not None:
        value['code'] = detail['code']
    if request_id is not None:
        value['request_id'] = request_id
    return validate_aws_error(value, secrets)
