"""Structured stdout keeps its schema until the complete authority projects it."""
import json

import pytest

from api_agent.providers.scrubbed_response import ResponseValidationError
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.diagnostics import CredentialScrubber
from api_agent.testing.structured_diagnostics import (
    profile_records, report_records, render_record, append_profile_records,
)


def profile(value='safe-model', source='shared_default'):
    return {'LLM_DEFAULT_MODEL': {'value': value, 'source': source}}


def decoded(text):
    line = next(line for line in text.splitlines() if line.startswith('Integration test profile: '))
    return json.loads(line.removeprefix('Integration test profile: '))


def test_harmless_escaped_stdout_no_longer_masks_the_whole_profile():
    record = {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': {'value': None,
        'source': 'inactive_bundled_test_control:shared_default'}}
    payload = json.dumps(record)
    prefix = 'other output: \\u0061\n'
    scrub = CredentialScrubber()
    assert scrub(prefix + 'Integration test profile: ' + payload).endswith('[REDACTED]')

    rendered = append_profile_records(scrub(prefix), [payload], scrub)

    assert rendered.startswith(prefix)
    assert decoded(rendered) == record


def test_only_credential_string_is_emptied_and_safe_native_siblings_survive():
    record = dict(profile('synthetic-secret'),
        WORKFLOW_MAX_PASSES={'value': 4, 'source': 'shared_default'},
        LLM_REASONING_TEST_TOKEN_REPORTING={'value': True, 'source': 'shared_default'},
        LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS={'value': None, 'source': 'shared_default'})
    scrub = CredentialScrubber(secret_values=('synthetic-secret',))

    result = decoded(append_profile_records('', [json.dumps(record)], scrub))

    assert result['LLM_DEFAULT_MODEL']['value'] == ''
    assert result['LLM_DEFAULT_MODEL']['source'] == 'shared_default'
    assert result['WORKFLOW_MAX_PASSES']['value'] == 4
    assert result['LLM_REASONING_TEST_TOKEN_REPORTING']['value'] is True
    assert result['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS']['value'] is None


def test_credential_member_name_is_omitted_without_losing_safe_members():
    record = dict(profile(), WORKFLOW_MAX_PASSES={'value': 4, 'source': 'shared_default'})
    scrub = CredentialScrubber(secret_values=('LLM_DEFAULT_MODEL',))
    result = decoded(append_profile_records('', [json.dumps(record)], scrub))
    assert set(result) == {'WORKFLOW_MAX_PASSES'}


def test_encoded_credential_string_is_redacted_through_the_shared_mechanism():
    record = profile('prefix \\u0073ynthetic-secret suffix')
    scrub = CredentialScrubber(secret_values=('synthetic-secret',))
    assert decoded(append_profile_records('', [json.dumps(record)], scrub))['LLM_DEFAULT_MODEL']['value'] == ''


@pytest.mark.parametrize('payload', [None, 'not-json', '{"x":1,"x":2}', 'NaN', '[]',
    json.dumps(profile(source='')), json.dumps({'API_KEY': {'value': 'unknown-secret', 'source': 'shared'}})])
def test_unusable_record_is_explicitly_withheld_without_echoing_its_input(payload):
    text = append_profile_records('', [payload], CredentialScrubber())
    assert text == 'Integration test profile: [withheld: structured diagnostic unavailable]\n'


def test_redacted_required_source_is_rejected_and_another_record_remains_readable():
    scrub = CredentialScrubber(secret_values=('protected-source',))
    text = append_profile_records('', [json.dumps(profile(source='protected-source')),
                                       json.dumps(profile())], scrub)
    assert '[withheld: structured diagnostic unavailable]' in text
    assert 'protected-source' not in text
    assert 'safe-model' in text


def test_late_registration_is_applied_when_rendering_raw_records_again():
    scrub = CredentialScrubber()
    raw = [json.dumps(profile('later-secret'))]
    assert decoded(append_profile_records('', raw, scrub))['LLM_DEFAULT_MODEL']['value'] == 'later-secret'
    scrub.register_secret('later-secret')
    assert decoded(append_profile_records('', raw, scrub))['LLM_DEFAULT_MODEL']['value'] == ''


def test_generic_record_enforces_required_strings_after_scrubbing():
    schema = {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string', 'minLength': 1}}}
    with pytest.raises(ResponseValidationError, match='credential scrubbing changed'):
        render_record('record', {'id': 'synthetic-secret'}, schema=schema,
            authority=EmissionBoundary(secrets=('synthetic-secret',)), operation='diagnostic')


def test_serialized_record_is_checked_for_credentials_spanning_label_and_data():
    with pytest.raises(ResponseValidationError, match='credential scrubbing changed'):
        render_record('record', {'x': 'safe'}, schema={}, operation='diagnostic',
            authority=EmissionBoundary(secrets=('record: {"x": "safe"}',)))


def test_combined_stdout_cannot_assemble_a_registered_credential_across_records():
    scrub = CredentialScrubber()
    scrub.boundary.register_secret('prefix\nIntegration test profile: {}')
    assert append_profile_records('prefix', ['{}'], scrub) == (
        'stdout withheld: structured diagnostic credential protection.\n')


@pytest.mark.parametrize('stdout', ['', 'already scrubbed\n'])
def test_no_records_does_not_change_ordinary_stdout(stdout):
    assert append_profile_records(stdout, [], CredentialScrubber()) == stdout


def test_stdout_without_a_trailing_newline_stays_separate_from_typed_records():
    text = append_profile_records('ordinary stdout', [json.dumps(profile())], CredentialScrubber())
    assert text.startswith('ordinary stdout\nIntegration test profile: ')


@pytest.mark.parametrize('properties', [None, {}, [('integration_test_profile', 'x')] * 1025])
def test_unbounded_or_invalid_property_container_reports_loss(properties):
    assert profile_records(properties) == [None]


def test_property_capture_deduplicates_and_never_renders_unrelated_objects():
    properties = [None, ('bad',), (object(), 'ignored'), ('other', object()),
        ('integration_test_profile', object()), ('integration_test_profile', 'x' * 65537),
        ('integration_test_profile', '{}'), ('integration_test_profile', '{}')]
    assert profile_records(properties) == [None, '{}']
    assert report_records([{'profile_evidence': ['{}']}, {'profile_evidence': ['{}']}]) == ['{}']
    assert report_records([{}]) == []


def test_record_count_overflow_retains_bounded_records_and_an_explicit_loss_marker():
    values = [str(index) for index in range(17)]
    assert profile_records([('integration_test_profile', value) for value in values]) == [*values[:16], None]


@pytest.mark.parametrize('records', [None, 'not-list', ['{}'] * 18])
def test_invalid_report_records_are_explicitly_unavailable(records):
    assert report_records([{'profile_evidence': records}]) == [None]
