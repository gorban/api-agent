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
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    for case in CASES:
        expected = values if case is CASES[-1] else dict(values, **{key: None})
        provenance = {} if case is CASES[-1] else {key: 'inactive_bundled_test_control:shared_default'}
        assert rows[case.id]['config'] == expected and rows[case.id]['provenance'] == provenance
    assert key not in values


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


def test_workflow_cap_and_timeout_use_generic_defaults_and_workflow_profile_only():
    result = configuration()
    rows = resolve_test_profiles(CASES, result.values, owner_root=ROOT, fields=result.fields)
    assert result.values['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    assert result.values['LLM_WORKFLOW_TEST_TIMEOUT_SECONDS'] == 600
    workflow = rows[CASES[-1].id]
    assert workflow['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 1024
    assert workflow['provenance']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'].endswith(':workflow')
    assert rows[CASES[0].id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    assert rows[CASES[0].id]['provenance']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 'inactive_bundled_test_control:shared_default'


@pytest.mark.parametrize('value,expected', [('', None), ('2048', 2048)])
def test_explicit_workflow_cap_including_empty_beats_provider_profile(value, expected):
    result = configuration(LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=value,
                           LLM_WORKFLOW_TEST_TIMEOUT_SECONDS='123')
    workflow = resolve_test_profiles(CASES[-1:], result.values, owner_root=ROOT,
                                     fields=result.fields)[CASES[-1].id]
    assert workflow['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == expected
    assert workflow['config']['LLM_WORKFLOW_TEST_TIMEOUT_SECONDS'] == 123
    assert workflow['provenance']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 'interactive'
    assert result.fields['LLM_WORKFLOW_TEST_TIMEOUT_SECONDS'].source == 'interactive'


def test_unused_legacy_ceiling_does_not_gate_normal_test_profiles():
    result = configuration(LLM_WORKFLOW_TEST_MAX_GRAPHS='999')
    workflow = resolve_test_profiles(CASES[-1:], result.values, owner_root=ROOT,
                                     fields=result.fields)[CASES[-1].id]
    assert workflow['config']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 999
    assert workflow['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 1024


def test_normal_profile_declaration_never_reads_legacy_statistical_ceiling(monkeypatch):
    from api_agent.workflows import sampling
    monkeypatch.setattr(sampling, 'coverage_ceiling', lambda *args, **kwargs:
                        pytest.fail('Normal profile admission must not require statistical calibration'))
    result = configuration()
    loaded = declaration(result.values, owner_root=ROOT)
    assert loaded[2]['workflow']['settings']['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 120
    row = resolve_test_profiles(CASES[-1:], result.values, owner_root=ROOT, fields=result.fields)[CASES[-1].id]
    assert row['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 1024


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
@pytest.mark.parametrize('value', [None, 2048])
def test_workflow_cap_is_inactive_only_for_exact_known_bundled_nonconsumers(provider, value):
    from dataclasses import replace
    result = configuration(LLM_PROVIDER=str(ROOT / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory')
    result.values['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] = value
    original = deepcopy(result.values)
    cases = [CASES[0], CASES[-1], replace(CASES[0], id='unknown', node_id='tests/integration/test_custom.py::test_custom'),
             replace(CASES[0], id='parameterized', node_id=CASES[0].node_id + '[custom]')]
    rows = resolve_test_profiles(cases, result.values, owner_root=ROOT)
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert rows[CASES[0].id]['config'][key] is None
    assert rows[CASES[0].id]['provenance'][key] == 'inactive_bundled_test_control:shared_default'
    assert rows[CASES[-1].id]['config'][key] == value
    assert rows['unknown']['config'][key] == value
    assert rows['parameterized']['config'][key] == value
    assert result.values == original


def test_external_same_node_cap_is_conservative_and_order_thread_isolated(tmp_path):
    from dataclasses import replace
    result = configuration(LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=2048)
    before = deepcopy(result.values), deepcopy(result.fields)
    external = replace(CASES[0], id='external', root=tmp_path)
    cases = [CASES[0], CASES[-1], external]
    def resolve(order):
        return resolve_test_profiles(order, result.values, owner_root=ROOT, fields=result.fields)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(resolve, [cases, list(reversed(cases))])
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert first == second
    assert first[CASES[0].id]['config'][key] is None
    assert first[CASES[-1].id]['config'][key] == first['external']['config'][key] == 2048
    assert first[CASES[-1].id]['provenance'][key] == 'interactive'
    assert (result.values, result.fields) == before


def test_nonconsumer_reset_occurs_after_provider_profile_merging(monkeypatch):
    from api_agent.testing import test_profiles
    result = configuration()
    loaded = declaration(result.values, owner_root=ROOT)
    path, schema, profiles = loaded
    profiles['ordinary']['settings']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] = 999
    monkeypatch.setattr(test_profiles, 'declaration', lambda *args, **kwargs: loaded)
    row = resolve_test_profiles(CASES[:1], result.values, owner_root=ROOT, fields=result.fields)[CASES[0].id]
    assert row['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    assert row['provenance']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 'inactive_bundled_test_control:shared_default'
    assert profiles['ordinary']['settings']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 999


def test_unavailable_test_root_does_not_gain_bundled_nonconsumer_scope(tmp_path):
    from dataclasses import replace
    result = configuration(LLM_PROVIDER=str(ROOT / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
                           LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=2048)
    case = replace(CASES[0], root=tmp_path / 'absent')
    row = resolve_test_profiles([case], result.values, owner_root=ROOT)[case.id]
    assert row['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 2048
    assert 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS' not in row['provenance']


def test_real_collector_implicit_base_and_external_owner_share_correct_profile_scope(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from api_agent.testing import collection
    owner = tmp_path / 'pack'
    (owner / 'tests/integration').mkdir(parents=True)
    context = SimpleNamespace(base_root=ROOT, owner_root=owner, defaults={},
                              suite_paths=lambda mode: [owner / 'tests/integration'])
    ordinary, workflow = CASES[0].node_id, CASES[-1].node_id
    def collect(args, root, env, repo, cancel_event):
        nodes = [ordinary, workflow] if root == ROOT else [ordinary]
        Path(args[3]).write_text(json.dumps([dict(node_id=node, tags=[]) for node in nodes]))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(collection, '_run_collection', collect)
    tests = collection.collect_tests(context, 'integration')
    assert len(tests) == 3 and all(test.root is None for test in tests)
    base_repo = collection.repository_label(context, ROOT)
    base = next(test for test in tests if test.repo == base_repo and test.node_id == ordinary)
    flow = next(test for test in tests if test.repo == base_repo and test.node_id == workflow)
    external = next(test for test in tests if test.repo != base_repo)
    result = configuration(LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=2048)
    rows = resolve_test_profiles(tests, result.values, owner_root=owner, fields=result.fields, context=context)
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert rows[base.id]['config'][key] is None
    assert rows[flow.id]['config'][key] == rows[external.id]['config'][key] == 2048
    assert rows[flow.id]['config']['LLM_WORKFLOW_TEST_MODEL'] == 'zai.glm-5'
    assert rows[flow.id]['config']['BEDROCK_API_MODE'] == 'completions'
    assert rows[external.id]['provenance'] == {}
    default = configuration()
    row = resolve_test_profiles([flow], default.values, owner_root=owner, fields=default.fields, context=context)[flow.id]
    assert row['config'][key] == 1024 and row['provenance'][key].endswith(':workflow')


def test_implicit_repository_labels_handle_equal_basenames_unknown_and_explicit_roots(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from api_agent.testing import test_profiles, collection
    base = tmp_path / 'base/same'; base.mkdir(parents=True)
    owner = tmp_path / 'owner/same'; owner.mkdir(parents=True)
    context = SimpleNamespace(base_root=base, owner_root=owner)
    monkeypatch.setattr(test_profiles, 'bundled_suite_root', lambda: base)
    values = {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': 2048}
    cases = [replace(CASES[0], id=label, repo=label, root=None) for label in ('base', 'pack', 'unknown')]
    cases += [replace(CASES[0], id='explicit', repo='base', root=owner),
              replace(CASES[0], id='missing', repo='base', root=tmp_path / 'absent')]
    assert collection.repository_label(context, base) == 'base'
    assert collection.repository_label(context, owner) == 'pack'
    rows = resolve_test_profiles(cases, values, owner_root=owner, context=context)
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert rows['base']['config'][key] is None
    assert all(rows[name]['config'][key] == 2048 for name in ('pack', 'unknown', 'explicit', 'missing'))


def test_installed_canonical_label_and_ambiguous_owner_are_not_guessed(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from api_agent.testing import collection, test_profiles
    base = tmp_path / 'site-packages/api_agent'; base.mkdir(parents=True)
    owner = tmp_path / 'external'; owner.mkdir()
    context = SimpleNamespace(base_root=base, owner_root=owner)
    monkeypatch.setattr(collection, '_installed_base_identity', lambda root: root == base)
    monkeypatch.setattr(test_profiles, 'bundled_suite_root', lambda: base)
    case = replace(CASES[0], repo='api-agent', root=None)
    values = {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': 2048}
    assert resolve_test_profiles([case], values, owner_root=owner, context=context)[case.id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    collision = tmp_path / 'api-agent'; collision.mkdir()
    context.owner_root = collision
    assert collection.repository_label(context, base) == 'base'
    assert collection.repository_label(context, collision) == 'pack'
    scoped = replace(case, repo='base')
    assert resolve_test_profiles([scoped], values, owner_root=collision, context=context)[scoped.id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    monkeypatch.setattr(collection, 'repository_label', lambda context, root: 'api-agent')
    assert resolve_test_profiles([case], values, owner_root=collision, context=context)[case.id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 2048
    # Without context, the owner is the only justified mapping; canonical
    # installed labels and deduplicated same base/owner remain supported.
    assert resolve_test_profiles([case], values, owner_root=base)[case.id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None



def test_profile_context_owner_mismatch_rejects_before_declaration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.testing import test_profiles
    monkeypatch.setattr(test_profiles, 'declaration', lambda *args, **kwargs: pytest.fail('Mismatched authority loaded provider metadata'))
    with pytest.raises(ValueError, match='collection owner differs'):
        resolve_test_profiles(CASES, {}, owner_root=ROOT,
                              context=SimpleNamespace(base_root=ROOT, owner_root=tmp_path))


def test_inaccessible_explicit_root_never_gains_bundled_scope(tmp_path, monkeypatch):
    from dataclasses import replace
    blocked = tmp_path / 'blocked'; blocked.mkdir()
    original = Path.samefile
    def samefile(path, other):
        if path == blocked:
            raise PermissionError('synthetic inaccessible root')
        return original(path, other)
    monkeypatch.setattr(Path, 'samefile', samefile)
    case = replace(CASES[0], root=blocked)
    result = configuration(LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=2048)
    row = resolve_test_profiles([case], result.values, owner_root=ROOT)[case.id]
    assert row['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 2048
    assert row['provenance'] == {}


def test_unrecorded_runner_forwards_real_collection_root_context_before_setup(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from api_agent.testing import integration, test_profiles, setup_pool
    from api_agent.testing.collection import repository_label
    owner = tmp_path / 'pack'; owner.mkdir()
    context = SimpleNamespace(base_root=ROOT, owner_root=owner)
    tests = [replace(case, root=None, repo=repository_label(context, ROOT)) for case in (CASES[0], CASES[-1])]
    result = configuration()
    actual = test_profiles.resolve_test_profiles
    captured = []
    def resolve(*args, **kwargs):
        rows = actual(*args, **kwargs)
        captured.append((kwargs.get('context'), rows))
        raise ValueError('synthetic stop after scoped profile admission')
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: tests)
    monkeypatch.setattr(test_profiles, 'resolve_test_profiles', resolve)
    monkeypatch.setattr(setup_pool, 'SetupPool', lambda *args, **kwargs: pytest.fail('Synthetic profile witness started setup'))
    monkeypatch.setattr(integration.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Synthetic profile witness spawned worker'))
    run = integration.run_integration_tests.__wrapped__(context, tests, accepted=True,
        config=result.values, config_fields=result.fields)
    assert run.exit_code == 1 and len(captured) == 1
    assert captured[0][0] is context
    rows = captured[0][1]
    assert rows[tests[0].id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] is None
    assert rows[tests[1].id]['config']['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 1024


def test_runtime_endpoint_keeps_selected_models_api_and_test_overrides():
    result = configuration(BEDROCK_ENDPOINT='runtime', BEDROCK_API_MODE='converse',
                           LLM_DEFAULT_MODEL=r'us\.anthropic\.claude-haiku-4-5-20251001-v1:0',
                           LLM_REASONING_TEST_MODEL='user-selected')
    original = deepcopy(result.values)
    rows = resolve_test_profiles(CASES, result.values, owner_root=ROOT, fields=result.fields)
    for row in rows.values():
        assert row['config']['BEDROCK_ENDPOINT'] == 'runtime'
        assert row['config']['BEDROCK_API_MODE'] == 'converse'
        assert row['config']['LLM_DEFAULT_MODEL'] == original['LLM_DEFAULT_MODEL']
        assert row['config']['LLM_REASONING_TEST_MODEL'] == 'user-selected'
        assert not any(source.startswith('provider_test_profile:') for source in row['provenance'].values())
    assert result.values == original


def test_missing_endpoint_uses_mantle_declaration_default():
    result = configuration()
    values = dict(result.values)
    values.pop('BEDROCK_ENDPOINT')
    _, _, profiles = declaration(values, owner_root=ROOT)
    assert 'ordinary' in profiles


@pytest.mark.parametrize('condition', [None, 'invalid', {'MISSING': 'x'},
                                       {'LLM_DEFAULT_MODEL': 'x'}, {'EXAMPLE_OTHER': 'x'},
                                       {'EXAMPLE_API_MODE': 'unknown'}, {'EXAMPLE_API_MODE': 1},
                                       {'EXAMPLE_SECRET_API_MODE': 'responses'}])
def test_profile_condition_rejects_unsafe_or_invalid_fields(synthetic_declaration, monkeypatch, condition):
    with pytest.raises(ValueError, match='condition'):
        load_synthetic_declaration(monkeypatch, synthetic_declaration,
                                   lambda data: data.update(applies_when=condition))


def test_inactive_profile_is_still_validated(synthetic_declaration, monkeypatch):
    def mutate(data):
        data['applies_when'] = {'EXAMPLE_API_MODE': 'completions'}
        data['profiles']['ordinary']['settings']['UNKNOWN_FIELD'] = 'invalid'
    with pytest.raises(ValueError, match='Forbidden'):
        load_synthetic_declaration(monkeypatch, synthetic_declaration, mutate)


def test_inactive_profile_condition_has_no_settings(synthetic_declaration, monkeypatch):
    _, _, profiles = load_synthetic_declaration(monkeypatch, synthetic_declaration,
        lambda data: data.update(applies_when={'EXAMPLE_API_MODE': 'completions'}))
    assert profiles == {}
