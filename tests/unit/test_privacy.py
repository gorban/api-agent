import io
import json
import logging
from dataclasses import replace
from uuid import uuid4

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, OwnedLogFilter


MARKER = 'SYNTHETIC_CONTENT_914'
SECRET = 'SYNTHETIC_SECRET_625'


def config(value=True, source='environment', explicit=True, **kwargs):
    return ConfigResult({'LOG_CONTENT': value},
                        {'LOG_CONTENT': ResolvedValue(value, source, explicit)},
                        (), (), (), **kwargs)


@pytest.mark.parametrize('source', ['environment', 'cli', 'dotenv', '../base/.env', '../pack/.env', '../.env'])
def test_explicit_external_true(source):
    assert ContentPolicy.from_config(config(source=source)).allow_content is True


@pytest.mark.parametrize('result', [None, {}, ConfigResult({}, {}, (), (), ()),
    config(False), config('True'), config(1), config(source='default', explicit=False),
    config(source='pack default', explicit=False), config(source='interactive'),
    config(source='/tmp/.env'), config(source='../pack/config.toml'),
    config(source='../pack/.env.backup'), config(explicit=False),
    replace(config(), errors=('invalid',)), replace(config(), missing=('REQUIRED',)),
    replace(config(), values={'LOG_CONTENT': False})])
def test_fail_closed_provenance(result):
    assert ContentPolicy.from_config(result).allow_content is False


def test_disabled_boundary_omits_all_content_categories_without_mutating_input():
    identifier = str(uuid4())
    payload = {key: MARKER for key in ('prompt', 'history', 'ui_context', 'context',
        'tool_request', 'tool_result', 'response_text', 'html', 'reasoning', 'chunks',
        'retry', 'exception', 'message', 'provider', 'backend_name', 'status', 'event')}
    payload.update({'nested': {'prompt': MARKER}, 'serialized': json.dumps({'prompt': MARKER}),
                    'duration_ms': 3.5, 'input_tokens': 0, 'retry_count': 2,
                    'correlation_id': identifier, 'request_id': str(uuid4())})
    boundary = EmissionBoundary(assigned_ids=[identifier])
    assert boundary.export(payload) == {'duration_ms': 3.5, 'input_tokens': 0,
        'retry_count': 2, 'correlation_id': identifier}
    assert payload['prompt'] == MARKER
    assert MARKER not in json.dumps(boundary.export(payload))


@pytest.mark.parametrize('payload, expected', [
    ({'status': 'success', 'event': 'flush'}, {'status': 'success', 'event': 'flush'}),
    ({'error_kind': 'timeout'}, {'error_kind': 'timeout'}),
    ({'status': 1, 'event': {}, 'error_kind': MARKER}, {}),
    ({'duration_ms': True, 'duration_seconds': -1}, {}),
    ({'duration_ms': float('nan'), 'duration_seconds': float('inf')}, {}),
    ({'duration_seconds': 0}, {'duration_seconds': 0}),
    ({'token_count': True, 'retry_count': -1, 'http_status': 99}, {}),
    ({'http_status': 200}, {'http_status': 200}),
    ({'token_count': 10, 'total_tokens': 5}, {'token_count': 10, 'total_tokens': 5}),
    ({'http_status': 600, 'request_id': 3}, {}),
    ({'request_id': MARKER, 'duration_ms': MARKER}, {}),
    ({3: MARKER, 'password': MARKER}, {}),
    ([MARKER], {}),
])
def test_metadata_is_typed_and_allowlisted(payload, expected):
    assert EmissionBoundary().export(payload) == expected


@pytest.mark.parametrize('key', ['password', 'Password', 'passwordValue', 'TOTP',
    'Authorization', 'cookie', 'set-cookie', 'id_token', 'access_token', 'refreshToken',
    'client_secret', 'apiKey', 'signingSecret', 'EXTENSION_SECRET', 'credentials', 'UNATTENDED_AUTH', 'tokens', 'jwt'])
def test_credentials_always_excluded(key):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=[SECRET])
    payload = {'prompt': MARKER, 'nested': {key: SECRET, 'response': MARKER + SECRET},
               key: SECRET, 'response': SECRET, SECRET: 'value'}
    result = boundary.export(payload)
    assert result['prompt'] == MARKER
    assert result['nested'] == {'response': MARKER + '[REDACTED]'}
    assert result['response'] == '[REDACTED]'
    assert SECRET not in json.dumps(result)
    assert payload[key] == SECRET


def test_serialized_json_streams_retries_and_nested_values_scrub_secrets():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=[SECRET, SECRET + '_long'])
    payload = {'chunks': [{'text': MARKER + SECRET + '_long'}, (MARKER, SECRET)],
               'retry': {'error': ValueError(MARKER)},
               'json': json.dumps({'password': SECRET, 'prompt': MARKER, 'text': SECRET}),
               'plain': 'null', 'finite': 1.5, 'nan': float('nan'), 'null': None,
               'flag': True, 'count': 1}
    result = boundary.export(payload)
    assert result['chunks'] == [{'text': MARKER + '[REDACTED]'}, [MARKER, '[REDACTED]']]
    assert result['retry'] == {}
    assert json.loads(result['json']) == {'prompt': MARKER, 'text': '[REDACTED]'}
    assert result['plain'] == 'null'
    assert result['finite'] == 1.5 and 'nan' not in result
    assert result['null'] is None and result['flag'] is True and result['count'] == 1


