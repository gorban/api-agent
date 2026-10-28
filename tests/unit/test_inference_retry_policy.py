from datetime import datetime, timezone
import pytest
from api_agent.providers import inference_retry as retry


def test_only_explicit_http_status_is_retryable():
    class Error(Exception):
        pass
    error = Error()
    for status in (429, 500, 599):
        error.http_status = status
        assert retry.retryable(error)
    for status in (None, 200, 400, 499, 600, True):
        error.http_status = status
        assert not retry.retryable(error)


def test_nominal_backoff_jitter_and_server_minimum(monkeypatch):
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: (low + high) / 2)
    assert [retry.retry_delay(n) for n in range(1, 6)] == pytest.approx([.2, 1, 5, 25, 30])
    assert retry.retry_delay(1, 80) == 80
    assert retry.retry_delay(1, "garbage") == pytest.approx(.2)
    assert not retry.retry_hint_supported(10 ** 1000)


def test_backoff_caps_jitter_but_preserves_larger_server_minimum(monkeypatch):
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: high)
    assert retry.retry_delay(5) == 30
    assert retry.retry_delay(5, 40) == 40


@pytest.mark.parametrize('option,value', [
    ('max_attempts', 0), ('max_attempts', 21), ('max_attempts', True),
    ('inference_timeout', 0), ('inference_timeout', float('inf')), ('inference_timeout', True),
])
def test_owner_rejects_invalid_retry_limits(option, value):
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.shared_setup import SharedSetupOwner

    with pytest.raises(ContractError):
        SharedSetupOwner('op', 'selected', lambda _: None, **{option: value})


def test_evidence_isolation_failure_stops_before_provider_dispatch(monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.workflows import inference_capture

    failure = RuntimeError('Synthetic evidence isolation failure')
    @contextmanager
    def fail_isolation():
        raise failure
        yield
    monkeypatch.setattr(inference_capture, 'isolated_response_dispatch', fail_isolation)

    async def scenario():
        auth_calls = []
        physical = AsyncMock()
        async def call(callback):
            auth_calls.append('selected')
            return await callback(auth='selected')
        owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=call, aclose=AsyncMock()),
                                 complete=physical)
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('test'),)),)))
        ledger = TransportAttemptLedger(5)
        try:
            with bind_transport_attempt_ledger(ledger), pytest.raises(RuntimeError) as caught:
                await owner.complete(request)
            assert caught.value is failure
            physical.assert_not_awaited()
            # Readiness and inference each enter the shared auth owner once.
            assert auth_calls == ['selected', 'selected']
            attempts = ledger.snapshot()['attempts']
            assert len(attempts) == 1
            assert attempts[0]['outcome'] == 'failed'
            assert attempts[0]['usage'] is None and attempts[0]['retry_delay_seconds'] is None
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_retry_after_parsing_is_finite_bounded_and_date_aware():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert retry.retry_after(None, now=now) is None
    assert retry.retry_after('2.5', now=now) == 2.5
    assert retry.retry_after('Thu, 01 Jan 2026 00:00:10 GMT', now=now) == 10
    assert retry.retry_after('9' * 129) == retry.UNSUPPORTED_RETRY_AFTER
    for raw in ('nan', 'inf', '-1', 'garbage'):
        assert retry.retry_after(raw, now=now) is None


def test_retry_after_rejects_dates_without_timezone_or_in_the_past():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert retry.retry_after('01 Jan 2026 00:00:10', now=now) is None
    assert retry.retry_after('Wed, 31 Dec 2025 23:59:59 GMT', now=now) is None


@pytest.mark.parametrize('interruption', ['cancel', 'deadline'])
def test_interruption_during_retry_wait_does_not_dispatch_again(monkeypatch, interruption):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner

    monkeypatch.setattr(retry, 'retry_delay', lambda number, hint: 30)

    async def scenario():
        attempted = asyncio.Event()
        calls = []
        async def invoke(callback):
            return await callback(auth='selected')
        async def physical(request, *, auth):
            calls.append(request)
            attempted.set()
            error = ProviderAccessError('unavailable', 'Synthetic failure')
            error.http_status = 503
            raise error
        owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=invoke, aclose=AsyncMock()),
                                 complete=physical, inference_timeout=.05 if interruption == 'deadline' else 5)
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('test'),)),)))
        ledger = TransportAttemptLedger(5)
        with bind_transport_attempt_ledger(ledger):
            task = asyncio.create_task(owner.complete(request))
            await asyncio.wait_for(attempted.wait(), 2)
            if interruption == 'cancel':
                task.cancel()
            with pytest.raises(asyncio.CancelledError if interruption == 'cancel' else TimeoutError):
                await asyncio.wait_for(task, 2)
        assert len(calls) == 1
        attempts = ledger.snapshot()['attempts']
        assert len(attempts) == 1
        assert attempts[0]['outcome'] == 'failed'
        assert attempts[0]['retry_delay_seconds'] == 30
        await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('limit,status,hint,expected', [(5, 503, None, 5), (1, 503, None, 1), (5, None, None, 1), (5, 429, None, 5), (5, 503, 'garbage', 5)])
