"""Application policy supplies test defaults without route or identity substitution."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing.model_choices import resolved_model, studio_choices, reasoning_choices, probe_reasoning_tokens


@pytest.fixture
def catalog():
    return tuple(AvailableSelection(identifier, family=family, version=version,
        features=('reasoning_effort', 'max_output_tokens'), reasoning_efforts=('low', 'high'), max_output_tokens=8192)
        for identifier, family, version in [('a.1', 'a', '1'), ('a.2', 'a', '2'), ('b.1', 'b', '1')])


@pytest.fixture
def config():
    return {'LLM_DEFAULT_MODEL': 'a[.].+', 'LLM_ALLOWED_MODELS': '.+'}


def test_resolved_default_uses_highest_version_without_mutating_configuration(config, catalog):
    original = dict(config)
    assert resolved_model(config, 'CHOICE', catalog) == 'a.2'
    assert config == original
    assert resolved_model(config | {'CHOICE': 'b.1'}, 'CHOICE', catalog) == 'b.1'


def test_default_resolution_does_not_fall_back_to_unrelated_model(config, catalog):
    with pytest.raises(ProviderAccessError, match='matches no allowed model'):
        resolved_model(config | {'LLM_ALLOWED_MODELS': 'b[.]1'}, 'CHOICE', catalog)


def test_studio_chooses_distinct_allowed_model_and_efforts(config, catalog):
    assert studio_choices(config, catalog) == ('a.2', 'a.1', 'low', 'high')
    assert studio_choices(config | {'LLM_ALLOWED_MODELS': '(?:a[.]2|b[.]1)'}, catalog) == ('a.2', 'b.1', 'low', 'high')


def test_studio_honors_exact_overrides_and_efforts(config, catalog):
    values = config | {'LLM_STUDIO_TEST_DEFAULT_MODEL': 'b.1', 'LLM_STUDIO_TEST_SELECTED_MODEL': 'a.1',
                       'LLM_STUDIO_TEST_DEFAULT_EFFORT': 'high', 'LLM_STUDIO_TEST_SELECTED_EFFORT': 'low'}
    assert studio_choices(values, catalog) == ('b.1', 'a.1', 'high', 'low')


def test_studio_reports_no_eligible_change(config, catalog):
    assert studio_choices(config | {'LLM_ALLOWED_MODELS': 'a[.]2'}, catalog) is None


@pytest.mark.parametrize('change', [
    {'LLM_STUDIO_TEST_SELECTED_MODEL': 'a.2'},
    {'LLM_STUDIO_TEST_DEFAULT_EFFORT': 'low', 'LLM_STUDIO_TEST_SELECTED_EFFORT': 'low'}])
def test_studio_rejects_conflicting_explicit_choices(config, catalog, change):
    with pytest.raises(ValueError, match='distinct'):
        studio_choices(config | change, catalog)


def test_studio_skips_ineligible_alternates(config, catalog):
    narrow = tuple(replace(row, reasoning_efforts=('low',)) if row.selected_llm != 'b.1' else row for row in catalog)
    assert studio_choices(config, narrow) == ('a.2', 'b.1', 'low', 'high')
    assert studio_choices(config | {'LLM_DEFAULT_MODEL': 'a[.]2'}, (replace(catalog[0], family=None), catalog[1], replace(catalog[2], reasoning_efforts=None))) is None


def test_studio_rejects_unsupported_explicit_effort(config, catalog):
    with pytest.raises(ValueError, match='effort'):
        studio_choices(config | {'LLM_STUDIO_TEST_SELECTED_EFFORT': 'medium'}, catalog)


def test_reasoning_uses_default_and_advertised_efforts(config, catalog):
    assert reasoning_choices(config, catalog) == ('a.2', 'low', 'high')
    assert reasoning_choices(config | {'LLM_REASONING_TEST_MODEL': 'b.1'}, catalog) == ('b.1', 'low', 'high')
    off = tuple(replace(row, reasoning_efforts=('off', 'medium')) for row in catalog)
    assert reasoning_choices(config, off) == ('a.2', 'off', 'medium')
    assert reasoning_choices(config, tuple(replace(row, reasoning_efforts=('high',)) for row in catalog)) is None
    assert reasoning_choices(config, tuple(replace(row, features=()) for row in catalog)) is None


def test_reasoning_preserves_explicit_supported_pair(config, catalog):
    assert reasoning_choices(config | {'LLM_REASONING_TEST_LOW_EFFORT': 'low', 'LLM_REASONING_TEST_HIGH_EFFORT': 'high'}, catalog) == ('a.2', 'low', 'high')


@pytest.mark.parametrize('count,expected', [(0, True), (12, True), (None, False)])
@pytest.mark.parametrize('maximum', [None, 128, 8192])
def test_probe_uses_normal_owner_and_bounded_exact_request(catalog, count, expected, maximum):
    result = LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(10, 20, reasoning_tokens=count), actual_model='a.2')
    provider = SimpleNamespace(complete=Mock(return_value=result))
    row = replace(catalog[1], max_output_tokens=maximum)
    assert probe_reasoning_tokens(provider, row, 'low') is expected
    sent = provider.complete.call_args.args[0]
    assert sent.selected_llm == 'a.2'
    assert sent.payload.generation.reasoning_effort == 'low'
    assert sent.payload.generation.max_output_tokens == min(maximum or 4096, 4096)
    assert sent.request_id


def test_probe_without_usage_or_output_control_does_not_invent_counters(catalog):
    provider = SimpleNamespace(complete=Mock(return_value=LLMResult(Message('assistant', (TextContent('323'),)))))
    assert not probe_reasoning_tokens(provider, replace(catalog[1], features=()), 'low')
    assert provider.complete.call_args.args[0].payload.generation.max_output_tokens is None


@pytest.mark.parametrize('edit,message', [({'outcome': 'length'}, 'did not complete'), ({'actual_model': 'other'}, 'different model')])
def test_probe_rejects_incomplete_or_wrong_model_response(catalog, edit, message):
    result = replace(LLMResult(Message('assistant', (TextContent('323'),))), **edit)
    with pytest.raises(ValueError, match=message):
        probe_reasoning_tokens(SimpleNamespace(complete=lambda _: result), catalog[1], 'low')


def test_probe_propagates_provider_failure_without_retry(catalog):
    complete = Mock(side_effect=ProviderAccessError('authorization', 'Denied'))
    with pytest.raises(ProviderAccessError, match='Denied'):
        probe_reasoning_tokens(SimpleNamespace(complete=complete), catalog[1], 'low')
    assert complete.call_count == 1


def integration_module(name):
    import importlib.util
    from pathlib import Path
    import sys
    root = Path(__file__).parents[1] / 'integration'
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location('catalog_' + name, root / (name + '.py'))
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.remove(str(root))


def test_studio_fixture_resolves_after_prepared_catalog(config, catalog):
    from api_agent.testing.provider_setup import IntegrationRequirements
    module = integration_module('test_studio_selection')
    configuration = module.require_selection_configuration(IntegrationRequirements(config))
    assert configuration == config and configuration is not config
    observed = module.selection_catalog.__wrapped__(configuration, SimpleNamespace(describe=lambda: catalog))
    assert observed.choices == ('a.2', 'a.1', 'low', 'high')


def test_studio_fixture_skips_only_when_catalog_lacks_alternate(config, catalog):
    module = integration_module('test_studio_selection')
    with pytest.raises(pytest.skip.Exception, match='distinct allowed model'):
        module.selection_catalog.__wrapped__(config | {'LLM_ALLOWED_MODELS': 'a[.]2'}, SimpleNamespace(describe=lambda: catalog))


@pytest.mark.parametrize('reporting', [None, True])
def test_reasoning_fixture_resolves_and_probes_only_unqualified_counters(config, catalog, reporting):
    from api_agent.testing.provider_setup import IntegrationRequirements
    values = config | {'LLM_REASONING_TEST_TOKEN_REPORTING': reporting}
    result = LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(10, 20, reasoning_tokens=4), actual_model='a.2')
    provider = SimpleNamespace(describe=lambda: catalog, complete=Mock(return_value=result))
    module = integration_module('test_reasoning_contrast')
    request = SimpleNamespace(getfixturevalue=lambda name: provider)
    assert module.reasoning_ready.__wrapped__(IntegrationRequirements(values), request) == ('a.2', ('low', 'high'), None, 150)
    assert provider.complete.call_count == (1 if reporting is None else 0)
    assert values == config | {'LLM_REASONING_TEST_TOKEN_REPORTING': reporting}


def test_explicit_false_prevents_reasoning_probe(config):
    from api_agent.testing.provider_setup import IntegrationRequirements
    request = SimpleNamespace(getfixturevalue=Mock(side_effect=AssertionError('No provider acquisition')))
    with pytest.raises(pytest.skip.Exception, match='qualified for reasoning-token reporting'):
        integration_module('test_reasoning_contrast').reasoning_ready.__wrapped__(
            IntegrationRequirements(config | {'LLM_REASONING_TEST_TOKEN_REPORTING': False}), request)
    request.getfixturevalue.assert_not_called()


@pytest.mark.parametrize('variation,message', [('efforts', 'two supported'), ('counters', 'separate reasoning-token')])
def test_reasoning_auto_preflight_explains_capability_skip(config, catalog, variation, message):
    from api_agent.testing.provider_setup import IntegrationRequirements
    rows = tuple(replace(row, reasoning_efforts=('low',)) for row in catalog) if variation == 'efforts' else catalog
    provider = SimpleNamespace(describe=lambda: rows, complete=Mock(return_value=LLMResult(Message('assistant', (TextContent('323'),)))))
    with pytest.raises(pytest.skip.Exception, match=message):
        integration_module('test_reasoning_contrast').reasoning_ready.__wrapped__(
            IntegrationRequirements(config), SimpleNamespace(getfixturevalue=lambda name: provider))
    assert provider.complete.call_count == (0 if variation == 'efforts' else 1)


def test_workflow_default_passes_resolved_identity_to_real_driver(monkeypatch, config, catalog):
    from api_agent.workflows import fixture_driver
    calls = []
    monkeypatch.setattr(fixture_driver, 'coverage_preflight', lambda values: None)
    monkeypatch.setattr(fixture_driver, 'node_coverage', lambda owner, model, values, **kwargs: calls.append((model, values)))
    provider = SimpleNamespace(describe=lambda: catalog)
    integration_module('test_workflow').test_workflow_node_coverage(
        SimpleNamespace(getfixturevalue=lambda name: provider), SimpleNamespace(values=config))
    assert calls == [('a.2', config | {'LLM_WORKFLOW_TEST_MODEL': 'a.2'})]
    assert 'LLM_WORKFLOW_TEST_MODEL' not in config


@pytest.mark.parametrize('low,high', [('high', 'low'), ('low', 'low')])
def test_reasoning_rejects_non_increasing_pair_before_inference(config, catalog, low, high):
    with pytest.raises(ValueError, match='two distinct'):
        reasoning_choices(config | {'LLM_REASONING_TEST_LOW_EFFORT': low, 'LLM_REASONING_TEST_HIGH_EFFORT': high}, catalog)


def test_reasoning_off_low_pair_remains_available(config, catalog):
    rows = tuple(replace(row, reasoning_efforts=('off', 'low')) for row in catalog)
    assert reasoning_choices(config, rows) == ('a.2', 'off', 'low')


@pytest.mark.parametrize('key,value', [('LLM_STUDIO_TEST_DEFAULT_EFFORT', 'medium'), ('LLM_STUDIO_TEST_SELECTED_EFFORT', 'medium')])
def test_explicit_invalid_studio_effort_does_not_become_capability_skip(config, catalog, key, value):
    with pytest.raises(ValueError, match='effort'):
        studio_choices(config | {'LLM_STUDIO_TEST_SELECTED_MODEL': 'a.1', key: value}, catalog)


@pytest.mark.parametrize('operation', ['acquire', 'describe', 'complete'])
def test_automatic_reasoning_runtime_skips_remain_failures(config, catalog, operation):
    from api_agent.testing.provider_setup import IntegrationRequirements
    provider = SimpleNamespace(describe=Mock(return_value=catalog), complete=Mock())
    lookup = Mock(return_value=provider)
    target = lookup if operation == 'acquire' else getattr(provider, operation)
    target.side_effect = pytest.skip.Exception('runtime unavailable')
    with pytest.raises(AssertionError, match='setup was unavailable') as caught:
        integration_module('test_reasoning_contrast').reasoning_ready.__wrapped__(
            IntegrationRequirements(config), SimpleNamespace(getfixturevalue=lookup))
    assert isinstance(caught.value.__cause__, pytest.skip.Exception)


def test_pytest_injects_request_for_automatic_reasoning_preflight(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).parents[2]
    case = tmp_path / 'test_automatic_reasoning.py'
    case.write_text('''from test_reasoning_contrast import reasoning_ready

def test_choices(reasoning_ready):
    assert reasoning_ready == ('model', ('low', 'high'), None, 150)
''')
    receipt = tmp_path / 'events.json'
    script = '''
import json, pathlib, pytest, sys
from types import SimpleNamespace
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing.provider_setup import IntegrationRequirements
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'tests/integration'))
events = []
class Fixtures:
    @pytest.fixture
    def integration_config(self):
        return IntegrationRequirements({'LLM_DEFAULT_MODEL': 'model'})
    @pytest.fixture
    def llm_provider(self):
        def complete(request):
            events.append('probe')
            return LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(1, 10, reasoning_tokens=2))
        return SimpleNamespace(describe=lambda: (AvailableSelection('model', features=('reasoning_effort',), reasoning_efforts=('low', 'high')),), complete=complete)
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.skipped:
            events.append(report.outcome)
code = pytest.main([sys.argv[2], '-q', '-o', 'addopts='], plugins=[Fixtures()])
pathlib.Path(sys.argv[3]).write_text(json.dumps(events))
raise SystemExit(code)
'''
    completed = subprocess.run([sys.executable, '-c', script, str(root), str(case), str(receipt)],
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'), capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(receipt.read_text()) == ['probe', 'passed']


@pytest.mark.parametrize('limit,expected', [(64, 64), (4096, 4096), (8192, 4096)])
def test_probe_honors_explicit_output_cap(catalog, limit, expected):
    provider = SimpleNamespace(complete=Mock(return_value=LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(1, 10, reasoning_tokens=2))))
    assert probe_reasoning_tokens(provider, catalog[1], 'low', max_output_tokens=limit)
    assert provider.complete.call_args.args[0].payload.generation.max_output_tokens == expected


@pytest.mark.parametrize('row_change,limit,message', [({'max_output_tokens': 128}, 256, 'exceeds'), ({'features': ()}, 64, 'requires')])
def test_invalid_probe_output_cap_rejects_before_inference(catalog, row_change, limit, message):
    complete = Mock()
    with pytest.raises(ValueError, match=message):
        probe_reasoning_tokens(SimpleNamespace(complete=complete), replace(catalog[1], **row_change), 'low', max_output_tokens=limit)
    complete.assert_not_called()


def test_probe_retains_counter_availability_in_allowlisted_evidence(tmp_path, monkeypatch, catalog):
    import json
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'probe-test (setup)')
    provider = SimpleNamespace(complete=Mock(return_value=LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(1, 10, reasoning_tokens=2))))
    assert probe_reasoning_tokens(provider, catalog[1], 'low')
    data = json.loads(path.read_text())['data']
    assert data == {'kind': 'reasoning_token_probe', 'accepted': True}
    assert '323' not in path.read_text()


def test_counter_probe_admits_test_authority_before_remote_handoff(config, catalog):
    from api_agent.testing.authority_scope import authority_scope, current_scope
    from api_agent.testing.provider_setup import IntegrationRequirements
    events = []
    def complete(request):
        lease = current_scope().freeze(credential_only=True)
        try:
            events.append((request.selected_llm, lease.secrets))
        finally:
            lease.release()
        return LLMResult(Message('assistant', (TextContent('323'),)), usage=Usage(1, 10, reasoning_tokens=2))
    provider = SimpleNamespace(config={'LOG_CONTENT': False}, describe=lambda: catalog, complete=complete)
    with authority_scope(('ui',)) as scope:
        result = integration_module('test_reasoning_contrast').reasoning_ready.__wrapped__(
            IntegrationRequirements(config), SimpleNamespace(getfixturevalue=lambda name: provider))
        assert not scope.authorities()[0].policy.allow_content
    assert result[:2] == ('a.2', ('low', 'high'))
    assert events == [('a.2', ())]


def test_studio_prefers_another_version_in_default_family_before_other_families(config, catalog):
    foreign = replace(catalog[2], selected_llm='0.foreign')
    rows = (foreign, *catalog)
    assert studio_choices(config, rows) == ('a.2', 'a.1', 'low', 'high')
    assert studio_choices(config | {'LLM_ALLOWED_MODELS': '(?:a[.]2|0[.]foreign)'}, rows) == ('a.2', '0.foreign', 'low', 'high')
