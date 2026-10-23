"""Dashboard ownership uses synthetic credentials and bounded observed pages."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from api_agent.runtime.local_tracing import DashboardError, LocalTracing, _origin


def config(**overrides):
    return {'TRACE_MODE':'LangFuse','TRACE_TARGET':'http://localhost:3000',
            'LANGFUSE_ADMIN_EMAIL':'operator@example.invalid','LANGFUSE_ADMIN_PASSWORD':'synthetic-password',**overrides}


def owner(values=None, ready=True):
    return LocalTracing(config() if values is None else values, emissions=Mock(), probe=Mock(return_value=ready))


def test_disabled_has_no_probe_or_credentials():
    tracing=owner({})
    asyncio.run(tracing.start())
    assert tracing.state=='disabled' and tracing.viewer_url is None
    assert tracing.interactive_credentials() is None
    tracing.probe.assert_not_called()


@pytest.mark.parametrize('values,viewer', [
    (config(), 'http://localhost:3000'),
    ({'TRACE_MODE':'OTEL','TRACE_TARGET':'http://localhost:4318/v1/traces'}, 'http://localhost:16686'),
    ({'TRACE_MODE':'OTEL','TRACE_TARGET':'http://localhost:4318/v1/traces','JAEGER_VIEWER_PORT':17686}, 'http://localhost:17686'),
    ({'TRACE_MODE':'OTEL','TRACE_TARGET':'https://collector.example.invalid/v1/traces'}, None),
    ({'TRACE_MODE':'OTEL','TRACE_TARGET':'https://collector.example.invalid/v1/traces','TRACE_VIEWER_URL':'https://viewer.example.invalid'}, 'https://viewer.example.invalid'),
])
def test_resolved_viewer_is_distinct_from_ingestion(values,viewer):
    tracing=owner(values)
    asyncio.run(tracing.start())
    assert tracing.viewer_url==viewer
    assert tracing.authentication=='not_attempted'
    if viewer is None:
        tracing.probe.assert_not_called()
    else:
        assert tracing.state=='ready'


@pytest.mark.parametrize('values', [config(TRACE_MODE='bad'), config(LANGFUSE_ADMIN_EMAIL=''),
    config(LANGFUSE_ADMIN_PASSWORD=None), config(LANGFUSE_ADMIN_PASSWORD='\x00'),
    config(TRACE_TARGET='https://user:password@example.invalid')])
def test_invalid_dashboard_fails_before_probe(values):
    tracing=owner(values)
    with pytest.raises(DashboardError): asyncio.run(tracing.start())
    tracing.probe.assert_not_called()


def test_readiness_failure_and_credential_cleanup():
    tracing=owner(ready=False)
    asyncio.run(tracing.start())
    assert tracing.state=='unavailable' and tracing.viewer_url is None
    assert tracing.interactive_credentials()==('operator@example.invalid','synthetic-password')
    assert tracing.emissions.register_secret.call_count==2
    asyncio.run(tracing.aclose())
    assert tracing.interactive_credentials() is None
    with pytest.raises(DashboardError): asyncio.run(tracing.login(object()))


def page():
    result=SimpleNamespace(url='http://localhost:3000/project/synthetic',route=AsyncMock(),
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        get_by_label=Mock(return_value=SimpleNamespace(fill=AsyncMock())),
        get_by_role=Mock(return_value=SimpleNamespace(click=AsyncMock(),is_visible=AsyncMock(return_value=True),first=SimpleNamespace(wait_for=AsyncMock()))), wait_for_url=AsyncMock())
    result.request=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status=200,
        json=AsyncMock(return_value={'user':{'email':'operator@example.invalid'}}))))
    return result


def test_observed_login_checks_identity_and_origin_guard():
    tracing=owner(); current=page()
    async def exercise():
        await tracing.start(); await tracing.login(current)
        assert tracing.authentication=='authenticated'
        guard=current.route.call_args.args[1]
        allowed=SimpleNamespace(request=SimpleNamespace(url='http://localhost:3000/api/auth/callback/credentials'),abort=AsyncMock(),continue_=AsyncMock())
        await guard(allowed); allowed.continue_.assert_awaited_once(); allowed.abort.assert_not_awaited()
        denied=SimpleNamespace(request=SimpleNamespace(url='https://elsewhere.example.invalid/'),abort=AsyncMock(),continue_=AsyncMock())
        await guard(denied); denied.abort.assert_awaited_once(); denied.continue_.assert_not_awaited()
        predicate=current.wait_for_url.call_args.args[0]
        assert predicate('http://localhost:3000/project/synthetic')
        assert not predicate('http://localhost:3000/auth/sign-in')
        assert not predicate('https://elsewhere.example.invalid/project/x')
    asyncio.run(exercise())
    current.get_by_label.assert_any_call('Email',exact=True)
    current.get_by_role.assert_any_call('button',name='Continue',exact=True)
    current.get_by_role.assert_any_call('button',name='Sign in',exact=True)
    current.request.get.assert_awaited_once_with('http://localhost:3000/api/auth/session',max_redirects=0)


@pytest.mark.parametrize('failure',['initial-redirect','later-redirect','session-status','wrong-account','missing-project','exception','cancel'])
def test_login_failure_is_fixed_and_never_authenticated(failure):
    tracing=owner(); current=page()
    if failure=='initial-redirect': current.url='https://elsewhere.example.invalid/auth/sign-in'
    if failure=='later-redirect': current.get_by_role.return_value.click.side_effect=lambda:setattr(current,'url','https://elsewhere.example.invalid/')
    if failure=='session-status': current.request.get.return_value.status=401
    if failure=='wrong-account': current.request.get.return_value.json.return_value={'user':{'email':'someone@example.invalid'}}
    if failure=='missing-project': current.get_by_role.return_value.first.wait_for.side_effect=RuntimeError('project unavailable')
    if failure=='exception': current.goto.side_effect=RuntimeError('synthetic-password raw backend')
    if failure=='cancel': current.goto.side_effect=asyncio.CancelledError()
    async def exercise():
        await tracing.start()
        with pytest.raises(asyncio.CancelledError if failure=='cancel' else DashboardError) as caught:
            await tracing.login(current)
        assert 'synthetic-password' not in str(caught.value)
        assert tracing.authentication!='authenticated'
    asyncio.run(exercise())


@pytest.mark.parametrize('status,url',[(200,'http://localhost:16686'),(503,'http://localhost:16686'),(200,'https://elsewhere.example.invalid')])
def test_otel_navigation_verifies_response_and_origin(status,url):
    tracing=owner({'TRACE_MODE':'OTEL','TRACE_TARGET':'http://localhost:4318/v1/traces'});current=page()
    current.url=url;current.goto.return_value.status=status
    async def exercise():
        await tracing.start()
        if status==200 and url=='http://localhost:16686': await tracing.login(current)
        else:
            with pytest.raises(DashboardError): await tracing.login(current)
    asyncio.run(exercise())
    current.get_by_label.assert_not_called()


def test_origin_normalizes_default_port():
    assert _origin('https://example.invalid')==_origin('https://example.invalid:443')


def test_probe_requires_verified_tls_and_rejects_redirects(monkeypatch):
    from urllib.error import URLError
    from api_agent.runtime import local_tracing
    opened=Mock()
    opened.open.return_value.__enter__=Mock(return_value=SimpleNamespace(status=200))
    opened.open.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setattr(local_tracing,'build_opener',Mock(return_value=opened))
    tls=Mock(); monkeypatch.setattr(local_tracing,'verified_client_context',Mock(return_value=tls))
    assert local_tracing._probe('https://example.invalid/auth/sign-in','ca.pem')
    local_tracing.verified_client_context.assert_called_once_with('ca.pem')
    opened.open.return_value.__enter__.return_value.status=503
    assert not local_tracing._probe('https://example.invalid/auth/sign-in',None)
    opened.open.side_effect=URLError('private diagnostic')
    assert not local_tracing._probe('https://example.invalid/auth/sign-in',None)
    with pytest.raises(URLError): local_tracing._NoRedirect().redirect_request()


def test_otel_none_response_is_actionable():
    tracing=owner({'TRACE_MODE':'OTEL','TRACE_TARGET':'http://localhost:4318/v1/traces'});current=page()
    current.goto.return_value=None
    async def exercise():
        await tracing.start()
        with pytest.raises(DashboardError): await tracing.login(current)
    asyncio.run(exercise())


def test_invalid_explicit_viewer_is_rejected():
    tracing=owner({'TRACE_MODE':'OTEL','TRACE_TARGET':'https://collector.example.invalid/v1/traces',
                   'TRACE_VIEWER_URL':'file:///tmp/viewer'})
    with pytest.raises(DashboardError): asyncio.run(tracing.start())


def test_observed_pinned_single_step_login():
    tracing=owner();current=page()
    current.get_by_role.return_value.is_visible.return_value=False
    async def exercise():
        await tracing.start();await tracing.login(current)
        assert tracing.authentication=='authenticated'
    asyncio.run(exercise())
    assert current.get_by_role.return_value.click.await_count==1


async def wait_for_thread_signal(signal):
    async with asyncio.timeout(2):
        while not signal.is_set():
            await asyncio.sleep(.001)


@pytest.mark.parametrize('timeout',[0,-1,True,float('inf'),float('nan'),'5'])
def test_cleanup_timeout_requires_a_positive_finite_number(timeout):
    from api_agent.providers.contracts import ContractError
    with pytest.raises(ContractError,match='Invalid cleanup timeout'):
        LocalTracing(config(),emissions=Mock(),cleanup_timeout=timeout)


def test_cancelled_start_keeps_readiness_until_bounded_cleanup_retry():
    from threading import Event
    from api_agent.providers.control import CleanupPendingError
    entered,release,finished = Event(),Event(),Event()
    def probe(*args):
        entered.set()
        try:
            assert release.wait(3)
            return True
        finally:
            finished.set()
    async def run():
        tracing = LocalTracing(config(),emissions=Mock(),probe=probe,cleanup_timeout=.02)
        start = asyncio.create_task(tracing.start())
        try:
            await wait_for_thread_signal(entered)
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            with pytest.raises(CleanupPendingError) as pending:
                await tracing.aclose()
            assert pending.value.owners==(tracing,)
            assert not finished.is_set()
            assert tracing.interactive_credentials()==('operator@example.invalid','synthetic-password')
            release.set()
            await wait_for_thread_signal(finished)
            await pending.value.retry()
            assert tracing.interactive_credentials() is None
            assert tracing.viewer_url is None
            await tracing.aclose()
            with pytest.raises(DashboardError,match='closed'):
                await tracing.start()
        finally:
            release.set()
            await tracing.aclose()
    asyncio.run(run())


def test_concurrent_start_and_close_callers_share_one_probe_and_cancelled_closer_retains_it():
    from threading import Event
    entered,release,finished = Event(),Event(),Event()
    calls = []
    def probe(*args):
        calls.append(args)
        entered.set()
        try:
            assert release.wait(3)
            return True
        finally:
            finished.set()
    async def run():
        tracing = LocalTracing(config(),emissions=Mock(),probe=probe,cleanup_timeout=1)
        starts = [asyncio.create_task(tracing.start()) for _ in range(2)]
        try:
            await wait_for_thread_signal(entered)
            closes = [asyncio.create_task(tracing.aclose()) for _ in range(2)]
            await asyncio.sleep(0)
            closes[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await closes[0]
            assert not closes[1].done()
            assert tracing.interactive_credentials() is not None
            release.set()
            await closes[1]
            assert finished.is_set() and tracing.interactive_credentials() is None
            assert await asyncio.gather(*starts)==[tracing,tracing]
            assert tracing.viewer_url is None
            assert len(calls)==1
        finally:
            release.set()
            await asyncio.gather(*starts,return_exceptions=True)
            await tracing.aclose()
    asyncio.run(run())


def test_probe_exception_is_fixed_and_consumed_after_start_cancellation():
    from threading import Event
    entered,release,finished = Event(),Event(),Event()
    def probe(*args):
        entered.set()
        try:
            assert release.wait(3)
            raise RuntimeError('synthetic-password backend payload')
        finally:
            finished.set()
    async def run():
        tracing = LocalTracing(config(),emissions=Mock(),probe=probe,cleanup_timeout=1)
        start = asyncio.create_task(tracing.start())
        try:
            await wait_for_thread_signal(entered)
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            release.set()
            await tracing.aclose()
            assert finished.is_set() and tracing.interactive_credentials() is None
        finally:
            release.set()
    asyncio.run(run())


def test_active_probe_exception_has_no_backend_text():
    tracing = LocalTracing(config(),emissions=Mock(),probe=Mock(side_effect=RuntimeError('synthetic-password raw payload')))
    async def run():
        with pytest.raises(DashboardError,match='readiness failed') as caught:
            await tracing.start()
        assert 'synthetic-password' not in str(caught.value)
        await tracing.aclose()
    asyncio.run(run())


def test_cancelled_readiness_task_done_callback_does_not_raise():
    async def run():
        task = asyncio.create_task(asyncio.sleep(1))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        LocalTracing._probe_finished(task)
    asyncio.run(run())


def test_local_cleanup_retains_later_dependencies_until_probe_retry_completes():
    from threading import Event
    from api_agent.providers.control import CleanupPendingError
    from api_agent.runtime.local_run import LocalCleanup
    entered,release,finished = Event(),Event(),Event()
    def probe(*args):
        entered.set()
        try:
            assert release.wait(3)
            return True
        finally:
            finished.set()
    async def run():
        tracing = LocalTracing(config(),emissions=Mock(),probe=probe,cleanup_timeout=.02)
        later = AsyncMock()
        cleanup = LocalCleanup()
        cleanup.push(later)
        cleanup.push(tracing.aclose)
        start = asyncio.create_task(tracing.start())
        try:
            await wait_for_thread_signal(entered)
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            with pytest.raises(CleanupPendingError):
                await cleanup.aclose()
            assert len(cleanup.callbacks)==2 and cleanup.pending is not None
            assert tracing.interactive_credentials() is not None
            later.assert_not_awaited()
            release.set()
            await wait_for_thread_signal(finished)
            await cleanup.aclose()
            assert cleanup.callbacks==[] and cleanup.pending is None
            later.assert_awaited_once()
        finally:
            release.set()
            await tracing.aclose()
    asyncio.run(run())
