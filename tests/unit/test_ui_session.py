"""Original-session ownership and declared UI preparation controls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.contracts import BrowserSession, ContractError, ProviderCapabilities, SessionClosedError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.loading import ProviderError, validate_capabilities
from api_agent.providers.sessions import ScopedRoleCredentials
from api_agent.testing.ui_session import prepare_ui_session
from api_agent.ui.session import PreparedUISession


@pytest.fixture
def resources():
    context = object()
    page = SimpleNamespace(context=context, goto=AsyncMock(), is_closed=lambda: False)
    browser = SimpleNamespace(is_connected=lambda: True)
    session = BrowserSession(browser, context, page)
    browser_owner = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
    implementation = SimpleNamespace(capabilities=ProviderCapabilities('ui', ('launch',)),
        astart=AsyncMock(), aprepare_session=AsyncMock())
    provider = SimpleNamespace(require=Mock(return_value=implementation), astart=AsyncMock(), aclose=AsyncMock())
    loader = AsyncMock(return_value=provider)
    runtime = SimpleNamespace(aclose=AsyncMock())
    owner = PreparedUISession({'UI_PROVIDER':'selected', 'UI_LISTENER_URL':'http://localhost:9443'},
        {'runtime':'injected'}, browser_owner=browser_owner, loader=loader, runtime_owner=runtime)
    return SimpleNamespace(**locals())


def test_anonymous_original_handoff_and_idempotent_cleanup(resources):
    r = resources
    async def exercise():
        assert await r.owner.start() is r.session
        assert await r.owner.start() is r.session
        r.loader.assert_called_once_with('selected', r.owner.config, {'runtime':'injected'}, kind='ui')
        r.provider.astart.assert_awaited_once()
        r.page.goto.assert_awaited_once_with('http://localhost:9443')
        r.implementation.aprepare_session.assert_not_awaited()
        await r.owner.aclose()
        await r.owner.aclose()
        for child in (r.browser_owner, r.provider, r.runtime):
            child.aclose.assert_awaited_once()
        with pytest.raises(SessionClosedError):
            await r.owner.start()
    asyncio.run(exercise())


def test_preparation_updated_page_and_scoped_credentials(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'), ('ui',))
    reader = ScopedRoleCredentials(('ui',), 2, AsyncMock())
    r.owner.user_index, r.owner.credentials = 2, reader
    page = SimpleNamespace(context=r.context, is_closed=lambda:False)
    async def prepare(**kwargs):
        assert kwargs == dict(session=r.session, user_index=2, credentials=reader)
        kwargs['session'].page = page
    r.implementation.aprepare_session.side_effect = prepare
    async def exercise():
        assert (await r.owner.start()).page is page
        await r.owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['browser', 'context', 'page', 'closed', 'disconnected', 'return', 'raise', 'cancel'])
def test_preparation_refuses_invalid_or_failed_handoff(resources, failure):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'))
    async def prepare(**kwargs):
        if failure == 'browser': r.session.browser = object()
        if failure == 'context': r.session.context = object()
        if failure == 'page': r.page.context = object()
        if failure == 'closed': r.page.is_closed = lambda:True
        if failure == 'disconnected': r.browser.is_connected = lambda:False
        if failure == 'return': return True
        if failure == 'raise': raise RuntimeError('controlled failure')
        if failure == 'cancel': raise asyncio.CancelledError()
    r.implementation.aprepare_session.side_effect = prepare
    async def exercise():
        with pytest.raises((ContractError, RuntimeError, asyncio.CancelledError)):
            await r.owner.start()
        assert r.owner.closed
        r.provider.aclose.assert_awaited_once()
        r.browser_owner.aclose.assert_awaited_once()
    asyncio.run(exercise())


@pytest.mark.parametrize('reader,index', [(None, 0), (ScopedRoleCredentials(('other',), 0, AsyncMock()), 0),
    (ScopedRoleCredentials(('ui',), 1, AsyncMock()), 0), (ScopedRoleCredentials(('ui',), 0, AsyncMock()), None)])
def test_bad_credential_binding_refused_before_launch(resources, reader, index):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'), ('ui',))
    r.owner.user_index, r.owner.credentials = index, reader
    async def exercise():
        with pytest.raises(ContractError): await r.owner.start()
        r.provider.astart.assert_not_awaited()
    asyncio.run(exercise())


@pytest.mark.parametrize('index', [-1, True, '1'])
def test_invalid_index(index):
    with pytest.raises(ContractError): PreparedUISession({}, {}, browser_owner=None, user_index=index)


def test_cleanup_retry_retains_provider_until_browser_released(resources):
    r = resources
    r.browser_owner.aclose.side_effect = [RuntimeError(), None]
    async def exercise():
        await r.owner.start()
        with pytest.raises(CleanupPendingError): await r.owner.aclose()
        r.provider.aclose.assert_not_awaited()
        await r.owner.aclose()
        r.provider.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_start_failure_retains_cleanup_duty(resources):
    r = resources
    r.loader.side_effect = RuntimeError('load failed')
    r.browser_owner.aclose.side_effect = [RuntimeError(), None]
    async def exercise():
        with pytest.raises(RuntimeError) as caught: await r.owner.start()
        assert isinstance(caught.value.cleanup_pending, CleanupPendingError)
        await caught.value.cleanup_pending.retry()
    asyncio.run(exercise())


@pytest.mark.parametrize('config', [{}, {'UI_PROVIDER':'selected', 'UI_LISTENER_URL':'https://example.com'}])
def test_missing_or_invalid_selection(resources, config):
    resources.owner.config = config
    async def exercise():
        with pytest.raises((ContractError, ValueError)): await resources.owner.start()
        resources.loader.assert_not_called()
    asyncio.run(exercise())


def test_testing_adapter(resources):
    r = resources
    async def exercise():
        owner = await prepare_ui_session(r.owner.config, {}, browser_owner=r.browser_owner, loader=r.loader)
        assert owner.session is r.session
        await owner.aclose()
    asyncio.run(exercise())


async def wrong_signature():
    pass


@pytest.mark.parametrize('hook', [None, lambda **kwargs:None, wrong_signature])
def test_loader_rejects_malformed_advertised_hook(hook):
    adapter = SimpleNamespace(capabilities=ProviderCapabilities('ui', ('prepare_session',)), aprepare_session=hook)
    with pytest.raises(ProviderError): validate_capabilities(adapter, 'ui')


def test_loader_accepts_version_one_optional_preparation():
    async def prepare(*, session, user_index, credentials): pass
    validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities('ui', ('prepare_session',)),
        aprepare_session=prepare), 'ui')


def test_close_cancels_inflight_preparation(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'))
    async def exercise():
        entered = asyncio.Event()
        async def prepare(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        r.implementation.aprepare_session.side_effect = prepare
        task = asyncio.create_task(r.owner.start())
        await entered.wait()
        await r.owner.aclose()
        with pytest.raises(asyncio.CancelledError): await task
        r.provider.aclose.assert_awaited_once()
        r.browser_owner.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_closed_owner_cannot_finish_handoff(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'))
    async def prepare(**kwargs):
        r.owner.closed = True
    r.implementation.aprepare_session.side_effect = prepare
    async def exercise():
        with pytest.raises(SessionClosedError): await r.owner.start()
    asyncio.run(exercise())


def test_injected_retrieval_scopes_ui_identity_without_eager_reads(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch', 'prepare_session'), ('ui',))
    retrieve = AsyncMock(return_value='separate-username')
    r.owner.user_index = 3
    r.owner.credentials_for = lambda roles, index: ScopedRoleCredentials(roles, index, retrieve)
    async def prepare(*, session, user_index, credentials):
        retrieve.assert_not_awaited()
        assert await credentials('ui', 3, 'username') == 'separate-username'
        with pytest.raises(ContractError): await credentials('llm', 3, 'password')
        with pytest.raises(ContractError): await credentials('ui', 4, 'username')
    r.implementation.aprepare_session.side_effect = prepare
    async def exercise():
        await r.owner.start()
        retrieve.assert_awaited_once_with('ui', 3, 'username')
        await r.owner.aclose()
    asyncio.run(exercise())


def test_anonymous_never_reads_injected_credentials(resources):
    r = resources
    r.owner.credentials_for = Mock()
    async def exercise():
        await r.owner.start()
        r.owner.credentials_for.assert_not_called()
        await r.owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('roles,index,expected', [(('ui',), 0, True), (('ui',), True, False),
    (('ui',), 1, False), (('other',), 0, False)])
def test_public_credential_binding_predicate(roles, index, expected):
    reader = ScopedRoleCredentials(('ui',), 0, AsyncMock())
    assert reader.matches_binding(roles, index) is expected


def test_ambiguous_credential_sources_fail_before_launch(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch',), ('ui',))
    r.owner.user_index = 0
    r.owner.credentials = ScopedRoleCredentials(('ui',), 0, AsyncMock())
    r.owner.credentials_for = Mock()
    async def exercise():
        with pytest.raises(ContractError, match='Ambiguous'): await r.owner.start()
        r.provider.astart.assert_not_awaited()
        r.owner.credentials_for.assert_not_called()
    asyncio.run(exercise())


def test_configured_profile_binder_failure_prevents_launch(resources):
    r = resources
    r.implementation.capabilities = ProviderCapabilities('ui', ('launch',), ('ui',))
    r.owner.user_index = 2
    r.owner.credentials_for = Mock(side_effect=ContractError('Selected account unavailable'))
    async def exercise():
        with pytest.raises(ContractError, match='unavailable'): await r.owner.start()
        r.owner.credentials_for.assert_called_once_with(('ui',), 2)
        r.provider.astart.assert_not_awaited()
        r.browser_owner.start.assert_not_awaited()
    asyncio.run(exercise())


def test_default_loader_executes_real_provider_in_creating_loop(resources, tmp_path):
    """Injected loader mocks must not conceal async-loader restrictions."""
    from api_agent.providers.loading import aload_provider
    source = tmp_path / 'ui.py'
    source.write_text('''from api_agent.providers.contracts import ProviderCapabilities
class Provider:
    version=1
    capabilities=ProviderCapabilities('ui',('launch',))
    async def astart(self): pass
    async def aclose(self): pass
def create(config,services): return Provider()
''')
    r = resources
    owner = PreparedUISession({'UI_PROVIDER':str(source)+':create'}, {}, browser_owner=r.browser_owner)
    async def exercise():
        assert owner.loader is aload_provider
        assert await owner.start() is r.session
        await owner.aclose()
        assert owner.provider.disposed
    asyncio.run(exercise())


def shortened_budget(monkeypatch, target):
    from contextlib import asynccontextmanager
    from api_agent.providers import progress
    original = progress.report_timeout
    @asynccontextmanager
    async def short(name, seconds):
        async with original(name, .02 if name == target else seconds) as budget:
            yield budget
    monkeypatch.setattr('api_agent.ui.session.report_timeout', short)


def test_cleanup_timeout_retains_live_task_until_retry(resources, monkeypatch):
    r = resources
    shortened_budget(monkeypatch, 'UI session cleanup')
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        r.browser_owner.aclose.side_effect = close
        await r.owner.start()
        with pytest.raises(CleanupPendingError) as raised:
            await asyncio.wait_for(r.owner.aclose(), .5)
        assert entered.is_set() and not r.owner._release_task.done()
        r.provider.aclose.assert_not_awaited()
        release.set()
        await raised.value.retry()
        r.browser_owner.aclose.assert_awaited_once()
        r.provider.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_setup_timeout_has_independent_cleanup_and_direct_receipt(resources, monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.providers import progress
    r = resources
    original = progress.component_timeout
    @asynccontextmanager
    async def short(name, seconds):
        async with original(name, .02 if name == 'UI session preparation' else seconds) as budget:
            yield budget
    monkeypatch.setattr('api_agent.ui.session.component_timeout', short)
    r.provider.astart.side_effect = lambda: None
    async def exercise():
        release = asyncio.Event()
        async def launch(): await release.wait()
        r.provider.astart.side_effect = launch
        r.browser_owner.aclose.side_effect = [RuntimeError('controlled'), None]
        with pytest.raises(TimeoutError) as raised:
            await asyncio.wait_for(r.owner.start(), .5)
        r.browser_owner.aclose.assert_awaited_once()
        assert raised.value.cleanup_pending is not None
        await raised.value.cleanup_pending.retry()
        r.provider.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_failed_loader_owners_are_part_of_cleanup_retry(resources):
    r = resources
    retained = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('controlled'), None]))
    r.loader.side_effect = ProviderError('provider failed', retained_owners=(retained,))
    async def exercise():
        with pytest.raises(ProviderError) as raised:
            await r.owner.start()
        assert raised.value.cleanup_pending is not None
        r.runtime.aclose.assert_not_awaited()
        await raised.value.cleanup_pending.retry()
        assert retained.aclose.await_count == 2
        r.runtime.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_cancelling_waiting_start_does_not_dispose_active_preparation(resources):
    r=resources
    async def exercise():
        entered,release=asyncio.Event(),asyncio.Event()
        async def launch():
            entered.set()
            await release.wait()
        r.provider.astart.side_effect=launch
        first=asyncio.create_task(r.owner.start())
        await entered.wait()
        waiter=asyncio.create_task(r.owner.start())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        assert not first.done()
        r.provider.aclose.assert_not_awaited()
        r.browser_owner.aclose.assert_not_awaited()
        release.set()
        assert await first is r.session
        await r.owner.aclose()
    asyncio.run(exercise())


def test_repeated_close_does_not_recancel_preparation_drain(resources,monkeypatch):
    r=resources
    shortened_budget(monkeypatch,'UI preparation cancellation')
    async def exercise():
        entered,cancelled,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        cancellations=[]
        async def launch():
            entered.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(1)
                cancelled.set()
                try: await release.wait()
                except asyncio.CancelledError:
                    cancellations.append(2)
                    raise
                raise
        r.provider.astart.side_effect=launch
        starter=asyncio.create_task(r.owner.start())
        await entered.wait()
        with pytest.raises(CleanupPendingError):await r.owner.aclose()
        await cancelled.wait()
        with pytest.raises(CleanupPendingError):await r.owner.aclose()
        assert cancellations==[1] and not starter.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await starter
        await r.owner.aclose()
    asyncio.run(exercise())


def test_explicit_retained_setup_failure_is_captured_before_disposal(resources):
    r=resources
    r.owner.retain_on_failure=True
    r.provider.astart.side_effect=RuntimeError('controlled setup failure')
    async def exercise():
        with pytest.raises(RuntimeError) as caught: await r.owner.start()
        assert caught.value.retained_owners==(r.owner,)
        r.browser_owner.aclose.assert_not_awaited()
        r.provider.aclose.assert_not_awaited()
        await caught.value.retained_owners[0].aclose()
        r.provider.aclose.assert_awaited_once()
    asyncio.run(exercise())
