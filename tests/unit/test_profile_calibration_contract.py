"""Calibration binds selected test controls and actual transport deadlines."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_workflow_fingerprint_changes_when_setup_budget_changes():
    from api_agent.workflows.fixture_driver import fingerprint
    values = {
        'LLM_PROVIDER':str(ROOT/'src/api_agent/llm_providers/bedrock/factory.py')+':factory',
        'BEDROCK_API_MODE':'completions','BEDROCK_AUTH_MODE':'session',
        'BEDROCK_REGION':'us-east-1','LLM_WORKFLOW_TEST_MODEL':'zai.glm-5',
        'WORKFLOW_LLM_TIMEOUT_SECONDS':120,'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':960,
    }
    original = fingerprint('zai.glm-5', values, root=ROOT, owner_root=ROOT)
    changed = fingerprint('zai.glm-5', dict(values, LLM_SETUP_REQUEST_TIMEOUT_SECONDS=60), root=ROOT, owner_root=ROOT)
    assert original != changed, 'A shorter enforced worker setup/inference transport cap changes sampling'


@pytest.mark.parametrize('choice', ['', 'openai.gpt-oss-20b'])
def test_calibration_preserves_explicit_workflow_choice(monkeypatch, choice):
    from api_agent.workflows import calibrate
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr(calibrate.os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    provider = str(ROOT/'src/api_agent/llm_providers/bedrock/factory.py')+':factory'
    _, result = calibrate.configuration(ROOT, ['--llm-provider='+provider, '--llm-workflow-test-model='+choice])
    assert result.values['LLM_WORKFLOW_TEST_MODEL'] == choice


def test_gpt_calibration_uses_same_exact_workflow_model_as_integration(monkeypatch):
    from api_agent.workflows import calibrate
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr(calibrate.os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    provider = str(ROOT/'src/api_agent/llm_providers/gpt/factory.py')+':factory'
    _, result = calibrate.configuration(ROOT, ['--llm-provider='+provider])
    assert result.values['LLM_WORKFLOW_TEST_MODEL'] == 'gpt-6-luna'


@pytest.fixture
def adapter_identity_values():
    return {
        'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
        'BEDROCK_API_MODE': 'completions', 'BEDROCK_AUTH_MODE': 'session',
        'BEDROCK_REGION': 'us-east-1', 'LLM_WORKFLOW_TEST_MODEL': 'zai.glm-5',
        'BEDROCK_PROFILE': 'chosen',
        'BEDROCK_SESSION_PROVIDER': 'src/api_agent/llm_providers/bedrock/aws_login.py:factory',
    }


def test_bundled_adapter_path_aliases_share_fingerprint_without_mutating_config(adapter_identity_values):
    from copy import deepcopy
    import os
    from api_agent.workflows.fixture_driver import fingerprint
    environment = dict(os.environ)
    original = deepcopy(adapter_identity_values)
    source = fingerprint('zai.glm-5', original, root=ROOT, owner_root=ROOT)
    package = ROOT / 'src/api_agent'
    installed_spelling = dict(original, BEDROCK_SESSION_PROVIDER='llm_providers/bedrock/aws_login.py:factory')
    alias = fingerprint('zai.glm-5', installed_spelling, root=ROOT, owner_root=package)
    absolute = fingerprint('zai.glm-5', dict(original,
        BEDROCK_SESSION_PROVIDER=str(package / 'llm_providers/bedrock/../bedrock/aws_login.py') + ':factory'), root=ROOT)
    assert source == alias == absolute
    assert adapter_identity_values == original
    assert dict(os.environ) == environment
    assert installed_spelling['BEDROCK_SESSION_PROVIDER'] == 'llm_providers/bedrock/aws_login.py:factory'


def test_relocated_package_adapter_preserves_source_fingerprint(adapter_identity_values, tmp_path, monkeypatch):
    import json
    from api_agent.workflows import fixture_driver as driver
    source = driver.fingerprint('zai.glm-5', adapter_identity_values, root=ROOT, owner_root=ROOT)
    package = ROOT / 'src/api_agent'
    installed = tmp_path / 'installed/api_agent'
    profile = json.loads((package / 'llm_providers/bedrock/calibration.json').read_text())
    for name in set(driver.SAMPLING_SOURCES) | set(profile['sources']):
        destination = installed / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((package / name).read_bytes())
    monkeypatch.setattr(driver, '__file__', str(installed / 'workflows/fixture_driver.py'))
    values = dict(adapter_identity_values,
        LLM_PROVIDER='llm_providers/bedrock/factory.py:factory',
        BEDROCK_SESSION_PROVIDER='llm_providers/bedrock/aws_login.py:factory')
    relocated = driver.fingerprint('zai.glm-5', values, root=ROOT, owner_root=installed)
    assert relocated == source


@pytest.mark.parametrize('setting,value', [
    ('BEDROCK_SESSION_PROVIDER', 'src/api_agent/llm_providers/bedrock/aws_login.py:other_factory'),
    ('BEDROCK_PROFILE', 'other'),
    ('BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID', '123456789012'),
    ('BEDROCK_LOGIN_EXPECTED_PRINCIPAL', 'arn:aws:iam::123456789012:user/example'),
])
def test_adapter_identity_changes_are_not_canonicalized_away(adapter_identity_values, setting, value):
    from api_agent.workflows.fixture_driver import fingerprint
    from api_agent.workflows.sampling import validate_calibration, DECLARED
    from test_workflow_fixture_driver import synthetic_current_report
    original = fingerprint('zai.glm-5', adapter_identity_values, root=ROOT, owner_root=ROOT)
    changed = fingerprint('zai.glm-5', dict(adapter_identity_values, **{setting: value}), root=ROOT, owner_root=ROOT)
    assert changed['backend']['configuration_sha256'] != original['backend']['configuration_sha256']
    assert changed['backend']['adapter_sources'] == original['backend']['adapter_sources']
    # The unchanged report remains valid for the original fingerprint only.
    report = synthetic_current_report()
    for key, data in original.items():
        report[key] = data
    assert validate_calibration(report, DECLARED, original) == report['max_runs']
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, changed)


@pytest.mark.parametrize('location_kind', ['copy', 'symlink', 'empty'])
def test_external_adapter_locations_keep_exact_spelling(adapter_identity_values, tmp_path, location_kind):
    from api_agent.workflows.fixture_driver import fingerprint
    adapter = ROOT / 'src/api_agent/llm_providers/bedrock/aws_login.py'
    external = tmp_path / 'external.py'
    external.write_bytes(adapter.read_bytes())
    if location_kind == 'symlink':
        link = tmp_path / 'alias.py'
        link.symlink_to(external)
        external = link
    spelling = '' if location_kind == 'empty' else str(external) + ':factory'
    values = dict(adapter_identity_values, BEDROCK_SESSION_PROVIDER=spelling)
    observed = fingerprint('zai.glm-5', values, root=ROOT, owner_root=ROOT)
    import hashlib
    import json
    profile = json.loads((ROOT / 'src/api_agent/llm_providers/bedrock/calibration.json').read_text())
    expected = {name: values.get(name) for name in profile['identity_settings']}
    assert observed['backend']['configuration_sha256'] == hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    if spelling:
        bundled = fingerprint('zai.glm-5', adapter_identity_values, root=ROOT, owner_root=ROOT)
        assert observed['backend']['adapter_sources'] == bundled['backend']['adapter_sources']
        assert observed['backend']['configuration_sha256'] != bundled['backend']['configuration_sha256']


@pytest.mark.parametrize('adapter_kind', ['symlink_out', 'nonmodule'])
def test_packaged_adapter_requires_physical_module_identity(adapter_identity_values, tmp_path, monkeypatch, adapter_kind):
    import hashlib
    import json
    from api_agent.workflows import fixture_driver as driver
    package = ROOT / 'src/api_agent'
    installed = tmp_path / 'installed/api_agent'
    profile = json.loads((package / 'llm_providers/bedrock/calibration.json').read_text())
    for name in set(driver.SAMPLING_SOURCES) | set(profile['sources']):
        destination = installed / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((package / name).read_bytes())
    if adapter_kind == 'symlink_out':
        external = tmp_path / 'external.py'
        external.write_text('raise AssertionError("Fingerprint must not import an adapter")\n')
        adapter = installed / 'llm_providers/bedrock/adapter_alias.py'
        adapter.symlink_to(external)
    else:
        adapter = installed / 'llm_providers/bedrock/not-a-module.py'
        adapter.write_text('raise AssertionError("Fingerprint must not import an adapter")\n')
    monkeypatch.setattr(driver, '__file__', str(installed / 'workflows/fixture_driver.py'))
    values = dict(adapter_identity_values,
        LLM_PROVIDER='llm_providers/bedrock/factory.py:factory',
        BEDROCK_SESSION_PROVIDER=str(adapter) + ':factory')
    observed = driver.fingerprint('zai.glm-5', values, root=ROOT, owner_root=installed)
    expected = {name: values.get(name) for name in profile['identity_settings']}
    assert observed['backend']['configuration_sha256'] == hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    assert observed['backend']['adapter_sources']['BEDROCK_SESSION_PROVIDER'] == hashlib.sha256(adapter.read_bytes()).hexdigest()


@pytest.fixture
def response_fingerprint_config():
    def values(provider):
        config = {'LLM_PROVIDER': str(ROOT / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory',
                  'LLM_WORKFLOW_TEST_MODEL': 'gpt-6-luna', 'GPT_API_MODE': 'responses'}
        if provider == 'bedrock':
            config.update(BEDROCK_API_MODE='completions', BEDROCK_AUTH_MODE='session',
                          BEDROCK_REGION='us-east-1', LLM_WORKFLOW_TEST_MODEL='zai.glm-5')
        return config
    return values


@pytest.mark.parametrize('provider,helper', [
    ('gpt', 'llm_providers/response_policy.py'),
    ('gpt', 'llm_providers/gpt/response_policy.py'),
    ('bedrock', 'llm_providers/response_policy.py'),
    ('bedrock', 'llm_providers/bedrock/response_policy.py'),
])
def test_response_helper_change_invalidates_its_provider_qualification(monkeypatch, response_fingerprint_config, provider, helper):
    from api_agent.workflows import fixture_driver as driver
    from api_agent.workflows.sampling import DECLARED, validate_calibration
    from test_workflow_fixture_driver import synthetic_current_report
    config = response_fingerprint_config(provider)
    selection = config['LLM_WORKFLOW_TEST_MODEL']
    before = driver.fingerprint(selection, config, root=ROOT, owner_root=ROOT)
    key = 'src/api_agent/' + helper
    assert key in before['sources']
    report = synthetic_current_report()
    report.update(before)
    validate_calibration(report, DECLARED, before)
    original_digest = driver.sampling_digest
    target = ROOT / key

    def changed_helper_digest(path):
        return '0' * 64 if path == target else original_digest(path)

    monkeypatch.setattr(driver, 'sampling_digest', changed_helper_digest)
    after = driver.fingerprint(selection, config, root=ROOT, owner_root=ROOT)

    assert after['sources'] == {**before['sources'], key: '0' * 64}
    assert {name: value for name, value in after.items() if name != 'sources'} == {
        name: value for name, value in before.items() if name != 'sources'}
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, after)


@pytest.mark.parametrize('provider,irrelevant', [('gpt', 'llm_providers/bedrock/response_policy.py'),
                                               ('bedrock', 'llm_providers/gpt/response_policy.py')])
def test_other_provider_response_helper_does_not_change_qualification(monkeypatch, response_fingerprint_config, provider, irrelevant):
    from api_agent.workflows import fixture_driver as driver
    config = response_fingerprint_config(provider)
    selection = config['LLM_WORKFLOW_TEST_MODEL']
    before = driver.fingerprint(selection, config, root=ROOT, owner_root=ROOT)
    key = 'src/api_agent/' + irrelevant
    assert key not in before['sources']
    original_digest = driver.sampling_digest
    monkeypatch.setattr(driver, 'sampling_digest', lambda path: '0' * 64 if path == ROOT / key else original_digest(path))
    assert driver.fingerprint(selection, config, root=ROOT, owner_root=ROOT) == before
