"""Typed network recovery uses the existing bounded physical-attempt owner."""
import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest

from api_agent.providers import inference_retry as retry
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.shared_setup import SharedSetupOwner


@pytest.mark.parametrize('transport', [httpx, httpx2])
@pytest.mark.parametrize('name', ['ConnectError', 'ConnectTimeout', 'ReadError', 'ReadTimeout', 'WriteError', 'WriteTimeout', 'RemoteProtocolError'])
def test_only_explicit_typed_network_causes_qualify(transport, name):
    failure = getattr(transport, name)('synthetic details never exported')
    wrapped = RuntimeError('SDK wrapper')
    wrapped.__cause__ = failure
    assert retry.transient_network_failure(wrapped, transport)


@pytest.mark.parametrize('failure', [TimeoutError(), OSError(), ValueError(),
    httpx.PoolTimeout('pool'), httpx.InvalidURL('url'), httpx.UnsupportedProtocol('scheme')])
def test_local_and_unknown_failures_stay_terminal(failure):
    assert not retry.transient_network_failure(failure, httpx)


def test_tls_failure_is_not_reclassified_by_network_wrapper():
    failure = httpx.ConnectError('certificate details')
    failure.__cause__ = ssl.SSLCertVerificationError('synthetic certificate')
    assert not retry.transient_network_failure(failure, httpx)
    failure.__cause__ = failure
    assert retry.transient_network_failure(failure, httpx)


@pytest.mark.parametrize('failure', [TimeoutError(), httpx.PoolTimeout('pool'),
    httpx.UnsupportedProtocol('scheme'), httpx.LocalProtocolError('local request')])
def test_older_network_cause_does_not_override_excluded_failure(failure):
    failure.__cause__ = httpx.ConnectError('earlier unrelated failure')
    assert not retry.transient_network_failure(failure, httpx)


def test_suppressed_context_does_not_establish_network_eligibility():
    failure = RuntimeError('opaque wrapper')
    failure.__context__ = httpx.ConnectError('old failure')
    failure.__suppress_context__ = True
    assert not retry.transient_network_failure(failure, httpx)


@pytest.mark.parametrize('transport', [httpx, httpx2])
@pytest.mark.parametrize('cause', [TimeoutError('socket timeout'), ssl.SSLEOFError('connection lost')])
def test_typed_network_failure_retains_native_socket_timeout_or_tls_eof(transport, cause):
    failure = transport.ReadTimeout('read interrupted')
    failure.__cause__ = cause
    assert retry.transient_network_failure(failure, transport)


@pytest.mark.parametrize('transport', [httpx, httpx2])
def test_tls_configuration_error_stays_terminal(transport):
    failure = transport.ConnectError('connection failed')
    failure.__cause__ = ssl.SSLError('synthetic TLS configuration error')
    assert not retry.transient_network_failure(failure, transport)


@pytest.mark.parametrize('status, expected', [(None, True), (200, True), (401, False), (403, False), (404, False), (503, True)])
def test_network_classification_never_overrides_http_rejection(status, expected):
    error = ProviderAccessError('network', 'Connection interrupted.')
    error.http_status = status
    assert retry.retryable(error) is expected


