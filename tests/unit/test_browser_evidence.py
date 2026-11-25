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
    for invalid in (0, 36, 5.0, True):
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


@pytest.mark.parametrize('page_budget', [30, 35])
def test_extended_capture_keeps_parent_deadline_and_drains_cancelled_policy(page_budget):
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
            # Long enough for the policy to start on a slow host, far below the capture timeout.
            async with component_timeout('Short parent', 1):
                await evidence.capture_ui_evidence([SimpleNamespace(pages=[object()])],
                    EmissionBoundary(ContentPolicy(True)), submitted.append,
                    capture_policy=policy, capture_timeout_seconds=page_budget)
        assert cleaned == [True]
        assert not any(row[2] == 'image/png' for row in submitted)
    asyncio.run(run())


@pytest.mark.parametrize('page_budget', [30, 35])
def test_expired_extended_capture_cannot_publish_late_pixels(monkeypatch, page_budget):
    from api_agent.providers.progress import component_timeout
    declared, cleaned, submitted = [], [], []
    @asynccontextmanager
    async def accelerated(name, seconds):
        declared.append(seconds)
        # Long enough for the policy to start on a slow host, far below the declared 30 seconds.
        async with component_timeout(name, 1):
            yield
    monkeypatch.setattr(evidence, 'component_timeout', accelerated)
    async def policy(*_):
        try:
            await asyncio.sleep(60)
            return PNG
        finally:
            cleaned.append(True)
    rows = capture([SimpleNamespace(pages=[object()])], capture_policy=policy,
        capture_timeout_seconds=page_budget, submit=submitted.append)
    assert declared == [page_budget] and cleaned == [True]
    assert rows == tuple(submitted)
    assert [row[2] for row in submitted] == ['application/json']


def test_explicit_35_second_window_allows_transient_retry_but_not_late_capture(monkeypatch):
    from api_agent.ui import evidence as viewport
    from virtual_time import run as virtual_run
    events = []
    monkeypatch.setattr(viewport, 'monotonic', lambda: asyncio.get_running_loop().time())
    monkeypatch.setattr(viewport, 'CAPTURE_RETRY_DELAY_SECONDS', 0)

    async def case(timeout, final_delay):
        page = SimpleNamespace()
        attempts = 0
        async def staged(page, boundary, origin, *, unavailable, **kwargs):
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(12 if attempts == 1 else final_delay)
            if attempts == 1:
                unavailable('document_changed', None)
                return None
            return PNG
        monkeypatch.setattr(viewport, '_capture_viewport_once', staged)
        async def policy(page, boundary):
            return await viewport.capture_studio_viewport(page, boundary, 'https://owned.invalid', retry_window_seconds=25)
        diagnostics, submitted = [], []
        rows = await evidence.capture_ui_evidence([SimpleNamespace(pages=[page])],
            EmissionBoundary(ContentPolicy(True)), submitted.append, capture_policy=policy,
            capture_timeout_seconds=timeout, capture_outcome=diagnostics.append)
        events.append((attempts, rows[0][2], diagnostics))
    virtual_run(case(30, 19.5))
    virtual_run(case(35, 19.5))
    virtual_run(case(35, 24))
    assert events == [(2, 'application/json', ['timeout']), (2, 'image/png', []),
                      (2, 'application/json', ['timeout'])]


def test_fixed_capture_outcomes_never_reveal_exception_or_policy_data():
    for candidate in (AsyncMock(return_value=None), AsyncMock(return_value=b'private'),
                      AsyncMock(side_effect=RuntimeError('private reason'))):
        outcomes = []
        rows = capture([SimpleNamespace(pages=[object()])], capture_policy=candidate,
                       capture_outcome=outcomes.append)
        assert outcomes == ['rejected']
        assert b'private' not in rows[0][1]


def test_capture_outcome_callback_cannot_replace_failure_or_observe_private_error():
    outcomes = []
    def callback(value):
        outcomes.append(value)
        raise RuntimeError('private callback detail')
    rows = capture([SimpleNamespace(pages=[object()])],
                   capture_policy=AsyncMock(side_effect=RuntimeError('private capture detail')),
                   capture_outcome=callback, capture_timeout_seconds=35)
    assert outcomes == ['rejected']
    assert rows[0][2] == 'application/json' and b'private' not in rows[0][1]


def test_cancelled_capture_has_no_outcome_or_submission():
    outcomes, submitted = [], []
    with pytest.raises(asyncio.CancelledError):
        capture([SimpleNamespace(pages=[object()])], capture_outcome=outcomes.append,
                submit=submitted.append, capture_timeout_seconds=35,
                capture_policy=AsyncMock(side_effect=asyncio.CancelledError))
    assert not outcomes and not submitted


def test_explicit_35_second_budget_is_admitted_without_changing_default():
    rows = capture([SimpleNamespace(pages=[object()])],
                   capture_policy=AsyncMock(return_value=PNG), capture_timeout_seconds=35)
    assert rows[0][2] == 'image/png'


def test_failed_png_submission_reports_rejection_without_second_attachment():
    attempts, outcomes = [], []
    def fail(attachment):
        attempts.append(attachment)
        raise OSError('private')
    rows = capture([SimpleNamespace(pages=[object()])], submit=fail,
                   capture_policy=AsyncMock(return_value=PNG), capture_outcome=outcomes.append)
    assert rows == () and len(attempts) == 1 and attempts[0][2] == 'image/png'
    assert outcomes == ['rejected']
