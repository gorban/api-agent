"""Requested reasoning stays frozen across worker accounting and history."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.llm_providers.gpt.pricing import quote
from api_agent.telemetry.accounting import validate_evidence, validate_metadata
from api_agent.telemetry.estimation import call_signature, predictions_for
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.inference_usage import capture, publish
from api_agent.testing.provider_setup import RemoteIntegrationLLM, _configured_request


@pytest.fixture
def request_value():
    return LLMRequest('gpt-6-luna', CompletionRequest((Message('user', (TextContent('A simple task'),)),)))


@pytest.fixture
def result_value():
    return LLMResult(Message('assistant', (TextContent('Done'),)), Usage(11, 5, 0, 0, 0), actual_model='gpt-6-luna')


@pytest.mark.parametrize('choice,expected', [(None, 'high'), ('', ''), ('off', 'off'), ('low', 'low'), ('medium', 'medium'), ('high', 'high')])
def test_explicit_choice_wins_before_accounting(request_value, choice, expected):
    request = replace(request_value, payload=replace(request_value.payload, generation=GenerationOptions(reasoning_effort=choice)))
    configured = _configured_request(request, {'LLM_REASONING_EFFORT': 'high'})
    assert configured.payload.generation.reasoning_effort == expected
    assert request.payload.generation.reasoning_effort == choice
    assert _configured_request(configured, {'LLM_REASONING_EFFORT': 'low'}) is configured


@pytest.mark.parametrize('choice', ['', 'off', 'low', 'medium', 'high'])
@pytest.mark.parametrize('failed', [False, True])
def test_worker_events_preserve_choice(tmp_path, monkeypatch, request_value, result_value, choice, failed):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test (call)')
    request = _configured_request(request_value, {'LLM_REASONING_EFFORT': choice})
    call_id = publish(request)
    publish(request, None if failed else result_value, call_id=call_id, failed=failed)
    tracker = SuiteUsage('suite')
    for line in path.read_text().splitlines():
        data = json.loads(line)['data']
        assert data['reasoning_effort'] == (choice or None)
        capture(SimpleNamespace(text=json.dumps(data), identity=SimpleNamespace(id='test')), tracker)
    saved = tracker.inference_calls[call_id]
    assert saved['reasoning_effort'] == (choice or None)
    assert saved['outcome'] == ('failed' if failed else 'completed')
    assert saved['actual_model'] == (None if failed else 'gpt-6-luna')
    assert saved['serving_revision'] is None
    assert 'A simple task' not in path.read_text()


@pytest.fixture
def saved_evidence(tmp_path, monkeypatch, request_value, result_value):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test (call)')
    publish(_configured_request(request_value, {'LLM_REASONING_EFFORT': 'low'}), result_value)
    tracker = SuiteUsage('suite')
    capture(SimpleNamespace(text=json.dumps(json.loads(path.read_text())['data']), identity=SimpleNamespace(id='test')), tracker)
    evidence = dict(next(iter(tracker.inference_calls.values())))
    del evidence['test_id']
    return evidence


@pytest.mark.parametrize('bad', ['', True, 1, [], {}, 'none', 'unknown', 'max'])
def test_invalid_effort_rejected_in_evidence(saved_evidence, bad):
    saved_evidence['reasoning_effort'] = bad
    with pytest.raises(ContractError, match='Invalid inference evidence'):
        validate_evidence(saved_evidence)


def test_legacy_evidence_stays_readable(saved_evidence):
    del saved_evidence['reasoning_effort']
    assert validate_evidence(saved_evidence) is saved_evidence
    assert 'reasoning_effort' not in saved_evidence


@pytest.mark.parametrize('bad', ['', True, 1, [], {}, 'none', 'unknown', 'max'])
def test_invalid_effort_rejected_in_quote(request_value, result_value, bad):
    metadata = quote(request_value, result_value, direct=True, subscription=True)['accounting']
    metadata['reasoning_effort'] = bad
    with pytest.raises(ContractError, match='Invalid call accounting metadata'):
        validate_metadata(metadata)


@pytest.mark.parametrize('first,second', [(None, 'low'), ('low', 'high'), ('off', None)])
def test_effort_separates_signatures(first, second):
    from test_historical_estimation import row
    original = row()
    original['calls'][0]['quote']['accounting']['reasoning_effort'] = first
    changed = deepcopy(original)
    changed['calls'][0]['quote']['accounting']['reasoning_effort'] = second
    assert call_signature(original) != call_signature(changed)


def test_missing_choice_is_not_explicit_omission():
    from test_historical_estimation import row
    legacy = row()
    current = deepcopy(legacy)
    current['calls'][0]['quote']['accounting']['reasoning_effort'] = None
    assert call_signature(legacy) != call_signature(current)


def test_different_efforts_never_average():
    from api_agent.providers.rates import bundled_rate_card
    from test_historical_estimation import compat, row
    first = row(1000)
    first['calls'][0]['quote']['accounting']['reasoning_effort'] = 'low'
    second = deepcopy(first)
    second['calls'][0]['quote']['accounting']['reasoning_effort'] = 'high'
    test = SimpleNamespace(id='t', node_id=first['node_id'])
    prediction = predictions_for([test], [({}, {'tests': [first, second]})], bundled_rate_card(), {'t': 'hash'}, compat())['t']
    assert prediction['estimated_cost_usd'] is None
    assert prediction['sample_count'] == 0


def test_started_fact_cannot_change_effort(saved_evidence):
    tracker = SuiteUsage('suite')
    pending = dict(saved_evidence, usage=None, finished_at=None, outcome='unknown')
    tracker.record_inference(pending['call_id'], 'test', pending)
    with pytest.raises(ValueError, match='Conflicting inference identity'):
        tracker.record_inference(pending['call_id'], 'test', dict(pending, reasoning_effort='high'))


@pytest.mark.parametrize('choice,expected', [(None, 'high'), ('', ''), ('off', 'off')])
def test_real_worker_ipc_freezes_defaults(request_value, result_value, choice, expected):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    request = replace(request_value, payload=replace(request_value.payload, generation=GenerationOptions(reasoning_effort=choice)))
    observed = []
    async def complete(value):
        observed.append(value)
        return result_value
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        server = SetupServer(registry, 'synthetic-reasoning-capability-value-000000')
        endpoint = await server.start()
        config = {'LLM_REASONING_EFFORT': 'high'}
        remote = RemoteIntegrationLLM(endpoint, config)
        config['LLM_REASONING_EFFORT'] = 'low'
        try:
            assert await asyncio.to_thread(remote.complete, request) == result_value
            assert observed[0].payload.generation.reasoning_effort == expected
            assert remote.config['LLM_REASONING_EFFORT'] == 'high'
        finally:
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_sampling_binds_reasoning_setting_and_new_provider_module(monkeypatch):
    from pathlib import Path
    from api_agent.workflows.fixture_driver import fingerprint
    from test_workflow_sampling import FACTORY_CONFIG
    original = fingerprint('gpt-6-luna', FACTORY_CONFIG)
    selected = fingerprint('gpt-6-luna', FACTORY_CONFIG | {'LLM_REASONING_EFFORT': 'high'})
    assert original['effective_settings']['reasoning_effort'] is None
    assert selected['effective_settings']['reasoning_effort'] == 'high'
    source = 'src/api_agent/llm_providers/gpt/reasoning.py'
    assert source in original['sources']
    read_bytes = Path.read_bytes
    def changed(path):
        body = read_bytes(path)
        return body + b'\n# synthetic changed contract\n' if str(path).endswith(source) else body
    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert fingerprint('gpt-6-luna', FACTORY_CONFIG)['sources'][source] != original['sources'][source]


def test_changed_sampling_cannot_reuse_old_reviewed_cap(monkeypatch):
    from api_agent.workflows import fixture_driver
    from api_agent.workflows.sampling import measured_calibration, DECLARED
    from test_workflow_sampling import FACTORY_CONFIG
    report = measured_calibration([{'trial': trial, 'route': 'next_a', 'nodes': sorted(DECLARED)} for trial in range(1, 61)], DECLARED)
    report.update(fixture_driver.fingerprint('gpt-6-luna', FACTORY_CONFIG))
    report['effective_settings']['reasoning_effort'] = 'high'
    monkeypatch.setattr(fixture_driver, '_baselines', lambda: [report])
    with pytest.raises(ValueError, match='Calibration is stale'):
        fixture_driver.baseline_limit('gpt-6-luna', FACTORY_CONFIG)


def test_reasoning_setting_remains_structural_evidence():
    from api_agent.workflows.evidence import sanitize
    assert sanitize({'effective_settings': {'reasoning_effort': 'high'}}) == {'effective_settings': {'reasoning_effort': 'high'}}


@pytest.mark.parametrize('first,second', [(None, 'low'), ('low', 'high'), ('off', None)])
def test_generic_provider_legacy_quote_does_not_hide_requested_effort(first, second):
    from test_historical_estimation import row
    original = row()
    original['reasoning_effort'] = first
    original['calls'][0]['reasoning_effort'] = first
    changed = deepcopy(original)
    changed['calls'][0]['reasoning_effort'] = second
    assert call_signature(original) != call_signature(changed)


def test_generic_provider_effort_histories_do_not_pool():
    from api_agent.providers.rates import bundled_rate_card
    from test_historical_estimation import compat, row
    first = row()
    first['calls'][0]['reasoning_effort'] = 'low'
    second = deepcopy(first)
    second['calls'][0]['reasoning_effort'] = 'high'
    test = SimpleNamespace(id='t', node_id=first['node_id'])
    prediction = predictions_for([test], [({}, {'tests': [first, second]})], bundled_rate_card(), {'t': 'hash'}, compat())['t']
    assert prediction['estimated_cost_usd'] is None
    assert prediction['sample_count'] == 0


@pytest.mark.parametrize('quoted', [None, 'high'])
def test_conflicting_quote_and_request_fail_closed(saved_evidence, request_value, result_value, quoted):
    saved_evidence['quote'] = quote(request_value, result_value, direct=True, subscription=True)
    saved_evidence['quote']['accounting']['reasoning_effort'] = quoted
    with pytest.raises(ContractError, match='Conflicting requested reasoning effort'):
        validate_evidence(saved_evidence)


def test_unvalidated_conflicting_quote_cannot_calibrate():
    from test_historical_estimation import row
    value = row()
    value['calls'][0]['reasoning_effort'] = 'high'
    value['calls'][0]['quote']['accounting']['reasoning_effort'] = 'low'
    assert call_signature(value) is None


@pytest.mark.parametrize('prior_has_choice', [False, True])
def test_pending_unknown_and_explicit_omission_are_not_interchangeable(saved_evidence, prior_has_choice):
    tracker = SuiteUsage('suite')
    pending = dict(saved_evidence, usage=None, finished_at=None, outcome='unknown')
    pending.pop('reasoning_effort')
    explicit = dict(pending, reasoning_effort=None)
    first, second = (explicit, pending) if prior_has_choice else (pending, explicit)
    tracker.record_inference(first['call_id'], 'test', first)
    with pytest.raises(ValueError, match='Conflicting inference identity'):
        tracker.record_inference(second['call_id'], 'test', second)


@pytest.mark.parametrize('mode', ('responses', 'completions'))
def test_luna_56_worker_preserves_off_over_default(mode, request_value, result_value):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.llm_providers.gpt.inference import validate_text_request
    request = replace(request_value, selected_llm='gpt-5.6-luna',
        payload=replace(request_value.payload, generation=GenerationOptions(reasoning_effort='off')))
    observed = []
    async def complete(value):
        _, body = validate_text_request(base_url='https://api.openai.com/v1',
            access_token='synthetic-token', subscription=False, api_mode=mode, request=value)
        observed.append((value, body))
        return result_value
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        server = SetupServer(registry, 'synthetic-luna-capability-value-000000000')
        endpoint = await server.start()
        remote = RemoteIntegrationLLM(endpoint, {'LLM_REASONING_EFFORT': 'high'})
        try:
            await asyncio.to_thread(remote.complete, request)
            value, body = observed[0]
            assert value.selected_llm == 'gpt-5.6-luna'
            assert value.payload.generation.reasoning_effort == 'off'
            expected = {'reasoning': {'effort': 'none'}} if mode == 'responses' else {'reasoning_effort': 'none'}
            assert {key: body[key] for key in ('reasoning', 'reasoning_effort') if key in body} == expected
        finally:
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())
