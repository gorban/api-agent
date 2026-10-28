import threading
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.generation_attempts import (
    TransportAttemptLedger, rollup_transport_attempts, validate_transport_attempts,
)
from api_agent.providers.llm import Usage


def test_failed_and_recovered_attempts_keep_distinct_usage_and_known_subtotals():
    events = []
    ledger = TransportAttemptLedger(2, observer=lambda phase, row: events.append((phase, row)))
    failed = ledger.begin(1)
    ledger.finish(failed, error=SimpleNamespace(execution='possibly_executed', http_status=503, usage=Usage(input_tokens=7)), retry_delay_seconds=0.2)
    succeeded = ledger.begin(2)
    ledger.finish(succeeded, result=SimpleNamespace(usage=Usage(input_tokens=3, output_tokens=2), billing=None))

    snapshot = ledger.snapshot()
    summary = rollup_transport_attempts(snapshot)
    assert [event[0] for event in events] == ['start', 'finish', 'start', 'finish']
    assert snapshot['complete'] is True
    assert summary['usage']['input_tokens'] == 10
    assert summary['unknown_usage_attempts']['output_tokens'] == 1
    assert summary['unknown_cost_attempts'] == 2


def test_unfinished_attempt_remains_incomplete_and_is_bounded():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    assert ledger.snapshot()['complete'] is False
    with pytest.raises(ContractError):
        ledger.begin(2)


def test_invalid_ledger_rejects_content_and_nonsequential_attempts():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    snapshot = ledger.snapshot()
    snapshot['attempts'][0]['prompt'] = 'not retained'
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_capture_counts_physical_usage_once_and_keeps_terminal_usage():
    import json
    from uuid import uuid4
    from api_agent.testing.inference_usage import capture
    from api_agent.telemetry.usage import SuiteUsage

    ledger = TransportAttemptLedger(2, quote=lambda value: {'list_cost_usd': value.cost, 'payment': None})
    first = ledger.begin(1)
    error = SimpleNamespace(execution='possibly_executed', http_status=503, usage=Usage(input_tokens=7, output_tokens=1), cost=0.3)
    ledger.finish(first, error=error, retry_delay_seconds=0.2)
    second = ledger.begin(2)
    result = SimpleNamespace(usage=Usage(input_tokens=3, output_tokens=2), billing=None, cost=0.2)
    ledger.finish(second, result=result)
    call = str(uuid4())
    terminal_usage = {'input_tokens': 3, 'output_tokens': 2, 'cached_input_tokens': None, 'reasoning_tokens': None}
    row = {'call_id': call, 'selection': 'synthetic', 'usage': terminal_usage,
           'transport_attempts': ledger.snapshot()}
    event = SimpleNamespace(text=json.dumps(row), identity=SimpleNamespace(id='test'))
    tracker = SuiteUsage("run")

    capture(event, tracker)
    capture(event, tracker)

    assert row['usage'] == terminal_usage
    assert len(tracker.inference_calls) == 1
    assert tracker.report()['usage_rows'][0]['input_tokens'] == 10
    assert tracker.report()['usage_rows'][0]['output_tokens'] == 3
    assert tracker._costs.calls[call][1]['list_cost_usd'] == pytest.approx(0.5)


def test_loopback_transport_preserves_incremental_attempts_and_terminal_result():
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.generation_attempts import begin_transport_attempt, finish_transport_attempt, snapshot_transport_result
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer

    async def scenario():
        async def complete(request):
            first = begin_transport_attempt(1)
            finish_transport_attempt(first, error=SimpleNamespace(execution='possibly_executed', http_status=503, usage=Usage(5, 1)), retry_delay_seconds=0.2)
            second = begin_transport_attempt(2)
            result = LLMResult(Message('assistant', (TextContent('ok'),)), Usage(3, 2))
            snapshot_transport_result(result)
            finish_transport_attempt(second, result=result)
            return result

        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-capability-value-0000000000')
        endpoint = await server.start()
        client = SetupClient(endpoint, timeout=5)
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
        try:
            result = await asyncio.to_thread(client.complete, request)
            assert result.usage == Usage(3, 2)
            assert result.transport_attempts['complete'] is True
            assert [row['usage']['input_tokens'] for row in result.transport_attempts['attempts']] == [5, 3]
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()

    asyncio.run(scenario())