@pytest.mark.parametrize('recover', [False, True])
def test_default_five_attempts_preserve_unknown_charge_and_selected_identity(monkeypatch, recover):
    delays, requests = [], []
    request_task = None
    real_sleep = asyncio.sleep
    async def sleep(delay):
        # Background lifecycle/evidence tasks also sleep; measure only the
        # inference task's actual backoff waits, not unrelated polling.
        if asyncio.current_task() is request_task:
            delays.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: (low + high) / 2)
    from api_agent.providers import shared_setup
    monkeypatch.setattr(shared_setup.asyncio, 'sleep', sleep)

    async def scenario():
        identity = object()
        async def authenticated(callback):
            return await callback(auth=identity)
        async def physical(request, *, auth):
            nonlocal request_task
            request_task = asyncio.current_task()
            requests.append((request, auth))
            if recover and len(requests) == 5:
                from api_agent.providers.llm import LLMResult, Usage
                return LLMResult(Message('assistant', (TextContent('ok'),)), Usage(2, 1), 'completed')
            error = ProviderAccessError('network', 'Connection lost or timed out.')
            error.execution = 'possibly_executed'
            raise error
        owner = SharedSetupOwner('operation', 'selected', lambda _: SimpleNamespace(call=authenticated, aclose=AsyncMock()), complete=physical)
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        ledger = TransportAttemptLedger(5)
        try:
            with bind_transport_attempt_ledger(ledger):
                if recover:
                    assert (await owner.complete(request)).payload.content[0].text == 'ok'
                else:
                    with pytest.raises(ProviderAccessError, match='Connection lost or timed out.'):
                        await owner.complete(request)
            rows = ledger.snapshot()['attempts']
            assert len(rows) == len(requests) == 5
            assert all(item == (request, identity) for item in requests)
            failed = rows[:-1] if recover else rows
            assert all(row['execution'] == 'possibly_executed' and row['usage'] is None
                       and row['billing'] is None and row['status'] is None
                       and row['error_kind'] == 'network' for row in failed)
            assert [row['retry_delay_seconds'] for row in rows] == [.2, 1, 5, 25, None]
            assert delays == [.2, 1, 5, 25]
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_network_diagnostic_and_execution_survive_worker_frame_roundtrip():
    from api_agent.providers.setup_transport import _error, _raise_error
    error = ProviderAccessError('network', 'Bedrock network request failed; connection lost or timed out.')
    error.execution = 'possibly_executed'
    with pytest.raises(ProviderAccessError, match='connection lost or timed out') as caught:
        _raise_error(_error(error))
    assert caught.value.kind == 'network'
    assert caught.value.execution == 'possibly_executed'
    assert caught.value.usage is None


@pytest.mark.parametrize('name', ['ConnectError', 'ReadTimeout', 'RemoteProtocolError'])
def test_real_bedrock_sdk_emits_network_not_authentication(name):
    from api_agent.llm_providers.bedrock import worker
    sent, frames = [], []
    def handler(request):
        sent.append(request)
        raise getattr(httpx2, name)('synthetic private address', request=request)
    def http_factory(timeout):
        return httpx2.Client(transport=httpx2.MockTransport(handler), timeout=timeout)
    worker.execute({'operation': 'models', 'auth': {'mode': 'api_key', 'region': 'us-east-1', 'api_key': 'synthetic'},
                    'payload': {}, 'timeout': 5}, frames.append, http_factory=http_factory)
    assert len(sent) == 1  # No nested SDK retries.
    assert frames == [{'error': 'network', 'execution': 'possibly_executed'}]


def test_bedrock_body_disconnect_after_success_headers_remains_retryable():
    from api_agent.llm_providers.bedrock import worker
    class BrokenBody(httpx2.SyncByteStream):
        def __iter__(self):
            raise httpx2.ReadTimeout('synthetic private timeout detail')
            yield b''
    def handler(request):
        return httpx2.Response(200, stream=BrokenBody())
    frames = []
    worker.execute({'operation': 'models', 'auth': {'mode': 'api_key', 'region': 'us-east-1', 'api_key': 'synthetic'},
                    'payload': {}, 'timeout': 5}, frames.append,
        http_factory=lambda timeout: httpx2.Client(transport=httpx2.MockTransport(handler), timeout=timeout))
    assert frames == [{'error': 'network', 'execution': 'possibly_executed', 'http_status': 200}]


@pytest.mark.parametrize('failure, expected', [
    (httpx.ReadTimeout('private detail'), 'network'),
    (httpx.ConnectError('private detail'), 'network'),
    (httpx.PoolTimeout('private detail'), 'deadline'),
    (TimeoutError('private detail'), 'deadline'),
    (httpx.UnsupportedProtocol('private detail'), 'unavailable'),
])
def test_gpt_normalizes_only_transient_network_failures(failure, expected):
    from test_gpt_inference import run
    async def handler(request):
        raise failure
    with pytest.raises(ProviderAccessError) as caught:
        run(handler=handler)
    assert caught.value.kind == expected
    assert caught.value.execution == 'possibly_executed'
    assert retry.retryable(caught.value) is (expected == 'network')
    assert 'private detail' not in str(caught.value)


def test_gpt_body_timeout_after_headers_retains_success_status():
    from test_gpt_inference import run
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout('private detail')
            yield b''
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(200, stream=BrokenBody()))
    assert caught.value.kind == 'network'
    assert caught.value.http_status == 200
    assert retry.retryable(caught.value)
