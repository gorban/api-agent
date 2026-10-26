"""Reviewed baseline validation and explicit pilot never require live inference."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.workflows import fixture_driver as driver
from api_agent.workflows.sampling import DECLARED, FIRST, TARGETS, measurement, measured_calibration, validate_calibration

def structural_events(trial, route, *, retry=True):
    """Synthetic emitted lifecycles with independent scope and attempt identities."""
    events = []
    graph = f'graph-{trial}'
    def start(scope, parent, owner, kind, **values):
        row = dict(event='scope_start', scope_id=scope, parent_id=parent, owner=owner, kind=kind, **values)
        events.append(row)
        return row
    def end(row):
        events.append(dict(row, event='scope_end', outcome='passed'))
    graph_scope = start(graph, None, 'GRAPH', 'graph', attempt=1, max_passes=1)
    events.append(dict(event='edge', owner='GRAPH', source='START', target='first', implicit=True, attempt_id=None))
    for component in (['first'] if route == 'skip' else ['first', 'second']):
        scope = f'{graph}-{component}'
        component_scope = start(scope, graph, component, 'component', component=component, attempt=1, max_passes=2, attempt_id=None)
        for attempt in ((1, 2) if retry else (1,)):
            attempt_id = f'{scope}-attempt-{attempt}'
            events.append(dict(event='attempt', scope_id=scope, component=component, attempt=attempt, max_passes=2, attempt_id=attempt_id))
            for suffix in ('draft', 'format', 'guard'):
                node = f'{component}_{suffix}'
                node_scope = start(f'{attempt_id}-{node}', scope, node, 'node', component=component, node=node,
                                   attempt=attempt, max_passes=2, attempt_id=attempt_id)
                events.append(dict(event='node', scope_id=node_scope['scope_id'], node=node, component=component, attempt=attempt))
                end(node_scope)
        end(component_scope)
        target = ('END' if route == 'skip' else 'second') if component == 'first' else 'END'
        condition = ('first_draft.route == "skip"' if route == 'skip' else 'otherwise') if component == 'first' else 'always'
        events.append(dict(event='edge', owner='GRAPH', source=component, target=target, condition=condition,
                           implicit=component == 'second', visit_id=scope, attempt_id=None))
    end(graph_scope)
    return events




FACTORY = str(Path(driver.__file__).parent.parent / 'llm_providers/gpt/factory.py') + ':factory'
CONFIG = {'LLM_PROVIDER': FACTORY, 'GPT_AUTH_MODE': 'subscription', 'GPT_API_MODE': 'responses',
          'LLM_CACHE_POLICY': None, 'WORKFLOW_LLM_TIMEOUT_SECONDS': 120.0,
          'LLM_DEFAULT_MODEL': '^gpt-6-luna$'}


@pytest.fixture
def legacy_report():
    path = Path(__file__).parent / 'fixtures/workflow_baseline_legacy.json'
    body = path.read_bytes()
    assert sha256(body).hexdigest() == '7c281b11b45fecfb01e153db8913d8477e33259e4fbb94692a5d852d7b06e752'
    return json.loads(body)['reports'][0]


def synthetic_current_report(config=CONFIG):
    records = []
    for trial in range(1, 61):
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        nodes = DECLARED if route != 'skip' else FIRST
        records.append(measurement(trial, {'first_draft': {'route': route}}, nodes, structural_events(trial, route)))
    return measured_calibration(records, DECLARED) | driver.fingerprint('gpt-6-luna', config)


def test_effective_component_pass_override_changes_exact_fixture_fingerprint():
    default = driver.fingerprint('gpt-6-luna', CONFIG)
    changed = driver.fingerprint('gpt-6-luna', CONFIG | {'WORKFLOW_MAX_PASSES': 3})

    assert default['effective_settings']['retries'] == {'first': 2, 'second': 2}
    assert changed['effective_settings']['retries'] == {'first': 3, 'second': 3}
    assert changed['sources'] == default['sources']
    assert changed['effective_settings'] != default['effective_settings']


def historical_fingerprint(report):
    keys = ('fingerprint_version', 'sampling_method', 'model', 'dependency_versions',
            'python_version', 'backend', 'provider_identity', 'user_input_hash',
            'effective_settings', 'sources')
    return {key: deepcopy(report[key]) for key in keys}


def test_reviewed_baseline_is_recomputed_without_local_calibration_file(monkeypatch):
    from api_agent.workflows.sampling import measured_calibration
    report = synthetic_current_report()
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    maximum = driver.baseline_limit('gpt-6-luna', CONFIG)
    assert maximum == report['max_runs']
    assert driver.preflight_baselines(CONFIG) == {'gpt-6-luna': maximum}
    assert 'receipt' not in report


def test_legacy_node_only_measurements_cannot_qualify_expanded_targets(legacy_report):
    from api_agent.workflows.sampling import validate_calibration
    report = legacy_report
    with pytest.raises(ValueError, match='schema'):
        validate_calibration(legacy_report, DECLARED, {})
    assert report['trials'] == len(report['records'])
    assert sum(report['routes'].values()) == report['trials']
    assert 'receipt' not in report
    assert report['hits']['second_draft'] == report['routes']['next_a'] + report['routes']['next_b']


def test_reviewed_reasoning_equivalence_keeps_original_observations_and_exact_guard(legacy_report):
    from api_agent.workflows.sampling import measured_calibration
    report = legacy_report
    source = 'src/api_agent/llm_providers/gpt/reasoning.py'
    receipt = report['carry_forward']
    assert receipt == {
        'kind': 'reviewed_request_equivalence', 'model': 'gpt-6-luna',
        'route': 'official_subscription_responses', 'reasoning_effort': 'low',
        'source': source,
        'original_source_sha256': '8ea914b93096b4a48af3023da129d172ae474575b002562317459666df97a43c',
        'current_source_sha256': '03db69e5535cb82583f1acf2b0dedc1efc1cb843bacec2738ccb645c449dc7a2',
        'original_fixture_sha256': '8c12b099e26e0e2c9955ba3e2fe5543ae00179d3814fea313db522de6bb0c3d9',
        'basis': ('Deterministic old/new request-admission and native-wire equivalence for the retained '
                  'model and route; original pilot observations and bound are unchanged.'),
        'new_pilot': False}
    assert report['sources'][source] == receipt['current_source_sha256']
    assert report['trials'] == 20 and report['max_runs'] == 22
    assert report['records'][0]['trial'] == 1 and report['records'][-1]['trial'] == 20
    with pytest.raises(ValueError, match='schema'):
        measured_calibration(report['records'], DECLARED)
    with pytest.raises(ValueError, match='schema'):
        validate_calibration(report, DECLARED, historical_fingerprint(report))
    changed = deepcopy(report)
    changed['sources'][source] = receipt['original_source_sha256']
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(changed, DECLARED, historical_fingerprint(report))


def test_provenance_receipt_cannot_admit_the_old_or_an_unrelated_source(legacy_report):
    source = 'src/api_agent/llm_providers/gpt/reasoning.py'
    report = deepcopy(legacy_report)
    fingerprint = historical_fingerprint(report)
    with pytest.raises(ValueError, match='schema'):
        validate_calibration(report, DECLARED, fingerprint)
    report['sources'][source] = report['carry_forward']['original_source_sha256']
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, fingerprint)

    report['sources'][source] = report['carry_forward']['current_source_sha256']
    report['sources']['src/api_agent/llm_providers/gpt/inference.py'] = '0' * 64
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, fingerprint)


def test_browser_visibility_binding_retains_prior_receipt_measurements_and_strict_preflight(legacy_report):
    from api_agent.workflows.sampling import measured_calibration
    report = deepcopy(legacy_report)
    source = 'src/api_agent/llm_providers/gpt/factory.py'
    receipt = report['carry_forward_browser_policy']
    assert receipt == {
        'kind': 'reviewed_non_sampling_visibility_binding', 'model': 'gpt-6-luna',
        'route': 'official_subscription_responses', 'source': source,
        'original_source_sha256': '0065b50eb67b0f4efe9fc18676a1bbf99b645dd31de627e9c2c30660de7aa376',
        'current_source_sha256': '1a3228adad454cb28df7176c7f268d3df21064bf164b067ef59ed20331174665',
        'previous_fixture_sha256': 'ebc2add07aee3d8b0d518854b8258c45caada33f3131933c20f1a86ac4b154b4',
        'prior_receipt_kind': 'reviewed_request_equivalence',
        'basis': ('The factory change narrows visible login presentation outside the sampled workflow '
                  'and changes a login diagnostic; sampled request construction and execution remain unchanged.'),
        'new_pilot': False}
    assert report['carry_forward']['current_source_sha256'] == report['sources'][
        'src/api_agent/llm_providers/gpt/reasoning.py']
    assert receipt['current_source_sha256'] == report['sources'][source] == report['provider_identity']['source_sha256']
    assert sha256(json.dumps(report['records'], sort_keys=True, separators=(',', ':')).encode()).hexdigest() == (
        'a09434887a4366296bd5ea3c819a5519a9c12f255d35325c9654168021e0aa2d')
    assert report['trials'] == 20 and report['routes'] == {'next_a': 5, 'next_b': 8, 'skip': 7}
    assert report['minimum_runs'] == 18 and report['safety_margin'] == 4 and report['max_runs'] == 22
    with pytest.raises(ValueError, match='schema'):
        measured_calibration(report['records'], DECLARED)
    fingerprint = historical_fingerprint(report)
    with pytest.raises(ValueError, match='schema'):
        validate_calibration(report, DECLARED, fingerprint)

    report['sources'][source] = receipt['original_source_sha256']
    report['provider_identity']['source_sha256'] = receipt['original_source_sha256']
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, fingerprint)


@pytest.mark.parametrize('setting,value', [('GPT_API_MODE', 'completions'),
    ('LLM_CACHE_POLICY', 'off'), ('GPT_AUTH_MODE', 'api_key'),
    ('WORKFLOW_LLM_TIMEOUT_SECONDS', 30), ('WORKFLOW_GRAPH_TIMEOUT_SECONDS', 5)])
def test_stale_settings_reject_before_graph_execution(monkeypatch, setting, value):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    graph = Mock(side_effect=AssertionError('Stale sampling must not execute'))
    monkeypatch.setattr(driver, 'run_graph', graph)
    with pytest.raises(ValueError, match='stale'):
        driver.node_coverage(object(), 'gpt-6-luna', CONFIG | {setting: value})
    graph.assert_not_called()


def test_changed_worker_registry_rejects_retained_workflow_qualification(monkeypatch):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    original_digest = driver.sampling_digest

    def changed_digest(path):
        if path.name == 'setup_registry.py':
            return sha256(b'changed worker registry').hexdigest()
        return original_digest(path)

    monkeypatch.setattr(driver, 'sampling_digest', changed_digest)

    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', CONFIG)


@pytest.mark.parametrize('config', [{}, CONFIG | {'LLM_DEFAULT_MODEL': '^unknown$'},
                                    CONFIG | {'LLM_ALLOWED_MODELS': '^unknown$'}])
def test_preflight_rejects_unreviewed_configured_selection(config):
    with pytest.raises(ValueError, match='[Nn]o reviewed'):
        driver.preflight_baselines(config)


@pytest.mark.parametrize('reports', [[], [{'model': 'gpt-6-luna'}, {'model': 'gpt-6-luna'}]])
def test_missing_or_ambiguous_model_baseline_rejected(monkeypatch, reports):
    monkeypatch.setattr(driver, '_baselines', lambda: reports)
    with pytest.raises(ValueError, match='No reviewed'):
        driver.baseline_limit('gpt-6-luna', CONFIG)


@pytest.mark.parametrize('document', [{'schema': 2, 'reports': []}, {'schema': 1, 'reports': {}}])
def test_packaged_baseline_schema_rejected(tmp_path, monkeypatch, document):
    (tmp_path / 'fixture_baselines.json').write_text(json.dumps(document))
    monkeypatch.setattr(driver, 'files', lambda package: tmp_path)
    with pytest.raises(ValueError, match='Invalid reviewed'):
        driver._baselines()


def test_installed_fixture_root_uses_packaged_assets(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    monkeypatch.setattr(driver, 'files', lambda name: package)
    assert driver.fixture_root(tmp_path / 'missing') == package
    assert (driver.fixture_root() / 'demo/contrived/flow.md.html').samefile(
        Path(__file__).resolve().parents[2] / 'demo/contrived/flow.md.html')


def test_nested_declaration_change_invalidates_fixture_fingerprint(tmp_path, monkeypatch):
    from shutil import copytree

    source_root = Path(__file__).resolve().parents[2]
    copytree(source_root / 'demo', tmp_path / 'demo')
    before = driver.fingerprint('gpt-6-luna', CONFIG, root=tmp_path)
    node = tmp_path / 'demo/contrived/workflows/components/first.md.html'
    node.write_text(node.read_text().replace('## Node: first_draft\n', '## Node: first_draft\n\n', 1))

    after = driver.fingerprint('gpt-6-luna', CONFIG, root=tmp_path)

    assert before['sources'] != after['sources']
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', CONFIG, root=tmp_path)


def test_reporting_code_is_excluded_but_sampling_changes_invalidate(monkeypatch):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    before = driver.fingerprint('gpt-6-luna', CONFIG)
    assert not any('/tui/' in name or name.endswith(('testing/integration.py',
        'testing/contracts.py', 'testing/inspection_controls.py')) for name in before['sources'])
    original = Path.read_bytes

    def changed(path):
        body = original(path)
        return body.replace(b'_LIMIT = 1048576', b'_LIMIT = 1048577') if path.name == 'inference.py' else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', CONFIG)


@pytest.mark.parametrize(('helper', 'before_text', 'after_text'), [
    ('bounded_schema.py', '_VALIDATION_SECONDS = 1.0', '_VALIDATION_SECONDS = 2.0'),
    ('schema_validation_worker.py', 'if len(raw) <= 2097152:', 'if len(raw) <= 1048576:'),
])
def test_schema_acceptance_helper_change_rejects_calibration_before_graph(
        tmp_path, monkeypatch, helper, before_text, after_text):
    package = Path(driver.__file__).parent.parent
    config = CONFIG
    report = synthetic_current_report(config)
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    assert driver.baseline_limit('gpt-6-luna', config) == report['max_runs']
    source = package / 'providers' / helper
    original = source.read_text()
    assert before_text in original
    changed_source = tmp_path / helper
    changed_source.write_text(original.replace(before_text, after_text))
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: read_bytes(
        changed_source if path == source else path))
    graph = Mock()
    monkeypatch.setattr(driver, 'run_graph', graph)
    provider = SimpleNamespace(complete=Mock())

    current = driver.fingerprint('gpt-6-luna', config)

    key = 'src/api_agent/providers/' + helper
    assert current['sources'][key] != report['sources'][key]
    assert current['fingerprint_version'] == report['fingerprint_version'] == 4
    with pytest.raises(ValueError, match='stale'):
        driver.node_coverage(provider, 'gpt-6-luna', config)
    graph.assert_not_called()
    provider.complete.assert_not_called()


def test_maintenance_only_addition_does_not_change_sampling_digest(tmp_path):
    source = Path(driver.__file__)
    path = tmp_path / 'fixture_driver.py'
    path.write_bytes(source.read_bytes())
    before = driver.sampling_digest(path)
    path.write_bytes(path.read_bytes() + b'\ndef maintenance_helper_only():\n    return 20\n')
    assert driver.sampling_digest(path) == before


@pytest.mark.parametrize('failure', [False, True])
def test_graph_uses_independent_seed_and_publishes_observations(monkeypatch, failure):
    published, streamed, seeds = [], [], []
    provider = SimpleNamespace(complete=Mock(return_value='completed'))

    def build(root, **options):
        seeds.append(options['trial'])
        assert options['selection'] == 'synthetic'

        async def arun(inputs, *, turn_id):
            assert inputs == {'user_input': driver.USER_INPUT}
            assert await options['llm']('request') == 'completed'
            options['observe']({'event': 'llm', 'node': 'first_draft'})
            options['observe']({'event': 'node', 'node': 'first_draft'})
            if failure:
                raise RuntimeError('synthetic graph defect')
            return {'first_draft': {'route': 'skip'}}

        return SimpleNamespace(arun=arun)

    monkeypatch.setattr(driver, 'build', build)
    monkeypatch.setattr(driver, 'publish', published.append)
    if failure:
        with pytest.raises(RuntimeError, match='graph defect'):
            driver.run_graph(provider, 'synthetic', 1, CONFIG, observe=streamed.append)
    else:
        assert driver.run_graph(provider, 'synthetic', 1, CONFIG, observe=streamed.append) == ({'first_draft': {'route': 'skip'}}, {'first_draft'})
    assert len(seeds[0]) == 36
    assert published == [{'trial': 1, 'events': streamed}]
    assert len(streamed) == 2


def test_graph_observer_is_optional(monkeypatch):
    def build(root, **options):
        async def arun(inputs, *, turn_id):
            options['observe']({'event': 'node', 'node': 'first_draft'})
            return {}
        return SimpleNamespace(arun=arun)

    monkeypatch.setattr(driver, 'build', build)
    monkeypatch.setattr(driver, 'publish', lambda report: None)
    assert driver.run_graph(object(), 'synthetic', 1, CONFIG) == ({}, {'first_draft'})


def test_node_coverage_stops_when_all_targets_hit(monkeypatch):
    calls, published = [], []
    synthetic_report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [synthetic_report])

    def graph(provider, selection, trial, config, **options):
        calls.append(trial)
        route = ('skip', 'next_a', 'next_b')[trial - 1]
        for event in structural_events(trial, route):
            options['observe'](event)
        return {'first_draft': {'route': route}}, FIRST if trial == 1 else DECLARED

    monkeypatch.setattr(driver, 'run_graph', graph)
    monkeypatch.setattr(driver, 'publish', published.append)
    result = driver.node_coverage(object(), 'gpt-6-luna', CONFIG)
    assert calls == [1, 2, 3]
    assert result == {'runs': 3, 'nodes': sorted(DECLARED), 'targets': sorted(TARGETS), 'stopping_reason': 'all_targets_hit'}
    assert published[-1] == {'coverage': result}


def test_integration_collection_has_only_mandatory_coverage():
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/integration/test_workflow.py'))
    assert [name for name in module if name.startswith('test_')] == ['test_workflow_node_coverage']
    lookup = Mock(side_effect=AssertionError('Unreviewed config must not request provider fixtures'))
    with pytest.raises(ValueError, match='[Nn]o reviewed'):
        module['test_workflow_node_coverage'](SimpleNamespace(getfixturevalue=lookup), SimpleNamespace(values={}))
    lookup.assert_not_called()


def test_configured_reviewed_synthetic_baseline_preflights_before_provider_lookup(monkeypatch):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    assert driver.preflight_baselines(CONFIG) == {'gpt-6-luna': report['max_runs']}


@pytest.mark.parametrize('source', ['providers/loading.py', 'providers/inference.py', 'providers/setup_transport.py',
                                   'testing/setup_backend.py', 'llm_providers/gpt/factory.py'])
def test_sampling_wire_or_factory_change_invalidates_baseline(monkeypatch, source):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    original = Path.read_bytes

    def changed(path):
        body = original(path)
        return body + b'\n# simulated semantic mutation\n' if str(path).endswith(source) else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', CONFIG)


def test_mandatory_integration_validates_before_provider_lookup(monkeypatch):
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/integration/test_workflow.py'))
    events = []
    function = module['test_workflow_node_coverage']
    monkeypatch.setitem(function.__globals__, 'preflight_baselines', lambda config, **kwargs: events.append('preflight'))
    monkeypatch.setitem(function.__globals__, 'baseline_limit', lambda selection, config, **kwargs: events.append('selection admitted'))
    monkeypatch.setitem(function.__globals__, 'node_coverage', lambda provider, selection, config, **kwargs: events.append('coverage'))

    def lookup(name):
        events.append(name)
        return 'synthetic'

    function(SimpleNamespace(getfixturevalue=lookup), SimpleNamespace(values={}))
    assert events == ['preflight', 'llm_selection', 'selection admitted', 'llm_provider', 'coverage']


def test_reported_different_model_rejects_graph_measurement(monkeypatch):
    provider = SimpleNamespace(complete=Mock(return_value=SimpleNamespace(actual_model='different')))

    def build(root, **options):
        async def arun(inputs, *, turn_id):
            await options['llm']('request')
            raise AssertionError('Different model must not reach graph success')
        return SimpleNamespace(arun=arun)

    monkeypatch.setattr(driver, 'build', build)
    monkeypatch.setattr(driver, 'publish', lambda report: None)
    with pytest.raises(ValueError, match='different model'):
        driver.run_graph(provider, 'synthetic', 1, CONFIG)


def test_reported_matching_model_allows_graph_measurement(monkeypatch):
    provider = SimpleNamespace(complete=Mock(return_value=SimpleNamespace(actual_model='synthetic')))

    def build(root, **options):
        async def arun(inputs, *, turn_id):
            assert (await options['llm']('request')).actual_model == 'synthetic'
            return {}
        return SimpleNamespace(arun=arun)

    monkeypatch.setattr(driver, 'build', build)
    monkeypatch.setattr(driver, 'publish', lambda report: None)
    assert driver.run_graph(provider, 'synthetic', 1, CONFIG) == ({}, set())


def test_sampling_import_redirection_changes_fingerprint(tmp_path):
    source = Path(driver.__file__).read_bytes()
    path = tmp_path / 'fixture_driver.py'
    path.write_bytes(source)
    before = driver.sampling_digest(path)
    path.write_bytes(source.replace(b'from .contrived import build', b'from .alternate_fixture import build'))
    assert driver.sampling_digest(path) != before


def test_wrong_factory_same_model_rejects_before_provider_lookup(tmp_path, monkeypatch):
    report = synthetic_current_report()
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    import runpy
    other = tmp_path / 'other.py'
    other.write_text('raise AssertionError("Factory file must never execute during preflight")\n')
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/integration/test_workflow.py'))
    lookup = Mock(side_effect=AssertionError('Wrong factory must not request provider fixtures'))
    with pytest.raises(ValueError, match='replacement provider'):
        module['test_workflow_node_coverage'](SimpleNamespace(getfixturevalue=lookup),
            SimpleNamespace(values=CONFIG | {'LLM_PROVIDER': str(other) + ':factory'}))
    lookup.assert_not_called()


@pytest.mark.parametrize('value', [None, '', 1])
def test_missing_provider_has_no_reviewed_identity(value):
    with pytest.raises(ValueError, match='selected bundled'):
        driver.provider_identity({'LLM_PROVIDER': value})


def test_relative_factory_requires_owner_and_canonical_aliases_share_identity():
    root = Path(__file__).resolve().parents[2]
    relative = {'LLM_PROVIDER': 'src/api_agent/llm_providers/gpt/factory.py:factory'}
    with pytest.raises(ValueError, match='operation owner root'):
        driver.provider_identity(relative)
    canonical = driver.provider_identity(CONFIG)
    assert driver.provider_identity(relative, owner_root=root) == canonical
    alias = root / 'src/api_agent/llm_providers/gpt/../gpt/factory.py'
    assert driver.provider_identity({'LLM_PROVIDER': str(alias) + ':factory'}) == canonical
    assert str(root) not in json.dumps(canonical)


def test_noncanonical_factory_function_rejected():
    with pytest.raises(ValueError, match='replacement provider'):
        driver.provider_identity({'LLM_PROVIDER': FACTORY.replace(':factory', ':other')})


def test_installed_canonical_factory_identity_is_path_independent(tmp_path, monkeypatch):
    package = tmp_path / 'installed/api_agent'
    source = Path(driver.__file__).parent.parent / 'llm_providers/gpt/factory.py'
    installed = package / 'llm_providers/gpt/factory.py'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(source.read_bytes())
    expected = driver.provider_identity(CONFIG)
    monkeypatch.setattr(driver, '__file__', str(package / 'workflows/fixture_driver.py'))
    assert driver.provider_identity({'LLM_PROVIDER': str(installed) + ':factory'}) == expected


def test_source_identity_equivalence_retains_historical_measurements(legacy_report, monkeypatch):
    from api_agent.workflows.sampling import measured_calibration
    report = legacy_report
    source = 'src/api_agent/workflows/execution.py'
    receipt = report['carry_forward_source_identity']
    assert receipt == {
        'kind': 'reviewed_non_sampling_source_identity', 'model': 'gpt-6-luna',
        'route': 'official_subscription_responses', 'reasoning_effort': 'low',
        'source': source,
        'original_source_sha256': '0c4471785859c74724a6ad6a5a3c55c86c5be73c05dacd73735d56d0940b369f',
        'current_source_sha256': 'd67f91945d289673768fa3f0e354365e812f0533e69746b5a0adf56fc7829cd4',
        'previous_fixture_sha256': 'f0451ee275282141695b21d5c97273cf1d745115c8c3cba41f3f744e02f6320b',
        'basis': ('Independent old/new graph, request, output and structural-event equivalence for '
                  'the retained profile; only source-file identity admission changes. Original '
                  'observations and computed bound remain unchanged.'),
        'new_pilot': False}
    assert sha256(json.dumps(report['records'], sort_keys=True, separators=(',', ':')).encode()).hexdigest() == (
        'a09434887a4366296bd5ea3c819a5519a9c12f255d35325c9654168021e0aa2d')
    assert report['trials'] == 20 and report['max_runs'] == 22
    assert report['carry_forward_browser_policy']['prior_receipt_kind'] == report['carry_forward']['kind']
    with pytest.raises(ValueError, match='schema'):
        measured_calibration(report['records'], DECLARED)
    config = CONFIG | {'LLM_CACHE_POLICY': 'medium', 'LLM_REASONING_EFFORT': 'low'}
    assert report['sources'][source] == receipt['current_source_sha256']
    assert driver.fingerprint('gpt-6-luna', config)['sources'] != report['sources']
    # The former receipt cannot qualify the converted graph's source closure.
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    with pytest.raises(ValueError, match='stale'):
        driver.baseline_limit('gpt-6-luna', config)


@pytest.mark.parametrize('source,source_hash', [
    ('src/api_agent/workflows/execution.py', '0c4471785859c74724a6ad6a5a3c55c86c5be73c05dacd73735d56d0940b369f'),
    ('src/api_agent/workflows/execution.py', '0' * 64),
    ('src/api_agent/workflows/layout.py', '0' * 64),
])
def test_source_identity_receipt_cannot_admit_old_or_unrelated_drift(source, source_hash, legacy_report):
    report = deepcopy(legacy_report)
    fingerprint = historical_fingerprint(report)
    with pytest.raises(ValueError, match='schema'):
        validate_calibration(report, DECLARED, fingerprint)
    report['sources'][source] = source_hash
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, fingerprint)


def test_presentation_parser_change_invalidates_saved_sampling(monkeypatch):
    source = 'src/api_agent/workflows/document_format.py'
    report = synthetic_current_report()
    parser_path = Path(driver.__file__).with_name('document_format.py')
    assert report['sources'][source] == sha256(parser_path.read_bytes()).hexdigest()
    original_digest = driver.sampling_digest

    def changed_parser_digest(path):
        if Path(path).name == 'document_format.py':
            return '0' * 64
        return original_digest(path)

    monkeypatch.setattr(driver, 'sampling_digest', changed_parser_digest)
    changed = driver.fingerprint('gpt-6-luna', CONFIG)

    assert changed['sources'][source] != report['sources'][source]
    with pytest.raises(ValueError, match='stale'):
        validate_calibration(report, DECLARED, changed)
