"""Reviewed baseline validation and explicit pilot never require live inference."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.workflows import fixture_driver as driver
from api_agent.workflows.sampling import DECLARED, FIRST

FACTORY = str(Path(driver.__file__).parent.parent / 'llm_providers/gpt/factory.py') + ':factory'
CONFIG = {'LLM_PROVIDER': FACTORY, 'GPT_AUTH_MODE': 'subscription', 'GPT_API_MODE': 'responses',
          'LLM_CACHE_POLICY': None, 'WORKFLOW_LLM_TIMEOUT_SECONDS': 120.0,
          'LLM_DEFAULT_MODEL': '^gpt-6-luna$'}


def test_reviewed_baseline_is_recomputed_without_local_calibration_file():
    report = driver._baselines()[0]
    maximum = driver.baseline_limit('gpt-6-luna', CONFIG)
    assert maximum == report['max_runs']
    assert driver.preflight_baselines(CONFIG) == {'gpt-6-luna': maximum}
    assert report['trials'] == 20
    assert sum(report['routes'].values()) == 20
    assert 'receipt' not in report
    assert report['hits']['second_draft'] == report['routes']['next_a'] + report['routes']['next_b']


@pytest.mark.parametrize('setting,value', [('GPT_API_MODE', 'completions'),
    ('LLM_CACHE_POLICY', 'off'), ('GPT_AUTH_MODE', 'api_key'),
    ('WORKFLOW_LLM_TIMEOUT_SECONDS', 30), ('WORKFLOW_GRAPH_TIMEOUT_SECONDS', 5)])
def test_stale_settings_reject_before_graph_execution(monkeypatch, setting, value):
    report = deepcopy(driver._baselines()[0])
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    graph = Mock(side_effect=AssertionError('Stale sampling must not execute'))
    monkeypatch.setattr(driver, 'run_graph', graph)
    with pytest.raises(ValueError, match='stale'):
        driver.node_coverage(object(), 'gpt-6-luna', CONFIG | {setting: value})
    graph.assert_not_called()


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
    assert (driver.fixture_root() / 'demo/contrived/layout.md').samefile(
        Path(__file__).resolve().parents[2] / 'demo/contrived/layout.md')


def test_reporting_code_is_excluded_but_sampling_changes_invalidate(monkeypatch):
    report = deepcopy(driver._baselines()[0])
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
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

        async def arun(inputs):
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
        async def arun(inputs):
            options['observe']({'event': 'node', 'node': 'first_draft'})
            return {}
        return SimpleNamespace(arun=arun)

    monkeypatch.setattr(driver, 'build', build)
    monkeypatch.setattr(driver, 'publish', lambda report: None)
    assert driver.run_graph(object(), 'synthetic', 1, CONFIG) == ({}, {'first_draft'})


def test_node_coverage_stops_when_all_nodes_hit(monkeypatch):
    calls, published = [], []
    synthetic_report = deepcopy(driver._baselines()[0])
    synthetic_report.update(driver.fingerprint('gpt-6-luna', CONFIG))
    monkeypatch.setattr(driver, '_baselines', lambda: [synthetic_report])

    def graph(provider, selection, trial, config, **options):
        calls.append(trial)
        return {}, FIRST if trial == 1 else DECLARED

    monkeypatch.setattr(driver, 'run_graph', graph)
    monkeypatch.setattr(driver, 'publish', published.append)
    result = driver.node_coverage(object(), 'gpt-6-luna', CONFIG)
    assert calls == [1, 2]
    assert result == {'runs': 2, 'nodes': sorted(DECLARED), 'stopping_reason': 'all_nodes_hit'}
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
    report = deepcopy(driver._baselines()[0])
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
    monkeypatch.setattr(driver, '_baselines', lambda: [report])
    assert driver.preflight_baselines(CONFIG) == {'gpt-6-luna': report['max_runs']}


@pytest.mark.parametrize('source', ['providers/loading.py', 'providers/inference.py', 'providers/setup_transport.py',
                                   'testing/setup_backend.py', 'llm_providers/gpt/factory.py'])
def test_sampling_wire_or_factory_change_invalidates_baseline(monkeypatch, source):
    report = deepcopy(driver._baselines()[0])
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
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
        async def arun(inputs):
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
        async def arun(inputs):
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
    report = deepcopy(driver._baselines()[0])
    report.update(driver.fingerprint('gpt-6-luna', CONFIG))
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
