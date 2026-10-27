"""Coverage headroom changes a declared budget, never the statistical target."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.workflows import sampling
from api_agent.workflows.evidence import read_calibration, reserve_calibration, sanitize, store_calibration
from api_agent.workflows.sampling import (
    DECLARED, FIRST, TARGETS, calibration, cover, coverage_ceiling, measured_calibration,
    measurement, observed_targets, validate_calibration,
)
from test_workflow_sampling import records, structural_events
from test_test_profiles import CASES, ROOT, configuration


def sparse_retry_records():
    """Sixty valid trials; the retry target needs more than 60 normal graphs."""
    rows = []
    for trial in range(1, 61):
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        nodes = FIRST if route == 'skip' else DECLARED
        events = structural_events(trial, route, retry=trial <= 12)
        rows.append(measurement(trial, {'first_draft': {'route': route}}, nodes, events))
    return rows


def test_default_sixty_and_declared_one_twenty_keep_identical_statistics():
    ordinary = measured_calibration(records(), DECLARED)
    expanded = measured_calibration(records(), DECLARED, ceiling=120)
    assert ordinary['operating_ceiling'] == 60 and expanded['operating_ceiling'] == 120
    assert {key: value for key, value in ordinary.items() if key != 'operating_ceiling'} == {
        key: value for key, value in expanded.items() if key != 'operating_ceiling'}
    assert expanded['confidence'] == .95 and expanded['target'] == .999
    assert expanded['safety_margin'] == 4
    assert set(expanded['hits']) == TARGETS


def test_larger_declared_ceiling_qualifies_without_relabeling_failed_sixty_budget():
    rows = sparse_retry_records()
    with pytest.raises(ValueError, match='within the operating ceiling'):
        measured_calibration(rows, DECLARED)
    report = measured_calibration(rows, DECLARED, ceiling=120)
    assert report['trials'] == 60 and len(report['records']) == 60
    assert report['minimum_runs'] == 76 and report['max_runs'] == 80
    assert report['max_runs'] == report['minimum_runs'] + 4 <= report['operating_ceiling']
    assert validate_calibration(report, DECLARED, {'operating_ceiling': 120}, ceiling=120) == 80
    assert report['minimum_missing_node_bound'] <= .001
    assert sum((1 - bound) ** 75 for bound in report['bounds'].values()) > .001


@pytest.mark.parametrize('value', [0, 121, -1, True, False, 60.0, 120.0, '120', None])
def test_unsupported_ceilings_reject_without_clamping(value):
    with pytest.raises(ValueError, match='integer from 1 to 120'):
        coverage_ceiling(value)
    with pytest.raises(ValueError, match='integer from 1 to 120'):
        calibration([{'node'}], {'node'}, ceiling=value)


@pytest.mark.parametrize('value', [1, 60, 120])
def test_supported_ceiling_boundaries_are_exact(value):
    assert coverage_ceiling(value) == value


def test_pilot_observation_count_stays_sixty_at_larger_coverage_ceiling():
    with pytest.raises(ValueError, match='up to 60 observations'):
        calibration([{'node'}] * 61, {'node'}, ceiling=120)
    with pytest.raises(ValueError, match='Invalid pilot measurements'):
        measured_calibration(records() + [records()[0]], DECLARED, ceiling=120)


def test_saved_ceiling_cannot_raise_default_validation_budget():
    valid = measured_calibration(records(), DECLARED)
    assert validate_calibration(valid, DECLARED, {}) == valid['max_runs']
    forged = dict(valid, operating_ceiling=120)
    # Every measurement and all arithmetic remain valid under both ceilings.
    # Only an untrusted report-supplied budget differs from the selected default.
    with pytest.raises(ValueError, match='claims do not match'):
        validate_calibration(forged, DECLARED, {})


@pytest.mark.parametrize('value', [None, True, 60.0, 121, 60])
def test_mutated_report_ceiling_cannot_change_current_configuration(value):
    report = measured_calibration(sparse_retry_records(), DECLARED, ceiling=120)
    report['operating_ceiling'] = value
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, {'operating_ceiling': 120}, ceiling=120)


def test_missing_legacy_ceiling_is_readable_only_under_default_sixty():
    legacy = measured_calibration(records(), DECLARED)
    legacy.pop('operating_ceiling')
    assert validate_calibration(legacy, DECLARED, {}) == legacy['max_runs']
    with pytest.raises(ValueError, match='claims do not match'):
        validate_calibration(legacy, DECLARED, {}, ceiling=120)
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(legacy, DECLARED, {'operating_ceiling': 60})


def test_current_fingerprint_cannot_replace_report_ceiling_or_operator_budget():
    report = measured_calibration(sparse_retry_records(), DECLARED, ceiling=120)
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, {'operating_ceiling': 60}, ceiling=60)
    with pytest.raises(ValueError, match='within the operating ceiling'):
        validate_calibration(report, DECLARED, {'operating_ceiling': 120})


def test_signed_report_requires_explicit_trusted_budget_at_storage_and_validation(tmp_path):
    report = measured_calibration(sparse_retry_records(), DECLARED, ceiling=120)
    failed = tmp_path / 'not-authorized.json'
    with reserve_calibration(failed):
        with pytest.raises(ValueError, match='within the operating ceiling'):
            store_calibration(failed, report)
    assert not failed.exists() and not failed.with_name(failed.name + '.receipt-key').exists()
    path = tmp_path / 'authorized.json'
    with reserve_calibration(path):
        store_calibration(path, report, ceiling=120)
    saved = read_calibration(path)
    assert saved == report
    assert validate_calibration(saved, DECLARED, {}, ceiling=120) == 80
    assert sanitize({'calibration': {'operating_ceiling': 120}}) == {'calibration': {'operating_ceiling': 120}}


def test_normal_coverage_accepts_trial_beyond_pilot_limit_and_stops_early():
    calls = []
    async def run(trial):
        calls.append(trial)
        route = 'skip' if trial < 61 else 'next_a' if trial == 61 else 'next_b'
        row = measurement(trial, {'first_draft': {'route': route}}, FIRST if route == 'skip' else DECLARED,
                          structural_events(trial, route))
        return trial, observed_targets(row, maximum=80)
    summary, result = asyncio.run(cover(run, TARGETS, 80, ceiling=120))
    assert calls == list(range(1, 63)) and result == 62
    assert summary['runs'] == 62 and summary['stopping_reason'] == 'all_targets_hit'
    row = measurement(61, {'first_draft': {'route': 'skip'}}, FIRST, structural_events(61, 'skip'))
    with pytest.raises(ValueError, match='pilot trial identity'):
        observed_targets(row)


def test_expanded_coverage_exhausts_declared_cap_with_missing_target():
    calls = []
    async def run(trial):
        calls.append(trial)
        return trial, TARGETS - {'internal_retry'}
    with pytest.raises(AssertionError, match='internal_retry'):
        asyncio.run(cover(run, TARGETS, 120, ceiling=120))
    assert calls == list(range(1, 121))


@pytest.mark.parametrize('maximum,ceiling', [(61, 60), (121, 120), (True, 120), (80.0, 120)])
def test_coverage_never_exceeds_selected_budget(maximum, ceiling):
    with pytest.raises(ValueError, match='selected operating ceiling'):
        asyncio.run(cover(None, TARGETS, maximum, ceiling=ceiling))


def test_profile_ceiling_is_isolated_from_application_and_other_tests():
    from api_agent.testing.test_profiles import resolve_test_profiles
    result = configuration()
    rows = resolve_test_profiles(CASES, result.values, owner_root=ROOT, fields=result.fields)
    assert result.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 60
    assert rows[CASES[3].id]['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 120
    assert rows[CASES[0].id]['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 60
    assert rows[CASES[3].id]['provenance']['LLM_WORKFLOW_TEST_MAX_GRAPHS'].endswith(':workflow')


@pytest.mark.parametrize('value', [1, 60, 120])
def test_explicit_test_ceiling_overrides_provider_default_with_provenance(value):
    from api_agent.testing.test_profiles import resolve_test_profiles
    result = configuration(LLM_WORKFLOW_TEST_MAX_GRAPHS=value)
    row = resolve_test_profiles(CASES[3:], result.values, owner_root=ROOT, fields=result.fields)[CASES[3].id]
    assert row['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == value
    assert row['provenance']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 'interactive'


@pytest.mark.parametrize('value', [0, 121, True, 60.0, None])
def test_dict_only_test_ceiling_rejects_before_backend_creation(value):
    from api_agent.testing.test_profiles import resolve_test_profiles
    result = configuration()
    result.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = value
    with pytest.raises(ValueError, match='integer from 1 to 120'):
        resolve_test_profiles(CASES[3:], result.values, owner_root=ROOT)


def test_provider_declaration_cannot_supply_out_of_range_ceiling(monkeypatch):
    import api_agent.testing.test_profiles as profiles
    source = (ROOT / 'src/api_agent/llm_providers/bedrock/test-profiles.toml').read_text()
    original = profiles.tomllib.loads
    changed = source.replace('LLM_WORKFLOW_TEST_MAX_GRAPHS = 120', 'LLM_WORKFLOW_TEST_MAX_GRAPHS = 121')
    monkeypatch.setattr(profiles.tomllib, 'loads', lambda text: original(changed) if text == source else original(text))
    with pytest.raises(ValueError, match='integer from 1 to 120'):
        profiles.declaration(configuration().values, owner_root=ROOT)


def test_workflow_fingerprint_and_preflight_bind_current_ceiling(monkeypatch):
    from api_agent.workflows import fixture_driver as driver
    factory = ROOT / 'src/api_agent/llm_providers/gpt/factory.py'
    config = {'LLM_PROVIDER': str(factory) + ':factory'}
    current = driver.fingerprint('gpt-6-luna', config)
    expanded = driver.fingerprint('gpt-6-luna', dict(config, LLM_WORKFLOW_TEST_MAX_GRAPHS=120))
    assert current['operating_ceiling'] == 60 and expanded['operating_ceiling'] == 120
    assert current != expanded
    report = measured_calibration(sparse_retry_records(), DECLARED, ceiling=120) | expanded
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    assert driver.baseline_limit('gpt-6-luna', dict(config, LLM_WORKFLOW_TEST_MAX_GRAPHS=120)) == 80
    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', config)


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
@pytest.mark.parametrize('value', [121, True, 60.0])
def test_complete_workflow_configuration_rejects_invalid_ceiling_before_session(monkeypatch, provider, value):
    from api_agent.testing import integration, setup_pool
    from api_agent.testing.test_profiles import resolve_test_profiles
    from api_agent.runtime.resource_pack import load_launch_context
    from unittest.mock import Mock
    result = configuration()
    config = dict(result.values, LLM_PROVIDER=str(ROOT / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory')
    config['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = 60
    admitted = resolve_test_profiles(CASES[3:], config, owner_root=ROOT)
    assert admitted[CASES[3].id]['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 60
    config['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = value
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: CASES[3:])
    construct = Mock(side_effect=AssertionError('Invalid ceiling created a provider backend'))
    process = Mock(side_effect=AssertionError('Invalid ceiling started a test worker'))
    monkeypatch.setattr(setup_pool, 'SetupPool', construct)
    monkeypatch.setattr(integration.subprocess, 'Popen', process)
    run = integration.run_integration_tests(load_launch_context(ROOT), CASES[3:], accepted=True, config=config)
    assert run.exit_code == 1
    assert all('LLM_WORKFLOW_TEST_MAX_GRAPHS' in test.local_diagnostic for test in run.tests)
    construct.assert_not_called()
    process.assert_not_called()


def test_coverage_ceiling_changes_cost_compatibility_without_changing_identity():
    from api_agent.testing.cost_context import cost_config
    from api_agent.runtime.resource_pack import load_launch_context
    config = configuration().values
    context = load_launch_context(ROOT)
    ordinary = cost_config(context, dict(config, LLM_WORKFLOW_TEST_MAX_GRAPHS=60))
    expanded = cost_config(context, dict(config, LLM_WORKFLOW_TEST_MAX_GRAPHS=120))
    before = ordinary['_COST_REQUEST_SETTINGS']['LLM_WORKFLOW_TEST_MAX_GRAPHS']
    after = expanded['_COST_REQUEST_SETTINGS']['LLM_WORKFLOW_TEST_MAX_GRAPHS']
    assert len(before) == len(after) == 64 and before != after
    assert ordinary['_COST_PROVIDER_IDENTITY'] == expanded['_COST_PROVIDER_IDENTITY']
    assert ordinary['LLM_PROVIDER'] == expanded['LLM_PROVIDER']


def test_installed_and_relocated_package_keeps_ceiling_defaults_and_overrides(tmp_path):
    import shutil
    import subprocess
    import sys
    package = tmp_path / 'installed' / 'api_agent'
    shutil.copytree(ROOT / 'src/api_agent', package, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'cfg', package / 'cfg')
    probe = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from api_agent.config import load_shared_schema, resolve_config
from api_agent.providers.loading import bootstrap_schema
from api_agent.testing.collection import TestIdentity
from api_agent.testing.test_profiles import resolve_test_profiles
root = Path(sys.argv[1]) / 'api_agent'
node = 'tests/integration/test_workflow.py::test_workflow_node_coverage'
case = TestIdentity('api-agent:' + node, 'api-agent', node.split('::')[0], node, root=root)
for provider, expected in [('gpt', 60), ('bedrock', 120)]:
    values = {'LLM_PROVIDER':str(root/'llm_providers'/provider/'factory.py')+':factory',
              'TUI_MODE':'integration','SKIP_ALLURE_REPORTING':'True'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=values,
        anchors={'owner_root':root,'base_root':root,'pack_root':root}).raise_for_errors()
    selected = resolve_test_profiles([case], result.values, owner_root=root, fields=result.fields)[case.id]
    assert selected['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == expected
    values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = '60'
    explicit = resolve_config(schema, environ={}, dotenv={}, overrides=values,
        anchors={'owner_root':root,'base_root':root,'pack_root':root}).raise_for_errors()
    selected = resolve_test_profiles([case], explicit.values, owner_root=root, fields=explicit.fields)[case.id]
    assert selected['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 60
'''
    result = subprocess.run([sys.executable, '-I', '-c', probe, str(package.parent)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