def test_invalid_terminal_snapshot_retains_admitted_prefix():
    import asyncio
    from api_agent.providers.setup_transport import SetupClient, _encode, _request_digest

    async def scenario():
        client = SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1,
                              'capability': 'synthetic-capability-value-0000000000'})
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'model', 'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        async def drain():
            pass
        client._writer = SimpleNamespace(write=lambda value: None, drain=drain)
        request = {'synthetic': 'request'}
        ledger = TransportAttemptLedger(1)
        ledger.begin(1)
        row = ledger.snapshot()['attempts'][0]
        common = {'version': 1, 'call_sequence': 1, 'request_sha256': _request_digest(request),
                  'binding_sha256': _request_digest(client._binding)}
        client._reader.feed_data(_encode(dict(common, status='transport_attempt', phase='start', row=row)))
        client._reader.feed_data(_encode(dict(common, status='transport_terminal', ledger={'version': 1, 'complete': True, 'attempts': []})))
        with pytest.raises(ContractError):
            await client._exchange('complete', request)
        assert client.transport_attempts == {'version': 1, 'complete': False, 'attempts': [row]}

    asyncio.run(scenario())


def test_unquotable_failure_retains_known_success_subtotal_and_unknown_remainder():
    def quote(value):
        if not hasattr(value, 'cost'):
            raise ValueError('no admitted price')
        return {'list_cost_usd': value.cost, 'payment': None}

    ledger = TransportAttemptLedger(2, quote=quote)
    first = ledger.begin(1)
    ledger.finish(first, error=SimpleNamespace(execution='possibly_executed', http_status=503, usage=Usage(7, 1)), retry_delay_seconds=0.2)
    second = ledger.begin(2)
    ledger.finish(second, result=SimpleNamespace(usage=Usage(3, 2), billing=None, cost=0.2))
    ledger.mark_incomplete()

    summary = rollup_transport_attempts(ledger.snapshot())
    assert summary['list_cost_usd'] == pytest.approx(0.2)
    assert summary['unknown_cost_attempts'] == 1
    assert summary['unknown_remainder'] is True


@pytest.mark.parametrize('changes', [
    {'execution': 'not_started'}, {'status': 500}, {'error_kind': 'server'},
    {'retry_delay_seconds': 0.2}, {'stopped_ms': -1},
])
def test_success_rejects_contradictory_attempt_evidence(changes):
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    ledger.finish(token, result=SimpleNamespace(usage=Usage(2, 1), billing=None))
    snapshot = ledger.snapshot()
    snapshot['attempts'][0].update(changes)
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_started_row_rejects_unadmitted_usage():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    snapshot = ledger.snapshot()
    snapshot['attempts'][0]['usage'] = {'input_tokens': 2, 'output_tokens': 1,
                                        'cached_input_tokens': None, 'reasoning_tokens': None}
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_remote_outer_timeout_publishes_admitted_prefix(monkeypatch):
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.testing import inference_usage

    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    prefix = ledger.snapshot()
    client = SimpleNamespace(_lock=threading.RLock(), transport_attempts=None, response_evidence=None, evidence=[])
    remote = RemoteIntegrationLLM.__new__(RemoteIntegrationLLM)
    remote.config, remote.client = {}, client
    published = []
    monkeypatch.setattr(inference_usage, 'publish', lambda request, result=None, **kw: published.append(kw))
    def complete(_):
        client.transport_attempts = prefix
    client.complete = complete
    def timeout(call):
        call()
        raise TimeoutError('deadline')
    remote._request = timeout
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))

    with pytest.raises(TimeoutError):
        remote.complete(request)

    observed = published[-1]['error'].transport_attempts
    assert observed == prefix
    assert observed['complete'] is False


def test_failed_attempt_retains_its_dispatched_response_policy():
    from api_agent.providers.llm import ResponseEvidence
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    response = ResponseEvidence('text', 'reject', 'text', None, None, None)
    ledger.finish(token, error=SimpleNamespace(execution='possibly_executed'), response=response)
    assert ledger.snapshot()['attempts'][0]['response']['actual_mode'] == 'text'