class Hostile:
    def __str__(self):
        raise AssertionError('must not stringify')

    def __repr__(self):
        raise AssertionError('must not represent')


def test_unknown_objects_cycles_and_depth_fail_closed():
    boundary = EmissionBoundary(ContentPolicy(True))
    cyclic = []
    cyclic.append(cyclic)
    depth = MARKER
    for _ in range(23):
        depth = {'child': depth}
    result = boundary.export({'object': Hostile(), 'cycle': cyclic, 'depth': depth,
                              'sequence': [Hostile(), 'safe'], 'nested': {1: MARKER}})
    assert 'object' not in result and result['cycle'] == []
    assert MARKER not in json.dumps(result)
    assert result['sequence'] == ['safe'] and result['nested'] == {}
    encoded = MARKER
    for _ in range(23):
        encoded = json.dumps({'child': encoded})
    assert MARKER not in json.dumps(boundary.export({'encoded': encoded}))


@pytest.mark.parametrize('error, kind', [(TimeoutError(MARKER), 'timeout'),
    (ValueError(MARKER), 'invalid_value'), (OSError(MARKER), 'io_error'),
    (RuntimeError(MARKER), 'error'), (Hostile(), 'error')])
def test_safe_exception_classification(error, kind):
    assert EmissionBoundary.exception(error) == {'status': 'error', 'error_kind': kind}
    assert EmissionBoundary().export(EmissionBoundary.exception(error)) == {
        'status': 'error', 'error_kind': kind}


def test_registration_validation_and_explicit_assignment():
    boundary = EmissionBoundary()
    for value in ('', None, 1):
        with pytest.raises(ValueError):
            boundary.register_secret(value)
    for value in (None, '', 'bad', str(uuid4()).upper()):
        with pytest.raises(ValueError):
            boundary.register_id(value)
    identifier = str(uuid4())
    assert boundary.export({'run_id': identifier}) == {}
    boundary.register_id(identifier)
    boundary.register_secret(identifier)
    assert boundary.export({'run_id': identifier}) == {'run_id': '[REDACTED]'}


def test_numeric_secret_values_are_never_serialized_as_content():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['123456', '123.5'])
    assert boundary.export({'context': [123456, 123.5, 1], 'token_count': 3}) == {
        'context': [1], 'token_count': 3}
    boundary = EmissionBoundary(secrets=['123456', 'ok'])
    assert boundary.export({'input_tokens': 123456, 'status': 'ok'}) == {
        'status': '[REDACTED]'}


@pytest.mark.parametrize('secret', ['quote"secret', 'slash\\secret', 'line\nsecret'])
def test_escaped_json_scalar_strings_scrub_secrets(secret):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=[secret])
    value = json.dumps('prefix' + secret + 'suffix')
    result = boundary.export({'response_text': value})
    assert json.loads(result['response_text']) == 'prefix[REDACTED]suffix'
    nested = json.dumps({'response_text': value, 'password': secret})
    output = boundary.export({'tool_result': nested})
    decoded = json.loads(output['tool_result'])
    assert json.loads(decoded['response_text']) == 'prefix[REDACTED]suffix'
    assert 'password' not in decoded
    assert secret not in json.dumps(output)
    assert json.dumps(secret) not in json.dumps(output)
    double_encoded = json.dumps(value)
    double_clean = boundary.export({'stream_chunk': double_encoded})['stream_chunk']
    assert json.loads(json.loads(double_clean)) == 'prefix[REDACTED]suffix'


@pytest.mark.parametrize('allow', [False, True])
def test_giant_numbers_fail_closed_without_breaking_export_or_logging(allow):
    boundary = EmissionBoundary(ContentPolicy(allow))
    payload = {'duration_ms': 10 ** 1000, 'duration_seconds': -(10 ** 1000),
               'input_tokens': 10 ** 5000, 'output_tokens': -(10 ** 5000),
               'total_tokens': 9007199254740991, 'status': 'ok'}
    expected = {'total_tokens': 9007199254740991, 'status': 'ok'}
    assert boundary.export(payload) == expected
    record = logging.LogRecord('owned', logging.INFO, '', 1, payload, (), None)
    OwnedLogFilter(boundary).filter(record)
    assert json.loads(record.msg) == expected
    assert json.dumps(boundary.export(payload))


def test_serialized_unsafe_numbers_and_safe_scalars():
    boundary = EmissionBoundary(ContentPolicy(True))
    result = boundary.export({'huge': '1' + '0' * 1000, 'nan': 'NaN',
        'number': '3', 'fraction': '1.5', 'negative': -9007199254740991,
        'null': 'null', 'bool': 'true'})
    assert result == {'number': '3', 'fraction': '1.5',
        'negative': -9007199254740991, 'null': 'null', 'bool': 'true'}


