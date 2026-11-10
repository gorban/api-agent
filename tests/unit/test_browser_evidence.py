"""Controlled UI capture contracts; no live browser or credential access."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing import browser_evidence as evidence

PNG = evidence.PNG_SIGNATURE + b'controlled-pixels'


def capture(contexts, **kwargs):
    boundary = kwargs.pop('boundary', EmissionBoundary(ContentPolicy(True)))
    return asyncio.run(evidence.capture_ui_evidence(contexts, boundary,
                       kwargs.pop('submit', lambda attachment: None), **kwargs))


def test_content_off_does_not_enumerate_or_submit():
    class Unreadable:
        def __iter__(self):
            raise AssertionError('Browser accessed')
    policy, submit = AsyncMock(), Mock()
    assert capture(Unreadable(), boundary=EmissionBoundary(), capture_policy=policy, submit=submit) == ()
    policy.assert_not_called()
    submit.assert_not_called()


def test_selected_owned_capture_uses_bounded_timeout(monkeypatch):
    durations = []
    @asynccontextmanager
    async def timed(name, seconds):
        durations.append((name, seconds))
        yield
    monkeypatch.setattr(evidence, 'component_timeout', timed)
    page = object()
    assert capture([SimpleNamespace(pages=[page])], capture_policy=AsyncMock(return_value=PNG),
        capture_timeout_seconds=30)[0][2] == 'image/png'
    assert durations == [('UI browser evidence', 30)]
    for invalid in (0, 31, 5.0, True):
        with pytest.raises(ValueError, match='capture timeout'):
            capture([SimpleNamespace(pages=[page])], capture_policy=AsyncMock(return_value=PNG),
                capture_timeout_seconds=invalid)


def test_default_capture_budget_allows_full_original_pixel_audit(monkeypatch):
    durations = []
    @asynccontextmanager
    async def timed(name, seconds):
        durations.append((name, seconds))
        yield
    monkeypatch.setattr(evidence, 'component_timeout', timed)

    attachments = capture([SimpleNamespace(pages=[object()])],
        capture_policy=AsyncMock(return_value=PNG))

    assert attachments[0][2] == 'image/png'
    assert durations == [('UI browser evidence', 10)]


def test_selected_timeout_allows_a_slow_owned_page_without_changing_default():
    async def slow_page(*args):
        await asyncio.sleep(1.2)
        return PNG
    context = [SimpleNamespace(pages=[object()])]
    assert capture(context, capture_policy=slow_page, capture_timeout_seconds=1)[0][2] == 'application/json'
    assert capture(context, capture_policy=slow_page, capture_timeout_seconds=2)[0][2] == 'image/png'


@pytest.mark.parametrize('phase', ['success', 'failure'])
def test_all_pages_use_owned_policy_and_immutable_submission(phase):
    pages = [object(), object(), object()]
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
    policy, submit = AsyncMock(return_value=PNG), Mock()
    rows = capture([SimpleNamespace(pages=pages[:2]), SimpleNamespace(pages=pages[2:])],
                   boundary=boundary, capture_policy=policy, submit=submit, phase=phase)
    assert [call.args for call in policy.await_args_list] == [(page, boundary) for page in pages]
    assert rows == tuple(call.args[0] for call in submit.call_args_list)
    assert rows[-1] == (f'UI {phase} context 2 page 1', PNG, 'image/png')


@pytest.mark.parametrize('value', [None, b'not PNG', bytearray(PNG)])
def test_unsafe_or_invalid_capture_only_records_fixed_metadata(value):
    rows = capture([SimpleNamespace(pages=[object()])], capture_policy=AsyncMock(return_value=value))
    assert json.loads(rows[0][1]) == {'status': 'unavailable', 'phase': 'failure', 'context': 1, 'page': 1}


def test_missing_policy_never_reads_page():
    assert capture([SimpleNamespace(pages=[object()])])[0][2] == 'application/json'


def test_failed_capture_and_closed_context_continue():
    class Closed:
        @property
        def pages(self):
            raise RuntimeError('private error and URL')
    policy = AsyncMock(side_effect=[RuntimeError('private failure'), PNG])
    rows = capture([Closed(), SimpleNamespace(pages=[object(), object()])], capture_policy=policy)
    assert [row[2] for row in rows] == ['application/json', 'application/json', 'image/png']
    assert all(b'private' not in row[1] for row in rows)


def test_context_enumeration_failure_is_unavailable():
    class Broken:
        def __iter__(self):
            raise RuntimeError('private')
    assert json.loads(capture(Broken())[0][1])['context'] == 0


def test_submission_failure_continues():
    submit = Mock(side_effect=[OSError('private'), None])
    rows = capture([SimpleNamespace(pages=[object(), object()])], submit=submit,
                   capture_policy=AsyncMock(return_value=PNG))
    assert len(rows) == 1 and rows[0][0].endswith('page 2')


@pytest.mark.parametrize('where', ['capture', 'submit'])
def test_cancellation_propagates(where):
    policy = AsyncMock(side_effect=asyncio.CancelledError) if where == 'capture' else AsyncMock(return_value=PNG)
    submit = Mock(side_effect=asyncio.CancelledError) if where == 'submit' else Mock()
    with pytest.raises(asyncio.CancelledError):
        capture([SimpleNamespace(pages=[object()])], capture_policy=policy, submit=submit)


def test_invalid_phase_never_enumerates():
    assert capture(None, phase='private') == ()


def test_page_limit_bounds_work(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_PAGES', 2)
    policy = AsyncMock(return_value=PNG)
    rows = capture([SimpleNamespace(pages=[object()] * 4)], capture_policy=policy)
    assert policy.await_count == 2
    assert len(rows) == 3 and json.loads(rows[-1][1])['page'] == 0


def test_context_limit_bounds_work(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_CONTEXTS', 1)
    policy = AsyncMock(return_value=PNG)
    rows = capture([SimpleNamespace(pages=[object()])] * 3, capture_policy=policy)
    assert policy.await_count == 1 and len(rows) == 2
    assert json.loads(rows[-1][1])['context'] == 0


@pytest.mark.parametrize('limit', ['MAX_IMAGE_BYTES', 'MAX_TOTAL_BYTES'])
def test_pixel_byte_limits_fail_unavailable(monkeypatch, limit):
    monkeypatch.setattr(evidence, limit, len(PNG) - 1)
    rows = capture([SimpleNamespace(pages=[object()])], capture_policy=AsyncMock(return_value=PNG))
    assert rows[0][2] == 'application/json'


def test_empty_context_has_no_evidence():
    assert capture([SimpleNamespace(pages=[])]) == ()


@pytest.mark.parametrize('hook', [None, lambda **kwargs: PNG])
def test_capture_capability_rejects_missing_or_sync_hook(hook):
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.loading import ProviderError, validate_capabilities
    provider = SimpleNamespace(capabilities=ProviderCapabilities('ui', ('capture_evidence',)), acapture_evidence=hook)
    with pytest.raises(ProviderError):
        validate_capabilities(provider, 'ui')


def test_capture_capability_validates_keywords():
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.loading import ProviderError, validate_capabilities
    async def valid(*, page, boundary):
        return None
    async def invalid(*, page):
        return None
    provider = SimpleNamespace(capabilities=ProviderCapabilities('ui', ('capture_evidence',)), acapture_evidence=valid)
    validate_capabilities(provider, 'ui')
    provider.acapture_evidence = invalid
    with pytest.raises(ProviderError):
        validate_capabilities(provider, 'ui')


def test_submission_precedes_async_progress_scope_exit():
    from api_agent.providers.progress import progress_sink
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    boundary = EmissionBoundary(ContentPolicy(True))
    page = object()
    context = SimpleNamespace(pages=[page])
    order = []
    async def policy(page, boundary):
        order.append('policy')
        return evidence.PNG_SIGNATURE + b'controlled'
    def submit(attachment):
        assert 'late' not in boundary._secrets
        order.append('submit')
    def progress(component):
        if not component.active:
            boundary.register_secret('late')
            order.append('scope-exit')
    async def run():
        with progress_sink(progress):
            await evidence.capture_ui_evidence((context,), boundary, submit, capture_policy=policy)
    asyncio.run(run())
    assert order == ['policy','submit','scope-exit']


def test_excluded_pages_preserve_context_and_page_numbers():
    covered, other, second_context = object(), object(), object()
    policy = AsyncMock(return_value=PNG)
    rows = capture([SimpleNamespace(pages=[covered, other]),
                    SimpleNamespace(pages=[second_context])],
                   capture_policy=policy, phase='success', exclude_pages=(covered,))
    assert [call.args[0] for call in policy.await_args_list] == [other, second_context]
    assert [row[0] for row in rows] == ['UI success context 1 page 2', 'UI success context 2 page 1']


def test_extended_capture_keeps_parent_deadline_and_drains_cancelled_policy():
    from api_agent.providers.progress import component_timeout
    async def run():
        cleaned, submitted = [], []
        async def policy(*_):
            try:
                await asyncio.sleep(60)
                return PNG
            finally:
                cleaned.append(True)
        with pytest.raises(TimeoutError):
            async with component_timeout('Short parent', 0.05):
                await evidence.capture_ui_evidence([SimpleNamespace(pages=[object()])],
                    EmissionBoundary(ContentPolicy(True)), submitted.append,
                    capture_policy=policy, capture_timeout_seconds=30)
        assert cleaned == [True]
        assert not any(row[2] == 'image/png' for row in submitted)
    asyncio.run(run())


def test_expired_extended_capture_cannot_publish_late_pixels(monkeypatch):
    from api_agent.providers.progress import component_timeout
    declared, cleaned, submitted = [], [], []
    @asynccontextmanager
    async def accelerated(name, seconds):
        declared.append(seconds)
        async with component_timeout(name, 0.05):
            yield
    monkeypatch.setattr(evidence, 'component_timeout', accelerated)
    async def policy(*_):
        try:
            await asyncio.sleep(60)
            return PNG
        finally:
            cleaned.append(True)
    rows = capture([SimpleNamespace(pages=[object()])], capture_policy=policy,
        capture_timeout_seconds=30, submit=submitted.append)
    assert declared == [30] and cleaned == [True]
    assert rows == tuple(submitted)
    assert [row[2] for row in submitted] == ['application/json']