def test_snapshot_before_validation_survives_later_error():
    from api_agent.providers.generation_attempts import bind_transport_attempt_ledger, snapshot_transport_result
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    with bind_transport_attempt_ledger(ledger):
        snapshot_transport_result(SimpleNamespace(usage=Usage(4, 2), billing=None, response=None))
        observed = ledger.snapshot()
        ledger.finish(token, error=SimpleNamespace(execution='possibly_executed'))
    assert observed['attempts'][0]['outcome'] == 'observed'
    assert observed['complete'] is False
    assert ledger.snapshot()['attempts'][0]['usage']['input_tokens'] == 4
    assert ledger.snapshot()['attempts'][0]['execution'] == 'completed'


def test_remote_conflict_preserves_interrupted_attempt_prefix(monkeypatch):
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, ResponseEvidence, TextContent
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.testing import inference_usage
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    prefix = ledger.snapshot()
    first = ResponseEvidence('text', 'reject', 'text', None, None, None)
    second = ResponseEvidence('text', 'allow', 'text', None, None, None)
    client = SimpleNamespace(_lock=threading.RLock(), transport_attempts=None, response_evidence=None, evidence=[])
    remote = RemoteIntegrationLLM.__new__(RemoteIntegrationLLM)
    remote.config, remote.client = {}, client
    published = []
    monkeypatch.setattr(inference_usage, 'publish', lambda request, result=None, **kw: published.append(kw))
    def fail(_):
        client.transport_attempts = prefix
        client.response_evidence = first
        error = TimeoutError('deadline')
        error.response = second
        raise error
    client.complete = fail
    remote._request = lambda call: call()
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
    with pytest.raises(ContractError, match='Conflicting'):
        remote.complete(request)
    assert published[-1]['error'].transport_attempts == prefix


def test_success_cannot_be_followed_by_another_attempt():
    first, second = TransportAttemptLedger(1), TransportAttemptLedger(1)
    token = first.begin(1)
    first.finish(token, result=SimpleNamespace(usage=Usage(2, 1), billing=None))
    second.begin(1)
    later = second.snapshot()['attempts'][0]
    later['index'] = 2
    later['started_ms'] = first.snapshot()['attempts'][0]['stopped_ms']
    with pytest.raises(ContractError):
        validate_transport_attempts({'version': 1, 'complete': False, 'attempts': first.snapshot()['attempts'] + [later]})


@pytest.mark.parametrize('status,delay', [(None, 0.2), (400, 0.2), (503, 1e300), (429, 86401)])
def test_retry_delay_rejects_ineligible_status_or_unbounded_hint(status, delay):
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    error = SimpleNamespace(execution='possibly_executed', http_status=status)
    with pytest.raises(ContractError):
        ledger.finish(token, error=error, retry_delay_seconds=delay)


@pytest.mark.parametrize('status,delay', [(429, 86400), (500, 0.2), (599, 36)])
def test_retry_delay_accepts_supported_status_and_hint(status, delay):
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    ledger.finish(token, error=SimpleNamespace(execution='possibly_executed', http_status=status), retry_delay_seconds=delay)
    assert ledger.snapshot()['attempts'][0]['retry_delay_seconds'] == delay


def test_local_conflict_publishes_attempt_usage_and_quote(tmp_path, monkeypatch):
    import asyncio
    import json
    from api_agent.providers.generation_attempts import begin_transport_attempt, finish_transport_attempt
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, ResponseEvidence, TextContent
    from api_agent.testing.inference_content import content_boundary
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.workflows.inference_capture import record_response_dispatch

    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_example.py::test_call (call)')
    worker = IntegrationLLM.__new__(IntegrationLLM)
    worker.closed, worker.config, worker._provider_config = False, {}, {}
    worker.emissions = content_boundary({})
    worker.provider = SimpleNamespace(implementation=SimpleNamespace(
        quote_cost=lambda request, value: {'list_cost_usd': 0.25, 'payment': None}))
    worker._run_private = asyncio.run
    first = ResponseEvidence('text', 'reject', 'text', None, None, None)
    second = ResponseEvidence('text', 'allow', 'text', None, None, None)
    async def complete(request):
        token = begin_transport_attempt(1)
        error = RuntimeError('synthetic failure')
        error.usage = Usage(7, 1)
        error.response = second
        finish_transport_attempt(token, error=error, response=first)
        record_response_dispatch(first)
        raise error
    worker._complete = complete
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))

    with pytest.raises(ContractError, match='Conflicting'):
        worker.complete(request)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    terminal = [row['data'] for row in rows if row['kind'] == 'inference_usage'][-1]
    attempt = terminal['transport_attempts']['attempts'][0]
    assert attempt['usage']['input_tokens'] == 7
    assert attempt['quote']['list_cost_usd'] == 0.25
    assert attempt['response']['fallback'] == 'reject'


