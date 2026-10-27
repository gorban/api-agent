"""Unrecorded launch accounting uses the real selected worker event path."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.launched import run_launched_integration


def synthetic_completion(tmp_path, usage, quote, selection='example-model'):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    provider = tmp_path / 'provider.py'
    provider.write_text("""
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
class Auth:
    async def __call__(self): return object()
    async def aclose(self): pass
class Adapter:
    version = 1
    async def asetup(self, **kwargs): return Auth()
    async def adescribe(self, *, auth): return (AvailableSelection(MODEL),)
    async def acomplete(self, request, *, auth):
        assert request.selected_llm == MODEL
        return LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(**VALUES), actual_model=MODEL)
    def quote_cost(self, request, result): return QUOTE
    async def aclose(self): pass
def factory(config, services): return Adapter()
""".replace('MODEL', repr(selection)).replace('VALUES', repr(usage or {})).replace('QUOTE', repr(quote or {'list_cost_usd': None, 'payment': None})))
    (suite / 'test_usage.py').write_text(
        'from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent\n'
        'def test_usage(llm_selection, llm_provider):\n'
        '    result = llm_provider.complete(LLMRequest(llm_selection, CompletionRequest(\n'
        '        (Message("user", (TextContent("Reply Ok"),)),))))\n'
        '    assert result.payload.content[0].text == "Ok"\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,))
    config = {'TUI_MODE': 'integration', 'SKIP_ALLURE_REPORTING': True, 'TEST_PARALLEL_JOBS': 0,
        'LLM_PROVIDER': str(provider) + ':factory', 'LLM_DEFAULT_MODEL': selection}
    return context, config


@pytest.mark.parametrize('quote', [None, {'list_cost_usd': 0.125, 'payment': None}])
@pytest.mark.parametrize('usage', [None, {'input_tokens': 7, 'output_tokens': 3,
    'cached_input_tokens': None, 'reasoning_tokens': None, 'cache_write_tokens': None}])
def test_worker_usage_reaches_unrecorded_result_and_console(tmp_path, monkeypatch, usage, quote):
    from api_agent.testing import launched
    context, config = synthetic_completion(tmp_path, usage, quote)
    forbidden = Mock(side_effect=AssertionError('reporting prerequisite acquired'))
    monkeypatch.setattr(launched, 'local_recording_prerequisites', forbidden)
    events, output = [], []
    result = run_launched_integration(context, collect_tests(context, 'integration'),
        config, accepted=True, on_event=events.append, usage_output=output.append)
    assert result.exit_code == 0 and result.execution.tests[0].status == 'passed'
    assert len([event for event in events if event.kind == 'inference_usage']) == 2
    assert result.reporting_skipped and not result.persisted
    assert result.directory is None and result.manifest is None
    assert result.usage['estimated_cost_usd'] == (None if quote is None else 0.125)
    assert len(output) == 1
    assert ('This suite cost: unavailable' if quote is None else 'This suite cost: $0.125') in output[0]
    if usage is None:
        assert result.usage['usage_rows'][0]['input_tokens'] is None
        assert result.usage['usage_rows'][0]['output_tokens'] is None
        assert 'total: unavailable' in output[0]
    else:
        assert result.usage['usage_rows'][0]['input_tokens'] == 7
        assert result.usage['usage_rows'][0]['output_tokens'] == 3
        assert 'total: 10' in output[0] and 'reasoning: unavailable' in output[0]
    forbidden.assert_not_called()
    assert not (tmp_path / 'artifacts').exists()


def test_malformed_usage_is_rejected_before_forwarding(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import RunnerEvent
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    event = RunnerEvent('inference_usage', 'owner', test, text='{}')
    def execute(*args, **kwargs):
        kwargs['on_event'](event)
        pytest.fail('invalid usage accepted')
    monkeypatch.setattr(integration, 'run_integration_tests', execute)
    forward = Mock()
    with pytest.raises(ValueError, match='Invalid inference usage event'):
        run_launched_integration(context, (test,),
            {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'},
            accepted=True, on_event=forward)
    forward.assert_not_called()


def test_unrecorded_without_callbacks_retains_status(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import RunnerEvent
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    execution = SimpleNamespace(exit_code=130, tests=(), cancelled=True, inspections=())
    def execute(*args, **kwargs):
        kwargs['on_event'](RunnerEvent('started'))
        return execution
    monkeypatch.setattr(integration, 'run_integration_tests', execute)
    result = run_launched_integration(context, (test,),
        {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True)
    assert result.execution is execution and result.exit_code == 130
    assert result.usage['estimated_cost_usd'] is None


@pytest.mark.parametrize('failure', [OSError('output closed'), KeyboardInterrupt()])
@pytest.mark.parametrize('status', [1, 130])
def test_usage_output_failure_retains_execution_and_inspection(tmp_path, monkeypatch, failure, status):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    inspection = object()
    execution = SimpleNamespace(exit_code=status, tests=(), cancelled=status == 130,
        inspections=(inspection,))
    monkeypatch.setattr(integration, 'run_integration_tests', Mock(return_value=execution))
    with pytest.raises(type(failure)) as caught:
        run_launched_integration(context, (test,),
            {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True,
            usage_output=Mock(side_effect=failure))
    assert caught.value is failure
    assert failure.execution is execution and failure.inspections == (inspection,)
    assert failure.recorded_result.execution is execution
    assert failure.recorded_result.exit_code == status
    assert failure.recorded_result.reporting_skipped
    assert failure.recorded_result.usage['estimated_cost_usd'] is None


@pytest.mark.parametrize('source', ['bearer', 'boundary', 'explicit', 'config'])
def test_selected_completion_metadata_is_scrubbed_before_capture_and_forward(tmp_path, source):
    import json
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    secret = 'synthetic-registered-value'
    selection = 'Bearer synthetic-token-value' if source == 'bearer' else secret
    context, config = synthetic_completion(tmp_path,
        {'input_tokens': 7, 'output_tokens': 3}, None, selection)
    options = {}
    if source == 'boundary':
        options['evidence_boundary'] = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    elif source == 'explicit':
        options['secret_values'] = (secret,)
    elif source == 'config':
        config['SYNTHETIC_API_KEY'] = secret
    events, console = [], []
    result = run_launched_integration(context, collect_tests(context, 'integration'),
        config, accepted=True, on_event=events.append, usage_output=console.append, **options)
    assert result.exit_code == 0
    usage_events = [event for event in events if event.kind == 'inference_usage']
    assert len(usage_events) == 2
    encoded = json.dumps(result.usage) + ''.join(event.text for event in usage_events) + ''.join(console)
    assert selection not in encoded
    assert 'synthetic-token-value' not in encoded
    assert '[REDACTED]' in encoded
    assert result.usage['usage_rows'][0]['input_tokens'] == 7
    assert result.usage['usage_rows'][0]['output_tokens'] == 3
    assert result.usage['estimated_cost_usd'] is None
    assert json.loads(usage_events[-1].text)['actual_model'] != selection


def test_report_uses_latest_shared_secret_registrations(tmp_path, monkeypatch):
    import json
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import RunnerEvent
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    boundary = EmissionBoundary(ContentPolicy(True))
    secret = 'synthetic-late-registered-model'
    event = RunnerEvent('inference_usage', 'owner', test, text=json.dumps({
        'call_id': '00000000-0000-4000-8000-000000000001',
        'selection': secret, 'usage': {'input_tokens': 7, 'output_tokens': 3,
            'cached_input_tokens': None, 'reasoning_tokens': None, 'cache_write_tokens': None}}))
    def execute(*args, **kwargs):
        kwargs['on_event'](event)
        boundary.register_secret(secret)
        return SimpleNamespace(exit_code=0, tests=(), cancelled=False, inspections=())
    monkeypatch.setattr(integration, 'run_integration_tests', execute)
    console = []
    result = run_launched_integration(context, (test,),
        {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True,
        evidence_boundary=boundary, usage_output=console.append)
    assert secret not in json.dumps(result.usage) + ''.join(console)
    assert result.usage['usage_rows'][0]['input_tokens'] == 7
    assert result.usage['estimated_cost_usd'] is None


@pytest.mark.parametrize('invalid', [
    {'hidden_reasoning': 'synthetic-hidden-field'},
    {'actual_model': 'Bearer\tsynthetic-token-value'},
])
def test_raw_usage_is_rejected_before_scrubbing_can_repair_it(tmp_path, monkeypatch, invalid):
    import json
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import RunnerEvent
    from api_agent.testing.inference_usage import validate_event
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    raw = {'call_id': '00000000-0000-4000-8000-000000000001',
        'selection': 'example-model', 'usage': None, **invalid}
    event = RunnerEvent('inference_usage', 'owner', test, text=json.dumps(raw))
    # Control: privacy export alone repairs these malformed rows.
    clean = EmissionBoundary(ContentPolicy(True)).content_export(raw)
    validate_event(RunnerEvent('inference_usage', 'owner', test, text=json.dumps(clean)))
    def execute(*args, **kwargs):
        kwargs['on_event'](event)
        pytest.fail('invalid raw usage accepted')
    monkeypatch.setattr(integration, 'run_integration_tests', execute)
    forward, console = Mock(), Mock()
    with pytest.raises(ValueError):
        run_launched_integration(context, (test,),
            {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True,
            on_event=forward, usage_output=console)
    forward.assert_not_called()
    console.assert_not_called()
