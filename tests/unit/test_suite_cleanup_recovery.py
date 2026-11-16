"""Draft regression tests; copy into tests/unit only after authorization."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.loading import LoadedProvider
from api_agent.testing import integration
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult


class FailsFirstClose:
    capabilities = ProviderCapabilities(kind='test_prerequisites', operations=('prepare',))

    def __init__(self):
        self.close_calls = 0
        self.failures = 1
        self.outcome = 'passed'

    async def aprepare(self, request, *, auth=None):
        pass

    async def aclose(self):
        self.close_calls += 1
        if self.close_calls <= self.failures:
            raise RuntimeError('first close pending')


@pytest.fixture
def completed_wave(monkeypatch):
    provider = FailsFirstClose()
    async def load(*args, **kwargs):
        return LoadedProvider(provider, 'synthetic_provider')
    monkeypatch.setattr('api_agent.testing.suite_prerequisite_provider.aload_provider', load)
    case = TestIdentity('fixture::case', 'fixture', 'case.py', 'case.py::case',
                        tags=frozenset({'requires_ui_session'}))
    def wave(context, tests, **options):
        options['before_run']()
        outcome = TestResult(case, provider.outcome)
        options['on_result'](outcome)
        return integration.IntegrationRunResult(int(provider.outcome != 'passed'), (outcome,), '')
    monkeypatch.setattr(integration, '_run_integration_tests', wave)
    return case, provider


@pytest.mark.parametrize(('status', 'exit_code'), [('passed', 0), ('failed', 1)])
def test_successful_canonical_close_after_pending_preserves_completed_execution(tmp_path, completed_wave, status, exit_code):
    case, provider = completed_wave
    provider.outcome = status
    published = []
    try:
        result = integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'},
            after_run=published.append)
    except CleanupPendingError as pending:
        # Red control must release the suite lease before the next test.
        asyncio.run(pending.retry())
        raise
    assert provider.close_calls == 2
    assert result.exit_code == exit_code
    assert result.tests[0].status == status
    assert published == [result]


def test_independent_cleanup_duty_survives_successful_prerequisite_close(tmp_path, completed_wave, monkeypatch):
    case, provider = completed_wave
    independent_retry = AsyncMock()
    independent = CleanupPendingError((object(),), independent_retry)
    original_close = integration._close_suite_prerequisites
    def close_with_independent_duty(*args):
        try:
            original_close(*args)
        except CleanupPendingError as pending:
            pending.cleanup_pending = independent
            raise
    monkeypatch.setattr(integration, '_close_suite_prerequisites', close_with_independent_duty)
    with pytest.raises(CleanupPendingError) as failure:
        integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'})
    try:
        assert provider.close_calls == 2
        assert failure.value.cleanup_pending is independent
        independent_retry.assert_not_awaited()
        asyncio.run(failure.value.retry())
        from api_agent.testing.suite_lock import SuiteBusyError, suite_lock
        with pytest.raises(SuiteBusyError):
            with suite_lock():
                pass
    finally:
        asyncio.run(failure.value.retry())
        asyncio.run(independent.retry())


def test_failed_second_close_preserves_both_cleanup_receipts(tmp_path, completed_wave):
    case, provider = completed_wave
    provider.failures = 2
    with pytest.raises(CleanupPendingError) as failure:
        integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'})
    primary = failure.value
    secondary, = primary.confirmation_cleanup_errors
    try:
        assert isinstance(secondary, CleanupPendingError)
        assert secondary is not primary
        assert primary.owners == secondary.owners
        assert provider.close_calls == 3
    finally:
        asyncio.run(primary.retry())
        asyncio.run(secondary.retry())


def test_retry_return_without_provider_acknowledgement_stays_pending(tmp_path, completed_wave, monkeypatch):
    case, provider = completed_wave
    original_close = integration._close_suite_prerequisites
    calls = 0
    def close_with_unacknowledged_retry(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            return  # Models an inspection retaining the owner during retry.
        original_close(*args)
    monkeypatch.setattr(integration, '_close_suite_prerequisites', close_with_unacknowledged_retry)
    with pytest.raises(CleanupPendingError) as failure:
        integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'})
    try:
        assert calls == 3
        assert provider.close_calls == 2
    finally:
        asyncio.run(failure.value.retry())


def test_prior_nonreceipt_cleanup_failure_is_not_discarded(tmp_path, completed_wave, monkeypatch):
    case, provider = completed_wave
    other_failure = RuntimeError('independent cleanup failure')
    original_close = integration._close_suite_prerequisites
    def close_with_failure(*args):
        try:
            original_close(*args)
        except CleanupPendingError as pending:
            pending.confirmation_cleanup_errors = (other_failure,)
            raise
    monkeypatch.setattr(integration, '_close_suite_prerequisites', close_with_failure)
    with pytest.raises(CleanupPendingError) as failure:
        integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'})
    try:
        assert provider.close_calls == 2
        assert failure.value.confirmation_cleanup_errors == (other_failure,)
    finally:
        asyncio.run(failure.value.retry())