@pytest.mark.parametrize('value', ['1234567', '123456.0', '-1234567', '123456e10'])
def test_numeric_secret_substrings_in_text_and_encoded_text(value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['123456'])
    redacted = value.replace('123456', '[REDACTED]')
    assert boundary.export({'response_text': value}) == {'response_text': redacted}
    encoded = json.dumps(value)
    clean = boundary.export({'response_text': encoded})['response_text']
    assert json.loads(clean) == redacted
    double_encoded = json.dumps(encoded)
    clean = boundary.export({'response_text': double_encoded})['response_text']
    assert json.loads(json.loads(clean)) == redacted
    nested = json.dumps({'text': encoded})
    clean = boundary.export({'response_text': nested})['response_text']
    assert json.loads(json.loads(clean)['text']) == redacted


def test_raw_secret_redaction_does_not_obscure_credential_keys():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['password'])
    serialized = json.dumps({'password': 'PRIVATE_VALUE', 'text': 'password123'})
    clean = boundary.export({'tool_result': serialized})['tool_result']
    assert json.loads(clean) == {'text': '[REDACTED]123'}


@pytest.mark.parametrize('secret', ['["secret"]', '"secret"', '{"a":1}'])
def test_json_shaped_registered_secrets_are_redacted_in_all_encodings(secret):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=[secret])
    assert boundary.export({'message': secret}) == {'message': '[REDACTED]'}
    assert json.loads(boundary.export({'message': json.dumps(secret)})['message']) == '[REDACTED]'
    parsed = json.loads(secret)
    container = json.dumps({'value': parsed, 'password': 'PRIVATE_CREDENTIAL'})
    assert boundary.export({'message': container}) == {'message': '[REDACTED]'}
    compact = json.dumps({'value': parsed}, separators=(',', ':'))
    assert boundary.export({'message': compact}) == {'message': '[REDACTED]'}


def test_substring_spanning_serialized_json_syntax_fails_closed():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['": "secret'])
    payload = json.dumps({'value': 'secret', 'password': 'PRIVATE_CREDENTIAL'})
    assert boundary.export({'message': payload}) == {'message': '[REDACTED]'}
    assert boundary.export({'message': json.dumps({'value': 'ordinary'})}) == {
        'message': json.dumps({'value': 'ordinary'})}
    compact_boundary = EmissionBoundary(ContentPolicy(True), secrets=['":"secret'])
    compact_payload = json.dumps({'value': 'secret'}, separators=(',', ':'))
    assert compact_boundary.export({'message': compact_payload}) == {'message': '[REDACTED]'}
    encoded_boundary = EmissionBoundary(ContentPolicy(True), secrets=['"secret"'])
    assert encoded_boundary.export({'message': '"\\u0073ecret"'}) == {'message': '[REDACTED]'}


def test_excessive_valid_json_depth_is_omitted_before_logging():
    encoded = '[' * 10000 + '"SECRET_MARKER"' + ']' * 10000
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['SECRET_MARKER'])
    assert boundary.export({'tool_result': encoded, 'status': 'ok'}) == {'status': 'ok'}
    record = logging.LogRecord('owned', logging.INFO, '', 1,
                               {'tool_result': encoded, 'status': 'ok'}, (), None)
    OwnedLogFilter(boundary).filter(record)
    assert json.loads(record.msg) == {'status': 'ok'}
    assert 'SECRET_MARKER' not in record.msg and '[REDACTED]' not in record.msg
    boundary.register_secret(encoded)
    assert boundary.export({'json': '["ordinary"]'}) == {'json': '["ordinary"]'}


@pytest.mark.parametrize('allow', [False, True])
def test_owned_logging_boundary_failure_stream_and_flush(allow):
    boundary = EmissionBoundary(ContentPolicy(allow), secrets=[SECRET])
    capture = io.StringIO()
    handler = logging.StreamHandler(capture)
    handler.addFilter(OwnedLogFilter(boundary))
    logger = logging.Logger('private-owned')
    logger.addHandler(handler)
    logger.info({'chunks': [MARKER + SECRET], 'status': 'running'})
    logger.warning(MARKER + SECRET + ' %s', Hostile())
    try:
        raise ValueError(MARKER + SECRET)
    except ValueError:
        logger.exception(MARKER, extra={'prompt': MARKER}, stack_info=True)
    logger.info({'event': 'flush', 'duration_ms': 1})
    handler.flush()
    output = capture.getvalue()
    assert SECRET not in output and 'Traceback' not in output
    assert (MARKER in output) is allow
    assert 'running' in output and 'flush' in output and 'invalid_value' in output


def test_log_filter_clears_arbitrary_record_attributes():
    record = logging.LogRecord(MARKER, 999, MARKER, 2, Hostile(), (), None)
    record.private = MARKER
    record.stack_info = MARKER
    assert OwnedLogFilter(EmissionBoundary()).filter(record) is True
    assert record.levelno == logging.INFO and record.msg == '{}'
    assert MARKER not in json.dumps(record.__dict__)
