"""Operation configuration isolation, exact admission and declaration controls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from api_agent.config import load_shared_schema, resolve_config
from api_agent.providers.loading import bootstrap_schema
from api_agent.testing.collection import TestIdentity
from api_agent.testing.test_profiles import declaration, resolve_test_profiles, validate_snapshot_profiles

ROOT = Path(__file__).resolve().parents[2]


def configuration(**overrides):
    values = {'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
              'TUI_MODE': 'integration', 'SKIP_ALLURE_REPORTING': True, **overrides}
    values = {key: str(value) for key, value in values.items()}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=ROOT)
    return resolve_config(schema, environ={}, dotenv={}, overrides=values,
                          anchors={'base_root': ROOT, 'owner_root': ROOT, 'pack_root': ROOT}).raise_for_errors()


def identity(module, case):
    node = f'tests/integration/{module}.py::test_{case}'
    return TestIdentity('api-agent:' + node, 'api-agent', module, node, root=ROOT)


CASES = [identity('test_llm_request', 'llm_request'),
         identity('test_reasoning_contrast', 'reasoning_contrast'),
         identity('test_studio_selection', 'studio_selection'),
         identity('test_workflow', 'workflow_node_coverage')]


def test_profiles_use_qualified_defaults_without_changing_application_or_identity(monkeypatch):
    result = configuration(BEDROCK_API_MODE='completions', UNATTENDED_USER_INDEX=3,
                           BEDROCK_PROFILE='selected', LLM_REASONING_EFFORT='high')
    result.values['UNATTENDED_USER_INDEX'] = 3
    original = deepcopy(result.values)
    monkeypatch.setenv('BEDROCK_API_MODE', 'sentinel')
    rows = resolve_test_profiles(CASES, result.values, owner_root=ROOT, fields=result.fields)
    ordinary, reasoning, studio, workflow = [rows[case.id]['config'] for case in CASES]
    assert ordinary['BEDROCK_API_MODE'] == reasoning['BEDROCK_API_MODE'] == studio['BEDROCK_API_MODE'] == 'responses'
    assert ordinary['LLM_REASONING_EFFORT'] == 'low'
    assert reasoning['LLM_REASONING_TEST_HIGH_EFFORT'] == 'medium'
    assert reasoning['LLM_REASONING_TEST_MAX_OUTPUT_TOKENS'] == 16000
    assert studio['LLM_STUDIO_TEST_SELECTED_EFFORT'] == 'medium'
    assert workflow['BEDROCK_API_MODE'] == 'completions'
    assert workflow['WORKFLOW_MAX_PASSES'] == 4
    assert ordinary['WORKFLOW_MAX_PASSES'] == 2
    for row in rows.values():
        assert row['config']['UNATTENDED_USER_INDEX'] == 3
        assert row['config']['BEDROCK_PROFILE'] == 'selected'
        assert row['config']['LLM_PROVIDER'] == original['LLM_PROVIDER']
        assert row['config']['LLM_ALLOWED_MODELS'] == original['LLM_ALLOWED_MODELS']
        assert row['provenance']
    assert result.values == original
    import os
    assert os.environ['BEDROCK_API_MODE'] == 'sentinel'


@pytest.mark.parametrize('value', ['', 'openai.gpt-oss-20b'])
def test_explicit_test_choice_including_empty_is_authoritative(value):
    result = configuration(LLM_REASONING_TEST_MODEL=value)
    row = resolve_test_profiles(CASES[1:2], result.values, owner_root=ROOT, fields=result.fields)[CASES[1].id]
    assert row['config']['LLM_REASONING_TEST_MODEL'] == value
    assert row['provenance']['LLM_REASONING_TEST_MODEL'] == 'interactive'


def test_profile_resolution_is_order_and_thread_isolated():
    result = configuration()
    def run(cases):
        return resolve_test_profiles(cases, result.values, owner_root=ROOT, fields=result.fields)
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(run, [CASES, list(reversed(CASES))]))
    assert first == second
    first[CASES[0].id]['config']['BEDROCK_API_MODE'] = 'changed'
    assert second[CASES[0].id]['config']['BEDROCK_API_MODE'] == 'responses'


def test_parameterized_case_requires_exact_declaration():
    result = configuration()
    case = identity('test_llm_request', 'llm_request[extra]')
    row = resolve_test_profiles([case], result.values, owner_root=ROOT, fields=result.fields)[case.id]
    assert row['provenance'] == {}


def test_profile_model_still_obeys_allowed_policy():
    result = configuration(LLM_ALLOWED_MODELS='zai.glm-5')
    from api_agent.providers.contracts import ProviderAccessError
    with pytest.raises(ProviderAccessError):
        resolve_test_profiles(CASES[1:2], result.values, owner_root=ROOT, fields=result.fields)


def test_ipc_profile_validation_rejects_unknown_keys():
    with pytest.raises(ValueError):
        validate_snapshot_profiles({'case': {'config': {}, 'provenance': {}, 'auth': {}}})


def test_provider_declarations_are_inert():
    result = configuration()
    path, schema, profiles = declaration(result.values, owner_root=ROOT)
    assert path.name == 'test-profiles.toml'
    assert profiles['workflow']['settings']['LLM_WORKFLOW_TEST_MODEL'] == 'zai.glm-5'
    assert 'BEDROCK_AUTH_MODE' not in profiles['workflow']['settings']


def test_gpt_keeps_application_and_existing_contributed_choices():
    factory = ROOT / 'src/api_agent/llm_providers/gpt/factory.py'
    values = {'LLM_PROVIDER': str(factory) + ':factory', 'LLM_REASONING_TEST_MODEL': 'gpt-6-luna',
              'GPT_API_MODE': 'responses', 'LLM_REASONING_TEST_HIGH_EFFORT': 'high'}
    rows = resolve_test_profiles(CASES, values, owner_root=ROOT)
    assert all(row['config'] == values and row['provenance'] == {} for row in rows.values())


def test_dict_only_explicit_empty_cannot_be_replaced():
    result = configuration()
    result.values['LLM_WORKFLOW_TEST_MODEL'] = ''
    row = resolve_test_profiles(CASES[3:], result.values, owner_root=ROOT)[CASES[3].id]
    assert row['config']['LLM_WORKFLOW_TEST_MODEL'] == ''
    assert row['provenance']['LLM_WORKFLOW_TEST_MODEL'] == 'explicit_mapping'


@pytest.mark.parametrize('setting', ['LLM_PROVIDER', 'LLM_ALLOWED_MODELS', 'BEDROCK_AUTH_MODE',
                                    'BEDROCK_PROFILE', 'UNATTENDED_USER_INDEX', 'CLIENT_CA_FILE'])
def test_profile_rejects_security_and_identity_overrides(tmp_path, monkeypatch, setting):
    import tomllib
    import api_agent.testing.test_profiles as module
    result = configuration()
    source = declaration(result.values, owner_root=ROOT)[0].read_text()
    values = {'LLM_PROVIDER': "'/tmp/other.py:factory'", 'LLM_ALLOWED_MODELS': "'.*'",
              'BEDROCK_AUTH_MODE': "'api_key'", 'BEDROCK_PROFILE': "'another'",
              'UNATTENDED_USER_INDEX': '1', 'CLIENT_CA_FILE': "'/tmp/other-ca.pem'"}
    altered = source.replace("[profiles.ordinary.settings]", "[profiles.ordinary.settings]\n" + setting + ' = ' + values[setting])
    original = tomllib.loads
    monkeypatch.setattr(module.tomllib, 'loads', lambda value: original(altered) if value == source else original(value))
    with pytest.raises(ValueError, match='Forbidden'):
        declaration(result.values, owner_root=ROOT)


def test_worker_admits_only_exact_case_and_copies_effective_values(monkeypatch):
    from api_agent.testing import provider_setup
    result = configuration()
    row = resolve_test_profiles(CASES[1:2], result.values, owner_root=ROOT, fields=result.fields)[CASES[1].id]
    snapshot = {'config': result.values, 'test_profiles': {CASES[1].node_id: row}}
    monkeypatch.setattr(provider_setup, '_read_snapshot', lambda: deepcopy(snapshot))
    request = SimpleNamespace(node=SimpleNamespace(nodeid=CASES[1].node_id))
    first = provider_setup._test_snapshot(request)
    first['config']['BEDROCK_API_MODE'] = 'changed'
    assert provider_setup._test_snapshot(request)['config']['BEDROCK_API_MODE'] == 'responses'
    request.node.nodeid += '[unadmitted]'
    from api_agent.providers.contracts import ContractError
    with pytest.raises(ContractError, match='outside'):
        provider_setup._test_snapshot(request)


def test_unsupported_workflow_is_not_offered_calibration():
    from api_agent.workflows.fixture_driver import preflight_baselines
    result = configuration()
    with pytest.raises(ValueError, match='before calibration'):
        preflight_baselines(result.values, owner_root=ROOT)


@pytest.fixture
def synthetic_declaration(tmp_path):
    """A valid inert provider contract, independently authored for admission checks."""
    factory = tmp_path / 'factory.py'
    factory.write_text('')
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/config.toml').write_text('''version = 1
[fields.EXAMPLE_API_MODE]
type = "str"
enum = ["responses", "completions"]
[fields.EXAMPLE_REASONING_EFFORT]
type = "str"
enum = ["low", "medium"]
[fields.EXAMPLE_MAX_OUTPUT_TOKENS]
type = "int"
validation = "positive"
[fields.EXAMPLE_INFERENCE_TIMEOUT_SECONDS]
type = "float"
validation = "positive"
[fields.EXAMPLE_OTHER]
type = "str"
[fields.EXAMPLE_SECRET_API_MODE]
type = "str"
sensitive = true
''')
    (tmp_path / 'test-profiles.toml').write_text('synthetic profile input')
    data = {'version': 1, 'factory': 'factory', 'provider_fields': ['EXAMPLE_API_MODE'],
            'profiles': {'ordinary': {'cases': [CASES[0].node_id], 'settings': {'EXAMPLE_API_MODE': 'responses'}}}}
    return {'LLM_PROVIDER': str(factory) + ':factory'}, data


def load_synthetic_declaration(monkeypatch, fixture, mutate=lambda data: None):
    import api_agent.testing.test_profiles as module
    config, data = fixture
    data = deepcopy(data)
    mutate(data)
    original = module.tomllib.loads
    monkeypatch.setattr(module.tomllib, 'loads', lambda value: data if value == 'synthetic profile input' else original(value))
    return declaration(config, owner_root=ROOT)


@pytest.mark.parametrize('change', [
    lambda d: d.update(version=2), lambda d: d.update(factory='wrong'), lambda d: d.update(extra=True),
    lambda d: d.update(provider_fields='invalid'), lambda d: d.update(provider_fields=['EXAMPLE_API_MODE'] * 2),
    lambda d: d.update(provider_fields=['LLM_DEFAULT_MODEL']), lambda d: d.update(provider_fields=['MISSING']),
    lambda d: d.update(provider_fields=[3]), lambda d: d.update(provider_fields=['EXAMPLE_SECRET_API_MODE']),
    lambda d: d.update(provider_fields=['EXAMPLE_OTHER']), lambda d: d.update(profiles=[]),
    lambda d: d['profiles'].update(ordinary=[]),
    lambda d: d['profiles']['ordinary'].update(extra=True),
    lambda d: d['profiles']['ordinary'].update(cases=[]),
    lambda d: d['profiles']['ordinary'].update(cases='invalid'),
    lambda d: d['profiles']['ordinary'].update(settings=[]),
    lambda d: d['profiles']['ordinary'].update(cases=[3]),
    lambda d: d['profiles']['ordinary'].update(cases=['tests/elsewhere.py::test_wrong']),
    lambda d: d['profiles']['ordinary'].update(cases=[CASES[0].node_id] * 2),
])
def test_inert_profile_contract_rejects_malformed_admission(synthetic_declaration, monkeypatch, change):
    def isolated_change(data):
        original_fields = deepcopy(data['provider_fields'])
        change(data)
        if data['provider_fields'] != original_fields and isinstance(data['profiles'], dict):
            data['profiles']['ordinary']['settings'] = {}
    with pytest.raises(ValueError):
        load_synthetic_declaration(monkeypatch, synthetic_declaration, isolated_change)


@pytest.mark.parametrize('key,value', [('EXAMPLE_REASONING_EFFORT', 'medium'),
                                      ('EXAMPLE_MAX_OUTPUT_TOKENS', 1024),
                                      ('EXAMPLE_INFERENCE_TIMEOUT_SECONDS', 900)])
def test_typed_provider_generation_controls_are_admitted(synthetic_declaration, monkeypatch, key, value):
    def controls(data):
        data['provider_fields'] = [key]
        data['profiles']['ordinary']['settings'] = {key: value}
    _, _, profiles = load_synthetic_declaration(monkeypatch, synthetic_declaration, controls)
    assert profiles['ordinary']['settings'] == {key: value}


def test_duplicate_host_identity_rejected_before_pool():
    result = configuration()
    with pytest.raises(ValueError, match='Duplicate'):
        resolve_test_profiles([CASES[0], CASES[0]], result.values, owner_root=ROOT, fields=result.fields)


def test_same_node_in_another_owner_does_not_receive_bundled_defaults(tmp_path):
    from dataclasses import replace
    result = configuration(BEDROCK_API_MODE='completions')
    other = replace(CASES[0], id='other:' + CASES[0].node_id, repo='other', root=tmp_path)
    rows = resolve_test_profiles([CASES[0], other], result.values, owner_root=ROOT, fields=result.fields)
    assert rows[CASES[0].id]['config']['BEDROCK_API_MODE'] == 'responses'
    assert rows[other.id]['config']['BEDROCK_API_MODE'] == 'completions'
    assert rows[other.id]['provenance'] == {}


@pytest.mark.parametrize('profiles', [None, [], {'case': None}, {'case': {'config': {}, 'provenance': {3: 'invalid'}}}])
def test_malformed_worker_profile_maps_reject(profiles):
    with pytest.raises(ValueError):
        validate_snapshot_profiles(profiles)


def test_worker_profile_without_backend_is_valid():
    validate_snapshot_profiles({'case': {'config': {}, 'provenance': {}}})


def test_effective_profile_fixture_and_setup_hook_publish_only_controls(monkeypatch, capsys):
    from api_agent.testing import provider_setup
    snapshot = {'owner_root': '/', 'config': {'LLM_REASONING_EFFORT': 'low', 'SECRET': 'never show'},
                'profile_provenance': {'LLM_REASONING_EFFORT': 'provider_test_profile:ordinary'}}
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda request: deepcopy(snapshot))
    properties = []
    requirements = provider_setup.integration_config.__wrapped__(None, lambda *args: properties.append(args))
    assert requirements.provenance == snapshot['profile_provenance']
    monkeypatch.delenv('API_AGENT_INTEGRATION_CONTEXT', raising=False)
    item = SimpleNamespace(user_properties=[])
    provider_setup.pytest_runtest_setup(item)
    assert item.user_properties == []
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', '/synthetic')
    provider_setup.pytest_runtest_setup(item)
    assert item.user_properties[0][0] == properties[0][0] == 'integration_test_profile'
    assert 'never show' not in capsys.readouterr().out
    snapshot['profile_provenance'] = {}
    provider_setup.pytest_runtest_setup(item)
    assert len(item.user_properties) == 1


def test_runner_rejects_unadmitted_resolved_profile_map_before_process(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.collection import CollectionError
    case = TestIdentity('owner::test.py::test_ok', 'owner', 'test.py', 'test.py::test_ok', root=tmp_path)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: (case,))
    def unexpected(*args, **kwargs):
        raise AssertionError('Invalid profile map started a process')
    monkeypatch.setattr(integration.subprocess, 'Popen', unexpected)
    with pytest.raises(CollectionError, match='do not match'):
        integration.run_integration_tests(context, [case], accepted=True, config={}, resolved_test_profiles={})


def test_unselected_provider_has_no_profile_declaration():
    assert declaration({}, owner_root=ROOT) is None


def test_valid_worker_profile_backend_descriptor_is_retained():
    descriptor = {'version': 1, 'host': '127.0.0.1', 'port': 43210, 'capability': 'a' * 32}
    row = {'config': {}, 'provenance': {}, 'setup_backend': descriptor}
    validate_snapshot_profiles({'case': row})
    assert row['setup_backend'] == descriptor


def test_tool_profiles_use_structured_completion_without_changing_ordinary_or_workflow_defaults():
    result = configuration(LLM_REASONING_EFFORT='high', BEDROCK_API_MODE='responses')
    tools = [identity('test_tool_instances', 'shared_primary'), identity('test_tool_alternate', 'independent_tools')]
    rows = resolve_test_profiles([*CASES, *tools], result.values, owner_root=ROOT, fields=result.fields)
    for case in tools:
        row = rows[case.id]
        assert row['config']['BEDROCK_API_MODE'] == 'completions'
        assert row['config']['LLM_DEFAULT_MODEL'] == r'zai\.glm-5'
        assert row['config']['LLM_REASONING_EFFORT'] == ''
        assert row['config']['BEDROCK_INFERENCE_TIMEOUT_SECONDS'] == 900
        assert row['config']['LLM_SETUP_REQUEST_TIMEOUT_SECONDS'] == 960
        assert row['config']['WORKFLOW_MAX_PASSES'] == 2
        assert row['provenance']['LLM_DEFAULT_MODEL'].endswith(':tools')
    assert rows[CASES[0].id]['config']['BEDROCK_API_MODE'] == 'responses'
    assert rows[CASES[3].id]['config']['WORKFLOW_MAX_PASSES'] == 4
    assert result.values['LLM_REASONING_EFFORT'] == 'high'
