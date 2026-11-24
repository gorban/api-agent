"""The shared S3 settings family replaces the Allure-only LocalStack names."""
import pytest

from api_agent.config import load_shared_schema, resolve_config

RENAMED = [('ALLURE_S3_USE_LOCALSTACK', 'S3_USE_LOCALSTACK', 'True'),
           ('ALLURE_LOCALSTACK_PORT', 'LOCALSTACK_PORT', '4567'),
           ('ALLURE_S3_ENDPOINT', 'S3_ENDPOINT', 'http://localhost:4566'),
           ('ALLURE_S3_REGION', 'S3_REGION', 'us-east-1')]


def resolve(mode, **environ):
    return resolve_config(load_shared_schema(), environ={'TUI_MODE': mode, **environ})


@pytest.mark.parametrize('source', ['environment', 'dotenv'])
@pytest.mark.parametrize(('old', 'new', 'value'), RENAMED)
def test_retired_allure_names_fail_loudly_naming_the_replacement(old, new, value, source):
    options = {'environ': {'TUI_MODE': 'unit'}, 'dotenv': {}}
    options['environ' if source == 'environment' else 'dotenv'][old] = value

    result = resolve_config(load_shared_schema(), **options)

    assert [error for error in result.errors if old in error] == [
        f'{old} was renamed to {new}; update your .env or environment.']


def test_a_retired_name_in_both_sources_is_reported_once():
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': 'unit', 'ALLURE_S3_REGION': 'eu-west-1'},
                            dotenv={'ALLURE_S3_REGION': 'eu-west-1'})

    assert [error for error in result.errors if 'ALLURE_S3_REGION' in error] == [
        'ALLURE_S3_REGION was renamed to S3_REGION; update your .env or environment.']


def test_a_retired_name_is_ignored_where_the_renamed_setting_does_not_apply():
    result = resolve('unit', COLLECT_ONLY='True', ALLURE_S3_REGION='eu-west-1')

    assert not [error for error in result.errors if 'renamed' in error]


def test_retired_names_do_not_block_help():
    result = resolve_config(load_shared_schema(), argv=['-h'],
                            environ={'ALLURE_S3_REGION': 'eu-west-1'})

    assert result.help_requested and not result.errors


@pytest.mark.parametrize('mode', ['run-local', 'unit'])
def test_run_local_and_unit_default_to_the_shared_localstack(mode):
    result = resolve(mode)

    assert not result.errors
    assert result.values['S3_USE_LOCALSTACK'] is True
    assert result.values['LOCALSTACK_PORT'] == 4566
    assert result.values['S3_ENDPOINT'] == 'http://localhost:4566'
    assert result.values['S3_REGION'] == 'us-east-1'
    assert result.values['S3_EXTRA_BUCKETS'] == 'application-data'
    assert 'S3_USE_LOCALSTACK' not in result.missing


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_recording_modes_still_require_an_explicit_choice(mode):
    result = resolve(mode)

    assert 'S3_USE_LOCALSTACK' in result.missing
    assert 'S3_USE_LOCALSTACK' not in result.values


def test_opting_out_removes_local_only_settings_and_keeps_the_endpoint_verbatim():
    result = resolve('run-local', S3_USE_LOCALSTACK='False', S3_ENDPOINT='https://s3.example')

    assert not result.errors
    assert result.values['S3_ENDPOINT'] == 'https://s3.example'
    assert 'LOCALSTACK_PORT' not in result.values
    assert 'S3_EXTRA_BUCKETS' not in result.values


def test_allure_bucket_and_prefix_remain_allure_specific():
    result = resolve('integration', S3_USE_LOCALSTACK='True')

    assert result.values['ALLURE_S3_BUCKET'] == 'allure-results'
    assert result.values['ALLURE_S3_PREFIX'] == 'allure'
    assert 'ALLURE_S3_BUCKET' not in resolve('run-local').values