@pytest.mark.parametrize('snapshot', [None, {}, {'version': True, 'complete': True, 'attempts': []},
                                     {'version': 1, 'complete': False, 'attempts': [None] * 21}])
def test_invalid_ledger_envelope_is_rejected(snapshot):
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


@pytest.mark.parametrize('limit', [0, 21, True, 1.5])
def test_attempt_limit_must_be_an_integer_in_supported_range(limit):
    with pytest.raises(ContractError):
        TransportAttemptLedger(limit)


def test_observed_result_cannot_claim_failure_status():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    ledger.observe_result(SimpleNamespace(usage=Usage(2, 1), billing=None))
    snapshot = ledger.snapshot()
    snapshot['attempts'][0]['status'] = 503
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_failed_attempt_requires_classification():
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    ledger.finish(token, error=SimpleNamespace(execution='possibly_executed'))
    snapshot = ledger.snapshot()
    snapshot['attempts'][0]['error_kind'] = None
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_attempt_identifier_requires_canonical_uuid():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    snapshot = ledger.snapshot()
    snapshot['attempts'][0]['id'] = snapshot['attempts'][0]['id'].upper()
    with pytest.raises(ContractError):
        validate_transport_attempts(snapshot)


def test_invalid_finish_token_does_not_finish_active_attempt():
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    with pytest.raises(ContractError):
        ledger.finish('wrong', result=SimpleNamespace(usage=Usage(1, 1)))
    assert ledger.snapshot()['attempts'][0]['outcome'] == 'started'
    ledger.finish(token, result=SimpleNamespace(usage=Usage(1, 1)))


def test_observation_without_active_attempt_is_ignored():
    ledger = TransportAttemptLedger(1)
    ledger.observe_result(SimpleNamespace(usage=Usage(1, 1)))
    assert ledger.snapshot() == {'version': 1, 'complete': True, 'attempts': []}


def test_invalid_result_evidence_is_omitted_without_masking_result():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    ledger.observe_result(SimpleNamespace(usage=object(), billing={'usd': -1}, response=object()))
    row = ledger.snapshot()['attempts'][0]
    assert row['outcome'] == 'observed'
    assert row['usage'] is None and row['billing'] is None and row['response'] is None


def test_repeated_observation_preserves_first_admitted_evidence():
    from api_agent.providers.llm import ResponseEvidence
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    billing = {'usd': 0.02, 'request_id': None, 'generation_id': None}
    response = ResponseEvidence('text', 'reject', 'text', None, None, None)
    result = SimpleNamespace(usage=Usage(3, 2), billing=billing, response=response)
    ledger.observe_result(result)
    ledger.observe_result(SimpleNamespace(usage=Usage(10, 4), billing=billing, response=response))
    ledger.finish(token, result=result)
    row = ledger.snapshot()['attempts'][0]
    assert row['usage']['input_tokens'] == 3
    assert row['billing'] == billing
    assert row['response']['actual_mode'] == 'text'


def test_absent_observation_fields_remain_unknown():
    ledger = TransportAttemptLedger(1)
    ledger.begin(1)
    ledger.observe_result(SimpleNamespace())
    row = ledger.snapshot()['attempts'][0]
    assert row['usage'] is None and row['billing'] is None


