"""Normal fixture coverage selects real UUID cases before any inference."""
import asyncio
import json
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
from api_agent.workflows import fixture_driver as driver
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.sampling import DETERMINISTIC_CASES, coverage_case, case_targets
from test_workflow_fixture_driver import structural_events

ROOT = Path(__file__).resolve().parents[2]


def seed(route, first, second=True):
    last = {'skip': '0', 'next_a': '5', 'next_b': 'a'}[route]
    return '00000000-0000-4000-8000-000000000' + ('0' if second else '8') + ('0' if first else '8') + last


SEEDS = tuple(seed(route, first, second is not False) for route, first, second in sorted(
    DETERMINISTIC_CASES, key=repr))


class Provider:
    def __init__(self):
        self.requests = []

    def describe(self):
        return (AvailableSelection('replacement-model'),)

    def complete(self, request):
        self.requests.append(request)
        tail = json.loads(request.payload.messages[-1].content[0].text)
        text = request.payload.messages[1].content[0].text
        value = {'original_input': text, 'final_output': text,
                 'route': tail['decisions']['route'], 'ready': tail['decisions']['ready']}
        return LLMResult(Message('assistant', (TextContent(json.dumps(value)),)), actual_model='replacement-model')


def selected_draws(monkeypatch, values):
    """Only the selection thread consumes injected draws; graph IDs remain fresh."""
    pending, consumed = iter(values), []
    caller = threading.get_ident()

    def draw():
        if threading.get_ident() != caller:
            return uuid4()
        value = next(pending)
        consumed.append(value)
        return value

    monkeypatch.setattr(driver, 'uuid4', draw)
    return consumed


def test_reachable_cases_ignore_unreachable_skip_second_readiness():
    assert len(DETERMINISTIC_CASES) == 10
    for first in (False, True):
        assert coverage_case(seed('skip', first, False)) == coverage_case(seed('skip', first, True))
        assert coverage_case(seed('skip', first)) == ('skip', first, None)
    assert {coverage_case(value) for value in SEEDS} == DETERMINISTIC_CASES


@pytest.mark.parametrize('value', ['00000000-0000-1000-8000-000000000005',
                                  '00000000-0000-4000-8000-00000000000A'])
def test_coverage_case_rejects_non_v4_or_noncanonical_draw(value):
    with pytest.raises((ValueError, WorkflowError)):
        coverage_case(value)


