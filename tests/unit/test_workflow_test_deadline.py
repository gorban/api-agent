"""The workflow's one parent budget reaches local and actual IPC setup."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderCapabilities, ContractError
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.progress import report_timeout_sync
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend


@pytest.fixture
def slow_provider(monkeypatch):
    entered, released = threading.Event(), threading.Event()
    calls = []
    gate = threading.Event()
    gate.set()

    class Auth:
        async def __call__(self):
            return 'synthetic-session'
        async def aclose(self):
            calls.append('auth closed')

    class Adapter:
        capabilities = ProviderCapabilities(kind='llm')
        async def asetup(self, **kwargs):
            calls.append('setup')
            entered.set()
            try:
                while not gate.is_set():
                    await asyncio.sleep(.001)
                await asyncio.sleep(.2)
                return Auth()
            finally:
                released.set()
        async def adescribe(self, *, auth):
            calls.append('catalog')
            return (AvailableSelection('synthetic'),)

    loaded = SimpleNamespace(implementation=Adapter(), aclose=AsyncMock())
    loader = AsyncMock(return_value=loaded)
    monkeypatch.setattr(provider_setup, 'aload_provider', loader)
    return SimpleNamespace(entered=entered, released=released, gate=gate, calls=calls, loaded=loaded, loader=loader)


def test_expired_parent_closes_never_started_local_coroutine(tmp_path, monkeypatch, slow_provider):
    from api_agent.providers import progress
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    local = provider_setup.IntegrationLLM({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path)
    pending = local._describe()
    try:
        with report_timeout_sync('Workflow integration test', 1):
            clock[0] = 2
            with pytest.raises(TimeoutError, match='already expired'):
                local._run_private(pending)
        assert pending.cr_frame is None
        slow_provider.loader.assert_not_awaited()
        assert not slow_provider.calls
    finally:
        pending.close()
        local.close()


def test_local_setup_waiter_uses_parent_deadline_and_owned_cleanup(tmp_path, slow_provider):
    local = provider_setup.IntegrationLLM({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path)
    try:
        with report_timeout_sync('Workflow integration test', .05):
            with pytest.raises(TimeoutError):
                local.describe()
        assert slow_provider.entered.is_set()
        assert slow_provider.calls == ['setup']
    finally:
        local.close()
    assert slow_provider.released.is_set()
    slow_provider.loaded.aclose.assert_awaited_once()
    assert not asyncio.all_tasks(local.loop)


@pytest.mark.parametrize('method', ['describe', 'ensure_ready'])
def test_actual_ipc_setup_honors_parent_and_preserves_sibling(tmp_path, slow_provider, method):
    from concurrent.futures import ThreadPoolExecutor
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path)
    pool = ThreadPoolExecutor(max_workers=1)
    slow_provider.gate.clear()
    first = sibling = None
    try:
        endpoint = backend.start()
        first = provider_setup.RemoteIntegrationLLM(endpoint)
        sibling = provider_setup.RemoteIntegrationLLM(endpoint)
        surviving = pool.submit(sibling.describe)
        assert slow_provider.entered.wait(2), 'Synthetic setup never entered'
        with report_timeout_sync('Workflow integration test', .05):
            with pytest.raises(ContractError, match='Shared setup connection failed') as timed_out:
                getattr(first, method)()
        assert str(timed_out.value.__cause__).startswith('Worker transport failure category: deadline')
        assert slow_provider.entered.is_set()
        slow_provider.gate.set()
        assert surviving.result(timeout=2)[0].selected_llm == 'synthetic'
        assert slow_provider.calls.count('setup') == 1
        assert slow_provider.calls.count('catalog') == 1
    finally:
        slow_provider.gate.set()
        pool.shutdown(wait=True)
        if first is not None:
            first.close()
        if sibling is not None:
            sibling.close()
        backend.close()
    assert slow_provider.released.is_set()
    slow_provider.loaded.aclose.assert_awaited_once()


@pytest.mark.parametrize('remote', [False, True])
def test_normal_test_starts_budget_before_dynamic_provider_and_expired_fixture_cannot_start_setup(
        tmp_path, monkeypatch, slow_provider, remote):
    from pathlib import Path
    import runpy
    from unittest.mock import Mock
    from api_agent.providers import progress
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / 'tests/integration/test_workflow.py'))
    run = module['test_workflow_node_coverage']
    coverage = Mock(side_effect=AssertionError('No graph after expired setup'))
    monkeypatch.setitem(run.__globals__, 'node_coverage', coverage)
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path) if remote else None
    provider = (provider_setup.RemoteIntegrationLLM(backend.start()) if remote else
                provider_setup.IntegrationLLM({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path))
    def fixture(name):
        assert name == 'llm_provider'
        assert progress._scope.get().remaining == 600
        clock[0] = 601  # Acquisition consumed the one outer budget.
        return provider
    try:
        error = ContractError if remote else TimeoutError
        with pytest.raises(error):
            run(SimpleNamespace(getfixturevalue=fixture), SimpleNamespace(values={
                'LLM_WORKFLOW_TEST_MODEL': 'synthetic', 'LLM_WORKFLOW_TEST_TIMEOUT_SECONDS': 600}))
        coverage.assert_not_called()
        slow_provider.loader.assert_not_awaited()
        assert not slow_provider.calls
    finally:
        provider.close()
        if backend is not None:
            backend.close()
    slow_provider.loaded.aclose.assert_not_awaited()


@pytest.mark.parametrize('reported', [False, True])
@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_external_loop_interruption_preserves_started_operation_until_owned_drain(monkeypatch, interruption, reported):
    loop = asyncio.new_event_loop()
    local = object.__new__(provider_setup.IntegrationLLM)
    local.loop = loop
    entered, release = asyncio.Event(), asyncio.Event()
    cleaned = []
    async def work():
        entered.set()
        try:
            await release.wait()
        finally:
            cleaned.append('drained')
    run = loop.run_until_complete
    owned, reports = [], []
    def interrupt(operation):
        owned.append(loop.create_task(operation))
        run(entered.wait())
        raise interruption()
    monkeypatch.setattr(loop, 'run_until_complete', interrupt)
    try:
        from api_agent.providers.progress import progress_sink
        with progress_sink(reports.append if reported else None):
            with pytest.raises(interruption):
                local._run_private(work())
        assert not cleaned
    finally:
        monkeypatch.setattr(loop, 'run_until_complete', run)
        async def drain():
            release.set()
            await asyncio.gather(*owned)
        run(drain())
        loop.close()
    assert cleaned == ['drained']
    assert all(task.done() and task.exception() is None for task in owned)
    if reported:
        assert reports and reports[-1].active is False


def test_loop_refusal_closes_only_never_started_coroutines(monkeypatch):
    loop = asyncio.new_event_loop()
    local = object.__new__(provider_setup.IntegrationLLM)
    local.loop = loop
    operations = []
    async def work():
        pytest.fail('Refused operation must not run')
    pending = work()
    def refuse(operation):
        operations.append(operation)
        raise RuntimeError('loop refused operation')
    monkeypatch.setattr(loop, 'run_until_complete', refuse)
    try:
        with pytest.raises(RuntimeError, match='loop refused'):
            local._run_private(pending)
        assert pending.cr_frame is None
        assert operations[0].cr_frame is None
    finally:
        pending.close()
        for operation in operations:
            operation.close()
        loop.close()


def test_expired_parent_does_not_cancel_a_borrowed_future(monkeypatch):
    from api_agent.providers import progress
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    loop = asyncio.new_event_loop()
    local = object.__new__(provider_setup.IntegrationLLM)
    local.loop = loop
    borrowed = loop.create_future()
    try:
        with report_timeout_sync('Workflow integration test', 1):
            clock[0] = 2
            with pytest.raises(TimeoutError):
                local._run_private(borrowed)
        assert not borrowed.done()
    finally:
        borrowed.cancel()
        loop.close()


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_real_loop_interruption_before_first_step_keeps_queued_operation_owned(monkeypatch, interruption):
    from api_agent.providers import progress
    from inspect import CORO_CREATED, getcoroutinestate
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    loop = asyncio.new_event_loop()
    local = object.__new__(provider_setup.IntegrationLLM)
    local.loop = loop
    async def work():
        pytest.fail('Expired queued provider work must not start')
    pending = work()
    step = loop._run_once
    def interrupt():
        raise interruption()
    monkeypatch.setattr(loop, '_run_once', interrupt)
    try:
        with report_timeout_sync('Workflow integration test', 1):
            with pytest.raises(interruption):
                local._run_private(pending)
            tasks = list(asyncio.all_tasks(loop))
            assert len(tasks) == 1
            assert getcoroutinestate(tasks[0].get_coro()) == CORO_CREATED
            assert pending.cr_frame is not None
            clock[0] = 2
            monkeypatch.setattr(loop, '_run_once', step)
            with pytest.raises(TimeoutError):
                loop.run_until_complete(tasks[0])
            assert pending.cr_frame is None
    finally:
        monkeypatch.setattr(loop, '_run_once', step)
        pending.close()
        loop.close()


@pytest.mark.parametrize('refused', [False, True])
def test_expired_or_refused_entry_preserves_borrowed_suspended_coroutine(monkeypatch, refused):
    from api_agent.providers import progress
    from inspect import CORO_SUSPENDED, getcoroutinestate
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    loop = asyncio.new_event_loop()
    local = object.__new__(provider_setup.IntegrationLLM)
    local.loop = loop
    drained = []
    async def work():
        try:
            await asyncio.sleep(0)
        finally:
            drained.append(True)
    pending = work()
    pending.send(None)
    run = loop.run_until_complete
    if refused:
        def refuse(operation):
            raise RuntimeError('refused')
        monkeypatch.setattr(loop, 'run_until_complete', refuse)
    try:
        with report_timeout_sync('Workflow integration test', 1):
            clock[0] = 2
            with pytest.raises(RuntimeError if refused else TimeoutError):
                local._run_private(pending)
        assert getcoroutinestate(pending) == CORO_SUSPENDED
        assert not drained
        monkeypatch.setattr(loop, 'run_until_complete', run)
        run(pending)
        assert drained == [True]
    finally:
        pending.close()
        loop.close()
