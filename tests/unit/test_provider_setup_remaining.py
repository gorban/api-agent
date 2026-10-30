"""Regression cases for provider setup around workflow ownership."""
import asyncio
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer
from api_agent.testing.authority_scope import authority_scope
from api_agent.testing.provider_setup import RemoteIntegrationLLM
from api_agent.workflows.attribution import bind_workflow_visit


def test_legacy_workflow_without_boundary_does_not_invent_authority():
    expected = LLMResult(Message('assistant', (TextContent('safe response'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    observed = []
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            observed.append('called')
            return expected
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-legacy-capability-0000000', timeout=5)
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
        journal = SimpleNamespace(capture_json=lambda **kwargs: None)
        try:
            with authority_scope(('coordinator',)) as scope, bind_workflow_visit(journal, 'visit'):
                assert await asyncio.to_thread(provider.complete, request) == expected
                assert scope.authorities() == ()
            assert observed == ['called']
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_background_result_copy_failure_finishes_caller_and_allows_retry():
    """Use a child process so a readiness regression cannot hang the suite."""
    import subprocess
    import sys
    code = r'''
import asyncio
from unittest.mock import AsyncMock
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing import provider_setup
from api_agent.testing.authority_scope import authority_scope

expected = LLMResult(Message('assistant', (TextContent('safe'),)), Usage(1, 1))
class Owner:
    ensure_ready = AsyncMock()
    aclose = AsyncMock()
    async def complete(self, request):
        return expected
async def main():
    registry = SetupRegistry('op', 'selected', Owner)
    server = SetupServer(registry, 'synthetic-copy-failure-capability-0000', timeout=5,
                         authority=EmissionBoundary(ContentPolicy(True)))
    provider = provider_setup.RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    original = provider_setup.deepcopy
    failures = []
    def fail_once(value):
        if value is None and not failures:
            failures.append(True)
            raise MemoryError('synthetic-copy-secret')
        return original(value)
    provider_setup.deepcopy = fail_once
    try:
        with authority_scope(('coordinator',)) as scope:
            try:
                await asyncio.wait_for(asyncio.to_thread(provider.complete, request), 3)
            except ContractError as error:
                assert 'Background result finalization failed' in str(error.__cause__)
                while error is not None:
                    assert 'synthetic-copy-secret' not in str(error)
                    error = error.__context__
            else:
                raise AssertionError('failed quote snapshot must fail the call')
            await asyncio.to_thread(scope.finish_evidence)
            assert failures == [True]
            assert await asyncio.wait_for(asyncio.to_thread(provider.complete, request), 3) == expected
            await asyncio.to_thread(scope.finish_evidence)
    finally:
        provider_setup.deepcopy = original
        await asyncio.to_thread(provider.close)
        await server.aclose()
        await registry.aclose()
asyncio.run(main())
'''
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert 'synthetic-copy-secret' not in completed.stdout + completed.stderr


class _ReadinessTracker:
    """Track only the first of each call's two Futures in its caller thread."""
    def __init__(self):
        import threading
        self._local = threading.local()
        self._lock = threading.Lock()
        self._ready = []

    def create(self):
        future = concurrent.futures.Future()
        # _complete_background creates readiness and job Futures consecutively
        # in the same caller thread, before submitting work.
        is_ready = not getattr(self._local, 'awaiting_job', False)
        self._local.awaiting_job = is_ready
        if is_ready:
            with self._lock:
                self._ready.append(future)
        return future

    def release_pending(self):
        with self._lock:
            for future in self._ready:
                if not future.done():
                    try:
                        future.set_exception(TimeoutError('test cleanup'))
                    except concurrent.futures.InvalidStateError:
                        # The background worker may complete it after done().
                        pass


def test_real_background_copy_failure_signals_safe_error_and_retains_retry(monkeypatch):
    import pytest
    from api_agent.providers.contracts import ContractError
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing import provider_setup

    expected = LLMResult(Message('assistant', (TextContent('safe'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            return expected
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-copy-failure-capability-0000', timeout=5,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
        original_copy = provider_setup.deepcopy
        readiness, failures, tasks, jobs = _ReadinessTracker(), [], [], []
        def fail_once(value):
            if value is None and not failures:
                failures.append(True)
                raise MemoryError('synthetic-copy-secret')
            return original_copy(value)
        monkeypatch.setattr(provider_setup, 'Future', readiness.create)
        monkeypatch.setattr(provider_setup, 'deepcopy', fail_once)
        try:
            with authority_scope(('coordinator',)) as scope:
                enroll = scope.enroll_evidence
                def record_job(job):
                    jobs.append(job)
                    return enroll(job)
                scope.enroll_evidence = record_job
                try:
                    tasks.append(asyncio.create_task(asyncio.to_thread(provider.complete, request)))
                    with pytest.raises(ContractError) as caught:
                        await asyncio.wait_for(asyncio.shield(tasks[-1]), 4)
                    assert 'Background result finalization failed' in str(caught.value.__cause__)
                    error = caught.value
                    while error is not None:
                        assert 'synthetic-copy-secret' not in str(error)
                        error = error.__context__
                    await asyncio.to_thread(scope.finish_evidence)
                    assert failures == [True]
                    tasks.append(asyncio.create_task(asyncio.to_thread(provider.complete, request)))
                    assert await asyncio.wait_for(asyncio.shield(tasks[-1]), 4) == expected
                    await asyncio.to_thread(scope.finish_evidence)
                    assert len(jobs) == 2 and all(job.done() and job.exception() is None for job in jobs)
                finally:
                    readiness.release_pending()
                    if tasks:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                    await asyncio.wait_for(asyncio.to_thread(scope.finish_evidence), 5)
                    assert all(job.done() and job.exception() is None for job in jobs)
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_background_copy_failure_does_not_strand_concurrent_call(monkeypatch):
    import threading
    import pytest
    from api_agent.providers.contracts import ContractError
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing import provider_setup
    expected = LLMResult(Message('assistant', (TextContent('safe'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    entered, release = threading.Event(), threading.Event()
    readiness, tasks, jobs = _ReadinessTracker(), [], []
    original_copy = provider_setup.deepcopy
    def fail_first_copy(value):
        if value is None and not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release first copy')
            raise MemoryError('synthetic-copy-secret')
        return original_copy(value)
    monkeypatch.setattr(provider_setup, 'Future', readiness.create)
    monkeypatch.setattr(provider_setup, 'deepcopy', fail_first_copy)
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            return expected
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-copy-failure-capability-0000', timeout=10,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 10})
        try:
            with authority_scope(('coordinator',)) as scope:
                enroll = scope.enroll_evidence
                def record_job(job):
                    jobs.append(job)
                    return enroll(job)
                scope.enroll_evidence = record_job
                first = asyncio.create_task(asyncio.to_thread(provider.complete, request))
                tasks.append(first)
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    assert not first.done()
                    second = asyncio.create_task(asyncio.to_thread(provider.complete, request))
                    tasks.append(second)
                    assert await asyncio.wait_for(asyncio.shield(second), 5) == expected
                    assert not first.done()
                    release.set()
                    with pytest.raises(ContractError) as caught:
                        await asyncio.wait_for(asyncio.shield(first), 5)
                    assert 'Background result finalization failed' in str(caught.value.__cause__)
                    await asyncio.to_thread(scope.finish_evidence)
                    assert len(jobs) == 2 and all(job.done() and job.exception() is None for job in jobs)
                finally:
                    release.set()
                    readiness.release_pending()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                    await asyncio.wait_for(asyncio.to_thread(scope.finish_evidence), 5)
                    assert all(job.done() and job.exception() is None for job in jobs)
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_background_failed_connection_retains_slot_after_close_retry(monkeypatch):
    import threading
    import pytest
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

    entered, release_close, close_waiting = asyncio.Event(), threading.Event(), threading.Event()
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    original_connect = asyncio.open_connection
    original_await_close = SetupClient._await_close
    interruptions = []

    async def connect(*args, **kwargs):
        reader, writer = await original_connect(*args, **kwargs)
        original_wait_closed = writer.wait_closed
        async def wait_closed():
            close_waiting.set()
            if not await asyncio.to_thread(release_close.wait, 10):
                raise TimeoutError('test did not release writer cleanup')
            await original_wait_closed()
        writer.wait_closed = wait_closed
        return reader, writer

    async def interrupt_cleanup(client):
        if len(interruptions) < 2:
            assert await asyncio.to_thread(close_waiting.wait, 3)
            interruptions.append(True)
            raise OSError('synthetic interrupted cleanup acknowledgement')
        await original_await_close(client)

    monkeypatch.setattr(asyncio, 'open_connection', connect)
    monkeypatch.setattr(SetupClient, '_await_close', interrupt_cleanup)

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            entered.set()
            await asyncio.Future()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-disconnect-capability-000000', timeout=5,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
        call = None
        try:
            with authority_scope(('coordinator',)) as scope:
                call = asyncio.create_task(asyncio.to_thread(provider.complete, request))
                await asyncio.wait_for(entered.wait(), 4)
                assert len(server._peers) == 1
                peer = next(iter(server._peers))
                assert peer.binding is not None
                peer.writer.close()
                with pytest.raises(CleanupPendingError):
                    await asyncio.wait_for(asyncio.shield(call), 4)
                await asyncio.wait_for(asyncio.to_thread(scope.finish_evidence), 4)
                assert len(interruptions) == 2
                assert len(provider._evidence_stranded) == 1
                slot, client = provider._evidence_stranded[0]
                assert client._failed and not client._revoke_confirmed
                with provider._evidence_slots._available.mutex:
                    assert slot not in provider._evidence_slots._available.queue
                release_close.set()
                await asyncio.wait_for(asyncio.to_thread(provider.close), 4)
                assert client._disposed and not provider._evidence_stranded
                with provider._evidence_slots._available.mutex:
                    assert slot not in provider._evidence_slots._available.queue
        finally:
            release_close.set()
            await server.aclose()
            if call is not None:
                await asyncio.wait_for(asyncio.gather(call, return_exceptions=True), 5)
            await asyncio.to_thread(provider.close)
            await registry.aclose()
    asyncio.run(scenario())


def test_background_executor_shutdown_returns_slot_and_finishes_evidence():
    import pytest
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

    expected = LLMResult(Message('assistant', (TextContent('safe'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    calls = []
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            calls.append(request)
            return expected

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-executor-capability-000000', timeout=5,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
        try:
            with authority_scope(('coordinator',)) as scope:
                assert await asyncio.wait_for(asyncio.to_thread(provider.complete, request), 4) == expected
                await asyncio.wait_for(asyncio.to_thread(scope.finish_evidence), 4)
                with provider._evidence_slots._available.mutex:
                    before = {slot[0] for slot in provider._evidence_slots._available.queue}
                assert len(before) == 8
                provider._evidence_pool.shutdown(wait=True)
                with pytest.raises(RuntimeError, match='cannot schedule new futures after shutdown'):
                    await asyncio.wait_for(asyncio.to_thread(provider.complete, request), 4)
                await asyncio.wait_for(asyncio.to_thread(scope.finish_evidence), 4)
                with provider._evidence_slots._available.mutex:
                    after = {slot[0] for slot in provider._evidence_slots._available.queue}
                assert after == before
                assert len(calls) == 1
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())
