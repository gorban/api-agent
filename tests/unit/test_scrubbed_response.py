"""Response scrubbing preserves ordinary JSON and reports no rejected input."""
import asyncio
import logging

import pytest

from api_agent.providers.scrubbed_response import (
    ResponseValidationError, async_scrub_response, scrub_response,
)

SECRET = 'synthetic-credential'
SCHEMA = {'type': 'object', 'properties': {'id': {'type': 'string', 'minLength': 1}},
          'required': ['id'], 'additionalProperties': True}


def test_safe_fields_and_native_extra_types_survive():
    source = {'id': 'model-1', 'extra': ['123', 123, True, None, 1.5, {}]}
    clean, diagnostics = scrub_response(source, schema=SCHEMA)
    assert clean == source and clean is not source
    assert clean['extra'] is not source['extra']
    assert not diagnostics


def test_unused_secret_text_becomes_empty_without_losing_safe_catalog():
    clean, diagnostics = scrub_response({'id': 'model-1', 'description': 'prose '+SECRET},
        schema=SCHEMA, secrets={SECRET}, operation='model_discovery')
    assert clean == {'id': 'model-1', 'description': ''}
    assert diagnostics[0].path == (1,)
    assert diagnostics[0].action == 'empty_string'
    assert SECRET not in repr(diagnostics)


def test_required_identity_rejects_after_redaction():
    with pytest.raises(ResponseValidationError) as caught:
        scrub_response({'id': 'model-'+SECRET}, schema=SCHEMA, secrets={SECRET})
    assert caught.value.rule == 'minLength'
    assert caught.value.diagnostics[0].path == ('id',)
    assert SECRET not in str(caught.value)


def test_redacted_extra_names_use_sidecar_indices_without_collision():
    clean, diagnostics = scrub_response({'id': 'model-1', SECRET: 1,
        'other-'+SECRET: 2, '': 'ordinary', 'safe': 3}, schema=SCHEMA, secrets={SECRET})
    assert clean == {'id': 'model-1', '': 'ordinary', 'safe': 3}
    assert [row.path for row in diagnostics] == [(1,), (2,)]
    assert [member.value for member in diagnostics.anonymous_members] == [1, 2]
    assert 'value=' not in repr(diagnostics)


@pytest.mark.parametrize('raw', [b'{"id":"a","id":"b"}', b'{"id":NaN}', b'not json'])
def test_invalid_json_never_exposes_input(raw):
    with pytest.raises(ResponseValidationError, match='invalid_json'):
        scrub_response(raw, schema=SCHEMA)


def test_json_decoding_and_async_adapter():
    clean, diagnostics = asyncio.run(async_scrub_response(b'{"id":"model-1"}', schema=SCHEMA))
    assert clean == {'id': 'model-1'} and not diagnostics


def test_nested_diagnostics_and_logging_are_value_free(caplog):
    schema = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'description': {'type': 'string'}}}}}}
    with caplog.at_level(logging.DEBUG, logger='api_agent.scrubbing'):
        clean, diagnostics = scrub_response({'items': [{'description': SECRET}]},
            schema=schema, secrets={SECRET}, operation='catalog')
    assert clean == {'items': [{'description': ''}]}
    assert diagnostics[0].path == ('items', 0, 'description')
    assert 'catalog' in caplog.text and 'description' in caplog.text
    assert SECRET not in caplog.text

@pytest.mark.parametrize('value', [2**60, {'type': 'continuation', 'value': 1}, {'max_output_tokens': -1}])
def test_functional_values_are_not_filtered_by_evidence_policy(value):
    clean, diagnostics = scrub_response({'extra': value}, schema={'type': 'object'})
    assert clean == {'extra': value} and not diagnostics


@pytest.mark.parametrize('sibling', [42, {'safe': True}, 'ordinary'])
def test_partitioned_credentials_preserve_array_shape_and_safe_siblings(sibling):
    clean, diagnostics = scrub_response({'items': ['synthetic-', 'credential', sibling]},
        schema={'type': 'object'}, secrets={SECRET})
    assert clean == {'items': ['', '', sibling]}
    assert len(diagnostics) == 2