def test_shared_owner_bounds_physical_calls_and_preserves_identity(monkeypatch, limit, status, hint, expected):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner

    monkeypatch.setattr(retry, 'retry_delay', lambda number, hint: 0)
    async def scenario():
        auth = object()
        async def call(callback):
            return await callback(auth=auth)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        seen = []
        async def physical(request, *, auth):
            seen.append((request, auth))
            error = ProviderAccessError('unavailable', 'Synthetic failure')
            error.execution = 'possibly_executed'
            error.http_status = status
            error.retry_after = hint
            raise error
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child, complete=physical, max_attempts=limit)
        request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('test'),)),)))
        ledger = TransportAttemptLedger(limit)
        with bind_transport_attempt_ledger(ledger):
            with pytest.raises(ProviderAccessError):
                await owner.complete(request)
        assert len(seen) == expected
        assert all(item == (request, auth) for item in seen)
        assert len(ledger.snapshot()['attempts']) == expected
        await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('value,valid', [('0', False), ('1', True), ('20', True), ('21', False)])
def test_actual_configuration_validates_attempt_limit(value, valid):
    from api_agent.config import load_declarations, resolve_config
    schema = load_declarations('cfg/config.toml')
    result = resolve_config(schema, environ={'LLM_MAX_ATTEMPTS': value})
    assert not any('LLM_MAX_ATTEMPTS' in error for error in result.errors) == valid


def test_huge_retry_after_is_declined_without_sleep(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner
    assert retry.retry_after('1e308') == retry.UNSUPPORTED_RETRY_AFTER
    assert retry.retry_after('1e999') == retry.UNSUPPORTED_RETRY_AFTER
    async def scenario():
        calls = []
        async def invoke(callback):
            return await callback(auth='selected')
        async def physical(request, *, auth):
            calls.append(request)
            error = ProviderAccessError('rate_limited', 'Synthetic failure')
            error.http_status = 429
            error.retry_after = retry.UNSUPPORTED_RETRY_AFTER
            raise error
        owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=invoke, aclose=AsyncMock()), complete=physical)
        request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('test'),)),)))
        with pytest.raises(ProviderAccessError):
            await owner.complete(request)
        assert len(calls) == 1
        await owner.aclose()
    asyncio.run(scenario())


def test_loaded_provider_retains_billing_before_structured_output_rejection(tmp_path):
    import asyncio
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.loading import aload_provider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, StructuredOutput, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    module = tmp_path / 'provider.py'
    module.write_text('''from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
class Provider:
    version = 1
    capabilities = ProviderCapabilities('llm', ('complete',), features=('structured',))
    async def acomplete(self, request, *, auth=None):
        return LLMResult(Message('assistant', (TextContent('{"value": 7}'),)), Usage(3, 2), billing={'usd': 0.1, 'request_id': None, 'generation_id': None})
def create(config, services): return Provider()
''')
    async def scenario():
        loaded = await aload_provider(str(module) + ':create', {}, {}, kind='llm')
        provider = loaded.require('llm', 'complete')
        async def call(callback):
            return await callback(auth='selected')
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda _: child, complete=provider.acomplete)
        schema = '{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}'
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('test'),)),), structured=StructuredOutput('answer', schema)))
        ledger = TransportAttemptLedger(5)
        with bind_transport_attempt_ledger(ledger):
            with pytest.raises(ProviderAccessError):
                await owner.complete(request)
        row = ledger.snapshot()['attempts'][0]
        assert row['outcome'] == 'failed'
        assert row['usage']['input_tokens'] == 3
        assert row['billing']['usd'] == .1
        await owner.aclose()
        await loaded.aclose()
    asyncio.run(scenario())
