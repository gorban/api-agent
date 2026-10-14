import asyncio
from copy import deepcopy
import pytest
from pathlib import Path

FACTORY_CONFIG = {'LLM_PROVIDER': str(Path(__file__).resolve().parents[2] / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}
from api_agent.workflows.sampling import calibration, cover, lower_probability, validate_calibration, measured_calibration, DECLARED


def test_exact_all_success_bound():
    assert lower_probability(10, 10, .05) == pytest.approx(.05 ** .1)
    assert lower_probability(0, 10, .05) == 0


@pytest.mark.parametrize('args', [(True, 2, .05), (3, 2, .05), (0, 0, .05), (0, 2, 1)])
def test_invalid_observations(args):
    with pytest.raises(ValueError):
        lower_probability(*args)


def test_calibration_recomputes_claims():
    report = measured_calibration([{'trial':trial,'route':'next_a','nodes':sorted(DECLARED)} for trial in range(1,61)], DECLARED)
    report['model'] = 'synthetic'
    assert validate_calibration(report, DECLARED, {'model': 'synthetic'}) == report['max_runs']


@pytest.mark.parametrize('key,value', [('bounds', {'a': 1}), ('max_runs', 1), ('missing_node_bound', 0), ('hits', {'a': 60}), ('trials', True), ('model', 'stale')])
def test_tampered_calibration_rejected(key, value):
    report = measured_calibration([{'trial':trial,'route':'next_a','nodes':sorted(DECLARED)} for trial in range(1,61)], DECLARED)
    report['model'] = 'synthetic'
    report[key] = value
    with pytest.raises(ValueError):
        validate_calibration(report, DECLARED, {'model': 'synthetic'})


@pytest.mark.parametrize('observations,required,ceiling', [([], {'a'}, 60), ([{'a'}], set(), 60), ([{'b'}], {'a'}, 60), ([{'a'}]*61, {'a'}, 60), ([{'a'}], {'a'}, 61), ([set()]*60, {'a'}, 60)])
def test_invalid_or_insufficient_pilot(observations, required, ceiling):
    with pytest.raises(ValueError):
        calibration(observations, required, ceiling=ceiling)


def test_coverage_stops_on_first_complete_graph():
    calls, events = [], []
    async def run(trial):
        calls.append(trial)
        return trial, {'a'} if trial == 1 else {'b'}
    summary, result = asyncio.run(cover(run, {'a', 'b'}, 60, events.append))
    assert calls == [1, 2]
    assert result == 2
    assert summary['stopping_reason'] == 'all_nodes_hit'
    assert [event['complete'] for event in events] == [False, True]


def test_coverage_fails_at_cap():
    async def run(trial):
        return None, {'a'}
    with pytest.raises(AssertionError):
        asyncio.run(cover(run, {'a', 'b'}, 2))


@pytest.mark.parametrize('required,maximum', [(set(), 2), ({'a'}, 0), ({'a'}, 61)])
def test_invalid_coverage(required, maximum):
    with pytest.raises(ValueError):
        asyncio.run(cover(None, required, maximum))


def test_unknown_coverage_node():
    async def run(trial):
        return None, {'unknown'}
    with pytest.raises(ValueError):
        asyncio.run(cover(run, {'a'}, 1))


@pytest.mark.parametrize('records', [None, [], [{'trial':True,'route':'next_a','nodes':sorted(DECLARED)}], [{'trial':1,'route':'skip','nodes':sorted(DECLARED)}], [{'trial':1,'route':'unknown','nodes':[]}], [{'trial':1,'route':'next_a','nodes':[1]}], [{'trial':1,'route':'next_a','nodes':tuple(sorted(DECLARED))}]])
def test_measurements_reject_inconsistent_route_or_trials(records):
    with pytest.raises(ValueError):
        measured_calibration(records, DECLARED)


def test_measurements_derive_routes_and_hits():
    records=[{'trial':trial,'route':'skip' if trial <= 20 else 'next_a','nodes':sorted(DECLARED if trial > 20 else {'first_draft','first_guard','first_format'})} for trial in range(1,61)]
    report=measured_calibration(records,DECLARED)
    assert report['routes']=={'skip':20,'next_a':40,'next_b':0}
    assert report['hits']['second_guard']==40
    report['routes']={'skip':60}
    with pytest.raises(ValueError):
        validate_calibration(report,DECLARED,{})


@pytest.fixture
def workflow_driver():
    import runpy
    from pathlib import Path
    return runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/integration/test_workflow.py'))


@pytest.mark.parametrize('setting,value', [('GPT_AUTH_MODE','api_key'),('GPT_BASE_URL','https://example.invalid/v1'),('GPT_API_MODE','completions'),('LLM_CACHE_POLICY','off')])
def test_fingerprint_changes_with_backend_settings(workflow_driver,setting,value):
    fingerprint=workflow_driver['fingerprint']
    assert fingerprint('synthetic',FACTORY_CONFIG) != fingerprint('synthetic',FACTORY_CONFIG | {setting:value})


def test_fingerprint_binds_user_input_and_driver(workflow_driver,monkeypatch):
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    monkeypatch.setitem(fingerprint.__globals__,'USER_INPUT','different synthetic input')
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['user_input_hash'] != after['user_input_hash']
    assert 'src/api_agent/workflows/fixture_driver.py' in before['sources']


def test_fingerprint_binds_provider_inference_code(workflow_driver,monkeypatch):
    from pathlib import Path
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    source='src/api_agent/llm_providers/gpt/inference.py'
    original=Path.read_bytes
    def changed(path):
        return original(path).replace(b'_LIMIT = 1048576', b'_LIMIT = 1048577') if str(path).endswith(source) else original(path)
    monkeypatch.setattr(Path,'read_bytes',changed)
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['sources'][source] != after['sources'][source]


def test_all_skip_records_cannot_claim_second_component_success():
    records=[{'trial':trial,'route':'skip','nodes':sorted(DECLARED)} for trial in range(1,61)]
    with pytest.raises(ValueError,match='route disagrees'):
        measured_calibration(records,DECLARED)


def test_fingerprint_changes_when_installed_dependency_changes(workflow_driver,monkeypatch):
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    original=fingerprint.__globals__['version']
    monkeypatch.setitem(fingerprint.__globals__,'version',lambda name: '999.0' if name=='langgraph' else original(name))
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['dependency_versions']['langgraph'] != after['dependency_versions']['langgraph']
    report=measured_calibration([{'trial':trial,'route':'next_a','nodes':sorted(DECLARED)} for trial in range(1,61)],DECLARED)
    report.update(before)
    with pytest.raises(ValueError,match='stale'):
        validate_calibration(report,DECLARED,after)


@pytest.mark.parametrize('successful_second', [10, 15, 20])
def test_calibrated_cap_adds_four_runs_after_exact_minimum(successful_second):
    records = [{'trial': trial, 'route': 'next_a' if trial <= successful_second else 'skip',
                'nodes': sorted(DECLARED if trial <= successful_second else
                                {'first_draft', 'first_guard', 'first_format'})}
               for trial in range(1, 21)]
    report = measured_calibration(records, DECLARED)
    minimum = report['minimum_runs']
    bounds = report['bounds'].values()
    assert report['safety_margin'] == 4 and report['max_runs'] == minimum + 4
    assert report['minimum_missing_node_bound'] == sum((1 - probability) ** minimum for probability in bounds)
    assert report['minimum_missing_node_bound'] <= .001
    assert sum((1 - probability) ** (minimum - 1) for probability in bounds) > .001
    assert report['missing_node_bound'] == sum((1 - probability) ** report['max_runs'] for probability in bounds)
    assert report['missing_node_bound'] < report['minimum_missing_node_bound']
    assert validate_calibration(report, DECLARED, {}) == minimum + 4


@pytest.mark.parametrize('ceiling, minimum', [(13, 10), (60, 57)])
def test_required_margin_above_operating_ceiling_rejects_without_clamping(monkeypatch, ceiling, minimum):
    from api_agent.workflows import sampling
    probability = 1 - .0009 ** (1 / minimum)
    monkeypatch.setattr(sampling, 'lower_probability', lambda *args: probability)
    assert (1 - probability) ** minimum <= .001
    assert (1 - probability) ** (minimum - 1) > .001
    with pytest.raises(ValueError, match='safety margin exceeds'):
        calibration([{'node'}], {'node'}, ceiling=ceiling)


@pytest.mark.parametrize('key, value', [('minimum_runs', 1), ('safety_margin', 0), ('minimum_missing_node_bound', 0.0)])
def test_buffer_policy_claims_are_recomputed_before_acceptance(key, value):
    records = [{'trial': trial, 'route': 'next_a', 'nodes': sorted(DECLARED)} for trial in range(1, 21)]
    report = measured_calibration(records, DECLARED)
    report[key] = value
    with pytest.raises(ValueError, match='claims do not match'):
        validate_calibration(report, DECLARED, {})
