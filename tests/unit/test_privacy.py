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


@pytest.mark.parametrize('key',[
    'hidden_reasoning','hiddenReasoning','Hidden-Reasoning','hidden.reasoning',
    'reasoning_content','internal_analysis','thinking_content','scratchpad','chain_of_thought',
    'continuation_state','continuationState','continuation.id',
    'encrypted_content','previous_response_id','conversation_state',
])
@pytest.mark.parametrize('serialized',[False,True])
def test_hidden_protocol_fields_are_excluded_even_with_content_opt_in(key,serialized):
    boundary = EmissionBoundary(ContentPolicy(True))
    hidden = 'excluded-hidden-sentinel'
    value = {key:hidden,'response_text':'visible answer','reasoning_tokens':7,'reasoning_effort':'high'}
    payload = {'nested':json.dumps(value) if serialized else value,'prompt':'visible question'}
    original = json.dumps(payload)
    exported = boundary.export(payload)
    assert hidden not in json.dumps(exported)
    assert exported['prompt']=='visible question'
    nested = json.loads(exported['nested']) if serialized else exported['nested']
    assert nested['response_text']=='visible answer'
    assert nested['reasoning_tokens']==7 and nested['reasoning_effort']=='high'
    assert json.dumps(payload)==original


@pytest.mark.parametrize('tag',[{'type':'reasoning'},{'type':'redacted_thinking'},
    {'type':'continuation'},{'channel':'analysis'},{'channel':'reasoning'}])