def test_finish_captures_billing_without_prior_observation():
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    billing = {'usd': 0.02, 'request_id': None, 'generation_id': None}
    ledger.finish(token, result=SimpleNamespace(usage=Usage(3, 2), billing=billing))
    summary = rollup_transport_attempts(ledger.snapshot())
    assert summary['reported_charge_usd'] == pytest.approx(0.02)
    assert summary['unknown_charge_attempts'] == 0


def test_unbound_optional_hooks_leave_values_unchanged():
    from api_agent.providers.generation_attempts import (attach_transport_attempts, begin_transport_attempt,
        finish_transport_attempt, snapshot_transport_result)
    result = object()
    assert begin_transport_attempt(1) is None
    assert finish_transport_attempt(None, result=result) is None
    assert snapshot_transport_result(result) is result
    assert attach_transport_attempts(None, None) is None


def test_shared_immutable_result_gets_distinct_concurrent_ledgers():
    import asyncio
    from api_agent.providers.generation_attempts import attach_transport_attempts
    from api_agent.providers.llm import LLMResult, Message, TextContent

    shared = LLMResult(Message('assistant', (TextContent('ok'),)), Usage(3, 2))
    first, second = TransportAttemptLedger(1), TransportAttemptLedger(1)
    for ledger in (first, second):
        token = ledger.begin(1)
        ledger.finish(token, result=shared)
    async def attach(ledger):
        await asyncio.sleep(0)
        return attach_transport_attempts(shared, ledger.snapshot())
    async def scenario():
        return await asyncio.gather(attach(first), attach(second))

    left, right = asyncio.run(scenario())
    assert not hasattr(shared, 'transport_attempts')
    assert left is not shared and right is not shared and left is not right
    assert left == right == shared
    assert left.transport_attempts == first.snapshot()
    assert right.transport_attempts == second.snapshot()
    assert left.transport_attempts['attempts'][0]['id'] != right.transport_attempts['attempts'][0]['id']


def test_absent_ledger_preserves_legacy_result_and_error_identity():
    from api_agent.providers.generation_attempts import attach_transport_attempts
    from api_agent.providers.llm import LLMResult, Message, TextContent
    result = LLMResult(Message('assistant', (TextContent('ok'),)), Usage(3, 2))
    error = RuntimeError('synthetic')
    assert attach_transport_attempts(result, None) is result
    assert attach_transport_attempts(error, None) is error
    assert not hasattr(result, 'transport_attempts') and not hasattr(error, 'transport_attempts')


def test_integration_callers_do_not_mutate_shared_concurrent_provider_result(tmp_path, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
    from api_agent.testing import provider_setup

    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    shared = LLMResult(Message('assistant', (TextContent('ok'),)), Usage(3, 2))
    barrier = threading.Barrier(2, timeout=5)
    async def physical(request, *, auth):
        await asyncio.to_thread(barrier.wait)
        return shared
    async def load(*args, **kwargs):
        implementation = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic')),
                                         acomplete=physical)
        return SimpleNamespace(implementation=implementation, aclose=AsyncMock())
    monkeypatch.setattr(provider_setup, 'aload_provider', load)
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
    def call():
        owner = provider_setup.IntegrationLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path)
        try:
            return owner.complete(request)
        finally:
            owner.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(lambda _: call(), range(2)))

    assert left == right == shared
    assert left is not shared and right is not shared and left is not right
    assert not hasattr(shared, 'transport_attempts')
    assert left.transport_attempts['complete'] and right.transport_attempts['complete']
    assert len(left.transport_attempts['attempts']) == len(right.transport_attempts['attempts']) == 1
    assert left.transport_attempts['attempts'][0]['id'] != right.transport_attempts['attempts'][0]['id']