def test_invalid_optional_limit_is_not_silently_removed():
    schema = {'type': 'object', 'properties': {'max_output_tokens': {'type': 'integer', 'minimum': 1}}}
    with pytest.raises(ResponseValidationError, match='minimum'):
        scrub_response({'max_output_tokens': -1}, schema=schema)


def test_anonymous_members_over_budget_reject_instead_of_disappearing():
    source = {f'{SECRET}-{index}': index for index in range(101)}
    with pytest.raises(ResponseValidationError, match='anonymous_member_budget'):
        scrub_response(source, schema={}, secrets={SECRET})


def test_diagnostic_overflow_is_explicit_and_values_survive():
    clean, report = scrub_response([SECRET] * 101, schema={}, secrets={SECRET})
    assert clean == [''] * 101
    assert len(report) == 100
    assert report.omitted_diagnostics == 1


def test_common_decoder_rejects_duplicates_before_parsing_loses_them():
    from api_agent.providers.scrubbed_response import decode_json
    with pytest.raises(ResponseValidationError):
        decode_json('{"safe": 1, "safe": 2}')
    assert decode_json('{"safe": 1}') == {'safe': 1}


@pytest.mark.parametrize('max_depth', [-1, 129, True])
def test_invalid_depth_configuration_rejects(max_depth):
    with pytest.raises(ResponseValidationError):
        scrub_response({}, schema={}, max_depth=max_depth)


@pytest.mark.parametrize('value', [object(), float('nan'), {1: 'value'}])
def test_unsupported_native_values_fail_without_coercion(value):
    with pytest.raises(ResponseValidationError):
        scrub_response(value, schema={})


def test_contract_validator_runs_before_and_after_scrubbing():
    seen = []
    def expected(value):
        seen.append(value.copy())
        if not value['id']:
            raise ValueError('unsafe rejected value')
        return value['id']
    clean, report = scrub_response({'id': 'safe', 'unused': SECRET}, schema=expected, secrets={SECRET})
    assert clean == {'id': 'safe', 'unused': ''} and report
    assert seen == [{'id': 'safe', 'unused': SECRET}, {'id': 'safe', 'unused': ''}]
    with pytest.raises(ResponseValidationError, match='contract') as caught:
        scrub_response({'id': SECRET}, schema=expected, secrets={SECRET})
    assert caught.value.__context__ is None
    assert SECRET not in str(caught.value)


def test_operation_labels_do_not_leak_registered_credentials(caplog):
    with caplog.at_level(logging.DEBUG, logger='api_agent.scrubbing'):
        scrub_response(SECRET, schema={}, secrets={SECRET}, operation=SECRET)
    assert SECRET not in caplog.text
    assert 'operation=response' in caplog.text


def test_strict_mode_never_silently_changes_a_functional_payload():
    with pytest.raises(ResponseValidationError) as caught:
        scrub_response({'safe': SECRET}, schema={}, secrets={SECRET}, transform=False)
    assert caught.value.diagnostics
    source = {'safe': [True, None, 42]}
    clean, report = scrub_response(source, schema={}, transform=False)
    assert clean == source and not report


def test_additional_property_schema_applies_after_scrubbing():
    schema = {'type': 'object', 'additionalProperties': {'type': 'string', 'minLength': 1}}
    with pytest.raises(ResponseValidationError, match='minLength'):
        scrub_response({'unknown': SECRET}, schema=schema, secrets={SECRET})


def test_non_schema_additional_properties_and_items_preserve_normal_types():
    clean, report = scrub_response({'unknown': [True, None]}, schema={'additionalProperties': True})
    assert clean == {'unknown': [True, None]} and not report


def test_partitioned_bearer_carrier_preserves_non_text_siblings():
    clean, report = scrub_response(['Bearer ', 'example-token', 42], schema={})
    assert clean == ['', '', 42] and report


def test_sidecar_fragment_values_are_protected_together():
    clean, report = scrub_response({SECRET+'a': 'synthetic-', SECRET+'b': 'credential'},
        schema={}, secrets={SECRET})
    assert clean == {}
    assert [member.value for member in report.anonymous_members] == ['', '']