@pytest.mark.parametrize('carrier',['native','serialized','embedded','parts'])
def test_tagged_hidden_carriers_cannot_escape_through_serialized_or_split_json(tag,carrier):
    boundary = EmissionBoundary(ContentPolicy(True))
    hidden = {**tag,'text':'excluded-hidden-sentinel'}
    encoded = json.dumps(hidden)
    value = {'native':hidden,'serialized':encoded,'embedded':'prefix '+encoded+' suffix',
        'parts':[encoded[:len(encoded)//2],encoded[len(encoded)//2:]]}[carrier]
    result = boundary.export({'payload':value,'visible':{'type':'output_text','text':'reasoning is a visible word'}})
    assert 'excluded-hidden-sentinel' not in json.dumps(result)
    assert result['visible']=={'type':'output_text','text':'reasoning is a visible word'}
    assert boundary.export(hidden)=={}


@pytest.mark.parametrize('allow',[False,True])
def test_explicit_genai_reasoning_metadata_requires_typed_counts_and_requested_effort(allow):
    boundary = EmissionBoundary(ContentPolicy(allow))
    payload = {'reasoning_tokens':7,'gen_ai.usage.reasoning_tokens':8,
        'reasoning_effort':'high','gen_ai.request.reasoning_effort':'low',
        'hidden_reasoning':'excluded-hidden-sentinel'}
    result = boundary.export(payload)
    assert result=={key:value for key,value in payload.items() if key!='hidden_reasoning'}
    for key in ('gen_ai.usage.reasoning_tokens','gen_ai.request.reasoning_effort'):
        assert key not in boundary.export({key:'excluded-hidden-sentinel'})


def test_visible_analysis_explanation_fields_are_not_hidden_protocol_aliases():
    boundary = EmissionBoundary(ContentPolicy(True))
    visible = {'analysis':'visible user analysis','reasoning':'visible explanation',
        'thinking':'visible user text','response_text':'visible answer'}
    assert boundary.export(visible)==visible
    assert json.loads(boundary.export({'serialized':json.dumps(visible)})['serialized'])==visible
    assert boundary.export({'gen_ai.response.hidden_reasoning':'excluded-hidden-sentinel'})=={}


def test_embedded_reserved_metadata_still_obeys_typed_admission():
    boundary = EmissionBoundary(ContentPolicy(True))
    for key in ('gen_ai.request.reasoning_effort','gen_ai.usage.reasoning_tokens'):
        assert 'excluded-hidden-sentinel' not in repr(boundary.export(
            {'text':'prefix '+json.dumps({key:'excluded-hidden-sentinel'})+' suffix'}))


def test_unavailable_token_accounting_retains_null_when_content_is_permitted():
    payload = {'usage':{'input_tokens':None,'output_tokens':None,'reasoning_tokens':None}}
    assert EmissionBoundary(ContentPolicy(True)).export(payload)==payload
    assert EmissionBoundary().export({'reasoning_tokens':None})=={}


def test_visible_literal_continuation_and_fractional_accounting_keep_owner_semantics():
    boundary = EmissionBoundary(ContentPolicy(True),preserve_json_text=True)
    text = 'Normal text with {"continuation":"user content"}'
    payload = {'prompt':text,'category_costs_usd':{'input_tokens':.0000002,
        'output_tokens':.0000004,'total_tokens':.0000006}}
    assert boundary.export(payload)==payload
    assert boundary.export({'continuation_state':'excluded-sentinel','prompt':text})=={'prompt':text}


@pytest.mark.parametrize('effort',['off',None,'provider-specific-metadata'])
def test_plain_reasoning_effort_metadata_retains_its_owner_defined_meaning(effort):
    payload = {'reasoning_effort':effort}
    assert EmissionBoundary(ContentPolicy(True)).export(payload)==payload
    if effort=='off':
        assert EmissionBoundary().export(payload)==payload
    else:
        assert EmissionBoundary().export(payload)=={}


def test_explicit_genai_count_guards_do_not_exempt_other_credential_namespaces():
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({'gen_ai.usage.input_tokens':7,'gen_ai.usage.output_tokens':2,
        'gen_ai.usage.access_token':'credential-sentinel'})=={
            'gen_ai.usage.input_tokens':7,'gen_ai.usage.output_tokens':2}
    assert boundary.export({'gen_ai.usage.input_tokens':.1})=={}
    assert boundary.export({'gen_ai.request.reasoning_effort':None})=={'gen_ai.request.reasoning_effort':None}


@pytest.fixture(params=['native', 'nested', 'serialized'])
def generation_limit_carrier(request):
    if request.param == 'native':
        return lambda value: value
    if request.param == 'nested':
        return lambda value: {'provider_payload': value}
    return lambda value: {'serialized': json.dumps(value)}


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', [None, 0, 128, (1 << 53) - 1])
def test_native_generation_limit_retains_only_typed_bounded_counts(key, value, generation_limit_carrier):
    payload = generation_limit_carrier({key: value})
    assert EmissionBoundary(ContentPolicy(True)).export(payload) == payload


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', [True, False, 'synthetic-sensitive-value', 128.0, -1, 1 << 53])
def test_native_generation_limit_rejects_unsafe_values(key, value, generation_limit_carrier):
    payload = generation_limit_carrier({key: value})
    assert EmissionBoundary(ContentPolicy(True)).export(payload) == generation_limit_carrier({})


class GenerationLimitInt(int):
    pass


class GenerationLimitFloat(float):
    pass


class GenerationLimitString(str):
    pass


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', [GenerationLimitInt(128), GenerationLimitFloat(128), GenerationLimitString('128')])
@pytest.mark.parametrize('nested', [False, True])
def test_native_generation_limit_rejects_custom_scalar_types(key, value, nested):
    payload = {'provider_payload': {key: value}} if nested else {key: value}
    expected = {'provider_payload': {}} if nested else {}
    assert EmissionBoundary(ContentPolicy(True)).export(payload) == expected


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
@pytest.mark.parametrize('secret_kind', ['numeric', 'container'])
def test_native_generation_limit_never_exempts_registered_secrets(key, secret_kind, generation_limit_carrier):
    native = {key: 128}
    payload = generation_limit_carrier(native)
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export(payload) == payload
    boundary.register_secret('128' if secret_kind == 'numeric' else json.dumps(native))
    exported = boundary.export(payload)
    assert exported != payload
    assert '128' not in json.dumps(exported)


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_completion_tokens', 'max_tokens'])
def test_native_generation_limit_does_not_enable_content_disabled_metadata(key, generation_limit_carrier):
    payload = {**generation_limit_carrier({key: 128}), 'input_tokens': 2}
    assert EmissionBoundary().export(payload) == {'input_tokens': 2}