@pytest.mark.parametrize('measured_zero', [False, True])
def test_unknown_category_only_exposes_known_subtotal_when_observed(measured_zero):
    import json
    from uuid import uuid4
    from api_agent.testing.inference_usage import capture
    from api_agent.telemetry.usage import SuiteUsage

    ledger = TransportAttemptLedger(2)
    first = ledger.begin(1)
    ledger.finish(first, error=SimpleNamespace(execution='possibly_executed', http_status=503,
                                               usage=Usage(2, 1)), retry_delay_seconds=0.2)
    if measured_zero:
        second = ledger.begin(2)
        ledger.finish(second, result=SimpleNamespace(usage=Usage(3, 2, reasoning_tokens=0), billing=None))
    identifier = str(uuid4())
    data = {'call_id': identifier, 'selection': 'synthetic', 'usage': None,
            'transport_attempts': ledger.snapshot()}
    event = SimpleNamespace(text=json.dumps(data), identity=SimpleNamespace(id='test'))
    tracker = SuiteUsage('run')

    capture(event, tracker)

    observation = tracker.report()['usage_observations'][0]
    assert 'reasoning_tokens' in observation['unknown_fields']
    assert observation['reasoning_tokens'] == (0 if measured_zero else None)


@pytest.mark.parametrize('kind', ['binding', 'start_outcome', 'finish_before_start', 'changed_evidence'])
def test_ipc_rejects_invalid_attempt_stream_and_keeps_admitted_prefix(monkeypatch, kind):
    import asyncio
    from copy import deepcopy
    from unittest.mock import AsyncMock
    from api_agent.providers import setup_transport as transport
    from api_agent.providers.setup_transport import SetupClient, _request_digest

    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    start = ledger.snapshot()['attempts'][0]
    ledger.observe_result(SimpleNamespace(usage=Usage(3, 2)))
    observed = ledger.snapshot()['attempts'][0]
    ledger.finish(token, result=SimpleNamespace(usage=Usage(3, 2), billing=None))
    finished = ledger.snapshot()['attempts'][0]
    request = {'synthetic': 'request'}
    client = SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1,
                          'capability': 'synthetic-capability-value-0000000000'})
    client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'model', 'worker_id': 'worker', 'attempt': 1}
    client._writer = SimpleNamespace(write=lambda value: None, drain=AsyncMock())
    client._reader = object()
    common = {'version': 1, 'status': 'transport_attempt', 'call_sequence': 1,
              'request_sha256': _request_digest(request), 'binding_sha256': _request_digest(client._binding)}
    frame = lambda phase, row: dict(common, phase=phase, row=deepcopy(row))
    prefix = []
    if kind == 'binding':
        bad = frame('start', start)
        bad['binding_sha256'] = 'wrong'
    elif kind == 'start_outcome':
        bad = frame('start', finished)
    elif kind == 'finish_before_start':
        bad = frame('finish', finished)
    else:
        prefix = [frame('start', start), frame('evidence', observed)]
        bad = frame('finish', finished)
        bad['row']['usage']['input_tokens'] = 1
    monkeypatch.setattr(transport, '_read', AsyncMock(side_effect=[*prefix, bad]))
    with pytest.raises(ContractError):
        asyncio.run(client._exchange('complete', request))
    retained = client.transport_attempts
    assert retained['complete'] is False
    assert retained['attempts'] == ([observed] if prefix else [])


def test_setup_client_response_conflict_retains_attempt_ledger(monkeypatch):
    from api_agent.providers.contracts import ProviderAccessError
    from test_response_dispatch_evidence import bound_client, request, evidence
    req = request('json')
    client = bound_client()
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    ledger.finish(token, error=SimpleNamespace(execution='possibly_executed', usage=Usage(3, 2)))
    snapshot = ledger.snapshot()
    error = ProviderAccessError('unknown', 'Synthetic failure', usage=Usage(3, 2), response=evidence(req))
    def fail(*args):
        client.response_evidence = evidence(req, 'prompted_json', 'unsupported_mode')
        client.transport_attempts = snapshot
        raise error
    monkeypatch.setattr(client, '_run_request', fail)
    with pytest.raises(ContractError, match='Conflicting') as caught:
        client.complete(req)
    assert caught.value.transport_attempts == snapshot
    assert caught.value.usage == Usage(3, 2)


@pytest.mark.parametrize('invalid', [-1, float('nan'), float('inf'), True])
def test_cost_ledger_rejects_invalid_known_subtotal(invalid):
    from api_agent.telemetry.costs import CostLedger
    ledger = CostLedger()
    with pytest.raises(ValueError, match='subtotal'):
        ledger.record('call', 'session', known_subtotal=invalid)