@pytest.mark.parametrize('transform', [True, False])
def test_validator_cannot_reintroduce_credentials(transform):
    def converter(value):
        return {'safe': value['safe'], 'extra': SECRET}
    clean, report = scrub_response({'safe': 'ordinary'}, schema=converter,
        secrets={SECRET}, transform=transform)
    assert clean == {'safe': 'ordinary'} and not report


@pytest.mark.parametrize('anonymous', [False, True])
def test_registered_match_does_not_hide_a_partitioned_bearer(anonymous):
    parts = [SECRET, 'Bearer ', 'unregistered-token', 42, '; ordinary']
    source = {SECRET: parts} if anonymous else parts
    clean, report = scrub_response(source, schema={}, secrets={SECRET})
    values = report.anonymous_members[0].value if anonymous else clean
    assert values == ['', '', '', 42, '; ordinary']


@pytest.mark.parametrize('transform', [True, False])
def test_validator_cannot_mutate_the_screened_document(transform):
    def mutate(value):
        value['note'] = SECRET
    source = {'safe': 'ordinary'}
    clean, report = scrub_response(source, schema=mutate, secrets={SECRET}, transform=transform)
    assert clean == source == {'safe': 'ordinary'} and not report


@pytest.mark.parametrize('raw', [None, 'x' * (8 * 1024 * 1024 + 1)])
def test_common_decoder_bounds_input_before_parsing(raw):
    from api_agent.providers.scrubbed_response import decode_json
    with pytest.raises(ResponseValidationError):
        decode_json(raw)


def test_transform_input_byte_budget_is_bounded():
    with pytest.raises(ResponseValidationError):
        scrub_response(b'x' * (8 * 1024 * 1024 + 1), schema={})


def test_strict_mode_reports_root_rejection_for_invalid_native_content():
    with pytest.raises(ResponseValidationError) as caught:
        scrub_response(object(), schema=None, transform=False)
    assert caught.value.diagnostics[0].action == 'reject'


def test_schema_free_mode_still_preserves_native_values():
    clean, report = scrub_response([1, True, None], schema=None)
    assert clean == [1, True, None] and not report


def test_revealed_carriers_have_a_fixed_normalization_budget():
    with pytest.raises(ResponseValidationError, match='carrier'):
        scrub_response(['c', 'a', 'xx', 'b', 'd'], schema={}, secrets={'xx', 'ab', 'cd'})


def test_registered_structured_credential_cannot_be_released_as_native_fields():
    with pytest.raises(ResponseValidationError):
        scrub_response({'a': 1, 'b': 2}, schema={}, secrets={'{"a":1,"b":2}'})


@pytest.mark.parametrize('level, expected', [('off', 51), ('warning', 30), ('info', 20), ('debug', 10)])
def test_configurable_diagnostics_are_bounded_and_value_free(monkeypatch, level, expected):
    from api_agent.providers import scrubbed_response as module
    monkeypatch.setattr(module._LOG, 'handlers', [])
    monkeypatch.setattr(module._LOG, 'level', module._LOG.level)
    module.configure_diagnostics(level)
    assert module._LOG.level == expected
    handler, = module._LOG.handlers
    module.configure_diagnostics(level)
    assert module._LOG.handlers == [handler]
    handler.close()


def test_invalid_diagnostics_level_rejects():
    from api_agent.providers.scrubbed_response import configure_diagnostics
    with pytest.raises(ValueError, match='Invalid scrub diagnostics level'):
        configure_diagnostics('include-values')



def test_common_decoder_accepts_owned_http_byte_buffers():
    from api_agent.providers.scrubbed_response import decode_json
    buffer = bytearray(b'{"value": true}')
    assert decode_json(buffer) == {'value': True}
    assert buffer == bytearray(b'{"value": true}')



def test_single_use_credential_snapshot_is_frozen_before_multiple_checks():
    source = {'a': 1, 'b': 2}
    with pytest.raises(ResponseValidationError):
        scrub_response(source, schema={}, secrets=iter(['{"a":1,"b":2}']))