def test_duplicate_draws_make_no_graph_and_chosen_uuid_reaches_real_prompts(monkeypatch):
    provider, reports = Provider(), []
    consumed = selected_draws(monkeypatch, [SEEDS[0], SEEDS[0], *SEEDS[1:]])
    monkeypatch.setattr(driver, 'publish', reports.append)
    forbidden = lambda *a, **k: pytest.fail('Normal coverage must not inspect calibration ownership or bounds')
    for name in ('provider_identity', 'calibration_profile', 'baseline_limit', 'preflight_baselines'):
        monkeypatch.setattr(driver, name, forbidden)
    from api_agent.workflows import sampling
    monkeypatch.setattr(sampling, 'coverage_ceiling', forbidden)

    result = driver.node_coverage(provider, 'replacement-model',
        {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': 1024, 'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': 10})

    assert result['runs'] == 10
    assert result['stopping_reason'] == 'all_cases_hit'
    assert len(consumed) == 11
    assert {row['trial_uuid'] for row in result['records']} == set(SEEDS)
    assert len(provider.requests) == 27  # This synthetic provider always honors the complete prompt.
    assert {json.loads(request.payload.messages[-1].content[0].text)['trial']
            for request in provider.requests} == set(SEEDS)
    assert all(request.payload.generation.max_output_tokens == 1024 for request in provider.requests)
    assert set(result['targets']) == driver.TARGETS
    assert reports[-1] == {'coverage': result}


def test_graph_failure_is_not_retried_with_another_draw(monkeypatch):
    consumed = selected_draws(monkeypatch, SEEDS)
    def fail(*args, **kwargs):
        raise RuntimeError('genuine graph failure')
    monkeypatch.setattr(driver, 'run_graph', fail)
    with pytest.raises(RuntimeError, match='genuine graph failure'):
        driver.node_coverage(object(), 'replacement-model', {})
    assert consumed == [SEEDS[0]]


def test_duplicate_stream_expires_without_inference_after_first_graph(monkeypatch):
    def duplicate():
        return SEEDS[0]
    monkeypatch.setattr(driver, 'uuid4', duplicate)
    calls = []
    def graph(provider, selection, trial, config, **options):
        calls.append(options['trial_uuid'])
        route = coverage_case(options['trial_uuid'])[0]
        for event in structural_events(trial, route):
            options['observe'](event)
        return {'first_draft': {'route': route}}, driver.DECLARED if route != 'skip' else driver.DECLARED & {
            'first_draft', 'first_format', 'first_guard'}
    monkeypatch.setattr(driver, 'run_graph', graph)
    with pytest.raises(TimeoutError):
        driver.node_coverage(object(), 'replacement-model', {'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': .02})
    assert calls == [SEEDS[0]]


@pytest.mark.parametrize('config', [{'WORKFLOW_MAX_PASSES': 1},
    {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': True}, {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': 0},
    {'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS': 1048577}])
def test_impossible_or_invalid_controls_reject_before_provider_fixture(config):
    module = runpy.run_path(str(ROOT / 'tests/integration/test_workflow.py'))
    def fixture(name):
        pytest.fail('Invalid controls must fail before setup')
    with pytest.raises(ValueError):
        module['test_workflow_node_coverage'](SimpleNamespace(getfixturevalue=fixture),
            SimpleNamespace(values={'LLM_WORKFLOW_TEST_MODEL': 'replacement-model', **config}))


@pytest.mark.parametrize('case', [('skip', False, None), ('next_a', True, False)])
def test_missing_required_correction_does_not_cover_case(case):
    from api_agent.workflows.sampling import measurement, FIRST, DECLARED
    row = measurement(1, {'first_draft': {'route': case[0]}},
                      FIRST if case[0] == 'skip' else DECLARED,
                      structural_events(1, case[0], retry=False))
    with pytest.raises(AssertionError, match='did not execute its correction'):
        case_targets(row, case)


def test_forged_output_route_does_not_cover_selected_case():
    from api_agent.workflows.sampling import measurement, FIRST
    row = measurement(1, {'first_draft': {'route': 'skip'}}, FIRST, structural_events(1, 'skip'))
    with pytest.raises(AssertionError, match='route differs'):
        case_targets(row, ('next_b', False, False))


def fake_successful_graph(provider, selection, trial, config, **options):
    route = coverage_case(options['trial_uuid'])[0]
    for event in structural_events(trial, route):
        options['observe'](event)
    nodes = driver.DECLARED if route != 'skip' else {'first_draft', 'first_format', 'first_guard'}
    return {'first_draft': {'route': route}}, nodes


def test_reused_scope_identity_is_not_independent_graph_proof(monkeypatch):
    selected_draws(monkeypatch, SEEDS)
    def reused(provider, selection, trial, config, **options):
        return fake_successful_graph(provider, selection, 1, config, **options)
    monkeypatch.setattr(driver, 'run_graph', reused)
    with pytest.raises(AssertionError, match='reuse executed scope'):
        driver.node_coverage(object(), 'replacement-model', {})


def test_all_cases_do_not_replace_aggregate_actual_target_proof(monkeypatch):
    selected_draws(monkeypatch, SEEDS)
    monkeypatch.setattr(driver, 'run_graph', fake_successful_graph)
    original = driver.case_targets
    monkeypatch.setattr(driver, 'case_targets', lambda row, case: original(row, case) - {'next_b'})
    with pytest.raises(AssertionError, match='all workflow targets'):
        driver.node_coverage(object(), 'replacement-model', {})


def test_optional_upstream_model_metadata_and_empty_cap_remain_generic():
    from dataclasses import replace
    provider = Provider()
    complete = provider.complete
    provider.complete = lambda request: replace(complete(request), actual_model=None)
    _, nodes = driver.run_graph(provider, 'replacement-model', 1, {},
                                trial_uuid=seed('skip', True))
    assert nodes == {'first_draft', 'first_format', 'first_guard'}
    assert len(provider.requests) == 1
    assert provider.requests[0].payload.generation.max_output_tokens is None


@pytest.mark.parametrize('value', [seed('next_b', True), 'not-a-uuid',
    '00000000-0000-1000-8000-000000000000', '00000000-0000-4000-8000-00000000000A'])
def test_coverage_seed_evidence_is_canonical_v4_metadata_without_prompt_content(value):
    from api_agent.workflows.evidence import sanitize
    record = {'trial_uuid': value, 'route': 'next_b', 'prompt': 'synthetic-private-prompt'}
    if value == seed('next_b', True):
        assert sanitize(record) == {'trial_uuid': value, 'route': 'next_b'}
    else:
        with pytest.raises((ValueError, WorkflowError)):
            sanitize(record)


def test_logically_expired_selection_budget_rejects_before_another_draw(monkeypatch):
    from api_agent.providers import progress
    clock, draws = [0.0], []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    def draw():
        assert not draws, 'No UUID draw is permitted after the parent deadline'
        draws.append(SEEDS[0])
        return SEEDS[0]
    original = driver.case_targets
    class Targets:
        def __init__(self, values):
            self.values = values
        def __iter__(self):
            clock[0] = 2  # Synchronous aggregation consumes the remaining budget.
            return iter(self.values)
    monkeypatch.setattr(driver, 'case_targets', lambda row, case: Targets(original(row, case)))
    monkeypatch.setattr(driver, 'uuid4', draw)
    monkeypatch.setattr(driver, 'run_graph', fake_successful_graph)
    with pytest.raises(TimeoutError, match='during UUID selection'):
        driver.node_coverage(object(), 'replacement-model', {'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': 1})
    assert draws == [SEEDS[0]]


def test_graph_deadline_drains_owned_work_before_reporting_timeout(monkeypatch):
    import time
    finished = threading.Event()
    def graph(*args, **kwargs):
        try:
            time.sleep(.05)
            raise RuntimeError('late graph failure after deadline')
        finally:
            finished.set()
    monkeypatch.setattr(driver, 'run_graph', graph)
    with pytest.raises(TimeoutError):
        driver.node_coverage(object(), 'replacement-model', {'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': .01})
    assert finished.is_set()


def test_final_synchronous_graph_proof_cannot_report_late_success(monkeypatch):
    from api_agent.providers import progress
    clock, validated, published = [0.0], [], []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    selected_draws(monkeypatch, SEEDS)
    monkeypatch.setattr(driver, 'run_graph', fake_successful_graph)
    monkeypatch.setattr(driver, 'publish', published.append)
    original = driver.case_targets
    def validate(row, case):
        targets = original(row, case)
        validated.append(case)
        if len(validated) == 10:
            clock[0] = 2
        return targets
    monkeypatch.setattr(driver, 'case_targets', validate)
    with pytest.raises(TimeoutError, match='after graph proof'):
        driver.node_coverage(object(), 'replacement-model', {'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': 1})
    assert len(validated) == 10
    assert not published


def test_final_synchronous_aggregate_check_cannot_report_late_success(monkeypatch):
    from api_agent.providers import progress
    clock, published = [0.0], []
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    selected_draws(monkeypatch, SEEDS)
    monkeypatch.setattr(driver, 'run_graph', fake_successful_graph)
    monkeypatch.setattr(driver, 'publish', published.append)
    class Targets(frozenset):
        def __le__(self, other):
            result = super().__le__(other)
            clock[0] = 2
            return result
    monkeypatch.setattr(driver, 'TARGETS', Targets(driver.TARGETS))
    with pytest.raises(TimeoutError, match='before coverage result'):
        driver.node_coverage(object(), 'replacement-model', {'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': 1})
    assert not published


def test_graph_error_retains_identity_usage_and_safe_remaining_case_note(monkeypatch):
    from api_agent.providers.contracts import ProviderAccessError
    selected_draws(monkeypatch, SEEDS)
    failure = ProviderAccessError('invalid_request', 'synthetic fixed provider failure')
    usage = object()
    failure.usage = usage
    def graph(*args, **kwargs):
        raise failure
    monkeypatch.setattr(driver, 'run_graph', graph)
    with pytest.raises(ProviderAccessError) as result:
        driver.node_coverage(object(), 'replacement-model', {})
    assert result.value is failure
    assert result.value.usage is usage
    assert result.value.__notes__[0].startswith('Uncovered workflow cases: ')
    assert result.value.__notes__[0].count('first=') == 10
    assert result.value.__notes__[0].count('second=') == 8
    assert not any(value in result.value.__notes__[0] for value in SEEDS)
